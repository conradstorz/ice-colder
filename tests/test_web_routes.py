"""Tests for web_interface routes using FastAPI TestClient."""

import pytest
from fastapi.testclient import TestClient
from config.config_model import ConfigModel
from contracts.vending_machine import FaultCode
from controller.vmc import VMC
from services.inventory_manager import InventoryManager
from web_interface.server import app
from web_interface import routes


@pytest.fixture
def client(tmp_path):
    """Create a TestClient with a real ConfigModel, VMC, and InventoryManager."""
    cfg = ConfigModel()
    vmc = VMC(config=cfg)
    inv = InventoryManager([], path=tmp_path / "inventory.json")
    routes.set_config_object(cfg)
    routes.set_vmc_instance(vmc)
    routes.set_inventory_manager(inv)

    with TestClient(app) as c:
        c.auth = ("admin", "changeme")
        c.headers["HX-Request"] = "true"
        yield c

        for t in vmc._pending_tasks:
            t.cancel()


class TestDashboard:
    def test_dashboard_returns_html(self, client):
        resp = client.get("/")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]

    def test_dashboard_contains_title(self, client):
        resp = client.get("/")
        assert "Vending Machine" in resp.text


class TestStatusEndpoint:
    def test_status_returns_html(self, client):
        resp = client.get("/status")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]


class TestInventoryEndpoints:
    def test_inventory_list(self, client):
        resp = client.get("/inventory")
        assert resp.status_code == 200

    def test_inventory_new_form(self, client):
        resp = client.get("/inventory/new")
        assert resp.status_code == 200

    def test_add_product(self, client):
        resp = client.post(
            "/inventory/add",
            data={
                "sku": "TEST-001",
                "name": "Test Ice",
                "price": "2.50",
            },
        )
        assert resp.status_code == 200
        assert "Test Ice" in resp.text

    def test_edit_form(self, client):
        """Edit form for a product created via the dashboard."""
        client.post(
            "/inventory/add",
            data={"sku": "EDIT-1", "name": "Editable", "price": "1.50"},
        )
        resp = client.get("/inventory/edit/EDIT-1")
        assert resp.status_code == 200
        assert "Editable" in resp.text

    def test_add_product_without_slot_auto_assigns(self, client):
        resp = client.post(
            "/inventory/add",
            data={"sku": "AUTO-1", "name": "Auto Slot", "price": "1.00"},
        )
        assert resp.status_code == 200
        added = next(p for p in routes.config.products if p.sku == "AUTO-1")
        assert added.slot == 0  # first product added to an empty catalog

    def test_add_product_with_explicit_slot(self, client):
        resp = client.post(
            "/inventory/add",
            data={"sku": "SLOT-1", "name": "Slotted", "price": "1.00", "slot": "7"},
        )
        assert resp.status_code == 200
        added = next(p for p in routes.config.products if p.sku == "SLOT-1")
        assert added.slot == 7

    def test_add_product_with_negative_slot_does_not_500(self, client):
        resp = client.post(
            "/inventory/add",
            data={
                "sku": "NEG-1",
                "name": "Negative Slot",
                "price": "1.00",
                "slot": "-1",
            },
        )
        assert resp.status_code == 200
        assert not any(p.sku == "NEG-1" for p in routes.config.products)

    def test_inventory_table_renders_slot_column(self, client):
        client.post(
            "/inventory/add",
            data={"sku": "SLOT-2", "name": "Slotted Two", "price": "1.00", "slot": "3"},
        )
        resp = client.get("/inventory")
        assert resp.status_code == 200
        assert "3" in resp.text

    def test_edit_form_shows_slot_input(self, client):
        client.post(
            "/inventory/add",
            data={
                "sku": "EDIT-2",
                "name": "Editable Two",
                "price": "1.50",
                "slot": "9",
            },
        )
        resp = client.get("/inventory/edit/EDIT-2")
        assert resp.status_code == 200
        assert 'name="slot"' in resp.text
        assert 'value="9"' in resp.text

    def test_update_product_changes_slot(self, client):
        client.post(
            "/inventory/add",
            data={"sku": "UPD-1", "name": "Updatable", "price": "1.50", "slot": "1"},
        )
        resp = client.post(
            "/inventory/update/UPD-1",
            data={"name": "Updatable", "price": "1.50", "slot": "6"},
        )
        assert resp.status_code == 200
        updated = next(p for p in routes.config.products if p.sku == "UPD-1")
        assert updated.slot == 6

    def test_add_product_carries_kind(self, client):
        resp = client.post(
            "/inventory/add",
            data={
                "sku": "KIND-1",
                "name": "Water Bottle",
                "price": "1.25",
                "kind": "water",
            },
        )
        assert resp.status_code == 200
        assert routes.config.products[-1].kind == "water"

    def test_update_product_changes_kind(self, client):
        client.post(
            "/inventory/add",
            data={"sku": "KIND-2", "name": "Flexible", "price": "1.00"},
        )
        resp = client.post(
            "/inventory/update/KIND-2",
            data={"name": "Flexible", "price": "1.00", "slot": "0", "kind": "ice"},
        )
        assert resp.status_code == 200
        updated = next(p for p in routes.config.products if p.sku == "KIND-2")
        assert updated.kind == "ice"

    def test_copy_form_preselects_source_product_kind(self, client):
        client.post(
            "/inventory/add",
            data={
                "sku": "KIND-3",
                "name": "Sparkling Water",
                "price": "1.50",
                "kind": "water",
            },
        )
        resp = client.get("/inventory/copy/KIND-3")
        assert resp.status_code == 200
        assert 'value="water" selected' in resp.text


