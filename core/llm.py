"""模型工厂：统一走 OpenAI 兼容接口，DeepSeek / Qwen / vLLM / Ollama 均可。"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_openai import ChatOpenAI
from pydantic import SecretStr

from .config import Settings

ModelFactory = Callable[[Settings, dict[str, Any]], BaseChatModel]
_PROVIDERS: dict[str, ModelFactory] = {}


def register_model_provider(name: str, factory: ModelFactory) -> None:
    """Register a model provider factory. Existing providers may be replaced explicitly."""
    normalized = name.strip().lower()
    if not normalized:
        raise ValueError("模型 provider 名称不能为空")
    if not callable(factory):
        raise TypeError("模型 provider factory 必须可调用")
    _PROVIDERS[normalized] = factory


def _openai_compatible(settings: Settings, overrides: dict[str, Any]) -> BaseChatModel:
    return ChatOpenAI(
        model=settings.llm_model,
        api_key=SecretStr(settings.llm_api_key or "EMPTY"),
        base_url=settings.llm_base_url or None,
        temperature=settings.llm_temperature,
        timeout=120,
        max_retries=2,
        **overrides,
        stream_usage=True,
        streaming=True,
        extra_body={"enable_thinking": settings.llm_enable_thinking},
    )


register_model_provider("openai-compatible", _openai_compatible)


def create_chat_model(settings: Settings | None = None, **overrides: Any) -> BaseChatModel:
    """根据配置创建一个 Chat 模型实例。

    额外参数（如 temperature、timeout）可以通过 overrides 覆盖。
    """
    s = settings or Settings()
    provider = s.llm_provider.strip().lower()
    try:
        factory = _PROVIDERS[provider]
    except KeyError as error:
        raise ValueError(f"未注册模型 provider：{s.llm_provider!r}") from error
    return factory(s, overrides)
