"""工单拆分与排班的 Agent 工具入口。计算结果不落盘、不落库。"""

from __future__ import annotations

from langchain_core.tools import tool

from core.artifacts import make_artifact_result_tool
from core.steps import with_progress
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

TOOLS = [split_work_orders, prepare_rework_set, schedule_work_orders]

__all__ = ["TOOLS", "split_work_orders", "prepare_rework_set", "schedule_work_orders"]
