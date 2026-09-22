# ice-colder Roadmap

This is the system-level plan for turning the software in this repo into the
control system of a real ice-and-water vending machine. `PLAN.md` records the
software migration that already happened (asyncio, MQTT, health monitor,
simulators, monitor contract). This document is about what is still missing
before a machine can sell product unattended, and in what order to build it.

The design rules here were shaped by an outside architecture review (Perplexity,
2026-09). Its central point stands: **a Raspberry Pi is an excellent supervisor,
historian and remote-access node, but it must never be the only thing that
stops a motor, closes a valve, or keeps a heater within limits.**

## 1. The machine

A commercially built ice vending machine whose factory controller is being
replaced. Physical subsystems:

| Subsystem | Parts | Notes |
|---|---|---|
| Ice production | Stand-alone commercial ice maker | Has its own controls; exposes enable/status; monitored by the separate ice-maker-monitor project over the [monitor contract](docs/contracts/ice-maker-monitor/CONTRACT.md) |
| Ice storage | Insulated hopper with agitator motor | Agitator breaks up bridged ice before/during dispense |
| Ice dispense | Dispense motor → chute | Drops ice into a staged bag |
| Bagging | Bag inflation fan, bag-full ice-level sensor, electric trap door | Fan inflates bag, sensor stops fill, trap door releases bag to customer |
| Water | 1-gal and 5-gal selection buttons, electric dispense valve, pulse flow meter | Volume is metered by pulses, not time |
| Payment | Autonomous cash system | Accepts cash on its own; the controller can tell it to sleep or to finish the transaction and return change |
| Freeze protection | Cabinet heater | Keeps non-hopper areas at or above 40 °F |
| Customer interface | Selection buttons, customer display | Display is driven by the VMC display controller |
| Remote ops | Network gateway, controlled power domains | Remote monitoring plus bounded power-cycle of payment system and ice maker |

All motors and actuators are believed to be on 24 VDC industrial control
(relay/contactor coils), to be confirmed by the field survey in Phase A.

## 2. Responsibility split

Three layers. Each lower layer must stay safe when everything above it is gone.

| Layer | Owns | Must survive |
|---|---|---|
| **Safety and power** (hardwired) | Fuses/breakers, contactors, motor overloads, independent freeze thermostat and heater high-limit, normally-closed water solenoid, leak shutdown, keyed service switch, E-stop where fitted | Everything, including ESP32 lockup |
| **Machine control** (ESP32 firmware) | The deterministic ice and water vend sequences, every interlock in §4, every bounded runtime, local fault detection, MDB/payment protocol, button input, sensor input | Loss of the RPi, the broker, and the internet |
| **Supervisor** (RPi, this repo) | Product catalog and pricing, sale bookkeeping and escrow, availability decisions, dashboard, event history, alerts, remote commands with audit, display content, Home Assistant visibility | Loss of internet; degraded when an ESP32 is missing |

Rules that follow from the split:

- A VMC command over MQTT is a **request**. The ESP32 checks its own
  interlocks before acting and reports what it actually did.
- Every motor, fan, valve and heater output has a **maximum on-time enforced
  in firmware**, independent of any MQTT message arriving.
- Loss of MQTT or heartbeat from the VMC puts the ESP32 into a **safe idle**:
  outputs off, payment inhibited, no new vends. It does not reboot itself and
  it does not retry a half-finished vend.
- The VMC never declares a vend successful because it sent the command. It
  waits for the ESP32's `complete` report for the slot it commanded. No
  terminal report within `physical.dispense_timeout_seconds` is a failed vend
  (`PAY-102`), never a sale.
- Remote actuation of motors, valves, doors or heaters outside a defined
  maintenance procedure is not a feature and will not be added.

The ice-maker-monitor contract is the model for this: capabilities, heartbeat
with Last-Will, idempotent commands by `request_id`, acks, a 300 s power-cycle
lockout enforced on the device side. The vending ESP32 gets the same treatment
(Phase B).

## 3. Availability permissives

Payment is enabled only when a vend is physically possible. The VMC computes
two flags from ESP32-reported state and publishes `cmd/payment/enable`
accordingly. A bag problem disables ice only; a leak, an open service door, a
power fault or a lost ESP32 disables both.

