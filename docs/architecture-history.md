# Architecture history and reference

Moved out of the root `CLAUDE.md`: background and history, not working rules.

### Mapping to the aspirational documents


| Aspirational section | Lives in |
|---|---|
| Field device layer (contracts spec §2) | `firmware/vending/`, simulated by `ice_sims` |
| Board ↔ edge contract (spec §3) | `ice_contracts.board` + `docs/contracts/{vending-machine,ice-maker-monitor}` |
| Edge §2.1 HAL | `ice_edge.hal` |
| Edge §2.2 sale FSM | `ice_edge.sale` |
| Edge §2.3 escrow ledger | `ice_edge.ledger` |
| Edge §2.4 vend verification | `ice_edge.vend` |
| Edge §2.5 faults/availability | `ice_edge.faults` |
| Edge §2.6 monitoring | `ice_edge.monitoring` |
| Edge §2.7 customer UI | `ice_edge.customer_ui` (+ `/screen` route in `ice_edge.web`) |
| Edge §2.8 local service UI | `ice_edge.web`, rendering `ice_ui` |
| Edge §2.9 store-and-forward uplink | `ice_edge.uplink` |
| Edge §2.10 supervisor | `ice_edge.supervisor` + `ice_common.task_supervisor` |
| §3 Payments | `ice_edge.payment` (MDB arrives through `ice_edge.hal`) |
| §4 CCTV | not built (the `ice_edge.cctv` stub was removed 2026-11-02; the spec keeps the intent) |
| §5 Roles/access | `ice_contracts.access` (model) + `ice_edge.access` (store) |
| §7 Reporting / alerting | `ice_edge.reporting`, `ice_edge.notify` |
| §7 "local and remote use the same UI" | `ice_ui` (both `ice_edge.web` and `ice_cloud.portal` render it) |
| Edge ↔ cloud contract (spec §4) | `ice_contracts.cloud` |
| §8 Cloud/fleet backend | `ice_cloud` |

Three deliberate deviations from the original design doc, recorded here rather than silently drifting: there is no top-level `DispenserFile` model, because each `[slot.N]` table is validated independently so one malformed slot only loses that slot, never the whole file; channel ids use the contract's own `CHANNEL_ID_PATTERN` (`packages/contracts/src/ice_contracts/board/common.py`) rather than a separate pattern; and the 0.7.0 → 0.8.0 contract bump that added `CFG-101`/`CFG-102` was originally this feature's only version bump — the VMC/routes runtime wiring (`DispenseCommand`, `DispenseStep`, the new `DispenserOutcome` members, `fault_for_outcome`) first reused that same 0.8.0 section on the theory that it was additive, not a schema break, but a Copilot review of PR #32 caught that `dispense`'s params actually changed shape and `cmd/dispense` was removed outright — wire-breaking, so `CONTRACT_VERSION` was bumped again, to **1.0.0**, per the module's own major-bump rule for breaking changes; see the Fault Codes section below for that bump's detail. Further deviations recorded for that runtime increment: `DispenserStatus.state` stays typed `str` rather than becoming an enum (both `DispenseStep` and `DispenserOutcome` strings flow through the same field over the sale's lifetime); the simulator's legacy `cmd/dispense` subscription was removed immediately rather than kept alongside the new path for a deprecation window, since no physical board exists yet to break; the command dispatcher reports a `door_open` completion as `status="failed"` (with a `detail` explaining the bag released but the door did not close) even though the VMC itself treats `door_open` as a customer success plus a fault, because the dispatcher's ack vocabulary has no third state; and `SessionSnapshot.dispense_mechanism` was added to the crash snapshot, additive beyond what the spec asked for.

`packages/contracts/src/ice_contracts/board/vending_machine.py` also bumped **2.3.0 → 2.4.0** (additive) for money in cents: `VMCStatus.credit_escrow_cents` added, `credit_escrow` (float dollars) deprecated until 3.0.0.

Both contracts bumped their `CONTRACT_VERSION` for this part: `packages/contracts/src/ice_contracts/board/vending_machine.py` **0.4.0 → 0.5.0** and `packages/contracts/src/ice_contracts/board/ice_maker_monitor.py` **1.1.0 → 1.2.0**. The vending-machine bump is a minor version absorbing two additive changes: adding `SVC-102` (new `FaultCode` + `FAULT_TABLE` entry + `PAYMENT_BLOCKING_FAULTS` member, six → seven), and the `DATA-101` description wording fix above ("sale journal in use; sales are being written to a fallback file" → "sale write failed; held in fallback file") — a part 3 change that shipped without its own version bump at the time; that deferred bump is absorbed here too.

