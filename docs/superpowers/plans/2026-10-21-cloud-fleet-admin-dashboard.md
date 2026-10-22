# Cloud Fleet Admin Dashboard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The platform admin's `/admin` becomes a fleet dashboard — machines, owners, unclaimed machines calling in, silent machines, faults, revenue, software — with drill-down lists behind every number.

**Architecture:** One pure function, `ice_cloud.fleet_view.build_fleet_view`, turns the rows and reports the cloud already stores into a `FleetView` of frozen dataclasses; thin routes in a new `ice_cloud/admin/` package load the inputs and render cloud templates that use the dashboard v2 shell. Three new `Repository` methods supply memberships, per-machine 24 h alert counts and fleet-wide recent events. No new wire data, no new tables.

**Tech Stack:** Python 3.12, FastAPI, Pydantic v2, SQLite (stdlib), Jinja2/HTMX, Tailwind v3.4.17 standalone CLI, pytest.

Spec: `docs/superpowers/specs/2026-10-21-cloud-fleet-admin-dashboard-design.md`. Section numbers (§) refer to it. Branch: `worktree-agent-isolated` (worktree at `.claude/worktrees/agent-isolated`), off `ops/review-fixes-2`.

## Global Constraints

- Package layers (import-linter): `ice_contracts` → `ice_common` → `ice_ui` → (`ice_edge` | `ice_cloud` | `ice_sims`). `ice_cloud` never imports `ice_edge`. Cloud tests import `ice_edge` only in `tests/cloud/test_ui_parity.py`.
- Plan style (user rule): tasks specify interfaces and tests, not full code bodies. Implementers write the code.
- Before every commit: `uv run ruff check --fix .`, then `uv run ruff format .`, then the task's tests. Always `uv run ruff`, never the global ruff. Do not chain shell commands with `&&`; one command per shell call.
- Commit trailer, exactly: `Assisted-by: Claude Sonnet 4.5 (Claude Code)` (substitute the model actually writing the commit); no `Co-Authored-By`.
- Private-access ratchet (`tests/meta/test_no_private_access.py`): new tests reach no `_private` attribute. `tests/meta/test_routes_shape.py` does not cover `ice_cloud`, but every cloud route module exposes a module-level `router`.
- Cloud templates (`packages/cloud/src/ice_cloud/templates/`) may use only classes present in the committed `packages/ui/src/ice_ui/static/app.css` (`tests/cloud/test_cloud_static_css.py`). After Task 1 the cloud templates are in the Tailwind content scan, so **any task that edits a cloud template rebuilds `app.css`** with the standalone binary and commits it. The binary is not in the worktree; call it by absolute path, from the worktree root:
  `D:\Users\Conrad\Documents\programming\ice-colder\.tailwind\tailwindcss.exe -c packages/ui/tailwind.config.js -i packages/ui/tailwind.input.css -o packages/ui/src/ice_ui/static/app.css --minify`
  Then run `uv run pytest tests/ui/test_static_css.py tests/cloud/test_cloud_static_css.py`.
- Every `/admin` route: `Depends(require_admin)` (404 for non-admins). Every mutating route: `Depends(require_htmx)`.
- Constants (exact): `STALE_FACTOR = 3` (moves to `fleet_view.py`), alert window 24 h, attention list cap 12, overview poll `every 60s`, fleet alerts page limit 100, owners panel on the overview shows 8.
- Times shown to the admin: ages via `ice_ui.filters.humanize_seconds`; absolute times `%Y-%m-%d %H:%M UTC`.
- Visual language: slate-50 page, white `rounded-xl border border-gray-200 shadow-sm` cards, `text-xs text-gray-400 uppercase tracking-wide` labels, `text-3xl font-bold` values, green-700 / amber-600 / red-600 / gray-900 for tones good / warn / bad / neutral, `touch-target` buttons. No JavaScript beyond HTMX; no inline `<script>`.
- No `TBD`/`TODO` in delivered code or docs.
- Tests never sleep on the wall clock; the cloud's clock is the injected `clock` fixture (`tests/cloud/conftest.py`).

