"""周期工单共享请求层契约测试。"""

import json
import os
import unittest
from unittest.mock import patch

from agents.work_order_scheduling.shared import ApiError, MainCycleClient, MainCycleSettings
from agents.work_order_scheduling.split_and_schedule import rework_orders


class _Response:
    def __init__(self, value):
        self.value = value

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self):
        return json.dumps(self.value).encode("utf-8")


class TestSharedClient(unittest.TestCase):
    def test_settings_load_agent_env_and_allow_process_override(self):
        local = {
            "MAIN_CYCLE_BASE": "http://from-file.test/",
            "MAIN_CYCLE_BASIC": "file-basic",
            "MAIN_CYCLE_TOKEN": "file-token",
        }
        with patch("agents.work_order_scheduling.shared.config.dotenv_values", return_value=local):
            with patch.dict(os.environ, {"MAIN_CYCLE_BASE": "http://override.test/"}, clear=True):
                settings = MainCycleSettings.from_env()
        self.assertEqual(settings.base_url, "http://override.test")
        self.assertEqual(settings.basic_auth, "file-basic")
        self.assertEqual(settings.token, "file-token")

    def test_post_sends_gateway_headers_and_json(self):
        settings = MainCycleSettings("http://example.test", "abc", "zxhc-auth", "xyz")
        client = MainCycleClient(settings)
        with patch("agents.work_order_scheduling.shared.client.urlopen", return_value=_Response({"code": 200})) as send:
            result = client.post("/work-orders", {"current": 2})
        request = send.call_args.args[0]
        self.assertEqual(result, {"code": 200})
        self.assertEqual(request.full_url, "http://example.test/work-orders")
        self.assertEqual(request.get_header("Authorization"), "Basic abc")
        self.assertEqual(request.get_header("Zxhc-auth"), "bearer xyz")
        self.assertEqual(json.loads(request.data), {"current": 2})

    def test_missing_credentials_fail_before_network(self):
        client = MainCycleClient(MainCycleSettings("http://example.test", "", "zxhc-auth", ""))
        with patch("agents.work_order_scheduling.shared.client.urlopen") as send:
            with self.assertRaisesRegex(ApiError, "MAIN_CYCLE_BASIC, MAIN_CYCLE_TOKEN"):
                client.post("/work-orders", {})
        send.assert_not_called()

    def test_existing_order_pagination_uses_shared_client(self):
        class Client:
            def __init__(self):
                self.calls = []

            def post(self, path, body):
                self.calls.append((path, body))
                page = body["current"]
                return {"code": 200, "data": {"records": [{"workOrderGuid": str(page)}],
                                               "current": page, "pages": 2}}

        client = Client()
        with patch.object(rework_orders, "get_shared_client", return_value=client):
            rows = rework_orders.fetch_existing_orders("2026-11-01", "2026-11-30")
        self.assertEqual([row["workOrderGuid"] for row in rows], ["1", "2"])
        self.assertEqual([body["current"] for _, body in client.calls], [1, 2])
        self.assertEqual(client.calls[0][1]["inspectionMethod"], 1)
        self.assertEqual(client.calls[0][0], rework_orders.API_PATH)


if __name__ == "__main__":
    unittest.main()
