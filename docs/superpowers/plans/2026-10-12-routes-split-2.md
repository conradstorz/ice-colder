# Routes Split 2 (health, settings) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn `web_interface/routes/{health,settings}.py` into packages of small sub-modules, each with its own `APIRouter` (one helper sub-module without), with no behaviour change.

**Architecture:** Each package `__init__.py` builds `router` by including the sub-routers in the original file's order and re-exports the names tests and `tests_level/` import. Handlers, helpers and comment blocks move byte-for-byte; only import blocks and cross-module references change. The shape guard learns an explicit helper-module allowlist.

**Tech Stack:** FastAPI `APIRouter`, `ast` (guard and move verification), pytest, ruff, uv.

**Spec:** `docs/superpowers/specs/2026-10-12-routes-split-2-design.md` — the sub-module table in §1, the include orders in §2 and the guard rule in §3 are binding.

## Global Constraints

- Branch `routes-split-2` off `main`; one PR; never commit to `main`.
- Never chain shell commands with `&&`; one command per invocation. Use `uv run ...`, never `pip`.
- A moved function, constant, decorator, docstring, or comment block is **byte-for-byte identical** to the original. The only permitted edits are the import block, the `router = APIRouter()` line, the module docstring split, and the cross-module references the spec names. Do not rename, reorder lines inside a function, or "tidy".
- Package include order is exactly the spec's §2 order, with a comment saying no sub-router's parametrised path shadows another's static one, so file order is kept.
- Every commit: `uv run ruff check .`, `uv run ruff format --check .`, and `uv run pytest` green (browser tests skip locally; that is expected).
- Commit messages end with `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`.

## Move recipe (Tasks 2–3)

1. `git mv web_interface/routes/<area>.py web_interface/routes/<area>/__init__.py` so git tracks the rename, then cut the sub-module contents out of `__init__.py` into the new files. Each sub-module starts with: its share of the module docstring (or the one-sentence docstring the spec allows), the imports it needs (copy the original import block, then let `uv run ruff check --fix` drop unused names), `router = APIRouter()` (not in `settings/secrets.py`), then the functions and constants in the **same relative order** they had in the original file.
2. `__init__.py` after the cut holds only: the area-overview docstring, `from fastapi import APIRouter`, the sub-module imports, the re-exports the spec lists, `__all__`, `router = APIRouter()`, and the `include_router` calls in spec order with the order comment.
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
        elif isinstance(n, ast.Assign) and isinstance(n.targets[0], ast.Name) and n.targets[0].id not in ("router", "__all__"):
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

4. The verify script sees only defs and assignments. **Module-level comment blocks and section dividers (`# ---- /settings/machine`, the `# Task 14 review finding 1:` block above `_pay104_lock`, the `# active_faults() reports ...` block above `_PAYMENT_BLOCKING_CODES`, the `# A fault's key ...` block above `_SELECTOR_SAFE_KEY_RE`, the `# Fixed placeholder ...` block above `SECRET_MASK`) move with the definition they precede**, verbatim. Then run the line-multiset check below; it must print `OK`:

```python
# scratch script; do not commit
import collections, pathlib, subprocess, sys
area = sys.argv[1]
old = subprocess.run(["git", "show", f"main:web_interface/routes/{area}.py"], capture_output=True, text=True, check=True).stdout
want = collections.Counter(l for l in old.splitlines() if l.strip())
have = collections.Counter()
for p in pathlib.Path(f"web_interface/routes/{area}").glob("*.py"):
    have.update(l for l in p.read_text(encoding="utf-8").splitlines() if l.strip())
# Allowed to vanish: the original import block (every line of every
# Import/ImportFrom node) and the router line.
import ast
tree = ast.parse(old); lines = old.splitlines()
import_lines = set()
for n in tree.body:
    if isinstance(n, (ast.Import, ast.ImportFrom)):
        import_lines.update(lines[n.lineno - 1:n.end_lineno])
import_lines.add("router = APIRouter()")
missing = {l: n - have[l] for l, n in want.items() if have[l] < n and l not in import_lines}
for l, n in missing.items(): print("MISSING", n, repr(l))
print("OK" if not missing else "FAIL")
```

Any `MISSING` line that is not part of the original import block or a docstring sentence the spec lets you drop is a defect: restore it.

5. `uv run ruff check --fix .` then `uv run ruff format .`; re-run both scripts (ruff must not have touched a function body — if it did, the original had a formatting difference and you must restore the original text).

---

### Task 1: Guard learns helper modules

**Files:**
- Modify: `tests/test_routes_shape.py`

**Interfaces:**
- Produces: `HELPER_MODULES: frozenset[str]` of dotted names exempt from the router rule; `_ROUTE_DECORATOR_ATTRS = frozenset({"get", "post", "put", "patch", "delete", "api_route"})`.

- [ ] **Step 1:** Below `ROUTER_ATTR`, add:

```python
# Helper sub-modules: shared code with no routes, so no router of their own.
# The guard checks instead that no route decorator appears in them.
HELPER_MODULES = frozenset({"settings.secrets"})

_ROUTE_DECORATOR_ATTRS = frozenset({"get", "post", "put", "patch", "delete", "api_route"})
```

