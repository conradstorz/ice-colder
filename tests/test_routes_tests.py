"""Tests for the Tests level: Task 13a's discovery routes (GET /tests,
GET /tests/{subsystem}, GET /tests/log) plus Task 13b's action routes
(POST run a command, POST verdict, POST run-all, GET/POST sale, POST end,
POST takeover).

Fixtures come from tests/conftest.py: `wired` seeds a ConfigModel, VMC,
InventoryManager and AccessStore (owner "Ada"); `login_as`/`client` sign in
as a given role. Every test that wires a HealthMonitor, EventRecorder or
CommandDispatcher restores module state to None in a `finally`/fixture
teardown, matching the pattern used throughout tests/test_routes_health.py
and tests/test_routes_reports.py, since all three are plain module globals
in web_interface.context shared across tests.
"""

import asyncio

import pytest

from contracts.common import COMMAND_PARAM_VALIDATORS, CommandAck
from contracts.vending_machine import FaultCode
from services.access import ROLE_PERMISSIONS, Permission, Role
from services.command_dispatcher import CommandTimeout
from services.config_store import add_product
from services.event_recorder import EventRecorder
from web_interface import auth as web_auth
from web_interface import context
from web_interface import routes
from web_interface.routes.tests_level import (
    POWER_CYCLE_DWELL_DEFAULT,
    POWER_CYCLE_DWELL_RANGE,
    WATER_VALVE_SECONDS_RANGE,
    contract_match,
)
from web_interface.routes.tests_level import _recent_test_runs
from web_interface.routes.tests_level import testable_commands as compute_testable

# Imported under an alias: a bare `testable_commands` name starting with
# "test" is collected by pytest as if it were a test function itself
# (python_functions defaults to "test_*"... and pytest's default collection
# also matches "test*" for imported names), which fails at collection with
# "fixture 'subsystem' not found". Confirmed by reproducing it during
# development of this file.

_TESTS_GET_ROUTES = ["/tests", "/tests/log", "/tests/vending"]


@pytest.fixture
def wire_subsystem():
    """Wire a fresh HealthMonitor into routes for one test, with a helper
    to record one subsystem's heartbeat + capabilities in one call."""
    from services.health_monitor import HealthMonitor

    hm = HealthMonitor()
    routes.set_health_monitor(hm)

    def _wire(name, commands, *, alive=True, contract_version="0.5.0"):
        if alive:
            hm.record_heartbeat(name, {"subsystem": name, "uptime_seconds": 5})
        hm.record_capabilities(
            name,
            {
                "subsystem": name,
                "firmware": "abc1234",
                "contract_version": contract_version,
                "brand": "ice-colder",
                "model": "sim",
                "hardware_id": "02:11:22:33:44:55",
                "ip": "172.18.0.7",
                "commands": commands,
            },
        )
        return hm

    try:
        yield _wire
    finally:
        routes.set_health_monitor(None)


@pytest.fixture
def wire_event_recorder(tmp_path):
    """A real EventRecorder wired into routes, for /tests/log."""
    recorder = EventRecorder(db_path=str(tmp_path / "events.db"))
    routes.set_event_recorder(recorder)
    try:
        yield recorder
    finally:
        routes.set_event_recorder(None)


class FakeAckDispatcher:
    """A services.command_dispatcher.CommandDispatcher stand-in for POST
    route tests (Task 13b): returns a fixed ack, or times out
    unconditionally, instead of touching MQTT. `_retries` mirrors the real
    dispatcher's attribute name -- `_run_command` reads it via
    `getattr(dispatcher, "_retries", 1)` to compute the exact "N attempts"
    wording, so this fake must carry the same name to be a faithful stand-in.
    """

    def __init__(self, *, status="ok", detail=None, always_timeout=False, retries=1):
        self.status = status
        self.detail = detail
        self.always_timeout = always_timeout
        self._retries = retries
        self.calls: list[tuple] = []

    async def send(self, subsystem, command, params=None):
        self.calls.append((subsystem, command, params))
        if self.always_timeout:
            raise CommandTimeout(subsystem, command)
        return CommandAck(
            request_id="a" * 8, command=command, status=self.status, detail=self.detail
        )


class ConcurrencyCheckingDispatcher:
    """Records the highest number of `send()` calls ever in flight at once,
    to prove run-all dispatches strictly one command at a time ("in
    sequence" -- task brief) rather than via `asyncio.gather`. `await
    asyncio.sleep(0)` between the increment and the decrement yields
    control back to the event loop -- a concurrent caller that started a
    second `send()` before awaiting the first would be counted here; a
    strictly sequential caller never lets `_current` exceed 1.
    """

    def __init__(self):
        self._current = 0
        self.max_concurrent = 0
        self.calls: list[tuple] = []
        self._retries = 1

    async def send(self, subsystem, command, params=None):
        self._current += 1
        self.max_concurrent = max(self.max_concurrent, self._current)
        await asyncio.sleep(0)
        self.calls.append((subsystem, command))
        self._current -= 1
        return CommandAck(request_id="a" * 8, command=command, status="ok")


@pytest.fixture
def wire_dispatcher():
    """Wire a dispatcher (FakeAckDispatcher/ConcurrencyCheckingDispatcher,
    or any object with an async `send`) into routes for one test."""

    def _wire(dispatcher):
        routes.set_command_dispatcher(dispatcher)
        return dispatcher

    try:
        yield _wire
    finally:
        routes.set_command_dispatcher(None)


# --- Permission gates -------------------------------------------------


class TestPermissionGates:
    """All three GET routes gate on run_tests: 200 for owner/tech, 403 for
    secretary/loader (run_tests is held by exactly owner and tech --
    services/access.py's ROLE_PERMISSIONS). Reaches
    web_interface/routes/tests_level.py's `Depends(web_auth.require(...))`
    on each of the three routes.

    Mutation proof (dropping the dependency from GET /tests/log): removed
    `dependencies=[Depends(web_auth.require(Permission.run_tests))]` from
    that one route -- test_matrix[/tests/log-Role.secretary] and
    [/tests/log-Role.loader] failed (200 != 403, AssertionError); every
    owner/tech case kept passing (they were already 200). Restored the
    dependency -- full parametrized set passed again.
    """

    @pytest.mark.parametrize("role", list(Role))
    @pytest.mark.parametrize("path", _TESTS_GET_ROUTES)
    def test_matrix(self, login_as, path, role):
        client = login_as(role)
        resp = client.get(path)
        if Permission.run_tests in ROLE_PERMISSIONS[role]:
            assert resp.status_code == 200, (role, path, resp.status_code)
        else:
            assert resp.status_code == 403, (role, path, resp.status_code)


# --- Discovery: advertised ∩ allowlist ---------------------------------


