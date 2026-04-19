# Simulator Fault Injection & Autonomous Behaviour Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add fault injection (random + MQTT-triggered), degraded hardware behaviour, structured alert publishing, and richer customer interactions to the three ESP32 simulators.

**Architecture:** Fault infrastructure lives in `ESP32Simulator` base class — `FaultDef` dataclass, `register_fault()`, `_fault_loop()` task, `_publish_alert()`, and inject command subscription. Subclasses register faults in `__init__` and implement `on_activate`/`on_recover` async methods that manipulate their hardware state.

**Tech Stack:** Python 3.12, `aiomqtt`, `asyncio`, `loguru`, existing `simulators/` package structure.

---

## File Structure

| File | Changes |
|------|---------|
| `simulators/base.py` | Add `FaultDef` dataclass, `register_fault()`, `_active_fault_names` property, `_check_recoveries()`, `_try_roll_faults()`, `_activate_fault()`, `_publish_alert()`, `_fault_loop()`, inject command subscription; wire `_fault_loop` into `run()` |
| `simulators/ice_maker.py` | Add `_sensor_by_name()` helper; register 4 faults in `__init__`; update `tick()` to check active faults |
| `simulators/vending_machine.py` | Register 4 faults in `__init__`; update `_run_ice_dispense()` and `_run_water_dispense()` to check active faults; update `_publish_sensors()` for stuck valve; update `_customer_loop()` with 4 new behaviours |
| `simulators/mdb_gateway.py` | Update `PaymentStrategy.pick_method()` to accept `excluded` set; register 4 faults in `__init__`; update `_payment_loop()` to build excluded set from active faults |
| `tests/test_simulator_base.py` | Add `TestFaultRegistration`, `TestFaultLoop`, `TestFaultInject`, `TestPublishAlert` |
| `tests/test_simulator_ice_maker.py` | Add `TestIceMakerFaults` (one test class per fault) |
| `tests/test_simulator_vending.py` | Add `TestVendingFaults`, `TestCustomerBehaviours` |
| `tests/test_simulator_mdb.py` | Add `TestMDBFaults`, `TestPaymentStrategyExclusion` |

---

### Task 1: Base Class — `FaultDef`, `register_fault`, `_active_fault_names`

**Files:**
- Modify: `simulators/base.py`
- Modify: `tests/test_simulator_base.py`

- [ ] **Step 1: Write failing tests**

Add to `tests/test_simulator_base.py` after the existing `TestHADiscovery` class:

```python
import time
from simulators.base import FaultDef


class TestFaultRegistration:
    def test_register_fault_stores_def(self):
        sim = ConcreteSimulator()
        fault = FaultDef(
            name="test_fault",
            category="short",
            probability=1.0,
            on_activate=AsyncMock(),
            on_recover=AsyncMock(),
            message="Test fault",
        )
        sim.register_fault(fault)
        assert len(sim._fault_defs) == 1
        assert sim._fault_defs[0].name == "test_fault"

    def test_register_fault_initialises_state(self):
        sim = ConcreteSimulator()
        fault = FaultDef(
            name="test_fault",
            category="short",
            probability=1.0,
            on_activate=AsyncMock(),
            on_recover=AsyncMock(),
            message="Test fault",
        )
        sim.register_fault(fault)
        assert sim._fault_state["test_fault"] == {"active": False, "recover_at": 0.0}

    def test_active_fault_names_empty_initially(self):
        sim = ConcreteSimulator()
        assert sim._active_fault_names == set()

    def test_active_fault_names_reflects_active_state(self):
        sim = ConcreteSimulator()
        fault = FaultDef(
            name="test_fault",
            category="short",
            probability=1.0,
            on_activate=AsyncMock(),
            on_recover=AsyncMock(),
            message="Test fault",
        )
        sim.register_fault(fault)
        sim._fault_state["test_fault"]["active"] = True
        assert "test_fault" in sim._active_fault_names

    def test_register_multiple_faults(self):
        sim = ConcreteSimulator()
        for name in ("fault_a", "fault_b", "fault_c"):
            sim.register_fault(FaultDef(
                name=name, category="short", probability=0.1,
                on_activate=AsyncMock(), on_recover=AsyncMock(),
                message=f"{name} message",
            ))
        assert len(sim._fault_defs) == 3
        assert set(sim._fault_state.keys()) == {"fault_a", "fault_b", "fault_c"}
```

- [ ] **Step 2: Run tests to confirm failure**

Run: `uv run pytest tests/test_simulator_base.py::TestFaultRegistration -v`
Expected: `ImportError: cannot import name 'FaultDef' from 'simulators.base'`

- [ ] **Step 3: Implement `FaultDef`, `register_fault`, `_active_fault_names`**

Add these imports at the top of `simulators/base.py`, after the existing imports:

```python
import random
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal
```

Add the `FaultDef` dataclass and `RECOVERY_RANGES` dict **before** the `ESP32Simulator` class definition:

```python
RECOVERY_RANGES: dict[str, tuple[float, float]] = {
    "short":  (3 * 60,  10 * 60),
    "medium": (10 * 60, 20 * 60),
    "long":   (20 * 60, 60 * 60),
}

FAULT_LOOP_INTERVAL = 30.0  # seconds between fault probability rolls


@dataclass
class FaultDef:
    name: str
    category: Literal["short", "medium", "long"]
    probability: float         # chance per FAULT_LOOP_INTERVAL tick
    on_activate: Callable      # async fn(client) — enter degraded state
    on_recover: Callable       # async fn(client) — restore normal state
    message: str               # human-readable alert text
    severity: str = "warning"  # "warning" | "critical"
```

In `ESP32Simulator.__init__`, add after `self._subscriptions`:

```python
        self._fault_defs: list[FaultDef] = []
        self._fault_state: dict[str, dict] = {}
```

Add these two methods to `ESP32Simulator`, before `ha_discovery_entities`:

```python
    def register_fault(self, fault: FaultDef) -> None:
        """Register a fault definition. Call from subclass __init__."""
        self._fault_defs.append(fault)
        self._fault_state[fault.name] = {"active": False, "recover_at": 0.0}

    @property
    def _active_fault_names(self) -> set[str]:
        """Return the set of currently active fault names."""
        return {name for name, state in self._fault_state.items() if state["active"]}
```

- [ ] **Step 4: Run tests to confirm pass**

Run: `uv run pytest tests/test_simulator_base.py::TestFaultRegistration -v`
Expected: 5 tests PASS

- [ ] **Step 5: Commit**

```bash
git add simulators/base.py tests/test_simulator_base.py
git commit -m "feat: add FaultDef dataclass and register_fault to ESP32Simulator base"
```

---

### Task 2: Base Class — `_check_recoveries`, `_try_roll_faults`, `_activate_fault`, `_publish_alert`

**Files:**
- Modify: `simulators/base.py`
- Modify: `tests/test_simulator_base.py`

- [ ] **Step 1: Write failing tests**

Add to `tests/test_simulator_base.py` after `TestFaultRegistration`:

