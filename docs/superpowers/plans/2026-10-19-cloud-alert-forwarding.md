# Cloud Alert Forwarding Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The cloud emails a machine's owner and techs when a fault raises, clears or the machine stops reporting; people see the history in the portal; a claimed machine's edge stops emailing locally while the cloud is reachable.

**Architecture:** No new wire data from the edge: `AlertEngine` diffs each `MachineReport` against a stored `machine_alert_state` row, an `OfflineSweep` adds offline/online, `Router` picks recipients by role and mute flag, `AlertDispatcher` bundles into one email per recipient per machine under a 10-minute cooldown and records every delivery. SMTP code moves to `ice_common.mailer`; the cloud gets a `Mailer` protocol. `RegisterResponse.alerts_handled` (contract 0.3.0) tells the edge to defer its own `Notifier`, which falls back after 3 missed intervals.

**Tech Stack:** Python 3.12, FastAPI, Pydantic v2, SQLite (stdlib), `smtplib`, Jinja2/HTMX, pytest.

Spec: `docs/superpowers/specs/2026-10-19-cloud-alert-forwarding-design.md`. Section numbers (§) refer to it. Branch: `cloud/alerts` off `cloud/identity-tenancy`.

## Global Constraints

- Package layers (import-linter): `ice_contracts` → `ice_common` → `ice_ui` → (`ice_edge` | `ice_cloud` | `ice_sims`). `ice_cloud` never imports `ice_edge`. Cloud tests import `ice_edge` only in `tests/cloud/test_ui_parity.py`.
- Plan style (user rule): tasks specify interfaces and tests, not full code bodies. Implementers write the code.
- Before every commit: `uv run ruff check --fix .`, `uv run ruff format .`, then the task's tests. Always `uv run ruff`, never the global ruff. Do not chain shell commands with `&&`.
- Commit trailer, exactly: `Assisted-by: <model> (Claude Code)`; no `Co-Authored-By`.
- Private-access ratchet (`tests/meta/test_no_private_access.py`): new tests reach no `_private` attribute. `tests/meta/test_routes_shape.py` does not cover `ice_cloud`, but every cloud route module exposes a module-level `router`.
- Cloud templates (`packages/cloud/src/ice_cloud/templates/`) may use only CSS classes already in the committed `packages/ui/src/ice_ui/static/app.css` (`tests/cloud/test_cloud_static_css.py`); copy class sets from `people.html`, `fleet.html`, `admin.html`.
- Every mutating cloud route: `Depends(require_htmx)`; machine pages use `require_machine_access()`; non-members get 404.
- Constants (exact): `SWEEP_SECONDS = 60`, offline threshold `3 * report_interval_seconds` (strictly greater), `COOLDOWN_SECONDS = 600`, max delivery attempts `5`, alerts page limit `200`, fleet count window 24 h, SMTP default port `587`, STARTTLS default on, error text truncated to 500 chars.
- Role table (§3.1): owner gets every `raised`/`offline`/`online`; tech gets `raised` only when `gate in {"safety","fulfillment"}`, plus `offline`/`online`; `cleared` never sends alone, it rides along as "cleared since your last email"; muted gets nothing (delivery status `muted`); suspended accounts are excluded before routing.
- Every event timestamp is the cloud's injected `now()`, never `report.sent_at`.
- Alerts never fail ingest: exceptions are logged, the report still returns 204.
- Contract `0.2.0 → 0.3.0` additive; cloud accepts both report versions.
- No `TBD`/`TODO` in delivered code or docs.

## File map