```
Ice_Sale_Available =
      vending ESP32 alive (heartbeat fresh, no LWT)
  AND payment gateway ready
  AND NOT service mode
  AND cabinet/service door closed
  AND no active critical fault
  AND ice available (hopper level or ice-maker bin status)
  AND bag present
  AND trap door proven closed
  AND 24 V control power good

Water_Sale_Available =
      vending ESP32 alive
  AND payment gateway ready
  AND NOT service mode
  AND cabinet/service door closed
  AND no active critical fault
  AND water pressure OK
  AND water treatment healthy (filter/UV/RO status where instrumented)
  AND NOT leak detected
  AND water valve proven closed (no flow pulses while closed)
  AND 24 V control power good
```

Implemented in `services/availability.py`: known inputs are evaluated, inputs
the firmware cannot report yet are listed as not instrumented and pass until
Phase B/D.

## 4. Product sequences and interlocks

The VMC's FSM stays coarse: `idle → interacting_with_user → dispensing → idle`,
with `error` reachable from anywhere. The fine-grained sequence below runs on
the vending ESP32, which reports each step on `hardware/dispenser` as an
intermediate `state` (the VMC already logs these as `DISPENSER: slot n,
state: ...`). The step names become the contract vocabulary in Phase B.

### Ice vend

| Step | ESP32 action | Advance when | Fail when |
|---|---|---|---|
| `precheck` | Re-read bag present, trap door closed, ice available, no fault | All true | Any false → `fault` before any motion |
| `bag_prepare` | Run fan | Bag inflated (sensor if fitted, else bounded fan time + bag present) | Fan timeout, bag lost |
| `agitate` | Run agitator | Pre-agitate time elapsed | Run feedback absent / overcurrent |
| `fill` | Run dispense motor (agitator optional) | Bag-full sensor true | Bag lost, motor fault, **max fill time** |
| `settle` | Stop motors, short dwell | Dwell elapsed with full still indicated | Bag lost |
| `door_open` | Command trap door open | Open limit confirmed | Open timeout |
| `door_hold` | Hold | Hold timer | — |
| `door_close` | Command trap door closed | Closed limit confirmed | **Close timeout → critical**, lock out ice |
| `complete` | Report `complete` | — | — |
| `fault` | Stop fan, agitator, dispenser; command door closed; report `jammed`/`error` | — | — |

Hard rules, enforced in firmware, never relaxed by an MQTT command:

- No dispense motor unless a bag is present and the trap door is proven closed.
- Stop the dispenser immediately if bag-present drops during fill.
- Stop the dispenser at max fill time even if the full sensor never trips.
- Never open the trap door while the dispenser or agitator is running.
- No new vend until the trap door is proven closed.
- A door that fails to close is a more urgent fault than one that fails to
  open; they get different codes.

### Water vend

| Step | ESP32 action | Advance when | Fail when |
|---|---|---|---|
| `precheck` | Pressure OK, treatment OK, no leak, no flow while closed, container present if instrumented | All true | Any false |
| `arm_counter` | Zero pulse counter, compute target pulses from calibration | Armed | Pulses seen while valve closed |
| `valve_open` | Energize normally-closed valve | First pulses within no-flow timeout | No-flow timeout → close, fault |
| `dispense` | Count pulses | Target reached | Overrun, leak, pressure loss, **max time** |
| `valve_close` | De-energize valve | Flow stops within confirmation window | **Flow continues → critical**, close upstream valve if fitted |
| `complete` | Report `complete` with actual pulse count | — | — |

Hard rules:

- Volume comes from calibrated pulses per gallon, measured on the installed
  plumbing, not from the meter's label and never from time alone.
- Max dispense time applies even while pulse counting works.
- No water sale while a leak or overflow input is active.
- No remote command opens the customer water valve.

## 5. Fault codes

Alerts today are free text. The registry below becomes a Pydantic enum shared
by the VMC, the vending ESP32 contract and the dashboard. Codes are stable once
published; new codes are added, never renumbered.

