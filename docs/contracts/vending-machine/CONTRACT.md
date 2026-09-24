# Vending Machine Contract — v0.4.0 (stub)

This document and the JSON Schema files in `schemas/` define the interface
between the ice-colder VMC and the vending ESP32 firmware plus the MDB
payment gateway. Version 0.1.0 covers only what the VMC needs for honest
vend outcomes and acked refunds. The full sequence vocabulary, capabilities
document, heartbeat/LWT rules and interlock MUSTs follow in Phase B
(`ROADMAP.md` §4–§5, §9).

The `schemas/*.schema.json` files are generated from
`contracts/vending_machine.py` by `uv run python -m contracts.generate` and
are the normative payload definitions.

## Topic map

All topics are relative to `vmc/{machine_id}/`.

| Topic | Direction | Payload | Notes |
|---|---|---|---|
| `hardware/dispenser` | ESP32 → VMC | `DispenserStatus` (`services/mqtt_messages.py`) | `state` is a [`DispenserOutcome`](schemas/dispenser_outcome.schema.json) for terminal states; any other string is an intermediate step |
| `cmd/payment/refund` | VMC → gateway | [`PaymentRefundCommand`](schemas/payment_refund_command.schema.json) | QoS 1; `request_id` unique per refund |
| `cmd/payment/refund/ack` | gateway → VMC | [`PaymentRefundResult`](schemas/payment_refund_result.schema.json) | QoS 1; exactly one per command; repeated `request_id` re-sends the stored result, never pays twice |
| `cmd/payment/enable` | VMC → gateway | `PaymentEnableCommand {accept: bool}` | QoS 1; not retained; the VMC republishes on connect and whenever the `mdb` subsystem returns |
| `alerts` | VMC → world | `VMCAlert` (`services/mqtt_messages.py`) | carries a [`FaultCode`](schemas/fault_code.schema.json) when one applies |
| `capabilities/<subsystem>` | subsystem → VMC | [`SubsystemCapabilities`](schemas/subsystem_capabilities.schema.json) | retained; MUST be published on connect and re-published on any change; `firmware`, `hardware_id`, `ip` identify the board |

## Semantics fixed in 0.4.0

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
  `WTR-103`, `WTR-104`, `ENV-102`, `ENV-103`, `PWR-102`. Every other fault
  alerts and may block an individual sale, but never stops the machine taking
  money.
- Fault codes are stable; see `ROADMAP.md` §5 for the registry.
- The VMC expects `vending`, `mdb` and `ice_maker` (`EXPECTED_SUBSYSTEMS`); a
  subsystem that has never published a heartbeat is shown as never seen.

## Reference implementation

`simulators/vending_machine.py` publishes the outcomes; `simulators/mdb_gateway.py`
answers refunds (including the `changer_empty` failure path).
