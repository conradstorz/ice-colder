"""The fleet portal (cloud spec sections 2, 4, 5; identity spec 6.3).

Machine pages are served at the machine's own paths and rendered through the
same ``ice_ui`` templates, tile builders and pill logic the edge routes use
(never re-implemented here), from the selected machine's latest stored report.

Every page needs a signed-in account. The selection cookie says *which*
machine; the session says *whether* the account may see it, re-checked on
every request.
"""

from __future__ import annotations

import asyncio
from datetime import datetime

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response

from ice_cloud.auth import (
    Role,
    current_account,
    is_admin,
    require_htmx,
    require_machine_access,
    require_signed_in,
)
from ice_cloud.store.repository import Account, Machine
from ice_cloud.ui_registry import ResolvedUi
from ice_cloud.viewer import VIEWER, RemoteViewer
from ice_contracts.access import REMOTE_VIEWER_PERMISSIONS
from ice_contracts.cloud.report import MachineReport
from ice_contracts.cloud.signing import short_fingerprint
from ice_ui.filters import humanize_seconds
from ice_ui.levels import LEVEL_HEALTH, LEVEL_HEALTH_FAULTS, LEVEL_HOME, Level
from ice_ui.tiles import availability_line, build_tiles, fault_count_line, health_tiles
from ice_ui.views.health import VMC_MISSING_HTML, pill_state

COOKIE = "ice_cloud_machine"
STALE_FACTOR = 3
LEVEL_FLEET = Level(title="Fleet", url="/fleet")

__all__ = ["VIEWER", "RemoteViewer", "router"]

router = APIRouter()


def _age_seconds(request: Request, report: MachineReport) -> float:
    now: datetime = request.app.state.now()
    return (now - report.sent_at).total_seconds()


def _is_stale(request: Request, report: MachineReport) -> bool:
    return _age_seconds(request, report) > STALE_FACTOR * report.report_interval_seconds


async def _selected(
    request: Request, account: Account
) -> tuple[Machine, MachineReport | None] | None:
    """The cookie says *which* machine; the session says *whether* to show it."""
    fingerprint = request.cookies.get(COOKIE)
    if not fingerprint:
        return None
    state = request.app.state
    machine = await asyncio.to_thread(state.repo.machine, fingerprint)
    if machine is None:
        return None
    role = await asyncio.to_thread(state.repo.access_for, account.id, fingerprint)
    if role is None and not is_admin(state.settings, account):
        return None
    report = await asyncio.to_thread(state.store.latest, fingerprint)
    return machine, report


def _resolve(request: Request, report: MachineReport | None) -> ResolvedUi:
    registry = request.app.state.registry
    return registry.resolve(report.ui.digest if report else registry.installed_digest)


resolve_ui = _resolve


def _remote(
    request: Request, report: MachineReport, ui: ResolvedUi, fingerprint: str
) -> dict:
    age = max(_age_seconds(request, report), 0.0)
    return {
        "machine_id": report.machine_id,
        "short_fingerprint": short_fingerprint(fingerprint),
        "last_report_local": report.sent_at.astimezone().strftime("%Y-%m-%d %H:%M:%S"),
        "age_text": humanize_seconds(age),
        "stale": _is_stale(request, report),
        "ui_mismatch": ui.mismatch,
        "report_digest": report.ui.digest,
        "portal_digest": request.app.state.registry.installed_digest,
    }


def _context(
    request: Request,
    account: Account,
    ui: ResolvedUi,
    level: Level | None,
    *,
    machine: Machine | None = None,
    report: MachineReport | None = None,
    banner: bool = True,
    **extra,
) -> dict:
    ctx = {
        "request": request,
        "current_user": RemoteViewer(display_name=account.name),
        "perms": REMOTE_VIEWER_PERMISSIONS,
        "level": level,
    }
    if banner and report is not None and machine is not None:
        ctx["remote"] = _remote(request, report, ui, machine.fingerprint)
    ctx.update(extra)
    return ctx


page_context = _context


def _to_fleet() -> RedirectResponse:
    return RedirectResponse("/fleet", status_code=303)


def _no_access_for_fragment(request: Request) -> Response:
    """A polled fragment (/status, /pill, /kpi) with no usable selection.

    htmx follows a 303 and would swap the whole /fleet page into the
    fragment's slot, so an htmx request gets 204 (htmx swaps nothing); a plain
    request still gets the spec's 303 to /fleet."""
    if request.headers.get("HX-Request") == "true":
        return Response(status_code=204)
    return _to_fleet()