class TestConfigEndpoints:
    def test_machine_info(self, client):
        resp = client.get("/config/machine")
        assert resp.status_code == 200

    def test_machine_info_shows_product_count(self, client):
        client.post(
            "/inventory/add",
            data={"sku": "CNT-1", "name": "Counted", "price": "1.00"},
        )
        resp = client.get("/config/machine")
        assert resp.status_code == 200
        assert ">1</dd>" in resp.text.replace(" ", "").replace("\n", "")

    @pytest.mark.skip(reason="Template partials/contacts.html not yet created")
    def test_contacts(self, client):
        resp = client.get("/config/contacts")
        assert resp.status_code == 200

    @pytest.mark.skip(reason="Template partials/payments.html not yet created")
    def test_payments(self, client):
        resp = client.get("/config/payments")
        assert resp.status_code == 200

    @pytest.mark.skip(reason="Template partials/comms.html not yet created")
    def test_comms(self, client):
        resp = client.get("/config/comms")
        assert resp.status_code == 200


class TestActionEndpoint:
    def test_restart_action(self, client):
        resp = client.post("/action/restart")
        assert resp.status_code == 200
        assert "Restart" in resp.text

    def test_unknown_action(self, client):
        resp = client.post("/action/foobar")
        assert resp.status_code == 200
        assert "Unknown" in resp.text

    def test_reset_action_recovers_from_error(self, client):
        from web_interface import routes as r

        r.vmc_instance.error_occurred()
        assert r.vmc_instance.state == "error"
        resp = client.post("/action/reset")
        assert resp.status_code == 200
        assert "Reset complete" in resp.text
        assert r.vmc_instance.state == "idle"


class TestLogsEndpoint:
    def test_logs_returns_html(self, client):
        resp = client.get("/logs")
        assert resp.status_code == 200


class TestActivityEndpoint:
    def test_activity_returns_200(self, client):
        response = client.get("/activity")
        assert response.status_code == 200

    def test_activity_without_recorder_returns_fallback(self, client):
        response = client.get("/activity")
        assert response.status_code == 200


class TestKpiEndpoint:
    def test_kpi_returns_200(self, client):
        response = client.get("/kpi")
        assert response.status_code == 200

    def test_kpi_without_recorder_returns_placeholder(self, client):
        # No event_recorder set on the fixture — should return placeholder cards
        response = client.get("/kpi")
        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]

    def test_kpi_with_recorder(self, client, tmp_path):
        from services.event_recorder import EventRecorder
        from web_interface import routes as r

        recorder = EventRecorder(db_path=str(tmp_path / "test.db"))
        r.set_event_recorder(recorder)
        try:
            response = client.get("/kpi")
            assert response.status_code == 200
            # "no history yet" only appears in the recorder-present branch (average sub-line);
            # the placeholder skeleton uses "no data" instead.
            assert "no history yet" in response.text
        finally:
            r.set_event_recorder(None)


