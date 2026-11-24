"""结构化排班请求经多 Agent 运行时进入工具并交付 Artifact。"""

import json
import re
import unittest
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage

from agents.work_order_scheduling.split_and_schedule.tools import (
    REQUEST_NAMESPACE,
    schedule_work_orders_from_request,
)
from core.api import create_app
from core.artifacts import MemoryArtifactStore, bind_artifact_store, materialize_artifact_delivery
from core.steps import StepEvent
from core.workflow_state import MemoryWorkflowStateStore, bind_workflow_state_store


DATA_FILE = Path(__file__).resolve().parent / "mock_data" / "split_input.json"


class _Runtime:
    def __init__(self):
        self.settings = SimpleNamespace(api_keys={})
        self.artifact_store = MemoryArtifactStore()
        self.workflow_state_store = MemoryWorkflowStateStore()
        self.agent_display_names = {"split_and_schedule": "工单拆分与排班"}
        self.graph_messages = []

    @asynccontextmanager
    async def thread_lock(self, thread_id):
        yield

    @contextmanager
    def execution_turn(self, turn_id):
        yield

    def begin_artifact_run(self, *, thread_id):
        return self.artifact_store.begin_run(thread_id)

    def take_artifacts(self, *, thread_id):
        run_id = self.artifact_store.active_run(thread_id)
        entries = materialize_artifact_delivery(self.artifact_store, thread_id, run_id)
        artifacts = [
            {"id": item.id, "producer": item.producer, "type": item.type, "data": data}
            for item, data in entries
        ]
        self.artifact_store.delete_run(thread_id, run_id)
        return artifacts

    def discard_artifact_run(self, *, thread_id):
        run_id = self.artifact_store.active_run(thread_id)
        if run_id:
            self.artifact_store.delete_run(thread_id, run_id)

    async def arecord_conversation_message(self, message):
        pass

    async def apending_approvals(self, *, thread_id):
        return []

    async def astream_steps(self, message, *, thread_id):
        self.graph_messages.append(message)
        match = re.search(r"请求编号 ([0-9a-f]+)", message)
        assert match is not None
        step_id = uuid4().hex
        yield StepEvent(
            id=step_id, agent="split_and_schedule",
            title="工单排班", status="in_progress",
        )
        bound = bind_artifact_store(
            bind_workflow_state_store(
                schedule_work_orders_from_request, self.workflow_state_store,
            ),
            self.artifact_store, "split_and_schedule",
        )
        summary = json.loads(bound.invoke(
            {"request_id": match.group(1)},
            config={"configurable": {
                "thread_id": thread_id,
                "artifact_run_id": self.artifact_store.active_run(thread_id),
            }},
        ))["summary"]
        yield StepEvent(
            id=step_id, agent="split_and_schedule",
            title="工单排班", status="completed",
        )
        yield AIMessage(content=summary, name="split_and_schedule"), {}

    async def aclose(self):
        pass


class TestScheduleWebSocket(unittest.TestCase):
    def test_payload_enters_agent_runtime_and_delivers_artifact(self):
        runtime = _Runtime()
        payload = json.loads(DATA_FILE.read_text(encoding="utf-8"))
        with patch(
            "agents.work_order_scheduling.split_and_schedule.rework_orders.fetch_existing_orders",
            return_value=[],
        ) as fetch:
            with TestClient(create_app(runtime)) as client:
                with client.websocket_connect("/v1/chat/ws") as ws:
                    ws.send_json({
                        "type": "work_order_schedule",
                        "thread_id": "schedule-1",
                        "payload": payload,
                    })
                    events = []
                    while True:
                        event = ws.receive_json()
                        events.append(event)
                        if event["type"] in {"done", "error"}:
                            break
        fetch.assert_called_once()
        self.assertEqual(events[0]["type"], "start")
        self.assertEqual(events[-1]["type"], "done")
        self.assertEqual(
            [e["status"] for e in events if e["type"] == "step"],
            ["in_progress", "completed"],
        )
        self.assertTrue(any(e["type"] == "delta" for e in events))
        self.assertIn("新工单 16 张", events[-1]["answer"])
        artifact = events[-1]["artifacts"][0]
        self.assertEqual(artifact["type"], "scheduled_work_orders")
        self.assertEqual(artifact["data"]["planSetInfo"]["workOrderCount"], 16)
        self.assertEqual(len(artifact["data"]["planStrategyList"]), 2)
        self.assertEqual(runtime.artifact_store.list("schedule-1"), [])
        self.assertEqual(len(runtime.graph_messages), 1)
        self.assertNotIn("planStrategyList", runtime.graph_messages[0])
        self.assertIn("split_and_schedule", runtime.graph_messages[0])
        request_id = re.search(r"请求编号 ([0-9a-f]+)", runtime.graph_messages[0]).group(1)
        self.assertEqual(
            runtime.workflow_state_store.load("schedule-1", REQUEST_NAMESPACE, request_id)["payload"],
            payload,
        )

    def test_invalid_payload_returns_error_without_graph_dispatch(self):
        runtime = _Runtime()
        with TestClient(create_app(runtime)) as client:
            with client.websocket_connect("/v1/chat/ws") as ws:
                ws.send_json({
                    "type": "work_order_schedule",
                    "thread_id": "schedule-2",
                    "payload": {},
                })
                error = ws.receive_json()
        self.assertEqual(error["type"], "error")
        self.assertIn("planSetInfo", error["message"])
        self.assertEqual(runtime.graph_messages, [])

    def test_existing_rows_is_rejected(self):
        runtime = _Runtime()
        with TestClient(create_app(runtime)) as client:
            with client.websocket_connect("/v1/chat/ws") as ws:
                ws.send_json({
                    "type": "work_order_schedule",
                    "payload": {},
                    "existing_rows": [],
                })
                event = ws.receive_json()
        self.assertEqual(event["type"], "error")
        self.assertIn("existing_rows", event["message"])


if __name__ == "__main__":
    unittest.main()
