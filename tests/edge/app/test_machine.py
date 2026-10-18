"""Unit tests for ice_edge/app/machine.py's `Machine` composition root (vmc-
reduction plan, Task 5).

These exercise `Machine` directly -- no routes, no real MQTT/health/
availability -- with the same small fake sinks `tests/fakes.py` already
provides, plus a couple of local fakes (`RecordingMqtt`, `FakeSessionStore`)
for the pieces that need a `load()`/recording `register()` those shared
fakes don't offer.
"""

from __future__ import annotations

import asyncio

from ice_common.config_model import ConfigModel, PhysicalDetails, Product
from ice_contracts.board.vending_machine import FaultCode
from ice_edge.app.machine import Machine
from ice_edge.hal.mqtt_inbound import SUBSCRIPTIONS
from ice_edge.sale.vmc import VMC
from ice_edge.ledger.session_store import SessionSnapshot
from tests.dispenser_fixtures import FakeDispatcher, profiles_for
from tests.fakes import FakeAvailability, FakeTaskRunner


class RecordingMqtt:
    """Records every `register()` call, in order, plus every publish."""

    def __init__(self) -> None:
        self.registered: list[tuple[str, object]] = []
        self.published: list[tuple[str, object, bool]] = []

    def register(self, topic, handler) -> None:
        self.registered.append((topic, handler))

    async def publish(self, topic, payload, qos=1, retain=False) -> None:
        self.published.append((topic, payload, retain))


class FakeSessionStore:
    """A `SessionStore` stand-in whose `load()` returns a fixed snapshot --
    `tests.fakes.FakeStore` has no `load()` at all, since the tests it backs
    (test_outputs.py, test_fault_service.py) never need boot recovery."""

    def __init__(self, *, snapshot: SessionSnapshot | None = None) -> None:
        self._snapshot = snapshot
        self.clear_calls = 0
        self.saved: list[SessionSnapshot] = []

    def load(self) -> SessionSnapshot | None:
        return self._snapshot

    def clear(self) -> bool:
        self.clear_calls += 1
        return True

    async def save_async(self, snap: SessionSnapshot) -> None:
        self.saved.append(snap)

    async def clear_async(self) -> bool:
        self.clear_calls += 1
        return True


def _config(*products: Product) -> ConfigModel:
    return ConfigModel(physical=PhysicalDetails(products=list(products)))


class TestConstruction:
    def test_construction_with_no_loop_attached_succeeds(self):
        Machine(ConfigModel())  # must not raise

    def test_construction_builds_a_real_vmc(self):
        machine = Machine(ConfigModel())
        assert isinstance(machine.vmc, VMC)
        assert machine.vmc.state == "idle"

    def test_collaborator_properties_are_never_none(self):
        machine = Machine(ConfigModel())
        assert machine.vmc is not None
        assert machine.tasks is not None
        assert machine.escrow is not None
        assert machine.faults is not None
        assert machine.outputs is not None
        assert machine.refunds is not None
        assert machine.gate is not None
        assert machine.lease is not None
        assert machine.recovery is not None
        assert machine.telemetry is not None

    def test_sink_properties_are_none_until_attached(self):
        machine = Machine(ConfigModel())
        assert machine.session_store is None
        assert machine.mqtt_client is None
        assert machine.command_dispatcher is None
        assert machine.health_monitor is None
        assert machine.availability is None
        assert machine.event_recorder is None
        assert machine.display_controller is None
        assert machine.inventory is None
        assert machine.maintenance_hold is None
        assert machine.subsystem_capabilities == {}

    def test_vmc_and_machine_share_the_same_escrow_ledger(self):
        machine = Machine(ConfigModel())
        assert machine.vmc.escrow is machine.escrow

    def test_vmc_and_machine_share_the_same_fault_service(self):
        machine = Machine(ConfigModel())
        assert machine.vmc.faults is machine.faults

    def test_vmc_and_machine_share_the_same_outputs(self):
        machine = Machine(ConfigModel())
        assert machine.vmc.outputs is machine.outputs

    def test_vmc_and_machine_share_the_same_refund_protocol(self):
        machine = Machine(ConfigModel())
        assert machine.vmc.refunds is machine.refunds

    def test_vmc_and_machine_share_the_same_dispenser_gate(self):
        """Task 6 removed VMC's own public `gate` property (routes/tests
        now read `machine.gate`), so this identity check reaches the
        VMC-private `_gate` directly -- exactly the construction-wiring
        case the guard test's `# private:` escape hatch exists for."""
        machine = Machine(ConfigModel())
        # private: asserts Machine/VMC share one DispenserProfileGate; no
        # public VMC accessor for this exists since Task 6 removed `gate`.
        assert machine.vmc._gate is machine.gate

    def test_vmc_in_maintenance_callable_reflects_machine_lease_hold(self):
        """Task 11 (lease-preconditions): the VMC no longer holds a
        reference to `MaintenanceLease` at all -- only the one-way
        `in_maintenance` boolean callable `Machine` wires in
        (`lambda: self.lease.hold is not None`). This proves that
        callable actually tracks `machine.lease.hold` rather than some
        stale snapshot, by flipping the hold directly (no timers, no
        event loop needed) and reading the callable's result each time."""
        from ice_edge.sale.maintenance_lease import MaintenanceHold

        machine = Machine(ConfigModel())
        # private: asserts VMC's internal `_in_maintenance` callable
        # tracks `machine.lease.hold`; no public VMC accessor for this
        # exists -- `deposit_funds` is its only production reader.
        assert machine.vmc._in_maintenance() is False

        machine.lease.hold = MaintenanceHold(
            holder_user_id="user-1",
            holder_session_id="sess-1",
            started_at=0.0,
            last_activity_at=0.0,
        )
        # private: see the marker above -- same internal callable.
        assert machine.vmc._in_maintenance() is True

        machine.lease.hold = None
        # private: see the marker above -- same internal callable.
        assert machine.vmc._in_maintenance() is False

    def test_no_closure_is_invoked_during_construction(self):
        """Building a Machine must not touch the VMC's FSM/escrow/fault
        state at all -- every cross-referencing closure is deferred."""
        machine = Machine(ConfigModel())
        assert machine.faults.active_faults() == []
        assert machine.escrow.total == 0.0


