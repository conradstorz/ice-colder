"""Opt-in real-browser check (same conditions as the other browser tests:
ICE_COLDER_BROWSER_TESTS=1 plus Chrome and node): the cloud portal's remote
banner must appear exactly once, inside the out-of-band #bar, across boosted
navigation. A banner rendered as a plain <body> child would stack on every
swap. See tests/browser/cloud_banner_check.mjs."""

from __future__ import annotations

import json
import threading

import pytest
import uvicorn

from ice_cloud.app import create_app
from tests.browser_harness import BROWSER_OPT_IN, free_port, run_node_check
from tests.cloud.reports import NOW, make_report

pytestmark = BROWSER_OPT_IN


@pytest.fixture
def cloud_server(tmp_path):
    app = create_app(data_dir=tmp_path / "cloud", now=lambda: NOW)
    app.state.store.save(make_report())
    port = free_port()
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        for _ in range(200):
            if server.started:
                break
            threading.Event().wait(0.05)
        else:
            raise RuntimeError("uvicorn server never reported started")
        yield {
            "base_url": f"http://127.0.0.1:{port}",
            "session_cookie": "-",
            "device_cookie": "-",
        }
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def test_remote_banner_never_stacks_on_boosted_navigation(cloud_server):
    _result, payload = run_node_check("cloud_banner_check.mjs", cloud_server)
    assert payload.get("ok") is True, json.dumps(payload, indent=2)
    labels = [s["label"] for s in payload["steps"]]
    assert labels == [
        "fleet",
        "after-select",
        "after-health",
        "after-faults",
        "after-home",
    ]
