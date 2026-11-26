"""步骤进度事件：以 进行中 / 完成 / 失败 三态暴露多 Agent 编排的关键环节。

设计边界：
- 只负责事件模型与采集器（工具事件 + Agent 节点事件）；
- 审批环节的步骤由 AgentRuntime 在 pending / resume 处补发；
- 不要求业务 Agent 修改提示词或代码，由框架在图上自动感知。
"""
from __future__ import annotations

import os
import sqlite3
import threading
import uuid
from collections.abc import Callable, Iterable, Mapping
from typing import Any, Literal, Protocol

from langchain_core.callbacks import AsyncCallbackHandler, BaseCallbackHandler
from langchain_core.callbacks.manager import adispatch_custom_event, dispatch_custom_event
from langgraph.errors import GraphInterrupt, ParentCommand
from pydantic import BaseModel, Field

from ._sqlite import connect_sqlite, enable_wal

StepStatus = Literal["in_progress", "completed", "failed", "paused"]


class StepEvent(BaseModel):
    """一次步骤进度信号。

    id 在同一逻辑步骤的 in_progress -> completed / failed 之间保持不变，
    前端据此更新同一条目而不是每次都新增。
    """

    id: str
    type: Literal["step"] = "step"
    thread_id: str = Field(default="", description="所属会话；持久化与查询时按此过滤")
    turn_id: str = Field(default="", description="所属执行轮次；由 API 响应层填充")
    agent: str = Field(default="", description="步骤归属方：supervisor / 专家名 / 审批")
    title: str
    status: StepStatus
    detail: str = ""


def _new_step_id() -> str:
    return uuid.uuid4().hex


# 采集器推送目标：可将 StepEvent 追加到 list，也可塞入 asyncio.Queue 实现实时流出。
StepSink = Callable[[StepEvent], None]
ProgressSummary = Callable[[dict[str, Any]], str]
_PROGRESS_EVENT = "axis_tool_progress"


def with_progress(
    tool: Any,
    *,
    title: str,
    summary: ProgressSummary | None = None,
) -> Any:
    """Attach a user-facing progress title and optional allowlisted argument summary.

    The summary callback should return only information safe to show to end users.
    Tool arguments are never included automatically.
    """
    if not title.strip():
        raise ValueError("进度标题不能为空")
    metadata = dict(getattr(tool, "metadata", None) or {})
    metadata["axis_display_title"] = title
    if summary is not None:
        metadata["axis_progress_summary"] = summary
    return tool.model_copy(update={"metadata": metadata})


def report_progress(detail: str) -> None:
    """Publish one allowlisted progress update from inside a synchronous tool."""
    if detail.strip():
        dispatch_custom_event(_PROGRESS_EVENT, {"detail": detail[:300]})


async def areport_progress(detail: str) -> None:
    """Publish one allowlisted progress update from inside an asynchronous tool."""
    if detail.strip():
        await adispatch_custom_event(_PROGRESS_EVENT, {"detail": detail[:300]})


def tool_step_title(tool_name: str) -> str:
    """把内部工具名转成面向使用者的展示标题。"""
    if tool_name.startswith("transfer_to_"):
        return f"委派专家「{tool_name[len('transfer_to_'):]}」"
    if tool_name.startswith("parallel_to_"):
        targets = tool_name[len("parallel_to_"):].split("_")
        return f"并行委派专家「{'、'.join(targets)}」"
    if tool_name.endswith("_run_code"):
        return f"执行代码（{tool_name[: -len('_run_code')]}）"
    if tool_name.endswith("_run_command"):
        return f"执行命令（{tool_name[: -len('_run_command')]}）"
    return f"执行工具：{tool_name}"


