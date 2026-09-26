# Dashboard v2 Shell Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking. Dispatch by **wave** — see "Execution schedule" below; it is not a flat task list.

**Goal:** Turn the single-page tabbed dashboard into a touch-first shell where every level has a real URL, a Back that goes to its parent, and a live health pill — working fully offline on a 7-inch tablet.

**Architecture:** One `base.html` renders a 56 px bar; every level extends it and declares a `Level` (title, parent, crumbs). `hx-boost` on the body swaps `<main>` and pushes the URL, and each response also carries the bar as an out-of-band swap so Home/Back/crumbs update without a reload. `routes.py` (1924 lines) splits into a `web_interface/routes/` package, one module per area, with shared state and the template-context helper in `web_interface/context.py`. Tailwind is compiled to a committed `static/app.css`; HTMX is already vendored.

**Tech Stack:** Python 3.12, FastAPI, Jinja2, HTMX 1.9.10 (vendored at `web_interface/static/htmx.min.js`), Tailwind via the standalone CLI binary (no Node project), Pydantic v2, loguru, pytest, `uv`, `ruff`.

**Spec:** `docs/superpowers/specs/2026-09-25-dashboard-v2-shell-design.md`
**Program plan:** `docs/superpowers/plans/2026-09-25-dashboard-v2-program.md` (part 2 of 4)
**Depends on:** part 1, merged as `88456b2`

## How this plan is written

Two owner-approved rules from program plan §3.1 and §3.2 govern this document. They **override** the `writing-plans` skill's default of embedding full code bodies.

1. **Prose-plan rule.** Each task gives its spec section, the files it may touch, the interface (exact names, signatures, routes, template variables), the behavior in prose, and the tests to write first. **The implementer writes the code.** A code block appears only for a wire format, a shell command, or a genuinely non-obvious algorithm. Part 1's plan embedded function bodies and implementers transcribed three defects out of them; tasks rewritten in this style needed no fix rounds.
2. **Wave rule.** Tasks are grouped into numbered waves whose "files it may touch" sets are pairwise disjoint and whose tests share no fixture being edited in the same wave. The executor dispatches a wave's implementers in parallel, then its reviewers in parallel, then runs the full `uv run pytest` and `ruff check .` once before the next wave. A reviewer failure returns only its own task. Anything touching a shared file is a **serial step** run one implementer and one reviewer at a time.

Names inside an **Interfaces** block are contracts other tasks depend on; spell them exactly. Reviewer defects must cite the spec or a named interface, never a missing line of plan code.

## Global Constraints

Values copied verbatim from the spec. Every task's requirements implicitly include this section.

- **No CDN references anywhere.** `base.html` loads `static/app.css` and `static/htmx.min.js` only. HTMX is already vendored at 1.9.10 by part 1; do not re-download or change its version.
- **No new runtime dependency.** The Tailwind standalone CLI is a build-time binary, not a Python dependency; `pyproject.toml` must not change.
- **Eight home tiles:** Health, Products, Inventory, Reports, Controls, Tests, Users, Settings. A tile is rendered only when `perms` intersects that tile's permission set. Reports and Tests render in a "coming soon" style for a role that holds their permission.
- **Bar:** 56 px, slate-800. Home, Back, breadcrumb, health pill, Lock. Home and Back hidden on Home; Lock present on every level including Home. On phones only the last two crumbs show.
- **Health pill:** `<span id="pill" hx-get="/pill" hx-trigger="load, every 5s" hx-swap="outerHTML">`, outside `<main>`. Green `OK` when healthy, red with the highest-priority active fault code otherwise, grey `…` before first load. Tapping it navigates to `/health/faults`.
- **Polling:** only the Home hero (`/status`, every 1 s), the Home KPIs (`/kpi`, every 60 s), and the pill (every 5 s). Lists and forms load once and refresh on action. `/status`, `/kpi` and `/pill` are the **only** fragment endpoints.
- **Breakpoints:** `lg` ≥ 900 px → 4×2 tile grid; `md` 600–899 px → 2×4; below 600 px → one column, full-width tile rows. Desktop centers a 1024 px-wide layout.
- **Touch:** every button and row at least **48 px** tall with **8 px** gaps. Body 16 px, tile title 20 px, hero 24 px. No hover-only affordances.
- **Light theme only.** slate-50 page, white cards, green/amber/red status, slate-800 bar.
- **Lists not tables** everywhere except the Reports activity table, which gets `overflow-x: auto`.
- **Numeric entry** uses part 1's `partials/keypad.html` pattern with `inputmode="numeric"` as the fallback; text fields use the device keyboard.
- **Feedback** is a swapped row or an inline message under the button. No toasts, no modals.
- **Two-tap confirm** on Controls and on fault Clear and code Regenerate: the first tap turns the button into a confirm prompt with Cancel; the second posts.
- **`/screen` and `/screen/body` are unchanged.** Any diff to `templates/screen.html` or `templates/partials/screen_body.html` is a defect.
- **Every POST keeps `require_htmx`.** Boosted requests send `HX-Request`, so this keeps working.
- **Commands:** `uv run pytest`, `uv sync`, `ruff check --fix .` then `ruff format .`. Never chain shell commands with `&&`. Never run Docker.
- **Out of scope:** sales-report content beyond the moved activity table (part 3), any test actions (part 4), dark theme, animations, service workers, editing machine id / web host / port.

## Naming reconciliation, decided up front

Spec §1.2 calls the template-context helper `_ctx(request, level, **extra)`. Part 1 actually shipped `web_interface/auth.py::template_context(request, **extra)`, which is used by every existing route and injects `perms` and `current_user` (as a frozen `TemplateUser` with no `pin_hash`/`pin_salt`). **This plan keeps the name `template_context`** and adds a `level` parameter, moving it to `web_interface/context.py`. Renaming ~60 call sites to `_ctx` would be churn with no behavioral gain. Record as a deviation.

## File Structure

| File | Responsibility |
|---|---|
| `web_interface/context.py` | New. Shared state setters (`set_config_object`, `set_vmc_instance`, `set_health_monitor`, `set_event_recorder`, `set_availability`, `set_inventory_manager`, `set_access_store`, `set_display_controller`), their module globals, `ensure_setup_mode`, and `template_context(request, level=None, **extra)` |
| `web_interface/levels.py` | New. `Level` dataclass, `Level.child`, the static tree of §2 levels |
| `web_interface/routes/__init__.py` | New. Assembles every area router; re-exports the setters so `main.py` keeps working |
| `web_interface/routes/auth.py` | Login, enrollment, logout, setup, setup codes, setup review |
| `web_interface/routes/home.py` | `/`, `/status`, `/kpi`, `/pill` |
| `web_interface/routes/health.py` | The six Health levels and `POST /health/faults/{key}/clear` |
| `web_interface/routes/products.py` | The Products levels and their POSTs |
| `web_interface/routes/inventory.py` | `/inventory` restock and `POST /inventory/{sku}/adjust` |
| `web_interface/routes/reports.py` | `/reports` |
| `web_interface/routes/controls.py` | `/controls` and `POST /controls/{command}` |
| `web_interface/routes/tests_level.py` | `/tests` placeholder |
| `web_interface/routes/users.py` | Users, Person, Devices, Emergency codes, Ownership |
| `web_interface/routes/settings.py` | The six Settings levels and their POSTs |
| `web_interface/routes/screen.py` | `/screen`, `/screen/body` — moved verbatim |
| `web_interface/templates/base.html` | The shell |
| `web_interface/tailwind.config.js`, `web_interface/tailwind.input.css` | Build inputs |
| `web_interface/static/app.css` | Committed compiled output |
| `tests/test_levels.py`, `tests/test_static_css.py` | New |
| `tests/test_routes_<area>.py` | New, one per area — see the note below |

