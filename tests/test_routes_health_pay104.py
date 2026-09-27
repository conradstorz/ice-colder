"""Task 14: PAY-104 recovery on Health > Faults -- record or discard a
pending sale left behind by a crash mid-dispense.

Fixtures come from tests/conftest.py (`wired`, `login_as`) for the login
plumbing; this file adds its own `_pay104_setup` helper, which drives the
real deposit -> `_process_payment` pipeline (never a hand-built
SessionSnapshot or a directly-assigned `credit_escrow`) so the resulting
snapshot's `pending_sale_shares` are genuine FIFO shares, then boots a
second VMC against the saved snapshot so PAY-104 is genuinely raised --
mirroring tests/test_vmc_flows.py's
test_pay_104_snapshot_exposes_pending_sale_after_crash_mid_dispense.

Every test asserts a positive control first (the fault is active and
`pending_sale_for_recovery()` reports the pending sale) before asserting
what an action did -- see task-14-brief.md's "beware the fixture that
makes the asserted branch unreachable".

Part 3 review (findings 1 and 2): TestRecordSale's
`test_replay_after_success_writes_no_second_row_and_does_not_error` only
issues its second POST after the first has fully returned -- it proves the
*sequential* replay path is safe (the snapshot is gone, the accessor
returns None) and says nothing about two in-flight requests racing the
check itself. `TestRecordSaleExactlyOnce` below adds the two cases that
test was never able to cover: genuine concurrency (two requests actually
in flight together) and a `clear_fault` failure after a successful write.
"""

import asyncio
import sqlite3
import threading

import httpx
import pytest

from contracts.vending_machine import FaultCode
from controller.vmc import VMC
from services.access import ROLE_PERMISSIONS, Permission, Role
from services.config_store import add_product
from services.event_recorder import EventRecorder
from services.session_store import SessionStore
from web_interface import routes
from web_interface.server import app


def _pay104_setup(
    cfg,
    tmp_path,
    sku="ICE-1",
    deposits=(("cash_bill", 2.00), ("card", 0.50)),
):
    """Build a genuinely open PAY-104 with real pending_sale_shares.

    Drives VMC #1 through the real deposit_funds -> _process_payment path
    (never assigning credit_escrow or pending_sale_shares directly -- that
    would bypass the FIFO ledger's divergence guard and collapse the
    shares to {"unknown": price}, per task-14-brief.md), captures the
    resulting snapshot, saves it synchronously (no event loop is attached
    anywhere in this file -- every VMC codepath touched here guards for a
    missing loop by no-op'ing), then boots VMC #2 against that file so
    `set_session_store` loads an open snapshot and raises PAY-104 exactly
    as a real restart-after-crash would.

    Returns (vmc2, session_store, product, expected_shares, expected_price).
    """
    store_path = tmp_path / "session.json"
    vmc1 = VMC(config=cfg)
    vmc1.machine.set_state("interacting_with_user")
    product = next(p for p in vmc1.products if p.sku == sku)
    vmc1.selected_product = product
    for method, amount in deposits:
        vmc1.deposit_funds(amount, payment_method=method)
    vmc1._process_payment()

    # Positive control on VMC #1's own state, before it is ever discarded:
    # the real FIFO path actually ran (not silently swallowed by
    # _process_payment's @logger.catch()).
    assert vmc1.state == "dispensing", "setup did not reach dispensing"
    assert vmc1.pending_sale_shares, "setup produced no pending_sale_shares"
    expected_shares = dict(vmc1.pending_sale_shares)
    expected_price = round(sum(expected_shares.values()), 2)

    snap = vmc1._snapshot()
    assert snap.is_open()
    SessionStore(store_path).save(snap)

    vmc2 = VMC(config=cfg)
    vmc2.set_session_store(SessionStore(store_path))  # loads -> raises PAY-104

    return vmc2, SessionStore(store_path), product, expected_shares, expected_price


