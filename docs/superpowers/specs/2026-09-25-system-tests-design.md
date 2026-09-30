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
  duration; credit that arrives anyway is refunded, never escrowed.

## Approach

Generalize the ice maker's command/ack pattern to every subsystem and give
the VMC one `CommandDispatcher`. Rejected: the simulator-only
`cmd/sim/inject_fault` channel (never exists on real firmware) and HTTP side
channels to the ESP32s (MQTT is the bus).

## 1. Contract

### 1.1 Command channel, all subsystems

Every subsystem subscribes to `vmc/<machine_id>/cmd/<subsystem>` and
publishes acks on `vmc/<machine_id>/cmd/<subsystem>/ack`. Payloads are the
ice-maker contract's existing `MonitorCommand` and `CommandAck` shapes,
unchanged on the wire, moved to `contracts/common.py` as `SubsystemCommand`
and `CommandAck` with the ice-maker module re-exporting them under the old
names. The only addition is an optional `result` field on the ack; every
existing field, including the required `command` and the optional `detail`,
stays, so today's ice-maker firmware and the VMC's current ack handler keep
validating:

```json
// request (as today)
{"request_id": "hex", "command": "self_test", "params": {}, "timestamp": "..."}
// ack (as today, plus optional result)
{"request_id": "hex", "command": "self_test",
 "status": "ok" | "rejected" | "failed" | "unsupported",
 "detail": "optional text", "result": {...optional, command-specific...},
 "timestamp": "..."}
```

The `command` literal on the request model widens to `str`, with
per-command param validation moved to a registry keyed by command name so
the ice maker's `power_cycle` rule (`dwell_seconds` in 5–300) is preserved.

Unknown commands answer `unsupported`. A subsystem answers within 10 s or
the VMC treats it as a timeout.

