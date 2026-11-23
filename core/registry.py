"""Agent 插件注册表：定义与组合解耦，Agent 可插拔、可随意增删。"""
from __future__ import annotations

import importlib
import pkgutil
import re
from dataclasses import dataclass, field
from typing import Any, Iterable

from .facts import FactSpec


_COMPONENT_NAME_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")
_MAX_COMPONENT_NAME_LENGTH = 45


def validate_component_name(name: str, kind: str) -> None:
    """Validate names used in graph nodes and generated model tool names."""
    if not isinstance(name, str) or not name:
        raise ValueError(f"{kind} 必须提供非空 name")
    if len(name) > _MAX_COMPONENT_NAME_LENGTH:
        raise ValueError(
            f"{kind} name 最长 {_MAX_COMPONENT_NAME_LENGTH} 个字符：{name!r}"
        )
    if not _COMPONENT_NAME_PATTERN.fullmatch(name):
        raise ValueError(
            f"{kind} name 只能以英文字母开头，并且只包含英文字母、数字、下划线或连字符：{name!r}"
        )


@dataclass
class AgentSpec:
    """描述一个 Agent 插件的最小契约。"""

    name: str                                             # 唯一标识，也是图节点名 / handoff 目标
    description: str                                      # 给 supervisor 路由用的能力描述
    system_prompt: str                                    # 该 Agent 的人设与规则
    tools: list[Any] = field(default_factory=list)        # 业务工具（langchain 工具）
    handoffs: list[str] = field(default_factory=list)     # 可直接交接的兄弟 Agent 名
    parallel_handoffs: list[str] = field(default_factory=list)  # 可一次性并行委派的一组兄弟 Agent（fan-out）
    required_facts: list[FactSpec] = field(default_factory=list)  # 动作前必须收集齐的事实
    required_actions: list[str] = field(default_factory=list)      # 给出结论前必须调用过的工具（框架级硬保证）
    max_action_retries: int = 2                            # 未调用声明动作时，允许自动重试的最大次数
    skills: list[str] = field(default_factory=list)       # 装配时注入的 Skill（名字或路径）
    mcp_servers: list[str] = field(default_factory=list)  # 装配时挂载的 MCP server 名
    sandboxes: list[str] = field(default_factory=list)       # 装配时挂载的沙箱 provider 名
    metadata: dict[str, Any] = field(default_factory=dict)  # 扩展字段，留作他用
    group: str | None = None                              # 两级路由时所属领域 Router
    display_name: str = ""                                # 步骤中的用户可见名称


@dataclass
class RouterSpec:
    """领域 Router 契约：总 Supervisor 先选领域，Router 再选组内 Agent。"""

    name: str
    description: str
    system_prompt: str = ""
    display_name: str = ""


class AgentRegistry:
    """集中登记 AgentSpec。"""

    def __init__(self) -> None:
        self._specs: dict[str, AgentSpec] = {}
        self._routers: dict[str, RouterSpec] = {}

    def register(self, spec: AgentSpec) -> AgentSpec:
        if not spec:
            raise ValueError("AgentSpec 不能为空")
        validate_component_name(spec.name, "AgentSpec")
        if spec.name in self._specs:
            raise ValueError(f"AgentSpec name 重复注册：{spec.name}")
        self._specs[spec.name] = spec
        return spec

    def register_many(self, specs: Iterable[AgentSpec]) -> "AgentRegistry":
        for spec in specs:
            self.register(spec)
        return self

    def register_router(self, router: RouterSpec) -> RouterSpec:
        if not router:
            raise ValueError("RouterSpec 不能为空")
        validate_component_name(router.name, "RouterSpec")
        if router.name in self._routers:
            raise ValueError(f"RouterSpec name 重复注册：{router.name}")
        self._routers[router.name] = router
        return router

    def register_routers(self, routers: Iterable[RouterSpec]) -> "AgentRegistry":
        for router in routers:
            self.register_router(router)
        return self

    def get(self, name: str) -> AgentSpec:
        return self._specs[name]

    def has(self, name: str) -> bool:
        return name in self._specs

    def all(self) -> list[AgentSpec]:
        return list(self._specs.values())

    def names(self) -> list[str]:
        return list(self._specs.keys())

    def routers(self) -> list[RouterSpec]:
        return list(self._routers.values())

    def router_names(self) -> list[str]:
        return list(self._routers.keys())

    def __len__(self) -> int:
        return len(self._specs)

    def __iter__(self):
        return iter(self._specs.values())

    # ---------------------------------------------------------- 插件发现
    def discover_package(self, package_name: str) -> "AgentRegistry":
        """导入一个 Python 包下的所有模块，收集其中的 AgentSpec。

        插件模块约定：暴露 `spec: AgentSpec` 或 `specs: list[AgentSpec]`。
        """
        pkg = importlib.import_module(package_name)
        path = getattr(pkg, "__path__", None)
        if not path:
            raise ValueError(f"{package_name!r} 不是一个包")
        prefix = pkg.__name__ + "."
        _collect_from_module(pkg, self)
        for info in pkgutil.iter_modules(path, prefix):
            module = importlib.import_module(info.name)
            _collect_from_module(module, self)
        return self

    def discover_modules(self, module_names: Iterable[str]) -> "AgentRegistry":
        for name in module_names:
            module = importlib.import_module(name)
            _collect_from_module(module, self)
        return self


def _collect_from_module(module, registry: AgentRegistry) -> None:
    candidates: list[Any] = []
    for attr in ("specs", "spec"):
        if hasattr(module, attr):
            value = getattr(module, attr)
            if isinstance(value, list):
                candidates.extend(value)
            elif isinstance(value, AgentSpec):
                candidates.append(value)
    for c in candidates:
        if isinstance(c, AgentSpec):
            registry.register(c)
    router_candidates: list[Any] = []
    for attr in ("routers", "router"):
        if hasattr(module, attr):
            value = getattr(module, attr)
            if isinstance(value, list):
                router_candidates.extend(value)
            elif isinstance(value, RouterSpec):
                router_candidates.append(value)
    for candidate in router_candidates:
        if isinstance(candidate, RouterSpec):
            registry.register_router(candidate)


# 进程内默认注册表：适合单应用直接使用。
registry = AgentRegistry()


def register(spec: AgentSpec) -> AgentSpec:
    return registry.register(spec)