@pytest.fixture
def pay104(wired, tmp_path):
    """Rewires `wired`'s VMC with one carrying a genuinely open PAY-104
    with real pending-sale shares, plus a real EventRecorder (tmp sqlite
    db) wired the same way `context.event_recorder` is in production."""
    cfg, old_vmc, _inv, _store = wired
    add_product(cfg, "ICE-1", "Ice", 2.50)
    vmc2, session_store, product, expected_shares, expected_price = _pay104_setup(
        cfg, tmp_path
    )

    # Positive control: the fault is genuinely active and the accessor
    # genuinely reports the pending sale -- before any test asserts on
    # what an action against it did.
    assert "PAY-104" in {f["code"] for f in vmc2.active_faults()}
    pending = vmc2.pending_sale_for_recovery()
    assert pending is not None
    assert pending["sku"] == "ICE-1"
    assert pending["methods"] == expected_shares
    assert pending["price"] == expected_price

    recorder = EventRecorder(db_path=str(tmp_path / "events.db"))
    routes.set_vmc_instance(vmc2)
    routes.set_event_recorder(recorder)
    try:
        yield {
            "vmc": vmc2,
            "session_path": session_store.path,
            "product": product,
            "shares": expected_shares,
            "price": expected_price,
            "recorder": recorder,
            "db_path": tmp_path / "events.db",
        }
    finally:
        routes.set_event_recorder(None)
        routes.set_vmc_instance(old_vmc)


def _sales_rows(db_path):
    with sqlite3.connect(str(db_path)) as conn:
        return conn.execute(
            "SELECT sku, name, slot, price, methods FROM sales"
        ).fetchall()


class TestFaultsListRendersPendingSale:
    def test_sku_price_and_method_shares_render(self, pay104, login_as):
        client = login_as(Role.tech)
        resp = client.get("/health/faults")
        assert resp.status_code == 200
        text = resp.text
        assert "ICE-1" in text
        assert "$2.50" in text
        assert "cash_bill: $2.00" in text
        assert "card: $0.50" in text

    def test_two_actions_replace_plain_clear(self, pay104, login_as):
        client = login_as(Role.tech)
        resp = client.get("/health/faults")
        assert resp.status_code == 200
        text = resp.text
        assert 'id="record-sale-PAY-104"' in text
        assert 'id="discard-PAY-104"' in text
        assert 'id="clear-PAY-104"' not in text

    def test_no_pending_sale_keeps_plain_clear(self, wired, login_as):
        """A PAY-104 raised with no session store attached at all carries
        no pending sale -- the plain Clear button must still be offered,
        unchanged from part 2."""
        _cfg, vmc, _inv, _store = wired
        vmc._raise_fault(FaultCode.PAY_104, outcome="test, no session store")
        assert vmc.pending_sale_for_recovery() is None  # positive control
        client = login_as(Role.tech)
        resp = client.get("/health/faults")
        assert resp.status_code == 200
        text = resp.text
        assert 'id="clear-PAY-104"' in text
        assert 'id="record-sale-PAY-104"' not in text
        assert 'id="discard-PAY-104"' not in text

    def test_clearing_a_pay104_with_no_pending_sale_writes_no_row(
        self, wired, login_as, tmp_path
    ):
        cfg, vmc, _inv, _store = wired
        recorder = EventRecorder(db_path=str(tmp_path / "events.db"))
        routes.set_event_recorder(recorder)
        try:
            vmc._raise_fault(FaultCode.PAY_104, outcome="test, no session store")
            assert vmc.pending_sale_for_recovery() is None
            client = login_as(Role.tech)
            resp = client.post("/health/faults/PAY-104/clear")
            assert resp.status_code == 200
            assert "PAY-104" not in {f["code"] for f in vmc.active_faults()}
            assert _sales_rows(tmp_path / "events.db") == []
        finally:
            routes.set_event_recorder(None)


