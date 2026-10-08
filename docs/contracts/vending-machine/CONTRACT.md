# Vending Machine Contract — v0.8.0

This document and the JSON Schema files in `schemas/` define the interface
between the ice-colder VMC and the vending ESP32 firmware plus the MDB
payment gateway. The full sequence vocabulary, capabilities document,
heartbeat/LWT rules and interlock MUSTs follow in Phase B (`ROADMAP.md`
§4–§5, §9).

The `schemas/*.schema.json` files are generated from
`contracts/vending_machine.py` by `uv run python -m contracts.generate` and
are the normative payload definitions.

## Topic map

All topics are relative to `vmc/{machine_id}/`.

| Topic | Direction | Payload | Notes |
|---|---|---|---|
| `hardware/dispenser` | ESP32 → VMC | `DispenserStatus` (`services/mqtt_messages.py`) | `state` is a [`DispenserOutcome`](schemas/dispenser_outcome.schema.json) for terminal states, or a [`DispenseStep`](schemas/dispense_step.schema.json) (`agitate`/`fill`/`release`) for an intermediate step. `request_id` (v0.6.0) echoes the command-channel `dispense` request for both test runs and production sales (v0.8.0: production sales moved onto the command channel too — see `cmd/vending` below). `detail` (v0.8.0, optional) carries board-supplied text, e.g. `"stall 4.2 A"` or `"412 pulses"` |
| `cmd/payment/refund` | VMC → gateway | [`PaymentRefundCommand`](schemas/payment_refund_command.schema.json) | QoS 1; `request_id` unique per refund |
| `cmd/payment/refund/ack` | gateway → VMC | [`PaymentRefundResult`](schemas/payment_refund_result.schema.json) | QoS 1; exactly one per command; repeated `request_id` re-sends the stored result, never pays twice |
| `cmd/payment/enable` | VMC → gateway | `PaymentEnableCommand {accept: bool}` | QoS 1; not retained; the VMC republishes on connect and whenever the `mdb` subsystem returns |
| `cmd/vending` | VMC → ESP32 | [`SubsystemCommand`](schemas/subsystem_command.schema.json) | QoS 1; test and control commands with per-subsystem params; **answer deadline 10 seconds** |
| `cmd/vending/ack` | ESP32 → VMC | [`CommandAck`](schemas/command_ack.schema.json) | QoS 1; exactly one per command, correlated by `request_id`; idempotency: ESP32 keeps the last 32 `request_id`s and replays cached acks on duplicates |
| `cmd/mdb` | VMC → gateway | [`SubsystemCommand`](schemas/subsystem_command.schema.json) | QoS 1; test and control commands; **answer deadline 10 seconds** |
| `cmd/mdb/ack` | gateway → VMC | [`CommandAck`](schemas/command_ack.schema.json) | QoS 1; exactly one per command, correlated by `request_id`; idempotency: gateway keeps the last 32 `request_id`s and replays cached acks on duplicates |
| `alerts` | VMC → world | `VMCAlert` (`services/mqtt_messages.py`) | carries a [`FaultCode`](schemas/fault_code.schema.json) when one applies |
| `capabilities/<subsystem>` | subsystem → VMC | [`SubsystemCapabilities`](schemas/subsystem_capabilities.schema.json) | retained; MUST be published on connect and re-published on any change; `firmware`, `hardware_id`, `ip` identify the board; `commands` lists all commands the firmware supports (test, control, and production together) |

## Command channel

Every subsystem (vending, mdb, ice_maker) subscribes to `vmc/<machine_id>/cmd/<subsystem>` and acknowledges on `vmc/<machine_id>/cmd/<subsystem>/ack`. The request and ack payloads are defined by the shared models [`SubsystemCommand`](schemas/subsystem_command.schema.json) and [`CommandAck`](schemas/command_ack.schema.json).

A subsystem **must answer within 10 seconds** (`ACK_TIMEOUT_SECONDS` in `contracts/common.py`), or the VMC treats it as a timeout and may retry with the same `request_id`. **This 10-second deadline is for the ACK only.** Read "Two timeouts: accept vs. complete" below — as of v0.6.0 the ack for a long-running command means "I started this," not "I finished this."

