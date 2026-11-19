"""统一入口 main.py 的冒烟测试：拆单 → 合并 → 重排 一条链路跑通。

    python -m unittest test_pipeline -v
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from main import run_pipeline

DATA_DIR = Path(__file__).resolve().parent / "mock_data"


def load_payload() -> dict:
    return json.loads((DATA_DIR / "split_input.json").read_text(encoding="utf-8"))


def raw_order(guid: str) -> dict:
    """一条可参与重排的旧机场工单（归一化前形态）。"""
    return {
        "workOrderGuid": guid, "workOrderNo": f"ZQ-{guid}", "planGuid": "p-old",
        "inspectionMethod": "1", "cycleAirportGuid": "ap-a",
        "startDate": "2026-11-01 08:30:00", "endDate": "2026-11-01 09:30:00",
        "secondNum": 3600, "workOrderStatus": "9",
        "airportModel": "M350H", "professionalCategory": "tms",
        "workNature": "1", "workNatureSub": "t-rcxs",
        "cycleVoltageLevel": "AC110kV", "oneDay": 1,
    }


class TestPipeline(unittest.TestCase):
    def test_full_flow_offline(self):
        out = run_pipeline(load_payload(), [raw_order("old-1")])
        self.assertEqual(set(out), {"summary", "work_order_details", "rework"})
        # split_input.json 的黄金值：16 张工单 / 78 条明细
        self.assertEqual(out["summary"]["new_orders"], 16)
        self.assertEqual(len(out["work_order_details"]), 78)
        rw = out["rework"]
        self.assertIn(rw["entry"], {"normal", "insert_rework", "force_insert"})
        self.assertEqual(len(rw["new"]), out["summary"]["new_orders"])
        self.assertEqual(out["summary"]["old_orders"], 1)

    def test_conservation_failure_raises(self):
        # 设备凭空重复 -> verify 不通过 -> 整个流程必须拒绝向下走
        payload = load_payload()
        payload["planStrategyList"][0]["deviceList"] = (
            payload["planStrategyList"][0]["deviceList"] * 2)
        with self.assertRaises(ValueError) as ctx:
            run_pipeline(payload, [])
        self.assertIn("守恒", str(ctx.exception))


if __name__ == "__main__":
    unittest.main(verbosity=2)