class TestActivityPeriodParam:
    def test_activity_default_period(self, client):
        response = client.get("/activity")
        assert response.status_code == 200

    def test_activity_period_168(self, client):
        response = client.get("/activity?period=168")
        assert response.status_code == 200

    def test_activity_period_720(self, client):
        response = client.get("/activity?period=720")
        assert response.status_code == 200

    def test_activity_invalid_period_falls_back_to_24(self, client):
        # Invalid period values should fall back to 24 without error
        response = client.get("/activity?period=99")
        assert response.status_code == 200


class TestEventRecorderCallsOffloaded:
    """/status, /kpi and /activity must never run sqlite-backed EventRecorder
    calls directly on the asyncio event loop — they belong on a worker
    thread via asyncio.to_thread so MQTT dispatch/FSM handling isn't
    stalled by a synchronous SELECT."""

    def test_status_offloads_get_summary_to_thread(self, client, tmp_path, monkeypatch):
        from services.event_recorder import EventRecorder
        from web_interface import routes as r

        recorder = EventRecorder(db_path=str(tmp_path / "test.db"))
        r.set_event_recorder(recorder)

        calls = []
        real_to_thread = r.asyncio.to_thread

        async def spying_to_thread(func, *args, **kwargs):
            calls.append((func, args))
            return await real_to_thread(func, *args, **kwargs)

        monkeypatch.setattr(r.asyncio, "to_thread", spying_to_thread)
        try:
            resp = client.get("/status")
            assert resp.status_code == 200
            assert (recorder.get_summary, (24,)) in calls
        finally:
            r.set_event_recorder(None)

    def test_kpi_offloads_summary_and_average_to_thread(
        self, client, tmp_path, monkeypatch
    ):
        from services.event_recorder import EventRecorder
        from web_interface import routes as r

        recorder = EventRecorder(db_path=str(tmp_path / "test.db"))
        r.set_event_recorder(recorder)

        calls = []
        real_to_thread = r.asyncio.to_thread

        async def spying_to_thread(func, *args, **kwargs):
            calls.append((func, args))
            return await real_to_thread(func, *args, **kwargs)

        monkeypatch.setattr(r.asyncio, "to_thread", spying_to_thread)
        try:
            resp = client.get("/kpi")
            assert resp.status_code == 200
            assert (recorder.get_summary, (24,)) in calls
            assert (recorder.get_historical_average, (24,)) in calls
        finally:
            r.set_event_recorder(None)

    def test_activity_offloads_summary_and_average_to_thread(
        self, client, tmp_path, monkeypatch
    ):
        from services.event_recorder import EventRecorder
        from web_interface import routes as r

        recorder = EventRecorder(db_path=str(tmp_path / "test.db"))
        r.set_event_recorder(recorder)

        calls = []
        real_to_thread = r.asyncio.to_thread

        async def spying_to_thread(func, *args, **kwargs):
            calls.append((func, args))
            return await real_to_thread(func, *args, **kwargs)

        monkeypatch.setattr(r.asyncio, "to_thread", spying_to_thread)
        try:
            resp = client.get("/activity?period=168")
            assert resp.status_code == 200
            assert (recorder.get_summary, (168,)) in calls
            assert (recorder.get_historical_average, (168,)) in calls
        finally:
            r.set_event_recorder(None)


