"""Direct-SQLite helpers for the edge event recorder's database.

Each helper opens its own ``sqlite3`` connection to the file, seeds or reads
rows, and closes it, the same way ``tests/cloud/sql_helpers.py`` does for the
cloud store. ``metadata`` is stored verbatim: pass the JSON text the column
holds (or ``None`` for NULL), exactly as the recorder writes it.
"""

from collections.abc import Iterable
from pathlib import Path

import sqlite3

DbPath = str | Path

_EVENT_INSERT = (
    "INSERT INTO events (event_type, timestamp, value, metadata) VALUES (?, ?, ?, ?)"
)


def insert_event(db: DbPath, event_type, ts, value, metadata=None) -> None:
    """Insert one row into ``events``."""
    insert_events(db, [(event_type, ts, value, metadata)])


def insert_events(db: DbPath, rows: Iterable[tuple]) -> None:
    """Insert many ``events`` rows in one transaction.

    Each row is ``(event_type, timestamp, value)`` or
    ``(event_type, timestamp, value, metadata)``; a missing metadata is NULL.
    """
    padded = [tuple(row) + (None,) * (4 - len(row)) for row in rows]
    conn = sqlite3.connect(db)
    try:
        with conn:
            conn.executemany(_EVENT_INSERT, padded)
    finally:
        conn.close()


def db_rows(db: DbPath, sql: str, params: tuple = ()) -> list[tuple]:
    """Run one query on its own connection and return every row."""
    conn = sqlite3.connect(db)
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def user_version(db: DbPath) -> int:
    """The database's ``PRAGMA user_version`` (its schema migration level)."""
    return db_rows(db, "PRAGMA user_version")[0][0]
