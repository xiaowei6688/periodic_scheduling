"""重排模块：拉取时间段内已排好的旧工单，和新拆出来的工单合并成待排集合。

职责边界
--------
- 只负责「拿到旧工单 → 过滤 → 转成统一结构 → 和新工单合并」
- 不负责重排算法本身（谁排到哪天，是排班器的事）
- 不落库

数据来源
--------
POST {BASE_URL}/api/main-cycle-server/workOrderInfo/getWorkOrderPage
    只需要 startTime / endTime 两个参数，范围由新拆工单的最早 ~ 最晚日期决定。

过滤规则
--------
- workOrderStatus = "11" 已作废   → 丢弃（Java 参与占用的状态是 8/9/12/16）
- workOrderStatus = "12" 延期     → 暂时丢弃（接口未返回周期时间窗口，无法重排）
- 其余（8/9/16）                  → 保留

调试完整流程请从项目根目录运行 python main.py。
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Optional

from .split_work_orders import (
    INSPECTION_AIRPORT,
    split_all,
    verify,
)

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parents[3]
DEFAULT_INPUT = BASE_DIR / "mock_data" / "split_input.json"
DEFAULT_OUT = BASE_DIR / "out" / "rework_set.json"
DEFAULT_EXISTING = BASE_DIR / "out" / "existing_orders.json"

# ---- 接口配置（可用环境变量覆盖）----
API_BASE = os.environ.get("MAIN_CYCLE_BASE", "http://10.10.2.101:8000")
API_PATH = "/api/main-cycle-server/workOrderInfo/getCycleOrderProjectPage"
# 网关鉴权需要两个头一起发：
#   authorization: Basic <client_id:client_secret>   —— 客户端凭证
#   zxhc-auth:     bearer <access_token>             —— 用户令牌（本产品的自定义头名）
# 可用环境变量覆盖，例如换 token 只改 MAIN_CYCLE_TOKEN
AUTH_BASIC = os.environ.get("MAIN_CYCLE_BASIC", "Basic c2FiZXI6c2FiZXJfc2VjcmV0")
AUTH_HEADER = os.environ.get("MAIN_CYCLE_AUTH_HEADER", "zxhc-auth")
TOKEN = os.environ.get(
    "MAIN_CYCLE_TOKEN",
    "bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJkZXB0X2NvZGUiOiIxMDAiLCJ0ZW5hbnRfaWQiOiIwMDAwMDAiLCJ1c2VyX25hbWUiOiJaTllKIiwicmVhbF9uYW1lIjoi566hKuWRmCIsImNsaWVudF9pZCI6InNhYmVyIiwibWFqb3IiOiJxenkiLCJyb2xlX2lkIjoiMTEyMzU5ODgxNjczODY3NTIwMSwxNzY1NTY4NzU1OTY1OTM1NjE3LDE3NjU1NjkwMjA0Mzc3NzQzMzgsMTc2NzgwNDA0NTk1NDY5NTE2OSwxNzY3Nzg4MzgyNjYxOTUxNDkwLDE3Njc3ODc4Mzc4ODQ3NzY0NDksMTc5NjEzNTYzNzQ2Njc5MTkzNywxODkzMjA5NDcwMDIyMTU2MjkwLDE5MjgzODc3MTUyMTg3MzkyMDEsMjAwMDc1NTY3Nzg2MjI2NDgzMywyMDAzMzg0NTg2MTE0MTM4MTEzIiwicG9zdF9uYW1lIjoi6K645Y-v5Lq6LOetvuWPkeS6uizlpJbkuJoiLCJzY29wZSI6WyJhbGwiXSwib2F1dGhfaWQiOiIiLCJleHAiOjE3OTYwMTUwMDIsImp0aSI6IjRlZmZjNTYwLThmOGEtNDIxYy1iM2UzLWRhODM2Y2JmNzRhZCIsImRlcHRfbmFtZSI6Iua1i-ivleWbvSIsImF2YXRhciI6Imh0dHBzOi8vZ3cuYWxpcGF5b2JqZWN0cy5jb20vem9zL3Jtc3BvcnRhbC9CaWF6ZmFueG1hbU5Sb3h4VnhrYS5wbmciLCJtYWpvcl9uYW1lIjoi5YWo5LiT5LiaIiwiYXV0aG9yaXRpZXMiOlsiYWRtaW5pc3RyYXRvciIsInVhdl9jb3JlX3dvcmtpbmdfcHJvZmVzc2lvbmFsIiwidWF2X2NvcmVfdGFza19wcm9mZXNzaW9uYWwiLCJ1YXZfY29yZV90ZWFtX21lbWJlcnMiLCJkb3duX3BpYyIsImhvbWV3b3JrX2NsYXNzX2xlYWRlciIsInVhdl9jb3JlX3Rhc2tfcGVyc29uIiwiZXF1aXBfdGVhbV9tZW1iZXIiLCJ1YXZfY29yZV9kaXJlY3RvciIsInRtc19idXNfZnVsbF90aW1lIl0sInJvbGVfbmFtZSI6ImFkbWluaXN0cmF0b3IsdWF2X2NvcmVfd29ya2luZ19wcm9mZXNzaW9uYWwsdWF2X2NvcmVfdGVhbV9tZW1iZXJzLHVhdl9jb3JlX2RpcmVjdG9yLHVhdl9jb3JlX3Rhc2tfcHJvZmVzc2lvbmFsLHVhdl9jb3JlX3Rhc2tfcGVyc29uLGhvbWV3b3JrX2NsYXNzX2xlYWRlcix0bXNfYnVzX2Z1bGxfdGltZSxkb3duX3BpYyxlcXVpcF90ZWFtX21lbWJlciIsImxpY2Vuc2UiOiJwb3dlcmVkIGJ5IHp4aGMiLCJwb3N0X2lkIjoiMTEyMzU5ODgxNzczODY3NTIwMiwxMTIzNTk4ODE3NzM4Njc1MjAzLDE3ODgwMjk3OTQ4NjIwNzU5MDYiLCJ1c2VyX2lkIjoiMTEyMzU5ODgyMTczODY3NTIwMSIsInF1ZXJ5X2RlcHRfY29kZSI6IjEwMCIsInBob25lIjoiMTIzMzMzMzMzMzMzMDAiLCJuaWNrX25hbWUiOiLnrqHnkIblkZgiLCJwb3N0X2NvZGUiOiJsaWNlbnNvcixpc3N1ZXIsb3BlcmF0aXZlcyIsImRlcHRfY2F0ZWdvcnkiOjEsImRldGFpbCI6eyJ0eXBlIjoid2ViIn0sImRlcHRfaWQiOiIxNTU2OTg0ODg2NzAzNzIyNDk3IiwiYWNjb3VudCI6IlpOWUoifQ.3oxSMddBqcRuSFe2xSKaHYQi3jsHpkQiv5kkaWJm6jY",
)

# ---- 状态码 ----
STATUS_CANCELLED = "11"     # 已作废
STATUS_DELAYED = "12"       # 延期
STATUS_REWORKABLE = {"8", "9", "16"}

DATE_FMT = "%Y-%m-%d %H:%M:%S"


# ---------------------------------------------------------------- 接口访问


class ApiError(RuntimeError):
    pass


def fetch_existing_orders(start_time: str, end_time: str, *, page_size: int = 100,
                          max_pages: int = 500) -> list[dict]:
    """按时间段拉旧工单，自动翻页。只传 startTime / endTime 即可。"""
    url = API_BASE.rstrip("/") + API_PATH
    collected: list[dict] = []
    for page in range(1, max_pages + 1):
        body = {
            "startTime": start_time,
            "endTime": end_time,
            "inspectionMethod": 1,
            "current": page,
            "size": page_size,
            "extData": {},
        }
        payload = _post_json(url, body)
        rows, has_next = _extract_rows(payload)
        collected.extend(rows)
        logger.info("拉取旧工单 第%s页 rows=%s 累计=%s", page, len(rows), len(collected))
        if not has_next or not rows:
            break
    return collected


def _post_json(url: str, body: dict) -> dict:
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST", headers={
        "Content-Type": "application/json",
        "authorization": AUTH_BASIC,
        AUTH_HEADER: TOKEN,
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:300]
        raise ApiError(f"HTTP {exc.code} {url} -> {detail}") from exc
    except urllib.error.URLError as exc:
        raise ApiError(f"连接失败 {url}: {exc.reason}") from exc


def _extract_rows(payload: dict) -> tuple[list[dict], bool]:
    """兼容 BladeX 分页结构：data.records / data.rows / data(list)。"""
    code = payload.get("code")
    if code not in (None, 200, "200"):
        raise ApiError(f"接口返回失败 code={code} msg={payload.get('msg')!r}")
    data = payload.get("data")
    if data is None:
        return [], False
    if isinstance(data, list):
        return data, False
    for key in ("records", "rows", "list"):
        rows = data.get(key)
        if isinstance(rows, list):
            current = data.get("current") or data.get("page") or 1
            pages = data.get("pages") or data.get("totalPages")
            has_next = bool(pages) and int(current) < int(pages)
            return rows, has_next
    return [], False


# ---------------------------------------------------------------- 过滤与归一化


@dataclass
class ExistingFilterStats:
    total: int = 0
    cancelled: int = 0
    delayed: int = 0
    kept: int = 0
    dropped: list[dict] = field(default_factory=list)


def filter_reworkable(rows: Iterable[dict]) -> tuple[list[dict], ExistingFilterStats]:
    """丢弃已作废(11)和延期(12)的工单。

    作废：Java 参与占用的状态只有 8/9/12/16，11 无效
    延期：接口未返回周期时间窗口，暂时无法重排
    """
    stats = ExistingFilterStats()
    kept: list[dict] = []
    for row in rows:
        stats.total += 1
        status = str(row.get("workOrderStatus") or "").strip()
        if status == STATUS_CANCELLED:
            stats.cancelled += 1
            stats.dropped.append({"workOrderGuid": row.get("workOrderGuid"),
                                  "workOrderNo": row.get("workOrderNo"),
                                  "reason": "已作废"})
            continue
        if status == STATUS_DELAYED:
            stats.delayed += 1
            stats.dropped.append({"workOrderGuid": row.get("workOrderGuid"),
                                  "workOrderNo": row.get("workOrderNo"),
                                  "reason": "延期（缺周期时间窗口）"})
            continue
        if status not in STATUS_REWORKABLE:
            stats.dropped.append({"workOrderGuid": row.get("workOrderGuid"),
                                  "workOrderNo": row.get("workOrderNo"),
                                  "reason": f"非重排状态 {status}"})
            continue
        kept.append(row)
    stats.kept = len(kept)
    return kept, stats


def normalize_existing(raw: dict) -> dict:
    """业务工单 DTO → 待排集合统一结构。

    只取重排用得到的字段，其余原样带走放在 raw 里。
    """
    start = _parse_dt(raw.get("startDate"))
    end = _parse_dt(raw.get("endDate"))
    return {
        # 身份与位置
        "work_order_guid": raw.get("workOrderGuid"),
        "work_order_no": raw.get("workOrderNo"),
        "plan_guid": raw.get("planGuid"),
        "inspection_method": str(raw.get("inspectionMethod") or INSPECTION_AIRPORT),
        "cycle_airport_guid": raw.get("cycleAirportGuid"),
        # 时长与占用
        "second_num": raw.get("secondNum"),
        "start_date": start,
        "end_date": end,
        # 可调整窗口：接口补齐 preStartDate/preEndDate 后自动生效；
        # 未提供时保持 null，重排以 start_date / end_date 作为现状基准
        "pre_start_date": _parse_dt(raw.get("preStartDate")),
        "pre_end_date": _parse_dt(raw.get("preEndDate")),
        # 原计划作业时段：接口提供 timePeriod 时用之，否则重排走默认窗口
        "time_period": raw.get("timePeriod"),
        "capacity_limit": None,     # 由 merge 阶段按 airportModel 等算
        # 拆单侧没有的字段，供重排参考
        "source": "existing",
        "work_order_status": str(raw.get("workOrderStatus") or ""),
        "airport_model": raw.get("airportModel"),
        "professional_category": raw.get("professionalCategory"),
        "work_nature": raw.get("workNature"),
        "work_nature_sub": raw.get("workNatureSub"),
        "voltage_level": raw.get("cycleVoltageLevel") or raw.get("voltageLevel"),
        "one_day": raw.get("oneDay"),
        "raw": raw,
    }


def _parse_dt(value: Any) -> Optional[str]:
    """统一成 YYYY-MM-DD HH:MM:SS；空值返回 None。"""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    text = text.replace("T", " ")
    for fmt in (DATE_FMT, "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).strftime(DATE_FMT)
        except ValueError:
            continue
    return None


# ---------------------------------------------------------------- 合并


@dataclass
class ReworkSet:
    new_orders: list[dict] = field(default_factory=list)
    old_orders: list[dict] = field(default_factory=list)
    details: list[dict] = field(default_factory=list)
    window: dict = field(default_factory=dict)
    filter_stats: Optional[ExistingFilterStats] = None

    @property
    def all_orders(self) -> list[dict]:
        return [*self.old_orders, *self.new_orders]

    def summary(self) -> dict:
        return {
            "window": self.window,
            "new_orders": len(self.new_orders),
            "old_orders": len(self.old_orders),
            "total": len(self.all_orders),
            "existing_filtered": self.filter_stats.__dict__ if self.filter_stats else None,
        }


def order_date_span(orders: Iterable[dict]) -> tuple[Optional[str], Optional[str]]:
    """取工单的最早 pre_start / 最晚 pre_end，作为拉旧工单的时间范围。"""
    starts = [o["pre_start_date"] for o in orders if o.get("pre_start_date")]
    ends = [o["pre_end_date"] for o in orders if o.get("pre_end_date")]
    lo = min(starts) if starts else None
    hi = max(ends) if ends else None
    return lo, hi


def build_rework_set(new_orders: list[dict], existing_rows: list[dict],
                     window_start: str, window_end: str,
                     *, details: Optional[list[dict]] = None) -> ReworkSet:
    """把新工单和过滤后的旧工单合成待排集合。

    两种工单的约束来源不同，不能互相套：
      - 新工单：有 pre_start_date / pre_end_date（继承自计划，必须排在这个窗口内）
      - 旧工单：没有 pre 窗口，重排基准就是它自己的 start_date / end_date；
                本次查询时间段只作为重排的整体范围记在 ReworkSet.window，
                不下发到工单字段上，避免伪造一个数据里并不存在的约束。
    """
    kept, stats = filter_reworkable(existing_rows)
    old_orders = [normalize_existing(row) for row in kept]
    return ReworkSet(new_orders=list(new_orders), old_orders=old_orders,
                     details=list(details or []),
                     window={"start": window_start, "end": window_end},
                     filter_stats=stats)


def collect_rework_set(payload: dict, existing_rows: Optional[list[dict]] = None,
                       *, save_existing: Optional[str] = None) -> ReworkSet:
    """拆单 → 定窗口 → 取旧工单 → 过滤归一化，合并成待排集合。

    existing_rows=None 时按新工单窗口在线调接口拉取；
    save_existing 非空时把拉到的旧工单原样存到该文件。
    """
    orders, details, results = split_all(payload)
    problems = verify(results)
    if problems:
        raise ValueError("拆单守恒校验失败：" + "；".join(problems))
    window_start, window_end = order_date_span(orders)
    logger.info("拆单完成 工单=%s 明细=%s 窗口=%s ~ %s",
                len(orders), len(details), window_start, window_end)
    if not window_start or not window_end:
        raise ValueError("新工单缺少 pre 窗口，无法确定拉取范围")
    if existing_rows is None:
        existing_rows = fetch_existing_orders(window_start, window_end)
    elif save_existing:
        save_existing = None
    if save_existing:
        path = Path(save_existing)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(existing_rows, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    rework_set = build_rework_set(orders, existing_rows, window_start, window_end,
                                  details=details)
    stats = rework_set.filter_stats
    logger.info("旧工单 原始=%s 保留=%s（作废=%s 延期=%s 其他=%s）",
                stats.total, stats.kept, stats.cancelled, stats.delayed,
                len(stats.dropped) - stats.cancelled - stats.delayed)
    return rework_set


# ---------------------------------------------------------------- 主流程


def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s - %(message)s")
    parser = argparse.ArgumentParser(description="拉取旧工单并与新拆工单合并（不重排、不落库）")
    parser.add_argument("--input", default=str(DEFAULT_INPUT), help="拆单输入报文")
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="合并结果输出")
    parser.add_argument("--existing-file", help="离线模式：直接读本地旧工单 JSON")
    parser.add_argument("--save-existing", help="把拉到的旧工单原样存到该文件")
    parser.add_argument("--normalize-only", action="store_true", help="只处理旧工单，不拆单")
    args = parser.parse_args()

    if args.normalize_only:
        path = args.existing_file or str(DEFAULT_EXISTING)
        rows = json.loads(Path(path).read_text(encoding="utf-8"))
        kept, stats = filter_reworkable(rows)
        print(f"旧工单 {stats.total} 条 -> 保留 {stats.kept} / 作废 {stats.cancelled} / 延期 {stats.delayed}")
        for item in stats.dropped[:20]:
            print(f"  丢弃 {item['workOrderNo']}  {item['reason']}")
        print(json.dumps([normalize_existing(r) for r in kept][:3], ensure_ascii=False, indent=2))
        return

    # 1) 拆单 + 定范围 + 取旧工单 + 过滤合并
    payload = json.loads(Path(args.input).read_text(encoding="utf-8"))
    existing_rows = None
    if args.existing_file:
        existing_rows = json.loads(Path(args.existing_file).read_text(encoding="utf-8"))
        print(f"离线读取旧工单：{len(existing_rows)} 条（{args.existing_file}）")
    try:
        rework = collect_rework_set(
            payload, existing_rows,
            save_existing=args.save_existing if existing_rows is None else None)
    except ApiError as exc:
        print(f"接口获取失败：{exc}")
        print("可用 --existing-file 离线模式，或检查 MAIN_CYCLE_TOKEN / MAIN_CYCLE_AUTH_HEADER")
        raise SystemExit(2)
    except ValueError as exc:
        print(str(exc))
        raise SystemExit(1)

    # 2) 汇总
    stats = rework.filter_stats
    print(f"拆单：{len(rework.new_orders)} 张工单 / {len(rework.details)} 条明细，守恒通过")
    print(f"重排范围：{rework.window['start']} ~ {rework.window['end']}")
    if args.save_existing:
        print(f"原始响应已存：{args.save_existing}")
    print(f"旧工单过滤：总计 {stats.total} -> 保留 {stats.kept}"
          f"（作废 {stats.cancelled} / 延期 {stats.delayed} / 其他 {len(stats.dropped) - stats.cancelled - stats.delayed}）")

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps({
        "summary": rework.summary(),
        "orders": rework.all_orders,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"待排集合：{len(rework.all_orders)} 张（新 {len(rework.new_orders)} + 旧 {len(rework.old_orders)}）")
    print(f"已写出：{out_path}")


if __name__ == "__main__":
    main()