| Path | Responsibility |
|---|---|
| `packages/contracts/src/ice_contracts/cloud/identity.py` | `RegisterResponse.alerts_handled: bool = False` |
| `packages/contracts/src/ice_contracts/cloud/report.py` | `CLOUD_CONTRACT_VERSION = "0.3.0"`, `contract: Literal["0.2.0", "0.3.0"]` |
| `docs/contracts/edge-cloud/CONTRACT.md` | 0.3.0 section |
| `packages/common/src/ice_common/mailer.py` (new) | `smtp_send(*, host, port, username, password, use_starttls, msg)` |
| `packages/edge/src/ice_edge/notify/mailer.py` | `smtp_send(email_config, msg)` calls through |
| `packages/cloud/src/ice_cloud/settings.py` | SMTP fields, `smtp_configured` |
| `packages/cloud/src/ice_cloud/store/repository.py`, `store/sqlite.py`, `store/__init__.py` | §6 types, methods, 3 migrations, cascades |
| `packages/cloud/src/ice_cloud/alerts/__init__.py` (new) | empty package marker |
| `packages/cloud/src/ice_cloud/alerts/engine.py` (new) | `AlertEvent`, `diff_reports` |
| `packages/cloud/src/ice_cloud/alerts/router.py` (new) | `Recipient`, `recipients_for` |
| `packages/cloud/src/ice_cloud/alerts/mail.py` (new) | `Mailer` protocol, `SmtpMailer`, `LogOnlyMailer`, `RecordingMailer`, `render_email` |
| `packages/cloud/src/ice_cloud/alerts/dispatcher.py` (new) | `AlertDispatcher` |
| `packages/cloud/src/ice_cloud/alerts/sweep.py` (new) | `OfflineSweep` |
| `packages/cloud/src/ice_cloud/alerts/pages.py` (new) | `/machines/{short}/alerts`, mute toggle |
| `packages/cloud/src/ice_cloud/templates/alerts.html` (new), `people.html`, `fleet.html`, `admin.html` | pages |
| `packages/cloud/src/ice_cloud/ingest.py`, `app.py`, `portal.py`, `people.py`, `admin.py` | wiring |
| `packages/edge/src/ice_edge/uplink/identity.py`, `client.py`, `loop.py`, `notify/notifier.py`, `__main__.py`, `web/routes/health/overview.py` + template | edge hand-off |
| `packages/cloud/README.md`, `CLAUDE.md`, `.env.example`, `docker/docker-compose.cloud.yml` | docs/deploy |

## Waves (for the controller)

- **Wave 1 (parallel, independent files):** T1 contracts, T2 common mailer, T3 settings, T4 repository, T5 engine, T6 router, T7 mail. T5/T6/T7 each create an identical empty `alerts/__init__.py`; merges are clean.
- **Wave 2:** T8 dispatcher (T4, T6, T7), T11 edge hand-off (T1), T12 portal pages (T4), T13 admin SMTP (T3, T7).
- **Wave 3:** T9 sweep (T4, T8), T10 ingest + app wiring (T3, T4, T5, T8, T9, T1).
- **Wave 4:** T14 docs. Then final whole-branch review.

---

### Task 1: Contract 0.3.0

**Files:** Modify `packages/contracts/src/ice_contracts/cloud/identity.py`, `report.py`; `docs/contracts/edge-cloud/CONTRACT.md`. Test `tests/contracts/test_cloud_identity.py`, `tests/contracts/test_cloud_contract.py`.

**Interfaces — produces:** `RegisterResponse(fingerprint, claimed, alerts_handled: bool = False)`; `CLOUD_CONTRACT_VERSION == "0.3.0"`; `MachineReport.contract: Literal["0.2.0", "0.3.0"]`.

- [ ] Tests: `test_register_response_defaults_alerts_handled_false` (parse `{"fingerprint":..,"claimed":true}`), `test_register_response_round_trips_alerts_handled`, `test_contract_version_is_0_3_0`, `test_report_accepts_0_2_0_and_0_3_0`, `test_contract_doc_has_0_3_0_section` (grep CONTRACT.md for `## 0.3.0`). Run, see them fail.
- [ ] Implement; the edge's `build_machine_report` sends `CLOUD_CONTRACT_VERSION` (confirm it reads the constant; fix if it hardcodes). CONTRACT.md 0.3.0 section: the one new field, its meaning (§4.1), that reports may still say 0.2.0.
- [ ] Run `uv run pytest tests/contracts tests/edge/uplink tests/cloud/test_ingest.py`. Commit `feat(contracts): cloud contract 0.3.0 adds RegisterResponse.alerts_handled`.

