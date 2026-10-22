"""M4/M7: availability is re-checked at payment; one tracked payment retry timer."""

import asyncio

from ice_edge.faults.availability import Availability
from ice_contracts.board.vending_machine import FaultCode
from tests.edge.sale.test_vmc_flows import FakeSoldOutInventory, make_machine
from tests.fakes import FakeTaskRunner


class _BlockedAvailability(Availability):
    """Stands in for a safety/fulfilment row that failed after selection."""

    def product_sellable(self, product, **kwargs):
        return False, ["WTR-104"]


def _labels(runner: FakeTaskRunner, label: str) -> int:
    return len([c for c in runner.scheduled if c.label == label])


def _selected(tmp_path, price: float = 2.50):
    runner = FakeTaskRunner()
    machine = make_machine(price=price, tmp_path=tmp_path, tasks=runner)
    machine.attach_to_loop(asyncio.get_running_loop())
    messages: list[str] = []
    machine.vmc.outputs.set_message_callback(messages.append)
    machine.vmc.select_product(machine.vmc.products[0])
    assert machine.vmc.state == "interacting_with_user"
    return machine, runner, messages


async def test_happy_path_still_dispenses(tmp_path):
    machine, runner, _ = _selected(tmp_path)
    vmc = machine.vmc
    vmc.credit_escrow = 5.00
    runner.fire("process_payment")
    assert vmc.state == "dispensing"
    assert vmc.credit_escrow == 2.50


async def test_machine_fault_after_selection_blocks_payment(tmp_path):
    machine, runner, messages = _selected(tmp_path)
    vmc = machine.vmc
    machine.set_availability(_BlockedAvailability())
    vmc.credit_escrow = 5.00
    runner.fire("process_payment")
    assert vmc.state == "interacting_with_user"
    assert vmc.credit_escrow == 5.00
    assert vmc.selected_product is None
    assert any("unavailable" in m.lower() for m in messages)


async def test_lockout_after_selection_blocks_payment(tmp_path):
    machine, runner, messages = _selected(tmp_path)
    vmc = machine.vmc
    vmc.raise_fault(FaultCode.ICE_101, sku="ICE-1")
    vmc.credit_escrow = 5.00
    runner.fire("process_payment")
    assert vmc.state == "interacting_with_user"
    assert vmc.credit_escrow == 5.00
    assert vmc.selected_product is None
    assert any("unavailable" in m.lower() for m in messages)


async def test_sold_out_after_selection_blocks_payment(tmp_path):
    machine, runner, messages = _selected(tmp_path)
    vmc = machine.vmc
    machine.set_inventory_manager(FakeSoldOutInventory())
    vmc.credit_escrow = 5.00
    runner.fire("process_payment")
    assert vmc.state == "interacting_with_user"
    assert vmc.credit_escrow == 5.00
    assert vmc.selected_product is None
    assert any("sold out" in m.lower() for m in messages)


async def test_repeated_selection_arms_one_payment_timer(tmp_path):
    machine, runner, _ = _selected(tmp_path)
    vmc = machine.vmc
    vmc.select_product(vmc.products[0])
    vmc.select_product(vmc.products[0])
    assert _labels(runner, "process_payment") == 1


async def test_insufficient_funds_rearms_single_timer(tmp_path):
    machine, runner, _ = _selected(tmp_path)
    runner.fire("process_payment")
    assert _labels(runner, "process_payment") == 1


async def test_no_payment_timer_pending_after_vend_leaves_change(tmp_path):
    machine, runner, _ = _selected(tmp_path)
    vmc = machine.vmc
    vmc.credit_escrow = 5.00
    runner.fire("process_payment")
    assert vmc.state == "dispensing"
    assert _labels(runner, "process_payment") == 0
    await vmc.on_dispenser_event("hardware/dispenser", {"slot": 0, "state": "complete"})
    assert vmc.state == "interacting_with_user"
    assert vmc.selected_product is None
    assert _labels(runner, "process_payment") == 0
