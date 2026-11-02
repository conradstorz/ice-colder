# DRY / codebase-size review — 2026-11-02

Read-only review of the tree on branch `docs/claude-md-split`, 814c6d8). Five parallel reviewers
(edge core, edge web + ice_ui, cloud/common/contracts/scripts, sims + firmware, tests); every finding was
verified by reading code and grepping callers. Line numbers are as of 814c6d8. Savings are estimates of net
removable lines. Nothing was edited and no tests were run.

## Size today

| Area | Lines |
|---|---|
| tests/ | 73,281 |
| packages/edge | 25,345 |
| firmware (common + vending + mdb, incl. host tests) | 18,914 |
| packages/cloud | 6,973 |
| packages/ui (Python + templates + JS) | 5,381 |
| packages/sims | 4,557 |
| packages/contracts | 2,859 |
| scripts/ | 2,094 |
| packages/common | 1,502 |
| **Total** | **~141,000** |

## Headline

Realistic reduction is **~6,000–7,000 lines (~5%)**. The code is not badly duplicated; the large
files are mostly broad coverage. The wins cluster in five places:

| # | Item | Lines | Risk |
|---|---|---|---|
| 1 | Delete finished reorg tooling (`scripts/refactor/*`, `tests/meta/test_codemod.py`, `libcst`) | ~1,600 | low (policy) |
| 2 | Test suite: one permission-gate table, shared fakes, drop obsolete guards, strip redundant `@pytest.mark.asyncio` | ~1,500–2,000 | low–med |
| 3 | Edge web/UI: Jinja form macros, report partials, settings/route helpers | ~900–1,000 | low |
| 4 | Edge core: unwired customer-message/QR layer, atomic-write helper, fault/timer/observer helpers | ~650–750 (+~200 tests) | low–med |
| 5 | Firmware + sims: shared `comms_kit`, command envelope, test fakes, data-driven sim faults | ~700–800 | low–high |
| 6 | Cloud/common/contracts: context builder, sqlite helpers, dead aliases, legacy-dollar shims | ~500–700 | low–med |

## Defects found along the way (fix regardless of DRY work)

These are drift bugs that duplication caused:

1. **Restart/Shutdown buttons change colour after Confirm → Cancel.** `ui/templates/controls.html:14-28` re-derives what
   `partials/confirm_command.html:21-26` already does, but omits `tone`. As a result, the first render is not red and the re-render is.
2. **Last-collection lookup tie-break differs.** `reporting/reports.py:565-566` orders `ts DESC, id DESC`; `:605-606`
   orders `ts DESC` only; `ledger/event_recorder.py:1202` is a third copy.
3. **`monitoring/inventory_manager.py:83-90` writes atomically without any fsync.** The other six hand-rolled atomic writes do fsync.
4. **Inconsistent product-form error status.** `web/routes/products/catalog.py` answers 400 for the first three validation errors and 200 for the rest.
5. **N+1 query on the fleet page.** `portal.fleet` (`portal.py:287-291`) calls `alert_count_since` per machine, although
   `alert_counts_since` (`store/sqlite.py:945`) already returns all counts in one query.
6. **`AccessStore.prune_devices` is never called in production** (`access/access.py:499-516`). Unenrolled devices are
   never pruned. This is either a missing feature or dead code.

## Decisions needed from the owner

Some items are blocked on a policy call rather than on engineering:

- **Is the layered-workspace reorg finished?** If yes, delete `scripts/refactor/`, `tests/meta/test_codemod.py`, `libcst`
  and the root CLAUDE.md mention (~1,600 lines). Note that CLAUDE.md still says "migration in progress", and the remaining
  PR 5 stubs are not in `move_map.py`.
- **Is the customer-text/QR feature still planned?** `StatusOutputs.set_message_callback` / `set_update_callback` /
  `set_qrcode_callback` (`sale/outputs.py:131-138`) have no production caller; `PaymentGatewayManager` is a stub with dummy
  Stripe URLs. If the feature is not planned, delete the whole layer and the `qrcode` dependency (~250 source + ~170 test lines).
