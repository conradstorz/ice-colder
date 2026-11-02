# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**ice-colder** is a vending machine controller (VMC) application. It manages product selection, payment processing, hardware communication (MDB bus), and provides a web-based dashboard for monitoring and configuration.

## Layout

The repo is a uv workspace (`packages/*`): `ice_contracts`, `ice_common`, `ice_ui`, `ice_edge` and `ice_sims` are the packages (the former `controller/`, `services/`, `config/` and `web_interface/` roots are gone; `main.py` is `ice_edge/__main__.py`); the former `simulators/` root is now `ice_sims`. Board-contract generation: `uv run python -m ice_contracts.generate`.

### Current layout

```
packages/
  contracts/   src/ice_contracts/
      board/           board↔edge contract (layer-contracts spec §3): common, vending_machine,
                       ice_maker_monitor, mqtt_messages, dispenser_schema, payment
      cloud/           edge↔cloud contract (spec §4; cloud spec): report, ui
      access.py        Role, Permission, ROLE_PERMISSIONS, REMOTE_VIEWER_PERMISSIONS
      generate.py      schema generator → docs/contracts/**
  common/      src/ice_common/  build_info, paths, logging_setup, task_supervisor, task_lifecycle,
                                mqtt_client, config_model, observers
  ui/          tailwind.config.js  tailwind.input.css
               src/ice_ui/      templates/ static/ levels.py tiles.py filters.py templating.py
                                views/ (subsystem_window; health re-exports) bundle.py
  edge/        src/ice_edge/
      __main__.py      (was main.py)
      app/             composition root: machine, startup_config, startup_dispensers, startup_recovery
      web/             local service UI (ref arch §2.8): server, auth, context, routes/**
      uplink/          store-and-forward to cloud (§2.9)
      views/           view-model builders shared by web + uplink
      sale/            sale state machine (§2.2): vmc, sale_context, session_timer, outputs,
                       sale_journal, payout_desk, test_sale, maintenance_lease, fsm_control
      vend/            vend verification (§2.4): dispense_cycle, dispenser_gate, dispensers, dispensers_doc
      payment/         payments (§3): ports (Payments, PendingPayout), payment_gateway_manager,
                       mdb/ (MdbGateway; intake, state, policy, payout, reconcile)
      reporting/       reports (§7): reports, report_scheduler
      faults/          fault & availability engine (§2.5): fault_registry, fault_service, availability
      notify/          alert delivery (§7): mailer, notifier
      monitoring/      environment & physical monitoring (§2.6): health_monitor, inventory_manager
      access/          roles & access control (§5): access, auth_policy
      ledger/          escrow ledger & durability (§2.3): escrow_ledger, event_recorder, session_store,
                       session_recovery
      customer_ui/     customer UI (§2.7): display_controller
      hal/             hardware abstraction (§2.1): mqtt_inbound, command_dispatcher
      config/          config persistence: config_store
      supervisor/      supervisor (§2.10): task_runner
  cloud/       Dockerfile  src/ice_cloud/  admin, app, auth, body_limit, claim, commands, ingest,
                                           machine_auth, oidc, oidc_google, people, portal, settings,
                                           shadow, ui_registry, viewer, store/{snapshots,repository,sqlite},
                                           templates/
  sims/        src/ice_sims/    simulated board layer: base, ice_maker, mdb_gateway + vending_machine (thin entries),
                                mdb/ (cash, policy, event_log, customer, faults, simulator),
                                vending/ (board, channels, dispense, customer, faults, ha, ro_tank, climate, simulator)
firmware/      README.md, docs/{vending,mdb}-board-interface.md — field-device/board layer
               common/ (boardcore, boardidf: shared platform), vending/, mdb/ (ESP-IDF/PlatformIO projects)
scripts/       repo tooling (stays at root): _repo.py, gen_dispensers_example.py, make_click_wav.py,
               check_package_isolation.py, flash_board.py
tests/         one package; conftest.py + helpers stay at root
   meta/ contracts/ common/ ui/ sims/ integration/ cloud/ firmware/{,mdb/}
   edge/{app,web,views,uplink,sale,vend,payment,reporting,faults,notify,monitoring,access,ledger,
         customer_ui,hal,config,supervisor}/
```

### Package dependency rules (enforced by `uv run lint-imports` and `scripts/check_package_isolation.py`)


- Lowest to highest: `ice_contracts` → `ice_common` → `ice_ui` → (`ice_edge` | `ice_cloud` | `ice_sims`).
- A package imports only packages below it.
- The three top packages are mutually independent.
- `ice_sims` additionally may not import `ice_ui`, because boards have no UI.
- `ice_ui` may import only `ice_contracts`, never `ice_common`.
- Test isolation (`tests/meta/test_test_isolation.py`, an `ast` walk that follows `tests.*` helper imports transitively): simulator tests never import `ice_edge`, and edge tests never import `ice_sims`. Both allowlists are now empty and shrink-only; the one place simulator and edge code meet is `tests/integration/` (e.g. `test_im600xl_capabilities.py`, `test_simulator_commands.py`). Shared test helpers that must stay neutral live in `tests/async_helpers.py` and `tests/repo_root.py`.


