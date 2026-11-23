"""运行时门面：统一多轮对话 / 流式 / 异步接口。"""
from __future__ import annotations

import asyncio
import contextlib
import contextvars
import logging
import inspect
import queue
import threading
from collections.abc import AsyncGenerator, Iterable, Iterator
from contextlib import asynccontextmanager
from contextlib import contextmanager
from typing import Any, cast

from langchain_core.messages import BaseMessage, HumanMessage
from langchain_core.runnables import RunnableConfig
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command

from .approval import (
    ApprovalDecision,
    ApprovalRecord,
    ApprovalRequest,
    ApprovalStore,
    NullApprovalStore,
    PendingApproval,
    PostgresApprovalStore,
    SqliteApprovalStore,
)
from .artifacts import ArtifactStore, create_artifact_store, materialize_artifact_delivery
from .workflow_state import WorkflowStateStore, create_workflow_state_store
from .conversation import (
    ConversationMessage,
    ConversationListPage,
    ConversationPage,
    ConversationStore,
    SqliteConversationStore,
    create_conversation_store,
)
from .config import Settings
from .messages import last_visible_text
from .observability import Observability, build_observability
from .sandbox import SandboxBackend, close_sandbox_backends
from .state import AgentState
from .steps import (
    AsyncStepTracker,
    MemoryStepStore,
    PostgresStepStore,
    SqliteStepStore,
    StepEvent,
    StepSink,
    StepStore,
    SyncStepTracker,
)

# astream 流式项：要么是消息增量，要么是步骤进度事件。
StreamItem = tuple[BaseMessage, dict[str, Any]] | StepEvent
_approval_actor: contextvars.ContextVar[str] = contextvars.ContextVar(
    "axis_approval_actor", default=""
)
_execution_turn_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "axis_execution_turn_id", default=""
)