class TestRecordSale:
    def test_first_tap_returns_confirm_without_writing_or_clearing(
        self, pay104, login_as
    ):
        client = login_as(Role.tech)
        resp = client.get("/health/faults/PAY-104/record-sale/confirm")
        assert resp.status_code == 200
        assert "Confirm" in resp.text
        vmc = pay104["vmc"]
        assert "PAY-104" in {f["code"] for f in vmc.active_faults()}
        assert _sales_rows(pay104["db_path"]) == []
        assert pay104["session_path"].exists()

    def test_cancel_returns_to_initial_state_having_done_neither(
        self, pay104, login_as
    ):
        client = login_as(Role.tech)
        client.get("/health/faults/PAY-104/record-sale/confirm")  # first tap
        cancel_resp = client.get(
            "/health/faults/PAY-104/record-sale/confirm",
            params={"confirming": "false"},
        )
        assert cancel_resp.status_code == 200
        assert "Confirm" not in cancel_resp.text
        vmc = pay104["vmc"]
        assert "PAY-104" in {f["code"] for f in vmc.active_faults()}
        assert _sales_rows(pay104["db_path"]) == []
        assert pay104["session_path"].exists()

    def test_writes_exactly_one_row_clears_fault_and_removes_snapshot(
        self, pay104, login_as
    ):
        vmc = pay104["vmc"]
        # Positive control, restated right before the write this test is
        # actually about.
        assert "PAY-104" in {f["code"] for f in vmc.active_faults()}
        assert vmc.pending_sale_for_recovery() is not None
        assert pay104["session_path"].exists()

        client = login_as(Role.tech)
        resp = client.post("/health/faults/PAY-104/record-sale")
        assert resp.status_code == 200

        rows = _sales_rows(pay104["db_path"])
        assert len(rows) == 1
        sku, name, slot, price, methods_json = rows[0]
        assert sku == "ICE-1"
        assert name == pay104["product"].name
        assert slot == pay104["product"].slot
        assert price == pay104["price"]
        import json

        assert json.loads(methods_json) == pay104["shares"]

        assert "PAY-104" not in {f["code"] for f in vmc.active_faults()}
        assert not pay104["session_path"].exists()

    def test_replay_after_success_writes_no_second_row_and_does_not_error(
        self, pay104, login_as
    ):
        """Sequential replay only: the second POST is issued after the
        first has fully returned, so this proves a *strictly sequential*
        replay (double GET/refresh, retried request after the response was
        already seen) is safe -- it says nothing about two requests
        actually in flight together. See TestRecordSaleExactlyOnce for
        that (review findings 1 and 2)."""
        client = login_as(Role.tech)

        first = client.post("/health/faults/PAY-104/record-sale")
        assert first.status_code == 200
        assert len(_sales_rows(pay104["db_path"])) == 1  # first call really wrote

        vmc = pay104["vmc"]
        assert "PAY-104" not in {f["code"] for f in vmc.active_faults()}
        assert vmc.pending_sale_for_recovery() is None  # idempotency token is gone

        second = client.post("/health/faults/PAY-104/record-sale")
        assert second.status_code == 200
        assert len(_sales_rows(pay104["db_path"])) == 1  # still just the one row

    @pytest.mark.parametrize("role", [Role.secretary, Role.loader])
    def test_403_for_role_without_clear_faults(self, pay104, login_as, role):
        client = login_as(role)
        assert role not in ROLE_PERMISSIONS or (
            Permission.clear_faults not in ROLE_PERMISSIONS[role]
        )
        resp = client.post("/health/faults/PAY-104/record-sale")
        assert resp.status_code == 403
        assert _sales_rows(pay104["db_path"]) == []

    @pytest.mark.parametrize("role", [Role.owner, Role.tech])
    def test_200_for_role_with_clear_faults(self, pay104, login_as, role):
        assert Permission.clear_faults in ROLE_PERMISSIONS[role]
        client = login_as(role)
        resp = client.post("/health/faults/PAY-104/record-sale")
        assert resp.status_code == 200
        assert len(_sales_rows(pay104["db_path"])) == 1

    def test_403_without_htmx_header(self, pay104, login_as):
        client = login_as(Role.tech)
        resp = client.post(
            "/health/faults/PAY-104/record-sale", headers={"HX-Request": ""}
        )
        assert resp.status_code == 403
        assert _sales_rows(pay104["db_path"]) == []