class TestStatusHealthSignal:
    def test_status_is_healthy_without_recorder_or_monitor(self, client):
        # When neither event_recorder nor health_monitor is set, no issues → healthy
        response = client.get("/status")
        assert response.status_code == 200
        assert "All Systems OK" in response.text

    def test_status_with_recorder_no_errors(self, client, tmp_path):
        from services.event_recorder import EventRecorder
        from web_interface import routes as r

        recorder = EventRecorder(db_path=str(tmp_path / "test.db"))
        r.set_event_recorder(recorder)
        try:
            response = client.get("/status")
            assert response.status_code == 200
            assert "All Systems OK" in response.text
        finally:
            r.set_event_recorder(None)

    def test_status_with_recorder_has_errors(self, client, tmp_path):
        from services.event_recorder import EventRecorder
        from web_interface import routes as r

        recorder = EventRecorder(db_path=str(tmp_path / "test.db"))
        recorder.record("error")
        r.set_event_recorder(recorder)
        try:
            response = client.get("/status")
            assert response.status_code == 200
            assert "Issues Detected" in response.text
        finally:
            r.set_event_recorder(None)


class TestDeleteAndEmptyState:
    def test_empty_state_shown_when_no_products(self, client):
        resp = client.get("/inventory")
        assert resp.status_code == 200
        assert "No products configured" in resp.text

    def test_delete_product_removes_row(self, client):
        client.post(
            "/inventory/add",
            data={"sku": "DEL-1", "name": "Doomed", "price": "1.00"},
        )
        resp = client.post("/inventory/delete/DEL-1")
        assert resp.status_code == 200
        assert "Doomed" not in resp.text
        assert "No products configured" in resp.text

    def test_delete_unknown_sku_is_harmless(self, client):
        resp = client.post("/inventory/delete/NOPE")
        assert resp.status_code == 200

    def test_delete_requires_auth(self, client):
        resp = client.post("/inventory/delete/X", auth=None)
        assert resp.status_code == 401

    def test_add_registers_inventory_sku(self, client):
        from web_interface import routes as r

        client.post(
            "/inventory/add",
            data={"sku": "INV-1", "name": "Tracked Thing", "price": "1.00"},
        )
        assert "INV-1" in r.inventory_manager.get_all()

    def test_delete_removes_inventory_sku(self, client):
        from web_interface import routes as r

        client.post(
            "/inventory/add",
            data={"sku": "INV-2", "name": "Gone Soon", "price": "1.00"},
        )
        client.post("/inventory/delete/INV-2")
        assert "INV-2" not in r.inventory_manager.get_all()


class TestAuth:
    def test_unauthenticated_request_rejected(self, client):
        resp = client.get("/", auth=None)
        assert resp.status_code == 401

    def test_wrong_password_rejected(self, client):
        resp = client.get("/", auth=("admin", "wrong"))
        assert resp.status_code == 401

    def test_mutating_endpoint_requires_auth(self, client):
        resp = client.post("/action/reset", auth=None)
        assert resp.status_code == 401