def _no_report_page(request: Request, account: Account) -> Response:
    ui = _resolve(request, None)
    return ui.templates.TemplateResponse(
        request, "no_report.html", _context(request, account, ui, None)
    )


def _fault_count(report: MachineReport) -> str:
    return fault_count_line(None if report.health.vmc_missing else len(report.faults))


@router.get("/fleet", response_class=HTMLResponse)
async def fleet(request: Request, account: Account = Depends(require_signed_in)):
    state = request.app.state
    access = await asyncio.to_thread(state.repo.machines_for_account, account.id)
    entries: list[tuple[Machine, Role]] = [(a.machine, a.role) for a in access]
    if is_admin(state.settings, account):
        held = {a.machine.fingerprint for a in access}
        everything = await asyncio.to_thread(state.repo.all_machines)
        entries += [(m, "admin") for m in everything if m.fingerprint not in held]
    rows = []
    for machine, role in entries:
        report = await asyncio.to_thread(state.store.latest, machine.fingerprint)
        rows.append(
            {
                "label": machine.label,
                "short": short_fingerprint(machine.fingerprint),
                "role": role,
                "reported": report is not None,
                "stale": report is not None and _is_stale(request, report),
                "age_text": (
                    humanize_seconds(max(_age_seconds(request, report), 0.0))
                    if report is not None
                    else ""
                ),
                "fault_line": _fault_count(report) if report is not None else "",
            }
        )
    ui = _resolve(request, None)
    return ui.templates.TemplateResponse(
        request,
        "fleet.html",
        _context(request, account, ui, LEVEL_FLEET, machines=rows),
    )


def _select_response(request: Request, machine: Machine) -> RedirectResponse:
    response = RedirectResponse("/", status_code=303)
    response.set_cookie(
        COOKIE,
        machine.fingerprint,
        httponly=True,
        samesite="lax",
        secure=not request.app.state.settings.insecure_cookies,
    )
    return response


@router.post("/fleet/select/{short}", dependencies=[Depends(require_htmx)])
async def select_machine(request: Request, access=Depends(require_machine_access())):
    _account, machine, _role = access
    return _select_response(request, machine)


@router.get("/m/{short}")
async def deep_link(request: Request, access=Depends(require_machine_access())):
    """Pre-select the machine but land on the fleet list, never on a machine
    page, so a cross-site click cannot render machine content directly."""
    _account, machine, _role = access
    response = _select_response(request, machine)
    response.headers["location"] = "/fleet"
    return response


@router.get("/", response_class=HTMLResponse)
async def home(request: Request, account: Account = Depends(require_signed_in)):
    selected = await _selected(request, account)
    if selected is None:
        return _to_fleet()
    machine, report = selected
    if report is None:
        return _no_report_page(request, account)
    ui = _resolve(request, report)
    tiles = build_tiles(REMOTE_VIEWER_PERMISSIONS, {"Health": _fault_count(report)})
    return ui.templates.TemplateResponse(
        request,
        "home.html",
        _context(
            request,
            account,
            ui,
            LEVEL_HOME,
            machine=machine,
            report=report,
            tiles=tiles,
        ),
    )


@router.get("/status", response_class=HTMLResponse)
async def status_fragment(
    request: Request, account: Account = Depends(require_signed_in)
):
    selected = await _selected(request, account)
    if selected is None:
        return _no_access_for_fragment(request)
    machine, report = selected
    if report is None or report.health.vmc_missing:
        return HTMLResponse(VMC_MISSING_HTML)
    ui = _resolve(request, report)
    h = report.health
    return ui.templates.TemplateResponse(
        request,
        "partials/status_fragment.html",
        _context(
            request,
            account,
            ui,
            None,
            machine=machine,
            report=report,
            banner=False,
            status=h.status,
            is_healthy=h.is_healthy,
            issues=h.issues,
            active_faults=h.active_faults,
            payment_enabled=h.payment_enabled,
            payment_reasons=h.payment_reasons,
            machine_stopped=h.machine_stopped,
            maintenance=h.maintenance,
        ),
    )


