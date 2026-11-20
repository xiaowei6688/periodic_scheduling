"""周期计划 -> 工单拆分（只拆单，不排班）。

对应 Java：WorkOrderInfoServiceImpl.addOrderByPlanNew 及其调用链（约 6146-6814 行）。
刻意 **不包含** AirportPlanScheduler：不选日期、不查机场占用、不做超量均摊。
拆单阶段唯一的时间约束就是「一张工单的工作量不得超过单日容量」，
也就是 Java pendingSupplementSlices（5093-5118）里那段贪心装箱。

输入：
    mock_data/split_input.json    自包含报文（planSetInfo + planStrategyList{deviceList, planList}
                                  + airports；承载力走内置 airport_capacity()）

输出：
    out/work_orders.json   工单主表 + 明细（不含 start_date/end_date，留给排班阶段）

用法：
    python split_work_orders.py
    python split_work_orders.py --data-dir mock_data --out out/work_orders.json
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_DATA_DIR = BASE_DIR / "mock_data"
DEFAULT_OUT = BASE_DIR / "out" / "work_orders.json"

# ---------------------------------------------------------------- 常量（对齐 Java）

# 理论日作业秒数：可见光 8h / 红外 6h（文档 §4.2 表）
DAY_SECONDS_VISIBLE = 8 * 3600
DAY_SECONDS_INFRARED = 6 * 3600
# 正常排班日配额 80%（文档 §4.2、§4.3）
QUOTA_RATIO = 0.80
# 红外作业性质子类（Java 6623-6625 isAirportInfrared）
INFRARED_SUB_NATURES = {"s-hwxs", "t-hwxs", "d-hwxs"}
# 估时保底秒数（Java 6494 整组只记 3600，其余为 0）
FALLBACK_SECONDS = 3600
# 工单状态：9=排队中；来源 5=自动生成；巡检方式 1=机场 / 2=人工
WORK_ORDER_STATUS_QUEUED = "9"
ORDER_SOURCE_AUTO = "5"
INSPECTION_AIRPORT = "1"
INSPECTION_MANUAL = "2"


def is_airport_infrared(work_nature_sub: str) -> bool:
    return (work_nature_sub or "") in INFRARED_SUB_NATURES


# 变电航迹类型（Java 6671-6674 getAirportWorkSeconds 的推断口径）
SMS_TRACK_INFRARED = "309398410241898018"
SMS_TRACK_BY_SUB = {
    "s-lhxs": "12",
    "s-trcxs": "363569449772114767",
    "s-rcxs": "11",
}


def resolve_sms_track_type(plan: dict, infrared: bool) -> Optional[str]:
    """变电航迹类型：优先计划自带，空则按红外/巡视子类回退（Java 6671-6674）。"""
    if (plan.get("trackType") or "").strip():
        return plan["trackType"].strip()
    if infrared:
        return SMS_TRACK_INFRARED
    return SMS_TRACK_BY_SUB.get(plan.get("workNatureSub") or "")


def day_seconds(infrared: bool) -> int:
    return DAY_SECONDS_INFRARED if infrared else DAY_SECONDS_VISIBLE


def budget(infrared: bool) -> int:
    """单日配额（秒）。Java: AirportPlanScheduler.budget(infrared)。"""
    return int(day_seconds(infrared) * QUOTA_RATIO)


def local_time(text: str) -> datetime:
    return datetime.strptime(text.strip(), "%H:%M:%S")


def windows_total_seconds(time_period: str) -> Optional[int]:
    """解析 '08:30:00-17:30:00;12:45:00-18:30:00' 的时段总时长（秒）。

    返回 None 表示计划没有配置作业时段（此时单日容量只受预算约束）。
    """
    if not time_period or not time_period.strip():
        return None
    total = 0
    for part in time_period.split(";"):
        part = part.strip()
        if not part:
            continue
        if "-" not in part:
            raise ValueError(f"作业时段无法解析: {part!r}（完整值 {time_period!r}）")
        start_text, end_text = part.split("-", 1)
        start, end = local_time(start_text), local_time(end_text)
        span = int((end - start).total_seconds())
        if span <= 0:
            raise ValueError(f"作业时段非法（结束不晚于开始）: {part!r}")
        total += span
    return total or None


def estimate_seconds(workload: float, capacity: float, infrared: bool) -> int:
    """预计秒数 = ceil(工作量 ÷ 理论日承载力 × 理论日作业秒数)（文档 §4.2）。"""
    if capacity is None or capacity <= 0:
        raise ValueError(f"承载力无效: {capacity!r}")
    return math.ceil(workload / capacity * day_seconds(infrared))


def normalize_voltage(voltage: str) -> str:
    """'AC110kV' -> '110'；非纯数字返回空串（Java 6627-6631）。"""
    if not voltage or not voltage.strip():
        return ""
    text = voltage.strip()
    if text.upper().startswith("AC"):
        text = text[2:]
    if text.lower().endswith("kv"):
        text = text[:-2]
    text = text.strip()
    return text if text.isdigit() else ""


def parse_airport_coverage(covering_airports: str) -> list[str]:
    """逗号分隔 -> 去空、去重、保序（Java 7279-7283）。"""
    if not covering_airports:
        return []
    seen: list[str] = []
    for raw in covering_airports.split(","):
        code = raw.strip()
        if code and code not in seen:
            seen.append(code)
    return seen


def is_body_nature(work_nature: str) -> bool:
    """本体判定：1 与 101 同义（Java 6637-6639）。"""
    return (work_nature or "") in {"1", "101"}


def airport_capacity_item(plan: dict, strategy: Optional[dict], infrared: bool) -> str:
    """作业类型匹配键（Java 6642-6663）。

    非红外分支只有三种取值：变电「融合/例行」、通道「通道」、本体「本体」。
    红外分支依赖策略塔型标签，模拟数据里不触发，这里按 Java 的默认分支返回。
    """
    if plan.get("speciality") == "sms":
        return "红外专项" if infrared else "融合/例行"
    if not infrared:
        if (plan.get("workNature") or "") == "2":
            return "通道"
        return "本体" if is_body_nature(plan.get("workNature")) else ""
    # 红外：按 towerLabels 细分（电缆终端 / 复合绝缘子 / 设备杆 / 耐张）
    labels = (strategy or {}).get("towerLabels") or ""
    if not labels.strip():
        return "耐张"
    return "耐张"


def airport_filter_passed(speciality: str, work_nature: str, filter_value, filter_td) -> bool:
    """机场过滤：tms 通道看 filterTd，其余看 filter，均须为 0（Java 7111-7116）。"""
    if speciality == "tms" and (work_nature or "") == "2":
        return str(filter_td) == "0"
    return str(filter_value) == "0"


# ---------------------------------------------------------------- 数据结构


@dataclass
class DeviceAssignment:
    """一台杆塔的分流结论（Java: DeviceAssignment）。

    first_airport 即 Java 的 DeviceAssignment.coveringAirports 字段，
    语义是「台账覆盖列表的首位机场」，也是唯一允许排班的机场。
    """

    device_guid: str
    device_type: str                     # airport / manual
    first_airport: str                   # 首位机场，空串表示无覆盖
    device_name: str
    parent_device_guid: str              # 线路 GUID
    parent_device_name: str              # 线路名称
    span_meters: Optional[float] = None
    candidate_airports: list[str] = field(default_factory=list)   # 完整覆盖列表
    raw: dict = field(default_factory=dict)                       # deviceList 原始元素，原样回传


@dataclass
class Slice:
    """一段可独立成单的设备集合（Java: AirportPlanScheduler.Slice 的拆单子集）。

    indices 指向所属 AirportOrderTask.devices 的下标；拆单阶段不产生 date。
    """

    indices: list[int]
    seconds: int
    reason: Optional[str] = None


@dataclass
class AirportTask:
    """一个「计划 × 首位机场」的作业桶（Java: AirportOrderTask）。"""

    plan: dict
    airport_guid: str
    devices: list[DeviceAssignment] = field(default_factory=list)
    seconds: list[int] = field(default_factory=list)
    slices: list[Slice] = field(default_factory=list)
    whole: bool = False                  # True = 整单不拆（变电 / 历史 / 保底）
    fallback_reason: Optional[str] = None
    capacity_hit: Optional[float] = None
    limit: Optional[int] = None          # 单张工单工作量上限 = min(日配额, 时段总长)


@dataclass
class PlanSplitResult:
    plan: dict
    strategy: dict
    orders: list[dict] = field(default_factory=list)
    details: list[dict] = field(default_factory=list)
    tasks: list["AirportTask"] = field(default_factory=list)
    manual_order: Optional[dict] = None
    manual_details: list[dict] = field(default_factory=list)
    total_devices: int = 0               # 台账取到的设备数（剔除前）
    removed_channel: list[str] = field(default_factory=list)
    unmapped: list[str] = field(default_factory=list)
    manual_devices: list[str] = field(default_factory=list)


# ---------------------------------------------------------------- 台账访问


def _is_deleted(row: dict) -> bool:
    """删除标记：只有明确为 1 才算删除。

    报文里未落库的占位值是 -1（id/createUser/isDeleted 都是 -1），
    不能当成删除，否则整批设备会被静默跳过。
    """
    value = row.get("is_deleted", row.get("isDeleted", 0))
    return str(value).strip().lower() in {"1", "true"}


# ---------------------------------------------------------------- 承载力（内置，对齐单机场承载力.xlsx / AirportPlanScheduler.capacity()）

SMS_VOLTAGES = ("35", "110", "220", "500")
TMS_BODY_THIRD = (30, 20, 15, 10)      # jc_03 三代 本体（35/110/220/500）
TMS_BODY_SECOND = (25, 15, 10, 7)      # jc_08 二代 本体
TMS_TENSION_THIRD = (12, 12, 10, 6)    # jc_03 红外耐张
TMS_TENSION_SECOND = (2, 2, 2, 1)      # jc_08 红外耐张


def airport_capacity(model: Optional[str], speciality: str, infrared: bool,
                     item: str, voltage: str) -> Optional[float]:
    """理论日承载力：内置业务表，模型 jc_03=三代 / jc_08=二代 / jc_09=三代×2。

    其余机场型号返回 None（未配置），调用方走整组保底；不再需要随报文传 capacityTable。
    """
    model = (model or "").strip()
    if model == "jc_09":
        third = airport_capacity("jc_03", speciality, infrared, item, voltage)
        return None if third is None else third * 2
    if model not in ("jc_03", "jc_08"):
        return None
    third = model == "jc_03"
    speciality = speciality or ""
    item = item or ""
    if speciality == "sms" and voltage in SMS_VOLTAGES:
        return (600 if third else 200) if infrared else (1000 if third else 800)
    if speciality == "dms" and voltage == "10":
        if infrared:
            return 30 if (item == "设备杆" and third) else None
        if item == "本体":
            return 100 if third else 60
        if item == "通道":
            return 20 if third else 18
        return None
    if speciality != "tms" or voltage not in SMS_VOLTAGES:
        return None
    voltage_index = SMS_VOLTAGES.index(voltage)
    if not infrared:
        if item == "通道":
            return 15 if third else 12
        if item == "本体":
            return (TMS_BODY_THIRD if third else TMS_BODY_SECOND)[voltage_index]
        return None
    if item == "耐张":
        return (TMS_TENSION_THIRD if third else TMS_TENSION_SECOND)[voltage_index]
    if item in ("复合绝缘子", "负荷绝缘子") and voltage_index >= 2:
        return 9 if third else 2
    if item == "电缆终端" and voltage == "110":
        return 6 if third else 1
    return None


def _index_airports(airports) -> dict:
    """airports 输入是数组 [{airportGuid,...}]；工程内部按 guid 建索引。兼容旧的 dict。"""
    if isinstance(airports, dict):
        return dict(airports)
    index: dict = {}
    for row in airports or []:
        guid = (row or {}).get("airportGuid")
        if guid:
            index[str(guid)] = row
    return index


class Ledgers:
    """拆单还需要的两块外部数据：机场台账 + 变电航点。

    机场台账随报文传（airports 数组，取 airportModel）；承载力不再传，直接查内置 airport_capacity()。
    设备本身也不在这里——设备就在 deviceList 上。
    """

    def __init__(self, airports, device_routes: Optional[list[dict]] = None):
        self.airports = _index_airports(airports)
        self.device_routes = device_routes or []

    @classmethod
    def from_payload(cls, payload: dict) -> "Ledgers":
        return cls(payload.get("airports") or [],
                   payload.get("deviceRoutes") or [])

    def route_points(self, device_guid: str, track_type: str) -> int:
        """变电整站有效航点总数（Java 6676-6681）。

        条件：设备 GUID + 变电类型 + 航迹类型匹配 + 有效 + 未删除，对 taskPoint 求和。
        """
        total = 0
        for row in self.device_routes:
            if row.get("deviceGuid") != device_guid:
                continue
            if (row.get("deviceType") or "") != "sms":
                continue
            if (row.get("trackType") or "") != track_type:
                continue
            if str(row.get("validating")) != "1":
                continue
            if _is_deleted(row):
                continue
            total += int(row.get("taskPoint") or 0)
        return total

    def airport(self, airport_guid: str) -> Optional[dict]:
        return self.airports.get(airport_guid)

    def capacity(self, model: Optional[str], speciality: str, infrared: bool,
                 item: str, voltage: str) -> Optional[float]:
        return airport_capacity(model, speciality, infrared, item, voltage)


# ---------------------------------------------------------------- 设备获取与分流


def _sort_of(tower: dict):
    """排序值：数字优先，字符串数字转 int，其余退到末尾。"""
    raw = tower.get("sort")
    try:
        return (0, int(raw))
    except (TypeError, ValueError):
        return (1, 0)


def _branch_order(text) -> tuple:
    """分支顺序：纯数字分支按数值排，其余按字符串排到数字分支之后。"""
    text = str(text or "")
    if text.isdigit():
        return (0, int(text), "")
    return (1, 0, text)


def _device_order(tower: dict) -> tuple:
    """towerBranchName 分组在前，组内按 sort 排（两者一起确定顺序）。"""
    return (_branch_order(tower.get("branch_name")),) + _sort_of(tower)


def _tower_from_device_item(item: dict, line_guid: str, index: int = 0) -> dict:
    """deviceList 单个元素 -> 内部统一行结构。缺 sort 时用数组下标保证顺序确定。"""
    guid = item.get("jobObjectDeviceGuid")
    return {
        "tower_guid": guid,
        "tower_name": item.get("jobObjectDeviceName") or guid,
        "line_guid": item.get("jobObjectGuid") or line_guid,
        "branch_name": item.get("towerBranchName") or "",
        "sort": item.get("sort", index),
        "covering_airports": item.get("coveringAirports") or "",
        "filter": item.get("filter", 0),
        "filter_td": item.get("filterTd", 0),
        "span": item.get("span"),
        "raw": item,          # 原样保留，明细直接回传
    }


def query_strategy_devices(plan: dict, strategy: dict,
                           result: PlanSplitResult) -> list[DeviceAssignment]:
    """按策略取设备并完成机场/人工分流（Java 6823-7098 queryStrategyDeviceAssignments）。

    设备来源只有一个：strategy.deviceList。报文给多少就拆多少，
    不看 isLine、不看 jobObjectNum，也不去任何台账补数据。
    """
    speciality = plan.get("speciality") or ""
    work_nature = plan.get("workNature") or ""
    line_guid = plan.get("jobObjectGuid") or ""
    job_object = plan.get("jobObject") or ""
    assignments: list[DeviceAssignment] = []

    rows: list[dict] = []
    for index, item in enumerate(strategy.get("deviceList") or []):
        tower_guid = item.get("jobObjectDeviceGuid")
        if not tower_guid:
            continue
        rows.append(_tower_from_device_item(item, line_guid, index))
    rows.sort(key=_device_order)

    for tower in rows:
        tower_guid = tower["tower_guid"]
        coverage = parse_airport_coverage(tower.get("covering_airports") or "")
        first_airport = coverage[0] if coverage else ""
        passed = airport_filter_passed(speciality, work_nature,
                                       tower.get("filter"), tower.get("filter_td"))
        device_type = "airport" if (first_airport and passed) else "manual"
        assignments.append(DeviceAssignment(
            device_guid=tower_guid,
            device_type=device_type,
            first_airport=first_airport,
            device_name=tower.get("tower_name") or tower_guid,
            parent_device_guid=line_guid,
            parent_device_name=job_object,
            span_meters=tower.get("span"),
            candidate_airports=coverage,
            raw=tower.get("raw") or {},
        ))
    result.total_devices = len(assignments)
    return assignments


def filter_plan_devices(plan: dict, assignments: list[DeviceAssignment],
                        result: PlanSplitResult) -> list[DeviceAssignment]:
    """通道无覆盖杆塔直接剔除；有原计划时间时只补未巡设备（临时§六 巡检对象与防重）。

    防重所需图片数据不在当前报文里，规则留钩子按 deviceList 现状处理。
    """
    # ① 输配电通道(workNature=2)仅处理有机场覆盖的杆塔；无覆盖不转人工
    if (plan.get("workNature") or "") == "2" and (plan.get("speciality") or "") in {"tms", "dms"}:
        kept = []
        for device in assignments:
            if not (device.first_airport or "").strip():
                result.removed_channel.append(device.device_guid)
            else:
                kept.append(device)
        assignments = kept

    # ② 计划带原计划时间时，按设备最新关联工单的去重图片数与阈值筛选未巡设备。
    #    图片数不在当前报文里，留钩子；数据到位后再补 filter_uninspected(plan, assignments)。
    has_original_time = bool(plan.get("originalPlanStartDate")) or bool(plan.get("originalPlanEndDate"))
    if has_original_time and assignments and (plan.get("speciality") or "") in {"tms", "dms"}:
        # TODO(对接工单库): assignments = filter_uninspected(plan, assignments)
        pass
    return assignments


# ---------------------------------------------------------------- 估时


def estimate_task(task: AirportTask, plan: dict, strategy: dict, ledgers: Ledgers) -> None:
    """按机场承载力给桶内每台设备估时；任一步失败则整组保底（Java 6457-6496）。"""
    infrared = is_airport_infrared(plan.get("workNatureSub") or "")
    try:
        airport = ledgers.airport(task.airport_guid)
        model = (airport or {}).get("airportModel")
        item = airport_capacity_item(plan, strategy, infrared)
        voltage = normalize_voltage(plan.get("voltageLevel") or "")
        capacity = ledgers.capacity(model, plan.get("speciality") or "", infrared, item, voltage)
        if capacity is None:
            reason = ("未匹配到有效承载力[机场型号=%s,专业=%s,红外=%s,作业类型=%s,电压=%s]"
                      % (model, plan.get("speciality"), infrared, item, voltage))
            _mark_hour_fallback(task, reason)
            return
        task.capacity_hit = capacity
        task.seconds = get_work_seconds(plan, task.devices, capacity, infrared, ledgers)
    except NotImplementedError:
        # 未实现的估时分支必须显式暴露，不能被保底吞掉造成静默降级
        raise
    except Exception as failure:                                    # noqa: BLE001
        _mark_hour_fallback(task, f"承载力/工作量计算异常: {failure}")


def _mark_hour_fallback(task: AirportTask, reason: str) -> None:
    """整组保底 1 小时：整单不拆，仅首台设备记 3600 秒（Java 6491-6496）。"""
    task.whole = True
    task.seconds = [0] * max(1, len(task.devices))
    if task.seconds:
        task.seconds[0] = FALLBACK_SECONDS
    task.fallback_reason = f"{reason}，保底1小时"
    logger.warning("保底估时 计划=%s 机场=%s 原因=%s",
                   task.plan.get("planNo"), task.airport_guid, task.fallback_reason)


def get_work_seconds(plan: dict, devices: list[DeviceAssignment], capacity: float,
                     infrared: bool, ledgers: Ledgers) -> list[int]:
    """逐设备工作量 -> 秒（Java 6666-6710 getAirportWorkSeconds）。

    本体(1/101)：每台设备计 1 个单位。
    通道(2)：按台账档距米数换算公里；档距缺失 -> 抛错 -> 整组保底。
    变电(sms)：整站有效航点求和算一次，只记在首台设备，整单不拆。
    """
    if (plan.get("speciality") or "") == "sms":
        # 变电整站：按航点总数算一次，只记在首台设备上，其余 0（Java 6669-6685）
        track_type = resolve_sms_track_type(plan, infrared)
        station_guid = plan.get("jobObjectGuid") or ""
        if not track_type or not station_guid:
            raise ValueError(f"变电航迹类型或变电站GUID缺失：trackType={track_type!r} jobObjectGuid={station_guid!r}")
        points = ledgers.route_points(station_guid, track_type)
        if points <= 0:
            # Java 此处不校验，会算出 0 秒工单；这里显式暴露航点缺失，走整组保底
            raise ValueError(f"变电有效航点为 {points}：deviceGuid={station_guid!r} trackType={track_type!r}")
        station_seconds = estimate_seconds(points, capacity, infrared)
        seconds = [0] * len(devices)
        if seconds:
            seconds[0] = station_seconds
        return seconds

    channel = (not infrared) and (plan.get("workNature") or "") == "2"
    seconds: list[int] = []
    for device in devices:
        if channel:
            span = _parse_span(device)
            if span is None or span <= 0:
                raise ValueError(f"通道台账间距缺失/无效，设备={device.device_guid}，原值={device.span_meters!r}")
            workload = span / 1000.0                             # 米 -> 公里
        else:
            workload = 1.0                                       # 本体：1 台 = 1 单位
        seconds.append(estimate_seconds(workload, capacity, infrared))
    return seconds


def _parse_span(device: DeviceAssignment) -> Optional[float]:
    """档距解析：None / 空串 / 非数字 一律返回 None（客户真实数据里就是空串）。"""
    raw = device.span_meters
    if raw is None:
        return None
    if isinstance(raw, str):
        raw = raw.strip()
        if not raw:
            return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------- 装箱（拆单核心）


def plan_limit(plan: dict) -> int:
    """单张工单工作量上限 = min(日配额, 计划时段总长)。

    拆单用它装箱；重排时新工单和已有工单都可以用同一口径算上限，
    保证「能不能一天做完」的判断一致。
    """
    infrared = is_airport_infrared(plan.get("workNatureSub") or "")
    limit = budget(infrared)
    windows = windows_total_seconds(plan.get("timePeriod") or "")
    if windows is not None:
        limit = min(limit, windows)
    return limit


def pack_slices(task: AirportTask, plan: dict) -> list[Slice]:
    """把桶内设备切成若干张工单（Java 5093-5118 pendingSupplementSlices 同口径）。

    逐台累加，累加后超 limit 即封单并开下一张；整单任务(whole)完全不拆。
    """
    limit = plan_limit(plan)
    task.limit = limit

    # 变电整单不拆（Java 6405 scheduleWhole / 6203 sms 分组）
    if task.whole or (plan.get("speciality") or "") == "sms":
        indices = list(range(len(task.devices)))
        return [Slice(indices=indices, seconds=sum(task.seconds),
                      reason=task.fallback_reason)]

    slices: list[Slice] = []
    indices: list[int] = []
    seconds = 0
    for i, item in enumerate(task.seconds):
        if indices and seconds + item > limit:
            slices.append(Slice(indices=indices, seconds=seconds, reason=task.fallback_reason))
            indices, seconds = [], 0
        indices.append(i)
        seconds += item
    if indices:
        slices.append(Slice(indices=indices, seconds=seconds, reason=task.fallback_reason))
    return slices


# ---------------------------------------------------------------- 工单组装


def extract_year(date_text: str) -> str:
    """从计划边界取年份；兼容 'YYYY-MM-DD HH:MM:SS' / ISO T / 纯日期。"""
    if not date_text or not str(date_text).strip():
        return str(datetime.now().year)
    text = str(date_text).strip().replace("/", "-").replace("T", " ")
    head = text.split(" ")[0]
    for fmt in ("%Y-%m-%d", "%Y%m%d"):
        try:
            return str(datetime.strptime(head, fmt).year)
        except ValueError:
            continue
    raise ValueError(f"无法从 {date_text!r} 解析年份")


def build_order(plan: dict, devices: list[DeviceAssignment], airport: Optional[dict],
                airport_guid: str, slice_: Slice, sequence: int,
                capacity_limit: Optional[int] = None) -> dict:
    """生成一张机场工单（Java 6729-6781 createScheduledAirportOrder）。

    只输出拆单自己算出来的字段；计划上的其余参数整块带在 `plan` 里原样回传，
    不在这一层挑选、改名或补默认值。
    """
    year = extract_year(plan.get("preStartDate") or "")
    guid = f"{plan.get('speciality')}_{year}_{uuid.uuid4().hex}"
    airport_name = (airport or {}).get("airportName") or airport_guid
    reason = slice_.reason
    return {
        # ---- 拆单结论 ----
        "work_order_guid": guid,
        "work_order_no": f"ZQ-{plan.get('planNo')}-{airport_name}-{sequence}",
        "plan_guid": plan.get("planGuid"),
        "inspection_method": INSPECTION_AIRPORT,
        "order_source": ORDER_SOURCE_AUTO,
        "work_order_status": WORK_ORDER_STATUS_QUEUED,
        "cycle_airport_guid": airport_guid,
        "parent_device_num": len(devices),      # 这张单装几台设备
        "second_num": slice_.seconds,           # 预计作业秒数
        "capacity_limit": capacity_limit,       # 单张工单工作量上限
        "schedule_status": "1" if reason else "0",
        "schedule_reason": reason,
        # ---- 执行时间：pre 边界与时段在生成时继承自计划，是工单自己的列 ----
        "pre_start_date": plan.get("preStartDate"),
        "pre_end_date": plan.get("preEndDate"),
        "time_period": plan.get("timePeriod"),
        # ---- 排班阶段回填 ----
        "start_date": None,
        "end_date": None,
        # ---- 计划参数原样回传（仅追溯，重排不依赖） ----
        "plan": plan,
    }


def build_detail(device: DeviceAssignment, order_guid: str) -> dict:
    """工单明细：设备参数原样回传。"""
    return {
        "work_order_guid": order_guid,
        "device": device.raw,
    }


def build_manual_order(plan: dict, devices: list[DeviceAssignment]) -> tuple[dict, list[dict]]:
    """人工（无人机飞手）工单：同一计划合一张（Java 7410-7453）。"""
    year = extract_year(plan.get("preStartDate") or "")
    guid = f"{plan.get('speciality')}_{year}_{uuid.uuid4().hex}"
    order = {
        "work_order_guid": guid,
        "work_order_no": f"RX-{plan.get('planNo')}-1",
        "plan_guid": plan.get("planGuid"),
        "inspection_method": INSPECTION_MANUAL,
        "order_source": ORDER_SOURCE_AUTO,
        "work_order_status": WORK_ORDER_STATUS_QUEUED,
        "cycle_airport_guid": None,
        "parent_device_num": len(devices),
        "second_num": None,                # 人工不参与机场估时
        "capacity_limit": None,            # 人工不走机场容量约束
        "schedule_status": "0",
        "schedule_reason": None,
        "pre_start_date": plan.get("preStartDate"),
        "pre_end_date": plan.get("preEndDate"),
        "time_period": plan.get("timePeriod"),
        "start_date": None,
        "end_date": None,
        "plan": plan,
    }
    details = [build_detail(device, guid) for device in devices]
    return order, details


# ---------------------------------------------------------------- 单个计划拆单


def prepare_plan(plan: dict, strategy: dict, ledgers: Ledgers,
                 result: Optional[PlanSplitResult] = None) -> PlanSplitResult:
    """一个计划 -> 机场桶（已估时、已装箱）+ 人工工单，但还不生成工单。

    拆成「准备」和「出单」两步，是为了让所有计划的机场桶能跨计划统一排序，
    与 Java 在 addOrderByPlanNew 里对 tasks 统一排序后再出单的行为一致。
    """
    result = result or PlanSplitResult(plan=plan, strategy=strategy)

    assignments = query_strategy_devices(plan, strategy, result)
    assignments = filter_plan_devices(plan, assignments, result)
    logger.info("拆单 计划=%s 设备=%s 通道剔除=%s 人工=%s",
                plan.get("planNo"), result.total_devices,
                len(result.removed_channel), len(result.manual_devices))
    if not assignments:
        return result

    # ① 按首位机场分桶；其余（无覆盖 / 过滤不通过）走人工
    groups: dict[str, list[DeviceAssignment]] = {}
    manual: list[DeviceAssignment] = []
    for device in assignments:
        if device.device_type == "airport" and (device.first_airport or "").strip():
            groups.setdefault(device.first_airport, []).append(device)
        else:
            manual.append(device)
    result.manual_devices = [d.device_guid for d in manual]

    # ② 每个机场桶：估时 -> 装箱
    for airport_guid, devices in groups.items():
        task = AirportTask(plan=plan, airport_guid=airport_guid, devices=list(devices))
        estimate_task(task, plan, strategy, ledgers)
        task.slices = pack_slices(task, plan)
        result.tasks.append(task)
        logger.info("机场桶 计划=%s 机场=%s 设备=%s 单数=%s 秒数=%s%s",
                    plan.get("planNo"), airport_guid, len(task.devices),
                    len(task.slices), task.seconds[:3],
                    f" 保底:{task.fallback_reason}" if task.fallback_reason else "")

    # ③ 人工：同计划合一张（Java 6211 在 plan 循环内立即生成）
    if manual:
        result.manual_order, result.manual_details = build_manual_order(plan, manual)

    return result


def airport_profession_priority(speciality: str) -> int:
    return {"sms": 0, "tms": 1, "dms": 2}.get(speciality or "", 3)


def task_sort_key(task: AirportTask) -> tuple:
    """机场桶排序（Java 6259-6266）。

    变电优先 -> 有作业时段优先 -> 专业顺序 -> one_day 升序 -> 预开始时间 -> id -> GUID。
    """
    plan = task.plan
    one_day = plan.get("oneDay")
    plan_id = plan.get("id")
    return (
        0 if (plan.get("speciality") or "") == "sms" else 1,
        1 if not (plan.get("timePeriod") or "").strip() else 0,
        airport_profession_priority(plan.get("speciality") or ""),
        one_day if isinstance(one_day, int) else 10 ** 6,
        plan.get("preStartDate") or "",
        plan_id if isinstance(plan_id, int) and plan_id >= 0 else 0,
        plan.get("planGuid") or "",
    )


def emit_orders(results: list[PlanSplitResult], ledgers: Ledgers
                ) -> tuple[list[dict], list[dict]]:
    """按 Java 顺序出单：先所有人工工单，再排序后的机场工单。"""
    orders: list[dict] = []
    details: list[dict] = []

    # 人工工单在 plan 循环里就生成了，天然排在机场工单之前
    for result in results:
        if result.manual_order is None:
            continue
        orders.append(result.manual_order)
        details.extend(result.manual_details)
        result.orders.append(result.manual_order)
        result.details.extend(result.manual_details)

    pairs = [(result, task) for result in results for task in result.tasks]
    pairs.sort(key=lambda pair: task_sort_key(pair[1]))

    sequence = len(orders) + 1                     # Java: orderInfoList.size() + 1
    for result, task in pairs:
        airport = ledgers.airport(task.airport_guid)
        for slice_ in task.slices:
            selected = [task.devices[i] for i in slice_.indices]
            order = build_order(result.plan, selected, airport, task.airport_guid,
                                slice_, sequence, task.limit)
            sequence += 1
            order_details = [build_detail(d, order["work_order_guid"]) for d in selected]
            orders.append(order)
            details.extend(order_details)
            result.orders.append(order)
            result.details.extend(order_details)

    logger.info("出单完成 工单=%s 明细=%s", len(orders), len(details))
    return orders, details


# ---------------------------------------------------------------- 主流程


def split_all(payload: dict, ledgers: Optional[Ledgers] = None
              ) -> tuple[list[dict], list[dict], list[PlanSplitResult]]:
    """拆单入口。只吃一个自包含报文，台账/机场/承载力都在里面。"""
    ledgers = ledgers or Ledgers.from_payload(payload)
    strategies = {s["planStrategyGuid"]: s for s in payload.get("planStrategyList") or []}
    logger.info("拆单开始 计划包=%s 机场=%s", len(strategies), len(ledgers.airports))
    results: list[PlanSplitResult] = []
    for plan in iterate_plans(payload):
        strategy = strategies.get(plan.get("planStrategyGuid") or "")
        if strategy is None:
            continue
        results.append(prepare_plan(plan, strategy, ledgers))
    orders, details = emit_orders(results, ledgers)
    return orders, details, results


def iterate_plans(payload: dict) -> Iterable[dict]:
    for strategy in payload.get("planStrategyList") or []:
        for plan in strategy.get("planList") or []:
            yield plan


def _device_guid_of(detail: dict) -> str:
    """明细里的设备 GUID —— 明细原样保存 deviceList 元素，这里只读它的 GUID。"""
    return (detail.get("device") or {}).get("jobObjectDeviceGuid") or ""


def verify(results: list[PlanSplitResult]) -> list[str]:
    """守恒校验：机场工单设备 + 人工工单设备 + 剔除设备 = 台账设备全集。"""
    problems: list[str] = []
    for result in results:
        plan_no = result.plan.get("planNo")
        airport_devices = sum(o["parent_device_num"] for o in result.orders
                              if o["inspection_method"] == INSPECTION_AIRPORT)
        manual_devices = sum(o["parent_device_num"] for o in result.orders
                             if o["inspection_method"] == INSPECTION_MANUAL)
        detail_devices = len(result.details)
        expected = result.total_devices - len(result.removed_channel)
        total = airport_devices + manual_devices
        if total != expected:
            problems.append(f"{plan_no}: 工单设备 {total} != 应处理设备 {expected}")
        if detail_devices != total:
            problems.append(f"{plan_no}: 明细 {detail_devices} != 工单设备 {total}")
        seen: set[str] = set()
        for detail in result.details:
            device_guid = _device_guid_of(detail)
            if device_guid in seen:
                problems.append(f"{plan_no}: 设备 {device_guid} 重复出现在多张工单")
            seen.add(device_guid)
        # 被判删除的设备必须显式记账，不能既不出现在工单里也不知去向
        if result.unmapped:
            lost = [g for g in result.unmapped if g in seen]
            if lost:
                problems.append(f"{plan_no}: 已跳过设备同时出现在工单里: {lost}")
    return problems


def summarize(results: list[PlanSplitResult]) -> str:
    lines = []
    for result in results:
        plan = result.plan
        airport_orders = [o for o in result.orders if o["inspection_method"] == INSPECTION_AIRPORT]
        manual_orders = [o for o in result.orders if o["inspection_method"] == INSPECTION_MANUAL]
        lines.append(f"计划 {plan.get('planNo')}  窗口 {plan.get('preStartDate')} ~ {plan.get('preEndDate')}")
        lines.append(f"  台账设备 {result.total_devices} / 通道剔除 {len(result.removed_channel)}"
                     f" / 转人工 {len(result.manual_devices)}")
        for order in airport_orders:
            reason = f"  [{order['schedule_reason']}]" if order["schedule_reason"] else ""
            lines.append(f"  - {order['work_order_no']:<48} 机场={order['cycle_airport_guid']:<8}"
                         f" 设备={order['parent_device_num']:>2}  预计={order['second_num']:>6}s"
                         f" 单日容量上限={order['capacity_limit']}{reason}")
        for order in manual_orders:
            lines.append(f"  - {order['work_order_no']:<48} 人工  设备={order['parent_device_num']:>2}")
    return "\n".join(lines)


def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s - %(message)s")
    parser = argparse.ArgumentParser(description="周期计划 -> 工单拆分")
    parser.add_argument("--data-dir", default=str(DEFAULT_DATA_DIR))
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    args = parser.parse_args()

    data_dir = Path(args.data_dir)
    payload = json.loads((data_dir / "split_input.json").read_text(encoding="utf-8"))
    orders, details, results = split_all(payload)

    problems = verify(results)
    print(summarize(results))
    print()
    print(f"合计：计划 {len(results)} 条，工单 {len(orders)} 张，明细 {len(details)} 条")
    print(f"  机场工单 {sum(1 for o in orders if o['inspection_method'] == INSPECTION_AIRPORT)} 张"
          f" / 人工工单 {sum(1 for o in orders if o['inspection_method'] == INSPECTION_MANUAL)} 张")
    print(f"  保底估时 {sum(1 for o in orders if o['schedule_reason'] and '保底' in o['schedule_reason'])} 张")
    print("守恒校验：" + ("通过" if not problems else "失败"))
    for problem in problems:
        print("  - " + problem)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps({"work_orders": orders, "work_order_details": details},
                   ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"已写出：{out_path}")

    if problems:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
