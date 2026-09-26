# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**ice-colder** is a vending machine controller (VMC) application. It manages product selection, payment processing, hardware communication (MDB bus), and provides a web-based dashboard for monitoring and configuration.

## Commands

| Task | Command |
|------|---------|
| Install dependencies | `uv sync` |
| Run the application | `uv run python main.py` |
| Run all tests | `uv run pytest` |
| Run a single test | `uv run pytest tests/test_file.py::test_name` |
| Lint/format | `ruff check --fix .` then `ruff format .` |
| Build Tailwind CSS | `.tailwind/tailwindcss.exe -c web_interface/tailwind.config.js -i web_interface/tailwind.input.css -o web_interface/static/app.css --minify` |

**Tailwind:** The binary is [v3.4.17 standalone CLI](https://github.com/tailwindlabs/tailwindcss/releases/download/v3.4.17/tailwindcss-windows-x64.exe); a Linux or macOS checkout needs the matching asset from the same release. The binary is gitignored (`.tailwind/`, ~40 MB); `web_interface/static/app.css` is **committed**. `tests/test_static_css.py` fails CI when a template uses a class the committed file lacks — but that test only checks that classes templates *use* are present, never that unused ones are *absent*. Rebuild and commit `app.css` by hand whenever templates change; a stale file (leftover classes from a deleted template, or a missing rebuild after one) passes CI silently and only a manual rebuild catches it.

## Architecture

### Entry Point & Startup (`main.py`)

`main()` loads `config.json` into a Pydantic `ConfigModel`, then runs three
concurrent asyncio tasks on a single event loop: a uvicorn web server (host/port
from `config.web`, default `0.0.0.0:26123`, with sessions persisted in
`data/access.json`), the MQTT client, and the health monitor. The MQTT client
and health monitor are wrapped in a supervisor that restarts them on crash; if
uvicorn exits, the process exits (Docker's `restart: unless-stopped` handles
process-level restarts).

### Configuration (`config/config_model.py`, `config.json`)

All configuration is a single Pydantic `ConfigModel` loaded from `config.json`. The model has six top-level sections: `version`, `physical` (machine details, people, products), `payment` (Stripe, PayPal, MDB), `communication` (email, SMS, Snapchat gateways), `mqtt` (broker connection), and `web` (dashboard host/port/trusted proxies) — plus the scalar `machine_id` field. `ConfigModel` exposes convenience properties (e.g., `config.products`, `config.machine_owner`, `config.stripe`) so consumers don't need to navigate the nested structure. Missing keys are filled from Pydantic defaults at load time. Saves via
`services/config_store.py` are atomic (tmp + rename), write real secret values,
and keep a rolling `config.json.bak`.

The config file path is configurable via the `ICE_COLDER_CONFIG` environment
variable (read at call time by both `main.py` and `services/config_store.py`),
defaulting to `config.json` in the current working directory when unset. This
lets Docker point the app at a writable, bind-mounted location instead of
relying on a bind-mount targeting `config.json` directly (which would let
Docker create it as a directory on a fresh clone, since the file is
gitignored). If the resolved config path exists but is a directory, startup
logs a clear error and exits with code 1 rather than papering over it.

### FSM Core (`controller/vmc.py`)

`VMC` is a finite state machine built on the `transitions` library. States: `idle` -> `interacting_with_user` -> `dispensing` -> back to `idle` (or `error` from any state). Extra transitions: `cancel_sale` (interacting → idle, catalog edit removed the selection) and `vend_failed` (dispensing → interacting, price restored to escrow, product locked out per `contracts/vending_machine.py` `FAULT_TABLE`). Refunds are real: `request_refund` publishes `cmd/payment/refund` and tracks the ack. The transition table is defined as a list of dicts (`TRANSITIONS`) at module level. Business logic (deposit funds, select product, dispense, refund) lives as methods on `VMC`. The VMC holds a reference to the live `ConfigModel` and a `PaymentGatewayManager`. Heartbeat loss raises `COM-101` (vending), `COM-102` (ice maker), `PAY-101` (MDB) and `COM-103` (broker) through the fault registry and auto-clears on recovery.

### Web Dashboard (`web_interface/`)

FastAPI app (`server.py`) with Jinja2 templates and HTMX-driven partials. `routes/` is a package with one module per area (`home.py`, `health.py`, `products.py`, `inventory.py`, `reports.py`, `controls.py`, `tests_level.py`, `users.py`, `settings.py`); `routes/__init__.py` wires them all with `attach_routes`. Templates live in `web_interface/templates/` with HTMX partial fragments in `templates/partials/`. Static assets in `web_interface/static/`.

**The v2 shell** (`base.html` + `web_interface/levels.py`) replaces the tabbed dashboard. Every level is a real URL and every level has its own breadcrumb trail and Back button that goes to the parent, not through history. `base.html` defines three blocks: `title` (for `<title>`), `body` (the sole content of `<main>`, where your level goes), and `bar_variant` (the bar header, not to be overridden). The bar itself uses `hx-swap-oob="true"` to deliver an out-of-band swap on every response, so a boosted navigation updates it with no reload. The navigation tree is a hierarchy of 24 named levels plus three parameterized ones (a product SKU, a subsystem name, a user id). Each level is defined in `web_interface/levels.py` as a frozen dataclass with `title`, `url`, `parent`, and properties `crumbs`, `parent_url`, plus a classmethod `Level.child(parent, title, url)` for parameterized levels.

The tree, every URL with the permission that gates it (`Permission.<x>` from `services/access.py`; "A or B" is `_require_any(A, B)`, a local OR-semantics dependency defined in `products.py` and `settings.py` — `web_auth.require()` itself is AND-only):

| URL | Gate |
|---|---|
| `/` | `view_status` (held by every role) |
| `/health` | `view_status` |
| `/health/subsystems` | `view_status` |
| `/health/subsystems/{name}` | `view_status` |
| `/health/faults` | `view_status` |
| `/health/availability` | `view_status` |
| `/health/logs` | `view_logs` |
| `/products` | `edit_catalog` **or** `edit_placement` |
| `/products/new` | `edit_catalog` |
| `/products/{sku}` | `edit_catalog` **or** `edit_placement` |
| `/products/{sku}/catalog` | `edit_catalog` |
| `/products/{sku}/placement` | `edit_placement` |
| `/products/{sku}/copy` | `edit_catalog` |
| `/inventory` | `edit_placement` |
| `/reports` | `view_reports` |
| `/controls` | `machine_controls` |
| `/tests` | `run_tests` |
| `/users` | `manage_users` |
| `/users/new` | `manage_users` |
| `/users/{id}` | `manage_users` |
| `/devices` | `manage_users` |
| `/users/codes` | `manage_ownership` |
| `/users/ownership` | `manage_ownership` |
| `/settings` | `edit_contacts` **or** `edit_secrets` |
| `/settings/machine` | `edit_contacts` |
| `/settings/contacts` | `edit_contacts` |
| `/settings/payments` | `edit_secrets` |
| `/settings/comms` | `edit_secrets` |
| `/settings/mqtt` | `edit_secrets` |
| `/settings/web` | `edit_secrets` |

`/devices` sits under Users in this **navigation** tree (`LEVEL_DEVICES`'s `parent` is `LEVEL_USERS` in `web_interface/levels.py`) even though its URL is not under `/users/` — the tree is a navigation hierarchy, not a URL-prefix hierarchy. Every mutating route under these levels additionally requires `Depends(context.require_htmx)`.

`web_interface/context.py` holds shared state (config, VMC, health monitor, access store, availability, inventory manager) and helpers: `template_context(request, level=None, **extra)` injects request, current_user and perms into every template, `require_htmx` guards all POST routes (the CSRF dependency), `tail(file_path, lines=50)` reads log tails, `health_snapshot()` is the single health predicate shared by `/status` and `/pill` (so the hero and pill never disagree), and `LOW_STOCK_THRESHOLD = 3` (a tracked product is "low" at or below this count; not configurable per-product yet).

The dashboard uses cookie-based session auth via `services/access.py`'s `AccessStore`,
persisted in `data/access.json` (mode 0600). Users have four roles (`owner`,
`secretary`, `tech`, `loader`) and a 4–8 digit PIN. First login on a new device
requires a second factor: a 6-digit OTP sent by email (when
`communication.email_gateway` is configured) or an 8-digit emergency code,
which works offline. On first boot, setup mode redirects every route to `/setup`
behind a code that exists only at the machine. The `Backoff` class enforces
exponential back-off per `(kind, subject, client)` tuple plus a per-user budget
for untrusted clients — no hard caps. Every route is gated by `require(Permission)`;
templates receive `perms` and `current_user` via `template_context` so the server
does not render controls it would refuse. POST routes require the `HX-Request`
header (HTMX's own requests set it), which blocks a plain cross-site form post
as a CSRF guard. The only fragment endpoints are `/status` (home hero, 1 s), `/kpi` (home KPIs, 60 s), and `/pill` (health indicator, 5 s); lists and forms load once and refresh on action.

The Home level shows a status strip (hero showing the next scheduled event or the health summary) plus a tile grid of eight tiles: Health, Products, Inventory, Reports, Controls, Tests, Users, Settings. A tile is rendered only when `perms` intersects that tile's permission set; the grid adapts to screen width (4×2 on desktop, 2×4 on tablets, 1 column on phones). Each tile links to its level, and the pill in the top-right taps to `/health/faults`.

Tailwind v3.4.17 and HTMX 1.9.10 are **vendored** in `static/app.css` and `static/htmx.min.js`, so the tablet operator's dashboard works with no internet — that claim covers the dashboard proper; the customer-facing `/screen` page (and `/screen/body`) is the one exception, keeps CDN references, stays byte-identical to `origin/main`, and is out of scope for part 2. Secrets on Settings pages are masked: a placeholder is rendered for a field that is already set, and posting the unchanged placeholder leaves the stored value alone. The MQTT page shows the effective value and disables the field when an env override is active, and never writes an env value into `config.json`. Machine id, web host and web port are displayed but not editable — the handlers accept and ignore those fields even if a direct POST supplies them.

`partials/confirm_button.html` (Task 5) is the server-rendered two-tap confirm used by Controls, the Health fault Clear and the Users code Regenerate, with **no JavaScript state**. Parameters: `label`, `confirm_label`, `post_url`, `target` (an `#id` selector), `confirm_url`, and optional `confirming` (default false). The first tap `hx-get`s `confirm_url`, which the including route renders in its confirming state; the second tap `hx-post`s `post_url`. Cancel re-`hx-get`s `confirm_url` with `confirming=false` — so **any route backing a confirm button must honour that query parameter**: absent or anything but the literal string `"false"` renders the Confirm/Cancel pair, `"false"` renders the plain first-tap button.

`POST /inventory/{sku}/adjust` (the Inventory restock level, `web_interface/routes/inventory.py`) takes one form field, `delta`, accepting exactly `-10`, `-1`, `1`, `10` (a loader tapping the four adjust buttons); anything else — including a non-integer string — returns **400** with the stored count unchanged. A valid delta **clamps the count at zero** rather than erroring, so tapping `-10` on a count of 3 lands on 0 — deliberately different from the Products placement form (`POST /products/{sku}/placement`), which *rejects* a typed negative count outright and leaves the stored value unchanged.

The Health level is split across six sub-levels rather than one merged page. `/health` itself shows only a four-tile summary (Subsystems, Faults, Availability, Logs) plus the VMC's own build identity from `services/build_info.py` (image env vars set by CI, or `git` when run from a checkout). `/health/subsystems` and `/health/subsystems/{name}` show heartbeats (liveness, uptime) and each subsystem's retained `capabilities/<subsystem>` document (`SubsystemCapabilities`: firmware, contract version, brand/model, hardware_id, ip) — subsystems in `EXPECTED_SUBSYSTEMS` are listed even before they speak. `/health/faults` lists active faults with a Clear action (gated on `clear_faults`, the two-tap confirm above); `/health/availability` shows the permissive truth table and payment-blocking reasons; `/health/logs` (gated on `view_logs`) shows the last **50** lines of `LOGS/vmc.log` (`context.tail`), not the pre-v2 fragment's 10.

### Services (`services/`)

- `payment_gateway_manager.py` - manages Stripe/PayPal/Square gateways, generates QR codes via `qrcode` library
- `config_store.py` - persists config changes (add/update products) back to `config.json`
- `access.py` - session auth with roles, PINs, devices, emergency codes, and back-off; persists to `data/access.json`
- `mailer.py` - sends OTP and setup-code emails via SMTP
- `fsm_control.py` - translates admin commands (restart, reset, shutdown) into actions
- `availability.py` - permissive truth table (ROADMAP §3) split into three
  gates: `safety` rows block payment and sales, `fulfillment` rows block only
  the individual sale, `alert` rows block nothing. Publishes
  `cmd/payment/enable` on change; feeds the health tab and `/screen`. Only the
  six codes in `contracts.vending_machine.PAYMENT_BLOCKING_FAULTS` can inhibit
  payment.
- `session_store.py` - atomic snapshot of the live sale in `data/session.json`;
  an open snapshot at boot raises `PAY-104`, which alerts the operator and
  holds the evidence file until an admin clears it, but never inhibits payment
- `paths.py` - `LOG_DIR`, `LOG_FILE`, `DATA_DIR` shared by main, routes and services
- `auth_policy.py` - PIN policy (`pin_problem`); validates 4–8 digits with no repeats or runs; identifies loopback hosts (`is_loopback`)

### Hardware (`hardware/`)

- `mdb_interface.py` - reference/simulation stub; real MDB communication happens on the ESP32 firmware and arrives over MQTT, not this module
- `button_panel.py`, `camera_monitor.py`, `ice_maker.py` - hardware control modules

### Docker

`Dockerfile` builds from `python:3.12-slim`, installs dependencies with
`uv sync --frozen --no-dev` from `pyproject.toml`/`uv.lock`, and runs
`uv run python main.py` (port 26123). `docker-compose.yml` orchestrates the VMC,
the three ESP32 simulators, and a mosquitto broker, all with
`restart: unless-stopped`. There is no `requirements.txt` — `pyproject.toml` is
the single dependency source of truth. Inside compose, config lives at
`data/config.json` (bind-mounted `./data:/app/data`, writable for the `vmc`
service and read-only for the simulators), pointed to via `ICE_COLDER_CONFIG`
in each service's `environment` — not bind-mounted directly as
`config.json`, since that file is gitignored and doesn't exist on a fresh
clone.

CI/CD: `.github/workflows/ci.yml` runs ruff and pytest on every push/PR and,
on `main`, publishes the image to `ghcr.io/conradstorz/ice-colder` (`latest`
and `sha-<commit>`). Compose services reference that image (with `build: .`
kept for local `--build`) and carry the Watchtower enable label, so the
simulation host updates itself; `docker compose pull` then `up -d` forces it.
A `compose-config` CI job runs `docker compose config` against both compose
files with `.env.example` to catch YAML/interpolation errors before `image`
builds.

Both the root `docker-compose.yml` and `docker/docker-compose.prod.yml`
require a `.env` file (`cp .env.example .env`) and run a one-shot
`mosquitto-init` service that writes the broker's password file from it
before `mosquitto` starts; `docker/docker-compose.yml` stays an anonymous
broker for local development only. `mosquitto-init` rewrites that file from
`.env` on every start (`mosquitto_passwd -c`), so accounts added by hand are
discarded on the next `up`; the optional `HA_MQTT_USERNAME`/`HA_MQTT_PASSWORD`
pair adds a second account for Home Assistant and is skipped when the password
is empty. `MQTT_USERNAME`/`MQTT_PASSWORD` from
`.env` are passed into the VMC and simulators and read by
`main.apply_env_overrides`, which returns an `EnvOverrides` (a `model_copy`
of `config.mqtt` with env values applied, plus the resolved trusted-proxies
list) without mutating the live `ConfigModel` — so an env-only
`MQTT_PASSWORD` can never be written back to `config.json` by a later
`save_config`. `ICE_COLDER_TRUSTED_PROXIES` is resolved the same way and
applied to the dashboard's login back-off via
`routes.backoff.set_trusted_proxies(...)`, called after
`routes.set_config_object(...)` so the env value wins.

## Key Patterns

- **Logging**: Uses `loguru` throughout; logs rotate daily to `LOGS/vmc.log`. State changes are prefixed with `STATE_CHANGE_PREFIX`.
- **Config mutation**: Product changes go through `services/config_store.py` which writes back to `config.json`. The in-memory `ConfigModel` is mutated directly (Pydantic models with mutable fields).
- **Web UI updates**: The dashboard uses HTMX to swap HTML partials from FastAPI endpoints. No SPA framework.

## Removed Routes (Dashboard v2)

The v2 shell replaces all URLs from the old tabbed dashboard. Anyone holding a bookmark or an external script can find the replacement here:

| Removed | Replacement |
|---|---|
| `GET /` (old tabbed dashboard body) | `GET /` (the v2 Home: status strip + tile grid) |
| `GET /health` (fragment) | `GET /health` (a level) and its four sub-levels |
| `GET /logs` | `GET /health/logs` (50 lines, was 10) |
| `GET /activity` | `GET /reports?period=` |
| `POST /action/{command}` | `POST /controls/{command}` |
| `POST /faults/{key}/clear` | `POST /health/faults/{key}/clear` |
| `GET /inventory` (table fragment) | `GET /inventory` (the restock level) |
| `GET /inventory/new`, `POST /inventory/add` | `GET`/`POST /products/new` |
| `GET /inventory/copy/{sku}` | `GET /products/{sku}/copy` |
| `GET /inventory/edit/{sku}/catalog`, `POST /inventory/update/{sku}/catalog` | `GET`/`POST /products/{sku}/catalog` |
| `GET /inventory/edit/{sku}/placement`, `POST /inventory/update/{sku}/placement` | `GET`/`POST /products/{sku}/placement` |
| `POST /inventory/delete/{sku}` | `POST /products/{sku}/delete` |
| `GET /config/machine` | `GET`/`POST /settings/machine` |
| `GET /config/contacts` | `GET`/`POST /settings/contacts` |
| `GET /config/payments` | `GET`/`POST /settings/payments` |
| `GET /config/comms` | `GET`/`POST /settings/comms` |
| the old `/users/*` and `/devices/*` forms | the `/users` and `/devices` levels |
