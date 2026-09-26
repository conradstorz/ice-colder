"""Tests for web_interface routes using FastAPI TestClient."""

import re
import uuid

import pytest
from fastapi.testclient import TestClient
from config.config_model import ConfigModel
from contracts.vending_machine import FaultCode
from controller.vmc import VMC
from services.access import ROLE_PERMISSIONS, AccessStore, Permission, Role, User
from services.display_controller import DisplayController
from services.inventory_manager import InventoryManager
from web_interface import auth as web_auth
from web_interface.server import app
from web_interface import routes


def sign_in(store: AccessStore, user: User, *, shared: bool = False) -> TestClient:
    """Mint a device, trust *user* on it, open a session, and return a
    client that is fully logged in: it carries vmc_device, vmc_session and
    the HX-Request: true header, the way a real enrolled browser would."""
    device, token = store.create_device(f"{user.name}'s device", shared=shared)
    store.trust_device(device.id, user.id)
    session_id = store.create_session(user.id, device.id)
    store.record_login(user.id)

    client = TestClient(app)
    client.headers["HX-Request"] = "true"
    client.cookies.set(web_auth.DEVICE_COOKIE, token)
    client.cookies.set(web_auth.SESSION_COOKIE, session_id)
    return client


def make_client(
    store: AccessStore,
    role: Role = Role.owner,
    *,
    shared: bool = False,
    name: str = "Ada",
) -> tuple[TestClient, User]:
    """Seed a fresh user of *role* and sign them in."""
    email = f"{name.lower().replace(' ', '.')}@example.com"
    user = store.create_user(name, email, role, "2468")
    return sign_in(store, user, shared=shared), user


@pytest.fixture
def wired(tmp_path):
    """A ConfigModel, VMC, InventoryManager and AccessStore, wired into
    routes the way main() wires them. The owner "Ada" is already seeded
    (email ada@example.com, PIN 1379) and setup is finalized, so route
    tests are never in setup mode."""
    cfg = ConfigModel()
    vmc = VMC(config=cfg)
    inv = InventoryManager([], path=tmp_path / "inventory.json")
    store = AccessStore(path=tmp_path / "access.json")
    store.create_user("Ada", "ada@example.com", Role.owner, "1379")
    store.finalize_setup()

    routes.set_config_object(cfg)
    routes.set_vmc_instance(vmc)
    routes.set_inventory_manager(inv)
    routes.set_access_store(store)

    yield cfg, vmc, inv, store

    routes.set_access_store(None)
    for t in vmc._pending_tasks:
        t.cancel()


@pytest.fixture
def login_as(wired):
    """login_as(role=Role.owner, *, shared=False, name=None) -> TestClient.

    Role.owner returns a client for the fixture's existing owner (the store
    enforces one owner per machine); any other role seeds a fresh user.
    Every client this makes is closed on teardown.
    """
    _cfg, _vmc, _inv, store = wired
    clients: list[TestClient] = []

    def _login_as(
        role: Role = Role.owner, *, shared: bool = False, name: str | None = None
    ) -> TestClient:
        if role == Role.owner:
            client = sign_in(store, store.owner(), shared=shared)
        else:
            client, _user = make_client(
                store, role, shared=shared, name=name or role.value.capitalize()
            )
        clients.append(client)
        return client

    yield _login_as

    for c in clients:
        c.close()


@pytest.fixture
def client(login_as):
    """Every pre-existing test in this file runs through this client,
    authenticated as the owner."""
    return login_as(Role.owner)


