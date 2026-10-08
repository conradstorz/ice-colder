"""The Settings levels: /settings and its six sub-levels — Machine,
Contacts, Payments, Comms, MQTT and Web (spec §2, §5).

Two of the six pages never existed before this task: /config/payments and
/config/comms (routes/legacy.py) were left without working templates in
part 1, and their coverage in tests/test_web_routes.py has been
@pytest.mark.skip'd ever since. This module's /settings/payments and
/settings/comms are the real, working replacements — see
.superpowers/sdd/part2/task-13-brief.md, executor resolution 2, for why
those two skips could not be deleted from this task (tests/test_web_routes.py
is owned by another task in this wave).

Masking (brief resolution 4): a SecretStr that is already set must never
round-trip its real value through the browser. Every masked field renders
the fixed `SECRET_MASK` placeholder instead, and `_secret_changed` is the
one helper every masked field's POST handler calls to decide whether the
submitted value is a genuinely new secret (overwrite) or the unchanged
placeholder / a blank left untouched (leave the stored value alone).
Repeating that comparison inline per field is exactly how one field would
end up missing the check, so every page below calls this same function.

MQTT env overrides (brief resolution 6): the shared
`services.startup_config.mqtt_env_overrides()` helper identifies active
fields at request time. The startup loader separately applies those values
to a copy of `config.mqtt`, so an env-only MQTT_PASSWORD is never written
back into config.json by a later save. Keeping this helper in a service
module avoids importing main.py and creating a cycle through the web app.
"""

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from loguru import logger
from pydantic import SecretStr, ValidationError

from config.config_model import Channel, ReportsConfig
from services import config_store
from services.access import Permission
from services.mailer import send_email
from services.startup_config import mqtt_env_overrides
from web_interface import auth as web_auth
from web_interface import context
from web_interface.levels import (
    LEVEL_SETTINGS,
    LEVEL_SETTINGS_COMMS,
    LEVEL_SETTINGS_CONTACTS,
    LEVEL_SETTINGS_MACHINE,
    LEVEL_SETTINGS_MQTT,
    LEVEL_SETTINGS_PAYMENTS,
    LEVEL_SETTINGS_REPORTS,
    LEVEL_SETTINGS_WEB,
)

# Fixed placeholder rendered for any secret that is already set, real value
# or dummy default alike — never the actual secret text (brief resolution
# 4). A constant length that does not hint at the real secret's length.
SECRET_MASK = "********"


def _secret_placeholder(secret) -> str:
    """The value a masked <input> should render: the fixed mask when
    *secret* is already set (an Optional field that is None renders empty,
    so a first value can be typed)."""
    return SECRET_MASK if secret is not None else ""


def _secret_changed(submitted: str) -> bool:
    """True when *submitted* is a genuinely new secret value rather than
    the unchanged mask this page renders for an already-set secret, or a
    blank the user left untouched. Shared by every masked field on
    Payments, Comms and MQTT (brief resolution 4) so the same rule applies
    everywhere — an empty submission is treated as "leave alone" rather
    than a request to clear the credential, since nothing in this task
    asks for an explicit clear action and silently wiping a credential
    because a field arrived blank would be the destructive mistake this
    rule exists to prevent.
    """
    return submitted != SECRET_MASK and submitted != ""


def _require_any(*permissions: Permission):
    """FastAPI dependency: a live session holding at least one of
    *permissions*. Copied from web_interface/routes/products.py (not
    imported from — brief resolution 13 scopes this module to its own
    files); web_auth.require() is AND-only and GET /settings needs OR
    semantics (edit_contacts or edit_secrets)."""

    def dependency(request: Request) -> web_auth.Principal:
        principal = web_auth.current_principal(request)
        if principal is None:
            raise web_auth._unauthenticated(request)
        if not any(p in principal.perms for p in permissions):
            raise HTTPException(status_code=403, detail="Not permitted")
        return principal

    return dependency


def _channels_from_form(values: list[str]) -> list[Channel]:
    """Filter posted channel checkbox values down to valid Channel members,
    silently dropping anything else rather than raising on a stray value."""
    valid = {c.value for c in Channel}
    return [Channel(v) for v in values if v in valid]


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


