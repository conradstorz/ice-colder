# DRY cleanup and codebase reduction — design

**Date:** 2026-11-02
**Source review:** `docs/reviews/2026-11-02-dry-review.md`. It holds every finding with `file:line` and estimates, and
this spec does not repeat them. Line numbers in the review are as of 814c6d8.
**Goal:** remove duplication, dead code and obsolete compatibility paths. Behaviour stays identical, except for the
listed defect fixes. The target is about 6,000 to 7,000 fewer lines.

## Owner decisions (2026-11-02)

Facts stated by the owner:

- No edge VMC instance is in the field whose data must be preserved, because the project is in its early stages.
- The fleet will stay under 1,000 machines, so SQLite is sufficient on the cloud.

Decisions:

| # | Question | Decision |
|---|---|---|
| D1 | Legacy dollar / pre-cents compatibility (edge and cloud) | **Delete all of it.** This includes `pending_refund_request_id`. |
| D2 | Customer-message/QR layer and `PaymentGatewayManager` | **Delete all of it**, including the `qrcode` dependency. Future Stripe or PayPal support will be built as a gateway behind `payment/ports.py`, with its own spec. |
| D3 | Is the layered-workspace reorg finished? | **Yes.** Delete the codemod tooling and update the root CLAUDE.md. |
| D4 | Cloud `Repository` Protocol | **Delete it.** Type the code against `SqliteRepository`. |
| D5 | One-time test guards | **Delete** `TestScreenTemplatesUnchanged`, the deleted-partials guards and `TestRemovedLegacyRoutesAre404`. **Reduce** the contract-version pins to one per contract. |
| D6 | `AccessStore.prune_devices` | **Wire it up.** Call it at device creation and at boot. |
| D7 | Placeholder stubs `ice_cloud/commands.py`, `ice_cloud/shadow.py`, `ice_edge/cctv/` | **Delete them.** The specs keep the intent. |
| D8 | Firmware consolidation | Do the **test side and shims now**. The **core** (`comms_kit`, `command_envelope`, wire builders) goes in a later spec with a bench run on both boards. |
| D9 | Scope | Include the **defect fixes**. **Exclude** the `contract_gen.h` generator, which gets a later spec. |
| D10 | Product-form errors | **Keep 400** for every validation error, and add a global HTMX rule so 400/422 responses swap. Today the 400 errors are never shown, because HTMX 1.9.10 does not swap a 4xx response by default. |
| D11 | Sims port-forwarding adapters | **Keep them.** A `BoundClient` would lose the `Protocol` typing. |
| D12 | Test-only code in production modules | **Move it to test helpers.** |

## Delivery

The work is a series of eight PRs from one branch line, each branched from the merged previous one, in risk order. Every PR:

- passes `uv run pytest`, `uv run lint-imports`, `uv run python scripts/check_package_isolation.py`, and both
  `uv run ruff check .` and `uv run ruff format --check .` (never the global ruff);
- rebuilds and commits `packages/ui/src/ice_ui/static/app.css` when templates change;
- keeps the ratchets green. These are private access, public surface, money types, test isolation and VMC complexity.
  Where a deletion takes a ratchet more than 10% under its budget, the PR lowers the budget too;
- updates every scoped `CLAUDE.md` whose text it makes stale;
- is a pure refactor except for the PR 3 defect fixes. When a refactor PR changes an observable behaviour, the PR description calls it out.

### PR 1 — mechanical, no behaviour change

- Remove the 201 redundant `@pytest.mark.asyncio` markers (`asyncio_mode = "auto"`).
- Delete these:
  - the unused `config_model` aliases (`PhysicalDetails` 292-310 all; `ConfigModel` `location_owner`,
    `service_technicians`, `stripe`, `paypal`, `mdb_devices`, `comm`);
  - the `coming_soon` field (`TileDef`, `tile.html`, its assertions);
  - the cloud `portal.__all__` re-exports and `viewer.VIEWER`;
  - `dispatcher.MAX_ATTEMPTS`;
  - the unused `fp` parameter of `ingest._admit_new`;
  - the duplicate SMTP `get()` calls in `cloud/settings.py`.
- Move test-only production code into `tests/`: `RecordingMailer` (and `SmtpConfig.from_settings` if it then becomes
  test-only), `EventRecorder.dropped_events`, `fault_registry.severity_level`, `auth_policy.is_loopback`/`LOOPBACK_HOSTS`,
  and the cloud repository methods used only by tests (`account_by_email`, `consumed_claim_hashes`,
  `delete_alert_state`, `is_muted`). Seeding helpers that tests need, such as `grant_tech`, `put_alert_state` and
  `record_alert_events`, move into `tests/cloud/` helpers.
  Before removing each one, grep `packages/`, `tests/` and `scripts/` again.

