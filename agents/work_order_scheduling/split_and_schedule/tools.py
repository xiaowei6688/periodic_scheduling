"""工单拆分与排班的 Agent 工具入口。计算结果不落盘、不落库。"""

from __future__ import annotations

from typing import Any

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import StructuredTool
from langchain_core.tools import tool

from core.approval import ApprovalSpec, require_approval
from core.artifacts import ArtifactStore, make_artifact_result_tool
from core.steps import with_progress
from core.workflow_state import WorkflowStateStore
from .rework_orders import collect_rework_set
from .service import run_pipeline
from .split_work_orders import split_all, verify


def _check_payload(payload: dict) -> None:
    if not isinstance(payload, dict):
        raise ValueError("payload 必须是对象")
    if not isinstance(payload.get("planSetInfo"), dict):
        raise ValueError("payload.planSetInfo 必须是对象")
    if not isinstance(payload.get("planStrategyList"), list):
        raise ValueError("payload.planStrategyList 必须是数组")
    if not isinstance(payload.get("airports"), list):
        raise ValueError("payload.airports 必须是数组")


@tool("split_work_orders")
def _split_work_orders(payload: dict) -> dict:
    """根据计划报文拆分新工单，返回工单、明细和汇总计数；不排班。"""
    _check_payload(payload)
    orders, details, results = split_all(payload)
    problems = verify(results)
    if problems:
        raise ValueError("拆单守恒校验失败：" + "；".join(problems))
    return {
        "orders": orders,
        "details": details,
        "summary": {
            "plan_count": len(results),
            "order_count": len(orders),
            "detail_count": len(details),
            "removed_channel_count": sum(len(result.removed_channel) for result in results),
        },
    }


@tool("prepare_rework_set")
def _prepare_rework_set(payload: dict, existing_rows: list[dict] | None = None) -> dict:
    """拆分新工单并合并旧工单，返回待排集合；不执行排班。existing_rows 未传时在线查询旧工单，空数组表示没有旧工单。"""
    _check_payload(payload)
    pending = collect_rework_set(payload, existing_rows)
    return {
        "summary": pending.summary(),
        "orders": pending.all_orders,
        "details": pending.details,
    }


@tool("schedule_work_orders")
def _schedule_work_orders(
    payload: dict,
    existing_rows: list[dict] | None = None,
    enforce_quota: bool = True,
    skip_weekdays: list[int] | None = None,
    holidays: list[str] | None = None,
) -> dict:
    """拆单并排班，返回按计划回组的新旧工单和重排结果。existing_rows 未传时在线查询旧工单，空数组表示没有旧工单；skip_weekdays 为 1=周一至 7=周日，holidays 为 YYYY-MM-DD 日期。"""
    _check_payload(payload)
    return run_pipeline(
        payload,
        existing_rows,
        enforce_quota=enforce_quota,
        skip_weekdays=skip_weekdays,
        holidays=holidays,
    )


def _schedule_summary(result: dict) -> str:
    orders = [
        order
        for strategy in result["planStrategyList"]
        for plan in strategy["planList"]
        for order in plan["workOrders"]
    ]
    unplaced = [
        str(order.get("workOrderNo") or order.get("workOrderGuid"))
        for order in orders if not order.get("startDate") or not order.get("endDate")
    ]
    return (
        f"新工单 {len(orders)} 张，旧工单 {len(result['oldWorkOrders'])} 张；"
        f"排班入口 {result['entry']}；未排入 {len(unplaced)} 张"
        + (f"（{', '.join(unplaced[:10])}{' 等' if len(unplaced) > 10 else ''}）" if unplaced else "")
        + f"；强插建议 {len(result['forceProposals'])} 条；警告 {len(result['warnings'])} 条"
    )