class TestDiscoveryIntersection:
    """GET /tests/{subsystem} renders a Run button only for a command that
    is both server-allowlisted (contracts.common.TESTABLE_COMMANDS) and
    advertised by that subsystem's capabilities -- reaches
    web_interface/routes/tests_level.py's `testable_commands` and the
    tests_subsystem.html template's automatic/actuator loops.

    Mutation proof: changed `testable_commands` to `return set(advertised
    or [])` (drop the allowlist intersection entirely) and ran this whole
    class. 3 failed, 1 passed:
      FAILED test_control_command_advertised_gets_no_button
      FAILED test_payment_enable_advertised_gets_no_button
      FAILED test_intersection_helper_directly
      (test_allowlisted_and_advertised_gets_a_button kept passing --
       unrelated to this mutation, it only checks the button that SHOULD
       be there)
    test_intersection_helper_directly's failure detail:
      AssertionError: assert {'coin_return_test', 'ping', 'refund'} ==
      {'coin_return_test', 'ping'} -- Extra items in the left set: 'refund'
    Restored `testable_commands`'s real intersection -- all 4 passed again.
    """

    def test_allowlisted_and_advertised_gets_a_button(self, client, wire_subsystem):
        wire_subsystem("mdb", ["ping", "refund"])
        resp = client.get("/tests/mdb")
        assert resp.status_code == 200
        assert 'hx-post="/tests/mdb/ping"' in resp.text

    def test_control_command_advertised_gets_no_button(self, client, wire_subsystem):
        wire_subsystem("mdb", ["ping", "refund"])
        resp = client.get("/tests/mdb")
        assert resp.status_code == 200
        assert 'hx-post="/tests/mdb/refund"' not in resp.text

    def test_payment_enable_advertised_gets_no_button(self, client, wire_subsystem):
        wire_subsystem("vending", ["ping", "payment/enable"])
        resp = client.get("/tests/vending")
        assert resp.status_code == 200
        assert 'hx-post="/tests/vending/payment/enable"' not in resp.text

    def test_intersection_helper_directly(self):
        """Unit-level check of the helper itself, independent of HTML --
        reaches contracts.common.TESTABLE_COMMANDS directly."""
        result = compute_testable("mdb", ["ping", "refund", "coin_return_test"])
        assert result == {"ping", "coin_return_test"}
        assert "refund" not in result


class TestNoTestsAdvertised:
    """A subsystem advertising nothing (never spoken, or an older contract
    with no capabilities doc at all) shows the "no tests advertised"
    placeholder rather than an empty page or a 500.

    Mutation proof: same `testable_commands` mutation as above (return
    every advertised command with no allowlist check) has no effect on
    this specific test since advertised is empty either way, so a second,
    more targeted mutation was used: changed tests_subsystem.html's guard
    from `{% if not automatic and not actuator %}` to `{% if false %}`
    (i.e. never show the placeholder). Both
    test_alive_but_nothing_advertised and
    test_never_seen_shows_no_tests_advertised then failed ("No tests
    advertised." missing from resp.text); test_unknown_subsystem_is_404
    kept passing (it never reaches this branch -- 404 before any
    template renders). Restored the guard -- all 3 passed again.
    """

    def test_alive_but_nothing_advertised(self, client, wired):
        _cfg, vmc, _inv, _store = wired
        from services.health_monitor import HealthMonitor

        hm = HealthMonitor()
        hm.record_heartbeat(
            "ice_maker", {"subsystem": "ice_maker", "uptime_seconds": 5}
        )
        routes.set_health_monitor(hm)
        try:
            resp = client.get("/tests/ice_maker")
            assert resp.status_code == 200
            assert "No tests advertised." in resp.text
        finally:
            routes.set_health_monitor(None)

    def test_never_seen_shows_no_tests_advertised(self, client):
        assert context.health_monitor is None
        resp = client.get("/tests/mdb")
        assert resp.status_code == 200
        assert "No tests advertised." in resp.text

    def test_unknown_subsystem_is_404(self, client):
        resp = client.get("/tests/nope")
        assert resp.status_code == 404


# --- The maintenance lease: viewing never takes it ----------------------


class TestLeaseNotTaken:
    """Entering /tests, or any subsystem's detail page, must never call
    VMC.begin_maintenance -- it only ever reads the read-only
    `maintenance_hold` property. Reaches
    web_interface/routes/tests_level.py's `tests_level` handler.

    Mutation proof: added `context.vmc_instance.begin_maintenance(
    "mutation-test", "mutation-session")` as the first line inside the
    `tests_level` handler (simulating the bug the brief calls out by
    name: "starting a test does [take the lease], entering does not").
    test_viewing_tests_does_not_take_lease then failed (vmc.
    maintenance_hold was a MaintenanceHold, not None, after the GET --
    log line "Maintenance lease granted to user=mutation-test
    session=mutation-session" confirms it was actually taken).
    test_viewing_subsystem_does_not_take_lease kept passing (the mutation
    only touched the /tests handler, not /tests/{subsystem}). Removed the
    injected call -- both passed again.
    """

    def test_viewing_tests_does_not_take_lease(self, client, wired):
        _cfg, vmc, _inv, _store = wired
        assert vmc.maintenance_hold is None
        resp = client.get("/tests")
        assert resp.status_code == 200
        assert vmc.maintenance_hold is None

    def test_viewing_subsystem_does_not_take_lease(self, client, wired):
        _cfg, vmc, _inv, _store = wired
        resp = client.get("/tests/vending")
        assert resp.status_code == 200
        assert vmc.maintenance_hold is None


class TestLeaseHolderDisplay:
    """/tests shows the current lease holder, and offers End when the
    viewer is the holder or Take over when they are not -- reaches
    `tests_level`'s `hold`/`held_by_me` computation and tests.html's
    banner.
    """

    def test_shows_holder_and_take_over_for_a_different_session(self, client, wired):
        _cfg, vmc, _inv, _store = wired
        granted, reason = vmc.begin_maintenance("someone-else", "different-session")
        assert granted, reason
        resp = client.get("/tests")
        assert resp.status_code == 200
        assert "someone-else" in resp.text
        assert 'hx-post="/tests/takeover"' in resp.text
        assert 'hx-post="/tests/end"' not in resp.text

    def test_shows_end_for_the_holders_own_session(self, client, wired):
        _cfg, vmc, _inv, store = wired
        owner = store.owner()
        session_id = client.cookies.get(web_auth.SESSION_COOKIE)
        assert session_id, "owner client must carry a session cookie"
        granted, reason = vmc.begin_maintenance(owner.id, session_id)
        assert granted, reason
        resp = client.get("/tests")
        assert resp.status_code == 200
        assert 'hx-post="/tests/end"' in resp.text
        assert 'hx-post="/tests/takeover"' not in resp.text

    def test_no_banner_when_no_lease_held(self, client, wired):
        _cfg, vmc, _inv, _store = wired
        assert vmc.maintenance_hold is None
        resp = client.get("/tests")
        assert 'id="tests-hold"' not in resp.text


# --- Contract match -------------------------------------------------


class TestContractMatch:
    """Reaches `contract_match` (web_interface/routes/tests_level.py) --
    the one place this program computes a contract-version match; no
    sibling exists to reuse (see that function's docstring).

    Mutation proof: changed `contract_match`'s final `return reported ==
    expected` to `return True` unconditionally (once past the None
    guard). Ran the whole class: test_mismatched_contract_version failed
    ("Contract mismatch" missing from resp.text -- it now said "Contract
    OK"); test_matching_contract_version, test_unknown_when_never_seen
    and test_helper_returns_none_for_unseen_row all kept passing (the
    first two never exercise a real mismatch; the None-guard is untouched
    by this mutation). Restored the real comparison -- all 4 passed
    again.
    """

    def test_matching_contract_version(self, client, wire_subsystem):
        from contracts.vending_machine import CONTRACT_VERSION

        wire_subsystem("vending", ["ping"], contract_version=CONTRACT_VERSION)
        resp = client.get("/tests")
        assert resp.status_code == 200
        assert "Contract OK" in resp.text

    def test_mismatched_contract_version(self, client, wire_subsystem):
        wire_subsystem("vending", ["ping"], contract_version="0.0.1-ancient")
        resp = client.get("/tests")
        assert resp.status_code == 200
        assert "Contract mismatch" in resp.text

    def test_unknown_when_never_seen(self, client):
        assert context.health_monitor is None
        resp = client.get("/tests")
        assert resp.status_code == 200
        assert "Contract unknown" in resp.text

    def test_helper_returns_none_for_unseen_row(self):
        from services.health_monitor import HealthMonitor

        row = HealthMonitor.empty_subsystem_row()
        assert contract_match("vending", row) is None


