"""Unit tests for money-critical VMC flows (no broker required).

These paths were previously only covered by tests/test_integration_e2e.py,
which skips without a live MQTT broker.
"""

import asyncio

import pytest

from config.config_model import ConfigModel, Product
from contracts.vending_machine import DispenserOutcome, FaultCode, OUTCOME_FAULTS
from controller.vmc import VMC
from services.health_monitor import HealthMonitor


def make_vmc(price: float = 2.50) -> VMC:
    cfg = ConfigModel()
    cfg.physical.products = [Product(sku="ICE-1", name="Ice Bag", price=price)]
    return VMC(config=cfg)


class FakeEventRecorder:
    def __init__(self):
        self.events: list[tuple] = []

    def record(self, event_type, value=1.0, metadata=None):
        self.events.append((event_type, value, metadata))


class FakeSoldOutInventory:
    def is_available(self, sku):
        return False

    def is_tracked(self, sku):
        return True

    def decrement(self, sku):
        pass

    def get_count(self, sku):
        return 0


async def test_late_dispenser_fault_after_completed_sale_is_ignored():
    """A duplicate/late 'jam' MQTT message (QoS 0, no dedup) arriving after a
    sale has already completed must not credit a bogus refund or take the VMC
    offline — only a fault reported *during* dispensing is real."""
    vmc = make_vmc(price=2.50)
    vmc.attach_to_loop(asyncio.get_running_loop())
    vmc.machine.set_state("interacting_with_user")
    vmc.selected_product = vmc.products[0]
    vmc.credit_escrow = 2.50

    vmc._process_payment()
    assert vmc.state == "dispensing"

    await vmc._handle_mqtt_dispenser(
        "hardware/dispenser", {"slot": 0, "state": "complete"}
    )
    assert vmc.state == "idle"  # no credit left
    assert vmc.selected_product is None
    assert vmc.credit_escrow == 0.0

    # Late/duplicate jam for the same slot arrives after completion.
    await vmc._handle_mqtt_dispenser("hardware/dispenser", {"slot": 0, "state": "jam"})

    assert vmc.state == "idle"
    assert vmc.credit_escrow == 0.0  # no bogus refund credited
    vmc.cancel_pending_tasks()


async def test_dispenser_jam_with_mismatched_slot_is_ignored():
    """A delayed 'jammed' report for a different slot than the active sale must
    not fault the machine or issue a refund for the wrong product."""
    vmc = make_vmc()
    vmc.attach_to_loop(asyncio.get_running_loop())
    messages: list[str] = []
    vmc.set_message_callback(messages.append)
    vmc.selected_product = vmc.products[0]
    vmc.machine.set_state("dispensing")
    vmc.credit_escrow = 0.0

    other_slot = vmc.products[0].slot + 1
    await vmc._handle_mqtt_dispenser(
        "hardware/dispenser", {"slot": other_slot, "state": "jammed"}
    )

    assert vmc.state == "dispensing"  # unaffected — wrong slot
    assert vmc.credit_escrow == 0.0  # no bogus refund
    assert messages == []


async def test_dispense_complete_with_mismatched_slot_is_ignored():
    """A delayed/duplicate 'complete' for a different slot than the active sale
    must not finalize the sale."""
    vmc = make_vmc(price=2.50)
    vmc.attach_to_loop(asyncio.get_running_loop())
    vmc.machine.set_state("interacting_with_user")
    vmc.selected_product = vmc.products[0]
    vmc.credit_escrow = 2.50

    vmc._process_payment()
    assert vmc.state == "dispensing"

    other_slot = vmc.products[0].slot + 1
    await vmc._handle_mqtt_dispenser(
        "hardware/dispenser", {"slot": other_slot, "state": "complete"}
    )

    assert vmc.state == "dispensing"  # not finished — wrong slot
    assert vmc.selected_product is vmc.products[0]
    vmc.cancel_pending_tasks()


async def test_dispense_complete_with_matching_slot_still_completes():
    vmc = make_vmc(price=2.50)
    vmc.attach_to_loop(asyncio.get_running_loop())
    vmc.machine.set_state("interacting_with_user")
    vmc.selected_product = vmc.products[0]
    vmc.credit_escrow = 2.50

    vmc._process_payment()
    assert vmc.state == "dispensing"

    await vmc._handle_mqtt_dispenser(
        "hardware/dispenser",
        {"slot": vmc.products[0].slot, "state": "complete"},
    )

    assert vmc.state == "idle"  # completed — matching slot
    assert vmc.selected_product is None
    vmc.cancel_pending_tasks()


