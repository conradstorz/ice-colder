# Routes split, part 3 (products) — design

**Date:** 2026-10-12
**Scope:** `web_interface/routes/products.py`; `tests/test_routes_shape.py`; prose that names `web_interface/routes/products.py` (two test files, `settings/overview.py`'s docstring, CLAUDE.md).
**Out of scope:** any change to handler bodies, URLs, permissions, templates, or behaviour; `routes/__init__.py`; `reports.py` (639 lines, the largest module left — its own pass after this one).

## Problem

After parts 1 and 2 (`2026-10-12-routes-split-design.md`, `2026-10-12-routes-split-2-design.md`), `products.py` (538 lines) is the second-largest route module left. It carries eight private helpers shared by every page, the two pages a placement-only role may read, the six catalog-editing routes, and the two placement routes. Same remedy: a package, one sub-module per concern, helpers in a routerless module.

## Design

### 1. Package

`products.py` becomes the package `products/` whose `__init__.py` exposes `router`, so `routes/__init__.py` and `attach_routes` keep working unchanged.

| Sub-module | Contents | Gate |
|---|---|---|
| `helpers.py` | `_require_any`, `_locked_skus`, `_find_product`, `_get_or_404`, `_inventory_count`, `_is_tracked`, `_product_level`, `_render_product_form` — **no router** | |
| `catalog.py` | `new_product_form`, `create_product`, `catalog_form`, `update_catalog`, `copy_product_form`, `delete_product_route` | `edit_catalog` |
| `placement.py` | `placement_form`, `update_placement` | `edit_placement` |
| `overview.py` | `products_list`, `product_detail` | `edit_catalog` **or** `edit_placement` |

Every sub-module except `helpers.py` defines its own module-level `router = APIRouter()` and decorates its handlers on it. A sub-module keeps only the imports it uses.

Each moved function, decorator, and comment block is byte-for-byte identical to the current one. The only edits are the import blocks and these cross-module references:

- `catalog.py` imports `_get_or_404`, `_inventory_count`, `_is_tracked`, `_product_level`, `_render_product_form` from `.helpers`.
- `placement.py` imports `_get_or_404`, `_inventory_count`, `_is_tracked`, `_product_level` from `.helpers`.
- `overview.py` imports `_get_or_404`, `_inventory_count`, `_is_tracked`, `_locked_skus`, `_product_level`, `_require_any` from `.helpers`.
- Within `helpers.py` the definitions keep their current relative order (`_get_or_404` after `_find_product`, `_render_product_form` last).
- `__init__.py` exposes `router` only, in `__all__`; nothing outside the package imports a products helper today, so nothing is re-exported.
- Module docstrings: `products.py`'s whole docstring is the area overview and stays in `products/__init__.py`; each sub-module gets a one-sentence docstring naming its routes (or, for `helpers.py`, saying it is the shared helpers with no routes).

### 2. Route order

`GET /products/{sku}` (in `overview`) would capture `GET /products/new` (in `catalog`), so `overview` is included last. `__init__.py` includes `catalog`, `placement`, `overview`, in that order, with a comment naming the shadowed route. No other pair overlaps: the four `/products/{sku}/<action>` paths differ in their last segment, and nothing is parametrised beyond `{sku}`.

`tests/test_routes_products.py` already drives `GET /products/new`, so a wrong include order fails the suite.

### 3. Shape guard

`tests/test_routes_shape.py`: `HELPER_MODULES` becomes `frozenset({"products.helpers", "settings.secrets"})`, and `test_discovery_finds_route_modules` also asserts `"products.helpers"` is discovered. The helper branch itself is unchanged.

### 4. Prose

No test monkeypatches a `products` global, so there are no patch retargets. The references to the old path are retargeted in the same PR:

- `tests/test_routes_products.py:1`: `web_interface/routes/products.py` → `web_interface/routes/products/`.
- `tests/test_web_routes.py:399`: same substitution.
- `web_interface/routes/settings/overview.py:20`: `web_interface/routes/products.py` → `web_interface/routes/products/helpers.py`.
- CLAUDE.md, Web Dashboard: the `routes/` package description lists `products` as a package with its sub-modules (same sentence pattern, with `helpers` marked "shared helpers, no router") and adds `products` to the include-order parenthetical ("for `products` the last sub-router holds `GET /products/{sku}`, which would shadow `GET /products/new`"); the `_require_any` sentence names `products/helpers.py` instead of `products.py`.

### 5. Delivery

One branch, `routes-split-3`, one PR against `main`. Commits:

1. `products` package, with the `HELPER_MODULES` change (the guard would fail on the new package without it, and the new allowlist entry would fail on the old layout, so they land together) and the two test-file and `settings/overview.py` prose fixes;
2. CLAUDE.md.

`uv run pytest`, `ruff check`, and `ruff format --check` are green after every commit.

## Testing

- Existing suite: the behavioural check; it drives every route through the real app.
- `tests/test_routes_shape.py`: every sub-module and package `__init__` exposes a router, or is an allowlisted helper with none; no `build_router`; no nested decorated function.
- Route-table diff against `main` in the final review: the same 104 routes, handlers, permission dependencies and response classes.

## Non-goals and follow-ups

- Merging `products/helpers.py`'s `_require_any` with `settings/overview.py`'s copy.
- Making any `_private` helper public or moving a helper to `services/` or `web_interface/`.
- Splitting `reports.py` (the next pass).
