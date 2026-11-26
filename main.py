"""工单算法调试入口。业务流程由 Agent 包内的 service 实现。"""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

from agents.work_order_scheduling.split_and_schedule.tools.rework_orders import ApiError
from agents.work_order_scheduling.split_and_schedule.service import run_pipeline

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_INPUT = BASE_DIR / "mock_data" / "split_input.json"
DEFAULT_OUT = BASE_DIR / "out" / "result.json"


def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s - %(message)s")
    parser = argparse.ArgumentParser(description="拆分工单 → 重排新旧工单（完整流程）")
    parser.add_argument("--input", default=str(DEFAULT_INPUT), help="拆单输入报文")
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="完整流程结果输出")
    parser.add_argument("--existing-file", help="离线模式：用本地 JSON 代替旧工单接口")
    parser.add_argument("--save-existing", help="把在线拉到的旧工单原样另存")
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

    plan_set = out["planSetInfo"]
    print(f"入口：{out['entry']}")
    print(f"计划包 {plan_set.get('planPackageCount')} / 计划 {plan_set.get('planCount')} "
          f"/ 新工单 {plan_set.get('workOrderCount')} / 旧工单 {len(out['oldWorkOrders'])}")
    detail_count = sum(len(order.get("details", []))
                       for strategy in out["planStrategyList"]
                       for plan in strategy["planList"]
                       for order in plan.get("workOrders", []))
    print(f"明细 {detail_count} 条")

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"已写出：{out_path}")


if __name__ == "__main__":
    main()
