"""Opt-in, real-browser regression test for touch feedback: the CSS
:active press state, htmx's `.htmx-request` busy indicator during a
boosted navigation, and the click-sound `play()` call.

Every other test in this suite drives the app through FastAPI's
TestClient, which only ever asserts on the HTML *string* a route handler
returns. `:active` styling, `.htmx-request` timing during an in-flight
boosted GET, and whether `HTMLMediaElement.play()` was invoked are all
client-side, browser-executed facts invisible to TestClient — only a real
browser proves them.

Opt-in locally: skipped unless ICE_COLDER_BROWSER_TESTS=1 is set in the
environment AND a Chrome/Chromium binary plus a `node` executable can both
be found. Mandatory in CI: `.github/workflows/ci.yml` sets the flag and
installs both Chrome and Node, and `tests/skip_policy.py` deliberately
excludes browser tests from the set allowed to skip, so a skip here fails
CI's guard. Run it explicitly with:

    ICE_COLDER_BROWSER_TESTS=1 uv run pytest tests/test_touch_feedback_browser.py -v
"""

from __future__ import annotations

import json

from tests.browser_harness import BROWSER_OPT_IN, run_node_check

_NODE_SCRIPT = "touch_feedback_check.mjs"

pytestmark = BROWSER_OPT_IN


def test_press_busy_and_click_sound_in_a_real_browser(live_server):
    result, payload = run_node_check(_NODE_SCRIPT, live_server, timeout=90)

    assert payload.get("ok") is True, json.dumps(payload, indent=2)
    assert payload["pressFilter"] != "none"
    assert payload["busyDuringFlight"] is True
    assert payload["busyAfterSwap"] is False
    assert payload["landedOnHealth"] is True
    assert payload["plays"] >= 1
