# Routes Flattening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace every `build_router(templates)` closure under `web_interface/routes/` with a module-level `router` and module-level handlers, so each handler is an importable, individually readable function.

**Architecture:** A new `web_interface/templating.py` owns the `Jinja2Templates` singleton (the only thing the closures ever captured). Each route module then declares `router = APIRouter()` at module level, de-indents its handlers and helpers one level, and `attach_routes(app)` just includes the module routers in the existing order. An `ast`-based guard test keeps nested handlers from coming back.

**Tech Stack:** FastAPI `APIRouter`, Jinja2Templates, pytest, `ast`, ruff.

**Spec:** `docs/superpowers/specs/2026-10-11-routes-flattening-design.md`

## Global Constraints

- No handler body, decorator, URL, permission dependency, or docstring changes, other than removing the sentence about `templates` being passed in (`context._render_status`) and deleting `build_router` docstrings that only describe the closure.
- `web_interface/context.py` must not bind the `templates` object as a module-level name (the `tests/test_route_context.py` globals guard would flag it); it imports the module `templating` and reads `templating.templates`.
- Router registration order in `attach_routes` is preserved exactly: `home, screen, health, products, inventory, reports, controls, tests_level, users, settings, auth_routes.public`.
- Every task ends with `uv run pytest` green and `ruff check .` / `ruff format .` clean. Browser tests stay skipped locally (no `ICE_COLDER_BROWSER_TESTS`).
- One commit per converted module, so `git diff -w <commit>^ <commit>` reads as a de-indent plus the import and router lines.
- Three PRs: (a) Tasks 1–6, (b) Tasks 7–10, (c) Tasks 11–14. Each PR description ends with the attribution line from the user's global CLAUDE.md.
- Do not chain shell commands with `&&`; one command per tool call.

## The conversion recipe (applied verbatim by Tasks 3–6, 8–10, 12–14)

For route module `web_interface/routes/<m>.py`:

1. Replace `from fastapi.templating import Jinja2Templates` with `from web_interface.templating import templates`. (`controls.py` mentions `Jinja2Templates` in a docstring; that text stays.)
2. Delete the line `def build_router(templates: Jinja2Templates) -> APIRouter:` and the trailing `    return router` (or `    return public` in `auth.py`).
3. De-indent everything that was inside `build_router` by exactly four spaces, including the `router = APIRouter()` / `public = APIRouter()` line, which becomes module-level. Use a script, not hand editing, e.g. from the scratchpad: read the file, for lines in the `build_router` span strip one leading `"    "` when present (blank lines unchanged), write back.
4. Run `ruff format web_interface/routes/<m>.py`; it may rejoin a few lines that now fit the line length. Accept that.
5. In `web_interface/routes/__init__.py`, change `app.include_router(<m>.build_router(templating.templates))` to `app.include_router(<m>.router)` (`auth_routes.public` for auth).
6. Add `"<m>"` to `CONVERTED` in `tests/test_routes_shape.py`.
7. Run `uv run pytest tests/test_routes_shape.py tests/test_routes_<m>*.py` then the full suite.
8. Commit: `git commit -m "routes: flatten <m>.py to module-level handlers"`.

The guard test is the TDD red for each conversion: adding the module name to `CONVERTED` before converting makes it fail, converting makes it pass. Do the add first, run, see the failure naming `build_router` and the nested handlers, then convert.

---

### Task 1: `web_interface/templating.py` and the `attach_routes(app)` signature

**Files:**
- Create: `web_interface/templating.py`
- Modify: `web_interface/server.py:1-19`
- Modify: `web_interface/routes/__init__.py:17,54,143-157`
- Test: `tests/test_templating.py` (new)

**Interfaces:**
- Produces: `web_interface.templating.templates: Jinja2Templates` with filters `humanize_seconds` and `sku_url_segment` registered on `templates.env.filters`.
- Produces: `web_interface.routes.attach_routes(app: FastAPI) -> None` (the `templates` parameter is gone).

- [ ] **Step 1: Write the failing test** `tests/test_templating.py`: import `templates` from `web_interface.templating`; assert `"humanize_seconds"` and `"sku_url_segment"` are in `templates.env.filters`; assert `templates.get_template("base.html")` loads. Run it; expect `ModuleNotFoundError`.
- [ ] **Step 2: Create `web_interface/templating.py`** exactly as the spec's section 1 shows (three statements plus two imports, module docstring explaining it is a constant, not an injected dependency).
- [ ] **Step 3: `server.py`**: delete the `Jinja2Templates` import, the three `templates` lines, and the `filters` import; call `routes.attach_routes(app)`.
- [ ] **Step 4: `routes/__init__.py`**: drop the `Jinja2Templates` import; add `from web_interface.templating import templates`; signature `def attach_routes(app: FastAPI) -> None`; the 404/403 handler keeps using the name `templates` (now the module import); every `include_router` line becomes `app.include_router(<m>.build_router(templates))` for now (unchanged in effect). Update the docstring's one sentence that says `templates` is threaded in.
- [ ] **Step 5: Run** `uv run pytest tests/test_templating.py tests/test_routes_controls.py tests/test_route_context.py`, then the full suite. Expect all green (nothing behavioural changed).
- [ ] **Step 6: Commit** `routes: own the Jinja2Templates singleton in web_interface/templating.py`.