## File map

| Path | Responsibility |
|---|---|
| `packages/ui/tailwind.config.js` | add cloud templates to `content` |
| `packages/ui/src/ice_ui/static/app.css` | rebuilt (committed) |
| `packages/cloud/src/ice_cloud/store/repository.py`, `store/sqlite.py` | `all_memberships`, `alert_counts_since`, `recent_alert_events` |
| `packages/cloud/src/ice_cloud/fleet_view.py` (new) | §4 pure view model |
| `packages/cloud/src/ice_cloud/portal.py` | import `STALE_FACTOR`/`is_stale` from `fleet_view`; admin link on `/fleet` |
| `packages/cloud/src/ice_cloud/admin/` (new package, replaces `admin.py`) | `__init__.py`, `common.py`, `overview.py`, `machines.py`, `owners.py`, `accounts.py`, `alerts.py` |
| `packages/cloud/src/ice_cloud/templates/admin.html` (rewritten), `admin_machines.html`, `admin_owners.html`, `admin_accounts.html`, `admin_alerts.html`, `partials/admin_overview.html`, `partials/admin_stat.html` (new); `fleet.html` | pages |
| `tests/cloud/test_fleet_view.py`, `tests/cloud/test_admin_dashboard.py` (new); `tests/cloud/test_admin.py`, `tests/cloud/test_repository.py` or the existing store test module | tests |
| `packages/cloud/README.md`, `CLAUDE.md` | docs |

## Order

Tasks run sequentially in one worktree. T1 → T2 → T3 → T4 → T5 → T6 → T7 → T8 → T9. T2 and T3 are independent of each other but T4 onward need both.

---

### Task 1: Cloud templates join the Tailwind content scan

**Files:** Modify `packages/ui/tailwind.config.js`; rebuild `packages/ui/src/ice_ui/static/app.css`. Tests: `tests/ui/test_static_css.py`, `tests/cloud/test_cloud_static_css.py`, `tests/ui/test_tailwind_config.py` if it exists (check with `ls tests/ui`).

**Interfaces — produces:** the rebuild mechanism every later template task uses (Global Constraints).

- [ ] Add `"./packages/cloud/src/ice_cloud/templates/**/*.html"` to `content` in `packages/ui/tailwind.config.js`, with a one-line comment: the portal serves this stylesheet to its own templates too (spec §6).
- [ ] Add a test to `tests/cloud/test_cloud_static_css.py`: `test_tailwind_scans_cloud_templates` reads `packages/ui/tailwind.config.js` (path via `tests.repo_root.REPO_ROOT`) and asserts the cloud glob is in it. Run it, see it fail, apply the config change, see it pass.
- [ ] Rebuild `app.css` with the binary (Global Constraints). Confirm `git diff --stat packages/ui/src/ice_ui/static/app.css` shows a change only if cloud templates already used a class the edge did not (it may be byte-identical; either is fine).
- [ ] Run `uv run pytest tests/ui tests/cloud/test_cloud_static_css.py tests/cloud/test_ui_parity.py`. Commit `build(ui): scan cloud templates for Tailwind classes`.

### Task 2: Repository additions

**Files:** Modify `packages/cloud/src/ice_cloud/store/repository.py` (Protocol), `packages/cloud/src/ice_cloud/store/sqlite.py`. Test: the existing cloud store test module (find with `grep -l "SqliteRepository" tests/cloud/*.py`; if several, use the one testing alert methods), or create `tests/cloud/test_repository_fleet.py`.

**Interfaces — produces:**
- `Repository.all_memberships() -> list[Membership]` — every row, ordered by `fingerprint, granted_at`.
- `Repository.alert_counts_since(since: datetime) -> dict[str, int]` — `{fingerprint: count}` of `raised` + `offline` events with `at >= since`; machines with zero events are absent.
- `Repository.recent_alert_events(*, limit: int) -> list[tuple[StoredEvent, Machine]]` — newest first (`at DESC, id DESC`), joined to `machines`.

