"""The read-only fleet portal (cloud spec sections 2, 4, 5).

Machine pages are served at the machine's own paths and rendered through the
same ``ice_ui`` templates, tile builders and pill logic the edge routes use
(never re-implemented here), from the selected machine's latest stored report.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response

from ice_cloud.ui_registry import ResolvedUi
from ice_contracts.access import REMOTE_VIEWER_PERMISSIONS
from ice_contracts.cloud.report import MachineReport
from ice_ui.filters import humanize_seconds
from ice_ui.levels import LEVEL_HEALTH, LEVEL_HEALTH_FAULTS, LEVEL_HOME, Level
from ice_ui.tiles import availability_line, build_tiles, fault_count_line, health_tiles
from ice_ui.views.health import VMC_MISSING_HTML, pill_state

COOKIE = "ice_cloud_machine"
STALE_FACTOR = 3
LEVEL_FLEET = Level(title="Fleet", url="/fleet")

router = APIRouter()


@dataclass(frozen=True)
class RemoteViewer:
    """``current_user`` for the remote viewer; ``base.html`` reads no
    attribute of it today (spec section 5)."""

    display_name: str = "Fleet viewer"


VIEWER = RemoteViewer()


def _age_seconds(request: Request, report: MachineReport) -> float:
    now: datetime = request.app.state.now()
    return (now - report.sent_at).total_seconds()


def _is_stale(request: Request, report: MachineReport) -> bool:
    return _age_seconds(request, report) > STALE_FACTOR * report.report_interval_seconds


def _selected(request: Request) -> MachineReport | None:
    machine_id = request.cookies.get(COOKIE)
    if not machine_id:
        return None
    try:
        return request.app.state.store.latest(machine_id)
    except ValueError:
        return None


def _resolve(request: Request, report: MachineReport | None) -> ResolvedUi:
    registry = request.app.state.registry
    return registry.resolve(report.ui.digest if report else registry.installed_digest)


def _remote(request: Request, report: MachineReport, ui: ResolvedUi) -> dict:
    age = max(_age_seconds(request, report), 0.0)
    return {
        "machine_id": report.machine_id,
        "last_report_local": report.sent_at.astimezone().strftime("%Y-%m-%d %H:%M:%S"),
        "age_text": humanize_seconds(age),
        "stale": _is_stale(request, report),
        "ui_mismatch": ui.mismatch,
        "report_digest": report.ui.digest,
        "portal_digest": request.app.state.registry.installed_digest,
    }


def _context(
    request: Request,
    report: MachineReport | None,
    ui: ResolvedUi,
    level: Level | None,
    *,
    banner: bool = True,
    **extra,
) -> dict:
    ctx = {
        "request": request,
        "current_user": VIEWER,
        "perms": REMOTE_VIEWER_PERMISSIONS,
        "level": level,
    }
    if banner and report is not None:
        ctx["remote"] = _remote(request, report, ui)
    ctx.update(extra)
    return ctx


def _to_fleet() -> RedirectResponse:
    return RedirectResponse("/fleet", status_code=303)


def _no_selection_for_fragment(request: Request) -> Response:
    """A polled fragment (/status, /pill, /kpi) with no machine selected.

    htmx follows a 303 and would swap the whole /fleet page into the
    fragment's slot, so an htmx request gets 204 (htmx swaps nothing); a plain
    request still gets the spec's 303 to /fleet."""
    if request.headers.get("HX-Request") == "true":
        return Response(status_code=204)
    return _to_fleet()


def _fault_count(report: MachineReport) -> str:
    return fault_count_line(None if report.health.vmc_missing else len(report.faults))


@router.get("/fleet", response_class=HTMLResponse)
async def fleet(request: Request):
    rows = []
    for fingerprint, report in request.app.state.store.machines():
        rows.append(
            {
                "fingerprint": fingerprint,
                "machine_id": report.machine_id,
                "stale": _is_stale(request, report),
                "age_text": humanize_seconds(max(_age_seconds(request, report), 0.0)),
                "fault_line": _fault_count(report),
            }
        )
    ui = _resolve(request, None)
    return ui.templates.TemplateResponse(
        request, "fleet.html", _context(request, None, ui, LEVEL_FLEET, machines=rows)
    )


