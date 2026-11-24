"""Agent 工具契约测试。"""

import json
import unittest
from pathlib import Path

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

    def test_split_returns_orders_and_details(self):
        result = json.loads(split_work_orders.invoke({"payload": self.payload}))
        self.assertEqual(result["summary"]["order_count"], 16)
        self.assertEqual(result["summary"]["detail_count"], 78)
        self.assertEqual(len(result["orders"]), 16)

    def test_prepare_and_schedule_offline(self):
        pending = json.loads(prepare_rework_set.invoke({
            "payload": self.payload, "existing_rows": [],
        }))
        self.assertEqual(pending["summary"]["new_orders"], 16)
        self.assertEqual(pending["summary"]["old_orders"], 0)

        scheduled = json.loads(schedule_work_orders.invoke({
            "payload": self.payload,
            "existing_rows": [],
            "skip_weekdays": [7],
            "holidays": ["2026-11-03"],
        }))
        self.assertEqual(scheduled["planSetInfo"]["workOrderCount"], 16)
        self.assertIn(scheduled["entry"], {"normal", "insert_rework", "force_insert"})

    def test_missing_payload_fields_raise(self):
        with self.assertRaisesRegex(ValueError, "planSetInfo"):
            split_work_orders.invoke({"payload": {}})

    def test_agent_is_discoverable_with_all_tools(self):
        registry = AgentRegistry().discover_package("agents")
        self.assertEqual(registry.names(), ["split_and_schedule"])
        self.assertEqual(
            [item.name for item in registry.get("split_and_schedule").tools],
            ["split_work_orders", "prepare_rework_set", "schedule_work_orders"],
        )


if __name__ == "__main__":
    unittest.main()