### Task 2: `ice_common.mailer`

**Files:** Create `packages/common/src/ice_common/mailer.py`; modify `packages/edge/src/ice_edge/notify/mailer.py`. Test `tests/common/test_mailer.py` (new), `tests/edge/notify/test_mailer.py`.

**Interfaces — produces:** `ice_common.mailer.smtp_send(*, host: str, port: int, username: str | None, password: str | None, use_starttls: bool, msg: EmailMessage, smtp_factory=smtplib.SMTP, timeout: float = SMTP_TIMEOUT_SECONDS) -> None` (raises on failure; skips `login` when `username` is None; skips `starttls` when `use_starttls` is False). `SMTP_TIMEOUT_SECONDS = 15.0` lives here. Edge `smtp_send(email_config, msg)` keeps its signature and calls through with `use_starttls=True`.

- [ ] Tests with a fake SMTP class recording calls: `test_smtp_send_starttls_login_send`, `test_smtp_send_no_login_without_username`, `test_smtp_send_no_starttls_when_disabled`, `test_smtp_send_propagates_errors`; edge: `test_edge_smtp_send_passes_config_through`. Fail first.
- [ ] Implement. Run `uv run pytest tests/common/test_mailer.py tests/edge/notify`. Commit `refactor: move SMTP send to ice_common.mailer`.

### Task 3: SMTP settings

**Files:** Modify `packages/cloud/src/ice_cloud/settings.py`, `.env.example`, `docker/docker-compose.cloud.yml`. Test `tests/cloud/test_settings.py`.

**Interfaces — produces:** `CloudSettings` fields `smtp_host: str | None`, `smtp_port: int = 587`, `smtp_user: str | None`, `smtp_password: str | None`, `smtp_from: str | None`, `smtp_starttls: bool = True`; property `smtp_configured -> bool` (host set). `from_env` reads `ICE_CLOUD_SMTP_HOST/_PORT/_USER/_PASSWORD/_FROM/_STARTTLS` (`STARTTLS` `"0"` disables; anything else on). `SettingsError` when host set without `FROM`, or `PORT` not an int in 1..65535.

- [ ] Tests: `test_smtp_unset_is_not_configured`, `test_smtp_host_without_from_is_error`, `test_smtp_defaults_port_and_starttls`, `test_smtp_bad_port_is_error`, `test_smtp_starttls_zero_disables`. Fail first.
- [ ] Implement; add the env block to `.env.example` (commented defaults) and pass-through in the compose file (same style as the `ICE_CLOUD_GOOGLE_*` lines). Run `uv run pytest tests/cloud/test_settings.py`. Commit `feat(cloud): SMTP settings`.

### Task 4: Repository: alert state, events, deliveries, prefs

**Files:** Modify `packages/cloud/src/ice_cloud/store/repository.py`, `store/sqlite.py`, `store/__init__.py`. Test `tests/cloud/test_repository.py`.

**Interfaces — produces (§6, exact):** dataclasses `AlertState`, `AlertEventRow(fingerprint, kind, at, key, code, severity, gate, description, sku, product)` (the store's own frozen dataclass; Task 5's `AlertEvent` has the same fields so `record_alert_events` accepts any object with them, typed as `AlertEventRow`), `StoredEvent(AlertEventRow + id: int)`, `Recipient(account_id, email, role)`, `Delivery(id, fingerprint, account_id, event_ids: tuple[int, ...], status, attempted_at, sent_at, error, attempts)`. Methods: `alert_state`, `put_alert_state`, `delete_alert_state`, `claimed_machines_with_state`, `record_alert_events -> list[StoredEvent]`, `alert_events(fingerprint, *, limit)`, `alert_count_since`, `cleared_since_last_sent`, `people_for_alerts -> list[tuple[Recipient, bool]]` (owner first, then techs by `granted_at`; excludes suspended), `set_muted`, `is_muted`, `create_delivery -> Delivery`, `pending_deliveries(*, before) -> list[Delivery]` (status `pending` with `attempted_at <= before`, or `failed` with `attempts < 5`), `mark_delivery(id, *, status, now, error=None)` (increments `attempts`; sets `sent_at` on `sent`), `last_sent_at`, `deliveries_for_events(event_ids) -> dict[int, list[Delivery]]`. Migrations appended: `machine_alert_state`; `alert_events` + `alert_deliveries`; `alert_prefs`. Cascades: `claim_machine` (owner changes) deletes state and prefs for the machine; `revoke` deletes that person's pref; `unbind_machine` deletes state, events, deliveries, prefs.

