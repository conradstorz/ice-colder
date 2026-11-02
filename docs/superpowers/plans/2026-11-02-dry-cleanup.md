# DRY Cleanup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Carry out `docs/superpowers/specs/2026-11-02-dry-cleanup-design.md`. It removes duplication, dead code and obsolete compatibility paths in eight phased PRs, and fixes the listed drift defects.

**Architecture:** There are eight phases, one per PR, executed in order. Each phase is a branch stacked on the previous one: `cleanup/p1-mechanical` and onward. Tasks are small, and each leaves the suite green. The finding details, with `file:line` as of 814c6d8, are in `docs/reviews/2026-11-02-dry-review.md`. **Re-locate every line number by grep before editing**, because earlier tasks shift them.

**Tech Stack:** Python 3.12 uv workspace, FastAPI + Jinja2 + HTMX 1.9.10, pytest (`asyncio_mode = "auto"`), SQLite, ESP-IDF/PlatformIO C++ (native host tests).

## Plan style

This plan follows the owner's preference: it specifies **interfaces, behaviour and tests, not code bodies**. Implementers read the cited code and write the change themselves. A code block appears only for a non-obvious contract.

## Global Constraints

- **Commands.** Never chain shell commands with `&&`; use separate calls. Use `uv run ...` for everything. Never use the global `ruff`; always use `uv run ruff`.
- **Done check for a task.** Run the targeted tests named in the task, then `uv run ruff check .` and `uv run ruff format --check .`.
- **Done check for a phase.** Also run:
  - `uv run lint-imports`;
  - `uv run python scripts/check_package_isolation.py`;
  - the full `uv run pytest -q`. If the local run is killed for memory, use the hpz440 Linux harness (`reference_linux_test_harness` memory). Its only known harness failures are `test_build_info::test_git_fallback` and `test_build_info::test_git_dirty_suffix`; `TestScreenTemplatesUnchanged` is deleted in phase 2.
- **Package rules.** `ice_contracts → ice_common → ice_ui → (ice_edge | ice_cloud | ice_sims)`. `ice_ui` never imports `ice_common`, and `ice_sims` never imports `ice_ui`. Test isolation: sims tests never import `ice_edge`, and edge tests never import `ice_sims`. Neutral test helpers go in `tests/fakes.py`, `tests/async_helpers.py` or `tests/repo_root.py`.
- **Ratchets.** These must stay green: `tests/meta/test_no_private_access.py`, the public-surface test, `test_money_types.py`, `test_test_isolation.py` and `test_vmc_complexity.py`. When a deletion takes a budget more than 10% under its limit, lower the budget in the same commit.
- **Mutation rule.** Tests and routes mutate only through `VMC`/`Machine` methods, never through underscore attributes.
- **Money.** Money is integer cents everywhere, and every field name ends in `_cents`.
- **Templates.** Any template change requires a Tailwind rebuild, and the rebuilt `app.css` is committed:
  `.tailwind/tailwindcss.exe -c packages/ui/tailwind.config.js -i packages/ui/tailwind.input.css -o packages/ui/src/ice_ui/static/app.css --minify`
  Never write `class="..."` inside a Jinja comment.
- **Docs.** Every scoped `CLAUDE.md` that a task makes stale is updated in that task.
- **Commits.** End every commit message with `Assisted-by: <model> (Claude Code)`, naming the model that actually wrote it, and no `Co-Authored-By`. Use conventional prefixes: `refactor:`, `chore:`, `fix:`, `test:`, `docs:`.
- **Behaviour.** The only intended behaviour changes are the phase 3 fixes and the D1/D2 deletions. Anything else observable that changes must be stated in the commit message.
- **Model tiers.** **[H]** means a Haiku implementer for mechanical work, **[S]** a Sonnet implementer, and Sonnet does all reviews.

---

## Phase 1 — mechanical (branch `cleanup/p1-mechanical` from `cleanup/dry-design`)

### Task 1.1 [H]: Strip redundant asyncio markers
**Files:** every `tests/**/*.py` containing `@pytest.mark.asyncio`, about 201 lines in about 15 files.
- [ ] Delete each `@pytest.mark.asyncio` decorator line. Where `pytestmark = pytest.mark.asyncio` exists, delete it too. Remove `import pytest` only if the file no longer uses it; ruff F401 will report that.
- [ ] Verify: `grep -rn "mark.asyncio" tests` is empty, then run `uv run pytest tests/sims tests/common tests/edge/monitoring tests/edge/ledger tests/edge/customer_ui -q`. The pass count must be unchanged from before the edit; record both counts.
- [ ] Commit `chore(tests): drop redundant asyncio markers (asyncio_mode=auto)`.

### Task 1.2 [H]: Delete dead config aliases and `coming_soon`
**Files:**
- `packages/common/src/ice_common/config_model.py`:
  - `PhysicalDetails` alias properties `machine_id`, `machine_location`, `machine_owner`, `location_owner`, `service_technicians`;
  - `ConfigModel` alias properties `location_owner`, `service_technicians`, `stripe`, `paypal`, `mdb_devices`, `comm`.

  Keep `ConfigModel.machine_owner`, `products`, `get_preferred_gateway_for` and the `machine_id` field.
- `packages/ui/src/ice_ui/tiles.py` (`TileDef.coming_soon`, every `"coming_soon": False` key), `packages/ui/src/ice_ui/templates/partials/tile.html` (the four `coming_soon` branches and its doc comment), and the Python tile dicts in `packages/edge/src/ice_edge/web/routes/{settings,products}/overview.py` and `users/accounts.py`.
- Tests: `tests/ui/test_health_logic.py` (the `coming_soon` assertion), and `tests/edge/web/test_web_routes.py::test_no_tile_is_coming_soon` (delete it).
- [ ] Before deleting each alias, grep `packages/ tests/ scripts/` for `.<name>` reached through a non-`payment.`/`people.` path. Keep any alias that has a real caller.
- [ ] Update the `packages/edge/CLAUDE.md` sentence that cites `config.stripe` as an example convenience property.
- [ ] Rebuild `app.css`, then run `uv run pytest tests/ui tests/common tests/edge/web -q`.
- [ ] Commit `refactor: drop unused config aliases and the never-true coming_soon flag`.

