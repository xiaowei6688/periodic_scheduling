"""拆单、待排集合与排班 Agent 工具。"""

from __future__ import annotations

from typing import Any

from langchain_core.tools import tool

from core.artifacts import make_artifact_result_tool
from core.steps import with_progress
from .rework_orders import collect_rework_set
from ..service import run_pipeline
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
    if "mockExistingRows" in payload and not isinstance(payload["mockExistingRows"], list):
        raise ValueError("payload.mockExistingRows 必须是数组")


def _mock_existing_rows(payload: dict) -> list[dict] | None:
    """页面调试报文可内嵌旧工单；正式报文仍从业务接口查询。"""
    if "mockExistingRows" not in payload:
        return None
    return payload["mockExistingRows"]


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


def _schedule_orders(result: dict) -> list[dict]:
    return [
        order
        for strategy in result.get("planStrategyList") or []
        for plan in strategy.get("planList") or []
        for order in plan.get("workOrders") or []
    ]


def _date_part(value: Any) -> str | None:
    text = str(value or "").strip()
    return text[:10] if len(text) >= 10 else None


def _device_count(orders: list[dict]) -> int:
    total = 0
    for order in orders:
        try:
            total += int(order.get("parentDeviceNum") or 0)
        except (TypeError, ValueError):
            continue
    return total


