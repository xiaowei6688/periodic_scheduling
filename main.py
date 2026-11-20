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


def _attach_details(new_orders: list[dict], details: list[dict]) -> list[dict]:
    """把拆分明细挂到对应新工单上，保持工单自包含。"""
    detail_map: dict[str, list[dict]] = defaultdict(list)
    for detail in details:
        detail_map[detail["work_order_guid"]].append(detail)
    out = []
    for order in new_orders:
        item = dict(order)
        item["details"] = detail_map.get(item.get("work_order_guid"), [])
        out.append(item)
    return out


def _build_output(payload: dict, pending, result) -> dict:
    """按输入报文格式回组：工单按计划嵌套到 planList，旧工单平级另放。"""
    new_orders = _attach_details(result.new_orders_out, pending.details)
    plan_orders: dict[int, list[dict]] = defaultdict(list)
    for order in new_orders:
        plan_orders[id(order.get("plan"))].append(order)

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

    update_map = {u["work_order_guid"]: u for u in result.update_orders}
    old_orders_out = []
    for order in pending.old_orders:
        raw = dict(order.get("raw") or order)
        update = update_map.get(raw.get("workOrderGuid") or order.get("work_order_guid"))
        if update:
            raw["startDate"] = update.get("start_date", raw.get("startDate"))
            raw["endDate"] = update.get("end_date", raw.get("endDate"))
            if "cycle_airport_guid" in update:
                raw["cycleAirportGuid"] = update["cycle_airport_guid"]
        old_orders_out.append(raw)
    logger.info("回组输出 计划包=%s 计划=%s 新工单=%s 旧工单=%s 入口=%s",
                len(strategies_out), total_plans, total_orders,
                len(old_orders_out), result.entry)

    return {
        "planSetInfo": plan_set_out,
        "planStrategyList": strategies_out,
        "oldWorkOrders": old_orders_out,
        "entry": result.entry,
        "forceProposals": [p.__dict__ for p in result.force_proposals],
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
