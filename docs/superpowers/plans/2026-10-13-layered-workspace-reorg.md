# Layered Workspace Reorganization + Cloud Stub — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Lay the code out as a uv workspace whose packages and sub-packages correspond one-to-one with the sections of the aspirational documents. Enforce those boundaries in CI. Stub in a cloud monitoring server that renders the machine's own UI definition, including while the machine is offline.

**Architecture:** There are six workspace packages under `packages/`, layered bottom-up:
1. `ice_contracts` holds the wire contracts.
2. `ice_common` holds the runtime plumbing shared by the processes.
3. `ice_ui` is the single UI definition: templates, static assets, levels, filters, view-model re-exports and the bundle digest.
4. On top sit three independent applications: `ice_edge` (the machine), `ice_cloud` (the fleet server) and `ice_sims` (the board simulators).

Inside `ice_edge`, one sub-package per component of reference architecture §2 forms an ordered layer stack.

Every move is a `git mv`, plus a deterministic libcst codemod driven by one machine-readable move map. import-linter enforces the package and layer boundaries in CI.

**Tech Stack:**
- Python 3.12, uv workspaces, hatchling;
- libcst (codemod, dev only) and import-linter (dev only);
- FastAPI, Jinja2 (`SandboxedEnvironment` for cloud-cached bundles), Pydantic v2, httpx;
- Tailwind 3.4.17 standalone CLI.

**Inputs:**
- `docs/vending-system-reference-architecture.md` (§2 edge components, §3 payments, §7 reporting/remote, §8 cloud);
- `docs/superpowers/specs/2026-10-13-layer-contracts-design.md` (§3 board↔edge, §4 edge↔cloud);
- `docs/aspirational-gap-analysis.md` (§4 gaps).

## Decisions already made by the owner (do not re-ask)

| Question | Answer |
|---|---|
| Packaging | **uv workspace**: separate installable packages under `packages/`. |
| Cloud UI source | **Both**: the shared `ice_ui` package is the default. Each machine reports the digest of the UI definition it runs and can upload that bundle. The cloud renders with the matching cached bundle when its own differs. |
| Cloud depth | **Spec + runnable skeleton**: write the spec, plus a FastAPI app that renders the shared UI from stored machine reports. Shadow and commands are typed stubs. |
| Enforcement and delivery | **import-linter in CI + staged PRs**. Every PR is green on its own. History is preserved with `git mv`. |

## Global Constraints

- **Tooling:** `uv` only (`uv sync`, `uv run …`). Never `pip`, `venv` or `activate`.
- **Shell:** one command per Bash call, never chained with `&&`.
- **Docker:** never run locally. Image verification happens in CI (`image` job). Do not switch docker contexts.
- **Every PR leaves these green before it is opened:**
  - `uv run pytest -q`
  - `uv run ruff check .`
  - `uv run ruff format --check .`
  - `uv run lint-imports` and `uv run python scripts/check_package_isolation.py`, from PR 2 on.
- **No behaviour change** except in tasks explicitly marked **[behaviour]**. Moves are moves.
- **Module basenames are preserved by every move.** The only exception is `main.py` → `ice_edge/__main__.py`. This keeps local names and test patch targets stable.
- **Strings that look like module paths are not always module paths.**
  - MQTT topic strings (`"hardware/dispenser"`, `"hardware/io/+"`, `"hardware/buttons"`, …) and file names (`"config.json"`, `"config.example.json"`, `"config.json.bak"`) are **never** rewritten.
  - The codemod rewrites a string only when it begins with a full dotted *module* key from the move map.
- **`/screen` stays byte-identical.** `templates/screen.html` and `templates/partials/screen_body.html` keep their content byte-for-byte (`TestScreenTemplatesUnchanged`).
- **CSS:** `app.css` must be rebuilt and committed whenever a template's class set changes (`test_static_css.py`).
- **Commit trailer**, exactly, in place of any `Co-Authored-By` line: `Assisted-by: <model actually writing it> (Claude Code)`. Use the same line at the end of each PR description.
- **Historical docs are frozen.** Never rewrite `docs/superpowers/specs/*` or `docs/superpowers/plans/*` other than this file and the new cloud spec. Live docs (`CLAUDE.md`, `README.md`, `docs/contracts/**/CONTRACT.md`) are updated.
- **Private-access counts must not change across a move.** `tests/private_access_budget.py` keys are renamed by the move; the numbers stay the same.
- **Stop conditions** (bottom of this file) are mandatory: stop and report rather than work around.

## Target layout

```
packages/
  contracts/   src/ice_contracts/
      board/           board↔edge contract (layer-contracts spec §3): common, vending_machine,
                       ice_maker_monitor, mqtt_messages, dispenser_schema
      cloud/           edge↔cloud contract (spec §4; cloud spec): report, ui            [PR 5]
      access.py        Role, Permission, ROLE_PERMISSIONS, REMOTE_VIEWER_PERMISSIONS
      generate.py      schema generator → docs/contracts/**
  common/      src/ice_common/  build_info, paths, logging_setup, task_supervisor, task_lifecycle,
                                mqtt_client, config_model
  ui/          tailwind.config.js  tailwind.input.css
               src/ice_ui/      templates/ static/ levels.py tiles.py filters.py templating.py
                                views/ (subsystem_window; health re-exports [PR 5]) bundle.py [PR 5]
  edge/        src/ice_edge/
      __main__.py      (was main.py)
      app/             composition root: machine, startup_config, startup_dispensers, startup_recovery
      web/             local service UI (ref arch §2.8): server, auth, context, routes/**
      uplink/          store-and-forward to cloud (§2.9)              [stub PR 4, code PR 5]
      views/           view-model builders shared by web + uplink      [PR 5]
      sale/            sale state machine (§2.2): vmc, sale_context, session_timer, outputs,
                       sale_journal, test_sale, maintenance_lease, fsm_control
      vend/            vend verification (§2.4): dispense_cycle, dispenser_gate, dispensers, dispensers_doc
      payment/         payments (§3): refund_protocol, payment_gateway_manager
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
      cctv/            CCTV (§4) — docstring-only stub
  cloud/       Dockerfile  src/ice_cloud/  app, ingest, portal, store, ui_registry, shadow, commands,
                                           auth, templates/                           [PR 5]
  sims/        src/ice_sims/    simulated board layer: base, ice_maker, mdb_gateway, vending_machine
firmware/      README.md — field-device/board layer home (ESPHome configs later)
scripts/       repo tooling (stays at root): _repo.py, gen_dispensers_example.py, make_click_wav.py,
               check_package_isolation.py, refactor/{move_map.py,codemod.py}
tests/         one package; conftest.py + helpers stay at root
   meta/ contracts/ common/ ui/ sims/ integration/ cloud/
   edge/{app,web,views,uplink,sale,vend,payment,reporting,faults,notify,monitoring,access,ledger,
         customer_ui,hal,config,supervisor}/
```

### Package dependency rules (import-linter, PR 2 onward)

- Lowest to highest: `ice_contracts` → `ice_common` → `ice_ui` → (`ice_edge` | `ice_cloud` | `ice_sims`).
- A package imports only packages below it.
- The three top packages are mutually independent.
- `ice_sims` additionally may not import `ice_ui`, because boards have no UI.
- `ice_ui` may import only `ice_contracts`, never `ice_common`.

### Edge layer order (import-linter `layers`, PR 4 onward)

Higher layers may import lower ones. `|` separates siblings, which must not import each other.

```
ice_edge.app
ice_edge.web
ice_edge.uplink
ice_edge.views
ice_edge.sale
ice_edge.vend | ice_edge.payment | ice_edge.reporting
ice_edge.faults | ice_edge.notify
ice_edge.monitoring | ice_edge.access
ice_edge.ledger | ice_edge.customer_ui | ice_edge.hal | ice_edge.config
ice_edge.supervisor | ice_edge.cctv
```

**Type-only imports are excluded** (`exclude_type_checking_imports = true`). The runtime layering is strict, and collaborators are constructed only in `ice_edge.app`; this matches the existing injected-collaborator design. A cross-layer import that is used only in annotations goes under `if TYPE_CHECKING:`. A cross-layer import that is used at runtime and points upward is a **stop condition**: report it rather than adding an `ignore_imports` entry.

This order was checked against the current import graph on 2026-10-13. Every current upward edge is annotation-only and is listed in Task 3.

### Mapping to the aspirational documents

| Aspirational section | Lives in |
|---|---|
| Field device layer (contracts spec §2) | `firmware/` (future), simulated by `ice_sims` |
| Board ↔ edge contract (spec §3) | `ice_contracts.board` + `docs/contracts/{vending-machine,ice-maker-monitor}` |
| Edge §2.1 HAL | `ice_edge.hal` |
| Edge §2.2 sale FSM | `ice_edge.sale` |
| Edge §2.3 escrow ledger | `ice_edge.ledger` |
| Edge §2.4 vend verification | `ice_edge.vend` |
| Edge §2.5 faults/availability | `ice_edge.faults` |
| Edge §2.6 monitoring | `ice_edge.monitoring` |
| Edge §2.7 customer UI | `ice_edge.customer_ui` (+ `/screen` route in `ice_edge.web`) |
| Edge §2.8 local service UI | `ice_edge.web`, rendering `ice_ui` |
| Edge §2.9 store-and-forward uplink | `ice_edge.uplink` |
| Edge §2.10 supervisor | `ice_edge.supervisor` + `ice_common.task_supervisor` |
| §3 Payments | `ice_edge.payment` (MDB arrives through `ice_edge.hal`) |
| §4 CCTV | `ice_edge.cctv` (stub) |
| §5 Roles/access | `ice_contracts.access` (model) + `ice_edge.access` (store) |
| §7 Reporting / alerting | `ice_edge.reporting`, `ice_edge.notify` |
| §7 "local and remote use the same UI" | `ice_ui` (both `ice_edge.web` and `ice_cloud.portal` render it) |
| Edge ↔ cloud contract (spec §4) | `ice_contracts.cloud` |
| §8 Cloud/fleet backend | `ice_cloud` |