class _StepTrackerBase:
    """共享的采集逻辑；同步/异步子类只负责对接 LangChain 回调签名。"""

    def __init__(
        self,
        sink: StepSink,
        *,
        agent_names: Iterable[str] = (),
        supervisor_name: str = "supervisor",
        continuations: dict[str, StepEvent] | None = None,
        thread_id: str = "",
        agent_display_names: Mapping[str, str] | None = None,
        supervisor_has_started: bool = False,
    ) -> None:
        self._sink = sink
        self._thread_id = thread_id
        self._agents = set(agent_names)
        self._supervisor = supervisor_name
        self._agent_display_names = dict(agent_display_names or {})
        # 上个执行周期（例如审批暂停前）已发出的进行中步骤；恢复时沿用其 id，避免重复新增。
        self._continuations = dict(continuations or {})
        # run_id -> (agent_name, StepEvent)：当前未结束的 Agent 节点步骤。
        self._agent_runs: dict[str, tuple[str, StepEvent]] = {}
        self._agent_stack: list[tuple[str, str]] = []  # (agent_name, run_id)
        self._supervisor_runs_started = int(supervisor_has_started)
        # run_id -> StepEvent：当前未结束的工具步骤。
        self._tool_runs: dict[str, StepEvent] = {}

    # ------------------------------------------------------------ 发射
    def _emit(self, event: StepEvent) -> None:
        self._sink(event)

    def _agent_title(self, node: str, supervisor: str) -> str:
        if node == supervisor:
            return (
                "总调度分析任务中"
                if self._supervisor_runs_started == 0
                else "总调度汇总结果中"
            )
        if node.startswith("router_"):
            name = node[len("router_"):]
            display_name = self._agent_display_names.get(node)
            return f"领域路由「{display_name or name}」决策中"
        display_name = self._agent_display_names.get(node)
        return f"{display_name}处理中" if display_name else f"专家「{node}」处理中"

    def _current_agent(self) -> str:
        """当前正在执行的 Agent（供工具步骤归属）。"""
        if self._agent_stack:
            return self._agent_stack[-1][0]
        return "系统"

    # ------------------------------------------------------------ Agent 节点
    def _begin_agent(
        self,
        node: str | None,
        run_id: str,
        parent_run_id: str | None,
    ) -> None:
        if not node or node not in self._agents:
            return
        # 恢复上次暂停时，复用已发出的 in_progress 步骤，不重复发射。
        if node in self._continuations:
            step = self._continuations.pop(node)
            self._agent_runs[run_id] = (node, step)
            self._agent_stack.append((node, run_id))
            return
        # 节点外层已经开过同节点（嵌套子图），去重。
        if parent_run_id is not None and parent_run_id in self._agent_runs:
            return
        step_id = _new_step_id()
        step = StepEvent(
            id=step_id,
            thread_id=self._thread_id,
            agent=node,
            title=self._agent_title(node, self._supervisor),
            status="in_progress",
        )
        if node == self._supervisor:
            self._supervisor_runs_started += 1
        self._agent_runs[run_id] = (node, step)
        self._agent_stack.append((node, run_id))
        self._emit(step)

    def _end_agent(self, run_id: str, error: BaseException | None) -> None:
        # 审批中断会以 GraphInterrupt 形式上报：步骤保持“进行中”，供暂停时保存、恢复时续用。
        if isinstance(error, GraphInterrupt):
            return
        found = self._agent_runs.pop(run_id, None)
        if found is None:
            return
        _node, step = found
        if self._agent_stack and self._agent_stack[-1][1] == run_id:
            self._agent_stack.pop()
        # 父图用 ParentCommand 表示“转移到其他节点”，不是执行失败，避免把委派误报为异常。
        failed = error is not None and not isinstance(error, ParentCommand)
        status: StepStatus = "failed" if failed else "completed"
        self._emit(
            step.model_copy(
                update={
                    "status": status,
                    "detail": str(error)[:500] if failed else "",
                }
            )
        )

    def snapshot_open_agent_steps(self) -> dict[str, StepEvent]:
        """返回尚未结束的 Agent 步骤，按 agent 名索引，供审批暂停后在运行时保存。"""
        return {node: step for node, step in self._agent_runs.values()}

    # ------------------------------------------------------------ 工具
    def _begin_tool(
        self,
        tool_name: str,
        run_id: str,
        tool_metadata: dict[str, Any] | None,
        inputs: Mapping[str, Any] | None = None,
    ) -> None:
        # 审批类工具在人脑决策期间不单独展示“进行中”，避免暂停后残留悬空的工具步骤。
        if not tool_name or any(
            (tool_metadata or {}).get(marker)
            for marker in ("axis_requires_approval", "axis_internal_tool")
        ):
            return
        # 部分框架工具会通过 metadata.axis_display_title 提供更友好的展示标题（如并行委派）。
        title = (tool_metadata or {}).get("axis_display_title") or tool_step_title(tool_name)
        detail = ""
        summary = (tool_metadata or {}).get("axis_progress_summary")
        if callable(summary) and inputs is not None:
            try:
                detail = str(summary(dict(inputs)))[:300]
            except Exception:
                detail = ""
        step = StepEvent(
            id=_new_step_id(),
            thread_id=self._thread_id,
            agent=self._current_agent(),
            title=title,
            status="in_progress",
            detail=detail,
        )
        self._tool_runs[run_id] = step
        self._emit(step)

    def _end_tool(self, run_id: str, error: BaseException | None) -> None:
        step = self._tool_runs.pop(run_id, None)
        if step is None:
            return
        if isinstance(error, GraphInterrupt):
            self._emit(
                step.model_copy(
                    update={"status": "paused", "detail": "等待人工审批"}
                )
            )
            return
        status: StepStatus = "failed" if error is not None else "completed"
        self._emit(
            step.model_copy(
                update={
                    "status": status,
                    "detail": str(error)[:500] if error is not None else step.detail,
                }
            )
        )

    def _update_tool_progress(self, run_id: str, data: Any) -> None:
        step = self._tool_runs.get(run_id)
        if step is None or not isinstance(data, dict):
            return
        detail = data.get("detail")
        if isinstance(detail, str) and detail.strip():
            updated = step.model_copy(update={"detail": detail[:300]})
            self._tool_runs[run_id] = updated
            self._emit(updated)


