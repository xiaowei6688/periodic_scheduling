"""面向用户的会话记录存储，不依赖可压缩的 Agent Checkpointer。"""
from __future__ import annotations

import os
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Literal, Protocol

from pydantic import BaseModel

from ._sqlite import connect_sqlite, enable_wal


class ConversationMessage(BaseModel):
    thread_id: str
    turn_id: str
    message_id: str
    role: Literal["user", "assistant"]
    content: str
    agent: str = ""
    agent_display_name: str = ""
    created_at: datetime


class ConversationPage(BaseModel):
    messages: list[ConversationMessage]
    has_more: bool
    cursor_found: bool = True


class ConversationSummary(BaseModel):
    thread_id: str
    last_message: str
    last_message_at: datetime
    message_count: int


class ConversationListPage(BaseModel):
    conversations: list[ConversationSummary]
    has_more: bool
    next_before: str | None = None
    cursor_found: bool = True


class ConversationStore(Protocol):
    def append(self, message: ConversationMessage) -> None: ...
    def list_page(
        self, *, thread_id: str, limit: int, before: str | None = None
    ) -> ConversationPage: ...
    def list_conversations(
        self, *, thread_prefix: str, limit: int, before: str | None = None
    ) -> ConversationListPage: ...


class MemoryConversationStore:
    """开发/测试存储；进程退出后记录消失。"""

    def __init__(self) -> None:
        self._messages: dict[str, list[ConversationMessage]] = {}
        self._sequence: dict[str, int] = {}
        self._next_sequence = 0
        self._lock = threading.RLock()

    def append(self, message: ConversationMessage) -> None:
        with self._lock:
            messages = self._messages.setdefault(message.thread_id, [])
            for index, current in enumerate(messages):
                if (current.turn_id, current.message_id) == (message.turn_id, message.message_id):
                    messages[index] = message
                    return
            messages.append(message)
            self._next_sequence += 1
            self._sequence[message.thread_id] = self._next_sequence

    def list_page(
        self, *, thread_id: str, limit: int, before: str | None = None
    ) -> ConversationPage:
        with self._lock:
            messages = list(self._messages.get(thread_id, []))
        end = len(messages)
        if before is not None:
            end = next((i for i, item in enumerate(messages) if item.message_id == before), -1)
            if end < 0:
                return ConversationPage(messages=[], has_more=False, cursor_found=False)
        start = max(0, end - limit)
        return ConversationPage(messages=messages[start:end], has_more=start > 0)

    def list_conversations(
        self, *, thread_prefix: str, limit: int, before: str | None = None
    ) -> ConversationListPage:
        with self._lock:
            summaries = []
            for thread_id, messages in self._messages.items():
                if not thread_id.startswith(thread_prefix) or not messages:
                    continue
                summaries.append((
                    self._sequence.get(thread_id, 0),
                    ConversationSummary(
                        thread_id=thread_id,
                        last_message=messages[-1].content[:240],
                        last_message_at=messages[-1].created_at,
                        message_count=len(messages),
                    ),
                ))
        summaries.sort(key=lambda item: item[0], reverse=True)
        if before is not None:
            try:
                cursor = int(before)
            except ValueError:
                return ConversationListPage(conversations=[], has_more=False, cursor_found=False)
            summaries = [item for item in summaries if item[0] < cursor]
        selected = summaries[:limit + 1]
        has_more = len(selected) > limit
        page = selected[:limit]
        return ConversationListPage(
            conversations=[item[1] for item in page],
            has_more=has_more,
            next_before=str(page[-1][0]) if has_more and page else None,
        )


