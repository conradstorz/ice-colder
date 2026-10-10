"""Tests for the six Health levels (Task 6): /health, /health/subsystems,
/health/subsystems/{name}, /health/faults (+ its clear flow),
/health/availability and /health/logs.

Fixtures come from tests/conftest.py: `wired` seeds a ConfigModel, VMC,
InventoryManager and AccessStore (owner "Ada", PIN 1379, setup finalized);
`login_as`/`client` sign in as a given role. Every test that wires a
HealthMonitor or Availability restores module state to None in a `finally`,
matching the pattern already used throughout tests/test_web_routes.py, since
both are plain module globals in web_interface.context shared across tests.
"""

import re

from contracts.vending_machine import FaultCode
from services.access import ROLE_PERMISSIONS, Permission, Role
from services.config_store import add_product
from web_interface import context
from web_interface import routes
from web_interface.routes.health import _dom_safe_key, _fault_gate

import pytest


def _seed_product(cfg, sku: str = "ICE-1") -> str:
    """A product-scope fault needs a real product on the config (a fresh
    ConfigModel() ships with none) -- mirrors legacy.py's own add_product
    usage rather than going through an HTTP round trip."""
    add_product(cfg, sku, "Ice", 2.5)
    return sku


# --- Permission gates for the five non-parameterized GET levels -----------

_HEALTH_GET_ROUTES = [
    ("/health", Permission.view_status),
    ("/health/subsystems", Permission.view_status),
    ("/health/faults", Permission.view_status),
    ("/health/availability", Permission.view_status),
    ("/health/logs", Permission.view_logs),
]


class TestLevelPermissionGates:
    """Each of the six levels returns 200 for a role holding its gate and
    403 for one that does not. view_status is held by every role in
    ROLE_PERMISSIONS, so the only role/level pairs that can actually
    diverge to 403 involve /health/logs (view_logs) -- exercised here by
    parametrizing over every role, which covers "loader gets 403 on
    /health/logs, 200 on the other five" as one case of the matrix."""

    @pytest.mark.parametrize("role", list(Role))
    @pytest.mark.parametrize("path, permission", _HEALTH_GET_ROUTES)
    def test_matrix(self, login_as, path, permission, role):
        client = login_as(role)
        resp = client.get(path)
        if permission in ROLE_PERMISSIONS[role]:
            assert resp.status_code == 200, (role, path, resp.status_code)
        else:
            assert resp.status_code == 403, (role, path, resp.status_code)

    def test_loader_gets_403_on_logs_200_on_the_other_five(self, login_as):
        client = login_as(Role.loader)
        assert client.get("/health/logs").status_code == 403
        for path, _perm in _HEALTH_GET_ROUTES:
            if path == "/health/logs":
                continue
            assert client.get(path).status_code == 200, path

    def test_subsystem_detail_requires_view_status(self, login_as):
        client = login_as(Role.loader)
        assert client.get("/health/subsystems/vending").status_code == 200


class TestHealthLanding:
    def test_renders_logs_subtile_for_a_tech(self, client_as_tech):
        resp = client_as_tech.get("/health")
        assert resp.status_code == 200
        assert 'href="/health/logs"' in resp.text

    def test_omits_logs_subtile_for_a_loader(self, login_as):
        client = login_as(Role.loader)
        resp = client.get("/health")
        assert resp.status_code == 200
        assert 'href="/health/logs"' not in resp.text

    def test_shows_the_other_three_subtiles_for_every_role(self, client):
        resp = client.get("/health")
        for url in ("/health/subsystems", "/health/faults", "/health/availability"):
            assert f'href="{url}"' in resp.text

    def test_renders_with_nothing_wired(self, client):
        """No health_monitor is set by the `wired` fixture, and this test
        sets no Availability either -- the landing page must still answer
        200, not 500 (rule 3)."""
        assert context.health_monitor is None
        assert context.availability is None
        resp = client.get("/health")
        assert resp.status_code == 200


