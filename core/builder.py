"""图装配器：把 AgentSpec 装配成一个 Supervisor + Handoff 的 LangGraph 图。"""
from __future__ import annotations

import asyncio
from collections.abc import Hashable, Iterable, Sequence
from typing import Any

from langchain.agents import create_agent
from langchain.agents.middleware import AgentMiddleware, ModelResponse
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolCall, ToolMessage
from langchain_core.tools import BaseTool
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph

from .config import Settings
from .facts import build_agent_prompt
from .handoff import (
    DYNAMIC_PARALLEL_TOOL_NAME,
    FINISH_WITHOUT_REPLY_TOOL_NAME,
    make_dynamic_parallel_handoff_tool,
    make_finish_without_reply_tool,
    make_handoff_tool,
    make_parallel_handoff_tool,
)
from .mcp import load_mcp_tools
from .approval import is_rejected_output, rejection_reason
from .artifacts import ArtifactStore, MemoryArtifactStore, bind_artifact_store
from .workflow_state import WorkflowStateStore, bind_workflow_state_store, create_workflow_state_store
from .memory import (
    create_async_checkpointer,
    create_checkpointer,
    needs_compression,
    summarize_and_trim,
)
from .messages import visible_text
from .registry import AgentSpec, RouterSpec, validate_component_name
from .sandbox import (
    SandboxBackend,
    close_sandbox_backends,
    load_sandbox_backends,
    make_sandbox_tools,
)
from .skills import render_skill_block, resolve_skills
from .state import AgentState

DEFAULT_SUPERVISOR_PROMPT = """\
你是智能体编排总调度（supervisor）。你的职责：根据用户诉求，把任务委派给最合适的专家 Agent，
并在专家完成工作后汇总结果、继续决策，直到你能直接、完整地回答用户为止。

规则：
1. 需要一个专家时调用对应的 transfer_to_<agent>；多个任务无先后依赖时调用一次 parallel_to_agents，
   并在 assignments 中为每个目标写明具体且互不重叠的子任务；
2. 如果你已经掌握足够信息，能直接回答用户，就直接输出最终答案，**不要调用任何工具**；
3. 专家完成后继续判断是否需要其他专家；需要则继续委派，不需要则调用 finish_without_reply 静默结束；
4. 不要复述、总结或改写专家已经给出的最终答复；
5. 如果专家只是向用户提出澄清问题、收集缺失事实，请原样转达该问题，不要替专家编造结论或直接给出处置意见，也**不要再次调用任何 transfer_to_<agent> 工具**，应结束本轮等待用户补充；
6. 用户的一条消息包含多个独立诉求时，必须逐项识别并为每项委派合适的 Agent；不得因一个 Agent 已完成其部分而遗漏其他诉求。只有所有可执行子任务都有对应专家结果后才能结束本轮。
"""

DEFAULT_HIERARCHICAL_SUPERVISOR_PROMPT = """\
你是智能体编排总调度（supervisor）。你的职责是判断用户请求属于哪个领域，必要时委派给领域 Router，
并在专家完成工作后汇总结果、继续决策，直到你能直接、完整地回答用户为止。

规则：
1. 需要专业能力时，只能调用 transfer_to_router_<group> 工具选择领域，不要直接选择具体专家；
   多个领域可并行时，在 assignments 中为每个领域写明具体且互不重叠的子任务；
2. 如果你已经掌握足够信息，能直接回答用户，就直接输出最终答案，不要调用工具；
3. 专家完成后继续判断是否需要其他领域；需要则继续委派，不需要则调用 finish_without_reply 静默结束；
4. 不要复述、总结或改写专家已经给出的最终答复；
5. 如果专家正在向用户收集缺失事实，请原样转达该问题并结束本轮，不要重复委派。
6. 用户的一条消息包含多个独立诉求时，必须逐项识别并委派给相应领域；不得因一个领域已完成其部分而遗漏其他诉求。只有所有可执行子任务都有对应专家结果后才能结束本轮。
"""

DEFAULT_ROUTER_PROMPT = """\
你是领域路由器。你只负责根据用户请求和已有上下文，选择本领域最合适的专家 Agent。
必须调用 transfer_to_<agent> 或 parallel_to_agents 工具完成委派；
并行委派时，必须在 assignments 中为每个专家写明具体且互不重叠的子任务；
同一专家负责多个诉求时，只在 targets 中列出一次，并将诉求合并到该专家的 assignment；
不要自己回答业务问题，不要委派给本领域之外的 Agent。
"""


