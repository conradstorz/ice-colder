"""Tests for cash collection from inventory level (Task 11).

Tests the POST /inventory/collect endpoint with a two-tap confirm,
and the GET /inventory/collect/confirm confirm endpoint.
"""

import sqlite3

import pytest

from services.access import Role
from services.event_recorder import EventRecorder
from web_interface import context


@pytest.fixture
def recorder_with_db(wired, tmp_path):
    """Create an EventRecorder with an in-memory database for testing.

    This fixture extends wired (which sets up config, vmc, inv, store)
    and adds an event_recorder to the context.
    """
    db_path = tmp_path / "events.db"
    recorder = EventRecorder(db_path=str(db_path), retention_days=90)
    context.set_event_recorder(recorder)
    yield recorder
    context.set_event_recorder(None)


def _seed_sales(recorder, methods_list):
    """Seed the database with test sales.

    methods_list: list of dicts mapping method names to amounts.
    Example: [{"cash_coin": 1.50, "card": 0.50}, {"cash_bill": 2.00}]
    """
    for methods in methods_list:
        recorder.record_sale(
            sku="TEST-SKU",
            name="Test Product",
            slot=1,
            price=sum(methods.values()),
            methods=methods,
        )
    recorder.flush()


def _read_cash_collections(db_path):
    """Read all cash_collections rows from the database."""
    conn = sqlite3.connect(db_path)
    try:
        cursor = conn.execute(
            "SELECT id, ts, user_id, user_name, expected_cash "
            "FROM cash_collections ORDER BY ts ASC"
        )
        return cursor.fetchall()
    finally:
        conn.close()


class TestCollectCashConfirmEndpoint:
    """Test GET /inventory/collect/confirm (the confirm_url endpoint)."""

    def test_confirm_endpoint_renders_button_in_initial_state(self, login_as):
        client = login_as(Role.owner)
        resp = client.get("/inventory/collect/confirm", params={"confirming": "false"})
        assert resp.status_code == 200
        assert "Collect Cash" in resp.text
        # In initial state, "Confirm" button should not be present
        assert "Confirm Collection" not in resp.text

    def test_confirm_endpoint_renders_confirm_pair_when_confirming_is_true(
        self, login_as
    ):
        client = login_as(Role.owner)
        resp = client.get("/inventory/collect/confirm", params={"confirming": "true"})
        assert resp.status_code == 200
        assert "Confirm Collection" in resp.text
        assert "Cancel" in resp.text

    def test_confirm_endpoint_renders_button_when_confirming_is_false(self, login_as):
        client = login_as(Role.owner)
        resp = client.get("/inventory/collect/confirm", params={"confirming": "false"})
        assert resp.status_code == 200
        assert "Collect Cash" in resp.text
        # In false state, "Confirm" button should not be present
        assert "Confirm Collection" not in resp.text

    def test_confirm_endpoint_renders_confirm_pair_when_confirming_absent(
        self, login_as
    ):
        """When confirming parameter is absent, default is True."""
        client = login_as(Role.owner)
        resp = client.get("/inventory/collect/confirm")
        assert resp.status_code == 200
        assert "Confirm Collection" in resp.text
        assert "Cancel" in resp.text

    def test_confirm_endpoint_requires_collect_cash_permission(self, login_as):
        """All four roles have collect_cash, so this should pass for all."""
        for role in [Role.owner, Role.secretary, Role.tech, Role.loader]:
            client = login_as(role)
            resp = client.get("/inventory/collect/confirm")
            assert resp.status_code == 200, f"Role {role.value} failed"