class SyncStepTracker(_StepTrackerBase, BaseCallbackHandler):
    """同步图运行时（graph.invoke / graph.stream）使用的步骤采集器。"""

    def on_chain_start(
        self,
        serialized: dict[str, Any],
        inputs: dict[str, Any],
        *,
        run_id,
        parent_run_id=None,
        tags=None,
        metadata=None,
        **kwargs,
    ) -> None:
        self._begin_agent(
            (metadata or {}).get("langgraph_node"),
            str(run_id),
            str(parent_run_id) if parent_run_id else None,
        )

    def on_chain_end(
        self,
        outputs: dict[str, Any],
        *,
        run_id,
        parent_run_id=None,
        **kwargs,
    ) -> None:
        self._end_agent(str(run_id), None)

    def on_chain_error(
        self,
        error,
        *,
        run_id,
        parent_run_id=None,
        **kwargs,
    ) -> None:
        self._end_agent(str(run_id), error)

    def on_tool_start(
        self,
        serialized: dict[str, Any],
        input_str: str,
        *,
        run_id,
        parent_run_id=None,
        tags=None,
        metadata=None,
        inputs=None,
        **kwargs,
    ) -> None:
        self._begin_tool(
            str(serialized.get("name") or ""), str(run_id), metadata,
            inputs if isinstance(inputs, Mapping) else None,
        )

    def on_tool_end(
        self,
        output,
        *,
        run_id,
        parent_run_id=None,
        **kwargs,
    ) -> None:
        self._end_tool(str(run_id), None)

    def on_tool_error(
        self,
        error,
        *,
        run_id,
        parent_run_id=None,
        **kwargs,
    ) -> None:
        self._end_tool(str(run_id), error)

    def on_custom_event(self, name: str, data: Any, *, run_id, **kwargs) -> None:
        if name == _PROGRESS_EVENT:
            self._update_tool_progress(str(run_id), data)


class AsyncStepTracker(_StepTrackerBase, AsyncCallbackHandler):
    """异步图运行时（graph.ainvoke / graph.astream）使用的步骤采集器。"""

    async def on_chain_start(
        self,
        serialized: dict[str, Any],
        inputs: dict[str, Any],
        *,
        run_id,
        parent_run_id=None,
        tags=None,
        metadata=None,
        **kwargs,
    ) -> None:
        self._begin_agent(
            (metadata or {}).get("langgraph_node"),
            str(run_id),
            str(parent_run_id) if parent_run_id else None,
        )

    async def on_chain_end(
        self,
        outputs: dict[str, Any],
        *,
        run_id,
        parent_run_id=None,
        tags=None,
        **kwargs,
    ) -> None:
        self._end_agent(str(run_id), None)

    async def on_chain_error(
        self,
        error,
        *,
        run_id,
        parent_run_id=None,
        tags=None,
        **kwargs,
    ) -> None:
        self._end_agent(str(run_id), error)

    async def on_tool_start(
        self,
        serialized: dict[str, Any],
        input_str: str,
        *,
        run_id,
        parent_run_id=None,
        tags=None,
        metadata=None,
        inputs=None,
        **kwargs,
    ) -> None:
        self._begin_tool(
            str(serialized.get("name") or ""), str(run_id), metadata,
            inputs if isinstance(inputs, Mapping) else None,
        )

    async def on_tool_end(
        self,
        output,
        *,
        run_id,
        parent_run_id=None,
        tags=None,
        **kwargs,
    ) -> None:
        self._end_tool(str(run_id), None)

    async def on_tool_error(
        self,
        error,
        *,
        run_id,
        parent_run_id=None,
        tags=None,
        **kwargs,
    ) -> None:
        self._end_tool(str(run_id), error)

    async def on_custom_event(self, name: str, data: Any, *, run_id, **kwargs) -> None:
        if name == _PROGRESS_EVENT:
            self._update_tool_progress(str(run_id), data)


class StepStore(Protocol):
    """步骤事件的持久化扩展点；不实现时用 MemoryStepStore，配置 step_db_path 时用 SQLite。"""

    def append(self, event: StepEvent) -> None:
        """持久化一条步骤事件；同一 step_id 的 in_progress -> completed/failed 均独立记录。"""
        ...

    def list(self, *, thread_id: str | None = None, limit: int = 100) -> list[StepEvent]:
        """按会话查询最近的事件（新事件在前）。"""
        ...