---

## The move map (authoritative data for `scripts/refactor/move_map.py`)

Every row has a `phase` (the PR that executes it). The codemod runs with `--phase N` and applies only that phase's rows. Rows from earlier phases are assumed to be applied already.

### MODULE_MOVES (old dotted module → new dotted module; file moves follow from it)

| phase | old | new |
|---|---|---|
| 2 | `contracts.common` | `ice_contracts.board.common` |
| 2 | `contracts.vending_machine` | `ice_contracts.board.vending_machine` |
| 2 | `contracts.ice_maker_monitor` | `ice_contracts.board.ice_maker_monitor` |
| 2 | `contracts.generate` | `ice_contracts.generate` |
| 2 | `contracts` (package `__init__`) | `ice_contracts` |
| 2 | `services.mqtt_messages` | `ice_contracts.board.mqtt_messages` |
| 2 | `services.dispenser_schema` | `ice_contracts.board.dispenser_schema` |
| 2 | `services.build_info` | `ice_common.build_info` |
| 2 | `services.paths` | `ice_common.paths` |
| 2 | `services.logging_setup` | `ice_common.logging_setup` |
| 2 | `services.task_supervisor` | `ice_common.task_supervisor` |
| 2 | `services.task_lifecycle` | `ice_common.task_lifecycle` |
| 2 | `services.mqtt_client` | `ice_common.mqtt_client` |
| 2 | `config.config_model` | `ice_common.config_model` |
| 3 | `web_interface.levels` | `ice_ui.levels` |
| 3 | `web_interface.filters` | `ice_ui.filters` |
| 3 | `web_interface.templating` | `ice_ui.templating` |
| 3 | `web_interface.subsystem_window` | `ice_ui.views.subsystem_window` |
| 4 | `main` | `ice_edge.__main__` |
| 4 | `controller.machine` | `ice_edge.app.machine` |
| 4 | `services.startup_config` | `ice_edge.app.startup_config` |
| 4 | `services.startup_dispensers` | `ice_edge.app.startup_dispensers` |
| 4 | `services.startup_recovery` | `ice_edge.app.startup_recovery` |
| 4 | `web_interface.server` | `ice_edge.web.server` |
| 4 | `web_interface.auth` | `ice_edge.web.auth` |
| 4 | `web_interface.context` | `ice_edge.web.context` |
| 4 | `web_interface.routes` (package, all submodules) | `ice_edge.web.routes` |
| 4 | `web_interface` (package `__init__`) | `ice_edge.web` |
| 4 | `controller.vmc` | `ice_edge.sale.vmc` |
| 4 | `controller.sale_context` | `ice_edge.sale.sale_context` |
| 4 | `controller.session_timer` | `ice_edge.sale.session_timer` |
| 4 | `controller.outputs` | `ice_edge.sale.outputs` |
| 4 | `controller.sale_journal` | `ice_edge.sale.sale_journal` |
| 4 | `controller.test_sale` | `ice_edge.sale.test_sale` |
| 4 | `controller.maintenance_lease` | `ice_edge.sale.maintenance_lease` |
| 4 | `services.fsm_control` | `ice_edge.sale.fsm_control` |
| 4 | `controller.dispense_cycle` | `ice_edge.vend.dispense_cycle` |
| 4 | `controller.dispenser_gate` | `ice_edge.vend.dispenser_gate` |
| 4 | `services.dispensers` | `ice_edge.vend.dispensers` |
| 4 | `services.dispensers_doc` | `ice_edge.vend.dispensers_doc` |
| 4 | `controller.refund_protocol` | `ice_edge.payment.refund_protocol` |
| 4 | `services.payment_gateway_manager` | `ice_edge.payment.payment_gateway_manager` |
| 4 | `services.reports` | `ice_edge.reporting.reports` |
| 4 | `services.report_scheduler` | `ice_edge.reporting.report_scheduler` |
| 4 | `controller.fault_registry` | `ice_edge.faults.fault_registry` |
| 4 | `controller.fault_service` | `ice_edge.faults.fault_service` |
| 4 | `services.availability` | `ice_edge.faults.availability` |
| 4 | `services.mailer` | `ice_edge.notify.mailer` |
| 4 | `services.notifier` | `ice_edge.notify.notifier` |
| 4 | `services.health_monitor` | `ice_edge.monitoring.health_monitor` |
| 4 | `services.inventory_manager` | `ice_edge.monitoring.inventory_manager` |
| 4 | `services.access` | `ice_edge.access.access` |
| 4 | `services.auth_policy` | `ice_edge.access.auth_policy` |
| 4 | `controller.escrow_ledger` | `ice_edge.ledger.escrow_ledger` |
| 4 | `services.event_recorder` | `ice_edge.ledger.event_recorder` |
| 4 | `services.session_store` | `ice_edge.ledger.session_store` |
| 4 | `controller.session_recovery` | `ice_edge.ledger.session_recovery` |
| 4 | `services.display_controller` | `ice_edge.customer_ui.display_controller` |
| 4 | `controller.mqtt_inbound` | `ice_edge.hal.mqtt_inbound` |
| 4 | `services.command_dispatcher` | `ice_edge.hal.command_dispatcher` |
| 4 | `services.config_store` | `ice_edge.config.config_store` |
| 4 | `controller.task_runner` | `ice_edge.supervisor.task_runner` |
| 4 | `simulators` (package, all submodules incl. `__main__`) | `ice_sims` |

Package roots (`PACKAGE_ROOTS`):

| Import root | Directory |
|---|---|
| `ice_contracts` | `packages/contracts/src/ice_contracts` |
| `ice_common` | `packages/common/src/ice_common` |
| `ice_ui` | `packages/ui/src/ice_ui` |
| `ice_edge` | `packages/edge/src/ice_edge` |
| `ice_cloud` | `packages/cloud/src/ice_cloud` |
| `ice_sims` | `packages/sims/src/ice_sims` |

A module's file is `<root dir>/<rest of dotted path>.py`, or `<…>/__init__.py` for a package row.

### FILE_MOVES (non-module files; `git mv`)

| phase | old | new |
|---|---|---|
| 3 | `web_interface/templates/` (whole tree) | `packages/ui/src/ice_ui/templates/` |
| 3 | `web_interface/static/` (whole tree) | `packages/ui/src/ice_ui/static/` |
| 3 | `web_interface/tailwind.config.js` | `packages/ui/tailwind.config.js` |
| 3 | `web_interface/tailwind.input.css` | `packages/ui/tailwind.input.css` |

### DELETIONS (PR 1, Task 1)

`hello.py`, `services/payment.py(old)`, `hardware/mdb_interface.py` (and so the `hardware/` dir). Nothing in the live tree imports any of them; only the ignored `colder-docker/` copy references `mdb_interface`.

### TEST_MOVES (phase 4; `git mv tests/<f>.py tests/<dir>/<f>.py`)

Each destination dir gets an empty `__init__.py`. Tests import helpers as `tests.fakes` etc., so `tests/` stays a package and every sub-dir must be one too.

| dest | files (`.py` omitted) |
|---|---|
| `tests/meta/` | test_no_private_access, test_routes_shape, test_vmc_complexity, test_env_isolation, test_skip_policy, test_fakes, test_paths, test_codemod, test_workspace, test_import_contracts, test_layout, test_ui_package |
| `tests/contracts/` | test_contract_schemas, test_contracts, test_contracts_vending, test_dispenser_schema, test_mqtt_messages_validation |
| `tests/common/` | test_async_lifecycle, test_build_info, test_config_model, test_logging_setup, test_mqtt |
| `tests/ui/` | test_filters, test_levels, test_subsystem_window, test_templating, test_static_css, test_feedback_assets |
| `tests/sims/` | test_simulator_base, test_simulator_ice_maker, test_simulator_mdb, test_simulator_vending |
| `tests/integration/` | test_integration_e2e |
| `tests/edge/app/` | test_first_run, test_machine, test_startup_dispensers, test_startup_policy |
| `tests/edge/sale/` | test_vmc_fsm, test_vmc_flows, test_vmc_observers, test_vmc_dispense_profiles, test_sale_context, test_sale_journal, test_session_timer, test_maintenance_lease, test_maintenance_standby, test_test_sale_runner, test_fsm_control, test_outputs |
| `tests/edge/vend/` | test_dispense_cycle, test_dispenser_gate, test_dispensers_cli, test_dispensers_example, test_dispensers_service, test_dispensers_validation |
| `tests/edge/payment/` | test_refund_protocol |
| `tests/edge/faults/` | test_availability, test_fault_registry, test_fault_service |
| `tests/edge/monitoring/` | test_health_monitor, test_inventory_manager |
| `tests/edge/notify/` | test_mailer, test_notifier |
| `tests/edge/access/` | test_access, test_auth_policy |
| `tests/edge/ledger/` | test_escrow_ledger, test_event_recorder, test_session_recovery, test_session_store |
| `tests/edge/reporting/` | test_reports, test_report_scheduler |
| `tests/edge/customer_ui/` | test_display_controller |
| `tests/edge/hal/` | test_command_dispatcher, test_mqtt_inbound |
| `tests/edge/config/` | test_config_store |
| `tests/edge/supervisor/` | test_task_runner |
| `tests/edge/web/` | test_route_context, test_routes_controls, test_routes_health, test_routes_health_pay104, test_routes_inventory, test_routes_inventory_collect, test_routes_products, test_routes_reports, test_routes_reports_levels, test_routes_settings, test_routes_settings_reports, test_routes_tests, test_routes_tests_level, test_routes_users, test_web_auth, test_web_routes, test_dashboard_v2_home_browser, test_dashboard_v2_boosted_nav_browser, test_users_codes_pin_browser, test_touch_feedback_browser |