def _confirmation_view(result: dict) -> dict[str, Any]:
    """把排班结果转换成业务前端可直接渲染的两类确认视图。"""
    orders = _schedule_orders(result)
    airport_orders = [o for o in orders if str(o.get("inspectionMethod")) == "1"]
    manual_orders = [o for o in orders if str(o.get("inspectionMethod")) == "2"]
    scheduled = [o for o in orders if o.get("startDate") and o.get("endDate")]
    forced = [o for o in orders if o.get("forceInserted")]
    unplaced = [o for o in orders if not o.get("startDate") or not o.get("endDate")]
    plans = [
        plan
        for strategy in result.get("planStrategyList") or []
        for plan in strategy.get("planList") or []
    ]
    range_starts = [
        date for order in orders
        if (date := _date_part(order.get("cycleStartDate") or order.get("preStartDate")))
    ]
    range_ends = [
        date for order in orders
        if (date := _date_part(order.get("cycleEndDate") or order.get("preEndDate")))
    ]
    impact_stats = (result.get("impactChains") or {}).get("stats") or {}
    moved_old = {
        order.get("workOrderGuid")
        for order in result.get("oldWorkOrders") or []
        if order.get("workOrderGuid") and order.get("result") != "未调整"
    }
    proposed_updates = {
        str(
            update.get("work_order_guid") or update.get("workOrderGuid")
            if isinstance(update, dict) else update
        )
        for proposal in result.get("forceProposals") or []
        for update in proposal.get("updateProposals") or []
        if update
    }
    manual_plan_ids = {
        str(order.get("planGuid"))
        for order in manual_orders
        if order.get("planGuid")
    }
    airport_schedule_starts = [
        date for order in airport_orders
        if order.get("startDate") and (date := _date_part(order.get("startDate")))
    ]
    airport_schedule_ends = [
        date for order in airport_orders
        if order.get("endDate") and (date := _date_part(order.get("endDate")))
    ]
    warnings = result.get("warnings") or []
    is_force_insert = result.get("entry") == "forceInsert" or bool(unplaced)
    chain_count = int(impact_stats.get("chainCount") or 0)
    round_counts = [int(value) for value in impact_stats.get("roundCounts") or []]
    is_reworked = result.get("entry") == "insertRework" and chain_count > 0
    counts = {
        "planTotal": len(plans),
        "newTotal": len(orders),
        "airport": len(airport_orders),
        "manual": len(manual_orders),
        "airportDevices": _device_count(airport_orders),
        "manualDevices": _device_count(manual_orders),
        "manualPlans": len(manual_plan_ids),
        "scheduled": len(scheduled),
        "forced": len(forced),
        "airportScheduled": sum(
            bool(o.get("startDate") and o.get("endDate")) for o in airport_orders
        ),
        "unplaced": len(unplaced),
        "directAffected": int(impact_stats.get("directCount") or 0),
        "indirectAffected": int(impact_stats.get("indirectCount") or 0),
        "oldUpdates": len(proposed_updates) if is_force_insert else len(moved_old),
        "impactChains": chain_count,
        "totalReworkRounds": int(impact_stats.get("totalRounds") or sum(round_counts)),
        "reworkRounds": round_counts,
        "warnings": len(warnings),
    }
    inspection_range = {
        "start": min(range_starts) if range_starts else None,
        "end": max(range_ends) if range_ends else None,
    }
    airport_schedule_range = {
        "start": min(airport_schedule_starts) if airport_schedule_starts else None,
        "end": max(airport_schedule_ends) if airport_schedule_ends else None,
    }
    range_text = (
        f"{inspection_range['start']} 至 {inspection_range['end']}"
        if inspection_range["start"] and inspection_range["end"]
        else "未提供完整巡检起止日期"
    )
    airport_range_text = (
        f"{airport_schedule_range['start']} 至 {airport_schedule_range['end']}"
        if airport_schedule_range["start"] and airport_schedule_range["end"]
        else range_text
    )
    warning_note = (
        f"本次另有 {counts['warnings']} 条排班告警，请在排期方案中一并核对。"
        if counts["warnings"]
        else "本次没有额外排班告警。"
    )

    if is_force_insert:
        proposals = [
            proposal for proposal in result.get("forceProposals") or []
            if proposal.get("feasible")
        ]
        visible_proposals = [
            proposal for proposal in proposals
            if proposal.get("forceTimePeriod") == "16:30:00-17:30:00"
        ]
        infrared_proposals = [
            proposal for proposal in proposals
            if proposal.get("forceTimePeriod") == "21:00:00-22:00:00"
        ]
        proposal_dates = sorted({
            str(proposal.get("proposedStartDate") or "")[:10]
            for proposal in proposals if proposal.get("proposedStartDate")
        })
        force_parts = []
        if visible_proposals:
            force_parts.append(
                f"可见光 {len(visible_proposals)} 张安排在每日 16:30-17:30")
        if infrared_proposals:
            force_parts.append(
                f"红外 {len(infrared_proposals)} 张安排在每日 21:00-22:00")
        force_schedule_note = "；".join(force_parts) or "当前没有可执行的强排候选时段"
        date_note = "、".join(proposal_dates) if proposal_dates else "无候选日期"
        conflict_count = sum(
            len(proposal.get("affectedOrders") or []) for proposal in proposals
        )
        summary_lines = [
            (
                f"已校对 {counts['planTotal']} 个计划。新增工单按执行方式分流为："
                f"机场 {counts['airportDevices']} 基、{counts['airport']} 张工单；"
                f"人工 {counts['manualDevices']} 基、{counts['manual']} 张工单。"
            ),
            (
                f"巡检日期为 {range_text}。规则排班完成 "
                f"{counts['newTotal'] - counts['forced'] - counts['unplaced']} 张；"
                f"另有 {counts['forced']} 张已生成强排候选时间，"
                f"真正无候选时间的工单 {counts['unplaced']} 张。"
            ),
            (
                f"强排建议：{force_schedule_note}；候选日期为 {date_note}。"
                "工单按专业优先级进入强排队列，并在各自允许日期内按每日强排数量最少优先均摊。"
            ),
            (
                f"候选时段与 {conflict_count} 张已有工单发生时间重叠；"
                f"影响关系统计为直接影响 {counts['directAffected']} 张、"
                f"间接影响 {counts['indirectAffected']} 张。"
                "变电旧工单时间保持不变，强排不会擅自修改任何旧工单。"
                "每张工单的推荐日期、影响对象和推荐原因请在强排方案中核对。"
            ),
            warning_note,
            "详细排期可以在左侧“排期方案”页签查看，影响关系和旧工单调整明细可以在左侧“受影响工单”页签核对。",
            "如需改期，请使用左侧底部的“修改需求”，确认后调整原表单中的“巡检起止日期”，再重新判断。",
        ]
        variant = "force_insert"
        title = "确认强插"
        approve_label = "确认强插"
        modify_label = "修改时间"
        reject_label = "取消"
    elif is_reworked:
        if round_counts and len(set(round_counts)) == 1:
            chain_note = (
                f"{counts['impactChains']} 条影响链各完成 {round_counts[0]} 轮重排"
            )
        else:
            rounds = "、".join(str(value) for value in round_counts)
            chain_note = (
                f"{counts['impactChains']} 条影响链共完成 {counts['totalReworkRounds']} 轮重排"
                + (f"（各链轮次：{rounds}）" if rounds else "")
            )
        if (
            impact_stats.get("allChainsCompleted")
            and impact_stats.get("allPlacedWithinWindow")
        ):
            old_order_note = (
                f"{counts['oldUpdates']} 张旧工单均已在自身允许范围内排妥"
            )
        else:
            old_order_note = (
                f"{counts['oldUpdates']} 张旧工单已完成调整，请核对各工单允许范围"
            )
        summary_lines = [
            (
                f"{counts['planTotal']} 个计划已完成排期，拟创建 {counts['newTotal']} 张工单"
                f"（机场 {counts['airport']} 张、人工 {counts['manual']} 张）。"
                f"{chain_note}，{old_order_note}。请核对后确认创建。"
            ),
            warning_note,
            "详细排期可以在左侧“排期方案”页签查看。影响关系和旧工单调整明细，可以在左侧“受影响工单”页签核对。",
            "如需改期，请使用左侧底部的“修改需求”，确认后调整原表单中的“巡检起止日期”，再重新判断。",
        ]
        variant = "reworked"
        title = "确认创建并更新旧单"
        approve_label = "确认创建并更新旧单"
        modify_label = None
        reject_label = "取消"
    else:
        old_order_note = (
            f"已调整 {counts['oldUpdates']} 张旧工单并完成重排。"
            if counts["oldUpdates"]
            else "已有工单保持不变。"
        )
        manual_note = (
            f"同一子计划内的人工设备集中为 1 张工单，共涉及 {counts['manualPlans']} 个子计划、"
            f"形成 {counts['manual']} 张工单，已按周安排候选窗口。"
            if manual_orders
            else "本次没有人工设备，无需生成人工候选工单。"
        )
        summary_lines = [
            (
                f"已按执行方式分流：机场 {counts['airportDevices']} 基、人工 "
                f"{counts['manualDevices']} 基，共生成 {counts['newTotal']} 张新工单。"
            ),
            (
                f"机场部分拆分为 {counts['airport']} 张工单，已按允许日期和作业时段完成 "
                f"{counts['airportScheduled']} 张排期，排期日期覆盖 {airport_range_text}。"
            ),
            manual_note + "请核对工单与候选窗口，确认创建后由平台完成正式人工排期。",
            old_order_note + warning_note,
            "详细排期可以在左侧“排期方案”页签查看。",
            "如需改期，请使用左侧底部的“修改需求”，确认后调整原表单中的“巡检起止日期”，再重新判断。",
        ]
        variant = "ready"
        title = "确认创建机场与人工工单"
        approve_label = "确认创建机场与人工工单"
        modify_label = None
        reject_label = "取消"

    actions = [{
        "key": "approve",
        "label": approve_label,
        "action": "approve",
        "comment": "",
    }]
    if modify_label:
        actions.append({
            "key": "modify_time",
            "label": modify_label,
            "action": "reject",
            "comment": "修改时间",
        })
    actions.append({
        "key": "cancel",
        "label": reject_label,
        "action": "reject",
        "comment": "",
    })

    return {
        "variant": variant,
        "title": title,
        "sectionTitle": "本次确认",
        "summary": "\n".join(summary_lines),
        "summaryLines": summary_lines,
        "inspectionRange": inspection_range,
        "airportScheduleRange": airport_schedule_range,
        "counts": counts,
        "actions": actions,
        "approveLabel": approve_label,
        "modifyLabel": modify_label,
        "rejectLabel": reject_label,
    }


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


__all__ = [
    "split_work_orders",
    "prepare_rework_set",
    "schedule_work_orders",
]