**Deviation, required by the wave rule:** spec §6 puts the new route tests in `tests/test_web_routes.py`. That single 2933-line file is a shared fixture surface, so parallel area tasks would collide in it. Each area task gets `tests/test_routes_<area>.py` instead, and the shared fixtures (`sign_in`, `make_client`, `wired`, `login_as`, `anonymous`) move to `tests/conftest.py` in Task 1 so every area file can use them without redefining them. `tests/test_web_routes.py` keeps only what is not area-specific until Task 15 finishes retargeting it.

## Execution schedule

| Step | Kind | Tasks | Why this grouping |
|---|---|---|---|
| 1 | **Serial** | 1 | Touches `routes.py`, `server.py`, `main.py`, `conftest.py` — the shared surface everything else stands on |
| 2 | **Wave 1** | 2, 3 | `levels.py` + its test vs. Tailwind inputs + `app.css` + its test. Fully disjoint |
| 3 | **Serial** | 4 | Creates `base.html`, which every later template extends |
| 4 | **Serial** | 5 | Creates `partials/tile.html` and the `/pill` endpoint, both used by waves 2 and 3 |
| 5 | **Wave 2** | 6, 7, 8 | Health, Products, Inventory — one route module, own templates, own test file each |
| 6 | **Wave 3** | 9, 10, 11, 12, 13 | Reports, Controls, Tests, Users, Settings — same disjointness |
| 7 | **Serial** | 14 | Edits `base.html` (variant bar) and part 1's five auth templates |
| 8 | **Serial** | 15 | Deletes `dashboard.html`, the old partials and the removed routes; retargets `tests/test_web_routes.py` |
| 9 | **Serial** | 16 | `CLAUDE.md`, `README.md` — shared docs |

**3 waves, 6 serial steps, 16 tasks.** Tasks 4 and 5 are serial because every later task depends on their output, not because of a write conflict; tasks 1, 14, 15 and 16 are serial because they write shared files.

**Model policy** (program plan §3.1, unchanged): Sonnet for tasks 1, 2, 5, 6, 7, 8, 12, 13, 15 (routing, permissions, level logic, config writes, deletions); Haiku for tasks 3, 4, 9, 10, 11, 14, 16 (templates, restyling, moving partials, docs). Reviewers always Sonnet.

---

## Serial step 1

### Task 1: Split `routes.py` into an area package; extract `context.py`

**Spec:** §4 (migration — the route split), §7 (files).

Pure restructuring. **No behavior changes, no route URLs changed, no templates touched.** Every existing test must pass unmodified except for import paths and fixture location. A reviewer should reject any behavioral edit smuggled in here.

**Files:**
- Create: `web_interface/context.py`, `web_interface/routes/__init__.py`, `web_interface/routes/auth.py`, `web_interface/routes/home.py`, `web_interface/routes/legacy.py`, `web_interface/routes/screen.py`
- Delete: `web_interface/routes.py`
- Modify: `web_interface/server.py`, `main.py`, `web_interface/auth.py`, `tests/conftest.py`, `tests/test_web_routes.py`, `tests/test_web_auth.py`

**Interfaces — Produces:**
- `web_interface/context.py` module globals and setters, moved verbatim from `routes.py`: `config`, `vmc_instance`, `health_monitor`, `event_recorder`, `availability`, `inventory_manager`, `access_store`, `display_controller`, `_pending_codes`, plus `set_config_object`, `set_vmc_instance`, `set_health_monitor`, `set_event_recorder`, `set_availability`, `set_inventory_manager`, `set_access_store`, `set_display_controller`, `ensure_setup_mode`, `require_htmx`, `tail`, `LOG_PATH`.
- `web_interface/context.py::template_context(request, level=None, **extra) -> dict` — moved from `web_interface/auth.py`, keeping `request`, `current_user`, `perms`, and adding `level`. `web_interface/auth.py` keeps `require`, `Principal`, `current_principal`, `client_key`, `is_trusted_client`, `TemplateUser`, `backoff`, the cookie names and helpers; it must **not** import `context.py` (one-way dependency: `context` imports `auth`).
- `web_interface/routes/__init__.py::attach_routes(app, templates) -> None` — same signature `server.py` calls today. It includes every area router. It also re-exports every setter and `ensure_setup_mode` so `main.py`'s existing `routes.set_*` calls keep working with no change beyond the import line.
- Each area module exposes `def build_router(templates) -> APIRouter`.
- `web_interface/routes/legacy.py` holds every route not yet re-homed, moved verbatim.
- `web_interface/routes/screen.py` holds `/screen` and `/screen/body`, moved **verbatim**.

**Interfaces — test fixtures moved to `tests/conftest.py`** so area test files can share them: `sign_in(store, user, *, shared=False)`, `make_client(store, role=Role.owner, *, shared=False, name="Ada")`, and the fixtures `wired`, `login_as`, `client`, `anonymous`. Behavior unchanged from part 1 — the `wired` fixture still seeds owner "Ada" (PIN `1379`) and calls `store.finalize_setup()`.

**Behavior to get right:**
- Routes currently live inside `attach_routes` as closures over module globals. Moving them to module level means they must read `context.config`, `context.access_store` and so on **at request time**, not bind the value at import. Import the module (`from web_interface import context`) and dereference attributes inside the handler; do not `from web_interface.context import config`, which would capture `None` forever. This is the single most likely way to break this task.
- The access-gate middleware and `ensure_setup_mode` keep their current behavior; register the middleware from `attach_routes` exactly once.
- The public/gated router distinction survives: auth routes carry no session dependency, everything else declares its `Permission`.

**Tests to write first:** none new. This task is proven by the existing suite passing unchanged. Before starting, record the baseline (`uv run pytest` → 1080 passed, 13 skipped). After, the same counts, with the only test edits being import paths and the fixture move. A changed assertion is a defect.

**Done when:** `uv run pytest` reports 1080 passed, 13 skipped; `ruff check .` clean; `git diff --stat` shows no change to `templates/`. Commit: `refactor(web): split routes.py into an area package with shared context`.

---

## Wave 1 — tasks 2 and 3 in parallel

### Task 2: `Level` and the static level tree

**Spec:** §1.2, §2 (the URL tree).

**Files:**
- Create: `web_interface/levels.py`, `tests/test_levels.py`

