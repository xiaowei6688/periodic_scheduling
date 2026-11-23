"""FastAPI HTTP 接口。"""
from __future__ import annotations

import logging
import os
import hashlib
import secrets
import sys
from collections.abc import AsyncGenerator
from contextlib import aclosing, asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from langchain_core.messages import BaseMessage
from pydantic import BaseModel, Field, ValidationError

from . import (
    AgentRegistry,
    AgentRuntime,
    ApprovalDecision,
    ApprovalField,
    ApprovalRecord,
    PendingApproval,
    Settings,
    StepEvent,
    abuild_runtime,
    build_runtime,
    visible_text,
)
from .conversation import ConversationMessage as StoredConversationMessage

logger = logging.getLogger(__name__)


def _authenticate_token(settings: Settings, token: str | None) -> str:
    """返回认证主体。未配置 API_KEYS 时保持本地匿名模式。"""
    if not settings.api_keys:
        return "anonymous"
    candidate = token or ""
    for expected, subject in settings.api_keys.items():
        if secrets.compare_digest(candidate, expected):
            if not subject.strip():
                break
            return subject.strip()
    raise HTTPException(status_code=401, detail="认证失败", headers={"WWW-Authenticate": "Bearer"})


def _bearer_token(value: str | None) -> str | None:
    if not value:
        return None
    scheme, _, token = value.partition(" ")
    return token if scheme.lower() == "bearer" and token else None


def _scoped_thread_id(subject: str, thread_id: str) -> str:
    if subject == "anonymous":
        return thread_id
    namespace = hashlib.sha256(subject.encode("utf-8")).hexdigest()[:20]
    return f"tenant-{namespace}:{thread_id}"


class ChatRequest(BaseModel):
    """对话请求。"""

    message: str = Field(min_length=1)
    thread_id: str = Field(default="")


class ChatResponse(BaseModel):
    """对话响应。"""

    answer: str
    thread_id: str
    turn_id: str
    steps: list[StepEvent] = Field(default_factory=list)
    artifacts: list[dict[str, Any]] = Field(default_factory=list)


class ConversationMessage(BaseModel):
    """前端可展示的一条历史消息。"""

    message_id: str
    turn_id: str
    role: Literal["user", "assistant"]
    content: str
    agent: str = ""
    agent_display_name: str = ""
    created_at: datetime


class ConversationHistoryResponse(BaseModel):
    """按消息游标分页的会话历史。"""

    thread_id: str
    messages: list[ConversationMessage]
    has_more: bool
    next_before: str | None = None


class ConversationSummary(BaseModel):
    thread_id: str
    last_message: str
    last_message_at: datetime
    message_count: int


class ConversationListResponse(BaseModel):
    conversations: list[ConversationSummary]
    has_more: bool
    next_before: str | None = None


def _record_assistant_messages(
    chat_runtime: AgentRuntime,
    messages: list[BaseMessage],
    *,
    thread_id: str,
    turn_id: str,
) -> None:
    """Persist visible assistant messages produced after the latest user turn."""
    last_human = next(
        (index for index in range(len(messages) - 1, -1, -1) if messages[index].type == "human"),
        None,
    )
    if last_human is None:
        return
    for index, message in enumerate(messages[last_human + 1 :], start=last_human + 1):
        content = visible_text(message)
        if not content:
            continue
        agent = getattr(message, "name", None) or ""
        chat_runtime.record_conversation_message(StoredConversationMessage(
            thread_id=thread_id,
            turn_id=turn_id,
            message_id=getattr(message, "id", None) or f"{turn_id}-assistant-{index}",
            role="assistant",
            content=content,
            agent=agent,
            agent_display_name=chat_runtime.agent_display_names.get(agent) or "助手",
            created_at=datetime.now(timezone.utc),
        ))


class ApprovalDecisionMessage(ApprovalDecision):
    """WebSocket 审批决定。"""

    type: Literal["approval_decision"] = "approval_decision"
    approval_id: str
    thread_id: str
    turn_id: str = ""


class ApprovalRequiredItem(BaseModel):
    """单个待审批请求，供前端渲染审批卡片。"""

    approval_id: str
    tool_name: str
    description: str
    arguments: dict[str, Any]
    display_title: str = ""
    display_arguments: dict[str, Any] | None = None
    fields: list[ApprovalField] = Field(default_factory=list)
    agent: str = ""
    agent_display_name: str = ""


