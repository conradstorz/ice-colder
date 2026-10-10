# tests/test_fault_service.py
"""Unit tests for `controller.fault_service.FaultService`, in isolation
from `VMC` -- no FSM. Uses a real `controller.fault_registry.FaultRegistry`
and a real `controller.outputs.StatusOutputs` (sinks attached via the fakes
in `tests/fakes.py`: `FakeMqtt`, `FakeStore`, `FakeHealth`,
`FakeAvailability`), plus a `FakeRecorder` standing in for the event
recorder and plain closures for the other injected callables
(`lease_holder`, `lacks_valid_profile`, `set_transaction_certain`,
`fsm_state`).

Codes used, picked the same way `tests/test_fault_registry.py` does --
from `contracts.vending_machine.FAULT_TABLE` rather than invented ones:
- ICE_301: severity=lockout, scope=product -> locks a sku.
- ICE_101: severity=product_unavailable, scope=product -> locks a sku;
  the one code `clear_ice101_lockouts` clears.
- COM_101/COM_103: severity=product_unavailable/warning, scope=machine --
  the liveness/MQTT-connection machine faults.
- SVC_102/PAY_104: severity=warning, scope=machine -- the two codes with
  their own refusal guards in `clear_fault`.
- CFG_101: severity=product_unavailable, scope=product -- the dispenser
  re-lock re-raised by the CFG-101 guard.
"""

from __future__ import annotations

import asyncio

from contracts.vending_machine import FaultCode
from controller.fault_registry import FaultRegistry
from controller.fault_service import FaultService
from controller.outputs import StatusOutputs
from services.mqtt_messages import VMCAlert
from services.session_store import SessionSnapshot
from tests.fakes import (
    FakeAvailability,
    FakeHealth,
    FakeMqtt,
    FakeStore,
    FakeTaskRunner,
)


class FakeRecorder:
    """Stands in for `services.event_recorder.EventRecorder`: records every
    `record(kind, **kw)` call verbatim."""

    def __init__(self):
        self.rows: list[tuple[str, dict]] = []

    def record(self, kind, **kw):
        self.rows.append((kind, kw))


def not_open_snapshot(state: str | None) -> SessionSnapshot:
    return SessionSnapshot(state=state or "idle", credit_escrow=0.0)


def make_service(
    *,
    names: dict[str, str] | None = None,
    recorder: FakeRecorder | None = None,
    lease_holder=lambda: None,
    lacks_valid_profile=lambda sku: False,
    set_transaction_certain=None,
    fsm_state=lambda: "idle",
    mqtt: FakeMqtt | None = None,
    health: FakeHealth | None = None,
    availability: FakeAvailability | None = None,
    session_store: FakeStore | None = None,
):
    names = names or {}

    def product_name(sku):
        if sku is None:
            return None
        return names.get(sku, sku)

    registry = FaultRegistry(product_name)
    runner = FakeTaskRunner()
    outputs = StatusOutputs(
        snapshot=not_open_snapshot,
        credit_escrow=lambda: 0.0,
        selected_product=lambda: None,
        fsm_state=fsm_state,
        pay104_active=lambda: registry.has(FaultCode.PAY_104),
        tasks=runner,
    )
    if mqtt is not None:
        outputs.attach_mqtt(mqtt)
    if health is not None:
        outputs.attach_health(health)
    if availability is not None:
        outputs.attach_availability(availability)
    if session_store is not None:
        outputs.attach_session_store(session_store)

    recorder = recorder if recorder is not None else FakeRecorder()
    transaction_certain_calls: list[bool] = []
    if set_transaction_certain is None:
        set_transaction_certain = transaction_certain_calls.append

    service = FaultService(
        registry=registry,
        outputs=outputs,
        tasks=runner,
        recorder=lambda: recorder,
        lease_holder=lease_holder,
        lacks_valid_profile=lacks_valid_profile,
        set_transaction_certain=set_transaction_certain,
        fsm_state=fsm_state,
    )
    return service, registry, outputs, runner, recorder, transaction_certain_calls


# --- raise_fault ---


async def test_raise_fault_on_product_code_locks_records_alerts_and_pushes():
    mqtt = FakeMqtt()
    health = FakeHealth()
    availability = FakeAvailability()
    service, registry, _outputs, runner, recorder, _tx = make_service(
        mqtt=mqtt, health=health, availability=availability
    )
    runner.attach(asyncio.get_running_loop())

    service.raise_fault(FaultCode.ICE_301, sku="A1")
    await asyncio.sleep(0)

    assert registry.lockouts == {"A1": FaultCode.ICE_301}
    assert recorder.rows == [
        ("lockout_set", {"metadata": {"code": "ICE-301", "sku": "A1"}})
    ]
    assert len(mqtt.published) == 1
    topic, payload, _retain = mqtt.published[0]
    assert topic == "alerts"
    assert isinstance(payload, VMCAlert)
    assert payload.code is FaultCode.ICE_301
    assert payload.product_sku == "A1"
    assert len(health.raised_alerts) == 1
    key, level, source, message, code, product_sku = health.raised_alerts[0]
    assert key == "ICE-301:A1"
    assert source == "vmc"
    assert code == "ICE-301"
    assert product_sku == "A1"
    assert health.active_faults_calls[-1] == registry.snapshot()
    assert availability.active_faults_calls[-1] == registry.snapshot()


