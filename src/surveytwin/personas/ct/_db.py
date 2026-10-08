"""SQLite-only stand-in for the website repository's database helpers.

The ported bank catalog calls ``db_connection``, ``db_path_override``,
``active_db_override`` and ``utcnow_iso``. In the library every bank lives in
its own local SQLite file, so only the website's SQLite override path is kept.
"""

from __future__ import annotations

import contextvars
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterable, Iterator

_DB_PATH_OVERRIDE: contextvars.ContextVar[tuple[str, bool] | None] = contextvars.ContextVar(
    "surveytwin_bank_db", default=None
)


@contextmanager
def db_path_override(path: str | None, *, readonly: bool = False) -> Iterator[None]:
    if not path:
        yield
        return
    token = _DB_PATH_OVERRIDE.set((str(path), readonly))
    try:
        yield
    finally:
        _DB_PATH_OVERRIDE.reset(token)


def active_db_override() -> tuple[str, bool] | None:
    return _DB_PATH_OVERRIDE.get()


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class _Connection:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def execute(self, query: str, params: Iterable[Any] = ()) -> Any:
        return self._conn.execute(query, tuple(params))

    def executemany(self, query: str, rows: Iterable[Iterable[Any]]) -> Any:
        return self._conn.executemany(query, [tuple(r) for r in rows])

    def executescript(self, script: str) -> None:
        self._conn.executescript(script)

    def commit(self) -> None:
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()


@contextmanager
def db_connection() -> Iterator[Any]:
    override = _DB_PATH_OVERRIDE.get()
    if override is None:
        raise RuntimeError("No bank database is open (internal error: db_path_override missing)")
    path, readonly = override
    if readonly:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    else:
        conn = sqlite3.connect(path)
        conn.execute("PRAGMA synchronous=OFF")
    conn.row_factory = sqlite3.Row
    wrapped = _Connection(conn)
    try:
        yield wrapped
        if not readonly:
            wrapped.commit()
    finally:
        wrapped.close()