These helpers stay at the `tests/` root: `conftest.py`, `fakes.py`, `route_context.py`, `browser_harness.py`, `browser/`, `dispenser_fixtures.py`, `dom_utils.py`, `private_access_budget.py`, `run_skip_guard.py`, `skip_policy.py`, `_env_isolation_probe.py`, `benchmarks/`, `paths.py` (Task 2). (`tests/ui/golden/` is created in Task 12, after the moves.)

`move_map.py` must assert that **every** `tests/test_*.py` present when phase 4 runs appears exactly once in TEST_MOVES, and fail otherwise, because new tests may have been added since this plan was written.

### TEXT_REWRITE_FILES (non-Python files the codemod's text mode may touch)

`CLAUDE.md`, `README.md`, `Dockerfile`, `docker-compose.yml`, `docker/docker-compose.yml`, `docker/docker-compose.prod.yml`, `.github/workflows/ci.yml`, `.vscode/launch.json`, `.vscode/tasks.json`, `.dockerignore`, `.gitignore`, `packages/ui/tailwind.config.js`, `docs/contracts/*/CONTRACT.md`, `tests/private_access_budget.py` (string keys), every `pyproject.toml`.

Text mode rewrites three kinds of reference:
1. dotted module references whose prefix is a full MODULE_MOVES key;
2. slash paths `old/dir/file.py` and `old/dir/` derived from MODULE_MOVES and FILE_MOVES;
3. `tests/<f>.py` paths from TEST_MOVES.

It then prints every remaining match of `\b(controller|services|web_interface|simulators|hardware|contracts)[./]` in those files as a **manual-review list**. A task is not done until that list is empty, or every remaining hit is explained (for example an MQTT topic such as `hardware/io/+`).

---

## PR 1 — Cleanup and move-safety prep (no file moves)

### Task 1: Delete dead code and fix CLAUDE.md inaccuracies  *(model: Haiku)*

**Files:** Delete `hello.py`, `services/payment.py(old)`, `hardware/mdb_interface.py`. Modify `Dockerfile` (drop `COPY hardware/ hardware/`) and `CLAUDE.md` (Hardware section).

- [ ] **Step 1:** Prove they are dead: `git grep -n -E "hello|mdb_interface|from hardware|import hardware" -- . ":!docs" ":!CLAUDE.md"` must show no importer.
- [ ] **Step 2:** `git rm hello.py "services/payment.py(old)" hardware/mdb_interface.py`
- [ ] **Step 3:** Remove the `COPY hardware/ hardware/` line from `Dockerfile`.
- [ ] **Step 4:** In `CLAUDE.md`, replace the `### Hardware (hardware/)` section with one sentence: there is no in-process hardware code; real MDB and I/O live on the ESP32 boards and arrive over MQTT (simulated by `simulators/`).
- [ ] **Step 5:** `uv run pytest -q` → all pass; `uv run ruff check .` → clean.
- [ ] **Step 6:** Commit `chore: remove dead files and stale hardware docs`.

### Task 2: Make every path move-proof  *(model: Sonnet)*

Every path that a move would break silently, or turn red for a non-reason, becomes derived from a module's own location. After this task, no later task needs hand edits to paths.

**Files:**
- Create: `scripts/_repo.py`, `tests/paths.py`, `tests/test_paths.py`
- Modify:
  - `web_interface/templating.py:12`, `web_interface/server.py:12`
  - `contracts/generate.py:36,55`, `services/build_info.py:19`
  - `scripts/gen_dispensers_example.py:17`, `scripts/make_click_wav.py:61`
  - `tests/test_config_model.py:20`, `tests/test_dispensers_example.py:32`, `tests/test_env_isolation.py:8`, `tests/test_feedback_assets.py:15`, `tests/test_logging_setup.py:9`
  - `tests/test_routes_shape.py:24,158`, `tests/test_simulator_vending.py:32`, `tests/test_startup_dispensers.py:20`
  - `tests/test_static_css.py:20-22`, `tests/test_vmc_complexity.py:12`
  - `tests/test_web_routes.py:3395-3396` and the `TestScreenTemplatesUnchanged` class (~3507-3535), `tests/run_skip_guard.py:25`

**Interfaces (produced; every later task relies on them):**
- `scripts/_repo.py`: `repo_root() -> Path` walks up from `__file__` to the first directory containing both `pyproject.toml` and `uv.lock`. `contracts/generate.py` gets an identical private helper, because it must not import `scripts`.
- `web_interface/templating.py`:
  - `TEMPLATES_DIR: Path = Path(__file__).parent / "templates"` and `STATIC_DIR: Path = Path(__file__).parent / "static"`;
  - `templates = Jinja2Templates(directory=TEMPLATES_DIR)`;
  - `server.py` mounts `StaticFiles(directory=STATIC_DIR)`.
- `services/build_info.py`: replace `_REPO_ROOT = parents[1]` with a walk-up search for `.git` starting at `Path(__file__).parent`. It returns `None` when none is found, and the existing "unknown" fallback applies.
- `tests/paths.py` derives every name from a module `__file__`, so the codemod's import rewrite updates it automatically:
  - `REPO_ROOT` (same walk-up);
  - `TEMPLATES_DIR`, `STATIC_DIR` (re-exported from `web_interface.templating`);
  - `ROUTES_DIR = Path(web_interface.routes.__file__).parent` and `ROUTES_PACKAGE = web_interface.routes.__name__`;
  - `VMC_PATH = Path(controller.vmc.__file__)`;
  - `SCREEN_TEMPLATES: list[tuple[str, tuple[str, ...]]]`: each entry is a repo-relative current path plus the legacy paths to try at `origin/main`. Legacy is empty now; Task 6 fills it.
- `tests/test_routes_shape.py` imports by `f"{ROUTES_PACKAGE}.{module_name}"` instead of the literal `web_interface.routes`.
- `TestScreenTemplatesUnchanged.test_matches_origin_main` compares the working-tree bytes of the current path with `git show origin/main:<p>`, where `p` is the first of `(current, *legacy)` that exists at `origin/main`. Its skip behaviour when `origin/main` is unreachable is unchanged.

- [ ] **Step 1: Write the failing test** `tests/test_paths.py`:
  - every path in `tests.paths` exists;
  - `TEMPLATES_DIR / "base.html"` is a file;
  - `VMC_PATH.name == "vmc.py"`;
  - `test_cwd_independent`: in a subprocess with `cwd=tmp_path` and `env={**os.environ, "PYTHONPATH": str(REPO_ROOT)}`, run `sys.executable -c "from web_interface.templating import templates; templates.get_template('base.html')"` and assert exit 0.
    - `PYTHONPATH` is needed because the legacy dirs are not installed before PR 2.
    - Today it fails with `TemplateNotFound`, because the template directory is relative to the working directory.
    - Assert that the failure text before the fix is `TemplateNotFound` and not `ModuleNotFoundError`, so the test fails for the right reason.
    - After PR 3 the codemod rewrites the import to `ice_ui.templating`, and the `PYTHONPATH` is harmless.
- [ ] **Step 2:** `uv run pytest tests/test_paths.py -v` → FAIL (ImportError for `tests.paths`; `TemplateNotFound` in the subprocess case).
- [ ] **Step 3:** Implement the interfaces above and replace each listed hard-coded path with the `tests.paths` name.
- [ ] **Step 4:** `uv run python contracts/generate.py` from the repo root → zero `git diff` under `docs/contracts/`. `uv run python scripts/gen_dispensers_example.py` → zero diff on `dispensers.example.toml`.
- [ ] **Step 5:** `uv run pytest -q` → all pass.
- [ ] **Step 6:** Commit `refactor: derive every path from module location, not cwd or depth`.

### Task 3: Decouple contracts from runtime; make type-only imports explicit  *(model: Sonnet)*

Two edits make the target layering satisfiable before anything moves. No behaviour change is intended.

**Files:** Modify `services/mqtt_messages.py:18,208`, `services/mqtt_client.py:186,204`, `controller/fault_service.py:54`, `controller/dispense_cycle.py:50-51`, `controller/mqtt_inbound.py:41` (and its `HealthMonitor` import), `controller/outputs.py:38`, `services/notifier.py` (`Alert` import). Test: `tests/test_mqtt_messages_validation.py`.

**Interfaces:**
- **`mqtt_messages` stops importing `build_info`.** `VMCStatus.version` loses its `default_factory` and becomes a required `str`. `controller/outputs.py`, its only production construction site, passes `version=BUILD_INFO.commit_short`. `VMCStatus` is in no generated schema, so no contract version bump is needed. (As executed: an earlier draft of this plan named `VMCOnline.firmware`, which does not exist.)
- **Annotation-only cross-layer imports move under `if TYPE_CHECKING:`,** with `from __future__ import annotations` added where missing. The list was verified on 2026-10-13: every non-import mention of these names is in a docstring or an annotation.
  - `fault_service` → `StatusOutputs`
  - `dispense_cycle` → `StatusOutputs`, `SaleContext`
  - `mqtt_inbound` → `Availability`, `HealthMonitor`
  - `outputs` → `Availability`
  - `notifier` → `Alert`

- [ ] **Step 1: Write the failing test:** `VMCStatus(state="idle")` raises `ValidationError`, and `services.mqtt_messages` has no attribute `BUILD_INFO`.
- [ ] **Step 2:** Run it → FAIL.
- [ ] **Step 3:** Make the edits.
- [ ] **Step 4:** `uv run pytest -q` → all pass; `uv run python -c "import controller.machine"` → exit 0.
- [ ] **Step 5:** Commit `refactor: contracts carry no runtime deps; type-only imports guarded`.

**PR 1:** open it with the three commits. CI must be green (lint, test, compose-config, image).

---

## PR 2 — Workspace scaffold, codemod, `ice_contracts` + `ice_common`

### Task 4: Move map + libcst codemod  *(model: Sonnet)*

