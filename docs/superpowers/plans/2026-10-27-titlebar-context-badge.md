# Titlebar Context Badge Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** One bar badge that names the current context (machine name on the edge and on a cloud remote view, "Cloud" on fleet-level pages), replaces the bottom remote banner, and makes `/fleet` most-recently-viewed-first.

**Architecture:** `base.html`'s OOB `#bar` renders a new `badge` template variable (and a cloud-only `ui_notice` strip); the edge fills it in `template_context`, the cloud in `portal._context` / `admin.common.admin_context`. The cloud gains a `machine_views` table for recency.

**Tech Stack:** Jinja2/HTMX templates in `ice_ui`, FastAPI route context on both edge and cloud, sqlite migrations in `ice_cloud.store`, Tailwind standalone CLI for `app.css`.

**Spec:** `docs/superpowers/specs/2026-10-27-titlebar-context-badge-design.md` — read it first; it is the authority on behaviour.

## Global Constraints

- Plan style (user preference): tasks specify interfaces and tests, not full code bodies — implementers write the code.
- Never chain shell commands with `&&`; separate Bash/PowerShell calls.
- Python: always `uv run ...` (`uv run pytest`, `uv run ruff check --fix .` then `uv run ruff format .`). Never bare `ruff` (global one is older).
- `tests/meta/test_no_private_access.py`: tests must not touch `_private` attributes; use public seams.
- `tests/ui/test_static_css.py` fails CI if a template uses a class missing from the committed `packages/ui/src/ice_ui/static/app.css`. Rebuild with: `.tailwind/tailwindcss.exe -c packages/ui/tailwind.config.js -i packages/ui/tailwind.input.css -o packages/ui/src/ice_ui/static/app.css --minify` and commit the result.
- Cloud sqlite schema changes are APPENDED to `MIGRATIONS` in `packages/cloud/src/ice_cloud/store/sqlite.py`, never edited in place.
- Any anchor to an external origin in a template must carry `hx-boost="false"`.
- `tests/cloud/test_ui_parity.py` compares `<main>` only; the badge lives in `#bar` (outside `<main>`) and must stay there.
- Commit messages end with: `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`.
- No contract version bumps anywhere in this plan.

---

### Task 1: Badge + notice markup in `base.html`; bottom banner removed

**Files:**
- Modify: `packages/ui/src/ice_ui/templates/base.html` (the `#bar` header, lines ~34–85)
- Delete: `packages/ui/src/ice_ui/templates/partials/remote_banner.html`
- Delete: `tests/ui/test_remote_banner.py`
- Create: `tests/ui/test_badge.py`
- Modify (rebuild + commit): `packages/ui/src/ice_ui/static/app.css`

**Interfaces:**
- Consumes: nothing from other tasks.
- Produces: two template variables every later task relies on, rendered inside the `hx-swap-oob` `#bar` header:
  - `badge`: object with attributes `label: str`, `url: str | None`, `tone: str` (`"local" | "remote" | "offline" | "cloud"`), `title: str | None`, `external: bool`. Rendered as `<a id="context-badge" href=...>` when `url` is set (plus `hx-boost="false"` when `external` is true, and `title` attribute when `title` is set), else `<span id="context-badge">`. Absent `badge` → no element with id `context-badge` at all. Placement: between the health-pill anchor and the `hat_switch` link, same chip styling family as the role chip (`inline-flex items-center rounded-full ... text-xs font-semibold px-2 py-1`). Tone → background: `local` slate (like the role chip, `bg-slate-600`), `remote` and `cloud` sky (`bg-sky-700`), `offline` amber (`bg-amber-500 text-amber-950` or similar readable pair).
  - `ui_notice`: plain string. When set, render `<div id="ui-notice">{{ ui_notice }}</div>` inside `#bar`, absolutely positioned just below the h-14 bar (e.g. `absolute left-0 right-0 top-14 z-10`, amber strip: `bg-amber-50 text-amber-900 border-b border-amber-200 px-4 py-1 text-sm`) — the header needs `relative` added for that. Absent → no element.
- The `{% if remote %}{% include "partials/remote_banner.html" %}{% endif %}` include is removed along with the partial.