async def test_dispense_complete_records_event_via_recorder():
    """The VMC — not the recorder listening on hardware/dispenser directly —
    is the source of truth for a 'dispense' event, since only the VMC knows
    whether the completion was actually accepted for the active sale."""
    vmc = make_vmc(price=2.50)
    vmc.attach_to_loop(asyncio.get_running_loop())
    recorder = FakeEventRecorder()
    vmc.set_event_recorder(recorder)
    vmc.machine.set_state("interacting_with_user")
    vmc.selected_product = vmc.products[0]
    vmc.credit_escrow = 2.50

    vmc._process_payment()
    assert vmc.state == "dispensing"

    await vmc._handle_mqtt_dispenser(
        "hardware/dispenser",
        {"slot": vmc.products[0].slot, "state": "complete"},
    )

    assert recorder.events == [("dispense", float(vmc.products[0].slot), None)]
    vmc.cancel_pending_tasks()


async def test_dispense_complete_with_mismatched_slot_does_not_record_event():
    vmc = make_vmc(price=2.50)
    vmc.attach_to_loop(asyncio.get_running_loop())
    recorder = FakeEventRecorder()
    vmc.set_event_recorder(recorder)
    vmc.machine.set_state("interacting_with_user")
    vmc.selected_product = vmc.products[0]
    vmc.credit_escrow = 2.50

    vmc._process_payment()
    assert vmc.state == "dispensing"

    other_slot = vmc.products[0].slot + 1
    await vmc._handle_mqtt_dispenser(
        "hardware/dispenser", {"slot": other_slot, "state": "complete"}
    )

    assert recorder.events == []
    vmc.cancel_pending_tasks()


async def test_dispense_timeout_fallback_does_not_record_event():
    """The 60s hardware-silence fallback completes the transaction without any
    hardware confirmation, so it must not record a 'dispense' event."""
    vmc = make_vmc(price=2.50)
    vmc.attach_to_loop(asyncio.get_running_loop())
    recorder = FakeEventRecorder()
    vmc.set_event_recorder(recorder)
    vmc.machine.set_state("interacting_with_user")
    vmc.selected_product = vmc.products[0]
    vmc.credit_escrow = 2.50

    vmc._process_payment()
    assert vmc.state == "dispensing"

    vmc._finish_dispensing()  # simulate the 60s hardware-silence fallback firing
    assert vmc.state == "idle"
    assert recorder.events == []
    vmc.cancel_pending_tasks()


async def test_session_timeout_refunds_and_returns_to_idle():
    vmc = make_vmc()
    vmc.attach_to_loop(asyncio.get_running_loop())
    vmc._session_timeout_seconds = 0.05
    messages: list[str] = []
    vmc.set_message_callback(messages.append)
    vmc.credit_escrow = 3.00
    vmc.start_interaction()

    await asyncio.sleep(0.3)

    assert vmc.state == "idle"
    assert vmc.credit_escrow == 0.0
    assert any("Refund" in m for m in messages)


async def test_insufficient_funds_waits_without_charging():
    vmc = make_vmc(price=2.50)
    vmc.attach_to_loop(asyncio.get_running_loop())
    messages: list[str] = []
    vmc.set_message_callback(messages.append)
    vmc.machine.set_state("interacting_with_user")
    vmc.selected_product = vmc.products[0]
    vmc.credit_escrow = 1.00

    vmc._process_payment()

    assert vmc.state == "interacting_with_user"
    assert vmc.credit_escrow == 1.00
    assert any("Insufficient funds" in m for m in messages)
    vmc.cancel_pending_tasks()  # cancel the scheduled 5s retry


async def test_sold_out_rejects_selection():
    vmc = make_vmc()
    vmc.attach_to_loop(asyncio.get_running_loop())
    vmc.set_inventory_manager(FakeSoldOutInventory())
    messages: list[str] = []
    vmc.set_message_callback(messages.append)

    vmc.select_product(0)

    assert vmc.state == "idle"
    assert any("sold out" in m.lower() for m in messages)


