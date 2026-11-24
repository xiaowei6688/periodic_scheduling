"""工单拆分、旧单合并和排班的流程编排。"""

from __future__ import annotations

import logging
from typing import Optional

from .output import _build_output
from .rework_algorithm import rework
from .rework_orders import collect_rework_set

logger = logging.getLogger(__name__)


def run_pipeline(payload: dict, existing_rows: Optional[list[dict]] = None, *,
                 enforce_quota: bool = True,
                 save_existing: Optional[str] = None,
                 skip_weekdays: Optional[list[int]] = None,
                 holidays: Optional[list[str]] = None) -> dict:
    """拆单 → 拉旧单合并 → 重排，按输入报文格式回组结果。"""
    logger.info("整流程开始 配额约束=%s 跳过星期=%s 节假日=%s 离线旧单=%s",
                enforce_quota, skip_weekdays, holidays,
                "是" if existing_rows is not None else "否")
    pending = collect_rework_set(payload, existing_rows,
                                 save_existing=save_existing)
    result = rework(pending.new_orders, pending.old_orders,
                    pending.window["start"], pending.window["end"],
                    enforce_quota=enforce_quota,
                    skip_weekdays=skip_weekdays, holidays=holidays)
    return _build_output(payload, pending, result)
