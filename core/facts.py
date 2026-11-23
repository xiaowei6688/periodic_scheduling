"""事实收集（intake）通用能力。

把“先把事实收集完整，再生成动作”做成框架级能力：
1. 任意 Agent 通过 AgentSpec.required_facts 声明动作前必须收集齐的事实；
2. 框架装配图时自动把“先收集事实、缺一必问、禁止占位”的规则注入其 system_prompt，
   由 Agent 在普通回复中直接向用户提问补齐，无需专门的澄清事件；
3. 同一条声明可派生工具参数校验器（必填 + 禁止占位值）。
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from .approval import Validator, chain_validators, reject_placeholders, required_fields


@dataclass(frozen=True)
class FactSpec:
    """动作前必须收集齐的一条事实。

    - name：事实/工具参数字段名；
    - question：缺这条事实时向用户补问的问题；
    - description：事实口径说明（可选）；
    - examples：示例值（可选）。
    """

    name: str
    question: str
    description: str = ""
    examples: tuple[str, ...] = ()


def render_intake_rules(facts: Iterable[FactSpec]) -> str:
    """把必填事实渲染成注入 Agent 系统提示词的“先收集事实”规则。"""
    items = list(facts)
    lines = [
        "在生成任何动作（调用工具）之前，必须先完成事实收集。下列事实缺一不可，"
        "每条都必须来自用户明确提供或确认：",
    ]
    for fact in items:
        label = f"{fact.name}（{fact.description}）" if fact.description else fact.name
        line = f"- {label}：{fact.question}"
        if fact.examples:
            line += "（例如：" + "、".join(fact.examples) + "）"
        lines.append(line)
    lines += [
        "规则：",
        "- 任一事实缺失时，先向用户提出明确问题，等用户答复补齐后，再调用工具生成动作；禁止猜测、禁止代填；",
        "- 禁止使用“待补充/待定/TBD”等占位值；",
        "- 事实齐备后才能调用工具生成动作；",
        "- 若用户在本次或此前的对话中已经明确给出全部必填事实，直接据此调用工具生成动作，不要再向用户重复确认同一事实；",
        "- 只有事实缺失或含糊不清时才继续提问。",
    ]
    return "\n".join(lines)


def build_agent_prompt(system_prompt: str, facts: Iterable[FactSpec]) -> str:
    """组合业务提示词与自动生成的 intake 规则；无必填事实时原样返回。"""
    prompt = system_prompt.strip() if system_prompt else ""
    items = list(facts)
    if not items:
        return prompt
    return f"{prompt}\n\n{render_intake_rules(items)}".strip()


def facts_validator(facts: Iterable[FactSpec]) -> Validator:
    """由同一声明生成工具参数校验器：必填 + 禁止占位值。"""
    names = tuple(fact.name for fact in facts)
    return chain_validators(required_fields(*names), reject_placeholders(*names))
