# IM600XL Simulator — Design

> **Status:** Approved (brainstormed with owner), 2026-10-17. Makes the vending simulator, the board contract
> and the edge match the real machine described in
> [`docs/hardware/im600xl-io-list.md`](../../hardware/im600xl-io-list.md) (the "I/O list"). Tags such as IC4
> or W7 refer to that document.

## 1. Goal

The vending simulator must stand in for the real Kooler Ice IM600XL: declare exactly its channels, run its
ice and water sequences, and exercise the edge the way the real board will. Today it models a different,
better-instrumented machine (bin level, door sensor, motor current, cabinet temperature), and the edge relies
on some of that. This change removes the dependence on sensors the machine does not have, and adds the
inputs it does have.

## 2. Decisions

| # | Question | Decision |
|---|---|---|
| D1 | Scope | Everything in one spec: simulator, contract, edge, profiles, docs. |
| D2 | How ICE-101 (ice unavailable) clears with no bin sensor | **Auto-retry after a cooldown.** The lockout clears itself after `physical.ice_retry_cooldown_seconds` (default 1800). If the next fill also times out, it locks again. |
| D3 | How a named button selects a product | **Catalog field.** `Product.button` names a button channel id; the board reports the channel id; the edge looks the product up. |
| D4 | Trap-door catch (energized = hold) | A new bagged-ice release variant, `ReleaseByCatch`. |
| D5 | Sump high | Reports `WTR-104` (leak/overflow). It is critical and in `PAYMENT_BLOCKING_FAULTS`, so a flooded sump stops all sales. |
| D6 | RO tank empty | New `WTR-106`, water products unavailable, does not block payment. |
| D7 | RO production overrun | New `WTR-107`, warning only. |

## 3. Contract (`ice_contracts.board`, vending-machine **1.0.0 → 2.0.0**)

A major bump: D3 changes the type of an existing field on the wire.

### 3.1 `ButtonPress` (`mqtt_messages.py`)

`button: int` (list position, `ge=0`) becomes `button: str`, a channel id matching
`CHANNEL_ID_PATTERN`. The board publishes the id of the button input that was pressed (for example
`"button_ice"`). Every button the board can report must be declared in its capabilities as a binary input
channel.

### 3.2 `ReleaseByCatch` (`dispenser_schema.py`)

A third member of the `ReleaseStep` discriminated union:

| Field | Type | Meaning |
|---|---|---|
| `proof` | `Literal["catch"]` | Discriminator |
| `catch_channel` | `ChannelId` | The output that drives the electromagnetic catch |
| `drop_seconds` | float, 0.5–30 s | How long the catch stays released after the drop before the step completes (time for the door to fall open and self-close) |

Semantics, stated as MUSTs in the contract: the board **energizes the catch before any motion of the ice
sequence** (before agitate) and holds it through the fill; the release step **de-energizes** it to drop the
bag, waits `drop_seconds`, and reports the step complete. The catch is left de-energized between vends. A
fault anywhere in the sequence also de-energizes it. `catch_channel` is a drive channel for the existing
drive/sense conflict check, and `drop_seconds` counts toward `worst_case_seconds`. `ReleaseBySensor` and
`ReleaseTimed` are unchanged.

### 3.3 Fault codes (`vending_machine.py`)

| Code | Severity | Scope | Description | Payment-blocking |
|---|---|---|---|---|
| `WTR-106` (new) | `product_unavailable` | product | RO storage tank empty | No |
| `WTR-107` (new) | `warning` | machine | RO production ran past its maximum time without the tank reporting full | No |

`OUTCOME_FAULTS[("water_fill", bin_empty)]` changes from `ICE-101` to `WTR-106`. `PAYMENT_BLOCKING_FAULTS`
is unchanged (seven codes). Existing codes take on these inputs: sump → `WTR-104`, low pressure → `WTR-105`.
Low ambient temperature uses the existing health-monitor temperature alert; wiring `ENV-101` is out of scope.

### 3.4 Docs

`CONTRACT.md` gets a "Semantics fixed in 2.0.0 (breaking)" section covering 3.1–3.3; schemas are regenerated
with `uv run python -m ice_contracts.generate`.