### PR 2 — approved deletions (D1–D5, D7)

- **D3:** delete `scripts/refactor/`, `tests/meta/test_codemod.py` and `libcst` (then `uv lock`). Also update the root
  CLAUDE.md: drop "migration in progress", the `[PR 5]`/`[stub PR 4]` tags, and the `refactor/` and `cctv/` layout entries.
- **D2:** delete:
  - `payment/payment_gateway_manager.py`, `VMC.initiate_virtual_payment` and `VMC.send_customer_message`;
  - `StatusOutputs.update_callback`/`message_callback`/`qrcode_callback` and their setters, plus `message()`, `refresh()` and `show_qr()` and every call to them;
  - the `message=` argument through `SaleJournal`, `StateConsumer`, `EventIntake` and `MdbGateway`, and the `SaleJournal._message` calls;
  - `qrcode` from `packages/edge/pyproject.toml`.

  `StatusOutputs.display()` and the journal's log lines stay; they are the log contract. Lower the
  `tests/meta/test_vmc_complexity.py` budgets, and update `packages/edge/CLAUDE.md` and `sale/CLAUDE.md`.
- **D1 edge:** delete `session_store._convert_legacy_dollars` and the units-marker branch,
  `event_recorder._journal_row_cents` (legacy rows), and `SessionSnapshot.pending_refund_request_id` with its readers
  in `session_recovery.py` and `app/machine.py`. The `"units": "cents"` marker is still written but no longer read.
  Events.db migration 2 stays in `MIGRATIONS`, because the list is append-only and a fresh db runs it as a no-op.
- **D1 cloud:** delete `contracts/cloud/report.py` `_legacy_dollars`/`_to_float_cents`, the `_from_dollars` validators
  and the 0.x versions in the accepted-version `Literal`. Also delete `portal._with_legacy`/`_legacy_status`/`_legacy_faults`,
  `ui_registry.uses_legacy_money`/`_LEGACY_MONEY_RE`, and `ResolvedUi.legacy_money`. The 1.x schema is unchanged, so
  the contract version is not bumped. The cloud simply stops accepting 0.x reports. Regenerate `docs/contracts/**` and record the
  dropped 0.x acceptance in `docs/architecture-history.md`.
- **D4:** delete the `Repository` Protocol from `store/repository.py` and keep its row dataclasses. Type `alerts/dispatcher.py`, `sweep.py` and `handled.py` against `SqliteRepository`, and move the docstrings across.
- **D7:** delete `ice_cloud/commands.py`, `ice_cloud/shadow.py` and `ice_edge/cctv/`.
- **D5:** delete `TestScreenTemplatesUnchanged` with `SCREEN_TEMPLATES` (`tests/paths.py`, `tests/meta/test_paths.py`),
  the deleted/survivor-partials tests (keep `test_every_include_and_extends_target_exists`), and
  `TestRemovedLegacyRoutesAre404`. Keep one contract-version pin per contract and delete the rest. Update the
  `/screen` "byte-identical to origin/main" note in `web/CLAUDE.md`.

### PR 3 — defect fixes (each test-first: a failing test, then the fix)

1. **HTMX error swap (D10).** Add a `htmx:beforeSwap` handler in `ice_ui/static/feedback.js` that sets `shouldSwap`
   for status 400 and 422, and make every `create_product`/`update_catalog`/placement validation error answer 400.
   Add a browser test (opt-in suite, `ICE_COLDER_BROWSER_TESTS=1`) proving an over-long name's error message
   appears. Check the other 4xx-returning HTMX routes still behave as intended, since `HTTPException` JSON bodies would now be
   swapped. Restrict the swap to HTML responses (`content-type: text/html`).
2. **Controls tone.** `controls.html` renders each command through `partials/confirm_command.html` and loses the dead
   `{% if command %}` branch. Restart and Shutdown are red on first render.
3. **Last-collection tie-break.** Add one `_last_collection_ts(conn)` ordered by `ts DESC, id DESC`, used by
   `cash_since_last_collection`, `summary` and `event_recorder`.
4. **Fleet N+1.** `portal.fleet` calls `alert_counts_since` once. Delete `alert_count_since`.
5. **Device pruning (D6).** `AccessStore` calls `prune_devices()` when it creates a device and once at load or boot.
   A test proves that a stale, never-trusted device is gone after the next creation.

The missing fsync in `inventory_manager` is fixed by PR 7's `atomic_write`, which carries the test proving it.

### PR 4 — edge web Python helpers

Add `web_auth.require_any(*perms)`, which replaces both `_require_any` copies; update the gate-table text in `web/CLAUDE.md`. Also add:

