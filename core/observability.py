"""运行时可观测性与成本统计：接 Langfuse 或 Phoenix（OpenInference + OTel）。

设计为“可插拔后端”：框架运行时按 Settings.observability_provider 选择后端，
并把后端产出的 LangChain 回调挂到每次图运行配置上，从而自动采集：
- 各 Agent / 工具 / 模型的调用链路；
- 每轮 LLM 的 token 用量与成本（由目标平台依据 usage 计算）。

默认 provider=none，零依赖、零开销；配置为 langfuse / phoenix / langsmith 后，
会懒加载对应依赖，缺依赖时抛出带安装提示的 RuntimeError。
"""
from __future__ import annotations

import importlib
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from .config import Settings

FlushFn = Callable[[], None]
AFlushFn = Callable[[], Awaitable[None]]


@dataclass(frozen=True)
class Observability:
    """一个可观测性后端：回调集合 + 刷新/关闭钩子。"""

    provider: str
    callbacks: tuple[Any, ...] = ()
    _flush: FlushFn | None = field(default=None, repr=False)
    _aflush: AFlushFn | None = field(default=None, repr=False)

    def flush(self) -> None:
        """同步刷新待上报的 span / event。"""
        if self._flush is not None:
            self._flush()

    async def aflush(self) -> None:
        """异步刷新；后端仅支持同步刷新时在后台线程执行。"""
        if self._aflush is not None:
            await self._aflush()
        elif self._flush is not None:
            import asyncio

            await asyncio.to_thread(self._flush)


def null_observability() -> Observability:
    """默认空后端：不接任何平台。"""
    return Observability(provider="none")


def build_observability(settings: Settings | None = None) -> Observability:
    """根据配置构建可观测性后端。"""
    s = settings or Settings()
    provider = s.observability_provider.strip().lower()
    if not provider or provider == "none":
        return null_observability()
    if provider == "langfuse":
        return _build_langfuse(s)
    if provider == "phoenix":
        return _build_phoenix(s)
    if provider == "langsmith":
        return _build_langsmith(s)
    raise ValueError(
        f"不支持的 observability_provider：{s.observability_provider!r}"
        "（可选值：none / langfuse / phoenix / langsmith）"
    )


def _import_or_raise(module_name: str, hint: str) -> Any:
    try:
        return importlib.import_module(module_name)
    except ImportError as error:
        raise RuntimeError(hint) from error


def _build_langfuse(settings: Settings) -> Observability:
    """接 Langfuse：通过官方 langchain CallbackHandler 采集链路与成本。"""
    langfuse_langchain = _import_or_raise(
        "langfuse.langchain",
        "已配置 observability_provider=langfuse，但未安装 langfuse；"
        "请先安装：pip install langfuse（或 uv add langfuse）",
    )
    public_key = settings.langfuse_public_key or ""
    secret_key = settings.langfuse_secret_key or ""
    if not public_key or not secret_key:
        raise RuntimeError(
            "启用 Langfuse 需要同时配置 LANGFUSE_PUBLIC_KEY 与 LANGFUSE_SECRET_KEY"
        )
    kwargs: dict[str, Any] = {"public_key": public_key, "secret_key": secret_key}
    if settings.langfuse_host:
        kwargs["host"] = settings.langfuse_host
    handler = langfuse_langchain.CallbackHandler(**kwargs)
    return Observability(
        provider="langfuse",
        callbacks=(handler,),
        _flush=handler.flush,
    )


def _build_phoenix(settings: Settings) -> Observability:
    """接 Phoenix：OpenInference 插桩 LangChain，OTLP 上报到 Phoenix Collector。"""
    openinference = _import_or_raise(
        "openinference.instrumentation.langchain",
        "已配置 observability_provider=phoenix，但未安装 OpenInference；"
        "请先安装：pip install arize-phoenix openinference-instrumentation-langchain opentelemetry-exporter-otlp",
    )
    otlp_exporter = _import_or_raise(
        "opentelemetry.exporter.otlp.proto.http.trace_exporter",
        "已配置 observability_provider=phoenix，但未安装 opentelemetry-exporter-otlp",
    )
    sdk_resources = _import_or_raise(
        "opentelemetry.sdk.resources",
        "已配置 observability_provider=phoenix，但未安装 opentelemetry-sdk",
    )
    sdk_trace = _import_or_raise(
        "opentelemetry.sdk.trace",
        "已配置 observability_provider=phoenix，但未安装 opentelemetry-sdk",
    )
    sdk_trace_export = _import_or_raise(
        "opentelemetry.sdk.trace.export",
        "已配置 observability_provider=phoenix，但未安装 opentelemetry-sdk",
    )

    endpoint = settings.phoenix_endpoint or "http://127.0.0.1:6006/v1/traces"
    project_name = settings.phoenix_project_name or "axis"
    tracer_provider = sdk_trace.TracerProvider(
        resource=sdk_resources.Resource(
            attributes={"openinference.project.name": project_name}
        )
    )
    tracer_provider.add_span_processor(
        sdk_trace_export.BatchSpanProcessor(
            otlp_exporter.OTLPSpanExporter(endpoint=endpoint)
        )
    )
    openinference.LangChainInstrumentor().instrument(
        tracer_provider=tracer_provider
    )
    # Phoenix 走进程级 OTel 插桩，不依赖 LangChain callback 列表。
    return Observability(
        provider="phoenix",
        callbacks=(),
        _flush=tracer_provider.force_flush,
    )


def _build_langsmith(settings: Settings) -> Observability:
    """接 LangSmith：通过官方 LangChainTracer 采集链路与成本。"""
    langsmith = _import_or_raise(
        "langsmith",
        "已配置 observability_provider=langsmith，但未安装 langsmith；"
        "请先安装：pip install langsmith（或 uv add langsmith）",
    )
    tracers = _import_or_raise(
        "langchain_core.tracers",
        "已配置 observability_provider=langsmith，但未安装 langchain-core",
    )
    api_key = settings.langsmith_api_key or ""
    if not api_key:
        raise RuntimeError("启用 LangSmith 需要配置 LANGSMITH_API_KEY")
    client = langsmith.Client(
        api_key=api_key,
        api_url=settings.langsmith_endpoint or "https://api.smith.langchain.com",
    )
    tracer = tracers.LangChainTracer(
        project_name=settings.langsmith_project or "axis",
        client=client,
    )
    # LangChainTracer 同步上报，通常无需显式刷新；若存在 flush 钩子则挂上。
    return Observability(
        provider="langsmith",
        callbacks=(tracer,),
        _flush=getattr(tracer, "flush", None),
    )
