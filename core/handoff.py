"""Handoff 工具工厂：Agent 之间直接/动态交接的控制权转移。"""
from __future__ import annotations

from typing import Annotated, Any

from langchain_core.messages import SystemMessage, ToolMessage
from langchain_core.tools import InjectedToolCallId, tool
from langgraph.graph import END
from langgraph.types import Command


FINISH_WITHOUT_REPLY_TOOL_NAME = "finish_without_reply"
DYNAMIC_PARALLEL_TOOL_NAME = "parallel_to_agents"


def make_handoff_tool(
    target: str,
    description: str,
    *,
    display_name: str | None = None,
    result_label: str = "专家",
    display_title: str | None = None,
):
    """生成一个“交接给目标 Agent”的工具。

    模型调用该工具后，返回 Command(goto=target, graph=PARENT) 把控制权切换到
    父图中的目标节点。同时回写一条中文 ToolMessage，保证该工具调用在消息历史里
    有一条明确的、面向用户的占位结果，避免运行时用英文“无输出”类文案补白。
    """
    name = f"transfer_to_{target}"
    label = display_name or target

    @tool(name_or_callable=name, description=description)
    def _handoff(tool_call_id: Annotated[str, InjectedToolCallId]) -> Command[str]:
        return Command(
            goto=target,
            graph=Command.PARENT,
            update={
                "messages": [
                    ToolMessage(
                        content=f"已转交「{label}」{result_label}处理。",
                        tool_call_id=tool_call_id,
                    )
                ]
            },
        )

    metadata: dict[str, Any] = {
        "axis_handoff_target": target,
        "axis_handoff_label": label,
    }
    if display_title:
        metadata["axis_display_title"] = display_title
    _handoff.metadata = metadata
    return _handoff

def make_parallel_handoff_tool(targets: list[str], description: str):
    """生成一个“并行委派一组专家”的工具。

    模型调用该工具后，返回 Command(goto=list(targets), graph=PARENT) 一次把控制权
    切到父图中的多个目标节点，由 LangGraph 的 fan-out 机制并行执行；目标节点全部
    完成后在父图的汇合点 fan-in，再回到 supervisor 继续决策。

    同时回写一条中文 ToolMessage，避免英文补白，并供父图识别“该 Agent 已完成交接”。
    """
    ordered = sorted(targets)
    key = "_".join(ordered)
    name = f"parallel_to_{key}"
    group = "、".join(ordered)
    label = f"「{group}」"

    @tool(name_or_callable=name, description=description)
    def _parallel_handoff(tool_call_id: Annotated[str, InjectedToolCallId]) -> Command[str]:
        return Command(
            goto=list(ordered),
            graph=Command.PARENT,
            update={
                "messages": [
                    ToolMessage(
                        content=f"已并行委派{label}专家处理。",
                        tool_call_id=tool_call_id,
                    )
                ]
            },
        )

    # 供步骤采集器展示“并行委派专家「a、b」”，而不是内部的 parallel_to_a_b 工具名。
    _parallel_handoff.metadata = {"axis_display_title": f"并行委派专家{label}"}
    return _parallel_handoff