### Task 1.3 [H]: Cloud dead bits
**Files:** `packages/cloud/src/ice_cloud/portal.py` (`__all__` re-exports of `STALE_FACTOR`/`VIEWER`, the `VIEWER` import), `viewer.py` (`VIEWER = RemoteViewer()` if it is unused afterwards), `alerts/dispatcher.py` (`MAX_ATTEMPTS`), `ingest.py` (the unused `fp` parameter of `_admit_new` and its call sites), and `settings.py` (the second `get("ICE_CLOUD_SMTP_USER")`/`get("ICE_CLOUD_SMTP_PASSWORD")` calls, which reuse the earlier bindings).
- [ ] Grep `tests/` for each name first.
- [ ] Run `uv run pytest tests/cloud -q`.
- [ ] Commit `refactor(cloud): remove unused re-exports, constant and parameter`.

### Task 1.4 [S]: Move test-only production code into tests
Move each item below out of production code. Its tests move with it, or are deleted when they only tested the removed code.
- `ice_cloud/alerts/mail.py` `RecordingMailer` moves to `tests/cloud/alerts/helpers.py` or a cloud conftest. If `SmtpConfig.from_settings` then has no production caller, delete it and update `tests/cloud/alerts/test_mail.py`, which builds its settings with `SimpleNamespace`.
- `ice_edge/ledger/event_recorder.py` `dropped_events`: if only tests read it, the tests read the counter through a public seam that already exists. Otherwise keep it and record why in the commit. Do not add a new private access.
- `ice_edge/faults/fault_registry.py` `severity_level` and `ice_edge/access/auth_policy.py` `is_loopback`/`LOOPBACK_HOSTS`: delete them and their tests.
- Cloud `SqliteRepository.account_by_email`, `consumed_claim_hashes`, `delete_alert_state` and `is_muted`, plus their `Repository` protocol entries. The protocol itself goes in task 2.4. Tests that asserted through them switch to a direct SQL read helper in `tests/cloud/conftest.py`.
- `grant_tech`, `put_alert_state` and `record_alert_events` stay in production for now, because production code may use them. Grep for callers; move only the ones nothing in `packages/` calls.
- [ ] Run `uv run pytest tests/cloud tests/edge/ledger tests/edge/faults tests/edge/access -q`, then `uv run pytest tests/meta -q`.
- [ ] Commit `refactor: move test-only helpers out of production modules`.

**Phase 1 gate:** run the full phase checks. Then a Sonnet reviewer reads the phase diff against the spec's PR 1 section.

---

## Phase 2 — approved deletions (branch `cleanup/p2-deletions` from p1)

### Task 2.1 [H]: Delete the reorg tooling (D3)
**Files:**
- Delete: `scripts/refactor/` (whole directory) and `tests/meta/test_codemod.py`.
- Modify: `pyproject.toml` (remove the `libcst` dev dependency), then run `uv lock`.
- Modify: root `CLAUDE.md`:
  - remove "**Migration in progress:**" and recast the sentence as the current layout;
  - remove every `[PR 5]`, `[stub PR 4, code PR 5]` and `[PR 4]` tag;
  - remove `refactor/{move_map.py,codemod.py}` from the scripts line;
  - remove the `cctv/` layout line (this anticipates task 2.5).
- [ ] Grep the repo for `libcst` and `scripts.refactor`, excluding `docs/superpowers/plans`; both must be empty.
- [ ] Run `uv sync`, then `uv run pytest tests/meta -q`.
- [ ] Commit `chore: remove finished reorg codemod tooling`.

### Task 2.2 [S]: Delete the customer-message/QR layer (D2)
**Files:**
- Delete: `packages/edge/src/ice_edge/payment/payment_gateway_manager.py`.
- `sale/outputs.py`: delete `update_callback`, `message_callback`, `qrcode_callback`, their `set_*` setters, and `message()`, `refresh()` and `show_qr()`. Keep `display()`.
- `sale/vmc.py`: delete `payment_gateway_manager`, `initiate_virtual_payment`, `send_customer_message`, and every `self._outputs.refresh()` call. In `select_product`, the re-selection branch that called `initiate_virtual_payment` keeps its other behaviour and simply drops the call.
- `sale/sale_journal.py`: delete every `self._message(...)` and the `message` constructor argument. **Keep every logger line verbatim**, because they are a log contract.
- `payment/mdb/` (`__init__.py`, `intake.py`, `state.py`) and `app/machine.py`: drop the `message=` plumbing.
- `packages/edge/pyproject.toml`: remove `qrcode`, then run `uv lock`.
- Tests:
  - Delete the callback tests in `tests/edge/sale/test_outputs.py`, and the `next_payment_prompt` and `set_*_callback` uses in `test_vmc_fsm.py`, `test_payment_recheck.py` and `test_vmc_dispense_profiles.py`.
  - Tests that used a message callback as an observation point must assert through the journal's log output or `display()` instead. Never delete a test whose real subject is a different behaviour; retarget it.
- `tests/meta/test_vmc_complexity.py`: lower the budgets to the new measurement.
- Docs: remove the `payment_gateway_manager.py` bullet from `packages/edge/CLAUDE.md`; in `sale/CLAUDE.md`, remove the `StatusOutputs` callback text and the customer-message text.
- [ ] Before deleting anything, grep for `message_callback`, `show_qr`, `refresh(`, `send_customer_message`, `payment_gateway_manager` and `qrcode` across `packages/` and `tests/`. Afterwards, grep again; only `display` usages may remain.
- [ ] Run `uv run pytest tests/edge -q` and `uv run pytest tests/meta -q`.
- [ ] Commit `refactor(edge): remove unwired customer-message/QR layer and PaymentGatewayManager stub`.

### Task 2.3 [S]: Delete the legacy money compatibility code (D1)
**Edge files:**
- `ledger/session_store.py`: delete `_convert_legacy_dollars` and the units-marker read branch. Keep writing `"units": "cents"`.
- `ledger/event_recorder.py`: delete `_journal_row_cents`'s legacy path. Migration 2 stays in `MIGRATIONS`, because the list is append-only.
- `ledger/session_store.py` `SessionSnapshot.pending_refund_request_id`, and its readers in `ledger/session_recovery.py` and `app/machine.py`.
- `ice_common/config_model.py`: delete the legacy dollar `price` upgrade on load, if present. Grep `price_cents` near `model_validator`.