**Interfaces — Produces:**
- `@dataclass(frozen=True) class Level` with fields `title: str`, `url: str`, `parent: "Level | None"`.
- `Level.crumbs -> list[tuple[str, str]]` — root-first, each `(title, url)`, **including** the current level as the last entry. The consumer (Task 4) renders the last entry unlinked.
- `Level.parent_url -> str` — the parent's URL, or `"/"` when there is no parent.
- `Level.child(parent: "Level", title: str, url: str) -> "Level"` — a classmethod or staticmethod building a parameterized child at request time (a SKU, a subsystem name, a user id).
- A module-level constant per level in spec §2, named for its path with `LEVEL_` prefix: `LEVEL_HOME`, `LEVEL_HEALTH`, `LEVEL_HEALTH_SUBSYSTEMS`, `LEVEL_HEALTH_FAULTS`, `LEVEL_HEALTH_AVAILABILITY`, `LEVEL_HEALTH_LOGS`, `LEVEL_PRODUCTS`, `LEVEL_PRODUCTS_NEW`, `LEVEL_INVENTORY`, `LEVEL_REPORTS`, `LEVEL_CONTROLS`, `LEVEL_TESTS`, `LEVEL_USERS`, `LEVEL_USERS_NEW`, `LEVEL_DEVICES`, `LEVEL_USERS_CODES`, `LEVEL_USERS_OWNERSHIP`, `LEVEL_SETTINGS`, `LEVEL_SETTINGS_MACHINE`, `LEVEL_SETTINGS_CONTACTS`, `LEVEL_SETTINGS_PAYMENTS`, `LEVEL_SETTINGS_COMMS`, `LEVEL_SETTINGS_MQTT`, `LEVEL_SETTINGS_WEB`. Titles are the spec §2 level names.

**Behavior to get right:** Home's `crumbs` is a single entry and its `parent_url` is `"/"`. A child built with `Level.child` inherits the parent's whole chain, so `/products/{sku}/catalog` yields four crumbs (Home, Products, the product, Catalog). Frozen dataclasses mean `child` returns a new instance rather than mutating.

**Tests to write first, in `tests/test_levels.py`:** Home has one crumb and `parent_url == "/"`; a second-level constant has two crumbs ending in itself; a third-level constant has three; `parent_url` matches the parent's `url` for every constant in the tree (iterate the module, do not hand-write 24 assertions); `Level.child` produces the parent's crumbs plus its own and is not the same object as its parent; every URL in the tree is unique; every URL begins with `/`.

**Done when:** `uv run pytest tests/test_levels.py` green. Commit: `feat(web): Level dataclass and the v2 level tree`.

---

### Task 3: Compile Tailwind to a committed `app.css`, with a class-coverage test

**Spec:** §3 (Tailwind, compiled), §7 (build inputs).

**Files:**
- Create: `web_interface/tailwind.config.js`, `web_interface/tailwind.input.css`, `web_interface/static/app.css`, `tests/test_static_css.py`

Do **not** modify any template in this task — the class-coverage test must pass against templates as they stand today, and Task 4 onwards keeps it passing.

**Interfaces — Produces:**
- `web_interface/tailwind.config.js` with `content` scanning `web_interface/templates/**/*.html`, the light-theme token set from §3, and nothing else.
- `web_interface/tailwind.input.css` — the three `@tailwind` directives plus any `@layer components` rules the shell needs (a 48 px touch-target utility is the obvious candidate).
- `web_interface/static/app.css` — the committed compiled output.
- `tests/test_static_css.py::test_every_template_class_is_in_app_css` — extracts class tokens from every `class="..."` attribute in `web_interface/templates/**/*.html` and asserts each appears in `app.css`.

**Behavior to get right:**
- The build uses the **standalone Tailwind CLI binary**, not a Node project. There must be no `package.json`, no `node_modules`, and no change to `pyproject.toml`. Document the exact command in the task's commit message; Task 16 puts it in `CLAUDE.md`.
- The class extractor must not choke on Jinja: a `class="{{ 'a' if x else 'b' }}"` attribute or `class="p-2 {{ extra }}"` yields tokens containing `{{`, `}}`, `%` or quotes. Skip any token that is not a plausible CSS class (the pragmatic rule: skip tokens containing `{`, `}`, `"`, `'`, or whitespace after splitting). State the skipping rule in a comment so a reviewer can see what is *not* covered, and keep it as narrow as possible — a test that silently skips most classes is worse than no test.
- Matching is a substring search for the escaped selector in `app.css`, since Tailwind escapes `:` and `/` in generated selectors (`lg\:grid-cols-4`, `w-1\/2`). Handle that escaping or the test will produce false failures on every responsive class.

**Tests to write first:** the coverage test itself, plus one guard proving it is not vacuous — assert that a deliberately fabricated class name (e.g. `not-a-real-tailwind-class-xyz`) is reported missing when fed through the same checker, so a future empty `app.css` cannot pass silently.

**Done when:** `uv run pytest tests/test_static_css.py` green against current templates; `app.css` is non-empty and committed; `pyproject.toml` unchanged. Commit: `build(web): compile Tailwind to a committed app.css with a coverage test`.

---

## Serial step 3

### Task 4: `base.html` — the shell and the out-of-band bar

**Spec:** §1.1 (base.html, bar), §1.2 (level context).

**Files:**
- Create: `web_interface/templates/base.html`, `web_interface/templates/error.html`
- Modify: `web_interface/routes/home.py` (only enough to render one level through the shell), `web_interface/routes/__init__.py` (register the exception handlers), `tests/test_web_routes.py` (the bar/OOB assertions)

**This task also owns spec §5's error pages**, because they are shell chrome and every later task relies on them: `templates/error.html` extends `base.html` and renders a title, a message and a Back button. Register FastAPI exception handlers so an `HTTPException` with status 404 renders it with "Not found", and 403 renders it with "You don't have access to this" — **inside the shell, never as bare JSON.** Two constraints: the handlers must not swallow the 401 + `HX-Redirect: /login` that part 1's `require` raises for an unauthenticated HTMX request, nor the 303 redirect it raises for a page request; and they must leave the fragment endpoints (`/status`, `/kpi`, `/pill`) free to answer as they do today. Test that a 404 and a 403 both render HTML containing the bar, and that an unauthenticated HTMX request still returns 401 with `HX-Redirect` rather than an error page.

**Interfaces — Produces:**
- `base.html` declaring blocks `{% block body %}` for `<main>`'s content and `{% block title %}`. Every level template extends it and is rendered with a context carrying `level`, `perms` and `current_user` from `template_context`.
- `<header id="bar" hx-swap-oob="true">` — rendered on **every** response, so a boosted navigation updates the bar without a reload.
- The pill placeholder: `<span id="pill" hx-get="/pill" hx-trigger="load, every 5s" hx-swap="outerHTML">` rendering the grey `…` state inline. The endpoint arrives in Task 5; until then it 404s, which is acceptable mid-branch and must be noted in the hand-off.
- A `{% block bar_variant %}` or equivalent hook so Task 14 can render the login/setup bar with no Home/Back/Lock without duplicating the header.

**Behavior to get right:**
- `<body hx-boost="true" hx-target="main" hx-swap="innerHTML show:top">`. Boosted requests carry `HX-Request`, so part 1's CSRF guard keeps working — do not weaken it.
- A full-page request (reload, bookmark) and a boosted request render the **same template**. The OOB header is present in both; a browser ignores `hx-swap-oob` on a full load.
- Home and Back are omitted on Home (`level.parent is None`), `Back` targets `level.parent_url`, and the breadcrumb renders `level.crumbs` with the last entry unlinked. Phones show only the last two crumbs — a CSS rule, not a second render path.
- `Lock` posts to `/logout` on every level including Home, then navigates fully to `/login`. Part 1's `/logout` answers `HX-Redirect: /login`, which HTMX turns into a full navigation; verify that still holds through the boosted body.
- Only `static/app.css` and `static/htmx.min.js`. **No CDN URL may appear in this file.**

