"""Direct-SQLite helpers for cloud tests.

Each helper opens its own ``sqlite3`` connection to the database file, so tests
read and seed state without reaching into the repository's private connection.
"""

import sqlite3
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from fastapi.testclient import TestClient

from ice_cloud.store import AlertEventRow, AlertState, SqliteRepository, StoredEvent

# The file name ``create_app`` gives the cloud database inside ``data_dir``.
CLOUD_DB_NAME = "cloud.db"


def cloud_db(cloud: TestClient) -> Path:
    """The cloud app's SQLite file, from its public settings."""
    return cloud.app.state.settings.data_dir / CLOUD_DB_NAME


def sql_rows(db_path: Path, sql: str, params: tuple = ()) -> list[tuple]:
    """Run one read-only query against the database file on its own connection."""
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def sql_exec(db_path: Path, sql: str, params: tuple = ()) -> None:
    """Run one write statement on its own connection (test setup only)."""
    conn = sqlite3.connect(db_path)
    try:
        with conn:
            conn.execute(sql, params)
    finally:
        conn.close()


def account_id_by_email(db_path: Path, email: str) -> str | None:
    rows = sql_rows(
        db_path, "SELECT id FROM accounts WHERE email = ?", (email.lower(),)
    )
    return rows[0][0] if rows else None


def consumed_hashes(db_path: Path, fingerprint: str) -> set[str]:
    rows = sql_rows(
        db_path,
        "SELECT hash FROM consumed_claim_hashes WHERE fingerprint = ?",
        (fingerprint,),
    )
    return {r[0] for r in rows}


def is_muted_sql(db_path: Path, account_id: str, fingerprint: str) -> bool:
    rows = sql_rows(
        db_path,
        "SELECT muted FROM alert_prefs WHERE account_id = ? AND fingerprint = ?",
        (account_id, fingerprint),
    )
    return bool(rows) and bool(rows[0][0])


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def grant_tech(
    db_path: Path, account_id: str, fingerprint: str, granted_by: str, now: datetime
) -> None:
    """Seed a tech membership (idempotent, like the old repository method)."""
    sql_exec(
        db_path,
        "INSERT OR IGNORE INTO memberships"
        " (account_id, fingerprint, role, granted_by, granted_at)"
        " VALUES (?, ?, 'tech', ?, ?)",
        (account_id, fingerprint, granted_by, _iso(now)),
    )


def put_alert_state(db_path: Path, state: AlertState) -> None:
    """Seed (upsert) one machine's alert state through the public pass API."""
    SqliteRepository(db_path).record_alert_pass([], state)


def record_alert_events(
    db_path: Path, events: Sequence[AlertEventRow]
) -> list[StoredEvent]:
    """Seed alert events in order; return them with their row ids."""
    stored: list[StoredEvent] = []
    conn = sqlite3.connect(db_path)
    try:
        with conn:
            for e in events:
                cur = conn.execute(
                    'INSERT INTO alert_events (fingerprint, kind, at, "key", code,'
                    " severity, gate, description, sku, product)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        e.fingerprint,
                        e.kind,
                        _iso(e.at),
                        e.key,
                        e.code,
                        e.severity,
                        e.gate,
                        e.description,
                        e.sku,
                        e.product,
                    ),
                )
                stored.append(
                    StoredEvent(
                        fingerprint=e.fingerprint,
                        kind=e.kind,
                        at=e.at,
                        key=e.key,
                        code=e.code,
                        severity=e.severity,
                        gate=e.gate,
                        description=e.description,
                        sku=e.sku,
                        product=e.product,
                        id=cur.lastrowid,
                    )
                )
    finally:
        conn.close()
    return stored