- `context.confirming_flag` and `render_confirm`; merge the two lease handlers;
- `hx_redirect(url)`;
- `web_auth.start_session(...)` and `web_auth.perms_of(request)`;
- `context.product_or_404`;
- `settings/common.py` with `save_or_rerender` and `split_lines`. Keep the `save_config failed` log text;
- the `catalog.py` `fail()` closure, `_render_catalog_form` and `_render_placement`;
- the tests_level `_refusal` and `_require_lease`;
- the `reports/compute.py` `_period_rows` helper.

Two more changes:

- `notifier._send_email` calls `mailer.send_email`.
- Export the water-valve and power-cycle ranges from `ice_contracts.board.common` and use them in `tests_level/commands.py`.

### PR 5 — templates

Add these to `ice_ui/templates/partials/`:

- `form_macros.html` (`field`, `textarea`, `select`, `form_actions`, `form_error`), applied to the 12 form templates;
- `report_range_tabs.html`, `report_period_table.html`, `report_email_form.html`;
- `product_fields.html`, which also gets the `MAX_SKU_LENGTH`/`MAX_PRODUCT_NAME_LENGTH` limits through `template_context`.

Add a `tiles.tile()` builder and use it at every tile-dict site. On the cloud side, give each confirm action
(suspend, unbind, purge, revoke) one context builder, used by both the page and the fragment, and delete the three
one-line wrapper partials. Ids, `aria-*` attributes and visible text stay unchanged, because the browser tests pin them.

### PR 6 — test consolidation

The isolation rule decides where helpers live. Edge-only helpers go under `tests/edge/`, neutral ones in `tests/fakes.py` or `tests/async_helpers.py`.

- **Gate table.** Move `_MATRIX_ROUTES` to `tests/edge/web/gate_table.py` and extend it to the health, tests, reports and
  controls routes. Delete the per-file `test_get_status_per_role` and gate tests, but first check that each case they cover is in the table. Add one test, parametrized over the table's POST rows, that sends no HX-Request and expects 403.
- **Merges.** Delete the user and device tests in `test_web_routes.py` that duplicate `test_routes_users.py`, and move the rest of
  the user, ownership, codes and device classes into `test_routes_users.py`. Delete `TestHealthTabIdentity`.
- **Fakes.**
  - Extend `tests/fakes.py::FakeMqtt` with handler registration, `connected`, `deliver_ack` and `deliver_dispenser_report`, and replace the roughly 8 copies.
  - Make one `FakeEventRecorder(fail_with=None)`, plus one `FakeFaults` for the `payment/mdb` tests.
  - Move `_wait_until` into `tests/async_helpers.py`.
- **Helpers.**
  - `tests/edge/db_helpers.py`: `insert_event(s)`, `db_rows`, `user_version`.
  - `tests/dom_utils.py`: `_bar`.
  - `tests/edge/reporting/tz.py`: the synthetic DST zones and `_dt`. Update the root CLAUDE.md DST paragraph's line references.
  - A `fault_codes(vmc)` helper, a `health_monitor` fixture, and a `make_cloud(**kw)` fixture.
  - Share `_imported_modules` between the two sims structure tests.
- **Sims conformance.** Parametrize the generic ping, idempotency and force_report tests over every simulator. Keep the
  subclass-specific assertions, such as the power-cycle lockout.
- **Firmware Python fixtures.** Use a `sim_on_thread` context manager and a `wait_until_answering(..., subsystem)`, and share
  the golden-wire scaffolding in `tests/firmware/golden_common.py`.
- **Parametrize** the COM/PAY loss tests and the PAY-104 boot tests in `test_vmc_flows.py`.

### PR 7 — `ice_common` and cloud helpers

- **`ice_common.paths.atomic_write(path, data, *, mode=None, backup=False)`.**
  - Behaviour: temp file in the same directory, optional `O_CREAT` 0600 open, fsync, optional `.bak` copy made before the replace, `os.replace`, `fsync_dir`, and temp cleanup on failure.
  - Replaces the hand-rolled copies in `session_store`, `payment_cursor`, `event_recorder`, `config_store`, `dispensers`, `access`, `inventory_manager`, `uplink/identity`, and the cloud `snapshots`/`ui_registry` where they match. Each call keeps its current byte format and mode. `ice_ui/bundle.py` keeps its local implementation because `ice_ui` may not import `ice_common`.
  - Tests: the helper gets its own tests, including one that a failure leaves the target intact and removes the temp file. The inventory test proves the fsync now happens.
- **`ice_common.paths.config_path()`.** Use it in `startup_config`, `config_store`, `dispensers.main` and `sims/base`. An empty
  string means the default.
