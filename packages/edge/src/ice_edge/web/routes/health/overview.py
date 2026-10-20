"""The /health landing page (four-tile summary plus build identity)."""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from ice_edge.access.access import Permission
from ice_edge.uplink.identity import cloud_handles_alerts
from ice_edge.uplink.client import utc_now
from ice_edge.views.health import build_health_page_summary
from ice_edge.web import auth as web_auth
from ice_edge.web import context
from ice_ui.levels import LEVEL_HEALTH
from ice_ui.templating import templates
from ice_ui.tiles import fault_count_line, health_tiles
from ice_edge.web.routes.health.availability import _availability_context_line
from ice_edge.web.routes.health.faults import _faults_with_age
from ice_edge.web.routes.health.subsystems import _subsystem_summary

router = APIRouter()


def _alerts_mode() -> str | None:
    """ "cloud" or "local" when the uplink identity exists, else None."""
    identity = context.machine_identity
    if identity is None:
        return None
    interval = context.config.cloud.report_interval_seconds
    return "cloud" if cloud_handles_alerts(identity, interval, utc_now()) else "local"


@router.get(
    "/health",
    response_class=HTMLResponse,
    dependencies=[Depends(web_auth.require(Permission.view_status))],
)
async def health_home(request: Request):
    principal = web_auth.current_principal(request)
    perms = principal.perms if principal else frozenset()
    monitor_summary = (
        context.health_monitor.get_summary() if context.health_monitor else None
    )
    subsystems = _subsystem_summary()
    faults = _faults_with_age()
    page = build_health_page_summary(monitor_summary, subsystems)

    # Resolution 8: the four sub-tile dicts partials/tile.html expects are
    # built by ice_ui.tiles.health_tiles (shared with the cloud portal).
    tiles = health_tiles(
        subsystems=f"{page.subsystems_ok}/{page.subsystems_total} OK",
        faults=fault_count_line(len(faults)),
        availability=_availability_context_line(context.availability),
        logs="Last 50 lines",
        perms=perms,
    )

    return templates.TemplateResponse(
        request,
        "health.html",
        context.template_context(
            request,
            level=LEVEL_HEALTH,
            tiles=tiles,
            mqtt_connected=page.mqtt_connected,
            vmc_state=page.vmc_state,
            vmc_build=page.vmc_build,
            alerts_mode=_alerts_mode(),
            cloud_fingerprint=(
                context.machine_identity.short_fingerprint
                if context.machine_identity
                else None
            ),
        ),
    )