| Code | Meaning | Severity | Automatic response |
|---|---|---|---|
| `ICE-101` | Ice unavailable (hopper low / maker bin empty) | product unavailable | Inhibit ice, alert |
| `ICE-201` | Bag not detected | product unavailable | Inhibit ice, alert |
| `ICE-202` | Bag lost during fill | vend failed | Stop motors, refund path, alert |
| `ICE-301` | Fill timeout (full sensor never tripped) | lockout ice | Stop dispenser, lock out ice until service |
| `ICE-302` | Dispense/agitator motor fault (no run feedback / overcurrent) | lockout ice | Stop motors, lock out ice |
| `ICE-401` | Trap door failed to open | lockout ice | Stop, lock out ice until service |
| `ICE-402` | Trap door failed to close | **critical** | Lock out all ice vending, immediate alert |
| `WTR-101` | No flow after valve open | vend failed | Close valve, refund path |
| `WTR-102` | Over-dispense (pulses exceeded) | lockout water | Close valve, lock out water |
| `WTR-103` | Flow continues after valve close | **critical** | Close upstream valve if fitted, lock out water, immediate alert |
| `WTR-104` | Leak / overflow detected | **critical** | Close all water valves, inhibit both products |
| `WTR-105` | Pressure or treatment status failed | product unavailable | Inhibit water |
| `ENV-101` | Cabinet below freeze threshold | warning | Request heat, alert |
| `ENV-102` | Heater ineffective (low temperature persists) | **critical** | Alert, inhibit water |
| `ENV-103` | Heater high-limit tripped | **critical** | Alert |
| `PAY-101` | Payment device offline | product unavailable | Inhibit both products |
| `PAY-102` | Vend reported failed after credit taken | reconcile | Refund/retain per §7, alert |
| `PAY-103` | Refund not confirmed by payment gateway | warning | Alert; operator reconciles against the event history |
| `PAY-104` | Transaction uncertain after VMC restart | lockout | Payment inhibited until an operator clears the fault; snapshot in event history |
| `PWR-101` | Power restored after loss | info | Log, run self-test, keep payment inhibited until permissives pass |
| `PWR-102` | 24 V control supply bad | **critical** | Inhibit both products |
| `COM-101` | Vending ESP32 heartbeat lost / LWT | product unavailable | Inhibit both products, alert |
| `COM-102` | Ice-maker monitor heartbeat lost / LWT | warning | Ice availability becomes `UNKNOWN` |
| `COM-103` | MQTT broker unreachable | warning | Dashboard stays up, alert when reconnected |
| `SVC-101` | Service door open / service mode | info | Inhibit both products |

Severity meanings: *info* logs only; *warning* alerts; *product unavailable*
disables one or both products until the condition clears on its own;
*vend failed* ends the current sale and enters the refund path; *lockout*
disables a product until an operator resets it; *critical* also fires an
immediate alert and never auto-clears.

## 6. Remote commands

Three levels, each with stricter preconditions.

| Level | Examples | Preconditions |
|---|---|---|
| Soft | Restart VMC service, reconnect MQTT, force a report, change publish interval, edit catalog/prices | Authenticated admin |
| Controlled subsystem cycle | Power-cycle payment system; power-cycle ice maker (via monitor contract) | Machine marked unavailable first; **no transaction in progress**; no vend active; per-target lockout (ice maker: 300 s, already enforced device-side); reason recorded |
| Full machine recovery | Cycle control power | All outputs de-energized, water valve confirmed closed, no vend active, explicit confirmation; last resort only |

Every command carries a unique `request_id` (nonce) so a replayed message
cannot repeat an action, is answered by exactly one ack, and is written to the
event history with user, timestamp, reason, prior state, result. After any
power-cycle the target must pass a post-restart health check before payment is
re-enabled. Not on the list, and staying off it: any remote command that
directly runs a motor, opens a valve, or moves the trap door.

Power domains to wire separately so one subsystem can be cycled alone: control
supply + ESP32s, RPi + network, payment system, ice maker, cabinet heater,
lighting. The payment cycle sequence: inhibit → let the active transaction
finish or cancel through its own interface → quiet period → power off branch →
dwell → power on → confirm heartbeat → permissives → re-enable.

## 7. Payment and refund policy

- Money is accepted only while the corresponding `*_Sale_Available` flag is
  true. The VMC withdraws `payment/enable` the moment a flag drops.
- The price is moved from escrow at the start of `dispensing`. A terminal
  failure report (`bin_empty`, `timeout`, `jam`, `error`) or the dispense
  timeout returns the price to escrow, locks out the product per its fault
  severity, and keeps the customer in the session to choose again. If nothing
  sellable remains the VMC pays out immediately. Late or duplicate reports
  after a completed sale, and reports for another slot, are ignored.