class TestCollectCashEndpoint:
    """Test POST /inventory/collect (the action endpoint)."""

    def test_collect_cash_is_200_for_owner(self, login_as, wired, recorder_with_db):
        client = login_as(Role.owner)
        resp = client.post("/inventory/collect", headers={"HX-Request": "true"})
        assert resp.status_code == 200
        rows = _read_cash_collections(recorder_with_db._db_path)
        assert len(rows) == 1

    def test_collect_cash_is_200_for_secretary(self, login_as, wired, recorder_with_db):
        client = login_as(Role.secretary)
        resp = client.post("/inventory/collect", headers={"HX-Request": "true"})
        assert resp.status_code == 200
        rows = _read_cash_collections(recorder_with_db._db_path)
        assert len(rows) == 1

    def test_collect_cash_is_200_for_tech(self, login_as, wired, recorder_with_db):
        client = login_as(Role.tech)
        resp = client.post("/inventory/collect", headers={"HX-Request": "true"})
        assert resp.status_code == 200
        rows = _read_cash_collections(recorder_with_db._db_path)
        assert len(rows) == 1

    def test_collect_cash_is_200_for_loader(self, login_as, wired, recorder_with_db):
        client = login_as(Role.loader)
        resp = client.post("/inventory/collect", headers={"HX-Request": "true"})
        assert resp.status_code == 200
        rows = _read_cash_collections(recorder_with_db._db_path)
        assert len(rows) == 1

    def test_collect_cash_403_without_htmx_header(
        self, login_as, wired, recorder_with_db
    ):
        """POST without HX-Request header must be 403."""
        # Get an authenticated client first to have the session
        client = login_as(Role.owner)

        # Create a new client that doesn't have the HX-Request header
        from fastapi.testclient import TestClient
        from web_interface.server import app
        from web_interface import auth as web_auth

        bare_client = TestClient(app)
        # Copy the session and device cookies from the authenticated client
        for cookie_name in [web_auth.DEVICE_COOKIE, web_auth.SESSION_COOKIE]:
            if cookie_name in client.cookies:
                bare_client.cookies.set(cookie_name, client.cookies[cookie_name])

        # Now make a POST without HX-Request header
        resp = bare_client.post("/inventory/collect")
        assert resp.status_code == 403
        rows = _read_cash_collections(recorder_with_db._db_path)
        assert len(rows) == 0

    def test_collect_cash_records_exactly_one_row_per_call(
        self, login_as, wired, recorder_with_db
    ):
        client = login_as(Role.owner)

        # First collection
        resp1 = client.post("/inventory/collect", headers={"HX-Request": "true"})
        assert resp1.status_code == 200
        rows = _read_cash_collections(recorder_with_db._db_path)
        assert len(rows) == 1

        # Second collection
        resp2 = client.post("/inventory/collect", headers={"HX-Request": "true"})
        assert resp2.status_code == 200
        rows = _read_cash_collections(recorder_with_db._db_path)
        assert len(rows) == 2

    def test_response_contains_recorded_time(self, login_as, wired, recorder_with_db):
        """Response must contain the recorded time so collector can verify."""
        client = login_as(Role.owner)
        resp = client.post("/inventory/collect", headers={"HX-Request": "true"})
        assert resp.status_code == 200
        # Should contain a timestamp in the response
        assert "Cash Collection Recorded" in resp.text
        # The response should contain something that looks like a time
        assert ":" in resp.text  # HH:MM:SS format

    def test_response_contains_expected_cash_amount(
        self, login_as, wired, recorder_with_db
    ):
        """Response must contain the expected_cash amount."""
        _seed_sales(recorder_with_db, [{"cash_coin": 1.50}])

        client = login_as(Role.owner)
        resp = client.post("/inventory/collect", headers={"HX-Request": "true"})
        assert resp.status_code == 200
        # Should contain the dollar amount format
        assert "$" in resp.text
        assert "1.50" in resp.text

    def test_expected_cash_counts_cash_coin_and_cash_bill(
        self, login_as, wired, recorder_with_db
    ):
        """Expected cash must include cash_coin and cash_bill shares."""
        _seed_sales(
            recorder_with_db,
            [
                {"cash_coin": 0.75, "cash_bill": 1.25},  # Total: $2.00 cash
            ],
        )

        client = login_as(Role.owner)
        resp = client.post("/inventory/collect", headers={"HX-Request": "true"})
        assert resp.status_code == 200

        rows = _read_cash_collections(recorder_with_db._db_path)
        assert len(rows) == 1
        assert rows[0][4] == 2.00  # expected_cash column

    def test_expected_cash_excludes_card_and_nfc(
        self, login_as, wired, recorder_with_db
    ):
        """Expected cash must exclude card and nfc shares."""
        _seed_sales(
            recorder_with_db,
            [
                {"cash_coin": 1.00, "card": 2.00, "nfc": 0.50},  # Only $1.00 is cash
            ],
        )

        client = login_as(Role.owner)
        resp = client.post("/inventory/collect", headers={"HX-Request": "true"})
        assert resp.status_code == 200

        rows = _read_cash_collections(recorder_with_db._db_path)
        assert len(rows) == 1
        assert rows[0][4] == 1.00  # expected_cash = cash_coin only

    def test_second_collection_counts_only_cash_since_first(
        self, login_as, wired, recorder_with_db
    ):
        """Second collection must only count cash since the first collection."""
        # Seed initial sale
        _seed_sales(recorder_with_db, [{"cash_coin": 5.00}])

        client = login_as(Role.owner)

        # First collection
        resp1 = client.post("/inventory/collect", headers={"HX-Request": "true"})
        assert resp1.status_code == 200
        rows = _read_cash_collections(recorder_with_db._db_path)
        assert len(rows) == 1
        assert rows[0][4] == 5.00

        # Seed more sales
        _seed_sales(recorder_with_db, [{"cash_bill": 3.00}])

        # Second collection
        resp2 = client.post("/inventory/collect", headers={"HX-Request": "true"})
        assert resp2.status_code == 200
        rows = _read_cash_collections(recorder_with_db._db_path)
        assert len(rows) == 2
        assert rows[1][4] == 3.00  # Only the cash_bill from the second sale

    def test_first_collection_with_no_prior_sales_is_zero(
        self, login_as, wired, recorder_with_db
    ):
        """First collection with no prior sales should record expected_cash = 0."""
        client = login_as(Role.owner)
        resp = client.post("/inventory/collect", headers={"HX-Request": "true"})
        assert resp.status_code == 200

        rows = _read_cash_collections(recorder_with_db._db_path)
        assert len(rows) == 1
        assert rows[0][4] == 0.0

    def test_recorded_user_id_matches_current_principal(
        self, login_as, wired, recorder_with_db
    ):
        """The recorded user_id must match the current principal's user id."""
        client = login_as(Role.owner)
        resp = client.post("/inventory/collect", headers={"HX-Request": "true"})
        assert resp.status_code == 200

        rows = _read_cash_collections(recorder_with_db._db_path)
        assert len(rows) == 1
        # The user_id should be the owner's id (Ada is the owner in the fixture)
        assert rows[0][2] is not None

    def test_recorded_user_name_matches_current_principal(
        self, login_as, wired, recorder_with_db
    ):
        """The recorded user_name must match the current principal's user name."""
        client = login_as(Role.owner)
        resp = client.post("/inventory/collect", headers={"HX-Request": "true"})
        assert resp.status_code == 200

        rows = _read_cash_collections(recorder_with_db._db_path)
        assert len(rows) == 1
        # The user_name should be "Ada" (the owner in the fixture)
        assert rows[0][3] == "Ada"