- **Do any pre-cents files or 0.x edges still exist?** If not, these can be deleted:
  - edge: `session_store._convert_legacy_dollars`, `event_recorder._journal_row_cents`, and the legacy
    `pending_refund_request_id` (high risk: PAY-104 fail-safe);
  - cloud: `contracts/cloud/report.py` `_legacy_dollars` + validators, `portal._with_legacy*`,
    `ui_registry.uses_legacy_money` (~150 lines total).
- **Keep the `Repository` Protocol?** `store/repository.py:123-382` mirrors `SqliteRepository` with one implementation and no fake. It is only worth keeping if a Postgres backend is still planned (~185 lines).
- **Keep the one-time test guards?** These are `TestScreenTemplatesUnchanged` (diffs against `origin/main`), the
  deleted-partials guards, and the contract-version pin tests duplicated ~8×.

---

## 1. Cross-cutting themes

Duplication that crosses package boundaries is fixed by moving code **down** (contracts → common → ui), per the dependency rules.

| Theme | Copies | Fix |
|---|---|---|
| Atomic write (tmp + fsync + replace + fsync_dir) | 7 in edge (`session_store.py:134`, `payment_cursor.py:78`, `event_recorder.py:198`, `config_store.py:62`, `dispensers.py:1045`, `access.py:314`, `inventory_manager.py:83`), plus `uplink/identity.py:169`, `ui/bundle.py:166`, and cloud `snapshots.py:83`, `ui_registry.py:158` | `ice_common.paths.atomic_write(path, data, *, mode=None, backup=False)` next to the existing `fsync_dir`. Preserve the binary/0600/`.bak`-copy variants. |
| Config path from `ICE_COLDER_CONFIG` | `app/startup_config.py:22`, `config/config_store.py:36`, `vend/dispensers.py:1156`, `sims/base.py:914` | `ice_common.paths.config_path()`. The copies differ on empty string, so pick one behaviour. |
| MQTT topic matching | `sims/base.py:295-306` duplicates `ice_common/mqtt_client.py:271-286` | Module-level `topic_matches` in common. |
| `require_htmx` | `edge/web/context.py:194-204` and `cloud/auth.py` | Move to `ice_ui` (already depends on FastAPI). |
| Contract bounds mirrored in UI | `web/routes/tests_level/commands.py:110-116` vs `contracts/board/common.py:72-85` | Export the ranges from contracts. |
| Contract constants hand-mirrored in firmware | `vendcore/version.h`, `mdbcore/version.h`, `channels.h:28-54`, `mdbcore/wire.cpp:8-10`, `tests/firmware/board_client.py:24-34` | Generate `contract_gen.h` from `ice_contracts.generate` and add a drift check in CI. Less about line count, more about removing drift risk. |
| Observer lists | `sale/vmc.py:99-100, 430-467, 519-526` hand-roll what `ice_common.observers.Subscribers` provides | Use `Subscribers`. |
| "Failed edge / recovered edge" health observers | `event_recorder.py:1076-1106`, `inventory_manager.py:72-100`, glued by duck-typing in `app/machine.py:674-697` | Small `HealthEdge` helper in common. |

## 2. Edge core (sale, vend, payment, ledger, faults, hal, app, access, config)

