"""记忆与检查点：上下文压缩（总结+裁剪）+ Postgres/Memory 检查点。"""
from __future__ import annotations

import atexit
import os
from collections.abc import Sequence
from typing import Any, TypedDict

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    RemoveMessage,
    SystemMessage,
)
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph.message import REMOVE_ALL_MESSAGES

from ._sqlite import SQLITE_BUSY_TIMEOUT_MS, SQLITE_TIMEOUT_SECONDS, connect_sqlite, enable_wal
from .config import Settings

# 统一的“历史摘要”提示词，可被业务侧覆盖。
DEFAULT_SUMMARY_PROMPT = (
    "请把下面的早期对话压缩成一段简洁的中文摘要，"
    "只保留对后续判断有用的关键事实：用户身份/诉求、已完成的动作、"
    "各专家给出的结论、尚未解决的问题、以及任何重要约束。"
)


class CompressionState(TypedDict):
    messages: Sequence[BaseMessage]


# ---------------------------------------------------------------- token 估算
def _message_text(m: BaseMessage) -> str:
    parts: list[str] = []
    content = getattr(m, "content", "") or ""
    if isinstance(content, str):
        parts.append(content)
    elif isinstance(content, list):
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    parts.append(block.get("text", ""))
                elif block.get("type") == "image_url":
                    parts.append("[image]")
            else:
                parts.append(str(block))
    else:
        parts.append(str(content))

    for tc in getattr(m, "tool_calls", None) or []:
        if isinstance(tc, dict):
            parts.append(f"tool:{tc.get('name','')} {tc.get('args','')}")
        else:
            parts.append(str(tc))
    return "\n".join(parts)


def estimate_tokens(messages: Sequence[BaseMessage]) -> int:
    """粗略估算消息总 token 数，无法精确时按 4 字符/ token 兜底。"""
    try:
        import tiktoken

        enc = tiktoken.get_encoding("cl100k_base")
        return sum(len(enc.encode(_message_text(m))) for m in messages)
    except Exception:
        return sum(len(_message_text(m)) // 4 + 1 for m in messages)


# ---------------------------------------------------------------- 上下文压缩
def needs_compression(
    messages: Sequence[BaseMessage],
    *,
    max_tokens: int,
    keep_recent: int,
) -> bool:
    """消息既多又超过 token 上限时才压缩。"""
    if len(messages) <= keep_recent:
        return False
    return estimate_tokens(messages) > max_tokens


def summarize_and_trim(
    state: CompressionState,
    model: BaseChatModel,
    *,
    keep_recent: int,
    summary_prompt: str | None = None,
) -> dict[str, list[BaseMessage]]:
    """把早期消息总结成一条“历史摘要”，并用 RemoveMessage 裁剪掉原文。

    返回可直接作为节点返回值（merge 回图状态）的 dict：
        {"messages": [RemoveMessage(...), ..., AIMessage(摘要)]}
    """
    messages = list(state["messages"])
    if len(messages) <= keep_recent:
        return {}

    to_summarize = messages[:-keep_recent]
    for message in to_summarize:
        if message.id is None:
            raise ValueError("待压缩消息必须具有 id，才能从图状态中删除")
    records = "\n".join(
        f"{getattr(m, 'type', 'msg')}[{getattr(m, 'name', '')}]: {_message_text(m)}"
        for m in to_summarize
    )
    prompt = summary_prompt or DEFAULT_SUMMARY_PROMPT
    resp = model.invoke([SystemMessage(content=prompt), HumanMessage(content=records)])
    summary = resp.content if isinstance(resp.content, str) else str(resp.content)

    summary_msg = AIMessage(content=f"[早期对话摘要]\n{summary}", name="memory")
    # add_messages 对普通 RemoveMessage 会保留未删除消息的原位置，再把摘要
    # 追加到末尾。先清空再按时序重建，确保摘要始终位于最近消息之前。
    return {
        "messages": [
            RemoveMessage(id=REMOVE_ALL_MESSAGES),
            summary_msg,
            *messages[-keep_recent:],
        ]
    }


# ---------------------------------------------------------------- 检查点
def create_checkpointer(settings: Settings | None = None) -> BaseCheckpointSaver[Any]:
    """按配置创建检查点：Postgres、SQLite，最后回退内存。"""
    s = settings or Settings()
    if s.postgres_url:
        try:
            from langgraph.checkpoint.postgres import PostgresSaver
            from psycopg import Connection
            from psycopg.rows import DictRow, dict_row
        except ImportError as e:  # pragma: no cover
            raise RuntimeError(
                "使用 POSTGRES_URL 需要先安装：pip install langgraph-checkpoint-postgres psycopg[binary]"
            ) from e

        # 返回的 saver 需要保持连接打开，直到进程退出。
        conn = Connection[DictRow].connect(
            s.postgres_url, autocommit=True, prepare_threshold=0, row_factory=dict_row
        )
        try:
            saver = PostgresSaver(conn)
            saver.setup()  # 建表（幂等）
        except Exception:
            conn.close()
            raise
        atexit.register(conn.close)
        return saver
    if s.checkpointer_db_path:
        try:
            from langgraph.checkpoint.sqlite import SqliteSaver
        except ImportError as error:  # pragma: no cover
            raise RuntimeError(
                "使用 CHECKPOINTER_DB_PATH 需要安装：pip install langgraph-checkpoint-sqlite"
            ) from error
        path = s.checkpointer_db_path
        os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
        conn = connect_sqlite(path, check_same_thread=False)
        try:
            enable_wal(conn)
            saver = SqliteSaver(conn)
            saver.setup()
        except Exception:
            conn.close()
            raise
        setattr(saver, "_axis_owned_connection", conn)
        atexit.register(conn.close)
        return saver
    return MemorySaver()


async def create_async_checkpointer(
    settings: Settings | None = None,
) -> BaseCheckpointSaver[Any]:
    """为异步图创建 checkpointer。

    PostgresSaver 没有异步接口；FastAPI / astream 必须使用
    AsyncPostgresSaver。连接由 AgentRuntime.aclose() 释放。
    """
    s = settings or Settings()
    if s.postgres_url:
        try:
            from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
            from psycopg import AsyncConnection
            from psycopg.rows import DictRow, dict_row
        except ImportError as error:  # pragma: no cover
            raise RuntimeError(
                "使用 POSTGRES_URL 需要先安装："
                "pip install langgraph-checkpoint-postgres psycopg[binary]"
            ) from error

        conn = await AsyncConnection[DictRow].connect(
            s.postgres_url,
            autocommit=True,
            prepare_threshold=0,
            row_factory=dict_row,
        )
        try:
            saver = AsyncPostgresSaver(conn)
            await saver.setup()
        except Exception:
            await conn.close()
            raise
        setattr(saver, "_axis_owned_connection", conn)
        return saver
    if s.checkpointer_db_path:
        try:
            import aiosqlite
            from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
        except ImportError as error:  # pragma: no cover
            raise RuntimeError(
                "使用 CHECKPOINTER_DB_PATH 需要安装：pip install langgraph-checkpoint-sqlite"
            ) from error
        path = s.checkpointer_db_path
        os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
        conn = await aiosqlite.connect(path, timeout=SQLITE_TIMEOUT_SECONDS)
        try:
            await conn.execute("PRAGMA journal_mode=WAL")
            await conn.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS}")
            saver = AsyncSqliteSaver(conn)
            await saver.setup()
        except Exception:
            await conn.close()
            raise
        setattr(saver, "_axis_owned_connection", conn)
        return saver
    return MemorySaver()
