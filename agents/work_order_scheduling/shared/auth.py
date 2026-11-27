"""周期工单网关鉴权头。"""

from .config import MainCycleSettings


class ApiError(RuntimeError):
    pass


def basic_auth_header(settings: MainCycleSettings) -> dict[str, str]:
    if not settings.basic_auth:
        raise ApiError("缺少周期工单接口配置：MAIN_CYCLE_BASIC")
    basic = settings.basic_auth
    return {
        "Authorization": basic if basic.lower().startswith("basic ") else f"Basic {basic}",
    }


def token_auth_headers(settings: MainCycleSettings, token: str) -> dict[str, str]:
    missing = [
        name for name, value in (
            ("MAIN_CYCLE_AUTH_HEADER", settings.auth_header),
            ("access_token", token),
        ) if not value
    ]
    if missing:
        raise ApiError(f"缺少周期工单接口配置：{', '.join(missing)}")
    return {
        **basic_auth_header(settings),
        settings.auth_header: token if token.lower().startswith("bearer ") else f"bearer {token}",
    }


def auth_headers(settings: MainCycleSettings) -> dict[str, str]:
    """兼容仍使用静态 MAIN_CYCLE_TOKEN 的调用方。"""
    return token_auth_headers(settings, settings.token)