- [ ] **Step 1: Write the failing tests** — `tests/ui/test_badge.py`, modeled on the deleted `tests/ui/test_remote_banner.py` (same `_render` helper via `ice_ui.templating.templates`, `SimpleNamespace` for `badge`). Tests:
  - `test_no_badge_without_variable` — `context-badge` absent when `badge` not passed.
  - `test_badge_is_inside_the_oob_swapped_bar` — reuse the `tests/dom_utils` assertions from the old banner test: `#context-badge` is within `header`, `#bar` has `hx-swap-oob="true"`, exactly one occurrence.
  - `test_badge_with_url_renders_anchor` — `badge(label="Lobby Ice", url="/fleet", tone="remote", title=None, external=False)` → `<a`, `href="/fleet"`, label text, no `hx-boost` attribute.
  - `test_badge_external_link_disables_boost` — `external=True, url="https://cloud.example/m/abcd-..."` → anchor carries `hx-boost="false"`.
  - `test_badge_without_url_is_plain_span` — `url=None` → `<span`, no `href`.
  - `test_badge_tones` — parametrize tone → expected class fragment (slate/sky/amber).
  - `test_badge_title_attribute` — `title="last report from 2026-10-13 14:05 (12m ago)"` appears as the element's `title` attribute.
  - `test_ui_notice_strip` — `ui_notice="UI version mismatch: machine a1b2c3d4, portal e5f6a7b8"` → `#ui-notice` inside the header with that text; absent otherwise.
  - `test_remote_banner_is_gone` — rendering with a legacy `remote=SimpleNamespace(...)` context key produces no `remote-banner` id (guards against resurrection).
  - Keep the old file's sandboxed-environment parametrization (`make_templates(TEMPLATES_DIR, sandboxed=True)`) for one badge render.
- [ ] **Step 2: Run them, verify failure** — `uv run pytest tests/ui/test_badge.py -q` (fails: no badge markup yet).
- [ ] **Step 3: Implement** — edit `base.html` per Interfaces; delete `partials/remote_banner.html` and `tests/ui/test_remote_banner.py`.
- [ ] **Step 4: Rebuild `app.css`** — run the Tailwind command from Global Constraints; then `uv run pytest tests/ui/ -q` (includes `test_static_css.py`).
- [ ] **Step 5: Commit** — `feat(ui): context badge and ui-notice strip in the bar, remote banner removed`.

### Task 2: Edge fills the badge in `template_context`

**Files:**
- Modify: `packages/edge/src/ice_edge/web/context.py` (`template_context`, line ~234)
- Create: `tests/edge/web/test_context_badge.py`

**Interfaces:**
- Consumes: Task 1's `badge` variable shape; existing globals `context.config` (a `ConfigModel`) and `context.machine_identity` (`MachineIdentity | None`, has `.short_fingerprint`).
- Produces: every edge page rendered through `template_context` carries `badge` with:
  - `label` = `config.physical.common_name` or (falsy) `config.machine_id`;
  - `url` = `f"{config.cloud.url.rstrip('/')}/m/{machine_identity.short_fingerprint}"` when `config.cloud.enabled` and `machine_identity` is not None, else `None`;
  - `tone="local"`, `title=None`, `external=True` (only meaningful when `url` set).
  - When `context.config` is None (bare tests), no `badge` key — pages must still render.
  - Implement as a small module-level helper `def context_badge() -> SimpleNamespace | None` (or dataclass) in `context.py` so tests can call it directly, with `template_context` injecting it (an explicit `badge=` in `**extra` wins).
- Login/setup/enroll pages override `bar_variant` and never pass `current_user`; the badge renders from `template_context` regardless — that is fine on the edge (these pages replace the whole bar block... **verify**: if those templates still render the badge outside `bar_variant`, gate the badge render in `base.html` on nothing, but note the badge is placed *inside* the `bar_variant` block in Task 1, so overriding pages drop it automatically — confirm with one test).

- [ ] **Step 1: Failing tests** — `tests/edge/web/test_context_badge.py` using the existing edge web-test fixtures (see neighbouring tests in `tests/edge/web/` for the client/context setup; use `tests/route_context.py`-safe patterns — set globals via public `set_*` setters only):
  - `test_badge_label_prefers_common_name` and `test_badge_falls_back_to_machine_id` (call `context_badge()` directly with config variants).
  - `test_badge_links_to_cloud_when_enabled_and_identity` — url is `{cloud.url}/m/{short_fingerprint}`, `external` true.
  - `test_badge_not_clickable_when_cloud_disabled` and `test_badge_not_clickable_without_identity` — `url is None`.
  - `test_home_page_renders_badge` — GET `/` through the test client as a signed-in user: response contains `id="context-badge"` and the machine name.
  - `test_login_page_has_no_badge` — GET `/login` (or the setup-mode page): no `context-badge` (it lives in `bar_variant`, which login overrides).
- [ ] **Step 2: Run, verify failure** — `uv run pytest tests/edge/web/test_context_badge.py -q`.
- [ ] **Step 3: Implement** `context_badge()` + injection in `template_context`.
- [ ] **Step 4: Run** the new file plus `uv run pytest tests/edge/web -q` for regressions.
- [ ] **Step 5: Commit** — `feat(edge): bar badge names the machine and deep-links to the cloud portal`.

### Task 3: Cloud fills the badge, drops `remote`, passes `ui_notice`

