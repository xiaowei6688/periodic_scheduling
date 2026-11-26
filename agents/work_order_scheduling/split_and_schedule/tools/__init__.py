"""工单拆分与排班 Agent 工具集合。"""

from .scheduling import (
    _check_payload,
    _confirmation_view,
    prepare_rework_set,
    schedule_work_orders,
    split_work_orders,
)
from .request import REQUEST_NAMESPACE, schedule_work_orders_from_request
from .confirmation import confirm_work_order_submission

TOOLS = [
    split_work_orders,
    prepare_rework_set,
    schedule_work_orders,
    schedule_work_orders_from_request,
    confirm_work_order_submission,
]

__all__ = [
    "TOOLS",
    "REQUEST_NAMESPACE",
    "split_work_orders",
    "prepare_rework_set",
    "schedule_work_orders",
    "schedule_work_orders_from_request",
    "confirm_work_order_submission",
]
