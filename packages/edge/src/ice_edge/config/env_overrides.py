"""Environment overrides for MQTT and trusted proxies.

Resolved here, in the config layer, so both the startup code (`ice_edge.app`)
and the settings routes (`ice_edge.web`) can read them without the web layer
importing the composition root. `ice_edge.app.startup_config` re-exports these.
"""

import os
from dataclasses import dataclass

from loguru import logger
from pydantic import SecretStr

from ice_common.config_model import ConfigModel, MQTTConfig

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


def mqtt_env_overrides() -> dict[str, str]:
    """Return currently active MQTT field overrides, keyed by config field."""
    return {
        field: value
        for field, env_name in _MQTT_ENV_VARS.items()
        if (value := os.environ.get(env_name))
    }


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