- **Other moves.**
  - Make `topic_matches` a module-level function in `ice_common.mqtt_client`, used by the sims.
  - Move `require_htmx` into `ice_ui` and use it in edge and cloud.
  - Merge `MQTTClient.run`'s twin `except` blocks.
  - Make `logging_setup` loop over its three filtered sinks.
- **Contracts.** Add one `_Model` base in `contracts/cloud/` and a `_first_error` helper in `board/common.py`. Inline the single-use constants
  in `generate.py`. Move the contract version-history comments into `docs/architecture-history.md`.
- **Cloud.**
  - Add `ice_cloud/context.py` (badge and page context) and `UiRegistry.installed()`.
  - In `sqlite.py`, add `_one()` and `_wipe_machine_state()`, which reads `DELETE_MEMBERSHIPS_SQL` at call time; also rename `_ALERT_DELETES` and replace the field copy in `_insert_alert_events` with `asdict`.
  - `alerts/engine` emits `AlertEventRow`, through `from_fault`/`bare` helpers. Delete `_MailEvent` and `MailEvent.scope`.
  - Add `alerts/format.py` (`event_row`, `fmt_utc`).
  - Add the alert-kind and online-badge partials.
  - Add a shared machine-cookie setter, short-fingerprint resolver and `age_seconds`.

### PR 8 — edge core helpers, sims, low-risk firmware

- **Edge.**
  - `FaultService.ensure_raised(code, outcome=None)` and `ensure_cleared(code, by="auto")`, used at the nine sites. DATA-104's multi-source logic stays.
  - `supervisor/task_runner.Timer(tasks, label)` with `arm`, `arm_if_idle`, `cancel` and `armed`, replacing the eight hand-rolled timers. The `payout` retry uses `arm_if_idle`.
  - `VMC` uses `ice_common.observers.Subscribers` for its state-change and settled lists.
  - `vend/schema_introspect.py` is shared by `dispensers` and `dispensers_doc`, and the discriminator tags are derived rather than hand-listed.
  - `AccessStore` (de)serialises through `asdict`/`from_dict` with one `_reset()`, plus `_live`/`_cancel` for pending transfers and links.
  - Add a `HealthEdge` helper in `ice_common` for the recorder and inventory failed/ok observers.
  - Small twins: the slot checks, the DATA-103 branch, `CommandDispatcher._wait`, and collapsing `VMC.snapshot`/`_snapshot`.
  - Move the `dispensers` CLI to `vend/__main__.py` so it reuses `load_config`; update `dispensers.example.toml`'s usage line.
- **Sims.**
  - Make the fault tables data-driven: `InjectedFault` with optional callbacks and `set_channel`; delete the log-only handlers. Before deleting them, grep `tests/sims` for assertions on their log text.
  - Make one generic `InjectedFault`/`_fault_def` in `base.py`.
  - Add the ice-maker fault table and `uptime_entity`/`temperature_entity`.
  - Add `_cache_and_publish_ack`, a single sleep seam, and the ambient-reading helper.
- **Firmware (test side and shims only).**
  - MDB `ScriptedPeripheral` and `Sink<N>` in `test/support/fakes.h`.
  - Delete the vending `ack_cache.h`/`outbound_ring.h` shims.
  - Move the boardcore tests into `firmware/common`, adding a native test env there if needed.
  - Add `boardidf::mono_ms`/`mono_us` and a `board_boot` helper, with the MDB flash-guard callback.
  - `pio test -e native` must pass in both `firmware/vending` and `firmware/mdb`, and the goldens stay unchanged.

## Out of scope (later specs)

- **Firmware core consolidation.** `boardidf/comms_kit`, `boardcore/command_envelope`, and the wire builders (`write_caps_header`, `write_ha_entity`, `topic_heartbeat`). Each step needs a flash and bench check of both boards. The MDB `send()`, replay and `on_connect` ordering stay board-local.
- **The `contract_gen.h` generator** and its CI drift check.
- **A customer display / message feature**, if it is ever wanted again.

## Risks

- **PR 2 removes compatibility code.** Any developer's local `data/` from before the cents change will read as corrupt or empty. This is acceptable under D1, and the PR description should say so.
- **PR 3's global swap rule changes what HTMX shows for every 4xx HTML response.** Audit the routes that currently return 4xx to HTMX requests.
- **PR 7's `atomic_write` touches money persistence** (`session.json`, the payment cursor, the sales journal). Preserve the byte format, file modes and `.bak` ordering exactly. The existing per-path tests must pass unchanged.
- **PR 8's `Timer` must keep each site's `done()` semantics.** It must also keep the `ice_policy` cooldown behaviour, which drops the timer handle before clearing.