## Commands

| Task | Command |
|------|---------|
| Install dependencies | `uv sync` |
| Run the application | `uv run python -m ice_edge` |
| Regenerate board contract schemas | `uv run python -m ice_contracts.generate` |
| Check package import rules | `uv run lint-imports` and `uv run python scripts/check_package_isolation.py` |
| Run all tests | `uv run pytest` |
| Run a single test | `uv run pytest tests/test_file.py::test_name` |
| Lint/format | `ruff check --fix .` then `ruff format .` |
| Build, flash, verify and provision a board (exits 0 with a message when none is attached) | `uv run python scripts/flash_board.py <vending or mdb>` (`--port`, `--no-build`, `--verify-only`, `--provision FILE.toml`) |
| Build Tailwind CSS | `.tailwind/tailwindcss.exe -c packages/ui/tailwind.config.js -i packages/ui/tailwind.input.css -o packages/ui/src/ice_ui/static/app.css --minify` |
| Build the vending firmware | `pio run -e kc868-a8` (from `firmware/vending/`) |
| Run the firmware host tests | `pio test -e native` (from `firmware/vending/`) |
| Build the MDB board firmware | `pio run -e esp32-s3-devkitc-1` (from `firmware/mdb/`) |
| Run the MDB host tests | `pio test -e native` (from `firmware/mdb/`; refresh goldens with `-f test_wire`) |
| Run the opt-in browser tests locally | `ICE_COLDER_BROWSER_TESTS=1 uv run pytest tests/edge/web/test_dashboard_v2_home_browser.py tests/edge/web/test_dashboard_v2_boosted_nav_browser.py tests/edge/web/test_users_codes_pin_browser.py tests/edge/web/test_touch_feedback_browser.py tests/cloud/test_badge_boosted_browser.py` |