@pytest.fixture
def anonymous(wired):
    """A client with no cookies at all, against a wired store that does
    have an owner — for asserting what an unauthenticated visitor gets."""
    c = TestClient(app, follow_redirects=False)
    yield c
    c.close()


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
        """Catalog edit form for a product created via the dashboard."""
        client.post(
            "/inventory/add",
            data={"sku": "EDIT-1", "name": "Editable", "price": "1.50"},
        )
        resp = client.get("/inventory/edit/EDIT-1/catalog")
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
        resp = client.get("/inventory/edit/EDIT-2/placement")
        assert resp.status_code == 200
        assert 'name="slot"' in resp.text
        assert 'value="9"' in resp.text

    def test_update_product_changes_slot(self, client):
        client.post(
            "/inventory/add",
            data={"sku": "UPD-1", "name": "Updatable", "price": "1.50", "slot": "1"},
        )
        resp = client.post(
            "/inventory/update/UPD-1/placement",
            data={"slot": "6", "inventory_count": "0"},
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
            "/inventory/update/KIND-2/catalog",
            data={"name": "Flexible", "price": "1.00", "kind": "ice"},
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

    def test_old_single_edit_form_route_404s(self, client):
        client.post(
            "/inventory/add",
            data={"sku": "OLD-1", "name": "Old Form", "price": "1.00"},
        )
        resp = client.get("/inventory/edit/OLD-1")
        assert resp.status_code == 404

    def test_old_single_update_route_404s(self, client):
        client.post(
            "/inventory/add",
            data={"sku": "OLD-2", "name": "Old Form", "price": "1.00"},
        )
        resp = client.post(
            "/inventory/update/OLD-2",
            data={"name": "Old Form", "price": "1.00", "slot": "0"},
        )
        assert resp.status_code == 404

    def test_catalog_edit_form(self, client):
        client.post(
            "/inventory/add",
            data={"sku": "CAT-1", "name": "Catalog Item", "price": "2.00"},
        )
        resp = client.get("/inventory/edit/CAT-1/catalog")
        assert resp.status_code == 200
        assert "Catalog Item" in resp.text

    def test_catalog_post_changes_name_price_kind(self, client):
        client.post(
            "/inventory/add",
            data={"sku": "CAT-2", "name": "Old Name", "price": "1.00"},
        )
        resp = client.post(
            "/inventory/update/CAT-2/catalog",
            data={"name": "New Name", "price": "3.50", "kind": "water"},
        )
        assert resp.status_code == 200
        updated = next(p for p in routes.config.products if p.sku == "CAT-2")
        assert updated.name == "New Name"
        assert updated.price == 3.50
        assert updated.kind == "water"

    def test_placement_edit_form(self, client):
        client.post(
            "/inventory/add",
            data={"sku": "PLC-1", "name": "Placed Item", "price": "2.00", "slot": "4"},
        )
        resp = client.get("/inventory/edit/PLC-1/placement")
        assert resp.status_code == 200
        assert 'name="slot"' in resp.text
        assert 'value="4"' in resp.text

    def test_placement_post_changes_slot_count_and_tracked(self, client, wired):
        _cfg, _vmc, inv, _store = wired
        client.post(
            "/inventory/add",
            data={"sku": "PLC-2", "name": "Placed Item", "price": "2.00", "slot": "1"},
        )
        resp = client.post(
            "/inventory/update/PLC-2/placement",
            data={
                "slot": "8",
                "inventory_count": "15",
                "track_inventory": "on",
            },
        )
        assert resp.status_code == 200
        updated = next(p for p in routes.config.products if p.sku == "PLC-2")
        assert updated.slot == 8
        assert inv.get_count("PLC-2") == 15
        assert inv.is_tracked("PLC-2") is True

    def test_placement_post_returns_table_showing_stored_count(self, client, wired):
        """The count a loader just stored (in InventoryManager, not on
        Product) must appear in the table HTMX swaps back in — not the
        stale/zero value still on Product.inventory_count."""
        _cfg, _vmc, inv, _store = wired
        client.post(
            "/inventory/add",
            data={"sku": "PLC-5", "name": "Placed Item", "price": "2.00", "slot": "1"},
        )
        resp = client.post(
            "/inventory/update/PLC-5/placement",
            data={"slot": "1", "inventory_count": "42", "track_inventory": "on"},
        )
        assert resp.status_code == 200
        assert inv.get_count("PLC-5") == 42
        product = next(p for p in routes.config.products if p.sku == "PLC-5")
        assert product.inventory_count != 42
        assert ">42<" in resp.text

    def test_placement_post_unchecked_tracking_clears_flag(self, client, wired):
        _cfg, _vmc, inv, _store = wired
        client.post(
            "/inventory/add",
            data={"sku": "PLC-3", "name": "Placed Item", "price": "2.00", "slot": "2"},
        )
        client.post(
            "/inventory/update/PLC-3/placement",
            data={"slot": "2", "inventory_count": "5", "track_inventory": "on"},
        )
        assert inv.is_tracked("PLC-3") is True
        resp = client.post(
            "/inventory/update/PLC-3/placement",
            data={"slot": "2", "inventory_count": "5"},
        )
        assert resp.status_code == 200
        assert inv.is_tracked("PLC-3") is False

    def test_placement_post_cannot_change_name_or_price(self, client, wired):
        """The whole point of the split: a placement POST must not smuggle a
        catalog change through, regardless of what an attacker's form body
        contains — fields the placement form doesn't own are not applied."""
        _cfg, _vmc, inv, _store = wired
        client.post(
            "/inventory/add",
            data={"sku": "PLC-4", "name": "Original", "price": "9.99", "slot": "3"},
        )
        resp = client.post(
            "/inventory/update/PLC-4/placement",
            data={
                "slot": "5",
                "inventory_count": "2",
                "name": "Hacked Name",
                "price": "0.01",
            },
        )
        assert resp.status_code == 200
        updated = next(p for p in routes.config.products if p.sku == "PLC-4")
        assert updated.slot == 5
        assert updated.name == "Original"
        assert updated.price == 9.99


class TestCatalogPlacementPermissions:
    """A loader restocks (placement) but must never touch price/name/kind
    (catalog). A tech behaves the same as a loader here."""

    @pytest.mark.parametrize("role", [Role.loader, Role.tech])
    def test_placement_form_and_post_allowed(self, login_as, wired, role):
        _cfg, _vmc, inv, _store = wired
        owner = login_as(Role.owner)
        owner.post(
            "/inventory/add",
            data={
                "sku": f"PERM-{role.value}",
                "name": "Item",
                "price": "5.00",
                "slot": "1",
            },
        )

        worker = login_as(role)
        get_resp = worker.get(f"/inventory/edit/PERM-{role.value}/placement")
        assert get_resp.status_code == 200

        post_resp = worker.post(
            f"/inventory/update/PERM-{role.value}/placement",
            data={"slot": "2", "inventory_count": "3", "track_inventory": "on"},
        )
        assert post_resp.status_code == 200
        updated = next(
            p for p in routes.config.products if p.sku == f"PERM-{role.value}"
        )
        assert updated.slot == 2
        assert updated.price == 5.00
        assert inv.get_count(f"PERM-{role.value}") == 3

    @pytest.mark.parametrize("role", [Role.loader, Role.tech])
    def test_catalog_form_and_post_forbidden_price_unchanged(
        self, login_as, wired, role
    ):
        _cfg, _vmc, _inv, _store = wired
        owner = login_as(Role.owner)
        owner.post(
            "/inventory/add",
            data={"sku": f"NOPE-{role.value}", "name": "Item", "price": "5.00"},
        )

        worker = login_as(role)
        get_resp = worker.get(f"/inventory/edit/NOPE-{role.value}/catalog")
        assert get_resp.status_code == 403

        post_resp = worker.post(
            f"/inventory/update/NOPE-{role.value}/catalog",
            data={"name": "Hacked", "price": "0.01", "kind": "water"},
        )
        assert post_resp.status_code == 403

        unchanged = next(
            p for p in routes.config.products if p.sku == f"NOPE-{role.value}"
        )
        assert unchanged.price == 5.00
        assert unchanged.name == "Item"


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


# (method, path, permission) — the authoritative route -> permission table.
# /config/payments and /config/comms are deliberately excluded: their
# templates (partials/payments.html, partials/comms.html) don't exist yet —
# their own TestConfigEndpoints tests above are @pytest.mark.skip'd for the
# same reason — so a permitted role would get a 500 from the missing
# template, not the 200 this matrix expects, and the matrix would lie.
_MATRIX_ROUTES = [
    ("GET", "/", Permission.view_status),
    ("GET", "/status", Permission.view_status),
    ("GET", "/kpi", Permission.view_status),
    ("GET", "/activity", Permission.view_status),
    ("GET", "/health", Permission.view_status),
    ("GET", "/screen", Permission.view_status),
    ("GET", "/inventory", Permission.view_status),
    ("GET", "/logs", Permission.view_logs),
    ("GET", "/inventory/new", Permission.edit_catalog),
    ("GET", "/config/machine", Permission.edit_contacts),
    ("GET", "/config/contacts", Permission.edit_contacts),
    ("POST", "/action/restart", Permission.machine_controls),
    ("GET", "/users", Permission.manage_users),
    ("GET", "/users/new", Permission.manage_users),
]


class TestPermissionMatrix:
    """For every (route, role) pair, 200 exactly when ROLE_PERMISSIONS[role]
    holds the route's permission, and 403 otherwise."""

    @pytest.mark.parametrize("role", list(Role))
    @pytest.mark.parametrize("method, path, permission", _MATRIX_ROUTES)
    def test_matrix(self, login_as, method, path, permission, role):
        client = login_as(role)
        resp = client.get(path) if method == "GET" else client.post(path)
        if permission in ROLE_PERMISSIONS[role]:
            assert resp.status_code == 200, (
                role,
                path,
                resp.status_code,
                resp.text[:300],
            )
        else:
            assert resp.status_code == 403, (role, path, resp.status_code)


class TestUnauthenticatedAccess:
    """No session at all: Task 8's redirect/401 behavior, routed through by
    every gated route now that HTTP Basic is gone. Stale HTTP Basic
    credentials on the request must grant nothing — there is no HTTP Basic
    left to check them against."""

    def test_page_request_redirects_to_login(self, anonymous):
        resp = anonymous.get("/")
        assert resp.status_code == 303
        assert resp.headers["location"] == "/login"

    def test_htmx_request_gets_401_with_hx_redirect(self, anonymous):
        resp = anonymous.get("/status", headers={"HX-Request": "true"})
        assert resp.status_code == 401
        assert resp.headers["hx-redirect"] == "/login"

    def test_stale_basic_auth_credentials_grant_nothing(self, anonymous):
        page_resp = anonymous.get("/", auth=("admin", "changeme"))
        assert page_resp.status_code == 303

        htmx_resp = anonymous.get(
            "/status", auth=("admin", "changeme"), headers={"HX-Request": "true"}
        )
        assert htmx_resp.status_code == 401


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

    def test_delete_requires_auth(self, anonymous):
        resp = anonymous.post("/inventory/delete/X", headers={"HX-Request": "true"})
        assert resp.status_code == 401
        assert resp.headers["hx-redirect"] == "/login"

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
            "/inventory/update/X/catalog",
            "/inventory/update/X/placement",
            "/inventory/delete/X",
        ],
    )
    def test_post_without_htmx_header_is_forbidden(self, client, path):
        resp = client.post(
            path,
            headers={"HX-Request": ""},
            data={
                "sku": "X",
                "name": "n",
                "price": "1",
                "slot": "0",
                "kind": "other",
                "inventory_count": "0",
            },
        )
        assert resp.status_code == 403
        assert "HTMX" in resp.text

    def test_get_routes_do_not_need_header(self, client):
        resp = client.get("/status", headers={"HX-Request": ""})
        assert resp.status_code == 200