async def test_sufficient_funds_charges_and_dispenses():
    vmc = make_vmc(price=2.50)
    vmc.attach_to_loop(asyncio.get_running_loop())
    vmc.machine.set_state("interacting_with_user")
    vmc.selected_product = vmc.products[0]
    vmc.credit_escrow = 5.00

    vmc._process_payment()
    assert vmc.state == "dispensing"
    assert vmc.credit_escrow == 2.50

    await vmc._handle_mqtt_dispenser(
        "hardware/dispenser", {"slot": 0, "state": "complete"}
    )
    assert vmc.state == "interacting_with_user"  # credit remains
    vmc.cancel_pending_tasks()


async def test_dispense_timeout_fallback_completes_transaction():
    vmc = make_vmc(price=2.50)
    vmc.attach_to_loop(asyncio.get_running_loop())
    vmc.machine.set_state("interacting_with_user")
    vmc.selected_product = vmc.products[0]
    vmc.credit_escrow = 2.50

    vmc._process_payment()
    assert vmc.state == "dispensing"

    # Simulate the 60s hardware-silence fallback firing
    vmc._finish_dispensing()
    assert vmc.state == "idle"  # no credit left
    vmc.cancel_pending_tasks()


async def test_product_deleted_mid_session_cancels_sale_without_error():
    """Deleting the selected product mid-session should cancel the sale and return
    the VMC to idle — not park it in error, which would take the machine offline
    for every subsequent customer over a benign catalog edit."""
    vmc = make_vmc(price=2.50)
    vmc.attach_to_loop(asyncio.get_running_loop())
    messages: list[str] = []
    vmc.set_message_callback(messages.append)
    vmc.machine.set_state("interacting_with_user")
    vmc.selected_product = vmc.products[0]
    vmc.credit_escrow = 5.00
    vmc.products.clear()  # product deleted via the dashboard mid-session

    vmc._process_payment()

    assert vmc.state == "idle"
    assert vmc.credit_escrow == 0.0  # refunded, same as on_reset/_expire_session
    assert vmc.selected_product is None
    assert any("refund" in m.lower() for m in messages)
    vmc.cancel_pending_tasks()


async def test_sale_cancelled_then_new_sale_succeeds():
    """After a cancelled sale, the VMC should be immediately usable again — no
    admin reset required, unlike a hardware fault that goes through error_occurred."""
    vmc = make_vmc(price=2.50)
    vmc.attach_to_loop(asyncio.get_running_loop())
    vmc.machine.set_state("interacting_with_user")
    vmc.selected_product = vmc.products[0]
    vmc.credit_escrow = 5.00
    vmc.products.clear()  # product deleted via the dashboard mid-session

    vmc._process_payment()
    assert vmc.state == "idle"

    # A new product is configured; a normal sale should work with no admin reset.
    new_product = Product(sku="ICE-2", name="Ice Bag 2", price=2.50)
    vmc.products.append(new_product)
    vmc.select_product(0)
    assert vmc.state == "interacting_with_user"
    assert vmc.selected_product is new_product

    vmc.credit_escrow = 2.50
    vmc._process_payment()
    assert vmc.state == "dispensing"
    vmc.cancel_pending_tasks()


async def test_dispense_uses_product_slot_not_list_index():
    """Regression: deleting product 0 must not shift the slot used to dispense
    the remaining products. The MQTT dispense command must carry the product's
    stable `slot` field, not its current position in the list."""
    cfg = ConfigModel()
    cfg.physical.products = [
        Product(sku="ICE-1", name="Ice", price=1.0, slot=0),
        Product(sku="WATER-1", name="Water", price=1.0, slot=1),
    ]
    vmc = VMC(config=cfg)
    vmc.attach_to_loop(asyncio.get_running_loop())
    published = []

    class FakeMqtt:
        def register(self, *args, **kwargs):
            pass

        async def publish(self, topic, payload):
            published.append((topic, payload))

    vmc.set_mqtt_client(FakeMqtt())

    # Admin deletes the first product from the catalog via the dashboard.
    del vmc.products[0]
    assert vmc.products[0].sku == "WATER-1"

    vmc.selected_product = vmc.products[0]
    vmc.on_dispense_product()
    await asyncio.sleep(0)  # let the fire-and-forget publish task run

    topic, payload = published[-1]
    assert topic == "cmd/dispense"
    assert payload.slot == 1  # WATER-1's stable slot, not its new list index (0)


