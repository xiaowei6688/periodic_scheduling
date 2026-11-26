"""Agent 工具契约测试。"""

import json
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from core.approval import ApprovalDecision
from core.artifacts import MemoryArtifactStore, bind_artifact_store, materialize_artifact_delivery
from core.builder import GraphBuilder
from core.config import Settings
from core.steps import SyncStepTracker
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
from agents.work_order_scheduling.split_and_schedule.prompts import SYSTEM_PROMPT


ROOT_DIR = Path(__file__).resolve().parents[4]
DATA_FILE = ROOT_DIR / "mock_data" / "split_input.json"


class TestAgentTools(unittest.TestCase):
    def test_prompt_does_not_repeat_approval_card_in_chat_summary(self):
        self.assertIn("不要在聊天总结中复述审批卡视图", SYSTEM_PROMPT)
        self.assertIn("报告完排班事实和警告数量后立即结束", SYSTEM_PROMPT)
        self.assertIn("任何时候都不要提及 Agent 是否保存、存储、落库或创建数据", SYSTEM_PROMPT)
        self.assertNotIn("工具只计算，不保存文件或写入业务平台", SYSTEM_PROMPT)

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

    def test_handoff_step_uses_agent_display_name(self):
        spec = AgentRegistry().discover_package("agents").get("split_and_schedule")
        builder = GraphBuilder([spec], MagicMock(), settings=Settings())
        handoff = next(
            tool for tool in builder._supervisor_tools()
            if tool.name == "transfer_to_split_and_schedule"
        )

        self.assertEqual(handoff.metadata["axis_handoff_label"], "工单拆分与排班")
        self.assertEqual(handoff.metadata["axis_display_title"], "进行「工单拆分与排班」")

    def test_supervisor_step_distinguishes_analysis_and_summary(self):
        events = []
        initial_tracker = SyncStepTracker(
            events.append,
            agent_names=["supervisor", "split_and_schedule"],
            agent_display_names={"split_and_schedule": "工单拆分与排班"},
        )

        initial_tracker.on_chain_start(
            {}, {}, run_id="supervisor-1",
            metadata={"langgraph_node": "supervisor"},
        )
        initial_tracker.on_chain_end({}, run_id="supervisor-1")
        initial_tracker.on_chain_start(
            {}, {}, run_id="agent-1",
            metadata={"langgraph_node": "split_and_schedule"},
        )
        initial_tracker.on_chain_end({}, run_id="agent-1")

        resumed_tracker = SyncStepTracker(
            events.append,
            agent_names=["supervisor", "split_and_schedule"],
            agent_display_names={"split_and_schedule": "工单拆分与排班"},
            supervisor_has_started=True,
        )
        resumed_tracker.on_chain_start(
            {}, {}, run_id="supervisor-2",
            metadata={"langgraph_node": "supervisor"},
        )

        started = [event.title for event in events if event.status == "in_progress"]
        self.assertEqual(started, [
            "总调度分析任务中",
            "工单拆分与排班处理中",
            "总调度汇总结果中",
        ])

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
        view = request["display_arguments"]["confirmation"]
        self.assertEqual(view["variant"], "ready")
        self.assertEqual(view["approveLabel"], "确认创建机场与人工工单")
        self.assertEqual(view["rejectLabel"], "取消")
        self.assertEqual(view["actions"], [
            {"key": "approve", "label": "确认创建机场与人工工单",
             "action": "approve", "comment": ""},
            {"key": "cancel", "label": "取消",
             "action": "reject", "comment": ""},
        ])
        self.assertIn("新工单 0 张", request["display_arguments"]["scheduleSummary"])
        self.assertEqual(request["display_arguments"]["scheduleResult"],
                         store.get("test-thread", artifact.id)[1])
        self.assertNotIn("确认视图", request["display_arguments"])
        self.assertNotIn("排班摘要", request["display_arguments"])
        self.assertNotIn("完整组装结果", request["display_arguments"])
        self.assertEqual(approved["status"], "confirmed_for_submission")
        self.assertEqual(approved["confirmationVariant"], "ready")
        self.assertEqual(approved["scheduleArtifactId"], artifact.id)
        self.assertEqual(approved["message"], "已确认提交意向")

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
            "parentDeviceNum": 80,
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
                    "parentDeviceNum": 20,
                    "planGuid": "plan-manual-1",
                    "cycleStartDate": "2026-11-12 00:00:00",
                    "cycleEndDate": "2026-11-14 23:59:59",
                    "startDate": "2026-11-12 00:00:00",
                    "endDate": "2026-11-14 23:59:59",
                },
            ]}]}],
            "oldWorkOrders": [{"workOrderGuid": "old-1", "result": "已排妥"}],
            "impactChains": {"stats": {
                "chainCount": 1, "directCount": 1, "indirectCount": 0,
                "updatedOldCount": 1, "totalRounds": 1, "roundCounts": [1],
                "allChainsCompleted": True, "allPlacedWithinWindow": True,
            }},
            "entry": "insertRework",
            "forceProposals": [],
        }
        ready = _confirmation_view(reworked)
        self.assertEqual(ready["variant"], "reworked")
        self.assertEqual(ready["title"], "确认创建并更新旧单")
        self.assertEqual(ready["approveLabel"], "确认创建并更新旧单")
        self.assertEqual(ready["rejectLabel"], "取消")
        self.assertEqual(ready["inspectionRange"], {
            "start": "2026-11-12", "end": "2026-11-14",
        })
        self.assertEqual(ready["counts"]["oldUpdates"], 1)
        self.assertEqual(ready["counts"]["airportDevices"], 80)
        self.assertEqual(ready["counts"]["manualDevices"], 20)
        self.assertEqual(ready["counts"]["manualPlans"], 1)
        self.assertEqual(ready["airportScheduleRange"], {
            "start": "2026-11-12", "end": "2026-11-12",
        })
        self.assertEqual(len(ready["summaryLines"]), 4)
        self.assertIn("1 条影响链各完成 1 轮重排", ready["summary"])
        self.assertIn("1 张旧工单均已在自身允许范围内排妥", ready["summary"])
        self.assertIn("左侧“排期方案”页签", ready["summary"])
        self.assertIn("左侧“受影响工单”页签", ready["summary"])
        self.assertIn("左侧底部的“修改需求”", ready["summary"])

        forced = {
            **reworked,
            "entry": "forceInsert",
            "planStrategyList": [{"planList": [{"workOrders": [
                scheduled_airport,
                {
                    "inspectionMethod": "1", "parentDeviceNum": 25,
                    "startDate": None, "endDate": None,
                },
            ]}]}],
            "impactChains": {"stats": {"directCount": 3, "indirectCount": 6}},
            "forceProposals": [{
                "feasible": True,
                "proposedStartDate": "2026-11-13 16:30:00",
                "proposedEndDate": "2026-11-13 17:30:00",
                "forceTimePeriod": "16:30:00-17:30:00",
                "affectedOrders": [{"workOrderGuid": "old-1"}],
                "updateProposals": [
                {"work_order_guid": "old-1"},
                {"work_order_guid": "old-2"},
            ]}],
        }
        force_view = _confirmation_view(forced)
        self.assertEqual(force_view["variant"], "force_insert")
        self.assertEqual(force_view["counts"]["scheduled"], 1)
        self.assertEqual(force_view["counts"]["forced"], 0)
        self.assertEqual(force_view["counts"]["unplaced"], 1)
        self.assertEqual(force_view["counts"]["directAffected"], 3)
        self.assertEqual(force_view["counts"]["indirectAffected"], 6)
        self.assertEqual(force_view["counts"]["oldUpdates"], 2)
        self.assertEqual(force_view["counts"]["airportDevices"], 105)
        self.assertIn("真正无候选时间的工单 1 张", force_view["summary"])
        self.assertIn("直接影响 3 张、间接影响 6 张", force_view["summary"])
        self.assertIn("左侧“受影响工单”页签", force_view["summary"])
        self.assertEqual(force_view["title"], "确认强插")
        self.assertEqual(force_view["approveLabel"], "确认强插")
        self.assertEqual(force_view["modifyLabel"], "修改时间")
        self.assertEqual(force_view["rejectLabel"], "取消")
        self.assertEqual(force_view["actions"], [
            {"key": "approve", "label": "确认强插",
             "action": "approve", "comment": ""},
            {"key": "modify_time", "label": "修改时间",
             "action": "reject", "comment": "修改时间"},
            {"key": "cancel", "label": "取消",
             "action": "reject", "comment": ""},
        ])

    def test_request_tool_emits_five_business_steps(self):
        state = MemoryWorkflowStateStore()
        payload = {**self.payload, "mockExistingRows": []}
        state.save("thread-1", "work_order_schedule", "request-1", {
            "payload": payload,
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
        events = []
        tracker = SyncStepTracker(events.append, thread_id="thread-1")
        run_id = artifacts.begin_run("thread-1")
        bound.invoke(
            {"request_id": "request-1"},
            config={
                "callbacks": [tracker],
                "configurable": {
                    "thread_id": "thread-1", "artifact_run_id": run_id,
                },
            },
        )
        started = [event.title for event in events
                   if event.status == "in_progress"]
        completed = [event.title for event in events
                     if event.status == "completed"]
        expected = [
            "校验并拆分巡检计划",
            "获取并筛选已有工单",
            "执行工单排班规则",
            "判定排班结果",
            "整理排班方案",
        ]
        self.assertEqual(started, expected)
        self.assertEqual(completed, expected)
        self.assertNotIn("工单排班", started)

    def test_confirmation_view_matches_prd_reworked_summary(self):
        airport_orders = [{
            "inspectionMethod": "1", "parentDeviceNum": 1,
            "cycleStartDate": "2026-11-21 00:00:00",
            "cycleEndDate": "2026-11-22 23:59:59",
            "startDate": "2026-11-21 08:30:00",
            "endDate": "2026-11-21 09:30:00",
        } for _ in range(24)]
        manual_orders = [{
            "inspectionMethod": "2", "parentDeviceNum": 1,
            "planGuid": f"manual-plan-{index}",
            "cycleStartDate": "2026-11-21 00:00:00",
            "cycleEndDate": "2026-11-22 23:59:59",
            "startDate": "2026-11-21 00:00:00",
            "endDate": "2026-11-22 23:59:59",
        } for index in range(3)]
        result = {
            "planStrategyList": [{"planList": [
                {"workOrders": airport_orders + manual_orders},
                *({"workOrders": []} for _ in range(11)),
            ]}],
            "oldWorkOrders": [
                {"workOrderGuid": f"old-{index}", "result": "已排妥"}
                for index in range(9)
            ],
            "impactChains": {"stats": {
                "chainCount": 3, "directCount": 3, "indirectCount": 6,
                "updatedOldCount": 9, "totalRounds": 9,
                "roundCounts": [3, 3, 3],
                "allChainsCompleted": True, "allPlacedWithinWindow": True,
            }},
            "entry": "insertRework", "forceProposals": [], "warnings": [],
        }

        view = _confirmation_view(result)

        self.assertEqual(view["variant"], "reworked")
        self.assertEqual(view["sectionTitle"], "本次确认")
        self.assertIn(
            "12 个计划已完成排期，拟创建 27 张工单（机场 24 张、人工 3 张）",
            view["summary"],
        )
        self.assertIn("3 条影响链各完成 3 轮重排", view["summary"])
        self.assertIn("9 张旧工单均已在自身允许范围内排妥", view["summary"])
        self.assertEqual(view["inspectionRange"], {
            "start": "2026-11-21", "end": "2026-11-22",
        })
        self.assertEqual(view["counts"]["directAffected"], 3)
        self.assertEqual(view["counts"]["indirectAffected"], 6)
        self.assertEqual(view["counts"]["oldUpdates"], 9)

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
            "agents.work_order_scheduling.split_and_schedule.tools.rework_orders.fetch_existing_orders",
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
            "agents.work_order_scheduling.split_and_schedule.tools.rework_orders.fetch_existing_orders",
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