async def test_raise_fault_twice_writes_one_lockout_set():
    service, registry, _outputs, runner, recorder, _tx = make_service()
    runner.attach(asyncio.get_running_loop())

    service.raise_fault(FaultCode.ICE_301, sku="A1")
    service.raise_fault(FaultCode.ICE_301, sku="A1")
    await asyncio.sleep(0)

    assert registry.lockouts == {"A1": FaultCode.ICE_301}
    assert recorder.rows == [
        ("lockout_set", {"metadata": {"code": "ICE-301", "sku": "A1"}})
    ]


# --- clear_fault: product lockout ---


async def test_clear_fault_of_a_lockout_records_and_clears_health_alert():
    health = FakeHealth()
    service, registry, _outputs, runner, recorder, _tx = make_service(health=health)
    runner.attach(asyncio.get_running_loop())
    service.raise_fault(FaultCode.ICE_301, sku="A1")
    await asyncio.sleep(0)
    recorder.rows.clear()

    result = service.clear_fault("A1", by="tech")
    await asyncio.sleep(0)

    assert result is True
    assert registry.is_locked("A1") is None
    assert recorder.rows == [
        (
            "lockout_cleared",
            {"metadata": {"code": "ICE-301", "sku": "A1", "by": "tech"}},
        )
    ]
    assert health.cleared_alerts == ["ICE-301:A1"]


# --- clear_fault: SVC-102 lease guard ---


async def test_clear_fault_svc102_refused_while_lease_held():
    service, registry, _outputs, runner, _recorder, _tx = make_service(
        lease_holder=lambda: "tech-1"
    )
    runner.attach(asyncio.get_running_loop())
    service.raise_fault(FaultCode.SVC_102)
    await asyncio.sleep(0)

    result = service.clear_fault("SVC-102", by="tech")

    assert result is False
    assert registry.has(FaultCode.SVC_102) is True


async def test_clear_fault_svc102_allowed_once_lease_released():
    service, registry, _outputs, runner, _recorder, _tx = make_service(
        lease_holder=lambda: None
    )
    runner.attach(asyncio.get_running_loop())
    service.raise_fault(FaultCode.SVC_102)
    await asyncio.sleep(0)

    result = service.clear_fault("SVC-102", by="tech")

    assert result is True
    assert registry.has(FaultCode.SVC_102) is False


# --- clear_fault: PAY-104 session-evidence guard ---


async def test_clear_fault_pay104_refused_when_evidence_cannot_be_cleared():
    store = FakeStore(clear_result=False)
    service, registry, _outputs, runner, _recorder, tx = make_service(
        session_store=store
    )
    runner.attach(asyncio.get_running_loop())
    service.raise_fault(FaultCode.PAY_104)
    await asyncio.sleep(0)

    result = service.clear_fault("PAY-104", by="admin")

    assert result is False
    assert registry.has(FaultCode.PAY_104) is True
    assert store.clear_calls == 1
    assert tx == []


async def test_clear_fault_pay104_succeeds_and_sets_transaction_certain():
    store = FakeStore(clear_result=True)
    service, registry, _outputs, runner, _recorder, tx = make_service(
        session_store=store
    )
    runner.attach(asyncio.get_running_loop())
    service.raise_fault(FaultCode.PAY_104)
    await asyncio.sleep(0)

    result = service.clear_fault("PAY-104", by="admin")

    assert result is True
    assert registry.has(FaultCode.PAY_104) is False
    assert store.clear_calls == 1
    assert tx == [True]


# --- clear_fault: CFG-101 re-lock guard ---


async def test_clear_fault_relocks_cfg101_when_profile_still_missing():
    service, registry, _outputs, runner, recorder, _tx = make_service(
        lacks_valid_profile=lambda sku: True
    )
    runner.attach(asyncio.get_running_loop())
    service.raise_fault(FaultCode.ICE_301, sku="A1")
    await asyncio.sleep(0)
    recorder.rows.clear()

    result = service.clear_fault("A1", by="tech")
    await asyncio.sleep(0)

    assert result is True
    assert registry.is_locked("A1") is FaultCode.CFG_101
    assert [row for row in recorder.rows if row[0] == "lockout_cleared"] == [
        (
            "lockout_cleared",
            {"metadata": {"code": "ICE-301", "sku": "A1", "by": "tech"}},
        )
    ]
    assert [row for row in recorder.rows if row[0] == "lockout_set"] == [
        ("lockout_set", {"metadata": {"code": "CFG-101", "sku": "A1"}})
    ]


