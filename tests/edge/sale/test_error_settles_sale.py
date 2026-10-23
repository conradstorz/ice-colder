"""M10: the `error` state settles a running test sale, and an error during the
awaited sale write never refunds a product that already came out."""

import asyncio
import threading

from ice_common.config_model import Product

from tests.edge.sale.test_vmc_flows import RecordingClient, make_machine
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
