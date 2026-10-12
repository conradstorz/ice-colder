# Routes Split Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn `web_interface/routes/{tests_level,users,auth}.py` into packages of small sub-modules, each with its own `APIRouter`, with no behaviour change.

**Architecture:** Each package `__init__.py` builds the attribute the rest of the app already uses (`router`, or `public` for `auth`) by including the sub-routers in a fixed order, and re-exports the names tests import. Handlers and helpers move byte-for-byte; only import blocks and cross-module references change. The shape guard learns to discover sub-modules recursively.

**Tech Stack:** FastAPI `APIRouter`, `ast` (guard and move verification), pytest, ruff, uv.

**Spec:** `docs/superpowers/specs/2026-10-12-routes-split-design.md` — the sub-module tables in §1 and the include orders in §2 are binding.

## Global Constraints

- Branch `routes-split` off `main`; one PR; never commit to `main`.
- Never chain shell commands with `&&`; one command per invocation. Use `uv run ...`, never `pip`.
- A moved function, constant, decorator, or docstring is **byte-for-byte identical** to the original. The only permitted edits are the import block, the `router = APIRouter()` line, the module docstring split, and the cross-module references the spec names. Do not rename, reorder lines inside a function, or "tidy".
- Sub-modules of `auth` name their router `router`; only `auth/__init__.py` exposes `public`.
- Package include order is exactly the spec's §2 order, with the shadowing comment.
- Every commit: `uv run ruff check .`, `uv run ruff format --check .`, and `uv run pytest` green (browser tests skip locally; that is expected).
- Commit messages end with `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`.

## Move recipe (Tasks 2–4)

1. `git mv web_interface/routes/<area>.py web_interface/routes/<area>/__init__.py` so git tracks the rename, then cut the sub-module contents out of `__init__.py` into the new files. Each sub-module starts with: its share of the module docstring, the imports it needs (copy the original import block, then let `uv run ruff check --fix` drop unused names), `router = APIRouter()`, then the functions and constants in the **same relative order** they had in the original file.
2. `__init__.py` after the cut holds only: the area-overview docstring, `from fastapi import APIRouter`, the sub-module imports, the re-exports the spec lists (tests_level only), `__all__`, `router = APIRouter()` (or `public`), and the `include_router` calls in spec order with the shadowing comment.
3. Verify the move with the script below before running tests. It parses the original file from `main` and every file in the new package and asserts each top-level function and assignment in the original appears exactly once in the package with identical source text (decorators included):

```python
# scratch script; do not commit
import ast, pathlib, subprocess, sys
area = sys.argv[1]
old = subprocess.run(["git", "show", f"main:web_interface/routes/{area}.py"], capture_output=True, text=True, check=True).stdout
def defs(src):
    tree = ast.parse(src); lines = src.splitlines()
    out = {}
    for n in tree.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            start = (n.decorator_list[0].lineno if n.decorator_list else n.lineno) - 1
            out[n.name] = "\n".join(lines[start:n.end_lineno])
        elif isinstance(n, ast.Assign) and isinstance(n.targets[0], ast.Name) and n.targets[0].id not in ("router", "public", "__all__"):
            out[n.targets[0].id] = "\n".join(lines[n.lineno - 1:n.end_lineno])
        elif isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name):
            out[n.target.id] = "\n".join(lines[n.lineno - 1:n.end_lineno])
    return out
expected = defs(old)
found = {}
for p in sorted(pathlib.Path(f"web_interface/routes/{area}").glob("*.py")):
    for k, v in defs(p.read_text(encoding="utf-8")).items():
        found.setdefault(k, []).append((p.name, v))
bad = False
for k, v in expected.items():
    hits = found.get(k, [])
    if len(hits) != 1 or hits[0][1] != v:
        bad = True; print("MISMATCH", k, [h[0] for h in hits])
extra = set(found) - set(expected)
if extra: bad = True; print("EXTRA", sorted(extra))
print("OK" if not bad else "FAIL")
```

Run: `uv run python <scratch>/verify_move.py <area>` → must print `OK`.

4. `uv run ruff check --fix .` then `uv run ruff format .`; re-run the verify script (ruff must not have touched a function body — if it did, the original had a formatting difference and you must restore the original text).

---

### Task 1: Guard discovers sub-packages

**Files:**
- Modify: `tests/test_routes_shape.py`

