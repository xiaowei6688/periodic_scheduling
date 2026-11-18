"""统一入口：拆分工单 → 重排新旧工单（完整流程）。

各步骤的职责归档到三个模块，本文件只做编排：
    split_work_orders.split_all     拆单：计划报文 → 工单 + 明细
    rework_orders.collect_rework_set 取旧工单 → 过滤归一化 → 合并待排集合
    rework_algorithm.rework         重排：新工单插入 + 旧工单链式挪动 + 人工错峰

用法：
    python main.py                                    # 默认 mock 输入，在线拉旧单
    python main.py --existing-file out/existing_orders.json
    python main.py --save-existing out/existing_orders.json
    python main.py --enforce-quota
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Optional

from rework_algorithm import rework
from rework_orders import ApiError, collect_rework_set

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_INPUT = BASE_DIR / "mock_data" / "split_input.json"
DEFAULT_OUT = BASE_DIR / "out" / "result.json"


def run_pipeline(payload: dict, existing_rows: Optional[list[dict]] = None, *,
                 enforce_quota: bool = False,
                 save_existing: Optional[str] = None) -> dict:
    """拆单 → 拉旧单合并 → 重排，返回完整结果 dict（不落盘）。"""
    pending = collect_rework_set(payload, existing_rows,
                                 save_existing=save_existing)
    result = rework(pending.new_orders, pending.old_orders,
                    pending.window["start"], pending.window["end"],
                    enforce_quota=enforce_quota)
    return {
        "summary": pending.summary(),
        "work_order_details": pending.details,
        "rework": result.to_dict(),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="拆分工单 → 重排新旧工单（完整流程）")
    parser.add_argument("--input", default=str(DEFAULT_INPUT), help="拆单输入报文")
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="完整流程结果输出")
    parser.add_argument("--existing-file", help="离线模式：用本地 JSON 代替旧工单接口")
    parser.add_argument("--save-existing", help="把在线拉到的旧工单原样存到该文件")
    parser.add_argument("--enforce-quota", action="store_true", help="按 80%% 日配额约束排班")
    args = parser.parse_args()

    payload = json.loads(Path(args.input).read_text(encoding="utf-8"))
    existing_rows = None
    if args.existing_file:
        existing_rows = json.loads(Path(args.existing_file).read_text(encoding="utf-8"))
    try:
        out = run_pipeline(payload, existing_rows,
                           enforce_quota=args.enforce_quota,
                           save_existing=args.save_existing if existing_rows is None else None)
    except ApiError as exc:
        print(f"接口获取失败：{exc}")
        print("可用 --existing-file 离线模式，或检查 MAIN_CYCLE_TOKEN / MAIN_CYCLE_AUTH_HEADER")
        raise SystemExit(2)
    except ValueError as exc:
        print(str(exc))
        raise SystemExit(1)

    rw = out["rework"]
    print(f"入口：{rw['entry']}")
    summary = out["summary"]
    print(f"待排集合：{summary['total']} 张（新 {summary['new_orders']} + 旧 {summary['old_orders']}）"
          f" / 明细 {len(out['work_order_details'])} 条")
    print(f"重排：新单排入 {len(rw['new']) - len(rw['unplaced_new'])}"
          f" / 挪动旧单 {len(rw['update'])} / 未排下 {len(rw['unplaced_new'])}")

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"已写出：{out_path}")


if __name__ == "__main__":
    main()
