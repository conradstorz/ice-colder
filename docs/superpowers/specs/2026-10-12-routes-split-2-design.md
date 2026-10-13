# Routes split, part 2 (health, settings) — design

**Date:** 2026-10-12
**Scope:** `web_interface/routes/health.py`, `web_interface/routes/settings.py`; `tests/test_routes_shape.py`; the tests that monkeypatch `settings` module globals; the CLAUDE.md Web Dashboard paragraphs.
**Out of scope:** any change to handler bodies, URLs, permissions, templates, or behaviour; `routes/__init__.py`; `tests_level/`'s import of `_subsystem_summary` from `web_interface.routes.health` (kept working through a re-export).

## Problem

After the first split (`2026-10-12-routes-split-design.md`), `health.py` (774 lines) and `settings.py` (746 lines) are the two largest route modules left. `health.py` carries the landing page, the subsystem windows, the fault-clear flow, the PAY-104 recovery flow with its lock, and two small pages; `settings.py` carries seven form pages plus the secret-masking helpers they share. Same remedy as part 1: a package per area, one sub-module per concern.

## Design

### 1. Packages

Each module becomes a package of the same name whose `__init__.py` exposes `router`, so `routes/__init__.py` and `attach_routes` keep working unchanged.

| Package | Sub-module | Contents |
|---|---|---|
| `health/` | `overview.py` | `health_home` (`GET /health`) |
| | `subsystems.py` | `_subsystem_summary`, `_window_context`, `subsystems_view`, `subsystem_detail`, `subsystem_live` |
| | `faults.py` | `_PAYMENT_BLOCKING_CODES`, `_SELECTOR_SAFE_KEY_RE`, `dom_safe_key`, `fault_gate`, `_faults_with_age`, `_render_fault_list_oob`, `faults_view`, `fault_clear_confirm`, `clear_fault` |
| | `recovery.py` | `_pay104_lock`, `_pay104_active`, `pay104_record_sale_confirm`, `pay104_record_sale`, `pay104_discard_confirm`, `pay104_discard` |
| | `availability.py` | `_availability_context_line`, `availability_view` |
| | `logs.py` | `logs_view` |
| `settings/` | `secrets.py` | `SECRET_MASK`, `_secret_placeholder`, `_secret_changed` — **no router** |
| | `overview.py` | `_require_any`, `settings_home` |
| | `machine.py` | `_render_machine_form`, `machine_form`, `update_machine` |
| | `contacts.py` | `_channels_from_form`, `_render_contacts_form`, `contacts_form`, `update_contacts` |
| | `payments.py` | `_render_payments_form`, `payments_form`, `update_payments` |
| | `comms.py` | `_render_comms_form`, `comms_form`, `update_comms`, `send_test_email` |
| | `mqtt.py` | `_render_mqtt_form`, `mqtt_form`, `update_mqtt` |
| | `web.py` | `_render_web_form`, `web_form`, `update_web` |
| | `reports.py` | `_parse_extra_recipients`, `_reports_validation_message`, `_render_reports_form`, `reports_form`, `update_reports` |

Every sub-module except `settings/secrets.py` defines its own module-level `router = APIRouter()` and decorates its handlers on it. A sub-module keeps only the imports it uses.

Each moved function, constant, decorator, and comment block is byte-for-byte identical to the current one. The only edits are the import blocks and these cross-module references:

