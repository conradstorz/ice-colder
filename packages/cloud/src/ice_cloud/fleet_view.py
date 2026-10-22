"""Pure view model for the platform admin's fleet dashboard.

No I/O, no FastAPI, no SQL: every number on the dashboard is computed here from
data the cloud already stores (spec 2026-10-21-cloud-fleet-admin-dashboard-design).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Literal

from ice_cloud.store.repository import Account, Machine, Membership
from ice_contracts.cloud.report import FaultView, MachineReport
from ice_contracts.cloud.signing import short_fingerprint
from ice_ui.filters import humanize_seconds

STALE_FACTOR = 3
ATTENTION_LIMIT = 12
GATE_RANK = {"safety": 0, "fulfillment": 1, "alert": 2}

Tone = Literal["neutral", "good", "warn", "bad"]


class MachineStatus(StrEnum):
    online = "online"
    silent = "silent"
    never_reported = "never_reported"


_STATUS_RANK = {
    MachineStatus.silent: 0,
    MachineStatus.never_reported: 1,
    MachineStatus.online: 2,
}


def is_stale(report: MachineReport, now: datetime) -> bool:
    age = (now - report.sent_at).total_seconds()
    return age > STALE_FACTOR * report.report_interval_seconds


def classify(report: MachineReport | None, now: datetime) -> MachineStatus:
    if report is None:
        return MachineStatus.never_reported
    return MachineStatus.silent if is_stale(report, now) else MachineStatus.online


@dataclass(frozen=True)
class MachineRow:
    fingerprint: str
    short: str
    label: str
    owner_id: str | None
    owner_email: str | None
    status: MachineStatus
    age_seconds: float | None
    age_text: str
    interval_seconds: int | None
    fault_count: int
    top_fault: FaultView | None
    has_safety_fault: bool
    payment_enabled: bool | None
    payment_reason: str | None
    edge_build: str | None
    ui_mismatch: bool
    alerts_24h: int
    claim_open: bool
    registered_at: datetime
    last_seen_at: datetime
    vmc_missing: bool


@dataclass(frozen=True)
class Headline:
    label: str
    value: int | str
    sub: str
    url: str
    tone: Tone


@dataclass(frozen=True)
class StatusMix:
    online_ok: int
    online_faulted: int
    silent: int
    never_reported: int
    online_ok_pct: int
    online_faulted_pct: int
    silent_pct: int
    never_reported_pct: int


@dataclass(frozen=True)
class BuildRow:
    edge_build: str
    count: int
    online: int


@dataclass(frozen=True)
class OwnerRow:
    account_id: str
    email: str
    name: str
    suspended: bool
    machines: int
    online: int
    silent: int
    never_reported: int
    faulted: int
    techs: int
    alerts_24h: int
    url: str


@dataclass(frozen=True)
class AttentionRow:
    row: MachineRow
    reason: str
    tone: Tone


@dataclass(frozen=True)
class FleetView:
    headlines: tuple[Headline, ...]
    secondary: tuple[Headline, ...]
    mix: StatusMix
    attention: tuple[AttentionRow, ...]
    builds: tuple[BuildRow, ...]
    owners: tuple[OwnerRow, ...]
    rows: tuple[MachineRow, ...]


def percentages(counts: Sequence[int]) -> list[int]:
    """Integer percentages summing to 100 (largest remainder); zeros when empty."""
    total = sum(counts)
    if total <= 0:
        return [0] * len(counts)
    result = [c * 100 // total for c in counts]
    order = sorted(range(len(counts)), key=lambda i: (-(counts[i] * 100 % total), i))
    for i in order[: 100 - sum(result)]:
        result[i] += 1
    return result


def _top_fault(report: MachineReport) -> FaultView | None:
    if not report.faults:
        return None
    return min(report.faults, key=lambda f: (GATE_RANK[f.gate], f.code))


def _row(
    machine: Machine,
    report: MachineReport | None,
    *,
    owner_email: str | None,
    alerts_24h: int,
    installed_digest: str,
    now: datetime,
) -> MachineRow:
    age = (now - report.sent_at).total_seconds() if report is not None else None
    reasons = report.health.payment_reasons if report is not None else []
    expires = machine.claim_code_expires_at
    return MachineRow(
        fingerprint=machine.fingerprint,
        short=short_fingerprint(machine.fingerprint),
        label=machine.label,
        owner_id=machine.owner_id,
        owner_email=owner_email,
        status=classify(report, now),
        age_seconds=age,
        age_text=humanize_seconds(age) if age is not None else "never",
        interval_seconds=report.report_interval_seconds if report else None,
        fault_count=len(report.faults) if report is not None else 0,
        top_fault=_top_fault(report) if report is not None else None,
        has_safety_fault=report is not None
        and any(f.gate == "safety" for f in report.faults),
        payment_enabled=report.health.payment_enabled if report else None,
        payment_reason=reasons[0] if reasons else None,
        edge_build=report.edge_build if report else None,
        ui_mismatch=report is not None and report.ui.digest != installed_digest,
        alerts_24h=alerts_24h,
        claim_open=machine.claim_code_hash is not None
        and (expires is None or expires > now),
        registered_at=machine.registered_at,
        last_seen_at=machine.last_seen_at,
        vmc_missing=report.health.vmc_missing if report is not None else False,
    )


def _attention(rows: Sequence[MachineRow]) -> tuple[AttentionRow, ...]:
    online = MachineStatus.online
    picked: list[AttentionRow] = []
    seen: set[str] = set()

    def take(group: list[MachineRow], make: Callable[[MachineRow], AttentionRow]):
        for r in group:
            if r.fingerprint not in seen:
                seen.add(r.fingerprint)
                picked.append(make(r))

    def by_label(r: MachineRow) -> tuple[str, str]:
        return (r.label, r.fingerprint)

    take(
        sorted(
            (r for r in rows if r.status is online and r.has_safety_fault), key=by_label
        ),
        lambda r: AttentionRow(
            r, f"{r.top_fault.code} — {r.top_fault.description}", "bad"
        ),
    )
    take(
        sorted(
            (r for r in rows if r.status is MachineStatus.silent),
            key=lambda r: (-(r.age_seconds or 0), r.label, r.fingerprint),
        ),
        lambda r: AttentionRow(
            r, f"silent for {r.age_text}, expected every {r.interval_seconds}s", "bad"
        ),
    )
    take(
        sorted(
            (r for r in rows if r.status is online and r.payment_enabled is False),
            key=by_label,
        ),
        lambda r: AttentionRow(r, r.payment_reason or "payment disabled", "warn"),
    )
    take(
        sorted(
            (r for r in rows if r.status is online and r.owner_id is None), key=by_label
        ),
        lambda r: AttentionRow(
            r,
            "unclaimed, calling in since "
            f"{r.registered_at.strftime('%Y-%m-%d %H:%M')} UTC",
            "warn",
        ),
    )
    return tuple(picked[:ATTENTION_LIMIT])


def _owner_rows(
    rows: Sequence[MachineRow],
    by_account: Mapping[str, Account],
    memberships: Sequence[Membership],
) -> tuple[OwnerRow, ...]:
    techs_by_fp: dict[str, set[str]] = {}
    for ms in memberships:
        if ms.role == "tech":
            techs_by_fp.setdefault(ms.fingerprint, set()).add(ms.account_id)
    owners: list[OwnerRow] = []
    for oid in {r.owner_id for r in rows if r.owner_id is not None}:
        acct = by_account.get(oid)
        if acct is None:
            continue
        mine = [r for r in rows if r.owner_id == oid]
        techs: set[str] = set()
        for r in mine:
            techs |= techs_by_fp.get(r.fingerprint, set())
        online = [r for r in mine if r.status is MachineStatus.online]
        owners.append(
            OwnerRow(
                account_id=oid,
                email=acct.email,
                name=acct.name,
                suspended=acct.suspended_at is not None,
                machines=len(mine),
                online=len(online),
                silent=sum(1 for r in mine if r.status is MachineStatus.silent),
                never_reported=sum(
                    1 for r in mine if r.status is MachineStatus.never_reported
                ),
                faulted=sum(1 for r in online if r.fault_count),
                techs=len(techs),
                alerts_24h=sum(r.alerts_24h for r in mine),
                url=f"/admin/machines?owner={oid}",
            )
        )
    owners.sort(key=lambda o: (-o.machines, o.email))
    return tuple(owners)


def build_fleet_view(
    *,
    machines: Sequence[Machine],
    accounts: Sequence[Account],
    memberships: Sequence[Membership],
    reports: Mapping[str, MachineReport],
    alert_counts: Mapping[str, int],
    installed_digest: str,
    now: datetime,
) -> FleetView:
    by_account = {a.id: a for a in accounts}
    rows = [
        _row(
            m,
            reports.get(m.fingerprint),
            owner_email=by_account[m.owner_id].email
            if m.owner_id in by_account
            else None,
            alerts_24h=alert_counts.get(m.fingerprint, 0),
            installed_digest=installed_digest,
            now=now,
        )
        for m in machines
    ]
    online = [r for r in rows if r.status is MachineStatus.online]
    silent = [r for r in rows if r.status is MachineStatus.silent]
    never = [r for r in rows if r.status is MachineStatus.never_reported]
    unclaimed = [r for r in rows if r.owner_id is None]
    unclaimed_online = [r for r in unclaimed if r.status is MachineStatus.online]
    faulted = [r for r in online if r.fault_count > 0]
    safety_faulted = [r for r in faulted if r.has_safety_fault]
    blocked = [r for r in online if r.payment_enabled is False]
    owner_ids = {r.owner_id for r in rows if r.owner_id is not None}

    def tone_bad(n: int) -> Tone:
        return "bad" if n > 0 else "good"

    headlines = (
        Headline(
            "Machines",
            len(rows),
            f"{len(rows) - len(unclaimed)} claimed · {len(unclaimed)} unclaimed",
            "/admin/machines",
            "neutral",
        ),
        Headline(
            "Owners",
            len(owner_ids),
            f"{len(accounts)} accounts · "
            f"{sum(1 for a in accounts if a.suspended_at is not None)} suspended",
            "/admin/owners",
            "neutral",
        ),
        Headline(
            "Unclaimed calling in",
            len(unclaimed_online),
            f"{len(unclaimed) - len(unclaimed_online)} unclaimed silent or never reported",
            "/admin/machines?claimed=no&status=online",
            tone_bad(len(unclaimed_online)),
        ),
        Headline(
            "Silent",
            len(silent),
            f"{len(never)} never reported",
            "/admin/machines?status=silent",
            tone_bad(len(silent)),
        ),
    )

    summaries = [
        s for r in online if (s := reports[r.fingerprint].kpi.summary) is not None
    ]
    uptime = (
        sum(s.uptime_pct for s in summaries) / len(summaries) if summaries else None
    )
    builds_by: dict[str, list[MachineRow]] = {}
    for r in rows:
        if r.edge_build is not None:
            builds_by.setdefault(r.edge_build, []).append(r)

    secondary = (
        Headline(
            "Faulted",
            len(faulted),
            f"of which {len(safety_faulted)} safety",
            "/admin/machines?faulted=yes",
            "bad" if safety_faulted else ("warn" if faulted else "good"),
        ),
        Headline(
            "Payment blocked",
            len(blocked),
            f"across {len(online)} online machines",
            "/admin/machines?payment=blocked",
            "warn" if blocked else "neutral",
        ),
        Headline(
            "Alerts (24 h)",
            sum(r.alerts_24h for r in rows),
            "raised or offline",
            "/admin/alerts",
            "neutral",
        ),
        Headline(
            "Revenue (24 h)",
            f"${sum(s.money_in for s in summaries):.2f}",
            f"{sum(s.products_out for s in summaries)} products out · "
            f"{sum(s.vends_failed for s in summaries)} vends failed",
            "/admin/machines?status=online",
            "neutral",
        ),
        Headline(
            "Uptime (24 h)",
            f"{uptime:.1f}%" if uptime is not None else "—",
            f"across {len(online)} online machines",
            "/admin/machines?status=online",
            "neutral",
        ),
        Headline(
            "Software",
            len(builds_by),
            f"{sum(1 for r in rows if r.ui_mismatch)} with a different UI",
            "/admin/machines",
            "neutral",
        ),
    )

    counts = [len(online) - len(faulted), len(faulted), len(silent), len(never)]
    mix = StatusMix(*counts, *percentages(counts))
    builds = tuple(
        sorted(
            (
                BuildRow(
                    b, len(rs), sum(1 for r in rs if r.status is MachineStatus.online)
                )
                for b, rs in builds_by.items()
            ),
            key=lambda b: (-b.count, b.edge_build),
        )
    )
    rows.sort(key=lambda r: (_STATUS_RANK[r.status], r.label, r.fingerprint))
    return FleetView(
        headlines=headlines,
        secondary=secondary,
        mix=mix,
        attention=_attention(rows),
        builds=builds,
        owners=_owner_rows(rows, by_account, memberships),
        rows=tuple(rows),
    )


def filter_rows(
    rows: Sequence[MachineRow],
    *,
    status: MachineStatus | None = None,
    claimed: bool | None = None,
    faulted: bool | None = None,
    payment: Literal["blocked"] | None = None,
    owner: str | None = None,
) -> list[MachineRow]:
    out: list[MachineRow] = []
    for r in rows:
        if status is not None and r.status is not status:
            continue
        if claimed is not None and (r.owner_id is not None) != claimed:
            continue
        if faulted is not None and (r.fault_count > 0) != faulted:
            continue
        if payment == "blocked" and r.payment_enabled is not False:
            continue
        if owner is not None and r.owner_id != owner:
            continue
        out.append(r)
    return out