Both contracts bumped again for the subsystem-windows feature: `packages/contracts/src/ice_contracts/board/vending_machine.py` **0.6.0 → 0.7.0** and `packages/contracts/src/ice_contracts/board/ice_maker_monitor.py` **1.3.0 → 1.4.0**, for the additive `ChannelDescriptor.direction`/`driven_by` fields (new optional fields distinguishing a board's sensed inputs from its driven outputs, and naming the command whose refusal inhibits an output).

`packages/contracts/src/ice_contracts/board/vending_machine.py` bumped once more, **0.7.0 → 0.8.0**, for the dispenser-profiles feature: `CFG-101` (no valid dispenser profile for a slot; `product_unavailable` severity, `Scope.product`) and `CFG-102` (`dispensers.toml` could not be read; `warning` severity, `Scope.machine`) — neither is a member of `PAYMENT_BLOCKING_FAULTS`, so a missing or invalid profile locks only the affected product's sale, never payment machine-wide. `CFG-101` is raised and cleared by `machine.gate.reconcile()` and re-asserted by `FaultService.clear_fault` if clearing some other lockout on a still-profile-less product would otherwise leave it unlocked; `CFG-102` tracks whether `dispensers.toml` itself loaded cleanly. Plan 2 (`DispenseCommand`/`DispenseStep`, the new `DispenserOutcome` members, `fault_for_outcome`) first shipped under this same 0.8.0 version on the theory that it was additive, but a Copilot review of PR #32 found it wire-breaking (`dispense`'s params changed shape; `cmd/dispense` was removed), so `packages/contracts/src/ice_contracts/board/vending_machine.py` bumped again, **0.8.0 → 1.0.0**, a major bump per the module's own breaking-change rule. `CFG-101`/`CFG-102` themselves stay documented at 0.8.0 — only the plan-2 runtime wiring moved to 1.0.0's "Semantics fixed in 1.0.0 (breaking)" section.

`packages/contracts/src/ice_contracts/board/vending_machine.py` bumped a final time, **1.0.0 → 2.0.0**, for the IM600XL simulator feature (spec `docs/superpowers/specs/2026-10-17-im600xl-simulator-design.md`), a major bump because `ButtonPress.button` changed type on the wire from a list-position `int` to a channel-id `str` (`CHANNEL_ID_PATTERN`, e.g. `button_ice`; the edge maps it to a product via `Product.button`). Also in 2.0.0: `ReleaseByCatch` (`proof="catch"`, `catch_channel`, `drop_seconds`) joins the `ReleaseStep` union; the `sensors/water_flow` topic is removed (volume is metered by the `fill_pulses` counter on `telemetry/vending/<id>`); `VendingChannel` and `INPUT_FAULTS` give the edge and the simulator one source for the meaningful channel ids and the state-to-fault table; and three fault changes: `WTR-105` is re-scoped to machine scope at `warning` severity (was product scope, `product_unavailable`), `WTR-106` (RO storage tank empty) and `WTR-107` (RO production overran its maximum run time) are new, both machine-scope `warning`, and `OUTCOME_FAULTS[("water_fill", bin_empty)]` now maps to `WTR-106` instead of `ICE-101`. `PAYMENT_BLOCKING_FAULTS` is unchanged at seven codes. **Input-driven faults never lock a product:** `WTR-104`..`WTR-107` raised from board inputs are machine-scope, so a low-pressure or empty-tank water fault refuses water through `Availability` rows but leaves `faults.lockouts` untouched and ice sellable. **Alert keys follow scope:** `FaultRegistry.raise_fault` builds `alert_key` as `"<code>:<sku>"` for a product-scope code and `"<code>:machine"` (with `RaisedFault.sku = None`) for a machine-scope one, whatever `sku` the caller passed, so the health alert, the MQTT alert and `FaultChange` raise and clear under one key. `WTR-107` is raised by the edge's `ro_overrun` timer (not by the board), survives pump-off, and is cleared only by `ro_tank_full` or an operator. **Upgrading an existing deployment:** its gitignored `data/config.json` has `button=None` on every product (all presses refused; only the `warn_if_no_buttons` log line says so) and its `data/dispensers.toml` names channels the IM600XL does not declare (every product `CFG-101` once capabilities arrive). Re-seed `dispensers.toml` from `dispensers.example.toml` and assign each product's button; the steps are in `docs/contracts/vending-machine/CONTRACT.md` under "Upgrading to vending contract 2.0.0".