class TestSubsystemsLevel:
    def test_lists_every_expected_subsystem_with_no_heartbeats(self, client):
        assert context.health_monitor is None
        resp = client.get("/health/subsystems")
        assert resp.status_code == 200
        for name in ("vending", "mdb", "ice_maker"):
            assert name in resp.text
        assert resp.text.count("Never seen") >= 3

    def test_lists_every_expected_subsystem_when_monitor_wired_but_silent(self, client):
        from services.health_monitor import HealthMonitor

        hm = HealthMonitor()
        routes.set_health_monitor(hm)
        try:
            resp = client.get("/health/subsystems")
            for name in ("vending", "mdb", "ice_maker"):
                assert name in resp.text
        finally:
            routes.set_health_monitor(None)

    def test_links_to_the_subsystem_level(self, client):
        resp = client.get("/health/subsystems")
        assert 'href="/health/subsystems/vending"' in resp.text


class TestSubsystemDetailLevel:
    def test_unknown_subsystem_is_a_shell_404(self, client):
        resp = client.get("/health/subsystems/nope")
        assert resp.status_code == 404
        assert 'id="bar"' in resp.text
        assert "Back" in resp.text

    def test_identity_fields_render(self, client):
        from services.health_monitor import HealthMonitor

        hm = HealthMonitor()
        hm.record_heartbeat("mdb", {"subsystem": "mdb", "uptime_seconds": 5})
        hm.record_capabilities(
            "mdb",
            {
                "subsystem": "mdb",
                "firmware": "abc1234",
                "contract_version": "0.3.0",
                "brand": "ice-colder",
                "model": "mdb-sim",
                "hardware_id": "02:11:22:33:44:55",
                "ip": "172.18.0.7",
                "commands": ["refund"],
            },
        )
        routes.set_health_monitor(hm)
        try:
            resp = client.get("/health/subsystems/mdb")
            assert resp.status_code == 200
            assert "abc1234" in resp.text
            assert "0.3.0" in resp.text
            assert "ice-colder mdb-sim" in resp.text
            assert "02:11:22:33:44:55" in resp.text
            assert "172.18.0.7" in resp.text
        finally:
            routes.set_health_monitor(None)

    def test_breadcrumb_is_home_health_subsystems_name(self, client):
        resp = client.get("/health/subsystems/vending")
        assert ">Home<" in resp.text or 'href="/"' in resp.text
        assert ">Health<" in resp.text or 'href="/health"' in resp.text
        assert ">vending<" in resp.text

    def test_temperature_attributed_only_to_declaring_board(self, client):
        """Task 7 brief, test (a): a vending-declared channel never appears
        on the ice-maker page and vice versa -- the whole point of the
        board's own capabilities document being the only source of what
        its window shows (spec §3)."""
        from services.health_monitor import HealthMonitor

        hm = HealthMonitor()
        hm.record_capabilities(
            "vending",
            {
                "channels": [
                    {
                        "channel_id": "cabinet",
                        "kind": "temperature",
                        "unit": "C",
                        "description": "Cabinet temperature",
                        "direction": "input",
                        "driven_by": None,
                    }
                ],
                "commands": ["ping", "self_test", "force_report"],
            },
        )
        hm.record_capabilities(
            "ice_maker",
            {
                "channels": [
                    {
                        "channel_id": "evaporator",
                        "kind": "temperature",
                        "unit": "C",
                        "description": "Evaporator temperature",
                        "direction": "input",
                        "driven_by": None,
                    }
                ],
                "commands": ["ping", "self_test", "force_report"],
            },
        )
        hm.record_heartbeat("vending")
        hm.record_heartbeat("ice_maker")
        hm.record_temperature("cabinet", 5.0)
        hm.record_temperature("evaporator", -10.0)
        routes.set_health_monitor(hm)
        try:
            vending_resp = client.get("/health/subsystems/vending")
            assert "cabinet" in vending_resp.text
            assert "evaporator" not in vending_resp.text

            ice_resp = client.get("/health/subsystems/ice_maker")
            assert "evaporator" in ice_resp.text
            assert "cabinet" not in ice_resp.text
        finally:
            routes.set_health_monitor(None)

    def test_digital_signal_with_text_shows_label_text_and_red(self, client):
        """Task 7 brief, test (b): an MDB device declares a binary channel
        whose readiness word is carried as `text`; off (value 0.0) renders
        the red fill."""
        from services.health_monitor import HealthMonitor

        hm = HealthMonitor()
        hm.record_capabilities(
            "mdb",
            {
                "channels": [
                    {
                        "channel_id": "card_reader",
                        "kind": "binary",
                        "unit": "",
                        "description": "",
                        "direction": "input",
                        "driven_by": "payment/enable",
                    }
                ],
                "commands": ["ping", "self_test", "force_report"],
            },
        )
        hm.record_heartbeat("mdb")
        hm.record_signal("mdb", "card_reader", 0.0, text="error")
        routes.set_health_monitor(hm)
        try:
            resp = client.get("/health/subsystems/mdb")
            assert "card_reader" in resp.text
            assert "card reader" in resp.text  # label: underscores spaced
            assert "error" in resp.text
            assert "bg-red-600" in resp.text
        finally:
            routes.set_health_monitor(None)

    def test_never_seen_board_shows_gray_not_green(self, client):
        """Task 7 brief, test (c): a signal recorded with no heartbeat ever
        is not alive -- state must be "none" (gray), never rendered as if
        live, even though a reading exists."""
        from services.health_monitor import HealthMonitor

        hm = HealthMonitor()
        hm.record_capabilities(
            "vending",
            {
                "channels": [
                    {
                        "channel_id": "bag_full_sensor",
                        "kind": "binary",
                        "unit": "",
                        "description": "",
                        "direction": "input",
                        "driven_by": None,
                    }
                ],
                "commands": ["ping", "self_test", "force_report"],
            },
        )
        hm.record_signal("vending", "bag_full_sensor", 1.0)
        routes.set_health_monitor(hm)
        try:
            resp = client.get("/health/subsystems/vending")
            assert "bg-gray-200" in resp.text
            assert "bg-green-600" not in resp.text
        finally:
            routes.set_health_monitor(None)

    def test_inhibited_output_marked_fan_is_not(self, client):
        """Task 7 brief, test (d): with Availability wired and vending's
        heartbeat lost, dispense is inhibited -- an output driven by
        dispense renders ring-dashed and the word "inhibited"; a fan row
        with no driven_by (autonomous) does not."""
        from services.availability import Availability
        from services.health_monitor import HealthMonitor

        hm = HealthMonitor()
        hm.record_capabilities(
            "vending",
            {
                "channels": [
                    {
                        "channel_id": "auger_motor",
                        "kind": "binary",
                        "unit": "",
                        "description": "",
                        "direction": "output",
                        "driven_by": "dispense",
                    },
                    {
                        "channel_id": "fan",
                        "kind": "binary",
                        "unit": "",
                        "description": "",
                        "direction": "output",
                        "driven_by": None,
                    },
                ],
                "commands": ["ping", "self_test", "force_report", "dispense"],
            },
        )
        hm.record_heartbeat("vending")
        hm.record_signal("vending", "auger_motor", 0.0)
        hm.record_signal("vending", "fan", 1.0)
        routes.set_health_monitor(hm)

        avail = Availability()
        avail.set_subsystem_alive("vending", False)
        routes.set_availability(avail)
        try:
            resp = client.get("/health/subsystems/vending")
            text = resp.text
            assert "ring-dashed" in text
            assert "inhibited" in text

            def _row(signal_id: str) -> str:
                start = text.index(f'id="signal-{signal_id}"')
                end = text.index("</div>", start)
                return text[start:end]

            assert "inhibited" in _row("auger_motor")
            assert "inhibited" not in _row("fan")
        finally:
            routes.set_health_monitor(None)

    def test_stale_board_shows_gray_not_last_live_reading(self, client):
        """Copilot review (PR 27, finding 1): a board that heartbeat-timed-out
        (alive stays True, stale goes True -- see
        tests/test_health_monitor.py's TestCapabilitiesOnlyLiveness/stale
        tests for the mechanism) must render its cached signal as gray, not
        as the green/red it last reported."""
        from services.health_monitor import HealthMonitor

        hm = HealthMonitor(subsystem_timeout=-1.0)
        hm.record_capabilities(
            "mdb",
            {
                "channels": [
                    {
                        "channel_id": "card_reader",
                        "kind": "binary",
                        "unit": "",
                        "description": "",
                        "direction": "input",
                        "driven_by": None,
                    }
                ],
                "commands": ["ping", "self_test", "force_report"],
            },
        )
        hm.record_heartbeat("mdb")
        hm.record_signal("mdb", "card_reader", 1.0)
        assert hm.get_summary()["subsystems"]["mdb"]["alive"] is True
        assert hm.get_summary()["subsystems"]["mdb"]["stale"] is True
        routes.set_health_monitor(hm)
        try:
            resp = client.get("/health/subsystems/mdb")
            assert resp.status_code == 200
            assert "bg-gray-200" in resp.text
            assert "bg-green-600" not in resp.text
            assert "bg-red-600" not in resp.text
        finally:
            routes.set_health_monitor(None)

    def test_updated_clock_is_os_local_time_string(self, client):
        """Copilot review (PR 27, finding 2): the route must pass tz=None
        (OS-local, DST-aware per timestamp) rather than a fixed offset --
        checked here only as a well-formed local clock string, with no
        assumption about which offset is in effect."""
        from services.health_monitor import HealthMonitor

        hm = HealthMonitor()
        hm.record_capabilities(
            "vending",
            {
                "channels": [
                    {
                        "channel_id": "bag_full_sensor",
                        "kind": "binary",
                        "unit": "",
                        "description": "",
                        "direction": "input",
                        "driven_by": None,
                    }
                ],
                "commands": ["ping", "self_test", "force_report"],
            },
        )
        hm.record_heartbeat("vending")
        hm.record_signal("vending", "bag_full_sensor", 1.0)
        routes.set_health_monitor(hm)
        try:
            resp = client.get("/health/subsystems/vending")
            assert resp.status_code == 200
            assert re.search(r"\d\d:\d\d:\d\d", resp.text)
        finally:
            routes.set_health_monitor(None)

    def test_non_string_channel_id_does_not_500(self, client):
        """Copilot review (PR 27, finding 3): a schema-invalid capabilities
        payload with a non-string channel_id must be treated as malformed
        (same fail-safe path as a non-dict entry), not reach
        signals.get(channel_id) and raise TypeError."""
        from services.health_monitor import HealthMonitor

        hm = HealthMonitor()
        hm.record_capabilities(
            "mdb", {"channels": [{"channel_id": ["x"], "kind": "binary"}]}
        )
        routes.set_health_monitor(hm)
        try:
            resp = client.get("/health/subsystems/mdb")
            assert resp.status_code == 200
        finally:
            routes.set_health_monitor(None)
            routes.set_availability(None)

    def test_live_wrapper_hx_attributes_and_single_extra_trigger(self, client):
        """Task 7 brief, test (e): the full page carries exactly one
        hx-trigger besides the pill's (i.e. exactly 2 total), and #live
        self-targets."""
        from tests.dom_utils import find_by_id, parse_elements

        resp = client.get("/health/subsystems/vending")
        elements = parse_elements(resp.text)
        triggers = [e for e in elements if "hx-trigger" in e.attrs]
        assert len(triggers) == 2

        live = find_by_id(elements, "live")
        assert len(live) == 1
        assert live[0].attrs["hx-get"] == "/health/subsystems/vending/live"
        assert live[0].attrs["hx-trigger"] == "load, every 2s"
        assert live[0].attrs["hx-swap"] == "innerHTML"
        assert live[0].attrs["hx-target"] == "this"