class _CoordinatorControlMiddleware(AgentMiddleware):
    """Keep coordinator control flow to one tool call per model response."""

    def __init__(self, builder: "GraphBuilder", *, dynamic_continuation: bool) -> None:
        self._builder = builder
        self._dynamic_continuation = dynamic_continuation

    @staticmethod
    def _tool_name(tool: Any) -> str:
        return str(getattr(tool, "name", ""))

    @staticmethod
    def _metadata(tool: Any) -> dict[str, Any]:
        metadata = getattr(tool, "metadata", None)
        return metadata if isinstance(metadata, dict) else {}

    @staticmethod
    def _is_control_call(name: str) -> bool:
        return (
            name.startswith("transfer_to_")
            or name == DYNAMIC_PARALLEL_TOOL_NAME
            or name == FINISH_WITHOUT_REPLY_TOOL_NAME
        )

    def _prepare_request(self, request: Any):
        tools = list(request.tools)
        model_settings = {**request.model_settings, "parallel_tool_calls": False}
        has_expert_result = bool(
            self._builder._experts_in_current_turn(request.state)
        )
        if not self._dynamic_continuation or not has_expert_result:
            tools = [
                tool
                for tool in tools
                if self._tool_name(tool) != FINISH_WITHOUT_REPLY_TOOL_NAME
            ]
            return request.override(tools=tools, model_settings=model_settings)
        return request.override(
            tools=tools,
            tool_choice="required",
            model_settings=model_settings,
        )

    def _parallel_replacement(
        self, request: Any, calls: Sequence[ToolCall]
    ) -> ToolCall | None:
        if not calls or not all(
            str(call.get("name") or "").startswith("transfer_to_") for call in calls
        ):
            return calls[0] if calls else None
        tools = {self._tool_name(tool): tool for tool in request.tools}
        if DYNAMIC_PARALLEL_TOOL_NAME not in tools:
            return calls[0]
        labels: list[str] = []
        for call in calls:
            tool = tools.get(str(call.get("name") or ""))
            label = self._metadata(tool).get("axis_handoff_label") if tool else None
            if not isinstance(label, str) or not label:
                return calls[0]
            if label not in labels:
                labels.append(label)
        if len(labels) < 2:
            return calls[0]
        return ToolCall(
            name=DYNAMIC_PARALLEL_TOOL_NAME,
            args={
                "targets": labels,
                "assignments": {
                    label: f"只处理原始用户请求中属于「{label}」职责范围的部分。"
                    for label in labels
                },
            },
            id=calls[0].get("id") or "axis_parallel_dispatch",
            type="tool_call",
        )

    def _normalize_response(self, request: Any, response: ModelResponse[Any]):
        control_calls = [
            call
            for message in response.result
            if isinstance(message, AIMessage)
            for call in message.tool_calls
            if self._is_control_call(str(call.get("name") or ""))
        ]
        if len(control_calls) <= 1:
            return response
        replacement = self._parallel_replacement(request, control_calls)
        if replacement is None:
            return response

        replaced = False
        messages = []
        for message in response.result:
            if not isinstance(message, AIMessage):
                messages.append(message)
                continue
            remaining = [
                call
                for call in message.tool_calls
                if not self._is_control_call(str(call.get("name") or ""))
            ]
            if not replaced:
                remaining.append(replacement)
                replaced = True
            messages.append(message.model_copy(update={"tool_calls": remaining}))
        return ModelResponse(
            result=messages,
            structured_response=response.structured_response,
        )

    def wrap_model_call(self, request: Any, handler: Any):
        prepared = self._prepare_request(request)
        return self._normalize_response(prepared, handler(prepared))

    async def awrap_model_call(self, request: Any, handler: Any):
        prepared = self._prepare_request(request)
        return self._normalize_response(prepared, await handler(prepared))