**Files:**
- Create: `scripts/__init__.py` (if absent), `scripts/refactor/__init__.py`, `scripts/refactor/move_map.py`, `scripts/refactor/codemod.py`, `tests/test_codemod.py`
- Modify: root `pyproject.toml` (dev group gains `libcst` and `import-linter`), `uv.lock`

**Interfaces:**
- **`move_map.py` holds data only,** transcribed exactly from this plan's tables:
  - `ModuleMove(phase: int, old: str, new: str, package: bool)`, `FileMove(phase: int, old: str, new: str)`, `TestMove(phase: int, file: str, dest_dir: str)`: frozen dataclasses.
  - `MODULE_MOVES`, `FILE_MOVES`, `TEST_MOVES`, `PACKAGE_ROOTS: dict[str, str]`, `TEXT_REWRITE_FILES: list[str]`.
  - `module_file(dotted: str, *, package: bool) -> Path`, which resolves a *new* dotted name through `PACKAGE_ROOTS`.
  - `old_module_file(dotted: str, *, package: bool) -> Path`, which resolves the current repo-root location.
- **Pure functions in `codemod.py`, unit-tested without git:**
  - `rewrite_source(src: str, moves: list[ModuleMove]) -> tuple[str, list[str]]` returns the new source plus review items. Rules:
    - `import a.b` and `import a.b as x` → new path;
    - `from a.b import c` → new module;
    - `from a import b`, where `a.b` is a moved module → `from <parent of new> import <basename>`, keeping any `as` alias (basenames are preserved, so local names do not change);
    - parenthesised and multi-line imports are handled;
    - a relative import is reported as a review item (none exist today).
  - Simple string literals are rewritten only when the whole string equals a moved module, or starts with `"<moved module>."` (patch targets such as `"services.mailer.smtp_send"`). A string containing `/` is never rewritten, and neither is one whose matching prefix is only a package name (so `"config.json"` stays put). f-strings are never rewritten; each one whose static prefix matches a moved root is reported.
  - Longest-prefix match wins.
  - **Special row `main` → `ice_edge.__main__`.**
    - It is exempt from string rewriting: `"main"` appears dozens of times as the htmx target `hx-target="main"` and must never change.
    - Its import rewriting applies only to `import main` / `from main import …`. There are none today (verified with `git grep`), so the row is effectively a pure `git mv`.
  - **General binding rule.** An unaliased single-name `import X` whose module moves becomes `import <new> as X`, so the bound name survives. An unaliased *dotted* `import a.b` is rewritten and also reported as a review item, because later `a.b.attr` references in the body need checking.
  - **Self-exclusion.** The rewrite pass never touches `scripts/refactor/**` or `tests/**/test_codemod.py`, because their string literals *are* the move map and its fixtures.
  - `rewrite_text(text: str, moves, file_moves, test_moves) -> tuple[str, list[str]]` implements text mode as specified above.
- **CLI:** `uv run python -m scripts.refactor.codemod --phase N [--only-prefix P] [--dry-run]`. It runs these steps in order:
  1. Validate: every selected source exists, and no destination exists (or the row is already applied, in which case it is skipped, so the tool is idempotent).
  2. `git mv` every selected module, file tree and test file, creating destination dirs and `__init__.py` files as needed. For a package row, all children move with it.
  3. Run `rewrite_source` over every tracked `*.py`, excluding `docs/` and any path under `.venv`, `colder-docker` or `tmp`.
  4. Run `rewrite_text` over TEXT_REWRITE_FILES.
  5. Print a summary (moved, imports rewritten, strings rewritten) and the review list. Exit 0 even when review items exist; the task step decides.
- `--only-prefix P` restricts the phase to rows whose `new` (for TEST_MOVES, `dest_dir`) starts with `P`.

**Tests (`tests/test_codemod.py`):**

| Input | Expected |
|---|---|
| `from services.mqtt_messages import (A,\n    B)` with a phase-2 map | `from ice_contracts.board.mqtt_messages import (A,\n    B)`, formatting preserved |
| `from services import report_scheduler` | `from ice_edge.reporting import report_scheduler` |
| `from services import event_recorder as event_recorder_module` | alias kept |
| `import services.config_store as cs` | `import ice_edge.config.config_store as cs` |
| `from controller import mqtt_inbound` | `from ice_edge.hal import mqtt_inbound` |
| `mock.patch("services.mailer.smtp_send")` | rewritten |
| `"hardware/dispenser"`, `"hardware/io/+"`, `"config.json"`, `"config.example.json"`, `"config.json.bak"` | unchanged |
| `f"web_interface.routes.{x}"` | unchanged, one review item |
| `from servicesx import y`, `import configparser` | unchanged |
| `hx-target="main"` inside a Python string, and `"main"` | unchanged (row exempt from string rewriting) |
| `import services.paths` (unaliased, dotted) | rewritten, plus one review item |
| text: `controller/vmc.py` | `packages/edge/src/ice_edge/sale/vmc.py` |
| text: `services.startup_dispensers.load_dispenser_profiles` | `ice_edge.app.startup_dispensers.load_dispenser_profiles` |
| text: `"tests/test_vmc_flows.py": 2` | `"tests/edge/sale/test_vmc_flows.py": 2` |

Table invariants checked by the test:
- no duplicate `old` within a phase, and no two rows with the same `new`;
- every `new` starts with a `PACKAGE_ROOTS` key;
- every TEST_MOVES `dest_dir` starts with `tests/`;
- no basename changes except `main` → `ice_edge.__main__`;
- `test_codemod_never_rewrites_its_own_map`: running `rewrite_source` over the text of `scripts/refactor/move_map.py` through the CLI's file-selection function returns it unchanged, because the file is excluded.

- [ ] **Step 1:** Write the tests. **Step 2:** Run → FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** `uv run pytest tests/test_codemod.py -v` → PASS.
- [ ] **Step 5:** Workspace members do not exist until Task 5, so the phase-2 `--dry-run` validation of destination roots is exercised in Task 5. Here, run `--phase 2 --dry-run` and expect a clear "package root packages/contracts/src/ice_contracts missing" error, not a traceback.
- [ ] **Step 6:** Commit `build: add move map and libcst codemod for the reorg`.

### Task 5: uv workspace scaffold + `ice_contracts` + `ice_common`  *(model: Sonnet)*

**Files:**
- Create:
  - six `packages/<dir>/pyproject.toml`;
  - empty-but-documented `src/<pkg>/__init__.py` for ui, edge, cloud and sims, each with a docstring naming its aspirational section;
  - `packages/contracts/src/ice_contracts/board/__init__.py`, `packages/contracts/src/ice_contracts/access.py`;
  - `scripts/check_package_isolation.py`, `tests/test_workspace.py`, `tests/test_import_contracts.py`.
- Modify: root `pyproject.toml`, `uv.lock`, `services/access.py`, `Dockerfile`, `.dockerignore`, `.github/workflows/ci.yml`, `CLAUDE.md`.
- Moved by codemod phase 2: the phase-2 rows.

**Interfaces:**
- **Root `pyproject.toml`:**
  - `[project] name = "ice-colder"` with `dependencies = ["ice-edge", "ice-cloud", "ice-sims"]`;
  - `[tool.uv] package = false`;
  - `[tool.uv.workspace] members = ["packages/*"]`;
  - `[tool.uv.sources]` sets each of the six members to `{ workspace = true }`;
  - the dev group keeps `pytest-asyncio` and `ruff` and adds `libcst` and `import-linter`;
  - `[tool.pytest.ini_options]` unchanged.
  
  Plain `uv sync` therefore installs every member in editable mode, and CI's `uv sync --locked` needs no change.
- **Each member:**
  - `[build-system] requires = ["hatchling"]`, `build-backend = "hatchling.build"`;
  - `[tool.hatch.build.targets.wheel] packages = ["src/<import_name>"]` (templates and static ship as package data automatically);
  - `[project] name` is one of `ice-contracts`, `ice-common`, `ice-ui`, `ice-edge`, `ice-cloud`, `ice-sims`, with `version = "0.1.0"` and `requires-python = ">=3.12"`;
  - `[tool.uv.sources]` for its workspace deps.
- **Member dependencies** are the minimum its own imports need, proven by `check_package_isolation.py`. Starting point:

| Package | Dependencies |
|---|---|
| contracts | `pydantic`, `pydantic-extra-types` (if imported) |
| common | `ice-contracts`, `loguru`, `aiomqtt`, `pydantic`, `pydantic-settings`, `phonenumbers` (if `config_model` imports it) |
| ui | `ice-contracts`, `fastapi[standard]` |
| edge | `ice-common`, `ice-ui`, `fastapi[standard]`, `qrcode`, `pillow`, `loguru`, `uvicorn` (via fastapi[standard]) |
| sims | `ice-common`, `loguru` |
| cloud | `ice-ui`, `ice-common`, `fastapi[standard]` |
- **`ice_contracts.access`:**
  - `class Role(str, Enum)`, `class Permission(str, Enum)` and `ROLE_PERMISSIONS: dict[Role, frozenset[Permission]]` move verbatim out of `services/access.py`, which re-imports them, so every existing `from services.access import Permission` keeps working.
  - New: `REMOTE_VIEWER_PERMISSIONS: frozenset[Permission] = frozenset({Permission.view_status})`.
  - Read `tests/test_access.py:243` first, because it parses `access.py`'s AST. If its assertion concerns the role tables, point it at `ice_contracts/access.py` using the module's `__file__`.