class AgentRuntime:
    """对编译后的图做一层薄封装，屏蔽 thread_id / config 的细节。"""

    def __init__(
        self,
        graph: CompiledStateGraph[AgentState, None, AgentState, AgentState],
        *,
        settings: Settings | None = None,
        approval_store: ApprovalStore | None = None,
        sandbox_backends: dict[str, SandboxBackend] | None = None,
        agent_names: Iterable[str] | None = None,
        agent_display_names: dict[str, str] | None = None,
        observability: Observability | None = None,
        step_store: StepStore | None = None,
        artifact_store: ArtifactStore | None = None,
        workflow_state_store: WorkflowStateStore | None = None,
        conversation_store: ConversationStore | None = None,
    ) -> None:
        self.graph = graph
        self.settings = settings or Settings()
        if approval_store is not None:
            self.approval_store = approval_store
        elif self.settings.postgres_url:
            self.approval_store = PostgresApprovalStore(self.settings.postgres_url)
        elif self.settings.approval_db_path:
            self.approval_store = SqliteApprovalStore(self.settings.approval_db_path)
        else:
            self.approval_store = NullApprovalStore()
        if step_store is not None:
            self.step_store = step_store
        elif self.settings.postgres_url:
            self.step_store = PostgresStepStore(self.settings.postgres_url)
        elif self.settings.step_db_path:
            self.step_store = SqliteStepStore(self.settings.step_db_path)
        else:
            self.step_store = MemoryStepStore()
        self.observability = observability if observability is not None else build_observability(self.settings)
        self.artifact_store = create_artifact_store(self.settings, artifact_store)
        self.workflow_state_store = create_workflow_state_store(self.settings, workflow_state_store)
        self.conversation_store = (
            conversation_store
            if conversation_store is not None
            else create_conversation_store(
                postgres_url=self.settings.postgres_url,
                path=self.settings.conversation_db_path,
                retention_days=self.settings.conversation_retention_days,
            )
        )
        self.sandbox_backends = sandbox_backends or {}
        self.step_agent_names = list(agent_names) if agent_names is not None else self._infer_agent_names()
        self.agent_display_names = dict(agent_display_names or {})
        # 审批暂停前已发出的“进行中”Agent 步骤；恢复时沿用其 id（thread_id -> {agent_name: StepEvent}）。
        self._paused_steps: dict[str, dict[str, StepEvent]] = {}
        self._thread_locks: dict[str, asyncio.Lock] = {}
        self._step_write_queue: queue.Queue[StepEvent | None] | None = None
        self._step_writer: threading.Thread | None = None
        if isinstance(self.step_store, SqliteStepStore):
            self._step_write_queue = queue.Queue()
            self._step_writer = threading.Thread(
                target=self._run_step_writer,
                name="axis-sqlite-step-writer",
                daemon=True,
            )
            self._step_writer.start()
        self._closed = False

    def _infer_agent_names(self) -> list[str]:
        """尽量从编译图中推导 Agent 节点名；失败时退回到只识别总调度。"""
        try:
            nodes = getattr(self.graph.get_graph(), "nodes", {})
            names = [str(n) for n in nodes if not str(n).startswith("__")]
            return names or [self.settings.supervisor_name]
        except Exception:
            return [self.settings.supervisor_name]

    def _config(self, thread_id: str) -> RunnableConfig:
        return {"configurable": {"thread_id": thread_id}}

    def _run_config(self, thread_id: str) -> RunnableConfig:
        artifact_run_id = self.artifact_store.active_run(thread_id) or self.artifact_store.begin_run(thread_id)
        return {
            **self._config(thread_id),
            "configurable": {"thread_id": thread_id, "artifact_run_id": artifact_run_id},
            "recursion_limit": self.settings.recursion_limit,
            "callbacks": list(self.observability.callbacks),
        }

    @staticmethod
    def _attach_tracker(config: RunnableConfig, tracker: Any) -> None:
        raw = config.get("callbacks")
        if raw is None:
            callbacks: list[Any] = []
        elif isinstance(raw, list):
            callbacks = list(raw)
        else:
            callbacks = [raw]
        callbacks.append(tracker)
        config["callbacks"] = callbacks

    @staticmethod
    def _approval_step(approval: PendingApproval, thread_id: str) -> StepEvent:
        title = approval.request.display_title or approval.request.tool_name
        return StepEvent(
            id=approval.approval_id,
            thread_id=thread_id,
            agent="审批",
            title=(
                f"等待用户确认：{title}"
                if approval.request.display_title
                else f"等待人工审批：{title}"
            ),
            status="in_progress",
            detail=approval.request.description or "",
        )

    @staticmethod
    def _approval_decision_step(
        approval_id: str,
        approval: PendingApproval,
        decision: ApprovalDecision,
        thread_id: str,
    ) -> StepEvent:
        status = "completed" if decision.action == "approve" else "failed"
        title = approval.request.display_title or approval.request.tool_name
        return StepEvent(
            id=approval_id,
            thread_id=thread_id,
            agent="审批",
            title=(
                f"用户已确认：{title}"
                if decision.action == "approve" and approval.request.display_title
                else f"用户已拒绝：{title}"
                if approval.request.display_title
                else f"人工审批：{title}"
            ),
            status=status,
            detail=decision.comment or "",
        )

    def _record_paused_steps(
        self,
        thread_id: str,
        tracker: Any,
        sink: Any,
        pending: list[PendingApproval],
    ) -> None:
        """图运行结束但留下了待审批请求：保存未完成的 Agent 步骤并发出审批步骤。"""
        if not pending:
            return
        open_steps = tracker.snapshot_open_agent_steps()
        if open_steps:
            self._paused_steps[thread_id] = open_steps
        for approval in pending:
            sink(self._approval_step(approval, thread_id))

    def _take_continuations(self, thread_id: str) -> dict[str, StepEvent]:
        """取出审批暂停前的 Agent 步骤；进程重启后从持久化事件重建。"""
        cached = self._paused_steps.pop(thread_id, None)
        if cached:
            return cached
        latest: dict[str, StepEvent] = {}
        for event in self.list_steps(thread_id=thread_id, limit=1000):
            if event.id not in latest:
                latest[event.id] = event
        return {
            event.agent: event
            for event in latest.values()
            if event.status == "in_progress" and event.agent in self.step_agent_names
        }

    @asynccontextmanager
    async def thread_lock(self, thread_id: str):
        """串行化同一会话；Postgres 时使用 advisory lock 覆盖多进程。"""
        lock = self._thread_locks.setdefault(thread_id, asyncio.Lock())
        async with lock:
            if not self.settings.postgres_url:
                yield
                return
            from psycopg import AsyncConnection

            conn = await AsyncConnection.connect(self.settings.postgres_url, autocommit=True)
            try:
                await conn.execute(
                    "SELECT pg_advisory_lock(hashtextextended(%s, 0))", (thread_id,)
                )
                try:
                    yield
                finally:
                    await conn.execute(
                        "SELECT pg_advisory_unlock(hashtextextended(%s, 0))", (thread_id,)
                    )
            finally:
                await conn.close()

    @contextmanager
    def approval_actor(self, actor: str):
        """将当前认证主体传递到审批审计存储。"""
        token = _approval_actor.set(actor)
        try:
            yield
        finally:
            _approval_actor.reset(token)

    @contextmanager
    def execution_turn(self, turn_id: str):
        """将 API 轮次 ID 传递给本轮产生的步骤事件。"""
        token = _execution_turn_id.set(turn_id)
        try:
            yield
        finally:
            _execution_turn_id.reset(token)

    # ------------------------------------------------------------ 步骤持久化
    def _persist_step(self, event: StepEvent) -> None:
        """步骤事件落库；失败只记录日志，不阻断对话。"""
        turn_id = _execution_turn_id.get()
        if turn_id:
            event = event.model_copy(update={"turn_id": turn_id})
        if isinstance(self.step_store, SqliteStepStore):
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                pass
            else:
                assert self._step_write_queue is not None
                self._step_write_queue.put(event)
                return
        try:
            self.step_store.append(event)
        except Exception:
            logging.exception("steps store append failed")

    def _run_step_writer(self) -> None:
        """Serialize SQLite step writes without depending on an event loop."""
        assert self._step_write_queue is not None
        while True:
            event = self._step_write_queue.get()
            try:
                if event is None:
                    return
                self.step_store.append(event)
            except Exception:
                logging.exception("steps store append failed")
            finally:
                self._step_write_queue.task_done()

    async def _flush_step_writes(self) -> None:
        if self._step_write_queue is not None:
            await asyncio.to_thread(self._step_write_queue.join)

    async def _close_step_writer(self) -> None:
        if self._step_write_queue is None or self._step_writer is None:
            return
        self._step_write_queue.put(None)
        await asyncio.to_thread(self._step_writer.join)
        self._step_writer = None

    def _step_pipe(self, sink: StepSink | None) -> StepSink:
        """先持久化、再转交本地接收方；所有 *_with_steps / *_stream_steps 共用。"""
        def emit(event: StepEvent) -> None:
            turn_id = _execution_turn_id.get()
            if turn_id:
                event = event.model_copy(update={"turn_id": turn_id})
            self._persist_step(event)
            if sink is not None:
                sink(event)

        return emit

    def list_steps(
        self, *, thread_id: str | None = None, limit: int = 100
    ) -> list[StepEvent]:
        """查询历史步骤（新事件在前）。"""
        lister = getattr(self.step_store, "list", None)
        if lister is None:
            return []
        return list(lister(thread_id=thread_id, limit=limit))

    def begin_artifact_run(self, *, thread_id: str) -> str:
        return self.artifact_store.begin_run(thread_id)

    def record_conversation_message(self, message: ConversationMessage) -> None:
        """持久化一条面向用户的对话消息。"""
        self.conversation_store.append(message)

    async def arecord_conversation_message(self, message: ConversationMessage) -> None:
        """Persist conversation history without blocking an async checkpointer."""
        if isinstance(self.conversation_store, SqliteConversationStore):
            await asyncio.to_thread(self.conversation_store.append, message)
            return
        self.conversation_store.append(message)

    def list_conversation_messages(
        self, *, thread_id: str, limit: int = 100, before: str | None = None
    ) -> ConversationPage:
        return self.conversation_store.list_page(
            thread_id=thread_id, limit=limit, before=before,
        )

    def list_conversations(
        self, *, thread_prefix: str, limit: int = 100, before: str | None = None
    ) -> ConversationListPage:
        return self.conversation_store.list_conversations(
            thread_prefix=thread_prefix, limit=limit, before=before,
        )

    def discard_artifact_run(self, *, thread_id: str) -> None:
        run_id = self.artifact_store.active_run(thread_id)
        if run_id is not None:
            self.artifact_store.delete_run(thread_id, run_id)

    def list_artifacts(self, *, thread_id: str, run_id: str | None = None) -> list[Any]:
        return self.artifact_store.list(thread_id, run_id=run_id)

    def get_artifacts(self, *, thread_id: str, run_id: str | None = None) -> list[dict[str, Any]]:
        """Return full structured payloads for single-response delivery."""
        artifacts: list[dict[str, Any]] = []
        if run_id is None:
            run_id = self.artifact_store.active_run(thread_id)
        if run_id is None:
            return []
        for artifact, data in materialize_artifact_delivery(
            self.artifact_store, thread_id, run_id
        ):
            artifacts.append({
                "id": artifact.id,
                "producer": artifact.producer,
                "type": artifact.type,
                "schema_version": artifact.schema_version,
                "count": artifact.count,
                "created_at": artifact.created_at,
                "data": data,
            })
        return artifacts

    def take_artifacts(self, *, thread_id: str, run_id: str | None = None) -> list[dict[str, Any]]:
        """Read and remove this turn's data after it has been prepared for delivery."""
        run_id = run_id or self.artifact_store.active_run(thread_id)
        if run_id is None:
            return []
        artifacts = self.get_artifacts(thread_id=thread_id, run_id=run_id)
        self.artifact_store.delete_run(thread_id, run_id)
        return artifacts

    # ------------------------------------------------------------ 同步
    def invoke(self, text: str, *, thread_id: str = "default") -> AgentState:
        """发送一条用户消息，返回最终状态。多轮靠同一个 thread_id 续接。"""
        result = self.graph.invoke(
            {"messages": [HumanMessage(content=text)]},
            config=self._run_config(thread_id),
        )
        return cast(AgentState, result)

    def invoke_with_steps(
        self, text: str, *, thread_id: str = "default"
    ) -> tuple[AgentState, list[StepEvent]]:
        """同步版：返回最终状态 + 本次收集到的步骤事件。"""
        steps: list[StepEvent] = []
        sink = self._step_pipe(steps.append)
        continuations = self._take_continuations(thread_id)
        tracker = SyncStepTracker(
            sink,
            agent_names=self.step_agent_names,
            supervisor_name=self.settings.supervisor_name,
            continuations=continuations,
            thread_id=thread_id,
            agent_display_names=self.agent_display_names,
        )
        config = self._run_config(thread_id)
        self._attach_tracker(config, tracker)
        result = self.graph.invoke(
            {"messages": [HumanMessage(content=text)]},
            config=config,
        )
        state = cast(AgentState, result)
        self._record_paused_steps(
            thread_id,
            tracker,
            sink,
            self.pending_approvals(thread_id=thread_id),
        )
        return state, steps

    def stream(self, text: str, *, thread_id: str = "default"):
        """按消息流式输出增量（含各 Agent 的思维片段）。"""
        return self.graph.stream(
            {"messages": [HumanMessage(content=text)]},
            config=self._run_config(thread_id),
            stream_mode="messages",
        )

    def stream_steps(self, text: str, *, thread_id: str = "default") -> Iterator[StreamItem]:
        """同步版步骤流：内部借用异步实现，返回包含消息与步骤事件的迭代器。

        注意：这是先收集整轮再返回的缓冲式实现，适合 CLI / 脚本；实时流请用 astream_steps。
        """

        async def collect() -> list[StreamItem]:
            items: list[StreamItem] = []
            async for item in self.astream_steps(text, thread_id=thread_id):
                items.append(item)
            return items

        return iter(asyncio.run(collect()))

    def resume_with_steps(
        self,
        approval_id: str,
        decision: ApprovalDecision,
        *,
        thread_id: str,
    ) -> tuple[AgentState, list[StepEvent]]:
        """同步版：审批决定后恢复执行，并返回状态与步骤。"""
        self._validate_decision(approval_id, thread_id, decision)
        try:
            return self._resume_with_steps_claimed(
                approval_id, decision, thread_id=thread_id
            )
        except BaseException:
            self._release_decision(approval_id, thread_id)
            raise

    def _resume_with_steps_claimed(
        self,
        approval_id: str,
        decision: ApprovalDecision,
        *,
        thread_id: str,
    ) -> tuple[AgentState, list[StepEvent]]:
        steps: list[StepEvent] = []
        sink = self._step_pipe(steps.append)
        approval = self._find_approval(self.pending_approvals(thread_id=thread_id), approval_id)
        if approval is not None:
            sink(self._approval_decision_step(approval_id, approval, decision, thread_id))
        continuations = self._take_continuations(thread_id)
        tracker = SyncStepTracker(
            sink,
            agent_names=self.step_agent_names,
            supervisor_name=self.settings.supervisor_name,
            continuations=continuations,
            thread_id=thread_id,
            agent_display_names=self.agent_display_names,
        )
        config = self._run_config(thread_id)
        self._attach_tracker(config, tracker)
        result = self.graph.invoke(
            Command(resume={approval_id: decision.model_dump(mode="json")}),
            config=config,
        )
        self._record_decision(approval_id, thread_id, decision)
        state = cast(AgentState, result)
        self._record_paused_steps(
            thread_id,
            tracker,
            sink,
            self.pending_approvals(thread_id=thread_id),
        )
        return state, steps

    # ------------------------------------------------------------ 异步
    async def ainvoke(self, text: str, *, thread_id: str = "default") -> AgentState:
        result = await self.graph.ainvoke(
            {"messages": [HumanMessage(content=text)]},
            config=self._run_config(thread_id),
        )
        return cast(AgentState, result)

    async def ainvoke_with_steps(
        self, text: str, *, thread_id: str = "default"
    ) -> tuple[AgentState, list[StepEvent]]:
        """异步版：返回最终状态 + 本次收集到的步骤事件。"""
        steps: list[StepEvent] = []
        sink = self._step_pipe(steps.append)
        continuations = self._take_continuations(thread_id)
        tracker = AsyncStepTracker(
            sink,
            agent_names=self.step_agent_names,
            supervisor_name=self.settings.supervisor_name,
            continuations=continuations,
            thread_id=thread_id,
            agent_display_names=self.agent_display_names,
        )
        config = self._run_config(thread_id)
        self._attach_tracker(config, tracker)
        result = await self.graph.ainvoke(
            {"messages": [HumanMessage(content=text)]},
            config=config,
        )
        state = cast(AgentState, result)
        self._record_paused_steps(
            thread_id,
            tracker,
            sink,
            await self.apending_approvals(thread_id=thread_id),
        )
        await self._flush_step_writes()
        return state, steps

    async def astream(
        self, text: str, *, thread_id: str = "default"
    ) -> AsyncGenerator[tuple[BaseMessage, dict[str, Any]], None]:
        async for chunk in self.graph.astream(
            {"messages": [HumanMessage(content=text)]},
            config=self._run_config(thread_id),
            stream_mode="messages",
        ):
            yield cast(tuple[BaseMessage, dict[str, Any]], chunk)

    async def astream_steps(
        self, text: str, *, thread_id: str = "default"
    ) -> AsyncGenerator[StreamItem, None]:
        """异步实时流：按发生顺序交替产出消息增量与步骤进度事件。"""
        queue: asyncio.Queue[object] = asyncio.Queue()
        sink = self._step_pipe(queue.put_nowait)
        continuations = self._take_continuations(thread_id)
        tracker = AsyncStepTracker(
            sink,
            agent_names=self.step_agent_names,
            supervisor_name=self.settings.supervisor_name,
            continuations=continuations,
            thread_id=thread_id,
            agent_display_names=self.agent_display_names,
        )
        config = self._run_config(thread_id)
        self._attach_tracker(config, tracker)

        async def run_graph() -> None:
            try:
                async for chunk in self.graph.astream(
                    {"messages": [HumanMessage(content=text)]},
                    config=config,
                    stream_mode="messages",
                ):
                    await queue.put(("message", chunk))
            finally:
                await queue.put(("done", None))

        task = asyncio.create_task(run_graph())
        try:
            while True:
                item = await queue.get()
                if isinstance(item, StepEvent):
                    yield item
                    continue
                tag = item[0]  # type: ignore[index]
                if tag == "done":
                    break
                yield item[1]  # type: ignore[index]
            await task
        finally:
            if not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

        await self._record_paused_async(thread_id, tracker, sink)
        await self._flush_step_writes()

    async def _record_paused_async(
        self,
        thread_id: str,
        tracker: Any,
        sink: Any,
    ) -> None:
        self._record_paused_steps(
            thread_id,
            tracker,
            sink,
            await self.apending_approvals(thread_id=thread_id),
        )

    async def aresume_with_steps(
        self,
        approval_id: str,
        decision: ApprovalDecision,
        *,
        thread_id: str,
    ) -> tuple[AgentState, list[StepEvent]]:
        """异步版：审批决定后恢复执行，并返回状态与步骤。"""
        steps: list[StepEvent] = []
        async for item in self.astream_resume_steps(approval_id, decision, thread_id=thread_id):
            if isinstance(item, StepEvent):
                steps.append(item)
        snapshot = await self.graph.aget_state(self._config(thread_id))
        return cast(AgentState, snapshot.values), steps

    async def astream_resume_steps(
        self,
        approval_id: str,
        decision: ApprovalDecision,
        *,
        thread_id: str,
    ) -> AsyncGenerator[StreamItem, None]:
        """异步实时流：先发出审批结论，再按发生顺序产出恢复执行后的消息与步骤。"""
        await self._avalidate_decision(approval_id, thread_id, decision)
        try:
            async for item in self._astream_resume_steps_claimed(
                approval_id, decision, thread_id=thread_id
            ):
                yield item
        except BaseException:
            self._release_decision(approval_id, thread_id)
            raise

    async def _astream_resume_steps_claimed(
        self,
        approval_id: str,
        decision: ApprovalDecision,
        *,
        thread_id: str,
    ) -> AsyncGenerator[StreamItem, None]:
        approval = self._find_approval(
            await self.apending_approvals(thread_id=thread_id), approval_id
        )
        if approval is not None:
            decision_step = self._approval_decision_step(
                approval_id, approval, decision, thread_id
            )
            self._persist_step(decision_step)
            yield decision_step
            await self._flush_step_writes()

        queue: asyncio.Queue[object] = asyncio.Queue()
        continuations = self._take_continuations(thread_id)
        sink = self._step_pipe(queue.put_nowait)
        tracker = AsyncStepTracker(
            sink,
            agent_names=self.step_agent_names,
            supervisor_name=self.settings.supervisor_name,
            continuations=continuations,
            thread_id=thread_id,
            agent_display_names=self.agent_display_names,
        )
        config = self._run_config(thread_id)
        self._attach_tracker(config, tracker)

        async def run_graph() -> None:
            try:
                async for chunk in self.graph.astream(
                    Command(resume={approval_id: decision.model_dump(mode="json")}),
                    config=config,
                    stream_mode="messages",
                ):
                    await queue.put(("message", chunk))
            finally:
                await queue.put(("done", None))

        task = asyncio.create_task(run_graph())
        try:
            while True:
                item = await queue.get()
                if isinstance(item, StepEvent):
                    yield item
                    continue
                tag = item[0]  # type: ignore[index]
                if tag == "done":
                    break
                yield item[1]  # type: ignore[index]
            await task
            self._record_decision(approval_id, thread_id, decision)
        finally:
            if not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

        await self._record_paused_async(thread_id, tracker, sink)
        await self._flush_step_writes()

    async def aresume(
        self,
        approval_id: str,
        decision: ApprovalDecision,
        *,
        thread_id: str,
    ) -> AgentState:
        await self._avalidate_decision(approval_id, thread_id, decision)
        try:
            result = await self.graph.ainvoke(
                Command(resume={approval_id: decision.model_dump(mode="json")}),
                config=self._run_config(thread_id),
            )
            self._record_decision(approval_id, thread_id, decision)
            return cast(AgentState, result)
        except BaseException:
            self._release_decision(approval_id, thread_id)
            raise

    async def astream_resume(
        self,
        approval_id: str,
        decision: ApprovalDecision,
        *,
        thread_id: str,
    ) -> AsyncGenerator[tuple[BaseMessage, dict[str, Any]], None]:
        await self._avalidate_decision(approval_id, thread_id, decision)
        try:
            async for chunk in self.graph.astream(
                Command(resume={approval_id: decision.model_dump(mode="json")}),
                config=self._run_config(thread_id),
                stream_mode="messages",
            ):
                yield cast(tuple[BaseMessage, dict[str, Any]], chunk)
            self._record_decision(approval_id, thread_id, decision)
        except BaseException:
            self._release_decision(approval_id, thread_id)
            raise

    # ------------------------------------------------------------ 审批
    def pending_approvals(self, *, thread_id: str) -> list[PendingApproval]:
        snapshot = self.graph.get_state(self._config(thread_id))
        return self._pending_from_tasks(snapshot.tasks, thread_id)

    def list_approvals(
        self, *, thread_id: str | None = None, limit: int = 100
    ) -> list[ApprovalRecord]:
        """查询审批流水（按 thread 过滤即可，不限则返回全局最近记录）。"""
        lister = getattr(self.approval_store, "list", None)
        if lister is None:
            return []
        return list(lister(thread_id=thread_id, limit=limit))


    async def apending_approvals(self, *, thread_id: str) -> list[PendingApproval]:
        snapshot = await self.graph.aget_state(self._config(thread_id))
        return self._pending_from_tasks(snapshot.tasks, thread_id)

    def _find_approval(
        self, pending: list[PendingApproval], approval_id: str
    ) -> PendingApproval | None:
        return next((a for a in pending if a.approval_id == approval_id), None)

    def _pending_from_tasks(
        self, tasks: tuple[Any, ...], thread_id: str
    ) -> list[PendingApproval]:
        pending: list[PendingApproval] = []
        for task in tasks:
            for item in task.interrupts:
                try:
                    request = ApprovalRequest.model_validate(item.value)
                except ValueError:
                    continue
                approval = PendingApproval(
                    approval_id=item.id,
                    request=request,
                    agent=str(getattr(task, "name", "") or ""),
                )
                self.approval_store.requested(item.id, request, thread_id)
                pending.append(approval)
        return pending

    def _validate_decision(
        self,
        approval_id: str,
        thread_id: str,
        decision: ApprovalDecision,
    ) -> None:
        pending_ids = {
            approval.approval_id
            for approval in self.pending_approvals(thread_id=thread_id)
        }
        self._claim_decision(approval_id, thread_id, pending_ids)

    async def _avalidate_decision(
        self,
        approval_id: str,
        thread_id: str,
        decision: ApprovalDecision,
    ) -> None:
        pending_ids = {
            approval.approval_id
            for approval in await self.apending_approvals(thread_id=thread_id)
        }
        self._claim_decision(approval_id, thread_id, pending_ids)

    def _claim_decision(
        self,
        approval_id: str,
        thread_id: str,
        pending_ids: set[str],
    ) -> None:
        if approval_id not in pending_ids:
            raise ValueError("审批请求不存在、已处理或不属于当前会话")
        claim = getattr(self.approval_store, "claim", None)
        if callable(claim) and not claim(
            approval_id, thread_id, _approval_actor.get()
        ):
            raise ValueError("审批请求正在处理或已处理")

    def _record_decision(
        self,
        approval_id: str,
        thread_id: str,
        decision: ApprovalDecision,
    ) -> None:
        actor = _approval_actor.get()
        try:
            cast(Any, self.approval_store).decided(
                approval_id, decision, thread_id, actor
            )
        except TypeError:
            # 兼容旧版自定义 ApprovalStore。
            self.approval_store.decided(approval_id, decision, thread_id)

    def _release_decision(self, approval_id: str, thread_id: str) -> None:
        release = getattr(self.approval_store, "release", None)
        if callable(release):
            release(approval_id, thread_id, _approval_actor.get())

    # ------------------------------------------------------------ 可观测性
    def flush_observability(self) -> None:
        """同步刷新可观测性后端（上报待发送的 span / event）。"""
        self.observability.flush()

    async def aflush_observability(self) -> None:
        """异步刷新可观测性后端；应用关闭前调用。"""
        await self.observability.aflush()

    async def aclose(self) -> None:
        """Flush telemetry and release runtime-owned resources; safe to call repeatedly."""
        if self._closed:
            return
        self._closed = True
        try:
            await self._flush_step_writes()
            await self._close_step_writer()
            await self.aclose_sandboxes()
        finally:
            try:
                await self.aflush_observability()
            finally:
                checkpointer = getattr(self.graph, "checkpointer", None)
                conn = vars(checkpointer).get("_axis_owned_connection") if checkpointer else None
                if conn is not None:
                    result = conn.close()
                    if inspect.isawaitable(result):
                        await result

    def close(self) -> None:
        """Synchronous counterpart to aclose; use aclose inside async applications."""
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(self.aclose())
            return
        raise RuntimeError("检测到正在运行的事件循环，请改用 `await runtime.aclose()`。")

    async def __aenter__(self) -> "AgentRuntime":
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.aclose()

    # ------------------------------------------------------------ 沙箱
    async def aclose_sandboxes(self) -> None:
        """释放本运行时已加载的所有沙箱后端。"""
        if self.sandbox_backends:
            await close_sandbox_backends(self.sandbox_backends.values())
            self.sandbox_backends = {}

    def close_sandboxes(self) -> None:
        """同步释放沙箱后端；在异步上下文里请改用 `await aclose_sandboxes()`。"""
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(self.aclose_sandboxes())
            return
        raise RuntimeError(
            "检测到正在运行的事件循环，请改用 `await runtime.aclose_sandboxes()`。"
        )

    # ------------------------------------------------------------ 工具
    @staticmethod
    def last_text(result: AgentState) -> str:
        """返回最后一条用户可见的 AI 文本，跳过 Tool/System 等内部消息。"""
        return last_visible_text(result.get("messages", []))
