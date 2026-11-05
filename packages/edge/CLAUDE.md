# Edge (`ice_edge`)

Scoped `CLAUDE.md`, loaded when working under `packages/edge/`. Moved from the root `CLAUDE.md`; project-wide rules stay there.

### Entry Point & Startup (`packages/edge/src/ice_edge/__main__.py`)

`main()` loads `config.json` into a Pydantic `ConfigModel`, then runs four
concurrent asyncio tasks on a single event loop: a uvicorn web server (host/port
from `config.web`, default `0.0.0.0:26123`, with sessions persisted in
`data/access.json`), the MQTT client, the health monitor, and the report
scheduler. `packages/common/src/ice_common/task_supervisor.py` provides the reusable
`supervise(name, coro_factory, restart_delay=5.0)` helper: background components
restart after a crash or unexpected return, but cancellation propagates.
`packages/common/src/ice_common/task_lifecycle.py` provides `run_until_primary_exits(primary,
*background)`: when uvicorn finishes or fails, background tasks are cancelled
and awaited before application cleanup (Docker's `restart: unless-stopped`
handles process-level restarts). `main()` also installs its own SIGTERM/SIGINT handlers (`ice_edge.app.exit_signals.install_exit_handlers`, which set `server.should_exit`) so the shutdown sequence runs even when the edge is not PID 1, because uvicorn restores the previous handlers and re-raises the signal. Neither helper imports application components. `main()` calls `check_web_bind(host, port)` after `warn_if_no_buttons` and before any service is built, so an unbindable dashboard port (any resolved address) exits 1 up front; `run_until_primary_exits` additionally re-raises a primary's `SystemExit` (uvicorn's own bind-failure exit) only after cancelling the background tasks, a defensive guarantee since `asyncio.run` already runs `main`'s cleanup.

### Configuration (`packages/common/src/ice_common/config_model.py`, `config.json`)

All configuration is a single Pydantic `ConfigModel` loaded from `config.json`. The model has eight top-level sections: `version`, `physical` (machine details, people, products), `payment` (Stripe, PayPal, MDB), `communication` (email, SMS, Snapchat gateways), `mqtt` (broker connection), `web` (dashboard host/port/trusted proxies), and `reports` (scheduled sales-summary email: `schedule` off/daily/weekly, `hour` 0–23, `weekday` Monday-based 0–6, `extra_recipients`), and `cloud` (optional uplink: `enabled` default **false**, `url` default `http://127.0.0.1:26124`, `report_interval_seconds` 10–3600, default 60) — plus the scalar `machine_id` field. Two `physical` fields serve the IM600XL inputs: `ro_max_run_seconds` (default 3600, 60–86400; how long the RO pump may run before the edge raises `WTR-107`) and `ice_retry_cooldown_seconds` (default 1800, 60–86400; how long an ICE-101 lockout lasts before it clears itself, used only when the vending board has no bin sensor). `Product.sku` is limited to 64 characters and `Product.name` to 120 (`MAX_SKU_LENGTH`/`MAX_PRODUCT_NAME_LENGTH`): the catalog forms refuse longer values with a 400, while loading an existing `config.json` never refuses, clipping an over-long name with a warning and keeping an over-long sku (an identity) unchanged with a warning. `Product.button` is the optional customer-button id (e.g. `button_ice`) that selects the product; duplicates within the catalog are rejected. The reports section is edited at **`/settings/reports`** gated on `edit_contacts`. `ConfigModel` exposes convenience properties (`config.products`, `config.machine_owner`; nested sections such as `config.payment.stripe` are reached directly) so consumers don't need to navigate the nested structure. Missing keys are filled from Pydantic defaults at load time. Saves via
`packages/edge/src/ice_edge/config/config_store.py` are atomic (tmp + rename), write real secret values,
and keep a rolling `config.json.bak`.

