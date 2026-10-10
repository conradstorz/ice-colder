"""Opt-in, real-browser regression test for Copilot review comment
4113241348 on PR 20 (web_interface/templates/users_codes.html:25).

The claim: the owner's PIN `<input>` sits inside the same `<form>` as the
confirm/cancel `hx-get` buttons rendered by partials/confirm_button.html, so
htmx serializes that form's fields into the query string for those GET
requests too -- landing the plaintext PIN in access logs, proxies and
browser history, exactly the kind of exposure part 1's `Cache-Control:
no-store` header on the *response* was meant to guard against (this is the
*request* side of the same coin).

Nothing here is visible in a TestClient-based assertion on a single
response body -- the defect is about what htmx's own client-side JS puts on
the wire when a human taps a button, which requires a real browser actually
building and sending the request. See
tests/test_dashboard_v2_home_browser.py's module docstring for the same
reasoning applied to a different htmx runtime bug; this file follows the
identical opt-in pattern (ICE_COLDER_BROWSER_TESTS=1, Chrome + Node
required, not part of the normal `uv run pytest` / CI run).

Run explicitly with:
    ICE_COLDER_BROWSER_TESTS=1 uv run pytest tests/test_users_codes_pin_browser.py -v
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
_NODE_SCRIPT = Path(__file__).parent / "browser" / "users_codes_pin_check.mjs"


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
    """A real uvicorn server (not TestClient), wired like tests/conftest.py's
    `wired` fixture, with an owner already logged in on a trusted device --
    mirrors test_dashboard_v2_home_browser.py's fixture of the same name."""
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
        vmc.cancel_pending_tasks()
        web_auth_backoff_reset()


def web_auth_backoff_reset() -> None:
    from web_interface import auth as web_auth

    web_auth.backoff.reset()
    web_auth.backoff.set_trusted_proxies([])


def test_owner_pin_never_reaches_the_confirmation_get_query_string(live_server):
    chrome = _find_chrome()
    node = shutil.which("node")
    if not chrome or not node:
        pytest.skip("Chrome or Node not found on this machine")

    env = dict(os.environ)
    if chrome:
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
        timeout=60,
        env=env,
    )

    assert result.stdout.strip(), (
        f"browser check produced no output; stderr: {result.stderr}"
    )
    payload = json.loads(result.stdout.strip().splitlines()[-1])

    assert not payload.get("error"), (
        f"browser check errored: {payload.get('error')}\nstderr: {result.stderr}"
    )
    assert len(payload["getRequests"]) == 2, (
        "expected exactly two GET requests to /users/codes/regenerate/confirm "
        f"(first tap + Cancel): {json.dumps(payload, indent=2)}"
    )
    for req in payload["getRequests"]:
        assert not req["hasPinInQuery"], (
            "the owner's PIN leaked into a GET request's query string -- "
            f"{req['url']}\nfull payload: {json.dumps(payload, indent=2)}"
        )
    post = payload["postRequest"]
    assert post is not None, "the Confirm tap's POST was never observed"
    assert not post["hasPinInQuery"], (
        f"the PIN leaked into the POST's URL too: {post['url']}"
    )
    assert post["bodyHasPin"], (
        "the PIN must still reach the POST body that actually regenerates "
        f"the codes: {json.dumps(payload, indent=2)}"
    )
    assert payload.get("ok") is True
