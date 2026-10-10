# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**ice-colder** is a vending machine controller (VMC) application. It manages product selection, payment processing, hardware communication (MDB bus), and provides a web-based dashboard for monitoring and configuration.

## Commands

| Task | Command |
|------|---------|
| Install dependencies | `uv sync` |
| Run the application | `uv run python main.py` |
| Run all tests | `uv run pytest` |
| Run a single test | `uv run pytest tests/test_file.py::test_name` |
| Lint/format | `ruff check --fix .` then `ruff format .` |
| Build Tailwind CSS | `.tailwind/tailwindcss.exe -c web_interface/tailwind.config.js -i web_interface/tailwind.input.css -o web_interface/static/app.css --minify` |
| Run the opt-in browser tests locally | `ICE_COLDER_BROWSER_TESTS=1 uv run pytest tests/test_dashboard_v2_home_browser.py tests/test_dashboard_v2_boosted_nav_browser.py tests/test_users_codes_pin_browser.py tests/test_touch_feedback_browser.py` |

**Browser tests:** `tests/test_dashboard_v2_home_browser.py`, `tests/test_dashboard_v2_boosted_nav_browser.py`, `tests/test_users_codes_pin_browser.py` and `tests/test_touch_feedback_browser.py` drive real headless Chrome via `tests/browser/*.mjs` checker scripts (Node standard library only). They're skipped unless `ICE_COLDER_BROWSER_TESTS=1` is set **and** a Chrome/Chromium binary (honoring `CHROME_PATH` first) plus a `node` executable are both found, so a machine without Chrome still runs the rest of the suite clean. CI sets the flag and installs a pinned Chrome and Node so these four run on every push/PR; a guard step in `ci.yml` fails the build if any of them skip instead of passing.

**Tailwind:** The binary is [v3.4.17 standalone CLI](https://github.com/tailwindlabs/tailwindcss/releases/download/v3.4.17/tailwindcss-windows-x64.exe); a Linux or macOS checkout needs the matching asset from the same release. The binary is gitignored (`.tailwind/`, ~40 MB); `web_interface/static/app.css` is **committed**. `tests/test_static_css.py` fails CI when a template uses a class the committed file lacks — but that test only checks that classes templates *use* are present, never that unused ones are *absent*. Rebuild and commit `app.css` by hand whenever templates change; a stale file (leftover classes from a deleted template, or a missing rebuild after one) passes CI silently and only a manual rebuild catches it.

## Architecture

### Entry Point & Startup (`main.py`)

`main()` loads `config.json` into a Pydantic `ConfigModel`, then runs four
concurrent asyncio tasks on a single event loop: a uvicorn web server (host/port
from `config.web`, default `0.0.0.0:26123`, with sessions persisted in
`data/access.json`), the MQTT client, the health monitor, and the report
scheduler. `services/task_supervisor.py` provides the reusable
`supervise(name, coro_factory, restart_delay=5.0)` helper: background components
restart after a crash or unexpected return, but cancellation propagates.
`services/task_lifecycle.py` provides `run_until_primary_exits(primary,
*background)`: when uvicorn finishes or fails, background tasks are cancelled
and awaited before application cleanup (Docker's `restart: unless-stopped`
handles process-level restarts). Neither helper imports application components.

### Configuration (`config/config_model.py`, `config.json`)

All configuration is a single Pydantic `ConfigModel` loaded from `config.json`. The model has seven top-level sections: `version`, `physical` (machine details, people, products), `payment` (Stripe, PayPal, MDB), `communication` (email, SMS, Snapchat gateways), `mqtt` (broker connection), `web` (dashboard host/port/trusted proxies), and `reports` (scheduled sales-summary email: `schedule` off/daily/weekly, `hour` 0–23, `weekday` Monday-based 0–6, `extra_recipients`) — plus the scalar `machine_id` field. The reports section is edited at **`/settings/reports`** gated on `edit_contacts`. `ConfigModel` exposes convenience properties (e.g., `config.products`, `config.machine_owner`, `config.stripe`) so consumers don't need to navigate the nested structure. Missing keys are filled from Pydantic defaults at load time. Saves via
`services/config_store.py` are atomic (tmp + rename), write real secret values,
and keep a rolling `config.json.bak`.

`services/startup_config.py` owns config loading, first-run creation,
environment overrides, and the startup access-store / setup-mode warning
(`warn_if_setup_mode`); `main.py` calls it during startup. The config file path
is configurable via the `ICE_COLDER_CONFIG` environment variable (read at call
time by both `services/startup_config.py` and `services/config_store.py`),
defaulting to `config.json` in the current working directory when unset. This
lets Docker point the app at a writable, bind-mounted location instead of
relying on a bind-mount targeting `config.json` directly (which would let
Docker create it as a directory on a fresh clone, since the file is
gitignored). If the resolved config path exists but is a directory, startup
logs a clear error and exits with code 1 rather than papering over it.

### Dispenser profiles (`dispensers.toml`)

A standalone, hand-edited `dispensers.toml` gives each physical dispense slot (`[slot.N]`) its own mechanism parameters (agitate/fill/release timings, channel names, accessory lead/lag) — separate from `config.json`'s product catalog, which only knows a product's `sku`/`slot`/`kind`. The file lives next to `config.json` and is resolved the same way: the `ICE_COLDER_DISPENSERS` environment variable (read at call time, mirroring `_config_path`), defaulting to `dispensers.toml` in the current working directory. Like `config.json`, it is gitignored (`.gitignore`), so a fresh checkout has none; `dispensers.example.toml` is the committed, generated reference a first run copies from. Only the `vmc` compose service sets `ICE_COLDER_DISPENSERS=/app/data/dispensers.toml` — the VMC is the only reader: it sends a product's whole validated `SlotProfile` inside the `DispenseCommand` it dispatches, so the simulators never read the file themselves and their copy of the env var (present through plan 1) was removed once plan 2 shipped the runtime wiring below.

Three modules split the work. `services/dispenser_schema.py` holds the Pydantic models for one slot's profile (`SlotProfile`, a discriminated union of `BaggedIceProfile`/`WaterFillProfile`) plus the per-mechanism step models (agitate/fill/release, accessories) and helpers (`drive_channels`, `sense_channels`, `worst_case_seconds`, `MECHANISM_FOR_KIND`). `services/dispensers.py` owns validation and file I/O: `validate_document(text, products, capabilities=None, dispense_timeout_seconds=120.0)` runs TOML parsing, per-slot schema validation, and cross-checks (catalog sku/slot/kind agreement, channel drive/sense conflicts, the dispense time budget, and — when a vending board's capabilities are known — channel direction) into a `ValidationReport` (`.findings`/`.errors`/`.warnings`/`.ok`/`.file_error`/`.render_text()`, each a list of `Finding(slot, path, line, severity, message)`); `DispenserProfiles(config, path=None)` wraps that with `.load()`, `.validate_text()`, `.save_text()` (atomic, digest-guarded), `.profile_for_slot()`, and `.set_capabilities()`. `services/dispensers_doc.py` generates `dispensers.example.toml` from the schema's own field metadata (descriptions, units, ranges, discriminator tags) so the example cannot drift from the schema it documents; regenerate it with `uv run python scripts/gen_dispensers_example.py` after any schema or sample-value change, and `tests/test_dispensers_example.py` fails CI on byte drift. `uv run python -m services.dispensers --check` validates the active file against the live catalog and prints a human-readable report (exit 1 on any error); `--example` prints the generated reference instead.