| Finding | Locations | Fix | Lines | Risk |
|---|---|---|---|---|
| Unwired message/refresh/QR layer | `sale/outputs.py:88-90,131-138,257-283`; `vmc.py:424`, 14× `_outputs.refresh()`, `:850-861`; `sale_journal.py` `_message` ×~12; `payment/payment_gateway_manager.py` (141); `message=` args through `SaleJournal`/`StateConsumer`/`EventIntake`/`MdbGateway` | Delete (see Decisions). Keep `outputs.display()`, which is wired to `DisplayController`. Lower `test_vmc_complexity` budgets. | ~250 + ~170 tests | low–med |
| Atomic write ×7 | see §1 | `ice_common.paths.atomic_write` | ~70–100 | med (money persistence) |
| `if not has: raise` / `if has: clear` | `app/machine.py:573-626`, `payment/mdb/state.py:84-95`, `mdb/intake.py:97`, `faults/input_faults.py:67-73`, `fault_service.py:241,256` | `FaultService.ensure_raised()` / `ensure_cleared(by=)` | ~35 | low–med |
| Re-armable timer ×8 | `faults/ice_policy.py:77-86`, `input_faults.py:50-62`, `vend/dispense_cycle.py:204,484`, `mdb/payout.py:164-172`, `mdb/policy.py:207-262` (×3) | Generalise `SessionTimer` into `supervisor/task_runner.Timer` with `arm_if_idle` | ~45 | med |
| Dispenser schema introspection ×2 | `vend/dispensers.py:63-126,250,324-368` vs `vend/dispensers_doc.py:82-188`; hand-typed tag set vs computed one | `vend/schema_introspect.py`; derive tags from union aliases | ~55 | low |
| AccessStore field-by-field (de)serialise | `access/access.py:195-264, 282-312` | `asdict` / `from_dict` + one `_reset()` | ~40 | low–med |
| Pending transfer/link twins | `access/access.py:757-768` vs `:854-865`, `:797` vs `:883`, `:770` vs `:867` | `_live(record)`, `_cancel(attr)` | ~25 | low |
| Test-only production API | `EventRecorder.dropped_events` (`:713`), `fault_registry.severity_level`, `auth_policy.is_loopback`/`LOOPBACK_HOSTS`, `prune_devices` | Delete, or wire up `prune_devices` | ~30 | low |
| Small items | `config_store.py:112-128` vs `:153-168` (slot checks); `startup_recovery.py:55-76` (DATA-103 twice); `hal/command_dispatcher.py:272-278` vs `:386-393`; `VMC.snapshot`/`_snapshot` | Local helpers | ~50 | low |
| `dispensers.py` CLI `main()` | `vend/dispensers.py:1101-1204` re-implements config loading | Move to `vend/__main__.py` and reuse `startup_config.load_config` | ~15 (100 moved) | low |

Not duplication: the `sale_journal` log lines (deliberate log contract), `mqtt_inbound` typed wrappers, `MdbGateway` wiring,
the `_LateDispatcher`/`_GatewayPayments` proxies, the `fault_service.clear_fault` branches, `_quarantine_corrupt_db`, and `task_runner` vs
`ice_common.task_supervisor`.

## 3. Edge web + ice_ui

| Finding | Locations | Fix | Lines | Risk |
|---|---|---|---|---|
| Form markup copy-pasted | 12 templates (`settings_*.html`, `product_form/catalog/placement.html`, `user*.html`); label class ×61, input class ×43, Save/Cancel row ×11, error `<p>` ×10 | `partials/form_macros.html` (`field`, `textarea`, `select`, `form_actions`, `form_error`) | ~250–300 | low–med (browser tests pin ids) |
| Report templates | Range tabs ×4 (`reports_period.html:16-26` etc.); period table byte-identical in `reports_period.html:63-107` and `reports_product_sku.html:65-109`; email form ×5 | `report_range_tabs.html`, `report_period_table.html`, `report_email_form.html` | ~150 | low |
| Settings route scaffolding ×7 | `web/routes/settings/{machine,web,payments,mqtt,comms,contacts,reports}.py`; save-or-rerender block ×14 | `settings/common.py: save_or_rerender()`, `split_lines()`; optional router factory | ~120–160 | low |
| Tile dicts ×20 + dead `coming_soon` | `ui/tiles.py:99-132,166-199`, `settings/overview.py:54-110`, `products/overview.py:68-85`, `users/accounts.py:128-153`; `partials/tile.html:30-45` | `tile()` builder; delete `coming_soon` (never true) | ~130 | low |
| `catalog.py` render repeated 7× | `web/routes/products/catalog.py:90-235`, `:240-275`; `placement.py:37-48` vs `:69-84` | Local `fail()` closure; `_render_catalog_form`, `_render_placement` | ~110 | low |
| Confirm GET handlers ×10 | `health/faults.py:134`, `health/recovery.py:48,258`, `inventory.py:205`, `catalog.py:351`, `tests_level/commands.py:413`, `tests_level/lease.py:123,159` (identical), `users/accounts.py:444`, `users/ownership.py:72`; `controls.py:55` parses differently | `confirming_flag` dependency + `render_confirm()` | ~60 | low |
| Small helpers | `HX-Redirect` response ×24; session+device cookie ×4 (`auth/login.py`, `auth/setup.py`); `perms` derivation ×9; `_find_product` ×3 / `_get_or_404` ×2; tests_level refusal/lease ×~8 | `hx_redirect()`, `start_session()`, `perms_of()`, `context.product_or_404()`, `_require_lease()` | ~150 | low |
| `require_any` copied | `products/helpers.py:18-38` vs `settings/overview.py:18-35` (comment says "copied"); calls private `_unauthenticated` | `web_auth.require_any(*perms)` | ~35 | low |
| Notifier re-implements mailer | `notify/notifier.py:152-176` vs `notify/mailer.py:28-90` | Call `send_email` | ~14 | low–med |
| `controls.html` dead branch + drifted loop | `controls.html:7-28` | Include `confirm_command.html` (also fixes defect 1) | ~18 | low |
| Misc | `reports/compute.py:50-67` vs `:128-161`; product `maxlength` hard-coded vs `MAX_SKU_LENGTH`; `/screen` reimplements `views.health.subsystem_rows` + `/100` dollars | Helpers / `money` filter | ~75 | low–med |

