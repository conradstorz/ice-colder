# Vending Machine Contract — v0.5.0

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
| `hardware/dispenser` | ESP32 → VMC | `DispenserStatus` (`services/mqtt_messages.py`) | `state` is a [`DispenserOutcome`](schemas/dispenser_outcome.schema.json) for terminal states; any other string is an intermediate step |
| `cmd/dispense` | VMC → ESP32 | as production, for real sales | unchanged; each dispense publishes `hardware/dispenser` with the outcome |
| `cmd/payment/refund` | VMC → gateway | [`PaymentRefundCommand`](schemas/payment_refund_command.schema.json) | QoS 1; `request_id` unique per refund |
| `cmd/payment/refund/ack` | gateway → VMC | [`PaymentRefundResult`](schemas/payment_refund_result.schema.json) | QoS 1; exactly one per command; repeated `request_id` re-sends the stored result, never pays twice |
| `cmd/payment/enable` | VMC → gateway | `PaymentEnableCommand {accept: bool}` | QoS 1; not retained; the VMC republishes on connect and whenever the `mdb` subsystem returns |
| `cmd/vending` | VMC → ESP32 | [`SubsystemCommand`](schemas/subsystem_command.schema.json) | QoS 1; test and control commands with per-subsystem params; **answer deadline 10 seconds** |
| `cmd/vending/ack` | ESP32 → VMC | [`CommandAck`](schemas/command_ack.schema.json) | QoS 1; exactly one per command, correlated by `request_id`; idempotency: ESP32 keeps the last 32 `request_id`s and replays cached acks on duplicates |
| `alerts` | VMC → world | `VMCAlert` (`services/mqtt_messages.py`) | carries a [`FaultCode`](schemas/fault_code.schema.json) when one applies |
| `capabilities/<subsystem>` | subsystem → VMC | [`SubsystemCapabilities`](schemas/subsystem_capabilities.schema.json) | retained; MUST be published on connect and re-published on any change; `firmware`, `hardware_id`, `ip` identify the board; `commands` lists all commands the firmware supports (test, control, and production together) |

## Command channel

Every subsystem (vending, mdb, ice_maker) subscribes to `vmc/<machine_id>/cmd/<subsystem>` and acknowledges on `vmc/<machine_id>/cmd/<subsystem>/ack`. The request and ack payloads are defined by the shared models [`SubsystemCommand`](schemas/subsystem_command.schema.json) and [`CommandAck`](schemas/command_ack.schema.json).

A subsystem **must answer within 10 seconds** (`ACK_TIMEOUT_SECONDS` in `contracts/common.py`), or the VMC treats it as a timeout and may retry with the same `request_id`.

### Idempotency requirement on real firmware

**Every subsystem MUST keep the last 32 `request_id`s it has received, along with their ack responses.** When a duplicate `request_id` arrives, the subsystem **MUST republish the cached ack without repeating the side effect**. This is not an implementation note — it is a contract requirement enforced by the dispatcher's retry mechanism: if a retry re-executed the action, a timeout and retry would dispense two bags of ice, or perform the motor test twice.

### Standard commands (every subsystem)

Every subsystem answers these commands:

- **`ping`**: no parameters; ack status `ok` with no result. Proves round-trip connectivity.
- **`self_test`**: no parameters; ack status `ok` with result `{checks: [{name: "...", pass: true|false, detail: "..."}]}`. Each check represents a subsystem-specific health point (bus present, sensor responds, motor current, etc.). Firmware chooses its own checks.
- **`force_report`**: no parameters; ack status `ok` with no result. Causes the subsystem to republish every sensor reading, telemetry channel, heartbeat, and capability document immediately. The VMC's health monitor then shows fresh values.

An unknown command answered with status `unsupported`.

### Vending-specific commands

- **`dispense`** — parameters: `{slot: int}`. Runs the slot's motor one cycle, publishing `hardware/dispenser` with the outcome, identical to a production `cmd/dispense` sale. Test mode makes this acked and idempotent.
- **`water_valve`** — parameters: `{seconds: int, range 1–10}`. Opens the water valve for the specified duration. Validation failure (`seconds` outside [1, 10]) returns ack status `rejected` with detail message.

### Validation failures

A command whose parameters fail the contract's bounds (e.g., `water_valve` with `seconds: 0` or `11`, or `power_cycle` with `dwell_seconds` outside [5, 300]) is answered with status `rejected` and a detail message—never silence. A payload with no usable `request_id` (e.g., empty or oversized) is dropped because no ack can be correlated.

### Production topics unchanged

The VMC keeps publishing `cmd/dispense` for real sales and `cmd/payment/enable` / `cmd/payment/refund` exactly as before. The command channel is purely additive: firmware that does not implement it simply continues vending and advertises no tests (`SubsystemCapabilities.commands` stays empty).

### Capabilities advertisement

`SubsystemCapabilities.commands` lists **every** command the firmware supports, including the three standard commands (`ping`, `self_test`, `force_report`), the actuator commands (`dispense`, `water_valve`), and any control commands (`set_interval` for the ice maker). The VMC maintains a server-side allowlist, `TESTABLE_COMMANDS` in `contracts/common.py`, containing exactly the standard commands and the test-mode actuator commands; this allowlist is separate from what firmware advertises. A test button in the dashboard appears only for a command that is both allowlisted and advertised by the subsystem, ensuring firmware that ignores the command channel remains unaffected and a crafted request cannot invoke a control command through the test UI.

## Semantics fixed in 0.5.0

- The VMC finishes a sale only on `DispenserOutcome.complete` for the slot
  it commanded. `bin_empty`, `timeout`, `jam`, `error` end the sale as a
  failed vend and raise the fault in `OUTCOME_FAULTS`.
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