def build_router(templates: Jinja2Templates) -> APIRouter:
    router = APIRouter()

    # ---------------------------------------------------------------- /settings

    @router.get(
        "/settings",
        response_class=HTMLResponse,
        dependencies=[
            Depends(_require_any(Permission.edit_contacts, Permission.edit_secrets))
        ],
    )
    async def settings_home(request: Request):
        principal = web_auth.current_principal(request)
        perms = principal.perms if principal else frozenset()
        has_contacts = Permission.edit_contacts in perms
        has_secrets = Permission.edit_secrets in perms
        tiles = [
            {
                "title": "Machine",
                "url": "/settings/machine",
                "icon": "settings",
                "context": "Name, location",
                "enabled": has_contacts,
                "coming_soon": False,
            },
            {
                "title": "Contacts",
                "url": "/settings/contacts",
                "icon": "settings",
                "context": "Owner, technicians",
                "enabled": has_contacts,
                "coming_soon": False,
            },
            {
                "title": "Payments",
                "url": "/settings/payments",
                "icon": "settings",
                "context": "Stripe, PayPal, MDB",
                "enabled": has_secrets,
                "coming_soon": False,
            },
            {
                "title": "Comms",
                "url": "/settings/comms",
                "icon": "settings",
                "context": "Email, SMS gateways",
                "enabled": has_secrets,
                "coming_soon": False,
            },
            {
                "title": "MQTT",
                "url": "/settings/mqtt",
                "icon": "settings",
                "context": "Broker, credentials",
                "enabled": has_secrets,
                "coming_soon": False,
            },
            {
                "title": "Web",
                "url": "/settings/web",
                "icon": "settings",
                "context": "Host, port, proxies",
                "enabled": has_secrets,
                "coming_soon": False,
            },
            {
                "title": "Reports",
                "url": "/settings/reports",
                "icon": "reports",
                "context": "Scheduled summary email",
                "enabled": has_contacts,
                "coming_soon": False,
            },
        ]
        return templates.TemplateResponse(
            "settings.html",
            context.template_context(request, level=LEVEL_SETTINGS, tiles=tiles),
        )

    # ---------------------------------------------------------------- Machine

    def _render_machine_form(request: Request, *, error: str | None):
        physical = context.config.physical
        return templates.TemplateResponse(
            "settings_machine.html",
            context.template_context(
                request,
                level=LEVEL_SETTINGS_MACHINE,
                machine_id=context.config.machine_id,
                name=physical.common_name,
                address=physical.location.address,
                notes=physical.location.notes or "",
                error=error,
            ),
        )

    @router.get(
        "/settings/machine",
        response_class=HTMLResponse,
        dependencies=[Depends(web_auth.require(Permission.edit_contacts))],
    )
    async def machine_form(request: Request):
        return _render_machine_form(request, error=None)

    @router.post(
        "/settings/machine",
        response_class=HTMLResponse,
        dependencies=[
            Depends(web_auth.require(Permission.edit_contacts)),
            Depends(context.require_htmx),
        ],
    )
    async def update_machine(
        request: Request,
        name: str = Form(...),
        address: str = Form(...),
        notes: str = Form(""),
        # Accepted so a direct POST supplying it doesn't 422 — and ignored:
        # machine id is read-only (spec §8, brief resolution 10). A disabled
        # <input> is not a guard on its own, so this handler never reads it.
        machine_id: str | None = Form(None),
    ):
        physical = context.config.physical
        physical.common_name = name
        physical.location.address = address
        physical.location.notes = notes
        try:
            config_store.save_config(context.config)
        except Exception as e:
            logger.error(f"settings/machine: save_config failed: {e}")
            return _render_machine_form(request, error=f"Could not save changes: {e}")
        return HTMLResponse("", headers={"HX-Redirect": "/settings/machine"})

    # ---------------------------------------------------------------- Contacts

    def _render_contacts_form(request: Request, *, error: str | None):
        people = context.config.physical.people
        return templates.TemplateResponse(
            "settings_contacts.html",
            context.template_context(
                request,
                level=LEVEL_SETTINGS_CONTACTS,
                owner=people.machine_owner,
                location_owner=people.location_owner,
                technicians=people.service_technicians,
                channels=list(Channel),
                error=error,
            ),
        )

    @router.get(
        "/settings/contacts",
        response_class=HTMLResponse,
        dependencies=[Depends(web_auth.require(Permission.edit_contacts))],
    )
    async def contacts_form(request: Request):
        return _render_contacts_form(request, error=None)

    @router.post(
        "/settings/contacts",
        response_class=HTMLResponse,
        dependencies=[
            Depends(web_auth.require(Permission.edit_contacts)),
            Depends(context.require_htmx),
        ],
    )
    async def update_contacts(
        request: Request,
        owner_name: str = Form(...),
        owner_email: str = Form(...),
        owner_phone: str = Form(""),
        owner_address: str = Form(""),
        owner_notes: str = Form(""),
        owner_channels: list[str] = Form([]),
        loc_name: str = Form(...),
        loc_email: str = Form(...),
        loc_phone: str = Form(""),
        loc_address: str = Form(""),
        loc_notes: str = Form(""),
        loc_channels: list[str] = Form([]),
    ):
        people = context.config.physical.people
        owner = people.machine_owner
        owner.name = owner_name
        owner.email = owner_email
        owner.phone = owner_phone
        owner.address = owner_address
        owner.notes = owner_notes
        owner.preferred_comm = _channels_from_form(owner_channels)

        loc = people.location_owner
        loc.name = loc_name
        loc.email = loc_email
        loc.phone = loc_phone
        loc.address = loc_address
        loc.notes = loc_notes
        loc.preferred_comm = _channels_from_form(loc_channels)

        try:
            config_store.save_config(context.config)
        except Exception as e:
            logger.error(f"settings/contacts: save_config failed: {e}")
            return _render_contacts_form(request, error=f"Could not save changes: {e}")
        return HTMLResponse("", headers={"HX-Redirect": "/settings/contacts"})

    # ---------------------------------------------------------------- Payments

    def _render_payments_form(request: Request, *, error: str | None):
        payment = context.config.payment
        return templates.TemplateResponse(
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

    # ---------------------------------------------------------------- Comms

    def _render_comms_form(request: Request, *, error: str | None):
        comm = context.config.communication
        return templates.TemplateResponse(
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
                "partials/comms_test_result.html",
                context.template_context(
                    request, test_status="not_configured", owner_email=owner_email
                ),
            )
        ok = await send_email(
            gateway,
            owner_email,
            "ice-colder test email",
            "This is a test message from the ice-colder dashboard's "
            "Settings > Comms page.",
        )
        return templates.TemplateResponse(
            "partials/comms_test_result.html",
            context.template_context(
                request,
                test_status="ok" if ok else "failed",
                owner_email=owner_email,
            ),
        )

    # ---------------------------------------------------------------- MQTT

    def _render_mqtt_form(request: Request, *, error: str | None):
        mqtt = context.config.mqtt
        overrides = mqtt_env_overrides()
        return templates.TemplateResponse(
            "settings_mqtt.html",
            context.template_context(
                request,
                level=LEVEL_SETTINGS_MQTT,
                broker_host=overrides.get("broker_host", mqtt.broker_host),
                broker_port=mqtt.broker_port,
                username=overrides.get("username", mqtt.username or ""),
                password=_secret_placeholder(mqtt.password)
                if "password" not in overrides
                else SECRET_MASK,
                overrides=overrides,
                error=error,
            ),
        )

    @router.get(
        "/settings/mqtt",
        response_class=HTMLResponse,
        dependencies=[Depends(web_auth.require(Permission.edit_secrets))],
    )
    async def mqtt_form(request: Request):
        return _render_mqtt_form(request, error=None)

    @router.post(
        "/settings/mqtt",
        response_class=HTMLResponse,
        dependencies=[
            Depends(web_auth.require(Permission.edit_secrets)),
            Depends(context.require_htmx),
        ],
    )
    async def update_mqtt(
        request: Request,
        broker_host: str = Form(...),
        broker_port: int = Form(...),
        username: str = Form(""),
        password: str = Form(SECRET_MASK),
    ):
        overrides = mqtt_env_overrides()
        mqtt = context.config.mqtt

        # A field with a live env override is read-only on this page (its
        # <input> is disabled) and its submitted value — whatever a direct
        # POST might supply — is ignored here too, the same "read-only
        # means the handler ignores it" rule spec §8 applies to machine id
        # / web host / port (brief resolutions 6 and 10). This is what
        # keeps an env-only MQTT_PASSWORD out of config.json forever.
        if "broker_host" not in overrides:
            mqtt.broker_host = broker_host
        if "username" not in overrides:
            mqtt.username = username or None
        if "password" not in overrides and _secret_changed(password):
            mqtt.password = SecretStr(password)
        mqtt.broker_port = broker_port

        try:
            config_store.save_config(context.config)
        except Exception as e:
            logger.error(f"settings/mqtt: save_config failed: {e}")
            return _render_mqtt_form(request, error=f"Could not save changes: {e}")
        return HTMLResponse("", headers={"HX-Redirect": "/settings/mqtt"})

    # ---------------------------------------------------------------- Web

    def _render_web_form(request: Request, *, error: str | None):
        web = context.config.web
        return templates.TemplateResponse(
            "settings_web.html",
            context.template_context(
                request,
                level=LEVEL_SETTINGS_WEB,
                host=web.host,
                port=web.port,
                trusted_proxies="\n".join(web.trusted_proxies),
                error=error,
            ),
        )

    @router.get(
        "/settings/web",
        response_class=HTMLResponse,
        dependencies=[Depends(web_auth.require(Permission.edit_secrets))],
    )
    async def web_form(request: Request):
        return _render_web_form(request, error=None)

    @router.post(
        "/settings/web",
        response_class=HTMLResponse,
        dependencies=[
            Depends(web_auth.require(Permission.edit_secrets)),
            Depends(context.require_htmx),
        ],
    )
    async def update_web(
        request: Request,
        trusted_proxies: str = Form(""),
        # Accepted so a direct POST supplying either doesn't 422 — and
        # ignored: host and port are read-only, restart-to-apply (spec §8,
        # brief resolution 10).
        host: str | None = Form(None),
        port: str | None = Form(None),
    ):
        proxies = [
            line.strip()
            for line in trusted_proxies.replace(",", "\n").splitlines()
            if line.strip()
        ]
        context.config.web.trusted_proxies = proxies
        try:
            config_store.save_config(context.config)
        except Exception as e:
            logger.error(f"settings/web: save_config failed: {e}")
            return _render_web_form(request, error=f"Could not save changes: {e}")
        return HTMLResponse("", headers={"HX-Redirect": "/settings/web"})

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

    return router
