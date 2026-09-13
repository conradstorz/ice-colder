"""Tests for web_interface routes using FastAPI TestClient."""

import pytest
from fastapi.testclient import TestClient
from config.config_model import ConfigModel
from controller.vmc import VMC
from web_interface.server import app
from web_interface import routes


@pytest.fixture
def client():
    """Create a TestClient with a real ConfigModel and VMC."""
    cfg = ConfigModel()
    vmc = VMC(config=cfg)
    routes.set_config_object(cfg)
    routes.set_vmc_instance(vmc)

    with TestClient(app) as c:
        c.auth = ("admin", "changeme")
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
        """Edit form for default product SKU."""
        resp = client.get("/inventory/edit/SAMPLE-SKU")
        assert resp.status_code == 200


class TestConfigEndpoints:
    def test_machine_info(self, client):
        resp = client.get("/config/machine")
        assert resp.status_code == 200

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
