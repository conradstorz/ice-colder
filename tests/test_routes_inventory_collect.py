"""Tests for cash collection from inventory level (Task 11).

Tests the POST /inventory/collect endpoint with a two-tap confirm,
and the GET /inventory/collect/confirm confirm endpoint.
"""

import asyncio
import sqlite3
import time

import httpx
import pytest

from services.access import Role
from services.event_recorder import EventRecorder
from web_interface import context
from web_interface.server import app


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

    def test_response_preserves_the_swap_anchor_id(
        self, login_as, wired, recorder_with_db
    ):
        """Review round, Finding 3: confirm_button.html always swaps its own
        `<div id="{{ target }}">` via hx-swap="outerHTML", so a success
        partial with no id of its own permanently removes
        `#collect-cash-confirm` from the DOM. The success response must
        carry that id itself so the control remains usable afterwards.
        """
        client = login_as(Role.owner)
        resp = client.post("/inventory/collect", headers={"HX-Request": "true"})
        assert resp.status_code == 200
        assert 'id="collect-cash-confirm"' in resp.text

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
        """The recorded user_id must match the current principal's user id.

        Strengthened (review round, Finding 6): the original assertion was
        only `rows[0][2] is not None`, which any non-null placeholder --
        even a wrong hardcoded string -- would satisfy. Assert the actual
        owner's id from the fixture instead, so this earns its name the
        same way its sibling (test_recorded_user_name_matches_current_
        principal, below) already does.
        """
        _cfg, _vmc, _inv, store = wired
        client = login_as(Role.owner)
        resp = client.post("/inventory/collect", headers={"HX-Request": "true"})
        assert resp.status_code == 200

        rows = _read_cash_collections(recorder_with_db._db_path)
        assert len(rows) == 1
        assert rows[0][2] == store.owner().id

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


async def _async_client_like(sync_client):
    """An httpx.AsyncClient hitting the same in-process ASGI `app`,
    carrying the same session/device cookies as an already-logged-in
    `login_as`/TestClient client -- used where a test needs two real,
    concurrently-awaited requests (TestClient's own calls are each
    synchronous end-to-end and cannot interleave with each other)."""
    transport = httpx.ASGITransport(app=app)
    client = httpx.AsyncClient(transport=transport, base_url="http://testserver")
    client.headers["HX-Request"] = "true"
    for name, value in sync_client.cookies.items():
        client.cookies.set(name, value)
    return client


class TestConcurrentCollectionsShowOwnRow:
    """Finding 1 (CRITICAL, review round): the amount shown to a collector
    must never be a different collection's, even when two `cash_collections`
    rows share an identical `ts` -- reproduced, not theorised: two
    back-to-back `time.time()` calls are identical on this platform's timer
    granularity (probed 1000/1000), so this is a real, not hypothetical,
    collision.
    """

    async def test_concurrent_collections_each_show_their_own_row(
        self, login_as, wired, recorder_with_db, monkeypatch
    ):
        """Two collectors (owner, secretary) both tap Confirm within the
        same timer tick. `time.time()` is frozen for the duration so both
        of the resulting `cash_collections` rows are forced to share one
        `ts` -- the exact condition Finding 1 describes -- and the two
        requests are issued as real, concurrently-awaited coroutines (via
        httpx.AsyncClient/ASGITransport, in-process, same event loop) so
        they can genuinely interleave around the `await
        asyncio.to_thread(...)` points inside the route, the way two
        real HTTP requests from two collectors would.

        One sale exists before either collection. Whichever of the two
        requests the writer thread's single FIFO queue happens to process
        FIRST is -- by definition -- the first collection ever, and must
        see that sale's amount; the other, processed second with nothing
        sold in between, must see 0.0. This gives an unambiguous ground
        truth to check each response against that does not depend on
        which of the two concurrent requests "wins" the race.
        """
        _seed_sales(recorder_with_db, [{"cash_coin": 7.25}])

        frozen_ts = time.time()
        monkeypatch.setattr("services.event_recorder.time.time", lambda: frozen_ts)

        owner_client = login_as(Role.owner)
        secretary_client = login_as(Role.secretary)

        ac_owner = await _async_client_like(owner_client)
        ac_secretary = await _async_client_like(secretary_client)
        try:
            resp_owner, resp_secretary = await asyncio.gather(
                ac_owner.post("/inventory/collect"),
                ac_secretary.post("/inventory/collect"),
            )
        finally:
            await ac_owner.aclose()
            await ac_secretary.aclose()

        assert resp_owner.status_code == 200
        assert resp_secretary.status_code == 200

        rows = _read_cash_collections(recorder_with_db._db_path)
        assert len(rows) == 2
        rows_by_id = sorted(rows, key=lambda r: r[0])
        assert rows_by_id[0][1] == rows_by_id[1][1] == frozen_ts, (
            "test setup requires both rows to share a tied ts"
        )

        first_amount = rows_by_id[0][4]
        second_amount = rows_by_id[1][4]
        # Fixed by writer-thread FIFO processing order, not by which
        # coroutine "wins": whichever job is processed first is the very
        # first collection ever (sees the seeded 7.25); the other sees
        # nothing new since it.
        assert {first_amount, second_amount} == {7.25, 0.0}

        shown_amounts = []
        for resp in (resp_owner, resp_secretary):
            for amount in (first_amount, second_amount):
                if f"${amount:.2f}" in resp.text:
                    shown_amounts.append(amount)
                    break
            else:
                pytest.fail(f"response showed neither ground-truth amount: {resp.text}")

        # Each of the two rows' amounts must be shown to exactly one
        # collector. Under the tied-ts bug, `ORDER BY ts DESC LIMIT 1`
        # returns the same single winner to both reads -- both responses
        # end up showing the SAME amount, so this fails.
        assert sorted(shown_amounts) == sorted([first_amount, second_amount])


class TestDeadWriterThreadDoesNotShowStaleRow:
    """Finding 2 (CRITICAL, review round): if the writer thread has died,
    `flush()` returns immediately without waiting and the just-enqueued job
    is never processed -- the route must not then return 200 with a PRIOR
    collection's row as if it were this request's own.
    """

    def test_dead_writer_thread_does_not_surface_stale_row_as_success(
        self, login_as, wired, recorder_with_db, monkeypatch
    ):
        client = login_as(Role.owner)

        # A genuine first collection creates one real row -- the one a
        # dead writer thread would let a later request wrongly reuse.
        resp1 = client.post("/inventory/collect", headers={"HX-Request": "true"})
        assert resp1.status_code == 200
        rows = _read_cash_collections(recorder_with_db._db_path)
        assert len(rows) == 1

        # Simulate a dead writer thread exactly the way flush()'s own
        # docstring describes it: the enqueued job is never processed
        # (record_cash_collection becomes a black hole) and flush()
        # returns immediately without waiting, as it does once
        # `self._writer.is_alive()` is False.
        monkeypatch.setattr(
            recorder_with_db, "record_cash_collection", lambda *a, **kw: None
        )
        monkeypatch.setattr(recorder_with_db, "flush", lambda *a, **kw: None)

        resp2 = client.post("/inventory/collect", headers={"HX-Request": "true"})

        # No new row was written -- the route must not claim success.
        rows = _read_cash_collections(recorder_with_db._db_path)
        assert len(rows) == 1, "no new row should have been written"
        assert resp2.status_code == 500, (
            "a dead writer thread must surface a failure, never a stale row"
        )