class GraphBuilder:
    def __init__(
        self,
        specs: Iterable[AgentSpec],
        model: BaseChatModel,
        *,
        settings: Settings | None = None,
        supervisor_prompt: str | None = None,
        routers: Iterable[RouterSpec] | None = None,
        artifact_store: ArtifactStore | None = None,
        workflow_state_store: WorkflowStateStore | None = None,
    ) -> None:
        specs = list(specs)
        if not specs:
            raise ValueError("至少需要注册一个 Agent")
        names = [s.name for s in specs]
        for name in names:
            validate_component_name(name, "AgentSpec")
        if len(names) != len(set(names)):
            raise ValueError(f"Agent name 必须唯一，发现重复：{names}")

        self.specs = specs
        self._by_name = {s.name: s for s in specs}
        self._agent_names = {s.name for s in specs}
        self.routers = list(routers or [])
        self._router_by_name = {router.name: router for router in self.routers}
        self._router_nodes = {
            router.name: self._router_node_name(router.name) for router in self.routers
        }
        self.settings = settings or Settings()
        self.artifact_store = artifact_store if artifact_store is not None else MemoryArtifactStore()
        self.workflow_state_store = create_workflow_state_store(self.settings, workflow_state_store)
        validate_component_name(self.settings.supervisor_name, "supervisor")
        if self.settings.supervisor_name in self._agent_names:
            raise ValueError("Agent name 不能与 supervisor_name 重复")
        self._validate_routers()
        # 声明为“必须执行”的动作工具必须经过审批包装，否则拒绝装配（框架级硬保证）。
        self._validate_required_actions_approval()
        # 去重后的子 Agent 并行 handoff 组合；协调节点使用独立的动态并行工具。
        self._parallel_groups = self._validate_parallel_groups()
        self.model = model
        self._validate_specs()
        self.sandbox_backends: dict[str, SandboxBackend] = {}
        configured_supervisor_prompt = supervisor_prompt or self.settings.supervisor_prompt
        self.supervisor_prompt = configured_supervisor_prompt or (
            DEFAULT_HIERARCHICAL_SUPERVISOR_PROMPT
            if self.routers
            else DEFAULT_SUPERVISOR_PROMPT
        )

    @staticmethod
    def _router_node_name(name: str) -> str:
        return f"router_{name}"

    def _validate_routers(self) -> None:
        """分层模式要求所有 Agent 都属于唯一、已声明的领域。"""
        if not self.routers:
            return
        names = [router.name for router in self.routers]
        for name in names:
            validate_component_name(name, "RouterSpec")
        if len(names) != len(set(names)):
            raise ValueError(f"RouterSpec name 必须唯一，发现重复：{names}")
        if self.settings.supervisor_name in self._router_nodes.values():
            raise ValueError("Router 节点名不能与 supervisor_name 重复")
        collisions = self._agent_names.intersection(self._router_nodes.values())
        if collisions:
            raise ValueError(
                f"Agent name 不能与 Router 节点名重复：{'、'.join(sorted(collisions))}"
            )
        missing = [spec.name for spec in self.specs if not spec.group]
        if missing:
            raise ValueError(
                f"分层路由模式下所有 Agent 必须声明 group：{'、'.join(missing)}"
            )
        unknown = [
            f"{spec.name}:{spec.group}"
            for spec in self.specs
            if spec.group not in self._router_by_name
        ]
        if unknown:
            raise ValueError(f"Agent 引用了未声明的 RouterSpec：{'、'.join(unknown)}")
        empty = [
            router.name
            for router in self.routers
            if not any(spec.group == router.name for spec in self.specs)
        ]
        if empty:
            raise ValueError(f"RouterSpec 没有任何组内 Agent：{'、'.join(empty)}")

    def _validate_specs(self) -> None:
        """Fail fast on invalid references and ambiguous per-agent tool names."""
        configured_mcp = [server.name for server in self.settings.mcp_servers if server.enabled]
        configured_sandboxes = [provider.name for provider in self.settings.sandbox_providers if provider.enabled]
        if len(configured_mcp) != len(set(configured_mcp)):
            raise ValueError("Settings.mcp_servers 中存在重复 name")
        if len(configured_sandboxes) != len(set(configured_sandboxes)):
            raise ValueError("Settings.sandbox_providers 中存在重复 name")
        for spec in self.specs:
            for target in spec.handoffs:
                if target not in self._agent_names:
                    raise ValueError(f"Agent「{spec.name}」声明的 handoff 目标「{target}」未注册")
                if self.routers and self._by_name[target].group != spec.group:
                    raise ValueError(
                        f"半严格两级模式下 Agent handoff 只能指向同一 Router："
                        f"{spec.name}:{spec.group} -> {target}:{self._by_name[target].group}"
                    )
            tool_names = [getattr(tool, "name", "") for tool in spec.tools]
            duplicates = sorted({name for name in tool_names if name and tool_names.count(name) > 1})
            if duplicates:
                raise ValueError(
                    f"Agent「{spec.name}」存在重名工具：{'、'.join(duplicates)}；请为工具设置唯一名称"
                )
            mcp_names = set(configured_mcp)
            missing_mcp = [name for name in spec.mcp_servers if name not in mcp_names]
            if missing_mcp:
                raise ValueError(
                    f"Agent「{spec.name}」引用了未配置的 MCP server：{'、'.join(missing_mcp)}"
                )
            sandbox_names = set(configured_sandboxes)
            missing_sandboxes = [name for name in spec.sandboxes if name not in sandbox_names]
            if missing_sandboxes:
                raise ValueError(
                    f"Agent「{spec.name}」引用了未配置的 sandbox provider：{'、'.join(missing_sandboxes)}"
                )

    def _validate_required_actions_approval(self) -> None:
        """required_actions 中声明的动作工具必须在 tools 中且已用 require_approval 包装。

        否则动作可能未经“先校验参数 → 人工审批 → 执行”就直接生效，失去框架级硬保证。
        """
        for spec in self.specs:
            if not spec.required_actions:
                continue
            by_name = {getattr(t, "name", ""): t for t in spec.tools}
            for action in spec.required_actions:
                tool = by_name.get(action)
                if tool is None:
                    raise ValueError(
                        f"Agent「{spec.name}」声明 required_actions 中的「{action}」不在 tools 中，"
                        "框架无法保证该动作被执行"
                    )
                metadata = getattr(tool, "metadata", None) or {}
                if not metadata.get("axis_requires_approval"):
                    raise ValueError(
                        f"Agent「{spec.name}」的动作工具「{action}」必须用 require_approval 包装，"
                        "经人工审批后才能执行；当前未经过审批包装，拒绝装配"
                    )

    def _validate_parallel_groups(self) -> list[tuple[str, ...]]:
        """校验所有 Agent 声明的 parallel_handoffs，并按排序后的目标去重。"""
        groups: dict[tuple[str, ...], tuple[str, ...]] = {}
        for spec in self.specs:
            if not spec.parallel_handoffs:
                continue
            if len(spec.parallel_handoffs) < 2:
                raise ValueError(
                    f"Agent「{spec.name}」的 parallel_handoffs 至少需要两个目标：{spec.parallel_handoffs}"
                )
            if len(set(spec.parallel_handoffs)) != len(spec.parallel_handoffs):
                raise ValueError(
                    f"Agent「{spec.name}」的 parallel_handoffs 存在重复目标：{spec.parallel_handoffs}"
                )
            for target in spec.parallel_handoffs:
                if target not in self._agent_names:
                    raise ValueError(
                        f"Agent「{spec.name}」声明的 parallel_handoff 目标「{target}」未注册"
                    )
            key = tuple(sorted(spec.parallel_handoffs))
            tool_name = f"parallel_to_{'_'.join(key)}"
            if len(tool_name) > 64:
                raise ValueError(
                    f"Agent「{spec.name}」生成的并行工具名超过 64 个字符：{tool_name}"
                )
            if self.routers:
                target_groups = {
                    spec.group,
                    *(self._by_name[target].group for target in key),
                }
                if len(target_groups) != 1:
                    raise ValueError(
                        f"半严格两级模式下 parallel_handoffs 的声明者和目标必须属于同一 Router："
                        f"{spec.name} -> {key}"
                    )
            groups.setdefault(key, key)
        return list(groups.values())

    # ------------------------------------------------------------ 内部构造
    def _supervisor_tools(self):
        if self.routers:
            tools = [
                make_handoff_tool(
                    self._router_nodes[router.name],
                    f"进入「{router.name}」领域进一步识别专家。{router.description}",
                    display_name=router.display_name or router.name,
                    result_label="领域路由",
                    display_title=f"选择领域「{router.display_name or router.name}」",
                )
                for router in self.routers
            ]
            if len(self.routers) > 1:
                tools.append(
                    make_dynamic_parallel_handoff_tool(
                        {
                            router.name: self._router_nodes[router.name]
                            for router in self.routers
                        },
                        result_label="领域路由",
                        display_title="并行委派领域",
                        target_display_names={
                            router.name: router.display_name or router.name
                            for router in self.routers
                        },
                    )
                )
        else:
            tools = [
                make_handoff_tool(
                    spec.name,
                    f"把任务交给「{spec.name}」专家处理。{spec.description}",
                )
                for spec in self.specs
            ]
            if len(self.specs) > 1:
                tools.append(
                    make_dynamic_parallel_handoff_tool(
                        {spec.name: spec.name for spec in self.specs},
                        target_display_names={
                            spec.name: spec.display_name or spec.name
                            for spec in self.specs
                        },
                    )
                )
        tools.append(make_finish_without_reply_tool())
        return tools

    def _router_specs(self, router_name: str) -> list[AgentSpec]:
        return [spec for spec in self.specs if spec.group == router_name]

    def _router_tools(self, router_name: str) -> list[Any]:
        specs = self._router_specs(router_name)
        tools: list[Any] = [
            make_handoff_tool(
                spec.name,
                f"把任务交给「{spec.name}」专家处理。{spec.description}",
            )
            for spec in specs
        ]
        if len(specs) > 1:
            tools.append(
                make_dynamic_parallel_handoff_tool(
                    {spec.name: spec.name for spec in specs},
                    target_display_names={
                        spec.name: spec.display_name or spec.name for spec in specs
                    },
                )
            )
        return tools

    def _agent_handoff_tools(self, spec: AgentSpec):
        tools = []
        for target in spec.handoffs:
            if target not in self._by_name:
                raise ValueError(f"Agent「{spec.name}」声明的 handoff 目标「{target}」未注册")
            t = self._by_name[target]
            tools.append(make_handoff_tool(target, f"把任务直接交接给「{target}」专家。{t.description}"))
        if spec.parallel_handoffs:
            targets = tuple(sorted(spec.parallel_handoffs))
            tools.append(make_parallel_handoff_tool(list(targets), self._parallel_tool_description(targets)))
        return tools

    @staticmethod
    def _parallel_tool_description(targets: tuple[str, ...]) -> str:
        group = "、".join(targets)
        names = f"「{group}」"
        return (
            f"同时把任务并行委派给 {names} 专家；它们会并行执行，全部完成后由总调度汇合结果。"
            f"仅在多位专家彼此无先后依赖、可以同时推进时使用。"
        )

    def _needs_mcp(self) -> bool:
        """只要任一 Agent 声明了 MCP server，就需要异步连接加载工具。"""
        return any(s.mcp_servers for s in self.specs)

    def _needs_sandbox(self) -> bool:
        """只要任一 Agent 声明了沙箱，就加载对应后端的执行工具。"""
        return any(s.sandboxes for s in self.specs)

    def _resolve_sandbox_tools(self) -> dict[str, list[BaseTool]]:
        if not self._needs_sandbox():
            return {}
        wanted: list[str] = []
        for spec in self.specs:
            for name in spec.sandboxes:
                if name not in wanted:
                    wanted.append(name)
        self.sandbox_backends = load_sandbox_backends(
            self.settings.sandbox_providers, names=wanted
        )
        return {
            name: make_sandbox_tools(name, backend)
            for name, backend in self.sandbox_backends.items()
        }

    async def _load_mcp_tools(self) -> dict[str, list[BaseTool]]:
        wanted: list[str] = []
        for spec in self.specs:
            for name in spec.mcp_servers:
                if name not in wanted:
                    wanted.append(name)
        return await load_mcp_tools(self.settings.mcp_servers, names=wanted)

    def _agent_prompt(self, spec: AgentSpec) -> str:
        """业务提示词 + 技能 + 必填事实规则，统一在此组合。"""
        prompt = spec.system_prompt.strip() if spec.system_prompt else ""
        group_rule = (
            f"若最近分工没有 `{spec.name}`、但包含你的领域 `{spec.group}`，"
            f"则处理 `{spec.group}` 对应的领域子任务；"
            if spec.group
            else ""
        )
        prompt = (
            f"{prompt}\n\n你的 Agent 名称是 `{spec.name}`。"
            "如果上下文中存在 [Axis 并行子任务分工]，只采用最近一条分工，"
            "并且只处理与你名称对应的子任务；"
            f"{group_rule}"
            "不要处理、评价或解释分配给其他 Agent 的子任务。"
            "最终答复只报告你负责的子任务结果；除非用户明确询问系统分工，"
            "不要提及其他 Agent、其职责或能力，也不要说明其他子任务由谁处理。"
        ).strip()
        skills = resolve_skills(spec.skills, settings=self.settings)
        if skills:
            prompt = f"{prompt}\n\n{render_skill_block(skills)}".strip()
        return build_agent_prompt(prompt, spec.required_facts)

    def _agent_tools(
        self,
        spec: AgentSpec,
        mcp_tools: dict[str, list[BaseTool]] | None = None,
        sandbox_tools: dict[str, list[BaseTool]] | None = None,
    ):
        """业务/MCP/沙箱工具，以及当前路由模式允许的 Agent handoff 工具。"""
        tools: list[Any] = [
            bind_artifact_store(tool, self.artifact_store, spec.name)
            if isinstance(tool, BaseTool)
            else tool
            for tool in spec.tools
        ]
        tools = [
            bind_workflow_state_store(tool, self.workflow_state_store)
            if isinstance(tool, BaseTool) else tool
            for tool in tools
        ]
        mcp_tools = mcp_tools or {}
        for name in spec.mcp_servers:
            tools.extend(mcp_tools.get(name, []))
        sandbox_tools = sandbox_tools or {}
        for name in spec.sandboxes:
            tools.extend(sandbox_tools.get(name, []))
        tools.extend(self._agent_handoff_tools(spec))
        return tools

    def _supervisor_system_prompt(self) -> str:
        coverage_rule = (
            "\n\n任务覆盖约束：逐项识别原始用户消息中的每个独立诉求，并为每项委派对应的专家或领域；"
            "多个互不依赖的诉求必须并行委派。某个专家完成工作或进入审批暂停，不代表其他诉求已完成；"
            "同一专家负责多个诉求时，只在 targets 中列出该专家一次，并将这些诉求写入同一个 assignment；不要虚构带 _2 等后缀的专家名称。"
            "恢复后继续处理尚未完成的诉求。只有每项诉求均已获得结果或明确需要用户补充信息时才能结束本轮。"
            "这条约束优先于提示词中任何允许直接结束或只处理部分请求的说明。"
        )
        if self.routers:
            catalog = "\n".join(
                f"- {router.name}：{router.description}" for router in self.routers
            )
            return (
                f"{self.supervisor_prompt}\n\n当前可调用的领域 Router：\n{catalog}\n"
                "需要专业能力时，先调用 transfer_to_router_<group> 选择领域；"
                "不要越过 Router 直接选择具体专家。"
                f"{coverage_rule}"
            )
        catalog = "\n".join(f"- {s.name}：{s.description}" for s in self.specs)
        prompt = f"{self.supervisor_prompt}\n\n当前可调用的专家 Agent：\n{catalog}"
        if len(self.specs) > 1:
            prompt += (
                "\n\n多个专家任务彼此无先后依赖时，调用一次 parallel_to_agents，"
                "在 targets 中列出目标名称，并在 assignments 中逐一写明子任务；"
                "存在先后依赖时仍应逐个 transfer_to_<agent>。"
            )
        return prompt + coverage_rule

    def _router_system_prompt(self, router: RouterSpec) -> str:
        catalog = "\n".join(
            f"- {spec.name}：{spec.description}"
            for spec in self._router_specs(router.name)
        )
        extra = router.system_prompt.strip()
        prompt = DEFAULT_ROUTER_PROMPT
        if extra:
            prompt += f"\n领域补充规则：\n{extra}"
        return f"{prompt}\n\n当前领域：{router.name}\n可调用专家：\n{catalog}"

    @staticmethod
    def _router_retry_marker(router_name: str) -> str:
        return f"__axis_router_retry__{router_name}"

    def _router_retry_count(self, state: AgentState, router_name: str) -> int:
        marker = self._router_retry_marker(router_name)
        messages = state.get("messages", [])
        last_human = next(
            (
                i
                for i in range(len(messages) - 1, -1, -1)
                if isinstance(messages[i], HumanMessage)
            ),
            None,
        )
        if last_human is None:
            return 0
        return sum(
            1
            for message in messages[last_human + 1 :]
            if isinstance(message, SystemMessage)
            and getattr(message, "name", None) == marker
        )

    def _make_router_route(self, router: RouterSpec):
        def route(state: AgentState) -> str:
            if self._agent_just_handed_off(state):
                return "end"
            if self._router_retry_count(state, router.name) == 0:
                return "retry"
            return "violation"

        return route

    def _make_router_retry_node(self, router: RouterSpec):
        marker = self._router_retry_marker(router.name)

        def node(state: AgentState) -> dict[str, Any]:
            return {
                "messages": [
                    SystemMessage(
                        content=(
                            "[框架提醒] 领域 Router 不能直接回答业务问题。"
                            "请立即调用一个可用的 transfer_to_<agent> 或 "
                            "parallel_to_<agents> 工具完成委派。"
                        ),
                        name=marker,
                    )
                ]
            }

        return node

    @staticmethod
    def _make_router_violation_node(router: RouterSpec):
        def node(state: AgentState) -> dict[str, Any]:
            return {
                "messages": [
                    AIMessage(
                        content=(
                            f"领域路由「{router.name}」未能选择具体专家，"
                            "本轮已终止，请重试或调整领域路由配置。"
                        )
                    )
                ]
            }

        return node

    @staticmethod
    def _looks_like_clarification(text: str) -> bool:
        """判断专家消息是否在向用户追问事实。

        除了明显的问号，还覆盖“请确认/请补充/是否…”这类不带问号的请求句式，
        避免专家已经提问、却仍被图路由回 supervisor 再复述一遍。
        """
        text = text.strip()
        if not text:
            return False
        if "？" in text or "?" in text:
            return True
        markers = (
            "请确认",
            "请补充",
            "请提供",
            "请告知",
            "请回复",
            "请问",
            "请您",
            "烦请",
            "麻烦您",
            "是否",
            "能否",
            "可否",
        )
        return any(marker in text for marker in markers)

    def _should_await_user(self, state: AgentState) -> bool:
        """专家刚向用户提出澄清问题，本轮应直接结束，避免 supervisor 再次委派。

        只在“最近一条用户输入之后”的消息里找专家提问：如果专家还没回答问题，
        而是追问用户，就让图直接收尾，等用户补充后再从 START 重新进入 supervisor。
        """
        messages = state.get("messages", [])
        last_human = next(
            (i for i in range(len(messages) - 1, -1, -1) if isinstance(messages[i], HumanMessage)),
            None,
        )
        if last_human is None:
            return False
        for message in messages[last_human + 1 :]:
            if not isinstance(message, AIMessage):
                continue
            name = getattr(message, "name", None)
            if not name or name == self.settings.supervisor_name or name not in self._agent_names:
                continue
            if self._looks_like_clarification(visible_text(message)):
                return True
        return False

    def _agent_just_handed_off(self, state: AgentState) -> bool:
        """节点最后的消息是 handoff ToolMessage，或其后的并行分工消息。

        当专家调用 transfer_to_<other> 工具时，工具会通过 Command(goto=...,
        graph=PARENT) 把控制权交给父图。父图在接管的同时还会评估该专家节点上的
        条件边；如果这里返回 "prepare"，会把一次本不该发生的 supervisor 回路由
        排队到目标专家之后执行，导致目标专家提问/结束后 supervisor 又被重新拉起。
        """
        messages = state.get("messages", [])
        last = messages[-1] if messages else None
        if (
            isinstance(last, SystemMessage)
            and getattr(last, "name", None) == "__axis_parallel_assignments__"
        ):
            last = messages[-2] if len(messages) > 1 else None
        if not isinstance(last, ToolMessage):
            return False
        name = getattr(last, "name", None)
        if not isinstance(name, str):
            return False
        handoff_names = {f"transfer_to_{n}" for n in self._agent_names}
        if name in handoff_names and str(last.content).startswith("已转交「"):
            return True
        return name.startswith("parallel_to_") and str(last.content).startswith("已并行委派")

    def _experts_in_current_turn(self, state: AgentState) -> set[str]:
        """返回最近一条用户输入后实际参与的专家，忽略历史轮次。"""
        messages = state.get("messages", [])
        last_human = next(
            (i for i in range(len(messages) - 1, -1, -1) if isinstance(messages[i], HumanMessage)),
            None,
        )
        if last_human is None:
            return set()
        experts = {
            name
            for message in messages[last_human + 1 :]
            if isinstance(message, AIMessage)
            if (name := getattr(message, "name", None)) in self._agent_names
        }
        for message in messages[last_human + 1 :]:
            if not isinstance(message, ToolMessage):
                continue
            tool_name = getattr(message, "name", "") or ""
            if tool_name.startswith("transfer_to_"):
                target = tool_name[len("transfer_to_"):]
                if target in self._agent_names:
                    experts.add(target)
            elif tool_name.startswith("parallel_to_"):
                experts.update(
                    target
                    for group in self._parallel_groups
                    if tool_name == f"parallel_to_{'_'.join(group)}"
                    for target in group
                )
            if tool_name == DYNAMIC_PARALLEL_TOOL_NAME:
                raw_targets = message.additional_kwargs.get("axis_handoff_targets", [])
                experts.update(
                    target for target in raw_targets if target in self._agent_names
                )
        return experts

    def _agent_next(self, state: AgentState) -> str:
        """专家节点完成后的路由：澄清时结束，否则回 supervisor 判断是否续派。

        专家刚把任务 handoff 给兄弟专家时，实际路由由 handoff 工具的 Command 决定，
        这里统一返回 end，避免多余的 prepare 路由在目标专家之后再次执行。
        """
        if self._should_await_user(state):
            return "end"
        if self._agent_just_handed_off(state):
            return "end"
        return "prepare"

    def _missing_required_actions(
        self, state: AgentState, required: tuple[str, ...]
    ) -> list[str]:
        """检查自最近一次用户输入以来，required_actions 中还有哪些未被成功调用。

        只有自然结束（给出结论前）才检查；向用户提问、交接兄弟 Agent 不在此列。
        """
        if not required:
            return []
        messages = state.get("messages", [])
        last_human = next(
            (i for i in range(len(messages) - 1, -1, -1) if isinstance(messages[i], HumanMessage)),
            None,
        )
        if last_human is None:
            return list(required)
        seen: set[str] = set()
        for message in messages[last_human + 1 :]:
            if isinstance(message, ToolMessage):
                name = getattr(message, "name", None)
                # 只有“真正执行成功”的 ToolMessage 才算数；被人工驳回或参数校验失败都不算。
                if name in required and self._tool_succeeded(message):
                    seen.add(name)
        return [name for name in required if name not in seen]

    @staticmethod
    def _tool_succeeded(message: ToolMessage) -> bool:
        """判断 ToolMessage 是否代表工具真正执行成功。"""
        content = getattr(message, "content", "") or ""
        if is_rejected_output(content):
            return False
        text = str(content)
        if text.startswith("参数校验失败"):
            return False
        return True

    def _action_retry_count(self, state: AgentState, agent_name: str) -> int:
        """统计本次用户输入以来，为该 Agent 注入的框架级动作提醒次数。"""
        marker = f"__axis_retry__{agent_name}"
        messages = state.get("messages", [])
        last_human = next(
            (i for i in range(len(messages) - 1, -1, -1) if isinstance(messages[i], HumanMessage)),
            None,
        )
        if last_human is None:
            return 0
        return sum(
            1
            for m in messages[last_human + 1 :]
            if isinstance(m, SystemMessage) and getattr(m, "name", None) == marker
        )

    def _find_rejected_tool(self, state: AgentState) -> tuple[str, str] | None:
        """自最近一次用户输入以来，找寻“被人工驳回”的工具及其意见文本。"""
        messages = state.get("messages", [])
        last_human = next(
            (i for i in range(len(messages) - 1, -1, -1) if isinstance(messages[i], HumanMessage)),
            None,
        )
        if last_human is None:
            return None
        for message in reversed(messages[last_human + 1 :]):
            if isinstance(message, ToolMessage):
                reason = rejection_reason(message.content)
                if reason is not None:
                    return (getattr(message, "name", "") or "工具", reason)
        return None

    def _make_agent_router(self, spec: AgentSpec):
        """为单个 Agent 生成专家节点后的路由，绑定其声明的 required_actions。"""
        required = tuple(spec.required_actions)

        def route(state: AgentState) -> str:
            # 专家刚向用户提问，或刚把任务交接给兄弟 Agent：都先汇合到 __after_expert__ 统一裁决。
            # 这样并行 fan-out 时，任意一支提出澄清问题都能让整轮在汇合后结束，而不是继续跑 supervisor。
            if self._should_await_user(state):
                return "after"
            if self._agent_just_handed_off(state):
                return "after"
            missing = self._missing_required_actions(state, required)
            if missing:
                # 先给该 Agent 一次自纠正机会：注入提醒并重跑；次数用尽仍不执行才拦截。
                if self._action_retry_count(state, spec.name) < spec.max_action_retries:
                    return "retry"
                return "violation"
            return "after"

        return route

    def _make_retry_node(self, spec: AgentSpec):
        """required_actions 未满足时的自纠正节点：注入系统提醒并回到专家节点重跑。"""
        required = tuple(spec.required_actions)
        marker = f"__axis_retry__{spec.name}"

        def node(state: AgentState) -> dict[str, Any]:
            missing = self._missing_required_actions(state, required)
            rejected = self._find_rejected_tool(state)
            retries = self._action_retry_count(state, spec.name)
            attempt = retries + 1
            remaining = max(0, spec.max_action_retries - retries)
            if rejected is not None:
                tool_name, reason = rejected
                detail = f"：{reason}" if reason and reason.strip() else "（未填写意见）"
                text = (
                    f"[框架提醒] 工具「{tool_name}」被人工驳回{detail}。"
                    f"请按审批意见修正后重新调用该工具；"
                    f"不要绕过审批，也不要在重新调用前声称动作已完成。"
                    f"（第 {attempt} 次，剩余 {remaining} 次机会）"
                )
            else:
                text = (
                    f"[框架提醒] 你尚未调用声明的工具：{'、'.join(missing)}。"
                    f"请立即真正调用该工具完成动作，再从事实出发给出结论；"
                    f"不要在未调用工具的情况下声称已完成"
                    f"（第 {attempt} 次，剩余 {remaining} 次机会）。"
                )
            return {"messages": [SystemMessage(content=text, name=marker)]}

        return node

    def _make_missing_action_node(self, spec: AgentSpec):
        """required_actions 未满足时的拦截节点：产出明确的失败消息并结束本轮。"""
        required = tuple(spec.required_actions)

        def node(state: AgentState) -> dict[str, Any]:
            missing = self._missing_required_actions(state, required)
            text = (
                f"「{spec.name}」本轮未执行声明的动作：" + "、".join(missing)
                + "。为防止把未完成的动作当成结论继续流转，本轮已拦截，请补充信息后重试。"
            )
            return {"messages": [AIMessage(content=text)]}

        return node

    def _prepare(self, state: AgentState) -> dict[str, Any]:
        """进入 supervisor 前执行一次上下文压缩（超限才触发）。"""
        messages = state["messages"]
        last_human = next(
            (
                i
                for i in range(len(messages) - 1, -1, -1)
                if isinstance(messages[i], HumanMessage)
            ),
            None,
        )
        keep_recent = self.settings.keep_recent_messages
        if last_human is not None:
            keep_recent = max(keep_recent, len(messages) - last_human)
        if needs_compression(
            messages,
            max_tokens=self.settings.max_context_tokens,
            keep_recent=keep_recent,
        ):
            return summarize_and_trim(
                {"messages": messages},
                self.model,
                keep_recent=keep_recent,
            )
        return {}

    @staticmethod
    def _after_expert(state: AgentState) -> dict[str, Any]:
        """专家节点完成后的汇合点。

        串行 handoff 时它只经过一次；并行 fan-out 时，所有并行分支都会汇聚到这里，
        LangGraph 会在全部到达后仅执行一次，再统一决定结束等待用户、单专家直接结束，
        或回 supervisor 综合多专家结果。
        """
        return {}

    def _after_expert_next(self, state: AgentState) -> str:
        """汇合后等用户补充、单专家答复直接结束；多专家结果回 supervisor 综合。"""
        return self._agent_next(state)

    # ------------------------------------------------------------ 装配
    def _build_with_mcp(
        self,
        mcp_tools: dict[str, list[BaseTool]],
        sandbox_tools: dict[str, list[BaseTool]],
    ):
        supervisor = create_agent(
            self.model,
            self._supervisor_tools(),
            system_prompt=self._supervisor_system_prompt(),
            middleware=[
                _CoordinatorControlMiddleware(self, dynamic_continuation=True)
            ],
            name=self.settings.supervisor_name,
        )

        agents = {
            s.name: create_agent(
                self.model,
                self._agent_tools(s, mcp_tools, sandbox_tools),
                system_prompt=self._agent_prompt(s),
                name=s.name,
            )
            for s in self.specs
        }
        routers = {
            self._router_nodes[router.name]: create_agent(
                self.model,
                self._router_tools(router.name),
                system_prompt=self._router_system_prompt(router),
                middleware=[
                    _CoordinatorControlMiddleware(self, dynamic_continuation=False)
                ],
                name=self._router_nodes[router.name],
            )
            for router in self.routers
        }

        g = StateGraph(AgentState)
        g.add_node("__prepare__", self._prepare)
        g.add_node("__after_expert__", self._after_expert)
        g.add_node(self.settings.supervisor_name, supervisor)
        for name, node in routers.items():
            g.add_node(name, node)
            router = self._router_by_name[name[len("router_") :]]
            retry_node = f"__retry_{name}"
            violation_node = f"__miss_{name}"
            g.add_node(retry_node, self._make_router_retry_node(router))
            g.add_node(violation_node, self._make_router_violation_node(router))
            g.add_edge(retry_node, name)
            g.add_edge(violation_node, END)
            g.add_conditional_edges(
                name,
                self._make_router_route(router),
                {"end": END, "retry": retry_node, "violation": violation_node},
            )
        for name, node in agents.items():
            g.add_node(name, node)

        # 流程：START -> prepare -> supervisor
        g.add_edge(START, "__prepare__")
        g.add_edge("__prepare__", self.settings.supervisor_name)
        # supervisor 直接给出最终答案时结束；委派由 handoff 工具的 Command 接管
        g.add_edge(self.settings.supervisor_name, END)
        # 专家自然结束后统一进入 __after_expert__ 汇合：串行时只经过一次；
        # 并行 fan-out 时所有分支在此汇合，再由 _after_expert_next 决定结束等待用户
        # 还是回 prepare -> supervisor 汇总。这样能保证并行分支全部完成后 supervisor 只跑一次。
        g.add_conditional_edges(
            "__after_expert__",
            self._after_expert_next,
            {"end": END, "prepare": "__prepare__"},
        )
        for spec in self.specs:
            name = spec.name
            paths: dict[Hashable, str] = {"after": "__after_expert__"}
            if spec.required_actions:
                retry_node = f"__retry_{name}"
                miss_node = f"__miss_{name}"
                g.add_node(retry_node, self._make_retry_node(spec))
                g.add_node(miss_node, self._make_missing_action_node(spec))
                g.add_edge(retry_node, name)   # 自纠正：注入提醒后回到专家节点重跑
                g.add_edge(miss_node, END)
                paths["retry"] = retry_node
                paths["violation"] = miss_node
            g.add_conditional_edges(name, self._make_agent_router(spec), paths)

        return g

    async def abuild(self, checkpointer: BaseCheckpointSaver[Any] | None = None):
        """异步装配：先连接 MCP server 加载工具，再编译图。

        当任何 Agent 声明了 AgentSpec.mcp_servers 时建议使用本入口，
        避免在已有事件循环的事件回调里调用同步 build()。
        """
        mcp_tools = await self._load_mcp_tools() if self._needs_mcp() else {}
        sandbox_tools = self._resolve_sandbox_tools()
        graph = self._build_with_mcp(mcp_tools, sandbox_tools)
        cp = (
            checkpointer
            if checkpointer is not None
            else await create_async_checkpointer(self.settings)
        )
        return graph.compile(checkpointer=cp)

    def build(self, checkpointer: BaseCheckpointSaver[Any] | None = None):
        """同步装配。无 MCP 时走纯同步路径；需要 MCP 时在独立事件循环里完成连接。"""
        if not self._needs_mcp():
            sandbox_tools = self._resolve_sandbox_tools()
            graph = self._build_with_mcp({}, sandbox_tools)
            cp = checkpointer if checkpointer is not None else create_checkpointer(self.settings)
            return graph.compile(checkpointer=cp)

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self.abuild(checkpointer=checkpointer))
        raise RuntimeError(
            "检测到正在运行的事件循环，且图中声明了 MCP server；"
            "请在异步上下文里使用 `await abuild_runtime(...)` / `await abuild_graph(...)`，"
            "或在同步脚本里确保不在 async 函数内构建。"
        )

    async def aclose_sandboxes(self) -> None:
        """释放本图已加载的所有沙箱后端。"""
        if self.sandbox_backends:
            await close_sandbox_backends(self.sandbox_backends.values())
            self.sandbox_backends = {}

    def close_sandboxes(self) -> None:
        """同步释放沙箱后端；在异步上下文里请改用 `await aclose_sandboxes()`。"""
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(self.aclose_sandboxes())
            return
        raise RuntimeError(
            "检测到正在运行的事件循环，请改用 `await builder.aclose_sandboxes()`。"
        )