### Two timeouts: accept vs. complete (v0.6.0)

A previous review of the Tests level (the operator-facing dashboard tile that runs these commands against a real machine, `POST /tests/{subsystem}/{command}`) proved a real bug: `dispense`'s handler used to await the entire motor cycle — which can legitimately run past 20 seconds, and up to 90 seconds on a jam — before sending any ack at all. The VMC's 10-second × 2-attempt ack budget ran out and gave up long before the actuator actually stopped, and the operator's safety lease (which is supposed to keep the machine out of service for the whole test) released while the motor was still running.

The fix adds a **second, independent timeout**, and a field on the ack that says which phase you are in:

- **`CommandAck.phase`** — `"accepted"` or `"completed"`, default `"completed"`. Every ack a present-day (pre-v0.6.0) implementation sends is implicitly `"completed"`, so a payload with no `phase` key at all still validates and still means exactly what it always meant.
- **The 10-second ack deadline above governs ONLY the first ack** — whichever phase it is in. It is unchanged.
- **A second, per-command "completion timeout" governs how long the VMC will wait, after an `"accepted"` ack, for that command to actually finish.** It is much longer than 10 seconds, and — where the command takes a duration parameter — it is derived from that parameter, not a fixed constant. See the completion table below.

**What firmware must do for each command is in the table below.** The short version:

- If your handler finishes the real work before it acks at all — the three standard commands, and (verify this against your own hardware; do not assume) any actuator command that truly completes synchronously — send ONE ack, `phase="completed"` (the default; you don't have to set it explicitly). Nothing else changes for you.
- If your handler only STARTS the real work before acking — because finishing takes longer than is reasonable to hold the VMC waiting for a single ack — send an ack immediately with `phase="accepted"` as soon as the command is accepted and the actuator has actually started moving, then report completion separately once the real work is done, using whichever completion signal the table below specifies for that command. **Never ack `"accepted"` for something you have not actually started** — an accepted-but-not-yet-started ack would let the VMC believe the actuator lease is protecting real hardware movement when it is not yet.

#### Completion table

| Subsystem | Command | Kind | Completion signal | Completion timeout |
|---|---|---|---|---|
| vending | `ping` | immediate | the ack itself | — (10 s ack deadline only) |
| vending | `self_test` | immediate | the ack itself | — |
| vending | `force_report` | immediate | the ack itself | — |
| vending | `dispense` | **long-running** | the existing terminal `hardware/dispenser` report (`DispenserStatus`, `state` a `DispenserOutcome`: `complete`/`bin_empty`/`timeout`/`jam`/`error`/`door_open`/`no_flow`/`over_dispense`), now carrying this command's `request_id` | **fixed 120 s** — no duration parameter to derive from; a fixed margin over the worst case (the 90 s auger-jam path) |
| vending | `water_valve` | **long-running** | a SECOND ack on `cmd/vending/ack`, same `request_id`, `phase="completed"` | `seconds` (param, 1–10) **+ 5 s margin** → 6–15 s |
| mdb | `ping` | immediate | the ack itself | — |
| mdb | `self_test` | immediate | the ack itself | — |
| mdb | `force_report` | immediate | the ack itself | — |
| mdb | `bill_acceptor_test` | immediate | the ack itself | — |
| mdb | `coin_return_test` | immediate | the ack itself | — |
| mdb | `card_reader_test` | immediate | the ack itself | — |
| ice_maker | `power_cycle` | **long-running** | a SECOND ack on `cmd/ice_maker/ack`, same `request_id`, `phase="completed"` | `dwell_seconds` (param, 5–300) **+ 30 s margin** → 35–330 s (see [ice-maker-monitor CONTRACT.md](../ice-maker-monitor/CONTRACT.md)) |
| ice_maker | `ping` / `self_test` / `force_report` | immediate | the ack itself | — |

`dispense`'s completion channel was deliberately kept as the existing `hardware/dispenser` report — spec-mandated, and already exactly what a production sale publishes, test run or not (v0.8.0: there is no longer a separate production path) — rather than inventing a second ack, since that report already IS the ground truth for whether the motor finished. `water_valve` and `power_cycle` instead use a second ack on the same topic and `request_id` they already ack on: no new topic or payload shape is needed, and the existing idempotency cache (below) already keys on `request_id`, so a subsystem only has to re-cache the newer (completed) ack over the accepted one it cached first — a duplicate `request_id` arriving after completion then correctly replays the FINAL outcome, not "accepted" forever.

**Why `water_valve`'s and `power_cycle`'s completion timeouts scale with their own parameter, not a fixed constant:** `power_cycle`'s `dwell_seconds` can legitimately be as long as 300 seconds (the monitor's own re-trigger lockout window) — a fixed 120-second completion timeout would spuriously fail a completely legitimate 300-second dwell. The same reasoning applies to `water_valve`'s `seconds` (1–10): the timeout must always exceed the longest legitimate real duration of the command by a comfortable margin, or a slow-but-correct actuator gets flagged as failed.