No dead templates or routes were found. Cloud templates are distinct pages that already reuse `ice_ui` partials.

## 4. Cloud, common, contracts, scripts

| Finding | Locations | Fix | Lines | Risk |
|---|---|---|---|---|
| Finished reorg tooling | `scripts/refactor/{codemod,move_map,__init__}.py`, `tests/meta/test_codemod.py`, `libcst` (`pyproject.toml:105`) | Delete (see Decisions) | ~1,600 | low |
| `Repository` Protocol mirror | `store/repository.py:123-382` | Type against `SqliteRepository` (see Decisions) | ~185 | med |
| Legacy dollar compat | `contracts/cloud/report.py:35-54` + validators; `portal.py:216-248`; `ui_registry.py:35-57` | Delete once every edge reports 1.0.0 | ~100 | med |
| `sqlite.py` boilerplate | fetch-one ×8; per-machine wipe ×3 (`:473,525,564`); `_insert_alert_events` field copy (`:868-905`) | `_one()`, `_wipe_machine_state()` (read `DELETE_MEMBERSHIPS_SQL` at call time; a test monkeypatches it), `asdict` | ~60 | low |
| Page-context builders ×4 | `portal.py:106-157`, `admin/common.py:25-43`, `claim.py:51-71`, `auth.py:255-270`; `registry.resolve(installed_digest)` ×4 | `ice_cloud/context.py`; `UiRegistry.installed()` | ~45 | low |
| Confirm-button ctx twice + 3 one-line wrapper partials | `admin/accounts.py:47`, `admin/machines.py:61-97`, `people.py:36` vs the inline copies in templates; `admin_suspend/unbind/people_revoke.html` | One builder per action; delete the wrappers | ~45 | low–med |
| Alert event shapes | `alerts/engine.py:11-67` `AlertEvent` ≈ `AlertEventRow`; online/offline rows hand-built in `ingest.py:208` and `sweep.py:78`; `_MailEvent` / unused `scope`; unused `MAX_ATTEMPTS` | Emit `AlertEventRow`; `from_fault()`/`bare()` | ~45 | low–med |
| Dead config aliases | `common/config_model.py:292-310` (all 5), `:549-580` (`location_owner`, `service_technicians`, `stripe`, `paypal`, `mdb_devices`, `comm`) | Delete | ~40 | low |
| Test-only repo methods | `account_by_email`, `consumed_claim_hashes`, `delete_alert_state`, `is_muted`, `alert_count_since` (also defect 5) | Delete or move to test helpers | ~60 | low |
| Event-row / UTC format ×3; badge colours ×2 | `admin/alerts.py:38-49`, `alerts/pages.py:23,70-80`, `alerts/mail.py:111-117`; `alerts.html:13`/`admin_alerts.html:20`; `fleet.html:16`/`admin_status_badge.html` | `alerts/format.py`; partials | ~25 | low |
| Smaller cloud items | `machine_auth.py:212,216`; `portal.__all__` re-exports / `VIEWER`; machine cookie ×2; short-fingerprint→404 ×3; `settings.py:130-131`; unused `fp` in `ingest._admit_new`; `RecordingMailer` in prod; age calc ×2; `commands.py`/`shadow.py` stubs | Local fixes | ~40–70 | low |
| Contracts/common | `generate.py:74-137` single-use constants; `_Model` base ×4 in `contracts/cloud/`; `_first_error` ×2 in `board/common.py`; contract history comments that belong in `docs/architecture-history.md`; `logging_setup.py:61-90` three sinks; `MQTTClient.run` twin `except` | Loops / helpers / trim | ~80 | low |