`CFG-101` (no valid dispenser profile for a slot, product-scope, `product_unavailable`) and `CFG-102` (`dispensers.toml` could not be read, machine-scope, `warning`) are registered in `contracts/vending_machine.py` (contract version 0.8.0). The file is loaded and its `ValidationReport` logged at startup (`services.startup_dispensers.load_dispenser_profiles`, called from `main()` right after `load_config()`; each finding logs at `warning` or `error` per its own severity, the verdict line at `info`; a directory at the resolved path logs a clear error and `sys.exit(1)`, mirroring `load_config`'s own check). The loaded `DispenserProfiles` instance is then handed to both the VMC and the routes module by `services.startup_dispensers.wire_dispenser_profiles(vmc, profiles)`, called right after `vmc.set_health_monitor(health)`. `VMC.set_dispenser_profiles(profiles)` stores it and immediately calls `reconcile_dispenser_profiles()`, which re-derives every product's `CFG-101` lockout and the machine's `CFG-102` fault from the currently loaded profiles — raising `CFG-101` for a sku with no valid profile, clearing it only when the existing lockout is `CFG-101` itself, and never touching another code's lockout. `VMC.dispenser_profile_for(product)` is the single read path `reconcile_dispenser_profiles`, `on_dispense_product`, and `run_test_sale` go through; `select_product` itself never calls it — it refuses a locked sku the same way it refuses any other lockout, by checking `self._lockouts.get(candidate.sku)`, which `reconcile_dispenser_profiles` populated with `CFG-101` for that sku.

Three deliberate deviations from the original design doc, recorded here rather than silently drifting: there is no top-level `DispenserFile` model, because each `[slot.N]` table is validated independently so one malformed slot only loses that slot, never the whole file; channel ids use the contract's own `CHANNEL_ID_PATTERN` (`contracts/common.py`) rather than a separate pattern; and the 0.7.0 → 0.8.0 contract bump that added `CFG-101`/`CFG-102` was originally this feature's only version bump — the VMC/routes runtime wiring (`DispenseCommand`, `DispenseStep`, the new `DispenserOutcome` members, `fault_for_outcome`) first reused that same 0.8.0 section on the theory that it was additive, not a schema break, but a Copilot review of PR #32 caught that `dispense`'s params actually changed shape and `cmd/dispense` was removed outright — wire-breaking, so `CONTRACT_VERSION` was bumped again, to **1.0.0**, per the module's own major-bump rule for breaking changes; see the Fault Codes section below for that bump's detail. Further deviations recorded for that runtime increment: `DispenserStatus.state` stays typed `str` rather than becoming an enum (both `DispenseStep` and `DispenserOutcome` strings flow through the same field over the sale's lifetime); the simulator's legacy `cmd/dispense` subscription was removed immediately rather than kept alongside the new path for a deprecation window, since no physical board exists yet to break; the command dispatcher reports a `door_open` completion as `status="failed"` (with a `detail` explaining the bag released but the door did not close) even though the VMC itself treats `door_open` as a customer success plus a fault, because the dispatcher's ack vocabulary has no third state; and `SessionSnapshot.dispense_mechanism` was added to the crash snapshot, additive beyond what the spec asked for.

### FSM Core (`controller/vmc.py`)

`VMC` is a finite state machine built on the `transitions` library. States: `idle` -> `interacting_with_user` -> `dispensing` -> back to `idle` (or `error` from any state). Extra transitions: `cancel_sale` (interacting → idle, catalog edit removed the selection) and `vend_failed` (dispensing → interacting, price restored to escrow, product locked out per `contracts/vending_machine.py` `FAULT_TABLE`). Refunds are real: `request_refund` publishes `cmd/payment/refund` and tracks the ack. The transition table is defined as a list of dicts (`TRANSITIONS`) at module level. Business logic (deposit funds, select product, dispense, refund) lives as methods on `VMC`. The VMC holds a reference to the live `ConfigModel` and a `PaymentGatewayManager`. Heartbeat loss raises `COM-101` (vending), `COM-102` (ice maker), `PAY-101` (MDB) and `COM-103` (broker) through the fault registry and auto-clears on recovery.

**Fault registry extraction:** the fault state itself — `lockouts` (product-scope, by sku) and `machine_faults` (machine-scope, by code) — lives in `controller/fault_registry.py`'s `FaultRegistry`, the first piece carved off the `VMC` god object. `VMC._raise_fault`, `clear_fault`, and `active_faults` are thin delegates to `self._faults` (a `FaultRegistry` built in `__init__`): they keep every side effect in its original order (the event-recorder `lockout_set`/`lockout_cleared` rows, the `logger.error`/`logger.info` lines, the health-monitor alert, the MQTT `alerts` publish, `_push_active_faults`) and every VMC-specific guard that consults other collaborators (the `SVC-102` maintenance-lease refusal, the `PAY-104` session-store-clear refusal, the `CFG-101` dispenser-profile re-check, `set_transaction_certain(True)` on `PAY-104`). `VMC._lockouts` and `VMC._machine_faults` are read-only `@property` aliases returning the registry's own dict objects (not copies), kept only because many existing tests read and write them directly.

**Maintenance lease (`MaintenanceHold`, system-tests design §2.2):** `VMC.begin_maintenance(user_id, session_id)` grants a lease to take the machine out of service for operator testing — refused unless the FSM is `idle`, escrow is zero, and no lease is already held. Granting it raises `SVC-102`, which `services/availability.py` treats as a **safety** row (built generically off `PAYMENT_BLOCKING_FAULTS`, not a special case), so `cmd/payment/enable` is withdrawn machine-wide for the whole lease. The lease is **`MaintenanceHold`, a dataclass with `runs_in_flight`** (an in-progress-run counter, not a boolean) plus `release_requested`: `end_maintenance` and the 5-minute idle timer (`_maintenance_idle_expired`) only defer a release while `runs_in_flight > 0`, and `maintenance_test_run()` (a context manager `run_test_sale` uses) increments on entry and decrements in a `finally`, so a failing or timed-out run still frees the lease. The lease is **never persisted** — it lives only on the live `VMC` instance and is not part of `SessionSnapshot`/`services/session_store.py`, so a restart clears it, matching the FSM's own reset semantics. Credit arriving during a lease is **refunded, not escrowed**: `deposit_funds` detects `self._maintenance_hold is not None` and, unless the deposit's `payment_method == "test"` (the one credit `run_test_sale` itself deposits), adds it to escrow only long enough to call `request_refund(reason="maintenance")` immediately. Only one simulated sale runs at a time: a second, concurrent `run_test_sale` call (same session — e.g. a double-submit, or the two-tab case `tests_level.py`'s `_acquire_lease_or_refusal` deliberately lets through) is refused inline by a `_test_sale_in_progress` flag, set before `maintenance_test_run()` is entered and cleared in an outer `finally` in `VMC.run_test_sale`; refusing here, before `maintenance_test_run()`/`runs_in_flight` are ever touched, is what stops a refused second call from leaking the counter and pinning the lease past that process's lifetime. `POST /tests/sale` (`web_interface/routes/tests_level.py:544-558`) catches the resulting `RuntimeError` as a normal refusal rendered via `partials/test_refusal.html`, not a 500.

**A test sale is exempt from its own `SVC-102`, and only from that:** the same failing `no_critical_fault` safety row that withdraws `cmd/payment/enable` also fails `Availability.sale_available`/`product_sellable` for an ordinary sale, so an unmodified lease would refuse the very product it exists to let a tech dispense. `product_sellable`/`sale_available` (`services/availability.py:263-304`) take an `ignore_faults: frozenset[str]` (default empty, so every existing caller — including a customer's `select_product` reached during a lease — is unaffected); when every currently active blocking code is in that set, `no_critical_fault` is treated as passing for this call only, while every other row, and every other blocking code, still fails and still blocks normally. `Availability.test_sale_sellable(product)` (`services/availability.py:306-318`) calls it with exactly `{SVC-102}`. `VMC.select_product` (`controller/vmc.py:2196-2207`) picks `test_sale_sellable` over `product_sellable` when `self._sale_is_test` is true — the exemption is keyed on the sale, never on the lease, so a customer press reaching `select_product` during the same lease still has `_sale_is_test` false, still calls `product_sellable`, and is still refused like any other safety-blocked sale. Any *other* active safety fault — a leak, a stuck trap door, a bad 24 V supply, a tripped heater high-limit — still fails `no_critical_fault` for a test sale exactly as it would a real one; running an actuator into a real hazard "because it's only a test" would be worse than the bug this fixed. `payment_enabled`/`payment_blocking_reasons` (`services/availability.py:320-334`) never consult `ignore_faults` — they read the rows directly — so `cmd/payment/enable` stays withdrawn for the entire lease regardless of which sale is attempted.

**Test-ness lives on the sale, not the hold:** `VMC._sale_is_test` is set only by `run_test_sale`, on the in-flight sale context, and is read (never inferred from the maintenance lease) by the dispense-completion and timeout handlers to decide whether to call `_record_sale()` (skipped entirely for a test) or record a `test_run` event instead — so a test sale writes neither a `sale` nor a `dispense` row, and releasing the lease mid-run cannot reclassify it, because nothing about the recording path consults the lease. `SessionSnapshot.is_test` carries the same flag into the on-disk crash snapshot (`services/session_store.py`), set from `self._sale_is_test` at the moment the snapshot is built. This is the money-safety consequence a review in this part caught: without it, a test sale that crashed mid-dispense would leave an open snapshot that looks exactly like a real uncertain sale, and the `PAY-104` recovery flow would let an operator "Record as sale" it into the never-pruned `sales` table. `is_test` is checked at two chokepoints so that can never happen — `SessionRecovery.evaluate_at_boot()` discards a test-flagged snapshot found at boot instead of raising `PAY-104` for it at all, and `SessionRecovery.pending_sale_for_recovery()` refuses to surface one as a recoverable card even if some future path left `PAY-104` active anyway, as defence in depth. A future contributor adding a second persistence path for pending sales must carry `is_test` through it too, or this guarantee breaks silently.

**FIFO method attribution:** `escrow_credits` is a FIFO ledger of `Credit(method, amount, ts)` that backs `credit_escrow`, which remains the authoritative total. Deducting a sale's price consumes credits first-in-first-out and records the per-method shares on `pending_sale_shares`; `vend_failed` restores **exactly those shares as separate credits with their original methods**, so money is never reclassified when a sale fails. Method strings are stored raw from `PaymentEvent.method` and classified only at query time. If the ledger and the total ever disagree the sale is booked to `{"unknown": price}` with a warning — a bug guard, not a path. The ledger itself now lives in `controller/escrow_ledger.py`'s `EscrowLedger` (the second piece carved off the VMC god object, after `FaultRegistry`), with `credit_escrow`/`escrow_credits` kept on the VMC as `@property` aliases of its `total`/`credits`.

**Refund protocol extraction:** the refund request → ack → one retry (same `request_id`) → `PAY-103` state machine lives in `controller/refund_protocol.py`'s `RefundProtocol` (the third piece carved off the VMC god object, after `FaultRegistry` and `EscrowLedger`). `VMC.request_refund` and `_handle_mqtt_refund_ack` are the entry points — the former hands the registry's own amount/reason to `self._refunds.begin(...)`, the latter validates the MQTT payload and hands the result to `self._refunds.handle_ack(...)` — while the terminal side effects (persisting the session, transaction-log lines, event-recorder `refund`/`refund_failed` rows, the customer-facing message, and raising `PAY-103`) stay on the VMC as the `on_confirmed`/`on_failed` callbacks (`_refund_confirmed`/`_refund_failed`) the protocol is constructed with. The retry deliberately reuses the SAME `request_id` rather than minting a fresh one — the subsystems dedupe on it and replay a cached ack instead of re-running the handler, exactly like the `command_dispatcher` retry described above; a fresh id would repeat a real-world side effect. `VMC._pending_refunds` is a read-only `@property` alias of the protocol's own `pending` dict, kept because many existing tests read and write it directly.

**Session recovery extraction:** the PAY-104 read-and-decide half of crash recovery lives in `controller/session_recovery.py`'s `SessionRecovery` (the fourth piece carved off the VMC god object, after `FaultRegistry`, `EscrowLedger`, and `RefundProtocol`). It owns the boot-time three-way decision over a loaded snapshot (`evaluate_at_boot()` returns a `BootDecision` of `discard_test`/`uncertain`/`closed`/`none`, performing `SessionStore.clear()` itself for the two discard cases) plus the read-only `pending_sale_for_recovery()` accessor and its record-once guards (`_pay104_sale_key`, `pending_sale_already_recorded`, `reserve_pending_sale`, `mark_pending_sale_recorded`) — `_recorded_pay104_keys` is now this class's own state, lost on restart by design just as before. The VMC keeps snapshot building (`_snapshot`/`_persist_session`), persistence (`_persist_then_dispense`, `drain_persistence`), the boot side effects left out of the decision itself (`_flag_uncertain_session`, called from `set_session_store` on `uncertain`; the warning log, logged from `set_session_store` on `discard_test`), and the four public methods (`pending_sale_for_recovery`, `pending_sale_already_recorded`, `reserve_pending_sale`, `mark_pending_sale_recorded`) as one-line delegates onto `self._recovery` — routes and 20+ tests call them on the VMC. The `is_test` chokepoints described above now live in `SessionRecovery.evaluate_at_boot` and `SessionRecovery.pending_sale_for_recovery`; the guarantee is unchanged.

**Telemetry router extraction:** every inbound MQTT handler that only reads/validates its payload and forwards it to the health monitor, availability, or a log sink — hardware IO, payment status, sensor/water-flow/telemetry readings, heartbeats, ice-maker events, command acks, and capabilities — lives in `controller/mqtt_inbound.py`'s `TelemetryRouter` (the fifth piece carved off the VMC god object, after `FaultRegistry`, `EscrowLedger`, `RefundProtocol`, and `SessionRecovery`). The VMC keeps every handler that drives the FSM or a sale (`_handle_mqtt_payment`, `_handle_mqtt_button`, `_handle_mqtt_dispenser`, `_handle_mqtt_refund_ack`) plus a thin one-line `async` delegate onto `self._telemetry` for each of the rest, so every existing call site and test keeps calling the same `VMC` method. The ICE-101 auto-clear (inside hardware IO) and the vending-capabilities dispenser-profiles reconcile (inside capabilities) are the two exceptions that reach back into FSM/fault state; both stay VMC callbacks (`_clear_ice101_lockouts`/`_on_vending_capabilities_validated`) the router invokes rather than code the router owns itself. `SUBSCRIPTIONS` (`controller/mqtt_inbound.py`) is the single `(topic_pattern, VMC_method_name)` table `VMC.set_mqtt_client` loops over to register every inbound handler, telemetry-only and sale-driving alike, in the same order the individual `client.register(...)` calls used to run in.

**Maintenance lease extraction:** the lease lifecycle — the hold itself, its idle timer, the standby session-liveness sweep, and the grant/release/takeover/run accounting — lives in `controller/maintenance_lease.py`'s `MaintenanceLease` (the sixth and last piece carved off the VMC god object, after `FaultRegistry`, `EscrowLedger`, `RefundProtocol`, `SessionRecovery`, and `TelemetryRouter`). The VMC keeps the FSM/escrow preconditions in `begin_maintenance`/`begin_standby`, the refunds, `run_test_sale` itself, and raising/clearing `SVC-102` as the lease's `on_granted`/`on_released` callbacks (constructed alongside the lease as `self._lease` in `__init__`). `MaintenanceHold` is re-exported from `controller.vmc` for the callers (`web_interface/context.py`, `web_interface/routes/tests_level.py`, tests) that reference the name. `VMC._maintenance_hold`, `_maintenance_idle_task`, and `_maintenance_sweep_task` are read-only (`_maintenance_hold` also write-able, since one test forces it to `None` to simulate the lease vanishing mid-run) `@property` aliases onto the lease's own `hold`/`idle_task`/`sweep_task`, kept because existing tests read them directly; `_release_maintenance_hold`, `_arm_maintenance_idle_timer`, `_maintenance_idle_expired`, `_arm_maintenance_sweep`, `_maintenance_sweep_tick`, `_maintenance_run_started`, and `_maintenance_run_finished` are one-line delegates onto the matching `MaintenanceLease` method, for the same reason.

**Task runner extraction:** the asyncio task plumbing — fire-and-forget tasks with failure logging, delayed callbacks, and the persistent-task set drained (not cancelled) at shutdown — lives in `controller/task_runner.py`'s `TaskRunner`, pure event-loop bookkeeping with no domain knowledge. `VMC._fire_and_forget`/`_schedule`/`drain_persistence` are one-line delegates onto `self._tasks`; `cancel_pending_tasks` calls `self._tasks.cancel_pending()` and then still performs its own domain cancels (`_cancel_dispense_timeout`, `_cancel_session_timeout`, `_refunds.cancel_all()`). `VMC._loop`, `_pending_tasks`, and `_persist_tasks` are read-only `@property` aliases onto the runner's `loop`/`pending`/`persist`, kept because existing tests read them directly.

**Sales recording durability:** A sale is inserted **synchronously on its own WAL connection** (`synchronous=NORMAL`) and **awaited via `asyncio.to_thread` before the FSM returns to idle** — the write sits at `_record_sale`, immediately before `_finish_dispensing`, and the `dispense` event stays because the existing KPIs read it. On failure the record goes to `data/sales-journal.jsonl` (append + fsync) and the VMC raises **`DATA-101`** while letting the vend complete — a storage problem must never fail a sale. At startup the journal is replayed: the replay insert is **idempotent on `(ts, sku)`** so a crash between the insert and the truncate cannot duplicate a row, each row commits in its **own transaction** so one bad row cannot block the others, and a row that can be neither inserted nor set aside stays in the journal. `services/startup_recovery.py` clears `DATA-101` on the **journal being drained** (absent or empty after the call), **never** on the replay function's integer return — that integer is the count inserted and is `0` both for "nothing to do" and for "fully drained, all duplicates or rejects". A corrupt `events.db` is renamed aside to `events.db.corrupt-<timestamp>`, a fresh database is created, and **`DATA-102`** is raised so the machine keeps running; the constructor never raises on a corrupt file.

**Dispatcher-based dispense (dispenser-profiles plan 2):** `on_dispense_product` no longer publishes `cmd/dispense` directly; it looks up `self.dispenser_profile_for(product)` (keyed on the product's own stable `slot` field, not its list position), builds `DispenseCommand(slot, mechanism, profile)` from the result, and hands it to `_persist_then_dispense`, which saves the dispensing snapshot first and then awaits `CommandDispatcher.send("vending", "dispense", cmd.model_dump(mode="json"))` — only the accepted ack, never completion. Any failure on that path — no dispatcher wired, `send()` raising `CommandTimeout`, any other exception, or an ack whose `status` is not `"ok"` — fails the vend immediately with `PAY-102`, via `_fail_dispense_async`, rather than waiting out the 120 s dispense-timeout fallback. A fresh `self._sale_seq`, bumped once per `on_dispense_product` call, guards every one of those async failure paths: `_fail_dispense_async` compares its captured `seq` against the live `self._sale_seq` and against `self.state == "dispensing"` before acting, so a delayed failure from an *earlier* sale's dispatch (or a dispatch that already settled through the real `hardware/dispenser` report) can never cancel a later or already-finished sale's timer, raise its `PAY-102`, or refund its price. The terminal outcome is still read off `hardware/dispenser` as before: `complete` and `door_open` both record the sale (or the `test_run` event for a test sale) and call `_finish_dispensing`; `door_open` additionally raises `ICE-402` after recording, since it is a customer success (the product was released) and a hardware fault (the trap door didn't close) at once. Every other terminal outcome resolves through `fault_for_outcome(self._sale_mechanism, outcome)` (`contracts/vending_machine.py`), the sole reader of the `(mechanism, outcome) -> FaultCode` `OUTCOME_FAULTS` table; a board reporting an outcome unmapped for its own mechanism (e.g. a water board sending `jam`) is caught as `KeyError` and falls back to that mechanism's `error` mapping rather than crashing the MQTT handler. `CFG-101` is a standing invariant enforced at two points, never just one: `select_product` refuses a profile-less product up front via the ordinary lockout path — the sku is already `CFG-101`-locked in `self._lockouts` by `reconcile_dispenser_profiles`, so `on_dispense_product`'s own profile lookup should never fail — and `on_dispense_product` still re-checks and fails the vend with `CFG-101` if the profile vanished between selection and dispense (a `dispensers.toml` reload racing the sale) — a defensive fallback, not the primary guard. The vending simulator's `water_flow` telemetry channel stays cumulative gallons (what `sensors/water_flow` actually publishes); the per-fill flow-meter pulse count is its own `fill_pulses` channel, published alongside `agitator_current`/`auger_current` on `telemetry/vending/<id>` — none of which the VMC subscribes to yet, so a subsystem window shows "never reported" for all three until per-slot telemetry (spec §11) is built.

**Standby lease (system-tests design §2.2a):** `MaintenanceHold.standby` flags a lease taken explicitly by a tech to make a busy machine idle and hold it out of service for the whole login session. `VMC.begin_standby(user_id, session_id)` refunds any credit on the machine (`reason="maintenance"`), cancels a live customer sale, and grants the lease with `standby=True` — refused only while `dispensing` ("vend finishing, tap again") or when a different session holds the lease ("held by <id>"); if the caller's own session already holds an opportunistic lease it is upgraded in place. Unlike the opportunistic lease, a standby lease has no idle-timer release: `MaintenanceLease.idle_expired` skips it entirely. Instead, a 30 s sweep runs via `MaintenanceLease.arm_sweep()` and `MaintenanceLease.sweep_tick()`, calling `session_is_live(hold.holder_session_id)` — a predicate wired via `VMC.set_session_liveness(predicate)` — to check whether the holder's web session is still live **without refreshing its idle clock**. When the session is gone, the lease is released with reason `session_ended`, deferred if runs are in flight just like the idle timer. The predicate is `AccessStore.session_is_live(session_id: str | None) -> bool` (services/access.py:788), which applies the same idle-limit and absolute-cap rules as `resolve_session` but never touches `last_active_at` and never deletes expired entries. Wiring happens in `main.py:389` after the VMC instance is constructed and handed to routes. With no predicate wired (a fallback for tests), a standby lease behaves like the opportunistic one (idle timer armed and logged once).

### Web Dashboard (`web_interface/`)

FastAPI app (`server.py`) with Jinja2 templates and HTMX-driven partials. `routes/` is a package with one module per area (`home.py`, `health.py`, `products.py`, `inventory.py`, `reports.py`, `controls.py`, `tests_level.py`, `users.py`, `settings.py`); `routes/__init__.py` wires them all with `attach_routes`. Templates live in `web_interface/templates/` with HTMX partial fragments in `templates/partials/`. Static assets in `web_interface/static/`.

**The v2 shell** (`base.html` + `web_interface/levels.py`) replaces the tabbed dashboard. Every level is a real URL and every level has its own breadcrumb trail and Back button that goes to the parent, not through history. `base.html` defines three blocks: `title` (for `<title>`), `body` (the sole content of `<main>`, where your level goes), and `bar_variant` (the bar header, not to be overridden). The bar itself uses `hx-swap-oob="true"` to deliver an out-of-band swap on every response, so a boosted navigation updates it with no reload. The navigation tree is a hierarchy of 24 named levels plus three parameterized ones (a product SKU, a subsystem name, a user id). Each level is defined in `web_interface/levels.py` as a frozen dataclass with `title`, `url`, `parent`, and properties `crumbs`, `parent_url`, plus a classmethod `Level.child(parent, title, url)` for parameterized levels.

The tree, every URL with the permission that gates it (`Permission.<x>` from `services/access.py`; "A or B" is `_require_any(A, B)`, a local OR-semantics dependency defined in `products.py` and `settings.py` — `web_auth.require()` itself is AND-only):

| URL | Gate |
|---|---|
| `/` | `view_status` (held by every role) |
| `/health` | `view_status` |
| `/health/subsystems` | `view_status` |
| `/health/subsystems/{name}` | `view_status` |
| `/health/faults` | `view_status` |
| `/health/availability` | `view_status` |
| `/health/logs` | `view_logs` |
| `/products` | `edit_catalog` **or** `edit_placement` |
| `/products/new` | `edit_catalog` |
| `/products/{sku}` | `edit_catalog` **or** `edit_placement` |
| `/products/{sku}/catalog` | `edit_catalog` |
| `/products/{sku}/placement` | `edit_placement` |
| `/products/{sku}/copy` | `edit_catalog` |
| `/inventory` | `edit_placement` |
| `POST /inventory/collect` | `collect_cash` (**all four roles**) |
| `/reports` | `view_reports` |
| `/reports/period` | `view_reports` |
| `/reports/product` | `view_reports` |
| `/reports/product/{sku}` | `view_reports` |
| `/reports/method` | `view_reports` |
| `/reports/collections` | `view_reports` |
| `/controls` | `machine_controls` |
| `/tests` | `run_tests` |
| `/tests/log` | `run_tests` |
| `/tests/sale` | `run_tests` |
| `/tests/{subsystem}` | `run_tests` |
| `POST /tests/sale` | `run_tests` |
| `POST /tests/run-all` | `run_tests` |
| `/tests/standby/confirm` | `run_tests` |
| `POST /tests/standby` | `run_tests` |
| `POST /tests/end` | `run_tests` |
| `POST /tests/takeover` | `run_tests` |
| `POST /tests/runs/{run_id}/verdict` | `run_tests` |
| `POST /tests/{subsystem}/{command}` | `run_tests` (re-checks `TESTABLE_COMMANDS`, 403 if not allowlisted) |
| `/users` | `manage_users` |
| `/users/new` | `manage_users` |
| `/users/{id}` | `manage_users` |
| `/devices` | `manage_users` |
| `/users/codes` | `manage_ownership` |
| `/users/ownership` | `manage_ownership` |
| `/settings` | `edit_contacts` **or** `edit_secrets` |
| `/settings/machine` | `edit_contacts` |
| `/settings/contacts` | `edit_contacts` |
| `/settings/payments` | `edit_secrets` |
| `/settings/comms` | `edit_secrets` |
| `/settings/mqtt` | `edit_secrets` |
| `/settings/web` | `edit_secrets` |
| `/settings/reports` | `edit_contacts` |
| `POST /health/faults/PAY-104/record-sale` | `clear_faults` (two-tap confirm) |
| `POST /health/faults/PAY-104/discard` | `clear_faults` (two-tap confirm) |

`/devices` sits under Users in this **navigation** tree (`LEVEL_DEVICES`'s `parent` is `LEVEL_USERS` in `web_interface/levels.py`) even though its URL is not under `/users/` — the tree is a navigation hierarchy, not a URL-prefix hierarchy. Every mutating route under these levels additionally requires `Depends(context.require_htmx)`.

`web_interface/context.py` holds shared state (config, VMC, health monitor, access store, availability, inventory manager) and helpers: `template_context(request, level=None, **extra)` injects request, current_user and perms into every template, `require_htmx` guards all POST routes (the CSRF dependency), `tail(file_path, lines=50)` reads log tails, `health_snapshot()` is the single health predicate shared by `/status` and `/pill` (so the hero and pill never disagree), and `LOW_STOCK_THRESHOLD = 3` (a tracked product is "low" at or below this count; not configurable per-product yet).

The dashboard uses cookie-based session auth via `services/access.py`'s `AccessStore`,
persisted in `data/access.json` (mode 0600). Users have four roles (`owner`,
`secretary`, `tech`, `loader`) and a 4–8 digit PIN. First login on a new device
requires a second factor: a 6-digit OTP sent by email (when
`communication.email_gateway` is configured) or an 8-digit emergency code,
which works offline. On first boot, setup mode redirects every route to `/setup`
behind a code that exists only at the machine. The `Backoff` class enforces
exponential back-off per `(kind, subject, client)` tuple plus a per-user budget
for untrusted clients — no hard caps. Every route is gated by `require(Permission)`;
templates receive `perms` and `current_user` via `template_context` so the server
does not render controls it would refuse. POST routes require the `HX-Request`
header (HTMX's own requests set it), which blocks a plain cross-site form post
as a CSRF guard. The only fragment endpoints are `/status` (home hero, 1 s), `/kpi` (home KPIs, 60 s), `/pill` (health indicator, 5 s), and `/health/subsystems/{name}/live` (one board's live window, 2 s); lists and forms load once and refresh on action. All four are exempt from the session idle clock via `is_polling_path` (`web_interface/auth.py`) — the fixed `POLLING_PATHS` set (`/status`, `/kpi`, `/pill`) plus a regex matching the per-board `/live` pattern.

The Home level shows a status strip (hero showing the next scheduled event or the health summary) plus a tile grid of eight tiles: Health, Products, Inventory, Reports, Controls, Tests, Users, Settings. A tile is rendered only when `perms` intersects that tile's permission set; the grid adapts to screen width (4×2 on desktop, 2×4 on tablets, 1 column on phones). Each tile links to its level, and the pill in the top-right taps to `/health/faults`.

Tailwind v3.4.17 and HTMX 1.9.10 are **vendored** in `static/app.css` and `static/htmx.min.js`, so the tablet operator's dashboard works with no internet — that claim covers the dashboard proper; the customer-facing `/screen` page (and `/screen/body`) is the one exception, keeps CDN references, stays byte-identical to `origin/main`, and is out of scope for part 2. Every page extending `base.html` gets global press (`:active`), busy (`.htmx-request`), focus-visible and checked styling from `@layer base` in `tailwind.input.css` with no per-element classes needed, plus a click sound played from `static/feedback.js` (`static/click.wav`, generated by `scripts/make_click_wav.py` — regenerate rather than hand-edit). The Tailwind config sets `future.hoverOnlyWhenSupported` so hover-only styles don't stick after a touch tap. Secrets on Settings pages are masked: a placeholder is rendered for a field that is already set, and posting the unchanged placeholder leaves the stored value alone. The MQTT page shows the effective value and disables the field when an env override is active, and never writes an env value into `config.json`. Machine id, web host and web port are displayed but not editable — the handlers accept and ignore those fields even if a direct POST supplies them.

`partials/confirm_button.html` (Task 5) is the server-rendered two-tap confirm used by Controls, the Health fault Clear and the Users code Regenerate, with **no JavaScript state**. Parameters: `label`, `confirm_label`, `post_url`, `target` (an `#id` selector), `confirm_url`, and optional `confirming` (default false). The first tap `hx-get`s `confirm_url`, which the including route renders in its confirming state; the second tap `hx-post`s `post_url`. Cancel re-`hx-get`s `confirm_url` with `confirming=false` — so **any route backing a confirm button must honour that query parameter**: absent or anything but the literal string `"false"` renders the Confirm/Cancel pair, `"false"` renders the plain first-tap button.

`POST /inventory/{sku}/adjust` (the Inventory restock level, `web_interface/routes/inventory.py`) takes one form field, `delta`, accepting exactly `-10`, `-1`, `1`, `10` (a loader tapping the four adjust buttons); anything else — including a non-integer string — returns **400** with the stored count unchanged. A valid delta **clamps the count at zero** rather than erroring, so tapping `-10` on a count of 3 lands on 0 — deliberately different from the Products placement form (`POST /products/{sku}/placement`), which *rejects* a typed negative count outright and leaves the stored value unchanged.

The Health level is split across six sub-levels rather than one merged page. `/health` itself shows only a four-tile summary (Subsystems, Faults, Availability, Logs) plus the VMC's own build identity from `services/build_info.py` (image env vars set by CI, or `git` when run from a checkout). `/health/subsystems` and `/health/subsystems/{name}` show heartbeats (liveness, uptime) and each subsystem's retained `capabilities/<subsystem>` document (`SubsystemCapabilities`: firmware, contract version, brand/model, hardware_id, ip) — subsystems in `EXPECTED_SUBSYSTEMS` are listed even before they speak. `/health/subsystems/{name}` (subsystem-windows design) renders the identity card as before, then a `#live` wrapper (`health_subsystem.html`) that owns every htmx attribute — `hx-get`, `hx-trigger="load, every 2s"`, `hx-target="this"`, innerHTML swap — around `partials/subsystem_live.html`, which itself carries no `hx-` attribute; `GET /health/subsystems/{name}/live` re-renders just that fragment, shared with the page route through `_window_context` (`web_interface/routes/health.py`) so the first paint and every poll agree. The window renders only the channels the board declares in its retained `capabilities/<name>` document (`ChannelDescriptor.direction` input/output, `driven_by`) — Inputs and Outputs sections, each either a row per declared channel or "Nothing declared by this board." Digital inputs render as circles (`rounded-full`) and digital outputs as squares (`rounded-md`), tinted green (on), red (off), or gray (board not alive, or the channel never reported); analog (temperature/counter) rows are gated the same way and show value, unit, and (for temperature) an OK/Out-of-range word. Dwell time ("on for 12 s" / "off since VMC start") comes from `HealthMonitor.record_signal`'s in-memory transition tracking — "since VMC start" until the first observed change, reset on every VMC restart. A reading is attributed to a board by `HealthMonitor.declaring_subsystem` (matched by channel id, and kind when given) scanning stored capabilities; a reading no board declares is still consumed for control but never shown. An output or input whose `driven_by` names a command the VMC would currently refuse is marked inhibited (`ring-dashed` plus the word "inhibited") via `Availability.command_inhibited` for `dispense`/`water_valve`/`payment/enable`. The view model is built by the pure helper `web_interface/subsystem_window.py`'s `build_window`. Channel ids are unique within a board (the ice maker's binary output is `compressor_run`, derived from `power_on`/`power_off` events, precisely because `compressor` was already its discharge-temperature input), but `declaring_subsystem` still matches by `(id, kind)` rather than id alone so that two different boards declaring the same literal id under different kinds can never steal each other's readings. `/health/faults` lists active faults with a Clear action (gated on `clear_faults`, the two-tap confirm above); `/health/availability` shows the permissive truth table and payment-blocking reasons; `/health/logs` (gated on `view_logs`) shows the last **50** lines of `LOGS/vmc.log` (`context.tail`), not the pre-v2 fragment's 10.

The Tests level (`web_interface/routes/tests_level.py`) lets a tech prove a subsystem works without making a real sale, and the machine cannot sell while they do it. `/tests` shows one card per `EXPECTED_SUBSYSTEMS` entry plus the service-state card (always rendered), but is itself read-only — the GET routes (`/tests`, `/tests/log`, `/tests/sale`, `/tests/{subsystem}`) never call `VMC.begin_maintenance`; only a POST action acquires the lease. `POST /tests/{subsystem}/{command:path}` runs one command test after re-checking `command in testable_commands(subsystem, row["commands"])` — the intersection of `contracts.common.TESTABLE_COMMANDS` (below) with what that subsystem's live capabilities doc advertises — against a **freshly re-read** `_subsystem_summary()`, never trusting which buttons the client was shown; an unlisted command (`refund`, `payment/enable`, `set_interval`) is refused with **403** before the lease is ever touched or the dispatcher ever called. `POST /tests/run-all` runs `ping` then `self_test` against every alive subsystem **in sequence** (one `await` at a time, never `asyncio.gather`, so two subsystems' commands can never interleave on the wire). `POST /tests/sale` runs `VMC.run_test_sale` for a form-submitted SKU; `POST /tests/end` and `POST /tests/takeover` release or transfer the caller's lease; `POST /tests/runs/{run_id}/verdict` records pass/fail plus a note on a `test_run` log row via `EventRecorder.update_metadata`. `TESTABLE_COMMANDS` (`contracts/common.py`) is a **server-side allowlist**, per subsystem, of exactly the standard commands (`ping`, `self_test`, `force_report`) plus that subsystem's actuator commands — it is deliberately narrower than what a subsystem's capabilities document may advertise, so a control command like `refund` or `payment/enable` can never be invoked from the Tests tile even if it shows up in capabilities. The allowlist re-check on POST is the actual security boundary here, not which buttons the template renders — a crafted request to an unlisted command still gets 403 from the server, never a silent 404 that would look like the command doesn't exist. When any test is refused because the machine is busy, `_acquire_lease_or_refusal` (web_interface/routes/tests_level.py:308) remaps the two opportunistic-lease refusals ("machine is mid-sale", "credit is still on the machine") to "machine is busy — take it out of service first", pointing the operator at the `POST /tests/standby` button; the "held by <id>" wording stays unchanged. The Home hero shows "Out of service — maintenance by <name>" (via `context.health_snapshot()`'s `maintenance` field, web_interface/context.py:280) when `SVC-102` is active and a holder exists, without changing `is_healthy`.

### Services (`services/`)

- `payment_gateway_manager.py` - manages Stripe/PayPal/Square gateways, generates QR codes via `qrcode` library
- `config_store.py` - persists config changes (add/update products) back to `config.json`
- `access.py` - session auth with roles, PINs, devices, emergency codes, and back-off; persists to `data/access.json`
- `mailer.py` - sends OTP and setup-code emails via SMTP
- `fsm_control.py` - translates admin commands (restart, reset, shutdown) into actions
- `availability.py` - permissive truth table (ROADMAP §3) split into three
  gates: `safety` rows block payment and sales, `fulfillment` rows block only
  the individual sale, `alert` rows block nothing. Publishes
  `cmd/payment/enable` on change; feeds the health tab and `/screen`. Only the
  seven codes in `contracts.vending_machine.PAYMENT_BLOCKING_FAULTS` can
  inhibit payment. `product_sellable`/`sale_available` take an optional
  `ignore_faults` set that exempts named blocking codes from the
  `no_critical_fault` row for that call only, never from `payment_enabled`;
  `test_sale_sellable` is the one production caller, exempting a maintenance
  test sale from its own lease's `SVC-102` alone — see FSM Core's maintenance
  lease paragraphs above. `command_inhibited(command)` (subsystem-windows
  design) is a pure read of the already-computed rows — no recompute, no
  publish — answering whether the VMC would currently refuse `dispense`,
  `water_valve`, or `payment/enable`; a subsystem window's output/input
  signal is inhibited iff its declared `driven_by` names a command this
  returns true for.
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
- `command_dispatcher.py` - one `CommandDispatcher`, constructed in `main.py`
  and handed to both the VMC (`VMC.set_command_dispatcher`) and the routes
  (`routes.set_command_dispatcher`); it registers `cmd/+/ack` **once** and
  correlates each ack to its command by `request_id`. A timeout retries
  **exactly once, with the same `request_id`** — never a fresh one — because
  every subsystem (`simulators/base.py`) caches its last 32 acked
  `request_id`s and replays the cached ack instead of re-running the
  handler, so a retry with a new id would repeat a real-world side effect
  (a second `dispense` cycle) instead of just asking again. Raises
  `CommandTimeout(subsystem, command)` immediately when the broker is
  disconnected, without waiting out the timeout first.
- `paths.py` - `LOG_DIR`, `LOG_FILE`, `DATA_DIR` shared by main, routes and services
- `auth_policy.py` - PIN policy (`pin_problem`); validates 4–8 digits with no repeats or runs; identifies loopback hosts (`is_loopback`)
- `event_recorder.py` - records heartbeats, refunds, vend failures, and sales to a durable SQLite schema; `sales` and `cash_collections` are never pruned, while `events` keeps a 90-day retention window via `_prune_with` (which touches only the `events` table)
- `reports.py` - five report queries (`by_period`, `by_product`, `by_method`, `collections`, `summary`); window presets (`7d`, `30d`, `90d`, `12m`, `all`) via `resolve_window` (unknown values fall back to `30d`); CSV rendering via `render_csv` (returns **bytes**); and report filename via `report_filename`. All functions are **synchronous** and called from routes through `asyncio.to_thread`; each calls `recorder.flush()` first
- `report_scheduler.py` - a supervised loop with a **bounded sleep of at most 60 s**, re-reading the live `config` each pass so turning the schedule off stops the next send within a minute; **per-period de-duplication** via a `report_sent` event, matching **any** stored event for the period rather than only the most recent; and **at most one catch-up** at startup for the most recent completed period, never a backlog. A failed send waits for the next due occurrence rather than retrying every pass

### Hardware (`hardware/`)

- `mdb_interface.py` - reference/simulation stub; real MDB communication happens on the ESP32 firmware and arrives over MQTT, not this module
- `button_panel.py`, `camera_monitor.py`, `ice_maker.py` - hardware control modules

### Docker

`Dockerfile` builds from `python:3.12-slim`, installs dependencies with
`uv sync --frozen --no-dev` from `pyproject.toml`/`uv.lock`, and runs
`uv run python main.py` (port 26123). `docker-compose.yml` orchestrates the VMC,
the three ESP32 simulators, and a mosquitto broker, all with
`restart: unless-stopped`. There is no `requirements.txt` — `pyproject.toml` is
the single dependency source of truth. Inside compose, config lives at
`data/config.json` (bind-mounted `./data:/app/data`, writable for the `vmc`
service and read-only for the simulators), pointed to via `ICE_COLDER_CONFIG`
in each service's `environment` — not bind-mounted directly as
`config.json`, since that file is gitignored and doesn't exist on a fresh
clone.

CI/CD: `.github/workflows/ci.yml` runs ruff and pytest on every push/PR and,
on `main`, publishes the image to `ghcr.io/conradstorz/ice-colder` (`latest`
and `sha-<commit>`). Compose services reference that image (with `build: .`
kept for local `--build`) and carry the Watchtower enable label, so the
simulation host updates itself; `docker compose pull` then `up -d` forces it.
A `compose-config` CI job runs `docker compose config` against both compose
files with `.env.example` to catch YAML/interpolation errors before `image`
builds.

Both the root `docker-compose.yml` and `docker/docker-compose.prod.yml`
require a `.env` file (`cp .env.example .env`) and run a one-shot
`mosquitto-init` service that writes the broker's password file from it
before `mosquitto` starts; `docker/docker-compose.yml` stays an anonymous
broker for local development only. `mosquitto-init` rewrites that file from
`.env` on every start (`mosquitto_passwd -c`), so accounts added by hand are
discarded on the next `up`; the optional `HA_MQTT_USERNAME`/`HA_MQTT_PASSWORD`
pair adds a second account for Home Assistant and is skipped when the password
is empty. `MQTT_USERNAME`/`MQTT_PASSWORD` from
`.env` are passed into the VMC and simulators and read by
`services.startup_config.apply_env_overrides`, which returns an `EnvOverrides`
(a `model_copy` of `config.mqtt` with env values applied, plus the resolved
trusted-proxies list) without mutating the live `ConfigModel` — so an env-only
`MQTT_PASSWORD` can never be written back to `config.json` by a later
`save_config`. `ICE_COLDER_TRUSTED_PROXIES` is resolved the same way and
applied to the dashboard's login back-off via
`routes.backoff.set_trusted_proxies(...)`, called after
`routes.set_config_object(...)` so the env value wins.

## Fault Codes and Severity

`DATA-101` (sale write failed; held in fallback file) and `DATA-102` (event database was reset after corruption; history before the reset is lost) are both **alert-class** (`Severity.warning`, `Scope.machine`) and are **deliberately absent from `PAYMENT_BLOCKING_FAULTS`**. Neither fault can ever stop the machine taking money. Both are registered in `contracts/vending_machine.py`.

`PAYMENT_BLOCKING_FAULTS` holds **seven** codes: `ICE-402`, `WTR-103`, `WTR-104`, `ENV-102`, `ENV-103`, `PWR-102`, and `SVC-102` (the maintenance lease, above). `SVC-102` is the one member that is not `critical` severity — it is a deliberate operator-held lease rather than a hardware failure, and it clears itself the moment the lease is released; membership in the frozenset, not severity, is what gates payment.

Both contracts bumped their `CONTRACT_VERSION` for this part: `contracts/vending_machine.py` **0.4.0 → 0.5.0** and `contracts/ice_maker_monitor.py` **1.1.0 → 1.2.0**. The vending-machine bump is a minor version absorbing two additive changes: adding `SVC-102` (new `FaultCode` + `FAULT_TABLE` entry + `PAYMENT_BLOCKING_FAULTS` member, six → seven), and the `DATA-101` description wording fix above ("sale journal in use; sales are being written to a fallback file" → "sale write failed; held in fallback file") — a part 3 change that shipped without its own version bump at the time; that deferred bump is absorbed here too.

Both contracts bumped again for the subsystem-windows feature: `contracts/vending_machine.py` **0.6.0 → 0.7.0** and `contracts/ice_maker_monitor.py` **1.3.0 → 1.4.0**, for the additive `ChannelDescriptor.direction`/`driven_by` fields (new optional fields distinguishing a board's sensed inputs from its driven outputs, and naming the command whose refusal inhibits an output).

`contracts/vending_machine.py` bumped once more, **0.7.0 → 0.8.0**, for the dispenser-profiles feature: `CFG-101` (no valid dispenser profile for a slot; `product_unavailable` severity, `Scope.product`) and `CFG-102` (`dispensers.toml` could not be read; `warning` severity, `Scope.machine`) — neither is a member of `PAYMENT_BLOCKING_FAULTS`, so a missing or invalid profile locks only the affected product's sale, never payment machine-wide. `CFG-101` is raised and cleared by `VMC.reconcile_dispenser_profiles()` and re-asserted by `clear_fault` if clearing some other lockout on a still-profile-less product would otherwise leave it unlocked; `CFG-102` tracks whether `dispensers.toml` itself loaded cleanly. Plan 2 (`DispenseCommand`/`DispenseStep`, the new `DispenserOutcome` members, `fault_for_outcome`) first shipped under this same 0.8.0 version on the theory that it was additive, but a Copilot review of PR #32 found it wire-breaking (`dispense`'s params changed shape; `cmd/dispense` was removed), so `contracts/vending_machine.py` bumped again, **0.8.0 → 1.0.0**, a major bump per the module's own breaking-change rule. `CFG-101`/`CFG-102` themselves stay documented at 0.8.0 — only the plan-2 runtime wiring moved to 1.0.0's "Semantics fixed in 1.0.0 (breaking)" section.

## Key Patterns

- **Logging**: Uses `loguru` throughout. `services/logging_setup.py` owns `setup_logging()`, called explicitly at the start of `main()`; importing the module does not change handlers or create files. It configures `LOGS/vmc.log`, an INFO-level console sink, and filtered `transactions.log`, `ice_maker.log`, and `vending.log` files. File logs rotate at midnight with 300-day retention and ZIP compression. State changes are prefixed with `STATE_CHANGE_PREFIX`.
- **Config mutation**: Product changes go through `services/config_store.py` which writes back to `config.json`. The in-memory `ConfigModel` is mutated directly (Pydantic models with mutable fields).
- **Web UI updates**: The dashboard uses HTMX to swap HTML partials from FastAPI endpoints. No SPA framework.
- **Timezone handling**: Report bucketing uses the machine's local timezone; weeks start on **Monday**; a sale exactly on a boundary (e.g. midnight) belongs to the **later** bucket.
- **DST testing**: `zoneinfo.ZoneInfo` is **unusable on a Windows checkout without `tzdata`**, so the DST tests use synthetic `tzinfo` classes. The primary synthetic zones (`_SyntheticDstTz` and `_SyntheticFallBackTz`) are in `tests/test_reports.py` (lines 24, 91) to test report bucketing across DST transitions; `tests/test_report_scheduler.py` mirrors them (lines 72, 102) to verify the scheduler threads its `tz` parameter correctly. Production code uses the OS's real timezone resolver.
- **CSS class extractor limitation**: `tests/test_static_css.py`'s class extractor cannot tell markup inside a Jinja2 comment (`{# ... #}`) from real markup, so a template comment containing a literal `class="..."` will fail the test. This regex-based limitation (`_CLASS_ATTR_RE` at line 25) has no comment awareness — mentioning a Tailwind class name in a comment is safe, but writing an actual `class="..."` attribute in one is not.

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