class TestDiscard:
    def test_first_tap_returns_confirm_without_writing_or_clearing(
        self, pay104, login_as
    ):
        client = login_as(Role.tech)
        resp = client.get("/health/faults/PAY-104/discard/confirm")
        assert resp.status_code == 200
        assert "Confirm" in resp.text
        vmc = pay104["vmc"]
        assert "PAY-104" in {f["code"] for f in vmc.active_faults()}
        assert _sales_rows(pay104["db_path"]) == []
        assert pay104["session_path"].exists()

    def test_cancel_returns_to_initial_state_having_done_neither(
        self, pay104, login_as
    ):
        client = login_as(Role.tech)
        client.get("/health/faults/PAY-104/discard/confirm")  # first tap
        cancel_resp = client.get(
            "/health/faults/PAY-104/discard/confirm", params={"confirming": "false"}
        )
        assert cancel_resp.status_code == 200
        assert "Confirm" not in cancel_resp.text
        vmc = pay104["vmc"]
        assert "PAY-104" in {f["code"] for f in vmc.active_faults()}
        assert _sales_rows(pay104["db_path"]) == []
        assert pay104["session_path"].exists()

    def test_clears_fault_and_removes_snapshot_with_no_row_written(
        self, pay104, login_as
    ):
        vmc = pay104["vmc"]
        assert "PAY-104" in {f["code"] for f in vmc.active_faults()}
        assert vmc.pending_sale_for_recovery() is not None
        assert pay104["session_path"].exists()

        client = login_as(Role.tech)
        resp = client.post("/health/faults/PAY-104/discard")
        assert resp.status_code == 200

        assert "PAY-104" not in {f["code"] for f in vmc.active_faults()}
        assert not pay104["session_path"].exists()
        assert _sales_rows(pay104["db_path"]) == []

    def test_replay_after_discard_does_not_error(self, pay104, login_as):
        client = login_as(Role.tech)
        first = client.post("/health/faults/PAY-104/discard")
        assert first.status_code == 200
        vmc = pay104["vmc"]
        assert "PAY-104" not in {f["code"] for f in vmc.active_faults()}

        second = client.post("/health/faults/PAY-104/discard")
        assert second.status_code == 200
        assert _sales_rows(pay104["db_path"]) == []

    @pytest.mark.parametrize("role", [Role.secretary, Role.loader])
    def test_403_for_role_without_clear_faults(self, pay104, login_as, role):
        client = login_as(role)
        resp = client.post("/health/faults/PAY-104/discard")
        assert resp.status_code == 403

    @pytest.mark.parametrize("role", [Role.owner, Role.tech])
    def test_200_for_role_with_clear_faults(self, pay104, login_as, role):
        client = login_as(role)
        resp = client.post("/health/faults/PAY-104/discard")
        assert resp.status_code == 200
        vmc = pay104["vmc"]
        assert "PAY-104" not in {f["code"] for f in vmc.active_faults()}

    def test_403_without_htmx_header(self, pay104, login_as):
        client = login_as(Role.tech)
        resp = client.post("/health/faults/PAY-104/discard", headers={"HX-Request": ""})
        assert resp.status_code == 403
        vmc = pay104["vmc"]
        assert "PAY-104" in {f["code"] for f in vmc.active_faults()}