@router.post("/fleet/select/{fingerprint}")
async def select_machine(request: Request, fingerprint: str):
    try:
        known = request.app.state.store.latest(fingerprint)
    except ValueError:
        known = None
    if known is None:
        return Response(status_code=404)
    response = RedirectResponse("/", status_code=303)
    response.set_cookie(COOKIE, fingerprint, httponly=True, samesite="lax")
    return response


@router.get("/", response_class=HTMLResponse)
async def home(request: Request):
    report = _selected(request)
    if report is None:
        return _to_fleet()
    ui = _resolve(request, report)
    tiles = build_tiles(REMOTE_VIEWER_PERMISSIONS, {"Health": _fault_count(report)})
    return ui.templates.TemplateResponse(
        request,
        "home.html",
        _context(request, report, ui, LEVEL_HOME, tiles=tiles),
    )


@router.get("/status", response_class=HTMLResponse)
async def status_fragment(request: Request):
    report = _selected(request)
    if report is None:
        return _no_selection_for_fragment(request)
    if report.health.vmc_missing:
        return HTMLResponse(VMC_MISSING_HTML)
    ui = _resolve(request, report)
    h = report.health
    return ui.templates.TemplateResponse(
        request,
        "partials/status_fragment.html",
        _context(
            request,
            report,
            ui,
            None,
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
async def pill(request: Request):
    report = _selected(request)
    if report is None:
        return _no_selection_for_fragment(request)
    ui = _resolve(request, report)
    state, text = pill_state(report.health)
    return ui.templates.TemplateResponse(
        request,
        "partials/pill.html",
        _context(
            request, report, ui, None, banner=False, pill_state=state, pill_text=text
        ),
    )


@router.get("/kpi", response_class=HTMLResponse)
async def kpi_fragment(request: Request):
    report = _selected(request)
    if report is None:
        return _no_selection_for_fragment(request)
    ui = _resolve(request, report)
    return ui.templates.TemplateResponse(
        request,
        "partials/kpi_fragment.html",
        _context(
            request,
            report,
            ui,
            None,
            banner=False,
            summary=report.kpi.summary,
            average=report.kpi.average,
        ),
    )


@router.get("/health", response_class=HTMLResponse)
async def health_home(request: Request):
    report = _selected(request)
    if report is None:
        return _to_fleet()
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
            report,
            ui,
            LEVEL_HEALTH,
            tiles=tiles,
            mqtt_connected=page.mqtt_connected,
            vmc_state=page.vmc_state,
            vmc_build=page.vmc_build,
        ),
    )


@router.get("/health/faults", response_class=HTMLResponse)
async def faults_view(request: Request):
    report = _selected(request)
    if report is None:
        return _to_fleet()
    ui = _resolve(request, report)
    return ui.templates.TemplateResponse(
        request,
        "health_faults.html",
        _context(
            request,
            report,
            ui,
            LEVEL_HEALTH_FAULTS,
            faults=report.faults,
            can_clear=False,
            oob=False,
        ),
    )


@router.get("/static/{path:path}")
async def static_file(request: Request, path: str):
    ui = _resolve(request, _selected(request))
    try:
        base = ui.static_dir.resolve()
        target = (base / path).resolve()
    except (OSError, ValueError):
        return Response(status_code=404)
    if not target.is_relative_to(base) or not target.is_file():
        return Response(status_code=404)
    return FileResponse(target)


@router.get("/{path:path}", response_class=HTMLResponse)
@router.post("/{path:path}", response_class=HTMLResponse)
async def not_remote(request: Request, path: str):
    report = _selected(request)
    ui = _resolve(request, report)
    return ui.templates.TemplateResponse(
        request,
        "not_remote.html",
        _context(request, report, ui, None),
        status_code=404,
    )
