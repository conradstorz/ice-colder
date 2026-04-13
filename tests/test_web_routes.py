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
        resp = client.post("/inventory/add", data={
            "sku": "TEST-001",
            "name": "Test Ice",
            "price": "2.50",
        })
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


class TestLogsEndpoint:
    def test_logs_returns_html(self, client):
        resp = client.get("/logs")
        assert resp.status_code == 200
