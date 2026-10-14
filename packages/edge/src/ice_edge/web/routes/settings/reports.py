"""GET/POST /settings/reports."""

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from loguru import logger
from pydantic import ValidationError

from ice_common.config_model import ReportsConfig
from ice_edge.config import config_store
from ice_edge.access.access import Permission
from ice_edge.web import auth as web_auth
from ice_edge.web import context
from ice_ui.levels import (
    LEVEL_SETTINGS_REPORTS,
)
from ice_ui.templating import templates


router = APIRouter()


def _parse_extra_recipients(raw: str) -> list[str]:
    """One address per line (commas also accepted, matching /settings/web's
    trusted_proxies convention in this same module) -> a clean list with
    blank lines dropped. A wholly blank textarea must produce `[]`, never
    `[""]` — the value that would later make the scheduler try to email an
    empty address (task 12 brief)."""
    return [
        line.strip() for line in raw.replace(",", "\n").splitlines() if line.strip()
    ]


def _reports_validation_message(exc: ValidationError) -> str:
    """A human-readable reason for a rejected /settings/reports submission
    (out-of-range hour/weekday), not the raw pydantic exception text."""
    details = "; ".join(
        f"{err['loc'][-1] if err['loc'] else 'value'}: {err['msg']}"
        for err in exc.errors()
    )
    return f"Invalid report settings — {details}"


# ---------------------------------------------------------------- Reports


def _render_reports_form(
    request: Request,
    *,
    error: str | None,
    schedule: str | None = None,
    hour: int | None = None,
    weekday: int | None = None,
    extra_recipients: str | None = None,
):
    """Renders from the live config by default; a rejected submission
    (invalid hour/weekday) passes its raw submitted values through
    instead so the user sees what they typed rather than the last
    saved values, without those rejected values ever touching
    context.config (spec: "not a silently clamped value")."""
    reports = context.config.reports
    return templates.TemplateResponse(
        request,
        "settings_reports.html",
        context.template_context(
            request,
            level=LEVEL_SETTINGS_REPORTS,
            schedule=schedule if schedule is not None else reports.schedule,
            hour=hour if hour is not None else reports.hour,
            weekday=weekday if weekday is not None else reports.weekday,
            extra_recipients=(
                extra_recipients
                if extra_recipients is not None
                else "\n".join(reports.extra_recipients)
            ),
            error=error,
        ),
    )


@router.get(
    "/settings/reports",
    response_class=HTMLResponse,
    dependencies=[Depends(web_auth.require(Permission.edit_contacts))],
)
async def reports_form(request: Request):
    return _render_reports_form(request, error=None)


@router.post(
    "/settings/reports",
    response_class=HTMLResponse,
    dependencies=[
        Depends(web_auth.require(Permission.edit_contacts)),
        Depends(context.require_htmx),
    ],
)
async def update_reports(
    request: Request,
    schedule: str = Form(...),
    hour: int = Form(...),
    weekday: int = Form(...),
    extra_recipients: str = Form(""),
):
    recipients = _parse_extra_recipients(extra_recipients)
    try:
        new_reports = ReportsConfig(
            schedule=schedule,
            hour=hour,
            weekday=weekday,
            extra_recipients=recipients,
        )
    except ValidationError as e:
        # Rejected before context.config.reports is ever touched: the
        # stored config must be provably unchanged (brief's
        # "hour=24/weekday=7 change nothing" requirement), not merely
        # unchanged by coincidence.
        return _render_reports_form(
            request,
            error=_reports_validation_message(e),
            schedule=schedule,
            hour=hour,
            weekday=weekday,
            extra_recipients=extra_recipients,
        )

    context.config.reports = new_reports
    try:
        config_store.save_config(context.config)
    except Exception as e:
        logger.error(f"settings/reports: save_config failed: {e}")
        return _render_reports_form(request, error=f"Could not save changes: {e}")
    return HTMLResponse("", headers={"HX-Redirect": "/settings/reports"})
