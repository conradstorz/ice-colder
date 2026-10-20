"""Simulated-machine container launcher.

Spec: docs/superpowers/specs/2026-10-19-simulated-machine-container-design.md
(sections 4.1 environment and 4.2 seed). This module holds the settings parsing
and idempotent config seeding; process supervision arrives in a later task.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError

from ice_common.config_model import ConfigModel, Product
from ice_edge.config.config_store import save_config
from ice_edge.vend.dispensers_doc import render_example


class LauncherError(Exception):
    """A configuration problem the operator must fix; main() prints it and exits 1."""


@dataclass(frozen=True)
class Settings:
    cloud_url: str
    machine_id: str
    report_interval: int
    data_dir: Path

    @classmethod
    def from_env(cls, env: Mapping[str, str], *, hostname: str) -> "Settings":
        cloud_url = env.get("ICE_CLOUD_URL", "").strip()
        if not cloud_url:
            raise LauncherError(
                "ICE_CLOUD_URL is required (base URL of the cloud server)"
            )
        machine_id = env.get("ICE_COLDER_MACHINE_ID", "").strip() or hostname
        raw = env.get("ICE_COLDER_REPORT_INTERVAL", "").strip() or "60"
        try:
            interval = int(raw)
        except ValueError:
            raise LauncherError(
                f"ICE_COLDER_REPORT_INTERVAL must be an integer 10..3600, got {raw!r}"
            ) from None
        if not 10 <= interval <= 3600:
            raise LauncherError(
                f"ICE_COLDER_REPORT_INTERVAL must be within 10..3600, got {interval}"
            )
        data_dir = Path(env.get("ICE_COLDER_DATA", "").strip() or "/app/data")
        return cls(cloud_url, machine_id, interval, data_dir)


DEFAULT_PRODUCTS: tuple[dict, ...] = (
    {
        "sku": "SAMPLE-ICE",
        "name": "Sample Ice",
        "slot": 0,
        "kind": "ice",
        "button": "button_ice",
        "price": 3.00,
    },
    {
        "sku": "SAMPLE-WATER-SM",
        "name": "Sample Water Sm",
        "slot": 1,
        "kind": "water",
        "button": "button_water_1gal",
        "price": 1.00,
    },
    {
        "sku": "SAMPLE-WATER-LG",
        "name": "Sample Water Lg",
        "slot": 2,
        "kind": "water",
        "button": "button_water_5gal",
        "price": 2.00,
    },
)


def _load_or_default(config_path: Path) -> ConfigModel:
    if config_path.is_dir():
        raise LauncherError(f"{config_path} is a directory, expected a file")
    if not config_path.exists():
        config = ConfigModel()
        config.physical.products = [Product(**row) for row in DEFAULT_PRODUCTS]
        return config
    try:
        return ConfigModel.model_validate_json(config_path.read_text(encoding="utf-8"))
    except (ValueError, ValidationError, OSError) as exc:
        raise LauncherError(f"cannot parse {config_path}: {exc}") from exc


def seed(settings: Settings) -> ConfigModel:
    data_dir = settings.data_dir
    data_dir.mkdir(parents=True, exist_ok=True)
    config_path = data_dir / "config.json"
    config = _load_or_default(config_path)

    dispensers = data_dir / "dispensers.toml"
    if not dispensers.exists():
        dispensers.write_text(render_example(), encoding="utf-8")

    config.machine_id = settings.machine_id
    config.cloud.enabled = True
    config.cloud.url = settings.cloud_url
    config.cloud.report_interval_seconds = settings.report_interval
    config.mqtt.broker_host = "127.0.0.1"
    config.mqtt.username = None
    config.mqtt.password = None
    save_config(config, config_path)
    return config


def child_env(settings: Settings, base: Mapping[str, str]) -> dict[str, str]:
    env = {
        k: v
        for k, v in base.items()
        if k not in ("MQTT_BROKER_HOST", "MQTT_USERNAME", "MQTT_PASSWORD")
    }
    env["ICE_COLDER_CONFIG"] = str(settings.data_dir / "config.json")
    env["ICE_COLDER_DISPENSERS"] = str(settings.data_dir / "dispensers.toml")
    return env


def main() -> None:
    raise SystemExit("supervision arrives in Task 2")


if __name__ == "__main__":
    main()