async def test_clear_fault_does_not_relock_when_profile_is_valid():
    service, registry, _outputs, runner, _recorder, _tx = make_service(
        lacks_valid_profile=lambda sku: False
    )
    runner.attach(asyncio.get_running_loop())
    service.raise_fault(FaultCode.ICE_301, sku="A1")
    await asyncio.sleep(0)

    service.clear_fault("A1", by="tech")
    await asyncio.sleep(0)

    assert registry.is_locked("A1") is None


# --- on_subsystem_liveness ---


async def test_on_subsystem_liveness_false_raises_com101_and_tracks_alive():
    availability = FakeAvailability()
    service, registry, _outputs, runner, _recorder, _tx = make_service(
        availability=availability
    )
    runner.attach(asyncio.get_running_loop())

    service.on_subsystem_liveness("vending", False)
    await asyncio.sleep(0)

    assert registry.has(FaultCode.COM_101) is True
    assert availability.subsystem_alive == [("vending", False)]
    assert availability.republish_calls == 0


async def test_on_subsystem_liveness_true_clears_com101():
    availability = FakeAvailability()
    service, registry, _outputs, runner, _recorder, _tx = make_service(
        availability=availability
    )
    runner.attach(asyncio.get_running_loop())
    service.on_subsystem_liveness("vending", False)
    await asyncio.sleep(0)

    service.on_subsystem_liveness("vending", True)
    await asyncio.sleep(0)

    assert registry.has(FaultCode.COM_101) is False
    assert availability.subsystem_alive[-1] == ("vending", True)


async def test_on_subsystem_liveness_mdb_alive_republishes():
    availability = FakeAvailability()
    service, _registry, _outputs, runner, _recorder, _tx = make_service(
        availability=availability
    )
    runner.attach(asyncio.get_running_loop())

    service.on_subsystem_liveness("mdb", True)
    await asyncio.sleep(0)

    assert availability.subsystem_alive == [("mdb", True)]
    assert availability.republish_calls == 1


async def test_on_subsystem_liveness_mdb_not_alive_does_not_republish():
    availability = FakeAvailability()
    service, _registry, _outputs, runner, _recorder, _tx = make_service(
        availability=availability
    )
    runner.attach(asyncio.get_running_loop())

    service.on_subsystem_liveness("mdb", False)
    await asyncio.sleep(0)

    assert availability.republish_calls == 0


# --- on_mqtt_connection ---


async def test_on_mqtt_connection_false_raises_com103():
    availability = FakeAvailability()
    service, registry, _outputs, runner, _recorder, _tx = make_service(
        availability=availability
    )
    runner.attach(asyncio.get_running_loop())

    service.on_mqtt_connection(False)
    await asyncio.sleep(0)

    assert registry.has(FaultCode.COM_103) is True
    assert availability.mqtt_connected == [False]
    assert availability.republish_calls == 0


async def test_on_mqtt_connection_true_clears_republishes_and_pushes_state():
    availability = FakeAvailability()
    health = FakeHealth()
    service, registry, _outputs, runner, _recorder, _tx = make_service(
        availability=availability, health=health, fsm_state=lambda: "idle"
    )
    runner.attach(asyncio.get_running_loop())
    service.on_mqtt_connection(False)
    await asyncio.sleep(0)

    service.on_mqtt_connection(True)
    await asyncio.sleep(0)

    assert registry.has(FaultCode.COM_103) is False
    assert availability.mqtt_connected[-1] is True
    assert availability.republish_calls == 1
    assert health.states[-1] == "idle"


# --- clear_ice101_lockouts ---


async def test_clear_ice101_lockouts_clears_only_ice101():
    service, registry, _outputs, runner, _recorder, _tx = make_service()
    runner.attach(asyncio.get_running_loop())
    service.raise_fault(FaultCode.ICE_101, sku="A1")
    service.raise_fault(FaultCode.ICE_301, sku="B1")
    await asyncio.sleep(0)

    service.clear_ice101_lockouts()
    await asyncio.sleep(0)

    assert registry.is_locked("A1") is None
    assert registry.is_locked("B1") is FaultCode.ICE_301


# --- reads re-exposed from the registry ---


def test_reads_delegate_to_the_registry():
    service, registry, _outputs, _runner, _recorder, _tx = make_service()

    service.raise_fault(FaultCode.ICE_301, sku="A1")

    assert service.is_locked("A1") is FaultCode.ICE_301
    assert service.has(FaultCode.ICE_301) is False  # product-scope, not a machine fault
    assert service.lockouts == registry.lockouts
    assert service.machine_faults == registry.machine_faults
    assert service.active_faults() == registry.snapshot()
    assert service.parse_key("ICE-301") is FaultCode.ICE_301
    assert service.parse_key("not-a-code") is None
