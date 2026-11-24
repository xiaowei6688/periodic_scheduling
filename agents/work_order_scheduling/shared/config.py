"""周期工单接口配置。"""

from dataclasses import dataclass
import os
from pathlib import Path

from dotenv import dotenv_values


ENV_FILE = Path(__file__).resolve().parents[1] / ".env"


@dataclass(frozen=True)
class MainCycleSettings:
    base_url: str
    basic_auth: str
    auth_header: str
    token: str
    timeout_seconds: float = 30.0

    @classmethod
    def from_env(cls) -> "MainCycleSettings":
        local_env = dotenv_values(ENV_FILE)

        def value(name: str, default: str = "") -> str:
            return os.getenv(name, str(local_env.get(name) or default)).strip()

        timeout = float(value("MAIN_CYCLE_TIMEOUT_SECONDS", "30"))
        if timeout <= 0:
            raise ValueError("MAIN_CYCLE_TIMEOUT_SECONDS 必须大于 0")
        return cls(
            base_url=value("MAIN_CYCLE_BASE", "http://10.10.2.101:8000").rstrip("/"),
            basic_auth=value("MAIN_CYCLE_BASIC"),
            auth_header=value("MAIN_CYCLE_AUTH_HEADER", "zxhc-auth"),
            token=value("MAIN_CYCLE_TOKEN"),
            timeout_seconds=timeout,
        )