`packages/edge/src/ice_edge/app/startup_config.py` owns config loading, first-run creation,
environment overrides, and the startup access-store / setup-mode warning
(`warn_if_setup_mode`); `__main__.py` calls it during startup, including `warn_if_no_buttons(config)` right after `load_config` (a migration aid: logs a warning when no product has a `button`, since a press maps to a product only by `button`, and an info line for a partial assignment; it never raises or edits the config). The config file path
is configurable via the `ICE_COLDER_CONFIG` environment variable, read at call
time by `ice_common.paths.config_path()` (used by `packages/edge/src/ice_edge/app/startup_config.py`, `packages/edge/src/ice_edge/config/config_store.py`, `packages/edge/src/ice_edge/vend/dispensers.py` and the simulators in `ice_sims/base.py`),
defaulting to `config.json` in the current working directory when unset. This
lets Docker point the app at a writable, bind-mounted location instead of
relying on a bind-mount targeting `config.json` directly (which would let
Docker create it as a directory on a fresh clone, since the file is
gitignored). If the resolved config path exists but is a directory, startup
logs a clear error and exits with code 1 rather than papering over it.

### Cloud uplink (`ice_edge/uplink/`)

Off unless `config.cloud.enabled`. `ice_edge/__main__.py` then calls `init_cloud_identity` (`ice_edge.uplink.identity`; a corrupt identity file or unwritable `data/` logs an error and leaves the uplink off, boot continues) and adds a supervised `uplink` task running `run_uplink`. Each pass: **register** the machine's Ed25519 key (`POST /api/v1/machines/register`, until accepted; nothing is sent before it; the response's `alerts_handled` flag is kept on the in-memory identity), deliver any unacknowledged claim-code **notice** (or its `DELETE`), `PUT` the `ice_ui` **bundle** once (retried each interval until accepted; the cloud accepts bundles only from claimed machines), then `POST` a `MachineReport` (built by `build_machine_report` from the `ice_edge.views.health` builders; a pure read) to `{url}/api/v1/machines/{fingerprint}/report`, keyed by the key fingerprint, not `machine_id`; re-reading `report_interval_seconds` each pass. A 401 on any call (`HttpUplink.last_status`) sends the loop back to register. `HttpUplink` has a 5 s timeout and never raises (failure logs a warning and returns `False`); there is no outbox, each report supersedes the last, and a sale is never blocked. `ice_edge.uplink` sits above `views` and below `app`/`web`, so it imports `Machine` only under `TYPE_CHECKING`.

### FSM Core (`packages/edge/src/ice_edge/sale/vmc.py`, `packages/edge/src/ice_edge/app/machine.py`)

`VMC` is the sale finite-state machine, seven explicit trigger methods over one `_transition(source, dest, before)` helper, and nothing else. States: `idle` -> `interacting_with_user` -> `dispensing` -> back to `idle` (or `error` from any state). Extra transitions: `cancel_sale` (interacting → idle, catalog edit removed the selection) and `vend_failed` (dispensing → interacting, price restored to escrow, product locked out per `packages/contracts/src/ice_contracts/board/vending_machine.py`'s `FAULT_TABLE`). The seven triggers (`start_interaction`, `dispense_product`, `complete_transaction`, `cancel_sale`, `vend_failed`, `error_occurred`, `reset_state`) each name their source state, destination and `before` hook in one `_transition` call; an invalid trigger raises `InvalidTransition`; `set_state(state)` forces a state with no hook or observer (the two in-FSM bypasses in `_fail_vend`/`_expire_session`, and the test seam). The other public methods -- `deposit_funds`, `select_product`, `process_payment`, `request_refund`, the session timer (`packages/edge/src/ice_edge/sale/session_timer.py`'s `SessionTimer`, armed and cancelled by the FSM; `_expire_session` is the expiry policy), `_fail_vend`, `_finish_dispensing`, `snapshot()`, and `sale` are the whole of the class. Refunds are real: `request_refund` takes all of escrow and queues its cash part as a `payout` on the MDB board through `PayoutDesk` and the `Payments` port. Heartbeat loss (raised by `FaultService.on_subsystem_liveness`, not the VMC) maps to `COM-101` (vending), `COM-102` (ice maker), `PAY-101` (MDB); a broker disconnect maps to `COM-103` (`FaultService.on_mqtt_connection`); both auto-clear on recovery.