**2026-09-29 amendment — the ack means "accepted", not "done", for a
long-running command.** The sentence above ("a subsystem answers within
10 s or the VMC treats it as a timeout") is now only true of the FIRST
ack. A Copilot review on PR 22 (id=4128088504) proved, with a real VMC and
a real maintenance lease, that `SVC-102` cleared — and the lease released
— while a simulated dispense motor was still running: `dispense`'s handler
awaited the whole motor cycle (which can legitimately exceed 20 s, and
reach 90 s on the jam path) before acking at all, so the dispatcher's 10 s
× 2-attempt ack budget ran out and gave up long before the actuator
actually stopped.

The fix adds a second phase, `CommandAck.phase` (`"accepted"` |
`"completed"`, default `"completed"`):

- **Immediate commands** — the three standard commands (§1.2) and, per
  subsystem, every actuator command whose handler finishes within the ack
  (verified against each simulator handler, not assumed: the three MDB
  actuator tests) — are unaffected. `phase` stays `"completed"`, the
  default, so a present-day ack payload with no `phase` key at all still
  validates and still means exactly what it always meant: the ack IS the
  outcome. The 10 s ack deadline above is unchanged and is the WHOLE
  timeout for these.
- **Long-running commands** (`dispense`, `water_valve`, `power_cycle`) ack
  `phase="accepted"` as soon as the subsystem has started the real work —
  still within the same 10 s ack deadline, unchanged — and report
  completion separately, later: `dispense`'s completion is its existing
  terminal `hardware/dispenser` report (now carrying the command's
  `request_id` for correlation); `water_valve` and `power_cycle` each send
  a SECOND, `phase="completed"` ack on the same topic and `request_id`.
  Each has its own, separate, much more generous completion timeout,
  derived from the command's own duration where one is parameterized
  (`services/command_dispatcher.py`'s `send_and_await_completion`,
  `contracts/common.py`'s `COMPLETION_TIMEOUTS`) — see the completion
  table in both `docs/contracts/*/CONTRACT.md` files for the exact values.
  `services/command_dispatcher.py`'s `send()` keeps its original,
  unchanged meaning (resolve on the first ack, whatever its phase) so
  every existing caller — and the ack-timeout retry behaviour itself — is
  untouched; only `send_and_await_completion()` (used by the Tests level's
  `_run_command`) additionally waits for the second, completion phase.
  `VMC.maintenance_test_run()` brackets that whole call, so
  `runs_in_flight` — and therefore the maintenance lease and `SVC-102` —
  now stays held for the actuator's REAL lifetime, not just until it
  starts.

**Idempotency is part of the contract.** Every subsystem keeps the last 32
`request_id`s it has acked with their acks and, on a duplicate, republishes
the cached ack without repeating the side effect. The ice-maker simulator
already does this (`_acked`); the vending and MDB handlers must, and the
contract document states it as a requirement for real firmware. The
dispatcher's retry (§2.1) depends on it.

**Production topics are untouched.** The VMC keeps publishing dispenses for
real sales on `cmd/dispense` exactly as today, and `cmd/payment/enable` and
`cmd/payment/refund` stay where they are. The command channel is additive;
deployed firmware that ignores it simply advertises no tests.

### 1.2 Standard commands (every subsystem)

| Command | Params | Result | Kind |
|---|---|---|---|
| `ping` | none | none; ack alone proves the round trip | automatic |
| `self_test` | none | `{"checks": [{"name": "...", "pass": true, "detail": "..."}]}`; firmware decides its own checks (bus present, sensor responds, motor current sane) | automatic |
| `force_report` | none | none; the subsystem republishes every sensor, channel, and its heartbeat immediately | automatic; the dashboard shows the fresh values from the health monitor |

### 1.3 Actuator commands (per subsystem, advertised in capabilities)

| Subsystem | Command | Params | Effect |
|---|---|---|---|
| vending | `dispense` | `{"slot": int}` | Run the slot's motor one cycle; publishes `hardware/dispenser` as in a sale. Same firmware action as the production `cmd/dispense` topic, reached through the command channel so it is acked and idempotent |
| vending | `water_valve` | `{"seconds": 1–10}` | Open the water valve for that long |
| ice_maker | `power_cycle` | `{"dwell_seconds": 5–300}` | Exists today; the Tests UI offers a keypad defaulting to 30 |
| mdb | `bill_acceptor_test` | none | Cycle the acceptor's stacker motor |
| mdb | `coin_return_test` | none | Actuate the coin return |
| mdb | `card_reader_test` | none | Ask the reader to run its own diagnostic; result carries the reader's status text |

`SubsystemCapabilities.commands` keeps listing every command the firmware
supports, including control commands that are not tests (`payment/enable`,
`refund`, `set_interval`). The Tests UI never renders from that list
directly. Instead `contracts/common.py` defines the server-side allowlist
`TESTABLE_COMMANDS: dict[subsystem, frozenset[str]]` containing exactly the
standard commands in §1.2 and the actuator commands in §1.3, and a test
button exists only for a command that is both allowlisted and advertised.
`POST /tests/{subsystem}/{command}` re-checks the allowlist, so a crafted
request cannot invoke `refund` through the test tile. Contract versions bump
(`vending-machine` and `ice-maker-monitor` CONTRACT.md and the Python
models); the health tab's existing contract-mismatch warning covers firmware
that has not been updated, and such a subsystem shows "no tests advertised".

## 2. VMC side

### 2.1 `services/command_dispatcher.py`

```python
class CommandDispatcher:
    def __init__(self, mqtt_client, timeout: float = 10.0, retries: int = 1, clock=...)
    async def send(self, subsystem: str, command: str, params: dict | None = None) -> CommandAck
    async def send_and_await_completion(self, subsystem: str, command: str, params: dict | None = None) -> CommandAck  # 2026-09-29
```

Registers `cmd/+/ack` once, correlates by `request_id`, retries once on
timeout **with the same `request_id`** so the subsystem's idempotency cache
(§1.1) replays the ack instead of repeating the action, and raises
`CommandTimeout(subsystem, command)` after the last attempt. Refunds keep
their own path in this spec; migrating them is a follow-up.

**2026-09-29 amendment.** `send()` is unchanged: it still resolves on the
first ack for a `request_id`, whatever its `phase` (§1.1). `send_and_await_
completion()` is new: it calls `send()` unchanged for the accept phase,
then — only for a command listed in `contracts.common.COMPLETION_TIMEOUTS`
— additionally waits for that command's own completion signal, with its
own separate, params-derived timeout, raising `CompletionTimeout(subsystem,
command)` if it never arrives. The Tests level's `_run_command` (§3) is the
one caller that uses it; every other consumer of `CommandDispatcher` is
unaffected.

### 2.2 Maintenance hold

New fault `SVC-102 maintenance test in progress` in
`contracts/vending_machine.py` `FAULT_TABLE`, scope machine, gate class
`safety`, so `services/availability.py` publishes `cmd/payment/enable false`
and blocks sales while it is raised. It is added to
`PAYMENT_BLOCKING_FAULTS`. (`SVC-101` already means "service door open" and
is untouched.)

The hold is a **lease**: `MaintenanceHold(holder_user_id, holder_session_id,
started_at, last_activity_at, runs_in_flight: int)`.

- `VMC.begin_maintenance(user, session) -> bool` grants the lease only when
  the FSM is `idle` with zero escrow and no lease exists; otherwise returns
  False and the dashboard says why ("machine is mid-sale", "held by
  <name>"). Each test start increments `runs_in_flight` and refreshes
  `last_activity_at`; each completion, timeout, or failure decrements it.
- `VMC.end_maintenance(session) -> bool` releases the lease only for the
  holder's session and only when `runs_in_flight == 0`; otherwise it marks
  `release_requested` and the lease is released by the last in-flight run
  settling. A different owner or tech sees "held by <name>" with a
  **Take over** button that is enabled only when `runs_in_flight == 0` and
  the lease has been idle for 60 s; take-over records who did it.
- The idle timer (5 minutes since `last_activity_at`) behaves like a release
  request: it never clears a lease with runs in flight.
- The Tests level posts `/tests/end` on Home/Back and on
  `htmx:beforeHistoryUpdate`; the timer is the backstop for a closed tab.

**Credit during a hold.** `deposit_funds` today escrows credit even while
payment is disabled (an in-flight coin is still a customer's money). While
a lease exists it instead refunds it immediately through the existing
`request_refund(reason="maintenance")` path and records the `refund` event;
the credit never enters escrow and cannot be spent after the hold ends.
Payment is disabled for the whole lease, so this covers only the race
between the disable command and a coin already in the mechanism.

### 2.2a Standby: taking the machine out of service

The lease in §2.2 is *opportunistic*: it is granted only when the machine
happens to be idle with no credit, and it releases itself after five idle
minutes. On a busy machine (a customer walking away with credit on the
display, or a simulator that never stops pressing buttons) that lease can
never be taken, so every test refuses with "machine is mid-sale". A tech at
the machine needs the opposite: an explicit way to *make* it idle and keep
it out of service for as long as they are logged in.

**Standby** is the same `MaintenanceHold` with `standby: bool = True`,
granted by `VMC.begin_standby(user_id, session_id) -> (bool, reason)`:

- From `interacting_with_user`, or `idle` with credit on the machine: any
  escrow is refunded through the existing `request_refund(reason="maintenance")`
  path, the customer session timer is cancelled, the selection is cleared
  and the FSM is forced to `idle` (the `cancel_sale` transition for
  `interacting_with_user`). Then the lease is granted.
- From `error`: escrow is refunded the same way and the lease is granted
  with the FSM left in `error` — the fault that parked it there is still
  the operator's to clear. Command tests work in this state; a test sale
  refuses as it does for any non-idle FSM.
- From `dispensing`: refused with "vend finishing, tap again" — a running
  motor is never aborted, and a vend lasts seconds.
- When a lease already exists: refused with "held by <id>" unless the caller's
  own session holds it, in which case the existing lease is *upgraded* to
  standby in place (its idle timer is cancelled).

A standby lease differs from the opportunistic one in exactly two ways:

1. **No idle release.** The five-minute idle timer is never armed for it,
   and `_maintenance_idle_expired` ignores it. The machine stays out of
   service until the holder presses **Return to service** (`/tests/end`)
   or their web session ends.
2. **Bound to the holder's web session.** Lock (which posts `/logout`), the
   shared-device idle lock, the absolute session cap, and the user being
   disabled all end the session; the lease follows within 30 s. Because
   `AccessStore` expires sessions lazily (only when the cookie is next
   presented), the VMC sweeps: while a standby lease exists it asks a
   liveness predicate every 30 s and releases with reason `session_ended`
   when the holder's session is gone. The predicate is
   `AccessStore.session_is_live(session_id) -> bool`, which applies the
   same idle-limit and absolute-cap rules as `resolve_session` **without
   refreshing `last_active_at`** — the sweep must never keep the session
   alive that it is checking. It is wired in `main.py` via
   `VMC.set_session_liveness(predicate)`; with no predicate wired, a
   standby lease behaves like the opportunistic one (idle timer armed).

Both kinds of lease share everything else: `runs_in_flight` deferral of a
release, credit refunded on arrival, `SVC-102` raised for the whole lease,
takeover after 60 s idle with no run in flight, never persisted.

**Tests level.** A card at the top of `/tests` shows the machine's service
state. In service: a two-tap **Take out of service** button (the shared
`partials/confirm_button.html`) posting `/tests/standby`. Out of service:
"Out of service since HH:MM, held by <name>" and **Return to service**
(`/tests/end`) for the holder, **Take over** for anyone else. When a test
is refused because the machine is busy, the refusal reads "machine is busy
— take it out of service first" and sits next to that button. Run all,
single commands and test sales still take the opportunistic lease on an
idle machine exactly as before, so a quick test needs no extra tap.

**Home hero.** While `SVC-102` is active the hero reads "Out of service —
maintenance by <name>" in place of the fault line. The pill already shows
it through the fault.

### 2.3 Test sales

Test-ness is a property of the sale, not of the global hold. The VMC's
in-flight sale context (the selected product plus part 3's pending shares)
gains `is_test: bool`, set only by `run_test_sale`. The dispense completion
handler consults the sale's own flag: for a test sale it records neither
`sale` nor `dispense` and writes the `test_run` event instead (§4). A lease
release or timer expiry cannot flip a sale from test to production because
the flag lives on the sale, and the lease cannot be released while the run
is in flight (§2.2).

`VMC.run_test_sale(sku) -> TestSaleResult`: requires the lease; increments
`runs_in_flight`; deposits the product's price as one credit with method
`test` (the only credit `deposit_funds` accepts during a lease); selects
the product; runs the normal dispense path so the real FSM, the production
`cmd/dispense` command, and completion handling are exercised; awaits the
dispenser completion with the existing dispense timeout; returns the FSM
path taken and the outcome (`dispensed`, `vend_failed <code>`, `timeout`).
Escrow is cleared without a refund command. The operator then records Pass
or Fail.

## 3. Tests level

Gate `run_tests` (owner, tech). URLs under part 2's `/tests`:

| Level | URL | Body |
|---|---|---|
| Tests | `/tests` | One card per subsystem with alive state, firmware, contract match, count of testable commands (advertised ∩ allowlist); **Run all automatic** button; **Simulated sale** and **Test log** tiles; the current lease holder if any. Entering the level does not take the lease; starting a test does |
| Subsystem | `/tests/{subsystem}` | Testable commands in two groups. Automatic: `ping`, `self_test`, `force_report`, each a Run button. Actuator: the allowlisted rest, each with its params (slot picker from the catalog, seconds or dwell keypad with the contract's range) and a Run button |
| Run | `POST /tests/{subsystem}/{command}` | Rejects a command outside the allowlist (403); takes the lease (or returns the refusal inline); sends through the dispatcher; swaps in the result card: automatic tests show the ack status, round-trip time, and each check; actuator tests show "Watch the machine" then **Pass** / **Fail** buttons and an optional note field |
| Verdict | `POST /tests/runs/{run_id}/verdict` | Records `pass` / `fail` and the note on that run |
| Run all | `POST /tests/run-all` | `ping` and `self_test` on every alive subsystem in sequence; one result table |
| Simulated sale | `/tests/sale` → `POST /tests/sale` | SKU picker; runs §2.3; shows the FSM path and outcome; Pass / Fail buttons |
| End | `POST /tests/end` | Releases the caller's own lease once nothing is in flight; called on leaving the level |
| Take over | `POST /tests/takeover` | Transfers an idle lease to the caller (§2.2) |
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
- `simulators/vending_machine.py`: keeps `cmd/dispense` for production;
  adds `dispense` and `water_valve` on the channel, sharing the motor code.
- `simulators/ice_maker.py`: `power_cycle` and `set_interval` move onto the
  shared loop with their existing param validation; the `_acked` cache
  becomes the base-class idempotency cache.
- `simulators/mdb_gateway.py`: the three MDB actuator commands, each
  logging and acking `ok`, `card_reader_test` returning a status text.
- Each simulator's capabilities `commands` list is extended accordingly.

## 6. Error handling

- Timeout: result card shows "no answer from <subsystem> after 2 attempts";
  logged with status `timeout`; the run is no longer in flight. The lease
  stays until the operator leaves or the timer expires.
- `rejected` / `failed` / `unsupported` acks show the message verbatim.
- A test started while the FSM is not idle is refused before anything is
  sent, with a pointer to **Take out of service** (§2.2a).
- Broker down: the dispatcher raises immediately with the broker fault code;
  the Tests level shows every subsystem as unreachable.
- The maintenance lease is never persisted: a restart clears it, matching
  the FSM's own reset semantics.
- A credit that arrives during a lease is refunded, not escrowed (§2.2).

## 7. Testing

- `tests/test_command_dispatcher.py`: ack correlation, ignore foreign
  request ids, retry reuses the request id, timeout raises, concurrent sends
  to different subsystems.
- `tests/test_contracts.py`: today's ice-maker ack payload validates against
  the moved `CommandAck`; `power_cycle` without `dwell_seconds` is rejected;
  `TESTABLE_COMMANDS` excludes `payment/enable`, `refund`, `set_interval`.
- `tests/test_vmc.py` additions: lease granted only when idle, availability
  publishes payment disable, a credit during the lease is refunded not
  escrowed, `end_maintenance` by a non-holder is refused, release waits for
  in-flight runs, idle timer never releases with a run in flight, take-over
  only when idle, a test sale records no sale or dispense even if the lease
  is released mid-run, `run_test_sale` path and outcome for dispensed,
  failed, and timed-out dispenser.
- `tests/test_simulators.py` additions: each command acks; a duplicate
  request id replays the ack without a second side effect; `self_test` fails
  the injected fault's check; unknown command is `unsupported`; `cmd/dispense`
  still works.
- `tests/test_event_recorder.py`: `test_run` rows, `update_metadata`,
  excluded from summaries.
- Route tests: discovery renders only advertised-and-allowlisted commands;
  a POST for an advertised control command is 403; secretary and loader get
  403; run-all sequence; verdict updates the log; leaving the level releases
  the caller's lease; a second tech sees "held by" and can take over only
  when idle; simulated sale result card.
- Contract tests: the shared request/ack models validate the documented
  examples.

## 8. Files

| File | Change |
|---|---|
| `contracts/common.py` | New: `SubsystemCommand`, `CommandAck` (moved from the ice-maker module, re-exported there, wire-compatible), param registry, `TESTABLE_COMMANDS` |
| `contracts/vending_machine.py`, `contracts/ice_maker_monitor.py` | Version bump, `SVC-102`, command lists |
| `docs/contracts/*/CONTRACT.md` | Command channel, standard and actuator commands |
| `services/command_dispatcher.py` | New |
| `services/availability.py` | `SVC-102` as a safety row |
| `services/event_recorder.py` | `test_run`, `update_metadata` |
| `controller/vmc.py` | Maintenance lease, refund-during-lease, per-sale `is_test`, `run_test_sale` |
| `simulators/base.py`, `vending_machine.py`, `ice_maker.py`, `mdb_gateway.py` | Shared command loop with idempotency cache, actuator handlers, capabilities |
| `main.py` | Construct the dispatcher, hand it to the VMC and routes |
| `web_interface/routes/tests.py`, `templates/tests*.html` | New level |
| `tests/test_command_dispatcher.py` and additions to existing test files | New and extended |
| `CLAUDE.md`, `ROADMAP.md` | Document the command channel and maintenance hold |

## 9. Out of scope

- Migrating refunds onto the dispatcher.
- Scheduled or automatic self-tests without an operator.
- Firmware implementation on the real ESP32s (the contract is the
  deliverable; simulators prove it). Because production topics are
  unchanged, deployed firmware keeps vending; it only lacks tests.
- Remote fault injection from the dashboard (the simulator channel stays a
  developer tool).
