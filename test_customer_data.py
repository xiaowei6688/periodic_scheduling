"""用客户真实计划数据（input_type_data/*.json）做回归。

客户那 5 份报文是落库时从创建计划接口抓的，`deviceList` 里**不含**覆盖机场、过滤位
（这些在 Java 里要查设备台账）。本测试模拟"上游已经把这些字段补齐"后的输入：
直接在 deviceList 上注入台账字段，再加 airports 一块，然后拆单。承载力走内置表。

验证：不崩溃、守恒通过、工单数稳定（黄金值）。

    python -m unittest test_customer_data -v
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from agents.work_order_scheduling.split_and_schedule.tools.split_work_orders import (
    AirportTask,
    DeviceAssignment,
    Ledgers,
    estimate_task,
    split_all,
    verify,
)

CUSTOMER_DIR = Path("/Users/levin/Documents/新周期排班/input_type_data")

# 客户 5 种频次 + 固定注入规则下的黄金工单数
GOLDEN = {
    "day":      {"plans": 30, "orders": 30, "details": 870},
    "zhou":     {"plans": 10, "orders": 30, "details": 340},
    "yue":      {"plans": 8,  "orders": 8,  "details": 232},
    "zhouqi":   {"plans": 4,  "orders": 4,  "details": 116},
    "zidingyi": {"plans": 14, "orders": 56, "details": 476},
}

AIRPORTS = [{"airportGuid": "ap-a", "airportName": "机场A", "airportSn": "SN1", "airportModel": "jc_03"}]


def build_input(name: str) -> dict:
    """取客户原始报文，把台账字段注入 deviceList，补上 airports 机场台账。"""
    payload = json.loads((CUSTOMER_DIR / f"{name}.json").read_text(encoding="utf-8"))
    for strategy in payload["planStrategyList"]:
        for index, device in enumerate(strategy["deviceList"]):
            # 每 7 基留 1 基无覆盖：本体走人工，通道走剔除，两条分支都能触发
            device["coveringAirports"] = "ap-a" if index % 7 else ""
            device["filter"] = 0
            device["filterTd"] = 0
            device["sort"] = index
            # span 保持客户原值（全是空字符串），用于验证通道估时保底
    payload["airports"] = AIRPORTS
    return payload


class TestCustomerDatasets(unittest.TestCase):
    def test_all_datasets_run_and_stay_consistent(self):
        for name, golden in GOLDEN.items():
            with self.subTest(dataset=name):
                orders, details, results = split_all(build_input(name))
                self.assertEqual(len(results), golden["plans"], f"{name} 计划数")
                self.assertEqual(len(orders), golden["orders"], f"{name} 工单数")
                self.assertEqual(len(details), golden["details"], f"{name} 明细数")
                self.assertEqual(verify(results), [], f"{name} 守恒校验")

    def test_channel_work_nature_removes_uncovered(self):
        """day/yue/zhouqi 的 workNature=2（通道）：无覆盖杆塔必须剔除、不进人工。"""
        for name in ("day", "yue", "zhouqi"):
            with self.subTest(dataset=name):
                _, _, results = split_all(build_input(name))
                for result in results:
                    self.assertEqual(result.plan["workNature"], "2")
                    self.assertTrue(result.removed_channel, f"{name} 应有剔除设备")
                    self.assertEqual(result.manual_devices, [], f"{name} 通道不应转人工")

    def test_body_work_nature_routes_uncovered_to_manual(self):
        """zhou/zidingyi 的 workNature=1（本体）：无覆盖杆塔转人工。"""
        for name in ("zhou", "zidingyi"):
            with self.subTest(dataset=name):
                _, _, results = split_all(build_input(name))
                for result in results:
                    self.assertEqual(result.plan["workNature"], "1")
                    self.assertEqual(result.removed_channel, [])
                    self.assertEqual(len(result.manual_devices), 5)

    def test_channel_without_span_falls_back_as_whole(self):
        """客户数据 span 全空 -> 通道估时失败 -> 整组保底，不拆单。"""
        orders, _, _ = split_all(build_input("yue"))
        airport = [o for o in orders if o["inspection_method"] == "1"]
        self.assertTrue(airport)
        for order in airport:
            self.assertIn("保底1小时", order["schedule_reason"])
            self.assertEqual(order["second_num"], 3600)


class TestOrdering(unittest.TestCase):
    """出单顺序须与 Java 一致：人工工单在前，机场工单按 one_day 升序。"""

    def test_manual_orders_come_first(self):
        orders, _, _ = split_all(build_input("zhou"))
        manual_count = sum(1 for o in orders if o["inspection_method"] == "2")
        self.assertEqual(manual_count, 10)
        self.assertTrue(all(o["inspection_method"] == "2" for o in orders[:manual_count]))
        self.assertTrue(all(o["inspection_method"] == "1" for o in orders[manual_count:]))

    def test_sequence_starts_after_manual(self):
        orders, _, _ = split_all(build_input("zhou"))
        sequences = [int(o["work_order_no"].rsplit("-", 1)[-1])
                     for o in orders if o["inspection_method"] == "1"]
        self.assertEqual(sequences, list(range(11, 11 + len(sequences))))


class TestSubstationEstimation(unittest.TestCase):
    """变电：整站航点求和算一次，只记首台设备，整单不拆。"""

    def _ledgers(self, routes):
        return Ledgers(
            airports=[{"airportGuid": "ap-a", "airportName": "A", "airportModel": "jc_03"}],
            device_routes=routes,
        )

    def _task(self, plan):
        return AirportTask(
            plan=plan, airport_guid="ap-a",
            devices=[DeviceAssignment("d1", "airport", "ap-a", "站1", "L", "变电站"),
                     DeviceAssignment("d2", "airport", "ap-a", "站2", "L", "变电站")],
        )

    def test_station_points_summed_once(self):
        plan = {"speciality": "sms", "workNature": "1", "workNatureSub": "s-rcxs",
                "voltageLevel": "AC500kV", "jobObjectGuid": "st-1"}
        routes = [
            {"deviceGuid": "st-1", "deviceType": "sms", "trackType": "11", "taskPoint": 30, "validating": "1", "isDeleted": 0},
            {"deviceGuid": "st-1", "deviceType": "sms", "trackType": "11", "taskPoint": 30, "validating": "1", "isDeleted": 0},
            {"deviceGuid": "st-1", "deviceType": "sms", "trackType": "12", "taskPoint": 99, "validating": "1", "isDeleted": 0},
            {"deviceGuid": "st-1", "deviceType": "sms", "trackType": "11", "taskPoint": 99, "validating": "0", "isDeleted": 0},
            {"deviceGuid": "st-1", "deviceType": "sms", "trackType": "11", "taskPoint": 99, "validating": "1", "isDeleted": 1},
        ]
        task = self._task(plan)
        estimate_task(task, plan, {}, self._ledgers(routes))
        # 只累加 trackType=11 且有效未删除的 30+30=60 航点；jc_03 变电 1000 航点/日 -> 60/1000*28800
        self.assertEqual(task.seconds, [1728, 0])

    def test_missing_track_type_falls_back(self):
        plan = {"speciality": "sms", "workNature": "1", "workNatureSub": "未知子类",
                "voltageLevel": "AC500kV", "jobObjectGuid": "st-1"}
        task = self._task(plan)
        estimate_task(task, plan, {}, self._ledgers([]))
        self.assertTrue(task.whole)
        self.assertIn("保底1小时", task.fallback_reason)

    def test_missing_station_guid_falls_back(self):
        plan = {"speciality": "sms", "workNature": "1", "workNatureSub": "s-rcxs",
                "voltageLevel": "AC500kV", "jobObjectGuid": ""}
        task = self._task(plan)
        estimate_task(task, plan, {}, self._ledgers([]))
        self.assertTrue(task.whole)
        self.assertIn("保底1小时", task.fallback_reason)


if __name__ == "__main__":
    unittest.main(verbosity=2)
