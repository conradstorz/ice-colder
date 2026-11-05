"""Opt-in, real-browser regression test: a rejected product form shows its
error.

The product form posts with ``hx-post`` and the route answers 400 with the
form re-rendered around ``<p role="alert">``. htmx 1.9 does not swap 4xx
responses by default, so until ``ice_ui``'s ``feedback.js`` gained an
``htmx:beforeSwap`` rule the error was never shown. TestClient only sees the
response body, so only a real browser proves the swap.

Opt-in locally (skipped unless ICE_COLDER_BROWSER_TESTS=1 and Chrome plus
node are found; mandatory in CI). Run it with:

    ICE_COLDER_BROWSER_TESTS=1 uv run pytest tests/edge/web/test_product_form_errors_browser.py -v
"""

from __future__ import annotations

import json

from tests.browser_harness import BROWSER_OPT_IN, run_node_check

pytestmark = BROWSER_OPT_IN


def test_over_long_name_error_becomes_visible(live_server):
    result, payload = run_node_check(
        "product_form_errors_check.mjs", live_server, timeout=90
    )

    assert payload.get("ok") is True, json.dumps(payload, indent=2)
    assert payload["alertVisible"] is True
    assert "at most" in payload["alertText"]
