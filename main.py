"""统一入口：拆分工单 → 重排新旧工单（完整流程）。

各步骤的职责归档到三个模块，本文件只做编排：
    split_work_orders.split_all     拆单：计划报文 → 工单 + 明细
    rework_orders.collect_rework_set 取旧工单 → 过滤归一化 → 合并待排集合
    rework_algorithm.rework         重排：新工单插入 + 旧工单链式挪动 + 人工错峰

用法：
    python main.py                                    # 默认 mock 输入，在线拉旧单
    python main.py --existing-file out/existing_orders.json
    python main.py --save-existing out/existing_orders.json
    python main.py --no-enforce-quota                 # 关闭 80% 日配额约束
"""

from __future__ import annotations

import argparse
import json
import logging
from collections import defaultdict
from pathlib import Path
from typing import Optional

from rework_algorithm import rework
from rework_orders import ApiError, collect_rework_set

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_INPUT = BASE_DIR / "mock_data" / "split_input.json"
DEFAULT_OUT = BASE_DIR / "out" / "result.json"

# ---- 旧工单展示用的中文枚举（示例字段） ----
PROFESSION_ZH = {"tms": "输电", "dms": "配电", "sms": "变电"}
WORK_NATURE_ZH = {"1": "本体", "101": "本体", "2": "通道"}
INFRARED_SUBS = {"s-hwxs", "t-hwxs", "d-hwxs"}

# ---- 输出结构：专业排序（变电1/输电2/配电3）与 entry 名映射 ----
SORT_TYPE = {"sms": "1", "tms": "2", "dms": "3"}
ENTRY_MAP = {"normal": "normal", "insert_rework": "insertRework", "force_insert": "forceInsert"}


def _detail_items(order: dict, detail_map: dict) -> list[dict]:
    """新工单明细：设备集（不是全计划设备）。"""
    plan = order.get("plan") or {}
    out = []
    for detail in detail_map.get(order.get("work_order_guid"), []):
        device = detail.get("device") or {}
        out.append({
            "workOrderGuid": order.get("work_order_guid"),
            "parentDeviceGuid": device.get("jobObjectGuid") or plan.get("jobObjectGuid"),
            "deviceGuid": device.get("jobObjectDeviceGuid"),
            "deviceType": device.get("speciality") or plan.get("speciality"),
            "workNature": plan.get("workNature"),
            "deviceName": device.get("jobObjectDeviceName"),
            "parentDeviceName": device.get("jobObjectName") or plan.get("jobObject"),
        })
    return out


def _work_obj_name(plan: dict, details: list[dict]) -> str:
    """规则：线路名称:起始杆名称-结束杆名称。"""
    line = plan.get("jobObject") or ""
    names = [d["deviceName"] for d in details if d.get("deviceName")]
    if not names:
        return line
    if len(names) == 1:
        return f"{line}:{names[0]}"
    return f"{line}:{names[0]}-{names[-1]}"


def _new_order_item(order: dict, detail_map: dict, airports: dict) -> dict:
    """按新版输出结构，把拆单侧 snake_case 工单转成 camelCase 展示结构。"""
    plan = order.get("plan") or {}
    airport = airports.get(order.get("cycle_airport_guid")) if order.get("cycle_airport_guid") else {}
    details = _detail_items(order, detail_map)
    speciality = plan.get("speciality") or ""
    return {
        "workOrderGuid": order.get("work_order_guid"),
        "workOrderNo": order.get("work_order_no"),
        "planGuid": order.get("plan_guid") or plan.get("planGuid"),
        "inspectionMethod": order.get("inspection_method"),
        "orderSource": order.get("order_source") or "5",
        "workOrderStatus": order.get("work_order_status"),
        "cycleAirportGuid": order.get("cycle_airport_guid"),
        "cycleParentDeviceGuid": plan.get("jobObjectGuid"),
        "snCode": airport.get("airportSn"),
        "equipName": airport.get("airportName"),
        "workObjName": _work_obj_name(plan, details),
        "professionalCategory": speciality,
        "sortType": SORT_TYPE.get(speciality, ""),
        "cycleCnt": plan.get("cycleCnt"),
        "oneDay": plan.get("oneDay"),
        "cycleVoltageLevel": plan.get("voltageLevel"),
        "workNature": plan.get("workNature"),
        "workNatureSub": plan.get("workNatureSub"),
        "parentDeviceNum": order.get("parent_device_num"),
        "secondNum": order.get("second_num"),
        "capacityLimit": order.get("capacity_limit"),
        "scheduleStatus": order.get("schedule_status"),
        "scheduleReason": order.get("schedule_reason"),
        "preStartDate": order.get("pre_start_date"),
        "cycleStartDate": plan.get("cycleStartDate"),
        "preEndDate": order.get("pre_end_date"),
        "cycleEndDate": plan.get("cycleEndDate"),
        "timePeriod": order.get("time_period"),
        "startDate": order.get("start_date"),
        "endDate": order.get("end_date"),
        "projectId": plan.get("projectId"),
        "regionCode": plan.get("areaCode"),
        "trackType": plan.get("trackType"),
        "projectName": plan.get("projectName"),
        "details": details,
    }


