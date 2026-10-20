"""SQLite implementation of :class:`Repository`: the only module with SQL.

One connection per call, WAL, ``synchronous=NORMAL``, foreign keys on (the
edge's ``EventRecorder`` pattern). Schema changes are an ordered list of
scripts in ``MIGRATIONS`` applied on construction under ``schema_version``.
"""

from __future__ import annotations

import json
import secrets
import sqlite3
import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

from ice_contracts.cloud.report import FaultView

from ice_cloud.store.repository import (
    Account,
    AlertEventRow,
    AlertState,
    Delivery,
    EmailConflict,
    Invitation,
    Machine,
    MachineAccess,
    Membership,
    Recipient,
    StoredEvent,
)

MIGRATIONS: list[str] = [
    """
    CREATE TABLE accounts (
        id TEXT PRIMARY KEY,
        google_sub TEXT NOT NULL UNIQUE,
        email TEXT NOT NULL UNIQUE,
        name TEXT NOT NULL,
        created_at TEXT NOT NULL,
        suspended_at TEXT
    );
    CREATE TABLE machines (
        fingerprint TEXT PRIMARY KEY,
        public_key TEXT NOT NULL,
        label TEXT NOT NULL,
        owner_id TEXT REFERENCES accounts(id),
        registered_at TEXT NOT NULL,
        last_seen_at TEXT NOT NULL,
        claim_code_hash TEXT,
        claim_code_expires_at TEXT,
        claimed_at TEXT
    );
    CREATE TABLE memberships (
        account_id TEXT NOT NULL REFERENCES accounts(id),
        fingerprint TEXT NOT NULL REFERENCES machines(fingerprint),
        role TEXT NOT NULL,
        granted_by TEXT NOT NULL REFERENCES accounts(id),
        granted_at TEXT NOT NULL,
        PRIMARY KEY (account_id, fingerprint)
    );
    CREATE TABLE invitations (
        token TEXT PRIMARY KEY,
        inviter_id TEXT NOT NULL REFERENCES accounts(id),
        email TEXT NOT NULL,
        scope TEXT NOT NULL,
        created_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        accepted_by TEXT REFERENCES accounts(id),
        accepted_at TEXT
    );
    """,
    "ALTER TABLE machines ADD COLUMN consumed_claim_hash TEXT",
    """
    CREATE TABLE bundle_uploads (
        fingerprint TEXT NOT NULL REFERENCES machines(fingerprint),
        digest TEXT NOT NULL,
        uploaded_at TEXT NOT NULL,
        PRIMARY KEY (fingerprint, digest)
    )
    """,
    """
    CREATE TABLE consumed_claim_hashes (
        fingerprint TEXT NOT NULL REFERENCES machines(fingerprint),
        hash TEXT NOT NULL,
        consumed_at TEXT NOT NULL,
        PRIMARY KEY (fingerprint, hash)
    );
    INSERT INTO consumed_claim_hashes (fingerprint, hash, consumed_at)
        SELECT fingerprint, consumed_claim_hash, COALESCE(claimed_at, registered_at)
        FROM machines WHERE consumed_claim_hash IS NOT NULL
    """,
    """
    CREATE TABLE machine_alert_state (
        fingerprint TEXT PRIMARY KEY REFERENCES machines(fingerprint),
        faults_json TEXT NOT NULL,
        last_report_at TEXT NOT NULL,
        report_interval_seconds INTEGER NOT NULL,
        offline_since TEXT
    )
    """,
    """
    CREATE TABLE alert_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        fingerprint TEXT NOT NULL REFERENCES machines(fingerprint),
        kind TEXT NOT NULL,
        at TEXT NOT NULL,
        "key" TEXT,
        code TEXT,
        severity TEXT,
        gate TEXT,
        description TEXT,
        sku TEXT,
        product TEXT
    );
    CREATE INDEX alert_events_by_machine ON alert_events (fingerprint, at);
    CREATE TABLE alert_deliveries (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        fingerprint TEXT NOT NULL REFERENCES machines(fingerprint),
        account_id TEXT NOT NULL REFERENCES accounts(id),
        event_ids_json TEXT NOT NULL,
        status TEXT NOT NULL,
        attempted_at TEXT,
        sent_at TEXT,
        error TEXT,
        attempts INTEGER NOT NULL DEFAULT 0
    )
    """,
    """
    CREATE TABLE alert_prefs (
        account_id TEXT NOT NULL REFERENCES accounts(id),
        fingerprint TEXT NOT NULL REFERENCES machines(fingerprint),
        muted INTEGER NOT NULL,
        PRIMARY KEY (account_id, fingerprint)
    )
    """,
]

