"""沙箱执行能力：把阿里云 OpenSandbox 与腾讯云 CubeSandbox 接入框架。

设计目标（通用、可插拔，与 MCP / Skill 一致）：

1. 配置与使用分离：在 Settings.sandbox_providers 里声明“有哪些沙箱供应商”，
   在 AgentSpec.sandboxes 里声明“哪个专家 Agent 挂载哪些沙箱的执行工具”。
2. 统一执行接口：run_code（执行代码片段） + run_command（执行 shell 命令），
   对上层屏蔽阿里 / 腾讯两套 SDK 的差异。
3. 工具命名默认加 provider 前缀（也可自定义），避免多沙箱同名工具互相覆盖。
4. 后端按需惰性创建：首次真正调用工具时才建连，构建图不产生网络开销；
   运行结束可显式 close 释放云端沙箱。

阿里 OpenSandbox SDK 为异步接口；腾讯 CubeSandbox 走 E2B 兼容 SDK（同步实现），
适配层统一成 async，并用线程池执行同步 SDK 调用，避免阻塞事件循环。
"""
from __future__ import annotations

import asyncio
from collections.abc import Iterable
from datetime import timedelta
from typing import Any, Literal, Protocol

from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel, Field, model_validator

SandboxKind = Literal["alibaba_opensandbox", "tencent_cubesandbox"]


class SandboxConfig(BaseModel):
    """单个沙箱供应商的连接配置。

    - kind=alibaba_opensandbox：阿里云 OpenSandbox；
    - kind=tencent_cubesandbox：腾讯云 CubeSandbox（E2B 兼容 SDK）。
    """

    name: str
    """唯一标识，AgentSpec.sandboxes 按它引用。"""

    kind: SandboxKind
    """供应商类型。"""

    api_key: str = ""
    """API Key；留空时由对应 SDK 读取其约定的环境变量（OPEN_SANDBOX_API_KEY / E2B_API_KEY）。"""

    base_url: str | None = None
    """控制面地址（域名或完整 URL）。留空时由 SDK 使用默认端点或环境变量。"""

    template: str | None = None
    """沙箱镜像/模板；阿里为 image，腾讯为 template。留空时使用供应商默认。"""

    timeout_seconds: float | None = 600
    """单次沙箱生命周期上限（秒），None 表示不过期（由云侧默认策略管理）。"""

    env: dict[str, str] | None = None
    """注入沙箱环境变量。"""

    prefix: str | None = None
    """工具名前缀；None 表示默认用 `<name>_`。"""

    enabled: bool = True
    """False 时跳过该 provider（保留配置但临时停用）。"""

    @model_validator(mode="after")
    def _check_name(self) -> "SandboxConfig":
        if not self.name.strip():
            raise ValueError("sandbox provider 必须提供非空 name")
        return self


class SandboxBackend(Protocol):
    """沙箱后端统一接口。各云厂商适配器实现该协议。"""

    @property
    def config(self) -> SandboxConfig: ...

    async def run_code(self, code: str, *, language: str = "python") -> str:
        """在沙箱中执行代码片段，返回人类可读的输出。"""
        ...

    async def run_command(self, command: str | list[str]) -> str:
        """在沙箱中执行 shell 命令，返回退出码与输出。"""
        ...

    async def close(self) -> None:
        """释放沙箱资源。"""
        ...


def _format_output(
    *,
    stdout: str,
    stderr: str,
    results: list[str] | None = None,
    exit_code: int | None = None,
    error: str | None = None,
) -> str:
    """把各厂商返回的原始输出统一成稳定的中文纯文本。"""
    parts: list[str] = []
    if results:
        parts.append("执行结果：\n" + "\n".join(results))
    if stdout:
        parts.append("标准输出：\n" + stdout)
    if stderr:
        parts.append("标准错误：\n" + stderr)
    if error:
        parts.append("执行错误：\n" + error)
    if exit_code is not None:
        parts.append(f"退出码：{exit_code}")
    return "\n\n".join(parts) if parts else "（无输出）"


