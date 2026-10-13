"""MQTT env overrides (brief resolution 6): the shared
`services.startup_config.mqtt_env_overrides()` helper identifies active
fields at request time. The startup loader separately applies those values
to a copy of `config.mqtt`, so an env-only MQTT_PASSWORD is never written
back into config.json by a later save. Keeping this helper in a service
module avoids importing main.py and creating a cycle through the web app.
"""

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from loguru import logger
from pydantic import SecretStr

from services import config_store
from services.access import Permission
from services.startup_config import mqtt_env_overrides
from web_interface import auth as web_auth
from web_interface import context
from ice_ui.levels import (
    LEVEL_SETTINGS_MQTT,
)
from web_interface.routes.settings.secrets import (
    SECRET_MASK,
    _secret_changed,
    _secret_placeholder,
)
from ice_ui.templating import templates


router = APIRouter()


# ---------------------------------------------------------------- MQTT


def _render_mqtt_form(request: Request, *, error: str | None):
    mqtt = context.config.mqtt
    overrides = mqtt_env_overrides()
    return templates.TemplateResponse(
        request,
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