class TestStillSellingBanner:
    """A soft fault alerts but keeps selling; a hazard fault stops the machine."""

    @pytest.fixture
    def selling_client(self, client):
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

    def test_status_shows_still_selling_for_a_soft_fault(self, selling_client):
        client = selling_client
        vmc_instance = routes.vmc_instance
        vmc_instance._raise_fault(FaultCode.PAY_104, outcome="restart")
        body = client.get("/status", headers={"HX-Request": "true"}).text
        assert "still selling" in body
        assert "Machine Stopped" not in body
        assert "PAY-104" in body

    def test_status_shows_machine_stopped_for_a_hazard_fault(self, selling_client):
        client = selling_client
        vmc_instance = routes.vmc_instance
        vmc_instance._raise_fault(FaultCode.WTR_104, outcome="leak")
        body = client.get("/status", headers={"HX-Request": "true"}).text
        assert "Machine Stopped" in body
        assert "still selling" not in body

    def test_health_permissives_table_shows_the_gate(self, selling_client):
        client = selling_client
        body = client.get("/health", headers={"HX-Request": "true"}).text
        assert "Gate" in body
        assert "fulfillment" in body


class TestAvailabilityOnDashboard:
    @pytest.fixture
    def avail_client(self, client):
        from services.availability import Availability
        from services.health_monitor import HealthMonitor
        from web_interface import routes as r

        avail = Availability()
        r.set_availability(avail)
        r.set_health_monitor(HealthMonitor())
        yield client, avail
        r.set_availability(None)

    def test_status_shows_payment_disabled_with_reason(self, avail_client):
        # Only a safety-gate row can disable payment now; a service door left
        # open is a real hazard, unlike a fulfillment-gate row (e.g. no
        # products), which must not disable payment.
        client, avail = avail_client
        avail.set_hardware_io("service_door", True)
        resp = client.get("/status")
        assert "Payment" in resp.text
        assert "Disabled" in resp.text
        assert "service_door_closed" in resp.text

    def test_status_is_not_healthy_when_only_payment_is_disabled(self, avail_client):
        """A safety permissive (service_door_closed) failing raises no fault
        and adds nothing to `issues` — it just flips a permissive row. Before
        this fix, `is_healthy` was `len(issues) == 0` alone, so this rendered
        the green "All Systems OK" card with a red "Disabled" Payment field
        buried in the corner, and "Machine Stopped" was unreachable in
        exactly the case it exists for. A machine not taking money must never
        render as healthy."""
        client, avail = avail_client
        avail.set_hardware_io("service_door", True)
        resp = client.get("/status")
        assert "All Systems OK" not in resp.text
        assert "Machine Stopped" in resp.text

    def test_health_lists_permissives_with_not_instrumented(self, avail_client):
        client, _ = avail_client
        resp = client.get("/health")
        assert "bag_present" in resp.text
        assert "not instrumented" in resp.text
        assert "vending_alive" in resp.text

    def test_screen_is_read_only_and_mobile(self, avail_client):
        client, _ = avail_client
        resp = client.get("/screen")
        assert resp.status_code == 200
        assert 'name="viewport"' in resp.text
        assert "hx-post" not in resp.text
        assert 'hx-get="/screen/body"' in resp.text
        body = client.get("/screen/body")
        assert body.status_code == 200
        assert "hx-post" not in body.text
        assert "Ice" in body.text and "Water" in body.text

    def test_screen_requires_auth(self, anonymous):
        resp = anonymous.get("/screen")
        assert resp.status_code == 303
        assert resp.headers["location"] == "/login"

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

    def test_failure_responses_do_not_leak_user_existence_via_selection(self, public):
        """The picker must never mark an <option> 'selected' on a failure
        path — that only happens when the submitted id names an enabled
        user, which would let an attacker enumerate accounts without a PIN.
        """
        from services.access import Role

        c, store, owner = public
        disabled = store.create_user("Bob", None, Role.tech, "1111")
        store.set_user_disabled(disabled.id, True)

        # A unique id per run: the back-off tracker is a module-level
        # singleton shared across tests, keyed by (kind, subject, client) —
        # reusing a literal like "no-such-user" would collide with another
        # test's failure count for this same TestClient and spuriously 429.
        unknown_id = f"no-such-user-{uuid.uuid4()}"

        wrong_pin = c.post("/login", data={"user_id": owner.id, "pin": "9999"})
        unknown_user = c.post("/login", data={"user_id": unknown_id, "pin": "9999"})
        disabled_user_correct_pin = c.post(
            "/login", data={"user_id": disabled.id, "pin": "1111"}
        )

        assert wrong_pin.status_code == unknown_user.status_code
        assert wrong_pin.status_code == disabled_user_correct_pin.status_code
        assert wrong_pin.content == unknown_user.content
        assert wrong_pin.content == disabled_user_correct_pin.content

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


