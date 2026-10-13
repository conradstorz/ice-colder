"""Opt-in, real-browser regression test for Copilot review comment
4113241348 on PR 20 (packages/ui/src/ice_ui/templates/users_codes.html:25).

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

from tests.browser_harness import BROWSER_OPT_IN, run_node_check

_NODE_SCRIPT = "users_codes_pin_check.mjs"

pytestmark = BROWSER_OPT_IN


def test_owner_pin_never_reaches_the_confirmation_get_query_string(live_server):
    result, payload = run_node_check(_NODE_SCRIPT, live_server)

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
