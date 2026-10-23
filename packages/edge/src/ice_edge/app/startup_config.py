"""Configuration loading and environment overrides used during startup."""

import json
import os
import socket
import sys
from pathlib import Path

from loguru import logger
from pydantic import ValidationError

from ice_common.config_model import ConfigModel
from ice_edge.access.access import AccessStore
from ice_edge.config.config_store import save_config
from ice_edge.config.env_overrides import (  # noqa: F401
    EnvOverrides,
    apply_env_overrides,
    mqtt_env_overrides,
)


def _config_path() -> str:
    """Resolve the active config path from ``ICE_COLDER_CONFIG`` (read at call
    time so tests can monkeypatch env and cwd independently), defaulting to
    ``config.json`` in the current working directory — unchanged behavior for
    local runs and tests.
    """
    return os.environ.get("ICE_COLDER_CONFIG", "config.json")


def _create_default_config(path: str) -> ConfigModel:
    """First run: blank defaults, persisted, then continue.

    No credential is generated here — authentication lives entirely in
    ``data/access.json`` (ice_edge/access/access.py), created separately and
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


def check_web_bind(host: str, port: int) -> None:
    """Fail fast if the dashboard's host/port cannot be bound.

    uvicorn would otherwise discover this only after MQTT, the recorder and the
    uplink are wired, and ``sys.exit(1)`` from inside its task. Every address
    the host resolves to is probed. Logs and exits 1.
    """
    try:
        infos = socket.getaddrinfo(
            host, port, type=socket.SOCK_STREAM, flags=socket.AI_PASSIVE
        )
    except socket.gaierror as exc:
        logger.error(
            f"Cannot bind web interface: could not resolve host {host!r} ({exc})"
        )
        sys.exit(1)
    seen: set[tuple[int, tuple]] = set()
    for family, _type, _proto, _canon, sockaddr in infos:
        if (family, sockaddr) in seen:
            continue
        seen.add((family, sockaddr))
        try:
            with socket.socket(family, socket.SOCK_STREAM) as probe:
                if sys.platform == "win32":
                    # SO_REUSEADDR would let the probe share a port uvicorn's
                    # real bind (no reuse_address on Windows) cannot take.
                    probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                else:
                    probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                probe.bind(sockaddr)
                probe.listen(1)
        except OSError as exc:
            logger.error(
                f"Cannot bind web interface to {sockaddr[0]}:{port} "
                f"(errno {exc.errno}: {exc.strerror or exc}); is another instance running?"
            )
            sys.exit(1)


def warn_if_no_buttons(config: ConfigModel) -> None:
    """Migration aid: log which products have no customer button. Never raises
    or edits the config.

    Configs saved before ``Product.button`` existed have none assigned, and a
    press maps to a product only by ``button``, so every press would be ignored.
    """
    products = config.products
    if not products:
        return
    unassigned = [p.sku for p in products if p.button is None]
    if len(unassigned) == len(products):
        logger.warning(
            "No product has a customer button; button presses will select "
            "nothing — set Product → Button"
        )
    elif unassigned:
        logger.info(
            f"Products with no customer button (not selectable by button): "
            f"{', '.join(unassigned)}"
        )


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
