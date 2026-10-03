"""SQLite storage.

Two properties the spec depends on come from here:

* every write runs inside ``BEGIN IMMEDIATE``, which takes the write lock before
  the first statement executes — so concurrent bookings serialise instead of both
  reading "table free" and both writing;
* :meth:`Database.transaction` is re-entrant, so a batch of reservation moves
  composes the same primitives as a single booking and still commits as one unit.

Connections are thread-local and WAL is on, so the HTTP thread pool can read
while a writer holds the lock. State is ephemeral: the database is a file inside
the container and need not survive a restart.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


class Database:
    def __init__(self, path: str, *, busy_timeout_ms: int = 8_000) -> None:
        self.path = path
        self.busy_timeout_ms = busy_timeout_ms
        self._local = threading.local()
        parent = Path(path).parent
        if str(parent) not in ("", "."):
            parent.mkdir(parents=True, exist_ok=True)

    def connection(self) -> sqlite3.Connection:
        conn: sqlite3.Connection | None = getattr(self._local, "conn", None)
        if conn is not None:
            return conn
        conn = sqlite3.connect(
            self.path,
            timeout=self.busy_timeout_ms / 1000.0,
            isolation_level=None,  # explicit transaction control only
            check_same_thread=False,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute(f"PRAGMA busy_timeout={self.busy_timeout_ms}")
        self._local.conn = conn
        self._local.depth = 0
        return conn

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Re-entrant write transaction: the outermost call commits or rolls back."""
        conn = self.connection()
        depth = getattr(self._local, "depth", 0)
        if depth > 0:
            self._local.depth = depth + 1
            try:
                yield conn
            finally:
                self._local.depth = depth
            return

        self._local.depth = 1
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except BaseException:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:  # pragma: no cover
                pass
            self._local.depth = 0
            raise
        conn.execute("COMMIT")
        self._local.depth = 0

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        """Read-only snapshot; joins an enclosing write transaction if there is one."""
        conn = self.connection()
        if getattr(self._local, "depth", 0) > 0:
            yield conn
            return
        conn.execute("BEGIN")
        try:
            yield conn
        finally:
            conn.execute("ROLLBACK")

    def all(self, sql: str, params: Sequence[Any] = ()) -> list[dict]:
        with self.read() as conn:
            return [dict(row) for row in conn.execute(sql, params).fetchall()]

    def migrate(self) -> list[str]:
        """Apply ``*.sql`` migrations in filename order."""
        conn = self.connection()
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            " name TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        applied = {r["name"] for r in conn.execute("SELECT name FROM schema_migrations")}
        newly: list[str] = []
        for sql_file in sorted(MIGRATIONS_DIR.glob("*.sql")):
            if sql_file.name in applied:
                continue
            conn.executescript(sql_file.read_text())
            conn.execute(
                "INSERT INTO schema_migrations (name, applied_at) VALUES (?, datetime('now'))",
                (sql_file.name,),
            )
            newly.append(sql_file.name)
        return newly