- [ ] **Step 2:** Add a helper next to `_hits`:

```python
def _route_decorators(module_name: str) -> list[str]:
    """`path:line` of every function decorated with a `.get/.post/...` call."""
    path = _path_for(module_name)
    rel = path.relative_to(ROUTES_DIR.parent.parent).as_posix()
    tree = ast.parse(path.read_text(encoding="utf-8"))
    hits = []
    for node in ast.walk(tree):
        if isinstance(node, _FUNCS):
            for dec in node.decorator_list:
                call = dec if isinstance(dec, ast.Call) else None
                func = call.func if call else dec
                if isinstance(func, ast.Attribute) and func.attr in _ROUTE_DECORATOR_ATTRS:
                    hits.append(f"{rel}:{node.lineno}")
    return hits
```

- [ ] **Step 3:** Change `test_every_module_exposes_a_router` so a helper module takes the other branch:

```python
@pytest.mark.parametrize("module_name", ROUTE_MODULES)
def test_every_module_exposes_a_router(module_name: str) -> None:
    module = importlib.import_module(f"web_interface.routes.{module_name}")
    if module_name in HELPER_MODULES:
        routers = [n for n, v in vars(module).items() if isinstance(v, APIRouter)]
        assert not routers, f"helper {module_name} must not expose an APIRouter: {routers}"
        decorated = _route_decorators(module_name)
        assert not decorated, f"helper {module_name} defines routes: {decorated}"
        return
    attr = ROUTER_ATTR.get(module_name, "router")
    assert isinstance(getattr(module, attr, None), APIRouter), (
        f"{module_name} must expose a module-level APIRouter named {attr!r}"
    )
```

- [ ] **Step 4:** Add a unit test for `_route_decorators` that does not depend on the package existing yet: write a temp module under `tmp_path`, monkeypatch `ROUTES_DIR`-independent behaviour by calling the AST part directly. Simplest: factor the AST scan into `_route_decorators_in(tree: ast.AST, rel: str) -> list[str]` that `_route_decorators` calls, and test it:

```python
def test_route_decorators_in_finds_router_calls():
    src = "r = object()\n@r.get('/x')\ndef h(): ...\n@r.post('/y')\nasync def g(): ...\n@other\ndef k(): ...\n"
    hits = _route_decorators_in(ast.parse(src), "m.py")
    assert hits == ["m.py:3", "m.py:5"]
```

- [ ] **Step 5:** Update the module docstring: add one sentence saying a module listed in `HELPER_MODULES` is shared code with no routes, must not expose an `APIRouter`, and must not carry a route decorator.
- [ ] **Step 6:** In `test_discovery_finds_route_modules`, add a comment that `"health.faults"` and `"settings.secrets"` asserts are added in Tasks 2–3 once they exist.
- [ ] **Step 7:** `uv run pytest tests/test_routes_shape.py -q` → all pass (the allowlisted name does not exist yet; `ROUTE_MODULES` never contains it, so no test runs against it). `uv run ruff check .`, `uv run ruff format --check .`.
- [ ] **Step 8:** Commit: `tests: route shape guard allows helper sub-modules`.

---

### Task 2: `health` package

**Files:**
- Rename: `web_interface/routes/health.py` → `web_interface/routes/health/__init__.py`
- Create: `web_interface/routes/health/{overview,subsystems,faults,recovery,availability,logs}.py`
- Modify: `tests/test_routes_shape.py` (one assert)

**Interfaces:**
- Consumes: nothing new.
- Produces: `web_interface.routes.health.router`; re-exports `dom_safe_key`, `fault_gate`, `_subsystem_summary` (used by `tests/test_routes_health.py`, `tests_level/overview.py`, `tests_level/commands.py` — those files do not change).

- [ ] **Step 1:** Follow the Move recipe with `area=health`. Placement is spec §1's `health/` rows. Section-divider comments (`# ---- ...`) and the three comment blocks named in recipe step 4 move with their definitions. The whole module docstring stays in `__init__.py`; each sub-module gets a one-sentence docstring naming its routes.
- [ ] **Step 2:** Cross-module imports, exactly: `overview.py` imports `_availability_context_line` from `web_interface.routes.health.availability`, `_faults_with_age` from `web_interface.routes.health.faults`, `_subsystem_summary` from `web_interface.routes.health.subsystems`; `recovery.py` imports `_render_fault_list_oob` and `dom_safe_key` from `web_interface.routes.health.faults`. Use absolute `web_interface.routes.health.<sub>` imports, matching `tests_level/__init__.py`.
- [ ] **Step 3:** `__init__.py` ends with:

```python
router = APIRouter()

# No sub-router's parametrised path shadows another's static one
# (/health/subsystems/{name} has no static sibling; /health/faults/{key}/clear
# and /health/faults/PAY-104/record-sale|discard differ after the key), so the
# original file order is kept.
router.include_router(overview.router)
router.include_router(subsystems.router)
router.include_router(faults.router)
router.include_router(recovery.router)
router.include_router(availability.router)
router.include_router(logs.router)
```

