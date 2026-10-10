# VMC Public Surface Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the VMC a public surface for events, time, and state so no test touches a `vmc._<name>`, then delete the alias layer left by the eight extractions.

**Architecture:** Five PRs, one per task, each merged before the next starts. Tasks 1 to 3 are mechanical renames and a fake scheduler; task 4 folds the scattered in-flight-sale attributes into a `SaleContext`; task 5 adds the guard test and removes the deprecated aliases. Spec: `docs/superpowers/specs/2026-10-09-vmc-public-surface-design.md`.

**Tech Stack:** Python 3.12, `transitions`, pytest (asyncio tests via `asyncio.run` / existing fixtures), ruff 0.11.2 via `uv run`.

## Global Constraints

- Every task: `uv run ruff check --fix .`, `uv run ruff format .`, `uv run pytest -q` must pass with zero failures (15 opt-in browser skips are expected). Starting count: 2415 passed.
- Behavior never changes. A task that finds it must change behavior to satisfy a test stops and reports instead.
- Rule from the spec: tests may read a collaborator (`vmc.faults`, `vmc.escrow`, …) but mutate only through a VMC method. A test that mutated a collaborator directly to bypass side effects is rewritten, not renamed.
- Never chain shell commands with `&&`. Do not commit from a subagent; the orchestrator commits.
- Each task ends with a count of remaining `vmc\._` matches in `tests/` (`grep -rhoE "vmc\._[a-z_0-9]+" tests/ | sort | uniq -c | sort -rn`) and that count must be lower than the task started with.

---

### Task 1: Public event port

**Files:**
- Modify: `controller/vmc.py` (every `_handle_mqtt_*`, `_process_payment`, `_raise_fault`, `raise_data_fault`)
- Modify: `controller/mqtt_inbound.py` (`SUBSCRIPTIONS` method names)
- Modify: `controller/dispenser_gate.py` wiring in `VMC.__init__` (`raise_fault=` callable), `services/startup_recovery.py` (`raise_data_fault` caller), any other caller found by grep
- Modify: tests under `tests/` that reference the old names (~125 sites)
- Test: `tests/test_mqtt_inbound.py` (`SUBSCRIPTIONS` pin)

**Interfaces:**
- Produces on `VMC`, all `async def name(self, topic: str, data: dict)` unless noted: `on_payment_credit`, `on_button_press`, `on_dispenser_event`, `on_refund_ack`, `on_hardware_io`, `on_payment_status`, `on_sensor_reading`, `on_water_flow`, `on_heartbeat`, `on_ice_maker_event`, `on_capabilities`, `on_telemetry`, `on_command_ack`; sync `process_payment(self)`; sync `raise_fault(self, code: FaultCode, *, sku: str | None = None, outcome: str | None = None) -> None`.
- Deprecated aliases kept until task 5: each old `_handle_mqtt_*` name, `_process_payment`, `_raise_fault`, `raise_data_fault` as one-line delegates with a `# deprecated: removed in the public-surface cleanup` comment.

