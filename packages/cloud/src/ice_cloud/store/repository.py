"""The cloud's data-access contract (identity spec sections 3 and 7).

Routes and services depend on :class:`SqliteRepository`, never on SQL. Every method
is named for a use case. Times are timezone-aware UTC ``datetime`` values.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from ice_contracts.cloud.report import FaultView


class EmailConflict(ValueError):
    """A login's email already belongs to a different Google account."""


@dataclass(frozen=True)
class Account:
    id: str
    google_sub: str
    email: str
    name: str
    created_at: datetime
    suspended_at: datetime | None


@dataclass(frozen=True)
class Machine:
    fingerprint: str
    public_key_b64: str
    label: str
    owner_id: str | None
    registered_at: datetime
    last_seen_at: datetime
    claim_code_hash: str | None
    claim_code_expires_at: datetime | None
    claimed_at: datetime | None


@dataclass(frozen=True)
class Membership:
    account_id: str
    fingerprint: str
    role: str
    granted_by: str
    granted_at: datetime


@dataclass(frozen=True)
class Invitation:
    token: str
    inviter_id: str
    email: str
    scope: str | tuple[str, ...]  # "fleet" or a tuple of fingerprints
    created_at: datetime
    expires_at: datetime
    accepted_by: str | None
    accepted_at: datetime | None


@dataclass(frozen=True)
class MachineAccess:
    machine: Machine
    role: Literal["owner", "tech"]


@dataclass(frozen=True)
class AlertState:
    fingerprint: str
    faults: tuple[FaultView, ...]
    last_report_at: datetime
    report_interval_seconds: int
    offline_since: datetime | None

    @property
    def keys(self) -> frozenset[str]:
        return frozenset(f.key for f in self.faults)


@dataclass(frozen=True)
class AlertEventRow:
    fingerprint: str
    kind: str
    at: datetime
    key: str | None
    code: str | None
    severity: str | None
    gate: str | None
    description: str | None
    sku: str | None
    product: str | None


@dataclass(frozen=True)
class StoredEvent(AlertEventRow):
    id: int


@dataclass(frozen=True)
class Recipient:
    account_id: str
    email: str
    role: Literal["owner", "tech"]


@dataclass(frozen=True)
class Delivery:
    id: int
    fingerprint: str
    account_id: str
    event_ids: tuple[int, ...]
    status: str
    attempted_at: datetime | None
    sent_at: datetime | None
    error: str | None
    attempts: int
