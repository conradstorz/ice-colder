# Routes Split 4 (reports) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn `web_interface/routes/reports.py` into a package of three routed sub-modules plus two routerless helper modules, with no behaviour change.

**Architecture:** `reports/__init__.py` builds `router` by including `overview`, `levels`, `email` in the original file order (nothing shadows) and re-exports `resolve_bucket`. Handlers, helpers and comment blocks move byte-for-byte; only import blocks change. The shape guard's `HELPER_MODULES` allowlist gains `reports.helpers` and `reports.compute`.

**Tech Stack:** FastAPI `APIRouter`, `ast` (move verification), pytest, ruff, uv.

**Spec:** `docs/superpowers/specs/2026-10-12-routes-split-4-design.md` — the sub-module table in §1, the include order in §2, the guard change in §3 and the test/prose list in §4 are binding.

## Global Constraints

- Branch `routes-split-4` off `main`; one PR; never commit to `main`.
- Never chain shell commands with `&&`; one command per invocation. Use `uv run ...`, never `pip`.
- A moved function, constant, decorator, docstring, or comment block is **byte-for-byte identical** to the original. The only permitted edits are the import block, the `router = APIRouter()` line, the sub-module docstrings, and the `.helpers`/`.compute` imports the spec names. Do not rename, reorder lines inside a function, or "tidy".
- Package include order is exactly `overview`, `levels`, `email`, with a comment saying no sub-router's parametrised path shadows another's static one (`GET /reports/product/{sku:path}` only matches under `/reports/product/`), so the original file order is kept.
- `reports/__init__.py` has `__all__ = ["resolve_bucket", "router"]` and re-exports exactly `resolve_bucket` from `.helpers`; nothing else.
- Every commit: `uv run ruff check .`, `uv run ruff format --check .`, and `uv run pytest` green (browser tests skip locally; that is expected).
- Commit messages end with `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`.

## Move recipe (Task 1)

1. `git mv web_interface/routes/reports.py web_interface/routes/reports/__init__.py` so git tracks the rename, then cut the sub-module contents out of `__init__.py` into the new files. Each sub-module starts with: the one-sentence docstring the plan gives below, the imports it needs (copy the original import block, then let `uv run ruff check --fix` drop unused names), `router = APIRouter()` (not in `helpers.py` or `compute.py`), then the definitions in the **same relative order** they had in the original file.
2. `__init__.py` after the cut holds only: the original module docstring (unchanged, all three bullets), `from fastapi import APIRouter`, the sub-module imports, `from .helpers import resolve_bucket`, `__all__`, `router = APIRouter()`, and the three `include_router` calls in spec order with the order comment.
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

Run: `uv run python <scratch>/verify_move.py reports` → must print `OK`.

4. The verify script sees only defs and assignments. **Module-level comment blocks move with the definition they precede**, verbatim: the `# Bucket defaults per range preset ...` two lines above `_BUCKET_DEFAULTS` (→ `helpers.py`); the `# The four sub-tiles design §3 adds ...` five lines above `_REPORT_TILES` (→ `overview.py`); the six-line `# Per-report "compute" helpers ...` block that sits between `router = APIRouter()` and `_compute_period` (→ `compute.py`, at the top of the definitions); the three-line `# /reports (Task 9, unchanged)` divider (→ `overview.py`, above `reports_level`); the `# The four report levels (Task 10)` divider (→ `levels.py`); the `# POST /reports/email (Task 10)` divider (→ `email.py`). Then run the line-multiset check below; it must print `OK`:

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

Any `MISSING` line that is not part of the original import block is a defect: restore it.

5. `uv run ruff check --fix .` then `uv run ruff format .`; re-run both scripts (ruff must not have touched a function body — if it did, the original had a formatting difference and you must restore the original text).

---

### Task 1: `reports` package, guard entries, test retargets, home.py docstring

**Files:**
- Rename: `web_interface/routes/reports.py` → `web_interface/routes/reports/__init__.py`
- Create: `web_interface/routes/reports/helpers.py`, `compute.py`, `overview.py`, `levels.py`, `email.py`
- Modify: `tests/test_routes_shape.py:52` (`HELPER_MODULES`), `:59-66` (`test_discovery_finds_route_modules`)
- Modify: `tests/test_routes_reports_levels.py:490`, `:578` (monkeypatch targets)
- Modify: `web_interface/routes/home.py:6` (prose)
- Test: `tests/test_routes_shape.py`, `tests/test_routes_reports_levels.py`, `tests/test_web_routes.py`

**Interfaces:**
- Consumes: the existing `router` attribute contract in `web_interface/routes/__init__.py` (`app.include_router(reports.router)`), unchanged; `tests/test_routes_reports_levels.py:34`'s `from web_interface.routes import reports as reports_routes` and its `reports_routes.resolve_bucket(...)` calls.
- Produces: `web_interface.routes.reports.router`; `web_interface.routes.reports.resolve_bucket` (re-export); `web_interface.routes.reports.email.send_email` as the monkeypatch target.