class TestSubsystemLiveFragment:
    """Task 7 brief: GET /health/subsystems/{name}/live renders only the
    partial, with no hx- attribute of its own (base.html's #live wrapper
    owns every htmx attribute)."""

    @pytest.mark.parametrize("name", ["vending", "mdb", "ice_maker"])
    def test_known_name_is_200_with_no_hx_attribute(self, client, name):
        resp = client.get(f"/health/subsystems/{name}/live")
        assert resp.status_code == 200
        assert "hx-" not in resp.text

    def test_unknown_name_is_404(self, client):
        resp = client.get("/health/subsystems/nope/live")
        assert resp.status_code == 404

    def test_renders_three_section_headings(self, client):
        resp = client.get("/health/subsystems/vending/live")
        assert "Inputs" in resp.text
        assert "Outputs" in resp.text
        assert "Controls" in resp.text

    def test_requires_view_status(self, login_as):
        from services.access import Role

        client = login_as(Role.loader)
        resp = client.get("/health/subsystems/vending/live")
        assert resp.status_code == 200


class TestFaultsLevel:
    def test_breadcrumb_reads_home_health_faults(self, client):
        resp = client.get("/health/faults")
        assert resp.status_code == 200
        text = resp.text
        home_i = text.index(">Home<")
        health_i = text.index(">Health<")
        faults_i = text.index(">Faults<")
        assert home_i < health_i < faults_i

    def test_no_active_faults_says_so(self, client):
        resp = client.get("/health/faults")
        assert "No active faults" in resp.text

    def test_a_raised_fault_appears_with_age_and_gate(self, wired, client):
        _cfg, vmc, _inv, _store = wired
        from services.health_monitor import HealthMonitor

        hm = HealthMonitor()
        routes.set_health_monitor(hm)
        context.machine_instance.set_health_monitor(hm)
        try:
            vmc.raise_fault(FaultCode.WTR_104, outcome="leak")
            resp = client.get("/health/faults")
            assert resp.status_code == 200
            assert "WTR-104" in resp.text
            assert "safety" in resp.text
            assert "s ago" in resp.text or "m ago" in resp.text
        finally:
            routes.set_health_monitor(None)

    def test_renders_without_a_health_monitor(self, wired, client):
        """Fault age is None with no monitor wired -- the row must still
        render, without an age (executor resolution 6), never a 500."""
        _cfg, vmc, _inv, _store = wired
        assert context.health_monitor is None
        vmc.raise_fault(FaultCode.PAY_103, outcome="test")
        resp = client.get("/health/faults")
        assert resp.status_code == 200
        assert "PAY-103" in resp.text

    def test_renders_with_no_vmc(self, wired, client):
        _machine = context.machine_instance
        routes.set_machine_instance(None)
        try:
            resp = client.get("/health/faults")
            assert resp.status_code == 200
            assert "No active faults" in resp.text
        finally:
            routes.set_machine_instance(_machine)

    def test_clear_button_absent_without_clear_faults(self, wired, login_as):
        _cfg, vmc, _inv, _store = wired
        vmc.raise_fault(FaultCode.PAY_103, outcome="test")
        client = login_as(Role.loader)
        resp = client.get("/health/faults")
        assert "PAY-103" in resp.text
        assert 'id="clear-PAY-103"' not in resp.text

    def test_clear_button_present_with_clear_faults(self, wired, login_as):
        _cfg, vmc, _inv, _store = wired
        vmc.raise_fault(FaultCode.PAY_103, outcome="test")
        client = login_as(Role.tech)
        resp = client.get("/health/faults")
        assert 'id="clear-PAY-103"' in resp.text


