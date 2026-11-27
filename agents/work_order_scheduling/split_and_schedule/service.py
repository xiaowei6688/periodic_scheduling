"""工单拆分、旧单合并和排班的流程编排。"""

from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Optional

from .tools import rework_orders
from .output import _build_output
from .tools.rework_algorithm import rework
from .tools.rework_orders import (
    ReworkSet,
    build_rework_set,
)
from .tools.split_work_orders import month_window, schedule_reference_date, split_all, verify

logger = logging.getLogger(__name__)


def split_pipeline_input(
    payload: dict,
    reference_date: date | datetime | str | None = None,
) -> tuple[list[dict], list[dict], str, str]:
    """校验并拆分计划，返回新工单、明细和整体允许窗口。"""
    reference_date = schedule_reference_date(payload, reference_date)
    orders, details, results = split_all(payload, reference_date=reference_date)
    problems = verify(results)
    if problems:
        raise ValueError("拆单守恒校验失败：" + "；".join(problems))
    window_start, window_end = month_window(reference_date)
    return orders, details, window_start, window_end


def prepare_pipeline_orders(
    orders: list[dict],
    details: list[dict],
    window_start: str,
    window_end: str,
    existing_rows: Optional[list[dict]],
) -> ReworkSet:
    """获取、筛选并合并已有工单。"""
    rows = (
        rework_orders.fetch_existing_orders(window_start, window_end)
        if existing_rows is None else existing_rows
    )
    return build_rework_set(
        orders, rows, window_start, window_end, details=details)


def schedule_pipeline_orders(
    pending: ReworkSet,
    *,
    enforce_quota: bool,
    skip_weekdays: Optional[list[int]],
    holidays: Optional[list[str]],
):
    """执行规则排班、旧单重排和强排候选计算。"""
    return rework(
        pending.new_orders,
        pending.old_orders,
        pending.window["start"],
        pending.window["end"],
        enforce_quota=enforce_quota,
        skip_weekdays=skip_weekdays,
        holidays=holidays,
    )


def inspect_pipeline_result(result) -> dict:
    """判定排班分支，并返回供进度展示使用的业务统计。"""
    if result.entry not in {"normal", "insert_rework", "force_insert"}:
        raise ValueError(f"未知排班入口：{result.entry}")
    feasible_forces = sum(proposal.feasible for proposal in result.force_proposals)
    return {
        "entry": result.entry,
        "placed": len(result.new_placements),
        "moved_old": len(result.changed_old_orders),
        "rework_steps": len(result.rework_steps),
        "force_candidates": feasible_forces,
        "unplaced": len(result.unplaced),
        "warnings": len(result.warnings),
    }


def assemble_pipeline_output(payload: dict, pending: ReworkSet, result) -> dict:
    """按既有业务契约组装最终排班结果。"""
    return _build_output(payload, pending, result)


def run_pipeline(payload: dict, existing_rows: Optional[list[dict]] = None, *,
                 enforce_quota: bool = True,
                 save_existing: Optional[str] = None,
                 skip_weekdays: Optional[list[int]] = None,
                 holidays: Optional[list[str]] = None,
                 reference_date: date | datetime | str | None = None) -> dict:
    """拆单 → 拉旧单合并 → 重排，按输入报文格式回组结果。"""
    logger.info("整流程开始 配额约束=%s 跳过星期=%s 节假日=%s 离线旧单=%s",
                enforce_quota, skip_weekdays, holidays,
                "是" if existing_rows is not None else "否")
    orders, details, window_start, window_end = split_pipeline_input(
        payload, reference_date)
    pending = prepare_pipeline_orders(
        orders, details, window_start, window_end, existing_rows)
    result = schedule_pipeline_orders(
        pending,
        enforce_quota=enforce_quota,
        skip_weekdays=skip_weekdays,
        holidays=holidays,
    )
    inspect_pipeline_result(result)
    return assemble_pipeline_output(payload, pending, result)