### Task 2: `context._render_status(request)` and the shape guard

**Files:**
- Modify: `web_interface/context.py:407-445` (`_render_status`)
- Modify: `web_interface/routes/home.py:237`
- Create: `tests/test_routes_shape.py`

**Interfaces:**
- Produces: `web_interface.context._render_status(request: Request)` (one parameter).
- Produces: `tests/test_routes_shape.py` with module constant `CONVERTED: frozenset[str]` (module basenames without `.py`) and two tests, `test_no_build_router` and `test_no_nested_functions`, each failing with a list of `path:line` hits for every module in `CONVERTED`.

- [ ] **Step 1: `context.py`**: add `from web_interface import templating` (module import, not the object — see Global Constraints); change the signature to `async def _render_status(request: Request)`; the `TemplateResponse` call uses `templating.templates`; delete the docstring sentence "`templates` is passed in explicitly since this function lives outside any build_router(templates) closure." `home.py:237` becomes `return await context._render_status(request)`.
- [ ] **Step 2: Run** `uv run pytest tests/test_route_context.py tests/test_routes_home.py` (or whichever file covers `GET /status`; `grep -rl '"/status"' tests`). Expect green, including `test_globals_covers_every_module_global`.
- [ ] **Step 3: Write `tests/test_routes_shape.py`.** Module docstring states the rule (every route module exposes a module-level `router`; no route handler is nested in another function; `build_router` must not exist). `ROUTES_DIR = Path(__file__).parent.parent / "web_interface" / "routes"`. `CONVERTED = frozenset()` for now. Helper `_hits(module_name) -> tuple[list[str], list[str]]` parses the file with `ast.parse`, returns `(build_router_hits, nested_hits)` as `"<relpath>:<line>"` strings: a `build_router` hit is any `FunctionDef`/`AsyncFunctionDef` named `build_router` anywhere; a nested hit is any *decorated* (non-empty `decorator_list`) `FunctionDef`/`AsyncFunctionDef` found by `ast.walk` *inside the body* of another `FunctionDef`/`AsyncFunctionDef`; undecorated closures are allowed because hoisting them would change handler bodies (walk each function's body nodes; skip `ClassDef` subtrees so methods are not hits; `Lambda` is not a hit). Two tests parametrised over `sorted(CONVERTED)` asserting each list is empty with the list in the assertion message. With `CONVERTED` empty, pytest reports the two tests as skipped-by-empty-parametrisation; that is expected.
- [ ] **Step 4: Prove the guard bites.** Temporarily set `CONVERTED = frozenset({"controls"})`, run `uv run pytest tests/test_routes_shape.py -v`, expect both tests FAIL listing `web_interface/routes/controls.py:13` for `build_router` and the three handler lines as nested. Revert to `frozenset()`.
- [ ] **Step 5: Run** full suite, `ruff check .`, `ruff format .`. Commit `routes: drop the templates parameter from _render_status; add the shape guard`.

### Task 3: Convert `controls.py`

**Files:** Modify `web_interface/routes/controls.py` (whole file, 80 lines), `web_interface/routes/__init__.py`, `tests/test_routes_shape.py`.

- [ ] **Step 1:** Add `"controls"` to `CONVERTED`; run `uv run pytest tests/test_routes_shape.py -v`; expect FAIL.
- [ ] **Step 2:** Apply the conversion recipe steps 1–5.
- [ ] **Step 3:** `uv run pytest tests/test_routes_shape.py tests/test_routes_controls.py -v`; expect PASS. Full suite; ruff.
- [ ] **Step 4:** Commit `routes: flatten controls.py to module-level handlers`.

### Task 4: Convert `home.py`

**Files:** Modify `web_interface/routes/home.py:188-258`, `web_interface/routes/__init__.py`, `tests/test_routes_shape.py`.

- [ ] **Step 1:** Add `"home"` to `CONVERTED`; run the guard; expect FAIL.
- [ ] **Step 2:** Apply the recipe. Note `home.py`'s module docstring line 7 mentions `attach_routes`; leave it.
- [ ] **Step 3:** Guard plus `grep -rl "/status\|/kpi\|/pill" tests` files, then full suite; ruff.
- [ ] **Step 4:** Commit `routes: flatten home.py to module-level handlers`.

### Task 5: Convert `screen.py`

**Files:** Modify `web_interface/routes/screen.py:20-87`, `web_interface/routes/__init__.py`, `tests/test_routes_shape.py`.

- [ ] **Step 1:** Add `"screen"` to `CONVERTED`; guard FAILS.
- [ ] **Step 2:** Apply the recipe. The `_screen_context` helper becomes module-level. Templates under `templates/screen*.html` are not touched (CLAUDE.md's byte-identical claim is about the page, and it still holds).
- [ ] **Step 3:** Guard, `grep -rl '"/screen' tests` files, full suite; ruff.
- [ ] **Step 4:** Commit `routes: flatten screen.py to module-level handlers`.

### Task 6: Convert `inventory.py`; open PR (a)

**Files:** Modify `web_interface/routes/inventory.py:129-299`, `web_interface/routes/__init__.py`, `tests/test_routes_shape.py`.

- [ ] **Step 1:** Add `"inventory"` to `CONVERTED`; guard FAILS.
- [ ] **Step 2:** Apply the recipe.
- [ ] **Step 3:** Guard, `tests/test_routes_inventory*.py`, full suite; ruff.
- [ ] **Step 4:** Commit `routes: flatten inventory.py to module-level handlers`.
- [ ] **Step 5:** Push the branch and open PR (a) titled `Routes flattening 1/3: templating singleton; controls, home, screen, inventory`. Body: link the spec; state that no handler body changed and that `git diff -w` per commit shows the de-indent; list the four converted modules and the seven still to go.

### Task 7: Convert `health.py`

**Files:** Modify `web_interface/routes/health.py:201-768`, `web_interface/routes/__init__.py`, `tests/test_routes_shape.py`.

- [ ] **Step 1:** Add `"health"` to `CONVERTED`; guard FAILS listing 13 handlers plus `_render_fault_list_oob` and `_pay104_active`.
- [ ] **Step 2:** Apply the recipe. `_pay104_lock` is already module-level; the hoisted `_pay104_active` and `pay104_record_sale`/discard handlers keep referencing it unchanged.
- [ ] **Step 3:** Guard, `tests/test_routes_health*.py`, `tests/test_pay104*.py` if present, full suite; ruff.
- [ ] **Step 4:** Commit `routes: flatten health.py to module-level handlers`.

### Task 8: Convert `products.py`

**Files:** Modify `web_interface/routes/products.py:111-536`, `web_interface/routes/__init__.py`, `tests/test_routes_shape.py`.

- [ ] **Step 1:** Add `"products"` to `CONVERTED`; guard FAILS.
- [ ] **Step 2:** Apply the recipe. `_require_any` is already module-level in this file; `_render_product_form` hoists next to it.
- [ ] **Step 3:** Guard, `tests/test_routes_products*.py`, full suite; ruff.
- [ ] **Step 4:** Commit `routes: flatten products.py to module-level handlers`.

### Task 9: Convert `reports.py`

**Files:** Modify `web_interface/routes/reports.py:213-627`, `web_interface/routes/__init__.py`, `tests/test_routes_shape.py`.

- [ ] **Step 1:** Add `"reports"` to `CONVERTED`; guard FAILS listing 7 handlers and the six `_compute_*`/`_render` helpers.
- [ ] **Step 2:** Apply the recipe.
- [ ] **Step 3:** Guard, `tests/test_routes_reports*.py`, full suite; ruff.
- [ ] **Step 4:** Commit `routes: flatten reports.py to module-level handlers`.

### Task 10: Convert `settings.py`; open PR (b)

**Files:** Modify `web_interface/routes/settings.py:127-720`, `web_interface/routes/__init__.py`, `tests/test_routes_shape.py`.

- [ ] **Step 1:** Add `"settings"` to `CONVERTED`; guard FAILS.
- [ ] **Step 2:** Apply the recipe. The seven `_render_*_form` helpers hoist to module level.
- [ ] **Step 3:** Guard, `tests/test_routes_settings*.py`, full suite; ruff.
- [ ] **Step 4:** Commit `routes: flatten settings.py to module-level handlers`.
- [ ] **Step 5:** Push and open PR (b) `Routes flattening 2/3: health, products, reports, settings` with the same body shape as PR (a).

### Task 11: Convert `tests_level.py`

**Files:** Modify `web_interface/routes/tests_level.py:476-1020`, `web_interface/routes/__init__.py`, `tests/test_routes_shape.py`.

- [ ] **Step 1:** Add `"tests_level"` to `CONVERTED`; guard FAILS listing 12 handlers.
- [ ] **Step 2:** Apply the recipe. The 475 lines of module-level helpers above `build_router` (`_subsystem_summary`, `_acquire_lease_or_refusal`, …) are untouched. CLAUDE.md cites `web_interface/routes/tests_level.py:308` for `_acquire_lease_or_refusal`; that line number does not move because nothing above the old `build_router` changes.
- [ ] **Step 3:** Guard, `tests/test_routes_tests*.py` and `tests/test_tests_level*.py` (`grep -rl '"/tests' tests`), full suite; ruff.
- [ ] **Step 4:** Commit `routes: flatten tests_level.py to module-level handlers`.

### Task 12: Convert `users.py`

**Files:** Modify `web_interface/routes/users.py:176-841`, `web_interface/routes/__init__.py`, `tests/test_routes_shape.py`.

- [ ] **Step 1:** Add `"users"` to `CONVERTED`; guard FAILS listing 19 handlers and six helpers.
- [ ] **Step 2:** Apply the recipe.
- [ ] **Step 3:** Guard, `tests/test_routes_users*.py`, `tests/test_users_codes*.py`, full suite; ruff.
- [ ] **Step 4:** Commit `routes: flatten users.py to module-level handlers`.

### Task 13: Convert `auth.py`

**Files:** Modify `web_interface/routes/auth.py:37-724`, `web_interface/routes/__init__.py`, `tests/test_routes_shape.py`.

- [ ] **Step 1:** Add `"auth"` to `CONVERTED`; guard FAILS listing 13 handlers and ten helpers.
- [ ] **Step 2:** Apply the recipe with the router named `public` (module-level `public = APIRouter()`; `__init__.py` includes `auth_routes.public`, still last).
- [ ] **Step 3:** Guard, `tests/test_auth*.py`, `tests/test_routes_auth*.py`, `tests/test_setup*.py` (`grep -rl '"/login\|"/setup' tests`), full suite; ruff.
- [ ] **Step 4:** Commit `routes: flatten auth.py to module-level handlers`.

### Task 14: Close the guard, update CLAUDE.md, open PR (c)

**Files:** Modify `tests/test_routes_shape.py`, `CLAUDE.md` (Web Dashboard section), `web_interface/routes/__init__.py` docstring.

- [ ] **Step 1:** In `tests/test_routes_shape.py` replace `CONVERTED` with a discovered list: every `*.py` in `ROUTES_DIR` except `__init__.py`. Add `test_every_module_exposes_a_router`: import each module and assert it has a module-level `APIRouter` named `router`, except `auth`, whose router is named `public`. Run the guard: all green over eleven modules.
- [ ] **Step 2:** Prove the closed guard bites: in the scratchpad, copy `controls.py` to `web_interface/routes/_tmp_probe.py` with a nested `def` added inside a handler, run the guard, expect FAIL naming that file, delete the probe. (Do not commit the probe.)
- [ ] **Step 3:** `routes/__init__.py` docstring: remove the sentence describing routers "built inside one attach_routes() closure"; state that each area module exposes a module-level `router` (`public` for auth) and `attach_routes(app)` includes them in order.
- [ ] **Step 4:** CLAUDE.md, Web Dashboard paragraph: after "`routes/__init__.py` wires them all with `attach_routes`", add that every area module exposes a module-level `router` (`auth.py`'s is `public`), handlers and helpers are module-level functions, `web_interface/templating.py` owns the shared `Jinja2Templates` singleton, and `tests/test_routes_shape.py` fails on any nested handler or a reintroduced `build_router`. Update the Commands table only if a command changed (none did).
- [ ] **Step 5:** Full suite; ruff. Then `uv run python main.py` in the background, `curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:26123/` expecting `200` or a `303` to `/login`, then stop it. Commit `routes: close the shape guard over every module; docs`.
- [ ] **Step 6:** Push and open PR (c) `Routes flattening 3/3: tests_level, users, auth; guard closed; docs`.

---

## Self-review

- **Spec coverage:** §1 singleton → Task 1; §2 module shape and `attach_routes` → Tasks 1, 3–13; `_render_status(request)` → Task 2; §3 guard → Task 2 (scoped) and Task 14 (closed); §4 three PRs → Tasks 6, 10, 14; CLAUDE.md → Task 14; manual startup check → Task 14 Step 5.
- **Placeholders:** none; every conversion task names the exact line span and the recipe step list.
- **Type consistency:** `attach_routes(app)`, `templating.templates`, `CONVERTED: frozenset[str]`, `_render_status(request)` are used identically throughout.