- [x] Rename each method body to its new name; add the deprecated alias beside it.
- [x] Update `SUBSCRIPTIONS` to the new names and the pinned tuple in `tests/test_mqtt_inbound.py`.
- [x] Grep `controller/ services/ web_interface/ main.py` for every old name and switch callers to the new one (the `DispenserProfileGate` `raise_fault=` wiring, `startup_recovery`'s `raise_data_fault`, the `TelemetryRouter` callbacks).
- [x] Repoint tests with a careful search-and-replace per old name; keep positional/keyword call shapes unchanged. Note `_raise_fault(code, sku=..., outcome=...)` callers that pass `sku` positionally must become keyword.
- [x] Run lint, full suite, and the private-access count. Report the count before and after.

### Task 2: Public collaborators and `raise_fault` consumers

**Files:**
- Modify: `controller/vmc.py` (new public properties; delete alias properties)
- Modify: tests referencing `_lockouts`, `_machine_faults`, `_pending_refunds`, `_maintenance_hold`, `_maintenance_idle_task`, `_maintenance_sweep_task`, `_dispenser_profiles`, `_pending_tasks`, `_persist_tasks`, `_session_store`, `_mqtt_client`, `_command_dispatcher`, `_sellable_products`, `_snapshot`, `_publish_status`, `_finish_dispensing`
- Modify: `web_interface/` or `services/` callers of any deleted alias (grep first; `web_interface/context.py` uses `maintenance_hold`, which stays)

**Interfaces:**
- Produces on `VMC` as read-only `@property`: `faults -> FaultRegistry`, `escrow -> EscrowLedger`, `refunds -> RefundProtocol`, `recovery -> SessionRecovery`, `lease -> MaintenanceLease`, `gate -> DispenserProfileGate`, `tasks -> TaskRunner`, `session_store -> SessionStore | None`, `mqtt_client`, `command_dispatcher`, `health_monitor`, `availability`, `event_recorder`.
- Deleted: `_lockouts`, `_machine_faults`, `_pending_refunds`, `_maintenance_hold` (getter and setter), `_maintenance_idle_task`, `_maintenance_sweep_task`, `_dispenser_profiles`, `_pending_tasks`, `_persist_tasks`, `_loop`. Internal reads inside `vmc.py` switch to the collaborator (`self._faults.is_locked(...)`, `self._lease.hold`, `self._tasks.loop`).
- Kept public: `maintenance_hold`, `active_faults()`, `credit_escrow`, `escrow_credits`, `has_credit`, `get_status()`, `selected_product`, `pending_sale_shares`.

- [x] Add the properties; switch every internal `self._lockouts` / `self._maintenance_hold` / `self._loop` read in `vmc.py` to the collaborator; delete the alias properties.
- [x] Rewrite tests: `vmc._lockouts["X"] = code` → `vmc.raise_fault(code, sku="X")` (check that the test still passes with the side effects; if a test relied on *no* side effects, rewrite its assertions rather than restoring a bypass and report it); `vmc._lockouts` reads → `vmc.faults.is_locked(sku)` or `vmc.faults.lockouts`; `_machine_faults` → `vmc.faults.has(code)`; `_pending_refunds` → `vmc.refunds.pending`; `_maintenance_hold` reads → `vmc.maintenance_hold`; the single `vmc._maintenance_hold = None` write → `vmc.lease.release("admin")`; `_pending_tasks` / `_persist_tasks` → `vmc.tasks.pending` / `vmc.tasks.persist`; `_session_store` / `_mqtt_client` / `_command_dispatcher` → the public property; `_sellable_products` → `[p for p in vmc.products if vmc.faults.is_locked(p.sku) is None]` or a public `sellable_products()` if more than two tests need it; `_snapshot`, `_publish_status`, `_finish_dispensing` → look at each test and use the nearest public path (`vmc.recovery` / `active_faults()` / driving the FSM), reporting any that genuinely need a new public method.
- [x] Run lint, full suite, private-access count. Report the per-name remaining list.

### Task 3: Fake scheduler

**Files:**
- Modify: `controller/task_runner.py` (`schedule(..., *, label: str = "")`)
- Modify: `controller/vmc.py` (`__init__(self, config, *, tasks: TaskRunner | None = None)`; labels on every `_schedule` call; labels passed through by `RefundProtocol`, `MaintenanceLease` closures)
- Modify: `controller/refund_protocol.py`, `controller/maintenance_lease.py` (pass `label=` when calling the injected `schedule`)
- Create: `tests/fakes.py` (`FakeTask`, `ScheduledCall`, `FakeTaskRunner`)
- Modify: `tests/conftest.py` (`vmc_fake_time` fixture)
- Create: `tests/test_fakes.py` (FakeTaskRunner's own tests)
- Modify: tests referencing `_dispense_timed_out`, `_expire_session`, `_maintenance_idle_expired`, `_maintenance_sweep_tick`, `_maintenance_run_started`, `_maintenance_run_finished`, `_dispense_timeout_task`, `_session_timeout_task`, `_dispense_timeout_seconds`, `_session_timeout_seconds`, `_fire_and_forget`

**Interfaces:**
- `TaskRunner.schedule(self, delay_seconds: float, callback: Callable[[], None], *, label: str = "") -> asyncio.Task | None` (label ignored by the real runner).
- Labels used by the VMC and collaborators: `"dispense_timeout"`, `"session_timeout"`, `"refund_deadline"`, `"maintenance_idle"`, `"standby_sweep"`.
- `tests/fakes.py`: `@dataclass FakeTask(cancelled: bool = False)` with `done() -> bool` (True once fired or cancelled) and `cancel()`; `@dataclass ScheduledCall(delay: float, callback: Callable[[], None], label: str, task: FakeTask)`; `class FakeTaskRunner` with `loop`, `pending`, `persist`, `attach(loop)`, `fire_and_forget(coro, *, persistent=False)` (schedules on the running loop via `loop.create_task` and tracks it like the real one), `schedule(delay, callback, *, label="")`, `drain_persistence(timeout=3.0)`, `cancel_pending()`, and helpers `scheduled -> list[ScheduledCall]` (not fired, not cancelled), `fire(label: str) -> None` (most recent live call with that label; raises `LookupError` naming the live labels if none), `fire_all() -> None`.
- `tests/conftest.py`: fixture `vmc_fake_time` yielding `(vmc, runner)` built with `VMC(config, tasks=FakeTaskRunner())`, attached to the running loop, following whatever the existing `vmc` fixture does for config and wiring.

- [x] Add `label` to `TaskRunner.schedule` and to every call site; add the `tasks=` constructor argument.
- [x] Write `tests/fakes.py` and `tests/test_fakes.py` (schedule records and returns a task; `fire` runs exactly one and retires it; `cancel()` removes from `scheduled`; `fire` on an unknown label raises with the live labels in the message; `fire_and_forget` runs the coroutine).
- [x] Rewrite timer tests: `vmc._dispense_timed_out()` → `runner.fire("dispense_timeout")`; `vmc._expire_session()` → `runner.fire("session_timeout")`; `_maintenance_idle_expired` → `fire("maintenance_idle")`; `_maintenance_sweep_tick` → `fire("standby_sweep")`; `_dispense_timeout_task is not None` → `any(c.label == "dispense_timeout" for c in runner.scheduled)`; `_dispense_timeout_seconds` / `_session_timeout_seconds` overrides → assert on `ScheduledCall.delay` instead, or keep the override via a public class attribute if the test needs a short real timeout (report which). `_maintenance_run_started` / `_finished` → `with vmc.maintenance_test_run():`. `_fire_and_forget` → `vmc.tasks.fire_and_forget`.
- [x] Run lint, full suite, private-access count.

### Task 4: SaleContext

**Files:**
- Create: `controller/sale_context.py`
- Modify: `controller/vmc.py` (replace `selected_product`, `pending_sale_shares`, `_sale_is_test`, `_sale_mechanism`, `_dispense_request_id`, `_dispense_seq` with `self._sale: SaleContext | None` and a `sale` property; `selected_product` and `pending_sale_shares` become read-only properties over it)
- Modify: tests referencing `_sale_is_test`, `_test_sale_in_progress`, `_dispense_request_id`
- Create: `tests/test_sale_context.py`

**Interfaces:**
- `controller/sale_context.py`: `@dataclass(frozen=True) class SaleContext: product: Product; shares: dict[str, float] | None = None; is_test: bool = False; mechanism: str | None = None; request_id: str | None = None; seq: int = 0; started_at: float = 0.0` with helper `with_(self, **changes) -> SaleContext` wrapping `dataclasses.replace`.
- `VMC.sale -> SaleContext | None` (read-only property). `VMC.selected_product -> Product | None` and `VMC.pending_sale_shares -> dict[str, float] | None` become properties reading `self._sale`. Transitions: `select_product` creates the context; `process_payment` replaces it with `shares`; `on_dispense_product` replaces with `request_id`, `seq`, `mechanism`; `run_test_sale` creates it with `is_test=True`; `_finish_dispensing`, `on_cancel_sale`, `on_reset`, `on_error`, `_expire_session` clear it; `on_vend_failed` keeps `product` but clears `shares`, `request_id`, `mechanism` (the sale returns to `interacting_with_user` with the product still selected, matching today's behavior where `selected_product` survives a failed vend — verify by reading `on_vend_failed` before coding and report if the current code clears it).
- `_test_sale_in_progress` stays a private flag (it guards re-entrancy, not sale state) but gains a public read-only `test_sale_in_progress` property for the three tests that read it.

- [x] Write `tests/test_sale_context.py`: construction defaults, `with_` returns a new instance and leaves the original unchanged, frozen (assignment raises).
- [x] Introduce `self._sale` and the properties; migrate each write site; keep every read through the properties where it already exists.
- [x] Rewrite the tests: `vmc._sale_is_test` → `vmc.sale is not None and vmc.sale.is_test`; `_dispense_request_id` → `vmc.sale.request_id`; `_test_sale_in_progress` → `vmc.test_sale_in_progress`.
- [x] Read `process_payment`, `on_vend_failed`, `_record_sale`, `_persist_then_dispense`, `on_dispenser_event` end to end after the change and confirm the shares that `on_vend_failed` restores are exactly the shares `process_payment` consumed (the FIFO attribution guarantee in CLAUDE.md). Report the reasoning.
- [x] Run lint, full suite, private-access count (expect zero outside `vmc.__class__` monkeypatches; list anything left).

### Task 5: Guard and cleanup

**Files:**
- Create: `tests/test_no_private_access.py`
- Modify: `controller/vmc.py` (delete the deprecated aliases from task 1)
- Modify: `CLAUDE.md` (FSM Core: describe the public surface, the read-vs-mutate rule, the fake scheduler fixture; delete the sentences that describe alias properties "kept for tests")
- Modify: any test still using a deprecated alias

**Interfaces:**
- `tests/test_no_private_access.py`: walks `tests/**/*.py` except itself, fails listing `file:line: match` for every regex `\bvmc\._[a-zA-Z]` hit (allow `vmc.__class__` and `vmc.__dict__` via the pattern requiring a lowercase letter after the underscore).

- [x] Write the guard test; run it; fix whatever it lists.
- [x] Delete the deprecated aliases; grep `controller/ services/ web_interface/ main.py tests/` for each old name to prove nothing references it.
- [x] Update CLAUDE.md.
- [x] Run lint and full suite.
