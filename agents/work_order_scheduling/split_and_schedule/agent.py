"""工单拆分与排班 Agent 装配入口。"""

from core.registry import AgentSpec

from .prompts import SYSTEM_PROMPT
from .tools import TOOLS


spec = AgentSpec(
    name="split_and_schedule",
    display_name="工单拆分与排班",
    description="根据周期计划报文拆分工单、合并已有工单、安排新旧工单作业时间，并在组装排班结果后请求人工确认。",
    system_prompt=SYSTEM_PROMPT,
    tools=TOOLS,
)