- `health/overview.py` imports `_subsystem_summary` from `.subsystems`, `_faults_with_age` from `.faults`, `_availability_context_line` from `.availability`.
- `health/recovery.py` imports `dom_safe_key` and `_render_fault_list_oob` from `.faults`.
- `health/__init__.py` re-exports `dom_safe_key`, `fault_gate` (from `.faults`) and `_subsystem_summary` (from `.subsystems`), listed in `__all__` with `router`, so `tests/test_routes_health.py` and `tests_level/{overview,commands}.py` keep importing from `web_interface.routes.health`.
- `settings/payments.py`, `comms.py`, `mqtt.py` import what they use of `SECRET_MASK`, `_secret_placeholder`, `_secret_changed` from `.secrets`.
- `settings/__init__.py` re-exports `SECRET_MASK` (from `.secrets`), listed in `__all__` with `router`, for `tests/test_routes_settings.py`.
- Module docstrings: `health.py`'s whole docstring is the area overview and stays in `health/__init__.py`; each health sub-module gets a one-sentence docstring naming its routes. `settings.py`'s docstring splits by paragraph: the overview and the "two of the six pages never existed" paragraph stay in `settings/__init__.py`; the masking paragraph goes to `secrets.py`; the MQTT env-overrides paragraph goes to `mqtt.py`. No sentence is rewritten except to drop a now-false file reference.

### 2. Route order

No sub-router in either package has a parametrised path that would capture another sub-router's static path (`/health/subsystems/{name}` has no static sibling; `/health/faults/{key}/clear/...` and `/health/faults/PAY-104/record-sale|discard/...` differ in the segment after the key; `settings` has no parametrised path). Each `__init__.py` therefore includes the sub-routers in the order the original file defined them, with a comment saying so:

- `health`: `overview`, `subsystems`, `faults`, `recovery`, `availability`, `logs`.
- `settings`: `overview`, `machine`, `contacts`, `payments`, `comms`, `mqtt`, `web`, `reports`.

### 3. Shape guard

`tests/test_routes_shape.py` gains `HELPER_MODULES = frozenset({"settings.secrets"})`: a sub-module of shared code with no routes. For a helper module `test_every_module_exposes_a_router` asserts instead that the module has **no** `APIRouter` attribute and that no function in it (at any depth) carries a decorator that is a call on an attribute named `get`, `post`, `put`, `patch`, `delete`, `head`, `options`, `trace`, `api_route`, `route`, `websocket`, or `websocket_route` — so a route can never hide in a helper. The `build_router` and nested-function rules still apply to helpers. `test_discovery_finds_route_modules` also asserts `"health.faults"` and `"settings.secrets"` are discovered.

### 4. Tests and documentation

- `tests/test_routes_settings.py`: the three `send_email` monkeypatches retarget to `web_interface.routes.settings.comms`, the module whose global `send_test_email` reads.
- `tests/test_routes_settings.py` (three sites) and `tests/test_routes_settings_reports.py` (one site): `monkeypatch.setattr(settings_routes.config_store, "save_config", _raise)` becomes `monkeypatch.setattr(config_store, "save_config", _raise)` with `from services import config_store` — the attribute patched lives on that module, and the package should not re-export a module it does not itself use.
- No other test changes are expected.
- CLAUDE.md, Web Dashboard: the `routes/` package description lists `health` and `settings` as packages with their sub-modules (same sentence pattern as the existing three); the `_require_any` sentence names `web_interface/routes/settings/overview.py`; the subsystem-window paragraph's `_window_context` reference names `web_interface/routes/health/subsystems.py`; the shape-guard sentence mentions the helper-module allowlist.

### 5. Delivery

One branch, `routes-split-2`, one PR against `main`. Commits:

1. guard: helper-module allowlist (green on the current layout);
2. `health` package;
3. `settings` package, with the test retargets;
4. CLAUDE.md.

`uv run pytest`, `ruff check`, and `ruff format --check` are green after every commit.

## Testing

- Existing suite: the behavioural check; it drives every route through the real app.
- `tests/test_routes_shape.py`: every sub-module and package `__init__` exposes a router, or is an allowlisted helper with none; no `build_router`; no nested decorated function.
- Manual: `uv run python main.py` starts and `GET /` redirects to login, confirming no circular import among the sub-modules.

## Non-goals and follow-ups

- Making any `_private` helper public or moving a helper to `services/` or `web_interface/`.
- Merging `settings`'s `_require_any` with `products.py`'s copy.
- Splitting any other route module (the largest after this pass is `products.py`).