```python
class TestFaultLoop:
    @pytest.mark.asyncio
    async def test_activate_fault_sets_active_state(self):
        sim = ConcreteSimulator()
        fault = FaultDef(
            name="test_fault", category="short", probability=1.0,
            on_activate=AsyncMock(), on_recover=AsyncMock(),
            message="Test fault",
        )
        sim.register_fault(fault)
        client = AsyncMock()
        await sim._activate_fault(client, fault)
        assert sim._fault_state["test_fault"]["active"] is True
        assert sim._fault_state["test_fault"]["recover_at"] > time.monotonic()

    @pytest.mark.asyncio
    async def test_activate_fault_calls_on_activate(self):
        sim = ConcreteSimulator()
        on_activate = AsyncMock()
        fault = FaultDef(
            name="test_fault", category="short", probability=1.0,
            on_activate=on_activate, on_recover=AsyncMock(),
            message="Test fault",
        )
        sim.register_fault(fault)
        client = AsyncMock()
        await sim._activate_fault(client, fault)
        on_activate.assert_called_once_with(client)

    @pytest.mark.asyncio
    async def test_try_roll_faults_activates_on_probability_1(self):
        sim = ConcreteSimulator()
        on_activate = AsyncMock()
        fault = FaultDef(
            name="test_fault", category="short", probability=1.0,
            on_activate=on_activate, on_recover=AsyncMock(),
            message="Test fault",
        )
        sim.register_fault(fault)
        client = AsyncMock()
        await sim._try_roll_faults(client)
        assert sim._fault_state["test_fault"]["active"] is True
        on_activate.assert_called_once()

    @pytest.mark.asyncio
    async def test_try_roll_faults_skips_on_probability_0(self):
        sim = ConcreteSimulator()
        on_activate = AsyncMock()
        fault = FaultDef(
            name="test_fault", category="short", probability=0.0,
            on_activate=on_activate, on_recover=AsyncMock(),
            message="Test fault",
        )
        sim.register_fault(fault)
        client = AsyncMock()
        await sim._try_roll_faults(client)
        assert sim._fault_state["test_fault"]["active"] is False
        on_activate.assert_not_called()

    @pytest.mark.asyncio
    async def test_try_roll_faults_only_one_at_a_time(self):
        sim = ConcreteSimulator()
        for name in ("fault_a", "fault_b"):
            sim.register_fault(FaultDef(
                name=name, category="short", probability=1.0,
                on_activate=AsyncMock(), on_recover=AsyncMock(),
                message=f"{name} message",
            ))
        client = AsyncMock()
        await sim._try_roll_faults(client)
        active_count = sum(1 for s in sim._fault_state.values() if s["active"])
        assert active_count == 1

    @pytest.mark.asyncio
    async def test_try_roll_faults_skips_if_fault_already_active(self):
        sim = ConcreteSimulator()
        on_activate = AsyncMock()
        fault = FaultDef(
            name="test_fault", category="short", probability=1.0,
            on_activate=on_activate, on_recover=AsyncMock(),
            message="Test fault",
        )
        sim.register_fault(fault)
        # Pre-activate the fault
        sim._fault_state["test_fault"]["active"] = True
        client = AsyncMock()
        await sim._try_roll_faults(client)
        on_activate.assert_not_called()

    @pytest.mark.asyncio
    async def test_check_recoveries_clears_overdue_fault(self):
        sim = ConcreteSimulator()
        on_recover = AsyncMock()
        fault = FaultDef(
            name="test_fault", category="short", probability=0.0,
            on_activate=AsyncMock(), on_recover=on_recover,
            message="Test fault",
        )
        sim.register_fault(fault)
        sim._fault_state["test_fault"]["active"] = True
        sim._fault_state["test_fault"]["recover_at"] = time.monotonic() - 1.0  # past due
        client = AsyncMock()
        await sim._check_recoveries(client)
        assert sim._fault_state["test_fault"]["active"] is False
        on_recover.assert_called_once_with(client)

    @pytest.mark.asyncio
    async def test_check_recoveries_leaves_non_overdue_fault(self):
        sim = ConcreteSimulator()
        on_recover = AsyncMock()
        fault = FaultDef(
            name="test_fault", category="short", probability=0.0,
            on_activate=AsyncMock(), on_recover=on_recover,
            message="Test fault",
        )
        sim.register_fault(fault)
        sim._fault_state["test_fault"]["active"] = True
        sim._fault_state["test_fault"]["recover_at"] = time.monotonic() + 9999.0
        client = AsyncMock()
        await sim._check_recoveries(client)
        assert sim._fault_state["test_fault"]["active"] is True
        on_recover.assert_not_called()

    @pytest.mark.asyncio
    async def test_publish_alert_active_includes_recover_in(self):
        sim = ConcreteSimulator(machine_id="vmc-0001")
        fault = FaultDef(
            name="test_fault", category="short", probability=1.0,
            on_activate=AsyncMock(), on_recover=AsyncMock(),
            message="Test fault message",
            severity="warning",
        )
        client = AsyncMock()
        await sim._publish_alert(client, fault, "active", recover_in=300.0)
        client.publish.assert_called_once()
        topic, payload_str = client.publish.call_args[0]
        assert topic == "vmc/vmc-0001/alert/test_subsystem"
        payload = json.loads(payload_str)
        assert payload["subsystem"] == "test_subsystem"
        assert payload["fault"] == "test_fault"
        assert payload["status"] == "active"
        assert payload["message"] == "Test fault message"
        assert payload["severity"] == "warning"
        assert payload["recover_in_seconds"] == 300

    @pytest.mark.asyncio
    async def test_publish_alert_cleared_omits_recover_in(self):
        sim = ConcreteSimulator(machine_id="vmc-0001")
        fault = FaultDef(
            name="test_fault", category="short", probability=1.0,
            on_activate=AsyncMock(), on_recover=AsyncMock(),
            message="Test fault message",
        )
        client = AsyncMock()
        await sim._publish_alert(client, fault, "cleared")
        payload = json.loads(client.publish.call_args[0][1])
        assert "recover_in_seconds" not in payload
        assert payload["status"] == "cleared"
```

- [ ] **Step 2: Run tests to confirm failure**

Run: `uv run pytest tests/test_simulator_base.py::TestFaultLoop -v`
Expected: `AttributeError: 'ConcreteSimulator' object has no attribute '_activate_fault'`

- [ ] **Step 3: Implement `_activate_fault`, `_check_recoveries`, `_try_roll_faults`, `_publish_alert`**

Add these four methods to `ESP32Simulator` in `simulators/base.py`, after `_active_fault_names`:

```python
    async def _activate_fault(
        self,
        client: aiomqtt.Client,
        fault: FaultDef,
        recover_in: float | None = None,
    ) -> None:
        """Activate a fault: set state, call on_activate, publish alert."""
        if recover_in is None:
            lo, hi = RECOVERY_RANGES[fault.category]
            recover_in = random.uniform(lo, hi)
        self._fault_state[fault.name]["active"] = True
        self._fault_state[fault.name]["recover_at"] = time.monotonic() + recover_in
        await fault.on_activate(client)
        await self._publish_alert(client, fault, "active", recover_in=recover_in)
        logger.warning(
            f"[{self.subsystem_name}] Fault activated: {fault.name} "
            f"(recover in {recover_in / 60:.1f} min)"
        )

    async def _check_recoveries(self, client: aiomqtt.Client) -> None:
        """Clear any faults whose recovery timer has elapsed."""
        now = time.monotonic()
        for fault in self._fault_defs:
            state = self._fault_state[fault.name]
            if state["active"] and now >= state["recover_at"]:
                state["active"] = False
                await fault.on_recover(client)
                await self._publish_alert(client, fault, "cleared")
                logger.info(f"[{self.subsystem_name}] Fault cleared: {fault.name}")

    async def _try_roll_faults(self, client: aiomqtt.Client) -> None:
        """Roll for new faults if none are currently active."""
        if any(s["active"] for s in self._fault_state.values()):
            return
        for fault in self._fault_defs:
            if random.random() < fault.probability:
                await self._activate_fault(client, fault)
                break  # one fault at a time

    async def _publish_alert(
        self,
        client: aiomqtt.Client,
        fault: FaultDef,
        status: str,
        recover_in: float | None = None,
    ) -> None:
        """Publish a structured alert to vmc/{machine_id}/alert/{subsystem}."""
        topic = f"{self.topic_prefix}/alert/{self.subsystem_name}"
        payload: dict = {
            "subsystem": self.subsystem_name,
            "fault": fault.name,
            "status": status,
            "message": fault.message,
            "severity": fault.severity,
        }
        if recover_in is not None:
            payload["recover_in_seconds"] = int(recover_in)
        await client.publish(topic, json.dumps(payload))
        logger.debug(f"[{self.subsystem_name}] Alert: {fault.name} {status}")
```

- [ ] **Step 4: Run tests to confirm pass**

Run: `uv run pytest tests/test_simulator_base.py::TestFaultLoop -v`
Expected: 11 tests PASS

- [ ] **Step 5: Commit**

```bash
git add simulators/base.py tests/test_simulator_base.py
git commit -m "feat: add _activate_fault, _check_recoveries, _try_roll_faults, _publish_alert to base"
```

---

### Task 3: Base Class — `_fault_loop`, inject command, wire into `run()`

**Files:**
- Modify: `simulators/base.py`
- Modify: `tests/test_simulator_base.py`

- [ ] **Step 1: Write failing tests**

Add to `tests/test_simulator_base.py` after `TestFaultLoop`:

```python
class TestFaultInject:
    @pytest.mark.asyncio
    async def test_handle_inject_activates_named_fault(self):
        sim = ConcreteSimulator()
        on_activate = AsyncMock()
        fault = FaultDef(
            name="test_fault", category="short", probability=0.0,
            on_activate=on_activate, on_recover=AsyncMock(),
            message="Test fault",
        )
        sim.register_fault(fault)
        client = AsyncMock()
        await sim._handle_inject_command(client, {"fault": "test_fault"})
        assert sim._fault_state["test_fault"]["active"] is True
        on_activate.assert_called_once_with(client)

    @pytest.mark.asyncio
    async def test_handle_inject_ignores_unknown_fault(self):
        sim = ConcreteSimulator()
        client = AsyncMock()
        # Should not raise
        await sim._handle_inject_command(client, {"fault": "nonexistent_fault"})

    @pytest.mark.asyncio
    async def test_handle_inject_ignored_if_fault_already_active(self):
        sim = ConcreteSimulator()
        on_activate = AsyncMock()
        fault = FaultDef(
            name="test_fault", category="short", probability=0.0,
            on_activate=on_activate, on_recover=AsyncMock(),
            message="Test fault",
        )
        sim.register_fault(fault)
        sim._fault_state["test_fault"]["active"] = True
        client = AsyncMock()
        await sim._handle_inject_command(client, {"fault": "test_fault"})
        on_activate.assert_not_called()

    @pytest.mark.asyncio
    async def test_handle_inject_ignored_if_different_fault_active(self):
        sim = ConcreteSimulator()
        on_activate_b = AsyncMock()
        for name, activate in [("fault_a", AsyncMock()), ("fault_b", on_activate_b)]:
            sim.register_fault(FaultDef(
                name=name, category="short", probability=0.0,
                on_activate=activate, on_recover=AsyncMock(),
                message=f"{name} message",
            ))
        # fault_a already active
        sim._fault_state["fault_a"]["active"] = True
        client = AsyncMock()
        await sim._handle_inject_command(client, {"fault": "fault_b"})
        on_activate_b.assert_not_called()
```

