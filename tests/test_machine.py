"""Unit tests for controller/machine.py's `Machine` composition root (vmc-
reduction plan, Task 5).

These exercise `Machine` directly -- no routes, no real MQTT/health/
availability -- with the same small fake sinks `tests/fakes.py` already
provides, plus a couple of local fakes (`RecordingMqtt`, `FakeSessionStore`)
for the pieces that need a `load()`/recording `register()` those shared
fakes don't offer.
"""

from __future__ import annotations

from config.config_model import ConfigModel, PhysicalDetails, Product
from contracts.vending_machine import FaultCode
from controller.machine import Machine
from controller.mqtt_inbound import SUBSCRIPTIONS
from controller.vmc import VMC
from services.session_store import SessionSnapshot
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

    def test_vmc_and_machine_share_the_same_maintenance_lease(self):
        """Same reasoning as the gate check above -- VMC's public `lease`
        property was removed in Task 6."""
        machine = Machine(ConfigModel())
        # private: asserts Machine/VMC share one MaintenanceLease; no
        # public VMC accessor for this exists since Task 6 removed `lease`.
        assert machine.vmc._lease is machine.lease

    def test_no_closure_is_invoked_during_construction(self):
        """Building a Machine must not touch the VMC's FSM/escrow/fault
        state at all -- every cross-referencing closure is deferred."""
        machine = Machine(ConfigModel())
        assert machine.faults.active_faults() == []
        assert machine.escrow.total == 0.0


class TestSetMqttClient:
    def test_registers_the_thirteen_topics_in_order_to_bound_methods(self):
        machine = Machine(ConfigModel())
        mqtt = RecordingMqtt()
        machine.set_mqtt_client(mqtt)

        assert machine.mqtt_client is mqtt
        assert len(mqtt.registered) == 13
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

    def test_cancels_dispense_timeout(self):
        product = Product(sku="A", price=0.0, slot=1)
        runner = FakeTaskRunner()
        machine = Machine(_config(product), tasks=runner)
        vmc = machine.vmc
        vmc.start_interaction()
        vmc.selected_product = product
        vmc.process_payment()
        assert vmc.state == "dispensing"
        assert any(c.label == "dispense_timeout" for c in runner.scheduled)

        machine.cancel_pending_tasks()

        assert not any(c.label == "dispense_timeout" for c in runner.scheduled)
