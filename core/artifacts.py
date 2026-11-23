"""Large structured results stored outside model messages."""
from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from threading import RLock
from typing import Any, Protocol, cast
from uuid import uuid4

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import StructuredTool
from langchain_core.tools import BaseTool

from ._sqlite import connect_sqlite, enable_wal
from .config import Settings

ARTIFACT_REF_KEY = "$artifact_ref"


@dataclass(frozen=True)
class Artifact:
    id: str
    thread_id: str
    run_id: str
    producer: str
    type: str
    schema_version: int
    count: int
    created_at: str
    visible: bool = True


class ArtifactStore(Protocol):
    def begin_run(self, thread_id: str) -> str: ...
    def active_run(self, thread_id: str) -> str | None: ...
    def put(self, thread_id: str, artifact_type: str, data: Any, schema_version: int = 1, *, run_id: str | None = None, producer: str = "", visible: bool = True) -> Artifact: ...
    def list(self, thread_id: str, *, run_id: str | None = None) -> list[Artifact]: ...
    def get_page(self, thread_id: str, artifact_id: str, offset: int, limit: int, *, run_id: str | None = None) -> tuple[list[Any], int] | None: ...
    def get(self, thread_id: str, artifact_id: str, *, run_id: str | None = None) -> tuple[Artifact, Any] | None: ...
    def delete_run(self, thread_id: str, run_id: str) -> None: ...


def artifact_ref(artifact_id: str) -> dict[str, str]:
    """Build a reference that is hydrated only when artifacts are delivered."""
    if not artifact_id:
        raise ValueError("artifact_id 不能为空")
    return {ARTIFACT_REF_KEY: artifact_id}


def materialize_artifact_delivery(
    store: ArtifactStore, thread_id: str, run_id: str
) -> list[tuple[Artifact, Any]]:
    """Hydrate same-run references and return only top-level deliverables."""
    entries: dict[str, tuple[Artifact, Any]] = {}
    for metadata in store.list(thread_id, run_id=run_id):
        entry = store.get(thread_id, metadata.id, run_id=run_id)
        if entry is not None:
            entries[metadata.id] = entry

    referenced: set[str] = set()

    def collect(value: Any) -> None:
        if isinstance(value, dict):
            if set(value) == {ARTIFACT_REF_KEY} and isinstance(value[ARTIFACT_REF_KEY], str):
                referenced.add(value[ARTIFACT_REF_KEY])
                return
            for child in value.values():
                collect(child)
        elif isinstance(value, list):
            for child in value:
                collect(child)

    for _, payload in entries.values():
        collect(payload)

    memo: dict[str, Any] = {}

    def resolve(value: Any, stack: tuple[str, ...]) -> Any:
        if isinstance(value, dict):
            if set(value) == {ARTIFACT_REF_KEY} and isinstance(value[ARTIFACT_REF_KEY], str):
                artifact_id = value[ARTIFACT_REF_KEY]
                if artifact_id in stack:
                    raise ValueError("Artifact 引用存在循环")
                if artifact_id not in entries:
                    raise ValueError(f"Artifact 引用不存在或不属于当前运行：{artifact_id}")
                if artifact_id not in memo:
                    memo[artifact_id] = resolve(entries[artifact_id][1], (*stack, artifact_id))
                return memo[artifact_id]
            return {key: resolve(child, stack) for key, child in value.items()}
        if isinstance(value, list):
            return [resolve(child, stack) for child in value]
        return value

    roots = [
        entry for artifact_id, entry in entries.items()
        if artifact_id not in referenced and entry[0].visible
    ]
    if entries and not roots and any(
        metadata.visible for metadata, _ in entries.values()
    ):
        # Resolving any entry produces the useful cycle/missing-reference error.
        first_id = next(iter(entries))
        resolve(entries[first_id][1], (first_id,))
        raise ValueError("Artifact 没有可交付的根节点")
    return [(metadata, resolve(payload, (metadata.id,))) for metadata, payload in roots]


