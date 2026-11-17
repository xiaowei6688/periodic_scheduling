"""用客户真实计划数据（input_type_data/*.json）做回归。

客户那 5 份报文是落库时从创建计划接口抓的，`deviceList` 里**不含**覆盖机场、过滤位
（这些在 Java 里要查设备台账）。本测试模拟"上游已经把这些字段补齐"后的输入：
直接在 deviceList 上注入台账字段，再加 airports / capacityTable 两块，然后拆单。

验证：不崩溃、守恒通过、工单数稳定（黄金值）。

    python -m unittest test_customer_data -v
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from split_work_orders import (
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

AIRPORTS = {"ap-a": {"airportName": "机场A", "airportSn": "SN1", "airportModel": "M350H"}}
CAPACITY_TABLE = [
    {"airportModel": "M350H", "speciality": "tms", "infrared": False,
     "item": "本体", "voltage": "110", "capacity": 20.0},
    {"airportModel": "M350H", "speciality": "tms", "infrared": False,
     "item": "通道", "voltage": "110", "capacity": 2.0},
]


def build_input(name: str) -> dict:
    """取客户原始报文，把台账字段注入 deviceList，补上 airports / capacityTable。"""
    payload = json.loads((CUSTOMER_DIR / f"{name}.json").read_text(encoding="utf-8"))
    for strategy in payload["planStrategyList"]:
        for index, device in enumerate(strategy["deviceList"]):
            # 每 7 基留 1 基无覆盖：本体走人工，通道走剔除，两条分支都能触发
            device["coveringAirports"] = "ap-a" if index % 7 else ""
            device["filter"] = 0
            device["filterTd"] = 0
            device["isDeleted"] = 0
            device["sort"] = index
            device["psrEquipCode"] = f"PSR-{index:03d}"
            # span 保持客户原值（全是空字符串），用于验证通道估时保底
    payload["airports"] = AIRPORTS
    payload["capacityTable"] = CAPACITY_TABLE
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


class TestUnimplementedBranchIsLoud(unittest.TestCase):
    def test_sms_estimation_raises_instead_of_silent_fallback(self):
        """变电整站估时没实现，必须抛错，不能被保底吞掉。"""
        ledgers = Ledgers(
            airports={"ap-a": {"airportName": "A", "airportModel": "M350H"}},
            capacity_table=[{"airportModel": "M350H", "speciality": "sms", "infrared": False,
                             "item": "融合/例行", "voltage": "110", "capacity": 3.0}],
        )
        task = AirportTask(
            plan={"speciality": "sms", "workNature": "1", "workNatureSub": "s-rcxs",
                  "voltageLevel": "AC110kV"},
            airport_guid="ap-a",
            devices=[DeviceAssignment("d1", "airport", "ap-a", "站1", "L", "变电站")],
        )
        with self.assertRaises(NotImplementedError):
            estimate_task(task, task.plan, {}, ledgers)


if __name__ == "__main__":
    unittest.main(verbosity=2)
