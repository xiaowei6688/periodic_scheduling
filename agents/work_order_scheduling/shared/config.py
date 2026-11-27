"""周期工单接口配置。"""

from dataclasses import dataclass
import os
from pathlib import Path

from dotenv import dotenv_values


ENV_FILE = Path(__file__).resolve().parents[1] / ".env"


def _time_seconds(value: str, name: str) -> int:
    parts = value.strip().split(":")
    if len(parts) not in {2, 3}:
        raise ValueError(f"{name} 必须为 HH:MM 或 HH:MM:SS")
    try:
        hour, minute = int(parts[0]), int(parts[1])
        second = int(parts[2]) if len(parts) == 3 else 0
    except ValueError as exc:
        raise ValueError(f"{name} 必须为有效时间") from exc
    if not 0 <= hour <= 23 or not 0 <= minute <= 59 or not 0 <= second <= 59:
        raise ValueError(f"{name} 必须为有效时间")
    return hour * 3600 + minute * 60 + second


def _window(value: str, name: str) -> tuple[int, int]:
    start_text, separator, end_text = value.strip().partition("-")
    if not separator:
        raise ValueError(f"{name} 必须为 HH:MM:SS-HH:MM:SS")
    start = _time_seconds(start_text, name)
    end = _time_seconds(end_text, name)
    if end <= start:
        raise ValueError(f"{name} 的结束时间必须晚于开始时间")
    return start, end


def _validate_windows(value: str, name: str) -> str:
    segments = [segment.strip() for segment in value.split(";") if segment.strip()]
    if not segments:
        raise ValueError(f"{name} 不能为空")
    for segment in segments:
        _window(segment, name)
    return ";".join(segments)


@dataclass(frozen=True)
class MainCycleSettings:
    base_url: str
    basic_auth: str
    auth_header: str
    token: str
    timeout_seconds: float = 30.0
    login_path: str = "/api/zxhc-system/product/apiLogin"
    tenant_id: str = ""
    username: str = ""
    password: str = ""
    grant_type: str = "password"
    scope: str = "all"
    login_type: str = "account"

    @classmethod
    def from_env(cls) -> "MainCycleSettings":
        local_env = dotenv_values(ENV_FILE)

        def value(name: str, default: str = "") -> str:
            return os.getenv(name, str(local_env.get(name) or default)).strip()

        timeout = float(value("MAIN_CYCLE_TIMEOUT_SECONDS", "30"))
        if timeout <= 0:
            raise ValueError("MAIN_CYCLE_TIMEOUT_SECONDS 必须大于 0")
        login_path = value(
            "MAIN_CYCLE_LOGIN_PATH",
            "/api/zxhc-system/product/apiLogin",
        )
        if not login_path.startswith("/"):
            raise ValueError("MAIN_CYCLE_LOGIN_PATH 必须以 / 开头")
        return cls(
            base_url=value("MAIN_CYCLE_BASE", "http://10.10.2.101:8000").rstrip("/"),
            basic_auth=value("MAIN_CYCLE_BASIC"),
            auth_header=value("MAIN_CYCLE_AUTH_HEADER", "zxhc-auth"),
            token=value("MAIN_CYCLE_TOKEN"),
            timeout_seconds=timeout,
            login_path=login_path,
            tenant_id=value("MAIN_CYCLE_TENANT_ID"),
            username=value("MAIN_CYCLE_USERNAME"),
            password=value("MAIN_CYCLE_PASSWORD"),
            grant_type=value("MAIN_CYCLE_GRANT_TYPE", "password"),
            scope=value("MAIN_CYCLE_SCOPE", "all"),
            login_type=value("MAIN_CYCLE_LOGIN_TYPE", "account"),
        )


@dataclass(frozen=True)
class ScheduleSettings:
    """工单拆分与排班的可部署业务参数。"""

    day_seconds_visible: int = 8 * 3600
    day_seconds_infrared: int = 6 * 3600
    quota_ratio: float = 0.80
    fallback_seconds: int = 3600
    default_window_visible: str = "08:30:00-17:30:00"
    default_window_infrared: str = "05:00:00-08:00:00;19:00:00-22:00:00"
    force_window_visible: tuple[int, int] = (16 * 3600 + 30 * 60, 17 * 3600 + 30 * 60)
    force_window_infrared: tuple[int, int] = (21 * 3600, 22 * 3600)
    sms_visible_start: int = 8 * 3600 + 30 * 60
    existing_orders_api_path: str = "/api/main-cycle-server/workOrderInfo/getCycleOrderProjectPlanPage"

    @classmethod
    def from_env(cls) -> "ScheduleSettings":
        local_env = dotenv_values(ENV_FILE)

        def value(name: str, default: str) -> str:
            return os.getenv(name, str(local_env.get(name) or default)).strip()

        visible_seconds = int(value("SCHEDULE_DAY_SECONDS_VISIBLE", str(8 * 3600)))
        infrared_seconds = int(value("SCHEDULE_DAY_SECONDS_INFRARED", str(6 * 3600)))
        fallback_seconds = int(value("SCHEDULE_FALLBACK_SECONDS", "3600"))
        if min(visible_seconds, infrared_seconds, fallback_seconds) <= 0:
            raise ValueError("排班作业秒数配置必须大于 0")

        quota_ratio = float(value("SCHEDULE_QUOTA_RATIO", "0.80"))
        if not 0 < quota_ratio <= 1:
            raise ValueError("SCHEDULE_QUOTA_RATIO 必须大于 0 且不超过 1")

        api_path = value(
            "SCHEDULE_EXISTING_ORDERS_API_PATH",
            "/api/main-cycle-server/workOrderInfo/getCycleOrderProjectPlanPage",
        )
        if not api_path.startswith("/"):
            raise ValueError("SCHEDULE_EXISTING_ORDERS_API_PATH 必须以 / 开头")

        return cls(
            day_seconds_visible=visible_seconds,
            day_seconds_infrared=infrared_seconds,
            quota_ratio=quota_ratio,
            fallback_seconds=fallback_seconds,
            default_window_visible=_validate_windows(
                value("SCHEDULE_DEFAULT_WINDOW_VISIBLE", "08:30:00-17:30:00"),
                "SCHEDULE_DEFAULT_WINDOW_VISIBLE",
            ),
            default_window_infrared=_validate_windows(
                value(
                    "SCHEDULE_DEFAULT_WINDOW_INFRARED",
                    "05:00:00-08:00:00;19:00:00-22:00:00",
                ),
                "SCHEDULE_DEFAULT_WINDOW_INFRARED",
            ),
            force_window_visible=_window(
                value("SCHEDULE_FORCE_WINDOW_VISIBLE", "16:30:00-17:30:00"),
                "SCHEDULE_FORCE_WINDOW_VISIBLE",
            ),
            force_window_infrared=_window(
                value("SCHEDULE_FORCE_WINDOW_INFRARED", "21:00:00-22:00:00"),
                "SCHEDULE_FORCE_WINDOW_INFRARED",
            ),
            sms_visible_start=_time_seconds(
                value("SCHEDULE_SMS_VISIBLE_START", "08:30:00"),
                "SCHEDULE_SMS_VISIBLE_START",
            ),
            existing_orders_api_path=api_path,
        )


schedule_settings = ScheduleSettings.from_env()