**Files:**
- Modify: `packages/cloud/src/ice_cloud/portal.py` (`_remote` → badge/notice builders, `_context` lines ~104–148)
- Modify: `packages/cloud/src/ice_cloud/admin/common.py` (`admin_context`, line ~26)
- Modify: `tests/cloud/test_portal.py` (lines 247 and 332 assert "Remote view"; rewrite against the badge)
- Test: extend `tests/cloud/test_portal.py` (or a new `tests/cloud/test_badge_cloud.py` if cleaner)

**Interfaces:**
- Consumes: Task 1's `badge`/`ui_notice` shapes; existing `_remote` inputs (`report.common_name`, `report.machine_id`, `_is_stale`, `humanize_seconds`, `_age_seconds`, `ui.mismatch`, digests).
- Produces, in `_context(...)`:
  - machine selected (`machine is not None`): `badge.label` = `report.common_name or report.machine_id` (fall back to `machine.label` when `report is None`, e.g. the no-report page); `url="/fleet"`; `tone="offline"` with label suffix `" — offline"` and `title=f"last report from {local} ({age} ago)"` when the report is stale, else `tone="remote"`, `title=None`; `external=False`.
  - no machine: `badge = SimpleNamespace(label="Cloud", url="/fleet", tone="cloud", title=None, external=False)`.
  - `ui_notice = f"UI version mismatch: machine {report_digest[:8]}, portal {portal_digest[:8]}"` only when a machine is selected and `ui.mismatch`; never otherwise.
  - The `remote` context key and the banner-shaped parts of `_remote` are deleted (keep whatever `_is_stale`/age helpers other callers use). The `banner=` keyword of `_context`: repurpose or drop — its only job was the banner; the badge should render on every `_context` page.
  - `admin_context(...)` additionally sets the same "Cloud" badge (`label="Cloud"`, `url="/fleet"`, `tone="cloud"`).
  - The login page renders no badge (it doesn't go through `_context`; verify and leave as is).
- Produces for Task 5: cloud pages where `#context-badge` reads "Cloud" on `/fleet` and the machine name on `/` after selection.

- [ ] **Step 1: Failing tests** — using the existing `tests/cloud/test_portal.py` fixtures (`make_settings`, `make_report`, claim/select flow):
  - `test_machine_pages_show_machine_badge` — after select, GET `/health/faults`: `id="context-badge"`, machine name (`common_name` when set, else `machine_id`), `href="/fleet"`, no "Remote view" text anywhere.
  - `test_stale_report_turns_badge_offline` — stale report → label contains "— offline", amber tone class, `title` contains "last report from".
  - `test_fleet_page_shows_cloud_badge` — GET `/fleet`: badge label "Cloud", href `/fleet`.
  - `test_admin_page_shows_cloud_badge` — GET `/admin` as admin: badge "Cloud".
  - `test_ui_mismatch_notice` — mismatching bundle digest → `#ui-notice` with "UI version mismatch: machine ..., portal ..."; absent on a matching digest. (Rework the old line-332 sandbox assertion to this.)
  - `test_no_remote_banner_markup` — no `remote-banner` id on any of the above pages.
- [ ] **Step 2: Run, verify failure** — `uv run pytest tests/cloud/test_portal.py -q`.
- [ ] **Step 3: Implement** in `portal.py` + `admin/common.py`.
- [ ] **Step 4: Run** `uv run pytest tests/cloud -q` (parity test included) — all green.
- [ ] **Step 5: Commit** — `feat(cloud): bar badge replaces the remote banner; mismatch becomes a bar notice`.

### Task 4: `/fleet` recency — `machine_views` table, upserts, ordering, cleanup

**Files:**
- Modify: `packages/cloud/src/ice_cloud/store/sqlite.py` (append `MIGRATIONS` entry; `_ALERT_DELETES`-adjacent cleanup; new methods near `machines_for_account`, line ~587)
- Modify: `packages/cloud/src/ice_cloud/store/repository.py` (protocol, near `machines_for_account`, line ~221)
- Modify: `packages/cloud/src/ice_cloud/portal.py` (`select_machine` line ~233, `deep_link` line ~239, `fleet` line ~179)
- Test: the existing repository test module under `tests/cloud/` (find it with `grep -l machines_for_account tests/cloud`) + `tests/cloud/test_portal.py`

**Interfaces:**
- Consumes: nothing from Tasks 1–3 (independent; only shares `portal.py`, apply after Task 3 to avoid conflicts).
- Produces:
  - Migration (appended): `CREATE TABLE machine_views (account_id TEXT NOT NULL, fingerprint TEXT NOT NULL, viewed_at TEXT NOT NULL, PRIMARY KEY (account_id, fingerprint))`.
  - `Repository.record_machine_view(self, account_id: str, fingerprint: str, at: datetime) -> None` — upsert (`INSERT ... ON CONFLICT(account_id, fingerprint) DO UPDATE SET viewed_at = excluded.viewed_at`).
  - `Repository.machine_views_for_account(self, account_id: str) -> dict[str, datetime]` — fingerprint → viewed_at.
  - Cleanup: add `"DELETE FROM machine_views WHERE fingerprint = ?"` to `_ALERT_DELETES` (it already runs on owner-change claim, `unbind_machine`, and `_delete_machines`; the tuple name is historic — add a comment, don't rename).
  - `select_machine` and `deep_link` call `await asyncio.to_thread(state.repo.record_machine_view, account.id, machine.fingerprint, state.now())` (the dependency tuple already yields `account`).
  - `fleet` sorts its `rows` most-recently-viewed first, never-viewed after, stable by label within each group: fetch the views dict once, sort `entries` by `(views.get(fp) is None, -timestamp-or-0, label)` — express it as `sorted(entries, key=...)` with `viewed_at` descending; keep the existing per-row fields untouched.
- Produces for Task 5: selecting machine B then loading `/fleet` lists B first.

- [ ] **Step 1: Failing repository tests** — in the existing repo test module: upsert overwrites `viewed_at`; `machine_views_for_account` returns only that account's rows; `unbind_machine` and an owner-changing `claim_machine` delete the machine's view rows; re-claim by the same owner keeps them (rides on the existing "`_ALERT_DELETES` only when owner changes" behaviour — mirror the existing alert-state tests).
- [ ] **Step 2: Failing route tests** — in `test_portal.py`: `test_fleet_orders_most_recent_first` (claim two machines, POST `/fleet/select/{B}`, GET `/fleet`, assert B's label appears before A's); `test_deep_link_records_view` (GET `/m/{short}` then `/fleet` shows that machine first); never-viewed machines keep label order after the viewed ones.
- [ ] **Step 3: Run, verify failures** — `uv run pytest tests/cloud -q -k "machine_view or fleet_orders or deep_link_records"`.
- [ ] **Step 4: Implement** migration + repo methods + protocol entries + portal hooks + sort.
- [ ] **Step 5: Run** `uv run pytest tests/cloud -q` — green.
- [ ] **Step 6: Commit** — `feat(cloud): /fleet lists most-recently-viewed machines first`.

### Task 5: Browser test repurpose, docs, full-suite verification

**Files:**
- Rename/modify: `tests/cloud/test_banner_boosted_browser.py` → `tests/cloud/test_badge_boosted_browser.py`
- Rename/modify: `tests/browser/cloud_banner_check.mjs` → `tests/browser/cloud_badge_check.mjs`
- Modify: `CLAUDE.md` (browser-test list in Commands + the `tests/cloud/test_banner_boosted_browser.py` description; the Portal bullet's "remote banner" mentions; Removed Routes untouched)
- Modify (if it names the file): `.github/workflows/ci.yml` (it doesn't per current grep — verify) and `tests/skip_policy.py` / `tests/run_skip_guard.py` only if they reference the filename (verify with grep)

**Interfaces:**
- Consumes: Tasks 1, 3, 4 (live cloud server now renders badges).
- Produces: the five CI-mandatory browser tests stay five, with the renamed one asserting: across the same boosted navigation steps (fleet → select → /health → /health/faults → home), `#context-badge` appears **exactly once** per page, reads "Cloud" on the fleet step and the machine label (`m-0042`) on machine steps, and no `#remote-banner` exists. The `.mjs` checker keeps the same harness contract (`run_node_check(name, server)`, JSON `{ok, steps:[{label,...}]}`) — adapt the existing script's DOM queries from `#remote-banner` to `#context-badge` + text content per step.

- [ ] **Step 1: Adapt the checker + test** (rename both files; update docstring, assertions, step labels unchanged).
- [ ] **Step 2: Run locally** — `$env:ICE_COLDER_BROWSER_TESTS=1; uv run pytest tests/cloud/test_badge_boosted_browser.py -q` (needs Chrome + node; if absent locally, state so and rely on CI — do not fake a pass).
- [ ] **Step 3: Grep for stragglers** — `grep -rn "remote_banner\|remote-banner\|Remote view\|banner_boosted\|cloud_banner_check" packages tests CLAUDE.md .github` → fix every hit (CLAUDE.md wording included).
- [ ] **Step 4: Full verification** — `uv run ruff check --fix .` then `uv run ruff format .`; `uv run pytest -q`; `uv run lint-imports`; `uv run python scripts/check_package_isolation.py`.
- [ ] **Step 5: Commit** — `test(cloud): boosted-nav browser check follows the badge; docs updated`.