def make_dynamic_parallel_handoff_tool(
    targets: dict[str, str],
    *,
    result_label: str = "专家",
    display_title: str | None = None,
    target_display_names: dict[str, str] | None = None,
):
    """生成协调节点的动态并行派发工具，目标不依赖 AgentSpec handoff 声明。"""
    allowed = dict(sorted(targets.items()))
    catalog = "、".join(allowed)
    display_names = target_display_names or {}

    def registered_target(name: str) -> str:
        if name in allowed:
            return name
        base, separator, suffix = name.rpartition("_")
        if separator and suffix.isdigit() and int(suffix) >= 2 and base in allowed:
            return base
        return name

    def progress_summary(args: dict[str, Any]) -> str:
        names = dict.fromkeys(
            display_names.get(registered_target(target), registered_target(target))
            for target in args.get("targets", [])
        )
        return "目标：" + "、".join(names)

    @tool(
        name_or_callable=DYNAMIC_PARALLEL_TOOL_NAME,
        description=(
            f"把两个或更多无先后依赖的任务并行派发给目标{result_label}。"
            f"targets 只能从以下名称选择：{catalog}。"
            "assignments 必须为每个 target 提供一个具体、互不重叠的子任务。"
        ),
    )
    def _dynamic_parallel_handoff(
        targets: list[str],
        assignments: dict[str, str],
        tool_call_id: Annotated[str, InjectedToolCallId],
    ) -> Command[str]:
        ordered = list(dict.fromkeys(registered_target(target) for target in targets))
        unknown = [target for target in ordered if target not in allowed]
        if unknown:
            raise ValueError(f"动态并行派发包含未注册目标：{'、'.join(unknown)}")
        for target in assignments:
            registered = registered_target(target)
            if registered in allowed and registered not in ordered:
                ordered.append(registered)
        if len(ordered) < 2:
            raise ValueError("动态并行派发至少需要两个不同目标")
        merged: dict[str, list[str]] = {target: [] for target in ordered}
        extra = []
        for target, assignment in assignments.items():
            registered = registered_target(target)
            if registered not in merged:
                extra.append(target)
            elif assignment.strip() and assignment.strip() not in merged[registered]:
                merged[registered].append(assignment.strip())
        missing = [target for target in ordered if not merged[target]]
        if missing or extra:
            details = []
            if missing:
                details.append(f"缺少子任务：{'、'.join(missing)}")
            if extra:
                details.append(f"包含非目标子任务：{'、'.join(extra)}")
            raise ValueError("动态并行派发的任务分工无效；" + "；".join(details))
        assignments = {
            target: "\n".join(merged[target]) for target in ordered
        }
        nodes = [allowed[target] for target in ordered]
        label = "、".join(ordered)
        task_lines = "\n".join(
            f"- {target}: {assignments[target].strip()}" for target in ordered
        )
        return Command(
            goto=nodes,
            graph=Command.PARENT,
            update={
                "messages": [
                    ToolMessage(
                        content=f"已并行委派「{label}」{result_label}处理。",
                        tool_call_id=tool_call_id,
                        additional_kwargs={
                            "axis_handoff_targets": nodes,
                            "axis_handoff_assignments": assignments,
                        },
                    ),
                    SystemMessage(
                        name="__axis_parallel_assignments__",
                        content=(
                            "[Axis 并行子任务分工]\n"
                            f"{task_lines}\n"
                            "接收方只采用上下文中最近一条并行子任务分工，"
                            "并且只处理与当前 Agent 或领域名称对应的子任务。"
                            "不要处理、评价或解释其他目标的子任务。"
                        ),
                    ),
                ]
            },
        )

    _dynamic_parallel_handoff.metadata = {
        "axis_display_title": display_title or f"并行委派{result_label}",
        "axis_dynamic_parallel_targets": allowed,
        "axis_progress_summary": progress_summary,
    }
    return _dynamic_parallel_handoff


def make_finish_without_reply_tool():
    """生成 supervisor 的隐藏结束工具，保留最近专家答复作为最终结果。"""

    @tool(
        name_or_callable=FINISH_WITHOUT_REPLY_TOOL_NAME,
        description="无需继续派发时静默结束；不要复述或改写专家答复。",
    )
    def _finish_without_reply(
        tool_call_id: Annotated[str, InjectedToolCallId],
    ) -> Command[str]:
        return Command(
            goto=END,
            graph=Command.PARENT,
            update={
                "messages": [
                    ToolMessage(
                        content="专家答复已作为本轮最终结果。",
                        tool_call_id=tool_call_id,
                    )
                ]
            },
        )

    _finish_without_reply.metadata = {"axis_internal_tool": True}
    return _finish_without_reply