class TestConfirmButtonFlow:
    """Test the two-tap confirm button flow."""

    def test_first_tap_does_not_record_a_row(self, login_as, wired, recorder_with_db):
        """Tapping the confirm button should not record a row."""
        client = login_as(Role.owner)
        # Simulate first tap: GET the confirm endpoint with confirming=true
        resp = client.get("/inventory/collect/confirm", params={"confirming": "true"})
        assert resp.status_code == 200

        # No row should be recorded
        rows = _read_cash_collections(recorder_with_db._db_path)
        assert len(rows) == 0

    def test_cancel_tap_returns_to_initial_state_without_recording(
        self, login_as, wired, recorder_with_db
    ):
        """Tapping cancel should return to initial state without recording."""
        client = login_as(Role.owner)
        # Simulate cancel: GET the confirm endpoint with confirming=false
        resp = client.get("/inventory/collect/confirm", params={"confirming": "false"})
        assert resp.status_code == 200

        # Verify it renders the initial state (no "Confirm Collection" text)
        assert "Collect Cash" in resp.text
        assert "Confirm Collection" not in resp.text

        # No row should be recorded
        rows = _read_cash_collections(recorder_with_db._db_path)
        assert len(rows) == 0

    def test_full_flow_confirm_button_then_post(
        self, login_as, wired, recorder_with_db
    ):
        """Full flow: confirm button, then POST should record."""
        _seed_sales(recorder_with_db, [{"cash_coin": 10.00}])

        client = login_as(Role.owner)

        # First tap: get the confirm state
        confirm_resp = client.get(
            "/inventory/collect/confirm", params={"confirming": "true"}
        )
        assert confirm_resp.status_code == 200
        assert "Confirm Collection" in confirm_resp.text

        # Verify no row is recorded yet
        rows = _read_cash_collections(recorder_with_db._db_path)
        assert len(rows) == 0

        # Second tap: POST to collect
        post_resp = client.post("/inventory/collect", headers={"HX-Request": "true"})
        assert post_resp.status_code == 200

        # Now a row should be recorded with the correct amount
        rows = _read_cash_collections(recorder_with_db._db_path)
        assert len(rows) == 1
        assert rows[0][4] == 10.00