class NullStepStore:
    """不持久化：查询始终为空。"""

    def append(self, event: StepEvent) -> None:
        pass

    def list(self, *, thread_id: str | None = None, limit: int = 100) -> list[StepEvent]:
        return []


class MemoryStepStore:
    """进程内持久化：适合开发与测试；进程退出即丢失。"""

    def __init__(self) -> None:
        self._events: list[StepEvent] = []
        self._lock = threading.Lock()

    def append(self, event: StepEvent) -> None:
        with self._lock:
            self._events.append(event)

    def list(self, *, thread_id: str | None = None, limit: int = 100) -> list[StepEvent]:
        limit = max(1, min(int(limit), 1000))
        with self._lock:
            items = [
                event
                for event in self._events
                if thread_id is None or event.thread_id == thread_id
            ]
        return items[::-1][:limit]


class SqliteStepStore:
    """SQLite 落库：步骤事件按会话查询，进程重启后仍可追溯。"""

    def __init__(self, path: str) -> None:
        self.path = path or ""
        if not self.path:
            raise ValueError("step_db_path 不能为空")
        os.makedirs(os.path.dirname(os.path.abspath(self.path)) or ".", exist_ok=True)
        with self._connect() as conn:
            enable_wal(conn)
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS step_events (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    step_id TEXT NOT NULL,
                    thread_id TEXT NOT NULL,
                    agent TEXT NOT NULL,
                    title TEXT NOT NULL,
                    status TEXT NOT NULL,
                    detail TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_steps_thread ON step_events(thread_id, seq)"
            )
            conn.commit()

    def _connect(self) -> sqlite3.Connection:
        return connect_sqlite(self.path)

    @staticmethod
    def _now() -> str:
        from datetime import datetime, timezone

        return datetime.now(timezone.utc).isoformat()

    def append(self, event: StepEvent) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO step_events
                    (step_id, thread_id, agent, title, status, detail, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.id,
                    event.thread_id,
                    event.agent,
                    event.title,
                    event.status,
                    event.detail,
                    self._now(),
                ),
            )
            conn.commit()

    def list(self, *, thread_id: str | None = None, limit: int = 100) -> list[StepEvent]:
        limit = max(1, min(int(limit), 1000))
        sql = "SELECT step_id, thread_id, agent, title, status, detail FROM step_events"
        params: list[Any] = []
        if thread_id:
            sql += " WHERE thread_id = ?"
            params.append(thread_id)
        sql += " ORDER BY seq DESC LIMIT ?"
        params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [
            StepEvent(
                id=row[0],
                thread_id=row[1],
                agent=row[2],
                title=row[3],
                status=row[4],
                detail=row[5],
            )
            for row in rows
        ]


class PostgresStepStore:
    """PostgreSQL-backed step event store; requires psycopg (already used by checkpointer)."""

    def __init__(self, conninfo: str) -> None:
        if not conninfo:
            raise ValueError("PostgreSQL conninfo 不能为空")
        self.conninfo = conninfo
        from psycopg import connect

        with connect(self.conninfo) as conn:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS axis_step_events (
                    seq BIGSERIAL PRIMARY KEY, step_id TEXT NOT NULL, thread_id TEXT NOT NULL,
                    agent TEXT NOT NULL, title TEXT NOT NULL, status TEXT NOT NULL,
                    detail TEXT NOT NULL DEFAULT '', created_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )"""
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_axis_steps_thread ON axis_step_events(thread_id, seq)")

    def append(self, event: StepEvent) -> None:
        from psycopg import connect

        with connect(self.conninfo) as conn:
            conn.execute(
                "INSERT INTO axis_step_events (step_id, thread_id, agent, title, status, detail) VALUES (%s, %s, %s, %s, %s, %s)",
                (event.id, event.thread_id, event.agent, event.title, event.status, event.detail),
            )

    def list(self, *, thread_id: str | None = None, limit: int = 100) -> list[StepEvent]:
        from psycopg import connect

        limit = max(1, min(int(limit), 1000))
        with connect(self.conninfo) as conn:
            if thread_id is None:
                rows = conn.execute("SELECT step_id, thread_id, agent, title, status, detail FROM axis_step_events ORDER BY seq DESC LIMIT %s", (limit,)).fetchall()
            else:
                rows = conn.execute("SELECT step_id, thread_id, agent, title, status, detail FROM axis_step_events WHERE thread_id = %s ORDER BY seq DESC LIMIT %s", (thread_id, limit)).fetchall()
        return [StepEvent(id=row[0], thread_id=row[1], agent=row[2], title=row[3], status=row[4], detail=row[5]) for row in rows]