def _force_proposal_out(proposal) -> dict:
    return {
        "orderGuid": proposal.order_guid,
        "workOrderNo": proposal.work_order_no,
        "reason": proposal.reason,
        "protectedHolders": proposal.protected_holders,
        "affectedOrders": [
            {"workOrderGuid": a.get("work_order_guid"),
             "workOrderNo": a.get("work_order_no"),
             "planGuid": a.get("plan_guid"),
             "relation": a.get("relation")}
            for a in proposal.affected_orders
        ],
        "updateProposals": proposal.update_proposals,
    }


def _impact_chains(payload: dict, pending, result) -> dict:
    """从 rework_steps 还原「谁影响谁」的影响链，与新/旧工单信息回组。"""
    new_by_guid = {o.get("work_order_guid"): o for o in result.new_orders_out}
    old_by_guid = {}
    for order in pending.old_orders:
        raw = dict(order.get("raw") or order)
        guid = order.get("work_order_guid") or raw.get("workOrderGuid")
        old_by_guid[guid] = raw

    chains: list[dict] = []
    current = None
    for step in result.rework_steps:
        if step.preempted_by in new_by_guid:
            current = {"root": new_by_guid[step.preempted_by], "nodes": []}
            chains.append(current)
        if current is None:
            continue
        raw = old_by_guid.get(step.moved_guid) or {}
        current["nodes"].append({
            "guid": step.moved_guid,
            "no": raw.get("workOrderNo") or step.moved_guid,
        })

    plan_set_name = (payload.get("planSetInfo") or {}).get("planSetName")
    list_out = []
    for chain in chains:
        root = chain["root"]
        plan = root.get("plan") or {}
        first = chain["nodes"][0] if chain["nodes"] else {"guid": None}
        raw = old_by_guid.get(first["guid"]) or {}
        node_nos = [n["no"] for n in chain["nodes"]][:3]
        list_out.append({
            "planSetName": plan_set_name,
            "jobObject": plan.get("jobObject"),
            "frequency": plan.get("inspectionFrequency"),
            "workObjName": raw.get("equipName") or plan.get("jobObject"),
            "planGuid": plan.get("planGuid"),
            "planNo": plan.get("planNo"),
            "workOrderGuid": raw.get("workOrderGuid") or first["guid"],
            "workOrderNo": raw.get("workOrderNo"),
            "cycleAirportGuid": raw.get("cycleAirportGuid"),
            "secondNum": raw.get("secondNum"),
            "workOrderStatus": raw.get("workOrderStatus"),
            "startDate": raw.get("startDate"),
            "endDate": raw.get("endDate"),
            "preStartDate": raw.get("startDate"),
            "preEndDate": raw.get("endDate"),
            "firstWorkOrderNo": node_nos[0] if len(node_nos) >= 1 else None,
            "secondWorkOrderNo": node_nos[1] if len(node_nos) >= 2 else None,
            "thirdWorkOrderNo": node_nos[2] if len(node_nos) >= 3 else None,
            "result": "连续重排成功" if result.entry == "insert_rework" else "连续重排失败",
        })

    direct = len(chains)
    indirect = max(0, sum(len(c["nodes"]) for c in chains) - direct)
    return {
        "stats": {
            "chainCount": len(chains),
            "directCount": direct,
            "indirectCount": indirect,
        },
        "list": list_out,
    }


