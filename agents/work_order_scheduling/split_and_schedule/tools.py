"""工单拆分与排班的 Agent 工具入口。计算结果不落盘、不落库。"""

from __future__ import annotations

import json

from langchain_core.tools import tool

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


def _json(value: dict) -> str:
    return json.dumps(value, ensure_ascii=False)


@tool("split_work_orders")
def _split_work_orders(payload: dict) -> str:
    """根据计划报文拆分新工单，返回工单、明细和汇总计数；不排班。"""
    _check_payload(payload)
    orders, details, results = split_all(payload)
    problems = verify(results)
    if problems:
        raise ValueError("拆单守恒校验失败：" + "；".join(problems))
    return _json({
        "orders": orders,
        "details": details,
        "summary": {
            "plan_count": len(results),
            "order_count": len(orders),
            "detail_count": len(details),
            "removed_channel_count": sum(len(result.removed_channel) for result in results),
        },
    })


@tool("prepare_rework_set")
def _prepare_rework_set(payload: dict, existing_rows: list[dict] | None = None) -> str:
    """拆分新工单并合并旧工单，返回待排集合；不执行排班。existing_rows 未传时在线查询旧工单，空数组表示没有旧工单。"""
    _check_payload(payload)
    pending = collect_rework_set(payload, existing_rows)
    return _json({
        "summary": pending.summary(),
        "orders": pending.all_orders,
        "details": pending.details,
    })


@tool("schedule_work_orders")
def _schedule_work_orders(
    payload: dict,
    existing_rows: list[dict] | None = None,
    enforce_quota: bool = True,
    skip_weekdays: list[int] | None = None,
    holidays: list[str] | None = None,
) -> str:
    """拆单并排班，返回按计划回组的新旧工单和重排结果。existing_rows 未传时在线查询旧工单，空数组表示没有旧工单；skip_weekdays 为 1=周一至 7=周日，holidays 为 YYYY-MM-DD 日期。"""
    _check_payload(payload)
    return _json(run_pipeline(
        payload,
        existing_rows,
        enforce_quota=enforce_quota,
        skip_weekdays=skip_weekdays,
        holidays=holidays,
    ))


split_work_orders = with_progress(_split_work_orders, title="拆分工单")
prepare_rework_set = with_progress(_prepare_rework_set, title="生成待排集合")
schedule_work_orders = with_progress(_schedule_work_orders, title="工单排班")

TOOLS = [split_work_orders, prepare_rework_set, schedule_work_orders]

__all__ = ["TOOLS", "split_work_orders", "prepare_rework_set", "schedule_work_orders"]