class TestRecordSaleExactlyOnce:
    """Part 3 review findings 1 and 2: exactly-once under genuine
    concurrency, and exactly-once when a successful write is followed by a
    `clear_fault` failure. Neither is covered by the sequential replay
    test above."""

    async def test_two_concurrent_requests_write_exactly_one_row(
        self, pay104, login_as
    ):
        """Finding 1: two /record-sale requests actually in flight at the
        same time must still write only one row.

        Two requests are sent together via `asyncio.gather` against the
        real ASGI app over an in-process transport (httpx.ASGITransport),
        so both genuinely run as concurrent asyncio tasks on this test's
        one event loop -- no TestClient here, since starlette's
        TestClient is a synchronous wrapper and cannot express two
        requests in flight at once.

        `record_sale` is stubbed to rendezvous the two calls: the first
        arrival waits (bounded, so the fixed/serialized code cannot hang)
        for a second arrival before actually inserting, which widens the
        race window so the unfixed code reliably shows the bug rather
        than depending on scheduler luck. Against the fixed code, the
        module-level `_pay104_lock` in web_interface/routes/health.py
        never lets the second request's `pending_sale_for_recovery()`
        check run until the first request has fully finished (write +
        clear) inside the lock, so `record_sale` is only ever entered
        once, the rendezvous wait times out harmlessly, and exactly one
        row is written.
        """
        vmc = pay104["vmc"]
        # Positive control, restated right before the concurrent write.
        assert "PAY-104" in {f["code"] for f in vmc.active_faults()}
        assert vmc.pending_sale_for_recovery() is not None
        assert pay104["session_path"].exists()

        recorder = pay104["recorder"]
        real_record_sale = recorder.record_sale

        arrivals = {"n": 0}
        arrivals_lock = threading.Lock()
        second_arrived = threading.Event()

        def rendezvous_record_sale(*args, **kwargs):
            with arrivals_lock:
                arrivals["n"] += 1
                is_first = arrivals["n"] == 1
            if is_first:
                # Give a genuine second racer a chance to also reach this
                # point before writing -- bounded so the fixed/serialized
                # code (where no second call ever arrives) does not hang.
                second_arrived.wait(timeout=0.5)
            else:
                second_arrived.set()
            return real_record_sale(*args, **kwargs)

        recorder.record_sale = rendezvous_record_sale

        client = login_as(Role.tech)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
            cookies=client.cookies,
            headers=client.headers,
        ) as async_client:
            responses = await asyncio.gather(
                async_client.post("/health/faults/PAY-104/record-sale"),
                async_client.post("/health/faults/PAY-104/record-sale"),
            )

        # The row count is the finding this test exists to prove -- assert
        # it first, so an unfixed run fails on the duplicate itself rather
        # than on a downstream symptom (e.g. the loser of the clear_fault
        # race getting a 500 because the other request already cleared
        # the fault out from under it).
        rows = _sales_rows(pay104["db_path"])
        assert len(rows) == 1, (
            f"expected exactly one sale row from two concurrent requests, "
            f"got {len(rows)}"
        )
        # arrivals["n"] may be 1 (fixed: lock serialized the two requests,
        # the second found nothing pending and never called record_sale)
        # or 2 (unfixed: both raced past the check and both wrote).

        for resp in responses:
            assert resp.status_code == 200

    def test_clear_fault_failure_after_successful_write_blocks_replay(
        self, pay104, login_as, monkeypatch
    ):
        """Finding 2: record_sale succeeds but the snapshot cannot be
        removed (SessionStore.clear() fails) -- PAY-104 must legitimately
        stay active, but the sale must never be recorded a second time.

        Simulates the removal failure by monkeypatching the VMC's own
        attached SessionStore's `clear()` (never services/session_store.py
        itself) to always report failure, mirroring `VMC.clear_fault`'s
        own PAY-104 branch. Asserts the row was written exactly once, the
        fault is still active through the same `active_faults()` registry
        the route uses, the on-disk snapshot has been rewritten with its
        pending-sale shares cleared (so the card falls back to the plain
        Clear button) rather than removed outright, and a second
        record-sale attempt writes no second row.
        """
        vmc = pay104["vmc"]
        # Positive control, restated right before the write this test is
        # actually about.
        assert "PAY-104" in {f["code"] for f in vmc.active_faults()}
        assert vmc.pending_sale_for_recovery() is not None
        assert pay104["session_path"].exists()

        monkeypatch.setattr(vmc._session_store, "clear", lambda: False)

        client = login_as(Role.tech)
        first = client.post("/health/faults/PAY-104/record-sale")
        assert first.status_code == 500

        rows = _sales_rows(pay104["db_path"])
        assert len(rows) == 1
        sku, name, slot, price, methods_json = rows[0]
        assert sku == "ICE-1"
        assert price == pay104["price"]

        # The fault, through the same registry the route reads, is still
        # active -- clear_fault's own removal failure left it in place.
        assert "PAY-104" in {f["code"] for f in vmc.active_faults()}

        # The snapshot is still on disk (clear_fault's unlink "failed"),
        # but its pending-sale shares have been durably rewritten away --
        # the accessor now reports no pending sale even with the fault
        # still active.
        assert pay104["session_path"].exists()
        assert vmc.pending_sale_for_recovery() is None
        rewritten = SessionStore(pay104["session_path"]).load()
        assert rewritten is not None
        assert not rewritten.pending_sale_shares

        # A second attempt (operator retry, or a second tap) must be a
        # money-safe no-op, not a second row.
        second = client.post("/health/faults/PAY-104/record-sale")
        assert second.status_code == 200
        assert len(_sales_rows(pay104["db_path"])) == 1


