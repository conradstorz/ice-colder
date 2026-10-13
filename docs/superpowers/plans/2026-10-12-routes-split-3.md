# Routes Split 3 (products) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn `web_interface/routes/products.py` into a package of three routed sub-modules plus one routerless helper module, with no behaviour change.

**Architecture:** `products/__init__.py` builds `router` by including `catalog`, `placement`, `overview` in that order (`overview` last because `GET /products/{sku}` would shadow `GET /products/new`). Handlers and helpers move byte-for-byte; only import blocks change. The shape guard's `HELPER_MODULES` allowlist gains `products.helpers`.

**Tech Stack:** FastAPI `APIRouter`, `ast` (move verification), pytest, ruff, uv.

**Spec:** `docs/superpowers/specs/2026-10-12-routes-split-3-design.md` — the sub-module table in §1, the include order in §2, the guard change in §3 and the prose list in §4 are binding.

## Global Constraints

- Branch `routes-split-3` off `main`; one PR; never commit to `main`.
- Never chain shell commands with `&&`; one command per invocation. Use `uv run ...`, never `pip`.
- A moved function, decorator, or docstring is **byte-for-byte identical** to the original. The only permitted edits are the import block, the `router = APIRouter()` line, the module docstrings, and the `.helpers` imports the spec names. Do not rename, reorder lines inside a function, or "tidy".
- Package include order is exactly `catalog`, `placement`, `overview`, with a comment naming the shadowed route (`GET /products/{sku}` would capture `GET /products/new`).
- `products/__init__.py` exposes `router` only (`__all__ = ["router"]`); no helper is re-exported.
- Every commit: `uv run ruff check .`, `uv run ruff format --check .`, and `uv run pytest` green (browser tests skip locally; that is expected).
- Commit messages end with `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`.

## Move recipe (Task 1)

1. `git mv web_interface/routes/products.py web_interface/routes/products/__init__.py` so git tracks the rename, then cut the sub-module contents out of `__init__.py` into the new files. Each sub-module starts with: the one-sentence docstring the spec allows, the imports it needs (copy the original import block, then let `uv run ruff check --fix` drop unused names), `router = APIRouter()` (not in `helpers.py`), then the functions in the **same relative order** they had in the original file.
2. `__init__.py` after the cut holds only: the area-overview docstring (the original module docstring, unchanged), `from fastapi import APIRouter`, the three sub-module imports, `__all__ = ["router"]`, `router = APIRouter()`, and the three `include_router` calls in spec order with the order comment.
3. Verify the move with the script below before running tests. It parses the original file from `main` and every file in the new package and asserts each top-level function in the original appears exactly once in the package with identical source text (decorators included):

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

Run: `uv run python <scratch>/verify_move.py products` → must print `OK`.

4. `products.py` has no module-level comment blocks (nothing starts with `#` at column 0), so the line-multiset check below only has to account for the import block, the router line, and the sub-module docstring lines. It must print `OK`:

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

### Task 1: `products` package, guard entry, prose fixes

**Files:**
- Rename: `web_interface/routes/products.py` → `web_interface/routes/products/__init__.py`
- Create: `web_interface/routes/products/helpers.py`, `catalog.py`, `placement.py`, `overview.py`
- Modify: `tests/test_routes_shape.py:52` (`HELPER_MODULES`), `:59-65` (`test_discovery_finds_route_modules`)
- Modify: `tests/test_routes_products.py:1`, `tests/test_web_routes.py:399`, `web_interface/routes/settings/overview.py:20` (prose only)
- Test: `tests/test_routes_shape.py`, `tests/test_routes_products.py`, `tests/test_web_routes.py`

**Interfaces:**
- Consumes: the existing `router` attribute contract in `web_interface/routes/__init__.py:151` (`app.include_router(products.router)`), unchanged.
- Produces: `web_interface.routes.products.router` (an `APIRouter` including the three sub-routers); `web_interface.routes.products.helpers` with the eight private helpers; the three routed sub-modules each with their own `router`.

Placement, by spec §1 (original line numbers from `main`):

| Sub-module | Definitions (original lines) | `.helpers` imports |
|---|---|---|
| `helpers.py` | `_require_any` 36–56, `_locked_skus` 59–71, `_find_product` 74–75, `_get_or_404` 78–84, `_inventory_count` 87–93, `_is_tracked` 96–99, `_product_level` 102–108, `_render_product_form` 114–160 | none |
| `catalog.py` | `new_product_form` 195–209, `create_product` 220–294, `catalog_form` 350–356, `update_catalog` 367–399, `copy_product_form` 504–519, `delete_product_route` 530–538 | `_get_or_404`, `_inventory_count`, `_is_tracked`, `_product_level`, `_render_product_form` |
| `placement.py` | `placement_form` 407–422, `update_placement` 433–496 | `_get_or_404`, `_inventory_count`, `_is_tracked`, `_product_level` |
| `overview.py` | `products_list` 170–187, `product_detail` 304–342 | `_get_or_404`, `_inventory_count`, `_is_tracked`, `_locked_skus`, `_product_level`, `_require_any` |