### Production sales use the command channel (v0.8.0)

**This is the sentence to read first.** A production sale is sent on the command channel (`cmd/vending`, command `dispense`) exactly like a test run; `cmd/dispense` is no longer published. `cmd/payment/enable` / `cmd/payment/refund` are unchanged. A board that only ever answered the legacy bare `cmd/dispense` topic must be updated to the command channel to keep selling ice or water.

### Idempotency requirement on real firmware

**Every subsystem MUST keep the last 32 `request_id`s it has received, along with their ack responses.** When a duplicate `request_id` arrives, the subsystem **MUST republish the cached ack without repeating the side effect**. This is not an implementation note — it is a contract requirement enforced by the dispatcher's retry mechanism: if a retry re-executed the action, a timeout and retry would dispense two bags of ice, or perform the motor test twice.

### Standard commands (every subsystem)

Every subsystem answers these commands:

- **`ping`**: no parameters; ack status `ok` with no result. Proves round-trip connectivity.
- **`self_test`**: no parameters; ack status `ok` with result `{checks: [{name: "...", pass: true|false, detail: "..."}]}`. Each check represents a subsystem-specific health point (bus present, sensor responds, motor current, etc.). Firmware chooses its own checks.
- **`force_report`**: no parameters; ack status `ok` with no result. Causes the subsystem to republish every sensor reading, telemetry channel, heartbeat, and capability document immediately. The VMC's health monitor then shows fresh values.

An unknown command answered with status `unsupported`.

### Vending-specific commands

- **`dispense`** — parameters: [`DispenseCommand`](schemas/dispense_command.schema.json): `{slot: int, mechanism: "bagged_ice" | "water_fill", profile: <SlotProfile>}` — all three required, no defaults; `profile` is the slot's whole validated [`SlotProfile`](schemas/slot_profile.schema.json) (`services/dispenser_schema.py`), carried with every dispense so the board is stateless about configuration. `profile.mechanism` must equal `mechanism` or the payload is rejected. Runs the slot's mechanism one cycle, publishing `hardware/dispenser` with the outcome — identical whether this is a test run or a production sale (v0.8.0: there is no longer a separate production path). Acked and idempotent, like every other command channel command. **Long-running (v0.6.0): acks `phase="accepted"` as soon as the mechanism actually starts, then runs it; completion is the terminal `hardware/dispenser` report — see the completion table above.**
- **`water_valve`** — parameters: `{seconds: int, range 1–10}`. Opens the water valve for the specified duration. Validation failure (`seconds` outside [1, 10]) returns ack status `rejected` with detail message. **Long-running (v0.6.0): acks `phase="accepted"` once the valve has actually opened, then a second, `phase="completed"` ack once it has closed again — see the completion table above.**

### MDB-specific commands