# --- /tests/log -----------------------------------------------------


class TestLog:
    """Reaches web_interface/routes/tests_level.py's `_recent_test_runs`
    (a direct sqlite3 read against EventRecorder._db_path, following
    services/reports.py's own documented deviation) and tests_log.html.
    """

    def test_no_verdict_renders_verdict_none(self, client, wire_event_recorder):
        wire_event_recorder.record(
            "test_run",
            metadata={
                "run_id": "r1",
                "subsystem": "mdb",
                "command": "ping",
                "params": {},
                "status": "ok",
                "verdict": None,
                "note": None,
            },
        )
        wire_event_recorder.flush()
        resp = client.get("/tests/log")
        assert resp.status_code == 200
        assert "verdict: none" in resp.text

    def test_survives_a_row_missing_subsystem_command_params(
        self, client, wire_event_recorder
    ):
        """The simulated-sale shape controller/vmc.py's run_test_sale
        writes today: {sku, outcome, fault_code, path}, no run_id, no
        subsystem/command/params at all.

        Mutation proof: changed `_recent_test_runs` to build the row via
        direct subscription (`meta["subsystem"]`, `meta["command"]`,
        `meta.get("params")`) instead of `row.update(meta)`. Ran the whole
        TestLog class: 2 failed, 3 passed --
          FAILED test_survives_a_row_missing_subsystem_command_params
            KeyError: 'subsystem' (web_interface/routes/tests_level.py:173)
            -- a real 500 traceback from inside the request, not merely a
            wrong assertion.
          FAILED test_verdict_pass_and_fail_render
            AssertionError: assert 'verdict: pass' in '...verdict: none...'
            -- the mutation also dropped `verdict`/`note` from every row
            (only subsystem/command/params were kept), a second, cheaper
            symptom of the same bug.
          (test_no_verdict_renders_verdict_none, test_no_recorder_renders_
           empty_state and test_limited_to_last_100 kept passing --
           unaffected by this particular row shape)
        Restored the plain `row.update(meta)` merge -- all 5 passed again.
        """
        wire_event_recorder.record(
            "test_run",
            value=1.5,
            metadata={
                "sku": "ICE-1",
                "outcome": "dispensed",
                "fault_code": None,
                "path": ["idle", "interacting_with_user", "dispensing", "idle"],
            },
        )
        wire_event_recorder.flush()
        resp = client.get("/tests/log")
        assert resp.status_code == 200
        assert "verdict: none" in resp.text
        assert "ICE-1" in resp.text

    def test_verdict_pass_and_fail_render(self, client, wire_event_recorder):
        wire_event_recorder.record(
            "test_run",
            metadata={
                "run_id": "r-pass",
                "subsystem": "mdb",
                "command": "ping",
                "verdict": "pass",
                "note": "sounded right",
            },
        )
        wire_event_recorder.record(
            "test_run",
            metadata={
                "run_id": "r-fail",
                "subsystem": "mdb",
                "command": "ping",
                "verdict": "fail",
                "note": "no click",
            },
        )
        wire_event_recorder.flush()
        resp = client.get("/tests/log")
        assert resp.status_code == 200
        assert "verdict: pass" in resp.text
        assert "verdict: fail" in resp.text
        assert "sounded right" in resp.text
        assert "no click" in resp.text

    def test_user_status_and_duration_render(self, client, wire_event_recorder):
        """Copilot review (PR 22, id=4128088722): spec §3 requires the log
        to show who ran it, the status, and the duration -- the card
        previously rendered none of the three, and _recent_test_runs
        didn't even select the event `value` column that holds the
        duration. `value=12.5` here is the run's actual DURATION (how
        long the command took), not "time ago" -- distinct from
        elapsed_seconds, which the card already rendered before this fix
        and which this test doesn't touch."""
        wire_event_recorder.record(
            "test_run",
            value=12.5,
            metadata={
                "run_id": "r-status",
                "user_id": "user-1",
                "user_name": "Ada Owner",
                "subsystem": "mdb",
                "command": "self_test",
                "params": {},
                "status": "ok",
                "verdict": None,
                "note": None,
            },
        )
        wire_event_recorder.flush()
        resp = client.get("/tests/log")
        assert resp.status_code == 200
        assert "Ada Owner" in resp.text
        assert "status: ok" in resp.text
        assert "12" in resp.text  # humanize_seconds(12.5) == "12s"

    def test_simulated_sale_user_status_and_duration_render(
        self, client, wire_event_recorder
    ):
        """Same guarantee for run_test_sale's row shape (sku/outcome/
        fault_code/path, no subsystem/command) -- user_name and status are
        present in that metadata shape too (controller/vmc.py's
        run_test_sale)."""
        wire_event_recorder.record(
            "test_run",
            value=3.25,
            metadata={
                "sku": "ICE-1",
                "user_id": "user-1",
                "user_name": "Tara Tech",
                "outcome": "dispensed",
                "fault_code": None,
                "status": "ok",
                "path": ["idle", "interacting_with_user", "dispensing", "idle"],
            },
        )
        wire_event_recorder.flush()
        resp = client.get("/tests/log")
        assert resp.status_code == 200
        assert "Tara Tech" in resp.text
        assert "status: ok" in resp.text
        assert "3s" in resp.text  # humanize_seconds(3.25) == "3s"

    def test_no_recorder_renders_empty_state(self, client):
        assert context.event_recorder is None
        resp = client.get("/tests/log")
        assert resp.status_code == 200
        assert "No test runs yet." in resp.text

    def test_limited_to_last_100(self, client, wire_event_recorder):
        for i in range(105):
            wire_event_recorder.record(
                "test_run",
                metadata={"run_id": f"r{i}", "subsystem": "mdb", "command": "ping"},
            )
        wire_event_recorder.flush()
        resp = client.get("/tests/log")
        assert resp.status_code == 200
        assert resp.text.count("verdict: none") == 100


# --- SKU-with-slash survives the params widget ---------------------


class TestSkuWithSlash:
    """The dispense params widget on /tests/vending lists every catalog
    product (slot + name + SKU) as plain option text -- never in a URL or
    CSS-id selector here (those are keyed on `subsystem`/`cmd.name`, both
    drawn from the fixed EXPECTED_SUBSYSTEMS/TESTABLE_COMMANDS vocabulary,
    never from catalog text -- see tests_subsystem.html's command_card
    macro comment), so no sku_url_segment-style escaping is needed for
    this particular widget. This test proves that text survives intact
    for a SKU containing "/".

    Mutation proof: changed `_params_widget`'s "dispense" branch to
    `return {"kind": "slot", "products": []}` (drop the catalog products).
    test_sku_with_slash_appears_in_the_option_list then failed (the SKU
    string was absent from resp.text). Restored the real `products` list
    -- passed again.
    """

    def test_sku_with_slash_appears_in_the_option_list(
        self, client, wired, wire_subsystem
    ):
        cfg, _vmc, _inv, _store = wired
        add_product(cfg, "ICE/COLD-1", "Cold Ice", 2.5, slot=3)
        wire_subsystem("vending", ["dispense"])
        resp = client.get("/tests/vending")
        assert resp.status_code == 200
        assert "ICE/COLD-1" in resp.text
        assert 'value="3"' in resp.text


