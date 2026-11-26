"""基于会话请求执行完整排班的 Agent 工具。"""

from __future__ import annotations

from typing import Any

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import StructuredTool, tool

from core.artifacts import make_artifact_result_tool
from core.steps import with_progress
from core.workflow_state import WorkflowStateStore
from ..service import (
    assemble_pipeline_output,
    inspect_pipeline_result,
    prepare_pipeline_orders,
    schedule_pipeline_orders,
    split_pipeline_input,
)
from .scheduling import (
    _check_payload,
    _mock_existing_rows,
    _schedule_orders,
    _schedule_summary,
)

REQUEST_NAMESPACE = "work_order_schedule"


def _make_schedule_request_tool(store: WorkflowStateStore | None = None):
    def schedule_request(request_id: str, config: RunnableConfig) -> dict[str, Any]:
        """按本会话已提交的结构化请求排班；调试报文可内嵌 mock 旧工单。"""
        if store is None:
            raise RuntimeError("排班请求工具尚未绑定运行时状态")
        thread_id = str((config.get("configurable") or {}).get("thread_id") or "")
        data = store.load(thread_id, REQUEST_NAMESPACE, request_id)
        if not isinstance(data, dict):
            raise ValueError("排班请求不存在或已过期，请重新提交计划报文")
        payload = data["payload"]
        _check_payload(payload)
        context: dict[str, Any] = {"payload": payload}

        @tool("validate_and_split_schedule_plans")
        def validate_and_split() -> dict[str, int]:
            """校验计划并按执行方式拆分待排工单。"""
            orders, details, window_start, window_end = split_pipeline_input(payload)
            context.update(
                orders=orders,
                details=details,
                window_start=window_start,
                window_end=window_end,
            )
            return {
                "plans": sum(
                    len(strategy.get("planList") or [])
                    for strategy in payload.get("planStrategyList") or []
                ),
                "orders": len(orders),
                "details": len(details),
            }

        @tool("prepare_existing_work_orders")
        def prepare_existing() -> dict[str, int]:
            """查询并筛选排班窗口内的已有工单。"""
            pending = prepare_pipeline_orders(
                context["orders"],
                context["details"],
                context["window_start"],
                context["window_end"],
                _mock_existing_rows(payload),
            )
            context["pending"] = pending
            return {
                "new_orders": len(pending.new_orders),
                "old_orders": len(pending.old_orders),
            }

        @tool("run_work_order_scheduling_rules")
        def run_rules() -> dict[str, int]:
            """执行优先级、容量、时段和旧单重排规则。"""
            result = schedule_pipeline_orders(
                context["pending"],
                enforce_quota=data["enforce_quota"],
                skip_weekdays=data["skip_weekdays"],
                holidays=data["holidays"],
            )
            context["result"] = result
            return {
                "placements": len(result.new_placements),
                "rework_steps": len(result.rework_steps),
            }

        @tool("inspect_schedule_outcome")
        def inspect_outcome() -> dict[str, Any]:
            """判定全部排妥、旧单重排或强排建议分支。"""
            outcome = inspect_pipeline_result(context["result"])
            context["outcome"] = outcome
            return outcome

        @tool("assemble_schedule_result")
        def assemble_result() -> dict[str, int]:
            """回组新工单、旧工单、影响链和确认信息。"""
            output = assemble_pipeline_output(
                payload, context["pending"], context["result"])
            context["output"] = output
            return {
                "new_orders": len(_schedule_orders(output)),
                "old_orders": len(output.get("oldWorkOrders") or []),
            }

        stages = (
            with_progress(validate_and_split, title="校验并拆分巡检计划"),
            with_progress(prepare_existing, title="获取并筛选已有工单"),
            with_progress(run_rules, title="执行工单排班规则"),
            with_progress(inspect_outcome, title="判定排班结果"),
            with_progress(assemble_result, title="整理排班方案"),
        )
        for stage in stages:
            stage.invoke({}, config=config)
        return context["output"]

    wrapped = StructuredTool.from_function(
        schedule_request,
        name="schedule_work_orders_from_request",
        metadata={"axis_internal_tool": True},
    )
    object.__setattr__(
        wrapped,
        "_axis_rebind_workflow_state_tool",
        lambda runtime_store: _make_schedule_request_tool(runtime_store),
    )
    return wrapped


schedule_work_orders_from_request = with_progress(
    make_artifact_result_tool(
        _make_schedule_request_tool(),
        artifact_type="scheduled_work_orders",
        summary=_schedule_summary,
    ),
    title="工单排班",
)


__all__ = ["REQUEST_NAMESPACE", "schedule_work_orders_from_request"]