class TestFaultsUI:
    def _lock(self, client):
        vmc = routes.vmc_instance
        vmc._raise_fault(FaultCode.ICE_301, sku=routes.config.products[0].sku)

    def _add_product(self, client):
        client.post(
            "/inventory/add",
            data={"sku": "ICE-1", "name": "Ice", "price": "2.5"},
            auth=client.auth,
        )

    def test_status_lists_active_fault_with_clear_button(self, client):
        self._add_product(client)
        self._lock(client)
        r = client.get("/status", auth=client.auth)
        assert r.status_code == 200
        assert "ICE-301" in r.text
        assert "Ice" in r.text
        assert 'hx-post="/faults/ICE-1/clear"' in r.text
        assert "Issues Detected" in r.text

    def test_status_shows_fault_age_when_health_monitor_set(self, client):
        from services.health_monitor import HealthMonitor

        hm = HealthMonitor()
        routes.set_health_monitor(hm)
        routes.vmc_instance.set_health_monitor(hm)
        try:
            self._add_product(client)
            self._lock(client)
            r = client.get("/status", auth=client.auth)
            assert r.status_code == 200
            assert "ICE-301" in r.text
            assert "s</span>" in r.text
        finally:
            routes.set_health_monitor(None)

    def test_status_still_renders_without_health_monitor(self, client):
        self._add_product(client)
        self._lock(client)
        try:
            r = client.get("/status", auth=client.auth)
            assert r.status_code == 200
            assert "ICE-301" in r.text
        finally:
            routes.set_health_monitor(None)

    def test_status_without_faults_says_none(self, client):
        r = client.get("/status", auth=client.auth)
        assert "No active faults" in r.text

    def test_status_banner_is_neutral_without_availability(self, client):
        """No Availability is attached on this fixture, so routes.py sets
        machine_stopped to None (payment state was never measured). Jinja
        treats None the same as False, so before this fix the banner fell
        into the "still selling" branch and asserted a payment state nobody
        actually checked. It must instead say neither "Machine Stopped" nor
        "still selling"."""
        self._add_product(client)
        self._lock(client)
        r = client.get("/status", auth=client.auth)
        assert "Issues Detected" in r.text
        assert "Machine Stopped" not in r.text
        assert "still selling" not in r.text

    def test_clear_endpoint_clears_and_rerenders(self, client):
        self._add_product(client)
        self._lock(client)
        r = client.post("/faults/ICE-1/clear", auth=client.auth)
        assert r.status_code == 200
        assert "ICE-301" not in r.text
        assert routes.vmc_instance.active_faults() == []

    def test_clear_unknown_key_returns_404(self, client):
        r = client.post("/faults/NOPE/clear", auth=client.auth)
        assert r.status_code == 404

    def test_inventory_table_shows_locked_badge(self, client):
        self._add_product(client)
        self._lock(client)
        r = client.get("/inventory", auth=client.auth)
        assert "locked" in r.text.lower()
        assert "ICE-301" in r.text

    def test_kpi_shows_failed_vends(self, client, tmp_path):
        from services.event_recorder import EventRecorder

        rec = EventRecorder(db_path=str(tmp_path / "events.db"))
        rec.record("vend_failed", value=2.5, metadata={"code": "ICE-301"})
        routes.set_event_recorder(rec)
        try:
            r = client.get("/kpi", auth=client.auth)
            assert "1 failed" in r.text
        finally:
            routes.set_event_recorder(None)

    def test_activity_shows_failed_vends_and_refunds(self, client, tmp_path):
        from services.event_recorder import EventRecorder

        rec = EventRecorder(db_path=str(tmp_path / "events.db"))
        rec.record("vend_failed", value=2.5)
        rec.record("refund", value=2.5)
        routes.set_event_recorder(rec)
        try:
            r = client.get("/activity", auth=client.auth)
            assert "Failed Vends" in r.text
            assert "Refunds Paid" in r.text
            assert "$2.50" in r.text
        finally:
            routes.set_event_recorder(None)


class TestHealthTabIdentity:
    def _hm(self):
        from services.health_monitor import HealthMonitor

        hm = HealthMonitor(machine_id="vmc-test")
        routes.set_health_monitor(hm)
        return hm

    def test_vmc_row(self, client):
        from services.build_info import BUILD_INFO

        self._hm()
        try:
            r = client.get("/health", auth=client.auth)
            assert r.status_code == 200
            assert BUILD_INFO.commit_short in r.text
            assert BUILD_INFO.source in r.text
            assert "vmc-test" in r.text
        finally:
            routes.set_health_monitor(None)

    def test_expected_subsystems_listed_when_silent(self, client):
        self._hm()
        try:
            r = client.get("/health", auth=client.auth)
            for name in ("vending", "mdb", "ice_maker"):
                assert name in r.text
            assert r.text.count("Never seen") >= 3
        finally:
            routes.set_health_monitor(None)

    def test_heartbeat_only_row_shows_dashes(self, client):
        hm = self._hm()
        try:
            hm.record_heartbeat(
                "vending", {"subsystem": "vending", "uptime_seconds": 90}
            )
            r = client.get("/health", auth=client.auth)
            assert "1m" in r.text  # uptime humanized
            assert "—" in r.text  # firmware/contract/hardware unknown
        finally:
            routes.set_health_monitor(None)

    def test_capabilities_render(self, client):
        hm = self._hm()
        try:
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
            r = client.get("/health", auth=client.auth)
            assert "abc1234" in r.text
            assert "0.3.0" in r.text
            assert "ice-colder mdb-sim" in r.text
            assert "02:11:22:33:44:55" in r.text
            assert "172.18.0.7" in r.text
            assert "refund" in r.text  # in the row title
        finally:
            routes.set_health_monitor(None)