class TestFaultGateHelper:
    """Executor resolution 1: test all three branches of the derived
    fault -> gate mapping directly, since it is a pure function."""

    def test_payment_blocking_code_is_safety(self):
        fault = {"code": "WTR-104", "scope": "machine"}
        assert _fault_gate(fault) == "safety"

    def test_product_scope_non_blocking_is_fulfillment(self):
        fault = {"code": "ICE-301", "scope": "product"}
        assert _fault_gate(fault) == "fulfillment"

    def test_machine_scope_non_blocking_is_alert(self):
        fault = {"code": "COM-103", "scope": "machine"}
        assert _fault_gate(fault) == "alert"


class TestFaultClearFlow:
    def test_clear_confirm_then_post_clears_for_a_tech(self, wired, login_as):
        _cfg, vmc, _inv, _store = wired
        key = _seed_product(_cfg)
        vmc.raise_fault(FaultCode.ICE_301, sku=key)
        client = login_as(Role.tech)

        confirm_resp = client.get(f"/health/faults/{key}/clear/confirm")
        assert confirm_resp.status_code == 200
        assert "Confirm" in confirm_resp.text

        cancel_resp = client.get(
            f"/health/faults/{key}/clear/confirm", params={"confirming": "false"}
        )
        assert cancel_resp.status_code == 200
        assert "Confirm" not in cancel_resp.text

        clear_resp = client.post(f"/health/faults/{key}/clear")
        assert clear_resp.status_code == 200
        assert key not in clear_resp.text
        assert vmc.active_faults() == []

    def test_post_clear_403_for_a_loader(self, wired, login_as):
        _cfg, vmc, _inv, _store = wired
        key = _seed_product(_cfg)
        vmc.raise_fault(FaultCode.ICE_301, sku=key)
        client = login_as(Role.loader)
        resp = client.post(f"/health/faults/{key}/clear")
        assert resp.status_code == 403

    def test_product_scoped_fault_with_dotted_sku_gets_a_selector_safe_dom_id(
        self, wired, login_as
    ):
        """Copilot review (PR 20, comment 4113241371): f.key is the SKU for
        a product-scoped fault, and SKUs are free text -- a SKU containing
        "." used to be dropped straight into "#clear-<sku>", an unescaped
        CSS id selector where "." is a class-selector delimiter, so
        htmx's hx-target could resolve to the wrong element (or none) and
        the two-tap Clear control could not reliably swap its own
        confirmation state. The rendered id/hx-target must now be built
        from a selector-safe key, round-tripping identically across the
        list, confirm and cancel renders, while the raw SKU stays in the
        endpoint URLs (post_url/confirm_url)."""
        _cfg, vmc, _inv, _store = wired
        key = _seed_product(_cfg, "ICE.301")
        vmc.raise_fault(FaultCode.ICE_301, sku=key)
        client = login_as(Role.tech)

        list_resp = client.get("/health/faults")
        assert list_resp.status_code == 200
        # The old, unsafe id must be gone...
        assert f'id="clear-{key}"' not in list_resp.text
        # ...but the raw SKU must still be exactly what the endpoint URLs use.
        assert f"/health/faults/{key}/clear" in list_resp.text
        assert f"/health/faults/{key}/clear/confirm" in list_resp.text

        dom_id = f"clear-{_dom_safe_key(key)}"
        assert re.fullmatch(r"clear-[A-Za-z0-9_-]+", dom_id)
        assert f'id="{dom_id}"' in list_resp.text
        assert f'hx-target="#{dom_id}"' in list_resp.text

        confirm_resp = client.get(f"/health/faults/{key}/clear/confirm")
        assert confirm_resp.status_code == 200
        assert f'id="{dom_id}"' in confirm_resp.text
        assert f'hx-target="#{dom_id}"' in confirm_resp.text

        cancel_resp = client.get(
            f"/health/faults/{key}/clear/confirm", params={"confirming": "false"}
        )
        assert cancel_resp.status_code == 200
        assert f'id="{dom_id}"' in cancel_resp.text

    def test_dom_safe_key_preserves_already_safe_keys(self):
        """Machine FaultCode.value keys (e.g. "PAY-103") are always drawn
        from a fixed selector-safe charset and must pass through unchanged
        -- existing tests assert on the literal id "clear-PAY-103"."""
        assert _dom_safe_key("PAY-103") == "PAY-103"

    def test_dom_safe_key_is_stable_and_collision_free_for_unsafe_keys(self):
        a = _dom_safe_key("ICE.301")
        b = _dom_safe_key("ICE.301")
        c = _dom_safe_key("ICE_301")
        assert a == b  # deterministic
        assert re.fullmatch(r"[A-Za-z0-9_-]+", a)
        assert a != c  # a dotted SKU never collides with a similar safe one

    def test_post_clear_without_htmx_header_is_403(self, wired, login_as):
        _cfg, vmc, _inv, _store = wired
        key = _seed_product(_cfg)
        vmc.raise_fault(FaultCode.ICE_301, sku=key)
        client = login_as(Role.tech)
        resp = client.post(f"/health/faults/{key}/clear", headers={"HX-Request": ""})
        assert resp.status_code == 403

    def test_post_clear_unknown_key_is_404(self, login_as):
        client = login_as(Role.tech)
        resp = client.post("/health/faults/NOPE/clear")
        assert resp.status_code == 404

    def test_post_clear_svc_102_rejected_while_maintenance_lease_held(
        self, wired, login_as
    ):
        """Copilot review (PR 22, id=4128088457): the generic Health >
        Faults clear route must not be able to clear SVC-102 out from
        under a live maintenance lease -- sibling of the PAY-104 guard
        above. A direct POST (bypassing the Tests level UI entirely) gets
        409, the fault stays active, and the lease is untouched."""
        _cfg, vmc, _inv, _store = wired
        granted, _reason = context.machine_instance.lease.begin_maintenance(
            "user-1", "sess-1"
        )
        assert granted is True
        client = login_as(Role.tech)

        resp = client.post(f"/health/faults/{FaultCode.SVC_102.value}/clear")

        assert resp.status_code == 409
        assert FaultCode.SVC_102.value in [f["code"] for f in vmc.active_faults()]
        assert context.machine_instance.maintenance_hold is not None
        assert context.machine_instance.maintenance_hold.holder_session_id == "sess-1"

        # The real release path still works afterwards.
        assert context.machine_instance.lease.end_maintenance("sess-1") is True
        assert FaultCode.SVC_102.value not in [f["code"] for f in vmc.active_faults()]

    def test_confirm_endpoint_requires_clear_faults(self, wired, login_as):
        _cfg, vmc, _inv, _store = wired
        key = _seed_product(_cfg)
        vmc.raise_fault(FaultCode.ICE_301, sku=key)
        client = login_as(Role.loader)
        resp = client.get(f"/health/faults/{key}/clear/confirm")
        assert resp.status_code == 403