def make_vmc2() -> VMC:
    cfg = ConfigModel()
    cfg.physical.products = [
        Product(sku="ICE-1", name="Ice Bag", price=2.50, slot=0),
        Product(sku="WATER-1", name="Water", price=1.00, slot=1),
    ]
    return VMC(config=cfg)


class TestFaultRegistry:
    async def test_lockout_fault_locks_product_and_alerts(self):
        vmc = make_vmc2()
        vmc.attach_to_loop(asyncio.get_running_loop())
        rec = FakeEventRecorder()
        vmc.set_event_recorder(rec)
        hm = HealthMonitor()
        vmc.set_health_monitor(hm)

        vmc._raise_fault(FaultCode.ICE_301, sku="ICE-1", outcome="timeout")
        await asyncio.sleep(0)

        assert vmc._lockouts == {"ICE-1": FaultCode.ICE_301}
        assert ("lockout_set", 1.0, {"code": "ICE-301", "sku": "ICE-1"}) in rec.events
        assert "ICE-301:ICE-1" in hm._fired_alerts
        faults = hm.get_summary()["active_faults"]
        assert faults[0]["code"] == "ICE-301" and faults[0]["product"] == "Ice Bag"

    async def test_vend_failed_severity_does_not_lock(self):
        vmc = make_vmc2()
        vmc.attach_to_loop(asyncio.get_running_loop())
        vmc._raise_fault(FaultCode.PAY_102, sku="ICE-1")
        assert vmc._lockouts == {}
        assert vmc.active_faults() == []

    async def test_machine_scope_fault_keyed_by_code(self):
        vmc = make_vmc2()
        vmc.attach_to_loop(asyncio.get_running_loop())
        vmc._raise_fault(FaultCode.PAY_103)
        faults = vmc.active_faults()
        assert faults == [
            {
                "key": "PAY-103",
                "sku": None,
                "product": None,
                "code": "PAY-103",
                "severity": "warning",
                "scope": "machine",
                "description": "Refund not confirmed by payment gateway; needs reconciliation",
            }
        ]
        assert vmc.clear_fault("PAY-103") is True
        assert vmc.active_faults() == []

    async def test_select_locked_product_is_refused(self):
        vmc = make_vmc2()
        vmc.attach_to_loop(asyncio.get_running_loop())
        messages: list[str] = []
        vmc.set_message_callback(messages.append)
        vmc._raise_fault(FaultCode.ICE_301, sku="ICE-1")

        vmc.select_product(0)

        assert vmc.state == "idle"
        assert vmc.selected_product is None
        assert any("ICE-301" in m for m in messages)

    async def test_sellable_products_excludes_locked(self):
        vmc = make_vmc2()
        vmc._raise_fault(FaultCode.ICE_401, sku="ICE-1")
        assert [p.sku for p in vmc._sellable_products()] == ["WATER-1"]

    async def test_clear_fault_records_and_rearms_alert(self):
        vmc = make_vmc2()
        vmc.attach_to_loop(asyncio.get_running_loop())
        rec = FakeEventRecorder()
        vmc.set_event_recorder(rec)
        hm = HealthMonitor()
        vmc.set_health_monitor(hm)
        vmc._raise_fault(FaultCode.ICE_301, sku="ICE-1")
        await asyncio.sleep(0)

        assert vmc.clear_fault("ICE-1") is True
        assert vmc._lockouts == {}
        assert "ICE-301:ICE-1" not in hm._fired_alerts
        assert (
            "lockout_cleared",
            1.0,
            {"code": "ICE-301", "sku": "ICE-1", "by": "admin"},
        ) in rec.events
        assert hm.get_summary()["active_faults"] == []
        assert vmc.clear_fault("ICE-1") is False

    async def test_bin_half_full_auto_clears_ice_101(self):
        vmc = make_vmc2()
        vmc.attach_to_loop(asyncio.get_running_loop())
        rec = FakeEventRecorder()
        vmc.set_event_recorder(rec)
        vmc._raise_fault(FaultCode.ICE_101, sku="ICE-1")
        vmc._raise_fault(FaultCode.ICE_301, sku="WATER-1")

        await vmc._handle_mqtt_hardware_io(
            "hardware/io/bin_half_full", {"device": "bin_half_full", "state": True}
        )

        assert vmc._lockouts == {"WATER-1": FaultCode.ICE_301}
        assert (
            "lockout_cleared",
            1.0,
            {"code": "ICE-101", "sku": "ICE-1", "by": "auto"},
        ) in rec.events

    def test_hardware_io_handler_is_registered(self):
        vmc = make_vmc2()

        class FakeClient:
            def __init__(self):
                self.topics = []

            def register(self, topic, handler):
                self.topics.append(topic)

        client = FakeClient()
        vmc.set_mqtt_client(client)
        assert "hardware/io/+" in client.topics