**Tailwind:** The binary is [v3.4.17 standalone CLI](https://github.com/tailwindlabs/tailwindcss/releases/download/v3.4.17/tailwindcss-windows-x64.exe); a Linux or macOS checkout needs the matching asset from the same release. The binary is gitignored (`.tailwind/`, ~40 MB); `packages/ui/src/ice_ui/static/app.css` is **committed**. `tests/ui/test_static_css.py` fails CI when a template uses a class the committed file lacks — but that test only checks that classes templates *use* are present, never that unused ones are *absent*. Rebuild and commit `app.css` by hand whenever templates change; a stale file (leftover classes from a deleted template, or a missing rebuild after one) passes CI silently and only a manual rebuild catches it.

## Where the detail lives

Subsystem detail is in scoped `CLAUDE.md` files that load only when you work under their directory. Read the relevant one before changing code it covers, even from elsewhere.

| File | Covers |
|---|---|
| `packages/edge/CLAUDE.md` | startup, configuration, cloud uplink, FSM Core overview (dependency rule, `Machine`), task runner, services list |
| `packages/edge/src/ice_edge/sale/CLAUDE.md` | `StatusOutputs`, `SaleJournal`, observers and `TestSaleRunner`, test-sale rules, maintenance lease |
| `packages/edge/src/ice_edge/payment/CLAUDE.md` | payouts, evidence hold, boot recovery, per-boot reconciliation, cutover residuals |
| `packages/edge/src/ice_edge/ledger/CLAUDE.md` | FIFO method attribution, session recovery |
| `packages/edge/src/ice_edge/faults/CLAUDE.md` | `FaultService`, `InputFaults` |
| `packages/edge/src/ice_edge/vend/CLAUDE.md` | dispenser profiles (`dispensers.toml`), `DispenseCycle` |
| `packages/edge/src/ice_edge/hal/CLAUDE.md` | telemetry router |
| `packages/edge/src/ice_edge/web/CLAUDE.md` | web dashboard, URL/permission gate table, auth, Health and Tests levels |
| `packages/contracts/CLAUDE.md` | fault codes and severity, `PAYMENT_BLOCKING_FAULTS` |
| `packages/cloud/CLAUDE.md` | cloud portal, identity, claim, alerts, admin |
| `packages/sims/CLAUDE.md` | simulated boards (IM600XL vending, MDB) |
| `firmware/CLAUDE.md` | vending and MDB ESP32 firmware, shared platform rule |
| `docker/CLAUDE.md` | images, compose, CI/CD, simulated machine image, backups |
| `tests/CLAUDE.md` | public-surface and private-access ratchet, fake time, route context, browser tests |
| `docs/architecture-history.md` | aspirational-doc mapping, contract version history, design deviations, removed routes |

Rules that cut across all of them:

- Only the seven codes in `ice_contracts.board.vending_machine.PAYMENT_BLOCKING_FAULTS` can stop the machine taking money; `DATA-*` faults never do.
- Tests and routes read collaborators but mutate only through a `VMC`/`Machine` method, never an underscore attribute (`tests/CLAUDE.md`).
- Config is located via `ICE_COLDER_CONFIG` (default `./config.json`), never by bind-mounting `config.json` directly.

## Money (integer cents)

Money is integer cents everywhere inside the system (spec `docs/superpowers/specs/2026-10-30-money-in-cents-design.md`); every money field name ends in `_cents`, and averages of money are float cents named `*_cents`. `ice_contracts.money` holds `Cents`/`PositiveCents` (strict non-negative / positive ints), `MAX_PRICE_CENTS` (100,000), `dollars_to_cents` (Decimal, half-even, `ValueError` on negative or non-finite), `format_cents` (`$1.25`) and `cents_to_dollar_text` (`1.25`); the `ice_ui` `money` filter wraps `format_cents`. `tests/meta/test_money_types.py` is a shrink-only ratchet that fails on any money-named model/dataclass field containing a `float`; its allowlist holds only the float-cents average (`KpiAverage.money_in_cents`); the legacy dollar payment models and `VMCStatus.credit_escrow` were deleted by the payment cutover (vending contract 3.0.0).

- **Catalog.** `Product.price_cents` replaces `price`; a legacy dollar `price` in `config.json` is upgraded on load (and saved as `price_cents`). The product form parses dollars with `Decimal` and answers 400 on more than two decimals, a negative, or a value over `MAX_PRICE_CENTS`.
- **Escrow.** `EscrowLedger.total_cents`, `is_empty` (exact; there is no `TOLERANCE`), `Credit.cents`, `VMC.credit_escrow_cents`, `SessionSnapshot.credit_escrow_cents`. `session.json` and sales-journal rows carry `"units": "cents"`; a row without the marker is read as legacy dollars and converted.
- **Persistence.** events.db migration 2 (see `event_recorder.py` in `packages/edge/CLAUDE.md`) converts the stored money; reports and CSV use `_cents` keys.
- **Contracts.** Vending contract 2.4.0 adds `VMCStatus.credit_escrow_cents` (the float `credit_escrow` it deprecated was removed in 3.0.0); cloud contract 1.0.0 is described in `packages/cloud/CLAUDE.md`.
- **Board payment messages are integer cents.** The payment contract's `cents` fields reach `VMC.deposit_funds` and `PayoutDesk` unconverted; the dollar-based legacy models are gone. `/screen` (`partials/screen_body.html`) still renders dollars; its route passes `cents / 100`.

## Key Patterns

- **Logging**: Uses `loguru` throughout. `packages/common/src/ice_common/logging_setup.py` owns `setup_logging()`, called explicitly at the start of `main()`; importing the module does not change handlers or create files. It configures `LOGS/vmc.log`, an INFO-level console sink, and filtered `transactions.log`, `ice_maker.log`, and `vending.log` files. `vmc.log` is INFO-level unless `ICE_COLDER_LOG_LEVEL` says otherwise (read when `setup_logging()` runs; an invalid value warns and falls back to INFO) and rotates at midnight or 50 MB; the filtered files rotate at midnight. All file logs keep 300 days and ZIP-compress. State changes are prefixed with `STATE_CHANGE_PREFIX`.
- **Config mutation**: Product changes go through `packages/edge/src/ice_edge/config/config_store.py` which writes back to `config.json`. The in-memory `ConfigModel` is mutated directly (Pydantic models with mutable fields).
- **Web UI updates**: The dashboard uses HTMX to swap HTML partials from FastAPI endpoints. No SPA framework.
- **Timezone handling**: Report bucketing uses the machine's local timezone; weeks start on **Monday**; a sale exactly on a boundary (e.g. midnight) belongs to the **later** bucket.
- **DST testing**: `zoneinfo.ZoneInfo` is **unusable on a Windows checkout without `tzdata`**, so the DST tests use synthetic `tzinfo` classes. The primary synthetic zones (`_SyntheticDstTz` and `_SyntheticFallBackTz`) are in `tests/edge/reporting/test_reports.py` (lines 24, 91) to test report bucketing across DST transitions; `tests/edge/reporting/test_report_scheduler.py` mirrors them (lines 72, 102) to verify the scheduler threads its `tz` parameter correctly. Production code uses the OS's real timezone resolver.
- **CSS class extractor limitation**: `tests/ui/test_static_css.py`'s class extractor cannot tell markup inside a Jinja2 comment (`{# ... #}`) from real markup, so a template comment containing a literal `class="..."` will fail the test. This regex-based limitation (`_CLASS_ATTR_RE` at line 25) has no comment awareness — mentioning a Tailwind class name in a comment is safe, but writing an actual `class="..."` attribute in one is not. `tests/meta/test_no_private_access.py`'s private-access ratchet does *not* share this blindness: it walks the `ast`, so private names in comments and docstrings are ignored.