class TestEnrollment:
    @pytest.fixture
    def public(self, tmp_path):
        from services.access import AccessStore, Role
        from web_interface import auth as web_auth

        cfg = ConfigModel()
        store = AccessStore(path=tmp_path / "access.json")
        owner = store.create_user("Ada", "ada@example.com", Role.owner, "1379")
        store.generate_emergency_codes()
        routes.set_config_object(cfg)
        routes.set_access_store(store)
        with TestClient(app, follow_redirects=False) as c:
            c.headers["HX-Request"] = "true"
            yield c, store, owner, cfg
        routes.set_access_store(None)
        web_auth.backoff.set_trusted_proxies([])

    def test_enrollment_issues_a_device_cookie_immediately(self, public):
        c, store, owner, _ = public
        c.post("/login", data={"user_id": owner.id, "pin": "1379"})
        assert c.cookies.get("vmc_device")
        assert len(store.devices) == 1

    def test_emergency_code_enrolls_and_logs_in(self, public):
        c, store, owner, _ = public
        codes = store.generate_emergency_codes()
        c.post("/login", data={"user_id": owner.id, "pin": "1379"})
        resp = c.post("/login/enroll", data={"code": codes[0]})
        assert resp.headers["hx-redirect"] == "/"
        assert c.cookies.get("vmc_session")
        device = next(iter(store.devices.values()))
        assert owner.id in device.trusted_user_ids
        assert store.unused_emergency_code_count() == 19

    def test_emergency_code_works_with_no_smtp_configured(self, public):
        c, store, owner, cfg = public
        assert cfg.communication.email_gateway.is_configured is False
        codes = store.generate_emergency_codes()
        c.post("/login", data={"user_id": owner.id, "pin": "1379"})
        assert (
            c.post("/login/enroll", data={"code": codes[0]}).headers["hx-redirect"]
            == "/"
        )

    def test_a_used_emergency_code_is_refused(self, public):
        c, store, owner, _ = public
        codes = store.generate_emergency_codes()
        c.post("/login", data={"user_id": owner.id, "pin": "1379"})
        c.post("/login/enroll", data={"code": codes[0]})

        # A fresh, untrusted client: no vmc_device, no vmc_session. Reusing
        # `c` here would hit the "already trusted device" fast path on the
        # second /login and never reach the emergency-code check at all.
        fresh = TestClient(app, follow_redirects=False)
        fresh.headers["HX-Request"] = "true"
        fresh.post("/login", data={"user_id": owner.id, "pin": "1379"})
        assert fresh.cookies.get("vmc_enroll")
        resp = fresh.post("/login/enroll", data={"code": codes[0]})
        assert "hx-redirect" not in {k.lower() for k in resp.headers}
        assert resp.status_code == 200
        assert "not accepted" in resp.text.lower()

    def test_otp_path_with_a_stubbed_mailer(self, public, monkeypatch):
        c, store, owner, cfg = public
        cfg.communication.email_gateway.smtp_server = "smtp.real.local"
        sent = {}

        async def fake_send_email(gateway, to, subject, body):
            sent["to"] = to
            sent["body"] = body
            return True

        monkeypatch.setattr(routes, "send_email", fake_send_email)
        c.post("/login", data={"user_id": owner.id, "pin": "1379"})
        page = c.post("/login/enroll/send", data={})
        assert page.status_code == 200
        assert sent["to"] == "ada@example.com"
        code = "".join(ch for ch in sent["body"] if ch.isdigit())[-6:]
        resp = c.post("/login/enroll", data={"code": code})
        assert resp.headers["hx-redirect"] == "/"

    def test_smtp_failure_tells_the_user_to_use_an_emergency_code(
        self, public, monkeypatch
    ):
        c, store, owner, cfg = public
        cfg.communication.email_gateway.smtp_server = "smtp.real.local"

        async def fake_send_email(gateway, to, subject, body):
            return False

        monkeypatch.setattr(routes, "send_email", fake_send_email)
        c.post("/login", data={"user_id": owner.id, "pin": "1379"})
        page = c.post("/login/enroll/send", data={})
        assert "emergency code" in page.text.lower()

    def test_wrong_code_backs_off_with_429(self, public):
        c, store, owner, _ = public
        c.post("/login", data={"user_id": owner.id, "pin": "1379"})
        for _ in range(3):
            c.post("/login/enroll", data={"code": "00000000"})
        resp = c.post("/login/enroll", data={"code": "00000000"})
        assert resp.status_code == 429
        assert int(resp.headers["retry-after"]) >= 1

    def test_enroll_without_a_valid_enroll_cookie_is_refused(self, public):
        c, store, owner, _ = public
        codes = store.generate_emergency_codes()
        resp = c.post("/login/enroll", data={"code": codes[0]})
        assert resp.status_code in (401, 403)
        assert not c.cookies.get("vmc_session")

    def test_logout_ends_the_session_and_keeps_the_device(self, public):
        c, store, owner, _ = public
        codes = store.generate_emergency_codes()
        c.post("/login", data={"user_id": owner.id, "pin": "1379"})
        c.post("/login/enroll", data={"code": codes[0]})
        device_cookie = c.cookies.get("vmc_device")
        resp = c.post("/logout", data={})
        assert resp.headers["hx-redirect"] == "/login"
        assert not c.cookies.get("vmc_session")
        assert c.cookies.get("vmc_device") == device_cookie

    def test_locked_shared_session_resumes_with_pin_only(self, public):
        c, store, owner, _ = public
        codes = store.generate_emergency_codes()
        c.post("/login", data={"user_id": owner.id, "pin": "1379"})
        c.post("/login/enroll", data={"code": codes[0]})
        c.post("/logout", data={})
        resp = c.post("/login", data={"user_id": owner.id, "pin": "1379"})
        assert resp.headers["hx-redirect"] == "/"
        assert c.cookies.get("vmc_session")


