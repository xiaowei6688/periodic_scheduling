"""Shared SQLite connection settings for framework-owned stores."""
from __future__ import annotations

import sqlite3

SQLITE_TIMEOUT_SECONDS = 30
SQLITE_BUSY_TIMEOUT_MS = SQLITE_TIMEOUT_SECONDS * 1000


def connect_sqlite(path: str, *, check_same_thread: bool = True) -> sqlite3.Connection:
    """Open a connection configured for concurrent framework stores."""
    conn = sqlite3.connect(
        path,
        timeout=SQLITE_TIMEOUT_SECONDS,
        check_same_thread=check_same_thread,
    )
    conn.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS}")
    return conn


def enable_wal(conn: sqlite3.Connection) -> None:
    """Enable WAL once while initializing a framework-owned SQLite database."""
    conn.execute("PRAGMA journal_mode=WAL")