- [ ] **Step 2: Run tests to confirm failure**

Run: `uv run pytest tests/test_simulator_base.py::TestFaultInject -v`
Expected: `AttributeError: 'ConcreteSimulator' object has no attribute '_handle_inject_command'`

- [ ] **Step 3: Implement `_handle_inject_command` and `_fault_loop`**

Add `_handle_inject_command` to `ESP32Simulator` in `simulators/base.py`, after `_publish_alert`:

```python
    async def _handle_inject_command(self, client: aiomqtt.Client, data: dict) -> None:
        """Process a manual fault inject command payload."""
        name = data.get("fault")
        if not name:
            return
        fault = next((f for f in self._fault_defs if f.name == name), None)
        if not fault:
            logger.warning(f"[{self.subsystem_name}] Inject: unknown fault '{name}'")
            return
        if any(s["active"] for s in self._fault_state.values()):
            logger.info(f"[{self.subsystem_name}] Inject ignored: a fault is already active")
            return
        await self._activate_fault(client, fault)
        logger.info(f"[{self.subsystem_name}] Fault injected: {name}")

    async def _fault_loop(self, client: aiomqtt.Client) -> None:
        """Periodic task: check recoveries, roll for new faults, handle inject commands."""
        inject_topic = f"{self.topic_prefix}/cmd/sim/inject_fault"
        inject_queue = await self.subscribe(client, inject_topic)
        logger.info(f"[{self.subsystem_name}] Fault loop started, inject topic: {inject_topic}")
        while True:
            await asyncio.sleep(FAULT_LOOP_INTERVAL)
            # Drain inject commands first
            while not inject_queue.empty():
                _, data = inject_queue.get_nowait()
                await self._handle_inject_command(client, data)
            # Check if any active faults have recovered
            await self._check_recoveries(client)
            # Roll for new faults
            await self._try_roll_faults(client)
```

Wire `_fault_loop` into `run()`. In `simulators/base.py`, find the `TaskGroup` block inside `run()` and add the fault loop task:

```python
                    async with asyncio.TaskGroup() as tg:
                        tg.create_task(self._heartbeat_loop(client))
                        tg.create_task(self.run_simulation(client))
                        tg.create_task(self._message_dispatcher(client))
                        tg.create_task(self._fault_loop(client))
```

- [ ] **Step 4: Run tests to confirm pass**

Run: `uv run pytest tests/test_simulator_base.py -v`
Expected: All existing + new tests PASS

- [ ] **Step 5: Commit**

```bash
git add simulators/base.py tests/test_simulator_base.py
git commit -m "feat: add _fault_loop with inject command handling; wire into run()"
```

---

### Task 4: Ice Maker Faults

**Files:**
- Modify: `simulators/ice_maker.py`
- Modify: `tests/test_simulator_ice_maker.py`

- [ ] **Step 1: Write failing tests**

Add to `tests/test_simulator_ice_maker.py` after `TestIceMakerSimulator`:

```python
class TestIceMakerFaultRegistration:
    def test_four_faults_registered(self):
        sim = IceMakerSimulator()
        assert len(sim._fault_defs) == 4

    def test_fault_names(self):
        sim = IceMakerSimulator()
        names = {f.name for f in sim._fault_defs}
        assert names == {
            "compressor_overtemp",
            "low_refrigerant",
            "water_inlet_blocked",
            "defrost_stuck",
        }

    def test_sensor_by_name(self):
        sim = IceMakerSimulator()
        sensor = sim._sensor_by_name("compressor")
        assert sensor.name == "compressor"

    def test_sensor_by_name_raises_for_unknown(self):
        sim = IceMakerSimulator()
        with pytest.raises(StopIteration):
            sim._sensor_by_name("nonexistent")


class TestCompressorOvertempFault:
    @pytest.mark.asyncio
    async def test_activate_forces_compressor_off(self):
        sim = IceMakerSimulator()
        sim.compressor_on = True
        client = AsyncMock()
        await sim._on_compressor_overtemp_activate(client)
        assert sim.compressor_on is False

    @pytest.mark.asyncio
    async def test_activate_overrides_refrigerant_high_targets(self):
        sim = IceMakerSimulator()
        client = AsyncMock()
        await sim._on_compressor_overtemp_activate(client)
        sensor = sim._sensor_by_name("refrigerant_high")
        assert sensor.target_on == 95.0
        assert sensor.target_off == 95.0

    @pytest.mark.asyncio
    async def test_recover_restores_refrigerant_high_targets(self):
        sim = IceMakerSimulator()
        client = AsyncMock()
        await sim._on_compressor_overtemp_activate(client)
        await sim._on_compressor_overtemp_recover(client)
        original = next(d for d in SENSOR_DEFS if d["name"] == "refrigerant_high")
        sensor = sim._sensor_by_name("refrigerant_high")
        assert sensor.target_on == original["target_on"]
        assert sensor.target_off == original["target_off"]

    @pytest.mark.asyncio
    async def test_activate_publishes_halt_event(self):
        sim = IceMakerSimulator()
        published = []
        async def capture_publish(client, suffix, payload):
            published.append((suffix, payload))
        sim.publish = capture_publish
        client = AsyncMock()
        await sim._on_compressor_overtemp_activate(client)
        assert any("ice_maker/event" in s for s, _ in published)

    def test_tick_halts_compressor_cycling_during_fault(self):
        sim = IceMakerSimulator()
        # Manually activate the already-registered fault
        sim._fault_state["compressor_overtemp"]["active"] = True
        sim._fault_state["compressor_overtemp"]["recover_at"] = 9e9
        # Advance well past compressor off time (300s default)
        for _ in range(100):
            sim.tick(dt=5.0)
        # compressor should not have flipped (still False)
        assert sim.compressor_on is False


class TestLowRefrigerantFault:
    @pytest.mark.asyncio
    async def test_activate_pins_refrigerant_low_target(self):
        sim = IceMakerSimulator()
        client = AsyncMock()
        await sim._on_low_refrigerant_activate(client)
        sensor = sim._sensor_by_name("refrigerant_low")
        assert sensor.target_on == 10.0
        assert sensor.target_off == 10.0

    @pytest.mark.asyncio
    async def test_recover_restores_refrigerant_low_targets(self):
        sim = IceMakerSimulator()
        client = AsyncMock()
        await sim._on_low_refrigerant_activate(client)
        await sim._on_low_refrigerant_recover(client)
        original = next(d for d in SENSOR_DEFS if d["name"] == "refrigerant_low")
        sensor = sim._sensor_by_name("refrigerant_low")
        assert sensor.target_on == original["target_on"]
        assert sensor.target_off == original["target_off"]

    def test_tick_allows_compressor_cycling_during_low_refrigerant(self):
        sim = IceMakerSimulator()
        # Manually activate the already-registered fault
        sim._fault_state["low_refrigerant"]["active"] = True
        sim._fault_state["low_refrigerant"]["recover_at"] = 9e9
        # Advance past the off-cycle (300s default)
        for _ in range(61):
            sim.tick(dt=5.0)
        # Compressor should have turned on (cycling continues under low_refrigerant)
        assert sim.compressor_on is True


class TestWaterInletBlockedFault:
    @pytest.mark.asyncio
    async def test_activate_pins_water_bath_target(self):
        sim = IceMakerSimulator()
        client = AsyncMock()
        await sim._on_water_inlet_blocked_activate(client)
        sensor = sim._sensor_by_name("water_bath")
        assert sensor.target_on == 20.0
        assert sensor.target_off == 20.0

    @pytest.mark.asyncio
    async def test_recover_restores_water_bath_targets(self):
        sim = IceMakerSimulator()
        client = AsyncMock()
        await sim._on_water_inlet_blocked_activate(client)
        await sim._on_water_inlet_blocked_recover(client)
        original = next(d for d in SENSOR_DEFS if d["name"] == "water_bath")
        sensor = sim._sensor_by_name("water_bath")
        assert sensor.target_on == original["target_on"]
        assert sensor.target_off == original["target_off"]


class TestDefrostStuckFault:
    @pytest.mark.asyncio
    async def test_activate_pins_hot_gas_valve_target(self):
        sim = IceMakerSimulator()
        client = AsyncMock()
        await sim._on_defrost_stuck_activate(client)
        sensor = sim._sensor_by_name("hot_gas_valve")
        assert sensor.target_on == 95.0
        assert sensor.target_off == 95.0

    @pytest.mark.asyncio
    async def test_recover_restores_hot_gas_valve_targets(self):
        sim = IceMakerSimulator()
        client = AsyncMock()
        await sim._on_defrost_stuck_activate(client)
        await sim._on_defrost_stuck_recover(client)
        original = next(d for d in SENSOR_DEFS if d["name"] == "hot_gas_valve")
        sensor = sim._sensor_by_name("hot_gas_valve")
        assert sensor.target_on == original["target_on"]
        assert sensor.target_off == original["target_off"]

    def test_tick_suppresses_ice_drop_events_during_fault(self):
        sim = IceMakerSimulator()
        # Manually activate the already-registered fault
        sim._fault_state["defrost_stuck"]["active"] = True
        sim._fault_state["defrost_stuck"]["recover_at"] = 9e9
        # Advance well past the ice drop interval (900s default)
        for _ in range(200):
            sim.tick(dt=5.0)
        ice_drop_events = [
            e for e in sim._pending_events if e.event == "ice_dropped"
        ]
        assert ice_drop_events == []
```

