"""GET/POST /settings/comms and POST /settings/comms/test."""

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from loguru import logger
from pydantic import SecretStr

from ice_edge.config import config_store
from ice_edge.access.access import Permission
from ice_edge.notify.mailer import send_email
from ice_edge.web import auth as web_auth
from ice_edge.web import context
from ice_ui.levels import (
    LEVEL_SETTINGS_COMMS,
)
from ice_edge.web.routes.settings.secrets import SECRET_MASK, _secret_changed
from ice_ui.templating import templates


router = APIRouter()


# ---------------------------------------------------------------- Comms


def _render_comms_form(request: Request, *, error: str | None):
    comm = context.config.communication
    return templates.TemplateResponse(
        request,
        "settings_comms.html",
        context.template_context(
            request,
            level=LEVEL_SETTINGS_COMMS,
            email=comm.email_gateway,
            sms=comm.sms_gateway,
            email_password=SECRET_MASK,
            sms_account_sid=SECRET_MASK,
            sms_auth_token=SECRET_MASK,
            error=error,
        ),
    )


@router.get(
    "/settings/comms",
    response_class=HTMLResponse,
    dependencies=[Depends(web_auth.require(Permission.edit_secrets))],
)
async def comms_form(request: Request):
    return _render_comms_form(request, error=None)


@router.post(
    "/settings/comms",
    response_class=HTMLResponse,
    dependencies=[
        Depends(web_auth.require(Permission.edit_secrets)),
        Depends(context.require_htmx),
    ],
)
async def update_comms(
    request: Request,
    smtp_server: str = Form(...),
    smtp_port: int = Form(...),
    smtp_username: str = Form(""),
    smtp_password: str = Form(SECRET_MASK),
    default_from: str = Form(""),
    sms_account_sid: str = Form(SECRET_MASK),
    sms_auth_token: str = Form(SECRET_MASK),
    sms_from_number: str = Form(""),
):
    email = context.config.communication.email_gateway
    email.smtp_server = smtp_server
    email.smtp_port = smtp_port
    email.username = smtp_username
    email.default_from = default_from
    if _secret_changed(smtp_password):
        email.password = SecretStr(smtp_password)

    sms = context.config.communication.sms_gateway
    sms.from_number = sms_from_number
    if _secret_changed(sms_account_sid):
        sms.account_sid = SecretStr(sms_account_sid)
    if _secret_changed(sms_auth_token):
        sms.auth_token = SecretStr(sms_auth_token)

    try:
        config_store.save_config(context.config)
    except Exception as e:
        logger.error(f"settings/comms: save_config failed: {e}")
        return _render_comms_form(request, error=f"Could not save changes: {e}")
    return HTMLResponse("", headers={"HX-Redirect": "/settings/comms"})


@router.post(
    "/settings/comms/test",
    response_class=HTMLResponse,
    dependencies=[
        Depends(web_auth.require(Permission.edit_secrets)),
        Depends(context.require_htmx),
    ],
)
async def send_test_email(request: Request):
    """Send a one-line test email through the configured gateway.

    Addressed to the machine owner's email (config.physical.people.
    machine_owner.email) — the obvious choice per brief resolution 8,
    since that is the one contact this page cannot help but already
    know. Never raises and never 500s: an unconfigured gateway (still
    pointing at the blank-config placeholder host) is rejected before
    any network call, exactly like context._can_email_owner's own
    is_configured check, so a test run against the shipped defaults
    cannot hang on a real SMTP connection attempt.
    """
    gateway = context.config.communication.email_gateway
    owner_email = context.config.physical.people.machine_owner.email
    if not gateway.is_configured:
        return templates.TemplateResponse(
            request,
            "partials/comms_test_result.html",
            context.template_context(
                request, test_status="not_configured", owner_email=owner_email
            ),
        )
    ok = await send_email(
        gateway,
        owner_email,
        "ice-colder test email",
        "This is a test message from the ice-colder dashboard's Settings > Comms page.",
    )
    return templates.TemplateResponse(
        request,
        "partials/comms_test_result.html",
        context.template_context(
            request,
            test_status="ok" if ok else "failed",
            owner_email=owner_email,
        ),
    )