class TestMarkerFailureExactlyOnce:
    """Part 3 review, finding 3: `mark_pending_sale_recorded()`'s own
    `bool` return was discarded at health.py:504-517. It can fail for the
    same underlying I/O reason that made `clear_fault`'s snapshot removal
    fail one line earlier -- both go through the same `SessionStore`
    against the same failing disk. When that happens the pre-fix code:

    * told the operator "retrying will not record the sale again" even
      though the marker was never written;
    * left `pending_sale_for_recovery()` still reporting the original
      sale, since the snapshot's `pending_sale_shares` was never
      rewritten away;
    * had nothing to stop a second POST finding that same pending sale
      and writing a second `sales` row for money already recorded once.

    These tests force *both* `SessionStore.clear()` and `SessionStore.save()`
    to fail (the marker write goes through `save()`) and assert the fixed
    three-part behaviour: an honest message, an in-memory guard that makes
    a same-process retry a safe no-op, and a `DATA-101` alert raised
    through the same fault registry the route already uses elsewhere.
    """

    def test_first_post_records_once_tells_the_truth_and_second_post_writes_no_second_row(
        self, pay104, login_as, monkeypatch
    ):
        vmc = pay104["vmc"]
        # Positive control, restated right before the write this test is
        # actually about -- see task-14-brief.md's "beware the fixture
        # that makes the asserted branch unreachable".
        assert "PAY-104" in {f["code"] for f in vmc.active_faults()}
        assert vmc.pending_sale_for_recovery() is not None
        assert pay104["session_path"].exists()

        # Both disk writes fail -- the realistic pairing: clear_fault's
        # snapshot removal and the marker's rewrite go through the same
        # SessionStore against the same failing disk.
        monkeypatch.setattr(vmc._session_store, "clear", lambda: False)

        def _save_fails(snap):
            raise OSError("simulated disk failure: read-only filesystem")

        monkeypatch.setattr(vmc._session_store, "save", _save_fails)

        client = login_as(Role.tech)

        first = client.post("/health/faults/PAY-104/record-sale")
        assert first.status_code == 500
        # Round 4 (part 3 review): the sale is now recorded with a
        # deterministic, idempotent key (the snapshot's saved_at), so a
        # retry is safe everywhere -- including here, where the marker
        # itself could not be written. The message must say that plainly,
        # not the earlier round's "do NOT retry" (which was wrong even
        # within this same process, since the in-memory guard already
        # made a same-process retry harmless -- see below -- and is now
        # also wrong across a restart, since the database itself is the
        # guarantee).
        assert "retrying will not record the sale again" not in first.text
        assert "do not retry" not in first.text.lower()
        assert "retrying is safe" in first.text.lower()
        assert "recorded" in first.text.lower()

        rows = _sales_rows(pay104["db_path"])
        assert len(rows) == 1, (
            f"expected exactly one row after the first POST, got {len(rows)}"
        )
        sku, name, slot, price, methods_json = rows[0]
        assert sku == "ICE-1"
        assert price == pay104["price"]

        # PAY-104 legitimately still active -- clear_fault's own removal
        # failure left it in place, unchanged from finding 2.
        assert "PAY-104" in {f["code"] for f in vmc.active_faults()}

        # The marker itself could not be persisted -- the accessor still
        # (truthfully, per its own unchanged semantics) reports the
        # pending sale. This is exactly the danger the in-memory guard
        # below must close, since the disk-based idempotency token is
        # gone.
        assert vmc.pending_sale_for_recovery() is not None

        # A second POST in the same process must not write a second row,
        # even though the disk still shows a pending sale.
        second = client.post("/health/faults/PAY-104/record-sale")
        rows_after = _sales_rows(pay104["db_path"])
        assert len(rows_after) == 1, (
            f"expected no second row from a same-process retry, got {len(rows_after)}"
        )
        assert second.status_code == 500
        assert "retrying will not record the sale again" not in second.text

    def test_marker_failure_raises_data_101_visible_through_active_faults(
        self, pay104, login_as, monkeypatch
    ):
        vmc = pay104["vmc"]
        assert "PAY-104" in {f["code"] for f in vmc.active_faults()}
        assert vmc.pending_sale_for_recovery() is not None
        assert "DATA-101" not in {
            f["code"] for f in vmc.active_faults()
        }  # positive control

        monkeypatch.setattr(vmc._session_store, "clear", lambda: False)

        def _save_fails(snap):
            raise OSError("simulated disk failure")

        monkeypatch.setattr(vmc._session_store, "save", _save_fails)

        client = login_as(Role.tech)
        resp = client.post("/health/faults/PAY-104/record-sale")
        assert resp.status_code == 500

        assert "DATA-101" in {f["code"] for f in vmc.active_faults()}


