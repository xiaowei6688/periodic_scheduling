"""周期工单网关鉴权头。"""

from .config import MainCycleSettings


class ApiError(RuntimeError):
    pass


def auth_headers(settings: MainCycleSettings) -> dict[str, str]:
    missing = [
        name for name, value in (
            ("MAIN_CYCLE_BASIC", settings.basic_auth),
            ("MAIN_CYCLE_TOKEN", settings.token),
            ("MAIN_CYCLE_AUTH_HEADER", settings.auth_header),
        ) if not value
    ]
    if missing:
        raise ApiError(f"缺少周期工单接口配置：{', '.join(missing)}")
    basic = settings.basic_auth
    token = settings.token
    return {
        "Authorization": basic if basic.lower().startswith("basic ") else f"Basic {basic}",
        settings.auth_header: token if token.lower().startswith("bearer ") else f"bearer {token}",
    }