class TestLogsContent:
    def test_logs_tab_shows_written_line(self, client, tmp_path, monkeypatch):
        from web_interface import routes as r

        log_file = tmp_path / "LOGS" / "vmc.log"
        log_file.parent.mkdir()
        log_file.write_text(
            "first line\nunique-marker-42;INFO 2026-09-21\n", encoding="utf-8"
        )
        monkeypatch.setattr(r, "LOG_PATH", log_file)

        resp = client.get("/logs")
        assert resp.status_code == 200
        assert "unique-marker-42" in resp.text

    def test_log_path_matches_logging_setup(self):
        from services.paths import LOG_FILE
        from web_interface import routes as r

        assert r.LOG_PATH == LOG_FILE
        assert LOG_FILE.parts[-2:] == ("LOGS", "vmc.log")


class TestCsrfGuard:
    @pytest.mark.parametrize(
        "path",
        [
            "/inventory/add",
            "/faults/PAY-104/clear",
            "/action/reset",
            "/inventory/update/X",
            "/inventory/delete/X",
        ],
    )
    def test_post_without_htmx_header_is_forbidden(self, client, path):
        resp = client.post(
            path,
            headers={"HX-Request": ""},
            data={"sku": "X", "name": "n", "price": "1", "slot": "0", "kind": "other"},
        )
        assert resp.status_code == 403
        assert "HTMX" in resp.text

    def test_get_routes_do_not_need_header(self, client):
        resp = client.get("/status", headers={"HX-Request": ""})
        assert resp.status_code == 200


class TestLoginLimiter:
    def test_lockout_after_ten_failures(self, client):
        from web_interface import routes as r

        r.login_limiter._failures.clear()
        r.login_limiter._locked_until.clear()
        for _ in range(10):
            assert client.get("/status", auth=("admin", "wrong")).status_code == 401
        resp = client.get("/status", auth=("admin", "wrong"))
        assert resp.status_code == 429
        assert "Retry-After" in resp.headers
        # even the right password is refused while locked
        assert client.get("/status").status_code == 429
        r.login_limiter._locked_until.clear()

    def test_success_resets_counter(self, client):
        from web_interface import routes as r

        r.login_limiter._failures.clear()
        r.login_limiter._locked_until.clear()
        for _ in range(9):
            client.get("/status", auth=("admin", "wrong"))
        assert client.get("/status").status_code == 200
        for _ in range(9):
            client.get("/status", auth=("admin", "wrong"))
        assert client.get("/status").status_code == 200

    def test_trusted_proxies_applied_from_config(self, tmp_path):
        from config.config_model import ConfigModel
        from web_interface import routes as r

        cfg = ConfigModel()
        cfg.web.trusted_proxies = ["172.25.0.0/16"]
        r.set_config_object(cfg)
        assert (
            r.login_limiter._networks
            and str(r.login_limiter._networks[0]) == "172.25.0.0/16"
        )
        r.set_config_object(ConfigModel())
        assert r.login_limiter._networks == []

    def test_set_config_object_resets_trusted_proxies_set_before_it(self):
        """set_config_object seeds the limiter from cfg.web.trusted_proxies,
        overwriting anything set earlier — so main() must call
        login_limiter.set_trusted_proxies(overrides.trusted_proxies) AFTER
        set_config_object(live_config), never before, or an env-derived
        override would be silently discarded."""
        from config.config_model import ConfigModel
        from web_interface import routes as r

        r.login_limiter.set_trusted_proxies(["172.25.0.0/16"])
        assert r.login_limiter._networks

        r.set_config_object(ConfigModel())
        assert r.login_limiter._networks == []