**The dependency rule.** `VMC` holds no transport or service handle — no MQTT client, health monitor, session store, event recorder, command dispatcher, display controller, or maintenance-lease reference. Its `__init__` takes `config` plus eleven required keyword-only collaborators: `tasks` (`TaskRunner`), `escrow` (`EscrowLedger`), `payments` (`Payments`, a `Protocol` in `ice_edge.payment.ports`), `faults` (`FaultService`), `outputs` (`StatusOutputs`), `journal` (`SaleJournal`), `gate` (`DispenserProfileGate`), `dispense_factory` (builds one `DispenseCycle` per sale), and three read-only callables -- `in_maintenance() -> bool`, `availability() -> Availability | None`, `inventory() -> InventoryManager | None`. Everything the FSM publishes, persists, or displays goes through `outputs`; every fault it raises or clears goes through `faults`. `packages/edge/src/ice_edge/app/machine.py`'s `Machine` is the sole construction path and builds every one of these before building the VMC itself. Import-linter enforces the layer order of `ice_edge` (contract 4: `app > web > uplink > views > sale > ...`, runtime imports only, so nothing outside `app/` can import `ice_edge.app`) and, as contract 5, that `packages/edge/src/ice_edge/sale/vmc.py` directly imports none of `ice_edge.app`, `ice_edge.sale.maintenance_lease`, `ice_edge.sale.test_sale`, `ice_common.mqtt_client`, `ice_edge.monitoring.health_monitor`, `ice_edge.ledger.session_store` -- except the plain `SessionSnapshot` value object `snapshot()` constructs, the one permitted ignore --, `ice_edge.ledger.event_recorder`, `ice_edge.hal.command_dispatcher`, or `ice_edge.customer_ui.display_controller` (indirect chains such as `vmc -> dispense_cycle -> command_dispatcher` are allowed). `tests/meta/test_no_private_access.py` carries the separate per-file private-access budget ratchet (below).

**`Machine` (`packages/edge/src/ice_edge/app/machine.py`).** The composition root `ice_edge/__main__.py` builds once. `Machine(config, *, tasks=None)` constructs, in dependency order, `TaskRunner`, `EscrowLedger`, `FaultRegistry`, `StatusOutputs`, then `FaultService`, `DispenserProfileGate`, `MaintenanceLease`, `SessionRecovery`, `TelemetryRouter`, the telemetry consumers (`HealthTelemetry`, `IcePolicy`, `InputFaults`, subscribed to the router; `Availability` joins through `set_availability`; the gate is subscribed in the constructor), the `DispenseCycle` factory, `VMC`, `TestSaleRunner` (built after the VMC, since it alone holds a direct reference to it), and finally the `MdbGateway` (`_build_gateway`, built last because its callables reach the VMC; the VMC pays through a forwarding `_GatewayPayments` that resolves the gateway lazily). Where two collaborators need each other before both exist -- the lease needs `vmc.make_idle_for_service`, the VMC needs `lease.hold` -- `Machine` passes a closure over its own attribute (`lambda *a: self.vmc.<method>(*a)`), resolved only when actually called, long after construction finishes, so the apparent circularity never bites. `Machine` owns ten `set_*` service attachments (`set_mqtt_client`, `set_health_monitor`, `set_availability`, `set_display_controller`, `set_inventory_manager`, `set_event_recorder`, `set_session_store`, `set_command_dispatcher`, `set_dispenser_profiles`, `set_session_liveness`), `attach_to_loop`, `cancel_pending_tasks`, `drain_persistence`, and twenty-one read-only properties (`vmc`, `tasks`, `escrow`, `faults`, `outputs`, `gateway`, `gate`, `lease`, `test_sales`, `recovery`, `telemetry`, `session_store`, `mqtt_client`, `command_dispatcher`, `health_monitor`, `availability`, `event_recorder`, `display_controller`, `inventory`, `maintenance_hold`, `subsystem_capabilities`). `set_session_store` runs the boot-time snapshot evaluation (`SessionRecovery.evaluate_at_boot()`, logging the `discard_test` warning or calling `Machine._flag_uncertain_session` on `uncertain`) and backs the four PAY-104 recovery forwards (`pending_sale_for_recovery`, `pending_sale_already_recorded`, `reserve_pending_sale`, `mark_pending_sale_recorded`) that routes and 20+ tests call as `machine.<method>()`. `packages/edge/src/ice_edge/web/context.py` stores `machine_instance` and sets `vmc_instance = machine.vmc` in lockstep, so the many pre-existing `context.vmc_instance` reads keep working. `SUBSCRIPTIONS` (`packages/edge/src/ice_edge/hal/mqtt_inbound.py`) is `(topic, owner, method)` triples -- `owner` is `"vmc"` for the two sale-driving handlers (`on_button_press`, `on_dispenser_event`), `"payments"` for `payment/event` (`intake.handle`) and `payment/state` (`state_consumer.handle`), or `"telemetry"` for the other seven -- that `Machine.set_mqtt_client` resolves against `self.vmc`/`self.gateway`/`self.telemetry` (a dotted method name is walked attribute by attribute), in table order.