## 5. Sims + firmware

| Finding | Locations | Fix | Lines | Risk |
|---|---|---|---|---|
| `comms_task.cpp` near-identical | `firmware/vending/src/comms_task.cpp` vs `firmware/mdb/src/comms_task.cpp`: constants, `RxItem`, inflight set, retry log, heartbeat, retained publish, subscribe, drop log | `boardidf/comms_kit.{h,cpp}`. Do not touch mdb `send()`, replay, or `on_connect` ordering. The bench gate applies. | ~140 | med–high |
| Command decode arena + envelope | `vendcore/command.cpp:16-154` vs `mdbcore/command.cpp:15-320`; `command.h` enums | `boardcore/command_envelope` (+ArduinoJson dep). Keep the 4096 arena / POOL_CAPACITY coupling. | ~100 | med |
| Data-driven sim faults | `sims/vending/faults.py:35-154`, `sims/mdb/faults.py:46-95` (log-only pairs; `base.py` already logs) | Optional callbacks + `set_channel` rows | ~90 | low |
| MDB test fakes | `mdb/test/support/fake_{changer,reader,validator}.h`; `Sink` ×4 in `test_{cashless,changer,validator,master}` | `ScriptedPeripheral` base, `Sink<N>` | ~135 | low |
| Wire JSON builders | caps header (`vendcore/wire.cpp:114` vs `mdbcore/wire.cpp:312`), HA doc (`vendcore/wire.cpp:196` vs `mdb/comms_task.cpp:331`), `"heartbeat/%s"` ×5, test-only `lwt_json` wrappers | `boardcore::write_caps_header`, `write_ha_entity`, `topic_heartbeat`. Goldens guard the bytes. | ~70 | low–med |
| Ice-maker fault table | `sims/ice_maker.py:178-325` | `(fault, sensor, pin, halt_detail)` table | ~60 | low–med |
| Port-forwarding adapters | `sims/mdb/simulator.py:51-124`, `sims/vending/simulator.py:64-95` | `BoundClient`. This loosens typing. | ~50 | med |
| `InjectedFault` / `_fault_def` ×2 | `vending/faults.py:21`, `mdb/faults.py:35`, `vending/simulator.py:98`, `mdb/simulator.py:127` | Generic in `base.py` | ~35 | low |
| Small | conformance fixtures (`tests/firmware/conftest.py:84-129` vs `mdb/conftest.py:34-83`); HA uptime/temperature dicts ×3; boot sequence + `EspClock` ×2; `ack_cache.h`/`outbound_ring.h` shims; base.py ack cache ×2; sleep seam ×3 | Helpers | ~130 | low |

Note: `vending/test/test_ack_cache` and `test_outbound_ring` test `firmware/common` code. They belong with common, which has no tests of its own.

## 6. Tests

The top files are large because coverage is broad: `test_vmc_flows.py` has 147 tests at ~28 lines each. The exception is
`test_web_routes.py` (3,678 lines, 41 classes), which has become a catch-all.