Also add at the top of `tests/test_simulator_ice_maker.py` after existing imports:

```python
from simulators.base import FaultDef
from unittest.mock import AsyncMock
```

- [ ] **Step 2: Run tests to confirm failure**

Run: `uv run pytest tests/test_simulator_ice_maker.py::TestIceMakerFaultRegistration -v`
Expected: `AssertionError: assert 0 == 4` (no faults registered yet)

- [ ] **Step 3: Implement ice maker faults**

Add `_sensor_by_name` helper to `IceMakerSimulator` in `simulators/ice_maker.py`, after `__init__`:

```python
    def _sensor_by_name(self, name: str) -> ThermalSensor:
        """Return the ThermalSensor with the given name."""
        return next(s for s in self.sensors if s.name == name)
```

Add the four `on_activate`/`on_recover` method pairs to `IceMakerSimulator`, after `_sensor_by_name`:

```python
    async def _on_compressor_overtemp_activate(self, client: aiomqtt.Client) -> None:
        self.compressor_on = False
        sensor = self._sensor_by_name("refrigerant_high")
        sensor.target_on = 95.0
        sensor.target_off = 95.0
        await self.publish(client, "ice_maker/event",
                           IceMakerEvent(event="halt", detail="compressor_overtemp"))
        logger.warning("[ice_maker] FAULT: compressor overtemp — halted")

    async def _on_compressor_overtemp_recover(self, client: aiomqtt.Client) -> None:
        original = next(d for d in SENSOR_DEFS if d["name"] == "refrigerant_high")
        sensor = self._sensor_by_name("refrigerant_high")
        sensor.target_on = original["target_on"]
        sensor.target_off = original["target_off"]
        await self.publish(client, "ice_maker/event",
                           IceMakerEvent(event="resume", detail="compressor_overtemp_cleared"))
        logger.info("[ice_maker] Fault cleared: compressor_overtemp")

    async def _on_low_refrigerant_activate(self, client: aiomqtt.Client) -> None:
        sensor = self._sensor_by_name("refrigerant_low")
        sensor.target_on = 10.0
        sensor.target_off = 10.0
        await self.publish(client, "ice_maker/event",
                           IceMakerEvent(event="halt", detail="low_refrigerant"))
        logger.warning("[ice_maker] FAULT: low refrigerant — cooling ineffective")

    async def _on_low_refrigerant_recover(self, client: aiomqtt.Client) -> None:
        original = next(d for d in SENSOR_DEFS if d["name"] == "refrigerant_low")
        sensor = self._sensor_by_name("refrigerant_low")
        sensor.target_on = original["target_on"]
        sensor.target_off = original["target_off"]
        await self.publish(client, "ice_maker/event",
                           IceMakerEvent(event="resume", detail="low_refrigerant_cleared"))
        logger.info("[ice_maker] Fault cleared: low_refrigerant")

    async def _on_water_inlet_blocked_activate(self, client: aiomqtt.Client) -> None:
        sensor = self._sensor_by_name("water_bath")
        sensor.target_on = 20.0
        sensor.target_off = 20.0
        await self.publish(client, "ice_maker/event",
                           IceMakerEvent(event="halt", detail="water_inlet_blocked"))
        logger.warning("[ice_maker] FAULT: water inlet blocked")

    async def _on_water_inlet_blocked_recover(self, client: aiomqtt.Client) -> None:
        original = next(d for d in SENSOR_DEFS if d["name"] == "water_bath")
        sensor = self._sensor_by_name("water_bath")
        sensor.target_on = original["target_on"]
        sensor.target_off = original["target_off"]
        await self.publish(client, "ice_maker/event",
                           IceMakerEvent(event="resume", detail="water_inlet_blocked_cleared"))
        logger.info("[ice_maker] Fault cleared: water_inlet_blocked")

    async def _on_defrost_stuck_activate(self, client: aiomqtt.Client) -> None:
        sensor = self._sensor_by_name("hot_gas_valve")
        sensor.target_on = 95.0
        sensor.target_off = 95.0
        await self.publish(client, "ice_maker/event",
                           IceMakerEvent(event="halt", detail="defrost_stuck"))
        logger.warning("[ice_maker] FAULT: defrost stuck — hot gas valve elevated")

    async def _on_defrost_stuck_recover(self, client: aiomqtt.Client) -> None:
        original = next(d for d in SENSOR_DEFS if d["name"] == "hot_gas_valve")
        sensor = self._sensor_by_name("hot_gas_valve")
        sensor.target_on = original["target_on"]
        sensor.target_off = original["target_off"]
        await self.publish(client, "ice_maker/event",
                           IceMakerEvent(event="resume", detail="defrost_stuck_cleared"))
        logger.info("[ice_maker] Fault cleared: defrost_stuck")
```

Register the four faults at the end of `IceMakerSimulator.__init__`, after `self._pending_events`:

```python
        # Register faults
        self.register_fault(FaultDef(
            name="compressor_overtemp",
            category="long",
            probability=0.0005,
            on_activate=self._on_compressor_overtemp_activate,
            on_recover=self._on_compressor_overtemp_recover,
            message="Compressor temp exceeded safe limit — service required",
            severity="critical",
        ))
        self.register_fault(FaultDef(
            name="low_refrigerant",
            category="long",
            probability=0.0003,
            on_activate=self._on_low_refrigerant_activate,
            on_recover=self._on_low_refrigerant_recover,
            message="Low refrigerant detected — cooling ineffective",
            severity="critical",
        ))
        self.register_fault(FaultDef(
            name="water_inlet_blocked",
            category="medium",
            probability=0.0008,
            on_activate=self._on_water_inlet_blocked_activate,
            on_recover=self._on_water_inlet_blocked_recover,
            message="Water inlet appears blocked — water bath not cooling",
            severity="warning",
        ))
        self.register_fault(FaultDef(
            name="defrost_stuck",
            category="short",
            probability=0.0015,
            on_activate=self._on_defrost_stuck_activate,
            on_recover=self._on_defrost_stuck_recover,
            message="Defrost cycle stuck — hot gas valve elevated",
            severity="warning",
        ))
```

Add `FaultDef` to the import from `simulators.base` at the top of `simulators/ice_maker.py`:

```python
from simulators.base import ESP32Simulator, FaultDef
```

Now update `tick()` in `simulators/ice_maker.py` to check active faults. Replace the existing `tick` method with:

```python
    def tick(self, dt: float):
        """Advance the simulation by dt seconds."""
        active = self._active_fault_names

        # compressor_overtemp: halt compressor entirely
        if "compressor_overtemp" in active:
            for sensor in self.sensors:
                sensor.update(self.compressor_on, dt)
            self._check_temp_bounds()
            return

        # low_refrigerant: compressor still cycles but cooling is ineffective
        # (refrigerant_low target already overridden in on_activate — just run normally)
        # water_inlet_blocked / defrost_stuck: halt compressor cycling
        if active and "low_refrigerant" not in active:
            for sensor in self.sensors:
                sensor.update(self.compressor_on, dt)
            self._check_temp_bounds()
            return

        # Normal operation (or low_refrigerant only — compressor cycles)
        self._cycle_elapsed += dt
        cycle_time = self.COMPRESSOR_ON_TIME if self.compressor_on else self.COMPRESSOR_OFF_TIME
        if self._cycle_elapsed >= cycle_time:
            self.compressor_on = not self.compressor_on
            self._cycle_elapsed = 0.0
            event_type = "power_on" if self.compressor_on else "power_off"
            self._pending_events.append(IceMakerEvent(event=event_type))
            logger.info(f"[ice_maker] Compressor {'ON' if self.compressor_on else 'OFF'}")

        for sensor in self.sensors:
            sensor.update(self.compressor_on, dt)

        self._check_temp_bounds()

        # Periodic ice drops only when no fault active
        if not active:
            self._ice_drop_elapsed += dt
            if self._ice_drop_elapsed >= self.ICE_DROP_INTERVAL:
                self._ice_drop_elapsed = 0.0
                self._pending_events.append(IceMakerEvent(event="ice_dropped"))
                logger.info("[ice_maker] Ice dropped")

    def _check_temp_bounds(self) -> None:
        """Append out-of-bounds events for any sensors outside safe range."""
        for sensor in self.sensors:
            if sensor.value < self.TEMP_LOW or sensor.value > self.TEMP_HIGH:
                self._pending_events.append(
                    IceMakerEvent(
                        event="temp_out_of_bounds",
                        detail=f"{sensor.name}={sensor.value:.1f}C",
                    )
                )
```

Remove the now-extracted `_check_temp_bounds` logic from the old `tick()` (the replacement above already includes it as a separate method).

- [ ] **Step 4: Run tests to confirm pass**

Run: `uv run pytest tests/test_simulator_ice_maker.py -v`
Expected: All tests PASS

- [ ] **Step 5: Commit**

```bash
git add simulators/ice_maker.py tests/test_simulator_ice_maker.py
git commit -m "feat: add 4 ice maker faults with halt/resume and sensor target overrides"
```

---

### Task 5: Vending Machine Faults

**Files:**
- Modify: `simulators/vending_machine.py`
- Modify: `tests/test_simulator_vending.py`

- [ ] **Step 1: Write failing tests**

Add to `tests/test_simulator_vending.py` after existing test classes. First add to the top of the file:

```python
from simulators.base import FaultDef
from unittest.mock import AsyncMock, patch
```

Then add the new test classes:

```python
class TestVendingFaultRegistration:
    def test_four_faults_registered(self):
        sim = VendingMachineSimulator()
        assert len(sim._fault_defs) == 4

    def test_fault_names(self):
        sim = VendingMachineSimulator()
        names = {f.name for f in sim._fault_defs}
        assert names == {
            "auger_jam",
            "bag_drop_solenoid_stuck",
            "water_valve_stuck_open",
            "ice_bin_empty",
        }


class TestAugerJamFault:
    @pytest.mark.asyncio
    async def test_fault_is_registered(self):
        sim = VendingMachineSimulator()
        names = {f.name for f in sim._fault_defs}
        assert "auger_jam" in names

    @pytest.mark.asyncio
    async def test_ice_dispense_publishes_timeout_during_fault(self):
        sim = VendingMachineSimulator()
        # Manually activate the already-registered fault
        sim._fault_state["auger_jam"]["active"] = True
        sim._fault_state["auger_jam"]["recover_at"] = 9e9
        published_states = []

        async def capture(client, topic, payload):
            if "hardware/dispenser" in topic and hasattr(payload, "state"):
                published_states.append(payload.state)

        sim.publish = capture
        sim._set_hw = AsyncMock()
        client = AsyncMock()
        # Patch asyncio.sleep to skip the 90s auger jam timeout
        with patch("asyncio.sleep", new=AsyncMock()):
            await sim._run_ice_dispense(client, slot=0)
        assert "timeout" in published_states
        assert "complete" not in published_states

    @pytest.mark.asyncio
    async def test_recover_logs_and_does_not_crash(self):
        sim = VendingMachineSimulator()
        client = AsyncMock()
        # Should complete without error
        await sim._on_auger_jam_recover(client)


class TestBagDropSolenoidStuckFault:
    @pytest.mark.asyncio
    async def test_ice_dispense_publishes_jam_during_fault(self):
        sim = VendingMachineSimulator()
        # Manually activate the already-registered fault
        sim._fault_state["bag_drop_solenoid_stuck"]["active"] = True
        sim._fault_state["bag_drop_solenoid_stuck"]["recover_at"] = 9e9
        published_states = []

        async def capture(client, topic, payload):
            if "hardware/dispenser" in topic and hasattr(payload, "state"):
                published_states.append(payload.state)

        sim.publish = capture
        sim._set_hw = AsyncMock()
        client = AsyncMock()
        # Patch asyncio.sleep to skip fill time wait
        with patch("asyncio.sleep", new=AsyncMock()):
            await sim._run_ice_dispense(client, slot=0)
        assert "jam" in published_states
        assert "complete" not in published_states


class TestWaterValveStuckOpenFault:
    @pytest.mark.asyncio
    async def test_activate_sets_valve_and_flow_sensor_on(self):
        sim = VendingMachineSimulator()
        set_hw_calls = []

        async def capture_set_hw(client, device, state):
            set_hw_calls.append((device, state))

        sim._set_hw = capture_set_hw
        client = AsyncMock()
        await sim._on_water_valve_stuck_open_activate(client)
        assert ("water_valve_solenoid", True) in set_hw_calls
        assert ("water_flow_sensor", True) in set_hw_calls

    @pytest.mark.asyncio
    async def test_recover_closes_valve_and_flow_sensor(self):
        sim = VendingMachineSimulator()
        set_hw_calls = []

        async def capture_set_hw(client, device, state):
            set_hw_calls.append((device, state))

        sim._set_hw = capture_set_hw
        client = AsyncMock()
        await sim._on_water_valve_stuck_open_recover(client)
        assert ("water_valve_solenoid", False) in set_hw_calls
        assert ("water_flow_sensor", False) in set_hw_calls


class TestIceBinEmptyFault:
    @pytest.mark.asyncio
    async def test_activate_sets_bin_half_full_false(self):
        sim = VendingMachineSimulator()
        set_hw_calls = []

        async def capture_set_hw(client, device, state):
            set_hw_calls.append((device, state))
            sim._hw[device] = state

        sim._set_hw = capture_set_hw
        client = AsyncMock()
        await sim._on_ice_bin_empty_activate(client)
        assert ("bin_half_full", False) in set_hw_calls

    @pytest.mark.asyncio
    async def test_ice_dispense_publishes_bin_empty_during_fault(self):
        sim = VendingMachineSimulator()
        sim._fault_state["ice_bin_empty"] = {"active": True, "recover_at": 9e9}
        published_states = []

        async def capture(client, topic, payload):
            if "hardware/dispenser" in topic and hasattr(payload, "state"):
                published_states.append(payload.state)

        sim.publish = capture
        sim._set_hw = AsyncMock()
        client = AsyncMock()
        await sim._run_ice_dispense(client, slot=0)
        assert "bin_empty" in published_states
        assert "complete" not in published_states

    @pytest.mark.asyncio
    async def test_recover_restores_bin_half_full(self):
        sim = VendingMachineSimulator()
        set_hw_calls = []

        async def capture_set_hw(client, device, state):
            set_hw_calls.append((device, state))

        sim._set_hw = capture_set_hw
        client = AsyncMock()
        await sim._on_ice_bin_empty_recover(client)
        assert ("bin_half_full", True) in set_hw_calls
```

- [ ] **Step 2: Run tests to confirm failure**

Run: `uv run pytest tests/test_simulator_vending.py::TestVendingFaultRegistration -v`
Expected: `AssertionError: assert 0 == 4`

- [ ] **Step 3: Implement vending machine faults**

Add `FaultDef` to the import in `simulators/vending_machine.py`:

```python
from simulators.base import ESP32Simulator, FaultDef
```

Add the eight activate/recover methods to `VendingMachineSimulator` in `simulators/vending_machine.py`, after `_publish_sensors`:

```python
    # --- Fault activate/recover methods ---

    async def _on_auger_jam_activate(self, client: aiomqtt.Client) -> None:
        logger.warning("[vending] FAULT: auger jam — bag fill will time out")

    async def _on_auger_jam_recover(self, client: aiomqtt.Client) -> None:
        logger.info("[vending] Fault cleared: auger_jam")

    async def _on_bag_drop_solenoid_stuck_activate(self, client: aiomqtt.Client) -> None:
        logger.warning("[vending] FAULT: bag drop solenoid stuck")

    async def _on_bag_drop_solenoid_stuck_recover(self, client: aiomqtt.Client) -> None:
        await self._set_hw(client, "bag_full_sensor", False)
        await self._set_hw(client, "bag_drop_solenoid", False)
        logger.info("[vending] Fault cleared: bag_drop_solenoid_stuck")

    async def _on_water_valve_stuck_open_activate(self, client: aiomqtt.Client) -> None:
        await self._set_hw(client, "water_valve_solenoid", True)
        await self._set_hw(client, "water_flow_sensor", True)
        logger.warning("[vending] FAULT: water valve stuck open — flow incrementing")

    async def _on_water_valve_stuck_open_recover(self, client: aiomqtt.Client) -> None:
        await self._set_hw(client, "water_valve_solenoid", False)
        await self._set_hw(client, "water_flow_sensor", False)
        logger.info("[vending] Fault cleared: water_valve_stuck_open")

    async def _on_ice_bin_empty_activate(self, client: aiomqtt.Client) -> None:
        await self._set_hw(client, "bin_half_full", False)
        logger.warning("[vending] FAULT: ice bin empty")

    async def _on_ice_bin_empty_recover(self, client: aiomqtt.Client) -> None:
        await self._set_hw(client, "bin_half_full", True)
        logger.info("[vending] Fault cleared: ice_bin_empty")
```