class AlibabaOpenSandboxBackend:
    """阿里云 OpenSandbox 适配器。"""

    kind: Literal["alibaba_opensandbox"] = "alibaba_opensandbox"

    def __init__(self, config: SandboxConfig) -> None:
        self._config = config
        self._sandbox: Any = None

    @property
    def config(self) -> SandboxConfig:
        return self._config

    async def _ensure(self) -> Any:
        if self._sandbox is None:
            from opensandbox import Sandbox
            from opensandbox.config.connection import ConnectionConfig

            timeout = (
                timedelta(seconds=self.config.timeout_seconds)
                if self.config.timeout_seconds is not None
                else None
            )
            connection = ConnectionConfig(
                api_key=self.config.api_key or None,
                domain=self.config.base_url or None,
            )
            self._sandbox = await Sandbox.create(
                image=self.config.template,
                connection_config=connection,
                timeout=timeout,
                env=dict(self.config.env) if self.config.env else None,
            )
        return self._sandbox

    async def run_code(self, code: str, *, language: str = "python") -> str:
        if language not in ("python", "python3"):
            raise ValueError(
                f"阿里云 OpenSandbox 当前仅支持 python 代码执行，收到 {language!r}"
            )
        sandbox = await self._ensure()
        execution = await sandbox.commands.run(["python3", "-c", code])
        return _format_output(
            stdout="".join(msg.text for msg in execution.logs.stdout),
            stderr="".join(msg.text for msg in execution.logs.stderr),
            results=[r.text for r in execution.result if r.text],
            exit_code=execution.exit_code,
            error=self._render_execution_error(execution.error),
        )

    async def run_command(self, command: str | list[str]) -> str:
        sandbox = await self._ensure()
        execution = await sandbox.commands.run(command)
        return _format_output(
            stdout="".join(msg.text for msg in execution.logs.stdout),
            stderr="".join(msg.text for msg in execution.logs.stderr),
            results=[r.text for r in execution.result if r.text],
            exit_code=execution.exit_code,
            error=self._render_execution_error(execution.error),
        )

    @staticmethod
    def _render_execution_error(error: Any) -> str | None:
        if error is None:
            return None
        traceback = "\n".join(error.traceback) if getattr(error, "traceback", None) else ""
        return f"{getattr(error, 'name', '')}: {getattr(error, 'value', '')}\n{traceback}".strip()

    async def close(self) -> None:
        if self._sandbox is None:
            return
        try:
            await self._sandbox.kill()
        except Exception:
            # 已销毁/连接失败时无须向上抛，释放动作尽力而为。
            pass
        finally:
            self._sandbox = None


class TencentCubeSandboxBackend:
    """腾讯云 CubeSandbox 适配器（基于 E2B 兼容 SDK）。"""

    kind: Literal["tencent_cubesandbox"] = "tencent_cubesandbox"

    def __init__(self, config: SandboxConfig) -> None:
        self._config = config
        self._sandbox: Any = None

    @property
    def config(self) -> SandboxConfig:
        return self._config

    def _ensure(self) -> Any:
        if self._sandbox is None:
            from e2b_code_interpreter import Sandbox

            timeout = (
                int(self.config.timeout_seconds)
                if self.config.timeout_seconds is not None
                else None
            )
            self._sandbox = Sandbox.create(
                template=self.config.template or "code-interpreter-v1",
                timeout=timeout,
                envs=dict(self.config.env) if self.config.env else None,
                api_key=self.config.api_key or None,
                domain=self.config.base_url or None,
            )
        return self._sandbox

    async def run_code(self, code: str, *, language: str = "python") -> str:
        sandbox = await asyncio.to_thread(self._ensure)
        execution = await asyncio.to_thread(sandbox.run_code, code, language)
        return _format_output(
            stdout="".join(execution.logs.stdout),
            stderr="".join(execution.logs.stderr),
            results=[r.text for r in execution.results if r.text],
            error=self._render_execution_error(execution.error),
        )

    async def run_command(self, command: str | list[str]) -> str:
        sandbox = await asyncio.to_thread(self._ensure)
        result = await asyncio.to_thread(sandbox.commands.run, command)
        return _format_output(
            stdout=result.stdout,
            stderr=result.stderr,
            exit_code=result.exit_code,
            error=result.error or None,
        )

    @staticmethod
    def _render_execution_error(error: Any) -> str | None:
        if error is None:
            return None
        traceback = getattr(error, "traceback", "") or ""
        return f"{getattr(error, 'name', '')}: {getattr(error, 'value', '')}\n{traceback}".strip()

    async def close(self) -> None:
        if self._sandbox is None:
            return
        try:
            await asyncio.to_thread(self._sandbox.kill)
        except Exception:
            pass
        finally:
            self._sandbox = None