**Cloud and contracts files:**
- `contracts/cloud/report.py`: delete `_legacy_dollars`, `_to_float_cents` and the `_from_dollars` validators; remove the 0.x entries from the accepted-version `Literal`.
- `cloud/portal.py`: delete `_with_legacy`, `_legacy_status` and `_legacy_faults`, and their uses.
- `cloud/ui_registry.py`: delete `uses_legacy_money`, `_LEGACY_MONEY_RE`, `ResolvedUi.legacy_money` and `_legacy_money`.

**Then:**
- [ ] Regenerate the schemas with `uv run python -m ice_contracts.generate`, and commit the changed `docs/contracts/**`.
- [ ] Add a line to `docs/architecture-history.md` saying the cloud stopped accepting 0.x reports and the edge dropped its pre-cents readers on 2026-11-02. Update the root CLAUDE.md "Money" section, removing the "legacy dollars converted" and `price` upgrade sentences, and update `packages/cloud/CLAUDE.md` and `ledger/CLAUDE.md`.
- [ ] Tests: delete the legacy-conversion tests (`tests/edge/ledger/test_migration_cents.py` legacy cases, the session_store legacy tests, the cloud legacy-report tests). Keep **one** test proving that a 0.x report is now rejected with 4xx by the ingest endpoint.
- [ ] Run `uv run pytest tests/edge/ledger tests/edge/app tests/cloud tests/contracts tests/common -q`, then `uv run pytest tests/meta -q`, which includes `test_money_types`.
- [ ] Commit `refactor: drop pre-cents and 0.x legacy money compatibility`.

### Task 2.4 [S]: Delete the `Repository` Protocol (D4)
**Files:**
- `packages/cloud/src/ice_cloud/store/repository.py`: delete the `Repository` Protocol class and keep the row dataclasses. Move any method docstrings that carry meaning onto the matching `SqliteRepository` methods in `store/sqlite.py`.
- `alerts/dispatcher.py`, `alerts/sweep.py` and `alerts/handled.py`: annotate with `SqliteRepository`.
- `packages/cloud/CLAUDE.md`: fix any mention of the protocol.
- [ ] Grep for `Repository` imports.
- [ ] Run `uv run pytest tests/cloud -q`.
- [ ] Commit `refactor(cloud): drop single-implementation Repository protocol`.

### Task 2.5 [H]: Delete placeholder stubs (D7) and one-time test guards (D5)
**Files:**
- Delete: `packages/cloud/src/ice_cloud/commands.py`, `packages/cloud/src/ice_cloud/shadow.py` and `packages/edge/src/ice_edge/cctv/`. Remove them from the root CLAUDE.md layout (the cloud line lists `commands` and `shadow`). If import-linter contracts in `pyproject.toml` name `ice_edge.cctv`, remove it there too.
- `tests/edge/web/test_web_routes.py`: delete `TestScreenTemplatesUnchanged`, `TestRemovedLegacyRoutesAre404`, and the deleted-partials and survivor-partials tests in `TestTemplateIncludeIntegrity`. Keep `test_every_include_and_extends_target_exists`.
- `tests/paths.py` `SCREEN_TEMPLATES`, and its loop in `tests/meta/test_paths.py`.
- Contract-version pins: in `tests/contracts/test_contracts.py`, `test_contracts_vending.py`, `test_payment_contract.py` and `test_cloud_contract.py`, keep exactly one test per contract that pins its version string, and delete the other pin tests. The firmware `test_capabilities.py` pin stays, because it checks a different artefact.
- `packages/edge/src/ice_edge/web/CLAUDE.md`: drop the `/screen` "stays byte-identical to `origin/main`" clause.
- [ ] Run `uv run pytest tests/edge/web tests/contracts tests/meta -q`.
- [ ] Commit `chore: remove placeholder stubs and one-time migration guards`.

**Phase 2 gate:** run the full phase checks, then have the phase reviewed.

---

## Phase 3 — defect fixes (branch `cleanup/p3-fixes` from p2). Test-first for each.

### Task 3.1 [S]: HTMX swaps 400/422 HTML errors (D10)
**Interfaces:**
- `packages/ui/src/ice_ui/static/feedback.js` gets a `htmx:beforeSwap` listener. When `evt.detail.xhr.status` is 400 or 422 **and** the response `Content-Type` starts with `text/html`, it sets `evt.detail.shouldSwap = true` and `evt.detail.isError = false`. It changes nothing else.
- `web/routes/products/catalog.py`: every validation error in `create_product`, `update_catalog` and `products/placement.py` answers status 400. That includes the negative-inventory, slot and duplicate errors that answer 200 today.

**Tests:**
- Start with a failing route test asserting a 400 for the negative-inventory error.
- Add a browser test, `tests/edge/web/test_product_form_errors_browser.py`, built like the other opt-in browser tests (see `tests/CLAUDE.md`). It submits an over-long name and asserts that the error `<p role="alert">` becomes visible. Add the file to the root CLAUDE.md browser-test command.

**Audit:**
- [ ] Grep the HTMX-posted routes that return a 4xx HTML `TemplateResponse`. List them in the commit body and confirm that swapping them is the right behaviour.
- [ ] `HTTPException` returns JSON, so the content-type guard excludes it.

**Steps:**
- [ ] Rebuild `app.css` if needed.
- [ ] Run `uv run pytest tests/edge/web -q`, and the browser test itself with `ICE_COLDER_BROWSER_TESTS=1`.
- [ ] Commit `fix(web): show 400 form errors — htmx did not swap 4xx responses`.

### Task 3.2 [H]: Controls renders through `confirm_command.html`
**Files:** `packages/ui/src/ice_ui/templates/controls.html`.
- Delete the `{% if command %}` branch.
- Render each command with `{% set command = cmd %}{% set confirming = false %}{% include "partials/confirm_command.html" %}`, or the equivalent `with` form.

