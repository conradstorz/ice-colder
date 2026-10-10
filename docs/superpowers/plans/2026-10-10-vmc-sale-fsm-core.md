# VMC as the Sale FSM Core Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reduce `controller/vmc.py` to the sale FSM (about 700 lines) by moving outbound fan-out, fault side effects, wiring, the per-sale dispense conversation, the test-sale harness, and the lease preconditions into six focused units.

**Architecture:** Six PRs in the order the spec fixes (outputs, faults, composition root, dispense cycle, observers plus test-sale runner, lease plus cleanup), each merged before the next starts. PRs 1 to 3 are pure moves; PR 4 is the one money-path change (`_sale_seq` becomes cycle identity); PR 5 replaces the `is_test` waiter branches with observers; PR 6 finishes the lease and the docs. Spec: `docs/superpowers/specs/2026-10-10-vmc-sale-fsm-core-design.md`.

**Tech Stack:** Python 3.12, `transitions`, pytest (async tests via the existing `vmc` / `vmc_fake_time` fixtures and `tests/fakes.py`'s `FakeTaskRunner`), ruff via `uv run`.

## Global Constraints

- Every task: `uv run ruff check --fix .`, `uv run ruff format .`, `uv run pytest -q` must pass with zero failures (15 opt-in browser skips are expected). Record the passing count at the start of Task 1 and never let it drop except where a test is deliberately deleted because its subject moved (say which).
- Behaviour never changes except where this plan says so (Task 8's identity guard, Task 9's always-clear-test-credit rule). A task that finds it must change behaviour elsewhere to satisfy a test stops and reports instead.
- Dependency rule from the spec: `VMC` holds no transport or service handle. After Task 6 a `grep -n "_mqtt_client\|_health_monitor\|_session_store\|_event_recorder\|_command_dispatcher\|_display_controller\|_inventory\b" controller/vmc.py` must return nothing except the two read-only callables `_availability` and `_inventory` (which become `self._availability()` / `self._inventory()` calls).
- Nothing imports `controller.machine` except `main.py`, `web_interface/`, `services/startup_*.py`, and tests. `controller/vmc.py` and `controller/maintenance_lease.py` never import each other.
- Tests read collaborators (`vmc.faults`, `machine.lease`, …) and mutate only through a method on the VMC, the `Machine`, or the collaborator itself when the test is exercising that collaborator. `tests/test_no_private_access.py` stays green throughout.
- Never chain shell commands with `&&`. Do not commit from a subagent; the orchestrator commits. Each PR goes through Copilot review before merge.
- Each task ends with `wc -l controller/vmc.py` and the number must be lower than the task started with (Tasks 1, 3, 5, 7, 9 may be flat; every even task must shrink it).
- Docstrings and comments that move with code are rewritten for their new home; a comment that names its old location ("see VMC.__init__") is updated or removed, never left stale.

---

## PR 1: StatusOutputs

### Task 1: `StatusOutputs` class

**Files:**
- Create: `controller/outputs.py`
- Create: `tests/test_outputs.py`
- Modify: `services/payment_gateway_manager.py` (`next_payment_prompt`)

**Interfaces:**
- `class StatusOutputs` constructed with keyword-only callables: `snapshot: Callable[[str | None], SessionSnapshot]`, `credit_escrow: Callable[[], float]`, `selected_product_name: Callable[[], str | None]`, `pay104_active: Callable[[], bool]`, `tasks: TaskRunner`. Sinks are attached later and default to `None`: `attach_mqtt(client)`, `attach_health(monitor)`, `attach_availability(availability)`, `attach_session_store(store)`, `attach_display(controller)`. Public read-only properties `mqtt`, `health`, `availability`, `session_store`, `display` return what was attached.
- Callbacks: `set_update_callback(cb)`, `set_message_callback(cb)`, `set_qrcode_callback(cb)`.
- Methods, all sync unless noted: `state_changed(state: str) -> None` (health `update_vmc_state`, availability `set_fsm_state`, `persist()`, then a retained `status` publish of `VMCStatus(state, credit_escrow, selected_product, uptime_seconds)` only when both an MQTT client and `tasks.loop` exist); `persist(state: str | None = None) -> None` (no store → return; `pay104_active()` → return; `snapshot(state).is_open()` → fire-and-forget `save_async`, else `clear_async`, both `persistent=True`); `async save_snapshot_async(snap: SessionSnapshot) -> None` (same two guards, then `await store.save_async(snap)`); `clear_session_evidence() -> bool` (`store.clear()`; `True` when no store); `display(state: str) -> None`; `message(text: str) -> None` (logs at info and calls the message callback); `refresh() -> None` (update callback); `show_qr(image) -> None`; `publish_payment_enable(accept: bool) -> None` (warns and returns without a client); `publish_refund(cmd: PaymentRefundCommand) -> None` (same); `publish_alert(alert: VMCAlert) -> None` (no-op without a client); `uptime_seconds -> int` property from a `time.monotonic()` taken at construction.
- `PaymentGatewayManager.next_payment_prompt(self, amount: float) -> tuple[str, object] | None`: `None` when no gateways; otherwise `(gateway_name, qr_image)` for the current index, then advances the index modulo the gateway count. `generate_qr_code` is unchanged.

- [ ] Write `tests/test_outputs.py` with a `FakeMqtt` (records `(topic, payload, retain)`), `FakeStore` (records `save_async`/`clear_async`/`clear` calls, `clear` returns a settable bool), `FakeHealth`, `FakeAvailability`, `FakeDisplay`, and a `FakeTaskRunner` from `tests/fakes.py`. Cover: `state_changed` with no sinks is a no-op; with every sink attached it calls health, availability, persists, and publishes retained `status` with the right fields; `persist` skips when `pay104_active()` is true and clears when the snapshot is not open; `save_snapshot_async` honours the same guard; `clear_session_evidence` returns `True` without a store and the store's answer with one; `publish_payment_enable` / `publish_refund` / `publish_alert` publish on the exact topics (`cmd/payment/enable`, `cmd/payment/refund`, `alerts`) and are silent without a client; `message` and `refresh` call their callbacks only when set; `next_payment_prompt` cycles and returns `None` with no gateways.
- [ ] Run the new file; expect every test to fail on import.
- [ ] Implement `controller/outputs.py` and `next_payment_prompt`. Module docstring states the rule: this is the FSM's only outbound channel.
- [ ] Run the new file, then lint and the full suite.

### Task 2: VMC speaks only through `StatusOutputs`

**Files:**
- Modify: `controller/vmc.py` (`__init__`, `set_mqtt_client`, `set_session_store`, `set_display_controller`, `set_health_monitor`, `set_availability`, `publish_payment_enable`, `_publish_refund_command`, `_persist_session`, `_publish_status`, `_update_display`, `_refresh_ui`, `_display_message`, `send_customer_message`, `set_update_callback`, `set_message_callback`, `set_qrcode_callback`, `initiate_virtual_payment`, `_persist_then_dispense`, `clear_fault`'s PAY-104 branch, `raise_fault`'s MQTT alert publish, `_start_time`)
- Modify: tests that set `vmc.update_callback` / `vmc.message_callback` / `vmc.qrcode_callback` as attributes, or read `vmc.mqtt_client` / `vmc.session_store` / `vmc.display_controller` (keep those three properties on the VMC for now, returning `self._outputs.<sink>`)

**Interfaces:**
- `VMC.__init__` builds `self._outputs = StatusOutputs(snapshot=self._snapshot, credit_escrow=lambda: self.credit_escrow, selected_product_name=..., pay104_active=lambda: self._faults.has(FaultCode.PAY_104), tasks=self._tasks)` before the lease and refund protocol (both capture closures that end up calling it).
- Public read-only `vmc.outputs -> StatusOutputs`.
- `set_mqtt_client` calls `self._outputs.attach_mqtt(client)` then registers `SUBSCRIPTIONS`; `set_session_store` calls `attach_session_store` then the existing boot evaluation; `set_display_controller`, `set_health_monitor`, `set_availability` attach to outputs **and** keep their existing VMC-side wiring (liveness callback, availability publisher now `self._outputs.publish_payment_enable`).
- `SessionRecovery`'s `store=` closure becomes `lambda: self._outputs.session_store`.
- `_publish_status()` becomes `self._outputs.state_changed(self.state)`; `_persist_session(state)` becomes `self._outputs.persist(state)`; `_update_display(target)` becomes `self._outputs.display(target or self.state)`; `_refresh_ui` → `refresh()`; `send_customer_message` → `message()`; `_persist_then_dispense`'s save → `await self._outputs.save_snapshot_async(snap)` (drop the local PAY-104 check, the method has it); `clear_fault`'s PAY-104 branch → `if not self._outputs.clear_session_evidence(): ... return False`; `raise_fault`'s MQTT branch → `self._outputs.publish_alert(VMCAlert(...))`; `_publish_refund_command` → `publish_refund`; `initiate_virtual_payment` → `next_payment_prompt` plus `show_qr` and the two messages.
- Deleted from the VMC: `_mqtt_client`, `_display_controller`, `_session_store`, `_start_time`, `update_callback`, `message_callback`, `qrcode_callback`, `virtual_payment_index`, `_display_message`.

- [ ] Grep `controller/vmc.py` for every name in the deleted list and every `self._mqtt_client` / `self._session_store` / `self._display_controller` read; rewrite each through `self._outputs`.
- [ ] Grep `tests/` for `update_callback` / `message_callback` / `qrcode_callback` attribute writes and switch them to the `set_*_callback` methods (which now forward to outputs); report any test that read `vmc._start_time` or `virtual_payment_index`.
- [ ] Run lint and the full suite; confirm the dependency grep from Global Constraints no longer lists `_mqtt_client`, `_session_store`, `_display_controller`.

## PR 2: FaultService

### Task 3: `FaultService` class

**Files:**
- Create: `controller/fault_service.py`
- Create: `tests/test_fault_service.py`

**Interfaces:**
- `class FaultService` constructed with keyword-only: `registry: FaultRegistry`, `outputs: StatusOutputs`, `tasks: TaskRunner`, and callables `recorder: Callable[[], object | None]`, `lease_holder: Callable[[], str | None]`, `lacks_valid_profile: Callable[[str], bool]`, `set_transaction_certain: Callable[[bool], None]`, `fsm_state: Callable[[], str]`. Health, MQTT, and availability are read from `outputs.health` / `outputs.mqtt` / `outputs.availability` at call time.
- Mutators: `raise_fault(code: FaultCode, *, sku: str | None = None, outcome: str | None = None) -> None` and `clear_fault(key: str, by: str = "admin") -> bool`, with today's bodies from `VMC.raise_fault` / `VMC.clear_fault` (`controller/vmc.py:849-960`) including the three guards and the trailing `_push_active_faults()` plus `outputs.state_changed(fsm_state())` after a clear.
- Event handlers moved from the VMC: `on_subsystem_liveness(subsystem: str, alive: bool) -> None` (uses `_LIVENESS_FAULTS`, which moves here), `on_mqtt_connection(connected: bool) -> None`, `clear_ice101_lockouts() -> None`.
- Reads re-exposed from the registry: `is_locked(sku) -> FaultCode | None`, `has(code) -> bool`, `lockouts` (property), `machine_faults` (property), `active_faults() -> list[dict]` (`registry.snapshot()`), `parse_key`, `push_active_faults() -> None`.

- [ ] Write `tests/test_fault_service.py` using a real `FaultRegistry`, a real `StatusOutputs` with the fakes from `tests/test_outputs.py` (move those fakes into `tests/fakes.py` if a second file needs them), and a `FakeRecorder` recording `record(kind, **kw)`. Cover: `raise_fault` on a product code writes `lockout_set`, publishes `alerts`, raises the health alert, pushes active faults to health and availability; raising twice writes one `lockout_set`; `clear_fault` of a lockout writes `lockout_cleared` and clears the health alert; SVC-102 refused while `lease_holder()` is not `None`; PAY-104 refused when `clear_session_evidence()` is false and `set_transaction_certain(True)` only when it succeeds; CFG-101 re-raised after clearing another lockout when `lacks_valid_profile(sku)` is true; `on_subsystem_liveness("vending", False)` raises COM-101 and `True` clears it and republishes for `mdb`; `on_mqtt_connection(False)` raises COM-103 and `True` clears it; `clear_ice101_lockouts` clears only ICE-101 lockouts.
- [ ] Run the new file; expect import failure.
- [ ] Implement `controller/fault_service.py`; copy the bodies, do not rewrite the logic.
- [ ] Run the new file, then lint and the full suite.

### Task 4: VMC delegates faults

**Files:**
- Modify: `controller/vmc.py` (`__init__`, `raise_fault`, `clear_fault`, `active_faults`, `_push_active_faults`, `_on_subsystem_liveness`, `on_mqtt_connection`, `_clear_ice101_lockouts`, `faults` property, `set_health_monitor`, `set_availability`, `_sellable_products`, `select_product`'s lockout read, every `self._faults.<x>` and `self._health_monitor` / `self._event_recorder` use that belongs to faults)
- Modify: `controller/dispenser_gate.py` / `controller/session_recovery.py` construction in `__init__` only if a callable they receive changes shape (it should not: `is_locked`, `has`, `raise_fault`, `clear_fault` keep their signatures on the service)
- Modify: `tests/test_vmc_*.py` that assert on `vmc.faults` being a `FaultRegistry` (type assertions only; `is_locked` / `has` / `lockouts` keep working)

**Interfaces:**
- `VMC.__init__` builds `self._registry = FaultRegistry(self._product_name)` and `self._faults = FaultService(registry=self._registry, outputs=self._outputs, tasks=self._tasks, recorder=lambda: self._event_recorder, lease_holder=lambda: self._lease.hold.holder_user_id if self._lease.hold else None, lacks_valid_profile=lambda sku: self._gate.lacks_valid_profile(sku), set_transaction_certain=..., fsm_state=lambda: self.state)`. Because the gate and lease are built after the service, those closures read `self._gate` / `self._lease` at call time, never at construction.
- `vmc.faults -> FaultService`. `vmc.raise_fault` / `vmc.clear_fault` / `vmc.active_faults` become one-line forwards. `vmc.on_mqtt_connection` forwards to `self._faults.on_mqtt_connection` for now (moves to `Machine` in Task 6). `set_health_monitor` registers `self._faults.on_subsystem_liveness` as the liveness callback.
- Deleted from the VMC: `_LIVENESS_FAULTS`, `_push_active_faults`, `_on_subsystem_liveness`, `_clear_ice101_lockouts` (the `TelemetryRouter` `on_bin_half_full=` wiring points at `self._faults.clear_ice101_lockouts`).

- [ ] Rewire `__init__`, replace the bodies with forwards, delete the moved members, and repoint the internal reads (`self._faults.is_locked`, `.has`, `.lockouts`, `.machine_faults` all still resolve on the service).
- [ ] Grep tests for `isinstance(vmc.faults, FaultRegistry)` or `FaultRegistry` imports asserting the type and update them to `FaultService`.
- [ ] Run lint and the full suite; confirm `_health_monitor` is now read in `vmc.py` only by `set_health_monitor` and the telemetry router closure (both go in Task 6).

## PR 3: Machine

### Task 5: `Machine` composition root

**Files:**
- Create: `controller/machine.py`
- Create: `tests/test_machine.py`
- Modify: `controller/mqtt_inbound.py` (`SUBSCRIPTIONS` triples)
- Modify: `tests/test_mqtt_inbound.py` (`TestSubscriptionsTable`)

**Interfaces:**
- `class Machine` with `__init__(self, config: ConfigModel, *, tasks: TaskRunner | None = None)`. Constructs in order: `tasks`, `escrow = EscrowLedger()`, `registry = FaultRegistry(...)`, `outputs = StatusOutputs(...)`, `faults = FaultService(...)`, `refunds = RefundProtocol(...)`, `gate = DispenserProfileGate(...)`, `lease = MaintenanceLease(...)`, `recovery = SessionRecovery(...)`, `telemetry = TelemetryRouter(...)`, then `vmc = VMC(config, tasks=..., escrow=..., refunds=..., faults=..., outputs=..., gate=..., lease=..., availability=lambda: self.availability, inventory=lambda: self.inventory)`. The `gate` and `lease` references are temporary: `gate` leaves the VMC in Task 8 (the cycle owns the lookup) and `lease` becomes the `in_maintenance` callable in Task 11. Closures that need the VMC (`outputs.snapshot`, `faults.fsm_state`, `refunds.on_confirmed` / `on_failed`, `lease.on_granted` / `on_released`) are `lambda *a: self.vmc.<method>(*a)` so construction order is not circular; the module docstring explains this once.
- Read-only properties: `vmc`, `tasks`, `escrow`, `faults`, `outputs`, `refunds`, `gate`, `lease`, `recovery`, `telemetry`, `session_store`, `mqtt_client`, `command_dispatcher`, `health_monitor`, `availability`, `event_recorder`, `display_controller`, `inventory`, `maintenance_hold`, `subsystem_capabilities`.
- Wiring methods moved verbatim from the VMC: `attach_to_loop(loop)`, `cancel_pending_tasks()`, `async drain_persistence(timeout=3.0)`, `set_mqtt_client(client)` (attaches to outputs, registers `SUBSCRIPTIONS`), `set_health_monitor(monitor)` (attaches to outputs, liveness callback → `faults.on_subsystem_liveness`), `set_availability(av)` (attaches to outputs, `set_fsm_state(vmc.state)`, `set_active_faults(faults.active_faults())`, `set_publisher(outputs.publish_payment_enable)`), `set_display_controller`, `set_inventory_manager`, `set_event_recorder`, `set_session_store(store)` (attach, `recovery.evaluate_at_boot()`, the `discard_test` warning, `_flag_uncertain_session` on `uncertain`), `set_command_dispatcher`, `set_dispenser_profiles(profiles)` (→ `gate.attach`), `set_session_liveness(predicate)` (→ `lease.set_session_liveness`), `on_mqtt_connection(connected)` (→ `faults.on_mqtt_connection`).
- Recovery conveniences on `Machine`: `pending_sale_for_recovery()`, `pending_sale_already_recorded(pending)`, `reserve_pending_sale(pending)`, `mark_pending_sale_recorded()` as one-line forwards to `recovery` (routes call them in Task 6).
- `SUBSCRIPTIONS: tuple[tuple[str, str, str], ...]` of `(topic, owner, method)` with owner `"vmc"` for `on_payment_credit`, `on_button_press`, `on_dispenser_event`, `on_refund_ack` and `"telemetry"` for the other nine, whose method names become the router's own `handle_*` names. Order unchanged. `Machine.set_mqtt_client` resolves `getattr(self.vmc | self.telemetry, method)`.
- `VMC.__init__` gains keyword-only `escrow`, `refunds`, `faults`, `outputs`, `gate`, `lease`, `availability`, `inventory` (all required) and stops constructing those itself. `self._availability` / `self._inventory` become callables and every read becomes `self._availability()` / `self._inventory()`. Its `products`, `owner_contact`, `payment_gateway_manager`, `session_timeout_seconds`, `dispense_timeout_seconds` stay.

- [ ] Update `SUBSCRIPTIONS` and its pin test to the triples; add a test that each `"telemetry"` method exists on `TelemetryRouter` and each `"vmc"` method on `VMC`.
- [ ] Write `tests/test_machine.py`: construction with no loop attached succeeds; every property returns a non-`None` collaborator (services `None` until attached); `set_mqtt_client` on a `FakeMqtt` with a recording `register` registers exactly the thirteen topics in table order to bound methods; `set_availability` publishes through `outputs`; `set_session_store` with a `FakeStore` holding an open production snapshot raises PAY-104 and with a test snapshot logs and clears; `cancel_pending_tasks` cancels `dispense_timeout` / `session_timeout` / refund deadlines (use `FakeTaskRunner` and arm each through a VMC call).
- [ ] Run the new tests; expect import failure.
- [ ] Implement `controller/machine.py` and the VMC constructor change. The VMC's own `set_*` methods, `attach_to_loop`, `cancel_pending_tasks`, `drain_persistence`, service properties, `recovery` / `lease` / `gate` / `tasks` properties, the four recovery forwards, the nine telemetry forwards, `_on_vending_capabilities_validated`, `_flag_uncertain_session`, `set_session_liveness`, `on_mqtt_connection`, and `subsystem_capabilities` are deleted in Task 6, not here; in this task they may temporarily forward to the `Machine`-built collaborators only if needed to keep the suite green mid-PR (prefer doing Task 5 and Task 6 as one branch, two commits).
- [ ] Run lint and the full suite.

### Task 6: Callers and tests build a `Machine`

**Files:**
- Modify: `main.py:53-177`, `web_interface/context.py` (`machine_instance` + `set_machine_instance`; `vmc_instance` derived), `web_interface/routes/__init__.py` (export), `web_interface/routes/health.py:438-722`, `web_interface/routes/home.py`, `web_interface/routes/tests_level.py:294` (`dispenser_profile_for` → `machine.gate.profile_for`), `services/startup_recovery.py` (`reconcile_sales_journal_faults(machine, recorder)` using `machine.faults`), `services/startup_dispensers.py` (`wire_dispenser_profiles(machine, profiles)`), `tests/conftest.py` (`vmc` and `vmc_fake_time` build a `Machine`; new `machine` and `machine_fake_time` fixtures yielding the root), every test file listed by `grep -rlE "VMC\(" tests/` and every test calling a moved `vmc.set_*` / `attach_to_loop` / `cancel_pending_tasks` / `drain_persistence` / `pending_sale_*` / `on_mqtt_connection` / `session_store` / `mqtt_client` / `command_dispatcher` / `health_monitor` / `event_recorder` / `display_controller` / `recovery` / `gate` / `tasks` / `subsystem_capabilities` / `dispenser_profile_for` / `reconcile_dispenser_profiles` / `catalog_changed`
- Modify: `controller/vmc.py` (delete everything listed under Task 5's last bullet; `dispenser_profile_for` / `reconcile_dispenser_profiles` / `catalog_changed` / `set_dispenser_profiles` go too, routes use `machine.gate`)
- Modify: `tests/test_no_private_access.py` (regex also matches `machine\._`)

**Interfaces:**
- `context.machine_instance` and `set_machine_instance(machine)`; `context.vmc_instance` stays as a module attribute kept in sync by `set_machine_instance` so the many `context.vmc_instance` reads in routes keep working; `health_snapshot()` reads `machine_instance.maintenance_hold` and `machine_instance.faults.active_faults()`.
- Test fixtures: `vmc` yields `machine.vmc`; `machine` yields the root; `vmc_fake_time` yields `(vmc, runner)` as today; `machine_fake_time` yields `(machine, runner)`. A test needing both takes both fixtures (they share one `Machine` only if the plan's implementer makes `vmc` depend on `machine`; do that).
- Mechanical repoints in tests, name for name: `vmc.set_X(` → `machine.set_X(`, `vmc.attach_to_loop` → `machine.attach_to_loop`, `vmc.cancel_pending_tasks` → `machine.cancel_pending_tasks`, `vmc.drain_persistence` → `machine.drain_persistence`, `vmc.on_mqtt_connection` → `machine.on_mqtt_connection`, `vmc.pending_sale_*` / `reserve_pending_sale` / `mark_pending_sale_recorded` → `machine.`, `vmc.session_store` / `mqtt_client` / `command_dispatcher` / `health_monitor` / `event_recorder` / `display_controller` / `recovery` / `gate` / `tasks` / `subsystem_capabilities` → `machine.`, `vmc.dispenser_profile_for(p)` → `machine.gate.profile_for(p)`, `vmc.reconcile_dispenser_profiles()` → `machine.gate.reconcile()`, `vmc.catalog_changed()` → `machine.gate.catalog_changed()`. A test that constructs `VMC(config)` directly constructs `Machine(config)` and takes `.vmc`.

- [ ] Repoint `main.py`, `context.py`, routes, and the two startup services; run the route and startup test files alone first.
- [ ] Repoint fixtures, then the test files, one file per edit pass; run each file after editing it.
- [ ] Delete the moved members from `controller/vmc.py`; run the dependency grep from Global Constraints and paste its output in the report.
- [ ] Extend the guard regex; run lint and the full suite; report `wc -l controller/vmc.py`.

## PR 4: DispenseCycle

### Task 7: `DispenseCycle` class

**Files:**
- Create: `controller/dispense_cycle.py`
- Create: `tests/test_dispense_cycle.py`

**Interfaces:**
- `@dataclass(frozen=True) class DispenseReport: outcome: DispenserOutcome; success: bool; fault: FaultCode | None`.
- `class DispenseCycle` constructed with keyword-only: `sale: SaleContext`, `dispatcher: Callable[[], object | None]`, `gate: DispenserProfileGate`, `outputs: StatusOutputs`, `faults: FaultService`, `recorder: Callable[[], object | None]`, `set_transaction_certain: Callable[[bool], None]`, `tasks: TaskRunner`, `timeout_seconds: Callable[[], float]`, `on_failed: Callable[[DispenseCycle, FaultCode, str], Awaitable[None]]`, `on_request_id: Callable[[str, str], None]` (request_id, mechanism).
- `start(self, snapshot_for: Callable[[str], SessionSnapshot | None]) -> None`: sync. Looks up `gate.profile_for(sale.product)`; on `None` logs and fire-and-forgets `on_failed(self, FaultCode.CFG_101, "no_profile")` (persistent) and returns. Otherwise mints `request_id = uuid4().hex`, calls `on_request_id(request_id, profile.mechanism)` synchronously, builds `DispenseCommand`, takes `snap = snapshot_for("dispensing")`, arms the timer `tasks.schedule(timeout_seconds(), self._timed_out, label="dispense_timeout")`, and fire-and-forgets `self._run(snap, cmd, request_id)` (persistent).
- `async _run(snap, cmd, request_id)`: today's `_persist_then_dispense` body with `await on_failed(self, PAY_102, "snapshot_failed" | "no_ack")` in place of `_fail_dispense_async` and `outputs.save_snapshot_async(snap)` for the save; the ack `request_id` mismatch warning stays.
- `_timed_out(self)`: clears its own task handle and fire-and-forgets `on_failed(self, FaultCode.PAY_102, "no_report")`.
- `classify(self, data: dict) -> DispenseReport | None`: today's slot-mismatch, `request_id`-mismatch, `door_open` and `fault_for_outcome` logic from `on_dispenser_event` (`controller/vmc.py:1021-1035`, `1133-1232`) and `_has_outcome_mapping`. Logs the reason for every `None`.
- `async record(self) -> None`: first the `dispense` KPI event (`recorder().record("dispense", value=float(sale.product.slot))`, skipped without a recorder), then today's `_record_sale` body against `self.sale` (price from `sale.shares`, `{"unknown": price}` when `None`), raising `DATA-101` / `PAY-104` through `faults.raise_fault` and calling `set_transaction_certain(False)` on the PAY-104 path. Returns normally in every case; the caller decides about `shares`.
- `cancel(self) -> None`: cancels the timer if live.
- `request_id` and `mechanism` properties (set in `start`).

- [ ] Write `tests/test_dispense_cycle.py` with `FakeTaskRunner`, a `FakeDispatcher` whose `send` can return an ack, raise `CommandTimeout`, or raise `RuntimeError`, a `FakeGate` returning a fixed `SlotProfile` or `None`, a `FakeRecorder` whose `record_sale` can succeed, raise `SaleRecordingFailed`, or raise `RuntimeError`, and a recording `on_failed`. Cover: no profile → `(CFG_101, "no_profile")`; snapshot save raising → `(PAY_102, "snapshot_failed")`; no dispatcher, timeout, other exception, non-`ok` ack → `(PAY_102, "no_ack")`; a good ack calls `on_failed` never and `on_request_id` once with the mechanism; `runner.fire("dispense_timeout")` → `(PAY_102, "no_report")`; `cancel()` retires the timer; `classify` returns `None` for a slot mismatch, an id mismatch, and a non-terminal state, accepts a report with no id, marks `complete` success, marks `door_open` success with `ICE_402` for `bagged_ice` and failure for `water_fill`, maps `jam` on `water_fill` to that mechanism's `error` code; `record` writes the sale with the share-sum price, raises `DATA-101` on a generic failure and `PAY-104` plus `set_transaction_certain(False)` on `SaleRecordingFailed`.
- [ ] Run the new file; expect import failure.
- [ ] Implement `controller/dispense_cycle.py`; copy the bodies and keep every log line.
- [ ] Run the new file, then lint and the full suite.

### Task 8: VMC drives a `DispenseCycle`

**Files:**
- Modify: `controller/vmc.py` (`on_dispense_product`, `on_dispenser_event`, `_dispense_timed_out`, `process_payment`'s timer arm, `_finish_dispensing`, `on_vend_failed`, `on_reset`, `on_cancel_sale`, `on_error`, `_fail_vend`, `cancel_pending_tasks` remnants, `__init__`)
- Modify: `controller/machine.py` (`dispense_factory`)
- Modify: `controller/sale_context.py` (remove `seq`)
- Modify: tests asserting on `sale.seq` or `_sale_seq`, and tests that fired `dispense_timeout` (label unchanged, still works)

**Interfaces:**
- `VMC.__init__` takes `dispense_factory: Callable[[SaleContext], DispenseCycle]`; `Machine` builds it as a closure over `self` supplying `on_failed=lambda c, code, outcome: self.vmc.on_dispense_failed(c, code, outcome)` and `on_request_id=self.vmc.note_dispense_request`.
- New VMC methods: `note_dispense_request(request_id: str, mechanism: str) -> None` (replaces `self._sale` with `with_(mechanism=..., request_id=...)`); `async on_dispense_failed(cycle: DispenseCycle, code: FaultCode, outcome: str) -> None` with the identity guard: return unless `cycle is self._cycle and self.state == "dispensing"`; then `cycle.cancel()`, `raise_fault(code, sku=...)`, `_fail_vend(code, outcome)`.
- `on_dispense_product`: `self._cycle = self._dispense_factory(self._sale)`; `self._cycle.start(lambda s: self._snapshot(s) if self._outputs.session_store else None)`. The timer arm leaves `process_payment`.
- `on_dispenser_event`: parse outcome; return unless `state == "dispensing"` and `self._cycle`; `report = self._cycle.classify(data)`; `None` → return. Success: test sale → `shares=None`; production sale → `await self._cycle.record()` (which writes the `dispense` KPI event first, see Task 7) then `shares=None`; `door_open` → `raise_fault(ICE_402, sku=...)`; `_finish_dispensing()`. Failure: `self._cycle.cancel()`, `raise_fault(report.fault, sku=...)`, capture `is_test`, `_fail_vend`, resolve the test waiter as today.
- `_finish_dispensing`, `on_vend_failed`, `on_reset`, `on_cancel_sale`, `on_error` each call `self._cycle.cancel()` if a cycle exists and set `self._cycle = None` where the sale ends.
- Deleted from the VMC: `_sale_seq`, `_dispense_timeout_task`, `_cancel_dispense_timeout`, `_dispense_timed_out`, `_fail_dispense_async`, `_persist_then_dispense`, `_record_sale`, `_dispenser_event_slot_mismatch`, `_has_outcome_mapping`, the `DispenseCommand` / `CommandTimeout` / `SaleRecordingFailed` / `fault_for_outcome` imports. `cancel_pending_tasks` on `Machine` cancels the live cycle via `vmc.cancel_dispense()` (new one-liner: `if self._cycle: self._cycle.cancel()`).

- [ ] Add a test in `tests/test_vmc_flows.py` (or the file that holds the existing `_sale_seq` late-failure test; find it with `grep -rn "seq" tests/`) proving that `on_dispense_failed` with a cycle object that is not the current one is ignored, and that a failure arriving after the sale settled is ignored.
- [ ] Rewrite the VMC methods and the `Machine` factory; remove `seq` from `SaleContext` and its docstring; delete the moved members.
- [ ] Run lint and the full suite; report the line count and any test whose assertion changed and why.

## PR 5: Observers and TestSaleRunner

### Task 9: Observers and the test-sale seams on the VMC

**Files:**
- Modify: `controller/vmc.py` (`_after_state_change`, `on_dispenser_event`, `on_dispense_failed`, `_fail_vend`, `snapshot`, `__init__`)
- Create: `tests/test_vmc_observers.py`

**Interfaces:**
- `subscribe_state_change(cb: Callable[[str], None]) -> Callable[[], None]` and `subscribe_sale_settled(cb: Callable[[SaleContext, str, str | None], None]) -> Callable[[], None]`; each returns an unsubscribe closure; callbacks run synchronously in subscription order; an exception in one is logged and does not stop the others.
- `_after_state_change` → `outputs.state_changed(self.state)` then the state-change observers. The settled notification fires from exactly three places with the sale captured before `on_vend_failed` clears it: the success branch of `on_dispenser_event` (`"dispensed"`, `None`), its failure branch (`"vend_failed"`, `code.value`), and `on_dispense_failed` for outcome `"no_report"` (`"timeout"`, `None`); any other `on_dispense_failed` outcome is `"vend_failed"` with the code.
- `begin_test_sale(product: Product) -> bool`, `end_test_sale() -> None`, `find_product(sku: str) -> tuple[int | None, Product | None]`, `test_sale_in_progress` property, all per spec section 3.
- `_fail_vend`: the `is_test and self._test_sale_waiter is None` branch becomes unconditional for a test sale: `if is_test: cleared = self._escrow.take_all()` with a debug log; the refund call stays skipped for a test sale.
- Deleted: `_test_sale_waiter`, `_test_sale_path`, `_resolve_test_sale_waiter`, `_test_sale_in_progress` (replaced by the flag `begin_test_sale` sets).

- [ ] Write `tests/test_vmc_observers.py` with `vmc_fake_time`: state observer sees every transition in order and stops after unsubscribe; settled fires once with `"dispensed"` on a `complete` report, `"vend_failed"` with the code on a `jam`, `"timeout"` on `runner.fire("dispense_timeout")`; a raising observer does not block the next; `begin_test_sale` refuses a second call while active and returns `False` for a locked sku; `end_test_sale` leaves the context when still `dispensing` and clears it otherwise; a failed test vend leaves escrow at zero with no `cmd/payment/refund` published.
- [ ] Run the new file; expect attribute errors.
- [ ] Implement; keep `run_test_sale` working in this task by having it use the new seams (it is deleted in Task 10).
- [ ] Run lint and the full suite.

### Task 10: `TestSaleRunner`

**Files:**
- Create: `controller/test_sale.py` (`TestSaleResult` moves here; `controller/vmc.py` re-exports it for one PR with a deprecation comment, removed in Task 12)
- Create: `tests/test_test_sale_runner.py`
- Modify: `controller/machine.py` (`test_sales` property), `web_interface/routes/tests_level.py:714-780` (`machine.test_sales.run_test_sale`), `tests/test_vmc_flows.py` `TestRunTestSale` and the other 23 `run_test_sale` call sites (→ `machine.test_sales.run_test_sale`)
- Modify: `controller/vmc.py` (delete `run_test_sale`, `maintenance_test_run`, `_find_product_by_sku`, `TestSaleResult`)

**Interfaces:**
- `class TestSaleRunner` constructed with keyword-only `vmc: VMC`, `lease: MaintenanceLease`, `gate: DispenserProfileGate`, `recorder: Callable[[], object | None]`, `tasks: TaskRunner`. `async run_test_sale(sku: str, *, user_id: str | None = None, user_name: str | None = None) -> TestSaleResult` with the sequence in spec section 3; `ValueError` for an unknown sku, `RuntimeError` for no valid profile (only when `gate.profiles is not None`), for "already in progress" (propagated from `begin_test_sale`), and for "could not select".
- `Machine.test_sales -> TestSaleRunner`.

- [ ] Write `tests/test_test_sale_runner.py` with `machine_fake_time`: dispensed path returns `outcome="dispensed"`, a path ending in `idle`, a `run_id`, and writes one `test_run` row with `status="ok"`; `jam` returns `"vend_failed"` with the code and `status="failed"`; `runner.fire("dispense_timeout")` returns `"timeout"` with `fault_code=None`; cancelling the awaiting task mid-vend leaves `vmc.sale.is_test` true and a later `complete` report still skips `record_sale`; a second concurrent call raises; no `cmd/payment/refund` is ever published; the lease's `runs_in_flight` is zero afterwards on every path.
- [ ] Run the new file; expect import failure.
- [ ] Implement the runner, wire it on `Machine`, repoint the route and tests, delete the VMC members.
- [ ] Run lint and the full suite; report the line count.

## PR 6: Lease preconditions and cleanup

### Task 11: Lease owns its preconditions

**Files:**
- Modify: `controller/maintenance_lease.py` (`__init__` gains `fsm_state`, `escrow_is_empty`, `make_idle_for_service`; new `begin_maintenance`, `begin_standby`, `end_maintenance`, `take_over_maintenance`)
- Modify: `controller/vmc.py` (new `make_idle_for_service`; delete `begin_maintenance`, `begin_standby`, `end_maintenance`, `take_over_maintenance`, `maintenance_hold`, `lease`, and the seven `_release_maintenance_hold` / `_arm_*` / `_maintenance_*` one-liners)
- Modify: `controller/machine.py` (pass the three callables)
- Modify: `web_interface/routes/tests_level.py` (`machine.lease.begin_maintenance` etc., `machine.lease.test_run()`), `web_interface/context.py` (`machine.maintenance_hold` already)
- Modify: `tests/test_maintenance_lease.py` (direct tests of the four methods), and the ~220 test call sites of `vmc.maintenance_hold` / `begin_maintenance` / `begin_standby` / `end_maintenance` / `take_over_maintenance` / `maintenance_test_run` / `vmc.lease` (→ `machine.maintenance_hold`, `machine.lease.<method>`, `machine.lease.test_run()`)

**Interfaces:**
- `MaintenanceLease.begin_maintenance(user_id, session_id) -> tuple[bool, str | None]`, `begin_standby(user_id, session_id) -> tuple[bool, str | None]`, `end_maintenance(session_id) -> bool` (= `request_release`), `take_over_maintenance(user_id, session_id)` (= `take_over`), bodies from `controller/vmc.py:2082-2173` with `self.state` → `self._fsm_state()`, `self._escrow.is_empty_within_tolerance` → `self._escrow_is_empty()`, and the standby state switch → `if not self._make_idle_for_service(): return False, "vend finishing, tap again"`.
- `VMC.make_idle_for_service() -> bool`: `False` in `dispensing`; otherwise today's per-state refund / `cancel_sale` / timer cancel, then `True`.
- `VMC.__init__`'s `lease=` argument becomes `in_maintenance: Callable[[], bool]` (`Machine` passes `lambda: self.lease.hold is not None`); `deposit_funds` is its only reader, and nothing else lease-related remains on the VMC.

- [ ] Add tests to `tests/test_maintenance_lease.py` driving the four methods with recording callables: refusals for each precondition, upgrade-in-place for the holder's own session, `make_idle_for_service` called only on the fresh-grant path.
- [ ] Add a `make_idle_for_service` test per FSM state in `tests/test_vmc_flows.py`.
- [ ] Move the bodies, repoint routes and tests (mechanical: `vmc.maintenance_hold` → `machine.maintenance_hold`, `vmc.begin_maintenance(` → `machine.lease.begin_maintenance(`, likewise the other three, `vmc.maintenance_test_run()` → `machine.lease.test_run()`, `vmc.lease` → `machine.lease`).
- [ ] Run lint and the full suite; report the line count.

### Task 12: Cleanup, guard, docs

**Files:**
- Modify: `controller/vmc.py` (remove the `TestSaleResult` re-export, `_consume_credits_fifo` if only `process_payment` uses it, `_fire_and_forget` / `_schedule` if each has a single caller; verify no comment still references a moved member)
- Modify: `tests/test_no_private_access.py` (docstring and regex cover `machine._`; add a second test asserting `controller/vmc.py` never imports `controller.machine`, `controller.maintenance_lease`, `services.mqtt_client`, `services.health_monitor`, `services.session_store`, `services.event_recorder`, `services.command_dispatcher`, or `services.display_controller`)
- Modify: `CLAUDE.md` (the "FSM Core" section and the extraction paragraphs are rewritten to describe `Machine`, `StatusOutputs`, `FaultService`, `DispenseCycle`, `TestSaleRunner`, the observer hooks, the lease preconditions, and the dependency rule; the "Public surface" paragraph lists what lives on `VMC` versus `Machine`; the dispenser-profiles paragraphs that name `VMC.set_dispenser_profiles` / `dispenser_profile_for` / `reconcile_dispenser_profiles` are updated to `Machine.set_dispenser_profiles` / `machine.gate.profile_for` / `machine.gate.reconcile`)
- Modify: `docs/superpowers/specs/2026-10-10-vmc-sale-fsm-core-design.md` status line → implemented, with PR numbers

**Interfaces:** none new.

- [ ] Write the import-boundary test; run it; expect pass (if it fails, the earlier task that left the import is wrong, fix there).
- [ ] Delete the leftovers, rewrite CLAUDE.md, update the spec status.
- [ ] Run lint and the full suite; report the final `wc -l controller/vmc.py` and the method list via `grep -n "^    def \|^    async def " controller/vmc.py`. Target: about 700 lines, every method about a sale.