class TestSetMqttClient:
    def test_registers_the_twelve_topics_in_order_to_bound_methods(self):
        machine = Machine(ConfigModel())
        mqtt = RecordingMqtt()
        machine.set_mqtt_client(mqtt)

        assert machine.mqtt_client is mqtt
        assert len(mqtt.registered) == 12
        for (topic, handler), (expected_topic, owner, name) in zip(
            mqtt.registered, SUBSCRIPTIONS
        ):
            assert topic == expected_topic
            target = machine.vmc if owner == "vmc" else machine.telemetry
            assert handler == getattr(target, name)


class TestSetAvailability:
    def test_attaches_and_publishes_through_outputs(self):
        machine = Machine(ConfigModel())
        av = FakeAvailability()
        machine.set_availability(av)

        assert machine.availability is av
        assert av.states == [machine.vmc.state]
        assert av.active_faults_calls == [[]]
        assert av.publisher == machine.outputs.publish_payment_enable


class TestSetSessionStore:
    def test_open_production_snapshot_raises_pay104(self):
        machine = Machine(ConfigModel())
        snap = SessionSnapshot(state="dispensing", credit_escrow=1.5, is_test=False)
        store = FakeSessionStore(snapshot=snap)

        machine.set_session_store(store)

        assert machine.faults.has(FaultCode.PAY_104)
        assert store.clear_calls == 0  # left in place for the operator

    def test_test_snapshot_logs_and_clears_without_pay104(self):
        machine = Machine(ConfigModel())
        snap = SessionSnapshot(state="dispensing", credit_escrow=1.5, is_test=True)
        store = FakeSessionStore(snapshot=snap)

        machine.set_session_store(store)

        assert not machine.faults.has(FaultCode.PAY_104)
        assert store.clear_calls == 1


class TestCancelPendingTasks:
    def test_cancels_session_timeout(self):
        runner = FakeTaskRunner()
        machine = Machine(ConfigModel(), tasks=runner)
        machine.vmc.deposit_funds(1.0, payment_method="cash")
        assert any(c.label == "session_timeout" for c in runner.scheduled)

        machine.cancel_pending_tasks()

        assert not any(c.label == "session_timeout" for c in runner.scheduled)

    def test_cancels_refund_deadline(self):
        runner = FakeTaskRunner()
        machine = Machine(ConfigModel(), tasks=runner)
        machine.vmc.deposit_funds(1.0, payment_method="cash")
        machine.vmc.request_refund(reason="test")
        assert any(c.label == "refund_deadline" for c in runner.scheduled)

        machine.cancel_pending_tasks()

        assert not any(c.label == "refund_deadline" for c in runner.scheduled)

    async def test_cancels_dispense_timeout(self, tmp_path):
        """Task 8: the dispense-timeout timer is now armed inside
        `DispenseCycle.start` (ice_edge/vend/dispense_cycle.py), which only
        runs once `on_dispense_product` finds a loop attached AND a valid
        dispenser profile for the product -- unlike the pre-Task-8 VMC,
        where `process_payment` armed this timer itself, unconditionally,
        regardless of whether the dispatch below it ever got off the
        ground. A product with no profile wired (as this test used before)
        now never reaches a dispatch at all (CFG-101 fails the vend
        immediately instead), so a profile is wired here to exercise the
        real path this timer actually guards."""
        product = Product(sku="A", price=0.0, slot=1, kind="ice")
        runner = FakeTaskRunner()
        machine = Machine(_config(product), tasks=runner)
        machine.attach_to_loop(asyncio.get_running_loop())
        profiles = profiles_for([product], tmp_path)
        machine.set_dispenser_profiles(profiles)
        machine.set_command_dispatcher(FakeDispatcher())
        vmc = machine.vmc
        vmc.start_interaction()
        vmc.selected_product = product
        vmc.process_payment()
        assert vmc.state == "dispensing"
        await asyncio.sleep(0)  # let the real dispatch actually run
        assert any(c.label == "dispense_timeout" for c in runner.scheduled)

        machine.cancel_pending_tasks()

        assert not any(c.label == "dispense_timeout" for c in runner.scheduled)