- **`bill_acceptor_test`** — no parameters. Cycles the bill acceptor's stacker motor; ack status `ok` with no result.
- **`coin_return_test`** — no parameters. Actuates the coin return; ack status `ok` with no result.
- **`card_reader_test`** — no parameters. Asks the card reader to run its own diagnostic; ack status `ok` with **`result`** carrying the reader's status text as a dict (e.g. `{"status": "reader OK, firmware 3.2"}`) — the only MDB command whose ack carries a `result`.

### Validation failures

A command whose parameters fail the contract's bounds (e.g., `water_valve` with `seconds: 0` or `11`, or `power_cycle` with `dwell_seconds` outside [5, 300]) is answered with status `rejected` and a detail message — never silently. A payload with no usable `request_id` (e.g., empty, missing, or not a string) is dropped with **no ack at all**, because no ack can be correlated to it.

### Capabilities advertisement

`SubsystemCapabilities.commands` lists **every** command the firmware supports, including the three standard commands (`ping`, `self_test`, `force_report`), the actuator commands (`dispense`, `water_valve`, `bill_acceptor_test`, `coin_return_test`, `card_reader_test`), and any control commands (`set_interval` for the ice maker). The VMC maintains a server-side allowlist, `TESTABLE_COMMANDS` in `contracts/common.py`, containing exactly the standard commands and the test-mode actuator commands; this allowlist is separate from what firmware advertises. A test button in the dashboard appears only for a command that is both allowlisted and advertised by the subsystem, ensuring firmware that ignores the command channel remains unaffected and a crafted request cannot invoke a control command through the test UI.

## Semantics fixed in 0.8.0

- `FaultCode` gains `CFG-101` (no valid dispenser profile for a slot;
  product-scope, `product_unavailable` severity) and `CFG-102`
  (dispensers.toml could not be read; machine-scope, `warning` severity).
- Neither `CFG-101` nor `CFG-102` appears in `PAYMENT_BLOCKING_FAULTS`, so
  a missing or bad-profile slot locks only that product, never payment
  machine-wide.