Placement, by spec §1 (original line numbers from `main`; a handler's decorator starts a few lines above the `def` — include it):

| Sub-module | Definitions (original lines) | Sibling imports |
|---|---|---|
| `helpers.py` | comment 66–67 + `_BUCKET_DEFAULTS` 68–74, `_VALID_BUCKETS` 75, `PERIOD_HEADER` 77–84, `PRODUCT_HEADER` 85, `METHOD_HEADER` 86, `COLLECTIONS_HEADER` 87, `_effective_range` 130–141, `_default_bucket` 144–145, `resolve_bucket` 148–165, `_find_product` 168–169, `_ts_display` 172–176, `_fmt` 179–185, `_totalize_period` 188–210 | none |
| `compute.py` | comment 215–220, `_compute_period` 223–258, `_compute_product` 261–283, `_compute_product_sku` 286–357, `_compute_method` 360–382, `_compute_collections` 385–414, `_render` 417–429 | from `.helpers`: `COLLECTIONS_HEADER`, `METHOD_HEADER`, `PERIOD_HEADER`, `PRODUCT_HEADER`, `_find_product`, `_fmt`, `_totalize_period`, `_ts_display` |
| `overview.py` | comment 89–93 + `_REPORT_TILES` 94–127, divider 432–434, `reports_level` 437–483 | none |
| `levels.py` | divider 486–488, `reports_period_level` 491–504, `reports_product_level` 507–517, `reports_product_sku_level` 520–539, `reports_method_level` 542–552, `reports_collections_level` 555–565 | from `.helpers`: `_effective_range`, `resolve_bucket`; from `.compute`: `_compute_collections`, `_compute_method`, `_compute_period`, `_compute_product`, `_compute_product_sku`, `_render` |
| `email.py` | divider 568–570, `email_report` 573–639 | the same as `levels.py` |

Sub-module docstrings:

- `helpers.py`: `"""Range, bucket and formatting helpers shared by the Reports sub-modules; no routes."""`
- `compute.py`: `"""The per-report compute builders and the shared renderer; no routes."""`
- `overview.py`: `"""The Reports landing page: GET /reports."""`
- `levels.py`: `"""The five report levels: /reports/period, /reports/product, /reports/product/{sku}, /reports/method and /reports/collections."""`
- `email.py`: `"""The email action: POST /reports/email."""`

`__init__.py` body after the (unchanged) docstring:

```python
from fastapi import APIRouter

from . import email, levels, overview
from .helpers import resolve_bucket

__all__ = ["resolve_bucket", "router"]

router = APIRouter()
# No sub-router's parametrised path shadows another's static one
# (GET /reports/product/{sku:path} only matches under /reports/product/),
# so the original file order is kept.
router.include_router(overview.router)
router.include_router(levels.router)
router.include_router(email.router)
```

- [ ] **Step 1: Make the guard fail first**

In `tests/test_routes_shape.py` change:

```python
HELPER_MODULES = frozenset({"products.helpers", "settings.secrets"})
```
to
```python
HELPER_MODULES = frozenset(
    {"products.helpers", "reports.compute", "reports.helpers", "settings.secrets"}
)
```

and in `test_discovery_finds_route_modules` add, after the `"products.helpers"` assert:

```python
    assert "reports.compute" in ROUTE_MODULES
```

Run: `uv run pytest tests/test_routes_shape.py -q`
Expected: 1 failure, `test_discovery_finds_route_modules` (`"reports.compute"` not discovered).

- [ ] **Step 2: Move the module**

Follow the Move recipe with the placement table and docstrings above. Run both verify scripts: `uv run python <scratch>/verify_move.py reports` and `uv run python <scratch>/verify_lines.py reports`. Expected: `OK` from each.

- [ ] **Step 3: Retarget the two monkeypatches**

In `tests/test_routes_reports_levels.py`, lines 490 and 578:

```python
        monkeypatch.setattr(routes.reports, "send_email", fake_send_email)
```
→
```python
        monkeypatch.setattr(routes.reports.email, "send_email", fake_send_email)
```
and the same for `failing_send_email` at line 578. Nothing else in that file changes.

- [ ] **Step 4: home.py docstring**

`web_interface/routes/home.py:6`: `` `reports.py`, `` → `` `reports/`, ``. Confirm with `grep -n "reports.py" web_interface/routes/home.py` → no output.

- [ ] **Step 5: Lint, format, test**

Run, one per call: `uv run ruff check --fix .`; `uv run ruff format .`; both verify scripts again (`OK`); `uv run pytest -q`.
Expected: all passed (15 skipped browser tests locally is normal), including every `tests/test_routes_shape.py` case and all of `tests/test_routes_reports_levels.py`.

- [ ] **Step 6: Commit**

Stage `web_interface/routes/reports/`, `tests/test_routes_shape.py`, `tests/test_routes_reports_levels.py`, `web_interface/routes/home.py` (check `git status` — nothing else). Message:

```
routes: split reports into a package

helpers and compute (no router), overview, levels, email, included in
the original order since nothing shadows. Guard allowlist gains
reports.helpers and reports.compute; send_email patches retarget to
reports.email; home.py docstring updated.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
```

---

### Task 2: CLAUDE.md

**Files:**
- Modify: `CLAUDE.md:109` (the `routes/` package sentence)

**Interfaces:** none (documentation).

- [ ] **Step 1: Edit line 109**

Three substitutions in that one paragraph:

1. In the module list, `` `reports.py`, `` → `` `reports`, ``.
2. `` `tests_level/`, `users/`, `auth/`, `health/`, `settings/`, and `products/` are packages `` → `` `tests_level/`, `users/`, `auth/`, `health/`, `settings/`, `products/`, and `reports/` are packages ``, and append to the sub-module parenthetical, after the `products` entry (before the closing `)`): ``; `reports`: `helpers` and `compute` (shared helpers, no router), `overview`, `levels`, `email` ``.
3. `` `health` and `settings` keep their original file order, having nothing to shadow; `` → `` `health`, `settings` and `reports` keep their original file order, having nothing to shadow; ``.

- [ ] **Step 2: Check and commit**

`grep -n "routes/reports.py\|\`reports.py\`, \`controls" CLAUDE.md` → no output. Then commit `CLAUDE.md` only:

```
docs: reports route package in CLAUDE.md

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
```
