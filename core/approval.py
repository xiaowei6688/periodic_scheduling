"""工具级人工审批：声明、策略、存储扩展点与工具包装器。

企业级安全动作的三段式：先校验动作参数，再交给人工审批，审批通过后才执行。

执行顺序（run / arun 内）：
1. 参数校验（ApprovalSpec.required_fields + 自定义 validate）
   —— 不通过时直接向模型返回错误信息，不触发审批、不执行工具；
2. 人工审批（policy + interrupt）
   —— 校验通过后才生成审批请求；
3. 执行工具。
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import os
import sqlite3
import threading
from typing import Any, Literal, Protocol, cast

from langchain_core.tools import BaseTool, StructuredTool
from langgraph.types import interrupt
from pydantic import BaseModel, Field

from ._sqlite import connect_sqlite, enable_wal

# 参数校验器：返回非 None 表示拦截（返回错误信息），返回 None 表示通过。
Validator = Callable[[dict[str, Any]], str | None]

# 常见占位值，提交给人审批前必须拦下。
_PLACEHOLDER_TOKENS = ("待补充", "待定", "待填", "待确认", "TBD", "TODO", "N/A")

# 人工审批驳回后，回填给模型的结构化提示前缀。框架据此识别“该动作未成功执行”，
# 从而触发 required_actions 的自纠正机制，把审批意见回灌给原 Agent 重新修正。
REJECTED_PREFIX = "工具调用已被人工拒绝"


def rejection_reason(content: Any) -> str | None:
    """若内容是“人工审批驳回”的回填文本，返回审批意见原文；否则返回 None。"""
    text = content if isinstance(content, str) else str(content or "")
    if not text.startswith(REJECTED_PREFIX):
        return None
    rest = text[len(REJECTED_PREFIX):]
    marker = "，原因："
    if marker not in rest:
        return ""
    comment = rest.split(marker, 1)[1]
    end = comment.find("。请根据审批意见")
    if end != -1:
        comment = comment[:end]
    return comment


def is_rejected_output(content: Any) -> bool:
    """判断某 ToolMessage 是否来自“人工审批驳回”——驳回并不算执行成功。"""
    return rejection_reason(content) is not None


def _reject_text(comment: str) -> str:
    reason = f"，原因：{comment}" if comment.strip() else ""
    return (
        f"{REJECTED_PREFIX}{reason}。"
        "请根据审批意见修正后重新调用本工具；审批通过前不要声称动作已完成；"
        "若无需继续，请向用户说明驳回原因。"
    )


def required_fields(*fields: str) -> Validator:
    """生成必填校验器：任一字段缺失或为空即返回错误。"""

    def validate(arguments: dict[str, Any]) -> str | None:
        missing = [f for f in fields if not str(arguments.get(f, "")).strip()]
        if missing:
            return (
                "参数校验失败：缺少必填项 "
                + "、".join(missing)
                + "。请先向用户收集真实信息，补齐后再调用，不要使用占位值。"
            )
        return None

    return validate


def reject_placeholders(*fields: str) -> Validator:
    """生成占位值校验器：字段含“待补充/待定/TBD”等占位词即返回错误。"""

    def validate(arguments: dict[str, Any]) -> str | None:
        bad = [
            f
            for f in fields
            if any(token in str(arguments.get(f, "")).upper() for token in _PLACEHOLDER_TOKENS)
        ]
        if bad:
            return (
                "参数校验失败：字段 "
                + "、".join(bad)
                + " 不能是占位值（如“待补充/待定/TBD”）。请先向用户确认具体事实后再调用。"
            )
        return None

    return validate


def chain_validators(*validators: Validator | None) -> Validator:
    """按顺序组合多个校验器，返回第一个错误；全部通过返回 None。"""
    active = [v for v in validators if v is not None]

    def validate(arguments: dict[str, Any]) -> str | None:
        for validator in active:
            error = validator(arguments)
            if error is not None:
                return error
        return None

    return validate


class ApprovalDecision(BaseModel):
    """人工对单次工具调用的决定。"""

    action: Literal["approve", "reject"]
    comment: str = ""
    values: dict[str, Any] = Field(default_factory=dict)


class ApprovalOption(BaseModel):
    """单个动态审批选项。"""

    value: str
    label: str
    description: str = ""


class ApprovalField(BaseModel):
    """动态审批表单字段。"""

    name: str
    label: str
    type: Literal["radio", "select", "multi_select", "text"]
    options: list[ApprovalOption] = Field(default_factory=list)
    required: bool = False
    placeholder: str = ""


class ApprovalRequest(BaseModel):
    """展示给审批人的工具调用信息。"""

    type: Literal["tool_approval"] = "tool_approval"
    tool_name: str
    description: str
    arguments: dict[str, Any]
    display_title: str = ""
    display_arguments: dict[str, Any] | None = None
    fields: list[ApprovalField] = Field(default_factory=list)


class PendingApproval(BaseModel):
    """带 LangGraph interrupt ID 的待审批请求。"""

    approval_id: str
    request: ApprovalRequest
    agent: str = ""


class ApprovalRecord(BaseModel):
    """一条可查询、可审计的审批流水。"""

    approval_id: str
    thread_id: str
    tool_name: str
    description: str
    arguments: dict[str, Any]
    status: Literal["pending", "processing", "approved", "rejected"]
    comment: str = ""
    values: dict[str, Any] = Field(default_factory=dict)
    decided_by: str = ""
    created_at: datetime | None = None
    decided_at: datetime | None = None


@dataclass(frozen=True)
class ApprovalSpec:
    """工具的审批声明 + 参数校验声明。

    - description：展示给审批人的动作说明；
    - required_fields：执行前必须非空的参数名；
    - validate：自定义校验器，返回错误信息表示拦截。
    """

    description: str = ""
    required_fields: tuple[str, ...] = ()
    validate: Validator | None = None
    display_title: str = ""
    display_arguments: Callable[[dict[str, Any]], dict[str, Any]] | None = None
    fields: Callable[[dict[str, Any]], list[ApprovalField]] | None = None


class ApprovalPolicy(Protocol):
    """决定某次工具调用是否需要审批。"""

    def requires_approval(self, request: ApprovalRequest) -> bool: ...


def _validate_decision(
    decision: ApprovalDecision, request: ApprovalRequest
) -> str | None:
    """Validate dynamic form values before allowing the approved tool to run."""
    if decision.action == "reject":
        return None
    fields = {field.name: field for field in request.fields}
    unknown = set(decision.values) - set(fields)
    if unknown:
        return f"审批结果包含未知字段：{'、'.join(sorted(unknown))}"
    for field in request.fields:
        value = decision.values.get(field.name)
        empty = value is None or value == "" or value == []
        if field.required and empty:
            return f"审批结果缺少必填项：{field.label}"
        if empty:
            continue
        allowed = {option.value for option in field.options}
        if field.type in ("radio", "select") and str(value) not in allowed:
            return f"审批选项无效：{field.label}"
        if field.type == "multi_select":
            if not isinstance(value, list) or any(str(item) not in allowed for item in value):
                return f"审批选项无效：{field.label}"
        if field.type == "text" and not isinstance(value, str):
            return f"审批文本无效：{field.label}"
    return None


class AlwaysRequireApproval:
    def requires_approval(self, request: ApprovalRequest) -> bool:
        return True


def is_approval_policy(obj: Any) -> bool:
    """是否为合法的审批策略对象（实现了 requires_approval）。"""
    return obj is not None and callable(getattr(obj, "requires_approval", None))


class ApprovalStore(Protocol):
    """审批审计扩展点；暂停状态本身由 LangGraph checkpointer 保存。"""

    def requested(self, approval_id: str, request: ApprovalRequest, thread_id: str) -> None:
        """幂等记录待审批请求。"""
        ...

    def decided(
        self,
        approval_id: str,
        decision: ApprovalDecision,
        thread_id: str,
    ) -> None: ...


class NullApprovalStore:
    def __init__(self) -> None:
        self._claimed: set[tuple[str, str]] = set()
        self._lock = threading.Lock()

    def requested(self, approval_id: str, request: ApprovalRequest, thread_id: str) -> None:
        pass

    def claim(self, approval_id: str, thread_id: str, actor: str = "") -> bool:
        key = (thread_id, approval_id)
        with self._lock:
            if key in self._claimed:
                return False
            self._claimed.add(key)
            return True

    def decided(
        self,
        approval_id: str,
        decision: ApprovalDecision,
        thread_id: str,
        actor: str = "",
    ) -> None:
        pass

    def release(self, approval_id: str, thread_id: str, actor: str = "") -> None:
        del actor
        with self._lock:
            self._claimed.discard((thread_id, approval_id))

    def list(self, *, thread_id: str | None = None, limit: int = 100) -> list[ApprovalRecord]:
        return []


class SqliteApprovalStore:
    """审批流水默认落库实现：SQLite 文件，字段幂等、可查询。

    requested 可被 pending_approvals 重复触发，因此用 INSERT OR IGNORE 保证幂等；
    decided 将同一 approval_id 更新为终态并记录审批意见与时间。
    """

    def __init__(self, path: str) -> None:
        self.path = path or ""
        if not self.path:
            raise ValueError("approval_db_path 不能为空")
        os.makedirs(os.path.dirname(os.path.abspath(self.path)) or ".", exist_ok=True)
        with self._connect() as conn:
            enable_wal(conn)
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS approval_records (
                    approval_id TEXT PRIMARY KEY,
                    thread_id TEXT NOT NULL,
                    tool_name TEXT NOT NULL,
                    description TEXT NOT NULL,
                    arguments TEXT NOT NULL,
                    status TEXT NOT NULL,
                    comment TEXT NOT NULL DEFAULT '',
                    selected_values TEXT NOT NULL DEFAULT '{}',
                    decided_by TEXT NOT NULL DEFAULT '',
                    created_at TEXT,
                    decided_at TEXT,
                    claimed_at TEXT
                )
                """
            )
            columns = {
                row[1] for row in conn.execute("PRAGMA table_info(approval_records)").fetchall()
            }
            if "decided_by" not in columns:
                conn.execute(
                    "ALTER TABLE approval_records ADD COLUMN decided_by TEXT NOT NULL DEFAULT ''"
                )
            if "claimed_at" not in columns:
                conn.execute("ALTER TABLE approval_records ADD COLUMN claimed_at TEXT")
            if "selected_values" not in columns:
                conn.execute("ALTER TABLE approval_records ADD COLUMN selected_values TEXT NOT NULL DEFAULT '{}'")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_approvals_thread ON approval_records(thread_id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_approvals_created ON approval_records(created_at)"
            )
            conn.commit()

    def _connect(self) -> sqlite3.Connection:
        return connect_sqlite(self.path)

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    def requested(self, approval_id: str, request: ApprovalRequest, thread_id: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO approval_records
                    (approval_id, thread_id, tool_name, description, arguments, status, created_at)
                VALUES (?, ?, ?, ?, ?, 'pending', ?)
                """,
                (
                    approval_id,
                    thread_id,
                    request.tool_name,
                    request.description,
                    json.dumps(request.arguments, ensure_ascii=False),
                    self._now(),
                ),
            )
            conn.commit()

    def claim(self, approval_id: str, thread_id: str, actor: str = "") -> bool:
        now = datetime.now(timezone.utc)
        stale_before = now - timedelta(seconds=30)
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE approval_records
                SET status = 'processing', decided_by = ?, claimed_at = ?
                WHERE approval_id = ? AND thread_id = ?
                  AND (
                    status = 'pending'
                    OR (status = 'processing' AND (claimed_at IS NULL OR claimed_at < ?))
                  )
                """,
                (actor, now.isoformat(), approval_id, thread_id, stale_before.isoformat()),
            )
            conn.commit()
            return cursor.rowcount == 1

    def release(self, approval_id: str, thread_id: str, actor: str = "") -> None:
        with self._connect() as conn:
            conn.execute(
                """UPDATE approval_records
                   SET status = 'pending', decided_by = '', claimed_at = NULL
                   WHERE approval_id = ? AND thread_id = ? AND status = 'processing'
                     AND decided_by = ?""",
                (approval_id, thread_id, actor),
            )
            conn.commit()

    def decided(
        self,
        approval_id: str,
        decision: ApprovalDecision,
        thread_id: str,
        actor: str = "",
    ) -> None:
        status = "approved" if decision.action == "approve" else "rejected"
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE approval_records
                SET status = ?, comment = ?, selected_values = ?, decided_by = ?, decided_at = ?
                WHERE approval_id = ? AND thread_id = ? AND status IN ('pending', 'processing')
                """,
                (
                    status,
                    decision.comment or "",
                    json.dumps(decision.values, ensure_ascii=False),
                    actor,
                    self._now(),
                    approval_id,
                    thread_id,
                ),
            )
            conn.commit()

    def list(self, *, thread_id: str | None = None, limit: int = 100) -> list[ApprovalRecord]:
        limit = max(1, min(int(limit), 1000))
        sql = (
            "SELECT approval_id, thread_id, tool_name, description, arguments, "
            "status, comment, selected_values, decided_by, created_at, decided_at FROM approval_records"
        )
        params: list[Any] = []
        if thread_id:
            sql += " WHERE thread_id = ?"
            params.append(thread_id)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        records: list[ApprovalRecord] = []
        for row in rows:
            records.append(
                ApprovalRecord(
                    approval_id=row[0],
                    thread_id=row[1],
                    tool_name=row[2],
                    description=row[3],
                    arguments=json.loads(row[4] or "{}"),
                    status=row[5],
                    comment=row[6],
                    values=json.loads(row[7] or "{}"),
                    decided_by=row[8],
                    created_at=_parse_dt(row[9]),
                    decided_at=_parse_dt(row[10]),
                )
            )
        return records


class PostgresApprovalStore:
    """PostgreSQL-backed approval audit store, compatible with ApprovalStore."""

    def __init__(self, conninfo: str) -> None:
        if not conninfo:
            raise ValueError("PostgreSQL conninfo 不能为空")
        self.conninfo = conninfo
        from psycopg import connect

        with connect(self.conninfo) as conn:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS axis_approval_records (
                    approval_id TEXT PRIMARY KEY, thread_id TEXT NOT NULL, tool_name TEXT NOT NULL,
                    description TEXT NOT NULL, arguments JSONB NOT NULL, status TEXT NOT NULL,
                    comment TEXT NOT NULL DEFAULT '', decided_by TEXT NOT NULL DEFAULT '',
                    selected_values JSONB NOT NULL DEFAULT '{}'::jsonb,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    decided_at TIMESTAMPTZ,
                    claimed_at TIMESTAMPTZ
                )"""
            )
            conn.execute(
                "ALTER TABLE axis_approval_records ADD COLUMN IF NOT EXISTS decided_by TEXT NOT NULL DEFAULT ''"
            )
            conn.execute(
                "ALTER TABLE axis_approval_records ADD COLUMN IF NOT EXISTS claimed_at TIMESTAMPTZ"
            )
            conn.execute(
                "ALTER TABLE axis_approval_records ADD COLUMN IF NOT EXISTS selected_values JSONB NOT NULL DEFAULT '{}'::jsonb"
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_axis_approvals_thread ON axis_approval_records(thread_id, created_at DESC)")

    def requested(self, approval_id: str, request: ApprovalRequest, thread_id: str) -> None:
        from psycopg import connect

        with connect(self.conninfo) as conn:
            conn.execute(
                "INSERT INTO axis_approval_records (approval_id, thread_id, tool_name, description, arguments, status) VALUES (%s, %s, %s, %s, %s::jsonb, 'pending') ON CONFLICT (approval_id) DO NOTHING",
                (approval_id, thread_id, request.tool_name, request.description, json.dumps(request.arguments, ensure_ascii=False)),
            )

    def claim(self, approval_id: str, thread_id: str, actor: str = "") -> bool:
        from psycopg import connect

        with connect(self.conninfo) as conn:
            cursor = conn.execute(
                "UPDATE axis_approval_records SET status = 'processing', decided_by = %s, "
                "claimed_at = now() WHERE approval_id = %s AND thread_id = %s AND "
                "(status = 'pending' OR (status = 'processing' AND "
                "(claimed_at IS NULL OR claimed_at < now() - interval '30 seconds')))",
                (actor, approval_id, thread_id),
            )
            return cursor.rowcount == 1

    def release(self, approval_id: str, thread_id: str, actor: str = "") -> None:
        from psycopg import connect

        with connect(self.conninfo) as conn:
            conn.execute(
                "UPDATE axis_approval_records SET status = 'pending', decided_by = '', "
                "claimed_at = NULL WHERE approval_id = %s AND thread_id = %s "
                "AND status = 'processing' AND decided_by = %s",
                (approval_id, thread_id, actor),
            )

    def decided(
        self,
        approval_id: str,
        decision: ApprovalDecision,
        thread_id: str,
        actor: str = "",
    ) -> None:
        from psycopg import connect

        status = "approved" if decision.action == "approve" else "rejected"
        with connect(self.conninfo) as conn:
            conn.execute(
                "UPDATE axis_approval_records SET status = %s, comment = %s, selected_values = %s::jsonb, decided_by = %s, "
                "decided_at = now() WHERE approval_id = %s AND thread_id = %s "
                "AND status IN ('pending', 'processing')",
                (status, decision.comment or "", json.dumps(decision.values, ensure_ascii=False), actor, approval_id, thread_id),
            )

    def list(self, *, thread_id: str | None = None, limit: int = 100) -> list[ApprovalRecord]:
        from psycopg import connect

        limit = max(1, min(int(limit), 1000))
        with connect(self.conninfo) as conn:
            if thread_id is None:
                rows = conn.execute("SELECT approval_id, thread_id, tool_name, description, arguments, status, comment, selected_values, decided_by, created_at, decided_at FROM axis_approval_records ORDER BY created_at DESC LIMIT %s", (limit,)).fetchall()
            else:
                rows = conn.execute("SELECT approval_id, thread_id, tool_name, description, arguments, status, comment, selected_values, decided_by, created_at, decided_at FROM axis_approval_records WHERE thread_id = %s ORDER BY created_at DESC LIMIT %s", (thread_id, limit)).fetchall()
        return [ApprovalRecord(approval_id=r[0], thread_id=r[1], tool_name=r[2], description=r[3], arguments=r[4], status=r[5], comment=r[6], values=r[7] or {}, decided_by=r[8], created_at=r[9], decided_at=r[10]) for r in rows]


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def require_approval(
    tool: BaseTool,
    spec: ApprovalSpec | None = None,
    *,
    policy: ApprovalPolicy | None = None,
) -> StructuredTool:
    """包装一个工具：先校验参数，通过后再暂停请求人工审批，批准后才执行。"""
    approval_spec = spec or ApprovalSpec()
    # 未显式提供策略、或提供了不合法对象时，一律兜底为“必须人工审批”。
    approval_policy = (
        cast(ApprovalPolicy, policy)
        if is_approval_policy(policy)
        else AlwaysRequireApproval()
    )

    validators: list[Validator] = []
    if approval_spec.required_fields:
        validators.append(required_fields(*approval_spec.required_fields))
    validate = chain_validators(*validators, approval_spec.validate)
    approved_tool = tool.model_copy(
        update={
            "metadata": {
                **(getattr(tool, "metadata", None) or {}),
                "axis_requires_approval": True,
            }
        }
    )

    def request(arguments: dict[str, Any]) -> tuple[ApprovalDecision | None, ApprovalRequest]:
        fields = approval_spec.fields(arguments) if approval_spec.fields is not None else []
        approval_request = ApprovalRequest(
            tool_name=tool.name,
            description=approval_spec.description or tool.description,
            arguments=arguments,
            display_title=approval_spec.display_title,
            display_arguments=(
                approval_spec.display_arguments(arguments)
                if approval_spec.display_arguments is not None
                else None
            ),
            fields=fields,
        )
        try:
            needs_approval = approval_policy.requires_approval(approval_request)
        except Exception:
            # 策略执行异常时同样不放行：宁可误拦截，也不让策略 bug 绕过审批。
            needs_approval = True
        if not needs_approval:
            return None, approval_request
        return (
            interrupt(
                approval_request.model_dump(mode="json"),
                response_schema=ApprovalDecision,
            ),
            approval_request,
        )

    def run(**arguments: Any) -> Any:
        error = validate(arguments)
        if error is not None:
            return error
        decision, approval_request = request(arguments)
        if decision is not None:
            error = _validate_decision(decision, approval_request)
            if error is not None:
                return error
        if decision is not None and decision.action == "reject":
            return _reject_text(decision.comment)
        return approved_tool.invoke(arguments)

    async def arun(**arguments: Any) -> Any:
        error = validate(arguments)
        if error is not None:
            return error
        decision, approval_request = request(arguments)
        if decision is not None:
            error = _validate_decision(decision, approval_request)
            if error is not None:
                return error
        if decision is not None and decision.action == "reject":
            return _reject_text(decision.comment)
        return await approved_tool.ainvoke(arguments)

    return StructuredTool.from_function(
        func=run,
        coroutine=arun,
        name=tool.name,
        description=tool.description,
        args_schema=tool.args_schema,
        return_direct=tool.return_direct,
        response_format=tool.response_format,
        metadata={**(getattr(tool, "metadata", None) or {}), "axis_requires_approval": True},
    )


def with_execution_policy(
    tool: BaseTool,
    *,
    timeout: float | None = None,
    retries: int = 0,
    retry_exceptions: tuple[type[Exception], ...] = (TimeoutError, ConnectionError),
) -> BaseTool:
    """Wrap an idempotent/read-only tool with timeout and bounded exception retries.

    Retries are explicit because repeating a side-effecting action can be unsafe.
    Apply this before require_approval when an approved action needs execution policy.
    """
    import asyncio
    from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
    import functools
    import time

    if retries < 0:
        raise ValueError("retries 不能小于 0")
    if timeout is not None and timeout <= 0:
        raise ValueError("timeout 必须大于 0")

    def invoke(fn: Callable[..., Any], *, is_async: bool) -> Callable[..., Any]:
        @functools.wraps(fn)
        def sync_call(*args: Any, **kwargs: Any) -> Any:
            for attempt in range(retries + 1):
                try:
                    if timeout is None:
                        return fn(*args, **kwargs)
                    executor = ThreadPoolExecutor(max_workers=1)
                    future = executor.submit(fn, *args, **kwargs)
                    try:
                        return future.result(timeout=timeout)
                    except FutureTimeout as error:
                        future.cancel()
                        raise TimeoutError(f"tool {tool.name!r} timed out after {timeout}s") from error
                    finally:
                        executor.shutdown(wait=False, cancel_futures=True)
                except retry_exceptions + (TimeoutError,):
                    if attempt == retries:
                        raise
                    time.sleep(min(0.1 * (2**attempt), 1.0))
            raise RuntimeError("unreachable")

        @functools.wraps(fn)
        async def async_call(*args: Any, **kwargs: Any) -> Any:
            for attempt in range(retries + 1):
                try:
                    call = fn(*args, **kwargs)
                    return await asyncio.wait_for(call, timeout) if timeout else await call
                except retry_exceptions:
                    if attempt == retries:
                        raise
                    await asyncio.sleep(min(0.1 * (2**attempt), 1.0))
            raise RuntimeError("unreachable")

        return async_call if is_async else sync_call

    func = getattr(tool, "func", None)
    coroutine = getattr(tool, "coroutine", None)
    if func is None and coroutine is None:
        raise TypeError("该工具不支持执行策略包装；需要由 func 或 coroutine 构建")
    updated = tool.model_copy(deep=False)
    if func is not None:
        updated.func = invoke(func, is_async=False)
    if coroutine is not None:
        updated.coroutine = invoke(coroutine, is_async=True)
    return updated