split_work_orders = with_progress(
    make_artifact_result_tool(
        _split_work_orders,
        artifact_type="split_work_orders",
        summary=lambda result: (
            f"计划 {result['summary']['plan_count']} 个，"
            f"新工单 {result['summary']['order_count']} 张，"
            f"明细 {result['summary']['detail_count']} 条"
        ),
    ),
    title="拆分工单",
)
prepare_rework_set = with_progress(
    make_artifact_result_tool(
        _prepare_rework_set,
        artifact_type="rework_set",
        summary=lambda result: (
            f"新工单 {result['summary']['new_orders']} 张，"
            f"旧工单 {result['summary']['old_orders']} 张，"
            f"明细 {len(result['details'])} 条"
        ),
    ),
    title="生成待排集合",
)
schedule_work_orders = with_progress(
    make_artifact_result_tool(
        _schedule_work_orders,
        artifact_type="scheduled_work_orders",
        summary=_schedule_summary,
    ),
    title="工单排班",
)

REQUEST_NAMESPACE = "work_order_schedule"


def _make_schedule_request_tool(store: WorkflowStateStore | None = None):
    def schedule_request(request_id: str, config: RunnableConfig) -> dict[str, Any]:
        """按本会话已提交的结构化请求排班，旧工单始终从业务接口查询。"""
        if store is None:
            raise RuntimeError("排班请求工具尚未绑定运行时状态")
        thread_id = str((config.get("configurable") or {}).get("thread_id") or "")
        data = store.load(thread_id, REQUEST_NAMESPACE, request_id)
        if not isinstance(data, dict):
            raise ValueError("排班请求不存在或已过期，请重新提交计划报文")
        payload = data["payload"]
        _check_payload(payload)
        return run_pipeline(
            payload,
            enforce_quota=data["enforce_quota"],
            skip_weekdays=data["skip_weekdays"],
            holidays=data["holidays"],
        )

    wrapped = StructuredTool.from_function(
        schedule_request,
        name="schedule_work_orders_from_request",
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


def _make_confirm_work_order_submission_tool(store: ArtifactStore | None = None):
    def confirm(schedule_artifact_id: str, config: RunnableConfig) -> dict | str:
        """展示本轮完整排班结果，人工确认后交给前端创建。"""
        if store is None:
            raise RuntimeError("确认工单工具尚未绑定运行时 ArtifactStore")
        configurable = (config or {}).get("configurable") or {}
        thread_id = str(configurable.get("thread_id") or "")
        run_id = str(configurable.get("artifact_run_id") or "")
        entry = store.get(thread_id, schedule_artifact_id, run_id=run_id)
        if entry is None or entry[0].type != "scheduled_work_orders":
            raise ValueError("排班结果不存在或不属于本轮请求，请重新排班")
        result = entry[1]

        @tool("confirm_work_order_submission")
        def approve(schedule_artifact_id: str) -> dict[str, str]:
            """确认本轮工单排班结果可交给前端创建。"""
            return {
                "status": "confirmed_for_submission",
                "scheduleArtifactId": schedule_artifact_id,
                "message": "已确认提交意向",
            }

        approval = require_approval(
            approve,
            ApprovalSpec(
                description="请核对结果。确认后可使用本轮数据创建机场与人工工单。",
                display_title="确认创建机场与人工工单",
                display_arguments=lambda _: {
                    "排班摘要": _schedule_summary(result),
                    "完整组装结果": result,
                },
                required_fields=("schedule_artifact_id",),
            ),
        )
        return approval.invoke({"schedule_artifact_id": schedule_artifact_id})

    wrapped = StructuredTool.from_function(
        confirm,
        name="confirm_work_order_submission",
        metadata={"axis_requires_approval": True},
    )
    object.__setattr__(
        wrapped,
        "_axis_rebind_artifact_tool",
        lambda runtime_store, _producer: _make_confirm_work_order_submission_tool(runtime_store),
    )
    return wrapped


confirm_work_order_submission = with_progress(
    _make_confirm_work_order_submission_tool(),
    title="确认工单提交",
)

TOOLS = [
    split_work_orders,
    prepare_rework_set,
    schedule_work_orders,
    schedule_work_orders_from_request,
    confirm_work_order_submission,
]

__all__ = [
    "TOOLS", "split_work_orders", "prepare_rework_set",
    "schedule_work_orders", "schedule_work_orders_from_request",
    "confirm_work_order_submission",
]
