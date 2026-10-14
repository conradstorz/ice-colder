"""Shared plumbing for the opt-in real-browser tests.

Used by tests/test_dashboard_v2_home_browser.py,
tests/test_dashboard_v2_boosted_nav_browser.py,
tests/test_users_codes_pin_browser.py and tests/test_touch_feedback_browser.py.
This module is deliberately not named ``test_*`` so pytest never collects it.

Each test module opts in with::

    from tests.browser_harness import BROWSER_OPT_IN, run_node_check

    pytestmark = BROWSER_OPT_IN

The skip conditions are: ICE_COLDER_BROWSER_TESTS=1 must be set (module-level
mark), and a Chrome binary (CHROME_PATH first) plus ``node`` must be found
(skipped inside ``run_node_check``). The ``live_server`` fixture is registered for every
test via tests/conftest.py. CI sets the flag and installs both, and
its guard fails the build if any browser test skips.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import threading
from pathlib import Path

import pytest
import uvicorn

from ice_common.config_model import ConfigModel
from controller.machine import Machine
from ice_edge.access.access import AccessStore, Role
from ice_edge.monitoring.inventory_manager import InventoryManager
from ice_edge.web import routes
from ice_edge.web.server import app

ENV_FLAG = "ICE_COLDER_BROWSER_TESTS"
NODE_SCRIPT_DIR = Path(__file__).parent / "browser"

BROWSER_OPT_IN = pytest.mark.skipif(
    os.environ.get(ENV_FLAG) != "1",
    reason=f"opt-in only: set {ENV_FLAG}=1 to run (requires Chrome + Node; CI always runs these)",
)


def find_chrome() -> str | None:
    env_path = os.environ.get("CHROME_PATH")
    if env_path and Path(env_path).exists():
        return env_path
    candidates = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        "/usr/bin/google-chrome",
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    ]
    for c in candidates:
        if Path(c).exists():
            return c
    return shutil.which("google-chrome") or shutil.which("chromium")


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def web_auth_backoff_reset() -> None:
    from ice_edge.web import auth as web_auth

    web_auth.backoff.reset()
    web_auth.backoff.set_trusted_proxies([])


@pytest.fixture
def live_server(tmp_path):
    """A real uvicorn server (not TestClient) on 127.0.0.1, wired exactly
    like tests/conftest.py's `wired` fixture, with an owner already logged in
    on a trusted device, so a real browser has an actual TCP endpoint and an
    actual Set-Cookie-worthy session to hit."""
    web_auth_backoff_reset()

    cfg = ConfigModel()
    machine = Machine(config=cfg)
    inv = InventoryManager([], path=tmp_path / "inventory.json")
    store = AccessStore(path=tmp_path / "access.json")
    owner = store.create_user("Ada", "ada@example.com", Role.owner, "1379")
    store.finalize_setup()

    routes.set_config_object(cfg)
    routes.set_machine_instance(machine)
    routes.set_inventory_manager(inv)
    routes.set_access_store(store)

    port = free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        for _ in range(200):
            if server.started:
                break
            threading.Event().wait(0.05)
        else:
            raise RuntimeError("uvicorn server never reported started")

        device, token = store.create_device("browser-check", shared=True)
        store.trust_device(device.id, owner.id)
        session_id = store.create_session(owner.id, device.id)

        yield {
            "base_url": f"http://127.0.0.1:{port}",
            "session_cookie": session_id,
            "device_cookie": token,
        }
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        machine.cancel_pending_tasks()
        web_auth_backoff_reset()


def run_node_check(script_name: str, server: dict, timeout: int = 60):
    """Run tests/browser/<script_name> against ``server`` (a live_server dict).

    Skips if Chrome or Node is missing. Returns ``(result, payload)`` where
    ``payload`` is the checker's last stdout line parsed as JSON; asserts the
    checker produced output and exited 0.
    """
    chrome = find_chrome()
    node = shutil.which("node")
    if not chrome or not node:
        pytest.skip("Chrome or Node not found on this machine")

    env = dict(os.environ)
    env["CHROME_PATH"] = chrome

    result = subprocess.run(
        [
            node,
            str(NODE_SCRIPT_DIR / script_name),
            server["base_url"],
            server["session_cookie"],
            server["device_cookie"],
        ],
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
    )
    assert result.stdout.strip(), (
        f"browser check produced no output; stderr: {result.stderr}"
    )
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert result.returncode == 0, (
        f"browser checker exited {result.returncode}; "
        f"stdout: {result.stdout!r}; stderr: {result.stderr!r}"
    )
    return result, payload
