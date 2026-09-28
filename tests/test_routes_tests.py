"""Tests for the Tests level's discovery routes (Task 13a): GET /tests,
GET /tests/{subsystem} and GET /tests/log.

Fixtures come from tests/conftest.py: `wired` seeds a ConfigModel, VMC,
InventoryManager and AccessStore (owner "Ada"); `login_as`/`client` sign in
as a given role. Every test that wires a HealthMonitor or EventRecorder
restores module state to None in a `finally`/fixture teardown, matching the
pattern used throughout tests/test_routes_health.py and
tests/test_routes_reports.py, since both are plain module globals in
web_interface.context shared across tests.

Task 13b (the action routes: run, verdict, run-all, sale, end, takeover)
is not built yet, so every button/form this module's templates render for
those routes is a 404 today -- that is expected and out of this file's
scope; see .superpowers/sdd/task-13a-report.md.
"""

import pytest

from contracts.common import COMMAND_PARAM_VALIDATORS
from services.access import ROLE_PERMISSIONS, Permission, Role
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
