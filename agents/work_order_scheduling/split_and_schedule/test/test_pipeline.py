"""Agent 流程的冒烟测试：拆单 → 合并 → 重排 一条链路跑通。

    python -m unittest agents.work_order_scheduling.split_and_schedule.test.test_pipeline -v
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from types import SimpleNamespace

from agents.work_order_scheduling.split_and_schedule.output import _impact_chains
from agents.work_order_scheduling.split_and_schedule.tools.rework_algorithm import ReworkStep
from agents.work_order_scheduling.split_and_schedule.service import run_pipeline
from agents.work_order_scheduling.split_and_schedule.tools import _confirmation_view

ROOT_DIR = Path(__file__).resolve().parents[4]
DATA_DIR = ROOT_DIR / "mock_data"
APPROVAL_SCENARIOS_DIR = DATA_DIR / "approval_scenarios"


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
    def test_approval_scenario_mocks_cover_all_three_outcomes(self):
        scenarios = {
            "normal_full.json": {
                "entry": "normal",
                "variant": "ready",
                "approveLabel": "确认创建机场与人工工单",
            },
            "reworked_full.json": {
                "entry": "insertRework",
                "variant": "reworked",
                "approveLabel": "确认创建并更新旧单",
            },
            "force_insert_full.json": {
                "entry": "forceInsert",
                "variant": "force_insert",
                "approveLabel": "确认强插",
            },
        }

        for filename, expected in scenarios.items():
            with self.subTest(filename=filename):
                payload = json.loads(
                    (APPROVAL_SCENARIOS_DIR / filename).read_text(encoding="utf-8"))
                result = run_pipeline(
                    payload, payload["mockExistingRows"], enforce_quota=False)
                view = _confirmation_view(result)

                self.assertEqual(result["entry"], expected["entry"])
                self.assertEqual(view["variant"], expected["variant"])
                self.assertEqual(view["approveLabel"], expected["approveLabel"])
                self.assertEqual(view["rejectLabel"], "取消")

                if expected["entry"] == "insertRework":
                    self.assertEqual(result["impactChains"]["stats"]["roundCounts"], [2])
                    self.assertEqual(result["impactChains"]["stats"]["updatedOldCount"], 2)
                    self.assertTrue(result["impactChains"]["stats"]["allChainsCompleted"])
                elif expected["entry"] == "forceInsert":
                    self.assertEqual(len(result["forceProposals"]), 1)

    def test_impact_chains_keep_complete_rounds_and_window_checks(self):
        pending = SimpleNamespace(old_orders=[
            {
                "work_order_guid": "B",
                "start_date": "2026-11-21 08:30:00",
                "end_date": "2026-11-21 09:30:00",
                "pre_start_date": "2026-11-21 00:00:00",
                "pre_end_date": "2026-11-22 23:59:59",
                "raw": {
                    "workOrderGuid": "B", "workOrderNo": "ZQ-B",
                    "planGuid": "old-plan-b", "planNo": "OLD-PLAN-B",
                    "planSetName": "旧计划集B", "cycleParentDeviceName": "黄新线",
                    "professionalCategory": "tms", "workNature": "2",
                    "workNatureSub": "t-rcxs", "equipName": "昔岭机场",
                    "cycleAirportGuid": "ap-a", "secondNum": 3600,
                    "workOrderStatus": "9", "timePeriod": "08:30:00-17:30:00",
                },
            },
            {
                "work_order_guid": "D",
                "start_date": "2026-11-21 09:30:00",
                "end_date": "2026-11-21 10:30:00",
                "pre_start_date": "2026-11-21 00:00:00",
                "pre_end_date": "2026-11-22 23:59:59",
                "raw": {"workOrderGuid": "D", "workOrderNo": "ZQ-D"},
            },
        ])
        result = SimpleNamespace(
            new_orders_out=[{
                "work_order_guid": "C",
                "plan": {"planGuid": "plan-c", "planNo": "PLAN-C"},
            }],
            rework_steps=[
                ReworkStep("C", "B", "2026-11-21 08:30:00",
                           "2026-11-21 09:30:00", "2026-11-22 08:30:00",
                           "2026-11-22 09:30:00"),
                ReworkStep("B", "D", "2026-11-21 09:30:00",
                           "2026-11-21 10:30:00", "2026-11-22 09:30:00",
                           "2026-11-22 10:30:00"),
            ],
            entry="insert_rework",
        )

        chains = _impact_chains({"planSetInfo": {}}, pending, result)

        self.assertEqual(chains["stats"]["chainCount"], 1)
        self.assertEqual(chains["stats"]["roundCounts"], [2])
        self.assertEqual(chains["stats"]["updatedOldCount"], 2)
        self.assertTrue(chains["stats"]["allChainsCompleted"])
        self.assertTrue(chains["stats"]["allPlacedWithinWindow"])
        self.assertEqual(len(chains["list"][0]["nodes"]), 2)
        self.assertEqual(chains["list"][0]["nodes"][1]["round"], 2)
        self.assertEqual(chains["list"][0]["nodes"][1]["relation"], "indirect")
        chain = chains["list"][0]
        self.assertEqual(chain["planStartDate"], None)
        self.assertEqual(chain["nodes"][0]["jobObject"], "黄新线")
        self.assertEqual(chain["nodes"][0]["professionalCategoryZh"], "输电")
        self.assertEqual(chain["nodes"][0]["workNatureZh"], "通道")
        self.assertEqual(chain["nodes"][0]["resultDesc"], "继续影响下一张旧单")

    def test_full_flow_offline(self):
        out = run_pipeline(load_payload(), [raw_order("old-1")])
        self.assertEqual(set(out), {"planSetInfo", "planStrategyList", "oldWorkOrders",
                                    "impactChains", "entry", "forceProposals", "warnings"})
        # 计划包 2（planStrategyList 条数）/ 计划 7（planList 总数）/ 新工单 16 / 明细 78；旧工单 1 条平级放
        ps = out["planSetInfo"]
        self.assertEqual((ps["planPackageCount"], ps["planCount"], ps["workOrderCount"]), (2, 7, 16))
        new_orders = [o for s in out["planStrategyList"] for p in s["planList"]
                      for o in p.get("workOrders", [])]
        self.assertEqual(len(new_orders), 16)
        self.assertEqual(sum(len(o.get("details", [])) for o in new_orders), 78)
        self.assertEqual(len(out["oldWorkOrders"]), 1)
        self.assertIn(out["entry"], {"normal", "insert_rework", "force_insert"})

    def test_old_order_output_preserves_non_time_fields(self):
        payload = json.loads(
            (APPROVAL_SCENARIOS_DIR / "reworked_full.json").read_text(encoding="utf-8"))
        payload["mockExistingRows"][0]["cycleParentDeviceName"] = "黄新线"
        result = run_pipeline(payload, payload["mockExistingRows"], enforce_quota=False)

        old = result["oldWorkOrders"][0]
        self.assertEqual(old["jobObject"], "黄新线")
        self.assertEqual(old["workObjName"], "黄新线")
        self.assertEqual(old["cycleAirportGuid"], "mock-airport-001")
        self.assertEqual(old["preStartDate"], "2026-12-01 00:00:00")
        self.assertEqual(old["preEndDate"], "2026-12-02 23:59:59")
        self.assertEqual(old["timePeriod"], "08:30:00-09:30:00")
        self.assertNotEqual(old["oldStartDate"], old["startDate"])

        chain = result["impactChains"]["list"][0]
        self.assertEqual(chain["planStartDate"], "2026-12-01 00:00:00")
        self.assertEqual(chain["planEndDate"], "2026-12-01 23:59:59")
        self.assertEqual(chain["timePeriod"], "08:30:00-09:30:00")
        self.assertEqual(chain["preStartDate"], chain["planStartDate"])
        self.assertEqual(chain["preEndDate"], chain["planEndDate"])
        self.assertEqual(chain["rootWorkOrderGuid"], chain["nodes"][0]["preemptedBy"])
        self.assertEqual(chain["nodes"][0]["jobObject"], "黄新线")
        self.assertEqual(chain["nodes"][0]["resultDesc"], "继续影响下一张旧单")
        self.assertEqual(chain["nodes"][1]["resultDesc"], "已排妥")

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