class TestStillSellingBanner:
    """A soft fault alerts but keeps selling; a hazard fault stops the machine."""

    @pytest.fixture
    def wired(self, client):
        from services.availability import Availability
        from services.health_monitor import HealthMonitor
        from web_interface import routes as r

        avail = Availability()
        r.vmc_instance.set_availability(avail)
        r.set_availability(avail)
        r.set_health_monitor(HealthMonitor())
        yield client
        r.set_availability(None)
        r.set_health_monitor(None)

    def test_status_shows_still_selling_for_a_soft_fault(self, wired):
        client = wired
        vmc_instance = routes.vmc_instance
        vmc_instance._raise_fault(FaultCode.PAY_104, outcome="restart")
        body = client.get("/status", headers={"HX-Request": "true"}).text
        assert "still selling" in body
        assert "Machine Stopped" not in body
        assert "PAY-104" in body

    def test_status_shows_machine_stopped_for_a_hazard_fault(self, wired):
        client = wired
        vmc_instance = routes.vmc_instance
        vmc_instance._raise_fault(FaultCode.WTR_104, outcome="leak")
        body = client.get("/status", headers={"HX-Request": "true"}).text
        assert "Machine Stopped" in body
        assert "still selling" not in body

    def test_health_permissives_table_shows_the_gate(self, wired):
        client = wired
        body = client.get("/health", headers={"HX-Request": "true"}).text
        assert "Gate" in body
        assert "fulfillment" in body


class TestAvailabilityOnDashboard:
    @pytest.fixture
    def wired(self, client):
        from services.availability import Availability
        from services.health_monitor import HealthMonitor
        from web_interface import routes as r

        avail = Availability()
        r.set_availability(avail)
        r.set_health_monitor(HealthMonitor())
        yield client, avail
        r.set_availability(None)

    def test_status_shows_payment_disabled_with_reason(self, wired):
        # Only a safety-gate row can disable payment now; a service door left
        # open is a real hazard, unlike a fulfillment-gate row (e.g. no
        # products), which must not disable payment.
        client, avail = wired
        avail.set_hardware_io("service_door", True)
        resp = client.get("/status")
        assert "Payment" in resp.text
        assert "Disabled" in resp.text
        assert "service_door_closed" in resp.text

    def test_status_is_not_healthy_when_only_payment_is_disabled(self, wired):
        """A safety permissive (service_door_closed) failing raises no fault
        and adds nothing to `issues` — it just flips a permissive row. Before
        this fix, `is_healthy` was `len(issues) == 0` alone, so this rendered
        the green "All Systems OK" card with a red "Disabled" Payment field
        buried in the corner, and "Machine Stopped" was unreachable in
        exactly the case it exists for. A machine not taking money must never
        render as healthy."""
        client, avail = wired
        avail.set_hardware_io("service_door", True)
        resp = client.get("/status")
        assert "All Systems OK" not in resp.text
        assert "Machine Stopped" in resp.text

    def test_health_lists_permissives_with_not_instrumented(self, wired):
        client, _ = wired
        resp = client.get("/health")
        assert "bag_present" in resp.text
        assert "not instrumented" in resp.text
        assert "vending_alive" in resp.text

    def test_screen_is_read_only_and_mobile(self, wired):
        client, _ = wired
        resp = client.get("/screen")
        assert resp.status_code == 200
        assert 'name="viewport"' in resp.text
        assert "hx-post" not in resp.text
        assert 'hx-get="/screen/body"' in resp.text
        body = client.get("/screen/body")
        assert body.status_code == 200
        assert "hx-post" not in body.text
        assert "Ice" in body.text and "Water" in body.text

    def test_screen_requires_auth(self, wired):
        client, _ = wired
        assert client.get("/screen", auth=("x", "y")).status_code == 401

    def test_screen_body_neutral_when_unwired(self, client):
        from web_interface import routes as r

        r.set_availability(None)
        r.set_health_monitor(None)
        try:
            resp = client.get("/screen/body")
            assert resp.status_code == 200
            assert "Disabled" not in resp.text
            assert "Unavailable" not in resp.text
        finally:
            r.set_availability(None)
            r.set_health_monitor(None)