def build_graph(
    specs: Iterable[AgentSpec],
    model: BaseChatModel,
    *,
    settings: Settings | None = None,
    supervisor_prompt: str | None = None,
    checkpointer: BaseCheckpointSaver[Any] | None = None,
    routers: Iterable[RouterSpec] | None = None,
    artifact_store: ArtifactStore | None = None,
    workflow_state_store: WorkflowStateStore | None = None,
):
    """便捷函数：同步得到编译后的图（含 MCP 自动兜底）。"""
    return GraphBuilder(
        specs,
        model,
        settings=settings,
        supervisor_prompt=supervisor_prompt,
        routers=routers,
        artifact_store=artifact_store,
        workflow_state_store=workflow_state_store,
    ).build(checkpointer=checkpointer)


async def abuild_graph(
    specs: Iterable[AgentSpec],
    model: BaseChatModel,
    *,
    settings: Settings | None = None,
    supervisor_prompt: str | None = None,
    checkpointer: BaseCheckpointSaver[Any] | None = None,
    routers: Iterable[RouterSpec] | None = None,
    artifact_store: ArtifactStore | None = None,
    workflow_state_store: WorkflowStateStore | None = None,
):
    """便捷函数：异步得到编译后的图（推荐在 async 应用里使用）。"""
    return await GraphBuilder(
        specs,
        model,
        settings=settings,
        supervisor_prompt=supervisor_prompt,
        routers=routers,
        artifact_store=artifact_store,
        workflow_state_store=workflow_state_store,
    ).abuild(checkpointer=checkpointer)