class ApprovalRequiredResponse(BaseModel):
    """待审批请求列表：并行分支可一次返回多个审批。"""

    type: Literal["approval_required"] = "approval_required"
    thread_id: str
    turn_id: str
    approvals: list[ApprovalRequiredItem]
    steps: list[StepEvent] = Field(default_factory=list)


class ApprovalDecisionRequest(ApprovalDecision):
    thread_id: str
    turn_id: str = ""


def _approval_responses(
    pending: list[PendingApproval],
    thread_id: str,
    turn_id: str,
    steps: list[StepEvent] | None = None,
    agent_display_names: dict[str, str] | None = None,
) -> ApprovalRequiredResponse:
    """把当前所有待审批请求一次性暴露给审批端。"""
    return ApprovalRequiredResponse(
        thread_id=thread_id,
        turn_id=turn_id,
        approvals=[
            ApprovalRequiredItem(
                approval_id=approval.approval_id,
                tool_name=approval.request.tool_name,
                description=approval.request.description,
                arguments=approval.request.arguments,
                display_title=approval.request.display_title,
                display_arguments=approval.request.display_arguments,
                fields=approval.request.fields,
                agent=approval.agent,
                agent_display_name=(agent_display_names or {}).get(approval.agent, ""),
            )
            for approval in pending
        ],
        steps=steps or [],
    )


def _ensure_root_on_path() -> None:
    cwd = os.getcwd()
    if cwd not in sys.path:
        sys.path.insert(0, cwd)


def _apply_storage_defaults(settings: Settings) -> Settings:
    """Web 服务的本地 SQLite Store 默认共用一个数据库文件。"""
    sqlite_path = ".core/core.sqlite3"
    if settings.postgres_url is None:
        if settings.checkpointer_db_path is None:
            settings.checkpointer_db_path = sqlite_path
        if settings.approval_db_path is None:
            settings.approval_db_path = sqlite_path
        if settings.step_db_path is None:
            settings.step_db_path = sqlite_path
        if settings.conversation_db_path is None:
            settings.conversation_db_path = sqlite_path
    if settings.artifact_db_path is None:
        settings.artifact_db_path = sqlite_path
    if settings.workflow_state_db_path is None:
        settings.workflow_state_db_path = sqlite_path
    return settings


def create_runtime() -> AgentRuntime:
    """按项目默认配置创建运行时。"""
    _ensure_root_on_path()
    settings = _apply_storage_defaults(Settings())
    registry = AgentRegistry().discover_package("agents")
    return build_runtime(
        registry.all(), settings=settings, routers=registry.routers()
    )


async def create_async_runtime() -> AgentRuntime:
    """按项目默认配置创建异步运行时。"""
    _ensure_root_on_path()
    settings = _apply_storage_defaults(Settings())
    registry = AgentRegistry().discover_package("agents")
    return await abuild_runtime(
        registry.all(), settings=settings, routers=registry.routers()
    )


