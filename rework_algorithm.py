"""重排算法 —— 按需求文档的 8 层规则实现。

只做机场工单的整单重排（新工单插入 + 旧工单连续重排 + 强插建议），
以及人工工单的按周错峰。不落库，只输出排期结果与变更集。

输入
----
新工单：split_work_orders.split_all() 的产出（每条带 plan / pre 窗口 / capacity_limit）
旧工单：rework_orders.normalize_existing() 的产出（start/end 有值，pre 通常为空）

两种工单都会先规整成统一内部结构（canonical），再跑算法。

对应文档
--------
L1 判定入口          文档2 第四节处理情形 / FR05
L2 对象筛选          接口说明 5.2 历史工单进入条件
L3 硬约束            文档2 第五节 / 接口说明 5.x / FR06
L4 链式重排          文档2 第五、六节 / FR06 因果链
L5 占用计算          文档2 第六节 占用计算
L6 强插边界          FR07 / 文档2 第四节
L7 变更集            接口说明 5.3 历史工单更新范围
L8 人工错峰          接口说明 6.1 / FR08

用法
----
    python rework_algorithm.py --input out/rework_set.json --out out/rework_result.json
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

from split_work_orders import (
    INSPECTION_AIRPORT,
    INSPECTION_MANUAL,
    budget,
    is_airport_infrared,
    local_time,
)

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_INPUT = BASE_DIR / "out" / "rework_set.json"
DEFAULT_OUTPUT = BASE_DIR / "out" / "rework_result.json"

# ---- 状态码（接口说明 5.2 / 6.1）----
STATUS_QUEUED = "9"          # 排队中：可重排
STATUS_FIXED = {"8", "12", "16"}   # 固定占用（16 只有预计时长>0 才占位）
# 参与占用日历的状态全集 8/9/12/16

# ---- 无作业时段时的默认窗口（文档2 第六节 / 接口说明 4.2）----
DEFAULT_WINDOW_VISIBLE = "08:30:00-17:30:00"
DEFAULT_WINDOW_INFRARED = "05:00:00-08:00:00;19:00:00-22:00:00"

DATE_FMT = "%Y-%m-%d %H:%M:%S"
DAY_FMT = "%Y-%m-%d"


# ================================================================ 通用工具


def parse_time_windows(time_period: str) -> list[tuple[int, int]]:
    """把 '08:30:00-17:30:00;12:45:00-18:30:00' 解析成 (起,止) 秒（距当日 0 点）。"""
    if not time_period or not time_period.strip():
        return []
    segments: list[tuple[int, int]] = []
    for part in time_period.split(";"):
        part = part.strip()
        if not part:
            continue
        if "-" not in part:
            raise ValueError(f"作业时段无法解析: {part!r}")
        start_text, end_text = part.split("-", 1)
        start, end = local_time(start_text), local_time(end_text)
        ss = start.hour * 3600 + start.minute * 60 + start.second
        ee = end.hour * 3600 + end.minute * 60 + end.second
        if ee <= ss:
            raise ValueError(f"作业时段非法（结束不晚于开始）: {part!r}")
        segments.append((ss, ee))
    return segments


def parse_dt(text: Any) -> Optional[datetime]:
    if text is None:
        return None
    s = str(text).strip().replace("T", " ")
    if not s:
        return None
    for fmt in (DATE_FMT, "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def day_str(dt: datetime) -> str:
    return dt.strftime(DAY_FMT)


def dt_str(dt: datetime) -> str:
    return dt.strftime(DATE_FMT)


def seconds_to_dt(day: str, seconds: int) -> datetime:
    base = datetime.strptime(day, DAY_FMT)
    return base + timedelta(seconds=seconds)


def iter_days(start: str, end: str) -> list[str]:
    """闭区间 [start, end] 内的自然日列表（YYYY-MM-DD）。"""
    s = parse_dt(start)
    e = parse_dt(end)
    if s is None or e is None:
        return []
    cur = s.replace(hour=0, minute=0, second=0, microsecond=0)
    last = e.replace(hour=0, minute=0, second=0, microsecond=0)
    days: list[str] = []
    while cur <= last:
        days.append(cur.strftime(DAY_FMT))
        cur += timedelta(days=1)
    return days


def effective_time_period(order: dict) -> str:
    """工单自己的作业时段；旧工单没时段时用默认窗口（文档2 第六节）。"""
    tp = order.get("time_period")
    if tp and str(tp).strip():
        return str(tp).strip()
    infrared = is_airport_infrared(order.get("work_nature_sub") or "")
    return DEFAULT_WINDOW_INFRARED if infrared else DEFAULT_WINDOW_VISIBLE


# ================================================================ 规整为统一结构


def canonicalize(order: dict) -> dict:
    """把新/旧两种工单结构规整成同一种内部结构。"""
    if order.get("source") == "existing":
        plan = {}
        return {
            "guid": order.get("work_order_guid"),
            "work_order_no": order.get("work_order_no"),
            "plan_guid": order.get("plan_guid"),
            "airport": order.get("cycle_airport_guid"),
            "seconds": int(order.get("second_num") or 0),
            "limit": order.get("capacity_limit"),
            "inspection": str(order.get("inspection_method") or INSPECTION_AIRPORT),
            "speciality": order.get("professional_category") or "",
            "work_nature": order.get("work_nature"),
            "work_nature_sub": order.get("work_nature_sub") or "",
            "one_day": order.get("one_day"),
            "time_period": None,
            "pre_start": order.get("pre_start_date"),
            "pre_end": order.get("pre_end_date"),
            "start": order.get("start_date"),
            "end": order.get("end_date"),
            "source": "existing",
            "status": str(order.get("work_order_status") or ""),
            "schedule_reason": order.get("schedule_reason")
            or (order.get("raw") or {}).get("scheduleReason"),
            "raw": order,
        }
    plan = order.get("plan") or {}
    return {
        "guid": order.get("work_order_guid"),
        "work_order_no": order.get("work_order_no"),
        "plan_guid": order.get("plan_guid"),
        "airport": order.get("cycle_airport_guid"),
        "seconds": int(order.get("second_num") or 0),
        "limit": order.get("capacity_limit"),
        "inspection": str(order.get("inspection_method") or INSPECTION_AIRPORT),
        "speciality": plan.get("speciality") or "",
        "work_nature": plan.get("workNature"),
        "work_nature_sub": plan.get("workNatureSub") or "",
        "one_day": plan.get("oneDay"),
        "time_period": order.get("time_period"),
        "pre_start": order.get("pre_start_date"),
        "pre_end": order.get("pre_end_date"),
        "start": order.get("start_date"),
        "end": order.get("end_date"),
        "source": "new",
        "status": order.get("work_order_status"),
        "raw": order,
    }


def effective_limit(order: dict) -> int:
    """单日工作量上限。新工单已有；旧工单按类型和默认时段现算。"""
    limit = order.get("limit")
    if limit:
        return int(limit)
    infrared = is_airport_infrared(order.get("work_nature_sub") or "")
    segments = parse_time_windows(effective_time_period(order))
    window_len = sum(e - s for s, e in segments)
    lim = budget(infrared)
    if window_len:
        lim = min(lim, window_len)
    return lim


def movable_window(order: dict, global_start: str, global_end: str) -> tuple[str, str]:
    """工单可以挪动的时间范围。

    - 新工单：计划窗口 pre_start ~ pre_end
    - 旧工单：数据里若带自身的 pre 窗口就用它；没有则退回本次重排整体范围
    """
    start = order.get("pre_start") or order.get("start") or global_start
    end = order.get("pre_end") or order.get("end") or global_end
    return start, end


# ================================================================ 排班日历（L5）


class Calendar:
    """单个机场的占用日历：自然日 -> 已占用片段。

    占用是「整单」：一个工单的 seconds 固定，占用一片或多片同日片段。
    日历按机场独立维护，工单不跨机场（L3 机场不降级）。
    """

    def __init__(self) -> None:
        # guid -> {"date": day, "seconds": int, "frags": [(s,e), ...]}
        self.slots: dict[str, dict] = {}

    def place(self, guid: str, day: str, frags: list[tuple[int, int]], seconds: int) -> None:
        self.slots[guid] = {"date": day, "seconds": seconds, "frags": list(frags)}

    def remove(self, guid: str) -> Optional[dict]:
        return self.slots.pop(guid, None)

    def restore(self, guid: str, saved: Optional[dict]) -> None:
        if saved is None:
            self.slots.pop(guid, None)
        else:
            self.slots[guid] = saved

    def snapshot(self) -> dict:
        return {g: dict(v) for g, v in self.slots.items()}

    def restore_snapshot(self, snap: dict) -> None:
        self.slots = {g: dict(v) for g, v in snap.items()}

    def occupied(self, day: str) -> list[tuple[int, int]]:
        out: list[tuple[int, int]] = []
        for slot in self.slots.values():
            if slot["date"] == day:
                out.extend(slot["frags"])
        return sorted(out)

    def occupants(self, day: str) -> list[str]:
        return [g for g, slot in self.slots.items() if slot["date"] == day]

    def load_seconds(self, day: str) -> int:
        return sum(slot["seconds"] for slot in self.slots.values() if slot["date"] == day)

    def free_intervals(self, day: str, windows: list[tuple[int, int]]) -> list[tuple[int, int]]:
        occ = self.occupied(day)
        free: list[tuple[int, int]] = []
        for ws, we in windows:
            cursor = ws
            for os_, oe in occ:
                if oe <= cursor:
                    continue
                if os_ >= we:
                    break
                if os_ > cursor:
                    free.append((cursor, min(os_, we)))
                cursor = max(cursor, oe)
                if cursor >= we:
                    break
            if cursor < we:
                free.append((cursor, we))
        merged: list[tuple[int, int]] = []
        for s, e in free:
            if merged and merged[-1][1] >= s:
                merged[-1] = (merged[-1][0], max(merged[-1][1], e))
            else:
                merged.append((s, e))
        return merged

    def can_place(self, order: dict, day: str,
                  enforce_quota: bool) -> Optional[list[tuple[int, int]]]:
        """order 能否落在 day：占用不重叠（必须）＋ 80% 配额（可选）。"""
        windows = parse_time_windows(effective_time_period(order))
        free = self.free_intervals(day, windows)
        seconds = order["seconds"]

        if enforce_quota:
            infrared = is_airport_infrared(order.get("work_nature_sub") or "")
            if self.load_seconds(day) + seconds > budget(infrared):
                return None

        frags: list[tuple[int, int]] = []
        remaining = seconds
        for s, e in free:
            if remaining <= 0:
                break
            span = e - s
            if span <= 0:
                continue
            take = min(span, remaining)
            frags.append((s, s + take))
            remaining -= take
        if remaining > 0:
            return None
        if not frags:  # 0 秒工单：锚到第一个窗口起点
            frags = [(windows[0][0], windows[0][0])] if windows else [(0, 0)]
        return frags


class Scheduler:
    def __init__(self, window_start: str, window_end: str,
                 enforce_quota: bool = False, max_depth: int = 20) -> None:
        self.window_start = window_start
        self.window_end = window_end
        self.enforce_quota = enforce_quota
        self.max_depth = max_depth
        self.calendars: dict[str, Calendar] = {}
        self.orders: dict[str, dict] = {}
        self.reworkable_guids: set[str] = set()

    def calendar(self, airport: str) -> Calendar:
        return self.calendars.setdefault(airport or "", Calendar())

    def find_slot(self, order: dict) -> Optional[tuple[str, list[tuple[int, int]]]]:
        start, end = movable_window(order, self.window_start, self.window_end)
        cal = self.calendar(order["airport"])
        for day in iter_days(start, end):
            frags = cal.can_place(order, day, self.enforce_quota)
            if frags is not None:
                return day, frags
        return None

    def slot_of(self, guid: str) -> Optional[dict]:
        for cal in self.calendars.values():
            if guid in cal.slots:
                return cal.slots[guid]
        return None

    def _cal_by_order(self, order: dict) -> Calendar:
        return self.calendar(order["airport"])


# ================================================================ L2 对象筛选


@dataclass
class Categorized:
    fixed: list[dict] = field(default_factory=list)        # 固定占用（不可动）
    reworkable: list[dict] = field(default_factory=list)   # 可重排旧单
    warnings: list[str] = field(default_factory=list)


def _window_intersects(order: dict, window_start: str, window_end: str) -> bool:
    """工单的预执行窗口是否与请求范围相交（接口说明 5.2）。"""
    s = parse_dt(order.get("pre_start")) or parse_dt(order.get("start"))
    e = parse_dt(order.get("pre_end")) or parse_dt(order.get("end"))
    ws = parse_dt(window_start)
    we = parse_dt(window_end)
    if s is None or e is None or ws is None or we is None:
        return True
    return not (e < ws or s > we)


def categorize_old(orders: list[dict], window_start: str, window_end: str) -> Categorized:
    """把旧工单分成「固定占用」和「可重排」。

    规则（接口说明 5.2）：
      - 变电（sms）历史工单保持固定占用，不重排
      - 状态 9 且非变电 → 可重排（且预执行窗口须与请求范围相交）
      - 状态 8/12/16 → 固定占用（16 只有预计时长>0 才占位）
      - 状态 16 且预计时长<=0 → 不占位
    """
    result = Categorized()
    for order in orders:
        guid = order.get("guid")
        if order["inspection"] != INSPECTION_AIRPORT:
            continue  # 人工走 L8，不在此处
        status = order["status"]
        speciality = order.get("speciality") or ""
        if status == STATUS_QUEUED and speciality != "sms":
            if _window_intersects(order, window_start, window_end):
                result.reworkable.append(order)
            # 预执行窗口与请求范围不相交：不进入本次任务，也不占本次日历
        elif status in STATUS_FIXED:
            if status == "16" and order["seconds"] <= 0:
                continue  # 16 无时长不占位
            result.fixed.append(order)
        elif status == STATUS_QUEUED and speciality == "sms":
            result.fixed.append(order)  # 变电历史工单保持固定占用
        elif speciality == "sms":
            result.fixed.append(order)
        else:
            result.warnings.append(f"{guid}: 状态 {status} 不参与占用日历，已忽略")
    return result


def _occupy_existing(sched: Scheduler, order: dict, warnings: list[str]) -> None:
    """把已有工单的当前起止时间放进日历（L5 共享占用 + 补齐占用）。

    仅「排队中(9) 且已标记未能排下」的工单跳过：其保留窗口不是真实执行占用
    （文档2 §六 / Java 6572-6573，限定 workOrderStatus == 9）。
    """
    if order.get("status") == STATUS_QUEUED and "未能排下" in (order.get("schedule_reason") or ""):
        return
    start = parse_dt(order.get("start"))
    end = parse_dt(order.get("end"))
    if start is None or end is None:
        return
    if day_str(start) != day_str(end):
        # 数据越日（测试环境常见）：只占开始日，并记告警
        warnings.append(f"{order['guid']}: 起止跨日（{order['start']} ~ {order['end']}），按开始日占用")
        end = start
    seconds = order["seconds"]
    if seconds <= 0:
        return
    s_sec = start.hour * 3600 + start.minute * 60 + start.second
    e_sec = start.hour * 3600 + start.minute * 60 + start.second + seconds
    cal = sched.calendar(order["airport"])
    cal.place(order["guid"], day_str(start), [(s_sec, e_sec)], seconds)


# ================================================================ L4 链式重排


@dataclass
class ReworkStep:
    """一条重排链：某工单抢占了另一张旧单，引发其挪动。"""
    preempted_by: str                       # 抢占方 guid
    moved_guid: str                         # 被挪动的旧单 guid
    old_start: str
    old_end: str
    new_start: str
    new_end: str


def _format_slot(day: str, frags: list[tuple[int, int]]) -> tuple[str, str, int]:
    start_dt = seconds_to_dt(day, frags[0][0])
    end_dt = seconds_to_dt(day, frags[-1][1])
    return dt_str(start_dt), dt_str(end_dt), frags[-1][1] - frags[0][0]


def _try_place(sched: Scheduler, order: dict, banned: set[str], depth: int,
               steps: list[ReworkStep]) -> Optional[bool]:
    """回溯地把 order 放进去（可能引发一条挪动链）。成功返回 True 并累计 steps。"""
    loc = sched.find_slot(order)
    if loc is not None:
        day, frags = loc
        sched._cal_by_order(order).place(order["guid"], day, frags, order["seconds"])
        return True
    if depth >= sched.max_depth:
        return None

    start, end = movable_window(order, sched.window_start, sched.window_end)
    for day in iter_days(start, end):
        cal = sched.calendar(order["airport"])
        candidates = [g for g in cal.occupants(day)
                      if g in sched.reworkable_guids and g not in banned and g != order["guid"]]
        # 抢占候选按「自身规则优先级」排序（文档2 §五：每步按当前工单自身优先级判断）
        candidates.sort(key=lambda g: _priority(sched.orders[g]))
        for occ_guid in candidates:
            occ = sched.orders[occ_guid]
            occ_cal = sched._cal_by_order(occ)
            occ_slot = occ_cal.slots.get(occ_guid)
            snapshot = occ_cal.snapshot()
            occ_cal.remove(occ_guid)

            # 先试探把 order 放进刚腾出来的位置
            now = sched.find_slot(order)
            if now is None:
                occ_cal.restore_snapshot(snapshot)
                continue
            day2, frags2 = now
            sched._cal_by_order(order).place(order["guid"], day2, frags2, order["seconds"])

            sub_ok = _try_place(sched, occ, banned | {occ_guid}, depth + 1, steps)
            if sub_ok is None:
                occ_cal.restore_snapshot(snapshot)
                continue

            # occ 已被挪到新位置；记录因果链
            new_slot = sched.slot_of(occ_guid)
            old_day, old_frags = occ_slot["date"], occ_slot["frags"]
            new_day, new_frags = new_slot["date"], new_slot["frags"]
            os_, oe_, _ = _format_slot(old_day, old_frags)
            ns_, ne_, _ = _format_slot(new_day, new_frags)
            steps.append(ReworkStep(preempted_by=order["guid"], moved_guid=occ_guid,
                                    old_start=os_, old_end=oe_, new_start=ns_, new_end=ne_))
            return True

    return None


# ================================================================ L6 强插边界


@dataclass
class ForceProposal:
    order_guid: str
    work_order_no: str
    reason: str
    protected_holders: list[str] = field(default_factory=list)


def build_force_proposals(sched: Scheduler, unplaced: list[dict]) -> list[ForceProposal]:
    """强插建议：只有规则内排不下时产出，且不越过受保护占用（FR07）。"""
    proposals: list[ForceProposal] = []
    for order in unplaced:
        limit = effective_limit(order)
        if order["seconds"] > limit:
            proposals.append(ForceProposal(
                order_guid=order["guid"], work_order_no=order.get("work_order_no") or "",
                reason=f"预计时长 {order['seconds']}s 超过单日容量 {limit}s，改需求或拆分"))
            continue
        start, end = movable_window(order, sched.window_start, sched.window_end)
        holders: list[str] = []
        for day in iter_days(start, end):
            holders.extend(sched.calendar(order["airport"]).occupants(day))
        unique = sorted(set(holders))
        proposals.append(ForceProposal(
            order_guid=order["guid"], work_order_no=order.get("work_order_no") or "",
            reason="规则内无可行窗口",
            protected_holders=unique))
    return proposals


# ================================================================ L8 人工错峰


def largest_remainder_split(total: int, day_counts: list[int]) -> list[int]:
    """跨月按自然日数用最大余数法分配数量（接口说明 6.1）。"""
    if not day_counts or sum(day_counts) <= 0:
        return [0] * len(day_counts)
    raw = [total * c / sum(day_counts) for c in day_counts]
    floors = [int(r) for r in raw]
    remainder = total - sum(floors)
    frac = [(raw[i] - floors[i], i) for i in range(len(day_counts))]
    frac.sort(key=lambda x: (-x[0], x[1]))
    for k in range(remainder):
        floors[frac[k % len(frac)][1]] += 1
    return floors


def _month_chunks(days: list[str]) -> list[list[str]]:
    chunks: dict[str, list[str]] = {}
    for d in days:
        chunks.setdefault(d[:7], []).append(d)
    return [chunks[k] for k in sorted(chunks)]


def week_window(day: str, month_days: list[str], window_start: str, window_end: str
                ) -> tuple[datetime, datetime]:
    """以 day 所在周的周一~周日为窗口，首尾按计划边界/月界截断。"""
    d = parse_dt(day + " 00:00:00")
    monday = d - timedelta(days=d.weekday())
    sunday = monday + timedelta(days=6)
    ws_start = parse_dt(window_start) or (d - timedelta(days=365))
    we_end = parse_dt(window_end) or (d + timedelta(days=365))
    ws = max(monday, ws_start)
    we = min(sunday, we_end)
    return ws, we


def schedule_manual(orders: list[dict], window_start: str, window_end: str) -> list[dict]:
    """人工工单按周错峰（接口说明 6.1）。

    没有班组产能数据，只做「同计划分散到不同周 + 全局周负载均衡」，
    每个工单落到一个自然周，首尾按窗口截断。
    """
    by_plan: dict[str, list[dict]] = {}
    for order in orders:
        if order["inspection"] != INSPECTION_MANUAL:
            continue
        by_plan.setdefault(order.get("plan_guid") or order["guid"], []).append(order)

    week_load: dict[str, int] = {}             # 全局：周起始日 -> 已排数量
    plan_week_load: dict[str, dict[str, int]] = {}   # 同计划：plan -> {周起始日: 数量}
    output: list[dict] = []
    for plan_guid, plan_orders in sorted(by_plan.items()):
        plane = plan_week_load.setdefault(plan_guid, {})
        start, end = movable_window(plan_orders[0], window_start, window_end)
        days = iter_days(start, end)
        months = _month_chunks(days)
        counts = largest_remainder_split(len(plan_orders), [len(m) for m in months])
        cursor = 0
        for month_days, count in zip(months, counts):
            for order in plan_orders[cursor:cursor + count]:
                best = None
                # ① 同计划周负载最少 ② 再看全局周负载 ③ 均匀目标近似：更早的周优先
                for day in month_days:
                    ws, we = week_window(day, month_days, window_start, window_end)
                    week_key = ws.strftime(DAY_FMT)
                    key = (plane.get(week_key, 0), week_load.get(week_key, 0), ws)
                    if best is None or key < best[0]:
                        best = (key, week_key, ws, we)
                _, week_key, ws, we = best
                plane[week_key] = plane.get(week_key, 0) + 1
                week_load[week_key] = week_load.get(week_key, 0) + 1
                output.append({
                    "work_order_guid": order["guid"],
                    "work_order_no": order.get("work_order_no"),
                    "plan_guid": plan_guid,
                    "month": month_days[0][:7],
                    "week_start": dt_str(ws),
                    "week_end": dt_str(we),
                })
            cursor += count
    return output


# ================================================================ 主流程


@dataclass
class ReworkResult:
    entry: str = "normal"                          # normal / insert_rework / force_insert
    window_start: str = ""
    window_end: str = ""
    new_placements: list[dict] = field(default_factory=list)
    rework_steps: list[ReworkStep] = field(default_factory=list)
    changed_old_orders: list[dict] = field(default_factory=list)
    unplaced: list[dict] = field(default_factory=list)
    force_proposals: list[ForceProposal] = field(default_factory=list)
    manual_schedule: list[dict] = field(default_factory=list)
    fixed_count: int = 0
    reworkable_count: int = 0
    warnings: list[str] = field(default_factory=list)
    # ---- 排班后的最终工单：两组 ----
    new_orders_out: list[dict] = field(default_factory=list)     # 新增（含排定 start/end）
    update_orders: list[dict] = field(default_factory=list)      # 需按 guid 修改的旧工单
    still_unplaced_old: list[dict] = field(default_factory=list)  # 此前「未能排下」、本次未动的旧单

    def to_dict(self) -> dict:
        return {
            "entry": self.entry,
            "window": {"start": self.window_start, "end": self.window_end},
            # ---- 最终工单效果：两组 ----
            "new": self.new_orders_out,                 # 新增工单（start/end 已排定；未排下为 null）
            "update": self.update_orders,               # 需按 work_order_guid 修改的旧工单
            # ---- 附注 ----
            "still_unplaced_old": self.still_unplaced_old,  # 此前未排下、本次原样对外对账
            "unplaced_new": self.unplaced,
            "force_proposals": [p.__dict__ for p in self.force_proposals],
            "rework_chain": [s.__dict__ for s in self.rework_steps],
            "warnings": self.warnings,
        }


def _priority(order: dict) -> tuple:
    """L1/L3 排序：变电优先 → 有时段优先 → 专业顺序 → one_day 升序 → 窗口天数 → 开始时间。"""
    spec = order.get("speciality") or ""
    is_sms = 0 if spec == "sms" else 1
    has_tp = 0 if (order.get("time_period") and str(order["time_period"]).strip()) else 1
    prof = {"sms": 0, "tms": 1, "dms": 2}.get(spec, 3)
    one_day = order.get("one_day")
    one_day_key = one_day if isinstance(one_day, int) else 10 ** 6
    ds = parse_dt(order.get("pre_start") or order.get("start"))
    de = parse_dt(order.get("pre_end") or order.get("end"))
    span = (de - ds).days if ds and de else 10 ** 6
    return (is_sms, has_tp, prof, one_day_key, span, dt_str(ds) if ds else "")


def rework(new_orders: list[dict], old_orders: list[dict],
           window_start: str, window_end: str,
           enforce_quota: bool = False, max_depth: int = 20) -> ReworkResult:
    """入口：按 8 层规则编排一次重排。"""
    result = ReworkResult(window_start=window_start, window_end=window_end)

    # ---- L2：旧单分类
    old_airport = [canonicalize(o) for o in old_orders if canonicalize(o)["inspection"] == INSPECTION_AIRPORT]
    manual_old = [canonicalize(o) for o in old_orders if canonicalize(o)["inspection"] == INSPECTION_MANUAL]
    cat = categorize_old(old_airport, window_start, window_end)
    result.fixed_count = len(cat.fixed)
    result.reworkable_count = len(cat.reworkable)
    result.warnings.extend(cat.warnings)

    # ---- 仍欠排的旧单：此前「未能排下」，本次不占位、不重排，原样保持（显式对账）----
    for order in cat.reworkable:
        if "未能排下" in (order.get("schedule_reason") or ""):
            result.still_unplaced_old.append({
                "work_order_guid": order["guid"],
                "work_order_no": order.get("work_order_no"),
                "work_order_status": order["status"],
            })

    # ---- 构建调度器 + 初始日历（固定 + 可重排都先占当前位）
    sched = Scheduler(window_start, window_end, enforce_quota, max_depth)
    for order in cat.fixed:
        sched.orders[order["guid"]] = order
        _occupy_existing(sched, order, result.warnings)
    for order in cat.reworkable:
        sched.orders[order["guid"]] = order
        sched.reworkable_guids.add(order["guid"])
        _occupy_existing(sched, order, result.warnings)

    # ---- 新单（机场）规整 + 排序
    new_airport = [canonicalize(o) for o in new_orders if canonicalize(o)["inspection"] == INSPECTION_AIRPORT]
    new_manual = [canonicalize(o) for o in new_orders if canonicalize(o)["inspection"] == INSPECTION_MANUAL]
    new_airport.sort(key=_priority)

    # ---- L1 判定：先固定旧安排试排（FR05）
    failed: list[dict] = []
    steps: list[ReworkStep] = []
    for order in new_airport:
        sched.orders[order["guid"]] = order
        loc = sched.find_slot(order)
        if loc is None:
            failed.append(order)
            continue
        day, frags = loc
        sched._cal_by_order(order).place(order["guid"], day, frags, order["seconds"])
        s_, e_, _ = _format_slot(day, frags)
        result.new_placements.append({
            "work_order_guid": order["guid"], "work_order_no": order.get("work_order_no"),
            "airport": order["airport"], "start": s_, "end": e_, "forced": False,
        })

    if not failed:
        result.entry = "normal"
    else:
        # ---- L4/L5：链式重排
        still_failed: list[dict] = []
        for order in failed:
            ok = _try_place(sched, order, set(), 0, steps)
            if ok is None:
                still_failed.append(order)
                continue
            slot = sched.slot_of(order["guid"])
            s_, e_, _ = _format_slot(slot["date"], slot["frags"])
            result.new_placements.append({
                "work_order_guid": order["guid"], "work_order_no": order.get("work_order_no"),
                "airport": order["airport"], "start": s_, "end": e_, "forced": False,
            })
        result.rework_steps = list(reversed(steps))  # 因果序：谁先抢占谁
        if not still_failed:
            result.entry = "insert_rework"
        else:
            result.entry = "force_insert"
            result.unplaced = [{
                "work_order_guid": o["guid"], "work_order_no": o.get("work_order_no"),
                "reason": "规则内无法排妥",
            } for o in still_failed]
            result.force_proposals = build_force_proposals(sched, still_failed)

    # ---- L7：变更集（被挪动的旧单）
    moved: dict[str, ReworkStep] = {}
    for step in steps:
        moved.setdefault(step.moved_guid, step)
    for guid, step in moved.items():
        result.changed_old_orders.append({
            "work_order_guid": guid,
            "preempted_by": step.preempted_by,
            "old_start": step.old_start, "old_end": step.old_end,
            "new_start": step.new_start, "new_end": step.new_end,
        })

    # ---- L8：人工错峰（新 + 旧人工都重排）
    manual_orders = new_manual + manual_old
    if manual_orders:
        result.manual_schedule = schedule_manual(manual_orders, window_start, window_end)

    # ---- 输出：最终工单两组 ----
    # 新增组：所有新工单（机场 + 人工），带上排定的起止时间；未排下的保持 null
    placed_map = {p["work_order_guid"]: p for p in result.new_placements}
    manual_map = {m["work_order_guid"]: m for m in result.manual_schedule}
    new_out: list[dict] = []
    for order in new_airport:
        item = dict(order.get("raw") or order)
        p = placed_map.get(order["guid"])
        item["start_date"] = p["start"] if p else None
        item["end_date"] = p["end"] if p else None
        new_out.append(item)
    for order in new_manual:
        item = dict(order.get("raw") or order)
        m = manual_map.get(order["guid"])
        item["start_date"] = m["week_start"] if m else None
        item["end_date"] = m["week_end"] if m else None
        new_out.append(item)
    result.new_orders_out = new_out

    # 修改组：需按 work_order_guid 修改的旧工单（被挪动的机场旧单 + 重排的旧人工单）
    old_manual_guids = {o["guid"] for o in manual_old}
    update_out: list[dict] = []
    for item in result.changed_old_orders:
        canonical = sched.orders.get(item["work_order_guid"])
        update_out.append({
            "work_order_guid": item["work_order_guid"],
            "work_order_no": (canonical or {}).get("work_order_no"),
            "start_date": item["new_start"],
            "end_date": item["new_end"],
        })
    for m in result.manual_schedule:
        if m["work_order_guid"] in old_manual_guids:
            update_out.append({
                "work_order_guid": m["work_order_guid"],
                "work_order_no": m.get("work_order_no"),
                "start_date": m["week_start"],
                "end_date": m["week_end"],
            })
    result.update_orders = update_out

    return result


# ================================================================ CLI


def main() -> None:
    parser = argparse.ArgumentParser(description="重排：新工单插入 + 旧工单连续重排 + 强插建议")
    parser.add_argument("--input", default=str(DEFAULT_INPUT))
    parser.add_argument("--out", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--enforce-quota", action="store_true", help="按 80%% 日配额约束排班")
    args = parser.parse_args()

    data = json.loads(Path(args.input).read_text(encoding="utf-8"))
    summary = data.get("summary", {})
    window = summary.get("window", {})
    orders = data.get("orders", [])
    new_orders = [o for o in orders if o.get("source") != "existing"]
    old_orders = [o for o in orders if o.get("source") == "existing"]

    result = rework(new_orders, old_orders,
                    window.get("start", ""), window.get("end", ""),
                    enforce_quota=args.enforce_quota)
    payload = result.to_dict()
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"入口：{result.entry}")
    print(f"新工单排入 {len(result.new_placements)} / 重排链 {len(result.rework_steps)} 步"
          f" / 挪动旧单 {len(result.changed_old_orders)} / 未排下 {len(result.unplaced)}")
    if result.force_proposals:
        print(f"强插建议 {len(result.force_proposals)} 条（不越过受保护占用）")
    if result.manual_schedule:
        print(f"人工错峰 {len(result.manual_schedule)} 张")
    if result.still_unplaced_old:
        print(f"仍欠排旧单 {len(result.still_unplaced_old)} 张（之前未能排下，本次未动）")
    if result.warnings:
        print(f"告警 {len(result.warnings)} 条")
    print(f"已写出：{out_path}")


if __name__ == "__main__":
    main()