# --- Param widget ranges match the contract's validators ---------------


class TestParamRangesMatchContract:
    """web_interface/routes/tests_level.py's WATER_VALVE_SECONDS_RANGE and
    POWER_CYCLE_DWELL_RANGE are a second literal copy of
    contracts/common.py's COMMAND_PARAM_VALIDATORS bounds (that module
    exposes validator functions, not reusable constants, and is out of
    this task's file list -- see tests_level.py's module docstring). This
    test is what keeps the two from silently drifting apart: it calls the
    REAL validators at the exact boundary values the widget renders.

    Mutation proof: changed WATER_VALVE_SECONDS_RANGE to (1, 11) in
    tests_level.py (simulating this constant having drifted from
    contracts/common.py's real bound, still 1-10). test_water_valve_
    seconds_range then failed -- not at the `pytest.raises` boundary
    check, but one line earlier, at the plain `validator({"seconds":
    hi})` call (hi now 11), which is expected to succeed:
      ValueError: water_valve requires seconds in [1, 10]
      (contracts/common.py:70, inside _validate_water_valve)
    i.e. the widget's own upper bound is no longer a value the real
    contract actually accepts. The other three tests in this class kept
    passing (test_power_cycle_dwell_range_and_default checks a different
    constant; the two test_rendered_*_bounds tests only check the HTML
    against tests_level.py's own constant, so they're self-consistent
    even when that constant is wrong -- which is exactly why this
    dedicated contract-calling test exists). Restored (1, 10) -- all 4
    passed again.
    """

    def test_water_valve_seconds_range(self):
        lo, hi = WATER_VALVE_SECONDS_RANGE
        validator = COMMAND_PARAM_VALIDATORS["water_valve"]
        validator({"seconds": lo})
        validator({"seconds": hi})
        with pytest.raises(ValueError):
            validator({"seconds": lo - 1})
        with pytest.raises(ValueError):
            validator({"seconds": hi + 1})

    def test_power_cycle_dwell_range_and_default(self):
        lo, hi = POWER_CYCLE_DWELL_RANGE
        validator = COMMAND_PARAM_VALIDATORS["power_cycle"]
        validator({"dwell_seconds": lo})
        validator({"dwell_seconds": hi})
        with pytest.raises(ValueError):
            validator({"dwell_seconds": lo - 1})
        with pytest.raises(ValueError):
            validator({"dwell_seconds": hi + 1})
        assert lo <= POWER_CYCLE_DWELL_DEFAULT <= hi

    def test_rendered_widget_carries_the_same_bounds(self, client, wire_subsystem):
        wire_subsystem("vending", ["water_valve"])
        resp = client.get("/tests/vending")
        lo, hi = WATER_VALVE_SECONDS_RANGE
        assert f'min="{lo}"' in resp.text
        assert f'max="{hi}"' in resp.text

    def test_rendered_dwell_widget_carries_the_same_bounds_and_default(
        self, client, wire_subsystem
    ):
        wire_subsystem("ice_maker", ["power_cycle"])
        resp = client.get("/tests/ice_maker")
        lo, hi = POWER_CYCLE_DWELL_RANGE
        assert f'min="{lo}"' in resp.text
        assert f'max="{hi}"' in resp.text
        assert f'value="{POWER_CYCLE_DWELL_DEFAULT}"' in resp.text


# ======================================================================
# Task 13b: the action routes
# ======================================================================


# --- THE security boundary: the allowlist re-check on POST --------------


class TestAllowlistSecurityBoundary:
    """THE security boundary (task brief): POST /tests/{subsystem}/
    {command} re-checks `testable_commands` against the LIVE, freshly-read
    advertised list -- never the button rendering. Uses the `client`
    fixture (owner, which holds run_tests) so a 403 here can only be the
    allowlist, never the permission gate -- see
    TestCommandRunAndSalePermissionGates below for that separate axis
    (verification rule 3: don't let the wrong gate explain a 403).

    Mutation proof: commented out the `if command not in testable: raise
    HTTPException(403, ...)` block in post_test_command entirely. Ran this
    whole class: 3 failed, 0 passed --
      FAILED test_post_refund_directly_is_403 (200 != 403)
      FAILED test_post_payment_enable_directly_is_403 (200 != 403)
      FAILED test_allowlisted_but_unadvertised_is_403 (200 != 403)
    each fell through to a real dispatch attempt instead (no dispatcher
    wired, so each rendered a 200 "timeout" result card). Restored the
    check -- all 3 passed again.
    """

    def test_post_refund_directly_is_403(self, client, wire_subsystem):
        # "refund" advertised on purpose -- proves the allowlist refuses
        # it even when the capabilities doc claims to support it.
        wire_subsystem("vending", ["ping", "refund"])
        resp = client.post("/tests/vending/refund")
        assert resp.status_code == 403

    def test_post_payment_enable_directly_is_403(self, client, wire_subsystem):
        # A command name containing "/" -- POST /tests/vending/payment/enable
        # must REACH this handler (via {command:path}) and be refused by
        # the allowlist, not 404 before the security check ever runs.
        wire_subsystem("vending", ["ping", "payment/enable"])
        resp = client.post("/tests/vending/payment/enable")
        assert resp.status_code == 403

    def test_allowlisted_but_unadvertised_is_403(self, client, wire_subsystem):
        # "dispense" IS in TESTABLE_COMMANDS["vending"] -- but this
        # subsystem advertises only "ping", so the intersection is empty.
        wire_subsystem("vending", ["ping"])
        resp = client.post("/tests/vending/dispense", data={"slot": "0"})
        assert resp.status_code == 403


# --- Permission gates on every POST action route -------------------------


class TestPostPermissionMatrix:
    """run_tests gates every POST action route, same as the GET routes
    (TestPermissionGates above) -- owner/tech 200, secretary/loader 403.
    These four routes need no extra setup to reach 200 for an authorized
    role (no lease, no dispatcher, no subsystem all resolve to a harmless
    response), so this matrix isolates the permission axis cleanly.
    POST /tests/{subsystem}/{command} and POST /tests/sale need a wired
    subsystem/product respectively to ever reach 200 and get their own
    dedicated permission tests below instead.

    Mutation proof: replaced POST /tests/end's `dependencies=[...]` (both
    the permission and require_htmx guards) with `dependencies=[]`. Ran
    the whole matrix: 2 failed --
    test_matrix[/tests/end-form1-secretary] and
    [/tests/end-form1-loader] (200 != 403); every other case (owner/tech
    on /tests/end, and every role on the other three routes) kept
    passing. Restored the dependencies -- 16 passed again. (The same
    mutation also proves TestPostRequiresHtmx's CSRF guard below -- see
    its own docstring.)
    """

    @pytest.mark.parametrize("role", list(Role))
    @pytest.mark.parametrize(
        "path,form",
        [
            ("/tests/run-all", {}),
            ("/tests/end", {}),
            ("/tests/takeover", {}),
            ("/tests/runs/nonexistent-run/verdict", {"verdict": "pass"}),
        ],
    )
    def test_matrix(self, login_as, path, form, role):
        client = login_as(role)
        resp = client.post(path, data=form)
        if Permission.run_tests in ROLE_PERMISSIONS[role]:
            assert resp.status_code == 200, (role, path, resp.status_code)
        else:
            assert resp.status_code == 403, (role, path, resp.status_code)


