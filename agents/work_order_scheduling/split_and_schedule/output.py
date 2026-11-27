"""把拆单和排班结果组装成业务平台需要的输出结构。"""

from __future__ import annotations

import logging
from collections import defaultdict

logger = logging.getLogger(__name__)

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
        "forceInserted": bool(order.get("force_inserted")),
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
        "feasible": proposal.feasible,
        "proposedStartDate": proposal.proposed_start,
        "proposedEndDate": proposal.proposed_end,
        "forceTimePeriod": proposal.force_time_period,
        "relaxedRules": proposal.relaxed_rules,
        "recommendationReason": proposal.recommendation_reason,
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
    normalized_old_by_guid = {}
    for order in pending.old_orders:
        raw = dict(order.get("raw") or order)
        guid = order.get("work_order_guid") or raw.get("workOrderGuid")
        old_by_guid[guid] = raw
        normalized_old_by_guid[guid] = order

    chains: list[dict] = []
    current = None
    for step in result.rework_steps:
        if step.preempted_by in new_by_guid:
            current = {"root": new_by_guid[step.preempted_by], "nodes": []}
            chains.append(current)
        if current is None:
            continue
        raw = old_by_guid.get(step.moved_guid) or {}
        normalized = normalized_old_by_guid.get(step.moved_guid) or {}
        allowed_start = normalized.get("pre_start_date") or normalized.get("start_date")
        allowed_end = normalized.get("pre_end_date") or normalized.get("end_date")
        within_allowed = bool(
            allowed_start and allowed_end
            and step.new_start >= allowed_start
            and step.new_end <= allowed_end
        )
        current["nodes"].append({
            "guid": step.moved_guid,
            "no": raw.get("workOrderNo") or step.moved_guid,
            "planSetName": raw.get("planSetName"),
            "planGuid": raw.get("planGuid"),
            "planNo": raw.get("planNo"),
            "jobObject": raw.get("cycleParentDeviceName") or raw.get("jobObject"),
            "workObjName": raw.get("cycleParentDeviceName") or raw.get("workObjName"),
            "professionalCategory": raw.get("professionalCategory"),
            "professionalCategoryZh": PROFESSION_ZH.get(
                raw.get("professionalCategory"), raw.get("professionalCategory") or ""),
            "workNature": raw.get("workNature"),
            "workNatureZh": WORK_NATURE_ZH.get(
                str(raw.get("workNature")), raw.get("workNature") or ""),
            "dayOrNightZh": (
                "红外" if (raw.get("workNatureSub") or "") in INFRARED_SUBS else "可见光"),
            "equipName": raw.get("equipName"),
            "cycleAirportGuid": raw.get("cycleAirportGuid"),
            "secondNum": raw.get("secondNum"),
            "workOrderStatus": raw.get("workOrderStatus"),
            "preemptedBy": step.preempted_by,
            "oldStartDate": step.old_start,
            "oldEndDate": step.old_end,
            "startDate": step.new_start,
            "endDate": step.new_end,
            "allowedStartDate": allowed_start,
            "allowedEndDate": allowed_end,
            "timePeriod": normalized.get("time_period") or raw.get("timePeriod"),
            "withinAllowedRange": within_allowed,
        })

    plan_set_name = (payload.get("planSetInfo") or {}).get("planSetName")
    list_out = []
    for chain in chains:
        root = chain["root"]
        plan = root.get("plan") or {}
        plan_start = plan.get("preStartDate") or plan.get("cycleStartDate")
        plan_end = plan.get("preEndDate") or plan.get("cycleEndDate")
        first = chain["nodes"][0] if chain["nodes"] else {"guid": None}
        raw = old_by_guid.get(first["guid"]) or {}
        node_nos = [n["no"] for n in chain["nodes"]][:3]
        completed = result.entry == "insert_rework" and bool(chain["nodes"])
        nodes = [
            {
                **node,
                "round": index,
                "relation": "direct" if index == 1 else "indirect",
                "result": "已排妥" if completed else "未排妥",
                "resultDesc": (
                    "继续影响下一张旧单"
                    if index < len(chain["nodes"])
                    else ("已排妥" if completed else "未排妥")
                ),
            }
            for index, node in enumerate(chain["nodes"], start=1)
        ]
        all_within_allowed = bool(nodes) and all(
            node["withinAllowedRange"] for node in nodes
        )
        list_out.append({
            "planSetName": plan_set_name,
            "jobObject": plan.get("jobObject"),
            "frequency": plan.get("inspectionFrequency"),
            "workObjName": raw.get("equipName") or plan.get("jobObject"),
            "planGuid": plan.get("planGuid"),
            "planNo": plan.get("planNo"),
            "planStartDate": plan_start,
            "planEndDate": plan_end,
            "timePeriod": plan.get("timePeriod"),
            "scheduledStartDate": root.get("start_date"),
            "scheduledEndDate": root.get("end_date"),
            "rootWorkOrderGuid": root.get("work_order_guid"),
            "rootWorkOrderNo": root.get("work_order_no"),
            "workOrderGuid": raw.get("workOrderGuid") or first["guid"],
            "workOrderNo": raw.get("workOrderNo"),
            "cycleAirportGuid": raw.get("cycleAirportGuid"),
            "secondNum": raw.get("secondNum"),
            "workOrderStatus": raw.get("workOrderStatus"),
            "startDate": raw.get("startDate"),
            "endDate": raw.get("endDate"),
            "preStartDate": plan_start,
            "preEndDate": plan_end,
            "firstWorkOrderNo": node_nos[0] if len(node_nos) >= 1 else None,
            "secondWorkOrderNo": node_nos[1] if len(node_nos) >= 2 else None,
            "thirdWorkOrderNo": node_nos[2] if len(node_nos) >= 3 else None,
            "roundCount": len(nodes),
            "nodes": nodes,
            "completed": completed,
            "allPlacedWithinWindow": all_within_allowed,
            "result": "连续重排成功" if completed else "连续重排失败",
        })

    direct = len(chains)
    indirect = max(0, sum(len(c["nodes"]) for c in chains) - direct)
    round_counts = [item["roundCount"] for item in list_out]
    return {
        "stats": {
            "chainCount": len(chains),
            "directCount": direct,
            "indirectCount": indirect,
            "updatedOldCount": direct + indirect,
            "totalRounds": sum(round_counts),
            "roundCounts": round_counts,
            "allChainsCompleted": bool(list_out) and all(
                item["completed"] for item in list_out
            ),
            "allPlacedWithinWindow": bool(list_out) and all(
                item["allPlacedWithinWindow"] for item in list_out
            ),
        },
        "list": list_out,
    }