- A refund is a `cmd/payment/refund` command acked by the gateway within
  10 s; one retry with the same `request_id`, then `PAY-103` for
  reconciliation. Escrow bookkeeping alone is never called a refund.
- A catalog edit that removes the product a customer has selected cancels the
  sale back to `idle` with escrow intact; it is not a machine error.
- After a VMC restart mid-sale, the transaction is **uncertain**: payment stays
  inhibited until the payment gateway's state and the ESP32's state are
  reconciled, and the event is logged for manual review (implemented as
  `PAY-104`; see `services/session_store.py`).
- The payment system is never power-cycled with a transaction open.

## 8. Failure modes

| Failure | Response |
|---|---|
| Internet lost | Vending continues; telemetry and alerts queue locally |
| MQTT broker down | Dashboard stays up (`COM-103`); ESP32s go to safe idle, no sales |
| RPi down | ESP32s go to safe idle; no sales; nothing moves |
| Vending ESP32 down (LWT or stale) | `COM-101`; both products inhibited |
| Ice-maker monitor down | `COM-102`; ice availability `UNKNOWN`; ice inhibited |
| Power loss and return | All outputs de-energized by hardware; `PWR-101`; self-test; payment inhibited until permissives pass |
| Bag missing | `ICE-201`; ice inhibited, water still sells |
| Full-bag sensor stuck on | Caught at precheck / self-test; ice inhibited |
| Full-bag sensor never trips | Max fill time stops motor; `ICE-301` |
| Trap door won't close | `ICE-402` critical; ice locked out |
| Flow meter stops pulsing | No-flow timeout closes valve; `WTR-101` |
| Valve won't close | `WTR-103` critical; upstream shutoff if fitted |
| Heater contactor welds | Hardware high-limit protects; `ENV-103` |
| Cabinet freezing | `ENV-101` then `ENV-102`; water inhibited |
| Ice maker lockup | Ice unavailable; controlled restart via monitor contract only |
| Operator deletes product mid-sale | Sale cancelled to idle, escrow kept |

## 9. Phases

Phases A and B unblock everything else. C, D and E can proceed in parallel once
B has a draft contract.

### Phase A — Document the present machine (field work, outside this repo)

Deliverable: a survey workbook (spreadsheet) with an as-found I/O list and a
timed recording of one normal ice sale and one normal water sale.

- Photograph every compartment, panel, relay, terminal and nameplate before
  touching anything.
- Confirm the 24 VDC control architecture: does the factory controller drive
  coils, or switch loads directly?
- One row per load: tag, supply, current, control path, feedback, max runtime.
- One row per sensor: tag, electrical type, normal state, broken-wire state.
- Payment system make/model/protocol and its inhibit/finish/cash-out commands.
- Flow meter nominal pulses per gallon.
- Mark every point `existing / replace / add / unknown`. Write `UNKNOWN`, do
  not guess.

The workbook, not this repo, holds wiring, terminal numbers and the electrical
drawings. This roadmap links to it once it exists.

### Phase B — Vending-machine contract

Do for the vending ESP32 what `docs/contracts/ice-maker-monitor/` does for the
ice maker. Deliverable: `docs/contracts/vending-machine/CONTRACT.md` plus
generated schemas.

- Capabilities document: which sensors and outputs exist, firmware version,
  calibration values (pulses per gallon, max fill time, dwell times).
- Heartbeat with Last-Will, same cadence and staleness rules.
- `hardware/dispenser` step vocabulary from §4 as an enum.
- `hardware/io/<device>` binary state for every sensor and output.
- Fault codes from §5 as an enum, carried on `alert/vending`.
- Commands: `dispense` (with `request_id`, product slot), `payment/enable`,
  `cancel`, `self_test`, `set_calibration` — each acked, idempotent by
  `request_id`, with the firmware-side interlocks stated as MUSTs.
- Product **slot** becomes a stable field on `Product` rather than list
  position, so catalog edits cannot shift products onto the wrong motor.
- The vending simulator becomes the reference implementation, as the
  ice-maker simulator is for its contract.

### Phase C — VMC changes (this repo)

- ~~Fault-code registry, honest vend outcomes, per-product lockouts, acked
  refunds~~ — done (spec `docs/superpowers/specs/2026-09-17-fault-registry-vend-outcomes-design.md`).
