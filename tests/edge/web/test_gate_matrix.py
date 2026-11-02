"""Drives the single permission gate table (`gate_table.py`).

Every (row, role) pair: a GET is 200 exactly for the table's roles and 403
otherwise; a POST is anything but 403 (or exactly the row's `ok_status`) for
those roles and 403 otherwise. Every POST row also answers 403 to a
permitted caller that omits `HX-Request` (the CSRF guard). The three routes
whose side effect must be stubbed (an email, the emergency-code pool, commands
dispatched to boards) have their own tests at the bottom.
"""

import pytest
from ice_edge.access.access import Role
from ice_edge.config.config_store import add_product
from ice_edge.web import routes

from tests.edge.web.gate_table import (
    GATES,
    OWNER,
    OWNER_SECRETARY,
    OWNER_TECH,
    POST_GATES,
    SKU,
    Gate,
)


def _ids(gates):
    return [g.id for g in gates]


def _seed(wired, gate: Gate):
    cfg = wired[0]
    add_product(cfg, SKU, "Matrix Item", 100)
    if gate.setup is not None:
        gate.setup(wired)


def _path(gate: Gate, store) -> str:
    return gate.path.format(sku=SKU, user_id=store.owner().id)


@pytest.mark.parametrize("role", list(Role), ids=lambda r: r.value)
@pytest.mark.parametrize("gate", GATES, ids=_ids(GATES))
def test_gate(login_as, wired, gate, role):
    _seed(wired, gate)
    path = _path(gate, wired[3])
    client = login_as(role)
    if gate.method == "GET":
        resp = client.get(path)
    else:
        resp = client.post(path, data=gate.data, headers={"HX-Request": "true"})
    code = resp.status_code
    if role not in gate.roles:
        assert code == 403, (role, gate.id, code)
    elif gate.ok_status is not None:
        assert code == gate.ok_status, (role, gate.id, code, resp.text[:300])
    elif gate.method == "GET":
        assert code == 200, (role, gate.id, code)
    else:
        assert code != 403, (role, gate.id, code)


@pytest.mark.parametrize("gate", POST_GATES, ids=_ids(POST_GATES))
def test_post_without_htmx_is_403(login_as, wired, gate):
    """The owner holds every permission, so a 403 can only be the CSRF guard."""
    _seed(wired, gate)
    client = login_as(Role.owner)
    resp = client.post(
        _path(gate, wired[3]), data=gate.data, headers={"HX-Request": ""}
    )
    assert resp.status_code == 403, (gate.id, resp.status_code)


# The routes below are mutating POSTs whose dummy-body sweep would have real
# side effects (an email sent, the emergency code pool replaced, commands
# dispatched to boards), so each gets its own test with the side effect
# observed. Allowed roles are hand-written, never derived from ROLE_PERMISSIONS.


@pytest.mark.parametrize("role", list(Role), ids=lambda r: r.value)
def test_reports_email(login_as, wired, monkeypatch, role):
    """POST /reports/email is gated on view_reports: owner, secretary."""
    cfg, _vmc, _inv, _store = wired
    cfg.communication.email_gateway.smtp_server = "smtp.real.local"
    sent = []

    async def fake_send_email(gateway, to, subject, body, attachments=None):
        sent.append(to)
        return True

    monkeypatch.setattr("ice_edge.web.routes.reports.email.send_email", fake_send_email)
    client = login_as(role)
    resp = client.post(
        "/reports/email", data={"report": "period"}, headers={"HX-Request": "true"}
    )
    if role in OWNER_SECRETARY:
        assert resp.status_code == 200, (role, resp.status_code, resp.text[:300])
        assert len(sent) == 1, role
    else:
        assert resp.status_code == 403, (role, resp.status_code)
        assert sent == [], role


@pytest.mark.parametrize("role", list(Role), ids=lambda r: r.value)
def test_codes_regenerate(login_as, wired, monkeypatch, role):
    """POST /users/codes/regenerate is gated on manage_ownership: owner
    only. A secretary has manage_users but not manage_ownership."""
    _cfg, _vmc, _inv, store = wired
    calls = []

    def fake_generate():
        calls.append(1)
        return ["AAAA-BBBB"]

    monkeypatch.setattr(store, "generate_emergency_codes", fake_generate)
    client = login_as(role)
    # Each role posts its own PIN so the owner's succeeds past the PIN
    # check; a denied role never reaches it.
    pin = "1379" if role is Role.owner else "2468"
    resp = client.post(
        "/users/codes/regenerate", data={"pin": pin}, headers={"HX-Request": "true"}
    )
    if role in OWNER:
        assert resp.status_code == 200, (role, resp.status_code, resp.text[:300])
        assert len(calls) == 1, role
    else:
        assert resp.status_code == 403, (role, resp.status_code)
        assert calls == [], role


@pytest.mark.parametrize("role", list(Role), ids=lambda r: r.value)
def test_tests_run_all_dispatches(login_as, wired, role):
    """POST /tests/run-all is gated on run_tests: owner, tech. One alive
    subsystem is wired so a permitted role really dispatches."""
    from ice_edge.hal.command_dispatcher import CommandAck
    from ice_edge.monitoring.health_monitor import HealthMonitor

    hm = HealthMonitor()
    hm.record_heartbeat("vending", {"subsystem": "vending", "uptime_seconds": 5})
    hm.record_capabilities(
        "vending",
        {
            "subsystem": "vending",
            "firmware": "abc1234",
            "contract_version": "0.6.0",
            "brand": "ice-colder",
            "model": "sim",
            "hardware_id": "02:11:22:33:44:55",
            "ip": "172.18.0.7",
            "commands": ["ping", "self_test"],
        },
    )
    routes.set_health_monitor(hm)

    class _StubDispatcher:
        _retries = 1

        def __init__(self):
            self.calls = []

        async def send_and_await_completion(self, subsystem, command, params=None):
            self.calls.append((subsystem, command))
            return CommandAck(request_id="a" * 8, command=command, status="ok")

    dispatcher = _StubDispatcher()
    routes.set_command_dispatcher(dispatcher)
    client = login_as(role)
    resp = client.post("/tests/run-all", headers={"HX-Request": "true"})
    if role in OWNER_TECH:
        assert resp.status_code == 200, (role, resp.status_code, resp.text[:300])
        assert dispatcher.calls, role
    else:
        assert resp.status_code == 403, (role, resp.status_code)
        assert dispatcher.calls == [], role