class TestSetupWizard:
    """Setup mode: a fresh store has no owner, so every route except /setup
    (and /static) is gated behind the wizard (spec §3.1)."""

    @pytest.fixture
    def fresh_store(self, tmp_path):
        """A ConfigModel, an AccessStore with no owner yet, and a real
        DisplayController (no MQTT attached, so publishing is a no-op) wired
        into routes — the state the setup-mode gate and wizard run against.

        The back-off subject for this route is the fixed string "setup"
        (there is no user yet to key on), so unlike the per-user PIN
        back-off tested elsewhere, the (kind, subject, client) triple is
        identical across every test in this class — the shared
        `web_auth.backoff` singleton must be cleared or one test's failures
        would 429 the next.
        """
        web_auth.backoff._failures.clear()
        web_auth.backoff._budget.clear()

        cfg = ConfigModel()
        store = AccessStore(path=tmp_path / "access.json")
        display = DisplayController()

        routes.set_config_object(cfg)
        routes.set_access_store(store)
        routes.set_display_controller(display)
        # routes._pending_codes is module-level state shared by every test in
        # this process (Task 15) — reset it on both sides so a test that
        # generates codes can never leak them into the next one.
        routes._pending_codes = []

        yield cfg, store, display

        routes._pending_codes = []
        routes.set_access_store(None)
        routes.set_display_controller(None)
        web_auth.backoff._failures.clear()
        web_auth.backoff._budget.clear()
        web_auth.backoff.set_trusted_proxies([])

    @pytest.fixture
    def anon(self, fresh_store):
        c = TestClient(app, follow_redirects=False)
        c.headers["HX-Request"] = "true"
        yield c
        c.close()

    @pytest.mark.parametrize(
        "path", ["/", "/status", "/inventory", "/login", "/health"]
    )
    def test_setup_mode_redirects_every_route_to_setup(self, anon, path):
        resp = anon.get(path)
        assert resp.status_code == 303
        assert resp.headers["location"] == "/setup"

    def test_setup_page_itself_answers_200(self, anon):
        resp = anon.get("/setup")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]
        assert "set up" in resp.text.lower()

    def test_setup_code_is_on_the_display_and_matches_the_store(
        self, anon, fresh_store
    ):
        _cfg, store, display = fresh_store
        anon.get("/setup")
        assert store.pending_setup_code is not None
        assert display.setup_code == store.pending_setup_code

    def test_wrong_code_creates_no_owner(self, anon, fresh_store):
        _cfg, store, _display = fresh_store
        anon.get("/setup")
        resp = anon.post(
            "/setup",
            data={
                "setup_code": "00000000",
                "name": "Ada",
                "email": "ada@example.com",
                "pin": "2468",
                "pin_confirm": "2468",
            },
        )
        assert resp.status_code == 200
        assert store.owner() is None
        assert "not accepted" in resp.text.lower()

    def test_repeated_wrong_codes_reach_429(self, anon, fresh_store):
        anon.get("/setup")
        payload = {
            "setup_code": "00000000",
            "name": "Ada",
            "email": "a@example.com",
            "pin": "2468",
            "pin_confirm": "2468",
        }
        for _ in range(3):
            anon.post("/setup", data=payload)
        resp = anon.post("/setup", data=payload)
        assert resp.status_code == 429
        assert int(resp.headers["retry-after"]) >= 1

    def test_right_code_creates_the_owner_and_completes_step_one(
        self, anon, fresh_store
    ):
        _cfg, store, _display = fresh_store
        anon.get("/setup")
        code = store.pending_setup_code
        resp = anon.post(
            "/setup",
            data={
                "setup_code": code,
                "name": "Ada",
                "email": "ada@example.com",
                "pin": "2468",
                "pin_confirm": "2468",
                "shared_device": "true",
            },
        )
        assert resp.headers["hx-redirect"] == "/setup/codes"
        assert resp.cookies.get("vmc_session")
        assert resp.cookies.get("vmc_device")

        owner = store.owner()
        assert owner is not None
        assert owner.name == "Ada"
        device = next(iter(store.devices.values()))
        assert device.shared is True
        assert owner.id in device.trusted_user_ids

    def test_pin_failing_policy_is_rejected_with_the_reason(self, anon, fresh_store):
        _cfg, store, _display = fresh_store
        anon.get("/setup")
        code = store.pending_setup_code
        resp = anon.post(
            "/setup",
            data={
                "setup_code": code,
                "name": "Ada",
                "email": "ada@example.com",
                "pin": "1111",
                "pin_confirm": "1111",
            },
        )
        assert resp.status_code == 200
        assert store.owner() is None
        assert "same digit" in resp.text.lower()

    def test_mismatched_confirmation_is_rejected(self, anon, fresh_store):
        _cfg, store, _display = fresh_store
        anon.get("/setup")
        code = store.pending_setup_code
        resp = anon.post(
            "/setup",
            data={
                "setup_code": code,
                "name": "Ada",
                "email": "ada@example.com",
                "pin": "2468",
                "pin_confirm": "1234",
            },
        )
        assert resp.status_code == 200
        assert store.owner() is None
        assert "match" in resp.text.lower()

    def test_lost_step_one_response_recovered_via_login_and_enroll(
        self, anon, fresh_store
    ):
        """The setup code stays valid as an enrollment code until Done
        (Task 15), so a second browser that never saw the step-1 response
        can still sign in and enroll with it (spec §3.1)."""
        _cfg, store, _display = fresh_store
        anon.get("/setup")
        code = store.pending_setup_code
        anon.post(
            "/setup",
            data={
                "setup_code": code,
                "name": "Ada",
                "email": "ada@example.com",
                "pin": "2468",
                "pin_confirm": "2468",
            },
        )
        owner = store.owner()
        assert owner is not None

        second = TestClient(app, follow_redirects=False)
        second.headers["HX-Request"] = "true"
        login_resp = second.post("/login", data={"user_id": owner.id, "pin": "2468"})
        assert login_resp.status_code == 200
        assert second.cookies.get("vmc_enroll")

        enroll_resp = second.post("/login/enroll", data={"code": code})
        assert enroll_resp.headers["hx-redirect"] == "/"
        assert second.cookies.get("vmc_session")
        second.close()

    def test_post_without_htmx_header_is_403(self, fresh_store):
        c = TestClient(app, follow_redirects=False)
        resp = c.post(
            "/setup",
            data={
                "setup_code": "00000000",
                "name": "Ada",
                "email": "a@example.com",
                "pin": "2468",
                "pin_confirm": "2468",
            },
        )
        assert resp.status_code == 403
        c.close()

    def test_owner_race_is_caught_and_re_rendered_not_500(self, anon, fresh_store):
        """Two racing step-1 submissions: the store rejects the second
        owner, and the route must turn that into an ordinary error page,
        never a 500 (spec §3.1)."""
        _cfg, store, _display = fresh_store
        anon.get("/setup")
        code = store.pending_setup_code
        store.create_user("First", "first@example.com", Role.owner, "2468")

        resp = anon.post(
            "/setup",
            data={
                "setup_code": code,
                "name": "Second",
                "email": "second@example.com",
                "pin": "3690",
                "pin_confirm": "3690",
            },
        )
        assert resp.status_code == 200
        assert "hx-redirect" not in {k.lower() for k in resp.headers}
        owners = [u for u in store.users.values() if u.role == Role.owner]
        assert len(owners) == 1
        assert owners[0].name == "First"

    # --- Task 15: /setup/codes (step 2) and Done ---

    def _create_owner(self, anon, store) -> User:
        """Walk step 1 to completion on *anon*, leaving it signed in as the
        fresh owner, positioned to hit /setup/codes next."""
        anon.get("/setup")
        code = store.pending_setup_code
        anon.post(
            "/setup",
            data={
                "setup_code": code,
                "name": "Ada",
                "email": "ada@example.com",
                "pin": "2468",
                "pin_confirm": "2468",
            },
        )
        return store.owner()

    def test_codes_page_shows_twenty_distinct_codes(self, anon, fresh_store):
        _cfg, store, _display = fresh_store
        self._create_owner(anon, store)
        resp = anon.get("/setup/codes")
        assert resp.status_code == 200
        codes = re.findall(r"\b\d{8}\b", resp.text)
        assert len(codes) == 20
        assert len(set(codes)) == 20
        assert store.unused_emergency_code_count() == 20

    def test_reload_shows_the_same_codes_and_does_not_regenerate(
        self, anon, fresh_store
    ):
        _cfg, store, _display = fresh_store
        self._create_owner(anon, store)
        first = re.findall(r"\b\d{8}\b", anon.get("/setup/codes").text)
        second = re.findall(r"\b\d{8}\b", anon.get("/setup/codes").text)
        assert first == second
        assert store.unused_emergency_code_count() == 20

    def test_done_finalizes_clears_display_and_redirects(self, anon, fresh_store):
        _cfg, store, display = fresh_store
        self._create_owner(anon, store)
        anon.get("/setup/codes")
        assert display.setup_code is not None
        resp = anon.post("/setup/codes/done", data={})
        assert resp.headers["hx-redirect"] == "/"
        assert store.setup_finalized is True
        assert display.setup_code is None

    def test_setup_code_stops_enrolling_after_done(self, anon, fresh_store):
        _cfg, store, _display = fresh_store
        owner = self._create_owner(anon, store)
        code = store.pending_setup_code
        anon.get("/setup/codes")
        anon.post("/setup/codes/done", data={})
        assert store.verify_setup_code(code) is False

        second = TestClient(app, follow_redirects=False)
        second.headers["HX-Request"] = "true"
        second.post("/login", data={"user_id": owner.id, "pin": "2468"})
        resp = second.post("/login/enroll", data={"code": code})
        assert "hx-redirect" not in {k.lower() for k in resp.headers}
        assert "not accepted" in resp.text.lower()
        second.close()

    def test_page_does_not_show_codes_again_after_done(self, anon, fresh_store):
        _cfg, store, _display = fresh_store
        self._create_owner(anon, store)
        anon.get("/setup/codes")
        anon.post("/setup/codes/done", data={})
        resp = anon.get("/setup/codes")
        assert resp.status_code == 303
        assert resp.headers["location"] == "/"

    def test_email_button_only_shown_when_gateway_configured(self, anon, fresh_store):
        cfg, store, _display = fresh_store
        self._create_owner(anon, store)
        page = anon.get("/setup/codes")
        assert "Email these to me" not in page.text
        cfg.communication.email_gateway.smtp_server = "smtp.real.local"
        page2 = anon.get("/setup/codes")
        assert "Email these to me" in page2.text

    def test_email_button_sends_all_codes_through_the_mailer(
        self, anon, fresh_store, monkeypatch
    ):
        cfg, store, _display = fresh_store
        self._create_owner(anon, store)
        cfg.communication.email_gateway.smtp_server = "smtp.real.local"
        sent = {}

        async def fake_send_email(gateway, to, subject, body):
            sent["to"] = to
            sent["body"] = body
            return True

        monkeypatch.setattr(routes, "send_email", fake_send_email)
        page = anon.get("/setup/codes")
        codes = re.findall(r"\b\d{8}\b", page.text)
        resp = anon.post("/setup/codes/email", data={})
        assert resp.status_code == 200
        assert sent["to"] == "ada@example.com"
        for code in codes:
            assert code in sent["body"]

    def test_email_button_errors_without_crashing_when_gateway_unconfigured(
        self, anon, fresh_store
    ):
        _cfg, store, _display = fresh_store
        self._create_owner(anon, store)
        anon.get("/setup/codes")
        resp = anon.post("/setup/codes/email", data={})
        assert resp.status_code == 200
        assert "not available" in resp.text.lower()

    def test_email_send_failure_is_an_error_not_a_crash(
        self, anon, fresh_store, monkeypatch
    ):
        cfg, store, _display = fresh_store
        self._create_owner(anon, store)
        cfg.communication.email_gateway.smtp_server = "smtp.real.local"

        async def fake_send_email(gateway, to, subject, body):
            return False

        monkeypatch.setattr(routes, "send_email", fake_send_email)
        anon.get("/setup/codes")
        resp = anon.post("/setup/codes/email", data={})
        assert resp.status_code == 200
        assert "error" in resp.text.lower() or "not" in resp.text.lower()

    def test_tech_cannot_reach_codes_page(self, anon, fresh_store):
        _cfg, store, _display = fresh_store
        self._create_owner(anon, store)
        tech = store.create_user("Tech", "tech@example.com", Role.tech, "1234")
        tech_client = sign_in(store, tech)
        resp = tech_client.get("/setup/codes")
        assert resp.status_code == 403
        tech_client.close()