- **`scripts/check_package_isolation.py`:** for each member, run `uv run --isolated --no-dev --package <name> python -c "<import every module under src/<pkg>>"`, with modules discovered by walking `src/`. Exit non-zero naming the first failure.
- **Dockerfile:**
  1. Copy root `pyproject.toml`, `uv.lock` and all six `packages/*/pyproject.toml`.
  2. `RUN uv sync --no-install-workspace --package ice-edge`, then `RUN uv sync --no-install-workspace --inexact --package ice-sims` (dependency layer).
  3. `COPY packages/ packages/`, plus each legacy top-level dir that still exists (`config/`, `contracts/` while present, `controller/`, `services/`, `simulators/`, `web_interface/`, `main.py`).
  4. `RUN uv sync --package ice-edge` and `RUN uv sync --inexact --package ice-sims`.
  5. `ENV UV_NO_SYNC=1 PATH="/app/.venv/bin:$PATH"`, keeping `UV_FROZEN=1` and `UV_NO_DEV=1`. Bare `python` in the container must be the project venv's, not the slim image's system Python; the smoke step depends on it.
  6. CMD unchanged until Task 7.

  `.dockerignore` adds `packages/cloud`.
- **CI:**
  - the `lint` job adds `uv run lint-imports` and `uv run python scripts/check_package_isolation.py`;
  - the `image` job adds a smoke step before the push step: `docker/build-push-action` with `platforms: linux/amd64`, `load: true`, `tags: ice-colder:smoke`; then three checks:
    1. `docker run --rm ice-colder:smoke python -c "import sys; assert sys.prefix.startswith('/app/.venv'), sys.prefix"` succeeds. This proves the import checks below run against the venv.
    2. `docker run --rm ice-colder:smoke python -c "import ice_contracts, ice_common"` succeeds.
    3. `docker run --rm ice-colder:smoke python -c "import ice_cloud"` fails. The step asserts the non-zero exit and that stderr contains `ModuleNotFoundError: No module named 'ice_cloud'`. The multi-arch build/push step is unchanged.
- **`[tool.importlinter]`** in root `pyproject.toml`:
  - `root_packages = ["ice_contracts", "ice_common", "ice_ui", "ice_edge", "ice_cloud", "ice_sims"]` and `exclude_type_checking_imports = true`. Verify the option exists in the installed version (`uv run lint-imports --help` or its docs); if it does not, this is a stop condition.
  - Contracts:
    1. layers: `ice_edge | ice_cloud | ice_sims` > `ice_ui` > `ice_common` > `ice_contracts`;
    2. forbidden: `ice_sims` → `ice_ui`;
    3. forbidden: `ice_ui` → `ice_common`.

  Legacy top-level dirs are not root packages, so they are unchecked until they are gone in PR 4.

- [ ] **Step 1: Write the failing tests:**
  - `tests/test_workspace.py`: imports `ice_contracts.board.vending_machine` and `ice_common.config_model`; `ice_contracts.access.REMOTE_VIEWER_PERMISSIONS == {Permission.view_status}`; `services.access.Permission is ice_contracts.access.Permission`.
  - `tests/test_import_contracts.py`: runs `uv run lint-imports` in a subprocess with `cwd=REPO_ROOT`, asserts exit 0, and prints stdout on failure.
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Create the member pyprojects and stub packages; `uv lock`; `uv sync`.
- [ ] **Step 4:** `uv run python -m scripts.refactor.codemod --phase 2 --dry-run`, then without `--dry-run`. `git status` must show renames (`R`) for every moved file. Resolve every review item.
- [ ] **Step 5:** Split `Role`/`Permission`/`ROLE_PERMISSIONS` into `ice_contracts/access.py`.
- [ ] **Step 6:** `uv run python -m ice_contracts.generate` → zero diff under `docs/contracts/`.
- [ ] **Step 7:** Full gate: pytest, ruff check, ruff format --check, lint-imports, check_package_isolation → all green.
- [ ] **Step 8:** `CLAUDE.md`:
  - add a "Layout" section near the top, containing the target-layout block, the package dependency rules, and the aspirational mapping table from this plan;
  - change the contract generator command to `uv run python -m ice_contracts.generate`;
  - review text mode's list.
- [ ] **Step 9:** Commit `build: uv workspace; contracts and shared runtime move into ice_contracts/ice_common`.

**PR 2:** open; CI green, including the image smoke step.

---

## PR 3 — `ice_ui`: the single UI definition

### Task 6: Move templates, static assets, levels, filters, templating and view models into `ice_ui`  *(model: Sonnet)*

**Files:**
- Codemod phase 3 (four module rows and four file rows).
- Create `tests/test_ui_package.py`.
- Modify `tests/paths.py` (`SCREEN_TEMPLATES` legacy entries), `packages/ui/tailwind.config.js` (`content` glob and header comment), `CLAUDE.md` (Tailwind command), and `.vscode/tasks.json` (if it has a Tailwind task).

**Interfaces:**
- `ice_ui.templating`:
  - `TEMPLATES_DIR` and `STATIC_DIR` are already package-relative after Task 2;
  - `make_templates(directory: Path, *, sandboxed: bool = False, extra_dirs: Sequence[Path] = ()) -> Jinja2Templates`:
    - always registers the `humanize_seconds` and `sku_url_segment` filters;
    - the loader is `ChoiceLoader([FileSystemLoader(d) for d in (*extra_dirs, directory)])`;
    - `sandboxed=True` builds on `jinja2.sandbox.SandboxedEnvironment(loader=…, autoescape=True)` and passes it as `Jinja2Templates(env=…)`;
  - `templates = make_templates(TEMPLATES_DIR)`.
- `tests/paths.SCREEN_TEMPLATES`:
  ```
  [("packages/ui/src/ice_ui/templates/screen.html", ("web_interface/templates/screen.html",)),
   ("packages/ui/src/ice_ui/templates/partials/screen_body.html", ("web_interface/templates/partials/screen_body.html",))]
  ```
- Tailwind:
  - `content: ["./packages/ui/src/ice_ui/templates/**/*.html"]`, resolved from the repo root, which is where the command runs;
  - command: `.tailwind/tailwindcss.exe -c packages/ui/tailwind.config.js -i packages/ui/tailwind.input.css -o packages/ui/src/ice_ui/static/app.css --minify`.
- `ice_ui` must not import `ice_common` (linter contract 3). `subsystem_window` imports only `ice_contracts.board.common`.
- **Static UI data moves with the UI.** The Home tile table `_TILE_DEFS` (`web_interface/routes/home.py:36`) is static UI definition, not live state.
  - It becomes the public `TILES: tuple[TileDef, ...]` in a new `ice_ui/tiles.py`, where `TileDef` is a frozen dataclass whose fields mirror today's tuple `(str, str, str, frozenset, bool)`.
  - Add `build_tiles(perms: frozenset, lines: Mapping[str, str]) -> list[dict]` there: the permission filtering and dict shaping from today's `_build_tiles`.
  - The edge route keeps only the computation of each tile's live `lines` entry and calls `ice_ui.tiles.build_tiles`. The cloud (Task 14) supplies `lines` from the report, so there is one tile definition.
  - **Find any other static UI data the same way.** Before Step 3, `git grep -n -E "^_?[A-Z][A-Z_]+ *(:|=)" web_interface/routes` lists module-level constants in routes. Any that a template consumes and that does not depend on live machine state (titles, URL lists, option tables) moves into `ice_ui` the same way. List each one moved in the commit message; constants that stay need no listing.

- [ ] **Step 1: Write the failing test** `tests/test_ui_package.py`:
  - `ice_ui.templating.TEMPLATES_DIR` is inside `Path(ice_ui.__file__).parent`;
  - `make_templates(TEMPLATES_DIR, sandboxed=True).env` is a `SandboxedEnvironment`;
  - `make_templates(TEMPLATES_DIR).get_template("base.html")` loads;
  - with `extra_dirs=[tmp]` containing a `base.html` marker, that marker wins;
  - no `web_interface/templates` or `web_interface/static` dir exists;
  - `ice_ui.tiles.build_tiles(REMOTE_VIEWER_PERMISSIONS, {})` returns only the tiles whose permission set includes `view_status`;
  - `build_tiles(<owner permission set>, {})` returns all eight tiles, in today's order.
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** `uv run python -m scripts.refactor.codemod --phase 3`; implement `make_templates`; resolve review items.
- [ ] **Step 4:** Rebuild CSS with the new command. Expect **zero diff** on `app.css`, because the templates did not change. A diff is a stop condition (wrong `content` glob).
- [ ] **Step 5:** Full gate. If Chrome and Node are available locally, also run the four browser tests with `ICE_COLDER_BROWSER_TESTS=1` (CI enforces them regardless).
- [ ] **Step 6:** Commit `refactor: one UI definition in ice_ui (templates, static, levels, filters)`.

**PR 3:** open; CI green, with `TestScreenTemplatesUnchanged` passing against the legacy paths on `origin/main`.

---

## PR 4 — `ice_edge` layers, `ice_sims`, tests mirrored, boundaries enforced

### Task 7: Move all edge code into layer sub-packages  *(model: Sonnet)*

**Files:**
- Codemod phase 4, module rows except `simulators`.
- Create: a documented `__init__.py` for every edge layer in the target layout, each docstring stating its reference-architecture section with a `§`, including the stubs `ice_edge/uplink/__init__.py` and `ice_edge/cctv/__init__.py`; `firmware/README.md`; `tests/test_layout.py`.
- Modify: `Dockerfile` (CMD `["uv", "run", "python", "-m", "ice_edge"]`), `.vscode/launch.json`, `CLAUDE.md`, `README.md` (run command).

`firmware/README.md` covers the field-device/board layer: ESPHome configs will live here, the contracts they implement (`docs/contracts/vending-machine`, `docs/contracts/ice-maker-monitor`, and the layer-contracts spec §2–3), and the fact that `ice_sims` simulates them today.

Run the phase in seven `--only-prefix` slices, in this order, with `uv run pytest -q` and a commit (`refactor(edge): move <layers>`) after each:
1. `ice_edge.supervisor`, `ice_edge.config`
2. `ice_edge.ledger`, `ice_edge.hal`, `ice_edge.customer_ui`
3. `ice_edge.monitoring`, `ice_edge.access`, `ice_edge.notify`
4. `ice_edge.faults`, `ice_edge.payment`, `ice_edge.reporting`, `ice_edge.vend`
5. `ice_edge.sale`
6. `ice_edge.web`
7. `ice_edge.app`, `ice_edge.__main__`

