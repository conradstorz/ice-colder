"""Opt-in, real-browser regression test for the Dashboard v2 Home landing
DOM-destruction bug.

Every other test in this suite drives the app through FastAPI's TestClient,
which only ever asserts on the HTML *string* a route handler returns. The
bug this test guards against never touched that string — the server's
response was always well-formed (one <main>, one #pill). The corruption
happened entirely client-side: htmx 1.9.10 resolves an element's default
swap target by walking up the DOM for an *inherited* hx-target when the
element declares none of its own, and base.html's
`<body hx-boost="true" hx-target="main" ...>` means any self-polling
element that omits its own hx-target inherits "main". #status-panel,
#kpi-panel (home.html) and #pill (base.html's placeholder) each fire an
`hx-trigger="load"` request the instant the Home page loads (the
swapped-in partials/pill.html fragment then re-polls on `every 5s` alone,
so the checker re-samples <main> after that first self-swap too);
without an explicit `hx-target="this"` on each, their responses landed on
the page's <main> instead of on themselves — the first to land (an
innerHTML swap) wiped out main's real content (the tile grid included),
and the next (the pill's own outerHTML swap) went further and replaced
<main> itself with a bare <span>, permanently destroying it (after which
`document.querySelector("main")` returns null, so every later 5-second
pill re-poll fails with htmx:targetError and the pill is stuck on its "…"
placeholder forever).

No amount of server-string or server-side-HTML-structure assertion can
catch this class of bug in principle, because the served bytes are never
wrong — it takes a real browser executing htmx's JS against a live page.
tests/test_web_routes.py's TestHomeSelfPollTargets class adds the
CI-running half of this regression test (a real DOM parse of the served
HTML asserting hx-target="this" is present on all three elements, which
*is* a server-visible fact and is what a reviewer or CI catches before a
merge); THIS file is the belt-and-braces end-to-end check that actually
drives Chrome and would have caught the bug even if the server-visible
attribute check above were somehow bypassed or the fix applied
differently (e.g. wrapping the panels in a `<div hx-target="this">`
ancestor instead).

This test is NOT part of the normal `uv run pytest` / CI run (ci.yml has
no Chrome, and this suite must stay green without one). It is skipped
unless ICE_COLDER_BROWSER_TESTS=1 is set in the environment AND a Chrome/
Chromium binary plus a `node` executable can both be found; run it
explicitly with:

    ICE_COLDER_BROWSER_TESTS=1 uv run pytest tests/test_dashboard_v2_home_browser.py -v
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
_NODE_SCRIPT = Path(__file__).parent / "browser" / "dashboard_v2_home_check.mjs"


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
        for t in vmc.tasks.pending:
            t.cancel()
        web_auth_backoff_reset()


def web_auth_backoff_reset() -> None:
    from web_interface import auth as web_auth

    web_auth.backoff.reset()
    web_auth.backoff.set_trusted_proxies([])


def test_home_dom_is_intact_in_a_real_browser(live_server):
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
        "Dashboard v2 Home landing DOM is broken in a real browser: "
        f"{json.dumps(payload, indent=2)}\n"
        "Expected exactly one <main>, exactly one #pill, at least one "
        "tile anchor inside <main>, and the pill to resolve off its "
        '"…" placeholder within ~6s. If this regresses, check that '
        "#status-panel, #kpi-panel (home.html) and #pill (base.html / "
        'partials/pill.html) each still declare hx-target="this" — '
        'without it they inherit <body>\'s hx-target="main" and their '
        "own load-triggered polls destroy <main>."
    )
    assert payload["mains"] == 1
    assert payload["pills"] == 1
    assert payload["mainsAfterFragmentPoll"] == 1
    assert payload["pillsAfterFragmentPoll"] == 1
    assert payload["tileAnchors"] > 0
    assert payload["pillResolved"] is True
