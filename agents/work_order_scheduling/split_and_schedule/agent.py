"""工单拆分与排班 Agent 装配入口。"""

from core.registry import AgentSpec

from .prompts import SYSTEM_PROMPT
from .tools import prepare_rework_set, schedule_work_orders, split_work_orders


spec = AgentSpec(
    name="split_and_schedule",
    display_name="工单拆分与排班",
    description="根据周期计划报文拆分工单、合并已有工单并安排新旧工单的作业时间。",
    system_prompt=SYSTEM_PROMPT,
    tools=[split_work_orders, prepare_rework_set, schedule_work_orders],
)