class MemoryArtifactStore:
    """Process-local implementation; inject a durable store for production deployments."""

    def __init__(self) -> None:
        self._items: dict[str, tuple[Artifact, Any]] = {}
        self._active_runs: dict[str, str] = {}
        self._lock = RLock()

    def begin_run(self, thread_id: str) -> str:
        with self._lock:
            return self._active_runs.setdefault(thread_id, uuid4().hex)

    def active_run(self, thread_id: str) -> str | None:
        with self._lock:
            return self._active_runs.get(thread_id)

    def put(self, thread_id: str, artifact_type: str, data: Any, schema_version: int = 1, *, run_id: str | None = None, producer: str = "", visible: bool = True) -> Artifact:
        if not thread_id or not artifact_type or schema_version < 1:
            raise ValueError("thread_id、artifact_type 和正数 schema_version 为必填")
        # Validate JSON compatibility and detach caller-owned mutable values.
        payload = json.loads(json.dumps(data, ensure_ascii=False))
        count = len(payload) if isinstance(payload, list) else 1
        item = Artifact(uuid4().hex, thread_id, run_id or self.begin_run(thread_id), producer, artifact_type, schema_version, count,
                        datetime.now(timezone.utc).isoformat(), visible)
        with self._lock:
            self._items[item.id] = (item, payload)
        return item

    def list(self, thread_id: str, *, run_id: str | None = None) -> list[Artifact]:
        with self._lock:
            return [item for item, _ in self._items.values() if item.thread_id == thread_id and (run_id is None or item.run_id == run_id)]

    def get_page(self, thread_id: str, artifact_id: str, offset: int, limit: int, *, run_id: str | None = None) -> tuple[list[Any], int] | None:
        with self._lock:
            entry = self._items.get(artifact_id)
            if entry is None or entry[0].thread_id != thread_id or (run_id is not None and entry[0].run_id != run_id):
                return None
            payload = entry[1]
        rows = payload if isinstance(payload, list) else [payload]
        return rows[offset:offset + limit], len(rows)

    def get(self, thread_id: str, artifact_id: str, *, run_id: str | None = None) -> tuple[Artifact, Any] | None:
        with self._lock:
            entry = self._items.get(artifact_id)
            if entry is None or entry[0].thread_id != thread_id or (run_id is not None and entry[0].run_id != run_id):
                return None
            return entry

    def delete_run(self, thread_id: str, run_id: str) -> None:
        with self._lock:
            for artifact_id in [key for key, (item, _) in self._items.items() if item.thread_id == thread_id and item.run_id == run_id]:
                del self._items[artifact_id]
            if self._active_runs.get(thread_id) == run_id:
                del self._active_runs[thread_id]


