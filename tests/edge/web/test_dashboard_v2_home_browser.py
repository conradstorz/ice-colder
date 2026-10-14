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

from tests.browser_harness import BROWSER_OPT_IN, run_node_check

_NODE_SCRIPT = "dashboard_v2_home_check.mjs"

pytestmark = BROWSER_OPT_IN


def test_home_dom_is_intact_in_a_real_browser(live_server):
    result, payload = run_node_check(_NODE_SCRIPT, live_server)

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