**Tests to write first, in `tests/test_web_routes.py`:** Home renders 200 with the bar and **without** Home/Back buttons; a request to a child level with `HX-Request: true` and `HX-Boosted: true` returns a body containing both `<main>` and `<header id="bar"` with `hx-swap-oob`, and the child's crumb titles; the same URL requested without those headers also contains the bar (full-page path); no response body contains `unpkg` or `cdn.tailwindcss.com`; the pill placeholder carries `hx-get="/pill"` and `every 5s`. Use whichever real level exists at this point — Home plus one child stub is enough; the wave-2 tasks assert their own levels.

**Done when:** those tests pass and the full suite stays green apart from `/pill` 404ing, which the next task fixes. `ruff check .` clean. Commit: `feat(web): base.html shell with an out-of-band navigation bar`.

---

## Serial step 4

### Task 5: Home — status strip, tile grid, and the `/pill` endpoint

**Spec:** §1.3 (Home), §1.1 (health pill), §2 (tile gates).

**Files:**
- Create: `web_interface/templates/home.html`, `web_interface/templates/partials/tile.html`, `web_interface/templates/partials/pill.html`, `web_interface/templates/partials/confirm_button.html`
- Modify: `web_interface/routes/home.py`, `tests/test_web_routes.py`

`partials/tile.html` and `partials/confirm_button.html` are created here because waves 2 and 3 both include them; that is why this step is serial.

**Interfaces — Produces:**
- `GET /` — gate: any live session. Renders `home.html`. Context adds `tiles`, a list of dicts with keys `title`, `url`, `icon`, `context`, `enabled`, `coming_soon`.
- `GET /pill` — gate: any live session. Renders `partials/pill.html`, returning the element with `id="pill"` so `hx-swap="outerHTML"` replaces itself and keeps polling.
- `partials/tile.html` — included with a `tile` variable, rendering a ≥ 48 px button with an inline SVG icon, a 20 px title and one line of context; links to `tile.url`; renders the "coming soon" style when `tile.coming_soon`.
- `partials/confirm_button.html` — included with `label`, `confirm_label`, `post_url` and `target`; renders the first-tap button that swaps itself into a confirm prompt with Cancel. Used by Controls, fault Clear and code Regenerate.
- `/status` and `/kpi` keep their existing URLs, permissions and response shapes. Restyle their templates for touch; **do not change the data they compute.**

**Behavior to get right:**
- **Tile visibility:** a tile is rendered when `perms` intersects that tile's permission set from spec §2. The eight sets are: Health `{view_status}`, Products `{edit_catalog, edit_placement}`, Inventory `{edit_placement}`, Reports `{view_reports}`, Controls `{machine_controls}`, Tests `{run_tests}`, Users `{manage_users}`, Settings `{edit_contacts, edit_secrets}`. A loader holds `view_status` and `edit_placement`, so a loader sees exactly Health, Products and Inventory — spec §6 names that as a test.
- **Tile context comes from the route, not from polling**: Health shows the active-fault count, Products the product count, Inventory the number of tracked products below their low-stock line or the text `tracking off` when nothing is tracked, Users the user count. The rest are static strings.
- **Pill state:** green `OK` when healthy, red with a fault code otherwise. "Healthy" must use the **same predicate `/status` already uses** — `len(issues) == 0 and payment_enabled is not False` — so the pill and the hero can never disagree. Factor that predicate into one function both call rather than duplicating it; a divergence here is the defect most likely to survive review.
- **Highest-priority fault** for the pill: rank by `severity` using `critical` > `lockout` > `vend_failed` > `product_unavailable` > `warning` > `info` (the `Severity` enum in `contracts/vending_machine.py`), tie-broken by the order `vmc.active_faults()` returns (product faults first, then machine faults). Test both the ranking and the tie-break.
- The pill tolerates a missing VMC or health monitor exactly as `/status` does today: grey/neutral, never a 500.
- The 24 h / 7 d / 30 d activity table does **not** appear on Home; it moves to Reports in Task 9.

**Tests to write first:** Home renders the four KPI cards and the hero with their polling attributes and intervals (1 s hero, 60 s KPI); a loader's Home contains exactly the Health, Products and Inventory tiles and none of the other five; an owner sees all eight; a secretary sees Reports rendered "coming soon"; tile context lines show the product count and the user count; `/pill` returns green `OK` on a clean machine and a red pill naming the code when a fault is raised; `/pill` with two faults of different severity names the higher one; `/pill` with no VMC wired returns a neutral pill and not a 500; `/pill` requires a session.

**Done when:** the full suite is green including Task 4's bar tests, `ruff check .` clean, and `tests/test_static_css.py` still passes (new classes must be in `app.css` — rebuild it and commit the result). Commit: `feat(web): Home status strip, tile grid and the health pill`.

---

## Wave 2 — tasks 6, 7 and 8 in parallel

Each task owns one route module, its own templates, and its own test file. No two touch the same file. All three may read `partials/tile.html` and `partials/confirm_button.html` from Task 5 but must not modify them.

### Task 6: The six Health levels

**Spec:** §2 (Health rows), §3 (lists not tables), §5 (404 inside the shell).

**Files:**
- Create: `web_interface/templates/health.html`, `health_subsystems.html`, `health_subsystem.html`, `health_faults.html`, `health_availability.html`, `health_logs.html`, `tests/test_routes_health.py`
- Modify: `web_interface/routes/health.py`

**Interfaces — Produces:**
- `GET /health` — gate `view_status`. Four sub-tiles: Subsystems, Faults, Availability, Logs. **Logs is rendered only when the role holds `view_logs`.** Each sub-tile shows a summary count.
- `GET /health/subsystems` — gate `view_status`. One card per `contracts.vending_machine.EXPECTED_SUBSYSTEMS` entry: alive/stale, uptime, firmware, contract version. Each card links to the subsystem level.
- `GET /health/subsystems/{name}` — gate `view_status`. Identity fields (brand, model, hardware id, ip, firmware, contract), heartbeat age, and temperature ranges **where the subsystem reports them**. Level built with `Level.child(LEVEL_HEALTH_SUBSYSTEMS, name, f"/health/subsystems/{name}")`.
- `GET /health/faults` — gate `view_status`. Active faults with age and gate class (safety / fulfillment / alert). A Clear button per fault, rendered only with `clear_faults`, using `partials/confirm_button.html`.
- `POST /health/faults/{key}/clear` — gate `clear_faults`, plus `require_htmx`. Replaces the old `POST /faults/{key}/clear`. Returns the re-rendered fault list.
- `GET /health/availability` — gate `view_status`. The permissive table as stacked cards: payment enabled, per-kind availability, blocking reasons.
- `GET /health/logs` — gate `view_logs`. Last **50** lines, monospace, with a Refresh button and **no polling**.