**Task runner extraction:** the asyncio task plumbing — fire-and-forget tasks with failure logging, delayed callbacks, and the persistent-task set drained (not cancelled) at shutdown — lives in `packages/edge/src/ice_edge/supervisor/task_runner.py`'s `TaskRunner`, pure event-loop bookkeeping with no domain knowledge. `VMC._schedule` is a one-line delegate onto `self._tasks`; `drain_persistence` and `cancel_pending_tasks` live on `Machine` instead — `Machine.cancel_pending_tasks` calls `self._tasks.cancel_pending()`, then the VMC's own `cancel_timers()` (the dispense cycle's timer and the session timeout). `tasks` is the public read-only `@property` over the `TaskRunner` itself on `machine`; tests read `machine.tasks.loop`/`.pending`/`.persist` directly, and the `machine_fake_time`/`vmc_fake_time` fixtures construct `Machine(config, tasks=FakeTaskRunner())` in place of a real one.

### Services (`ice_edge` layers)

- `payment/mdb/` - the edge side of the MDB payment board (`MdbGateway` and its `EventIntake`, `StateConsumer`, `PolicySync`, `PayoutProtocol`, `Reconciler`; see "Payouts" under FSM Core); `payment/ports.py` holds `Payments`, `PendingPayout`, `cash_cents`, `cash_share`; `ledger/payment_cursor.py`'s `PaymentCursorStore` persists the last processed `seq` of the two latest board boots in `data/payment_cursor.json`
- `config_store.py` - persists config changes (add/update products) back to `config.json`
- `access.py` - session auth with roles, PINs, devices, emergency codes, and back-off; persists to `data/access.json`
- `mailer.py` - sends all outbound email via SMTP (`send_email`): login OTP, setup/emergency codes, alert emails (via `notifier._send_email`), emailed reports, the access report, and the comms-settings test email
- `fsm_control.py` - translates admin commands (restart, reset, shutdown) into actions
- `availability.py` - permissive truth table (ROADMAP §3) split into three
  gates: `safety` rows block payment and sales, `fulfillment` rows block only
  the individual sale, `alert` rows block nothing. Publishes
  the payment policy on change (`set_publisher` is wired to `PolicySync.on_payment_enabled_changed`, which sends `set_policy` with `accept` = `payment_enabled`); feeds the health tab and `/screen`. Only the
  seven codes in `ice_contracts.board.vending_machine.PAYMENT_BLOCKING_FAULTS` can
  inhibit payment. `product_sellable`/`sale_available` take an optional
  `ignore_faults` set that exempts named blocking codes from the
  `no_critical_fault` row for that call only, never from `payment_enabled`;
  `test_sale_sellable` is the one production caller, exempting a maintenance
  test sale from its own lease's `SVC-102` alone — see FSM Core's maintenance
  lease paragraphs above. `command_inhibited(command)` (subsystem-windows
  design) is a pure read of the already-computed rows — no recompute, no
  publish — answering whether the VMC would currently refuse `dispense`,
  `water_valve`, or `set_policy`; a subsystem window's output/input
  signal is inhibited iff its declared `driven_by` names a command this
  returns true for. `bin_instrumented` (default true; `on_vending_capabilities`
  sets it false when the vending board declares no `bin_half_full` channel)
  makes `ice_available` pass with "no bin sensor" (ICE-101 still fails it
  first). The module-level `_INPUT_ROWS` table maps `VendingChannel` -> row
  (`sump_high`->`no_leak`, `water_pressure_ok`, `ro_tank_empty`->`ro_tank_ok`,
  `service_door`); a test pins its keys to the contract's `INPUT_FAULTS`. The MDB board's
  retained `payment/state` reaches it through `set_payment_state(state)` (fed by
  `StateConsumer`): `payment_coin_ready` and `payment_bill_ready` are `alert`-gate rows
  (fail on an `offline`/`error`/`tilt`/`jammed` device, pass "not present" for an absent one) and
  `cash_accepting` is a `fulfillment` row that fails when the board's `inhibited_reason` is
  `device_fault` or `edge_silent`, or when no cash device is usable; all three stay unknown until
  the first state arrives, and `payment_alive` is still the heartbeat row for `mdb`.
- `health_monitor.py` - per-board signal store (subsystem-windows design):
  `record_signal(subsystem, channel_id, value, *, text=None)` tracks each
  declared channel's latest value plus binary transition/dwell timing
  in-memory (reset on restart); `declaring_subsystem(channel_id, kind=None)`
  attributes a reading to the one board whose stored `capabilities/<name>`
  document declares that channel; `record_temperature`/`record_channel` call
  through it so readings for undeclared channels are still consumed for
  control and alerting but never shown on a window; `temp_range` feeds the
  in-range check for temperature rows.
- `session_store.py` - atomic snapshot of the live sale in `data/session.json`;
  an open snapshot at boot raises `PAY-104`, which alerts the operator and
  holds the evidence file until an admin clears it, but never inhibits payment
- `command_dispatcher.py` - one `CommandDispatcher`, constructed in `ice_edge/__main__.py`
  and handed to both the VMC (`Machine.set_command_dispatcher`) and the routes
  (`routes.set_command_dispatcher`); it registers `cmd/+/ack` **once** and
  correlates each ack to its command by `request_id`. A timeout retries
  **exactly once, with the same `request_id`** — never a fresh one — because
  every subsystem (`packages/sims/src/ice_sims/base.py`) caches its last 32 acked
  `request_id`s and replays the cached ack instead of re-running the
  handler, so a retry with a new id would repeat a real-world side effect
  (a second `dispense` cycle) instead of just asking again. Raises
  `CommandTimeout(subsystem, command)` immediately when the broker is
  disconnected, without waiting out the timeout first.
- `mqtt_client.py` - the MQTT client wrapper; `subscribed` is a read-only property, true once every registered topic's subscribe has been acknowledged on the current connection (false initially, on disconnect, and in `run()`'s error paths) -- tests wait on it before publishing a non-retained message the VMC must receive
- `paths.py` - `LOG_DIR`, `LOG_FILE`, `DATA_DIR` shared by main, routes and services
- `auth_policy.py` - PIN policy (`pin_problem`); validates 4–8 digits with no repeats or runs
- `event_recorder.py` - records heartbeats, refunds, vend failures, and sales to a durable SQLite schema; `sales` and `cash_collections` are never pruned, while `events` keeps a 90-day retention window via `_prune_with` (which touches only the `events` table); `set_write_observers(on_failed, on_ok)` reports writer-thread insert failures and recovery (wired to `DATA-105` by `Machine`); `db_path` is the public read-only property for the live database file, including after a `DATA-102` corrupt-rename fallback switches it; the writer queue is bounded (`QUEUE_MAXSIZE` 10,000): `record()` drops and counts rows when it is full (the count is logged, one warning at the 1st drop and every 1000th after), while `record_cash_collection` and `update_metadata` wait up to 5 s and then raise `RuntimeError("event recorder queue full")`; schema changes are appended to the module-level `MIGRATIONS` list, never edited in place, and `PRAGMA user_version` tracks the applied count (migration 2 converted money to integer cents: `sales.price_cents`, `sales.methods` JSON in cents, `cash_collections.expected_cash_cents`, and the money event values; report keys are `_cents`, and CSV money cells are written as two-decimal text such as `2.50`) (a database newer than the code, or a failed migration, sets `db_unavailable` and raises `DATA-103`); heartbeats are coalesced to one `heartbeat` row per `HEARTBEAT_BUCKET_SECONDS` (60 s) bucket across all subsystems, and the uptime queries count distinct buckets, so uptime resolution is one minute
- `reports.py` - five report queries (`by_period`, `by_product`, `by_method`, `collections`, `summary`); window presets (`7d`, `30d`, `90d`, `12m`, `all`) via `resolve_window` (unknown values fall back to `30d`); CSV rendering via `render_csv` (returns **bytes**); and report filename via `report_filename`. All functions are **synchronous** and called from routes through `asyncio.to_thread`; each calls `recorder.flush()` first
- `report_scheduler.py` - a supervised loop with a **bounded sleep of at most 60 s**, re-reading the live `config` each pass so turning the schedule off stops the next send within a minute; **per-period de-duplication** via a `report_sent` event, matching **any** stored event for the period rather than only the most recent; and **at most one catch-up** at startup for the most recent completed period, never a backlog. A failed send waits for the next due occurrence rather than retrying every pass