with `__all__ = ["_subsystem_summary", "dom_safe_key", "fault_gate", "router"]` above it.

- [ ] **Step 4:** In `tests/test_routes_shape.py::test_discovery_finds_route_modules` add `assert "health.faults" in ROUTE_MODULES`.
- [ ] **Step 5:** Both verify scripts print `OK`. `uv run pytest -q` all pass. `uv run ruff check .`, `uv run ruff format --check .` clean. `uv run python -c "import web_interface.server"` imports without error.
- [ ] **Step 6:** Commit: `routes: split health into a package`.

---

### Task 3: `settings` package and test retargets

**Files:**
- Rename: `web_interface/routes/settings.py` → `web_interface/routes/settings/__init__.py`
- Create: `web_interface/routes/settings/{secrets,overview,machine,contacts,payments,comms,mqtt,web,reports}.py`
- Modify: `tests/test_routes_settings.py`, `tests/test_routes_settings_reports.py`, `tests/test_routes_shape.py` (one assert)

**Interfaces:**
- Produces: `web_interface.routes.settings.router`; re-export `SECRET_MASK`; `web_interface.routes.settings.comms.send_email` is the name tests patch.

- [ ] **Step 1:** Follow the Move recipe with `area=settings`. Placement is spec §1's `settings/` rows. `secrets.py` has **no** `router` line and no `APIRouter` import. Docstring split per spec §1 (overview + "two of the six pages" paragraphs in `__init__.py`, masking paragraph in `secrets.py`, MQTT env-overrides paragraph in `mqtt.py`); the other sub-modules get a one-sentence docstring naming their routes. Every `# ---- /settings/<page>` divider goes to that page's sub-module.
- [ ] **Step 2:** Cross-module imports: `payments.py`, `comms.py`, `mqtt.py` import what each uses from `web_interface.routes.settings.secrets` (`SECRET_MASK`, `_secret_changed`; `mqtt.py` also `_secret_placeholder`). Nothing else crosses.
- [ ] **Step 3:** `__init__.py` ends with `__all__ = ["SECRET_MASK", "router"]`, `router = APIRouter()`, a comment that no parametrised paths exist so file order is kept, then `include_router` for `overview, machine, contacts, payments, comms, mqtt, web, reports` in that order.
- [ ] **Step 4:** Test retargets:
  - `tests/test_routes_settings.py`: add `from services import config_store` and `from web_interface.routes.settings import comms as settings_comms`; change the three `monkeypatch.setattr(settings_routes.config_store, "save_config", _raise)` to `monkeypatch.setattr(config_store, "save_config", _raise)`; change the three `monkeypatch.setattr(settings_routes, "send_email", _fake_send_email)` to `monkeypatch.setattr(settings_comms, "send_email", _fake_send_email)`. `SECRET_MASK = settings_routes.SECRET_MASK` stays.
  - `tests/test_routes_settings_reports.py`: add `from services import config_store`; change its one `settings_routes.config_store` patch the same way; if `settings_routes` is then unused, drop that import.
- [ ] **Step 5:** In `tests/test_routes_shape.py::test_discovery_finds_route_modules` add `assert "settings.secrets" in ROUTE_MODULES`.
- [ ] **Step 6:** Both verify scripts print `OK`. `uv run pytest -q` all pass. `uv run ruff check .`, `uv run ruff format --check .` clean. `uv run python -c "import web_interface.server"` imports without error.
- [ ] **Step 7:** Commit: `routes: split settings into a package`.

---

### Task 4: CLAUDE.md

**Files:**
- Modify: `CLAUDE.md` (Web Dashboard section)

- [ ] **Step 1:** In the `routes/` package sentence, change `` `health.py` `` to `` `health` `` and `` `settings.py` `` to `` `settings` ``, and extend the packages sentence: "`tests_level/`, `users/`, `auth/`, `health/`, and `settings/` are packages whose `__init__.py` builds the area router from sub-routers (… existing three …; `health`: `overview`, `subsystems`, `faults`, `recovery`, `availability`, `logs`; `settings`: `secrets` (shared masking helpers, no router), `overview`, `machine`, `contacts`, `payments`, `comms`, `mqtt`, `web`, `reports`)". Keep the existing shadowing clause and add "; `health` and `settings` keep their original file order, having nothing to shadow".
- [ ] **Step 2:** The shape-guard sentence: after "requires every sub-module to expose its own `router`", add "(except a sub-module listed in its `HELPER_MODULES` allowlist, which must expose none and carry no route decorator)".
- [ ] **Step 3:** The `_require_any` sentence (line ~113): "defined in `products.py` and `settings.py`" → "defined in `products.py` and `settings/overview.py`".
- [ ] **Step 4:** The subsystem-window paragraph: `` `_window_context` (`web_interface/routes/health.py`) `` → `` `_window_context` (`web_interface/routes/health/subsystems.py`) ``.
- [ ] **Step 5:** `grep -n -E "health\.py|settings\.py" CLAUDE.md` → no hits referring to the route modules.
- [ ] **Step 6:** Commit: `docs: health and settings route packages in CLAUDE.md`.
