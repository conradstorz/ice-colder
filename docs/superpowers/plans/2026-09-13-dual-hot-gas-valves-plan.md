# Dual Hot Gas Valves Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The ice maker simulator models two hot gas valves (`hot_gas_valve_1`/`_2`), each with an independent "stuck" fault under dumb-machine semantics: no self-reported halt, compressor keeps cycling, harvests keep being attempted (alternating evaporators; a stuck valve's turn yields `failed_cycle` instead of `ice_dropped`).

**Architecture:** Per spec `docs/superpowers/specs/2026-09-13-dual-hot-gas-valves-design.md`. All changes in `simulators/ice_maker.py` and `tests/test_simulator_ice_maker.py`, plus a repo-wide sweep for stale `hot_gas_valve`/`defrost_stuck` references.

**Tech Stack:** Python 3.12, aiomqtt, pytest (asyncio_mode=auto), uv.

## Global Constraints

- `uv run pytest` / `uv run python` only; never bare `pip`/`python`. Lint with `uv run ruff check --fix .` then `uv run ruff format .` (ALWAYS `uv run` — the global ruff is an older version that formats differently).
- NEVER chain shell commands with `&&` — separate tool calls.
- Commit messages end with: `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`
- Do NOT touch config.json, colder-docker/, or data/.
- Baseline: full suite 352 passed, 9 skipped.
- Dumb-machine rule (binding, from the user): stuck-valve faults must publish NO MQTT event on activate or recover, must not halt compressor cycling, and must not pause harvest attempts. Detection is external-only (pinned ~95 °C temps → `temp_out_of_bounds`, and `failed_cycle` events).

---

### Task 1: Two valves, per-valve faults, harvest alternation

**Files:**
- Modify: `simulators/ice_maker.py` (SENSOR_DEFS; `__init__` fault registration + `_next_harvest_valve`; remove `_on_defrost_stuck_*`; add valve-fault factories; rework `tick()`)
- Modify: `tests/test_simulator_ice_maker.py` (update counts/names; replace `TestDefrostStuckFault`; add per-valve + harvest tests)

**Interfaces:**
- Consumes: `ESP32Simulator.register_fault(FaultDef)`, `self._active_fault_names` (collection of active fault names), `self._fault_state[name]["active"]/["recover_at"]` (used by tests), `IceMakerEvent(event, detail)`.
- Produces: sensors `hot_gas_valve_1`, `hot_gas_valve_2`; faults `defrost_stuck_1`, `defrost_stuck_2`; events `ice_dropped` (detail `evaporator_N`) and `failed_cycle` (detail `hot_gas_valve_N_stuck`).

- [ ] **Step 1: Update the test file (failing first)**

In `tests/test_simulator_ice_maker.py`:

1. `TestSensorDefs` — replace both tests:

```python
class TestSensorDefs:
    def test_all_ten_sensors_defined(self):
        assert len(SENSOR_DEFS) == 10

    def test_expected_sensor_names(self):
        names = {s["name"] for s in SENSOR_DEFS}
        expected = {
            "water_inlet",
            "water_bath",
            "compressor",
            "exhaust_air",
            "ambient_air",
            "refrigerant_high",
            "refrigerant_low",
            "purge_water",
            "hot_gas_valve_1",
            "hot_gas_valve_2",
        }
        assert names == expected
```

2. `TestIceMakerSimulator.test_creates_with_defaults` — change `len(sim.sensors) == 9` to `== 10`.

3. `TestHADiscovery` — `test_returns_11_entities` becomes:

```python
    def test_returns_12_entities(self):
        sim = IceMakerSimulator()
        entities = sim.ha_discovery_entities()
        assert len(entities) == 12
```

