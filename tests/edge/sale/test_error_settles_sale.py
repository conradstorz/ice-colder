"""M10: the `error` state settles a running test sale, and an error during the
awaited sale write never refunds a product that already came out."""

import asyncio
import threading

from ice_common.config_model import Product

from ice_edge.ledger.event_recorder import SaleRecordingFailed
from ice_edge.ledger.session_store import SessionStore
from ice_edge.monitoring.inventory_manager import InventoryManager
from tests.edge.sale.test_vmc_flows import (
    RecordingClient,
    make_machine,
    two_products,
)
from tests.fakes import FakeMqtt, FakeTaskRunner, vmc_with_profiles

ICE_1 = Product(sku="ICE-1", slot=0, kind="ice")


async def test_error_during_test_sale_settles_runner_and_releases_lease(tmp_path):
    machine, vmc, _dispatcher = vmc_with_profiles(
        tmp_path, (ICE_1,), tasks=FakeTaskRunner()
    )
    client = FakeMqtt()
    machine.set_mqtt_client(client)
    product = vmc.products[0]
    granted, _ = machine.lease.begin_maintenance("user-1", "sess-1")
    assert granted is True
    task = asyncio.get_running_loop().create_task(
        machine.test_sales.run_test_sale(product.sku)
    )
    await asyncio.sleep(0)
    vmc.process_payment()
    assert vmc.state == "dispensing"

    vmc.error_occurred()
    result = await asyncio.wait_for(task, timeout=5)

    assert result.outcome == "error"
    assert result.fault_code is None
    assert vmc.state == "error"
    assert machine.lease.hold.runs_in_flight == 0
    assert vmc.test_sale_in_progress is False
    assert vmc.credit_escrow == 0.0
    assert not any(t == "cmd/payment/refund" for t, *_ in client.published)
    machine.cancel_pending_tasks()


async def test_error_from_interacting_with_credit_refunds_and_settles_once(tmp_path):
    machine = make_machine(tmp_path=tmp_path)
    vmc = machine.vmc
    machine.attach_to_loop(asyncio.get_running_loop())
    client = RecordingClient()
    machine.set_mqtt_client(client)
    settled = []
    vmc.subscribe_sale_settled(lambda s, o, c: settled.append((o, c)))
    vmc.select_product(vmc.products[0])
    vmc.deposit_funds(1.0, payment_method="cash")
    assert vmc.sale is not None

    vmc.error_occurred()
    await asyncio.sleep(0)

    assert settled == [("error", None)]
    assert len(client.refund_commands()) == 1
    assert client.refund_commands()[0].amount == 1.0


async def test_error_with_no_sale_settles_nothing(tmp_path):
    machine = make_machine(tmp_path=tmp_path)
    vmc = machine.vmc
    machine.attach_to_loop(asyncio.get_running_loop())
    settled = []
    vmc.subscribe_sale_settled(lambda s, o, c: settled.append(o))
    vmc.error_occurred()
    assert settled == []


async def test_error_during_sale_write_does_not_refund_dispensed_product(tmp_path):
    machine = make_machine(2.50, tmp_path)
    vmc = machine.vmc
    machine.attach_to_loop(asyncio.get_running_loop())
    client = RecordingClient()
    machine.set_mqtt_client(client)
    gate = threading.Event()
    sales = []

    class BlockingRecorder:
        def record(self, event_type, value=1.0, metadata=None):
            pass

        def record_sale(self, sku, name, slot, price, methods, ts=None):
            gate.wait(timeout=10)
            sales.append((sku, price, methods))

    machine.set_event_recorder(BlockingRecorder())
    settled = []
    vmc.subscribe_sale_settled(lambda s, o, c: settled.append(o))
    vmc.set_state("interacting_with_user")
    vmc.selected_product = vmc.products[0]
    vmc.deposit_funds(2.50, payment_method="cash_bill")
    vmc.process_payment()
    assert vmc.state == "dispensing"

    report = asyncio.get_running_loop().create_task(
        vmc.on_dispenser_event(
            "hardware/dispenser", {"slot": vmc.products[0].slot, "state": "complete"}
        )
    )
    for _ in range(5):
        await asyncio.sleep(0)  # reach the awaited record_sale thread
    assert not report.done()

    vmc.error_occurred()
    await asyncio.sleep(0)
    assert vmc.state == "error"
    gate.set()
    await asyncio.wait_for(report, timeout=5)
    await asyncio.sleep(0)

    assert vmc.state == "error"
    assert len(sales) == 1
    assert client.refund_commands() == []
    assert vmc.credit_escrow == 0.0
    assert vmc.pending_sale_shares is None
    assert settled == ["dispensed"]


class _GatedRecorder:
    """record_sale blocks on a threading.Event; optionally fails once released."""

    def __init__(self, fail=False):
        self.gate = threading.Event()
        self.sales = []
        self._fail = fail

    def record(self, event_type, value=1.0, metadata=None):
        pass

    def record_sale(self, sku, name, slot, price, methods, ts=None):
        self.gate.wait(timeout=10)
        if self._fail:
            raise SaleRecordingFailed("both writes failed")
        self.sales.append((sku, price, methods))