def _build_output(payload: dict, pending, result) -> dict:
    """按输入报文格式回组：工单按计划嵌套到 planList，旧工单平级另放。"""
    airports = {str(a.get("airportGuid")): a for a in payload.get("airports") or []}
    detail_map: dict[str, list[dict]] = defaultdict(list)
    for detail in pending.details:
        detail_map[detail["work_order_guid"]].append(detail)

    plan_orders: dict[str, list[dict]] = defaultdict(list)
    for order in result.new_orders_out:
        plan_orders[str(order.get("plan_guid") or "")].append(
            _new_order_item(order, detail_map, airports))

    strategies_out = []
    total_plans = 0
    total_orders = 0
    for strategy in payload.get("planStrategyList") or []:
        plan_list_out = []
        strategy_orders = 0
        for plan in strategy.get("planList") or []:
            orders = plan_orders.get(str(plan.get("planGuid") or ""), [])
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

        job_object = (
            raw.get("cycleParentDeviceName")
            or raw.get("jobObject")
            or plan.get("jobObject")
        )

        old_orders_out.append({
            "planSetName": plan_set_name,
            "jobObject": job_object,
            "frequency": raw.get("inspectionFrequency") or plan.get("inspectionFrequency"),
            "workObjName": raw.get("workObjName") or job_object,
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
            "cycleAirportGuid": raw.get("cycleAirportGuid"),
            "secondNum": raw.get("secondNum"),
            "workOrderStatus": raw.get("workOrderStatus"),
            "preStartDate": raw.get("preStartDate"),
            "preEndDate": raw.get("preEndDate"),
            "timePeriod": raw.get("timePeriod"),
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