def _start_dispensing(vmc: VMC, index: int = 0):
    vmc.machine.set_state("interacting_with_user")
    vmc.selected_product = vmc.products[index]
    vmc.credit_escrow = vmc.products[index].price
    vmc._process_payment()
    assert vmc.state == "dispensing"


class TestVendOutcomes:
    @pytest.mark.parametrize("outcome", ["timeout", "jam", "bin_empty", "error"])
    async def test_failure_outcome_restores_credit_and_records(self, outcome):
        vmc = make_vmc2()
        vmc.attach_to_loop(asyncio.get_running_loop())
        rec = FakeEventRecorder()
        vmc.set_event_recorder(rec)
        published: list[tuple[str, object]] = []

        class FakeClient:
            def register(self, *_):
                pass

            async def publish(self, topic, payload):
                published.append((topic, payload))

        vmc.set_mqtt_client(FakeClient())
        _start_dispensing(vmc, 0)
        price = vmc.products[0].price

        await vmc._handle_mqtt_dispenser(
            "hardware/dispenser", {"slot": 0, "state": outcome}
        )
        await asyncio.sleep(0)

        code = OUTCOME_FAULTS[DispenserOutcome(outcome)]
        assert vmc.state == "interacting_with_user"
        assert vmc.credit_escrow == price
        assert vmc.selected_product is None
        assert (
            "vend_failed",
            price,
            {"code": code.value, "sku": "ICE-1", "outcome": outcome},
        ) in rec.events
        assert not any(t == "cmd/payment/refund" for t, _ in published)
        assert vmc._lockouts == {"ICE-1": code}
        assert vmc._dispense_timeout_task is None

    async def test_intermediate_state_does_not_end_sale(self):
        vmc = make_vmc2()
        vmc.attach_to_loop(asyncio.get_running_loop())
        _start_dispensing(vmc, 0)
        await vmc._handle_mqtt_dispenser(
            "hardware/dispenser", {"slot": 0, "state": "fill_complete"}
        )
        assert vmc.state == "dispensing"

    async def test_dispense_timeout_is_a_failed_vend(self):
        vmc = make_vmc2()
        vmc.attach_to_loop(asyncio.get_running_loop())
        vmc._dispense_timeout_seconds = 0.01
        rec = FakeEventRecorder()
        vmc.set_event_recorder(rec)
        _start_dispensing(vmc, 0)

        await asyncio.sleep(0.05)

        assert vmc.state == "interacting_with_user"
        assert vmc.credit_escrow == 2.50
        assert (
            "vend_failed",
            2.50,
            {"code": "PAY-102", "sku": "ICE-1", "outcome": "no_report"},
        ) in rec.events
        assert vmc._lockouts == {}  # PAY-102 is vend_failed severity: no lockout
        assert not any(e[0] == "dispense" for e in rec.events)

    async def test_timeout_seconds_come_from_config(self):
        cfg = ConfigModel()
        cfg.physical.dispense_timeout_seconds = 45.0
        cfg.physical.products = [Product(sku="ICE-1", name="Ice", price=1.0)]
        vmc = VMC(config=cfg)
        assert vmc._dispense_timeout_seconds == 45.0

    async def test_complete_after_failure_is_ignored(self):
        vmc = make_vmc2()
        vmc.attach_to_loop(asyncio.get_running_loop())
        rec = FakeEventRecorder()
        vmc.set_event_recorder(rec)
        _start_dispensing(vmc, 0)
        await vmc._handle_mqtt_dispenser(
            "hardware/dispenser", {"slot": 0, "state": "timeout"}
        )
        await vmc._handle_mqtt_dispenser(
            "hardware/dispenser", {"slot": 0, "state": "complete"}
        )
        assert vmc.state == "interacting_with_user"
        assert not any(e[0] == "dispense" for e in rec.events)