**Test (first, failing):** in `tests/edge/web/test_routes_controls.py`, the first GET of `/controls` renders the Restart and Shutdown buttons with the same tone classes the confirm endpoint uses for them.
- [ ] Rebuild `app.css`, then run `uv run pytest tests/edge/web/test_routes_controls.py tests/ui -q`.
- [ ] Commit `fix(web): controls first render uses confirm_command tone`.

### Task 3.3 [S]: One last-collection lookup
**Interface:** `reporting/reports.py` gets `_last_collection_ts(conn: sqlite3.Connection) -> str | None`, which does `SELECT ts FROM cash_collections ORDER BY ts DESC, id DESC LIMIT 1`. Use it in `cash_since_last_collection` and `summary`. `ledger/event_recorder.py` (around 1202) uses the same query. It can import from `reporting` only if the layer rules allow that; otherwise it keeps an identical query with the `id DESC` tie-break, and a comment points to `reports._last_collection_ts`. Check with `uv run lint-imports`.

**Test (first):** two collections with the same `ts` must make `summary` and `cash_since_last_collection` agree on the higher id.
- [ ] Run `uv run pytest tests/edge/reporting tests/edge/ledger -q`.
- [ ] Commit `fix(reports): deterministic last-collection tie-break`.

### Task 3.4 [H]: Fleet page N+1
**Files:** `cloud/portal.py` `fleet`, and `store/sqlite.py`.
- Call `alert_counts_since(since)` once before the loop and index the result by fingerprint.
- Delete `alert_count_since`.

**Test:** the existing fleet tests keep passing. Add one test with two machines that have different counts.
- [ ] Run `uv run pytest tests/cloud -q`.
- [ ] Commit `fix(cloud): fleet page reads alert counts in one query`.

### Task 3.5 [S]: Wire `prune_devices` (D6)
**Interface:** `AccessStore` calls `self.prune_devices()`:
- at the start of whichever method creates a new `Device` (find it by grepping `Device(`);
- once at the end of a successful `load()`.

`prune_devices` keeps its contract: it removes untrusted devices more than `DEVICE_PRUNE_HOURS` old.

**Tests (first):**
- With the clock seam, a stale untrusted device is gone after the next device creation.
- A trusted device of the same age survives.
- A stale untrusted device present in `access.json` at load is gone after load.
- [ ] Run `uv run pytest tests/edge/access tests/edge/web -q`.
- [ ] Commit `fix(access): prune abandoned device enrolments`.

**Phase 3 gate:** run the full phase checks, then have the phase reviewed.

---

## Phase 4 — edge web Python helpers (branch `cleanup/p4-web` from p3)

### Task 4.1 [S]: Auth and context helpers
**Interfaces:**
- `ice_edge/web/auth.py`:
  - `require_any(*perms: Permission)` is a FastAPI dependency with OR semantics. It mirrors `require()`, sending unauthenticated users to the login redirect and refusing authenticated ones without any of the perms with 403. It replaces `_require_any` in `routes/products/helpers.py` and `routes/settings/overview.py`.
  - `perms_of(request) -> frozenset[Permission]`.
  - `start_session(response, request, session_id: str, device_token: str | None = None) -> None` sets the session cookie and, when given, the device cookie, using the attributes the call sites use today.
- `ice_edge/web/context.py`:
  - `confirming_flag(confirming: str | None = Query(None)) -> bool`, true unless the value is the literal `"false"`;
  - `render_confirm(request, template: str, **ctx) -> TemplateResponse`;
  - `hx_redirect(url: str) -> HTMLResponse`;
  - `product_or_404(sku: str) -> Product`, plus `find_product(sku) -> Product | None`.