Register the four faults at the end of `VendingMachineSimulator.__init__`, after the hardware state setup:

```python
        # Register faults
        self.register_fault(FaultDef(
            name="auger_jam",
            category="medium",
            probability=0.001,
            on_activate=self._on_auger_jam_activate,
            on_recover=self._on_auger_jam_recover,
            message="Auger motor jammed — ice bag fill timed out",
            severity="warning",
        ))
        self.register_fault(FaultDef(
            name="bag_drop_solenoid_stuck",
            category="medium",
            probability=0.0008,
            on_activate=self._on_bag_drop_solenoid_stuck_activate,
            on_recover=self._on_bag_drop_solenoid_stuck_recover,
            message="Bag drop solenoid stuck — bag not releasing",
            severity="warning",
        ))
        self.register_fault(FaultDef(
            name="water_valve_stuck_open",
            category="short",
            probability=0.0012,
            on_activate=self._on_water_valve_stuck_open_activate,
            on_recover=self._on_water_valve_stuck_open_recover,
            message="Water valve stuck open — water flowing continuously",
            severity="critical",
        ))
        self.register_fault(FaultDef(
            name="ice_bin_empty",
            category="medium",
            probability=0.0006,
            on_activate=self._on_ice_bin_empty_activate,
            on_recover=self._on_ice_bin_empty_recover,
            message="Ice bin empty — refill required",
            severity="warning",
        ))
```

Update `_run_ice_dispense` in `simulators/vending_machine.py`. Replace the existing method with:

```python
    async def _run_ice_dispense(self, client: aiomqtt.Client, slot: int):
        """Run ice dispense sequence with realistic hardware transitions."""
        active = self._active_fault_names

        await self.publish(client, "hardware/dispenser",
                           DispenserStatus(slot=slot, state="motor_active"))

        # Ice bin empty: report immediately and abort
        if "ice_bin_empty" in active:
            await self._set_hw(client, "agitator_motor", False)
            await self._set_hw(client, "fan", False)
            await self.publish(client, "hardware/dispenser",
                               DispenserStatus(slot=slot, state="bin_empty"))
            logger.warning(f"[vending] Slot {slot}: ice bin empty")
            return

        # Start agitator and fan first
        await self._set_hw(client, "agitator_motor", True)
        await self._set_hw(client, "fan", True)
        await asyncio.sleep(1.0)

        # Start auger to fill bag
        await self._set_hw(client, "auger_motor", True)
        logger.info(f"[vending] Slot {slot}: auger running, filling bag")

        if "auger_jam" in active:
            # Auger runs but bag never fills — time out after 90 seconds
            await asyncio.sleep(90.0)
            await self._set_hw(client, "auger_motor", False)
            await self._set_hw(client, "agitator_motor", False)
            await self._set_hw(client, "fan", False)
            await self.publish(client, "hardware/dispenser",
                               DispenserStatus(slot=slot, state="timeout"))
            logger.warning(f"[vending] Slot {slot}: auger jam — dispense timed out")
            return

        # Wait for bag to fill (simulated)
        fill_time = random.uniform(5.0, 12.0)
        await asyncio.sleep(fill_time)

        # Bag full sensor triggers
        await self._set_hw(client, "bag_full_sensor", True)
        await self._set_hw(client, "auger_motor", False)
        logger.info(f"[vending] Slot {slot}: bag full")

        await self.publish(client, "hardware/dispenser",
                           DispenserStatus(slot=slot, state="fill_complete"))

        await asyncio.sleep(0.5)

        if "bag_drop_solenoid_stuck" in self._active_fault_names:
            # Solenoid fires but bag doesn't drop — bag_full_sensor stays True
            await self._set_hw(client, "bag_drop_solenoid", True)
            await asyncio.sleep(0.5)
            # bag_full_sensor intentionally NOT cleared
            await self._set_hw(client, "agitator_motor", False)
            await self._set_hw(client, "fan", False)
            await self.publish(client, "hardware/dispenser",
                               DispenserStatus(slot=slot, state="jam"))
            logger.warning(f"[vending] Slot {slot}: bag drop solenoid stuck")
            return

        # Drop the bag normally
        await self._set_hw(client, "bag_drop_solenoid", True)
        await asyncio.sleep(0.5)
        await self._set_hw(client, "bag_drop_solenoid", False)
        await self._set_hw(client, "bag_full_sensor", False)
        logger.info(f"[vending] Slot {slot}: bag dropped")

        # Stop agitator and fan
        await self._set_hw(client, "agitator_motor", False)
        await self._set_hw(client, "fan", False)

        await self.publish(client, "hardware/dispenser",
                           DispenserStatus(slot=slot, state="complete"))
        logger.info(f"[vending] Slot {slot}: ice dispense complete")
```

Update `_run_water_dispense` in `simulators/vending_machine.py`. Replace the existing method with:

```python
    async def _run_water_dispense(self, client: aiomqtt.Client, slot: int):
        """Run water dispense sequence with valve and flow sensor."""
        await self.publish(client, "hardware/dispenser",
                           DispenserStatus(slot=slot, state="solenoid_open"))

        # Open valve
        await self._set_hw(client, "water_valve_solenoid", True)
        await self._set_hw(client, "water_flow_sensor", True)
        logger.info(f"[vending] Slot {slot}: water valve open, dispensing")

        # Simulate flow pulses
        pulse_seconds = random.randint(5, 10)
        for i in range(pulse_seconds):
            await asyncio.sleep(1.0)
            gallons_per_pulse = 0.1
            self._water_flow_total += gallons_per_pulse
            logger.debug(f"[vending] Slot {slot}: flow total {self._water_flow_total:.1f} gal")

        if "water_valve_stuck_open" in self._active_fault_names:
            # Valve does not close — hardware io already set to True in on_activate
            # flow incrementing continues in _publish_sensors
            logger.warning(f"[vending] Slot {slot}: water valve stuck open after dispense")
            await self.publish(client, "hardware/dispenser",
                               DispenserStatus(slot=slot, state="complete"))
            return

        # Close valve normally
        await self._set_hw(client, "water_valve_solenoid", False)
        await self._set_hw(client, "water_flow_sensor", False)

        await self.publish(client, "hardware/dispenser",
                           DispenserStatus(slot=slot, state="complete"))
        logger.info(f"[vending] Slot {slot}: water dispense complete")
```

Update `_publish_sensors` to increment flow while water valve is stuck. Find the `_publish_sensors` method and add this block after the cabinet temp section, before `await self.publish(client, "sensors/temp/cabinet", ...)`:

```python
            # If water valve stuck open, keep incrementing flow
            if self._hw.get("water_flow_sensor") and self._hw.get("water_valve_solenoid"):
                self._water_flow_total += 0.1 * (SENSOR_PUBLISH_INTERVAL / 1.0)
```

- [ ] **Step 4: Run tests to confirm pass**

Run: `uv run pytest tests/test_simulator_vending.py -v`
Expected: All tests PASS

- [ ] **Step 5: Commit**

```bash
git add simulators/vending_machine.py tests/test_simulator_vending.py
git commit -m "feat: add 4 vending machine faults with degraded dispense sequences"
```

---

### Task 6: Vending Machine Customer Interactions

**Files:**
- Modify: `simulators/vending_machine.py`
- Modify: `tests/test_simulator_vending.py`

- [ ] **Step 1: Write failing tests**

Add to `tests/test_simulator_vending.py` after `TestIceBinEmptyFault`. Also add `from datetime import datetime` to imports:

