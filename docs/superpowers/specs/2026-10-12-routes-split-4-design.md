# Routes split, part 4 (reports) — design

**Date:** 2026-10-12
**Scope:** `web_interface/routes/reports.py`; `tests/test_routes_shape.py`; `tests/test_routes_reports_levels.py` (two monkeypatch retargets); the prose that names `reports.py` as a route module (`web_interface/routes/home.py`'s docstring, CLAUDE.md).
**Out of scope:** any change to handler bodies, URLs, permissions, templates, or behaviour; `routes/__init__.py`; `services/reports.py` (a different module that happens to share the name).

## Problem

After parts 1–3, `web_interface/routes/reports.py` (639 lines) is the largest route module left. It carries the bucket/range helpers, the five per-report compute builders and the shared renderer, the landing page with its tile table, the five report level pages, and the email action. Same remedy: a package, one sub-module per concern, helpers in routerless modules.

## Design

### 1. Package

`reports.py` becomes the package `reports/` whose `__init__.py` exposes `router`, so `routes/__init__.py` and `attach_routes` keep working unchanged.

| Sub-module | Contents | Router |
|---|---|---|
| `helpers.py` | `_BUCKET_DEFAULTS` (with its comment), `_VALID_BUCKETS`, `PERIOD_HEADER`, `PRODUCT_HEADER`, `METHOD_HEADER`, `COLLECTIONS_HEADER`, `_effective_range`, `_default_bucket`, `resolve_bucket`, `_find_product`, `_ts_display`, `_fmt`, `_totalize_period` | none |
| `compute.py` | the "Per-report compute helpers" comment block, `_compute_period`, `_compute_product`, `_compute_product_sku`, `_compute_method`, `_compute_collections`, `_render` | none |
| `overview.py` | `_REPORT_TILES` (with its comment block), the `/reports (Task 9, unchanged)` divider, `reports_level` | `router` |
| `levels.py` | the `The four report levels (Task 10)` divider, `reports_period_level`, `reports_product_level`, `reports_product_sku_level`, `reports_method_level`, `reports_collections_level` | `router` |
| `email.py` | the `POST /reports/email (Task 10)` divider, `email_report` | `router` |

Every routed sub-module defines its own module-level `router = APIRouter()` and decorates its handlers on it. A sub-module keeps only the imports it uses.

Each moved function, constant, decorator, and comment block is byte-for-byte identical to the current one. The only edits are the import blocks and these cross-module references:

- `compute.py` imports `PERIOD_HEADER`, `PRODUCT_HEADER`, `METHOD_HEADER`, `COLLECTIONS_HEADER`, `_find_product`, `_fmt`, `_totalize_period`, `_ts_display` from `.helpers`.
- `levels.py` imports `_effective_range`, `resolve_bucket` from `.helpers` and `_compute_period`, `_compute_product`, `_compute_product_sku`, `_compute_method`, `_compute_collections`, `_render` from `.compute`.
- `email.py` imports `_effective_range`, `resolve_bucket` from `.helpers` and the same six names from `.compute`.
- `overview.py` imports nothing from its siblings.
- `__init__.py` re-exports `resolve_bucket` (from `.helpers`), listed in `__all__` with `router`, for `tests/test_routes_reports_levels.py`'s `reports_routes.resolve_bucket` calls.
- Module docstrings: `reports.py`'s whole docstring (the area overview plus its three design-note bullets) stays in `reports/__init__.py` unchanged, including its historical "only routes/reports.py and the five new templates" sentence; each sub-module gets a one-sentence docstring naming its routes or, for the two helper modules, its role.
- Within `helpers.py` the definitions keep their current relative order; `_REPORT_TILES` (currently between the header tuples and `_effective_range`) leaves for `overview.py`.
- The name `email` for a sub-module is safe: every import in the package is absolute (`from services.mailer import send_email`) or explicitly relative (`from . import email`), so the stdlib `email` package is never shadowed.

### 2. Route order

No sub-router's parametrised path captures another's static path: the only parametrised route is `GET /reports/product/{sku:path}`, which matches only paths with at least one segment after `/reports/product/`, and `/reports/period`, `/reports/method`, `/reports/collections` and `/reports` are not under that prefix; `POST /reports/email` is the only POST. `__init__.py` therefore includes `overview`, `levels`, `email` in the original file order, with a comment saying so.

### 3. Shape guard

`tests/test_routes_shape.py`: `HELPER_MODULES` becomes `frozenset({"products.helpers", "reports.compute", "reports.helpers", "settings.secrets"})`, and `test_discovery_finds_route_modules` also asserts `"reports.compute"` is discovered. The helper branch itself is unchanged.

### 4. Tests and prose

- `tests/test_routes_reports_levels.py`: the two `monkeypatch.setattr(routes.reports, "send_email", ...)` calls (lines 490 and 578) become `monkeypatch.setattr(routes.reports.email, "send_email", ...)`, the module whose global `email_report` reads. The `reports_routes.resolve_bucket` calls resolve through the package re-export and stay.
- `web_interface/routes/home.py:6`: `` `reports.py` `` → `` `reports/` ``.
- CLAUDE.md, Web Dashboard: the `routes/` package sentence lists `reports` as a package with its sub-modules (`helpers` and `compute` marked "no router") and adds `reports` to the "keep their original file order, having nothing to shadow" clause; nothing else in CLAUDE.md names this module (its other `reports.py` mentions are `services/reports.py`).

### 5. Delivery

One branch, `routes-split-4`, one PR against `main`. Commits:

1. `reports` package, with the `HELPER_MODULES` change, the two monkeypatch retargets, and the `home.py` docstring fix;
2. CLAUDE.md.

`uv run pytest`, `ruff check`, and `ruff format --check` are green after every commit.

## Testing

- Existing suite: the behavioural check; `tests/test_routes_reports_levels.py` drives every route, including the email action through both patched `send_email` paths.
- `tests/test_routes_shape.py`: every sub-module and package `__init__` exposes a router, or is an allowlisted helper with none; no `build_router`; no nested decorated function.
- Route-table diff against `main` in the final review: the same 104 routes, handlers, permission dependencies and response classes.

## Non-goals and follow-ups

- Making any `_private` helper public or moving a helper to `services/` or `web_interface/`.
- Splitting any other route module; after this pass the largest is `inventory.py` at 298 lines, which does not need one.
