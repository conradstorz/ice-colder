# Dual Hot Gas Valves in the Ice Maker Simulator — Design

**Date:** 2026-09-13
**Status:** Approved approach: rename to `hot_gas_valve_1`/`_2`, per-valve faults,
dumb-machine failure semantics.

## Goal

Model a two-evaporator ice machine: two hot gas valves, each with its own
temperature sensor and its own independently-occurring "stuck" fault — under
the constraint that **the real machine has no built-in failure detection and
keeps trying to make ice**. Failures show up only in what an outside monitor
can observe: temperatures and missing/failed harvests, never a self-reported
halt.

## Decisions (from brainstorming)

1. **Naming:** replace the single `hot_gas_valve` sensor with
   `hot_gas_valve_1` and `hot_gas_valve_2` (topics
   `sensors/temp/hot_gas_valve_1|_2`). The old topic disappears.
2. **Faults:** replace `defrost_stuck` with independent `defrost_stuck_1`
   and `defrost_stuck_2` (short category, probability 0.0015 each,
   severity warning).
3. **Dumb machine (user constraint):** a stuck valve does NOT halt the
   machine and does NOT emit `halt`/`resume` self-reports. The compressor
   keeps cycling; the machine keeps attempting harvests.

## Non-Goals

- Changing the other faults (`compressor_overtemp`, `low_refrigerant`,
  `water_inlet_blocked` keep their current halt semantics — an overtemp
  cutout is plausibly a real hardware safety, and they are out of scope).
- Modeling per-evaporator water circuits or production tonnage.
- Any VMC/HealthMonitor changes — detection stays inference-from-telemetry
  (`temp_out_of_bounds`, missing `ice_dropped` cadence), which already works.

## Design (all in `simulators/ice_maker.py` + its tests)

### 1. Sensors

`SENSOR_DEFS`: drop the `hot_gas_valve` entry; add `hot_gas_valve_1` and
`hot_gas_valve_2`, both with the original parameters
(`target_on=75.0, target_off=30.0, rate=0.04, noise=0.5`). Sensor count goes
9 → 10. HA discovery, the health panel, and the dashboard pick both up
automatically (they iterate `self.sensors`).

### 2. Per-valve stuck faults — dumb-machine semantics

`defrost_stuck_1` / `defrost_stuck_2` (`FaultDef`, category `short`,
probability 0.0015 each, severity `warning`, message
"Hot gas valve N stuck — harvest failing on evaporator N"):

- **on_activate:** pin only that valve's sensor (`target_on = target_off =
  95.0`). Log a warning. **No MQTT event is published** — the machine
  doesn't know. (95 °C exceeds `TEMP_HIGH=80`, so the existing
  `_check_temp_bounds` starts emitting `temp_out_of_bounds` events every
  tick; that plus the temperature stream is how monitoring finds out.)
- **on_recover:** restore that valve's original targets from `SENSOR_DEFS`.
  Log. **No `resume` event** — temps simply normalize.
- Both faults roll independently; both can be active at once.

### 3. Ice production under failure

The machine keeps trying. Harvests alternate between the two evaporators
(valve 1, valve 2, valve 1, ...), tracked by a `_next_harvest_valve` index
toggling on every harvest attempt:

- Every `ICE_DROP_INTERVAL` (unchanged, 900 s) a harvest is **attempted**
  whenever the machine is running — stuck valves never pause the timer.
  (Halting faults — `compressor_overtemp`, `water_inlet_blocked` — still
  stop the machine entirely, harvests included, as today.)
- If the attempting evaporator's valve is healthy → `ice_dropped` event
  (detail `"evaporator_N"`).
- If that valve is stuck → `failed_cycle` event (detail
  `"hot_gas_valve_N_stuck"`) — the machine tried and failed; ice output
  halves with one valve stuck, stops with both stuck, but the attempt
  cadence never changes.

### 4. Compressor cycling under failure

`tick()`'s halt-cycling branch currently treats ANY active fault (except
`low_refrigerant`) as halting. Change it to a set of halting faults:
`{"compressor_overtemp", "water_inlet_blocked"}` — `defrost_stuck_1/_2`
(like `low_refrigerant`) leave the compressor cycling normally.
`compressor_overtemp` keeps its special full-halt branch.

### 5. Tests (`tests/test_simulator_ice_maker.py`)

Update references to the old `hot_gas_valve` / `defrost_stuck` names, then
cover:

- Sensor count is 10; both `hot_gas_valve_1` and `hot_gas_valve_2` exist
  and publish (topic per sensor).
- `defrost_stuck_1` activation pins valve 1's targets at 95.0 while valve
  2's targets remain stock; recovery restores valve 1's originals.
- Activation publishes **no** `ice_maker/event` (assert the publish mock
  saw no halt/resume), and the compressor continues cycling
  (`compressor_on` still toggles across enough ticked time).
- Harvest alternation: healthy machine alternates `ice_dropped` details
  `evaporator_1`/`evaporator_2`.
- One valve stuck → its harvest turns produce `failed_cycle` with detail
  `hot_gas_valve_N_stuck`, the other valve's turns still produce
  `ice_dropped`; both stuck → only `failed_cycle` events.
- HA discovery includes `hot_gas_valve_1_temp` and `hot_gas_valve_2_temp`
  entities and no `hot_gas_valve_temp`.

## Observability summary (how a monitor detects a stuck valve)

| Signal | Healthy | Valve N stuck |
|---|---|---|
| `sensors/temp/hot_gas_valve_N` | 30–75 °C cycling | pinned ~95 °C |
| `ice_maker/event` | `ice_dropped` every ~15 min alternating evaporators | `failed_cycle` on N's turns; `temp_out_of_bounds` every publish |
| Compressor `power_on`/`power_off` | cycling | unchanged (machine is oblivious) |
