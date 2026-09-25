# System Tests — Design

**Date:** 2026-09-25
**Status:** Approved
**Series:** Dashboard v2, part 4 of 4 (roles → v2 shell → sales reports → **system tests**)
**Depends on:** part 1 (`run_tests` permission, `current_user`), part 2 (Tests level), part 3 (test mode must not record sales)

## Context

An operator at the machine needs to exercise a subsystem and see whether it
works without making a sale. Nothing test-shaped exists: only the ice maker
has a request/ack command channel (`cmd/ice_maker`, `cmd/ice_maker/ack`), the
vending simulator listens on an undocumented `cmd/dispense`, and the VMC's
only ack-tracked flow is refunds. Every subsystem's retained capabilities
document already carries a `commands` list, which is the discovery hook.

Decisions made in brainstorming:

- Four kinds of test: actuator exercise, communication check, sensor
  read-back, end-to-end simulated sale.
- Operator taps Pass or Fail for tests that move hardware; automatic checks
  record their own verdict.
- Test runs are logged under the Tests tile, 90-day retention.
- Tests run only when the machine is idle, and payment is inhibited for the
  duration.

## Approach

Generalize the ice maker's command/ack pattern to every subsystem and give
the VMC one `CommandDispatcher`. Rejected: the simulator-only
`cmd/sim/inject_fault` channel (never exists on real firmware) and HTTP side
channels to the ESP32s (MQTT is the bus).

## 1. Contract

### 1.1 Command channel, all subsystems

Every subsystem subscribes to `vmc/<machine_id>/cmd/<subsystem>` and
publishes acks on `vmc/<machine_id>/cmd/<subsystem>/ack`. Payloads reuse the
ice-maker contract's shapes, promoted to `contracts/common.py`:

```json
// request
{"request_id": "hex", "command": "self_test", "params": {}, "timestamp": "..."}
// ack
{"request_id": "hex", "status": "ok" | "rejected" | "failed" | "unsupported",
 "message": "optional text", "result": {...optional, command-specific...},
 "timestamp": "..."}
```

Unknown commands answer `unsupported`. A subsystem answers within 10 s or
the VMC treats it as a timeout.

### 1.2 Standard commands (every subsystem)

| Command | Params | Result | Kind |
|---|---|---|---|
| `ping` | none | none; ack alone proves the round trip | automatic |
| `self_test` | none | `{"checks": [{"name": "...", "pass": true, "detail": "..."}]}`; firmware decides its own checks (bus present, sensor responds, motor current sane) | automatic |
| `force_report` | none | none; the subsystem republishes every sensor, channel, and its heartbeat immediately | automatic; the dashboard shows the fresh values from the health monitor |

### 1.3 Actuator commands (per subsystem, advertised in capabilities)

| Subsystem | Command | Params | Effect |
|---|---|---|---|
| vending | `dispense` | `{"slot": int}` | Run the slot's motor one cycle; publishes `hardware/dispenser` as in a sale. Replaces the undocumented `cmd/dispense` topic |
| vending | `water_valve` | `{"seconds": 1–10}` | Open the water valve for that long |
| ice_maker | `power_cycle` | none | Exists today |
| mdb | `bill_acceptor_test` | none | Cycle the acceptor's stacker motor |
| mdb | `coin_return_test` | none | Actuate the coin return |
| mdb | `card_reader_test` | none | Ask the reader to run its own diagnostic; result carries the reader's status text |

`SubsystemCapabilities.commands` lists every command the firmware supports,
standard ones included. The dashboard renders only advertised commands.
Contract versions bump (`vending-machine` and `ice-maker-monitor` CONTRACT.md
and the Python models); the health tab's existing contract-mismatch warning
covers firmware that has not been updated, and such a subsystem shows "no
tests advertised".

## 2. VMC side

### 2.1 `services/command_dispatcher.py`

```python
class CommandDispatcher:
    def __init__(self, mqtt_client, timeout: float = 10.0, retries: int = 1, clock=...)
    async def send(self, subsystem: str, command: str, params: dict | None = None) -> CommandAck
```

Registers `cmd/+/ack` once, correlates by `request_id`, retries once on
timeout, raises `CommandTimeout(subsystem, command)` after the last attempt.
Refunds keep their own path in this spec; migrating them is a follow-up.

### 2.2 Maintenance hold

New fault `SVC-101 maintenance test in progress` in
`contracts/vending_machine.py` `FAULT_TABLE`, scope machine, gate class
`safety`, so `services/availability.py` publishes `cmd/payment/enable false`
and blocks sales while it is raised. It is added to
`PAYMENT_BLOCKING_FAULTS`.

`VMC.begin_maintenance(user) -> bool` raises the hold only when the FSM is
`idle` with zero escrow and no hold exists; otherwise returns False and the
dashboard says why ("machine is mid-sale"). `VMC.end_maintenance()` clears
it. The hold auto-clears after 5 minutes of no test activity (each test start
resets the timer) and when the operator navigates away from the Tests level
(the level's page posts `/tests/end` on `htmx:beforeHistoryUpdate` and on
Home/Back; the timer is the backstop for a closed tab).

### 2.3 Test mode and the end-to-end sale

`VMC.test_mode: bool` is true while the hold is raised. In test mode
`record_sale` and `record("dispense")` are skipped, so reports and KPIs
ignore test activity; a `test_run` event is recorded instead (§4).

`VMC.run_test_sale(sku) -> TestSaleResult`: requires the hold; deposits the
product's price as one credit with method `test`; selects the product;
runs the normal dispense path so the real FSM, dispenser command, and
completion handling are exercised; awaits the dispenser completion with the
existing dispense timeout; returns the FSM path taken and the outcome
(`dispensed`, `vend_failed <code>`, `timeout`). Escrow is cleared without a
refund command. The operator then records Pass or Fail.