**Behavior to get right:**
- Data sources are unchanged: `health_monitor.get_summary()`, `HealthMonitor.empty_subsystem_row()` for silent subsystems, `availability.table()`, `availability.payment_enabled`, `availability.payment_blocking_reasons()`, `vmc.active_faults()`. The old `/health` fragment computed all of this — reuse it, split across four levels. Do not change what any of them return.
- `EXPECTED_SUBSYSTEMS` entries that have never spoken are still listed, as today.
- An unknown `{name}` renders the shell's 404 page with a Back button, **not** a bare JSON 404 (spec §5).
- The gate class per fault comes from the availability gate the fault belongs to (safety / fulfillment / alert), the same classification `services/availability.py` already applies; read it, do not re-derive it.
- Logs use the existing `tail` helper at 50 lines (the old fragment used 10).

**Tests to write first, in `tests/test_routes_health.py`:** each of the six levels returns 200 for a role holding its gate and 403 for one that does not (loader gets 403 on `/health/logs`, 200 on the other five); `/health` renders the Logs sub-tile for a tech and omits it for a loader; the breadcrumb on `/health/faults` reads Home › Health › Faults; `/health/subsystems` lists every `EXPECTED_SUBSYSTEMS` name even with no heartbeats; an unknown subsystem name returns 404 **with the shell's bar present in the body**; a raised fault appears on `/health/faults` with its age and gate class; the Clear button is absent for a role without `clear_faults`; `POST /health/faults/{key}/clear` clears it for a tech and 403s for a loader; the clear POST without `HX-Request` is 403; `/health/logs` shows a written line and carries no polling attribute.

**Done when:** `uv run pytest tests/test_routes_health.py` green, full suite green, `app.css` rebuilt and committed if new classes appeared, `tests/test_static_css.py` green. Commit: `feat(web): the six Health levels`.

---

### Task 7: The Products levels

**Spec:** §2 (Products rows), §3 (loaders see catalog read-only).

**Files:**
- Create: `web_interface/templates/products.html`, `product.html`, `product_catalog.html`, `product_placement.html`, `product_form.html`, `tests/test_routes_products.py`
- Modify: `web_interface/routes/products.py`

**Interfaces — Produces:**
- `GET /products` — gate `edit_catalog` **or** `edit_placement`. A card row per product: name, price, slot, count, lock badge. Add button rendered only with `edit_catalog`.
- `GET /products/new` — gate `edit_catalog`. `product_form.html` with catalog **and** placement fields together, since the creator owns both.
- `POST /products/new` — gate `edit_catalog`, `require_htmx`. Replaces `POST /inventory/add`.
- `GET /products/{sku}` — gate `edit_catalog` or `edit_placement`. Read-only summary with two sub-tiles (Catalog, Placement) and Copy/Delete buttons rendered only with `edit_catalog`.
- `GET /products/{sku}/catalog` — gate `edit_catalog`. Name, price, kind, SKU form.
- `POST /products/{sku}/catalog` — gate `edit_catalog`, `require_htmx`.
- `GET /products/{sku}/placement` — gate `edit_placement`. Slot/button, inventory count, tracking toggle.
- `POST /products/{sku}/placement` — gate `edit_placement`, `require_htmx`.
- `GET /products/{sku}/copy` — gate `edit_catalog`. Prefilled add form.
- `POST /products/{sku}/delete` — gate `edit_catalog`, `require_htmx`.

**Behavior to get right:**
- **The read-only rule is the point of this task.** A loader or tech holds `edit_placement` but not `edit_catalog`: they reach `/products/{sku}` and **see the name, price and kind as read-only values**, while `/products/{sku}/catalog` returns 403 for them. Hiding the values would be wrong; so would letting them open the catalog form.
- Part 1's guarantees carry over and must not regress: a placement write cannot change name, price or kind, and it leaves **all** placement state unchanged when slot validation rejects the change (part 1's `a2b8829`). Counts and the tracking flag live in `InventoryManager`; the table must render the `InventoryManager` count, not `product.inventory_count` (part 1's `d58da37`). Negative counts are rejected (part 1's `231fe36`).
- Reuse `services/config_store.add_product`, `update_product`, `delete_product` and the `InventoryManager` methods as they are. Adding a product still registers its SKU with `InventoryManager`; deleting still removes it.
- A deleted or unknown SKU renders the shell 404 with Back.
- The lock badge comes from the product-scope entries in `vmc.active_faults()`, as `inventory_table.html` does today.

**Tests to write first, in `tests/test_routes_products.py`:** `/products` is 200 for owner, secretary, tech and loader and its Add button appears only for owner and secretary; `/products/{sku}` shows the price for a loader as text and not as an input; `/products/{sku}/catalog` is 403 for loader and tech, 200 for owner and secretary; `/products/{sku}/placement` is 200 for all four; a catalog POST changes name, price and kind; a placement POST changes slot, count and tracking; a placement POST carrying `price` leaves the price unchanged; a placement POST with a slot already in use changes **nothing**, count included; a negative count is rejected; the products list shows the `InventoryManager` count after an adjustment, not the stale `Product` field; create, copy and delete work for owner and 403 for loader; an unknown SKU is a shell 404; every POST without `HX-Request` is 403.

**Done when:** `uv run pytest tests/test_routes_products.py` green, full suite green, `app.css` rebuilt if needed. Commit: `feat(web): the Products levels with read-only catalog for placement-only roles`.

---

### Task 8: The Inventory restock level

**Spec:** §2 (Inventory row), §3 (touch targets, numeric entry).

**Files:**
- Create: `web_interface/templates/inventory.html`, `tests/test_routes_inventory.py`
- Modify: `web_interface/routes/inventory.py`

**Interfaces — Produces:**
- `GET /inventory` — gate `edit_placement`. One row per **tracked** product: name, current count, four adjust buttons (`−10 −1 +1 +10`), and a slot field. Untracked products listed at the bottom **without** adjust buttons.
- `POST /inventory/{sku}/adjust` — gate `edit_placement`, `require_htmx`. Form field `delta` (a signed integer, one of `-10`, `-1`, `1`, `10`). Returns the re-rendered row for that SKU only, not the whole list.

**Behavior to get right:**
- The adjustment goes through `InventoryManager.set_count(sku, new)` where `new = max(0, get_count(sku) + delta)`. **Clamp at zero rather than erroring** — a loader tapping `−10` on a count of 3 should land on 0, which is the physically meaningful result, and part 1 already refuses negative counts on the placement form. Test the clamp explicitly.
- Reject a `delta` outside the four allowed values with 400 rather than trusting the form; the buttons are the only intended caller but the endpoint is reachable directly.
- Returning one row keeps the page stable under a loader's repeated taps; returning the whole list would scroll-jump on a tablet.
- Untracked products must be visible (so a loader can see what is not being counted) but have no adjust affordance.
- An unknown SKU is a shell 404.

