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
"""

import sqlite3

import pytest

from contracts.vending_machine import FaultCode
from controller.vmc import VMC
from services.access import ROLE_PERMISSIONS, Permission, Role
from services.config_store import add_product
from services.event_recorder import EventRecorder
from services.session_store import SessionStore
from web_interface import routes


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
