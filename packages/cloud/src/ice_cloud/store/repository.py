"""The cloud's data-access contract (identity spec sections 3 and 7).

Routes and services depend on :class:`Repository`, never on SQL. Every method
is named for a use case. Times are timezone-aware UTC ``datetime`` values.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal, Protocol

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
    role: Literal["owner", "tech", "admin"]


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


class Repository(Protocol):
    # accounts
    def upsert_account_from_login(
        self, google_sub: str, email: str, name: str, now: datetime
    ) -> Account:
        """Create or refresh the account for ``google_sub``.

        Raises :class:`EmailConflict` (nothing written) when ``email`` already
        belongs to an account with a different ``google_sub``, whether this
        login is new or an existing account is changing its email.
        """
        ...

    def account(self, id: str) -> Account | None: ...

    def account_by_email(self, email: str) -> Account | None: ...

    def set_suspended(self, id: str, suspended_at: datetime | None) -> None: ...

    def all_accounts(self) -> list[Account]: ...

    # machines
    def register_machine(
        self, fingerprint: str, public_key_b64: str, label: str, now: datetime
    ) -> Machine: ...

    def machine(self, fingerprint: str) -> Machine | None: ...

    def resolve_short(self, short16: str) -> Machine | None: ...

    def touch_seen(self, fingerprint: str, now: datetime) -> None: ...

    def set_claim_code(
        self, fingerprint: str, hash: str | None, expires_at: datetime | None
    ) -> None: ...

    def claim_machine(
        self,
        fingerprint: str,
        account_id: str,
        now: datetime,
        *,
        expected_hash: str | None = None,
    ) -> bool:
        """Bind the machine to the account; False (no change) if either is unknown.

        With ``expected_hash`` the bind is a compare-and-swap: it applies only while
        the machine still holds that claim-code hash and it has not expired.
        """
        ...

    def can_upload_bundle(
        self, fingerprint: str, digest: str, limit: int, since: datetime
    ) -> bool:
        """True if ``digest`` is already this machine's or fewer than ``limit``
        distinct digests were uploaded since ``since``."""
        ...

    def record_bundle_upload(
        self, fingerprint: str, digest: str, now: datetime
    ) -> None: ...

    def consumed_claim_hashes(self, fingerprint: str) -> set[str]:
        """Claim-code hashes a claim has spent; ``set_claim_code`` ignores them."""
        ...

    def unbind_machine(self, fingerprint: str) -> None:
        """Admin reset: drops the owner, memberships and consumed hashes."""
        ...

    def all_machines(self) -> list[Machine]: ...

    def count_unclaimed_machines(self) -> int:
        """Machines with no owner (never claimed, or unbound)."""
        ...

    def prune_alert_history(self, before: datetime) -> int:
        """Retention: delete ``alert_events`` older than ``before``; deliveries that
        are ``sent``/``muted`` or ``failed`` with 5+ attempts and last attempted
        before it (``pending`` and still-retrying rows are kept); ``bundle_uploads``
        older than it; accepted invitations created before it and unaccepted ones
        expired before it. Returns the number of alert rows (events plus
        deliveries) removed."""
        ...

    def purge_unclaimed_machines(self, not_seen_since: datetime) -> list[str]:
        """Delete never-claimed machines last seen before *not_seen_since*, with
        their bundle uploads, memberships, consumed hashes and alert rows, in one
        transaction; return the deleted fingerprints."""
        ...

    def purge_all_unclaimed_machines(self) -> list[str]:
        """Admin: delete every machine with no owner (whatever its age or history)
        with the same dependent rows as ``purge_unclaimed_machines``; return the
        deleted fingerprints."""
        ...

    # access
    def machines_for_account(self, account_id: str) -> list[MachineAccess]: ...

    def access_for(
        self, account_id: str, fingerprint: str
    ) -> Literal["owner", "tech"] | None: ...

    def memberships(self, fingerprint: str) -> list[tuple[Membership, Account]]: ...

    def all_memberships(self) -> list[Membership]:
        """Every membership row across the fleet, ordered by fingerprint, then
        grant time."""
        ...

    def grant_tech(
        self, account_id: str, fingerprint: str, granted_by: str, now: datetime
    ) -> None:
        """Grant tech access; raises ``LookupError`` if the account or machine is unknown."""
        ...

    def revoke(self, account_id: str, fingerprint: str) -> None: ...

    # invitations
    def create_invitation(
        self,
        inviter_id: str,
        email: str,
        scope: str | tuple[str, ...],
        now: datetime,
        ttl: timedelta = timedelta(days=7),
    ) -> Invitation: ...

    def invitation(self, token: str) -> Invitation | None: ...

    def invitations_by(
        self, inviter_id: str, fingerprint: str | None, now: datetime
    ) -> list[Invitation]:
        """Pending invitations: unaccepted and with ``expires_at > now``."""
        ...

    def accept_invitation(
        self, token: str, account_id: str, now: datetime
    ) -> list[str]: ...

    def cancel_invitation(self, token: str) -> None: ...

    # alerts
    def alert_state(self, fingerprint: str) -> AlertState | None: ...

    def put_alert_state(self, state: AlertState) -> None: ...

    def delete_alert_state(self, fingerprint: str) -> None: ...

    def claimed_machines_with_state(self) -> list[tuple[Machine, AlertState]]:
        """Claimed machines (owner set) that have an alert state row."""
        ...

    def record_alert_events(self, events: list[AlertEventRow]) -> list[StoredEvent]:
        """Insert events (any object with the row's fields) in order; return them with ids."""
        ...

    def record_alert_pass(
        self, events: Sequence[AlertEventRow], state: AlertState
    ) -> list[StoredEvent]:
        """Insert events and upsert the alert state in one transaction."""
        ...

    def mark_offline_if_unchanged(
        self, fingerprint: str, last_report_at: datetime, event: AlertEventRow
    ) -> StoredEvent | None:
        """Set ``offline_since`` to ``event.at`` and insert ``event``, in one
        transaction, only while the state still has ``offline_since`` unset and
        ``last_report_at`` unchanged (no report arrived since the caller read it).
        Returns the stored event, or None when nothing changed."""
        ...

    def alert_events(self, fingerprint: str, *, limit: int) -> list[StoredEvent]:
        """Newest first."""
        ...

    def alert_events_by_ids(self, ids: Sequence[int]) -> list[StoredEvent]:
        """The events with these ids, ordered by id; unknown ids are skipped."""
        ...

    def alert_count_since(self, fingerprint: str, since: datetime) -> int:
        """Events at or after ``since``, counting ``raised`` and ``offline`` only
        (neither ``cleared`` nor ``online`` is an alert)."""
        ...

    def alert_counts_since(self, since: datetime) -> dict[str, int]:
        """``{fingerprint: count}`` of ``raised`` and ``offline`` events at or
        after ``since``; machines with none are absent."""
        ...

    def recent_alert_events(self, *, limit: int) -> list[tuple[StoredEvent, Machine]]:
        """The newest ``limit`` events across all machines, newest first, each
        with its machine."""
        ...

    def cleared_since_last_sent(
        self, fingerprint: str, account_id: str
    ) -> list[StoredEvent]:
        """``cleared`` events after the later of the account's last ``sent`` delivery
        and its access start (``claimed_at`` for the owner, ``granted_at`` for a
        tech); empty when the account has no access to the machine."""
        ...

    def people_for_alerts(self, fingerprint: str) -> list[tuple[Recipient, bool]]:
        """Owner first, then techs by ``granted_at``, each with its muted flag;
        suspended accounts are excluded."""
        ...

    def set_muted(self, account_id: str, fingerprint: str, muted: bool) -> None: ...

    def is_muted(self, account_id: str, fingerprint: str) -> bool: ...

    def create_delivery(
        self,
        fingerprint: str,
        account_id: str,
        event_ids: list[int] | tuple[int, ...],
        status: str,
        now: datetime,
    ) -> Delivery: ...

    def pending_deliveries(
        self, *, before: datetime, ignore_backoff: bool = False
    ) -> list[Delivery]:
        """``pending`` rows with ``attempted_at <= before`` and ``failed`` rows with
        fewer than 5 attempts whose backoff has elapsed: ``attempted_at`` plus
        ``min(2**attempts, 64)`` minutes is at or before ``before``. With
        ``ignore_backoff`` every failed row under the attempt cap is returned
        (used to fold them into an email that is going out anyway)."""
        ...

    def mark_delivery(
        self, id: int, *, status: str, now: datetime, error: str | None = None
    ) -> None:
        """Set status, ``attempted_at`` and ``error``; ``attempts`` += 1;
        ``sent_at`` is set only for ``sent``."""
        ...

    def delivery_health(self, fingerprint: str) -> bool:
        """False when the machine's most recently attempted delivery (ignoring
        ``muted`` rows and rows never attempted) is ``failed``; true otherwise,
        including when there is no delivery at all."""
        ...

    def last_sent_at(self, fingerprint: str, account_id: str) -> datetime | None: ...

    def deliveries_for_events(
        self, fingerprint: str, event_ids: list[int] | tuple[int, ...]
    ) -> dict[int, list[Delivery]]:
        """This machine's deliveries grouped by the given event ids."""
        ...