**Tests to write first, in `tests/test_routes_inventory.py`:** `/inventory` is 200 for every role (all four hold `edit_placement`); a tracked product shows all four adjust buttons and an untracked one shows none; `+10` raises the `InventoryManager` count by 10 and the response contains the new count; `−10` on a count of 3 clamps to 0 rather than going negative; a `delta` of `7` is refused with 400 and leaves the count unchanged; the adjust POST without `HX-Request` is 403; the response is the single row, not the full list (assert the other product's name is absent); an unknown SKU is a shell 404.

**Done when:** `uv run pytest tests/test_routes_inventory.py` green, full suite green. Commit: `feat(web): the Inventory restock level with clamped adjustments`.

---

## Wave 3 — tasks 9 through 13 in parallel

### Task 9: The Reports level

**Spec:** §2 (Reports row), §1.3 (the activity table moves off Home), §3 (the one permitted table).

**Files:**
- Create: `web_interface/templates/reports.html`, `tests/test_routes_reports.py`
- Modify: `web_interface/routes/reports.py`

**Interfaces — Produces:**
- `GET /reports?period=` — gate `view_reports`. The activity table moved from Home, with a 24 h / 7 d / 30 d selector. `period` accepts `24`, `168`, `720`; anything else falls back to `24`, exactly as the old `/activity` did.

**Behavior to get right:** the data comes from `event_recorder.get_summary(period)` and `get_historical_average(period)` through `asyncio.to_thread`, unchanged from `/activity`. With no recorder wired, render the existing neutral placeholder rather than a 500. This is the only level permitted a real `<table>`, and it must sit inside an `overflow-x: auto` container. Part 3 extends this level, so keep the template's structure obvious rather than clever.

**Tests to write first, in `tests/test_routes_reports.py`:** 200 for owner and secretary, 403 for tech and loader; the three valid periods render and an invalid one falls back to 24 h; with no recorder the placeholder renders and the status is 200; the recorder calls are offloaded to a thread (the existing `/activity` test asserts this — carry the technique over); the table is wrapped in an `overflow-x` container.

**Done when:** `uv run pytest tests/test_routes_reports.py` green, full suite green. Commit: `feat(web): the Reports level with the activity table moved off Home`.

---

### Task 10: The Controls level

**Spec:** §2 (Controls row), §3 (two-tap confirm, inline feedback).

**Files:**
- Create: `web_interface/templates/controls.html`, `tests/test_routes_controls.py`
- Modify: `web_interface/routes/controls.py`

**Interfaces — Produces:**
- `GET /controls` — gate `machine_controls`. Three large buttons: restart, reset, shutdown, each rendered through `partials/confirm_button.html`.
- `POST /controls/{command}` — gate `machine_controls`, `require_htmx`. Replaces `POST /action/{command}`. Returns the result message inline.

**Behavior to get right:** the command still runs through `services/fsm_control.perform_command(command, vmc_instance)`; an unknown command behaves exactly as it does today. The two-tap flow is server-rendered: the first tap `hx-get`s the confirm variant of the button, the second `hx-post`s the command — **no JavaScript state**, because a confirm that lives only in the DOM cannot be tested and cannot survive an OOB bar swap. The result message replaces the button area; no toast, no modal.

**Tests to write first, in `tests/test_routes_controls.py`:** `/controls` is 200 for owner and tech, 403 for secretary and loader; the initial render shows the first-tap buttons and **no** POST-triggering attribute for the bare command; requesting the confirm variant returns a button that posts to `/controls/restart` plus a Cancel that returns to the initial state; `POST /controls/restart` returns the result message and 200; an unknown command does not 500; the POST without `HX-Request` is 403; a loader's POST is 403 even with the header.

**Done when:** `uv run pytest tests/test_routes_controls.py` green, full suite green. Commit: `feat(web): the Controls level with server-rendered two-tap confirm`.

---

### Task 11: The Tests placeholder level

**Spec:** §2 (Tests row), §8 (no test actions in part 2).

**Files:**
- Create: `web_interface/templates/tests.html`, `tests/test_routes_tests_level.py`
- Modify: `web_interface/routes/tests_level.py`

**Interfaces — Produces:** `GET /tests` — gate `run_tests`. Placeholder copy stating that subsystem tests arrive in a later release. No actions, no forms, no MQTT.

**Behavior to get right:** this level must not acquire functionality by accident. It renders text inside the shell and nothing else. The module is named `tests_level.py`, **not** `tests.py`, so it can never shadow the `tests/` package on `sys.path`.

**Tests to write first:** 200 for owner and tech, 403 for secretary and loader; the body renders inside the shell with the correct breadcrumb; the response contains no `<form>` and no `hx-post`.

**Done when:** `uv run pytest tests/test_routes_tests_level.py` green, full suite green. Commit: `feat(web): the Tests placeholder level`.

---

### Task 12: The Users levels

**Spec:** §2 (Users rows), §4 (part 1's Users templates adopt the shell).

**Files:**
- Create: `web_interface/templates/users.html`, `user.html`, `user_form.html`, `devices.html`, `users_codes.html`, `users_ownership.html`, `tests/test_routes_users.py`
- Modify: `web_interface/routes/users.py`

**Interfaces — Produces:**
- `GET /users` — gate `manage_users`. People list, an Add button, and sub-tiles Devices, Emergency codes, Ownership (the last two only with `manage_ownership`).
- `GET /users/{id}` — gate `manage_users`. Edit name/email/role, Disable/Enable, Reset PIN, Delete. The **owner row is read-only for a secretary.**
- `GET /users/new`, `POST /users/new` — gate `manage_users`.
- `POST /users/{id}/disable`, `/enable`, `/reset-pin`, `/delete` — gate `manage_users`, `require_htmx`.
- `GET /devices` — gate `manage_users`. Label, shared toggle, trusted users, last seen, Forget.
- `POST /devices/{id}/forget`, `POST /devices/{id}/shared` — gate `manage_users`, `require_htmx`.
- `GET /users/codes` — gate `manage_ownership`. Unused count and a Regenerate button using `partials/confirm_button.html`; shows the new codes once.
- `POST /users/codes/regenerate` — gate `manage_ownership`, `require_htmx`.
- `GET /users/ownership` — gate `manage_ownership`. Machine report (email) and the transfer form (PIN + emergency code).
- `POST /users/report`, `POST /users/transfer`, `POST /users/transfer/cancel` — gate `manage_ownership`, `require_htmx`.

**Behavior to get right — every part 1 guarantee survives, and a reviewer must check each:**
- `_guard_owner_target`: a write whose target is the owner needs `manage_ownership`.
- `_guard_owner_self_lockout`: the owner cannot disable or delete **themselves**, and those controls are not rendered for their own row (part 1's `2dd8c41`).
- Device writes targeting a device that trusts the owner need `manage_ownership` (part 1's `84eaed2`).
- Reset PIN drops device trust **and** ends that user's sessions (part 1's `12a1115`).
- Responses rendering plaintext codes — the regenerate result and the transfer code — carry `Cache-Control: no-store` (part 1's `7fa2c14`).
- A wrong PIN on transfer consumes **no** emergency code.
- Starting a transfer leaves the current owner in full control.

**Tests to write first, in `tests/test_routes_users.py`:** each level 200 for its gate and 403 otherwise (tech and loader get 403 on all of them; a secretary gets 403 on codes and ownership); a secretary sees the owner's row read-only and gets 403 on disable/delete/reset-pin targeting the owner; the owner's own row renders no Disable or Delete control and the server refuses both; regenerate shows twenty codes once and the response is `no-store`; a wrong transfer PIN leaves the code pool untouched; a started transfer leaves the owner in control and `/users` shows it pending; forget removes a device and ends its sessions; forgetting the owner's device is 403 for a secretary; every POST without `HX-Request` is 403.

**Done when:** `uv run pytest tests/test_routes_users.py` green, full suite green. Commit: `feat(web): the Users levels in the v2 shell`.

---

### Task 13: The Settings levels

**Spec:** §2 (Settings rows), §5 (`save_config` failure handling).

**Files:**
- Create: `web_interface/templates/settings.html`, `settings_machine.html`, `settings_contacts.html`, `settings_payments.html`, `settings_comms.html`, `settings_mqtt.html`, `settings_web.html`, `tests/test_routes_settings.py`
- Modify: `web_interface/routes/settings.py`

This task writes the `/config/payments` and `/config/comms` pages that **never existed** — part 1 carried two `@pytest.mark.skip`ped tests for them. Those skips are removed here.

**Interfaces — Produces:**
- `GET /settings` — gate `edit_contacts` **or** `edit_secrets`. Six sub-tiles, each rendered only when the role holds that page's gate.
- `GET /settings/machine`, `POST /settings/machine` — gate `edit_contacts`. Name, location, notes editable; **machine id read-only.**
- `GET /settings/contacts`, `POST /settings/contacts` — gate `edit_contacts`. Owner and other people: name, email, phone, address, preferred channel.
- `GET /settings/payments`, `POST /settings/payments` — gate `edit_secrets`. Stripe, PayPal, MDB with **secrets masked**.
- `GET /settings/comms`, `POST /settings/comms` — gate `edit_secrets`. Email and SMS gateways masked, plus a **Send test email** button (`POST /settings/comms/test`).
- `GET /settings/mqtt`, `POST /settings/mqtt` — gate `edit_secrets`. Broker host, port, username, TLS; password masked; a note that env overrides win.
- `GET /settings/web`, `POST /settings/web` — gate `edit_secrets`. Trusted proxies editable; **host and port read-only** (restart to apply).

**Behavior to get right:**
- Writes mutate the live `ConfigModel` then call `services.config_store.save_config`, the same path products use. **A `save_config` failure returns the form with the error text and leaves the in-memory model as the user submitted it** (spec §5) — matching current product-save behavior, not reverting.
- **Masked means never round-tripping a secret through the browser.** Render a placeholder for a `SecretStr` that is already set; on POST, an unchanged placeholder leaves the stored value alone and only a genuinely new value overwrites it. Writing the mask back into config as the literal secret would be a serious defect — test it explicitly.
- `main.apply_env_overrides` returns a copy, so an env-set `MQTT_PASSWORD` is never in `config.mqtt`. The MQTT page shows the **effective** value when an env override is active and disables that field; it must not write an env value into `config.json`.
- Machine id, web host and web port are displayed but not editable (spec §8).
- Send test email goes through `services/mailer.send_email` and reports success or failure inline; it never raises.

**Tests to write first, in `tests/test_routes_settings.py`:** `/settings` is 200 for owner, secretary (contacts only) and 403 for tech and loader; its sub-tiles are filtered by gate — a secretary sees Machine and Contacts and not Payments, Comms, MQTT or Web; each page 200/403 per its gate; a machine POST round-trips through `save_config` into a temp config path; a contacts POST round-trips; a payments POST submitting the **unchanged mask** leaves the stored secret intact; a payments POST with a new value overwrites it; the rendered payments page never contains the real secret value; a `save_config` failure returns the form with the error and does not revert the submitted values; the MQTT page disables the password field and shows the effective value when `MQTT_PASSWORD` is set in the environment; machine id, host and port render as read-only; Send test email reports failure inline with an unconfigured gateway rather than raising.

**Done when:** `uv run pytest tests/test_routes_settings.py` green, full suite green, and part 1's two skipped payments/comms tests are deleted (their real coverage now lives here). Commit: `feat(web): the Settings levels with masked secrets`.

---

## Serial step 7

### Task 14: Move part 1's login, enrollment, setup and Users pages onto `base.html`

**Spec:** §4 — "Part 1's login, enrollment, setup, and Users templates adopt `base.html`; login and setup pages use a variant bar with no Home/Back/Lock."

This is the task program plan §5 names as the mitigation for "part 2 deletes what part 1 built on": part 1 deliberately landed these pages in the old dashboard so part 2 could move them once, rather than either part rebuilding them. Serial because it edits `base.html` and five templates other tasks read.

**Files:**
- Modify: `web_interface/templates/base.html`, `login.html`, `enroll.html`, `setup.html`, `setup_codes.html`, `setup_review_user.html`
- Modify: `web_interface/routes/auth.py` (context only — no URL or behavior change)
- Modify: `tests/test_web_routes.py` (the part 1 auth assertions that reach into page chrome)

Task 12 already re-homed the Users **levels**; this task covers only the five auth pages plus the partials they include.

**Interfaces — Produces:**
- The five pages extend `base.html` through the `bar_variant` hook from Task 4, rendering a **minimal bar**: the machine name or a title, and the health pill only if it is available without a session. No Home, no Back, no Lock — an unauthenticated visitor has nowhere to navigate and nothing to lock.
- `partials/keypad.html` is restyled to the §3 touch rules (≥ 48 px keys, 8 px gaps) while keeping its existing form contract: the `login-form` id, `hx-post="/login"`, `hx-target="#login-form"`, `hx-swap="outerHTML"`, and the `users`, `selected_user_id`, `error`, `wait_seconds` variables.

**Behavior to get right — part 1's security properties are easy to break here and a reviewer must re-verify each:**
- **The login failure response must stay byte-identical** between a wrong PIN for an enabled user, an unknown user id and a disabled user. Part 1 closed that enumeration oracle (`002460d`) by never echoing `selected_user_id` on a failure path. Any new chrome that varies with the submitted id — a title, a greeting, a hidden field — reopens it. Re-run the byte-comparison test; do not merely trust it.
- **No CDN reference may appear.** All five pages must keep loading `static/htmx.min.js`, which part 1 vendored precisely so enrollment works with no internet (`eefc1db`). Reintroducing a CDN `<script>` would silently break offline login again — the exact defect Copilot caught on part 1.
- The `Cache-Control: no-store` on `/setup/codes` (`7fa2c14`) survives the template change; it is set on the response, not the template, but confirm it.
- `require_htmx` still guards every POST on these pages, and the forms still post via HTMX.
- The setup-mode gate still redirects everything except `/setup` and `/setup/codes`, and a corrupt access file still answers 503 everywhere.

**Tests to write first:** the five pages render 200 and contain the variant bar with **no** Home, Back or Lock button; each contains `static/htmx.min.js` and no `unpkg` or `cdn.tailwindcss.com`; the three login failure bodies are byte-identical (re-use part 1's test, retargeted); `/setup/codes` still answers `no-store`; the keypad still posts to `/login` with the part 1 target and swap; every part 1 auth test in `tests/test_web_routes.py` passes with only chrome-related assertions updated — a changed **behavioral** assertion is a defect.

**Done when:** full suite green with no reduction in count, `ruff check .` clean, `tests/test_static_css.py` green. Commit: `feat(web): part 1 auth pages adopt the v2 shell`.

---

## Serial step 8

### Task 15: Delete the old dashboard, retire the removed routes, retarget the remaining tests

**Spec:** §2 (removed routes), §4 (migration), §7 (files removed).

**Files:**
- Delete: `web_interface/templates/dashboard.html`, `web_interface/routes/legacy.py`, and the superseded partials: `activity_fragment.html`, `health_fragment.html`, `inventory_table.html`, `inventory_add_form.html`, `inventory_catalog_form.html`, `inventory_placement_form.html`, `logs_fragment.html`, `machine_info.html`, `contacts.html`, `users_list.html`, `user_form.html`, `devices_list.html`
- Modify: `web_interface/routes/__init__.py`, `tests/test_web_routes.py`

**Routes that must no longer answer** (spec §2): `/config/machine`, `/config/contacts`, `/config/payments`, `/config/comms`, `/inventory/new`, `/inventory/copy/{sku}`, `/inventory/edit/{sku}/catalog`, `/inventory/edit/{sku}/placement`, `/inventory/update/{sku}/catalog`, `/inventory/update/{sku}/placement`, `/inventory/delete/{sku}`, `/inventory/add`, `/activity`, `/logs`, `/health` as a fragment, `/action/{command}`, `/faults/{key}/clear`, and the old `/users/*` and `/devices/*` forms superseded by Task 12.

**Behavior to get right:**
- `/status`, `/kpi`, `/pill`, `/screen`, `/screen/body` and every `/login`, `/logout`, `/setup` route **stay**. `/screen` and `/screen/body` must be byte-identical to `origin/main` — verify with a diff, not by eye.
- `GET /inventory` is **not** removed; it is re-pointed at Task 8's restock level. Check the spec's removal list against the new tree before deleting anything: `/inventory` appears in both.
- `partials/keypad.html`, `status_fragment.html`, `kpi_fragment.html`, `screen_body.html`, `pill.html`, `tile.html` and `confirm_button.html` all survive.
- Retarget whatever remains in `tests/test_web_routes.py` to the new URLs, and add a test asserting each removed route now 404s. Where an old test's *behavior* is now covered by an area test file, delete the duplicate rather than keeping both — and **state the count change and its arithmetic in the report**, as part 1 did, so no test is silently lost.

**Tests to write first:** a parametrised test asserting every route in the removal list above answers 404; `/status`, `/kpi`, `/pill`, `/screen`, `/screen/body` still answer 200 for a permitted role; `templates/screen.html` and `templates/partials/screen_body.html` are unchanged from `origin/main`; no template references a deleted partial (grep every `{% include %}` and `{% extends %}` target and assert the file exists).

**Done when:** full suite green; `ruff check .` clean; `grep -rn "dashboard.html\|/config/\|/action/" web_interface/ tests/` returns nothing functional; the test-count change is reconciled line by line in the report. Commit: `refactor(web): delete the old dashboard and its retired routes`.

---

## Serial step 9

### Task 16: Documentation

**Spec:** §7 (`CLAUDE.md`, `README.md` document the level tree, the CSS build, and the removed routes).

Documentation only — no code, no new tests. **Haiku implementer.** Verify with `uv run pytest` and `ruff check .` still green, plus the greps below.

**Files:** `CLAUDE.md`, `README.md`

**What must be written:**
- **`CLAUDE.md`, Web Dashboard section** — replace the tabbed-dashboard description with the v2 shell: `base.html` plus a `Level` tree in `web_interface/levels.py`; `hx-boost` swapping `<main>` with the bar delivered as an out-of-band swap; shared state and `template_context` in `web_interface/context.py`; `routes.py` split into `web_interface/routes/` with one module per area; the eight home tiles filtered by permission; `/status`, `/kpi` and `/pill` as the only fragment endpoints; and the fact that Tailwind and HTMX are vendored in `static/` so the tablet works offline.
- **`CLAUDE.md`, Commands table** — add the Tailwind build row with the **exact** standalone-CLI command, including the `--minify` flag and the input and output paths, and state that `app.css` is committed and that `tests/test_static_css.py` fails CI when a template uses a class the committed file lacks.
- **`README.md`** — a short "Using the dashboard" section: Home is a status strip plus tiles, every level has a URL you can bookmark, Back goes to the parent rather than through history, the pill is always live and one tap from the fault list, and Lock returns to the PIN screen. Mention that the tablet needs no internet.
- **Removed routes** — a list in `CLAUDE.md` of the old URLs and their v2 replacements, so anyone holding a bookmark or an external script knows where things went.

**Done when:** `grep -rn "tabbed\|content panel\|cdn.tailwindcss\|unpkg" CLAUDE.md README.md` returns nothing stale; the documented Tailwind command, run as written, reproduces the committed `app.css`; suite and `ruff` green. Commit: `docs: document the v2 shell, the level tree and the CSS build`.

---

## Definition of Done

1. `uv run pytest` green, `ruff check .` clean.
2. `pyproject.toml` and `uv.lock` unchanged — no new runtime dependency, and no `package.json` or `node_modules` anywhere.
3. `git diff origin/main -- web_interface/templates/screen.html web_interface/templates/partials/screen_body.html` is empty.
4. `grep -rn "unpkg\|cdn.tailwindcss.com" web_interface/` returns nothing.
5. Every level in spec §2 renders 200 for a role holding its gate and 403 for one that does not.
6. `tests/test_static_css.py` passes, and `app.css` was rebuilt by the documented command rather than hand-edited.
7. Every route in Task 15's removal list answers 404.
8. Part 1's security properties still hold: identical login failure bodies, no CDN on the auth pages, `no-store` on plaintext codes, owner self-lockout refused, owner-targeted writes gated on `manage_ownership`.
9. Deviations and the acceptance results from program plan §4 part 2 are in the pull-request description.

## Program goals claimed (program plan §2)

3 (the tablet works with no internet), 4 (status never hidden), 5 (every level has a URL, Back, and Home), 6 (nothing that works today stops working; existing tests retargeted). Goals 1 and 2 were part 1's and must not regress. Goals 7, 8 and 9 belong to parts 3 and 4.

## Spec coverage check

| Spec section | Task(s) |
|---|---|
| §1.1 `base.html`, bar, OOB header, pill | 4, 5 |
| §1.2 `Level`, level context | 2, 1 (`template_context`) |
| §1.3 Home status strip and tile grid | 5 |
| §2 Health levels | 6 |
| §2 Products levels | 7 |
| §2 Inventory restock | 8 |
| §2 Reports | 9 |
| §2 Controls | 10 |
| §2 Tests placeholder | 11 |
| §2 Users levels | 12 |
| §2 Settings levels | 13 |
| §2 removed routes | 15 |
| §3 Tailwind compiled, class test | 3 |
| §3 breakpoints, touch, lists-not-tables, numeric entry, feedback, theme | 4, 5, and every level task |
| §4 partial migration and restyle | 6, 7, 8, 9, 10, 12, 13, 15 |
| §4 part 1 auth pages adopt the shell | 14 |
| §4 `routes.py` split, `context.py` | 1 |
| §5 error handling — `error.html` and the 403/404 handlers | 4 |
| §5 error handling — missing objects, fragment tolerance, `save_config` failure | 6, 7, 8, 9, 13 |
| §6 testing | every task; `tests/test_levels.py` in 2, `tests/test_static_css.py` in 3 |
| §7 files | 1–16 |
| §8 out of scope | nothing built |

