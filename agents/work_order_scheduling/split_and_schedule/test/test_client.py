"""周期工单客户端动态 Token 测试。"""

from __future__ import annotations

import io
import json
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from agents.work_order_scheduling.shared.client import MainCycleClient
from agents.work_order_scheduling.shared.config import MainCycleSettings


class _Response:
    def __init__(self, payload: dict) -> None:
        self.body = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self) -> bytes:
        return self.body


def _settings(**overrides) -> MainCycleSettings:
    values = {
        "base_url": "http://example.test",
        "basic_auth": "Basic client-secret",
        "auth_header": "zxhc-auth",
        "token": "",
        "login_path": "/api/zxhc-system/product/apiLogin",
        "tenant_id": "000000",
        "username": "ZNYJ",
        "password": "encrypted-password",
    }
    values.update(overrides)
    return MainCycleSettings(**values)


class TestMainCycleClient(unittest.TestCase):
    def test_login_token_is_cached_and_sent_to_business_request(self):
        responses = [
            _Response({
                "code": 200,
                "data": {"access_token": "token-1", "expires_in": 3600},
            }),
            _Response({"code": 200, "data": {"records": []}}),
            _Response({"code": 200, "data": {"records": []}}),
        ]
        client = MainCycleClient(_settings())

        with patch(
            "agents.work_order_scheduling.shared.client.urlopen",
            side_effect=responses,
        ) as urlopen:
            client.post("/orders", {"current": 1})
            client.post("/orders", {"current": 2})

        self.assertEqual(urlopen.call_count, 3)
        login_request = urlopen.call_args_list[0].args[0]
        self.assertEqual(
            json.loads(login_request.data),
            {
                "tenantId": "000000",
                "username": "ZNYJ",
                "password": "encrypted-password",
                "grant_type": "password",
                "scope": "all",
                "type": "account",
            },
        )
        self.assertEqual(
            login_request.get_header("Authorization"), "Basic client-secret")
        self.assertEqual(login_request.get_header("Tenant-id"), "000000")
        for call in urlopen.call_args_list[1:]:
            request = call.args[0]
            self.assertEqual(request.get_header("Zxhc-auth"), "bearer token-1")

    def test_expired_cached_token_triggers_new_login(self):
        responses = [
            _Response({
                "code": 200,
                "data": {"access_token": "token-1", "expires_in": 3600},
            }),
            _Response({"code": 200, "data": {}}),
            _Response({
                "code": 200,
                "data": {"access_token": "token-2", "expires_in": 3600},
            }),
            _Response({"code": 200, "data": {}}),
        ]
        client = MainCycleClient(_settings())

        with patch(
            "agents.work_order_scheduling.shared.client.urlopen",
            side_effect=responses,
        ) as urlopen:
            client.post("/orders", {})
            client._expires_at = 0
            client.post("/orders", {})

        self.assertEqual(urlopen.call_count, 4)
        request = urlopen.call_args_list[3].args[0]
        self.assertEqual(request.get_header("Zxhc-auth"), "bearer token-2")

    def test_http_401_refreshes_token_and_retries_once(self):
        unauthorized = HTTPError(
            "http://example.test/orders",
            401,
            "Unauthorized",
            {},
            io.BytesIO(b"expired"),
        )
        responses = [
            _Response({
                "code": 200,
                "data": {"access_token": "token-1", "expires_in": 3600},
            }),
            unauthorized,
            _Response({
                "code": 200,
                "data": {"access_token": "token-2", "expires_in": 3600},
            }),
            _Response({"code": 200, "data": {"records": []}}),
        ]
        client = MainCycleClient(_settings())

        with patch(
            "agents.work_order_scheduling.shared.client.urlopen",
            side_effect=responses,
        ) as urlopen:
            result = client.post("/orders", {})

        self.assertEqual(result["code"], 200)
        self.assertEqual(urlopen.call_count, 4)
        request = urlopen.call_args_list[3].args[0]
        self.assertEqual(request.get_header("Zxhc-auth"), "bearer token-2")

    def test_static_token_remains_supported_without_login_config(self):
        client = MainCycleClient(_settings(
            token="legacy-token",
            tenant_id="",
            username="",
            password="",
        ))

        with patch(
            "agents.work_order_scheduling.shared.client.urlopen",
            return_value=_Response({"code": 200, "data": {}}),
        ) as urlopen:
            client.post("/orders", {})

        self.assertEqual(urlopen.call_count, 1)
        request = urlopen.call_args.args[0]
        self.assertEqual(
            request.get_header("Zxhc-auth"), "bearer legacy-token")


if __name__ == "__main__":
    unittest.main()
