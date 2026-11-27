"""周期工单业务系统共享 HTTP 客户端。"""

from functools import lru_cache
import json
import threading
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .auth import ApiError, basic_auth_header, token_auth_headers
from .config import MainCycleSettings


class _HttpStatusError(ApiError):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


class MainCycleClient:
    def __init__(self, settings: MainCycleSettings) -> None:
        self.settings = settings
        self._token_lock = threading.Lock()
        self._access_token: str | None = None
        self._expires_at = 0.0

    def _login_configured(self) -> bool:
        return bool(
            self.settings.tenant_id
            or self.settings.username
            or self.settings.password
        )

    def _login(self) -> tuple[str, float]:
        required = {
            "MAIN_CYCLE_TENANT_ID": self.settings.tenant_id,
            "MAIN_CYCLE_USERNAME": self.settings.username,
            "MAIN_CYCLE_PASSWORD": self.settings.password,
        }
        missing = [name for name, value in required.items() if not value]
        if missing:
            raise ApiError(f"缺少周期工单登录配置：{', '.join(missing)}")

        result = self._send(
            self.settings.login_path,
            {
                "tenantId": self.settings.tenant_id,
                "username": self.settings.username,
                "password": self.settings.password,
                "grant_type": self.settings.grant_type,
                "scope": self.settings.scope,
                "type": self.settings.login_type,
            },
            {
                **basic_auth_header(self.settings),
                "Tenant-Id": self.settings.tenant_id,
            },
        )
        code = result.get("code")
        if code not in (None, 200, "200"):
            raise ApiError(
                f"周期工单登录失败 code={code} msg={result.get('msg')!r}")
        data = result.get("data")
        if not isinstance(data, dict) or not data.get("access_token"):
            raise ApiError("周期工单登录响应缺少 data.access_token")
        try:
            expires_in = float(data.get("expires_in"))
        except (TypeError, ValueError) as exc:
            raise ApiError("周期工单登录响应的 data.expires_in 无效") from exc
        if expires_in <= 0:
            raise ApiError("周期工单登录响应的 data.expires_in 必须大于 0")

        token = str(data["access_token"]).strip()
        refresh_ahead = min(60.0, max(1.0, expires_in * 0.1))
        return token, time.monotonic() + max(1.0, expires_in - refresh_ahead)

    def _token(self, *, rejected_token: str | None = None) -> str:
        with self._token_lock:
            now = time.monotonic()
            current_is_usable = (
                self._access_token
                and now < self._expires_at
                and (
                    rejected_token is None
                    or self._access_token != rejected_token
                )
            )
            if current_is_usable:
                return self._access_token

            if self._login_configured():
                self._access_token, self._expires_at = self._login()
                return self._access_token

            if rejected_token is not None:
                raise ApiError(
                    "周期工单 Token 已失效，且未配置动态登录账号")
            if not self.settings.token:
                raise ApiError(
                    "缺少周期工单接口配置：动态登录账号或 MAIN_CYCLE_TOKEN")
            self._access_token = self.settings.token
            self._expires_at = float("inf")
            return self._access_token

    @staticmethod
    def _auth_rejected(result: dict) -> bool:
        return result.get("code") in (401, "401", 403, "403")

    def post(self, path: str, payload: dict[str, Any]) -> dict:
        token = self._token()
        try:
            result = self._send(
                path, payload, token_auth_headers(self.settings, token))
        except _HttpStatusError as exc:
            if exc.status not in {401, 403}:
                raise
            token = self._token(rejected_token=token)
            return self._send(
                path, payload, token_auth_headers(self.settings, token))

        if self._auth_rejected(result):
            token = self._token(rejected_token=token)
            return self._send(
                path, payload, token_auth_headers(self.settings, token))
        return result

    def _send(
        self,
        path: str,
        payload: dict[str, Any],
        headers: dict[str, str],
    ) -> dict:
        url = self.settings.base_url + "/" + path.lstrip("/")
        request = Request(
            url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json", **headers},
        )
        try:
            with urlopen(request, timeout=self.settings.timeout_seconds) as response:
                result = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:300]
            raise _HttpStatusError(
                exc.code, f"HTTP {exc.code} {url} -> {detail}") from exc
        except (URLError, TimeoutError) as exc:
            raise ApiError(f"连接失败 {url}: {exc}") from exc
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ApiError(f"接口返回无效 JSON: {url}") from exc
        if not isinstance(result, dict):
            raise ApiError(f"接口返回格式错误: {url}")
        return result


@lru_cache(maxsize=1)
def get_shared_client() -> MainCycleClient:
    return MainCycleClient(MainCycleSettings.from_env())
