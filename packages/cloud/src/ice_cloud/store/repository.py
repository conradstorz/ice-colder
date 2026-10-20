"""The cloud's data-access contract (identity spec sections 3 and 7).

Routes and services depend on :class:`Repository`, never on SQL. Every method
is named for a use case. Times are timezone-aware UTC ``datetime`` values.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal, Protocol


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

    # access
    def machines_for_account(self, account_id: str) -> list[MachineAccess]: ...

    def access_for(
        self, account_id: str, fingerprint: str
    ) -> Literal["owner", "tech"] | None: ...

    def memberships(self, fingerprint: str) -> list[tuple[Membership, Account]]: ...

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