async def _start_blocked_write(machine, vmc, recorder, product=None):
    """Drive a paid sale to the awaited sale write; return the report task."""
    machine.set_event_recorder(recorder)
    product = product or vmc.products[0]
    vmc.set_state("interacting_with_user")
    vmc.selected_product = product
    vmc.deposit_funds(product.price, payment_method="cash_bill")
    vmc.process_payment()
    assert vmc.state == "dispensing"
    report = asyncio.get_running_loop().create_task(
        vmc.on_dispenser_event(
            "hardware/dispenser", {"slot": product.slot, "state": "complete"}
        )
    )
    for _ in range(5):
        await asyncio.sleep(0)
    assert not report.done()
    return report


async def test_second_error_during_write_does_not_refund_or_settle_twice(tmp_path):
    machine = make_machine(2.50, tmp_path)
    vmc = machine.vmc
    machine.attach_to_loop(asyncio.get_running_loop())
    client = RecordingClient()
    machine.set_mqtt_client(client)
    recorder = _GatedRecorder()
    settled = []
    vmc.subscribe_sale_settled(lambda s, o, c: settled.append(o))
    report = await _start_blocked_write(machine, vmc, recorder)

    vmc.error_occurred()
    vmc.error_occurred()
    await asyncio.sleep(0)
    recorder.gate.set()
    await asyncio.wait_for(report, timeout=5)
    await asyncio.sleep(0)

    assert client.refund_commands() == []
    assert vmc.credit_escrow == 0.0
    assert len(recorder.sales) == 1
    assert settled == ["dispensed"]


async def test_error_during_failed_write_keeps_snapshot_for_pay104_recovery(tmp_path):
    machine = make_machine(2.50, tmp_path)
    vmc = machine.vmc
    machine.attach_to_loop(asyncio.get_running_loop())
    machine.set_mqtt_client(RecordingClient())
    store_path = tmp_path / "session.json"
    machine.set_session_store(SessionStore(store_path))
    recorder = _GatedRecorder(fail=True)
    report = await _start_blocked_write(machine, vmc, recorder)

    vmc.error_occurred()
    await asyncio.sleep(0)
    await machine.drain_persistence()
    recorder.gate.set()
    await asyncio.wait_for(report, timeout=5)
    await machine.drain_persistence()

    assert "PAY-104" in {f["code"] for f in vmc.active_faults()}
    snap = SessionStore(store_path).load()
    assert snap is not None
    assert snap.is_open() is True
    assert snap.pending_sale_shares == {"cash_bill": 2.5}
    pending = machine.pending_sale_for_recovery()
    assert pending is not None
    assert pending["sku"] == "ICE-1"
    assert machine.mark_pending_sale_recorded() is True


async def test_stale_write_does_not_touch_the_next_customers_sale(tmp_path):
    machine = make_machine(tmp_path=tmp_path, products=two_products())
    vmc = machine.vmc
    machine.attach_to_loop(asyncio.get_running_loop())
    machine.set_mqtt_client(RecordingClient())
    recorder = _GatedRecorder()
    settled = []
    vmc.subscribe_sale_settled(lambda s, o, c: settled.append((s.product.sku, o)))
    ice, water = vmc.products[0], vmc.products[1]
    report = await _start_blocked_write(machine, vmc, recorder, ice)

    vmc.error_occurred()
    vmc.reset_state()
    vmc.select_product(water)
    vmc.deposit_funds(water.price, payment_method="cash_bill")
    vmc.process_payment()
    assert vmc.state == "dispensing"
    b_shares = vmc.pending_sale_shares
    assert b_shares

    recorder.gate.set()
    await asyncio.wait_for(report, timeout=5)
    await asyncio.sleep(0)

    assert vmc.state == "dispensing"
    assert vmc.pending_sale_shares == b_shares
    assert vmc.sale is not None and vmc.sale.product.sku == water.sku
    assert settled == [(ice.sku, "dispensed")]
    machine.cancel_pending_tasks()


async def test_error_during_write_still_decrements_inventory(tmp_path):
    machine = make_machine(2.50, tmp_path)
    vmc = machine.vmc
    machine.attach_to_loop(asyncio.get_running_loop())
    machine.set_mqtt_client(RecordingClient())
    vmc.products[0].track_inventory = True
    inventory = InventoryManager(list(vmc.products), path=tmp_path / "inv.json")
    inventory.set_count("ICE-1", 5)
    machine.set_inventory_manager(inventory)
    recorder = _GatedRecorder()
    report = await _start_blocked_write(machine, vmc, recorder)

    vmc.error_occurred()
    recorder.gate.set()
    await asyncio.wait_for(report, timeout=5)
    await machine.drain_persistence()

    assert inventory.get_count("ICE-1") == 4


async def test_repeated_error_from_interacting_settles_once(tmp_path):
    machine = make_machine(tmp_path=tmp_path)
    vmc = machine.vmc
    machine.attach_to_loop(asyncio.get_running_loop())
    machine.set_mqtt_client(RecordingClient())
    settled = []
    vmc.subscribe_sale_settled(lambda s, o, c: settled.append(o))
    vmc.select_product(vmc.products[0])

    vmc.error_occurred()
    vmc.error_occurred()
    await asyncio.sleep(0)

    assert settled == ["error"]
