"""重排衔接模块测试：旧工单过滤、归一化、与新工单合并。

    python -m unittest test_rework -v
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from rework_orders import (
    build_rework_set,
    filter_reworkable,
    normalize_existing,
    order_date_span,
)
from split_work_orders import plan_limit, split_all, verify

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "mock_data"


def raw_order(**overrides) -> dict:
    """一条业务系统返回的旧工单（字段名照真实接口）。"""
    base = {
        "workOrderGuid": "tms_2026_aaa",
        "planGuid": "plan-1",
        "workOrderNo": "ZQ-JH-1-机场A-1",
        "inspectionMethod": "1",
        "cycleAirportGuid": "ap-a",
        "startDate": "2026-10-01 08:30:00",
        "endDate": "2026-10-01 10:06:00",
        "secondNum": 5760,
        "workOrderStatus": "9",
        "airportModel": "jc_03",
        "professionalCategory": "tms",
        "workNature": "1",
        "workNatureSub": "t-brcxs",
        "cycleVoltageLevel": "AC500kV",
        "oneDay": 1,
    }
    base.update(overrides)
    return base


class TestFilter(unittest.TestCase):
    def test_cancelled_dropped(self):
        kept, stats = filter_reworkable([raw_order(workOrderStatus="11")])
        self.assertEqual(kept, [])
        self.assertEqual(stats.cancelled, 1)
        self.assertEqual(stats.dropped[0]["reason"], "已作废")

    def test_delayed_dropped(self):
        kept, stats = filter_reworkable([raw_order(workOrderStatus="12")])
        self.assertEqual(kept, [])
        self.assertEqual(stats.delayed, 1)
        self.assertIn("延期", stats.dropped[0]["reason"])

    def test_reworkable_statuses_kept(self):
        for status in ("8", "9", "16"):
            with self.subTest(status=status):
                kept, stats = filter_reworkable([raw_order(workOrderStatus=status)])
                self.assertEqual(len(kept), 1)
                self.assertEqual(stats.kept, 1)

    def test_unknown_status_dropped(self):
        kept, stats = filter_reworkable([raw_order(workOrderStatus="99")])
        self.assertEqual(kept, [])

    def test_mixed_batch(self):
        rows = [
            raw_order(workOrderGuid="a", workOrderStatus="9"),
            raw_order(workOrderGuid="b", workOrderStatus="11"),
            raw_order(workOrderGuid="c", workOrderStatus="12"),
            raw_order(workOrderGuid="d", workOrderStatus="8"),
        ]
        kept, stats = filter_reworkable(rows)
        self.assertEqual([r["workOrderGuid"] for r in kept], ["a", "d"])
        self.assertEqual((stats.total, stats.kept, stats.cancelled, stats.delayed), (4, 2, 1, 1))


class TestNormalize(unittest.TestCase):
    def test_field_mapping(self):
        got = normalize_existing(raw_order())
        self.assertEqual(got["work_order_guid"], "tms_2026_aaa")
        self.assertEqual(got["cycle_airport_guid"], "ap-a")
        self.assertEqual(got["second_num"], 5760)
        self.assertEqual(got["start_date"], "2026-10-01 08:30:00")
        self.assertEqual(got["end_date"], "2026-10-01 10:06:00")
        self.assertEqual(got["source"], "existing")
        self.assertEqual(got["airport_model"], "jc_03")
        self.assertEqual(got["voltage_level"], "AC500kV")

    def test_time_format_normalized(self):
        got = normalize_existing(raw_order(startDate="2026-10-01T08:30:00",
                                           endDate="2026-10-01"))
        self.assertEqual(got["start_date"], "2026-10-01 08:30:00")
        self.assertEqual(got["end_date"], "2026-10-01 00:00:00")

    def test_empty_time_becomes_none(self):
        got = normalize_existing(raw_order(startDate="", endDate="   "))
        self.assertIsNone(got["start_date"])
        self.assertIsNone(got["end_date"])

    def test_pre_window_kept_when_present(self):
        got = normalize_existing(raw_order(preStartDate="2026-11-05 00:00:00",
                                           preEndDate="2026-11-09 23:59:59"))
        self.assertEqual(got["pre_start_date"], "2026-11-05 00:00:00")
        self.assertEqual(got["pre_end_date"], "2026-11-09 23:59:59")

    def test_time_period_kept_when_present(self):
        got = normalize_existing(raw_order(timePeriod="08:30:00-12:30:00"))
        self.assertEqual(got["time_period"], "08:30:00-12:30:00")

    def test_raw_kept(self):
        raw = raw_order()
        self.assertEqual(normalize_existing(raw)["raw"], raw)


class TestMerge(unittest.TestCase):
    def setUp(self):
        payload = json.loads((DATA_DIR / "split_input.json").read_text(encoding="utf-8"))
        self.orders, self.details, self.results = split_all(payload)
        self.window_start, self.window_end = order_date_span(self.orders)

    def test_span_from_new_orders(self):
        self.assertEqual(self.window_start, "2026-11-01 00:00:00")
        self.assertEqual(self.window_end, "2026-11-14 18:30:00")

    def test_old_orders_get_no_synthetic_window(self):
        rework = build_rework_set(self.orders, [raw_order(workOrderStatus="9")],
                                  self.window_start, self.window_end)
        old = rework.old_orders[0]
        self.assertIsNone(old["pre_start_date"])
        self.assertIsNone(old["pre_end_date"])
        self.assertEqual(old["start_date"], "2026-10-01 08:30:00")
        self.assertEqual(old["end_date"], "2026-10-01 10:06:00")

    def test_window_kept_on_set_not_on_orders(self):
        rework = build_rework_set(self.orders, [raw_order(workOrderStatus="9")],
                                  self.window_start, self.window_end)
        self.assertEqual(rework.window["start"], self.window_start)
        self.assertEqual(rework.window["end"], self.window_end)

    def test_cancelled_and_delayed_excluded(self):
        rows = [raw_order(workOrderGuid="keep", workOrderStatus="9"),
                raw_order(workOrderGuid="c", workOrderStatus="11"),
                raw_order(workOrderGuid="d", workOrderStatus="12")]
        rework = build_rework_set(self.orders, rows, self.window_start, self.window_end)
        self.assertEqual([o["work_order_guid"] for o in rework.old_orders], ["keep"])
        self.assertEqual(rework.filter_stats.cancelled, 1)
        self.assertEqual(rework.filter_stats.delayed, 1)

    def test_new_orders_untouched(self):
        rework = build_rework_set(self.orders, [raw_order(workOrderStatus="9")],
                                  self.window_start, self.window_end)
        self.assertEqual(len(rework.new_orders), len(self.orders))
        for order in rework.new_orders:
            self.assertIsNone(order["start_date"])
            if order["inspection_method"] == "1":
                self.assertEqual(order["capacity_limit"], plan_limit(order["plan"]))

    def test_split_conservation(self):
        self.assertEqual(verify(self.results), [])


class TestRealSampleFiltering(unittest.TestCase):
    def test_real_sample_all_filtered(self):
        path = BASE_DIR / "out" / "existing_orders.json"
        if not path.exists():
            self.skipTest("缺少真实样本文件")
        rows = json.loads(path.read_text(encoding="utf-8"))
        kept, stats = filter_reworkable(rows)
        self.assertEqual(stats.total, len(rows))
        self.assertEqual(stats.cancelled, 3)
        self.assertEqual(stats.delayed, len(rows) - 3)
        self.assertEqual(kept, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)