def create_sandbox_backend(config: SandboxConfig) -> SandboxBackend:
    """按配置创建对应厂商的后端适配器。"""
    if config.kind == "alibaba_opensandbox":
        return AlibabaOpenSandboxBackend(config)
    if config.kind == "tencent_cubesandbox":
        return TencentCubeSandboxBackend(config)
    raise ValueError(f"不支持的沙箱类型：{config.kind!r}")


class _RunCodeArgs(BaseModel):
    """沙箱 run_code 工具参数。"""

    code: str = Field(description="要执行的代码片段")
    language: str = Field(default="python", description="代码语言，默认 python")


class _RunCommandArgs(BaseModel):
    """沙箱 run_command 工具参数。"""

    command: str = Field(description="要执行的 shell 命令")


def _make_run_code_tool(backend: SandboxBackend, *, name: str) -> BaseTool:
    async def run_code(code: str, language: str = "python") -> str:
        return await backend.run_code(code, language=language)

    return StructuredTool.from_function(
        coroutine=run_code,
        name=name,
        description="在沙箱中执行代码片段（默认 Python），返回最终结果、标准输出与标准错误。适合计算、数据处理与脚本验证。",
        args_schema=_RunCodeArgs,
    )


def _make_run_command_tool(backend: SandboxBackend, *, name: str) -> BaseTool:
    async def run_command(command: str) -> str:
        return await backend.run_command(command)

    return StructuredTool.from_function(
        coroutine=run_command,
        name=name,
        description="在沙箱中执行 shell 命令，返回退出码、标准输出与标准错误。",
        args_schema=_RunCommandArgs,
    )


def make_sandbox_tools(name: str, backend: SandboxBackend) -> list[BaseTool]:
    """为一个沙箱后端生成 LangChain 工具列表（run_code + run_command）。"""
    prefix = backend.config.prefix if backend.config.prefix is not None else f"{name}_"
    return [
        _make_run_code_tool(backend, name=f"{prefix}run_code"),
        _make_run_command_tool(backend, name=f"{prefix}run_command"),
    ]


def load_sandbox_backends(
    providers: Iterable[SandboxConfig],
    *,
    names: Iterable[str] | None = None,
) -> dict[str, SandboxBackend]:
    """按声明加载一批沙箱后端，返回 {provider_name: 后端}。

    - providers：全部可用 provider 配置（通常来自 Settings.sandbox_providers）。
    - names：本次实际要挂载的 provider 名（通常来自各 AgentSpec.sandboxes 并集）；
      为 None 时加载全部。未在 providers 里声明的名字会直接报错，避免静默漏挂。

    后端自身是惰性建连，本函数只做配置校验与对象创建，不产生网络请求。
    """
    provider_list = [p for p in providers if p.enabled]
    by_name = {p.name: p for p in provider_list}
    if len(by_name) != len(provider_list):
        seen: set[str] = set()
        duplicates: list[str] = []
        for provider in provider_list:
            if provider.name in seen:
                duplicates.append(provider.name)
            seen.add(provider.name)
        raise ValueError(
            "sandbox provider name 必须唯一，发现重复："
            + "、".join(dict.fromkeys(duplicates))
        )
    if names is not None:
        wanted = list(dict.fromkeys(names))
        unknown = [n for n in wanted if n not in by_name]
        if unknown:
            raise ValueError(
                "以下 sandbox provider 未在 Settings.sandbox_providers 中配置："
                + "、".join(unknown)
                + "。请先配置 SANDBOX_PROVIDERS 环境变量或在代码中注入。"
            )
        provider_list = [by_name[n] for n in wanted]

    return {
        provider.name: create_sandbox_backend(provider)
        for provider in provider_list
    }


async def close_sandbox_backends(
    backends: Iterable[SandboxBackend],
) -> None:
    """释放一批沙箱后端占用的云端资源。"""
    for backend in backends:
        await backend.close()


__all__ = [
    "SandboxConfig",
    "SandboxBackend",
    "AlibabaOpenSandboxBackend",
    "TencentCubeSandboxBackend",
    "create_sandbox_backend",
    "make_sandbox_tools",
    "load_sandbox_backends",
    "close_sandbox_backends",
]
