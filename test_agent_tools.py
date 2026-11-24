"""Agent 工具契约测试。"""

import json
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from core.approval import ApprovalDecision
from core.artifacts import MemoryArtifactStore, bind_artifact_store, materialize_artifact_delivery
from core.builder import GraphBuilder
from core.config import Settings
from core.workflow_state import MemoryWorkflowStateStore, bind_workflow_state_store
from core.registry import AgentRegistry
from agents.work_order_scheduling.split_and_schedule.tools import (
    _confirmation_view,
    confirm_work_order_submission,
    prepare_rework_set,
    schedule_work_orders,
    schedule_work_orders_from_request,
    split_work_orders,
)


DATA_FILE = Path(__file__).resolve().parent / "mock_data" / "split_input.json"


class TestAgentTools(unittest.TestCase):
    def setUp(self):
        self.payload = json.loads(DATA_FILE.read_text(encoding="utf-8"))

    def invoke_artifact_tool(self, tool, arguments):
        store = MemoryArtifactStore()
        run_id = store.begin_run("test-thread")
        bound = bind_artifact_store(tool, store, "split_and_schedule")
        response = json.loads(bound.invoke(
            arguments,
            config={"configurable": {"thread_id": "test-thread", "artifact_run_id": run_id}},
        ))
        delivered = materialize_artifact_delivery(store, "test-thread", run_id)
        self.assertEqual(len(delivered), 1)
        self.assertEqual(response["artifact"]["id"], delivered[0][0].id)
        self.assertEqual(delivered[0][0].producer, "split_and_schedule")
        self.assertNotIn("planStrategyList", response)
        return response, delivered[0][0], delivered[0][1]

    def test_split_returns_orders_and_details(self):
        response, artifact, result = self.invoke_artifact_tool(
            split_work_orders, {"payload": self.payload},
        )
        self.assertEqual(artifact.type, "split_work_orders")
        self.assertIn("新工单 16 张", response["summary"])
        self.assertEqual(result["summary"]["order_count"], 16)
        self.assertEqual(result["summary"]["detail_count"], 78)
        self.assertEqual(len(result["orders"]), 16)

    def test_prepare_and_schedule_offline(self):
        _, pending_artifact, pending = self.invoke_artifact_tool(prepare_rework_set, {
            "payload": self.payload, "existing_rows": [],
        })
        self.assertEqual(pending_artifact.type, "rework_set")
        self.assertEqual(pending["summary"]["new_orders"], 16)
        self.assertEqual(pending["summary"]["old_orders"], 0)

        response, artifact, scheduled = self.invoke_artifact_tool(schedule_work_orders, {
            "payload": self.payload,
            "existing_rows": [],
            "skip_weekdays": [7],
            "holidays": ["2026-11-03"],
        })
        self.assertEqual(artifact.type, "scheduled_work_orders")
        self.assertIn("新工单 16 张", response["summary"])
        self.assertEqual(scheduled["planSetInfo"]["workOrderCount"], 16)
        self.assertIn(scheduled["entry"], {"normal", "insertRework", "forceInsert"})
        self.assertEqual(len(scheduled["planStrategyList"]), len(self.payload["planStrategyList"]))

    def test_missing_payload_fields_raise(self):
        bound = bind_artifact_store(split_work_orders, MemoryArtifactStore(), "split_and_schedule")
        with self.assertRaisesRegex(ValueError, "planSetInfo"):
            bound.invoke({"payload": {}}, config={"configurable": {"thread_id": "test-thread"}})

    def test_agent_is_discoverable_with_all_tools(self):
        registry = AgentRegistry().discover_package("agents")
        self.assertEqual(registry.names(), ["split_and_schedule"])
        self.assertEqual(
            [item.name for item in registry.get("split_and_schedule").tools],
            ["split_work_orders", "prepare_rework_set", "schedule_work_orders",
             "schedule_work_orders_from_request", "confirm_work_order_submission"],
        )

    def test_submission_approval_uses_existing_framework_contract(self):
        store = MemoryArtifactStore()
        run_id = store.begin_run("test-thread")
        artifact = store.put(
            "test-thread", "scheduled_work_orders",
            {"planSetInfo": {"workOrderCount": 16}, "planStrategyList": [],
             "oldWorkOrders": [], "entry": "normal",
             "forceProposals": [], "warnings": []},
            run_id=run_id,
        )
        arguments = {
            "schedule_artifact_id": artifact.id,
        }
        bound = bind_artifact_store(confirm_work_order_submission, store, "split_and_schedule")
        config = {"configurable": {"thread_id": "test-thread", "artifact_run_id": run_id}}
        with patch("core.approval.interrupt", return_value=ApprovalDecision(action="approve")) as pause:
            approved = bound.invoke(arguments, config=config)
        request = pause.call_args.args[0]
        self.assertEqual(request["tool_name"], "confirm_work_order_submission")
        self.assertEqual(request["display_title"], "确认创建机场与人工工单")
        view = request["display_arguments"]["确认视图"]
        self.assertEqual(view["variant"], "ready")
        self.assertEqual(view["approveLabel"], "确认创建机场与人工工单")
        self.assertEqual(view["rejectLabel"], "暂不提交")
        self.assertIn("新工单 0 张", request["display_arguments"]["排班摘要"])
        self.assertEqual(request["display_arguments"]["完整组装结果"],
                         store.get("test-thread", artifact.id)[1])
        self.assertEqual(approved["status"], "confirmed_for_submission")
        self.assertEqual(approved["confirmationVariant"], "ready")
        self.assertEqual(approved["scheduleArtifactId"], artifact.id)
        self.assertIn("尚未在业务平台创建", approved["message"])

        with patch("core.approval.interrupt", return_value=ApprovalDecision(action="reject")):
            rejected = bound.invoke(arguments, config=config)
        self.assertIn("工具调用已被人工拒绝", rejected)
        self.assertNotIn("confirmed_for_submission", rejected)
        with self.assertRaisesRegex(ValueError, "不属于本轮"):
            bound.invoke(arguments, config={"configurable": {
                "thread_id": "another-thread", "artifact_run_id": "another-run",
            }})

    def test_confirmation_view_distinguishes_rework_and_force_insert(self):
        scheduled_airport = {
            "inspectionMethod": "1",
            "cycleStartDate": "2026-11-12 00:00:00",
            "cycleEndDate": "2026-11-14 23:59:59",
            "startDate": "2026-11-12 08:30:00",
            "endDate": "2026-11-12 10:00:00",
        }
        reworked = {
            "planStrategyList": [{"planList": [{"workOrders": [
                scheduled_airport,
                {
                    "inspectionMethod": "2",
                    "cycleStartDate": "2026-11-12 00:00:00",
                    "cycleEndDate": "2026-11-14 23:59:59",
                    "startDate": "2026-11-12 00:00:00",
                    "endDate": "2026-11-14 23:59:59",
                },
            ]}]}],
            "oldWorkOrders": [{"workOrderGuid": "old-1", "result": "已排妥"}],
            "impactChains": {"stats": {"directCount": 1, "indirectCount": 0}},
            "entry": "insertRework",
            "forceProposals": [],
        }
        ready = _confirmation_view(reworked)
        self.assertEqual(ready["variant"], "ready")
        self.assertEqual(ready["inspectionRange"], {
            "start": "2026-11-12", "end": "2026-11-14",
        })
        self.assertEqual(ready["counts"]["oldUpdates"], 1)
        self.assertIn("已调整 1 张旧工单", ready["summary"])

        forced = {
            **reworked,
            "entry": "forceInsert",
            "planStrategyList": [{"planList": [{"workOrders": [
                scheduled_airport,
                {"inspectionMethod": "1", "startDate": None, "endDate": None},
            ]}]}],
            "impactChains": {"stats": {"directCount": 3, "indirectCount": 6}},
            "forceProposals": [{"updateProposals": [
                {"work_order_guid": "old-1"},
                {"work_order_guid": "old-2"},
            ]}],
        }
        force_view = _confirmation_view(forced)
        self.assertEqual(force_view["variant"], "force_insert")
        self.assertEqual(force_view["counts"]["scheduled"], 1)
        self.assertEqual(force_view["counts"]["unplaced"], 1)
        self.assertEqual(force_view["counts"]["directAffected"], 3)
        self.assertEqual(force_view["counts"]["indirectAffected"], 6)
        self.assertEqual(force_view["counts"]["oldUpdates"], 2)
        self.assertEqual(force_view["approveLabel"], "确认强插并创建")
        self.assertEqual(force_view["rejectLabel"], "放弃创建")

    def test_request_tool_loads_payload_from_thread_state(self):
        state = MemoryWorkflowStateStore()
        state.save("thread-1", "work_order_schedule", "request-1", {
            "payload": self.payload,
            "enforce_quota": True,
            "skip_weekdays": None,
            "holidays": None,
        })
        artifacts = MemoryArtifactStore()
        bound = bind_artifact_store(
            bind_workflow_state_store(schedule_work_orders_from_request, state),
            artifacts,
            "split_and_schedule",
        )
        run_id = artifacts.begin_run("thread-1")
        with patch(
            "agents.work_order_scheduling.split_and_schedule.rework_orders.fetch_existing_orders",
            return_value=[],
        ) as fetch:
            response = json.loads(bound.invoke(
                {"request_id": "request-1"},
                config={"configurable": {
                    "thread_id": "thread-1", "artifact_run_id": run_id,
                }},
            ))
        fetch.assert_called_once()
        self.assertIn("新工单 16 张", response["summary"])
        delivered = materialize_artifact_delivery(artifacts, "thread-1", run_id)
        self.assertEqual(delivered[0][0].type, "scheduled_work_orders")
        self.assertEqual(delivered[0][1]["planSetInfo"]["workOrderCount"], 16)
        with self.assertRaisesRegex(ValueError, "不存在或已过期"):
            bound.invoke(
                {"request_id": "request-1"},
                config={"configurable": {
                    "thread_id": "thread-2", "artifact_run_id": "run-2",
                }},
            )

    def test_graph_builder_binds_request_state_and_artifact_store(self):
        state = MemoryWorkflowStateStore()
        artifacts = MemoryArtifactStore()
        state.save("thread-1", "work_order_schedule", "request-1", {
            "payload": self.payload,
            "enforce_quota": True,
            "skip_weekdays": None,
            "holidays": None,
        })
        spec = AgentRegistry().discover_package("agents").get("split_and_schedule")
        builder = GraphBuilder(
            [spec], MagicMock(), settings=Settings(),
            workflow_state_store=state, artifact_store=artifacts,
        )
        tool = next(
            item for item in builder._agent_tools(spec)
            if item.name == "schedule_work_orders_from_request"
        )
        run_id = artifacts.begin_run("thread-1")
        with patch(
            "agents.work_order_scheduling.split_and_schedule.rework_orders.fetch_existing_orders",
            return_value=[],
        ):
            response = json.loads(tool.invoke(
                {"request_id": "request-1"},
                config={"configurable": {
                    "thread_id": "thread-1", "artifact_run_id": run_id,
                }},
            ))
        self.assertIn("新工单 16 张", response["summary"])
        self.assertEqual(artifacts.list("thread-1", run_id=run_id)[0].type,
                         "scheduled_work_orders")


if __name__ == "__main__":
    unittest.main()