class TestCrossRestartExactlyOnce:
    """Part 3 review, round 4: the previous round's report argued that
    `DATA-101` (raised when the durable marker write fails) was "the
    operator's surviving signal" because it "outlives the process
    boundary the in-memory guard cannot cross". That is false --
    `main.py`'s `reconcile_sales_journal_faults` clears `DATA-101` on
    *every* boot whenever the *sales journal* is drained, and it cannot
    tell "drained because nothing needed recovering" apart from "drained
    because a PAY-104 marker write failed" (recording a sale that
    succeeds, as it does here, never touches the journal at all). So on
    a real restart after this exact failure: `DATA-101` silently
    auto-clears, `PAY-104` re-raises from the still-open snapshot exactly
    as before, and `pending_sale_for_recovery()` still reports the same
    pending sale -- an operator sees an ordinary-looking PAY-104 card
    with a live Record-Sale button and *no* signal that pressing it
    would double-write. The true residual was *any* restart after this
    failure mode, not merely one where the disk stays broken.

    The fix removes the residual at its root: `record_sale` is called
    with a deterministic `ts` (the session snapshot's `saved_at`, fixed
    once at dispense time) and `idempotent=True`, so the row's `(ts,
    sku)` is derivable from the snapshot itself and a second attempt --
    from any process, at any time -- inserts zero rows. This test proves
    the residual was real (it is written to fail against the pre-fix
    code) and proves the fix removes it, with a restart that is genuine:
    a brand new `VMC` (its own, empty `_recorded_pay104_keys`) and a
    brand new `EventRecorder`, both built fresh against the same config,
    the same on-disk session snapshot, and the same sqlite file, then
    wired into `web_interface.routes` exactly as `main.py` wires a
    freshly booted process -- nothing from the pre-restart `VMC` is
    reachable from the route handlers afterward.
    """

    def test_record_sale_after_marker_failure_then_restart_writes_no_second_row(
        self, pay104, wired, login_as, monkeypatch
    ):
        cfg, _pre_wired_vmc, _inv, _store = wired
        vmc = pay104["vmc"]
        session_path = pay104["session_path"]
        db_path = pay104["db_path"]

        # Positive control, restated right before the write this test is
        # actually about.
        assert "PAY-104" in {f["code"] for f in vmc.active_faults()}
        assert vmc.pending_sale_for_recovery() is not None
        assert session_path.exists()

        # Force the same realistic failure pairing as
        # TestMarkerFailureExactlyOnce: the write succeeds, but neither
        # the snapshot removal nor the marker rewrite can persist -- the
        # storage problem that leaves PAY-104 active with a genuinely
        # unresolved on-disk snapshot.
        monkeypatch.setattr(vmc._session_store, "clear", lambda: False)

        def _save_fails(snap):
            raise OSError("simulated disk failure: read-only filesystem")

        monkeypatch.setattr(vmc._session_store, "save", _save_fails)

        client = login_as(Role.tech)
        first = client.post("/health/faults/PAY-104/record-sale")
        assert first.status_code == 500

        rows = _sales_rows(db_path)
        assert len(rows) == 1, (
            f"expected exactly one row after the first POST, got {len(rows)}"
        )

        # --- Simulate a genuine process restart. ---
        #
        # Nothing from the pre-restart process is reused: a fresh VMC
        # starts with an empty `_recorded_pay104_keys` (the in-memory
        # guard) and an empty `_machine_faults` (DATA-101, raised above
        # on the old VMC, does NOT carry over -- matching a real
        # restart, where fault state lives only in memory). It is wired
        # up the same way `_pay104_setup` builds `vmc2` from `vmc1`'s
        # snapshot: a fresh `SessionStore` over the SAME session.json
        # path, `set_session_store` re-loading the still-open snapshot
        # and re-raising PAY-104 from it. A fresh `EventRecorder` opens
        # the SAME sqlite file. `routes.set_vmc_instance` / `routes.
        # set_event_recorder` replace the module-level state the route
        # handlers actually read, so the route has no path back to the
        # old `vmc` object at all from this point on.
        vmc_after_restart = VMC(config=cfg)
        vmc_after_restart.set_session_store(SessionStore(session_path))
        assert "DATA-101" not in {
            f["code"] for f in vmc_after_restart.active_faults()
        }, "a fresh VMC must not inherit the pre-restart process's faults"
        # Positive control on the "restarted" process: this is exactly
        # what an operator's Faults page would show after a real
        # restart in this failure mode -- PAY-104 active, a pending sale
        # still reported, and (per this test's own docstring) no
        # DATA-101 survives to warn that a retry needs care.
        assert "PAY-104" in {f["code"] for f in vmc_after_restart.active_faults()}
        assert vmc_after_restart.pending_sale_for_recovery() is not None

        recorder_after_restart = EventRecorder(db_path=str(db_path))

        routes.set_vmc_instance(vmc_after_restart)
        routes.set_event_recorder(recorder_after_restart)
        try:
            second = client.post("/health/faults/PAY-104/record-sale")
            assert second.status_code == 200

            rows_after_restart = _sales_rows(db_path)
            assert len(rows_after_restart) == 1, (
                "expected exactly one row across a simulated restart, got "
                f"{len(rows_after_restart)}"
            )
        finally:
            routes.set_vmc_instance(vmc)
            routes.set_event_recorder(pay104["recorder"])