## 4. Edge

### 4.1 Named buttons

- `ice_common.config_model.Product` gains `button: Optional[str] = None` (channel-id pattern). Config
  validation rejects two products with the same non-null `button`.
- `VMC.on_button_press` validates the new `ButtonPress`, finds the product whose `button` equals
  `press.button`, and selects it. An unknown button is logged and refused (journal line), never an
  exception.
- Selection stops being positional: `select_product` takes the product (or its sku) rather than a list
  index. `begin_test_sale` already holds the product and uses the same path. The "bad index" journal line
  becomes "unknown button".
- The Products catalog form gets a Button field (`edit_catalog`), validated the same way.

### 4.2 Ice availability without a bin sensor

- `Availability` learns from the vending board's validated capabilities whether `bin_half_full` is
  declared. If it is not, the `ice_available` row becomes **not instrumented** (passes, detail "no bin
  sensor"), so ice stays sellable. A board that does declare it keeps today's behaviour.
- `FaultService` arms a labelled timer, **`ice_retry_cooldown`**, whenever it raises `ICE-101` while the bin
  is not instrumented. When it fires, the `ICE-101` lockouts clear (`by="cooldown"`, recorded as today's
  `lockout_cleared` row). A further timed-out fill raises `ICE-101` again and re-arms the timer. Clearing by
  an operator cancels the timer. The timer is not persisted; after a restart an `ICE-101` lockout (itself
  not persisted) is simply gone.
- `ConfigModel.physical` gains `ice_retry_cooldown_seconds: float = 1800` (60–86400).

### 4.3 Water and service inputs

`Availability.set_hardware_io` gains these devices (all from `hardware/io/<device>`):

| Device | Row | Applies | Gate | Change |
|---|---|---|---|---|
| `ro_tank_empty` | `ro_tank_ok` (new) | water | fulfillment | New instrumented row; fails while true |
| `water_pressure_ok` | `water_pressure_ok` | water | fulfillment | Stub → instrumented; fails while false |
| `sump_high` | `no_leak` | water | safety (unchanged) | Stub → instrumented; fails while true |
| `service_door` | `service_door_closed` | both | safety | Unchanged |

Rows for inputs the board does not declare stay "not instrumented", as today.

The edge does not consume board alerts (there is no `alert/vending` subscription), so the **edge raises and
clears the faults itself** from the same `hardware/io` transitions, through `FaultService.raise_fault` /
`clear_fault` (`by="hardware"`):

| Input | Fault | Scope handling |
|---|---|---|
| `sump_high` true | `WTR-104` | machine |
| `water_pressure_ok` false | `WTR-105` | product: every `kind == "water"` sku |
| `ro_tank_empty` true | `WTR-106` | product: every `kind == "water"` sku |
| `ro_production` on longer than `physical.ro_max_run_seconds` (default 3600) | `WTR-107` | machine; cleared when `ro_production` next turns off |

The fault clears when the input recovers; a `hardware`-raised fault is never cleared by recovery of a
different input. `WTR-107` is detection only: stopping the RO pump at its limit is the board's job (§5.2).
`ConfigModel.physical` gains `ro_max_run_seconds: float = 3600` (60–86400).

## 5. Simulator (`ice_sims.vending_machine`)

### 5.1 Channels

Declared exactly as the I/O list's channel-id column:

| Kind | Inputs | Outputs |
|---|---|---|
| binary | `bag_full_sensor`, `button_ice`, `button_water_1gal`, `button_water_5gal`, `ro_tank_full`, `ro_tank_empty`, `water_pressure_ok`, `sump_high`, `service_door` | `agitator_motor`, `auger_motor`, `bag_fan`, `trap_door_catch`, `water_valve_solenoid`, `ro_production`, `heater_power` |
| counter | `fill_pulses` | — |
| temperature | `ambient` | — |

`driven_by`: the four ice outputs → `dispense`; `water_valve_solenoid` → `water_valve`; `ro_production` and
`heater_power` → none (autonomous). Removed: `cabinet`, `bin_half_full`, `door_sensor`, `fan`,
`vending_now_light`, `agitator_current`, `auger_current`, `water_flow`, `water_flow_sensor`,
`heater_relay`, `bag_drop_solenoid`.

### 5.2 Behaviour

- **Ice vend:** follows the profile; with `ReleaseByCatch`, energizes the catch first, runs the existing
  agitate/fill steps, de-energizes it to drop, waits `drop_seconds`. Fill motor current is `unmonitored`
  (no current channels).
- **Water vend:** opens the valve and counts `fill_pulses` (telemetry counter) against the target; the
  binary flow-sensor publishing goes away. Each vend drains the simulated RO tank by its volume.
- **RO fill loop:** a simulated tank level (gallons) with full and empty thresholds; `ro_production`
  runs while the tank is not full, refilling at a fixed rate, and the board turns it off at its own maximum
  run time (the firmware safety limit). `ro_tank_full`/`ro_tank_empty` follow the level. All three are
  published on `hardware/io`; the edge derives `WTR-106`/`WTR-107` from them (§4.3).
- **Environment:** `ambient` drifts on `sensors/temp/ambient`; `heater_power` follows a hysteresis on it.
- **Inputs:** `water_pressure_ok` (normally true), `sump_high` and `service_door` (normally false) are
  published on `hardware/io`; the edge derives their faults (§4.3).
- **Customer loop:** presses a random declared button id.
- **Fault injection:** remove `door_stuck_open` and `bag_drop_solenoid_stuck` (no sensor to detect them);
  `ice_bin_empty` now produces a fill timeout (`bin_empty` outcome) without any bin channel. Add
  `ro_tank_empty`, `low_pressure`, `sump_high`, `service_door_open`, `ro_fill_stuck`.
- **Home Assistant discovery:** entities derived from the channel list instead of the hard-coded nine.

## 6. Profiles and generated reference

`dispensers_doc.py` sample values and the regenerated `dispensers.example.toml` describe the IM600XL:
slot 1 bagged ice (`IceFillBySensor` on `auger_motor`/`bag_full_sensor`, agitate on `agitator_motor`,
`ReleaseByCatch` on `trap_door_catch`, accessory `bag_fan`), slots 2 and 3 water 1 gal and 5 gal
(`WaterFillByVolume` on `water_valve_solenoid`/`fill_pulses`). The example `config.json` products get
matching `button` values.

## 7. Error handling

- A `ButtonPress` with an unknown or unassigned button: refused with a journal line; the sale is unaffected.
- A `dispensers.toml` naming a removed channel: today's capability cross-check reports it (`CFG-101` for
  that slot) — no new path.