MAX_DELIVERY_ATTEMPTS = 5

# Public so a test can substitute a failing statement and prove a claim is atomic.
DELETE_MEMBERSHIPS_SQL = "DELETE FROM memberships WHERE fingerprint = ?"

_FLEET = "fleet"


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.astimezone(UTC).isoformat()


def _dt(value: str | None) -> datetime | None:
    return None if value is None else datetime.fromisoformat(value)


def _account(row: sqlite3.Row) -> Account:
    return Account(
        id=row["id"],
        google_sub=row["google_sub"],
        email=row["email"],
        name=row["name"],
        created_at=_dt(row["created_at"]),
        suspended_at=_dt(row["suspended_at"]),
    )


def _machine(row: sqlite3.Row) -> Machine:
    return Machine(
        fingerprint=row["fingerprint"],
        public_key_b64=row["public_key"],
        label=row["label"],
        owner_id=row["owner_id"],
        registered_at=_dt(row["registered_at"]),
        last_seen_at=_dt(row["last_seen_at"]),
        claim_code_hash=row["claim_code_hash"],
        claim_code_expires_at=_dt(row["claim_code_expires_at"]),
        claimed_at=_dt(row["claimed_at"]),
    )


def _invitation(row: sqlite3.Row) -> Invitation:
    raw = row["scope"]
    scope: str | tuple[str, ...] = raw if raw == _FLEET else tuple(json.loads(raw))
    return Invitation(
        token=row["token"],
        inviter_id=row["inviter_id"],
        email=row["email"],
        scope=scope,
        created_at=_dt(row["created_at"]),
        expires_at=_dt(row["expires_at"]),
        accepted_by=row["accepted_by"],
        accepted_at=_dt(row["accepted_at"]),
    )


def _event(row: sqlite3.Row) -> StoredEvent:
    return StoredEvent(
        fingerprint=row["fingerprint"],
        kind=row["kind"],
        at=_dt(row["at"]),
        key=row["key"],
        code=row["code"],
        severity=row["severity"],
        gate=row["gate"],
        description=row["description"],
        sku=row["sku"],
        product=row["product"],
        id=row["id"],
    )


def _delivery(row: sqlite3.Row) -> Delivery:
    return Delivery(
        id=row["id"],
        fingerprint=row["fingerprint"],
        account_id=row["account_id"],
        event_ids=tuple(json.loads(row["event_ids_json"])),
        status=row["status"],
        attempted_at=_dt(row["attempted_at"]),
        sent_at=_dt(row["sent_at"]),
        error=row["error"],
        attempts=row["attempts"],
    )


def _alert_state(row: sqlite3.Row) -> AlertState:
    return AlertState(
        fingerprint=row["fingerprint"],
        faults=tuple(
            FaultView.model_validate(f) for f in json.loads(row["faults_json"])
        ),
        last_report_at=_dt(row["last_report_at"]),
        report_interval_seconds=row["report_interval_seconds"],
        offline_since=_dt(row["offline_since"]),
    )


_ALERT_DELETES = (
    "DELETE FROM machine_alert_state WHERE fingerprint = ?",
    "DELETE FROM alert_prefs WHERE fingerprint = ?",
)