@router.get("/pill", response_class=HTMLResponse)
async def pill(request: Request, account: Account = Depends(require_signed_in)):
    selected = await _selected(request, account)
    if selected is None or selected[1] is None:
        return _no_access_for_fragment(request)
    machine, report = selected
    ui = _resolve(request, report)
    state, text = pill_state(report.health)
    return ui.templates.TemplateResponse(
        request,
        "partials/pill.html",
        _context(
            request,
            account,
            ui,
            None,
            machine=machine,
            report=report,
            banner=False,
            pill_state=state,
            pill_text=text,
        ),
    )


@router.get("/kpi", response_class=HTMLResponse)
async def kpi_fragment(request: Request, account: Account = Depends(require_signed_in)):
    selected = await _selected(request, account)
    if selected is None or selected[1] is None:
        return _no_access_for_fragment(request)
    machine, report = selected
    ui = _resolve(request, report)
    return ui.templates.TemplateResponse(
        request,
        "partials/kpi_fragment.html",
        _context(
            request,
            account,
            ui,
            None,
            machine=machine,
            report=report,
            banner=False,
            summary=report.kpi.summary,
            average=report.kpi.average,
        ),
    )


@router.get("/health", response_class=HTMLResponse)
async def health_home(request: Request, account: Account = Depends(require_signed_in)):
    selected = await _selected(request, account)
    if selected is None:
        return _to_fleet()
    machine, report = selected
    if report is None:
        return _no_report_page(request, account)
    ui = _resolve(request, report)
    page = report.health_page
    tiles = health_tiles(
        subsystems=f"{page.subsystems_ok}/{page.subsystems_total} OK",
        # The edge counts its (possibly empty) fault list here, so a machine
        # without a VMC reads "0 active faults" on both sides.
        faults=fault_count_line(len(report.faults)),
        availability=availability_line(
            report.health.payment_enabled, len(report.health.payment_reasons)
        ),
        logs="Last 50 lines",
        perms=REMOTE_VIEWER_PERMISSIONS,
    )
    return ui.templates.TemplateResponse(
        request,
        "health.html",
        _context(
            request,
            account,
            ui,
            LEVEL_HEALTH,
            machine=machine,
            report=report,
            tiles=tiles,
            mqtt_connected=page.mqtt_connected,
            vmc_state=page.vmc_state,
            vmc_build=page.vmc_build,
        ),
    )


@router.get("/health/faults", response_class=HTMLResponse)
async def faults_view(request: Request, account: Account = Depends(require_signed_in)):
    selected = await _selected(request, account)
    if selected is None:
        return _to_fleet()
    machine, report = selected
    if report is None:
        return _no_report_page(request, account)
    ui = _resolve(request, report)
    return ui.templates.TemplateResponse(
        request,
        "health_faults.html",
        _context(
            request,
            account,
            ui,
            LEVEL_HEALTH_FAULTS,
            machine=machine,
            report=report,
            faults=report.faults,
            can_clear=False,
            oob=False,
        ),
    )


@router.get("/static/{path:path}")
async def static_file(
    request: Request, path: str, account: Account | None = Depends(current_account)
):
    """Static files stay public (the sign-in page needs its stylesheet); a
    signed-in viewer with access gets the selected machine's own bundle."""
    report = None
    if account is not None:
        selected = await _selected(request, account)
        report = selected[1] if selected else None
    ui = _resolve(request, report)
    try:
        base = ui.static_dir.resolve()
        target = (base / path).resolve()
    except (OSError, ValueError):
        return Response(status_code=404)
    if not target.is_relative_to(base) or not target.is_file():
        return Response(status_code=404)
    if ui.sandboxed:
        return FileResponse(
            target,
            headers={
                "Content-Security-Policy": "sandbox; default-src 'none'",
                "X-Content-Type-Options": "nosniff",
            },
        )
    return FileResponse(target)


@router.get("/{path:path}", response_class=HTMLResponse)
@router.post("/{path:path}", response_class=HTMLResponse)
async def not_remote(
    request: Request, path: str, account: Account = Depends(require_signed_in)
):
    selected = await _selected(request, account)
    machine, report = selected if selected else (None, None)
    ui = _resolve(request, report)
    return ui.templates.TemplateResponse(
        request,
        "not_remote.html",
        _context(request, account, ui, None, machine=machine, report=report),
        status_code=404,
    )
