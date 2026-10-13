"""GET/POST /settings/payments."""

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from loguru import logger
from pydantic import SecretStr

from services import config_store
from services.access import Permission
from web_interface import auth as web_auth
from web_interface import context
from web_interface.levels import (
    LEVEL_SETTINGS_PAYMENTS,
)
from web_interface.routes.settings.secrets import SECRET_MASK, _secret_changed
from web_interface.templating import templates


router = APIRouter()


# ---------------------------------------------------------------- Payments


def _render_payments_form(request: Request, *, error: str | None):
    payment = context.config.payment
    return templates.TemplateResponse(
        request,
        "settings_payments.html",
        context.template_context(
            request,
            level=LEVEL_SETTINGS_PAYMENTS,
            stripe_api_key=SECRET_MASK,
            stripe_webhook_secret=SECRET_MASK,
            paypal=payment.paypal,
            paypal_client_id=(SECRET_MASK if payment.paypal else ""),
            paypal_client_secret=(SECRET_MASK if payment.paypal else ""),
            mdb=payment.mdb,
            error=error,
        ),
    )


@router.get(
    "/settings/payments",
    response_class=HTMLResponse,
    dependencies=[Depends(web_auth.require(Permission.edit_secrets))],
)
async def payments_form(request: Request):
    return _render_payments_form(request, error=None)


@router.post(
    "/settings/payments",
    response_class=HTMLResponse,
    dependencies=[
        Depends(web_auth.require(Permission.edit_secrets)),
        Depends(context.require_htmx),
    ],
)
async def update_payments(
    request: Request,
    stripe_api_key: str = Form(SECRET_MASK),
    stripe_webhook_secret: str = Form(SECRET_MASK),
    paypal_client_id: str = Form(SECRET_MASK),
    paypal_client_secret: str = Form(SECRET_MASK),
):
    stripe = context.config.payment.stripe
    if _secret_changed(stripe_api_key):
        stripe.api_key = SecretStr(stripe_api_key)
    if _secret_changed(stripe_webhook_secret):
        stripe.webhook_secret = SecretStr(stripe_webhook_secret)

    # PayPal is Optional[PayPalConfig] — a config with paypal explicitly
    # set to null has nothing to write these into, and this page does
    # not invent a fresh PayPalConfig on the user's behalf (that would
    # silently "enable" PayPal with two secrets and nothing else set).
    paypal = context.config.payment.paypal
    if paypal is not None:
        if _secret_changed(paypal_client_id):
            paypal.client_id = SecretStr(paypal_client_id)
        if _secret_changed(paypal_client_secret):
            paypal.client_secret = SecretStr(paypal_client_secret)

    try:
        config_store.save_config(context.config)
    except Exception as e:
        logger.error(f"settings/payments: save_config failed: {e}")
        return _render_payments_form(request, error=f"Could not save changes: {e}")
    return HTMLResponse("", headers={"HX-Redirect": "/settings/payments"})
