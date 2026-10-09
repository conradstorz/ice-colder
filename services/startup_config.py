"""Configuration loading and environment overrides used during startup."""

import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

from loguru import logger
from pydantic import SecretStr, ValidationError

from config.config_model import ConfigModel, MQTTConfig
from services.access import AccessStore
from services.config_store import save_config

_MQTT_ENV_VARS = {
    "broker_host": "MQTT_BROKER_HOST",
    "username": "MQTT_USERNAME",
    "password": "MQTT_PASSWORD",
}


@dataclass
class EnvOverrides:
    """Env-derived values that must not be written back to config.json.

    ``mqtt`` is a copy of ``config.mqtt`` with env values layered on top;
    ``trusted_proxies`` is the env list if set, else the config's own.
    """

    mqtt: MQTTConfig
    trusted_proxies: list[str]


def _config_path() -> str:
    """Resolve the active config path from ``ICE_COLDER_CONFIG`` (read at call
    time so tests can monkeypatch env and cwd independently), defaulting to
    ``config.json`` in the current working directory — unchanged behavior for
    local runs and tests.
    """
    return os.environ.get("ICE_COLDER_CONFIG", "config.json")


def mqtt_env_overrides() -> dict[str, str]:
    """Return currently active MQTT field overrides, keyed by config field."""
    return {
        field: value
        for field, env_name in _MQTT_ENV_VARS.items()
        if (value := os.environ.get(env_name))
    }


def _create_default_config(path: str) -> ConfigModel:
    """First run: blank defaults, persisted, then continue.

    No credential is generated here — authentication lives entirely in
    ``data/access.json`` (services/access.py), created separately and
    walked through the setup wizard at /setup.
    """
    defaults = ConfigModel()
    save_config(defaults, Path(path))
    logger.info(f"First run: created '{path}' with blank defaults")
    return defaults


def load_config() -> ConfigModel:
    """Load configuration from ``ICE_COLDER_CONFIG`` (default ``config.json``).

    Pydantic fills in defaults for missing fields; the user's existing file
    is never overwritten.
    """
    path = _config_path()
    logger.info(f"Loading configuration from '{path}'")

    if os.path.isdir(path):
        logger.error(
            f"Config path '{path}' is a directory, not a file. This typically "
            "happens when a Docker bind-mount targets a file path that doesn't "
            "exist yet on the host, so Docker creates a directory there instead. "
            "Remove the directory and fix the bind-mount/ICE_COLDER_CONFIG "
            "setting, then retry."
        )
        sys.exit(1)

    if not os.path.exists(path):
        logger.warning(f"'{path}' not found — first run: creating defaults")
        return _create_default_config(path)

    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
    except Exception as e:
        logger.exception(f"Error reading '{path}': {e}")
        sys.exit(1)

    try:
        config_model = ConfigModel.model_validate(raw)
        logger.info(
            f"Configuration loaded successfully: version={config_model.version}"
        )
    except ValidationError as ve:
        logger.error("Configuration validation failed:")
        for err in ve.errors():
            loc = " -> ".join(str(l) for l in err.get("loc", []))  # noqa: E741
            logger.error(f"  {loc}: {err.get('msg', '')}")
        sys.exit(1)

    return config_model


def apply_env_overrides(config: ConfigModel) -> EnvOverrides:
    """Return Docker-friendly broker and trusted-proxy overrides.

    The MQTT config is copied before applying environment values, so the live
    ``config`` is never mutated and env-only secrets cannot be persisted by a
    later ``save_config``. Environment variables are read at call time.
    """
    mqtt = config.mqtt.model_copy(deep=True)

    mqtt_overrides = mqtt_env_overrides()
    if host := mqtt_overrides.get("broker_host"):
        mqtt.broker_host = host
        logger.info(f"MQTT broker host overridden by env: {host}")
    if username := mqtt_overrides.get("username"):
        mqtt.username = username
        logger.info(f"MQTT username overridden by env: {username}")
    if password := mqtt_overrides.get("password"):
        mqtt.password = SecretStr(password)

    proxies_env = os.environ.get("ICE_COLDER_TRUSTED_PROXIES")
    if proxies_env:
        trusted_proxies = [p.strip() for p in proxies_env.split(",") if p.strip()]
        logger.info(f"Trusted proxies overridden by env: {trusted_proxies}")
    else:
        trusted_proxies = list(config.web.trusted_proxies)

    return EnvOverrides(mqtt=mqtt, trusted_proxies=trusted_proxies)


def warn_if_setup_mode(store: AccessStore) -> None:
    """Log the dashboard's access-store health at startup. Never exits: a
    dashboard-only problem must never stop the machine selling.

    - Corrupt ``data/access.json``: logged at error level. The dashboard
      serves an error page on every route, but the VMC and MQTT client are
      unaffected and keep running.
    - No owner yet: logged at warning level — the dashboard is in setup mode
      until someone completes the wizard at /setup on the machine.
    - An owner already exists: silent.
    """
    if store.corrupt:
        logger.error(
            f"Access store at {store.path} is corrupt: the dashboard will "
            "serve an error page on every route. The VMC and MQTT client "
            "are unaffected and keep running."
        )
        return
    if store.setup_mode:
        logger.warning(
            "Dashboard is in setup mode: no owner exists yet. Visit /setup "
            "at the machine to create one."
        )