class TestCommandRunAndSalePermissionGates:
    """POST /tests/{subsystem}/{command} and POST /tests/sale each need
    real setup (an advertised, allowlisted command / a catalog product) to
    ever reach 200 for an authorized role -- reached separately from
    TestPostPermissionMatrix's four no-setup routes.
    """

    @pytest.mark.parametrize("role", list(Role))
    def test_command_run_matrix(self, login_as, wire_subsystem, role):
        wire_subsystem("mdb", ["ping"])
        client = login_as(role)
        resp = client.post("/tests/mdb/ping")
        if Permission.run_tests in ROLE_PERMISSIONS[role]:
            assert resp.status_code == 200, (role, resp.status_code)
        else:
            assert resp.status_code == 403, (role, resp.status_code)

    @pytest.mark.parametrize("role", list(Role))
    def test_sale_matrix(self, login_as, wired, role):
        # Locked out so run_test_sale refuses SYNCHRONOUSLY (see
        # TestSimulatedSaleFlow's class docstring: `wired`'s VMC is never
        # attach_to_loop()'d, so a real dispense wait would hang forever
        # here -- this matrix only needs to prove the permission axis).
        cfg, vmc, _inv, _store = wired
        add_product(cfg, "ICE-1", "Ice Bag", 2.50, slot=0)
        vmc._lockouts["ICE-1"] = FaultCode.ICE_101
        client = login_as(role)
        resp = client.post("/tests/sale", data={"sku": "ICE-1"})
        if Permission.run_tests in ROLE_PERMISSIONS[role]:
            assert resp.status_code == 200, (role, resp.status_code)
        else:
            assert resp.status_code == 403, (role, resp.status_code)


class TestPostRequiresHtmx:
    """require_htmx (web_interface/context.py) is the CSRF guard shared by
    every mutating route in this app (this module's own docstring). One
    representative action route proves it is wired on this level's POSTs
    too.

    Mutation proof: removed `Depends(context.require_htmx)` from POST
    /tests/end's dependencies. test_post_without_hx_header_is_403 failed
    (200 != 403). Restored -- passed again.
    """

    def test_post_without_hx_header_is_403(self, client, wired):
        resp = client.post("/tests/end", headers={"HX-Request": "false"})
        assert resp.status_code == 403


# --- The maintenance lease: a run TAKES it, viewing never does ----------


class TestLeaseTakenByRun:
    """Unlike GET /tests and GET /tests/{subsystem} (TestLeaseNotTaken,
    Task 13a -- viewing never takes the lease), a POST run DOES. Reaches
    _acquire_lease_or_refusal via post_test_command.

    Mutation proof: replaced _acquire_lease_or_refusal's whole body with
    `return None` (never calling vmc.begin_maintenance, and never
    refusing). Result: 5 failed across two classes --
    test_running_a_command_takes_the_lease (vmc.maintenance_hold was
    still None after the 200 response -- SVC-102 never raised),
    test_refused_inline_when_held_by_someone_else (the request reached
    the dispatcher instead of being refused), and all three TestRunAll
    tests below (with no real lease ever granted, `vmc.maintenance_
    test_run()` inside `_run_command` raises `RuntimeError("no
    maintenance lease held")`, which TestClient's `raise_server_
    exceptions=True` default re-raises instead of returning 200) --
    confirming run-all depends on this same helper too. Restored the
    real body -- all 5 passed again (2 here, 3 in TestRunAll).
    """

    def test_running_a_command_takes_the_lease(self, client, wired, wire_subsystem):
        _cfg, vmc, _inv, _store = wired
        wire_subsystem("vending", ["ping"])
        assert vmc.maintenance_hold is None
        resp = client.post("/tests/vending/ping")
        assert resp.status_code == 200
        assert vmc.maintenance_hold is not None

    def test_refused_inline_when_held_by_someone_else(
        self, wired, login_as, wire_subsystem, wire_dispatcher
    ):
        """A command run while a DIFFERENT session holds the lease is
        refused INLINE (200, a refusal fragment) and never reaches the
        dispatcher -- task brief: "takes the lease or returns the refusal
        inline".

        Mutation proof: changed _acquire_lease_or_refusal's "held by a
        different session" branch to `return None` (silently proceed
        instead of refusing). Result: this test failed -- the fake
        dispatcher's `calls` list gained an entry it should never have
        (the request reached the dispatcher instead of being refused
        first), and "held by" was absent from the response text. Restored
        -- passed again.
        """
        _cfg, vmc, _inv, store = wired
        wire_subsystem("mdb", ["ping"])
        dispatcher = wire_dispatcher(FakeAckDispatcher())
        owner_client = login_as(Role.owner)
        tech_client = login_as(Role.tech)
        owner_session = owner_client.cookies.get(web_auth.SESSION_COOKIE)
        granted, reason = vmc.begin_maintenance(store.owner().id, owner_session)
        assert granted, reason

        resp = tech_client.post("/tests/mdb/ping")
        assert resp.status_code == 200
        assert "held by" in resp.text
        assert dispatcher.calls == []


# --- End / Take over ------------------------------------------------------


class TestLeaseEndAndTakeover:
    """POST /tests/end releases only the CALLER's own lease
    (VMC.end_maintenance already enforces the session match); POST
    /tests/takeover transfers an idle lease (VMC.take_over_maintenance,
    system-tests design §2.2), refused while a run is in flight or before
    the 60s idle threshold.

    Mutation proof (takeover): changed post_test_command's sibling
    tests_takeover handler to call `vmc.begin_maintenance(principal.
    user.id, principal.session.id)` instead of `vmc.take_over_maintenance
    (...)`. Ran this class: test_takeover_succeeds_once_idle failed --
    begin_maintenance refuses whenever ANY lease is held (regardless of
    idle time), so the holder never changed even after the 61s idle
    backdate ("held by <owner's id>" persisted). test_end_releases_only_
    the_callers_own_lease and test_takeover_refused_before_idle_threshold
    kept passing (neither reaches take_over_maintenance's success path).
    Restored the real `take_over_maintenance` call -- all 3 passed again.
    """

    def test_end_releases_only_the_callers_own_lease(self, wired, login_as):
        _cfg, vmc, _inv, store = wired
        owner_client = login_as(Role.owner)
        tech_client = login_as(Role.tech)
        owner_session = owner_client.cookies.get(web_auth.SESSION_COOKIE)
        granted, reason = vmc.begin_maintenance(store.owner().id, owner_session)
        assert granted, reason

        resp = tech_client.post("/tests/end")
        assert resp.status_code == 200
        assert vmc.maintenance_hold is not None
        assert vmc.maintenance_hold.holder_session_id == owner_session

        # The actual holder CAN end it.
        owner_resp = owner_client.post("/tests/end")
        assert owner_resp.status_code == 200
        assert vmc.maintenance_hold is None

    def test_takeover_refused_before_idle_threshold(self, wired, login_as):
        _cfg, vmc, _inv, store = wired
        owner_client = login_as(Role.owner)
        tech_client = login_as(Role.tech)
        owner_session = owner_client.cookies.get(web_auth.SESSION_COOKIE)
        granted, reason = vmc.begin_maintenance(store.owner().id, owner_session)
        assert granted, reason

        resp = tech_client.post("/tests/takeover")
        assert resp.status_code == 200
        assert vmc.maintenance_hold.holder_session_id == owner_session  # unchanged
        assert "held by" in resp.text

    def test_takeover_succeeds_once_idle(self, wired, login_as):
        _cfg, vmc, _inv, store = wired
        owner_client = login_as(Role.owner)
        tech_client = login_as(Role.tech)
        owner_session = owner_client.cookies.get(web_auth.SESSION_COOKIE)
        tech_session = tech_client.cookies.get(web_auth.SESSION_COOKIE)
        granted, reason = vmc.begin_maintenance(store.owner().id, owner_session)
        assert granted, reason
        vmc.maintenance_hold.last_activity_at -= 61  # force past the 60s threshold

        resp = tech_client.post("/tests/takeover")
        assert resp.status_code == 200
        assert vmc.maintenance_hold.holder_session_id == tech_session