def _caps(subsystem: str = "vending") -> dict:
    from ice_contracts.board.vending_machine import CONTRACT_VERSION

    return {
        "subsystem": subsystem,
        "contract_version": CONTRACT_VERSION,
        "brand": "BrandX",
        "model": "VM-1",
        "firmware": "0.1.0",
        "channels": [],
        "commands": [],
    }


class TestTelemetryWiring:
    async def test_bin_half_full_true_clears_ice101_lockout(self):
        machine = Machine(_config(Product(sku="A", price=1.0, slot=1, kind="ice")))
        machine.faults.raise_fault(FaultCode.ICE_101, sku="A")
        assert machine.faults.is_locked("A") is FaultCode.ICE_101

        await machine.telemetry.handle_hardware_io(
            "hardware/io/bin_half_full", {"device": "bin_half_full", "state": True}
        )

        assert machine.faults.is_locked("A") is None

    async def test_bin_half_full_false_keeps_lockout(self):
        machine = Machine(_config(Product(sku="A", price=1.0, slot=1, kind="ice")))
        machine.faults.raise_fault(FaultCode.ICE_101, sku="A")

        await machine.telemetry.handle_hardware_io(
            "hardware/io/bin_half_full", {"device": "bin_half_full", "state": False}
        )

        assert machine.faults.is_locked("A") is FaultCode.ICE_101

    async def test_validated_vending_capabilities_reach_the_gate(self):
        from types import SimpleNamespace

        class Profiles:
            report = SimpleNamespace(file_error=False)

            def __init__(self):
                self.calls = []

            def set_capabilities(self, caps):
                self.calls.append(caps)

        machine = Machine(ConfigModel())
        machine.gate.profiles = Profiles()
        await machine.telemetry.handle_capabilities(
            "capabilities/vending", _caps("vending")
        )
        assert len(machine.gate.profiles.calls) == 1
        assert machine.gate.profiles.calls[0].firmware == "0.1.0"

        # A malformed doc arrives with caps=None and never reaches the gate.
        await machine.telemetry.handle_capabilities(
            "capabilities/vending", {"subsystem": "vending", "bad": 1}
        )
        assert len(machine.gate.profiles.calls) == 1

    async def test_subsystem_capabilities_holds_raw_document(self):
        machine = Machine(ConfigModel())
        doc = _caps("vending")
        await machine.telemetry.handle_capabilities("capabilities/vending", doc)
        assert machine.subsystem_capabilities["vending"] == doc
        assert machine.subsystem_capabilities is machine.telemetry.capabilities

    async def test_capabilities_before_attach_are_replayed_to_new_subscriber(self):
        machine = Machine(ConfigModel())
        doc = _caps("vending")
        await machine.telemetry.handle_capabilities("capabilities/vending", doc)
        probe = []
        machine.telemetry.subscribe_capabilities(lambda s, d, c: probe.append((s, d)))
        assert probe == [("vending", doc)]

    async def test_set_availability_routes_hardware_io_and_payment_status(self):
        machine = Machine(ConfigModel())
        av = FakeAvailability()
        machine.set_availability(av)
        await machine.telemetry.handle_hardware_io(
            "hardware/io/door", {"device": "door", "state": True}
        )
        await machine.telemetry.handle_payment_status(
            "payment/status", {"device": "coin_acceptor", "state": "ready"}
        )
        assert av.hardware_io == [("door", True)]
        assert av.payment_devices == [("coin_acceptor", "ready")]

    async def test_set_availability_twice_leaves_one_subscribed(self):
        machine = Machine(ConfigModel())
        first, second = FakeAvailability(), FakeAvailability()
        machine.set_availability(first)
        machine.set_availability(second)
        await machine.telemetry.handle_hardware_io(
            "hardware/io/door", {"device": "door", "state": True}
        )
        assert first.hardware_io == []
        assert second.hardware_io == [("door", True)]
