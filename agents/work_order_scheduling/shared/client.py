"""周期工单业务系统共享 HTTP 客户端。"""

from functools import lru_cache
import json
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .auth import ApiError, auth_headers
from .config import MainCycleSettings


class MainCycleClient:
    def __init__(self, settings: MainCycleSettings) -> None:
        self.settings = settings

    def post(self, path: str, payload: dict[str, Any]) -> dict:
        url = self.settings.base_url + "/" + path.lstrip("/")
        request = Request(
            url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json", **auth_headers(self.settings)},
        )
        try:
            with urlopen(request, timeout=self.settings.timeout_seconds) as response:
                result = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:300]
            raise ApiError(f"HTTP {exc.code} {url} -> {detail}") from exc
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