# --- Verdict --------------------------------------------------------------


class TestVerdict:
    """POST /tests/runs/{run_id}/verdict updates THAT run's row via
    EventRecorder.update_metadata, located by run_id -- reached the same
    way whether the row came from a subsystem-command run or a simulated
    sale (both carry run_id now).

    Mutation proof: changed post_verdict's `context.event_recorder.
    update_metadata(run_id, ...)` call to pass a hardcoded
    `run_id="wrong-id"` instead of the path parameter. Result:
    test_verdict_updates_only_the_targeted_run failed -- r1's row still
    said "verdict: none" (the update landed nowhere real, since no row's
    metadata.run_id equals "wrong-id" -- update_metadata's own documented
    no-op-with-a-warning behavior). Restored -- passed again.
    """

    def test_verdict_updates_only_the_targeted_run(self, client, wire_event_recorder):
        wire_event_recorder.record(
            "test_run", metadata={"run_id": "r1", "subsystem": "mdb", "command": "ping"}
        )
        wire_event_recorder.record(
            "test_run",
            metadata={"run_id": "r2", "subsystem": "mdb", "command": "self_test"},
        )
        wire_event_recorder.flush()

        resp = client.post(
            "/tests/runs/r1/verdict", data={"verdict": "pass", "note": "sounded right"}
        )
        assert resp.status_code == 200

        log_resp = client.get("/tests/log")
        assert log_resp.text.count("verdict: pass") == 1
        assert log_resp.text.count("verdict: none") == 1  # r2 untouched
        assert "sounded right" in log_resp.text

    def test_invalid_verdict_is_400(self, client, wire_event_recorder):
        wire_event_recorder.record(
            "test_run", metadata={"run_id": "r1", "subsystem": "mdb", "command": "ping"}
        )
        wire_event_recorder.flush()
        resp = client.post("/tests/runs/r1/verdict", data={"verdict": "maybe"})
        assert resp.status_code == 400


# --- Ack/timeout rendering (spec §6) ---------------------------------------


class TestResultRendering:
    """POST /tests/{subsystem}/{command} renders the ack's status/detail
    (spec §6: "rejected/failed/unsupported acks show the message
    verbatim") and logs that same status onto the test_run row -- reaches
    _run_command's ack branch (ok/rejected/failed/unsupported) and its
    CommandTimeout branch (the exact "no answer from <subsystem> after 2
    attempts" wording).

    Mutation proof: changed the ack branch's `detail = ack.detail` to
    `detail = None` unconditionally. Ran this whole class: 3 failed --
    test_rejected/_failed/_unsupported_ack_shows_detail_verbatim (their
    exact detail strings vanished from resp.text); test_ok_ack_status_ok
    and test_timeout_renders_exact_message kept passing (neither depends
    on ack.detail). Restored -- all 5 passed again.
    """

    def _post_with_ack(self, client, subsystem, command, dispatcher, **form):
        routes.set_command_dispatcher(dispatcher)
        try:
            return client.post(f"/tests/{subsystem}/{command}", data=form)
        finally:
            routes.set_command_dispatcher(None)

    def test_ok_ack_status_ok(self, client, wired, wire_subsystem, wire_event_recorder):
        wire_subsystem("mdb", ["ping"])
        resp = self._post_with_ack(
            client, "mdb", "ping", FakeAckDispatcher(status="ok")
        )
        assert resp.status_code == 200
        assert _recent_test_runs(1)[0]["status"] == "ok"

    def test_rejected_ack_shows_detail_verbatim(
        self, client, wired, wire_subsystem, wire_event_recorder
    ):
        wire_subsystem("mdb", ["coin_return_test"])
        resp = self._post_with_ack(
            client,
            "mdb",
            "coin_return_test",
            FakeAckDispatcher(status="rejected", detail="blocked by service door"),
        )
        assert resp.status_code == 200
        assert "blocked by service door" in resp.text
        assert _recent_test_runs(1)[0]["status"] == "rejected"

    def test_failed_ack_shows_detail_verbatim(
        self, client, wired, wire_subsystem, wire_event_recorder
    ):
        wire_subsystem("mdb", ["coin_return_test"])
        resp = self._post_with_ack(
            client,
            "mdb",
            "coin_return_test",
            FakeAckDispatcher(status="failed", detail="motor stalled"),
        )
        assert resp.status_code == 200
        assert "motor stalled" in resp.text
        assert _recent_test_runs(1)[0]["status"] == "failed"

    def test_unsupported_ack_shows_detail_verbatim(
        self, client, wired, wire_subsystem, wire_event_recorder
    ):
        wire_subsystem("mdb", ["card_reader_test"])
        resp = self._post_with_ack(
            client,
            "mdb",
            "card_reader_test",
            FakeAckDispatcher(status="unsupported", detail="firmware too old"),
        )
        assert resp.status_code == 200
        assert "firmware too old" in resp.text
        assert _recent_test_runs(1)[0]["status"] == "unsupported"

    def test_timeout_renders_exact_message(
        self, client, wired, wire_subsystem, wire_event_recorder
    ):
        wire_subsystem("mdb", ["ping"])
        resp = self._post_with_ack(
            client, "mdb", "ping", FakeAckDispatcher(always_timeout=True, retries=1)
        )
        assert resp.status_code == 200
        assert "no answer from mdb after 2 attempts" in resp.text
        assert _recent_test_runs(1)[0]["status"] == "timeout"


# --- Run all ----------------------------------------------------------