`test_nine_temperature_sensors` becomes `test_ten_temperature_sensors` asserting `len(temp_sensors) == 10`. In `test_discovery_publishes_all_entities`, change `call_count == 11` to `== 12` (and the docstring's "11" to "12"). Add:

```python
    def test_hot_gas_valve_entities(self):
        sim = IceMakerSimulator()
        ids = [e["object_id"] for e in sim.ha_discovery_entities()]
        assert "hot_gas_valve_1_temp" in ids
        assert "hot_gas_valve_2_temp" in ids
        assert "hot_gas_valve_temp" not in ids
```

4. `TestIceMakerFaultRegistration` — five faults now:

```python
    def test_five_faults_registered(self):
        sim = IceMakerSimulator()
        assert len(sim._fault_defs) == 5

    def test_fault_names(self):
        sim = IceMakerSimulator()
        names = {f.name for f in sim._fault_defs}
        assert names == {
            "compressor_overtemp",
            "low_refrigerant",
            "water_inlet_blocked",
            "defrost_stuck_1",
            "defrost_stuck_2",
        }
```

5. Delete `TestDefrostStuckFault` entirely; add in its place:

```python
class TestDefrostStuckPerValve:
    @staticmethod
    def _force_active(sim, valve: int):
        sim._fault_state[f"defrost_stuck_{valve}"]["active"] = True
        sim._fault_state[f"defrost_stuck_{valve}"]["recover_at"] = 9e9

    @staticmethod
    def _fault_def(sim, valve: int):
        return next(f for f in sim._fault_defs if f.name == f"defrost_stuck_{valve}")

    @pytest.mark.asyncio
    async def test_activate_pins_only_that_valve(self):
        sim = IceMakerSimulator()
        client = AsyncMock()
        await self._fault_def(sim, 1).on_activate(client)
        v1 = sim._sensor_by_name("hot_gas_valve_1")
        assert v1.target_on == 95.0
        assert v1.target_off == 95.0
        original = next(d for d in SENSOR_DEFS if d["name"] == "hot_gas_valve_2")
        v2 = sim._sensor_by_name("hot_gas_valve_2")
        assert v2.target_on == original["target_on"]
        assert v2.target_off == original["target_off"]

    @pytest.mark.asyncio
    async def test_recover_restores_only_that_valve(self):
        sim = IceMakerSimulator()
        client = AsyncMock()
        await self._fault_def(sim, 2).on_activate(client)
        await self._fault_def(sim, 2).on_recover(client)
        original = next(d for d in SENSOR_DEFS if d["name"] == "hot_gas_valve_2")
        v2 = sim._sensor_by_name("hot_gas_valve_2")
        assert v2.target_on == original["target_on"]
        assert v2.target_off == original["target_off"]

    @pytest.mark.asyncio
    async def test_activate_and_recover_publish_no_events(self):
        """Dumb machine: a stuck valve never self-reports."""
        sim = IceMakerSimulator()
        published = []

        async def capture_publish(client, suffix, payload):
            published.append((suffix, payload))

        sim.publish = capture_publish
        client = AsyncMock()
        await self._fault_def(sim, 1).on_activate(client)
        await self._fault_def(sim, 1).on_recover(client)
        assert published == []

    def test_compressor_keeps_cycling_while_valve_stuck(self):
        sim = IceMakerSimulator()
        self._force_active(sim, 1)
        for _ in range(61):  # 305s > 300s off-cycle
            sim.tick(dt=5.0)
        assert sim.compressor_on is True

    def test_harvests_alternate_when_healthy(self):
        sim = IceMakerSimulator()
        for _ in range(2 * 180):  # 1800s = two harvest intervals
            sim.tick(dt=5.0)
        drops = [e for e in sim._pending_events if e.event == "ice_dropped"]
        assert [e.detail for e in drops] == ["evaporator_1", "evaporator_2"]

    def test_stuck_valve_turn_fails_other_succeeds(self):
        sim = IceMakerSimulator()
        self._force_active(sim, 1)
        for _ in range(2 * 180):
            sim.tick(dt=5.0)
        fails = [e for e in sim._pending_events if e.event == "failed_cycle"]
        drops = [e for e in sim._pending_events if e.event == "ice_dropped"]
        assert [e.detail for e in fails] == ["hot_gas_valve_1_stuck"]
        assert [e.detail for e in drops] == ["evaporator_2"]

    def test_both_stuck_yields_only_failed_cycles(self):
        sim = IceMakerSimulator()
        self._force_active(sim, 1)
        self._force_active(sim, 2)
        for _ in range(2 * 180):
            sim.tick(dt=5.0)
        drops = [e for e in sim._pending_events if e.event == "ice_dropped"]
        fails = [e for e in sim._pending_events if e.event == "failed_cycle"]
        assert drops == []
        assert len(fails) == 2
```

- [ ] **Step 2: Run to verify failures**

Run: `uv run pytest tests/test_simulator_ice_maker.py -q`
Expected: many FAILs (sensor counts, fault names, missing `hot_gas_valve_1`, alternation details). Pre-change behavior has no `evaporator_N` details and suppresses drops under the old fault.

- [ ] **Step 3: Implement in `simulators/ice_maker.py`**

1. `SENSOR_DEFS`: replace the `hot_gas_valve` entry with two entries, both `target_on=75.0, target_off=30.0, rate=0.04, noise=0.5`, names `hot_gas_valve_1` and `hot_gas_valve_2`.

2. `__init__`: add `self._next_harvest_valve = 1` next to `_ice_drop_elapsed`. Replace the `defrost_stuck` registration with:

```python
        for valve in (1, 2):
            self.register_fault(
                FaultDef(
                    name=f"defrost_stuck_{valve}",
                    category="short",
                    probability=0.0015,
                    on_activate=self._make_valve_stuck_activate(valve),
                    on_recover=self._make_valve_stuck_recover(valve),
                    message=(
                        f"Hot gas valve {valve} stuck — "
                        f"harvest failing on evaporator {valve}"
                    ),
                    severity="warning",
                )
            )
```

3. Delete `_on_defrost_stuck_activate` and `_on_defrost_stuck_recover`; add the factories (note: no `publish` calls — the machine has no self-diagnostics; monitoring must infer from temps and failed harvests):

```python
    def _make_valve_stuck_activate(self, valve: int):
        """The machine has no fault detection: pin the valve temp, publish nothing."""

        async def _activate(client: aiomqtt.Client) -> None:
            sensor = self._sensor_by_name(f"hot_gas_valve_{valve}")
            sensor.target_on = 95.0
            sensor.target_off = 95.0
            logger.warning(
                f"[ice_maker] FAULT: hot gas valve {valve} stuck — "
                "machine unaware, still cycling"
            )

        return _activate

    def _make_valve_stuck_recover(self, valve: int):
        async def _recover(client: aiomqtt.Client) -> None:
            original = next(
                d for d in SENSOR_DEFS if d["name"] == f"hot_gas_valve_{valve}"
            )
            sensor = self._sensor_by_name(f"hot_gas_valve_{valve}")
            sensor.target_on = original["target_on"]
            sensor.target_off = original["target_off"]
            logger.info(f"[ice_maker] Fault cleared: defrost_stuck_{valve}")

        return _recover
```

4. Rework `tick()` — replace the whole method with:

```python
    def tick(self, dt: float):
        """Advance the simulation by dt seconds."""
        active = self._active_fault_names

        # compressor_overtemp: safety cutout — machine fully halted
        if "compressor_overtemp" in active:
            for sensor in self.sensors:
                sensor.update(self.compressor_on, dt)
            self._check_temp_bounds()
            return

        # water_inlet_blocked: no water — machine halted
        if "water_inlet_blocked" in active:
            for sensor in self.sensors:
                sensor.update(self.compressor_on, dt)
            self._check_temp_bounds()
            return

        # Normal cycling. low_refrigerant and defrost_stuck_1/2 do NOT halt
        # anything — this machine has no fault detection and keeps running.
        self._cycle_elapsed += dt
        cycle_time = (
            self.COMPRESSOR_ON_TIME if self.compressor_on else self.COMPRESSOR_OFF_TIME
        )
        if self._cycle_elapsed >= cycle_time:
            self.compressor_on = not self.compressor_on
            self._cycle_elapsed = 0.0
            event_type = "power_on" if self.compressor_on else "power_off"
            self._pending_events.append(IceMakerEvent(event=event_type))
            logger.info(
                f"[ice_maker] Compressor {'ON' if self.compressor_on else 'OFF'}"
            )

        for sensor in self.sensors:
            sensor.update(self.compressor_on, dt)

        self._check_temp_bounds()

        # Harvest attempts alternate evaporators and never pause for stuck
        # valves — a stuck valve's turn simply fails.
        self._ice_drop_elapsed += dt
        if self._ice_drop_elapsed >= self.ICE_DROP_INTERVAL:
            self._ice_drop_elapsed = 0.0
            valve = self._next_harvest_valve
            self._next_harvest_valve = 2 if valve == 1 else 1
            if f"defrost_stuck_{valve}" in active:
                self._pending_events.append(
                    IceMakerEvent(
                        event="failed_cycle",
                        detail=f"hot_gas_valve_{valve}_stuck",
                    )
                )
                logger.warning(
                    f"[ice_maker] Harvest FAILED on evaporator {valve} (valve stuck)"
                )
            else:
                self._pending_events.append(
                    IceMakerEvent(event="ice_dropped", detail=f"evaporator_{valve}")
                )
                logger.info(f"[ice_maker] Ice dropped from evaporator {valve}")
```

5. Module docstring: change "9 temperature sensors" to "10 temperature sensors (including two hot gas valves)". Update the `ha_discovery_entities` comment "# 9 temperature sensors" to "# 10 temperature sensors".

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/test_simulator_ice_maker.py -q`
Expected: all pass. If `self._active_fault_names` turns out not to support the `in` operator as used (check `simulators/base.py` if anything fails there), adapt the tick code to whatever collection type base exposes — do not change base.py.

- [ ] **Step 5: Full suite**

Run: `uv run pytest -q`
Expected: 352+ passed (net: this file's test delta), 9 skipped. If a test OUTSIDE this file fails on `hot_gas_valve`/`defrost_stuck` names, fix per Task 2's rules (test-only updates); production code failures = BLOCKED.

- [ ] **Step 6: Lint + commit**

Run `uv run ruff check --fix .` then `uv run ruff format .` (separate calls).

```bash
git add simulators/ice_maker.py tests/test_simulator_ice_maker.py
git commit -m "feat: dual hot gas valves with independent stuck faults, dumb-machine semantics

Two evaporators alternate harvests; a stuck valve publishes no self-report,
keeps the compressor cycling, and fails only its own harvest turns. Detection
is external: pinned ~95C valve temp (temp_out_of_bounds) and failed_cycle events.

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 2: Stale-reference sweep

**Files:**
- Possibly modify: any test or doc referencing the old names (NOT simulators/base.py, NOT controller/vmc.py logic).

**Interfaces:**
- Consumes: the new names from Task 1.

- [ ] **Step 1: Sweep**

Grep (tool, not shell; the regex engine has no lookahead) for the plain strings `hot_gas_valve` and `defrost_stuck` across `*.py` and `*.md`, excluding `colder-docker/`, `simulators/ice_maker.py`, `tests/test_simulator_ice_maker.py`, and this plan/spec pair. Manually discard hits that already carry a `_1`/`_2` suffix; what remains are stale old-name references.

- For each hit in tests or docs (e.g., `tests/test_integration_e2e.py`, CLAUDE.md): update to the new names, keeping assertions equivalent (e.g., a test asserting suppression under `defrost_stuck` becomes the per-valve equivalent consistent with Task 1's semantics — a stuck valve now FAILS its harvest rather than suppressing all drops; adjust the assertion to the new behavior, not the old).
- A hit in production code other than `simulators/ice_maker.py` → STOP, report BLOCKED with the file/line.
- Zero hits → this task is a no-op; skip to Step 3.

- [ ] **Step 2: Verify**

Run: `uv run pytest -q` — full suite green (e2e tests are auto-skipped without a broker; still update their text per Step 1 since they run in broker environments).

- [ ] **Step 3: Commit (only if changes were made)**

```bash
git add -u
git commit -m "chore: update stale hot_gas_valve/defrost_stuck references to per-valve names

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Post-merge note (controller, not part of the plan)

The live ice-maker simulator process started earlier is still running the old code; restart it (`uv run python -m simulators.ice_maker --broker hpz440`) after the plan lands so the dashboard shows both valves.