## 3. Tests level

Gate `run_tests` (owner, tech). URLs under part 2's `/tests`:

| Level | URL | Body |
|---|---|---|
| Tests | `/tests` | One card per subsystem with alive state, firmware, contract match, count of advertised tests; **Run all automatic** button; **Simulated sale** and **Test log** tiles. Entering the level does not raise the hold; starting a test does |
| Subsystem | `/tests/{subsystem}` | Advertised commands in two groups. Automatic: `ping`, `self_test`, `force_report`, each a Run button. Actuator: the rest, each with its params (slot picker from the catalog, seconds keypad) and a Run button |
| Run | `POST /tests/{subsystem}/{command}` | Raises the hold (or returns the refusal inline), sends through the dispatcher, swaps in the result card: automatic tests show the ack status, round-trip time, and each check; actuator tests show "Watch the machine" then **Pass** / **Fail** buttons and an optional note field |
| Verdict | `POST /tests/runs/{run_id}/verdict` | Records `pass` / `fail` and the note on that run |
| Run all | `POST /tests/run-all` | `ping` and `self_test` on every alive subsystem in sequence; one result table |
| Simulated sale | `/tests/sale` → `POST /tests/sale` | SKU picker; runs §2.3; shows the FSM path and outcome; Pass / Fail buttons |
| End | `POST /tests/end` | Ends maintenance; called on leaving the level |
| Log | `/tests/log` | Last 100 `test_run` events: when, who, subsystem, command, params, status, verdict, note, duration |

Each result card is also written to the log as it happens, so a test whose
verdict was never entered shows `verdict: none`.

## 4. Test log

`test_run` events in the existing `events` table (90-day retention):
`value` is the duration in seconds, `metadata` is
`{run_id, user_id, user_name, subsystem, command, params, status, checks,
verdict, note}`. The verdict POST updates the row's metadata in place by
`run_id` (a new `EventRecorder.update_metadata(run_id, **fields)` executed on
the writer thread). `get_summary` ignores `test_run`.

## 5. Simulators

- `simulators/base.py`: generic command loop on `cmd/<subsystem>` replacing
  the ice maker's private one; `ping`, `self_test`, `force_report`
  implemented once. `self_test` returns one check per registered `FaultDef`,
  failing the check whose fault is currently injected, so fault injection
  doubles as a test fixture.
- `simulators/vending_machine.py`: `dispense` moves from `cmd/dispense` to
  the channel; `water_valve` added.
- `simulators/ice_maker.py`: `power_cycle` and `set_interval` move onto the
  shared loop.
- `simulators/mdb_gateway.py`: the three MDB actuator commands, each
  logging and acking `ok`, `card_reader_test` returning a status text.
- Each simulator's capabilities `commands` list is extended accordingly.

## 6. Error handling

- Timeout: result card shows "no answer from <subsystem> after 2 attempts";
  logged with status `timeout`. The hold stays until the operator leaves or
  the timer expires.
- `rejected` / `failed` / `unsupported` acks show the message verbatim.
- A test started while the FSM is not idle is refused before anything is
  sent.
- Broker down: the dispatcher raises immediately with the broker fault code;
  the Tests level shows every subsystem as unreachable.
- The maintenance hold is never persisted: a restart clears it, matching
  the FSM's own reset semantics.

## 7. Testing

- `tests/test_command_dispatcher.py`: ack correlation, ignore foreign
  request ids, retry once, timeout raises, concurrent sends to different
  subsystems.
- `tests/test_vmc.py` additions: hold raise only when idle, availability
  publishes payment disable, auto-clear timer, test mode skips sale and
  dispense records, `run_test_sale` path and outcome for dispensed, failed,
  and timed-out dispenser.
- `tests/test_simulators.py` additions: each command acks; `self_test`
  fails the injected fault's check; unknown command is `unsupported`.
- `tests/test_event_recorder.py`: `test_run` rows, `update_metadata`,
  excluded from summaries.
- Route tests: discovery renders only advertised commands; secretary and
  loader get 403; run-all sequence; verdict updates the log; leaving the
  level ends maintenance; simulated sale result card.
- Contract tests: the shared request/ack models validate the documented
  examples.

## 8. Files

| File | Change |
|---|---|
| `contracts/common.py` | New: `SubsystemCommand`, `CommandAck` (moved from the ice-maker module, re-exported there) |
| `contracts/vending_machine.py`, `contracts/ice_maker_monitor.py` | Version bump, `SVC-101`, command lists |
| `docs/contracts/*/CONTRACT.md` | Command channel, standard and actuator commands |
| `services/command_dispatcher.py` | New |
| `services/availability.py` | `SVC-101` as a safety row |
| `services/event_recorder.py` | `test_run`, `update_metadata` |
| `controller/vmc.py` | Maintenance hold, test mode, `run_test_sale` |
| `simulators/base.py`, `vending_machine.py`, `ice_maker.py`, `mdb_gateway.py` | Shared command loop, actuator handlers, capabilities |
| `main.py` | Construct the dispatcher, hand it to the VMC and routes |
| `web_interface/routes/tests.py`, `templates/tests*.html` | New level |
| `tests/test_command_dispatcher.py` and additions to existing test files | New and extended |
| `CLAUDE.md`, `ROADMAP.md` | Document the command channel and maintenance hold |

## 9. Out of scope

- Migrating refunds onto the dispatcher.
- Scheduled or automatic self-tests without an operator.
- Firmware implementation on the real ESP32s (the contract is the deliverable;
  simulators prove it).
- Remote fault injection from the dashboard (the simulator channel stays a
  developer tool).
