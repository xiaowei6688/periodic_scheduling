"""拆单规则的回归测试（stdlib unittest，无第三方依赖）。

    python -m unittest test_split -v
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from split_work_orders import (
    AirportTask,
    DeviceAssignment,
    Ledgers,
    PlanSplitResult,
    airport_capacity_item,
    airport_filter_passed,
    budget,
    estimate_seconds,
    is_airport_infrared,
    normalize_voltage,
    pack_slices,
    parse_airport_coverage,
    query_strategy_devices,
    split_all,
    verify,
    windows_total_seconds,
)

DATA_DIR = Path(__file__).resolve().parent / "mock_data"


def load_input() -> dict:
    return json.loads((DATA_DIR / "split_input.json").read_text(encoding="utf-8"))


def tower(**overrides) -> dict:
    base = {
        "tower_guid": "t1", "tower_name": "塔1", "line_guid": "L1",
        "psr_equip_code": "", "sort": 1, "is_deleted": 0,
        "covering_airports": "ap-a", "filter": 0, "filter_td": 0, "span": 500,
    }
    base.update(overrides)
    return base


class TestBasics(unittest.TestCase):
    def test_normalize_voltage(self):
        self.assertEqual(normalize_voltage("AC110kV"), "110")
        self.assertEqual(normalize_voltage("ac220kv"), "220")
        self.assertEqual(normalize_voltage("110"), "110")
        self.assertEqual(normalize_voltage(""), "")
        self.assertEqual(normalize_voltage("AC未知"), "")

    def test_infrared_sub_natures(self):
        for code in ("s-hwxs", "t-hwxs", "d-hwxs"):
            self.assertTrue(is_airport_infrared(code))
        self.assertFalse(is_airport_infrared("t-rcxs"))

    def test_budget(self):
        self.assertEqual(budget(False), 23040)      # 8h * 80%
        self.assertEqual(budget(True), 17280)       # 6h * 80%

    def test_windows_total_single_and_multi(self):
        self.assertEqual(windows_total_seconds("08:30:00-17:30:00"), 32400)
        self.assertEqual(windows_total_seconds("08:30:00-12:30:00;12:45:00-18:30:00"),
                         4 * 3600 + 5 * 3600 + 45 * 60)
        self.assertIsNone(windows_total_seconds(""))

    def test_windows_rejects_bad_input(self):
        with self.assertRaises(ValueError):
            windows_total_seconds("17:30:00-08:30:00")
        with self.assertRaises(ValueError):
            windows_total_seconds("0830-1730")

    def test_coverage_parse(self):
        self.assertEqual(parse_airport_coverage(" ap-a, ap-b ,,ap-a "), ["ap-a", "ap-b"])
        self.assertEqual(parse_airport_coverage(""), [])

    def test_filter_rule(self):
        self.assertTrue(airport_filter_passed("tms", "2", 1, 0))    # 通道看 filterTd
        self.assertFalse(airport_filter_passed("tms", "2", 0, 1))
        self.assertTrue(airport_filter_passed("tms", "1", 0, 1))    # 其余看 filter
        self.assertFalse(airport_filter_passed("tms", "1", 1, 0))
        self.assertTrue(airport_filter_passed("dms", "2", 0, 1))

    def test_capacity_item(self):
        self.assertEqual(airport_capacity_item({"speciality": "tms", "workNature": "1"}, None, False), "本体")
        self.assertEqual(airport_capacity_item({"speciality": "tms", "workNature": "101"}, None, False), "本体")
        self.assertEqual(airport_capacity_item({"speciality": "tms", "workNature": "2"}, None, False), "通道")
        self.assertEqual(airport_capacity_item({"speciality": "sms", "workNature": "1"}, None, False), "融合/例行")

    def test_estimate_seconds(self):
        self.assertEqual(estimate_seconds(1.0, 4.0, False), 7200)
        self.assertEqual(estimate_seconds(1.0, 20.0, False), 1440)
        self.assertEqual(estimate_seconds(0.88, 2.0, False), 12672)
        with self.assertRaises(ValueError):
            estimate_seconds(1.0, 0, False)


class TestPacking(unittest.TestCase):
    plan = {"workNatureSub": "t-rcxs", "timePeriod": "08:30:00-17:30:00"}

    def _task(self, seconds: list[int], whole: bool = False) -> AirportTask:
        devices = [DeviceAssignment(f"d{i}", "airport", "ap-a", f"塔{i}", "L", "线路")
                   for i in range(len(seconds))]
        return AirportTask(plan=self.plan, airport_guid="ap-a", devices=devices,
                           seconds=seconds, whole=whole)

    def test_limit_is_min_of_budget_and_window(self):
        task = self._task([7200] * 4)
        slices = pack_slices(task, self.plan)
        self.assertEqual([len(s.indices) for s in slices], [3, 1])
        self.assertEqual(task.limit, 23040)

    def test_short_window_shrinks_limit(self):
        plan = {"workNatureSub": "t-rcxs", "timePeriod": "08:30:00-11:30:00"}
        task = self._task([7200, 7200])
        slices = pack_slices(task, plan)
        self.assertEqual([len(s.indices) for s in slices], [1, 1])
        self.assertEqual(task.limit, 10800)

    def test_exact_boundary_not_split(self):
        task = self._task([23040])
        self.assertEqual(len(pack_slices(task, self.plan)), 1)

    def test_one_over_boundary_splits(self):
        task = self._task([11520, 11521])
        self.assertEqual([len(s.indices) for s in pack_slices(task, self.plan)], [1, 1])

    def test_oversized_single_device_gets_own_order(self):
        task = self._task([40000, 7200])
        slices = pack_slices(task, self.plan)
        self.assertEqual([len(s.indices) for s in slices], [1, 1])
        self.assertEqual(slices[0].seconds, 40000)

    def test_whole_never_split(self):
        task = self._task([7200] * 5, whole=True)
        slices = pack_slices(task, self.plan)
        self.assertEqual(len(slices), 1)
        self.assertEqual(len(slices[0].indices), 5)

    def test_device_order_preserved(self):
        task = self._task([7200, 7200, 7200, 7200])
        flat = [i for s in pack_slices(task, self.plan) for i in s.indices]
        self.assertEqual(flat, [0, 1, 2, 3])


class TestDeviceAcquisition(unittest.TestCase):
    """设备只从 deviceList 取，台账字段内联，不查库。"""

    def _payload(self, towers: list[dict]) -> dict:
        return {
            "planStrategyList": [{
                "planStrategyGuid": "s1", "jobObjectGuid": "L1",
                "isLine": "0",
                "deviceList": [
                    {"jobObjectDeviceGuid": t["tower_guid"], "jobObjectDeviceName": t["tower_name"],
                     "jobObjectGuid": "L1", "span": str(t["span"]),
                     "coveringAirports": t["covering_airports"], "filter": t["filter"],
                     "filterTd": t["filter_td"], "isDeleted": t["is_deleted"],
                     "sort": t["sort"], "psrEquipCode": t["psr_equip_code"]}
                    for t in towers
                ],
            }],
            "airports": {}, "capacityTable": [],
        }

    def _plan(self) -> dict:
        return {"planGuid": "p1", "speciality": "tms", "workNature": "1",
                "jobObjectGuid": "L1", "jobObject": "线路1"}

    def test_deleted_device_skipped(self):
        payload = self._payload([tower(is_deleted=1)])
        result = PlanSplitResult(plan=self._plan(), strategy=payload["planStrategyList"][0])
        devices = query_strategy_devices(self._plan(), payload["planStrategyList"][0], result)
        self.assertEqual(devices, [])
        self.assertEqual(result.unmapped, ["t1"])

    def test_kept_device_included(self):
        payload = self._payload([tower()])
        strategy = payload["planStrategyList"][0]
        result = PlanSplitResult(plan=self._plan(), strategy=strategy)
        devices = query_strategy_devices(self._plan(), strategy, result)
        self.assertEqual([d.device_guid for d in devices], ["t1"])

    def test_order_follows_sort_not_array_position(self):
        payload = self._payload([tower(tower_guid="t2", sort=2),
                                 tower(tower_guid="t1", sort=1)])
        strategy = payload["planStrategyList"][0]
        result = PlanSplitResult(plan=self._plan(), strategy=strategy)
        devices = query_strategy_devices(self._plan(), strategy, result)
        self.assertEqual([d.device_guid for d in devices], ["t1", "t2"])

    def test_placeholder_minus_one_is_not_deleted(self):
        """报文未落库时 isDeleted 是 -1，不能当成删除（客户原始数据就是这样）。"""
        payload = self._payload([tower(is_deleted=-1)])
        strategy = payload["planStrategyList"][0]
        result = PlanSplitResult(plan=self._plan(), strategy=strategy)
        devices = query_strategy_devices(self._plan(), strategy, result)
        self.assertEqual([d.device_guid for d in devices], ["t1"])
        self.assertEqual(result.unmapped, [])

    def test_only_one_means_deleted(self):
        for flag in (1, "1", True):
            with self.subTest(flag=flag):
                payload = self._payload([tower(is_deleted=flag)])
                strategy = payload["planStrategyList"][0]
                result = PlanSplitResult(plan=self._plan(), strategy=strategy)
                self.assertEqual(query_strategy_devices(self._plan(), strategy, result), [])

    def test_missing_flag_treated_as_not_deleted(self):
        payload = self._payload([tower()])
        payload["planStrategyList"][0]["deviceList"][0].pop("isDeleted")
        strategy = payload["planStrategyList"][0]
        result = PlanSplitResult(plan=self._plan(), strategy=strategy)
        devices = query_strategy_devices(self._plan(), strategy, result)
        self.assertEqual(len(devices), 1)


class TestMockDataset(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.payload = load_input()
        cls.orders, cls.details, cls.results = split_all(cls.payload)
        cls.plans = {p["planGuid"]: p for s in cls.payload["planStrategyList"]
                     for p in s["planList"]}

    def test_conservation(self):
        self.assertEqual(verify(self.results), [])

    def test_order_count(self):
        self.assertEqual(len(self.orders), 33)
        self.assertEqual(len(self.details), 78)

    def test_airport_and_manual_split(self):
        airport = [o for o in self.orders if o["inspection_method"] == "1"]
        manual = [o for o in self.orders if o["inspection_method"] == "2"]
        self.assertEqual(len(airport), 30)
        self.assertEqual(len(manual), 3)

    def test_every_airport_order_within_day_capacity(self):
        """多设备工单必须 ≤ limit；单设备工单允许超限（Java 5096-5115）。"""
        overflow_single = 0
        for order in self.orders:
            if order["inspection_method"] != "1" or order["second_num"] is None:
                continue
            limit = order["capacity_limit"]
            self.assertIsNotNone(limit, "机场工单必须带 capacity_limit")
            if "保底" in (order["schedule_reason"] or ""):
                self.assertEqual(order["second_num"], 3600)
                continue
            if order["second_num"] > limit:
                self.assertEqual(order["parent_device_num"], 1,
                                 f"{order['work_order_no']} 多设备工单超容 {limit}")
                overflow_single += 1
        # 上午段 limit=14400 < 1040m 那基的 14976s；上午段计划 2 条各溢出 1 张
        self.assertEqual(overflow_single, 2)

    def test_channel_without_coverage_is_removed_not_manual(self):
        channel = [r for r in self.results if r.plan["workNature"] == "2"]
        self.assertTrue(channel)
        for result in channel:
            self.assertEqual(len(result.removed_channel), 2)
            self.assertEqual(result.manual_devices, [])

    def test_filter_failure_goes_manual(self):
        body = [r for r in self.results if r.plan["workNature"] == "1"]
        for result in body:
            self.assertEqual(len(result.manual_devices), 3)   # filter=1 一基 + 无覆盖两基

    def test_unknown_model_falls_back(self):
        fallback = [o for o in self.orders if "保底" in (o["schedule_reason"] or "")]
        self.assertEqual(len(fallback), 3)
        for order in fallback:
            self.assertEqual(order["cycle_airport_guid"], "ap-c")
            self.assertEqual(order["parent_device_num"], 2)

    def test_no_device_appears_twice(self):
        seen = set()
        for detail in self.details:
            key = (detail["work_order_guid"], detail["device"]["jobObjectDeviceGuid"])
            self.assertNotIn(key, seen)
            seen.add(key)

    def test_detail_passes_device_through_verbatim(self):
        """明细里的 device 必须是 deviceList 原始元素，一个字段都不改。"""
        originals = {d["jobObjectDeviceGuid"]: d
                     for st in self.payload["planStrategyList"] for d in st["deviceList"]}
        for detail in self.details:
            device = detail["device"]
            self.assertEqual(device, originals[device["jobObjectDeviceGuid"]])

    def test_order_passes_plan_through_verbatim(self):
        """工单里的 plan 必须是计划原始对象，一个字段都不改。"""
        for order in self.orders:
            self.assertEqual(order["plan"], self.plans[order["plan_guid"]])

    def test_scheduling_fields_left_empty(self):
        for order in self.orders:
            self.assertIsNone(order["start_date"])
            self.assertIsNone(order["end_date"])


class TestInputContract(unittest.TestCase):
    """输入报文契约：拆单必需的字段一个不能少，不许多余。

    这些字段原本在 Java 里要查三张表，现在必须随报文一起来。
    """

    DEVICE_EXTRA_FIELDS = {
        "coveringAirports",   # DeviceTmsTowerInfo.coveringAirports
        "filter",             # filter
        "filterTd",           # filterTd（通道过滤）
        "isDeleted",          # isDeleted
        "sort",               # sort（装箱顺序）
        "psrEquipCode",       # psrEquipCode
    }
    AIRPORT_FIELDS = {"airportName", "airportSn", "airportModel"}
    CAPACITY_FIELDS = {"airportModel", "speciality", "infrared", "item", "voltage", "capacity"}

    def setUp(self):
        self.payload = load_input()

    def test_top_level_blocks(self):
        self.assertEqual(set(self.payload),
                         {"planSetInfo", "planStrategyList", "airports", "capacityTable"})

    def test_device_list_carries_ledger_fields(self):
        for strategy in self.payload["planStrategyList"]:
            for device in strategy["deviceList"]:
                missing = self.DEVICE_EXTRA_FIELDS - set(device)
                self.assertEqual(missing, set(), f"{device['jobObjectDeviceGuid']} 缺字段 {missing}")

    def test_airports_block(self):
        for guid, airport in self.payload["airports"].items():
            self.assertEqual(set(airport), self.AIRPORT_FIELDS, f"{guid} 字段不符")

    def test_capacity_table(self):
        rows = self.payload["capacityTable"]
        self.assertTrue(rows)
        for row in rows:
            self.assertEqual(set(row), self.CAPACITY_FIELDS)

    def test_ledgers_built_without_external_files(self):
        ledgers = Ledgers.from_payload(self.payload)
        self.assertTrue(ledgers.airports)
        self.assertTrue(ledgers.capacity_index)

    def test_no_tower_store_in_ledgers(self):
        """设备数据只存在 deviceList 上，Ledgers 不再保存任何设备。"""
        ledgers = Ledgers.from_payload(self.payload)
        self.assertFalse(hasattr(ledgers, "towers"))


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestReworkReadiness(unittest.TestCase):
    """拆单产出要能直接和「已有日期的旧工单」放进同一个待排集合。"""

    @classmethod
    def setUpClass(cls):
        cls.payload = load_input()
        cls.orders, _, _ = split_all(cls.payload)

    def test_order_carries_limit_for_rework(self):
        from split_work_orders import plan_limit
        for order in self.orders:
            if order["inspection_method"] == "2":
                self.assertIsNone(order["capacity_limit"], "人工工单不走机场容量约束")
                continue
            self.assertEqual(order["capacity_limit"], plan_limit(order["plan"]),
                             f"{order['work_order_no']} 的 capacity_limit 与 plan_limit 不一致")

    def test_old_order_fits_same_schema(self):
        """旧工单只需填 start_date/end_date 就能塞进同一结构。"""
        new = next(o for o in self.orders if o["inspection_method"] == "1")
        old = dict(new)
        old["start_date"] = "2026-11-01 09:00:00"
        old["end_date"] = "2026-11-01 15:00:00"
        self.assertEqual(set(new), set(old))

    def test_scheduling_fields_empty_on_new_orders(self):
        """新工单不带日期，等重排填。"""
        for order in self.orders:
            self.assertIsNone(order["start_date"])
            self.assertIsNone(order["end_date"])

    def test_plan_limit_reacts_to_time_period(self):
        from split_work_orders import plan_limit
        morning = {"workNatureSub": "t-rcxs", "timePeriod": "08:30:00-12:30:00"}
        afternoon = {"workNatureSub": "t-rcxs", "timePeriod": "12:45:00-18:30:00"}
        full = {"workNatureSub": "t-rcxs", "timePeriod": "08:30:00-17:30:00"}
        self.assertEqual(plan_limit(morning), 14400)      # 时段 < 配额
        self.assertEqual(plan_limit(afternoon), 20700)    # 时段 < 配额
        self.assertEqual(plan_limit(full), 23040)         # 配额 < 时段