`packages/contracts/src/ice_contracts/board/vending_machine.py` bumped **2.0.0 → 2.1.0** (additive) for the payment contract: `PAY-105` (payment event gap) and `PAY-106`/`PAY-107`/`PAY-108` (coin changer / bill validator / cashless reader fault) are all machine-scope `warning` and none is payment-blocking, so `PAYMENT_BLOCKING_FAULTS` is still seven codes. `ice_contracts.board.payment` 1.0.0 (`docs/contracts/payment/CONTRACT.md`) (now 1.1.0, which added the `cash_sale` command) is the new MDB ↔ edge contract; the legacy payment models and topics in the vending contract were deprecated then. The payment cutover (spec `docs/superpowers/specs/2026-10-30-payment-cutover-design.md`) bumped the vending contract **2.4.0 → 3.0.0** (breaking) and removed them: `PaymentEvent`, `PaymentStatus`, `PaymentEnableCommand`, `PaymentRefundCommand`, `PaymentRefundResult`, `RefundStatus`, `VMCStatus.credit_escrow` and the topics `payment/credit`, `payment/status`, `cmd/payment/enable`, `cmd/payment/refund` and `cmd/payment/refund/ack`; the vending board no longer advertises a `payment/enable` command. The edge now consumes `ice_contracts.board.payment` 1.1.0 only (`payment/event`, `payment/event/ack`, the retained `payment/state`, and `cmd/mdb` with `set_policy`, `payout`, `cash_sale`, `device_reset`, `vend_request`/`vend_result`/`end_session`/`return_escrow`).

## Removed Routes (Dashboard v2)

The v2 shell replaces all URLs from the old tabbed dashboard. Anyone holding a bookmark or an external script can find the replacement here:

| Removed | Replacement |
|---|---|
| `GET /` (old tabbed dashboard body) | `GET /` (the v2 Home: status strip + tile grid) |
| `GET /health` (fragment) | `GET /health` (a level) and its four sub-levels |
| `GET /logs` | `GET /health/logs` (50 lines, was 10) |
| `GET /activity` | `GET /reports?period=` |
| `POST /action/{command}` | `POST /controls/{command}` |
| `POST /faults/{key}/clear` | `POST /health/faults/{key}/clear` |
| `GET /inventory` (table fragment) | `GET /inventory` (the restock level) |
| `GET /inventory/new`, `POST /inventory/add` | `GET`/`POST /products/new` |
| `GET /inventory/copy/{sku}` | `GET /products/{sku}/copy` |
| `GET /inventory/edit/{sku}/catalog`, `POST /inventory/update/{sku}/catalog` | `GET`/`POST /products/{sku}/catalog` |
| `GET /inventory/edit/{sku}/placement`, `POST /inventory/update/{sku}/placement` | `GET`/`POST /products/{sku}/placement` |
| `POST /inventory/delete/{sku}` | `POST /products/{sku}/delete` |
| `GET /config/machine` | `GET`/`POST /settings/machine` |
| `GET /config/contacts` | `GET`/`POST /settings/contacts` |
| `GET /config/payments` | `GET`/`POST /settings/payments` |
| `GET /config/comms` | `GET`/`POST /settings/comms` |
| the old `/users/*` and `/devices/*` forms | the `/users` and `/devices` levels |

### Legacy money compatibility removed (2026-11-02)

The cloud stopped accepting 0.x reports (`MachineReport.contract` is `"1.0.0"` only; the schema is unchanged, so no version bump) and dropped the dollar template aliases for old uploaded UI bundles. The edge dropped its pre-cents readers: the dollar `session.json` and sales-journal conversion (the `"units": "cents"` marker is still written, no longer read), the dollar `price` upgrade of `config.json` products, and `SessionSnapshot.pending_refund_request_id` (never written since the MDB cutover). events.db migration 2 stays in `MIGRATIONS` because the list is append-only; a fresh database runs it as a no-op.

## Contract version history

### ice-maker-monitor (`ice_contracts.board`)

```text
1.1.0 -> 1.2.0: minor bump. The command/ack models (MonitorCommand,
CommandAck) move to ice_contracts/board/common.py as SubsystemCommand/CommandAck and
are re-exported here under their original names — same classes, wire
format unchanged except the ack's new optional `result` field. Additive,
so today's ice-maker firmware and the VMC's current ack handler still
validate.

1.2.0 -> 1.3.0 (2026-09-29): minor bump, additive. `CommandAck` gains
`phase` ("accepted" | "completed", default "completed" -- a present-day
ack with no `phase` key still validates unchanged). `power_cycle` now
acks "accepted" as soon as it starts (unchanged: still within the 10 s
ack deadline, still enforcing the 300 s lockout) and sends a SECOND,
`phase="completed"` ack, same topic and `request_id`, once the dwell
actually elapses -- see docs/contracts/ice-maker-monitor/CONTRACT.md's
completion table. `set_interval`, `ping`, `self_test` and `force_report`
are unaffected; their single ack is still both accept and completion.

1.3.0 -> 1.4.0 (2026-09-30): minor bump, additive. `ChannelDescriptor`
(ice_contracts/board/common.py) gains `direction` ("input" | "output", default
"input") and `driven_by` (str | None, default None) so a board's
capabilities document can say which channels it drives versus senses,
and which command's refusal inhibits an output. Both fields are
optional with defaults, so every present-day channel descriptor still
validates unchanged.
```