class TestLogin:
    @pytest.fixture
    def public(self, tmp_path):
        """A client with a seeded AccessStore and no session cookies."""
        from services.access import AccessStore, Role
        from web_interface import auth as web_auth

        cfg = ConfigModel()
        store = AccessStore(path=tmp_path / "access.json")
        owner = store.create_user("Ada", "ada@example.com", Role.owner, "1379")
        routes.set_config_object(cfg)
        routes.set_access_store(store)
        with TestClient(app, follow_redirects=False) as c:
            c.headers["HX-Request"] = "true"
            yield c, store, owner
        routes.set_access_store(None)
        web_auth.backoff.set_trusted_proxies([])

    def test_login_page_lists_enabled_users_only(self, public):
        from services.access import Role

        c, store, owner = public
        hidden = store.create_user("Hidden", None, Role.tech, "2468")
        store.set_user_disabled(hidden.id, True)
        resp = c.get("/login", headers={})
        assert resp.status_code == 200
        assert "Ada" in resp.text
        assert "Hidden" not in resp.text

    def test_correct_pin_on_an_untrusted_browser_shows_enrollment(self, public):
        c, store, owner = public
        resp = c.post("/login", data={"user_id": owner.id, "pin": "1379"})
        assert resp.status_code == 200
        assert "code" in resp.text.lower()
        assert "hx-redirect" not in {k.lower() for k in resp.headers}
        assert c.cookies.get("vmc_enroll")

    def test_correct_pin_on_a_trusted_device_logs_in(self, public):
        from web_interface import auth as web_auth

        c, store, owner = public
        device, token = store.create_device("Tablet", shared=True)
        store.trust_device(device.id, owner.id)
        c.cookies.set(web_auth.DEVICE_COOKIE, token)
        resp = c.post("/login", data={"user_id": owner.id, "pin": "1379"})
        assert resp.status_code == 200
        assert resp.headers["hx-redirect"] == "/"
        assert c.cookies.get("vmc_session")
        assert store.get_user(owner.id).last_login_at is not None

    def test_wrong_pin_returns_a_generic_message_and_no_session(self, public):
        c, store, owner = public
        resp = c.post("/login", data={"user_id": owner.id, "pin": "9999"})
        assert resp.status_code == 200
        assert "wrong pin" in resp.text.lower()
        assert not c.cookies.get("vmc_session")

    def test_unknown_user_looks_identical_to_a_wrong_pin(self, public):
        c, store, owner = public
        wrong = c.post("/login", data={"user_id": owner.id, "pin": "9999"})
        unknown = c.post("/login", data={"user_id": "no-such-user", "pin": "9999"})
        assert unknown.status_code == wrong.status_code
        assert "wrong pin" in unknown.text.lower()

    def test_disabled_user_cannot_log_in(self, public):
        c, store, owner = public
        store.set_user_disabled(owner.id, True)
        resp = c.post("/login", data={"user_id": owner.id, "pin": "1379"})
        assert not c.cookies.get("vmc_session")
        assert "wrong pin" in resp.text.lower()

    def test_repeated_failures_back_off_with_429_and_retry_after(self, public):
        c, store, owner = public
        for _ in range(3):
            c.post("/login", data={"user_id": owner.id, "pin": "9999"})
        resp = c.post("/login", data={"user_id": owner.id, "pin": "9999"})
        assert resp.status_code == 429
        assert int(resp.headers["retry-after"]) >= 1

    def test_login_post_without_the_htmx_header_is_forbidden(self, public):
        c, store, owner = public
        resp = c.post(
            "/login",
            data={"user_id": owner.id, "pin": "1379"},
            headers={"HX-Request": ""},
        )
        assert resp.status_code == 403

    def test_login_page_redirects_to_setup_when_no_owner_exists(self, tmp_path):
        from services.access import AccessStore

        routes.set_config_object(ConfigModel())
        routes.set_access_store(AccessStore(path=tmp_path / "access.json"))
        with TestClient(app, follow_redirects=False) as c:
            resp = c.get("/login")
        assert resp.status_code == 303
        assert resp.headers["location"] == "/setup"
        routes.set_access_store(None)