- [ ] Tests, one per method plus: `test_migrations_apply_on_existing_db` (open a db created at the previous schema version), `test_claim_change_resets_alert_state_and_prefs`, `test_reclaim_same_owner_keeps_state`, `test_revoke_deletes_pref`, `test_unbind_deletes_everything_alert`, `test_pending_deliveries_excludes_exhausted_failed`, `test_people_for_alerts_excludes_suspended`. Fail first.
- [ ] Implement. Run `uv run pytest tests/cloud/test_repository.py tests/cloud`. Commit `feat(cloud): alert tables and repository methods`.

### Task 5: `AlertEngine`

**Files:** Create `packages/cloud/src/ice_cloud/alerts/__init__.py` (empty), `alerts/engine.py`. Test `tests/cloud/alerts/__init__.py`, `tests/cloud/alerts/test_engine.py`. Reuse report builders from `tests/cloud/reports.py`.

**Interfaces — produces:** `AlertEvent` frozen dataclass (§2.1 fields); `diff_reports(previous_keys: frozenset[str], report: MachineReport, *, now, fingerprint) -> list[AlertEvent]`; `KIND_ORDER = ("cleared", "raised")`. Imports only `ice_contracts`; no repository.

- [ ] Tests: `test_first_report_raises_every_fault`, `test_unchanged_emits_nothing`, `test_removed_key_is_cleared_with_code_from_key`, `test_mixed_orders_cleared_then_raised_by_key`, `test_at_is_now_not_sent_at`, `test_product_fault_carries_sku_and_product`. Fail first.
- [ ] Implement. Run `uv run pytest tests/cloud/alerts/test_engine.py`. Commit `feat(cloud): alert engine diffs reports`.

### Task 6: `Router`

**Files:** Create `alerts/__init__.py` (empty, if absent), `alerts/router.py`. Test `tests/cloud/alerts/test_router.py`.

**Interfaces — produces:** `Recipient(account_id: str, email: str, role: Literal["owner","tech"])` (the router's own; Task 4's store `Recipient` has identical fields — Task 8 passes store rows straight in); `recipients_for(events: Sequence[AlertEvent], people: Sequence[tuple[Recipient, bool]]) -> dict[str, list[AlertEvent]]` keyed by `account_id`, containing only triggering events (never `cleared`); `muted_ids(people) -> set[str]`. Takes `AlertEvent` from Task 5 by duck type (fields `kind`, `gate`), so this task defines a tiny local protocol rather than importing engine (keeps Wave 1 independent).

- [ ] Tests: the §3.1 table as parametrised cases, `test_cleared_never_triggers`, `test_muted_gets_nothing`, `test_empty_when_no_triggering_events`, `test_tech_skips_alert_gate_owner_gets_it`. Fail first.
- [ ] Implement. Run `uv run pytest tests/cloud/alerts/test_router.py`. Commit `feat(cloud): alert router by role and mute`.

### Task 7: Mailers and email rendering

**Files:** Create `alerts/__init__.py` (empty, if absent), `alerts/mail.py`. Test `tests/cloud/alerts/test_mail.py`.

