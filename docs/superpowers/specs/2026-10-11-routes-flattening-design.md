# Routes flattening — design

**Date:** 2026-10-11
**Scope:** `web_interface/routes/*.py`, `web_interface/server.py`, `web_interface/context.py`
**Out of scope:** splitting `tests_level.py`, `users.py`, or `auth.py` into sub-modules (a later effort); any change to handler bodies, URLs, permissions, templates, or behaviour.

## Problem

Every module under `web_interface/routes/` defines a single `build_router(templates)` function that creates an `APIRouter`, defines every handler and every helper as a nested closure, and returns the router. The seven largest of those functions are 415–688 lines each (`auth`, `users`, `settings`, `health`, `tests_level`, `products`, `reports`). The nesting exists for one reason only: to capture the `Jinja2Templates` instance that `server.py` builds and `attach_routes(app, templates)` threads through.

Consequences:

- No handler or helper is importable, so none can be unit-tested, referenced, or read in isolation. Tooling (jump-to-definition, size reports, coverage by function) sees one giant function per module.
- Every handler sits one indentation level deeper than it needs to, for ~6 000 lines.
- `context._render_status(templates, request)` carries an extra parameter purely because it lives outside the closure.

## Design

### 1. The `templates` singleton

New module `web_interface/templating.py`:

```python
from fastapi.templating import Jinja2Templates
from web_interface.filters import humanize_seconds, sku_url_segment

templates = Jinja2Templates(directory="web_interface/templates")
templates.env.filters["humanize_seconds"] = humanize_seconds
templates.env.filters["sku_url_segment"] = sku_url_segment
```

`templates` is a pure function of the template directory and the filter set. It has no mutable state and never changes after import, so it is a module constant, not an injected dependency and not a `context` global. `server.py` deletes its own three `templates` lines and imports nothing from `templating` directly (it no longer needs the object); `attach_routes` no longer takes it.

`web_interface/context.py` must **not** bind the `templates` object as a module-level name: `tests/route_context.py`'s `discoverable_globals()` walks `vars(context)` and would flag a non-callable instance as an undeclared mutable global. `context.py` imports the module (`from web_interface import templating`) and `_render_status` reads `templating.templates`; module objects are excluded by that guard.

### 2. Module shape after flattening

Every route module has, at module level and in this order: imports, module constants and module-level helpers (unchanged), `router = APIRouter()` (`auth.py` keeps its `public = APIRouter()` name), then every former nested helper and handler de-indented one level, with its decorator, signature, dependencies, docstring, and body byte-for-byte identical apart from indentation. `build_router` is deleted. Every `templates.TemplateResponse(...)` / `templates.get_template(...)` call resolves through a module-level `from web_interface.templating import templates`.

An `ast` scan confirmed no nested helper or handler name collides with an existing module-level name in any of the eleven modules, and no module defines the same handler name twice, so hoisting needs no renames.

`routes/__init__.py`:

```python
def attach_routes(app: FastAPI) -> None:
    ...middleware and exception handler unchanged, except the 404/403
    handler imports `templates` from web_interface.templating...
    app.include_router(home.router)
    app.include_router(screen.router)
    app.include_router(health.router)
    app.include_router(products.router)
    app.include_router(inventory.router)
    app.include_router(reports.router)
    app.include_router(controls.router)
    app.include_router(tests_level.router)
    app.include_router(users.router)
    app.include_router(settings.router)
    app.include_router(auth_routes.public)
```

Registration order is preserved exactly; the permission-gated routers go first and the session-less auth router last, as the existing docstring requires.

`context._render_status(templates, request)` becomes `_render_status(request)`; its one caller (`routes/home.py`'s `GET /status`) drops the argument. Its docstring sentence explaining why `templates` is passed in is deleted.

### 3. Shape guard

New `tests/test_routes_shape.py`, in the style of `tests/test_no_private_access.py`: parse every `web_interface/routes/*.py` with `ast` and fail, listing `path:line`, if

- any module defines a function named `build_router`, or
- any `FunctionDef`/`AsyncFunctionDef` is nested inside another function (walk each top-level function's body; any function node found below it is a hit). Lambdas and functions nested inside classes are not hits.

During the three-PR rollout the guard carries an explicit `CONVERTED` set of module names it checks; PR (c) replaces the set with "every module in the package" so a new module cannot opt out.

### 4. Delivery

Three PRs, each green on `uv run pytest` and `ruff check` / `ruff format`, each converted module its own commit so `git diff -w` on that commit reads as a pure de-indent plus the import and router lines:

| PR | Content |
|---|---|
| (a) | `web_interface/templating.py`; `server.py` and `attach_routes(app)` change; `context._render_status(request)`; convert `controls`, `home`, `screen`, `inventory`; shape guard scoped to those four. |
| (b) | Convert `health`, `products`, `reports`, `settings`; guard widened. |
| (c) | Convert `tests_level`, `users`, `auth`; guard covers the whole package; CLAUDE.md Web Dashboard section updated (routes modules expose a module-level `router`, `attach_routes(app)`, `templating.py`). |

Because every router is a module-level object, the routes register at import time of each module and `attach_routes` only includes them. The test suite builds the app through `web_interface.server.app` (`tests/conftest.py`), so no test changes are expected beyond the new guard; a test that breaks is a regression, not a required edit.

## Testing

- Existing suite: unchanged and green after every commit. This is the primary behavioural check, since the suite already drives every route through the real app.
- `tests/test_routes_shape.py`: fails on a nested function or a `build_router` in any covered module.
- `tests/test_route_context.py::test_globals_covers_every_module_global`: stays green, confirming `context.py` gained no new discoverable global.
- Manual: `uv run python main.py` starts and `GET /` renders, confirming the import-time router construction has no circular import (`context` → `templating` → `filters` only; `templating` imports nothing from `routes` or `context`).

## Non-goals and follow-ups

- Splitting `tests_level.py`, `users.py`, `auth.py` into sub-modules: separate design.
- Making `context._render_status` public or moving it into `home.py`: not touched.
- Any change to `/screen` (CDN references, byte-identical to `origin/main` per CLAUDE.md): the module is flattened like the others, but its handler bodies and templates are untouched.