class SqliteArtifactStore:
    """Durable single-node store suitable for local deployments and modest datasets."""

    def __init__(self, path: str) -> None:
        self.path = path or ""
        if not self.path:
            raise ValueError("artifact_db_path 不能为空")
        os.makedirs(os.path.dirname(os.path.abspath(self.path)) or ".", exist_ok=True)
        with self._connect() as conn:
            enable_wal(conn)
            conn.execute(
                """CREATE TABLE IF NOT EXISTS axis_artifacts (
                    id TEXT PRIMARY KEY, thread_id TEXT NOT NULL, run_id TEXT NOT NULL DEFAULT '',
                    producer TEXT NOT NULL DEFAULT '', type TEXT NOT NULL,
                    schema_version INTEGER NOT NULL, count INTEGER NOT NULL,
                    created_at TEXT NOT NULL, visible INTEGER NOT NULL DEFAULT 1,
                    payload TEXT NOT NULL
                )"""
            )
            columns = {row[1] for row in conn.execute("PRAGMA table_info(axis_artifacts)")}
            if "run_id" not in columns:
                conn.execute("ALTER TABLE axis_artifacts ADD COLUMN run_id TEXT NOT NULL DEFAULT ''")
                conn.execute("UPDATE axis_artifacts SET run_id = thread_id WHERE run_id = ''")
            if "producer" not in columns:
                conn.execute("ALTER TABLE axis_artifacts ADD COLUMN producer TEXT NOT NULL DEFAULT ''")
            if "visible" not in columns:
                conn.execute("ALTER TABLE axis_artifacts ADD COLUMN visible INTEGER NOT NULL DEFAULT 1")
            conn.execute("CREATE INDEX IF NOT EXISTS axis_artifacts_thread ON axis_artifacts(thread_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS axis_artifacts_run ON axis_artifacts(thread_id, run_id)")
            conn.execute("CREATE TABLE IF NOT EXISTS axis_artifact_runs (thread_id TEXT PRIMARY KEY, run_id TEXT NOT NULL)")

    def _connect(self) -> sqlite3.Connection:
        conn = connect_sqlite(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def begin_run(self, thread_id: str) -> str:
        with self._connect() as conn:
            conn.execute("INSERT OR IGNORE INTO axis_artifact_runs VALUES (?, ?)", (thread_id, uuid4().hex))
            row = conn.execute("SELECT run_id FROM axis_artifact_runs WHERE thread_id = ?", (thread_id,)).fetchone()
        return str(row[0])

    def active_run(self, thread_id: str) -> str | None:
        with self._connect() as conn:
            row = conn.execute("SELECT run_id FROM axis_artifact_runs WHERE thread_id = ?", (thread_id,)).fetchone()
        return str(row[0]) if row else None

    def put(self, thread_id: str, artifact_type: str, data: Any, schema_version: int = 1, *, run_id: str | None = None, producer: str = "", visible: bool = True) -> Artifact:
        if not thread_id or not artifact_type or schema_version < 1:
            raise ValueError("thread_id、artifact_type 和正数 schema_version 为必填")
        payload = json.dumps(data, ensure_ascii=False)
        normalized = json.loads(payload)
        count = len(normalized) if isinstance(normalized, list) else 1
        item = Artifact(uuid4().hex, thread_id, run_id or self.begin_run(thread_id), producer, artifact_type, schema_version, count,
                        datetime.now(timezone.utc).isoformat(), visible)
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO axis_artifacts (id, thread_id, run_id, producer, type, schema_version, count, created_at, visible, payload) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (item.id, item.thread_id, item.run_id, item.producer, item.type, item.schema_version,
                 item.count, item.created_at, int(item.visible), payload),
            )
        return item

    @staticmethod
    def _artifact(row: sqlite3.Row) -> Artifact:
        return Artifact(row["id"], row["thread_id"], row["run_id"], row["producer"], row["type"], row["schema_version"],
                        row["count"], row["created_at"], bool(row["visible"]))

    def list(self, thread_id: str, *, run_id: str | None = None) -> list[Artifact]:
        with self._connect() as conn:
            if run_id is None:
                rows = conn.execute(
                    "SELECT id, thread_id, run_id, producer, type, schema_version, count, created_at, visible "
                    "FROM axis_artifacts WHERE thread_id = ? ORDER BY rowid", (thread_id,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT id, thread_id, run_id, producer, type, schema_version, count, created_at, visible "
                    "FROM axis_artifacts WHERE thread_id = ? AND run_id = ? ORDER BY rowid", (thread_id, run_id),
                ).fetchall()
        return [self._artifact(row) for row in rows]

    def get_page(self, thread_id: str, artifact_id: str, offset: int, limit: int, *, run_id: str | None = None) -> tuple[list[Any], int] | None:
        with self._connect() as conn:
            if run_id is None:
                row = conn.execute(
                    "SELECT id, thread_id, run_id, producer, type, schema_version, count, created_at, visible, payload "
                    "FROM axis_artifacts WHERE id = ? AND thread_id = ?", (artifact_id, thread_id),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT id, thread_id, run_id, producer, type, schema_version, count, created_at, visible, payload "
                    "FROM axis_artifacts WHERE id = ? AND thread_id = ? AND run_id = ?", (artifact_id, thread_id, run_id),
                ).fetchone()
        if row is None:
            return None
        payload = json.loads(row["payload"])
        rows = payload if isinstance(payload, list) else [payload]
        return rows[offset:offset + limit], len(rows)

    def get(self, thread_id: str, artifact_id: str, *, run_id: str | None = None) -> tuple[Artifact, Any] | None:
        with self._connect() as conn:
            if run_id is None:
                row = conn.execute(
                    "SELECT id, thread_id, run_id, producer, type, schema_version, count, created_at, visible, payload "
                    "FROM axis_artifacts WHERE id = ? AND thread_id = ?", (artifact_id, thread_id),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT id, thread_id, run_id, producer, type, schema_version, count, created_at, visible, payload "
                    "FROM axis_artifacts WHERE id = ? AND thread_id = ? AND run_id = ?", (artifact_id, thread_id, run_id),
                ).fetchone()
        if row is None:
            return None
        return self._artifact(row), json.loads(row["payload"])

    def delete_run(self, thread_id: str, run_id: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM axis_artifacts WHERE thread_id = ? AND run_id = ?", (thread_id, run_id))
            conn.execute("DELETE FROM axis_artifact_runs WHERE thread_id = ? AND run_id = ?", (thread_id, run_id))


class PostgresArtifactStore:
    """Shared PostgreSQL implementation for multi-process deployments."""

    def __init__(self, conninfo: str) -> None:
        if not conninfo:
            raise ValueError("PostgreSQL conninfo 不能为空")
        self.conninfo = conninfo
        from psycopg import connect

        with connect(conninfo) as conn:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS axis_artifacts (
                    seq BIGSERIAL UNIQUE, id TEXT PRIMARY KEY,
                    thread_id TEXT NOT NULL, run_id TEXT NOT NULL,
                    producer TEXT NOT NULL DEFAULT '', type TEXT NOT NULL,
                    schema_version INTEGER NOT NULL, count INTEGER NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL,
                    visible BOOLEAN NOT NULL DEFAULT TRUE, payload JSONB NOT NULL
                )"""
            )
            conn.execute(
                "ALTER TABLE axis_artifacts ADD COLUMN IF NOT EXISTS visible BOOLEAN NOT NULL DEFAULT TRUE"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS axis_artifacts_thread ON axis_artifacts(thread_id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS axis_artifacts_run ON axis_artifacts(thread_id, run_id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS axis_artifacts_thread_seq ON axis_artifacts(thread_id, seq)"
            )
            conn.execute(
                """CREATE TABLE IF NOT EXISTS axis_artifact_runs (
                    thread_id TEXT PRIMARY KEY, run_id TEXT NOT NULL
                )"""
            )

    def begin_run(self, thread_id: str) -> str:
        from psycopg import connect

        with connect(self.conninfo) as conn:
            conn.execute(
                "INSERT INTO axis_artifact_runs (thread_id, run_id) VALUES (%s, %s) "
                "ON CONFLICT(thread_id) DO NOTHING",
                (thread_id, uuid4().hex),
            )
            row = conn.execute(
                "SELECT run_id FROM axis_artifact_runs WHERE thread_id = %s", (thread_id,)
            ).fetchone()
        if row is None:
            raise RuntimeError("无法创建或读取 Artifact run")
        return str(row[0])

    def active_run(self, thread_id: str) -> str | None:
        from psycopg import connect

        with connect(self.conninfo) as conn:
            row = conn.execute(
                "SELECT run_id FROM axis_artifact_runs WHERE thread_id = %s", (thread_id,)
            ).fetchone()
        return str(row[0]) if row else None

    def put(
        self,
        thread_id: str,
        artifact_type: str,
        data: Any,
        schema_version: int = 1,
        *,
        run_id: str | None = None,
        producer: str = "",
        visible: bool = True,
    ) -> Artifact:
        if not thread_id or not artifact_type or schema_version < 1:
            raise ValueError("thread_id、artifact_type 和正数 schema_version 为必填")
        from psycopg import connect
        from psycopg.types.json import Jsonb

        normalized = json.loads(json.dumps(data, ensure_ascii=False))
        count = len(normalized) if isinstance(normalized, list) else 1
        item = Artifact(
            uuid4().hex,
            thread_id,
            run_id or self.begin_run(thread_id),
            producer,
            artifact_type,
            schema_version,
            count,
            datetime.now(timezone.utc).isoformat(),
            visible,
        )
        with connect(self.conninfo) as conn:
            conn.execute(
                """INSERT INTO axis_artifacts
                    (id, thread_id, run_id, producer, type, schema_version, count, created_at, visible, payload)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (
                    item.id, item.thread_id, item.run_id, item.producer, item.type,
                    item.schema_version,
                    item.count,
                    datetime.fromisoformat(item.created_at),
                    item.visible,
                    Jsonb(normalized),
                ),
            )
        return item

    @staticmethod
    def _artifact(row: Any) -> Artifact:
        created_at = row[7]
        if isinstance(created_at, datetime):
            created_at = created_at.isoformat()
        return Artifact(
            id=row[0], thread_id=row[1], run_id=row[2], producer=row[3], type=row[4],
            schema_version=row[5], count=row[6], created_at=str(created_at), visible=row[8],
        )

    def list(self, thread_id: str, *, run_id: str | None = None) -> list[Artifact]:
        from psycopg import connect

        query = (
            "SELECT id, thread_id, run_id, producer, type, schema_version, count, created_at, visible "
            "FROM axis_artifacts WHERE thread_id = %s"
        )
        params: tuple[Any, ...] = (thread_id,)
        if run_id is not None:
            query += " AND run_id = %s"
            params += (run_id,)
        query += " ORDER BY seq"
        with connect(self.conninfo) as conn:
            rows = conn.execute(query, params).fetchall()
        return [self._artifact(row) for row in rows]

    def _get_row(
        self, thread_id: str, artifact_id: str, run_id: str | None = None
    ) -> Any | None:
        from psycopg import connect

        query = (
            "SELECT id, thread_id, run_id, producer, type, schema_version, count, created_at, visible, payload "
            "FROM axis_artifacts WHERE id = %s AND thread_id = %s"
        )
        params: tuple[Any, ...] = (artifact_id, thread_id)
        if run_id is not None:
            query += " AND run_id = %s"
            params += (run_id,)
        with connect(self.conninfo) as conn:
            return conn.execute(query, params).fetchone()

    def get_page(
        self,
        thread_id: str,
        artifact_id: str,
        offset: int,
        limit: int,
        *,
        run_id: str | None = None,
    ) -> tuple[list[Any], int] | None:
        row = self._get_row(thread_id, artifact_id, run_id)
        if row is None:
            return None
        payload = row[9]
        rows = payload if isinstance(payload, list) else [payload]
        return rows[offset:offset + limit], len(rows)

    def get(
        self, thread_id: str, artifact_id: str, *, run_id: str | None = None
    ) -> tuple[Artifact, Any] | None:
        row = self._get_row(thread_id, artifact_id, run_id)
        if row is None:
            return None
        return self._artifact(row), row[9]

    def delete_run(self, thread_id: str, run_id: str) -> None:
        from psycopg import connect

        with connect(self.conninfo) as conn:
            conn.execute(
                "DELETE FROM axis_artifacts WHERE thread_id = %s AND run_id = %s",
                (thread_id, run_id),
            )
            conn.execute(
                "DELETE FROM axis_artifact_runs WHERE thread_id = %s AND run_id = %s",
                (thread_id, run_id),
            )


def create_artifact_store(settings: Settings, store: ArtifactStore | None = None) -> ArtifactStore:
    if store is not None:
        return store
    if settings.postgres_url:
        return PostgresArtifactStore(settings.postgres_url)
    if settings.artifact_db_path:
        return SqliteArtifactStore(settings.artifact_db_path)
    return MemoryArtifactStore()


def _make_store_binding(store: ArtifactStore | None) -> dict[str, ArtifactStore | None]:
    binding: dict[str, ArtifactStore | None] = {"store": store}
    return binding


def _bound_store(binding: dict[str, ArtifactStore | None]) -> ArtifactStore:
    store = binding["store"]
    if store is None:
        raise RuntimeError("Artifact 工具尚未绑定运行时的 ArtifactStore")
    return store


def bind_artifact_store(tool: BaseTool, store: ArtifactStore, producer: str) -> BaseTool:
    rebind = getattr(tool, "_axis_rebind_artifact_tool", None)
    if rebind is None:
        return tool
    rebound = rebind(store, producer)
    metadata = {
        **(getattr(rebound, "metadata", None) or {}),
        **(getattr(tool, "metadata", None) or {}),
    }
    return rebound.model_copy(update={"metadata": metadata})


def make_publish_artifact_tool(
    store: ArtifactStore | None = None,
    *,
    name: str = "publish_artifact",
    producer: str | None = None,
):
    """Store full JSON data and return only a compact, thread-scoped reference to the model."""
    binding = _make_store_binding(store)
    def publish(
        artifact_type: str,
        data: Any,
        summary: str,
        schema_version: int = 1,
        config: RunnableConfig = cast(RunnableConfig, None),
    ) -> str:
        configurable = (config or {}).get("configurable") or {}
        thread_id = str(configurable.get("thread_id", ""))
        run_id = str(configurable.get("artifact_run_id", ""))
        producer_name = producer or str(((config or {}).get("metadata") or {}).get("langgraph_node", name))
        artifact = _bound_store(binding).put(thread_id, artifact_type, data, schema_version, run_id=run_id, producer=producer_name)
        return json.dumps({"artifact": asdict(artifact), "summary": summary}, ensure_ascii=False)

    wrapped = StructuredTool.from_function(
        publish,
        name=name,
        description="将完整结构化结果保存为可交付数据集，仅向模型返回摘要和引用。data 必须是 JSON 数据。",
    )
    object.__setattr__(
        wrapped,
        "_axis_rebind_artifact_tool",
        lambda runtime_store, runtime_producer: make_publish_artifact_tool(
            runtime_store, name=name, producer=runtime_producer
        ),
    )
    return wrapped


def make_artifact_result_tool(
    tool: BaseTool,
    store: ArtifactStore | None = None,
    *,
    artifact_type: str,
    summary: Callable[[Any], str],
    schema_version: int = 1,
    producer: str | None = None,
    visible: bool = True,
):
    """Wrap a data-producing tool so its full result bypasses the model context."""
    if not artifact_type or schema_version < 1:
        raise ValueError("artifact_type 和正数 schema_version 为必填")

    binding = _make_store_binding(store)
    internal_tool = tool.model_copy(
        update={
            "metadata": {
                **(getattr(tool, "metadata", None) or {}),
                "axis_internal_tool": True,
            }
        }
    )

    def run(config: RunnableConfig = cast(RunnableConfig, None), **kwargs: Any) -> str:
        configurable = (config or {}).get("configurable") or {}
        thread_id = str(configurable.get("thread_id", ""))
        run_id = str(configurable.get("artifact_run_id", ""))
        result = internal_tool.invoke(kwargs, config=config)
        producer_name = producer or str(((config or {}).get("metadata") or {}).get("langgraph_node", internal_tool.name))
        artifact = _bound_store(binding).put(
            thread_id, artifact_type, result, schema_version,
            run_id=run_id, producer=producer_name, visible=visible,
        )
        return json.dumps(
            {"artifact": asdict(artifact), "summary": summary(result)}, ensure_ascii=False
        )

    wrapped = StructuredTool.from_function(
        run,
        name=tool.name,
        description=tool.description,
        args_schema=tool.args_schema,
        metadata=getattr(tool, "metadata", None),
    )
    object.__setattr__(
        wrapped,
        "_axis_rebind_artifact_tool",
        lambda runtime_store, runtime_producer: make_artifact_result_tool(
            tool,
            runtime_store,
            artifact_type=artifact_type,
            summary=summary,
            schema_version=schema_version,
            producer=runtime_producer,
            visible=visible,
        ),
    )
    return wrapped

