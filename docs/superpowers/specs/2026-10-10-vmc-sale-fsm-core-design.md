# VMC as the sale FSM: design

Date: 2026-10-10. Status: approved, not yet implemented.

## Problem

After eight extractions and the public-surface work (2026-10-09),
`controller/vmc.py` is still 2774 lines. The sale lifecycle, the one thing
the class exists for, is about 700 of them. The rest is wiring, side-effect
fan-out, a per-sale board conversation, a test-sale harness, and one-line
delegates:

| Responsibility | Lines (approx.) |
|---|---|
| Sale lifecycle: FSM callbacks, `select_product`, `process_payment`, `deposit_funds`, `_expire_session`, `_fail_vend`, `_finish_dispensing` | 700 |
| Dispense dispatch and outcome: `on_dispense_product`, `_persist_then_dispense`, `_fail_dispense_async`, `on_dispenser_event`, `_record_sale`, `_dispense_timed_out` | 450 |
| `run_test_sale`, its waiter/path/in-progress flags, and `is_test` branches in six other methods | 330 |
| `__init__` wiring, twelve `set_*`, twelve read-only service properties | 400 |
| Fault side-effect orchestration: `raise_fault`/`clear_fault` fan-out, liveness, MQTT connection | 200 |
| Status, persistence, display, customer-message fan-out | 150 |
| One-line delegates onto lease, recovery, telemetry, tasks | 120 |
| Virtual-payment QR cycling | 40 |

The VMC holds a handle to every transport and service (`_mqtt_client`,
`_health_monitor`, `_session_store`, `_event_recorder`,
`_command_dispatcher`, `_display_controller`, `_inventory`,
`_availability`). That is what makes it a god object: any change anywhere
in the system has a reason to touch it.

## Goal

`VMC` becomes the sale FSM and nothing else. It holds no transport or
service handle. Everything that publishes, persists, records, or alerts
goes through two injected collaborators. The wiring moves to a
composition root. The public surface from the 2026-10-09 design is
**negotiable**: callers and tests are repointed where the new shape reads
better, but event injection through `vmc.on_*` and reads through
`vmc.faults` / `vmc.escrow` / `vmc.sale` / `vmc.refunds` keep working.

## 1. Ownership and dependency direction

```
Machine (controller/machine.py) -- composition root, built once by main.py
 |- vmc: VMC                 the sale FSM (transitions model)
 |- dispense factory         builds one DispenseCycle per entry into dispensing
 |- test_sales: TestSaleRunner
 |- faults: FaultService     wraps FaultRegistry plus every fan-out and guard
 |- outputs: StatusOutputs   status publish, session persist, display, messages
 |- lease: MaintenanceLease  gains begin/standby/end/take_over
 |- escrow, refunds, recovery, gate, telemetry, tasks   (unchanged classes)
 `- wired services: mqtt, health, availability, inventory, recorder,
                    session_store, dispatcher, display