**Interfaces:**
- `uv run python -m ice_edge` replaces `uv run python main.py` everywhere. There is no root `main.py` shim.
- When this task ends, `controller/`, `services/`, `config/` and `web_interface/` are gone.

- [ ] **Step 1: Write the failing test** `tests/test_layout.py`:
  - `test_legacy_roots_gone`: none of `controller`, `services`, `config`, `web_interface`, `simulators`, `contracts`, `hardware`, `main.py`, `hello.py` exists at the repo root;
  - `test_every_edge_layer_documented`: each layer named in the edge layer order has an `__init__.py` whose docstring contains `§`.
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Run the seven slices. If a slice fails with an import cycle, a runtime upward import was missed in Task 3. Move it under `TYPE_CHECKING` if it is annotation-only; otherwise **stop and report**.
- [ ] **Step 4:** `git grep -n -E "^\s*(from|import) (controller|services|web_interface|config)\b" -- "*.py"` → empty.
- [ ] **Step 5:** `uv run pytest -q` is green except `test_legacy_roots_gone` (simulators remain). Commit.

### Task 8: `ice_sims` package, compose and Dockerfile cleanup  *(model: Haiku)*

**Files:** codemod phase 4 `--only-prefix ice_sims`. Modify `docker-compose.yml` (the three `command:` lines become `["uv", "run", "python", "-m", "ice_sims.<ice_maker|mdb_gateway|vending_machine>", "--broker", "mosquitto"]`), `docker/docker-compose.yml` and `docker/docker-compose.prod.yml` (same, wherever a simulator command appears), `Dockerfile` (drop every legacy `COPY` line), and `CLAUDE.md`.

- [ ] **Step 1:** Run the codemod slice; resolve review items.
- [ ] **Step 2:** `uv run python -c "import ice_sims.vending_machine, ice_sims.ice_maker, ice_sims.mdb_gateway"` → exit 0.
- [ ] **Step 3:** `uv run pytest -q` → green, including `test_legacy_roots_gone`. Commit `refactor(sims): simulators become the ice_sims package`.

### Task 9: Mirror tests, retarget the ratchets, enforce edge layers  *(model: Sonnet)*

**Files:**
- Codemod phase 4 TEST_MOVES, which creates the `tests/<dir>/__init__.py` files.
- Modify: `tests/private_access_budget.py` (keys rewritten by text mode), `tests/test_no_private_access.py`, root `pyproject.toml` (`[tool.importlinter]` contracts 4–6), and `CLAUDE.md` (browser-test command paths and the boundary-enforcement paragraph).

**Interfaces:**
- **`test_no_private_access.py` keeps the private-access ratchet unchanged.** Its `TESTS_DIR.rglob` already recurses, so the moved files are still scanned.
- **Its import-boundary half is deleted:** the `controller/vmc.py` forbidden-import check (~line 305) and the "nothing under `controller/` imports `controller.machine`" check (~line 514). These import-linter contracts replace it; they cover the same direct imports, and contract 4 adds the full layer order:
  4. **layers:** the edge layer order above;
  5. **forbidden:** `ice_edge.sale.vmc` → `ice_edge.app`, `ice_edge.sale.test_sale`, `ice_edge.sale.maintenance_lease`, `ice_common.mqtt_client`, `ice_edge.monitoring.health_monitor`, `ice_edge.ledger.event_recorder`, `ice_edge.hal.command_dispatcher`, `ice_edge.customer_ui.display_controller`, and `ice_edge.ledger.session_store`, with `ignore_imports = ["ice_edge.sale.vmc -> ice_edge.ledger.session_store"]`. That ignore is CLAUDE.md's documented `SessionSnapshot` value-object exception, and it is the **only** permitted ignore anywhere. Contract 5 sets **`allow_indirect_imports = true`**, matching the old AST check, which examined only `vmc.py`'s direct imports. Indirect chains are legitimate and already exist at runtime (`vmc → dispense_cycle → command_dispatcher` for `CommandTimeout`, `→ event_recorder` for `SaleRecordingFailed`). Confirm the option name in the installed import-linter version;
  6. **independence:** `ice_edge`, `ice_cloud`, `ice_sims` (implied by contract 1; stated for readability).
- `CLAUDE.md`:
  - the browser-test command names `tests/edge/web/test_dashboard_v2_home_browser.py tests/edge/web/test_dashboard_v2_boosted_nav_browser.py tests/edge/web/test_users_codes_pin_browser.py tests/edge/web/test_touch_feedback_browser.py`;
  - the paragraph saying `tests/test_no_private_access.py` enforces the import boundary now names import-linter contracts 4–5 instead.

- [ ] **Step 1:** Before moving, record the test count: `uv run pytest -q --co` (last line) and `uv run python tests/test_no_private_access.py` (the counts).
- [ ] **Step 2:** `--phase 4 --only-prefix tests/ --dry-run`. It must report that every `tests/test_*.py` is mapped exactly once; add any unmapped file to the table first.
- [ ] **Step 3:** Run the test slice. `uv run pytest -q` → green, with the **same collected count** as Step 1.
- [ ] **Step 4:** `uv run python tests/meta/test_no_private_access.py` prints counts identical to Step 1.
- [ ] **Step 5:** Add contracts 4–6 and run `uv run lint-imports`. For each violation:
  - annotation-only → guard it under `TYPE_CHECKING`;
  - runtime upward import → **stop and report** the import line and the two layers. Do not reorder layers and do not add ignores.
- [ ] **Step 6:** Delete the import-boundary half of `test_no_private_access.py`. `tests/meta/test_import_contracts.py` now carries that guarantee.
- [ ] **Step 7:** Rewrite `CLAUDE.md`'s Architecture headings to the new layout ("FSM Core (`ice_edge/sale/vmc.py`, `ice_edge/app/machine.py`)" etc.). Module paths were already rewritten by text mode; empty the manual-review list.
- [ ] **Step 8:** Full gate. Commit `refactor(tests): mirror package layout; layer boundaries enforced by import-linter`.

**PR 4:** open. The description includes the edge layer table and the before/after test count and private-access counts, as evidence that no behaviour changed.

---

## PR 5 — Cloud monitoring server stub  (additive behaviour only)

### Task 10: Cloud monitoring server spec  *(model: Opus — design writing, no code)*

**Files:** Create `docs/superpowers/specs/2026-10-13-cloud-monitoring-server-design.md`.

Mandatory sections and the decisions each must record. These values are binding for Tasks 11–14.

1. **Scope.** The stub is a read-only fleet portal plus ingest. Explicitly out of scope: authentication, multi-tenancy, shadow and commands (typed stubs only), the mobile route app, video, MQTT/mTLS transport, payment webhooks. Reference architecture §8 and layer-contracts spec §4 remain the target.
2. **UI parity rule.**
   - The cloud renders **the same template files** as the machine (`ice_ui`).
   - Only cloud-only pages (the fleet list) live in `ice_cloud/templates`, prepended through `make_templates(..., extra_dirs=[cloud templates])`.
   - The viewer's permissions are `REMOTE_VIEWER_PERMISSIONS`. Per reference architecture §7, the machine dashboard is a subset of the portal, filtered by the same permission model.
3. **UI version: "both".**
   - **Digest.** `ui_digest` is the sha256 over the sorted (posix relpath, NUL, file bytes, NUL) of every file under `templates/` and `static/`, followed by the canonical JSON (`sort_keys=True`, `separators=(",", ":")`) of `levels_manifest()`.
   - **Reporting.** The machine reports its digest in every report.
   - **Resolution.** The cloud renders with its installed `ice_ui` when digests match. Otherwise it uses a cached bundle for the reported digest. If none is cached, it falls back to the installed `ice_ui` and shows a visible "UI version mismatch" banner.
   - **Upload.** Machines upload with `PUT /api/v1/ui-bundles/{digest}`, as a tar.gz containing `manifest.json` (`format: 1`, `digest`, `ice_ui_version`, `levels`), `templates/**` and `static/**`.
   - **Upload rejections:** absolute paths, `..` components, symlinks or hardlinks, non-regular members, members outside those three roots, total uncompressed size over 20 MB, or a recomputed digest that differs from the URL.
   - **Sandboxing.** Cached bundles render in a Jinja `SandboxedEnvironment`.
4. **Offline semantics.**
   - The cloud stores the latest `MachineReport` per machine.
   - A machine is **stale** when `now − sent_at > 3 × report_interval_seconds`.
   - A stale machine still renders every remote page from its last report, read-only, under the banner "Offline — showing last report from <local time> (<age> ago)".
   - The cloud never contacts a machine; it only reads stored reports.
5. **Machine selection.**
   - Templates use absolute URLs (`/status`, `/health/faults`, `/static/app.css`). To keep them byte-identical, the portal selects a machine with a cookie (`ice_cloud_machine`, set by `POST /fleet/select/{machine_id}`) and serves the machine pages at the same paths.
   - A per-machine URL prefix was rejected for the stub because it would change every template.
   - `/static/{path}` is served from the selected machine's resolved UI.
6. **Data contract `MachineReport` v0.1.0** (`ice_contracts.cloud.report`):
   - Fields: `contract: Literal["0.1.0"]`, `machine_id`, `sent_at` (UTC), `boot_id` (uuid4 per edge process), `seq` (int, monotonic per `boot_id`), `report_interval_seconds`, `edge_build` (commit short), `ui: UiIdentity{digest, ice_ui_version}`.
   - `health: HealthSummary` has exactly the fields of today's `health_snapshot()` dict, typed.
   - `kpi: KpiSummary` has exactly the fields `partials/kpi_fragment.html` reads.
   - `faults: list[FaultView]` has exactly the keys `FaultService.active_faults()` emits plus the optional `pending_sale` that `health_faults.html` reads.
   - The generated JSON Schema lives under `docs/contracts/edge-cloud/schemas/`.
   - A later contract version replaces this HTTP snapshot with spec §4's envelope, `seq` watermark and outbox.