```python
class TestCustomerBehaviours:
    def test_arrival_factor_peak_morning(self):
        sim = VendingMachineSimulator()
        factor = sim._arrival_factor(hour=12)
        assert factor == 0.5

    def test_arrival_factor_peak_evening(self):
        sim = VendingMachineSimulator()
        factor = sim._arrival_factor(hour=18)
        assert factor == 0.5

    def test_arrival_factor_overnight(self):
        sim = VendingMachineSimulator()
        factor = sim._arrival_factor(hour=3)
        assert factor == 2.0

    def test_arrival_factor_normal(self):
        sim = VendingMachineSimulator()
        factor = sim._arrival_factor(hour=9)
        assert factor == 1.0

    def test_fault_aware_idle_time_shortened(self):
        sim = VendingMachineSimulator()
        sim._fault_state["auger_jam"] = {"active": True, "recover_at": 9e9}
        idle = sim._compute_idle_time()
        # Must be within 5-15 range (fault-aware)
        assert 5.0 <= idle <= 15.0

    def test_normal_idle_time_in_range(self):
        sim = VendingMachineSimulator()
        for _ in range(50):
            idle = sim._compute_idle_time(hour=9)
            assert sim.IDLE_MIN <= idle <= sim.IDLE_MAX
```

- [ ] **Step 2: Run tests to confirm failure**

Run: `uv run pytest tests/test_simulator_vending.py::TestCustomerBehaviours -v`
Expected: `AttributeError: 'VendingMachineSimulator' object has no attribute '_arrival_factor'`

- [ ] **Step 3: Implement customer interaction helpers and update `_customer_loop`**

Add `datetime` to imports at the top of `simulators/vending_machine.py`:

```python
from datetime import datetime
```

Add these two helper methods to `VendingMachineSimulator`, after the fault activate/recover methods:

```python
    def _arrival_factor(self, hour: int | None = None) -> float:
        """Return an idle-time multiplier based on time of day.

        Peak hours (11am-2pm, 5pm-8pm): factor 0.5 (customers arrive twice as fast).
        Overnight (2am-6am): factor 2.0 (customers arrive half as fast).
        Otherwise: factor 1.0.
        """
        if hour is None:
            hour = datetime.now().hour
        if 11 <= hour < 14 or 17 <= hour < 20:
            return 0.5
        if 2 <= hour < 6:
            return 2.0
        return 1.0

    def _compute_idle_time(self, hour: int | None = None) -> float:
        """Return idle wait time in seconds, accounting for faults and time-of-day."""
        fault_active = "auger_jam" in self._active_fault_names or \
                       "ice_bin_empty" in self._active_fault_names
        if fault_active:
            return random.uniform(5.0, 15.0)
        factor = self._arrival_factor(hour=hour)
        return random.uniform(self.IDLE_MIN, self.IDLE_MAX) * factor
```

Replace the existing `_customer_loop` method with:

```python
    async def _customer_loop(self, client: aiomqtt.Client):
        """Simulate customers pressing buttons and waiting for dispense."""
        while True:
            idle_time = self._compute_idle_time()
            logger.info(f"[vending] Waiting {idle_time:.0f}s for next customer")
            await asyncio.sleep(idle_time)

            # Customer presses a button
            button = self._pick_button()
            await self.publish(client, "hardware/buttons", ButtonPress(button=button))
            logger.info(f"[vending] Customer pressed button {button}")

            # Indecisive customer (20%): changes their mind
            if random.random() < 0.20:
                await asyncio.sleep(random.uniform(5.0, 15.0))
                other_buttons = [b for b in range(self.num_buttons) if b != button]
                if other_buttons:
                    button = random.choice(other_buttons)
                    await self.publish(client, "hardware/buttons", ButtonPress(button=button))
                    logger.info(f"[vending] Indecisive customer changed to button {button}")

            # Impatient customer (15%): shorter timeout
            timeout = (
                random.uniform(10.0, 20.0)
                if random.random() < 0.15
                else self.DISPENSE_TIMEOUT
            )

            try:
                slot = await asyncio.wait_for(
                    self._dispense_command.get(),
                    timeout=timeout,
                )
            except asyncio.TimeoutError:
                logger.info("[vending] Customer walked away")
                continue

            # Run the appropriate dispense sequence
            if self.slot_type(slot) == "water":
                await self._run_water_dispense(client, slot)
            else:
                await self._run_ice_dispense(client, slot)

            # Repeat customer (10%): buys again immediately
            if random.random() < 0.10:
                logger.info("[vending] Repeat customer buying again")
                repeat_button = self._pick_button()
                await self.publish(client, "hardware/buttons",
                                   ButtonPress(button=repeat_button))
                logger.info(f"[vending] Repeat customer pressed button {repeat_button}")
                try:
                    slot = await asyncio.wait_for(
                        self._dispense_command.get(),
                        timeout=self.DISPENSE_TIMEOUT,
                    )
                    if self.slot_type(slot) == "water":
                        await self._run_water_dispense(client, slot)
                    else:
                        await self._run_ice_dispense(client, slot)
                except asyncio.TimeoutError:
                    logger.info("[vending] Repeat customer walked away")
```

- [ ] **Step 4: Run tests to confirm pass**

Run: `uv run pytest tests/test_simulator_vending.py -v`
Expected: All tests PASS

- [ ] **Step 5: Commit**

```bash
git add simulators/vending_machine.py tests/test_simulator_vending.py
git commit -m "feat: add customer interaction behaviours (indecisive, impatient, repeat, time-of-day)"
```

---

### Task 7: MDB Gateway Faults

**Files:**
- Modify: `simulators/mdb_gateway.py`
- Modify: `tests/test_simulator_mdb.py`

- [ ] **Step 1: Write failing tests**

Add to the top of `tests/test_simulator_mdb.py`:

```python
from simulators.base import FaultDef
from unittest.mock import AsyncMock
```

Add test classes after `TestPaymentStrategy`:

```python
class TestPaymentStrategyExclusion:
    def test_pick_method_excludes_cash_coin(self):
        strategy = PaymentStrategy()
        for _ in range(100):
            method = strategy.pick_method(excluded={"cash_coin"})
            assert method != "cash_coin"

    def test_pick_method_excludes_multiple(self):
        strategy = PaymentStrategy()
        excluded = {"cash_coin", "cash_bill"}
        for _ in range(100):
            method = strategy.pick_method(excluded=excluded)
            assert method not in excluded

    def test_pick_method_returns_none_when_all_excluded(self):
        strategy = PaymentStrategy()
        result = strategy.pick_method(excluded={"cash_coin", "cash_bill", "card", "nfc"})
        assert result is None

    def test_pick_method_no_exclusions_returns_valid(self):
        strategy = PaymentStrategy()
        methods = {strategy.pick_method() for _ in range(100)}
        assert methods.issubset({"cash_coin", "cash_bill", "card", "nfc"})


class TestMDBFaultRegistration:
    def test_four_faults_registered(self):
        sim = MDBGatewaySimulator()
        assert len(sim._fault_defs) == 4

    def test_fault_names(self):
        sim = MDBGatewaySimulator()
        names = {f.name for f in sim._fault_defs}
        assert names == {
            "coin_acceptor_jammed",
            "bill_validator_offline",
            "card_reader_error",
            "mdb_bus_reset",
        }


class TestCoinAcceptorJammedFault:
    @pytest.mark.asyncio
    async def test_activate_sets_device_state_to_error(self):
        sim = MDBGatewaySimulator()
        client = AsyncMock()
        await sim._on_coin_acceptor_jammed_activate(client)
        device = next(d for d in sim.devices if d["name"] == "coin_acceptor")
        assert device["state"] == "error"

    @pytest.mark.asyncio
    async def test_recover_sets_device_state_to_ready(self):
        sim = MDBGatewaySimulator()
        client = AsyncMock()
        await sim._on_coin_acceptor_jammed_activate(client)
        await sim._on_coin_acceptor_jammed_recover(client)
        device = next(d for d in sim.devices if d["name"] == "coin_acceptor")
        assert device["state"] == "ready"

    def test_excluded_methods_during_coin_fault(self):
        sim = MDBGatewaySimulator()
        sim._fault_state["coin_acceptor_jammed"] = {"active": True, "recover_at": 9e9}
        excluded = sim._build_payment_exclusions()
        assert "cash_coin" in excluded

    def test_no_exclusions_when_no_faults(self):
        sim = MDBGatewaySimulator()
        excluded = sim._build_payment_exclusions()
        assert excluded == set()


class TestBillValidatorOfflineFault:
    @pytest.mark.asyncio
    async def test_activate_sets_device_state_to_offline(self):
        sim = MDBGatewaySimulator()
        client = AsyncMock()
        await sim._on_bill_validator_offline_activate(client)
        device = next(d for d in sim.devices if d["name"] == "bill_validator")
        assert device["state"] == "offline"

    def test_excluded_methods_during_bill_fault(self):
        sim = MDBGatewaySimulator()
        sim._fault_state["bill_validator_offline"] = {"active": True, "recover_at": 9e9}
        excluded = sim._build_payment_exclusions()
        assert "cash_bill" in excluded


class TestCardReaderErrorFault:
    @pytest.mark.asyncio
    async def test_activate_sets_device_state_to_error(self):
        sim = MDBGatewaySimulator()
        client = AsyncMock()
        await sim._on_card_reader_error_activate(client)
        device = next(d for d in sim.devices if d["name"] == "card_reader")
        assert device["state"] == "error"

    def test_excluded_methods_during_card_fault(self):
        sim = MDBGatewaySimulator()
        sim._fault_state["card_reader_error"] = {"active": True, "recover_at": 9e9}
        excluded = sim._build_payment_exclusions()
        assert "card" in excluded
        assert "nfc" in excluded


class TestMDBBusResetFault:
    @pytest.mark.asyncio
    async def test_activate_sets_all_devices_offline(self):
        sim = MDBGatewaySimulator()
        client = AsyncMock()
        await sim._on_mdb_bus_reset_activate(client)
        for device in sim.devices:
            assert device["state"] == "offline"

    def test_bus_reset_excludes_all_payment_methods(self):
        sim = MDBGatewaySimulator()
        sim._fault_state["mdb_bus_reset"] = {"active": True, "recover_at": 9e9}
        excluded = sim._build_payment_exclusions()
        assert excluded == {"cash_coin", "cash_bill", "card", "nfc"}
```