class TestRunAll:
    """POST /tests/run-all: ping then self_test on every ALIVE subsystem,
    strictly IN SEQUENCE (task brief); one result table; broker down
    renders every alive subsystem's row unreachable (spec §6).

    Mutation proof (sequence): replaced the `for` loop's body in
    tests_run_all with code that collects every `_run_command(...)`
    coroutine into a list and `await`s them all at once via
    `asyncio.gather(*coros)`, instead of `await`ing each one inside the
    loop. Result: test_covers_every_alive_subsystem_in_sequence failed --
    `AssertionError: assert 4 == 1` at `dispatcher.max_concurrent == 1`
    (all four ping/self_test calls started concurrently). Restored the
    sequential `await` inside the loop -- passed again (max_concurrent
    back to 1).

    Mutation proof (lease-dependence, shared with TestLeaseTakenByRun):
    see that class's own docstring -- the same `_acquire_lease_or_
    refusal` mutation that breaks its two tests also breaks all three
    tests below (run-all depends on the same lease-acquisition helper).
    """

    def test_covers_every_alive_subsystem_in_sequence(
        self, client, wired, wire_subsystem, wire_dispatcher
    ):
        wire_subsystem("vending", ["ping", "self_test"])
        wire_subsystem("mdb", ["ping", "self_test"])
        dispatcher = wire_dispatcher(ConcurrencyCheckingDispatcher())

        resp = client.post("/tests/run-all")

        assert resp.status_code == 200
        assert dispatcher.max_concurrent == 1
        assert set(dispatcher.calls) == {
            ("vending", "ping"),
            ("vending", "self_test"),
            ("mdb", "ping"),
            ("mdb", "self_test"),
        }
        assert len(dispatcher.calls) == 4
        # ice_maker is EXPECTED_SUBSYSTEMS' third entry but was never
        # wire_subsystem'd alive -- confirms only ALIVE subsystems run.
        assert not any(c[0] == "ice_maker" for c in dispatcher.calls)

    def test_broker_down_shows_every_subsystem_unreachable(
        self, client, wired, wire_subsystem, wire_dispatcher
    ):
        wire_subsystem("vending", ["ping", "self_test"])
        wire_subsystem("mdb", ["ping", "self_test"])
        wire_dispatcher(FakeAckDispatcher(always_timeout=True, retries=1))

        resp = client.post("/tests/run-all")

        assert resp.status_code == 200
        assert resp.text.count("no answer from") == 4

    def test_refused_inline_when_lease_held_elsewhere(
        self, wired, login_as, wire_subsystem, wire_dispatcher
    ):
        _cfg, vmc, _inv, store = wired
        wire_subsystem("mdb", ["ping", "self_test"])
        dispatcher = wire_dispatcher(FakeAckDispatcher())
        owner_client = login_as(Role.owner)
        tech_client = login_as(Role.tech)
        owner_session = owner_client.cookies.get(web_auth.SESSION_COOKIE)
        granted, reason = vmc.begin_maintenance(store.owner().id, owner_session)
        assert granted, reason

        resp = tech_client.post("/tests/run-all")
        assert resp.status_code == 200
        assert "held by" in resp.text
        assert dispatcher.calls == []

    def test_run_all_dispatches_on_the_real_simulator_stack(
        self, client, wired, wire_subsystem, wire_dispatcher
    ):
        """Copilot review (PR 22, id=4128088689): the two tests above wire
        wire_subsystem("vending", ["ping", "self_test"]) directly -- a
        hand-picked commands list that would pass whether or not the real
        simulators actually advertise ping/self_test, exactly the
        "fixture that makes the branch unreachable" shape the review
        warns about. This test instead advertises each subsystem's REAL
        `build_capabilities().commands` (vending, mdb: the base class's
        list; ice_maker: its own MonitorCapabilities override) -- the
        actual output that would reach the VMC over MQTT on the compose
        simulator stack, which nothing here can run directly. Before the
        fix this failed with dispatcher.calls == [] (Run all executed
        nothing), because the advertised-∩-allowlist intersection was
        empty for the three automatic commands.
        """
        from simulators.ice_maker import IceMakerSimulator
        from simulators.mdb_gateway import MDBGatewaySimulator
        from simulators.vending_machine import VendingMachineSimulator

        wire_subsystem(
            "vending",
            VendingMachineSimulator(machine_id="vmc-t").build_capabilities().commands,
        )
        wire_subsystem(
            "mdb", MDBGatewaySimulator(machine_id="vmc-t").build_capabilities().commands
        )
        wire_subsystem(
            "ice_maker",
            IceMakerSimulator(machine_id="vmc-t").build_capabilities().commands,
        )
        dispatcher = wire_dispatcher(ConcurrencyCheckingDispatcher())

        resp = client.post("/tests/run-all")

        assert resp.status_code == 200
        assert set(dispatcher.calls) == {
            ("vending", "ping"),
            ("vending", "self_test"),
            ("mdb", "ping"),
            ("mdb", "self_test"),
            ("ice_maker", "ping"),
            ("ice_maker", "self_test"),
        }
        assert len(dispatcher.calls) == 6


# --- A failing run still frees the lease's run count -----------------------


class TestFailingRunStillDecrements:
    """A run whose dispatch raises (not merely times out) still decrements
    runs_in_flight -- reaches maintenance_test_run()'s own `finally` via
    _run_command, proven with a dispatcher that observes runs_in_flight AT
    THE MOMENT of the call (must be 1 -- incremented before dispatch) and
    the test checks it is back to 0 afterward -- a version that never
    increments at all cannot fake the first assertion, and a version that
    increments but never decrements (a dropped `finally`) cannot fake the
    second.

    Mutation proof: replaced `with vmc.maintenance_test_run():` in
    _run_command with a bare `if True:` (dropping the context manager, so
    neither increment nor decrement ever runs). Result:
    test_failing_run_still_decrements failed --
    `AssertionError: assert 0 == 1` at `dispatcher.observed_in_flight ==
    1` (never incremented, so the observer saw `runs_in_flight` still at
    its starting value, 0, not 1). Restored the `with` -- passed again.
    """

    def test_failing_run_still_decrements(
        self, client, wired, wire_subsystem, wire_dispatcher
    ):
        _cfg, vmc, _inv, _store = wired
        wire_subsystem("mdb", ["ping"])

        class ExplodingDispatcher:
            _retries = 1

            def __init__(self, vmc):
                self._vmc = vmc
                self.observed_in_flight = None

            async def send(self, subsystem, command, params=None):
                hold = self._vmc.maintenance_hold
                self.observed_in_flight = hold.runs_in_flight if hold else None
                raise RuntimeError("simulated dispatcher bug")

        dispatcher = ExplodingDispatcher(vmc)
        wire_dispatcher(dispatcher)

        with pytest.raises(RuntimeError):
            client.post("/tests/mdb/ping")

        assert dispatcher.observed_in_flight == 1  # incremented before dispatch
        assert vmc.maintenance_hold is not None  # lease itself still held
        assert vmc.maintenance_hold.runs_in_flight == 0  # but decremented after


# --- Param validation --------------------------------------------------


