"""排班结果人工确认 Agent 工具。"""

from __future__ import annotations

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import StructuredTool, tool

from core.approval import ApprovalSpec, require_approval
from core.artifacts import ArtifactStore
from core.steps import with_progress
from .scheduling import _confirmation_view, _schedule_summary


def _make_confirm_work_order_submission_tool(store: ArtifactStore | None = None):
    def confirm(schedule_artifact_id: str, config: RunnableConfig) -> dict | str:
        """展示本轮完整排班结果并发起人工确认。"""
        if store is None:
            raise RuntimeError("确认工单工具尚未绑定运行时 ArtifactStore")
        configurable = (config or {}).get("configurable") or {}
        thread_id = str(configurable.get("thread_id") or "")
        run_id = str(configurable.get("artifact_run_id") or "")
        entry = store.get(thread_id, schedule_artifact_id, run_id=run_id)
        if entry is None or entry[0].type != "scheduled_work_orders":
            raise ValueError("排班结果不存在或不属于本轮请求，请重新排班")
        result = entry[1]
        confirmation = _confirmation_view(result)

        @tool("confirm_work_order_submission")
        def approve(schedule_artifact_id: str) -> dict[str, str]:
            """确认本轮工单排班结果。"""
            return {
                "status": "confirmed_for_submission",
                "scheduleArtifactId": schedule_artifact_id,
                "confirmationVariant": confirmation["variant"],
                "message": "已确认提交意向",
            }

        approval = require_approval(
            approve,
            ApprovalSpec(
                description=confirmation["summary"],
                display_title=confirmation["title"],
                display_arguments=lambda _: {
                    "confirmation": confirmation,
                    "scheduleSummary": _schedule_summary(result),
                    "scheduleResult": result,
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
        lambda runtime_store, _producer: _make_confirm_work_order_submission_tool(
            runtime_store),
    )
    return wrapped


confirm_work_order_submission = with_progress(
    _make_confirm_work_order_submission_tool(),
    title="确认工单提交",
)


__all__ = ["confirm_work_order_submission"]