- [ ] **Step 2: Run tests to confirm failure**

Run: `uv run pytest tests/test_simulator_mdb.py::TestPaymentStrategyExclusion -v`
Expected: `TypeError: pick_method() got an unexpected keyword argument 'excluded'`

- [ ] **Step 3: Implement MDB faults**

Update `PaymentStrategy.pick_method` in `simulators/mdb_gateway.py`. Replace the existing method:

```python
    def pick_method(self, excluded: set[str] | None = None) -> str | None:
        """Pick a payment method, excluding any in the excluded set."""
        available = [m for m in self.METHODS if m not in (excluded or set())]
        if not available:
            return None
        return random.choice(available)
```

Add `FaultDef` to imports in `simulators/mdb_gateway.py`:

```python
from simulators.base import ESP32Simulator, FaultDef
```

Add the `_build_payment_exclusions` method and fault activate/recover methods to `MDBGatewaySimulator`, after `_do_card_payment`:

```python
    def _build_payment_exclusions(self) -> set[str]:
        """Return the set of payment methods excluded by currently active faults."""
        active = self._active_fault_names
        excluded: set[str] = set()
        if "coin_acceptor_jammed" in active:
            excluded.add("cash_coin")
        if "bill_validator_offline" in active:
            excluded.add("cash_bill")
        if "card_reader_error" in active:
            excluded.update({"card", "nfc"})
        if "mdb_bus_reset" in active:
            excluded.update({"cash_coin", "cash_bill", "card", "nfc"})
        return excluded

    def _device_by_name(self, name: str) -> dict:
        return next(d for d in self.devices if d["name"] == name)

    async def _set_device_state(self, client: aiomqtt.Client, name: str, state: str) -> None:
        device = self._device_by_name(name)
        device["state"] = state
        await self.publish(client, "payment/status", PaymentStatus(device=name, state=state))

    async def _on_coin_acceptor_jammed_activate(self, client: aiomqtt.Client) -> None:
        await self._set_device_state(client, "coin_acceptor", "error")
        logger.warning("[mdb] FAULT: coin acceptor jammed")

    async def _on_coin_acceptor_jammed_recover(self, client: aiomqtt.Client) -> None:
        await self._set_device_state(client, "coin_acceptor", "ready")
        logger.info("[mdb] Fault cleared: coin_acceptor_jammed")

    async def _on_bill_validator_offline_activate(self, client: aiomqtt.Client) -> None:
        await self._set_device_state(client, "bill_validator", "offline")
        logger.warning("[mdb] FAULT: bill validator offline")

    async def _on_bill_validator_offline_recover(self, client: aiomqtt.Client) -> None:
        await self._set_device_state(client, "bill_validator", "ready")
        logger.info("[mdb] Fault cleared: bill_validator_offline")

    async def _on_card_reader_error_activate(self, client: aiomqtt.Client) -> None:
        await self._set_device_state(client, "card_reader", "error")
        logger.warning("[mdb] FAULT: card reader error")

    async def _on_card_reader_error_recover(self, client: aiomqtt.Client) -> None:
        await self._set_device_state(client, "card_reader", "ready")
        logger.info("[mdb] Fault cleared: card_reader_error")

    async def _on_mdb_bus_reset_activate(self, client: aiomqtt.Client) -> None:
        for device in self.devices:
            await self._set_device_state(client, device["name"], "offline")
        logger.warning("[mdb] FAULT: MDB bus reset — all devices offline")

    async def _on_mdb_bus_reset_recover(self, client: aiomqtt.Client) -> None:
        for device in self.devices:
            await asyncio.sleep(random.uniform(10.0, 30.0))
            await self._set_device_state(client, device["name"], "ready")
            logger.info(f"[mdb] Device restored: {device['name']}")
        logger.info("[mdb] Fault cleared: mdb_bus_reset")
```

Register the four faults at the end of `MDBGatewaySimulator.__init__`, after `self._vmc_status`:

```python
        # Register faults
        self.register_fault(FaultDef(
            name="coin_acceptor_jammed",
            category="short",
            probability=0.0015,
            on_activate=self._on_coin_acceptor_jammed_activate,
            on_recover=self._on_coin_acceptor_jammed_recover,
            message="Coin acceptor jammed — coins rejected",
            severity="warning",
        ))
        self.register_fault(FaultDef(
            name="bill_validator_offline",
            category="short",
            probability=0.0012,
            on_activate=self._on_bill_validator_offline_activate,
            on_recover=self._on_bill_validator_offline_recover,
            message="Bill validator offline — bills not accepted",
            severity="warning",
        ))
        self.register_fault(FaultDef(
            name="card_reader_error",
            category="short",
            probability=0.001,
            on_activate=self._on_card_reader_error_activate,
            on_recover=self._on_card_reader_error_recover,
            message="Card reader error — card and NFC payments unavailable",
            severity="warning",
        ))
        self.register_fault(FaultDef(
            name="mdb_bus_reset",
            category="medium",
            probability=0.0004,
            on_activate=self._on_mdb_bus_reset_activate,
            on_recover=self._on_mdb_bus_reset_recover,
            message="MDB bus reset — all payment devices temporarily offline",
            severity="critical",
        ))
```

Update `_payment_loop` in `simulators/mdb_gateway.py` to use exclusions. Replace the method call to `self.strategy.pick_method()` section:

```python
    async def _payment_loop(self, client: aiomqtt.Client):
        """React to VMC state changes by inserting payments."""
        while True:
            status = await self._vmc_status.get()
            state = status.get("state", "")

            if state != "interacting_with_user":
                continue

            selected = status.get("selected_product")
            if not selected:
                continue

            price = self._product_prices.get(selected, 3.00)
            logger.info(f"[mdb] Customer interaction detected, product: {selected} (${price:.2f})")

            # Simulate customer reaching for wallet
            await asyncio.sleep(random.uniform(2.0, 5.0))

            excluded = self._build_payment_exclusions()
            if "mdb_bus_reset" in self._active_fault_names:
                logger.info("[mdb] Bus reset active — no payments accepted")
                continue

            method = self.strategy.pick_method(excluded=excluded)
            if method is None:
                logger.info("[mdb] No payment methods available (all excluded by faults)")
                continue

            logger.info(f"[mdb] Payment method: {method}")

            if method in ("card", "nfc"):
                await self._do_card_payment(client, method, price=price)
            else:
                await self._do_cash_payment(client, method)
```

- [ ] **Step 4: Run tests to confirm pass**

Run: `uv run pytest tests/test_simulator_mdb.py -v`
Expected: All tests PASS

- [ ] **Step 5: Commit**

```bash
git add simulators/mdb_gateway.py tests/test_simulator_mdb.py
git commit -m "feat: add 4 MDB gateway faults with device state changes and payment exclusions"
```

---

### Task 8: Full Test Suite Verification

**Files:**
- No new files

- [ ] **Step 1: Run the complete test suite**

Run: `uv run pytest tests/ -v`
Expected: All tests pass (existing tests unchanged plus all new fault/interaction tests)

- [ ] **Step 2: Run lint and format**

Run: `ruff check --fix simulators/`
Then: `ruff format simulators/`
Fix any issues reported.

- [ ] **Step 3: Commit any lint fixes**

```bash
git add simulators/
git commit -m "style: apply ruff fixes to simulator fault injection code"
```

(Skip this step if ruff reports no changes.)

- [ ] **Step 4: Smoke test — verify simulators start without errors**

Run (each in a separate terminal, Ctrl+C after a few seconds to confirm startup):

```bash
uv run python -m simulators.ice_maker --help
uv run python -m simulators.vending_machine --help
uv run python -m simulators.mdb_gateway --help
```

Expected: Each prints argparse help and exits cleanly with no import errors.