def create_app(runtime: AgentRuntime | None = None) -> FastAPI:
    """创建 API 应用，支持注入 runtime 以便测试或集成。"""
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        chat_runtime = runtime if runtime is not None else await create_async_runtime()
        app.state.runtime = chat_runtime
        if not chat_runtime.settings.api_keys:
            logger.warning("API_KEYS 未配置；API 正在本地匿名模式下运行")
        try:
            yield
        finally:
            await chat_runtime.aclose()

    app = FastAPI(title="axis API", version="0.1.0", lifespan=lifespan)
    if runtime is not None:
        app.state.runtime = runtime

    def get_runtime() -> AgentRuntime:
        return app.state.runtime

    pending_turn_ids: dict[str, str] = {}

    async def authenticate(request: Request) -> str:
        token = _bearer_token(request.headers.get("authorization"))
        return _authenticate_token(get_runtime().settings, token)

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(Path(__file__).parent / "static" / "chat.html")

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/v1/approvals", response_model=list[ApprovalRecord])
    async def list_approvals(
        thread_id: str | None = None,
        limit: int = 100,
        subject: str = Depends(authenticate),
    ) -> list[ApprovalRecord]:
        """查询审批流水：可按会话过滤，默认返回全局最近记录。"""
        if subject != "anonymous" and not thread_id:
            raise HTTPException(status_code=400, detail="鉴权模式下查询审批必须提供 thread_id")
        internal_id = _scoped_thread_id(subject, thread_id) if thread_id else None
        records = get_runtime().list_approvals(thread_id=internal_id, limit=limit)
        if thread_id:
            return [record.model_copy(update={"thread_id": thread_id}) for record in records]
        return records

    @app.get("/v1/threads/{thread_id}/steps", response_model=list[StepEvent])
    async def list_steps(
        thread_id: str,
        limit: int = 100,
        subject: str = Depends(authenticate),
    ) -> list[StepEvent]:
        """查询某个会话的步骤进度履历（新事件在前）。"""
        internal_id = _scoped_thread_id(subject, thread_id)
        events = get_runtime().list_steps(thread_id=internal_id, limit=limit)
        return [event.model_copy(update={"thread_id": thread_id}) for event in events]

    @app.get("/v1/threads", response_model=ConversationListResponse)
    async def list_conversations(
        limit: int = Query(default=50, ge=1, le=200),
        before: str | None = None,
        subject: str = Depends(authenticate),
    ) -> ConversationListResponse:
        """按当前认证主体列出有记录的会话，最新活动优先。"""
        prefix = "" if subject == "anonymous" else f"{_scoped_thread_id(subject, '')}"
        page = get_runtime().list_conversations(
            thread_prefix=prefix, limit=limit, before=before,
        )
        if before is not None and not page.cursor_found:
            raise HTTPException(status_code=400, detail="会话列表游标格式无效")
        return ConversationListResponse(
            conversations=[
                ConversationSummary(
                    thread_id=(
                        item.thread_id[len(prefix):]
                        if prefix and item.thread_id.startswith(prefix)
                        else item.thread_id
                    ),
                    last_message=item.last_message,
                    last_message_at=item.last_message_at,
                    message_count=item.message_count,
                )
                for item in page.conversations
            ],
            has_more=page.has_more,
            next_before=page.next_before,
        )

    @app.get(
        "/v1/threads/{thread_id}/messages",
        response_model=ConversationHistoryResponse,
    )
    async def list_conversation_messages(
        thread_id: str,
        limit: int = Query(default=100, ge=1, le=500),
        before: str | None = None,
        subject: str = Depends(authenticate),
    ) -> ConversationHistoryResponse:
        """读取独立会话记录 Store 中的历史消息。"""
        internal_id = _scoped_thread_id(subject, thread_id)
        page = get_runtime().list_conversation_messages(
            thread_id=internal_id, limit=limit, before=before,
        )
        if before is not None and not page.cursor_found:
            raise HTTPException(status_code=404, detail="历史消息游标不存在或已过期")
        return ConversationHistoryResponse(
            thread_id=thread_id,
            messages=[
                ConversationMessage(
                    message_id=message.message_id,
                    turn_id=message.turn_id,
                    role=message.role,
                    content=message.content,
                    agent=message.agent,
                    agent_display_name=message.agent_display_name,
                    created_at=message.created_at,
                )
                for message in page.messages
            ],
            has_more=page.has_more,
            next_before=page.messages[0].message_id if page.has_more and page.messages else None,
        )

    @app.post(
        "/v1/chat",
        response_model=ChatResponse | ApprovalRequiredResponse,
    )
    async def chat(
        request: ChatRequest,
        subject: str = Depends(authenticate),
    ) -> ChatResponse | ApprovalRequiredResponse:
        thread_id = request.thread_id or uuid4().hex
        turn_id = uuid4().hex
        internal_id = _scoped_thread_id(subject, thread_id)
        chat_runtime = get_runtime()
        async with chat_runtime.thread_lock(internal_id):
            await chat_runtime.arecord_conversation_message(StoredConversationMessage(
                thread_id=internal_id,
                turn_id=turn_id,
                message_id=uuid4().hex,
                role="user",
                content=request.message,
                created_at=datetime.now(timezone.utc),
            ))
            chat_runtime.begin_artifact_run(thread_id=internal_id)
            try:
                with chat_runtime.execution_turn(turn_id):
                    result, steps = await chat_runtime.ainvoke_with_steps(
                        request.message,
                        thread_id=internal_id,
                    )
                    pending = await chat_runtime.apending_approvals(thread_id=internal_id)
            except Exception:
                if not await chat_runtime.apending_approvals(thread_id=internal_id):
                    chat_runtime.discard_artifact_run(thread_id=internal_id)
                raise
        _record_assistant_messages(
            chat_runtime,
            list(result.get("messages", [])),
            thread_id=internal_id,
            turn_id=turn_id,
        )
        steps = [step.model_copy(update={"thread_id": thread_id, "turn_id": turn_id}) for step in steps]
        if pending:
            pending_turn_ids[internal_id] = turn_id
            return _approval_responses(
                pending,
                thread_id,
                turn_id,
                steps,
                chat_runtime.agent_display_names,
            )
        pending_turn_ids.pop(internal_id, None)
        return ChatResponse(
            answer=chat_runtime.last_text(result),
            thread_id=thread_id,
            turn_id=turn_id,
            steps=steps,
            artifacts=getattr(chat_runtime, "take_artifacts", lambda **_: [])(thread_id=internal_id),
        )

    @app.post(
        "/v1/approvals/{approval_id}",
        response_model=ChatResponse | ApprovalRequiredResponse,
    )
    async def decide_approval(
        approval_id: str,
        request: ApprovalDecisionRequest,
        subject: str = Depends(authenticate),
    ) -> ChatResponse | ApprovalRequiredResponse:
        chat_runtime = get_runtime()
        internal_id = _scoped_thread_id(subject, request.thread_id)
        turn_id = pending_turn_ids.get(internal_id) or request.turn_id or uuid4().hex
        decision = ApprovalDecision(
            action=request.action, comment=request.comment, values=request.values,
        )
        try:
            async with chat_runtime.thread_lock(internal_id):
                chat_runtime.begin_artifact_run(thread_id=internal_id)
                with chat_runtime.execution_turn(turn_id):
                    with chat_runtime.approval_actor(subject):
                        result, steps = await chat_runtime.aresume_with_steps(
                            approval_id,
                            decision,
                            thread_id=internal_id,
                        )
                    pending = await chat_runtime.apending_approvals(thread_id=internal_id)
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except Exception:
            if not await chat_runtime.apending_approvals(thread_id=internal_id):
                chat_runtime.discard_artifact_run(thread_id=internal_id)
            raise
        _record_assistant_messages(
            chat_runtime,
            list(result.get("messages", [])),
            thread_id=internal_id,
            turn_id=turn_id,
        )
        steps = [step.model_copy(update={"thread_id": request.thread_id, "turn_id": turn_id}) for step in steps]
        if pending:
            pending_turn_ids[internal_id] = turn_id
            return _approval_responses(
                pending,
                request.thread_id,
                turn_id,
                steps,
                chat_runtime.agent_display_names,
            )
        pending_turn_ids.pop(internal_id, None)
        return ChatResponse(
            answer=chat_runtime.last_text(result),
            thread_id=request.thread_id,
            turn_id=turn_id,
            steps=steps,
            artifacts=getattr(chat_runtime, "take_artifacts", lambda **_: [])(thread_id=internal_id),
        )

    @app.websocket("/v1/chat/ws")
    async def websocket_chat(websocket: WebSocket) -> None:
        chat_runtime = get_runtime()
        token = _bearer_token(websocket.headers.get("authorization"))
        token = token or websocket.query_params.get("access_token")
        try:
            subject = _authenticate_token(chat_runtime.settings, token)
        except HTTPException:
            await websocket.close(code=4401, reason="认证失败")
            return

        async def send_run(
            stream: AsyncGenerator[tuple[BaseMessage, dict[str, Any]] | StepEvent, None],
            thread_id: str,
            internal_id: str,
            turn_id: str,
        ) -> None:
            chat_runtime.begin_artifact_run(thread_id=internal_id)
            answers: dict[str, str] = {}
            try:
                async with aclosing(stream) as items:
                    async for item in items:
                        if isinstance(item, StepEvent):
                            await websocket.send_json({
                                **item.model_dump(mode="json"),
                                "thread_id": thread_id,
                                "turn_id": turn_id,
                            })
                            continue
                        message, metadata = item
                        text = visible_text(message)
                        if not text:
                            continue
                        agent = message.name or str(metadata.get("langgraph_node", "assistant"))
                        agent_display_name = chat_runtime.agent_display_names.get(agent) or "助手"
                        message_id = message.id or f"{metadata.get('langgraph_checkpoint_ns', '')}:{agent}"
                        answers[message_id] = answers.get(message_id, "") + text
                        await chat_runtime.arecord_conversation_message(StoredConversationMessage(
                            thread_id=internal_id,
                            turn_id=turn_id,
                            message_id=message_id,
                            role="assistant",
                            content=answers[message_id],
                            agent=agent,
                            agent_display_name=agent_display_name,
                            created_at=datetime.now(timezone.utc),
                        ))
                        await websocket.send_json({
                            "type": "delta", "thread_id": thread_id,
                            "turn_id": turn_id,
                            "message_id": message_id, "agent": agent,
                            "agent_display_name": agent_display_name, "text": text,
                        })
            except Exception:
                if not await chat_runtime.apending_approvals(thread_id=internal_id):
                    chat_runtime.discard_artifact_run(thread_id=internal_id)
                raise

            pending = await chat_runtime.apending_approvals(thread_id=internal_id)
            if pending:
                pending_turn_ids[internal_id] = turn_id
                for approval in pending:
                    agent_display_name = (
                        chat_runtime.agent_display_names.get(approval.agent) or ""
                    )
                    await websocket.send_json({
                        **approval.request.model_dump(mode="json"),
                        "type": "approval_required",
                        "thread_id": thread_id,
                        "turn_id": turn_id,
                        "approval_id": approval.approval_id,
                        "agent": approval.agent,
                        "agent_display_name": agent_display_name,
                    })
                return
            pending_turn_ids.pop(internal_id, None)
            await websocket.send_json({
                "type": "done", "thread_id": thread_id,
                "turn_id": turn_id,
                "answer": next(reversed(answers.values()), ""),
                "artifacts": getattr(chat_runtime, "take_artifacts", lambda **_: [])(thread_id=internal_id),
            })

        await websocket.accept()
        session_id = uuid4().hex
        turn_id = ""
        try:
            while True:
                try:
                    payload = await websocket.receive_json()
                except (ValidationError, ValueError):
                    await websocket.send_json({"type": "error", "message": "请求格式错误，message 必须为非空字符串"})
                    continue
                try:
                    if isinstance(payload, dict) and payload.get("type") == "approval_decision":
                        decision_message = ApprovalDecisionMessage.model_validate(payload)
                        session_id = decision_message.thread_id
                        internal_id = _scoped_thread_id(subject, session_id)
                        turn_id = (
                            pending_turn_ids.get(internal_id)
                            or decision_message.turn_id
                            or uuid4().hex
                        )
                        decision = ApprovalDecision.model_validate(decision_message.model_dump())
                        async with chat_runtime.thread_lock(internal_id):
                            await websocket.send_json({
                                "type": "resumed", "thread_id": session_id, "turn_id": turn_id,
                            })
                            with chat_runtime.execution_turn(turn_id):
                                with chat_runtime.approval_actor(subject):
                                    await send_run(
                                        chat_runtime.astream_resume_steps(
                                            decision_message.approval_id,
                                            decision,
                                            thread_id=internal_id,
                                        ),
                                        session_id,
                                        internal_id,
                                        turn_id,
                                    )
                    else:
                        request = ChatRequest.model_validate(payload)
                        session_id = request.thread_id or session_id
                        turn_id = uuid4().hex
                        internal_id = _scoped_thread_id(subject, session_id)
                        await websocket.send_json({
                            "type": "start", "thread_id": session_id, "turn_id": turn_id,
                        })
                        async with chat_runtime.thread_lock(internal_id):
                            await chat_runtime.arecord_conversation_message(StoredConversationMessage(
                                thread_id=internal_id,
                                turn_id=turn_id,
                                message_id=uuid4().hex,
                                role="user",
                                content=request.message,
                                created_at=datetime.now(timezone.utc),
                            ))
                            with chat_runtime.execution_turn(turn_id):
                                await send_run(
                                    chat_runtime.astream_steps(request.message, thread_id=internal_id),
                                    session_id,
                                    internal_id,
                                    turn_id,
                                )
                except WebSocketDisconnect:
                    raise
                except (ValidationError, ValueError) as error:
                    await websocket.send_json({
                        "type": "error",
                        "thread_id": session_id,
                        "turn_id": turn_id,
                        "message": str(error),
                    })
                except Exception:
                    logger.exception("WebSocket chat failed")
                    await websocket.send_json({
                        "type": "error", "thread_id": session_id, "turn_id": turn_id,
                        "message": "生成失败，请稍后重试",
                    })
        except WebSocketDisconnect:
            return

    return app


app = create_app()


def main() -> None:
    """启动开发服务器。生产环境建议直接使用 uvicorn 命令。"""
    uvicorn.run("axis.api:app", host="0.0.0.0", port=8000, reload=True)


if __name__ == "__main__":
    main()