**Interfaces — produces:** `class Mailer(Protocol): async def send(self, msg: EmailMessage) -> None`; `SmtpMailer(settings: CloudSettings)` (uses `ice_common.mailer.smtp_send` via `asyncio.to_thread`); `LogOnlyMailer()`; `RecordingMailer()` with `.sent: list[EmailMessage]` and `.fail_next: Exception | None`; `make_mailer(settings) -> Mailer`; `render_email(*, label, short, role, triggering: Sequence[AlertEvent], cleared: Sequence[AlertEvent], public_url: str | None, from_addr: str, to: str, last_report_at: datetime | None) -> EmailMessage` producing exactly the §3.4 layout (subject rule: `offline`/`online` → "offline"/"back online"; else "<n> raised, <m> cleared").

- [ ] Tests: golden strings for three bodies (raised+cleared, offline, online), `test_subject_forms`, `test_links_omitted_without_public_url`, `test_make_mailer_log_only_when_unconfigured`, `test_smtp_mailer_calls_smtp_send_with_settings` (monkeypatch `smtp_send`), `test_recording_mailer_fail_next`. Fail first.
- [ ] Implement. Run `uv run pytest tests/cloud/alerts/test_mail.py`. Commit `feat(cloud): mailer protocol and alert email rendering`.

### Task 8: `AlertDispatcher`

**Files:** Create `alerts/dispatcher.py`. Test `tests/cloud/alerts/test_dispatcher.py` (uses `SqliteRepository(tmp_path / "cloud.db")` directly plus `RecordingMailer`).