- A catch-release ice vend that times out in fill: `bin_empty`/`timeout` outcome as today; the board
  de-energizes the catch so the bag drops rather than hanging.

## 8. Testing

- **Contract:** `ButtonPress` rejects ints and bad ids; `ReleaseByCatch` validates bounds; worst-case time
  includes `drop_seconds`; `catch_channel` participates in conflict checks; new fault table rows; regenerated
  schemas drift-tested.
- **Edge:** button → product lookup (hit, unknown, duplicate rejected in config); test sale still selects
  by product; `ice_available` not instrumented when the board lacks `bin_half_full`; `ice_retry_cooldown`
  fires and clears via `FakeTaskRunner`, re-raise re-arms, operator clear cancels; the three new
  `set_hardware_io` devices drive their rows and gates; each §4.3 input raises and clears its fault, water
  codes land on every water sku and no other; `ro_production` held on past `ro_max_run_seconds` raises
  `WTR-107` and turning it off clears it.
- **Simulator:** exact channel list and `driven_by` map; catch energized before agitate and released at
  drop; water vend pulse count and tank drain; RO loop fill and its run-time cutoff; each new fault injection;
  HA entities match the channels.
- **Integration:** the end-to-end test sells ice and water against the new simulator with no
  `bin_half_full`.

## 9. Documentation updates

`CLAUDE.md` (contract version, timer labels now seven, fault codes, simulator channels, buttons);
the I/O list §7 (divergences resolved); `ROADMAP.md` §3 (rows now instrumented).

## 10. Out of scope

Real firmware; the customer-screen LCD (C3); the Scotsman serial tap; any change to the ice-maker-monitor
contract; bag-present or trap-door sensing (the machine has neither).