class TestUserManagement:
    """§4.1: a secretary manages everyone except the owner — every write
    whose target is the owner needs manage_ownership, and a secretary may
    never mint one either."""

    def test_owner_sees_the_list_containing_ada(self, client):
        resp = client.get("/users")
        assert resp.status_code == 200
        assert "Ada" in resp.text

    def test_list_shows_the_unused_code_count(self, client, wired):
        _cfg, _vmc, _inv, store = wired
        store.generate_emergency_codes()
        resp = client.get("/users")
        assert resp.status_code == 200
        assert "20" in resp.text

    @pytest.mark.parametrize("role", [Role.tech, Role.loader])
    def test_tech_and_loader_get_403(self, login_as, role):
        worker = login_as(role)
        resp = worker.get("/users")
        assert resp.status_code == 403

    def test_owner_creates_a_loader(self, client, wired):
        _cfg, _vmc, _inv, store = wired
        resp = client.post(
            "/users/new",
            data={"name": "Lonnie", "email": "", "role": "loader", "pin": "5297"},
        )
        assert resp.status_code == 200
        assert "Lonnie" in resp.text
        created = next(u for u in store.users.values() if u.name == "Lonnie")
        assert created.role == Role.loader

    def test_bad_pin_is_refused_with_the_reason_and_creates_nobody(self, client, wired):
        _cfg, _vmc, _inv, store = wired
        before = len(store.users)
        resp = client.post(
            "/users/new",
            data={"name": "Nope", "email": "", "role": "loader", "pin": "1111"},
        )
        assert resp.status_code == 200
        assert "same digit repeated" in resp.text
        assert len(store.users) == before

    def test_secretary_may_not_create_an_owner(self, login_as, wired):
        _cfg, _vmc, _inv, store = wired
        secretary = login_as(Role.secretary)
        original_owner_id = store.owner().id
        resp = secretary.post(
            "/users/new",
            data={"name": "Usurper", "email": "", "role": "owner", "pin": "5297"},
        )
        assert resp.status_code == 403
        assert store.owner().id == original_owner_id
        assert not any(u.name == "Usurper" for u in store.users.values())

    def test_new_user_form_offers_owner_only_to_manage_ownership(self, login_as):
        owner = login_as(Role.owner)
        secretary = login_as(Role.secretary)
        assert 'value="owner"' in owner.get("/users/new").text
        assert 'value="owner"' not in secretary.get("/users/new").text

    @pytest.mark.parametrize(
        "action,extra",
        [
            ("disable", {}),
            ("enable", {}),
            ("delete", {}),
            ("reset-pin", {"pin": "5297"}),
        ],
    )
    def test_secretary_403_on_owner_targeting_writes(
        self, login_as, wired, action, extra
    ):
        _cfg, _vmc, _inv, store = wired
        secretary = login_as(Role.secretary)
        owner = store.owner()
        resp = secretary.post(f"/users/{owner.id}/{action}", data=extra)
        assert resp.status_code == 403
        refreshed = store.get_user(owner.id)
        assert refreshed is not None
        assert refreshed.disabled is False

    def test_secretary_may_disable_and_reenable_a_loader(self, login_as, wired):
        _cfg, _vmc, _inv, store = wired
        secretary = login_as(Role.secretary)
        loader = store.create_user(
            "Loader One", "loader1@example.com", Role.loader, "5297"
        )
        resp = secretary.post(f"/users/{loader.id}/disable")
        assert resp.status_code == 200
        assert store.get_user(loader.id).disabled is True
        resp = secretary.post(f"/users/{loader.id}/enable")
        assert resp.status_code == 200
        assert store.get_user(loader.id).disabled is False

    def test_disable_ends_the_users_live_sessions(self, login_as, wired):
        _cfg, _vmc, _inv, store = wired
        owner = login_as(Role.owner)
        loader_client, loader = make_client(store, Role.loader, name="Loafer")
        session_id = loader_client.cookies.get(web_auth.SESSION_COOKIE)
        assert store.resolve_session(session_id) is not None
        resp = owner.post(f"/users/{loader.id}/disable")
        assert resp.status_code == 200
        assert store.resolve_session(session_id) is None
        loader_client.close()

    def test_delete_ends_the_users_live_sessions(self, login_as, wired):
        _cfg, _vmc, _inv, store = wired
        owner = login_as(Role.owner)
        loader_client, loader = make_client(store, Role.loader, name="Loafer3")
        session_id = loader_client.cookies.get(web_auth.SESSION_COOKIE)
        resp = owner.post(f"/users/{loader.id}/delete")
        assert resp.status_code == 200
        assert store.resolve_session(session_id) is None
        loader_client.close()

    def test_reset_pin_changes_hash_and_untrusts_every_device(self, login_as, wired):
        _cfg, _vmc, _inv, store = wired
        owner = login_as(Role.owner)
        loader_client, loader = make_client(store, Role.loader, name="Loafer2")
        old_hash = loader.pin_hash
        resp = owner.post(f"/users/{loader.id}/reset-pin", data={"pin": "5297"})
        assert resp.status_code == 200
        updated = store.get_user(loader.id)
        assert updated.pin_hash != old_hash
        assert all(loader.id not in d.trusted_user_ids for d in store.devices.values())
        loader_client.close()

    def test_delete_removes_the_user(self, login_as, wired):
        _cfg, _vmc, _inv, store = wired
        owner = login_as(Role.owner)
        loader = store.create_user("Gone", "gone@example.com", Role.loader, "5297")
        resp = owner.post(f"/users/{loader.id}/delete")
        assert resp.status_code == 200
        assert store.get_user(loader.id) is None

    def test_post_without_htmx_header_is_forbidden(self, client):
        resp = client.post(
            "/users/new",
            headers={"HX-Request": ""},
            data={"name": "X", "email": "", "role": "loader", "pin": "5297"},
        )
        assert resp.status_code == 403


class TestCorruptAccessFile:
    """A corrupt access.json must never silently become an open setup
    wizard (spec §6) — every route answers 503 instead."""

    @pytest.fixture
    def corrupt(self, tmp_path):
        path = tmp_path / "access.json"
        path.write_text("{not valid json", encoding="utf-8")
        store = AccessStore(path=path)
        assert store.corrupt

        routes.set_config_object(ConfigModel())
        routes.set_access_store(store)

        c = TestClient(app, follow_redirects=False)
        yield c

        routes.set_access_store(None)
        c.close()

    @pytest.mark.parametrize("path", ["/", "/setup", "/login", "/status"])
    def test_every_route_serves_503_with_corrupt_in_body(self, corrupt, path):
        resp = corrupt.get(path)
        assert resp.status_code == 503
        assert "corrupt" in resp.text.lower()
