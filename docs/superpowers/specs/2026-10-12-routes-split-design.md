# Routes split — design

**Date:** 2026-10-12
**Scope:** `web_interface/routes/tests_level.py`, `web_interface/routes/users.py`, `web_interface/routes/auth.py`; `tests/test_routes_shape.py`; the handful of tests that monkeypatch `send_email` on those modules; the CLAUDE.md Web Dashboard paragraph.
**Out of scope:** `health.py` and `settings.py` (large but not tangled; a later pass); any change to handler bodies, URLs, permissions, templates, or behaviour; `routes/__init__.py` and `attach_routes`.

## Problem

After the flattening (spec `2026-10-11-routes-flattening-design.md`), the three largest route modules are still 734–1014 lines each, every one carrying several unrelated areas: `tests_level.py` holds the command runner, the maintenance-lease actions, the test sale, and the test log; `users.py` holds account CRUD, ownership transfer and emergency codes, and device management; `auth.py` holds login plus device enrollment and the first-boot setup wizard. A reader looking for one area scrolls past the others.

## Design

### 1. Packages

Each module becomes a package of the same name. The package `__init__.py` exposes the same public attribute as before — `router` for `tests_level` and `users`, `public` for `auth` — so `routes/__init__.py`, `attach_routes`, and every `routes.<area>.<attr>` reference keep working unchanged.

| Package | Sub-module | Contents |
|---|---|---|
| `tests_level/` | `overview.py` | `tests_level` (`GET /tests`) |
| | `log.py` | `recent_test_runs`, `tests_log` (`GET /tests/log`), `post_verdict` (`POST /tests/runs/{run_id}/verdict`) |
| | `sale.py` | `tests_sale_picker` (`GET /tests/sale`), `tests_sale_run` (`POST /tests/sale`) |
| | `lease.py` | `_service_state`, `_BUSY_REFUSAL_WORDING`, `_BUSY_REFUSALS`, `_acquire_lease_or_refusal`, `tests_standby_confirm`, `tests_standby`, `tests_end`, `tests_takeover` |
| | `commands.py` | `AUTOMATIC_COMMANDS`, `_EXPECTED_CONTRACT_VERSION`, `contract_match`, `testable_commands`, `WATER_VALVE_SECONDS_RANGE`, `POWER_CYCLE_DWELL_RANGE`, `POWER_CYCLE_DWELL_DEFAULT`, `_params_widget`, `_parse_command_params`, `_run_command`, `tests_subsystem` (`GET /tests/{subsystem}`), `tests_run_all` (`POST /tests/run-all`), `post_test_command` (`POST /tests/{subsystem}/{command:path}`) |
| `users/` | `ownership.py` | `_check_owner_pin`, `codes_page`, `codes_regenerate_confirm`, `codes_regenerate`, `_render_ownership`, `_owner_pin_problem_ownership_response`, `ownership_page`, `email_machine_report_route`, `start_ownership_transfer`, `cancel_ownership_transfer` |
| | `devices.py` | `_guard_owner_device`, `_get_device_or_404`, `_render_devices_page`, `devices_list`, `forget_device_route`, `toggle_device_shared_route` |
| | `accounts.py` | `_guard_owner_target`, `_guard_owner_self_lockout`, `_guard_owner_self_demotion`, `_get_user_or_404`, `_user_level`, `_render_users_page`, `users_list`, `_render_new_user_form`, `new_user_form`, `create_user`, `_render_user_detail`, `user_detail`, `update_user_route`, `disable_user`, `enable_user`, `reset_user_pin`, `delete_user_route` |
| `auth/` | `login.py` | `_UNKNOWN_USER_SUBJECT`, `_keypad`, `login_page`, `login_submit`, `_enroll_page`, `_enrollment_response`, `_enroll_user_id`, `send_enroll_otp`, `enroll_device`, `logout` |
| | `setup.py` | `_setup_page`, `setup_page`, `setup_submit`, `_setup_codes_page`, `setup_codes_page`, `email_setup_codes`, `finish_setup`, `_review_candidates`, `_setup_review_response`, `_user_row`, `_review_response_after`, `setup_review_page`, `setup_review_keep`, `setup_review_remove` |

Every sub-module defines its own module-level `router = APIRouter()` (sub-modules of `auth` too — only the package-level attribute is named `public`) and decorates its handlers on it. A sub-module keeps only the imports it uses; `ruff check` removes the rest.

Each moved function, constant, and decorator is byte-for-byte identical to the current one. The only edits are the import block and cross-module references:

