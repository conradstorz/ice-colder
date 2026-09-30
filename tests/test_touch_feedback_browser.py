"""Opt-in, real-browser regression test for touch feedback: the CSS
:active press state, htmx's `.htmx-request` busy indicator during a
boosted navigation, and the tap-sound `play()` call.

Every other test in this suite drives the app through FastAPI's
TestClient, which only ever asserts on the HTML *string* a route handler
returns. `:active` styling, `.htmx-request` timing during an in-flight
boosted GET, and whether `HTMLMediaElement.play()` was invoked are all
client-side, browser-executed facts invisible to TestClient — only a real
browser proves them.

This test is NOT part of the normal `uv run pytest` / CI run (ci.yml has
no Chrome, and this suite must stay green without one). It is skipped
unless ICE_COLDER_BROWSER_TESTS=1 is set in the environment AND a Chrome/
Chromium binary plus a `node` executable can both be found; run it
explicitly with:

    ICE_COLDER_BROWSER_TESTS=1 uv run pytest tests/test_touch_feedback_browser.py -v
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

from config.config_model import ConfigModel
from controller.vmc import VMC
from services.access import AccessStore, Role
from services.inventory_manager import InventoryManager
from web_interface import routes
from web_interface.server import app

_ENV_FLAG = "ICE_COLDER_BROWSER_TESTS"
_NODE_SCRIPT = Path(__file__).parent / "browser" / "touch_feedback_check.mjs"


def _find_chrome() -> str | None:
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


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


pytestmark = pytest.mark.skipif(
    os.environ.get(_ENV_FLAG) != "1",
    reason=f"opt-in only: set {_ENV_FLAG}=1 to run (requires Chrome + Node; not run in CI)",
)


@pytest.fixture
def live_server(tmp_path):
    """A real uvicorn server (not TestClient) on 127.0.0.1, wired exactly
    like tests/conftest.py's `wired` fixture, so a real browser has an
    actual TCP endpoint and actual Set-Cookie-worthy session to hit."""
    web_auth_backoff_reset()

    cfg = ConfigModel()
    vmc = VMC(config=cfg)
    inv = InventoryManager([], path=tmp_path / "inventory.json")
    store = AccessStore(path=tmp_path / "access.json")
    owner = store.create_user("Ada", "ada@example.com", Role.owner, "1379")
    store.finalize_setup()

    routes.set_config_object(cfg)
    routes.set_vmc_instance(vmc)
    routes.set_inventory_manager(inv)
    routes.set_access_store(store)

    port = _free_port()
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
        routes.set_access_store(None)
        for t in vmc._pending_tasks:
            t.cancel()
        web_auth_backoff_reset()


def web_auth_backoff_reset() -> None:
    from web_interface import auth as web_auth

    web_auth.backoff.reset()
    web_auth.backoff.set_trusted_proxies([])


def test_press_busy_and_click_sound_in_a_real_browser(live_server):
    chrome = _find_chrome()
    node = shutil.which("node")
    if not chrome or not node:
        pytest.skip("Chrome or Node not found on this machine")
    env = dict(os.environ)
    env["CHROME_PATH"] = chrome
    result = subprocess.run(
        [
            node,
            str(_NODE_SCRIPT),
            live_server["base_url"],
            live_server["session_cookie"],
            live_server["device_cookie"],
        ],
        capture_output=True,
        text=True,
        timeout=90,
        env=env,
    )
    assert result.stdout.strip(), (
        f"browser check produced no output; stderr: {result.stderr}"
    )
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload.get("ok") is True, json.dumps(payload, indent=2)
    assert payload["pressFilter"] != "none"
    assert payload["busyDuringFlight"] is True
    assert payload["busyAfterSwap"] is False
    assert payload["landedOnHealth"] is True
    assert payload["plays"] >= 1