- ~~Availability permissives (§3) drive `payment/enable` instead of FSM
  state.~~ — done (spec `docs/superpowers/specs/2026-09-21-unattended-operation-design.md`).
- Fault-code registry (§5) as a shared enum; `VMCAlert` carries a code;
  dashboard groups by code and severity.
- ~~Refund policy (§7) as explicit code paths with tests, including the
  restart-mid-sale reconciliation.~~ — done (spec
  `docs/superpowers/specs/2026-09-21-unattended-operation-design.md`).
- Remote-command audit (§6): user, reason, `request_id`, prior state, ack,
  stored in the event history and shown on the dashboard.
- Payment power-cycle sequence as an admin action with the preconditions in §6.
- ~~Per-product availability on the dashboard (ice vs water) with the failing
  permissive named.~~ — done (spec
  `docs/superpowers/specs/2026-09-21-unattended-operation-design.md`).
- Startup self-test state: after boot or `PWR-101`, hold payment off until the
  vending ESP32 reports permissives.

### Phase D — Vending ESP32 firmware

- Implements the Phase B contract with every §4 interlock and every bounded
  runtime in firmware.
- Safe idle on loss of VMC heartbeat.
- MDB/payment protocol lives here (the repo's `hardware/mdb_interface.py` is a
  reference only).
- Keyed service mode input: customer sales inhibited, bounded local tests
  allowed.
- Persist calibration and fault lockouts across reboot.

### Phase E — Bench prototype

- Tabletop panel: ESP32, 24 V supply, interposing relays, switches simulating
  every sensor, lamps for every output, a pulse generator for the flow meter.
- Run the real VMC against the bench ESP32 and against the simulators with
  fault injection; every fault in §5 must be reproducible on demand.
- Verify for each: outputs go safe immediately, correct code recorded, payment
  inhibited, alert delivered, recovery requires the right condition, nothing
  restarts on its own after a reboot.

### Phase F — Staged retrofit

In this order, one subsystem at a time, keeping the factory controller in
place until step 4:

1. Monitoring only: temperature, doors, power, online state.
2. Read existing sensors without commanding loads.
3. Shadow mode: firmware logs what it would have done during factory-controlled
   sales.
4. Take over buttons and selection.
5. Bag fan and bag-present logic.
6. Agitator and dispense sequence.
7. Trap door with feedback.
8. Water dispense with flow calibration against measured containers.
9. Payment integration, only after physical vending is repeatable.
10. Remote reset and controlled power domains last.

### Phase G — Commissioning

No customer access. Dry runs with loads disabled, then live loads. Test with an
empty hopper, no bag, blocked chute, obscured sensor, power failure in every
state, broker and RPi failure mid-vend, payment interruption before credit,
after credit, during vend and after release. Enough repeated vends to surface
intermittent bag and ice-flow problems.

### Phase H — Operating discipline

Service, sanitizing and sensor-verification checklists; water-meter calibration
procedure; seasonal freeze-protection check; monthly interlock test; payment
reconciliation report; spare parts; backup of config, event database and
firmware; release process for VMC and firmware updates.

## 10. Open questions

Answers change the design; collect them during Phase A.

- Payment equipment make, model and protocol. MDB is assumed; if it is a
  proprietary pulse/dry-contact interface the payment layer in Phase D changes
  shape.
- Does the ice maker expose enable, bin-full and fault contacts, or is the
  monitor project the only view into it?
- Is there a bag-inflation confirmation sensor, or only bag-present and
  bag-full?
- Trap door: one actuator with open and closed limit switches, or a single
  position signal?
- Does the flow meter give a fixed pulse count per gallon, and has it been
  measured on this plumbing?
- Which inputs exist for leak, water pressure, treatment health, service door?
- Site connectivity: Ethernet, Wi-Fi or cellular, and is it behind carrier NAT
  (which decides whether an outbound overlay such as Tailscale is required)?
- Who besides the owner needs remote access, and at what permission level?

## 11. Out of scope for this repo

Electrical panel construction, motor protection sizing, code compliance, and
food/water sanitation and permitting are real requirements of the fielded
machine, but they are not software. They belong in the survey workbook and the
operating checklists, and the local health department should be consulted
before the machine sells to the public. The software's contribution is to
inhibit sales when sanitation-relevant inputs (leak, treatment health, service
door) say it should, and to keep the service records the checklists need.
