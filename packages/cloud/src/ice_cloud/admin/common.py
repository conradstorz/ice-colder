"""Shared pieces of the admin package: levels, template context, fleet loading."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from types import SimpleNamespace

from fastapi import HTTPException, Request

from ice_cloud.fleet_view import FleetView, build_fleet_view
from ice_cloud.store.repository import Account, Machine
from ice_cloud.viewer import MY_MACHINES_SWITCH, RemoteViewer
from ice_contracts.access import REMOTE_VIEWER_PERMISSIONS
from ice_contracts.cloud.signing import parse_short_fingerprint
from ice_ui.levels import Level

LEVEL_ADMIN = Level(title="Admin", url="/admin")
LEVEL_ADMIN_MACHINES = Level("Machines", "/admin/machines", parent=LEVEL_ADMIN)
LEVEL_ADMIN_OWNERS = Level("Owners", "/admin/owners", parent=LEVEL_ADMIN)
LEVEL_ADMIN_ACCOUNTS = Level("Accounts", "/admin/accounts", parent=LEVEL_ADMIN)
LEVEL_ADMIN_ALERTS = Level("Alerts", "/admin/alerts", parent=LEVEL_ADMIN)

ALERT_WINDOW = timedelta(hours=24)


def admin_context(
    request: Request, account: Account, level: Level | None, **extra
) -> dict:
    """Every ``/admin*`` page wears the Admin hat: the chip reads ``admin`` and
    the bar offers the way back to My machines."""
    return {
        "request": request,
        "current_user": RemoteViewer(display_name=account.name, role_label="admin"),
        "perms": REMOTE_VIEWER_PERMISSIONS,
        "level": level,
        "hat_switch": MY_MACHINES_SWITCH,
        "badge": SimpleNamespace(
            label="Cloud", url="/fleet", tone="cloud", title=None, external=False
        ),
        **extra,
    }


def templates(request: Request):
    registry = request.app.state.registry
    return registry.resolve(registry.installed_digest).templates


async def machine_or_404(request: Request, short: str) -> Machine:
    short16 = parse_short_fingerprint(short)
    machine = (
        await asyncio.to_thread(request.app.state.repo.resolve_short, short16)
        if short16
        else None
    )
    if machine is None:
        raise HTTPException(status_code=404)
    return machine


async def load_fleet_view(request: Request) -> FleetView:
    state = request.app.state
    now = state.now()
    repo = state.repo
    machines = await asyncio.to_thread(repo.all_machines)
    accounts = await asyncio.to_thread(repo.all_accounts)
    memberships = await asyncio.to_thread(repo.all_memberships)
    counts = await asyncio.to_thread(repo.alert_counts_since, now - ALERT_WINDOW)
    reports = await asyncio.to_thread(state.store.machines)
    return build_fleet_view(
        machines=machines,
        accounts=accounts,
        memberships=memberships,
        reports=dict(reports),
        alert_counts=counts,
        installed_digest=state.registry.installed_digest,
        now=now,
    )