7. **Transport (stub).**
   - The edge sends `POST /api/v1/machines/{machine_id}/report` (JSON → 204) and the bundle `PUT` above, both over plain HTTP.
   - The edge's `cloud.enabled` defaults to **false**.
   - Uploads never block a sale: they run in a supervised background task with a 5 s timeout, and failures are logged and dropped. There is no outbox yet; each report supersedes the previous one.
8. **Security (stub).** There is no authentication. `ice_cloud` binds to `127.0.0.1` by default and its compose file is marked dev-only. Auth and mTLS (spec §4) are **required before any deployment beyond localhost**.
9. **Deploy.** Separate image `packages/cloud/Dockerfile`, port 26124, data dir `ICE_CLOUD_DATA` (default `./cloud_data`, gitignored), and `docker/docker-compose.cloud.yml`. CI builds the image on every PR and never publishes it.
10. **Future work,** matching gap analysis §4.4: device shadow, signed remote commands with actor, event stream with `seq` watermark, per-role alert routing and escalation, multi-tenancy, offline-capable mobile route app, video.

- [ ] **Step 1:** Write the spec. **Step 2:** Check that every value listed above appears verbatim. **Step 3:** Commit `docs: cloud monitoring server stub design`.

### Task 11: `ice_contracts.cloud` models + schema generation  *(model: Sonnet)*

**Files:**
- Create: `packages/contracts/src/ice_contracts/cloud/{__init__,report,ui}.py`, `docs/contracts/edge-cloud/CONTRACT.md` (a short summary pointing to the spec), the generated `docs/contracts/edge-cloud/schemas/*.schema.json`, and `tests/contracts/test_cloud_contract.py`.
- Modify: `ice_contracts/generate.py` (add the edge-cloud output dir).

**Interfaces (the types Tasks 12–14 consume):**
- `ice_contracts.cloud.report`:
  - `CLOUD_CONTRACT_VERSION = "0.1.0"`;
  - `FaultView`, `PendingSaleView`, `HealthSummary`, `KpiSummary`, `UiIdentity` and `MachineReport`, each a `BaseModel` with `model_config = ConfigDict(extra="forbid", frozen=True)`;
  - field sets exactly as spec §6, derived by reading `ice_edge.web.context.health_snapshot`, `FaultService.active_faults`, `EventRecorder.get_summary`, and the templates `partials/status_fragment.html`, `partials/kpi_fragment.html`, `health.html` and `health_faults.html`. Every key those templates read must be a field, and no extra fields are allowed.
- `ice_contracts.cloud.ui`:
  - `LevelEntry(title: str, url: str, parent_url: str | None)`;
  - `BundleManifest(format: Literal[1], digest: str, ice_ui_version: str, levels: list[LevelEntry])`, with `digest` constrained to `^[0-9a-f]{64}$`.

**Tests:**
- `MachineReport.model_validate_json(r.model_dump_json()) == r` for a fully populated fixture;
- an unknown key → `ValidationError`;
- the digest pattern rejects uppercase and 63-character strings;
- schema drift: generate into `tmp_path` and compare bytes with the committed `docs/contracts/edge-cloud/schemas`, following the existing pattern in `tests/contracts/test_contract_schemas.py`.

- [ ] TDD cycle (failing tests → run → implement → pass) → full gate → commit `feat(contracts): edge-cloud MachineReport and UI bundle manifest v0.1.0`.

### Task 12: Shared view models on the edge + UI bundle + remote banner  *(model: Sonnet)*

No behaviour change for edge users.

**Files:**
- Create:
  - `packages/edge/src/ice_edge/views/{__init__,health}.py`;
  - `packages/ui/src/ice_ui/bundle.py`;
  - `packages/ui/src/ice_ui/templates/partials/remote_banner.html`;
  - `packages/ui/src/ice_ui/views/health.py` (re-exports `HealthSummary`, `FaultView`, `KpiSummary` for template authors);
  - `tests/edge/views/{__init__,test_health_view}.py`, `tests/ui/test_bundle.py`, `tests/ui/golden/base_no_remote.html`.
- Modify:
  - `ice_edge/web/context.py` (`health_snapshot` delegates and returns `HealthSummary`);
  - every Python caller that subscripts the snapshot (`git grep -n -E 'snap\[|snapshot\['`; known: `ice_edge/web/routes/home.py:219-224`) → attribute access;
  - `ice_ui/templates/base.html`: one `{% if remote %}{% include "partials/remote_banner.html" %}{% endif %}` as the first child of `<body>`;
  - `ice_ui/static/app.css` (rebuilt).

**Interfaces:**
- **`ice_edge.views.health`:**
  - `async def build_health_summary(*, vmc, event_recorder, health_monitor, maintenance_holder: Callable[[], str | None]) -> HealthSummary` is the body of today's `health_snapshot`, **moved, not rewritten**, and yields identical values.
  - `def build_fault_views(active_faults: list[dict]) -> list[FaultView]`.
  - `async def build_kpi_summary(event_recorder) -> KpiSummary`, the same computation the `/kpi` route does today, moved there and called by the route.
  - `context.health_snapshot()` becomes a one-line delegate that passes `context`'s globals.
  - Jinja `x.y` works on both dicts and models, so templates need no edit; Python callers need the `["…"]` → `.…` change.
- **`ice_ui.bundle`:**
  - `ICE_UI_VERSION = importlib.metadata.version("ice-ui")`;
  - `levels_manifest() -> list[LevelEntry]` covers every module-level `Level` instance in `ice_ui.levels`, sorted by `url`;
  - `compute_digest(root: Path, levels: list[LevelEntry]) -> str`, exactly per spec §3;
  - `ui_digest() -> str` is cached and uses the installed root;
  - `build_bundle(root: Path | None = None) -> bytes`;
  - `class BundleError(ValueError)`;
  - `extract_bundle(data: bytes, dest: Path) -> BundleManifest` enforces every spec §3 rejection, with a reason message for each.
- **`partials/remote_banner.html`** reads `remote.machine_id`, `remote.last_report_local`, `remote.age_text`, `remote.stale`, `remote.ui_mismatch`, `remote.report_digest` and `remote.portal_digest`.
  - When `stale`, it shows the spec §4 text.
  - When `ui_mismatch`, it shows "UI version mismatch: machine <report_digest[:8]>, portal <portal_digest[:8]>".
  - The edge never passes `remote`.

**Tests:**
- **Move-not-rewrite parity.** *Before* moving the body, add a test-local copy of the old `health_snapshot` dict computation to `tests/edge/views/test_health_view.py` as `_legacy_snapshot`, copied verbatim. On a `machine` fixture with one active fault and one PAY-104 pending sale, assert `build_health_summary(...).model_dump(mode="json") == _legacy_snapshot(...)`.
- Every existing `tests/edge/web/*` test passes unchanged.
- **Bundle:**
  - `compute_digest` is stable across calls, and changes when one template byte changes (`tmp_path` copy of the tree);
  - `extract_bundle(build_bundle())` round-trips and the digest matches;
  - one test per spec §3 rejection: absolute path, `../x`, symlink member, `other/x`, oversize (via the member size header), digest mismatch.
- **Golden.** *Before* editing `base.html`, render it with a fixed minimal context and commit the result as `tests/ui/golden/base_no_remote.html`. After the edit, rendering with `remote` unset must be byte-identical to that file.
- `test_static_css.py` passes after the `app.css` rebuild.

- [ ] TDD cycle → full gate → commit `feat(ui): shared health/KPI view models, UI bundle digest, remote banner`.

### Task 13: Edge uplink stub  *(model: Sonnet)*

Additive behaviour, off by default.

**Files:**
- Create: `packages/edge/src/ice_edge/uplink/{report,client,loop}.py`, `tests/edge/uplink/{__init__,test_report,test_client,test_loop}.py`.
- Modify: `ice_common/config_model.py` (`CloudConfig`), `config.example.json`, `packages/edge/pyproject.toml` (add `httpx`), `ice_edge/__main__.py`, and `CLAUDE.md` (eight config sections; one uplink paragraph).

**Interfaces:**
- **`CloudConfig(BaseModel)`:** `enabled: bool = False`, `url: str = "http://127.0.0.1:26124"`, `report_interval_seconds: int = Field(60, ge=10, le=3600)`. Add `ConfigModel.cloud: CloudConfig = CloudConfig()`; missing keys fill from defaults, as for every other section.
- **`ice_edge.uplink.report`:** `async def build_machine_report(machine: Machine, *, config: ConfigModel, seq: int, boot_id: str, now: datetime) -> MachineReport` uses the `ice_edge.views.health` builders and `ice_ui.bundle.ui_digest()`. It is a pure read and changes no machine state.
- **`ice_edge.uplink.client.HttpUplink`:**
  - `__init__(base_url: str, machine_id: str, *, timeout: float = 5.0, transport: httpx.AsyncBaseTransport | None = None)`;
  - `async post_report(report: MachineReport) -> bool` and `async put_ui_bundle(digest: str, data: bytes) -> bool`;
  - never raises: any exception or non-2xx logs a warning and returns `False`.
- **`ice_edge.uplink.loop`:** `async def run_uplink(machine, config, uplink: HttpUplink, *, sleep=asyncio.sleep, now=utc_now) -> None`.
  - First it calls `put_ui_bundle(ui_digest(), build_bundle())`, repeated once per interval until it returns `True`.
  - Then, forever, it builds and posts a report, sleeping `config.cloud.report_interval_seconds`, re-read each pass as the report scheduler does.
  - `seq` increments per attempt; `boot_id` is fixed per process.
- `__main__` adds `supervise("uplink", …)` only when `config.cloud.enabled`.