def _build_output(payload: dict, pending, result) -> dict:
    """按输入报文格式回组：工单按计划嵌套到 planList，旧工单平级另放。"""
    airports = {str(a.get("airportGuid")): a for a in payload.get("airports") or []}
    detail_map: dict[str, list[dict]] = defaultdict(list)
    for detail in pending.details:
        detail_map[detail["work_order_guid"]].append(detail)

    plan_orders: dict[int, list[dict]] = defaultdict(list)
    for order in result.new_orders_out:
        plan_orders[id(order.get("plan"))].append(_new_order_item(order, detail_map, airports))

    strategies_out = []
    total_plans = 0
    total_orders = 0
    for strategy in payload.get("planStrategyList") or []:
        plan_list_out = []
        strategy_orders = 0
        for plan in strategy.get("planList") or []:
            orders = plan_orders.get(id(plan), [])
            plan_out = dict(plan)
            plan_out["workOrders"] = orders
            plan_out["workOrderCount"] = len(orders)
            plan_list_out.append(plan_out)
            strategy_orders += len(orders)
            total_plans += 1
        strategy_out = dict(strategy)
        strategy_out["planList"] = plan_list_out
        strategy_out["planCount"] = len(plan_list_out)
        strategy_out["workOrderCount"] = strategy_orders
        strategies_out.append(strategy_out)
        total_orders += strategy_orders

    plan_set_out = dict(payload.get("planSetInfo") or {})
    # planStrategyList 是计划包列表，数量=len；planList 是计划列表，数量=汇总的 len
    plan_set_out["planPackageCount"] = len(strategies_out)
    plan_set_out["planCount"] = total_plans
    plan_set_out["workOrderCount"] = total_orders
    plan_set_out["workOrderCountAir"] = sum(
        1 for o in result.new_orders_out if o.get("inspection_method") == "1")
    plan_set_out["workOrderCountUser"] = sum(
        1 for o in result.new_orders_out if o.get("inspection_method") == "2")

    # ---- 旧工单回组：换成带计划信息、中文枚举、影响轮次与结果的展示结构 ----
    plan_by_guid = {}
    for strategy in payload.get("planStrategyList") or []:
        for plan in strategy.get("planList") or []:
            plan_by_guid[plan.get("planGuid")] = plan
    payload_plan_set_name = (payload.get("planSetInfo") or {}).get("planSetName")

    new_guids = {o.get("work_order_guid") for o in result.new_orders_out}
    moved_meta: dict[str, dict] = {}
    round_no = 1
    for step in result.rework_steps:
        if step.preempted_by in new_guids:
            round_no = 1
        moved_meta[step.moved_guid] = {
            "round": round_no,
            "descType": "直接影响" if round_no == 1 else "间接影响",
            "old_start": step.old_start,
            "old_end": step.old_end,
            "new_start": step.new_start,
            "new_end": step.new_end,
        }
        round_no += 1
    continues = {step.preempted_by for step in result.rework_steps}

    update_map = {u["work_order_guid"]: u for u in result.update_orders}
    old_orders_out = []
    for order in pending.old_orders:
        raw = dict(order.get("raw") or order)
        guid = order.get("work_order_guid") or raw.get("workOrderGuid")
        plan = plan_by_guid.get(raw.get("planGuid")) or {}
        update = update_map.get(guid)
        meta = moved_meta.get(guid)
        work_nature_sub = raw.get("workNatureSub") or ""
        # 旧工单接口会直接返回 planNo / planSetName，优先用回传值，兼容老数据回退匹配
        plan_set_name = raw.get("planSetName") or payload_plan_set_name
        plan_no = raw.get("planNo") or plan.get("planNo")

        old_start = (meta or {}).get("old_start") or raw.get("startDate")
        old_end = (meta or {}).get("old_end") or raw.get("endDate")
        if update:
            start_date = update.get("start_date", raw.get("startDate"))
            end_date = update.get("end_date", raw.get("endDate"))
        elif meta:
            start_date = meta.get("new_start")
            end_date = meta.get("new_end")
        else:
            start_date = raw.get("startDate")
            end_date = raw.get("endDate")

        if meta:
            desc = f"第 {meta['round']} 轮"
            desc_type = meta["descType"]
            result_txt = "已排妥"
            result_desc = "继续影响下一张旧单" if guid in continues else "已排妥"
        else:
            desc = ""
            desc_type = ""
            result_txt = result_desc = "未调整"

        cycle_airport = (update.get("cycle_airport_guid") if update else raw.get("cycleAirportGuid"))

        old_orders_out.append({
            "planSetName": plan_set_name,
            "jobObject": plan.get("jobObject"),
            "frequency": plan.get("inspectionFrequency"),
            "workObjName": raw.get("equipName") or plan.get("jobObject"),
            "planGuid": raw.get("planGuid"),
            "planNo": plan_no,
            "workOrderGuid": guid,
            "workOrderNo": raw.get("workOrderNo"),
            "professionalCategoryZh": PROFESSION_ZH.get(raw.get("professionalCategory"),
                                                         raw.get("professionalCategory") or ""),
            "workNatureZh": WORK_NATURE_ZH.get(str(raw.get("workNature")),
                                               raw.get("workNature") or ""),
            "dayOrNightZh": "红外" if work_nature_sub in INFRARED_SUBS else "可见光",
            "desc": desc,
            "descType": desc_type,
            "equipName": raw.get("equipName"),
            "cycleAirportGuid": cycle_airport,
            "secondNum": raw.get("secondNum"),
            "workOrderStatus": raw.get("workOrderStatus"),
            "oldStartDate": old_start,
            "oldEndDate": old_end,
            "startDate": start_date,
            "endDate": end_date,
            "result": result_txt,
            "resultDesc": result_desc,
        })
    logger.info("回组输出 计划包=%s 计划=%s 新工单=%s 旧工单=%s 入口=%s",
                len(strategies_out), total_plans, total_orders,
                len(old_orders_out), result.entry)

    return {
        "planSetInfo": plan_set_out,
        "planStrategyList": strategies_out,
        "oldWorkOrders": old_orders_out,
        "impactChains": _impact_chains(payload, pending, result),
        "entry": ENTRY_MAP.get(result.entry, result.entry),
        "forceProposals": [_force_proposal_out(p) for p in result.force_proposals],
        "warnings": result.warnings,
    }


