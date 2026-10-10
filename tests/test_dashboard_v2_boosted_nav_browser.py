"""Opt-in, real-browser regression test for the *second* instance of the
htmx ambient-hx-target defect: nested <main> on boosted navigation.

tests/test_dashboard_v2_home_browser.py's module docstring explains why a
browser is required for this class of bug and why it's opt-in; the same
reasoning applies here. That file's regression covers three self-polling
widgets that inherited <body>'s ambient hx-target="main" because they fired
their own "load" requests with no target of their own. This file covers a
different set of elements that inherit the *same* ambient default: ordinary
boosted navigation (tile links, the bar's own Home link — neither declares
an explicit hx-target) and bare hx-get/hx-post elements like
health_logs.html's Refresh button. Because every level extends base.html,
every one of those responses is a *full* HTML document; htmx's boosted
handling parses it, peels off the out-of-band #bar, and is left with the
child level's own <main> element as the swap fragment. With
hx-swap="innerHTML" that fragment lands *inside* the page's live <main>
instead of replacing it, nesting <main><main>...</main></main> -- confirmed
here by clicking through real navigation and a real Refresh tap, not by
asserting on a response string (which is always well-formed; the corruption
is entirely client-side, exactly as with the first defect).

tests/test_web_routes.py::TestBoostedSwapDoesNotNestMain is the CI-running
half: it asserts <body>'s hx-swap is "outerHTML ...", a server-visible fact,
but cannot itself prove a real htmx runtime avoids the nesting. THIS file is
the belt-and-braces end-to-end check.

This test is NOT part of the normal `uv run pytest` / CI run (ci.yml has no
Chrome, and this suite must stay green without one). It is skipped unless
ICE_COLDER_BROWSER_TESTS=1 is set in the environment AND a Chrome/Chromium
binary plus a `node` executable can both be found; run it explicitly with:

    ICE_COLDER_BROWSER_TESTS=1 uv run pytest tests/test_dashboard_v2_boosted_nav_browser.py -v
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
_NODE_SCRIPT = Path(__file__).parent / "browser" / "dashboard_v2_boosted_nav_check.mjs"


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
    """A real uvicorn server (not TestClient), wired exactly like
    tests/conftest.py's `wired` fixture and test_dashboard_v2_home_browser.py's
    fixture of the same name, so a real browser has an actual TCP endpoint
    and an actual Set-Cookie-worthy session to hit."""
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
        for t in vmc.tasks.pending:
            t.cancel()
        web_auth_backoff_reset()


def web_auth_backoff_reset() -> None:
    from web_interface import auth as web_auth

    web_auth.backoff.reset()
    web_auth.backoff.set_trusted_proxies([])


def test_boosted_navigation_does_not_nest_main(live_server):
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

    assert payload.get("ok") is True, (
        "Boosted navigation nests <main> inside <main> in a real browser: "
        f"{json.dumps(payload, indent=2)}\n"
        "Expected every step to show exactly one <main>, zero main>main "
        "nesting, and exactly one #bar. If this regresses, check that "
        'base.html\'s <body> still declares hx-swap="outerHTML ..." '
        'rather than "innerHTML ..." — see '
        "tests/test_web_routes.py::TestBoostedSwapDoesNotNestMain and "
        "this file's module docstring."
    )
    for step in payload["steps"]:
        assert step["mains"] == 1, step
        assert step["nestedMain"] == 0, step
        assert step["bars"] == 1, step
