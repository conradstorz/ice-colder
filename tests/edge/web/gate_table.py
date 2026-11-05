"""The single permission gate table for the web dashboard.

Hard-coded expected roles per route, written out by hand from
`packages/edge/src/ice_edge/web/CLAUDE.md`'s URL/permission gate table crossed
with the role->permission sets pinned in `tests/edge/access/test_access.py`'s
`TestPermissionTable` -- NOT derived from `ROLE_PERMISSIONS`, so a wrong grant
there (or a wrong gate on a route) turns a row red instead of being mirrored.
"A or B" gates are resolved to the union of the roles holding either
permission.

Each `Gate` is one (method, path, roles) row. `"{sku}"` and `"{user_id}"` in
`path` are filled in with seeded data. A POST row is sent with `HX-Request`,
so the permission gate is what is measured: a role in `roles` must get
anything but 403 (or exactly `ok_status` when given), every other role exactly
403. `setup(wired)` runs before the request for rows that need real state
(a raised fault, a wired subsystem, a locked-out product) so a permitted role
hits a real target rather than a 404 that would hide the permission axis.

`tests/edge/web/test_gate_matrix.py` drives the table.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from ice_contracts.board.common import CommandAck
from ice_contracts.board.vending_machine import FaultCode
from ice_edge.access.access import Role
from ice_edge.config.config_store import add_product
from ice_edge.monitoring.health_monitor import HealthMonitor
from ice_edge.web import routes

ALL = frozenset(Role)
OWNER = frozenset({Role.owner})
OWNER_SECRETARY = frozenset({Role.owner, Role.secretary})
OWNER_TECH = frozenset({Role.owner, Role.tech})

SKU = "MATRIX-1"

Setup = Callable[[tuple], None]


@dataclass(frozen=True)
class Gate:
    method: str
    path: str
    roles: frozenset
    data: dict = field(default_factory=dict)
    setup: Setup | None = None
    ok_status: int | None = None

    @property
    def id(self) -> str:
        return f"{self.method} {self.path}"


# --- setup hooks (each receives the `wired` fixture tuple) ------------------


def _raise_pay_103(wired: tuple) -> None:
    """A fresh fault per case: clearing consumes it, and an absent one would
    404 and make the row lie about the permission being exercised."""
    wired[1].raise_fault(FaultCode.PAY_103, outcome="permission matrix seed")


def _lock_out_ice(wired: tuple) -> None:
    """Locked out so POST /tests/sale refuses synchronously (the wired VMC is
    never attach_to_loop()'d, so a real dispense wait would hang)."""
    cfg, vmc = wired[0], wired[1]
    add_product(cfg, "ICE-1", "Ice Bag", 250, slot=1)
    vmc.raise_fault(FaultCode.ICE_101, sku="ICE-1")


class _AckDispatcher:
    _retries = 1

    async def send_and_await_completion(self, subsystem, command, params=None):
        return CommandAck(request_id="a" * 8, command=command, status="ok")


def _wire_mdb_ping(wired: tuple) -> None:
    """An alive mdb board advertising `ping`, so the allowlist re-check passes
    for a permitted role (and /tests/mdb/ping is the allowlisted command)."""
    hm = HealthMonitor()
    hm.record_heartbeat("mdb", {"subsystem": "mdb", "uptime_seconds": 5})
    hm.record_capabilities(
        "mdb",
        {
            "subsystem": "mdb",
            "firmware": "abc1234",
            "contract_version": "0.6.0",
            "brand": "ice-colder",
            "model": "sim",
            "hardware_id": "02:11:22:33:44:55",
            "ip": "172.18.0.7",
            "commands": ["ping"],
        },
    )
    routes.set_health_monitor(hm)
    routes.set_command_dispatcher(_AckDispatcher())


def _g(method: str, path: str, roles: frozenset, **kw) -> Gate:
    return Gate(method, path, roles, **kw)


GATES: list[Gate] = [
    # view_status -- every role
    _g("GET", "/", ALL),
    _g("GET", "/me", ALL),
    _g("GET", "/status", ALL),
    _g("GET", "/kpi", ALL),
    _g("GET", "/pill", ALL),
    _g("GET", "/screen", ALL),
    _g("GET", "/health", ALL),
    _g("GET", "/health/subsystems", ALL),
    _g("GET", "/health/subsystems/vending", ALL),
    _g("GET", "/health/subsystems/vending/live", ALL),
    _g("GET", "/health/faults", ALL),
    _g("GET", "/health/availability", ALL),
    # view_logs -- owner, tech
    _g("GET", "/health/logs", OWNER_TECH),
    # edit_catalog or edit_placement -- every role (all hold edit_placement)
    _g("GET", "/products", ALL),
    _g("GET", "/products/{sku}", ALL),
    # edit_catalog -- owner, secretary
    _g("GET", "/products/new", OWNER_SECRETARY),
    _g("GET", "/products/{sku}/catalog", OWNER_SECRETARY),
    _g("GET", "/products/{sku}/copy", OWNER_SECRETARY),
    # edit_placement -- every role
    _g("GET", "/products/{sku}/placement", ALL),
    _g("GET", "/inventory", ALL),
    # view_reports -- owner, secretary
    _g("GET", "/reports", OWNER_SECRETARY),
    _g("GET", "/reports/period", OWNER_SECRETARY),
    _g("GET", "/reports/product", OWNER_SECRETARY),
    _g("GET", "/reports/product/{sku}", OWNER_SECRETARY),
    _g("GET", "/reports/method", OWNER_SECRETARY),
    _g("GET", "/reports/collections", OWNER_SECRETARY),
    # machine_controls -- owner, tech
    _g("GET", "/controls", OWNER_TECH),
    # run_tests -- owner, tech
    _g("GET", "/tests", OWNER_TECH),
    _g("GET", "/tests/log", OWNER_TECH),
    _g("GET", "/tests/sale", OWNER_TECH),
    _g("GET", "/tests/vending", OWNER_TECH),
    _g("GET", "/tests/standby/confirm", OWNER_TECH),
    _g("GET", "/tests/takeover/confirm", OWNER_TECH),
    # manage_users -- owner, secretary
    _g("GET", "/users", OWNER_SECRETARY),
    _g("GET", "/users/new", OWNER_SECRETARY),
    _g("GET", "/users/{user_id}", OWNER_SECRETARY),
    _g("GET", "/devices", OWNER_SECRETARY),
    # manage_ownership -- owner only
    _g("GET", "/users/codes", OWNER),
    _g("GET", "/users/ownership", OWNER),
    # edit_contacts or edit_secrets -- owner, secretary (via edit_contacts)
    _g("GET", "/settings", OWNER_SECRETARY),
    # edit_contacts -- owner, secretary
    _g("GET", "/settings/machine", OWNER_SECRETARY),
    _g("GET", "/settings/contacts", OWNER_SECRETARY),
    _g("GET", "/settings/reports", OWNER_SECRETARY),
    # edit_secrets -- owner only
    _g("GET", "/settings/payments", OWNER),
    _g("GET", "/settings/comms", OWNER),
    _g("GET", "/settings/mqtt", OWNER),
    _g("GET", "/settings/web", OWNER),
    # --- mutating POSTs, called with a dummy body ---------------------------
    # machine_controls
    _g("POST", "/controls/bogus-command", OWNER_TECH),
    # clear_faults (the PAY-103 row has a real fault to clear -> exactly 200)
    _g("POST", "/health/faults/NOPE-000/clear", OWNER_TECH),
    _g(
        "POST",
        "/health/faults/PAY-103/clear",
        OWNER_TECH,
        setup=_raise_pay_103,
        ok_status=200,
    ),
    _g("POST", "/health/faults/PAY-104/record-sale", OWNER_TECH),
    _g("POST", "/health/faults/PAY-104/discard", OWNER_TECH),
    # edit_placement / collect_cash -- every role
    _g("POST", "/inventory/{sku}/adjust", ALL, data={"delta": "7"}),
    _g("POST", "/inventory/collect", ALL),
    _g("POST", "/products/{sku}/placement", ALL),
    # edit_catalog -- owner, secretary (the catalog row posts a valid body, so
    # a permitted role reaches the real write -> exactly 200)
    _g("POST", "/products/new", OWNER_SECRETARY),
    _g(
        "POST",
        "/products/{sku}/catalog",
        OWNER_SECRETARY,
        data={"name": "Matrix Item", "price": "1.00", "kind": "other"},
        ok_status=200,
    ),
    # edit_contacts -- owner, secretary
    _g("POST", "/settings/machine", OWNER_SECRETARY),
    _g("POST", "/settings/contacts", OWNER_SECRETARY),
    _g("POST", "/settings/reports", OWNER_SECRETARY),
    # edit_secrets -- owner only
    _g("POST", "/settings/payments", OWNER),
    _g("POST", "/settings/comms", OWNER),
    _g("POST", "/settings/mqtt", OWNER),
    _g("POST", "/settings/web", OWNER),
    # run_tests -- owner, tech
    _g(
        "POST",
        "/tests/sale",
        OWNER_TECH,
        data={"sku": "ICE-1"},
        setup=_lock_out_ice,
        ok_status=200,
    ),
    _g("POST", "/tests/run-all", OWNER_TECH, ok_status=200),
    _g("POST", "/tests/standby", OWNER_TECH),
    _g("POST", "/tests/end", OWNER_TECH, ok_status=200),
    _g("POST", "/tests/takeover", OWNER_TECH, ok_status=200),
    _g(
        "POST",
        "/tests/runs/nonexistent-run/verdict",
        OWNER_TECH,
        data={"verdict": "pass"},
        ok_status=200,
    ),
    _g("POST", "/tests/mdb/ping", OWNER_TECH, setup=_wire_mdb_ping, ok_status=200),
    # manage_users -- owner, secretary
    _g("POST", "/users/new", OWNER_SECRETARY),
    _g("POST", "/devices/nope/forget", OWNER_SECRETARY),
    # manage_ownership -- owner only
    _g("POST", "/users/transfer/cancel", OWNER),
]

POST_GATES = [g for g in GATES if g.method == "POST"]