class TestParamValidation:
    """POST /tests/{subsystem}/{command} validates params against the SAME
    bounds the widget renders (WATER_VALVE_SECONDS_RANGE etc, Task 13a) --
    reaches _parse_command_params, before the lease is ever touched.

    Mutation proof: removed the `if not (lo <= value <= hi): raise
    ValueError(...)` bounds check from _parse_command_params's
    water_valve branch. Result: test_water_valve_seconds_out_of_range_
    is_400 failed -- 200 instead of 400, and vmc.maintenance_hold was
    granted (the lease WAS taken for a value the widget itself would
    never submit). Restored the check -- passed again.
    """

    def test_dispense_slot_must_be_an_integer(self, client, wired, wire_subsystem):
        wire_subsystem("vending", ["dispense"])
        resp = client.post("/tests/vending/dispense", data={"slot": "not-a-number"})
        assert resp.status_code == 400

    def test_dispense_slot_not_in_catalog_is_400(
        self, client, wired, wire_subsystem, wire_dispatcher
    ):
        """Copilot review (PR 22, id=4128088598): a crafted POST with a
        syntactically valid but nonexistent slot must be refused before
        the lease is taken or the dispatcher is called -- the simulator
        maps an unknown slot to ice by default, so this would otherwise
        still actuate hardware. The fixture's catalog is empty (no
        add_product call), so any integer slot is "not in the catalog"."""
        cfg, vmc, _inv, _store = wired
        assert cfg.products == []  # guards the fixture assumption above
        wire_subsystem("vending", ["dispense"])
        dispatcher = wire_dispatcher(FakeAckDispatcher())

        resp = client.post("/tests/vending/dispense", data={"slot": "0"})

        assert resp.status_code == 400
        assert vmc.maintenance_hold is None
        assert dispatcher.calls == []

    def test_dispense_slot_in_catalog_is_accepted(
        self, client, wired, wire_subsystem, wire_dispatcher
    ):
        """Same crafted-request path, proving the fix doesn't also refuse
        a legitimate slot: once the SKU is on the catalog at slot 3, that
        exact slot is accepted and reaches the dispatcher."""
        cfg, _vmc, _inv, _store = wired
        add_product(cfg, "ICE-1", "Ice", 2.5, slot=3)
        wire_subsystem("vending", ["dispense"])
        dispatcher = wire_dispatcher(FakeAckDispatcher())

        resp = client.post("/tests/vending/dispense", data={"slot": "3"})

        assert resp.status_code == 200
        assert dispatcher.calls == [("vending", "dispense", {"slot": 3})]

    def test_water_valve_seconds_out_of_range_is_400(
        self, client, wired, wire_subsystem
    ):
        _cfg, vmc, _inv, _store = wired
        wire_subsystem("vending", ["water_valve"])
        lo, hi = WATER_VALVE_SECONDS_RANGE
        resp = client.post("/tests/vending/water_valve", data={"seconds": str(hi + 1)})
        assert resp.status_code == 400
        assert vmc.maintenance_hold is None  # refused before the lease was touched

    def test_power_cycle_defaults_dwell_seconds_when_omitted(
        self, client, wired, wire_subsystem, wire_dispatcher
    ):
        wire_subsystem("ice_maker", ["power_cycle"])
        dispatcher = wire_dispatcher(FakeAckDispatcher())
        resp = client.post("/tests/ice_maker/power_cycle", data={})
        assert resp.status_code == 200
        assert dispatcher.calls == [
            ("ice_maker", "power_cycle", {"dwell_seconds": POWER_CYCLE_DWELL_DEFAULT})
        ]


# --- Simulated sale ------------------------------------------------------


class TestSimulatedSaleFlow:
    """GET /tests/sale renders a picker (never taking the lease, same rule
    as GET /tests and GET /tests/{subsystem} -- Task 13a's
    TestLeaseNotTaken); POST /tests/sale runs VMC.run_test_sale for the
    submitted SKU.

    A full real dispensed-to-completion run driven purely over HTTP is out
    of scope for this file: `wired`'s VMC is never `attach_to_loop()`d
    (VMC._schedule silently no-ops without one), and every existing
    real-FSM-dispense test in this codebase (tests/test_vmc_flows.py)
    drives the VMC directly for exactly that reason rather than through
    TestClient. test_sale_result_card_shows_path_and_verdict_form below
    instead proves the ROUTE's own rendering (the production code this
    file is actually responsible for) against a real TestSaleResult, via
    monkeypatching VMC.run_test_sale itself -- the FSM behavior behind
    that result is tests/test_vmc_flows.py's TestRunTestSale's job, not
    this file's.
    """

    def test_picker_never_takes_the_lease(self, client, wired):
        cfg, vmc, _inv, _store = wired
        add_product(cfg, "ICE-1", "Ice Bag", 2.50, slot=0)
        assert vmc.maintenance_hold is None
        resp = client.get("/tests/sale")
        assert resp.status_code == 200
        assert "ICE-1" in resp.text
        assert vmc.maintenance_hold is None

    def test_sku_with_slash_end_to_end(self, client, wired):
        """The SKU travels: <select><option value="..."> (GET) -> a POST
        form field -> FastAPI's Form(...) decoding -> the route's catalog
        lookup -> VMC.run_test_sale's own _find_product_by_sku -- all real
        production code, "/" intact at every hop. Locking the product out
        first makes run_test_sale fail SYNCHRONOUSLY (before ever awaiting
        the dispense-completion Future, which nothing in this fixture
        would ever resolve) with a RuntimeError that echoes the SKU back
        verbatim -- see run_test_sale's own "could not select" message.

        Mutation proof: changed tests_sale_run's catalog lookup from
        `p.sku == sku` to `p.sku == sku.replace("/", "-")` (simulating a
        bug that mangles a slashed SKU before comparing it). Result:
        test_sku_with_slash_end_to_end failed -- 404 ("No such product:
        ICE/COLD-1") instead of the expected 200 refusal card, since
        "ICE/COLD-1".replace("/", "-") == "ICE-COLD-1" matches no catalog
        entry. Restored the exact-match lookup -- passed again.
        """
        cfg, vmc, _inv, _store = wired
        add_product(cfg, "ICE/COLD-1", "Cold Ice", 2.50, slot=0)
        vmc._lockouts["ICE/COLD-1"] = FaultCode.ICE_101

        picker = client.get("/tests/sale")
        assert picker.status_code == 200
        assert "ICE/COLD-1" in picker.text

        resp = client.post("/tests/sale", data={"sku": "ICE/COLD-1"})
        assert resp.status_code == 200
        assert "ICE/COLD-1" in resp.text
        assert "could not select" in resp.text

    def test_unknown_sku_is_404(self, client, wired):
        resp = client.post("/tests/sale", data={"sku": "NOPE-1"})
        assert resp.status_code == 404

    def test_sale_result_card_shows_path_and_verdict_form(
        self, client, wired, monkeypatch
    ):
        """Reaches tests_sale_run's success-path rendering
        (partials/test_sale_result.html) against a REAL TestSaleResult,
        with VMC.run_test_sale itself monkeypatched to return it instantly
        -- isolating the route's own rendering from FSM/dispense timing
        (see this class's docstring for why a real dispense isn't driven
        here).

        Mutation proof: changed test_sale_result.html's verdict form
        `hx-post` from `/tests/runs/{{ result.run_id }}/verdict` to a
        hardcoded `/tests/runs/unknown/verdict`. Result: this test failed
        (`hx-post="/tests/runs/fixed-run-id/verdict"` absent from
        resp.text). Restored the real `{{ result.run_id }}` -- passed
        again.
        """
        cfg, vmc, _inv, _store = wired
        add_product(cfg, "ICE-1", "Ice Bag", 2.50, slot=0)

        from controller.vmc import TestSaleResult

        async def fake_run_test_sale(sku, *, user_id=None, user_name=None):
            assert sku == "ICE-1"
            return TestSaleResult(
                sku=sku,
                path=["idle", "interacting_with_user", "dispensing", "idle"],
                outcome="dispensed",
                fault_code=None,
                run_id="fixed-run-id",
            )

        monkeypatch.setattr(vmc, "run_test_sale", fake_run_test_sale)
        resp = client.post("/tests/sale", data={"sku": "ICE-1"})
        assert resp.status_code == 200
        assert "dispensed" in resp.text
        assert "idle" in resp.text and "dispensing" in resp.text
        assert 'hx-post="/tests/runs/fixed-run-id/verdict"' in resp.text