(The decorator of each handler starts a few lines above the `def`; the line ranges above are the `def` through the function's end — include the decorator.)

- [ ] **Step 1: Make the guard fail first**

In `tests/test_routes_shape.py` change:

```python
HELPER_MODULES = frozenset({"settings.secrets"})
```
to
```python
HELPER_MODULES = frozenset({"products.helpers", "settings.secrets"})
```

and in `test_discovery_finds_route_modules` add, after the `"settings.secrets"` assert:

```python
    assert "products.helpers" in ROUTE_MODULES
```

Run: `uv run pytest tests/test_routes_shape.py -q`
Expected: 1 failure, `test_discovery_finds_route_modules` (`"products.helpers"` not discovered).

- [ ] **Step 2: Move the module**

Follow the Move recipe. Docstrings for the new files:

- `helpers.py`: `"""Helpers shared by the Products sub-modules; no routes of its own."""`
- `catalog.py`: `"""The catalog-editing routes: /products/new, /products/{sku}/catalog, /products/{sku}/copy and /products/{sku}/delete."""`
- `placement.py`: `"""The placement routes: GET and POST /products/{sku}/placement."""`
- `overview.py`: `"""The two pages a placement-only role may read: /products and /products/{sku}."""`

`__init__.py` body after the docstring:

```python
from fastapi import APIRouter

from . import catalog, overview, placement

__all__ = ["router"]

router = APIRouter()
# `overview` last: its GET /products/{sku} would otherwise capture
# GET /products/new, which lives in `catalog`.
router.include_router(catalog.router)
router.include_router(placement.router)
router.include_router(overview.router)
```

Run both verify scripts: `uv run python <scratch>/verify_move.py products` and `uv run python <scratch>/verify_lines.py products`. Expected: `OK` from each.

- [ ] **Step 3: Prose fixes**

- `tests/test_routes_products.py:1`: `web_interface/routes/products.py` → `web_interface/routes/products/`.
- `tests/test_web_routes.py:399`: same substitution.
- `web_interface/routes/settings/overview.py:20`: `web_interface/routes/products.py` → `web_interface/routes/products/helpers.py`.

Confirm with `grep -rn "routes/products.py" tests web_interface` → no output.

- [ ] **Step 4: Lint, format, test**

Run, one per call: `uv run ruff check --fix .`; `uv run ruff format .`; both verify scripts again (`OK`); `uv run pytest -q`.
Expected: all passed (15 skipped browser tests locally is normal), including every `tests/test_routes_shape.py` case.

- [ ] **Step 5: Commit**

Stage `web_interface/routes/products/`, `tests/test_routes_shape.py`, `tests/test_routes_products.py`, `tests/test_web_routes.py`, `web_interface/routes/settings/overview.py` (check `git status` — nothing else). Message:

```
routes: split products into a package

helpers (no router), catalog, placement, overview; overview included
last so GET /products/{sku} cannot capture GET /products/new. Guard
allowlist gains products.helpers; prose references retargeted.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
```

---

### Task 2: CLAUDE.md

**Files:**
- Modify: `CLAUDE.md:109` (the `routes/` package sentence), `CLAUDE.md:113` (the `_require_any` sentence)

**Interfaces:** none (documentation).

- [ ] **Step 1: Edit line 109**

Three substitutions in that one paragraph:

1. In the module list, `` `products.py` `` → `` `products` ``.
2. `` `tests_level/`, `users/`, `auth/`, `health/`, and `settings/` are packages `` → `` `tests_level/`, `users/`, `auth/`, `health/`, `settings/`, and `products/` are packages ``, and append to the sub-module parenthetical, after the `settings` entry: ``; `products`: `helpers` (shared helpers, no router), `catalog`, `placement`, `overview` ``.
3. In the include-order parenthetical, `` `health` and `settings` keep their original file order, having nothing to shadow) `` → `` `health` and `settings` keep their original file order, having nothing to shadow; `products` puts `overview` last because its `GET /products/{sku}` would capture `catalog`'s `GET /products/new`) ``.

- [ ] **Step 2: Edit line 113**

`` defined in `products.py` and `settings/overview.py` `` → `` defined in `products/helpers.py` and `settings/overview.py` ``.

- [ ] **Step 3: Check and commit**

`grep -n "products.py" CLAUDE.md` → no output. Then commit `CLAUDE.md` only:

```
docs: products route package in CLAUDE.md

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
```
