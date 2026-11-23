"""Cross-turn temporary JSON state, separate from response artifacts."""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from threading import RLock
from typing import Any, Protocol

from langchain_core.tools import BaseTool

from ._sqlite import connect_sqlite, enable_wal
from .config import Settings


class WorkflowStateStore(Protocol):
    def save(self, thread_id: str, namespace: str, key: str, data: Any, *, ttl_seconds: int = 86400) -> None: ...
    def load(self, thread_id: str, namespace: str, key: str) -> Any | None: ...
    def clear(self, thread_id: str, namespace: str, key: str) -> None: ...


def bind_workflow_state_store(tool: BaseTool, store: WorkflowStateStore) -> BaseTool:
    rebind = getattr(tool, "_axis_rebind_workflow_state_tool", None)
    if rebind is None:
        return tool
    rebound = rebind(store)
    return rebound.model_copy(update={
        "metadata": {
            **(getattr(rebound, "metadata", None) or {}),
            **(getattr(tool, "metadata", None) or {}),
        }
    })


def _payload(data: Any, ttl_seconds: int) -> tuple[str, str]:
    if ttl_seconds <= 0:
        raise ValueError("ttl_seconds 必须大于零")
    return json.dumps(data, ensure_ascii=False), (datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)).isoformat()


class MemoryWorkflowStateStore:
    def __init__(self) -> None:
        self._items: dict[tuple[str, str, str], tuple[str, str]] = {}
        self._lock = RLock()

    def save(self, thread_id: str, namespace: str, key: str, data: Any, *, ttl_seconds: int = 86400) -> None:
        value = _payload(data, ttl_seconds)
        with self._lock:
            now = datetime.now(timezone.utc)
            for expired_key, (_, expiry) in list(self._items.items()):
                if datetime.fromisoformat(expiry) <= now:
                    del self._items[expired_key]
            self._items[thread_id, namespace, key] = value

    def load(self, thread_id: str, namespace: str, key: str) -> Any | None:
        with self._lock:
            item = self._items.get((thread_id, namespace, key))
            if item is None:
                return None
            if datetime.fromisoformat(item[1]) <= datetime.now(timezone.utc):
                del self._items[thread_id, namespace, key]
                return None
            return json.loads(item[0])

    def clear(self, thread_id: str, namespace: str, key: str) -> None:
        with self._lock:
            self._items.pop((thread_id, namespace, key), None)


class SqliteWorkflowStateStore:
    def __init__(self, path: str) -> None:
        self.path = path
        os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
        with self._connect() as conn:
            enable_wal(conn)
            conn.execute("CREATE TABLE IF NOT EXISTS axis_workflow_state (thread_id TEXT NOT NULL, namespace TEXT NOT NULL, state_key TEXT NOT NULL, payload TEXT NOT NULL, expires_at TEXT NOT NULL, PRIMARY KEY (thread_id, namespace, state_key))")

    def _connect(self) -> sqlite3.Connection:
        return connect_sqlite(self.path)

    def save(self, thread_id: str, namespace: str, key: str, data: Any, *, ttl_seconds: int = 86400) -> None:
        payload, expiry = _payload(data, ttl_seconds)
        with self._connect() as conn:
            conn.execute("DELETE FROM axis_workflow_state WHERE expires_at <= ?", (datetime.now(timezone.utc).isoformat(),))
            conn.execute("INSERT INTO axis_workflow_state VALUES (?, ?, ?, ?, ?) ON CONFLICT(thread_id, namespace, state_key) DO UPDATE SET payload=excluded.payload, expires_at=excluded.expires_at", (thread_id, namespace, key, payload, expiry))

    def load(self, thread_id: str, namespace: str, key: str) -> Any | None:
        with self._connect() as conn:
            conn.execute("DELETE FROM axis_workflow_state WHERE expires_at <= ?", (datetime.now(timezone.utc).isoformat(),))
            row = conn.execute("SELECT payload FROM axis_workflow_state WHERE thread_id=? AND namespace=? AND state_key=?", (thread_id, namespace, key)).fetchone()
        return json.loads(row[0]) if row else None

    def clear(self, thread_id: str, namespace: str, key: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM axis_workflow_state WHERE thread_id=? AND namespace=? AND state_key=?", (thread_id, namespace, key))


class PostgresWorkflowStateStore:
    def __init__(self, conninfo: str) -> None:
        self.conninfo = conninfo
        from psycopg import connect
        with connect(conninfo) as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS axis_workflow_state (thread_id TEXT NOT NULL, namespace TEXT NOT NULL, state_key TEXT NOT NULL, payload JSONB NOT NULL, expires_at TIMESTAMPTZ NOT NULL, PRIMARY KEY (thread_id, namespace, state_key))")

    def save(self, thread_id: str, namespace: str, key: str, data: Any, *, ttl_seconds: int = 86400) -> None:
        from psycopg import connect
        from psycopg.types.json import Jsonb
        payload, expiry = _payload(data, ttl_seconds)
        with connect(self.conninfo) as conn:
            conn.execute("DELETE FROM axis_workflow_state WHERE expires_at <= now()")
            conn.execute("INSERT INTO axis_workflow_state VALUES (%s, %s, %s, %s, %s) ON CONFLICT(thread_id, namespace, state_key) DO UPDATE SET payload=excluded.payload, expires_at=excluded.expires_at", (thread_id, namespace, key, Jsonb(json.loads(payload)), expiry))

    def load(self, thread_id: str, namespace: str, key: str) -> Any | None:
        from psycopg import connect
        with connect(self.conninfo) as conn:
            conn.execute("DELETE FROM axis_workflow_state WHERE expires_at <= now()")
            row = conn.execute("SELECT payload FROM axis_workflow_state WHERE thread_id=%s AND namespace=%s AND state_key=%s", (thread_id, namespace, key)).fetchone()
        return row[0] if row else None

    def clear(self, thread_id: str, namespace: str, key: str) -> None:
        from psycopg import connect
        with connect(self.conninfo) as conn:
            conn.execute("DELETE FROM axis_workflow_state WHERE thread_id=%s AND namespace=%s AND state_key=%s", (thread_id, namespace, key))


def create_workflow_state_store(settings: Settings, store: WorkflowStateStore | None = None) -> WorkflowStateStore:
    if store is not None:
        return store
    if settings.postgres_url:
        return PostgresWorkflowStateStore(settings.postgres_url)
    if settings.workflow_state_db_path:
        return SqliteWorkflowStateStore(settings.workflow_state_db_path)
    return MemoryWorkflowStateStore()