### vending-machine (`ice_contracts.board`)

```text
0.4.0 -> 0.5.0: minor bump, absorbing two additive changes. (1) SVC-102
(new FaultCode + FAULT_TABLE entry + PAYMENT_BLOCKING_FAULTS member,
six -> seven). (2) Part 3's DATA-101 description wording fix
("Sale journal in use; sales are being written to a fallback file" ->
"Sale write failed; held in fallback file") shipped without its own bump
at the time; that deferred bump is absorbed here too.

0.5.0 -> 0.6.0 (2026-09-29): minor bump, additive. `CommandAck` gains
`phase` ("accepted" | "completed", default "completed" -- a present-day
ack payload with no `phase` key still validates and means exactly what
it always meant). A long-running actuator command (`dispense`,
`water_valve`) now acks "accepted" as soon as it starts, and reports
completion separately -- see `ice_contracts/board/common.py`'s COMPLETION_TIMEOUTS
and docs/contracts/vending-machine/CONTRACT.md's completion table. No
existing field changed shape or meaning; every present-day ack and
command payload still validates unchanged.

0.6.0 -> 0.7.0 (2026-09-30): minor bump, additive. `ChannelDescriptor`
(ice_contracts/board/common.py) gains `direction` ("input" | "output", default
"input") and `driven_by` (str | None, default None) so a board's
capabilities document can say which channels it drives versus senses,
and which command's refusal inhibits an output. Both fields are
optional with defaults, so every present-day channel descriptor still
validates unchanged.

0.7.0 -> 0.8.0 (plan 1): minor bump, additive. FaultCode gains CFG-101
(no valid dispenser profile for a slot, product-scope) and CFG-102
(dispensers.toml could not be read, machine-scope). Neither blocks
payment.

0.8.0 -> 1.0.0 (plan 2, Copilot review on PR #32): MAJOR bump -- this is
wire-breaking, not additive, so the minor-bump rule plan 2 originally
shipped under does not apply. `dispense`'s params previously accepted
just `slot`; this version requires `mechanism` and the slot's whole
`profile` too, and the production dispense path moves from a bare
`cmd/dispense` publish (removed) onto the command channel
(`cmd/vending`, command `dispense`), same as a test run.
`DispenserStatus.request_id` is now always set for a production sale,
not test runs only. See docs/contracts/vending-machine/CONTRACT.md's
"Semantics fixed in 1.0.0 (breaking)" section for the full list.

2.0.0 -> 2.1.0 (2026-10-18): minor bump, additive. PAY-105..108 for the
payment contract (ice_contracts.board.payment, spec
2026-10-18-payment-contract-design.md). Not payment-blocking.

2.1.0 -> 2.2.0 (2026-10-22): minor bump, additive. DATA-103 (event
database could not be initialised; writes are still attempted and a failed
sale write is journalled to the fallback file). Alert-class
and not payment-blocking.
Also in 2.2.0: DATA-104 (session snapshot could not be saved; a crash
now would lose the live sale's evidence). Alert-class, not
payment-blocking.

2.2.0 -> 2.3.0 (2026-10-22): minor bump, additive. DATA-105 (event row
could not be written; KPIs and history may be incomplete) and DATA-106
(inventory counts could not be saved; stock levels revert on restart).
Both alert-class, not payment-blocking.

2.3.0 -> 2.4.0 (2026-10-30): minor bump, additive. VMCStatus gains
credit_escrow_cents (integer cents); the float credit_escrow stays,
deprecated, and is removed in 3.0.0.

2.4.0 -> 3.0.0 (2026-10-30): major bump, breaking. The legacy payment
models and topics are removed: PaymentEvent, PaymentStatus,
PaymentEnableCommand, PaymentRefundCommand, PaymentRefundResult,
RefundStatus and VMCStatus.credit_escrow (the deprecated dollar float);
the topics payment/credit, payment/status, cmd/payment/enable,
cmd/payment/refund and cmd/payment/refund/ack. Payments are
ice_contracts.board.payment. The vending board no longer advertises the
payment/enable command.
```