- **`DispenseCommand`** (`services/mqtt_messages.py`) is the `dispense`
  command's params: `{slot: int (ge=0), mechanism: "bagged_ice" |
  "water_fill", profile: SlotProfile}` — all three required, no defaults.
  `profile` is the slot's whole validated `SlotProfile`
  (`services/dispenser_schema.py`, plan 1), so the board is stateless
  about configuration and a `dispensers.toml` save mid-vend cannot affect
  an in-flight command. A payload whose `profile.mechanism` does not
  match `mechanism` is rejected.
- **`DispenseStep`** (`agitate`, `fill`, `release`) lists the intermediate
  `hardware/dispenser` states this contract's boards emit before a
  terminal `DispenserOutcome`. Water-fill slots only ever emit `fill`.
- **`DispenserOutcome`** gains three terminal states: `door_open`
  (release completed but the door sensor never reported closed within
  `close_timeout_seconds` — this is a **successful vend for the
  customer**, since the bag did drop, but raises the critical, never
  auto-clearing `ICE-402`), `no_flow` (water: no flow after the valve
  opened), and `over_dispense` (water: flow continued past the target
  volume by more than `over_dispense_percent`).
- **`OUTCOME_FAULTS`** is now keyed by `(mechanism, outcome)`, and
  `fault_for_outcome(mechanism, outcome)` is the only reader — it raises
  `KeyError` naming both when the pair has no mapping (`complete` is
  never mapped):

  | mechanism | outcome | fault |
  |---|---|---|
  | bagged_ice | timeout | ICE-301 |
  | bagged_ice | error | ICE-302 |
  | bagged_ice | jam | ICE-401 |
  | bagged_ice | door_open | ICE-402 |
  | water_fill | no_flow | WTR-101 |
  | water_fill | over_dispense | WTR-102 |
  | water_fill | timeout | WTR-101 |
  | water_fill | error | ICE-302 |
  | bagged_ice | bin_empty | ICE-101 |
  | water_fill | bin_empty | ICE-101 |

  `ICE-302` is reused for a water-fill `error` because the board reports
  a generic actuator fault either way; its description is now mechanism-
  agnostic: "Dispense actuator fault reported by the board (motor stall,
  valve driver, over-current)" (was "Dispense/agitator motor fault").
  Severity and scope are unchanged.
- **`DispenserStatus.detail`** (optional) carries board-supplied text for
  a terminal report, e.g. `"stall 4.2 A"` or `"412 pulses"`.
  **`DispenserStatus.request_id`** now echoes the command-channel
  `dispense` request for both test runs and production sales — not test
  runs only, as before 0.8.0 — because production sales move onto the
  command channel in this version (next bullet). The VMC logs a mismatch
  but keys completion on `slot` and FSM state, never on this field.
- A production sale is sent on the command channel (`cmd/vending`,
  command `dispense`) exactly like a test run; `cmd/dispense` is no
  longer published.

## Semantics fixed in 0.7.0

- `ChannelDescriptor` (`contracts/common.py`) gains two optional fields:
  `direction` (`"input"` | `"output"`, default `"input"`) — output means
  something the board drives (motor, solenoid, relay, compressor), input
  means something it senses — and `driven_by` (`str | None`, default
  `None`) — for an output (or a payment device), the command name whose
  refusal by the VMC inhibits that signal.
- `driven_by`, when set, names a command present in the same board's own
  `SubsystemCapabilities.commands` list.
- Both fields are additive with defaults; every present-day
  `ChannelDescriptor` payload still validates unchanged.
- The dashboard renders only channels a board actually declares in its
  capabilities document — a reading for an undeclared channel is still
  consumed by the VMC for control but never shown on any window.

## Semantics fixed in 0.6.0

- `CommandAck.phase` (`"accepted"` | `"completed"`, default `"completed"`)
  distinguishes "started" from "finished" for a long-running command
  (`dispense`, `water_valve`). See "Two timeouts: accept vs. complete"
  above for the full completion table and both timeouts.
- The ack deadline (`ACK_TIMEOUT_SECONDS`, 10 s) is unchanged and governs
  only the first ack. A command's own completion timeout is separate,
  longer, and — for `water_valve` — derived from its own duration
  parameter.
- No wire shape changed for any existing field; every present-day
  `SubsystemCommand`/`CommandAck` payload still validates unchanged.

## Semantics fixed in 0.5.0

- The VMC finishes a sale on `DispenserOutcome.complete` or `door_open`
  for the slot it commanded (v0.8.0: `door_open` is also a successful
  vend, see above). `bin_empty`, `timeout`, `jam`, `error`, `no_flow`,
  `over_dispense` end the sale as a failed vend and raise the fault from
  `fault_for_outcome(mechanism, outcome)` (v0.8.0: mechanism-aware; see
  the "Semantics fixed in 0.8.0" table).
- No terminal report within the VMC's configured dispense timeout is a
  failed vend (`PAY-102`).
- Refund ack deadline is 10 s; the VMC retries once with the same
  `request_id`, then raises `PAY-103`.
- Transaction uncertain after VMC restart (a persisted open sale found on
  boot) is `PAY-104` (warning, machine scope). It alerts the operator and
  holds the session snapshot as evidence until an operator clears it; it does
  **not** inhibit payment and does not block product selection.
- Only the codes in `PAYMENT_BLOCKING_FAULTS` inhibit payment: `ICE-402`,
  `WTR-103`, `WTR-104`, `ENV-102`, `ENV-103`, `PWR-102`, `SVC-102` (maintenance
  test in progress). Every other fault alerts and may block an individual sale,
  but never stops the machine taking money.
- Fault codes are stable; see `ROADMAP.md` §5 for the registry.
- The VMC expects `vending`, `mdb` and `ice_maker` (`EXPECTED_SUBSYSTEMS`); a
  subsystem that has never published a heartbeat is shown as never seen.

## Reference implementation

`simulators/vending_machine.py` publishes the outcomes; `simulators/mdb_gateway.py`
answers refunds (including the `changer_empty` failure path).