**Call sites to convert:**
- the ten confirm GET handlers (`controls.py`'s different parse changes to `confirming_flag`);
- the 24 `HX-Redirect` sites;
- the four cookie trios in `auth/login.py` and `auth/setup.py`;
- the nine `perms` derivations;
- the three `_find_product` and two `_get_or_404` copies.

Merge the two identical `tests_level/lease.py` handlers into one handler with both paths. `web/CLAUDE.md`'s gate-table preamble names `require_any` and drops the "local OR-semantics dependency defined in two places" text.

**Tests:** unit tests for `require_any` (anonymous → login redirect, no perm → 403, either perm → 200) and for `confirming_flag`. The existing route suites are the regression net.
- [ ] Run `uv run pytest tests/edge/web -q`, then `uv run pytest tests/meta -q`.
- [ ] Commit `refactor(web): shared auth/confirm/redirect/product helpers`.

### Task 4.2 [S]: Settings scaffolding
**Interface:** create `ice_edge/web/routes/settings/common.py` as a helper module, and add it to `HELPER_MODULES` in `tests/meta/test_routes_shape.py`. It provides:
- `save_or_rerender(area: str, render: Callable[[str], Response]) -> Response`, which calls `config_store.save_config(context.config)`. On an exception it logs `f"settings/{area}: save_config failed: {e}"` and returns `render(f"Could not save changes: {e}")`; on success it returns `hx_redirect(f"/settings/{area}")`.
- `split_lines(raw: str) -> list[str]` splits on newlines and commas, strips, and drops blanks.

Convert all 14 sites. `reports.py::_parse_extra_recipients` and the `web.py` splitter use `split_lines`.

**Tests:** unit tests for `split_lines`. One test that a `save_config` failure renders the error and logs it already exists in the settings tests; keep it green.
- [ ] Run `uv run pytest tests/edge/web -q`.
- [ ] Commit `refactor(web): settings save/rerender helper`.

### Task 4.3 [S]: Catalog, tests_level, compute and notifier
- `products/catalog.py` `create_product` gets a local `fail(error: str, status: int = 400)` closure. Add `_render_catalog_form(request, product, *, price_text, error, button_value, status_code)`. `placement.py` GET and `_rerender` share `_render_placement(request, product, error=None, status_code=200)`.
- `tests_level`: add `_refusal(request, reason) -> Response` and `_require_lease(request, principal) -> Response | None`, used by run-all, `post_test_command` and `tests_sale_run`. Products-sorted-by-slot becomes one helper.
- `reports/compute.py`: add `_period_rows(range_key, bucket_key, sku=None) -> tuple[rows, total, csv_rows, body_lines]`, used by `_compute_period` and `_compute_product_sku`.
- `notify/notifier.py` `_send_email` returns `await mailer.send_email(email_config, owner_email, subject, body)`. Update tests that patch `notifier.smtp_send` to patch the mailer seam.
- `ice_contracts/board/common.py` exports `WATER_VALVE_SECONDS_RANGE = (1, 10)` and `POWER_CYCLE_DWELL_RANGE = (5, 300)`, used by its own validators and by `tests_level/commands.py`. Fold the two int-parse blocks into `_int_in_range(name, raw, lo, hi, default=None)`. Run `uv run python -m ice_contracts.generate` and commit any doc drift.
- [ ] Run `uv run pytest tests/edge/web tests/edge/notify tests/contracts -q`.
- [ ] Commit `refactor(web): collapse catalog/tests/report/notifier duplication`.

**Phase 4 gate:** run the full phase checks, then have the phase reviewed.

---

## Phase 5 — templates (branch `cleanup/p5-templates` from p4)

Before starting, capture a baseline. For each page listed in the tasks below, save the GET HTML from a TestClient render to the scratchpad. The rendered **ids, names, `aria-*`, labels and visible text must be identical** after the change; whitespace may differ. Write a throwaway comparison script in the scratchpad, not in the repo.

### Task 5.1 [S]: Form macros
**Interface:** create `packages/ui/src/ice_ui/templates/partials/form_macros.html` with:
- `field(id, label, name=None, type="text", value="", readonly=False, hint=None, **attrs)`
- `textarea(id, label, name=None, value="", rows=4, hint=None, **attrs)`
- `select(id, label, options, selected, name=None, **attrs)`
- `form_actions(cancel_url, label="Save")`
- `form_error(error)`

Each macro emits exactly the markup and classes in use today. Apply them to `settings_{comms,contacts,machine,mqtt,payments,reports,web}.html`, `product_form.html`, `product_catalog.html`, `product_placement.html`, `user.html` and `user_form.html`.

Also create `partials/product_fields.html`, shared by `product_form.html` and `product_catalog.html`. Its `maxlength` values come from template-context globals `max_sku_length` and `max_product_name_length`, which `context.template_context` injects from `MAX_SKU_LENGTH`/`MAX_PRODUCT_NAME_LENGTH`.
- [ ] Rebuild `app.css`.
- [ ] Run the baseline comparison, `uv run pytest tests/ui tests/edge/web -q`, and the opt-in browser tests.
- [ ] Commit `refactor(ui): form macros and shared product fields`.

### Task 5.2 [H]: Report partials
Create `partials/report_range_tabs.html` (params `base_url`, `range`, optional `bucket`), `partials/report_period_table.html` and `partials/report_email_form.html` (params `report`, `hidden`, `email_notice`, `email_error`). Apply them to `reports_{period,product,method,collections,product_sku}.html`.
- [ ] Rebuild `app.css`, run the baseline comparison, and run `uv run pytest tests/edge/web tests/ui -q`.
- [ ] Commit `refactor(ui): shared report partials`.

### Task 5.3 [H]: Tile builder
**Interface:** `ice_ui/tiles.py` gets `tile(title, url, icon, context, enabled=True) -> dict`. Use it in `REPORT_TILES`, `health_tiles`, `build_tiles`, `settings/overview.py`, `products/overview.py` and `users/accounts.py`. Settings tile URLs use the `LEVEL_*.url` constants.
- [ ] Run `uv run pytest tests/ui tests/edge/web tests/cloud -q`.
- [ ] Commit `refactor(ui): tile() builder`.

### Task 5.4 [S]: Cloud confirm builders and badges
- Give each of `admin/accounts.py` (suspend), `admin/machines.py` (unbind, purge) and `people.py` (revoke) a single `_<action>_ctx(...) -> dict`, used by both the page route and the confirm fragment route.
- The page templates render the context through `partials/confirm_button.html` with `{% with %}`.
- Delete `partials/admin_suspend.html`, `admin_unbind.html` and `people_revoke.html`.
- Extract `partials/alert_kind_badge.html` from `alerts.html` and `admin_alerts.html`. Extract the online badge from `fleet.html`, using the existing `partials/admin_status_badge.html`.
- [ ] Rebuild `app.css`, then run `uv run pytest tests/cloud -q`.
- [ ] Commit `refactor(cloud): single confirm context per action; shared badges`.

**Phase 5 gate:** run the full phase checks, plus the opt-in browser tests. Then have the phase reviewed.

---

## Phase 6 — test consolidation (branch `cleanup/p6-tests` from p5)

Rule for every task in this phase: **record the collected test count before and after.** A drop is allowed only for named duplicate deletions, and the commit body lists each deleted test and the surviving test that covers it.

### Task 6.1 [S]: Single gate table
- Move `_MATRIX_ROUTES` and the matrix driver from `test_web_routes.py` into `tests/edge/web/gate_table.py` (data) and `tests/edge/web/test_gate_matrix.py` (tests).
- Add every health, tests, reports and controls route from the `web/CLAUDE.md` gate table.
- Add `test_post_without_htmx_is_403`, parametrized over the table's POST rows.
- Delete `test_get_status_per_role` (all copies), `test_post_requires_htmx_header` and `test_post_without_htmx_header_is_403` copies, `TestPermissionGates`, `TestPostPermissionMatrix`, `TestCommandRunAndSalePermissionGates`, `TestLevelPermissionGates`, `test_reports_gate`, `test_gate` and `test_gate_product_sku`. **Keep** variants with special setup, such as fault seeding, a product sku, `/tests/mdb/ping` allowlisting or a body-specific 403. Move them into the table with a setup hook, or leave them in place.
- [ ] Run `uv run pytest tests/edge/web -q`.
- [ ] Commit `test(web): one permission gate table`.

### Task 6.2 [H]: Merge the user and device tests
- Delete the `test_web_routes.py` tests that duplicate `test_routes_users.py`: the secretary-403, self-disable, own-pin-reset, reset-pin-untrusts, own-row-controls and htmx-forbidden pairs, plus `TestHealthTabIdentity`.
- Move the remaining `TestUserManagement`, `TestOwnershipTransfer*`, `TestEmergencyCodeRegeneration` and `TestDeviceManagement` classes into `test_routes_users.py`. A new `test_routes_devices.py` is acceptable for the device classes.
- [ ] Run `uv run pytest tests/edge/web -q`.
- [ ] Commit `test(web): retire duplicate user/device tests from test_web_routes`.

### Task 6.3 [S]: Shared fakes
**Interfaces:**
- `tests/fakes.py::FakeMqtt` gains:
  - `register(topic, handler)`;
  - a `connected: bool` attribute, defaulting to true; publishes are dropped while it is false;
  - `deliver(topic, payload)`;
  - `deliver_ack(...)` and `deliver_dispenser_report(...)`, matching `tests/edge/hal/test_command_dispatcher.py::FakeMQTTClient`;
  - publishes recorded as `(topic, payload, retain)`.

  Callers that read 2-tuples are adapted.
- `tests/fakes.py::FakeEventRecorder(fail_with: Exception | None = None)` with `events` and `sales`.
- `FakeFaults` with `raised`, `cleared`, `has(code)`, `raise_fault(code, *, sku=None, outcome=None)` and `clear_fault(code, *, by=...)`. It lives in `tests/edge/payment/mdb/conftest.py` or `fakes.py`.
- `tests/async_helpers.py::wait_until(predicate, timeout=...)`.

Replace the copies:
- the MQTT recorder in `test_machine.py`, the `test_vmc_flows.py` inline classes, `test_display_controller.py` and `test_routes_tests.py`;
- the recorders in `test_vmc_flows.py`, `test_vmc_dispense_profiles.py`, `test_test_sale_runner.py` and `test_dispense_cycle.py`;
- the five payment/mdb `FakeFaults`;
- the three `_wait_until`.

Leave a fake alone where its semantics really differ, and say so in the commit body.
- [ ] Run `uv run pytest tests/edge tests/integration -q`, then `uv run pytest tests/meta -q`.
- [ ] Commit `test: shared MQTT/recorder/faults fakes`.

### Task 6.4 [H]: Small helpers and fixtures
- `tests/edge/db_helpers.py` provides `insert_event(db, event_type, ts, value, metadata=None)`, `insert_events(db, rows)`, `db_rows(db, sql, params=())` and `user_version(db)`. Use them in `test_event_recorder.py`, `test_reports.py`, `test_migration_cents.py`, `test_routes_health_pay104.py` and `test_integration_e2e.py`.
- `tests/dom_utils.py::bar(html)` replaces the five `_bar` copies, where the signatures match.
- `tests/edge/reporting/tz.py` holds `_SyntheticDstTz`, `_SyntheticFallBackTz`, `FIXED_TZ` and `_dt`. Update the root CLAUDE.md "DST testing" paragraph, which cites line numbers that will now be wrong.
- `tests/edge/fault_helpers.py::fault_codes(vmc) -> set[str]` replaces `{f["code"] for f in vmc.active_faults()}`.
- A `health_monitor` fixture in `tests/edge/web/conftest.py`.
- A `make_cloud(**kw)` factory fixture in `tests/cloud/conftest.py`, used across `test_ingest.py`, `test_auth.py`, `test_ingest_alerts.py`, `test_admin.py` and `test_ui_parity.py`.
- Share `_imported_modules` between `tests/sims/mdb/test_structure.py` and `tests/sims/test_vending_structure.py`, in `tests/sims/structure_helpers.py`.
- [ ] Run `uv run pytest tests -q`, or the affected directories.
- [ ] Commit `test: shared db/dom/tz/fault helpers and fixtures`.

### Task 6.5 [S]: Sims conformance and parametrization
- `tests/sims/test_simulator_conformance.py` is parametrized over the four simulator classes, for ping, duplicate-request_id idempotency and force_report. Delete the generic duplicates in `test_simulator_ice_maker.py` and `test_simulator_vending.py`, and keep their subclass-specific assertions.
- Turn the five ice-maker `capture` stubs into a fixture.
- In `tests/firmware/conftest.py`, add a `sim_on_thread(sim)` context manager and `wait_until_answering(..., subsystem)`, shared with `tests/firmware/mdb/conftest.py`. The vending conftest's lifetime and client-id comments are preserved.
- `tests/firmware/golden_common.py` holds `ACK_KEYS`, `load()` and the shared golden checks.
- Parametrize the COM-101/102/103/PAY-101 loss tests and the PAY-104 boot tests in `tests/edge/sale/test_vmc_flows.py`.
- [ ] Run `uv run pytest tests/sims tests/firmware tests/edge/sale -q`. The firmware conformance tests skip without a board, and that is expected.
- [ ] Commit `test: simulator conformance suite and parametrized loss/boot tests`.

**Phase 6 gate:** run the full phase checks, then have the phase reviewed.

---

## Phase 7 — common and cloud helpers (branch `cleanup/p7-common` from p6)

### Task 7.1 [S]: `atomic_write`
**Interface:** `ice_common/paths.py`:

```python
def atomic_write(path: Path, data: bytes | str, *, mode: int | None = None,
                 backup: bool = False, encoding: str = "utf-8") -> None:
    """Write via a same-dir temp file: (O_CREAT|O_EXCL with `mode` when given) -> write -> flush+fsync
    -> if backup and path exists: copy path to path.with_suffix(path.suffix + '.bak') (copy, not rename)
    -> os.replace(tmp, path) -> fsync_dir(path.parent). On any exception the temp file is unlinked
    and the exception re-raised; the target is untouched."""
```

**Tests first** (`tests/common/test_paths.py`):
- bytes and str both round-trip;
- `mode=0o600` gives 0600 on POSIX; skip the check on Windows following the existing skip policy;
- `backup=True` leaves the previous content in `.bak`;
- a failure injected at replace leaves the target unchanged and no temp file behind;
- fsync is called (monkeypatch `os.fsync`).

**Convert, one commit per group, preserving each site's exact mode, encoding, backup and fsync behaviour:**
- `ledger/session_store.py`, `ledger/payment_cursor.py`, `ledger/event_recorder.py::_atomic_write_text`;
- `config/config_store.py`, `vend/dispensers.py::save_text`, `access/access.py::save`;
- `monitoring/inventory_manager.py`, which gains fsync. Its test asserts fsync now happens;
- `uplink/identity.py`, `ice_ui/bundle.py` and the cloud `store/snapshots.py`/`ui_registry.py`, only where the semantics match. `ice_ui` cannot import `ice_common`, so `bundle.py` keeps its own copy and says so.
- [ ] Run the per-path suites unchanged: `uv run pytest tests/common tests/edge/ledger tests/edge/config tests/edge/vend tests/edge/access tests/edge/monitoring tests/edge/uplink tests/cloud -q`.
- [ ] Update the `fsync_dir` docstring and `ledger/CLAUDE.md` if it describes the pattern.
- [ ] Commits: `refactor(common): atomic_write helper`, then one `refactor: use atomic_write in <area>` per group, then `fix(monitoring): inventory save now fsyncs`.

### Task 7.2 [H]: `config_path`, `topic_matches`, `require_htmx`, small common items
**Interfaces:**
- `ice_common/paths.py::config_path() -> Path` returns `Path(os.environ.get("ICE_COLDER_CONFIG") or "config.json")`, read at call time. Use it in `app/startup_config.py`, `config/config_store.py`, `vend/dispensers.py` and `ice_sims/base.py`.
- `ice_common/mqtt_client.py::topic_matches(pattern: str, topic: str) -> bool` is module-level. `MQTTClient.topic_matches` delegates to it, and `ice_sims/base.py::_topic_matches` is deleted in favour of it. Merge the twin `except` blocks in `MQTTClient.run`, keeping both log texts distinguishable by type in a single message.
- `ice_ui` gets a new `ice_ui/htmx.py::require_htmx`, a FastAPI dependency. Both edge `web/context.require_htmx` and cloud `auth.require_htmx` become imports of it; keep the names re-exported where routes import them.
- `ice_common/logging_setup.py`: the three filtered sinks become one loop over `(name, filter_key)`.
- Update `packages/edge/CLAUDE.md`'s `ICE_COLDER_CONFIG` text so it names `config_path()`.
- [ ] Run `uv run pytest tests/common tests/edge tests/cloud tests/sims -q`.
- [ ] Commit `refactor(common): config_path, topic_matches, shared require_htmx`.

### Task 7.3 [S]: Contracts and cloud helpers
- **Contracts.**
  - Create `contracts/cloud/_base.py` with `_Model`, used by `report.py`, `identity.py` and `ui.py`.
  - Add `board/common.py::_first_error(exc) -> str`.
  - Inline the single-use constants in `generate.py`.
  - Move the contract version-history comment blocks in `board/ice_maker_monitor.py` and `board/vending_machine.py` into `docs/architecture-history.md`, unless the history is already there; check first.
  - Regenerate the schemas; the output must not change.
- **Cloud.**
  - `ice_cloud/context.py` holds `badge(...)` and `page_context(request, *, viewer, level, hat_switch=None, badge=...)`, used by `portal._context`, `admin/common.admin_context`, `claim._page` and `auth._login_page`. The claim page keeps having no badge.
  - Add `UiRegistry.installed() -> ResolvedUi`.
  - Remove the `resolve_ui`/`page_context` alias names by renaming the real functions.
  - Machine cookie: `set_machine_cookie(response, settings, fp)`.
  - `machine_or_404`-style short-fingerprint resolution is shared between `auth.require_machine_access`, `admin/common.machine_or_404` and `admin/alerts.py`.
  - `fleet_view.age_seconds(report, now)`.
  - Merge the `machine_auth.py` twin `BUNDLE_PREFIX` blocks.
- **`store/sqlite.py`.**
  - Add `_one(sql, params, mapper)` and `_wipe_machine_state(conn, fp)`. The latter must read the module-level `DELETE_MEMBERSHIPS_SQL` at call time, because `tests/cloud/test_repository.py` monkeypatches it.
  - Rename `_ALERT_DELETES` to `_MACHINE_STATE_DELETES`.
  - `_insert_alert_events` builds `StoredEvent(**asdict(e), id=...)`.
- **Alerts.**
  - The engine emits `AlertEventRow`, through `AlertEventRow.from_fault(fp, kind, at, fault)` and `AlertEventRow.bare(fp, kind, at)`, used by `ingest.py` and `sweep.py`.
  - Delete `engine.AlertEvent`, `dispatcher._MailEvent` and `MailEvent.scope`/`_scope_text`.
  - `alerts/format.py` holds `fmt_utc(dt)` and `event_row(e)`, used by `admin/alerts.py`, `alerts/pages.py` and `alerts/mail.py`.
- [ ] Run `uv run pytest tests/cloud tests/contracts -q`, then `uv run python -m ice_contracts.generate`, which must produce no diff.
- [ ] Commits: `refactor(contracts): shared base model and error helper`, then `refactor(cloud): shared page context, sqlite helpers, alert rows`.

**Phase 7 gate:** run the full phase checks, then have the phase reviewed.

---

## Phase 8 — edge core, sims, low-risk firmware (branch `cleanup/p8-core` from p7)

### Task 8.1 [S]: Fault idempotent helpers and `Subscribers`
**Interfaces:**
- `FaultService.ensure_raised(code: FaultCode, *, outcome: str | None = None) -> bool` raises the fault only when it is not active and returns whether it raised.
- `FaultService.ensure_cleared(code: FaultCode, *, by: str = "auto") -> bool` clears the fault only when it is active, going through `clear_fault` so the special handling still applies.

Tests come first in `tests/edge/faults/`. Then convert `app/machine.py` (`_raise_event_unwritten` and the rest), `payment/mdb/state.py`, `payment/mdb/intake.py::_raise_pay105`, `faults/input_faults.py` (by `"hardware"`) and `fault_service.py`'s own two sites. DATA-104's multi-source logic stays.

`VMC` uses `ice_common.observers.Subscribers` for its state-change and sale-settled lists, keeping the `subscribe_state_change`/`subscribe_sale_settled` signatures and return values.

Also add a `HealthEdge(on_failed, on_ok)` in `ice_common/observers.py` with `ok()` and `failed()`, firing each only on an edge. It is used by `EventRecorder.set_write_observers` and `InventoryManager.set_save_observers`.
- [ ] Run `uv run pytest tests/edge tests/common -q`, then `uv run pytest tests/meta -q`, which covers the VMC complexity and private-access ratchets.
- [ ] Commit `refactor(edge): idempotent fault helpers, Subscribers in VMC, HealthEdge`.

### Task 8.2 [S]: Shared `Timer`
**Interface:** `supervisor/task_runner.py::Timer(tasks: TaskRunner, label: str)` provides:
- `arm(delay: float, callback) -> None`, which replaces any armed timer;
- `arm_if_idle(delay, callback) -> bool`;
- `cancel() -> None`;
- an `armed: bool` property, which is false once the timer has fired or been cancelled.

Tests come first with `FakeTaskRunner`.

Convert `faults/ice_policy.py`, `faults/input_faults.py`, `vend/dispense_cycle.py`, `payment/mdb/payout.py` (`arm_if_idle`) and the three `payment/mdb/policy.py` timers. `ice_policy._on_cooldown` keeps dropping the handle before it clears. `SessionTimer`/`PaymentRetryTimer` may wrap `Timer`, but their public API is unchanged.
- [ ] Run `uv run pytest tests/edge -q`.
- [ ] Commit `refactor(edge): one re-armable Timer`.

### Task 8.3 [S]: Dispenser schema introspection and access/config twins
- `vend/schema_introspect.py` holds `unwrap_annotated`, `union_variants`, `discriminator_field`, `bounds_and_unit(field_info) -> (lo, lo_excl, hi, hi_excl, unit)`, and `union_tags(alias) -> frozenset[str]`. Delete the hand-typed `_DISCRIMINATOR_TAGS` and the duplicated scanners in `dispensers.py`/`dispensers_doc.py`. `humanize` and the CLI output must stay byte-identical, which the existing tests pin.
- `access/access.py`:
  - `load`/`_document` use `dataclasses.asdict` plus a `from_dict` that applies the defaults, with one `_reset()`;
  - add `_live(record)` and `_cancel(attr)` for pending transfers and links.

  The on-disk JSON key set and order must be unchanged; compare a saved file before and after.
- `config/config_store.py`: add `_slot_problem(config, slot, ignore_sku=None) -> str | None`.
- `app/startup_recovery.py`: one DATA-103 raise.
- `hal/command_dispatcher.py`: add `_wait(future, timeout)`, and a shared exception base with `subsystem` and `command` for `CommandTimeout`/`CompletionTimeout`.
- Collapse `VMC.snapshot` and `_snapshot` into the public `snapshot()`.
- Move the `dispensers` CLI to `vend/__main__.py` (`uv run python -m ice_edge.vend`), using `startup_config.load_config`. Update `dispensers.example.toml`'s usage comment and `vend/CLAUDE.md`.
- [ ] Run `uv run pytest tests/edge -q` and `tests/meta`.
- [ ] Commit `refactor(edge): schema introspection, access serialisation, small twins`.

### Task 8.4 [S]: Sims data-driven faults
- `ice_sims/base.py` gets a generic `InjectedFault` dataclass, `to_fault_def`, and `set_channel(channel, on_value)` helpers. `activate`/`recover` default to no-ops.
- The vending and mdb fault modules become data rows. Only `ro_tank_empty`, `ro_fill_stuck`, `changer_empty` and `board_reboot` keep real functions.
- `ice_maker.py` gets a `(fault, sensor, pin_value, halt_detail)` table with two generic closures. Keep the fault names.
- Add `uptime_entity(subsystem, name)` and `temperature_entity(channel_id, label)`, `_cache_and_publish_ack`, a single `sleep`/`set_sleep` seam on `ESP32Simulator`, and the ambient `SensorReading` helper.
- **Before** deleting the per-fault log lines, grep `tests/sims` for their text. Keep any that a test asserts, or retarget that test to `base.py`'s "Fault activated/cleared" line.
- `base.py` must not import from `vending` or `mdb`; `packages/sims/CLAUDE.md` describes the import-direction test.
- [ ] Run `uv run pytest tests/sims tests/integration -q`.
- [ ] Commit `refactor(sims): data-driven faults and shared base helpers`.

### Task 8.5 [S]: Firmware test side and shims
- In `firmware/mdb/test/support/fakes.h`, add `ScriptedPeripheral : FakePeripheral`, which provides the block queue, `clear_script`, `add`, `ack`, `nak`, `data` and `silent`. `fake_changer.h`, `fake_reader.h` and `fake_validator.h` inherit from it. Add `template <size_t N> struct Sink : EventSink` with `count`, `first` and `last`, and use it in `test_cashless`, `test_changer`, `test_validator` and `test_master`.
- Delete `firmware/vending/lib/vendcore/ack_cache.h` and `outbound_ring.h`. Include the `boardcore/` headers directly, with `using` declarations once in `ports.h`.
- Move `firmware/vending/test/test_ack_cache` and `test_outbound_ring` to `firmware/common`, with a native test env there; follow the `firmware/CLAUDE.md` rules. If `common/` cannot host a PlatformIO test env, keep the tests where they are and record why.
- Add `boardidf::mono_ms()`/`mono_us()`, which shrinks both `esp_clock.h` files. Add `boardidf::board_boot(identity, settings, after_load)` for the shared NVS/settings/console boot; MDB passes its flash-guard hooks as `after_load`.
- [ ] Run `pio test -e native` in `firmware/vending` and in `firmware/mdb` (the pio and toolchain notes are in the `project_firmware_toolchain` memory). Build both boards: `pio run -e kc868-a8` and `pio run -e esp32-s3-devkitc-1`. The goldens must be unchanged: `git diff --stat firmware/*/test/golden` is empty.
- [ ] Commit `refactor(firmware): shared test fakes, drop vendcore shims, boot/clock helpers`.

**Phase 8 gate:** run the full phase checks and both firmware native suites, then have the phase reviewed.

---

## Finish

- [ ] Write a final measurement into the PR descriptions: `git diff --shortstat cleanup/dry-design..cleanup/p8-core`.
- [ ] Ask the owner before pushing or opening PRs. There is one PR per phase branch, stacked.