**Interfaces — consumes:** Task 4 methods, Task 6 `recipients_for`, Task 7 `render_email`/`Mailer`. **Produces:** `AlertDispatcher(repo, mailer, *, now: Callable[[], datetime], public_url: str | None, from_addr: str)` with `async def dispatch(fingerprint: str, machine: Machine, events: Sequence[StoredEvent]) -> None` (route, create deliveries as `pending` if inside cooldown else attempt now) and `async def flush(now) -> None` (attempt every `pending_deliveries(before=now - COOLDOWN)`... precisely: pending rows whose recipient's `last_sent_at` is older than `COOLDOWN_SECONDS` or None, and retryable `failed` rows). `COOLDOWN_SECONDS = 600`, `MAX_ATTEMPTS = 5`. Sends sequentially; each recipient's outcome recorded on its own row; failures logged, never raised.

- [ ] Tests: `test_one_email_per_recipient_per_pass`, `test_cleared_rides_along_since_last_sent`, `test_cleared_alone_sends_nothing`, `test_inside_cooldown_creates_pending_and_flush_sends_after`, `test_failed_send_marks_failed_and_retries_on_flush`, `test_sixth_attempt_not_retried`, `test_muted_recipient_row_status_muted`, `test_mailer_exception_does_not_propagate`, `test_tech_not_emailed_for_alert_gate`. Fail first.
- [ ] Implement. Run `uv run pytest tests/cloud/alerts`. Commit `feat(cloud): alert dispatcher with bundling and cooldown`.

### Task 9: `OfflineSweep`

**Files:** Create `alerts/sweep.py`. Test `tests/cloud/alerts/test_sweep.py`.

**Interfaces — consumes:** Task 4 `claimed_machines_with_state`, `put_alert_state`, `record_alert_events`; Task 8 `dispatch`, `flush`. **Produces:** `SWEEP_SECONDS = 60`; `OfflineSweep(repo, dispatcher, *, now)` with `async def tick() -> list[StoredEvent]` (pure one pass: offline events for machines past `3 * interval`, then `dispatcher.flush(now())`) and `async def run(sleep=asyncio.sleep) -> None` (loop; per-machine exceptions logged, loop continues).

- [ ] Tests: `test_offline_strictly_after_three_intervals` (at exactly 3× nothing; at 3×+1 s one event), `test_offline_only_once`, `test_unclaimed_never`, `test_no_state_row_never`, `test_tick_flushes_pending`, `test_run_survives_exception` (repo stub raising once). Fail first.
- [ ] Implement. Run `uv run pytest tests/cloud/alerts`. Commit `feat(cloud): offline sweep`.

### Task 10: Ingest hook and app wiring

**Files:** Modify `packages/cloud/src/ice_cloud/ingest.py`, `app.py`. Test `tests/cloud/test_ingest_alerts.py` (new), `tests/cloud/test_ingest.py`.

**Interfaces — consumes:** everything above. **Produces:** `app.state.mailer`, `app.state.alerts` (`AlertDispatcher`), `app.state.sweep` (`OfflineSweep`, started in lifespan as a task, cancelled on shutdown; `create_app(..., start_sweep: bool = True)` so tests call `tick()` by hand). `register` returns `alerts_handled = claimed and settings.smtp_configured`. `post_report`: §2.4 steps, `online` when `offline_since` set, `delete`/`put_alert_state` in `asyncio.to_thread`; alerts wrapped so a raise still returns 204. `conftest.make_settings` gains optional SMTP kwargs; the `cloud_app` fixture injects `RecordingMailer` via a new `create_app(mailer=...)` keyword.

- [ ] Tests: `test_first_report_emails_owner_raised`, `test_second_identical_report_sends_nothing`, `test_cleared_bundled_into_next_raise`, `test_unclaimed_machine_no_events`, `test_online_after_sweep_offline`, `test_alert_failure_still_204` (monkeypatch dispatcher to raise), `test_register_alerts_handled_requires_claim_and_smtp` (4 combinations), `test_0_2_0_report_still_processed`. Fail first.
- [ ] Implement. Run `uv run pytest tests/cloud`. Commit `feat(cloud): wire alerts into ingest and app`.

### Task 11: Edge hand-off and fallback

**Files:** Modify `packages/edge/src/ice_edge/uplink/identity.py`, `client.py`, `loop.py`, `notify/notifier.py`, `__main__.py`, `web/routes/health/overview.py` and `packages/ui/src/ice_ui/templates/health.html` (the fingerprint line; cloud parity test renders the same template from the report, so add the label only inside the existing edge-only block or add a context key the cloud sets to `None` — check `tests/cloud/test_ui_parity.py` before touching the template). Tests `tests/edge/uplink/test_loop.py`, `test_client.py`, `test_identity.py`, `tests/edge/notify/test_notifier.py`, `tests/edge/web/test_health*.py`.

**Interfaces — consumes:** Task 1. **Produces:** `MachineIdentity.alerts_handled: bool` and `last_report_ok_at: datetime | None` (in-memory attributes, not written to the file; `_write` unchanged); `HttpUplink.register(label) -> bool` unchanged but sets `self.last_register: RegisterResponse | None`; loop sets `identity.alerts_handled = response.alerts_handled` on register, `identity.last_report_ok_at = now()` on a successful report, clears `alerts_handled` on any 401 or when `registered` becomes False; `ice_edge.uplink.identity.cloud_handles_alerts(identity, interval_seconds, now) -> bool` (§4.2); `Notifier(config, *, cloud_handles_alerts: Callable[[], bool] = lambda: False)`; `send()` defers after cooldown bookkeeping with the INFO line `Notifier: deferred to cloud: <key>`; `__main__` wires `lambda: cloud_handles_alerts(identity, live_config.cloud.report_interval_seconds, utc_now())` when identity exists (note `Notifier` is built before identity today: build the identity earlier or give `Notifier` a `set_cloud_handles_alerts` setter — pick the setter); `/health` shows "Alerts: cloud"/"Alerts: local" beside the fingerprint via a new `context` key `alerts_mode`.

- [ ] Tests: `test_register_stores_alerts_handled`, `test_report_ok_stamps_time`, `test_401_clears_alerts_handled`, `test_identity_file_unchanged_by_flags`, `cloud_handles_alerts` truth table (flag off; no report; at 3× true; at 3×+1 s false), notifier: `test_deferred_to_cloud_logs_and_skips_delivery`, `test_deferred_still_records_cooldown`, `test_falls_back_when_predicate_false`, health: `test_health_shows_alerts_mode`. Fail first.
- [ ] Implement. Run `uv run pytest tests/edge/uplink tests/edge/notify tests/edge/web tests/cloud/test_ui_parity.py`. Commit `feat(edge): defer alert email to the cloud while reachable`.

### Task 12: Portal: alerts page, mute toggle, fleet count

**Files:** Create `packages/cloud/src/ice_cloud/alerts/pages.py`, `templates/alerts.html`; modify `people.py`, `templates/people.html`, `portal.py`, `templates/fleet.html`, `app.py` (include `pages.router` before `portal.router`). Test `tests/cloud/test_alert_pages.py` (new), `tests/cloud/test_people.py`, `tests/cloud/test_portal.py`.

**Interfaces — consumes:** Task 4. **Produces:** `GET /machines/{short}/alerts` (`require_machine_access()`), level `Level.child(LEVEL_FLEET, "Alerts", ...)`, last 200 events with per-recipient delivery lines (`sent HH:MM`, `pending`, `failed: <error>`, `muted`); `POST /machines/{short}/people/{account_id}/mute` form `muted=0|1`, htmx, owner any / tech self only (403 otherwise), re-renders the people fragment; the owner's own row on the people page; `/fleet` shows "N alerts / 24 h" from `alert_count_since`; a link to Alerts from the fleet row and the people page.

- [ ] Tests: `test_alerts_page_lists_newest_first_with_deliveries`, `test_alerts_page_404_for_non_member`, `test_alerts_page_limit_200`, `test_owner_mutes_tech`, `test_tech_mutes_self`, `test_tech_cannot_mute_other_403`, `test_mute_requires_htmx`, `test_fleet_shows_24h_count`, `test_people_page_shows_owner_row_with_mute`. Fail first.
- [ ] Implement; `tests/cloud/test_cloud_static_css.py` must pass. Run `uv run pytest tests/cloud`. Commit `feat(cloud): alert history page, mute toggle, fleet counts`.

### Task 13: Admin SMTP status and test email

**Files:** Modify `packages/cloud/src/ice_cloud/admin.py`, `templates/admin.html`. Test `tests/cloud/test_admin.py`.

**Interfaces — consumes:** Task 3 `smtp_configured`, Task 7 `Mailer` on `app.state.mailer` (Task 10 sets it; this task may set it in its own test fixture until merge). **Produces:** admin page block "Email: configured (host)" or "Email: log-only"; `POST /admin/test-email` (`require_admin`, `require_htmx`) sends a fixed message to the admin's own email through `app.state.mailer`, returns an htmx fragment "Sent to <email>" or "Failed: <error>".

- [ ] Tests: `test_admin_shows_smtp_status_both_states`, `test_test_email_sends_to_admin_address`, `test_test_email_reports_failure`, `test_test_email_requires_admin_and_htmx`. Fail first.
- [ ] Implement. Run `uv run pytest tests/cloud/test_admin.py`. Commit `feat(cloud): admin SMTP status and test email`.

### Task 14: Docs

**Files:** `packages/cloud/README.md` ("Alerts" section per §9), `CLAUDE.md` Cloud section (one bullet: how alerts are derived, routed, cooled down, the SMTP env block, the edge fallback rule, contract 0.3.0), `docs/contracts/edge-cloud/CONTRACT.md` (verify 0.3.0 text matches as built), spec as-built notes if any task deviated.

- [ ] Write; run `uv run pytest tests/contracts tests/cloud/test_settings.py` (doc-pinning tests). Commit `docs: cloud alerts README, CLAUDE.md, contract notes`.

---

## Self-review

- Spec coverage: §2.1–2.4 → T5, T4, T9, T10; §3.1–3.5 → T6, T4, T8, T7, T3/T2; §4 → T1, T11; §5 → T12, T13; §6 → T4; §7 → T8/T9/T10 tests; §8 → every task's test list; §9 → T14.
- Type consistency: `AlertEvent` (T5) and `AlertEventRow` (T4) share fields; T8 passes `StoredEvent` (T4) to `recipients_for` (T6) and `render_email` (T7), both typed on the shared fields. `Recipient` exists in T4 and T6 with identical fields; T8 passes the store's rows to the router. `Delivery.status` strings: `pending`, `sent`, `failed`, `muted`.
- No code bodies by design (user plan-style rule).