**Tests:**
- `build_machine_report` on the `machine_fake_time` fixture validates as a `MachineReport`, contains the active fault's code, and has `ui.digest == ui_digest()`.
- `HttpUplink` against `httpx.MockTransport`: 204 → `True`; 500 → `False`; a raised `httpx.ConnectError` → `False`, no exception.
- `run_uplink` with a fake `sleep` that raises `CancelledError` after the third call:
  - one bundle PUT, then three report POSTs with `seq` 0, 1, 2;
  - flipping `report_interval_seconds` to 10 mid-run is honoured (assert the fake sleep's arguments).
- `CloudConfig` defaults are as above. A `config.json` with no `cloud` key loads, and the existing config and first-run tests stay green.

- [ ] TDD cycle → full gate → commit `feat(edge): uplink stub posting MachineReport and UI bundle (off by default)`.

### Task 14: `ice_cloud` runnable skeleton  *(model: Sonnet)*

**Files:**
- Create:
  - under `packages/cloud/src/ice_cloud/`: `__init__.py`, `__main__.py`, `app.py`, `store.py`, `ui_registry.py`, `ingest.py`, `portal.py`, `shadow.py`, `commands.py`, `auth.py`, `templates/fleet.html`, `templates/not_remote.html`;
  - `packages/cloud/Dockerfile`, `docker/docker-compose.cloud.yml`;
  - `tests/cloud/{__init__,conftest,test_store,test_ui_registry,test_ingest,test_portal,test_ui_parity}.py`.
- Modify:
  - `.gitignore` (`cloud_data/`);
  - `.github/workflows/ci.yml`: `compose-config` also checks `docker/docker-compose.cloud.yml`; the `image` job also builds `packages/cloud/Dockerfile` (context `.`, `push: false`, `linux/amd64` only);
  - `CLAUDE.md`: a "Cloud (`ice_cloud`)" section of at most 12 lines pointing to the spec and stating that it is dev-only with no auth.

**Interfaces:**
- **`store.SnapshotStore(root: Path)`:**
  - `save(report: MachineReport) -> bool` writes atomically (tmp + `os.replace`) to `root/machines/<machine_id>.json`. It returns `False` and leaves the stored report alone when the stored report has the same `boot_id` and a `seq` ≥ the new one.
  - `latest(machine_id: str) -> MachineReport | None`.
  - `machines() -> list[MachineReport]`, sorted by `machine_id`.
  - `machine_id` must match `^[A-Za-z0-9_.-]{1,64}$`, otherwise `ValueError`.
- **`ui_registry`:**
  - `ResolvedUi`: a frozen dataclass of `templates: Jinja2Templates`, `static_dir: Path`, `mismatch: bool`, `digest: str`.
  - `UiRegistry(root: Path, *, cloud_templates: Path)` exposes `installed_digest: str`.
  - `register(digest: str, data: bytes) -> bool` returns `True` if the bundle is newly stored. It uses `extract_bundle` into `root/ui/<digest>/` and raises `BundleError` on any rejection.
  - `resolve(digest: str) -> ResolvedUi`, with one `Jinja2Templates` cached per digest:

| Digest | Templates | Static | `mismatch` |
|---|---|---|---|
| installed | `make_templates(ice_ui TEMPLATES_DIR, extra_dirs=[cloud_templates])` | `ice_ui.STATIC_DIR` | `False` |
| cached | `make_templates(<cached>/templates, sandboxed=True, extra_dirs=[cloud_templates])` | `<cached>/static` | `False` |
| unknown | installed | installed | `True` |
- **`app.create_app(*, data_dir: Path, now: Callable[[], datetime] = utc_now) -> FastAPI`** wires the store, the registry, and the ingest and portal routers. There is no `StaticFiles` mount, because static is served per resolved UI.
- **`ingest`:**
  - `POST /api/v1/machines/{machine_id}/report`: body `MachineReport`; returns 204, or 422 when the body's `machine_id` differs from the path.
  - `PUT /api/v1/ui-bundles/{digest}`: raw body; returns 201 if new, 200 if already present, 400 with the `BundleError` reason.
- **`portal`.** Every page renders through the resolved UI with:
  - `perms=REMOTE_VIEWER_PERMISSIONS`;
  - `current_user=RemoteViewer(display_name="Fleet viewer")`, a frozen dataclass exposing every attribute `base.html` reads from `current_user`;
  - `remote` (the banner context);
  - the same `level` objects the edge uses.

  Routes:
  - `GET /fleet` renders `fleet.html`, which extends the shared `base.html`: one row per machine with id, report age, online or stale, fault count, and a select button posting to `/fleet/select/{id}`.
  - `POST /fleet/select/{machine_id}` sets the cookie and returns 303 to `/`. An unknown machine returns 404.
  - `GET /` builds its tiles with `ice_ui.tiles.build_tiles(REMOTE_VIEWER_PERMISSIONS, lines)`, with `lines` derived from the report. It never keeps its own tile table.
  - `GET /`, `/status`, `/pill`, `/kpi`, `/health` and `/health/faults` use the **same template names and context keys** the corresponding edge routes use (read them from `ice_edge.web.routes.home`, `health.overview` and `health.faults`). Data comes from the stored report. With no machine selected, each redirects 303 to `/fleet`.
  - Any other GET returns 404, rendering `not_remote.html` ("Not available remotely yet").
  - `GET /static/{path:path}` serves from the resolved UI's `static_dir` with `Path.resolve()` containment; an escape attempt returns 404.
- **`shadow.ShadowService`** (`get_desired`, `set_desired`, `get_reported`) and **`commands.CommandService`** (`send(machine_id, command, *, actor)`) are typed stubs whose bodies raise `NotImplementedError("cloud spec §10")`.
- **`auth.py`** is a docstring only, restating spec §8.
- **`__main__`:** `uvicorn.run(create_app(data_dir=Path(os.environ.get("ICE_CLOUD_DATA", "cloud_data"))), host=os.environ.get("ICE_CLOUD_HOST", "127.0.0.1"), port=26124)`.
- **Dockerfile:** the edge pattern with `--package ice-cloud` only, `EXPOSE 26124`, and `CMD ["uv", "run", "python", "-m", "ice_cloud"]`. The compose file sets `ICE_CLOUD_HOST=0.0.0.0`, mounts `./cloud_data`, and starts with the comment `# dev only — no auth (cloud spec §8)`.

**Tests (`tests/cloud/`):**
- **`test_store`:**
  - save/latest round-trip;
  - an older `seq` from the same `boot_id` is ignored (`save` returns `False`);
  - a new `boot_id` with `seq=0` replaces the stored report;
  - `machine_id="../x"` raises;
  - no `*.tmp` remains after a save.
- **`test_ui_registry`:**
  - the installed digest → `mismatch=False`;
  - an unknown digest → `mismatch=True`;
  - after registering a bundle built from a `tmp_path` copy of the `ice_ui` tree with a marker inserted into `partials/status_fragment.html`, `resolve(that_digest)` renders the marker, and its env is a `SandboxedEnvironment`.
- **`test_ingest`:** 204; 422 on id mismatch; bundle PUT 201, then 200, then 400 for corrupt bytes.
- **`test_portal`:**
  - no cookie: `/` → 303 to `/fleet`;
  - select, then `/health/faults` contains the fault code;
  - `now` advanced past 3 × interval → 200 and "Offline — showing last report";
  - a report with an unknown UI digest → "UI version mismatch";
  - `/static/../pyproject.toml` → 404;
  - `/controls` → 404 and "Not available remotely yet".
- **`test_ui_parity`** is the owner's core requirement in executable form:
  1. On the `machine` fixture with one active fault, render each of the paths below through the edge app's `TestClient`, with the requesting user's permissions narrowed to `REMOTE_VIEWER_PERMISSIONS` (monkeypatch the permission set that `ice_edge.web.context.template_context` injects).
  2. Build a report from the same machine with `build_machine_report`.
  3. POST it to `create_app(data_dir=tmp_path)`, select the machine, and GET the same paths.
  4. Compare per path type, parsing with `tests/dom_utils.py`:
     - **Full pages (`/`, `/health`, `/health/faults`):** the inner HTML of `<main>` must be identical.
     - **Fragments (`/status`, `/pill`, `/kpi`):** these have no `<main>` (they are swapped into one), so the whole response body must be identical.
  5. Repeat with the cloud's `now` past the stale threshold. Every comparison above must still hold; only the banner differs, and it renders outside `<main>` and only on full pages.
  6. If a compared value is inherently time-relative on both sides (e.g. "since N s"), freeze both clocks to the same instant rather than stripping the text.

- [ ] TDD cycle → full gate (pytest, ruff check, ruff format --check, lint-imports, check_package_isolation; the cloud image builds in CI) → commit `feat(cloud): runnable fleet portal stub rendering the shared UI from stored reports`.

**PR 5:** open; the description links the spec and states prominently that the stub is dev-only with no auth.

---

## Verification checklist (end of every PR)

```
uv sync --locked
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
uv run lint-imports                               (PR 2+)
uv run python scripts/check_package_isolation.py  (PR 2+)
git status --porcelain                            (clean after commit)
```

**Also at the end of PR 4:**
- the collected test count equals the pre-reorg count plus the tests this plan added;
- `uv run python tests/meta/test_no_private_access.py` reports exactly the pre-reorg numbers.

## Stop conditions (report to the owner; do not work around)

- A runtime (non-`TYPE_CHECKING`) import points up the edge layer order, or across the top-level packages.
- A codemod review item has an unclear correct rewrite.
- Any diff under `docs/contracts/**` other than the new `edge-cloud/` dir.
- `TestScreenTemplatesUnchanged` fails.
- `app.css` changes in PR 3.
- Private-access counts change across a move.
- `exclude_type_checking_imports` is unavailable in import-linter.
- Any test needs a wall-clock wait to pass (project rule: fake time or bounded polling only).

## Not in this plan (local working copy only; nothing tracked)

Ignored or untracked leftovers: `colder-docker/` (an old project copy), `tmp/`, `inventory.json`, `skip-report.json`, `__pycache__/`. The owner deletes them by hand if they are no longer wanted.
