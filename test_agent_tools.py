"""Agent 工具契约测试。"""

import json
import unittest
from pathlib import Path

from core.artifacts import MemoryArtifactStore, bind_artifact_store, materialize_artifact_delivery
from core.registry import AgentRegistry
from agents.work_order_scheduling.split_and_schedule.tools import (
    prepare_rework_set,
    schedule_work_orders,
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
            ["split_work_orders", "prepare_rework_set", "schedule_work_orders"],
        )


if __name__ == "__main__":
    unittest.main()