- `tests_level/commands.py` imports `_acquire_lease_or_refusal` from `.lease`.
- `tests_level/sale.py` imports `_acquire_lease_or_refusal` from `.lease` (`tests_sale_run` calls it).
- `tests_level/overview.py` imports `_service_state` from `.lease` and `contract_match`, `testable_commands` from `.commands`.
- `tests_level/__init__.py` re-exports the names tests import today: `contract_match`, `testable_commands`, `recent_test_runs`, `_run_command`, `WATER_VALVE_SECONDS_RANGE`, `POWER_CYCLE_DWELL_RANGE`, `POWER_CYCLE_DWELL_DEFAULT`, listed in `__all__` alongside `router`.
- The module docstrings of the three current files are split: the package `__init__.py` keeps the area overview, each sub-module gets the paragraphs about its own contents. No sentence is rewritten except to drop a now-false file reference.

`tests_level.py`'s existing import of `_subsystem_summary` from `web_interface.routes.health` moves with its users (`lease.py`, `commands.py`, `overview.py` as needed); it is pre-existing and not changed by this design.

### 2. Route order

Starlette matches routes in registration order, so a sub-router with a parametrised path is included after the sub-routers whose static paths it would otherwise shadow. The package `__init__.py` includes in exactly this order, with a comment naming the shadowed route:

- `tests_level`: `overview`, `log`, `sale`, `lease`, `commands` — `commands` last because `POST /tests/{subsystem}/{command:path}` would otherwise capture `POST /tests/runs/{run_id}/verdict`, `POST /tests/sale`, `POST /tests/standby`, `POST /tests/run-all`, `POST /tests/end`, `POST /tests/takeover`, and `GET /tests/{subsystem}` would capture `GET /tests/log`, `GET /tests/sale`, `GET /tests/standby/confirm`.
- `users`: `ownership`, `devices`, `accounts` — `accounts` last because `GET /users/{user_id}` would otherwise capture `GET /users/codes`, `GET /users/ownership`, `GET /users/codes/regenerate/confirm`, and `POST /users/{user_id}` would capture `POST /users/report`, `POST /users/transfer`, `POST /users/codes/regenerate`. Inside `accounts.py`, `new_user_form` (`GET /users/new`) stays defined before `user_detail`, as today.
- `auth`: `login`, `setup` — no overlap; the current file order is kept.

The existing route tests exercise every one of the shadowable routes, so a wrong include order fails the suite.

### 3. Shape guard

`tests/test_routes_shape.py` discovers with `ROUTES_DIR.rglob("*.py")`, skipping `ROUTES_DIR / "__init__.py"` only. A file's module name is its dotted path relative to `ROUTES_DIR` without the suffix, with a trailing `.__init__` removed, so `tests_level/__init__.py` is `tests_level` and `tests_level/commands.py` is `tests_level.commands`. The `build_router` rule and the nested-decorated-function rule apply to every discovered module. `test_every_module_exposes_a_router` imports `web_interface.routes.<name>` for every discovered name and asserts the attribute `ROUTER_ATTR.get(name, "router")` is an `APIRouter`; `ROUTER_ATTR` stays `{"auth": "public"}`. `test_discovery_finds_route_modules` also asserts `"tests_level.commands"` is present, so a discovery that stops at the top level fails.

### 4. Tests and documentation

- `tests/test_web_routes.py` retargets its six `send_email` monkeypatches to the sub-module whose global the handler reads: `routes.auth.login` for the enrollment OTP, `routes.auth.setup` for the setup-codes email, `routes.users.ownership` for the access report. A `monkeypatch.setattr(routes.auth, ...)` left on the package would set an attribute the handler never reads.
- No other test changes are expected. `tests/test_routes_tests.py`'s imports from `web_interface.routes.tests_level` resolve through the package re-exports.
- CLAUDE.md, Web Dashboard: the `routes/` package description lists the three packages and their sub-modules; the `_acquire_lease_or_refusal` reference (`web_interface/routes/tests_level.py:308`) becomes `web_interface/routes/tests_level/lease.py` without a line number; the Tests level paragraph's `web_interface/routes/tests_level.py` references become the package or the relevant sub-module.

### 5. Delivery

One branch, `routes-split`, one PR against `main`. Commits:

1. guard: `rglob` discovery with dotted names (green on the current flat layout, so it is a no-op until a package appears);
2. `tests_level` package;
3. `users` package;
4. `auth` package (with the `send_email` retargets for `auth` and `users` in their own package commits);
5. CLAUDE.md.

`uv run pytest`, `ruff check`, and `ruff format --check` are green after every commit.

## Testing

- Existing suite: the behavioural check; it drives every route through the real app.
- `tests/test_routes_shape.py`: every sub-module and package `__init__` exposes a router; no `build_router`; no nested decorated function.
- Manual: `uv run python main.py` starts and `GET /` redirects to login, confirming no circular import among the sub-modules.

## Non-goals and follow-ups

- Splitting `health.py` or `settings.py`.
- Making any `_private` helper public or moving a helper to `services/`.
- Changing `tests_level`'s import of `health._subsystem_summary`.