class SqliteConversationStore:
    """可跨进程重启保留记录的 SQLite 实现。"""

    def __init__(self, path: str, *, retention_days: int | None = None) -> None:
        if not path:
            raise ValueError("conversation_db_path 不能为空")
        if retention_days is not None and retention_days < 1:
            raise ValueError("conversation_retention_days 必须大于 0")
        self.path = path
        self.retention_days = retention_days
        os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
        with self._connect() as conn:
            enable_wal(conn)
            conn.execute(
                """CREATE TABLE IF NOT EXISTS axis_conversation_messages (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    thread_id TEXT NOT NULL,
                    turn_id TEXT NOT NULL,
                    message_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    agent TEXT NOT NULL DEFAULT '',
                    agent_display_name TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    UNIQUE(thread_id, turn_id, message_id)
                )"""
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_axis_conversation_thread_seq "
                "ON axis_conversation_messages(thread_id, seq)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_axis_conversation_created "
                "ON axis_conversation_messages(created_at, thread_id, seq)"
            )

    def _connect(self) -> sqlite3.Connection:
        return connect_sqlite(self.path)

    def append(self, message: ConversationMessage) -> None:
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO axis_conversation_messages
                    (thread_id, turn_id, message_id, role, content, agent, agent_display_name, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(thread_id, turn_id, message_id) DO UPDATE SET
                        role=excluded.role, content=excluded.content, agent=excluded.agent,
                        agent_display_name=excluded.agent_display_name""",
                (
                    message.thread_id, message.turn_id, message.message_id, message.role,
                    message.content, message.agent, message.agent_display_name,
                    message.created_at.isoformat(),
                ),
            )
            self._prune(conn)

    def _prune(self, conn: sqlite3.Connection) -> None:
        if self.retention_days is not None:
            cutoff = datetime.now(timezone.utc) - timedelta(days=self.retention_days)
            conn.execute(
                "DELETE FROM axis_conversation_messages WHERE created_at < ?",
                (cutoff.isoformat(),),
            )

    def list_page(
        self, *, thread_id: str, limit: int, before: str | None = None
    ) -> ConversationPage:
        with self._connect() as conn:
            if before is None:
                rows = conn.execute(
                    """SELECT seq, thread_id, turn_id, message_id, role, content, agent,
                              agent_display_name, created_at
                       FROM axis_conversation_messages WHERE thread_id = ?
                       ORDER BY seq DESC LIMIT ?""",
                    (thread_id, limit + 1),
                ).fetchall()
            else:
                cursor = conn.execute(
                    "SELECT seq FROM axis_conversation_messages WHERE thread_id = ? AND message_id = ? "
                    "ORDER BY seq DESC LIMIT 1",
                    (thread_id, before),
                ).fetchone()
                if cursor is None:
                    return ConversationPage(messages=[], has_more=False, cursor_found=False)
                rows = conn.execute(
                    """SELECT seq, thread_id, turn_id, message_id, role, content, agent,
                              agent_display_name, created_at
                       FROM axis_conversation_messages WHERE thread_id = ? AND seq < ?
                       ORDER BY seq DESC LIMIT ?""",
                    (thread_id, cursor[0], limit + 1),
                ).fetchall()
        has_more = len(rows) > limit
        selected = rows[:limit]
        selected.reverse()
        return ConversationPage(
            messages=[
                ConversationMessage(
                    thread_id=row[1], turn_id=row[2], message_id=row[3], role=row[4],
                    content=row[5], agent=row[6], agent_display_name=row[7],
                    created_at=datetime.fromisoformat(row[8]),
                )
                for row in selected
            ],
            has_more=has_more,
        )

    def list_conversations(
        self, *, thread_prefix: str, limit: int, before: str | None = None
    ) -> ConversationListPage:
        with self._connect() as conn:
            query = """WITH latest AS (
                    SELECT thread_id, MAX(seq) AS last_seq, COUNT(*) AS message_count
                    FROM axis_conversation_messages WHERE thread_id LIKE ? GROUP BY thread_id
                )
                SELECT latest.thread_id, messages.content, messages.created_at,
                       latest.message_count, latest.last_seq
                FROM latest JOIN axis_conversation_messages AS messages ON messages.seq = latest.last_seq"""
            params: list[Any] = [f"{thread_prefix}%"]
            if before is not None:
                try:
                    cursor = int(before)
                except ValueError:
                    return ConversationListPage(conversations=[], has_more=False, cursor_found=False)
                query += " WHERE latest.last_seq < ?"
                params.append(cursor)
            query += " ORDER BY latest.last_seq DESC LIMIT ?"
            params.append(limit + 1)
            rows = conn.execute(query, params).fetchall()
        has_more = len(rows) > limit
        selected = rows[:limit]
        return ConversationListPage(
            conversations=[
                ConversationSummary(
                    thread_id=row[0], last_message=row[1][:240],
                    last_message_at=datetime.fromisoformat(row[2]), message_count=row[3],
                )
                for row in selected
            ],
            has_more=has_more,
            next_before=str(selected[-1][4]) if has_more and selected else None,
        )


class PostgresConversationStore:
    """共享 PostgreSQL 会话存储，适用于多进程/多副本服务。"""

    def __init__(self, conninfo: str, *, retention_days: int | None = None) -> None:
        if not conninfo:
            raise ValueError("PostgreSQL conninfo 不能为空")
        if retention_days is not None and retention_days < 1:
            raise ValueError("conversation_retention_days 必须大于 0")
        self.conninfo = conninfo
        self.retention_days = retention_days
        from psycopg import connect

        with connect(conninfo) as conn:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS axis_conversation_messages (
                    seq BIGSERIAL PRIMARY KEY, thread_id TEXT NOT NULL, turn_id TEXT NOT NULL,
                    message_id TEXT NOT NULL, role TEXT NOT NULL, content TEXT NOT NULL,
                    agent TEXT NOT NULL DEFAULT '', agent_display_name TEXT NOT NULL DEFAULT '',
                    created_at TIMESTAMPTZ NOT NULL,
                    UNIQUE(thread_id, turn_id, message_id)
                )"""
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_axis_conversation_thread_seq "
                "ON axis_conversation_messages(thread_id, seq)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_axis_conversation_created "
                "ON axis_conversation_messages(created_at, thread_id, seq)"
            )

    def append(self, message: ConversationMessage) -> None:
        from psycopg import connect

        with connect(self.conninfo) as conn:
            conn.execute(
                """INSERT INTO axis_conversation_messages
                    (thread_id, turn_id, message_id, role, content, agent, agent_display_name, created_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT(thread_id, turn_id, message_id) DO UPDATE SET
                        role=EXCLUDED.role, content=EXCLUDED.content, agent=EXCLUDED.agent,
                        agent_display_name=EXCLUDED.agent_display_name""",
                (
                    message.thread_id, message.turn_id, message.message_id, message.role,
                    message.content, message.agent, message.agent_display_name, message.created_at,
                ),
            )
            if self.retention_days is not None:
                conn.execute(
                    "DELETE FROM axis_conversation_messages WHERE created_at < now() - (%s * interval '1 day')",
                    (self.retention_days,),
                )

    def list_page(
        self, *, thread_id: str, limit: int, before: str | None = None
    ) -> ConversationPage:
        from psycopg import connect

        with connect(self.conninfo) as conn:
            if before is None:
                rows = conn.execute(
                    """SELECT seq, thread_id, turn_id, message_id, role, content, agent,
                              agent_display_name, created_at
                       FROM axis_conversation_messages WHERE thread_id = %s
                       ORDER BY seq DESC LIMIT %s""",
                    (thread_id, limit + 1),
                ).fetchall()
            else:
                cursor = conn.execute(
                    "SELECT seq FROM axis_conversation_messages WHERE thread_id = %s AND message_id = %s "
                    "ORDER BY seq DESC LIMIT 1",
                    (thread_id, before),
                ).fetchone()
                if cursor is None:
                    return ConversationPage(messages=[], has_more=False, cursor_found=False)
                rows = conn.execute(
                    """SELECT seq, thread_id, turn_id, message_id, role, content, agent,
                              agent_display_name, created_at
                       FROM axis_conversation_messages WHERE thread_id = %s AND seq < %s
                       ORDER BY seq DESC LIMIT %s""",
                    (thread_id, cursor[0], limit + 1),
                ).fetchall()
        has_more = len(rows) > limit
        selected = rows[:limit]
        selected.reverse()
        return ConversationPage(
            messages=[
                ConversationMessage(
                    thread_id=row[1], turn_id=row[2], message_id=row[3], role=row[4],
                    content=row[5], agent=row[6], agent_display_name=row[7], created_at=row[8],
                )
                for row in selected
            ],
            has_more=has_more,
        )

    def list_conversations(
        self, *, thread_prefix: str, limit: int, before: str | None = None
    ) -> ConversationListPage:
        from psycopg import connect

        with connect(self.conninfo) as conn:
            query = """WITH latest AS (
                    SELECT thread_id, MAX(seq) AS last_seq, COUNT(*) AS message_count
                    FROM axis_conversation_messages WHERE thread_id LIKE %s GROUP BY thread_id
                )
                SELECT latest.thread_id, messages.content, messages.created_at,
                       latest.message_count, latest.last_seq
                FROM latest JOIN axis_conversation_messages AS messages ON messages.seq = latest.last_seq"""
            params: list[Any] = [f"{thread_prefix}%"]
            if before is not None:
                try:
                    cursor = int(before)
                except ValueError:
                    return ConversationListPage(conversations=[], has_more=False, cursor_found=False)
                query += " WHERE latest.last_seq < %s"
                params.append(cursor)
            query += " ORDER BY latest.last_seq DESC LIMIT %s"
            params.append(limit + 1)
            rows = conn.execute(query, params).fetchall()
        has_more = len(rows) > limit
        selected = rows[:limit]
        return ConversationListPage(
            conversations=[
                ConversationSummary(
                    thread_id=row[0], last_message=row[1][:240],
                    last_message_at=row[2], message_count=row[3],
                )
                for row in selected
            ],
            has_more=has_more,
            next_before=str(selected[-1][4]) if has_more and selected else None,
        )


def create_conversation_store(
    *, postgres_url: str | None, path: str | None, retention_days: int | None = None
) -> ConversationStore:
    if postgres_url:
        return PostgresConversationStore(postgres_url, retention_days=retention_days)
    if path:
        return SqliteConversationStore(path, retention_days=retention_days)
    return MemoryConversationStore()