**Interfaces:**
- Produces: `ROUTE_MODULES` as dotted names (`"tests_level"`, `"tests_level.commands"`); `_hits(module_name)` resolves a dotted name to its file (`<a>/<b>.py`, or `<a>/__init__.py` when the dotted name is a package).

- [ ] **Step 1:** Change discovery to `ROUTES_DIR.rglob("*.py")`, skipping only `ROUTES_DIR / "__init__.py"`. Module name = path relative to `ROUTES_DIR`, suffix dropped, parts joined by `.`, trailing `.__init__` removed. Keep `sorted`.
- [ ] **Step 2:** Add a `_path_for(module_name) -> Path` helper used by `_hits`: try `ROUTES_DIR / <parts>.py`, else `ROUTES_DIR / <parts> / "__init__.py"`.
- [ ] **Step 3:** Update the module docstring's "discovered from every `*.py` in the routes directory" sentence to say recursively, naming the dotted form.
- [ ] **Step 4:** In `test_discovery_finds_route_modules`, keep the `auth`/`home` asserts; add a comment that a sub-module assert (`"tests_level.commands"`) is added in Task 2 once it exists. (Adding it now would fail.)
- [ ] **Step 5:** Run `uv run pytest tests/test_routes_shape.py -q` → all pass on the flat layout (no behaviour change yet).
- [ ] **Step 6:** Commit: `tests: route shape guard discovers sub-packages`.

---

### Task 2: `tests_level` package

**Files:**
- Rename: `web_interface/routes/tests_level.py` → `web_interface/routes/tests_level/__init__.py`
- Create: `web_interface/routes/tests_level/{overview,log,sale,lease,commands}.py`
- Modify: `tests/test_routes_shape.py` (discovery assert)
- Tests: `tests/test_routes_tests.py`, `tests/test_routes_shape.py`, `tests/test_web_routes.py`

**Interfaces:**
- Consumes: spec §1 table row `tests_level/`; spec §2 order `overview, log, sale, lease, commands`.
- Produces: `web_interface.routes.tests_level` exposing `router`, `contract_match`, `testable_commands`, `recent_test_runs`, `_run_command`, `WATER_VALVE_SECONDS_RANGE`, `POWER_CYCLE_DWELL_RANGE`, `POWER_CYCLE_DWELL_DEFAULT` (all in `__all__`); `tests_level.lease._acquire_lease_or_refusal`, `tests_level.lease._service_state`; `tests_level.commands.contract_match`/`testable_commands`.

- [ ] **Step 1:** Apply the move recipe with the spec's table. Cross-module references: `commands.py` does `from web_interface.routes.tests_level.lease import _acquire_lease_or_refusal`; `overview.py` imports `_service_state` from `.lease` and `contract_match`, `testable_commands` from `.commands`. Use absolute imports (`from web_interface.routes.tests_level.lease import ...`) to match the codebase style.
- [ ] **Step 2:** `__init__.py` include order and comment:
  ```python
  # Order matters: commands.router holds the parametrised /tests/{subsystem}
  # and /tests/{subsystem}/{command:path} routes, which would otherwise capture
  # /tests/log, /tests/sale, /tests/standby/confirm, /tests/run-all, /tests/end,
  # /tests/takeover, and /tests/runs/{run_id}/verdict. It goes last.
  ```
- [ ] **Step 3:** Run the verify script for `tests_level` → `OK`.
- [ ] **Step 4:** Add `assert "tests_level.commands" in ROUTE_MODULES` to `test_discovery_finds_route_modules`; remove the Task 1 placeholder comment.
- [ ] **Step 5:** Run `uv run pytest tests/test_routes_tests.py tests/test_routes_shape.py tests/test_web_routes.py -q` → pass. Then `uv run pytest -q` → pass.
- [ ] **Step 6:** Commit: `routes: split tests_level into a package`.

---

### Task 3: `users` package

**Files:**
- Rename: `web_interface/routes/users.py` → `web_interface/routes/users/__init__.py`
- Create: `web_interface/routes/users/{ownership,devices,accounts}.py`
- Modify: `tests/test_web_routes.py` lines ~2791 and ~2807 (`test_report_is_emailed_to_the_owner_and_names_the_users`, `test_report_never_contains_a_hash_or_pin`): `monkeypatch.setattr(routes.users, "send_email", ...)` → `monkeypatch.setattr(routes.users.ownership, "send_email", ...)`.
- Tests: `tests/test_routes_users.py`, `tests/test_web_routes.py`, `tests/test_routes_shape.py`