class SqliteRepository:
    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._migrate()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self._path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA foreign_keys=ON")
            yield conn
        finally:
            conn.close()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")

    def _migrate(self) -> None:
        with self._tx() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)"
            )
            row = conn.execute("SELECT version FROM schema_version").fetchone()
            if row is None:
                conn.execute("INSERT INTO schema_version (version) VALUES (0)")
                current = 0
            else:
                current = row["version"]
            for index in range(current, len(MIGRATIONS)):
                for statement in MIGRATIONS[index].split(";"):
                    if statement.strip():
                        conn.execute(statement)
                conn.execute("UPDATE schema_version SET version = ?", (index + 1,))

    # accounts

    def upsert_account_from_login(
        self, google_sub: str, email: str, name: str, now: datetime
    ) -> Account:
        email = email.lower()
        with self._tx() as conn:
            row = conn.execute(
                "SELECT * FROM accounts WHERE google_sub = ?", (google_sub,)
            ).fetchone()
            clash = conn.execute(
                "SELECT 1 FROM accounts WHERE email = ? AND google_sub != ?",
                (email, google_sub),
            ).fetchone()
            if clash is not None:
                raise EmailConflict(email)
            if row is None:
                account_id = str(uuid.uuid4())
                conn.execute(
                    "INSERT INTO accounts (id, google_sub, email, name, created_at)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (account_id, google_sub, email, name, _iso(now)),
                )
            else:
                account_id = row["id"]
                conn.execute(
                    "UPDATE accounts SET email = ?, name = ? WHERE id = ?",
                    (email, name, account_id),
                )
            return _account(
                conn.execute(
                    "SELECT * FROM accounts WHERE id = ?", (account_id,)
                ).fetchone()
            )

    def account(self, id: str) -> Account | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM accounts WHERE id = ?", (id,)).fetchone()
        return None if row is None else _account(row)

    def account_by_email(self, email: str) -> Account | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM accounts WHERE email = ?", (email.lower(),)
            ).fetchone()
        return None if row is None else _account(row)

    def set_suspended(self, id: str, suspended_at: datetime | None) -> None:
        with self._tx() as conn:
            conn.execute(
                "UPDATE accounts SET suspended_at = ? WHERE id = ?",
                (_iso(suspended_at), id),
            )

    def all_accounts(self) -> list[Account]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM accounts ORDER BY email").fetchall()
        return [_account(r) for r in rows]

    # machines

    def register_machine(
        self, fingerprint: str, public_key_b64: str, label: str, now: datetime
    ) -> Machine:
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO machines (fingerprint, public_key, label, registered_at,"
                " last_seen_at) VALUES (?, ?, ?, ?, ?)"
                " ON CONFLICT(fingerprint) DO UPDATE SET"
                " label = excluded.label, last_seen_at = excluded.last_seen_at",
                (fingerprint, public_key_b64, label, _iso(now), _iso(now)),
            )
            return _machine(
                conn.execute(
                    "SELECT * FROM machines WHERE fingerprint = ?", (fingerprint,)
                ).fetchone()
            )

    def machine(self, fingerprint: str) -> Machine | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM machines WHERE fingerprint = ?", (fingerprint,)
            ).fetchone()
        return None if row is None else _machine(row)

    def resolve_short(self, short16: str) -> Machine | None:
        if not short16 or any(c in short16 for c in "%_\\"):
            return None
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM machines WHERE fingerprint LIKE ? LIMIT 2",
                (short16.lower() + "%",),
            ).fetchall()
        return _machine(rows[0]) if len(rows) == 1 else None

    def touch_seen(self, fingerprint: str, now: datetime) -> None:
        with self._tx() as conn:
            conn.execute(
                "UPDATE machines SET last_seen_at = ? WHERE fingerprint = ?",
                (_iso(now), fingerprint),
            )

    def set_claim_code(
        self, fingerprint: str, hash: str | None, expires_at: datetime | None
    ) -> None:
        with self._tx() as conn:
            if (
                hash is not None
                and conn.execute(
                    "SELECT 1 FROM consumed_claim_hashes WHERE fingerprint = ? AND hash = ?",
                    (fingerprint, hash),
                ).fetchone()
            ):
                return  # a spent code never reopens a claim (resale)
            if hash is None:
                conn.execute(
                    "UPDATE machines SET claim_code_hash = NULL,"
                    " claim_code_expires_at = NULL WHERE fingerprint = ?",
                    (fingerprint,),
                )
                return
            conn.execute(
                "UPDATE machines SET claim_code_hash = ?, claim_code_expires_at = ?"
                " WHERE fingerprint = ?",
                (hash, _iso(expires_at), fingerprint),
            )

    def claim_machine(
        self,
        fingerprint: str,
        account_id: str,
        now: datetime,
        *,
        expected_hash: str | None = None,
    ) -> bool:
        where = "fingerprint = ?"
        params: list[str] = [account_id, _iso(now), fingerprint]
        if expected_hash is not None:
            where += (
                " AND claim_code_hash = ?"
                " AND (claim_code_expires_at IS NULL OR claim_code_expires_at > ?)"
            )
            params += [expected_hash, _iso(now)]
        with self._tx() as conn:
            if not conn.execute(
                "SELECT 1 FROM accounts WHERE id = ?", (account_id,)
            ).fetchone():
                return False
            before = conn.execute(
                "SELECT owner_id, claim_code_hash FROM machines WHERE fingerprint = ?",
                (fingerprint,),
            ).fetchone()
            cur = conn.execute(
                "UPDATE machines SET owner_id = ?, claimed_at = ?,"
                " claim_code_hash = NULL, claim_code_expires_at = NULL"
                f" WHERE {where}",
                params,
            )
            if cur.rowcount == 0:
                return False
            if before["claim_code_hash"] is not None:
                conn.execute(
                    "INSERT OR IGNORE INTO consumed_claim_hashes"
                    " (fingerprint, hash, consumed_at) VALUES (?, ?, ?)",
                    (fingerprint, before["claim_code_hash"], _iso(now)),
                )
            if before["owner_id"] != account_id:
                conn.execute(DELETE_MEMBERSHIPS_SQL, (fingerprint,))
                for statement in _ALERT_DELETES:
                    conn.execute(statement, (fingerprint,))
            return True

    def can_upload_bundle(
        self, fingerprint: str, digest: str, limit: int, since: datetime
    ) -> bool:
        with self._connect() as conn:
            if conn.execute(
                "SELECT 1 FROM bundle_uploads WHERE fingerprint = ? AND digest = ?",
                (fingerprint, digest),
            ).fetchone():
                return True
            count = conn.execute(
                "SELECT COUNT(*) AS n FROM bundle_uploads"
                " WHERE fingerprint = ? AND uploaded_at > ?",
                (fingerprint, _iso(since)),
            ).fetchone()["n"]
        return count < limit

    def record_bundle_upload(
        self, fingerprint: str, digest: str, now: datetime
    ) -> None:
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO bundle_uploads (fingerprint, digest, uploaded_at)"
                " VALUES (?, ?, ?) ON CONFLICT(fingerprint, digest)"
                " DO UPDATE SET uploaded_at = excluded.uploaded_at",
                (fingerprint, digest, _iso(now)),
            )

    def consumed_claim_hashes(self, fingerprint: str) -> set[str]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT hash FROM consumed_claim_hashes WHERE fingerprint = ?",
                (fingerprint,),
            ).fetchall()
        return {r["hash"] for r in rows}

    def unbind_machine(self, fingerprint: str) -> None:
        with self._tx() as conn:
            conn.execute(
                "DELETE FROM consumed_claim_hashes WHERE fingerprint = ?",
                (fingerprint,),
            )
            conn.execute(
                "UPDATE machines SET owner_id = NULL, claimed_at = NULL,"
                " claim_code_hash = NULL, claim_code_expires_at = NULL"
                " WHERE fingerprint = ?",
                (fingerprint,),
            )
            conn.execute(DELETE_MEMBERSHIPS_SQL, (fingerprint,))
            for statement in _ALERT_DELETES:
                conn.execute(statement, (fingerprint,))
            conn.execute(
                "DELETE FROM alert_deliveries WHERE fingerprint = ?", (fingerprint,)
            )
            conn.execute(
                "DELETE FROM alert_events WHERE fingerprint = ?", (fingerprint,)
            )

    def all_machines(self) -> list[Machine]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM machines ORDER BY label, fingerprint"
            ).fetchall()
        return [_machine(r) for r in rows]

    # access

    def machines_for_account(self, account_id: str) -> list[MachineAccess]:
        with self._connect() as conn:
            owned = conn.execute(
                "SELECT * FROM machines WHERE owner_id = ?", (account_id,)
            ).fetchall()
            tech = conn.execute(
                "SELECT m.* FROM machines m JOIN memberships s"
                " ON s.fingerprint = m.fingerprint"
                " WHERE s.account_id = ? AND m.owner_id IS NOT ?",
                (account_id, account_id),
            ).fetchall()
        found = [MachineAccess(_machine(r), "owner") for r in owned]
        found += [MachineAccess(_machine(r), "tech") for r in tech]
        return sorted(found, key=lambda a: (a.machine.label, a.machine.fingerprint))

    def access_for(
        self, account_id: str, fingerprint: str
    ) -> Literal["owner", "tech"] | None:
        with self._connect() as conn:
            owner = conn.execute(
                "SELECT 1 FROM machines WHERE fingerprint = ? AND owner_id = ?",
                (fingerprint, account_id),
            ).fetchone()
            if owner is not None:
                return "owner"
            member = conn.execute(
                "SELECT 1 FROM memberships WHERE account_id = ? AND fingerprint = ?",
                (account_id, fingerprint),
            ).fetchone()
        return "tech" if member is not None else None

    def memberships(self, fingerprint: str) -> list[tuple[Membership, Account]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT a.*, s.role, s.granted_by, s.granted_at"
                " FROM memberships s JOIN accounts a ON a.id = s.account_id"
                " WHERE s.fingerprint = ? ORDER BY a.email",
                (fingerprint,),
            ).fetchall()
        return [
            (
                Membership(
                    account_id=r["id"],
                    fingerprint=fingerprint,
                    role=r["role"],
                    granted_by=r["granted_by"],
                    granted_at=_dt(r["granted_at"]),
                ),
                _account(r),
            )
            for r in rows
        ]

    def grant_tech(
        self, account_id: str, fingerprint: str, granted_by: str, now: datetime
    ) -> None:
        with self._tx() as conn:
            if not conn.execute(
                "SELECT 1 FROM accounts WHERE id = ?", (account_id,)
            ).fetchone():
                raise LookupError(f"unknown account {account_id}")
            if not conn.execute(
                "SELECT 1 FROM machines WHERE fingerprint = ?", (fingerprint,)
            ).fetchone():
                raise LookupError(f"unknown machine {fingerprint}")
            conn.execute(
                "INSERT OR IGNORE INTO memberships"
                " (account_id, fingerprint, role, granted_by, granted_at)"
                " VALUES (?, ?, 'tech', ?, ?)",
                (account_id, fingerprint, granted_by, _iso(now)),
            )

    def revoke(self, account_id: str, fingerprint: str) -> None:
        with self._tx() as conn:
            conn.execute(
                "DELETE FROM memberships WHERE account_id = ? AND fingerprint = ?",
                (account_id, fingerprint),
            )
            conn.execute(
                "DELETE FROM alert_prefs WHERE account_id = ? AND fingerprint = ?",
                (account_id, fingerprint),
            )

    # invitations

    def create_invitation(
        self,
        inviter_id: str,
        email: str,
        scope: str | tuple[str, ...],
        now: datetime,
        ttl: timedelta = timedelta(days=7),
    ) -> Invitation:
        token = secrets.token_urlsafe(32)
        stored = scope if isinstance(scope, str) else json.dumps(list(scope))
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO invitations (token, inviter_id, email, scope,"
                " created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?)",
                (token, inviter_id, email.lower(), stored, _iso(now), _iso(now + ttl)),
            )
            return _invitation(
                conn.execute(
                    "SELECT * FROM invitations WHERE token = ?", (token,)
                ).fetchone()
            )

    def invitation(self, token: str) -> Invitation | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM invitations WHERE token = ?", (token,)
            ).fetchone()
        return None if row is None else _invitation(row)

    def invitations_by(
        self, inviter_id: str, fingerprint: str | None, now: datetime
    ) -> list[Invitation]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM invitations WHERE inviter_id = ? AND accepted_by IS NULL"
                " ORDER BY created_at",
                (inviter_id,),
            ).fetchall()
        found = [i for i in map(_invitation, rows) if i.expires_at > now]
        if fingerprint is None:
            return found
        return [i for i in found if i.scope == _FLEET or fingerprint in i.scope]

    def accept_invitation(
        self, token: str, account_id: str, now: datetime
    ) -> list[str]:
        with self._tx() as conn:
            row = conn.execute(
                "SELECT * FROM invitations WHERE token = ?", (token,)
            ).fetchone()
            if row is None:
                return []
            invite = _invitation(row)
            if invite.accepted_by is not None or invite.expires_at <= now:
                return []
            owned = [
                r["fingerprint"]
                for r in conn.execute(
                    "SELECT fingerprint FROM machines WHERE owner_id = ?"
                    " ORDER BY fingerprint",
                    (invite.inviter_id,),
                )
            ]
            if invite.scope == _FLEET:
                targets = owned
            else:
                targets = [fp for fp in owned if fp in invite.scope]
            granted = []
            for fp in targets:
                if conn.execute(
                    "SELECT 1 FROM machines WHERE fingerprint = ? AND owner_id = ?",
                    (fp, account_id),
                ).fetchone():
                    continue  # the new member already owns it
                conn.execute(
                    "INSERT OR IGNORE INTO memberships"
                    " (account_id, fingerprint, role, granted_by, granted_at)"
                    " VALUES (?, ?, 'tech', ?, ?)",
                    (account_id, fp, invite.inviter_id, _iso(now)),
                )
                granted.append(fp)
            conn.execute(
                "UPDATE invitations SET accepted_by = ?, accepted_at = ? WHERE token = ?",
                (account_id, _iso(now), token),
            )
            return granted

    def cancel_invitation(self, token: str) -> None:
        with self._tx() as conn:
            conn.execute("DELETE FROM invitations WHERE token = ?", (token,))

    # alerts

    def alert_state(self, fingerprint: str) -> AlertState | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM machine_alert_state WHERE fingerprint = ?",
                (fingerprint,),
            ).fetchone()
        return None if row is None else _alert_state(row)

    def put_alert_state(self, state: AlertState) -> None:
        faults = json.dumps([f.model_dump(mode="json") for f in state.faults])
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO machine_alert_state (fingerprint, faults_json,"
                " last_report_at, report_interval_seconds, offline_since)"
                " VALUES (?, ?, ?, ?, ?) ON CONFLICT(fingerprint) DO UPDATE SET"
                " faults_json = excluded.faults_json,"
                " last_report_at = excluded.last_report_at,"
                " report_interval_seconds = excluded.report_interval_seconds,"
                " offline_since = excluded.offline_since",
                (
                    state.fingerprint,
                    faults,
                    _iso(state.last_report_at),
                    state.report_interval_seconds,
                    _iso(state.offline_since),
                ),
            )

    def delete_alert_state(self, fingerprint: str) -> None:
        with self._tx() as conn:
            conn.execute(_ALERT_DELETES[0], (fingerprint,))

    def claimed_machines_with_state(self) -> list[tuple[Machine, AlertState]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT m.*, s.faults_json, s.last_report_at,"
                " s.report_interval_seconds, s.offline_since"
                " FROM machines m JOIN machine_alert_state s"
                " ON s.fingerprint = m.fingerprint"
                " WHERE m.owner_id IS NOT NULL ORDER BY m.label, m.fingerprint"
            ).fetchall()
        return [(_machine(r), _alert_state(r)) for r in rows]

    def record_alert_events(self, events: list[AlertEventRow]) -> list[StoredEvent]:
        stored: list[StoredEvent] = []
        with self._tx() as conn:
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
        return stored

    def alert_events(self, fingerprint: str, *, limit: int) -> list[StoredEvent]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM alert_events WHERE fingerprint = ?"
                " ORDER BY at DESC, id DESC LIMIT ?",
                (fingerprint, limit),
            ).fetchall()
        return [_event(r) for r in rows]

    def alert_events_by_ids(self, ids: Sequence[int]) -> list[StoredEvent]:
        if not ids:
            return []
        marks = ",".join("?" * len(ids))
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM alert_events WHERE id IN ({marks}) ORDER BY id",  # noqa: S608
                tuple(ids),
            ).fetchall()
        return [_event(r) for r in rows]

    def alert_count_since(self, fingerprint: str, since: datetime) -> int:
        with self._connect() as conn:
            return conn.execute(
                "SELECT COUNT(*) AS n FROM alert_events WHERE fingerprint = ?"
                " AND at >= ? AND kind != 'cleared'",
                (fingerprint, _iso(since)),
            ).fetchone()["n"]

    def cleared_since_last_sent(
        self, fingerprint: str, account_id: str
    ) -> list[StoredEvent]:
        last = self.last_sent_at(fingerprint, account_id)
        sql = "SELECT * FROM alert_events WHERE fingerprint = ? AND kind = 'cleared'"
        params: list[str | None] = [fingerprint]
        if last is not None:
            sql += " AND at > ?"
            params.append(_iso(last))
        with self._connect() as conn:
            rows = conn.execute(sql + " ORDER BY at, id", params).fetchall()
        return [_event(r) for r in rows]

    def people_for_alerts(self, fingerprint: str) -> list[tuple[Recipient, bool]]:
        with self._connect() as conn:
            owner = conn.execute(
                "SELECT a.id, a.email, COALESCE(p.muted, 0) AS muted"
                " FROM machines m JOIN accounts a ON a.id = m.owner_id"
                " LEFT JOIN alert_prefs p ON p.account_id = a.id"
                " AND p.fingerprint = m.fingerprint"
                " WHERE m.fingerprint = ? AND a.suspended_at IS NULL",
                (fingerprint,),
            ).fetchall()
            techs = conn.execute(
                "SELECT a.id, a.email, COALESCE(p.muted, 0) AS muted"
                " FROM memberships s JOIN accounts a ON a.id = s.account_id"
                " JOIN machines m ON m.fingerprint = s.fingerprint"
                " LEFT JOIN alert_prefs p ON p.account_id = a.id"
                " AND p.fingerprint = s.fingerprint"
                " WHERE s.fingerprint = ? AND a.suspended_at IS NULL"
                " AND m.owner_id IS NOT a.id ORDER BY s.granted_at, a.email",
                (fingerprint,),
            ).fetchall()
        people = [(r, "owner") for r in owner] + [(r, "tech") for r in techs]
        return [
            (Recipient(r["id"], r["email"], role), bool(r["muted"]))
            for r, role in people
        ]

    def set_muted(self, account_id: str, fingerprint: str, muted: bool) -> None:
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO alert_prefs (account_id, fingerprint, muted)"
                " VALUES (?, ?, ?) ON CONFLICT(account_id, fingerprint)"
                " DO UPDATE SET muted = excluded.muted",
                (account_id, fingerprint, int(muted)),
            )

    def is_muted(self, account_id: str, fingerprint: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT muted FROM alert_prefs WHERE account_id = ? AND fingerprint = ?",
                (account_id, fingerprint),
            ).fetchone()
        return row is not None and bool(row["muted"])

    def create_delivery(
        self,
        fingerprint: str,
        account_id: str,
        event_ids: list[int] | tuple[int, ...],
        status: str,
        now: datetime,
    ) -> Delivery:
        with self._tx() as conn:
            cur = conn.execute(
                "INSERT INTO alert_deliveries (fingerprint, account_id,"
                " event_ids_json, status, attempted_at) VALUES (?, ?, ?, ?, ?)",
                (
                    fingerprint,
                    account_id,
                    json.dumps(list(event_ids)),
                    status,
                    _iso(now),
                ),
            )
            return _delivery(
                conn.execute(
                    "SELECT * FROM alert_deliveries WHERE id = ?", (cur.lastrowid,)
                ).fetchone()
            )

    def pending_deliveries(self, *, before: datetime) -> list[Delivery]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM alert_deliveries WHERE"
                " (status = 'pending' AND attempted_at <= ?)"
                " OR (status = 'failed' AND attempts < ?) ORDER BY id",
                (_iso(before), MAX_DELIVERY_ATTEMPTS),
            ).fetchall()
        return [_delivery(r) for r in rows]

    def mark_delivery(
        self, id: int, *, status: str, now: datetime, error: str | None = None
    ) -> None:
        with self._tx() as conn:
            conn.execute(
                "UPDATE alert_deliveries SET status = ?, attempted_at = ?, error = ?,"
                " attempts = attempts + 1,"
                " sent_at = CASE WHEN ? = 'sent' THEN ? ELSE sent_at END"
                " WHERE id = ?",
                (status, _iso(now), error, status, _iso(now), id),
            )

    def last_sent_at(self, fingerprint: str, account_id: str) -> datetime | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT MAX(sent_at) AS at FROM alert_deliveries"
                " WHERE fingerprint = ? AND account_id = ? AND status = 'sent'",
                (fingerprint, account_id),
            ).fetchone()
        return _dt(row["at"])

    def deliveries_for_events(
        self, event_ids: list[int] | tuple[int, ...]
    ) -> dict[int, list[Delivery]]:
        wanted = set(event_ids)
        found: dict[int, list[Delivery]] = {i: [] for i in wanted}
        if not wanted:
            return found
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM alert_deliveries ORDER BY id").fetchall()
        for delivery in map(_delivery, rows):
            for event_id in delivery.event_ids:
                if event_id in wanted:
                    found[event_id].append(delivery)
        return found
