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

from tests.browser_harness import BROWSER_OPT_IN, run_node_check

_NODE_SCRIPT = "dashboard_v2_boosted_nav_check.mjs"

pytestmark = BROWSER_OPT_IN


def test_boosted_navigation_does_not_nest_main(live_server):
    result, payload = run_node_check(_NODE_SCRIPT, live_server)

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