def run_pipeline(payload: dict, existing_rows: Optional[list[dict]] = None, *,
                 enforce_quota: bool = True,
                 save_existing: Optional[str] = None,
                 skip_weekdays: Optional[list[int]] = None,
                 holidays: Optional[list[str]] = None) -> dict:
    """拆单 → 拉旧单合并 → 重排，按输入报文格式回组结果（不落盘）。"""
    logger.info("整流程开始 配额约束=%s 跳过星期=%s 节假日=%s 离线旧单=%s",
                enforce_quota, skip_weekdays, holidays,
                "是" if existing_rows is not None else "否")
    pending = collect_rework_set(payload, existing_rows,
                                 save_existing=save_existing)
    result = rework(pending.new_orders, pending.old_orders,
                    pending.window["start"], pending.window["end"],
                    enforce_quota=enforce_quota,
                    skip_weekdays=skip_weekdays, holidays=holidays)
    return _build_output(payload, pending, result)


def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s - %(message)s")
    parser = argparse.ArgumentParser(description="拆分工单 → 重排新旧工单（完整流程）")
    parser.add_argument("--input", default=str(DEFAULT_INPUT), help="拆单输入报文")
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="完整流程结果输出")
    parser.add_argument("--existing-file", help="离线模式：用本地 JSON 代替旧工单接口")
    parser.add_argument("--save-existing", help="把在线拉到的旧工单原样存到该文件")
    parser.add_argument("--enforce-quota", action=argparse.BooleanOptionalAction, default=True,
                        help="按 80%% 日配额约束排班（默认开；--no-enforce-quota 关闭）")
    parser.add_argument("--skip-weekday", default="", help="跳过星期，逗号分隔，1=周一..7=周日")
    parser.add_argument("--holiday", default="", help="跳过节假日，逗号分隔的 YYYY-MM-DD")
    args = parser.parse_args()

    payload = json.loads(Path(args.input).read_text(encoding="utf-8"))
    existing_rows = None
    if args.existing_file:
        existing_rows = json.loads(Path(args.existing_file).read_text(encoding="utf-8"))
    skip_weekdays = [int(x) for x in args.skip_weekday.split(",") if x.strip().isdigit()]
    holidays = [x.strip() for x in args.holiday.split(",") if x.strip()]
    try:
        out = run_pipeline(payload, existing_rows,
                           enforce_quota=args.enforce_quota,
                           save_existing=args.save_existing if existing_rows is None else None,
                           skip_weekdays=skip_weekdays, holidays=holidays)
    except ApiError as exc:
        print(f"接口获取失败：{exc}")
        print("可用 --existing-file 离线模式，或检查 MAIN_CYCLE_TOKEN / MAIN_CYCLE_AUTH_HEADER")
        raise SystemExit(2)
    except ValueError as exc:
        print(str(exc))
        raise SystemExit(1)

    ps = out["planSetInfo"]
    print(f"入口：{out['entry']}")
    print(f"计划包 {ps.get('planPackageCount')} / 计划 {ps.get('planCount')} / 新工单 {ps.get('workOrderCount')}"
          f" / 旧工单 {len(out['oldWorkOrders'])}")
    detail_count = sum(len(o.get("details", []))
                       for s in out["planStrategyList"]
                       for p in s["planList"]
                       for o in p.get("workOrders", []))
    print(f"明细 {detail_count} 条")

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"已写出：{out_path}")


if __name__ == "__main__":
    main()