```

**Rule.** The VMC holds no transport or service handle. It depends on
exactly six injected collaborators -- `EscrowLedger`, `RefundProtocol`,
`FaultService`, `StatusOutputs`, a `DispenseCycle` factory, `TaskRunner`
-- plus three read-only callables: `availability()` (may return `None`),
`inventory()` (may return `None`), and `in_maintenance() -> bool`. Arrows
point from `Machine` down. Nothing imports `Machine`. `VMC` and
`MaintenanceLease` do not import each other.

**What stays in `VMC`.** `states`, `TRANSITIONS`, the seven FSM callbacks
(`on_start_interaction`, `on_dispense_product`, `on_complete_transaction`,
`on_reset`, `on_cancel_sale`, `on_vend_failed`, `on_error`),
`_after_state_change`, `deposit_funds`, `select_product`,
`process_payment`, `request_refund`, `_expire_session` and the session
timer, `_fail_vend`, `_finish_dispensing`, `snapshot()`, `sale` /
`selected_product` / `pending_sale_shares`, `has_credit`, `get_status()`,
`find_product(sku)`, the four sale-driving event-port methods
(`on_payment_credit`, `on_button_press`, `on_dispenser_event`,
`on_refund_ack`), the new `begin_test_sale` / `end_test_sale` /
`make_idle_for_service`, and the observer registration methods. Target
size: about 700 lines.

**Public read surface kept on the VMC**, because the FSM genuinely owns
these relationships: `faults` (now the `FaultService`), `escrow`,
`refunds`, `sale`, `selected_product`, `pending_sale_shares`,
`credit_escrow`, `escrow_credits`, `test_sale_in_progress`, `state`,
`products`, `session_timeout_seconds`, `dispense_timeout_seconds`.
`raise_fault` and `clear_fault` stay as one-line forwards onto
`FaultService` since the FSM is a legitimate raiser.

**Moved off the VMC.** The twelve `set_*` methods, `attach_to_loop`,
`cancel_pending_tasks`, `drain_persistence`, the service properties
(`session_store`, `mqtt_client`, `command_dispatcher`, `health_monitor`,
`availability`, `event_recorder`, `display_controller`), `recovery`,
`lease`, `gate`, `tasks`, `maintenance_hold`, the four PAY-104 recovery
delegates, the nine telemetry delegates, the maintenance delegates,
`run_test_sale`, `TestSaleResult`, `initiate_virtual_payment`,
`publish_payment_enable`, `on_mqtt_connection`.

## 2. The sale path and `DispenseCycle`

`DispenseCycle` (`controller/dispense_cycle.py`) is one object per entry
into `dispensing`. The VMC builds it through the injected factory in
`on_dispense_product` and holds it as `self._cycle` until the sale leaves
`dispensing`. It owns everything between "the FSM committed to
dispensing" and "a terminal outcome is classified":

- `start(snapshot: SessionSnapshot | None)`: profile lookup via the gate
  (the CFG-101 defensive fallback), mint `request_id` synchronously and
  publish it back to the sale before any task is created, save the
  dispensing snapshot through `outputs.save_snapshot_async` (guarded by
  the PAY-104 evidence rule), dispatch `dispense` through the command
  dispatcher with that `request_id`, check the ack status, and arm the
  `dispense_timeout` timer via `tasks.schedule(..., label=
  "dispense_timeout")`. Every failure (snapshot save, no dispatcher,
  `CommandTimeout`, any other exception, non-`ok` ack) calls
  `on_failed(cycle, FaultCode.PAY_102, outcome)` with the existing
  outcome strings (`snapshot_failed`, `no_ack`); the CFG-101 fallback
  calls it with `(cycle, FaultCode.CFG_101, "no_profile")`. The timer
  calls `on_failed(cycle, FaultCode.PAY_102, "no_report")`.
- `classify(data: dict) -> DispenseReport | None`: the slot-mismatch
  check, the `request_id` mismatch check (a report with no id is still
  accepted), `door_open`-is-success only when `(mechanism, door_open)`
  has an `OUTCOME_FAULTS` entry, and `fault_for_outcome` with the
  unmapped-outcome fallback to the mechanism's `error` mapping.
  `DispenseReport` is a frozen dataclass: `outcome: DispenserOutcome`,
  `success: bool`, `fault: FaultCode | None` (set for failures and for
  `door_open`, where it is `ICE_402`). `None` means "not for this cycle,
  ignore", and the cycle logs why.
- `record(sale: SaleContext) -> None` (async): the durable sale write off
  the loop via `asyncio.to_thread`, with the current escalation: generic
  failure raises `DATA-101` and finishes the vend; `SaleRecordingFailed`
  raises `PAY-104`, calls `set_transaction_certain(False)`, and leaves
  `sale.shares` set so the snapshot stays the sale's only record. Price is
  the sum of consumed shares, never `product.price`.
- `cancel()`: drops the timer. The VMC calls it on every exit from
  `dispensing` (`_finish_dispensing`, `on_vend_failed`, `on_reset`,
  `on_cancel_sale`, `on_error`).

Constructor dependencies, all injected by the factory `Machine` builds:
`dispatcher()` (callable, may return `None`), `gate`, `outputs`, `faults`,
`recorder()` (callable, may return `None`), `set_transaction_certain`,
`tasks`, `timeout_seconds()`, and the two VMC callbacks `on_failed` and
`on_request_id(request_id, mechanism)`.

**`_sale_seq` is deleted.** The guard against a late failure from an
earlier sale becomes object identity: `on_failed` is ignored unless
`cycle is self._cycle` and `self.state == "dispensing"`. `SaleContext.seq`
is removed in the same PR (the only reader was this guard).

**The VMC keeps every decision.** `on_dispenser_event` becomes: parse the
outcome (non-terminal states are logged and dropped), refuse unless
`state == "dispensing"` and a cycle exists, `report =
self._cycle.classify(data)`, then act. Success: `await
self._cycle.record(sale)` unless `sale.is_test` (a test sale instead has
its shares cleared and the `dispense` event skipped), raise `ICE-402` for
`door_open`, `_finish_dispensing()`, notify settled `"dispensed"`.
Failure: `faults.raise_fault(report.fault, sku=...)`, `_fail_vend(...)`,
notify settled `"vend_failed"` with the code. Timeout: raise `PAY-102`,
`_fail_vend`, notify settled `"timeout"`.

**Observers.** The VMC exposes `subscribe_state_change(cb: Callable[[str],
None]) -> Callable[[], None]` and `subscribe_sale_settled(cb:
Callable[[SaleContext, str, str | None], None]) -> Callable[[], None]`;
each returns its own unsubscribe function. `_after_state_change` calls
`outputs.state_changed(self.state)` then every state-change observer.
Settled fires exactly once per sale, with the `SaleContext` captured
before `on_vend_failed` clears it, and with outcome one of `"dispensed"`,
`"vend_failed"`, `"timeout"`. `fault_code` is a `FaultCode.value` string
for `"vend_failed"` and `None` otherwise, matching `TestSaleResult` today.

`sale.is_test` stays on `SaleContext` and keeps gating three money-safety
rules inside the FSM regardless of observers: no `record`, no
`vend_failed` KPI row, and no real refund of test credit.

## 3. `TestSaleRunner` and the maintenance lease

`TestSaleRunner` (`controller/test_sale.py`) owns `run_test_sale(sku, *,
user_id=None, user_name=None) -> TestSaleResult`, the `TestSaleResult`
dataclass (unchanged fields), the settlement future, the state path, the
`run_id`, and the `test_run` event row with its current metadata shape.
It is constructed with `vmc`, `lease`, `gate`, `recorder()`, and `tasks`.
It talks to the VMC only through public calls:

- `vmc.begin_test_sale(product) -> bool`: raises `RuntimeError` if
  `test_sale_in_progress` is already set; otherwise sets it, seeds
  `SaleContext(product, is_test=True)`, deposits `round(price, 2)` with
  method `"test"`, calls `select_product(index)`, and returns whether
  `selected_product is product and state == "interacting_with_user"`.
  Kept on the VMC because it touches escrow and the sale.
- `vmc.end_test_sale()`: clears the seeded context unless the state is
  still `dispensing` (the cancelled-mid-vend case, where the eventual
  hardware report must still settle a test sale), takes all remaining
  credit off escrow directly (never a refund command), and clears
  `test_sale_in_progress`.
- `subscribe_state_change` (appends to the path) and
  `subscribe_sale_settled` (resolves the future), attached before
  `begin_test_sale` and detached in a `finally`.

The runner's sequence: unknown sku raises `ValueError`; a product with no
valid profile (only when `gate.profiles` is wired) raises `RuntimeError`;
mint `run_id`; `with lease.test_run():` subscribe, `begin_test_sale` (a
`False` return raises the current "could not select" `RuntimeError`),
`await` the future, append the settled state if the path's last entry
differs (the `set_state("idle")` bypass), record the `test_run` row, and
in `finally` unsubscribe and `end_test_sale()`.

Two special cases go away. `_fail_vend` no longer checks for a waiter: a
failed **test** vend always takes the credit `on_vend_failed` just
restored straight off escrow (`escrow.take_all()`), so test money never
sits on the machine whether or not a runner is awaiting. The runner's
`take_all` in `end_test_sale` becomes a backstop. And `snapshot()`'s
`is_test` fallback reads `test_sale_in_progress`, which is now the VMC's
own flag rather than the runner's.

`_find_product_by_sku` becomes the public `vmc.find_product(sku) ->
tuple[int | None, Product | None]`.

**`MaintenanceLease`** gains `begin_maintenance(user_id, session_id)`,
`begin_standby(user_id, session_id)`, `end_maintenance(session_id)`, and
`take_over_maintenance(user_id, session_id)` with the bodies the VMC has
today. It takes three callables at construction instead of a VMC
reference: `fsm_state() -> str`, `escrow_is_empty() -> bool`, and
`make_idle_for_service() -> bool`. `vmc.make_idle_for_service()` holds
today's `begin_standby` state switch: returns `False` while `dispensing`;
in `interacting_with_user` refunds (`reason="maintenance"`) and
`cancel_sale()`; in `idle` refunds if escrow is non-empty and cancels the
session timer; in `error` refunds if escrow is non-empty; returns `True`.
`begin_standby` calls it only when no lease is held (the upgrade-in-place
branch runs first, as today). The VMC's `deposit_funds` reads the
injected `in_maintenance()` for the refund-during-lease rule.
`maintenance_test_run()` on the VMC is deleted; callers use
`lease.test_run()`.

## 4. `FaultService`, `StatusOutputs`, `Machine`

**`FaultService`** (`controller/fault_service.py`) wraps the existing
`FaultRegistry` and owns every side effect of `raise_fault(code, *, sku,
outcome)` and `clear_fault(key, by)`: the `lockout_set` /
`lockout_cleared` recorder rows, the `logger.error` / `logger.info`
lines, the health-monitor alert raise/clear, the MQTT `alerts` publish,
the push of `active_faults()` to health and availability, the
`outputs.state_changed`-equivalent status publish after a clear, and the
three guards -- SVC-102 refused while a lease is held, PAY-104 cleared
only once the session evidence file is removed (then
`set_transaction_certain(True)`), CFG-101 re-raised after any lockout
clear on a profile-less product. It also takes over
`on_subsystem_liveness(subsystem, alive)` (COM-101/COM-102/PAY-101 plus
the availability liveness push and the MDB republish),
`on_mqtt_connection(connected)` (COM-103 plus republish and status), and
`clear_ice101_lockouts()`. Reads are re-exposed: `is_locked(sku)`,
`has(code)`, `lockouts`, `machine_faults`, `snapshot()` /
`active_faults()`, so `vmc.faults.is_locked(...)` and route code keep
working. Dependencies are callables resolved at call time: `recorder()`,
`health()`, `mqtt()`, `availability()`, `lease_holder() -> str | None`,
`clear_session_evidence() -> bool`, `lacks_valid_profile(sku) -> bool`,
`publish_status()`, and `fire_and_forget`.

**`StatusOutputs`** (`controller/outputs.py`) is the FSM's only outbound
channel. Methods: `state_changed(state)` (push to health and
availability, persist or clear the session, publish retained `status`
when an MQTT client and loop exist); `persist(state=None)` (today's
`_persist_session`, including the PAY-104 evidence guard);
`save_snapshot_async(snapshot)` (the awaited save `DispenseCycle.start`
uses, same guard); `display(state)`; `message(text)` (customer message
callback plus log); `refresh()` (update callback); `show_qr(image)`;
`publish_payment_enable(accept)`; `publish_refund(cmd)`. It owns the
`update` / `message` / `qrcode` callbacks (`set_update_callback`,
`set_message_callback`, `set_qrcode_callback` move here) and the uptime
clock. It is constructed with `snapshot: Callable[[str | None],
SessionSnapshot]`, `credit_escrow()`, `selected_product_name()`,
`pay104_active()`, `fire_and_forget`, `loop()`, and the service callables
`mqtt()`, `health()`, `availability()`, `session_store()`, `display()`.

The QR-cycling body of `initiate_virtual_payment` moves to
`PaymentGatewayManager.next_payment_prompt(amount) -> tuple[str, object] |
None` (gateway name and QR image, cycling the index internally; `None`
when no gateways are configured). The VMC's `initiate_virtual_payment`
becomes: call it, message "unavailable" on `None`, else `outputs.show_qr`
and the "scan the QR code" message.

**`Machine`** (`controller/machine.py`) is the composition root.
`Machine(config, *, tasks: TaskRunner | None = None)` constructs, in
dependency order: `TaskRunner`, `EscrowLedger`, `FaultRegistry` then
`FaultService`, `StatusOutputs`, `RefundProtocol`, `DispenserProfileGate`,
`MaintenanceLease`, `SessionRecovery`, `TelemetryRouter`, `VMC`,
`TestSaleRunner`, and the `DispenseCycle` factory. Where two collaborators
reference each other (the lease needs `vmc.make_idle_for_service`, the
VMC needs `lease.hold`), `Machine` passes a closure over its own
attribute, so construction order is not circular. The twelve `set_*`
methods, `attach_to_loop`, `cancel_pending_tasks`, `drain_persistence`,
and the read-only properties (`vmc`, `faults`, `outputs`, `lease`,
`test_sales`, `recovery`, `gate`, `tasks`, `escrow`, `refunds`,
`session_store`, `mqtt_client`, `command_dispatcher`, `health_monitor`,
`availability`, `event_recorder`, `display_controller`,
`maintenance_hold`) live here. `set_session_store` keeps today's boot
evaluation and the `discard_test` warning / `uncertain` flagging, with
`_flag_uncertain_session` moving to `Machine` since it fans out to
recorder, availability, and faults.

`SUBSCRIPTIONS` in `controller/mqtt_inbound.py` becomes `(topic, owner,
method)` triples where owner is `"vmc"` or `"telemetry"`;
`Machine.set_mqtt_client` resolves each against `self.vmc` or
`self.telemetry` in table order. The nine telemetry one-liners on the VMC
are deleted. `on_hardware_io`'s ICE-101 callback and `on_capabilities`'s
gate reconcile are wired by `Machine` to `faults.clear_ice101_lockouts`
and `gate.on_vending_capabilities` directly.

**Caller migration.** `main.py` builds `Machine` and wires services on
it. `web_interface/context.py` stores `machine` and exposes `vmc` as
`machine.vmc`; `health_snapshot()` reads `machine.maintenance_hold` and
`machine.faults`. `routes/tests_level.py` uses `machine.lease` for
`begin_*` / `end` / `take_over` / `test_run()` and
`machine.test_sales.run_test_sale`. `routes/health.py` and `routes/home.py`
use `machine.faults` and `machine.recovery`. `services/startup_recovery.py`
and `services/startup_dispensers.py` take `machine`. Test fixtures
construct `Machine(config, tasks=FakeTaskRunner())` and read
`machine.vmc`; the `vmc_fake_time` fixture yields the VMC as before and a
sibling `machine_fake_time` yields the root. Event-injection tests keep
calling `vmc.on_*` unchanged. `tests/test_no_private_access.py` is
extended to match `machine\._` as well as `vmc\._`.

## 5. Sequencing

Six PRs, each green on its own, each shrinking `vmc.py`, none adding a
private alias:

1. **`StatusOutputs`.** Move publish / persist / display / message /
   callbacks and the uptime clock; VMC loses its `_mqtt_client`,
   `_display_controller`, `_session_store` reads. QR cycling moves to
   `PaymentGatewayManager`. Pure move, no behaviour change.
2. **`FaultService`.** `raise_fault` / `clear_fault` fan-out, liveness,
   MQTT connection, ICE-101 clear. VMC loses `_health_monitor` and
   `_event_recorder` for faults. `vmc.faults` returns the service.
3. **`Machine`.** Composition root, `set_*`, `SUBSCRIPTIONS` triples,
   telemetry and recovery delegates deleted, callers and fixtures
   repointed. Largest diff, zero logic change.
4. **`DispenseCycle`.** Dispatch, classify, record, timeout; `_sale_seq`
   and `SaleContext.seq` replaced by cycle identity. The money-path PR:
   full suite plus a focused review of `on_dispenser_event`, `_fail_vend`,
   and `record`.
5. **Observers and `TestSaleRunner`.** `subscribe_*`, `begin_test_sale` /
   `end_test_sale`, the runner extracted, the waiter branches in
   `on_dispenser_event`, `_dispense_timed_out`, `_fail_vend`,
   `_after_state_change`, and `snapshot()` collapse, and the
   always-clear-test-credit rule lands in `_fail_vend`.
6. **Lease preconditions and cleanup.** `begin_*` / `end` / `take_over`
   onto `MaintenanceLease`, `make_idle_for_service`, the remaining
   one-line delegates deleted, CLAUDE.md rewritten for the new layout,
   `test_no_private_access.py` extended to `machine._`.

## 6. Testing

Every PR runs the full suite (2415 tests at the time of writing, 15
opt-in browser skips). New tests per PR:

- PR 1: `StatusOutputs` with fake sinks -- each method hits exactly the
  sinks it should, and `persist` honours the PAY-104 guard.
- PR 2: `FaultService` -- one test per guard (SVC-102 held, PAY-104
  evidence not removable, CFG-101 re-raise), liveness raise/clear per
  subsystem, COM-103 on connect/disconnect.
- PR 3: `Machine` wiring -- every collaborator attached, every
  `SUBSCRIPTIONS` triple resolves to a bound method, construction order
  holds with no loop attached.
- PR 4: `DispenseCycle` -- each `start` failure path yields the right
  `(code, outcome)`, `classify` for slot mismatch / id mismatch / no id /
  `door_open` per mechanism / unmapped outcome, `record` escalation to
  DATA-101 and PAY-104, and the stale-cycle guard (a failure from a
  superseded cycle is ignored).
- PR 5: `TestSaleRunner` -- settle on dispensed / vend_failed / timeout,
  cancel mid-vend leaves the context and still classifies as test,
  double-submit refused, test credit never reaches a refund command.
- PR 6: lease preconditions via `MaintenanceLease` directly, and
  `make_idle_for_service` per FSM state.

Each PR goes through Copilot review before merge, as before.

## Out of scope

The `transitions` library and the FSM's states and triggers; the MQTT
contracts and `CONTRACT_VERSION`; `SaleContext`'s fields other than
removing `seq`; any route URL or template.