class TestAvailabilityLevel:
    def test_renders_with_no_availability_wired(self, client):
        assert context.availability is None
        resp = client.get("/health/availability")
        assert resp.status_code == 200
        assert "unknown" in resp.text.lower()

    def test_lists_permissives_with_not_instrumented(self, client):
        from services.availability import Availability

        avail = Availability()
        routes.set_availability(avail)
        try:
            resp = client.get("/health/availability")
            assert "bag_present" in resp.text
            assert "not instrumented" in resp.text
            assert "vending_alive" in resp.text
        finally:
            routes.set_availability(None)

    def test_shows_payment_enabled_and_per_kind(self, client):
        from services.availability import Availability

        avail = Availability()
        routes.set_availability(avail)
        try:
            resp = client.get("/health/availability")
            assert "Payment enabled" in resp.text
            assert "Ice" in resp.text
            assert "Water" in resp.text
        finally:
            routes.set_availability(None)


class TestLogsLevel:
    def test_shows_a_written_line(self, tmp_path, monkeypatch, client_as_tech):
        log_file = tmp_path / "LOGS" / "vmc.log"
        log_file.parent.mkdir()
        log_file.write_text(
            "first line\nunique-marker-77;INFO 2026-09-21\n", encoding="utf-8"
        )
        monkeypatch.setattr(context, "LOG_PATH", log_file)

        resp = client_as_tech.get("/health/logs")
        assert resp.status_code == 200
        assert "unique-marker-77" in resp.text

    def test_carries_no_polling_attribute(self, client_as_tech):
        """The bar's pill (outside `main`, present on every level) carries
        its own hx-trigger="load, every 5s" -- that one hit is expected.
        This level's own content must add none of its own (executor
        resolution 7): exactly one hx-trigger in the whole response, and
        it must be the pill's."""
        resp = client_as_tech.get("/health/logs")
        assert resp.text.count("hx-trigger") == 1
        assert 'id="pill" hx-get="/pill" hx-trigger="load, every 5s"' in resp.text

    def test_has_a_refresh_button(self, client_as_tech):
        resp = client_as_tech.get("/health/logs")
        assert 'hx-get="/health/logs"' in resp.text


@pytest.fixture
def client_as_tech(login_as):
    return login_as(Role.tech)