**Interfaces:**
- Consumes: spec §1 `users/` rows; spec §2 order `ownership, devices, accounts`.
- Produces: `web_interface.routes.users.router`; `users.ownership.send_email` as the monkeypatch target.

- [ ] **Step 1:** Apply the move recipe. No cross-module references between the three sub-modules (verified by the xref scan during design); if ruff reports an undefined name, stop and report rather than inventing an import.
- [ ] **Step 2:** `__init__.py` include order comment:
  ```python
  # Order matters: accounts.router holds /users/{user_id} (GET and POST), which
  # would otherwise capture /users/codes, /users/ownership, /users/report,
  # /users/transfer and /users/codes/regenerate. It goes last.
  ```
- [ ] **Step 3:** Verify script for `users` → `OK`.
- [ ] **Step 4:** Retarget the two monkeypatches.
- [ ] **Step 5:** `uv run pytest tests/test_routes_users.py tests/test_web_routes.py tests/test_routes_shape.py -q` → pass; then `uv run pytest -q` → pass.
- [ ] **Step 6:** Commit: `routes: split users into a package`.

---

### Task 4: `auth` package

**Files:**
- Rename: `web_interface/routes/auth.py` → `web_interface/routes/auth/__init__.py`
- Create: `web_interface/routes/auth/{login,setup}.py`
- Modify: `tests/test_web_routes.py` lines ~1487, ~1535 (`test_otp_path_with_a_stubbed_mailer`, `test_smtp_failure_tells_the_user_to_use_an_emergency_code`) → `routes.auth.login`; lines ~2050, ~2125 (`test_email_button_sends_all_codes_through_the_mailer`, `test_email_send_failure_is_an_error_not_a_crash`) → `routes.auth.setup`.
- Tests: `tests/test_web_auth.py`, `tests/test_startup_policy.py`, `tests/test_web_routes.py`, `tests/test_routes_shape.py`

**Interfaces:**
- Consumes: spec §1 `auth/` rows; order `login, setup`.
- Produces: `web_interface.routes.auth.public`; `auth.login.send_email`, `auth.setup.send_email` as monkeypatch targets. Sub-module routers are named `router`.

- [ ] **Step 1:** Apply the move recipe. `__init__.py` defines `public = APIRouter()` and includes `login.router` then `setup.router` (comment: "no overlapping paths; original file order kept").
- [ ] **Step 2:** Verify script for `auth` → `OK`.
- [ ] **Step 3:** Retarget the four monkeypatches.
- [ ] **Step 4:** `uv run pytest tests/test_web_auth.py tests/test_startup_policy.py tests/test_web_routes.py tests/test_routes_shape.py -q` → pass; then `uv run pytest -q` → pass.
- [ ] **Step 5:** Commit: `routes: split auth into a package`.

---

### Task 5: Documentation and startup check

**Files:**
- Modify: `CLAUDE.md` — line ~109 (Web Dashboard `routes/` sentence), line ~95 and ~198+ (`web_interface/routes/tests_level.py` references, including `tests_level.py:308`).

- [ ] **Step 1:** In the Web Dashboard paragraph, after the list of area modules, add one sentence: `tests_level/`, `users/`, and `auth/` are packages whose `__init__.py` builds the area router from sub-routers (`tests_level`: `overview`, `log`, `sale`, `lease`, `commands`; `users`: `ownership`, `devices`, `accounts`; `auth`: `login`, `setup`), included in that order because a later sub-router's parametrised path would otherwise shadow an earlier one's static path. Note the guard discovers sub-modules recursively.
- [ ] **Step 2:** Replace every `web_interface/routes/tests_level.py` reference with the package or the sub-module that now holds the named function (`_acquire_lease_or_refusal` → `web_interface/routes/tests_level/lease.py`, no line number; `testable_commands`/`post_test_command`/`tests_run_all` → `commands.py`; `POST /tests/sale` → `sale.py`). Do not rewrite surrounding sentences.
- [ ] **Step 3:** Startup check: `uv run python main.py` in the background, `curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:26123/` → `303`, then stop it.
- [ ] **Step 4:** `uv run pytest -q` → pass; `uv run ruff check .`; `uv run ruff format --check .`.
- [ ] **Step 5:** Commit: `docs: route packages in CLAUDE.md`.