| Finding | Locations | Fix | Lines | Risk |
|---|---|---|---|---|
| Permission gates answered 4+ ways | `test_web_routes.py:430-712` (`_MATRIX_ROUTES`) already covers settings, yet `test_get_status_per_role` is copied ×7 in `test_routes_settings.py`, plus gate tests in `test_routes_{health,reports,reports_levels,products,controls,tests}.py` | Move the table to `tests/edge/web/gate_table.py`, add the missing routes, delete the per-file copies | ~300–400 | low–med |
| "POST without HX-Request → 403" ×~13 | `test_routes_settings.py` ×6, `test_routes_controls.py:158`, `test_routes_users.py`, `test_destructive_confirms.py:197`, `test_web_routes.py:2141`, `test_routes_tests.py:968` | One parametrized test over the table's POSTs | ~80–120 | low |
| `@pytest.mark.asyncio` redundant | 201 occurrences; `asyncio_mode = "auto"` (`pyproject.toml:97`) | sed | ~200 | very low |
| Shared fakes | FakeMqtt ×8 (`test_routes_tests.py:1743` is a verbatim copy of `test_command_dispatcher.py:53`); FakeEventRecorder ×4; FakeFaults ×5 in `payment/mdb`; `_wait_until` ×3 | Extend `tests/fakes.py`; `_wait_until` in `tests/async_helpers.py` | ~220–290 | low |
| User/device tests duplicated | `test_web_routes.py:2374-3222` vs `test_routes_users.py` (≥7 identical pairs) | Delete the copies; move the rest into `test_routes_users.py` | ~130–170 | low |
| Obsolete one-time guards | `TestScreenTemplatesUnchanged` (`test_web_routes.py:3641`), deleted-partials guards (`:3582-3640`), `SCREEN_TEMPLATES` in `tests/paths.py` | Delete (see Decisions) | ~110–150 | low |
| Sim base behaviour re-tested per subclass | `test_simulator_ice_maker.py:602-833`, `test_simulator_vending.py:1078` vs `test_simulator_base.py` | Parametrized conformance over all simulators | ~150–200 | med |
| sqlite boilerplate | `INSERT INTO events` ×35, `sqlite3.connect` ×61 in `test_event_recorder.py`; sales readers ×3 | `tests/edge/db_helpers.py` | ~100–130 | low |
| Small helpers | `_bar` ×5, `_imported_modules` ×2, `_codes_in` ×2, `_dt`/synthetic tz ×2, fault-code set comprehension ×83, `HealthMonitor` setup ×16 | `tests/dom_utils.py`, `tests/edge/reporting/tz.py`, fixtures | ~120–180 | low |
| Cloud app built by hand | `test_ingest.py` ×11, `test_auth.py` ×4, others | `make_cloud(**kw)` fixture | ~60–90 | low |
| Misc | duplicate `TestHealthTabIdentity` (`test_web_routes.py:1075`); contract-version pins ×8; firmware golden tests ×2; parametrize candidates in `test_vmc_flows.py:1664-1760, 1985-2025` | Delete or parametrize | ~180 | low |

Keep these: the five clock fakes (they have different semantics), `conftest.py`/`route_context.py`, the other meta ratchets, and the `test_access.py` and `test_dispensers_validation.py` bulk.
Constraints: shared helpers must respect `tests/meta/test_test_isolation.py`, so edge-only helpers go under `tests/edge/` and neutral ones in `tests/fakes.py` / `tests/async_helpers.py`.

## Housekeeping

`.claude/worktrees/agent-isolated/` is a stale worktree on `cloud/fleet-admin-dashboard` (be61382, merged as PR #102).
It doubles every repo-wide grep result. Remove it with `git worktree remove .claude/worktrees/agent-isolated`.

## Suggested PR sequence

1. **Mechanical, zero-behaviour:** strip `@pytest.mark.asyncio`, remove the stale worktree, delete dead config aliases,
   `coming_soon`, test-only repo methods, and `portal.__all__` re-exports.
2. **Decision-gated deletions:** reorg tooling, the message/QR layer, obsolete test guards.
3. **Drift-bug fixes:** defects 1–5 above, each with a test.
4. **Edge web Python helpers:** `require_any`, `confirming_flag`, `hx_redirect`, `save_or_rerender`, `catalog.py` closures.
5. **Template macros:** form macros and report partials, with an `app.css` rebuild check.
6. **Test consolidation:** gate table, shared fakes, users-test merge.
7. **Common helpers:** `atomic_write`, `config_path`, `topic_matches`, `require_htmx`. These touch money persistence, so review carefully.
8. **Edge core helpers:** `ensure_raised`/`ensure_cleared`, `Timer`, `Subscribers` in VMC, schema introspection.
9. **Firmware:** test fakes first (low risk), then `command_envelope`, then `comms_kit` (bench gate).
10. **Legacy-dollar / pre-cents removal:** last, after confirming the fleet.