- [ ] Tests (fail first): `test_all_memberships_lists_every_grant` (two machines, two techs, ordered), `test_alert_counts_since_groups_by_machine` (seed events via `record_alert_events`: two `raised` + one `cleared` + one `offline` on A, one `online` on B, one old `raised` on A before `since`; expect `{A: 3}`), `test_recent_alert_events_newest_first_with_machine` (events on two machines; assert order and that each tuple's `Machine.label` matches).
- [ ] Implement in `sqlite.py` only; add the three signatures with docstrings to the Protocol.
- [ ] Run the module. Commit `feat(cloud): repository fleet queries (memberships, alert counts, recent events)`.

### Task 3: `fleet_view.py` pure model

**Files:** Create `packages/cloud/src/ice_cloud/fleet_view.py`; modify `packages/cloud/src/ice_cloud/portal.py` (import `STALE_FACTOR`, replace `_is_stale`'s body with `is_stale(report, now)`). Test: `tests/cloud/test_fleet_view.py` (new).

**Interfaces — produces** (all frozen dataclasses unless noted; spec §4 is normative, repeated here for the implementer):

```python
STALE_FACTOR = 3
ATTENTION_LIMIT = 12
GATE_RANK = {"safety": 0, "fulfillment": 1, "alert": 2}

class MachineStatus(StrEnum): online = "online"; silent = "silent"; never_reported = "never_reported"
Tone = Literal["neutral", "good", "warn", "bad"]

def is_stale(report: MachineReport, now: datetime) -> bool
def classify(report: MachineReport | None, now: datetime) -> MachineStatus

@dataclass(frozen=True) class MachineRow: fingerprint, short, label, owner_id, owner_email, status, age_seconds, age_text, interval_seconds, fault_count, top_fault, has_safety_fault, payment_enabled, payment_reason, edge_build, ui_mismatch, alerts_24h, claim_open, registered_at, last_seen_at, vmc_missing   # types per spec §4
@dataclass(frozen=True) class Headline: label: str; value: int | str; sub: str; url: str; tone: Tone
@dataclass(frozen=True) class StatusMix: online_ok, online_faulted, silent, never_reported: int; online_ok_pct, online_faulted_pct, silent_pct, never_reported_pct: int
@dataclass(frozen=True) class BuildRow: edge_build: str; count: int; online: int
@dataclass(frozen=True) class OwnerRow: account_id, email, name, suspended, machines, online, silent, never_reported, faulted, techs, alerts_24h, url
@dataclass(frozen=True) class AttentionRow: row: MachineRow; reason: str; tone: Tone
@dataclass(frozen=True) class FleetView: headlines, secondary, mix, attention, builds, owners, rows   # tuples per spec §4

def build_fleet_view(*, machines, accounts, memberships, reports, alert_counts, installed_digest, now) -> FleetView
def filter_rows(rows, *, status: MachineStatus | None = None, claimed: bool | None = None, faulted: bool | None = None, payment: Literal["blocked"] | None = None, owner: str | None = None) -> list[MachineRow]
def percentages(counts: Sequence[int]) -> list[int]   # largest-remainder; all zeros when sum is 0
```

Rules the implementer must honour (from spec §2 and §4): `claim_open` = `claim_code_hash` set and (`claim_code_expires_at` is None or `> now`); `top_fault` = min by `(GATE_RANK[gate], code)`; `payment_reason` = first of `health.payment_reasons` or None; headline order and sub-lines exactly as §2.2/§2.3 (labels: "Machines", "Owners", "Unclaimed calling in", "Silent"; secondary: "Faulted", "Payment blocked", "Alerts (24 h)", "Revenue (24 h)", "Uptime (24 h)", "Software"); headline URLs: `/admin/machines`, `/admin/owners`, `/admin/machines?claimed=no&status=online`, `/admin/machines?status=silent`; secondary URLs: `/admin/machines?faulted=yes`, `/admin/machines?payment=blocked`, `/admin/alerts`, `/admin/machines?status=online` (revenue and uptime), `/admin/machines` (software); revenue value is the string `"$%.2f"`, uptime `"%.1f%%"` or `"—"`; `rows` sort key `(status rank silent=0, never=1, online=2, label, fingerprint)`; owners sort `(-machines, email)`; builds sort `(-count, edge_build)`; attention per §2.4 with reasons worded as there, `tone` `bad` for rules 1–2, `warn` for 3–4.

- [ ] Write `tests/cloud/test_fleet_view.py` using `tests.cloud.reports.make_report` and hand-built `Machine`/`Account`/`Membership` dataclasses (helper `_machine(fp, label, owner_id=None, **kw)` in the test module). Tests: `test_classify_boundary_exactly_three_intervals_is_online`, `test_classify_one_second_past_is_silent`, `test_classify_none_is_never_reported`, `test_headlines_for_small_fleet` (2 owners, 1 unclaimed online, 1 claimed silent, 1 claimed never reported, 1 claimed online faulted → Machines 5 "4 claimed · 1 unclaimed", Owners 2, Unclaimed calling in 1 tone bad, Silent 1 tone bad with sub "1 never reported"), `test_secondary_revenue_excludes_silent`, `test_status_mix_percentages_sum_to_100_and_zero_for_empty`, `test_percentages_largest_remainder` (`[1,1,1]` → sums to 100), `test_attention_order_and_uniqueness` (one machine both silent and unclaimed appears once under silent), `test_attention_capped_at_12`, `test_owner_rows_count_distinct_techs`, `test_filter_rows_each_filter_and_conjunction`, `test_rows_sorted_silent_first`, `test_portal_is_stale_uses_fleet_view` (import `ice_cloud.portal.STALE_FACTOR is ice_cloud.fleet_view.STALE_FACTOR`). Fail first.
- [ ] Implement `fleet_view.py`; wire `portal.py` to import `STALE_FACTOR` and call `is_stale`.
- [ ] Run `uv run pytest tests/cloud/test_fleet_view.py tests/cloud/test_portal.py tests/cloud/test_ui_parity.py` (portal test module name: `ls tests/cloud | grep portal`). Commit `feat(cloud): fleet_view pure model for the admin dashboard`.

### Task 4: `admin/` package, machines and accounts levels

**Files:** Delete `packages/cloud/src/ice_cloud/admin.py`; create `packages/cloud/src/ice_cloud/admin/__init__.py`, `common.py`, `machines.py`, `accounts.py`, `overview.py` (stub landing, replaced in Task 5); create templates `admin_machines.html`, `admin_accounts.html`; rewrite `admin.html` as a minimal page with links to the two levels (Task 5 replaces it). Modify `tests/cloud/test_admin.py`. Test: `tests/cloud/test_admin.py`, new `tests/cloud/test_admin_dashboard.py` (started here, extended in later tasks).

**Interfaces — produces:**
- `ice_cloud.admin.router` (unchanged import for `app.py`), `LEVEL_ADMIN`, `LEVEL_ADMIN_MACHINES = Level("Machines", "/admin/machines", parent=LEVEL_ADMIN)`, `LEVEL_ADMIN_OWNERS`, `LEVEL_ADMIN_ACCOUNTS`, `LEVEL_ADMIN_ALERTS` (titles "Owners", "Accounts", "Alerts"; URLs `/admin/owners`, `/admin/accounts`, `/admin/alerts`), all defined in `admin/__init__.py`... **but** sub-modules cannot import from the package `__init__` without a cycle, so define the levels in `admin/common.py` and re-export them from `__init__.py`.
- `admin/common.py`: `admin_context(request, account, level, **extra) -> dict` (today's `_ctx`), `templates(request)`, `machine_or_404(request, short) -> Machine`, `async load_fleet_view(request) -> FleetView` (gathers `all_machines`, `all_accounts`, `all_memberships`, `alert_counts_since(now - 24h)`, `store.machines()`; each via `asyncio.to_thread`; builds the view with `registry.installed_digest`).
- `admin/machines.py`: `GET /admin/machines` with query params `status`, `claimed`, `faulted`, `payment`, `owner` (all `str | None`, validated against the §3 vocabularies; unknown → ignored); renders `admin_machines.html` with `rows`, `filters` (the pill list: `[{"label","url","active"}]`), `heading` ("Machines", or "Machines — <owner email>" for `owner=`), `count`; breadcrumb `Level.child(LEVEL_ADMIN_MACHINES, title, url)` for a filtered view, `LEVEL_ADMIN_MACHINES` itself for All. Unbind confirm/POST move here verbatim.
- `admin/accounts.py`: `GET /admin/accounts` renders `admin_accounts.html` (people list + SMTP line + test email button); suspend/unsuspend/test-email move here verbatim.
- `admin/overview.py`: `GET /admin` renders `admin.html` with `admin_context(..., LEVEL_ADMIN)` — in this task only links to `/admin/machines` and `/admin/accounts`.

Template contract for `admin_machines.html` (Task 6 restyles it; this task gets the structure right): a filter pill bar, then a table with the §5.2 columns using `MachineRow` fields (`status` badge: Online green, Silent red, Never reported gray), Unbind cell including `partials/admin_unbind.html` exactly as today's `admin.html` does, and the §5.3 links. Status badge text: "Online", "Silent", "Never reported".

- [ ] Update `tests/cloud/test_admin.py`: every `cloud.get("/admin")` that asserts on machine labels/"unclaimed"/"/unbind" becomes `cloud.get("/admin/machines")`; any assertion on account emails/Suspend becomes `/admin/accounts`. Keep the non-admin 404 test and add `/admin/machines`, `/admin/accounts` to its asserted-404 list. Create `tests/cloud/test_admin_dashboard.py` with: `test_admin_landing_links_to_levels` (admin GET `/admin` 200 and contains `href="/admin/machines"`), `test_machines_filter_status_silent` (push two reports via `push`, advance `clock` so one is silent by lowering its `interval`: `make_report(interval=10)` then `clock.advance(31)` makes it silent while a 60 s one is online — assert only the silent label appears under `?status=silent`), `test_machines_filter_claimed_no`, `test_machines_filter_unknown_value_ignored` (`?status=bogus` == `/admin/machines` row count), `test_machines_filter_owner_names_owner` (heading contains the owner's email), `test_machines_breadcrumb_for_filter` (page contains "Silent" as the current crumb: `aria-current="page">Silent<`). Fail first.
- [ ] Build the package; delete `admin.py`; `app.py` needs no change. Templates per the contract. Rebuild `app.css` (Global Constraints).
- [ ] Run `uv run pytest tests/cloud tests/ui/test_static_css.py`. Then `uv run lint-imports`. Commit `refactor(cloud): admin package; machines and accounts become their own levels`.

### Task 5: Overview dashboard

**Files:** Modify `packages/cloud/src/ice_cloud/admin/overview.py`; rewrite `packages/cloud/src/ice_cloud/templates/admin.html`; create `templates/partials/admin_overview.html`, `templates/partials/admin_stat.html`. Test: `tests/cloud/test_admin_dashboard.py`.

**Interfaces — consumes:** `load_fleet_view`, `FleetView`, `Headline`, `StatusMix`, `AttentionRow`, `OwnerRow`, `BuildRow`. **Produces:** `GET /admin` (full page) and `GET /admin/overview` (fragment) sharing one context builder `_overview_context(request, account, view) -> dict` with keys `view`, `computed_at` (`"%H:%M:%S UTC"` of `state.now()`), `owners_panel` (`view.owners[:8]`), `machine_count`.

Template contract (spec §5.1): `admin.html` extends `base.html`, title "Fleet · Admin", body is exactly `<div id="fleet-overview" hx-get="/admin/overview" hx-trigger="every 60s" hx-swap="outerHTML" hx-target="this">{% include "partials/admin_overview.html" %}</div>`; the fragment route returns the partial wrapped in that same `<div ...>` so an outerHTML swap keeps polling (render a tiny wrapper template or pass `wrap=True` — the implementer's choice, but the fragment response must start with `<div id="fleet-overview"` and contain no `<html`). `partials/admin_stat.html` takes `stat` (a `Headline`) and renders `<a href="{{ stat.url }}" class="block bg-white rounded-xl border border-gray-200 shadow-sm p-5 hover:bg-slate-50">` with label, value coloured by tone (`good` → `text-green-700`, `warn` → `text-amber-600`, `bad` → `text-red-600`, `neutral` → `text-gray-900`), sub-line. Status strip segments: `<div class="h-3 bg-green-500" style="width: {{ view.mix.online_ok_pct }}%"></div>` etc., skipped when 0; legend links to the filtered lists. Attention rows: tone dot (`bg-red-500` / `bg-amber-500`), label (link to `/m/{short}`), owner or "unclaimed", reason, `age_text`. Owners panel and Software panel per §5.1 item 5 (software count bar is `style="width: N%"` of the largest count). Footer per §5.1 item 6.

- [ ] Tests (fail first): `test_overview_shows_four_headlines` (seed: two owners each with one online machine, one unclaimed online, one claimed silent, one never-reported registered machine; assert "Machines" with "5", "Owners" with "2", "Unclaimed calling in" with "1", "Silent" with "1"), `test_overview_attention_lists_silent_before_unclaimed` (order of labels in `r.text`), `test_overview_fragment_is_bare_div` (`/admin/overview` starts with `<div id="fleet-overview"`, no `<html`, same headline numbers), `test_overview_non_admin_404` (both URLs), `test_overview_empty_fleet` (no machines → "Nothing needs attention." and a `0` Machines headline, no exception), `test_overview_software_panel_counts_builds` (two reports with `edge_build="aaa"`, one `"bbb"` → "aaa" appears with "2").
- [ ] Implement route + templates. Rebuild `app.css`.
- [ ] Run `uv run pytest tests/cloud tests/ui/test_static_css.py`. Commit `feat(cloud): fleet overview dashboard at /admin`.

### Task 6: Machines table polish and owners level

**Files:** Modify `templates/admin_machines.html`; create `packages/cloud/src/ice_cloud/admin/owners.py`, `templates/admin_owners.html`; register the router in `admin/__init__.py`. Test: `tests/cloud/test_admin_dashboard.py`.

**Interfaces — consumes:** `FleetView.owners`, `OwnerRow`. **Produces:** `GET /admin/owners` rendering `admin_owners.html` with `owners` (all `OwnerRow`s), `idle_accounts` (accounts owning nothing: `len(accounts) - len(owners)`).

Template contract: `admin_machines.html` gets the full §5.2 column set with faults coloured by gate (`safety` red-600, `fulfillment` amber-600, `alert` gray-600), "UI differs" marker, "claim open" marker, `vmc_missing` marker ("no VMC"), Payment On/Off/—, and "No machines match." empty state inside the card. `admin_owners.html` per §5.4 with the Machines count linking to `row.url` and a footer "N owners · M accounts own nothing".

- [ ] Tests (fail first): `test_owners_page_counts` (owner A with 2 machines, one silent; owner B with 1; a tech on A's machine → A's row shows machines 2, silent 1, techs 1; order A then B), `test_owners_footer_counts_idle_accounts` (the tech owns nothing → "1 account" or "1 accounts own nothing" — assert the number and the phrase "own nothing"), `test_owners_non_admin_404`, `test_machines_table_marks_top_fault_code` (faulted report → "ICE-301" in the row), `test_machines_table_payment_off` (report with `health.payment_enabled=False` → "Off").
- [ ] Implement. Rebuild `app.css`.
- [ ] Run `uv run pytest tests/cloud tests/ui/test_static_css.py`. Commit `feat(cloud): admin owners level and machines table detail`.

### Task 7: Fleet alerts level and the `/fleet` link

**Files:** Create `packages/cloud/src/ice_cloud/admin/alerts.py`, `templates/admin_alerts.html`; register in `admin/__init__.py`; modify `packages/cloud/src/ice_cloud/portal.py` (`fleet` route passes `is_admin=...`) and `templates/fleet.html`. Test: `tests/cloud/test_admin_dashboard.py`.

**Interfaces — consumes:** `Repository.recent_alert_events(limit=100)`. **Produces:** `GET /admin/alerts` rendering `admin_alerts.html` with `events`: list of dicts `{"at", "kind", "code", "description", "product", "label", "short", "alerts_url"}`.

Template contract (§5.5): kind badge colours `raised` → `bg-red-50 text-red-600`, `cleared` → `bg-green-50 text-green-700`, `offline` → `bg-amber-50 text-amber-700`, `online` → `bg-gray-100 text-gray-600`; machine label links to `/machines/{short}/alerts`; "No alerts yet." empty state. `fleet.html`: when `is_admin` is true, render `<p class="mt-3 text-sm"><a href="/admin" class="text-gray-600">Fleet dashboard</a></p>` above the "Claim a machine" line.

- [ ] Tests (fail first): `test_fleet_alerts_newest_first_with_machine_link` (two claimed machines, push a faulted report then a clean one on A — gives `raised` then `cleared`; push faulted on B; advance clock between pushes so `at` differs; assert the first event rendered is B's and each row links to `/machines/<short>/alerts`), `test_fleet_alerts_empty`, `test_fleet_alerts_non_admin_404`, `test_fleet_page_links_dashboard_for_admin_only` (admin sees `href="/admin"`, an owner does not).
- [ ] Implement. Rebuild `app.css`.
- [ ] Run `uv run pytest tests/cloud tests/ui/test_static_css.py`. Commit `feat(cloud): fleet-wide alerts level; dashboard link on /fleet`.

### Task 8: Docs

**Files:** Modify `packages/cloud/README.md` ("Platform admin" section), `CLAUDE.md` (Cloud section, one bullet), the spec (as-built notes if any deviation happened in Tasks 1–7; otherwise none).

- [ ] README: replace the `/admin` sentence with: the dashboard and its four headline counts and their definitions (§2.2), the drill-down levels (`/admin/machines` with its filters, `/admin/owners`, `/admin/accounts`, `/admin/alerts`), where Unbind and Suspend now live, the 60 s refresh, and that `/fleet` shows admins a "Fleet dashboard" link.
- [ ] `CLAUDE.md` Cloud bullets: add `- **Admin dashboard.** ...` (3–4 sentences: `/admin` is the fleet overview built by the pure `ice_cloud.fleet_view.build_fleet_view`; status vocabulary online/silent/never_reported with `STALE_FACTOR = 3` shared with the portal; the sub-levels; cloud templates are in the Tailwind content scan so editing one means rebuilding `app.css`).
- [ ] Run `uv run pytest tests/cloud tests/meta -q`. Commit `docs(cloud): admin fleet dashboard`.

### Task 9: Whole-branch verification

- [ ] `uv run ruff check .`, `uv run ruff format --check .`, `uv run lint-imports`, `uv run python scripts/check_package_isolation.py`.
- [ ] Full suite: `uv run pytest -q` — expect 0 failures (baseline before this branch: 3877 passed, 61 skipped).
- [ ] Confirm `git status` is clean and `app.css` is committed with the last template change (`git log -1 --stat -- packages/ui/src/ice_ui/static/app.css` lands after the last template commit, or the final rebuild is byte-identical).
- [ ] Hand off to the finishing-a-development-branch skill: push the branch and open the PR against `main` (body ends with the attribution line from the user's CLAUDE.md).
