"""SQLiteへの接続とマイグレーション(設計書 §2.1)。

- WALモードで使い、接続はスレッドごとに持つ
- 書き込みは短いトランザクションにまとめる(`with db.tx() as c:`)
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from remi.store.migrations import MIGRATIONS, SCHEMA_VERSION

log = logging.getLogger(__name__)


class SchemaTooNewError(RuntimeError):
    """DBのスキーマがアプリより新しい(新しいバージョンのRemiで作られた)。"""

    def __init__(self, found: int, supported: int) -> None:
        super().__init__(f"database schema {found} is newer than supported {supported}")
        self.found = found
        self.supported = supported


class Database:
    def __init__(self, path: Path | str) -> None:
        self.path = str(path)
        self._local = threading.local()
        self._all: list[sqlite3.Connection] = []
        self._lock = threading.Lock()

    # --- 接続 -------------------------------------------------------------
    def conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "conn", None)
        if c is None:
            c = sqlite3.connect(self.path, timeout=10, isolation_level=None)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA foreign_keys = ON")
            c.execute("PRAGMA busy_timeout = 5000")
            if self.path != ":memory:":
                c.execute("PRAGMA journal_mode = WAL")
                c.execute("PRAGMA synchronous = NORMAL")
            self._local.conn = c
            with self._lock:
                self._all.append(c)
        return c

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """書き込み用のトランザクション。例外が起きたらロールバックする。"""
        c = self.conn()
        c.execute("BEGIN IMMEDIATE")
        try:
            yield c
        except BaseException:
            c.execute("ROLLBACK")
            raise
        else:
            c.execute("COMMIT")

    def query(self, sql: str, params: tuple | dict = ()) -> list[sqlite3.Row]:
        return self.conn().execute(sql, params).fetchall()

    def query_one(self, sql: str, params: tuple | dict = ()) -> sqlite3.Row | None:
        return self.conn().execute(sql, params).fetchone()

    def close_all(self) -> None:
        with self._lock:
            for c in self._all:
                try:
                    c.close()
                except sqlite3.Error:
                    pass
            self._all.clear()
        self._local = threading.local()

    # --- マイグレーション --------------------------------------------------
    def schema_version(self) -> int:
        return self.conn().execute("PRAGMA user_version").fetchone()[0]

    def migrate(self) -> int:
        current = self.schema_version()
        if current > SCHEMA_VERSION:
            raise SchemaTooNewError(current, SCHEMA_VERSION)
        for version in range(current + 1, SCHEMA_VERSION + 1):
            log.info("migrating database schema to %d", version)
            c = self.conn()
            c.execute("BEGIN IMMEDIATE")
            try:
                for stmt in _split_sql(MIGRATIONS[version - 1]):
                    c.execute(stmt)
                c.execute(f"PRAGMA user_version = {version}")
            except BaseException:
                c.execute("ROLLBACK")
                raise
            c.execute("COMMIT")
        return SCHEMA_VERSION


def _split_sql(script: str) -> list[str]:
    return [s.strip() for s in script.split(";") if s.strip()]


def open_database(path: Path | str) -> Database:
    db = Database(path)
    db.migrate()
    return db
