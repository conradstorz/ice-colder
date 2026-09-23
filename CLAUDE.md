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

## Architecture

### Entry Point & Startup (`main.py`)

`main()` loads `config.json` into a Pydantic `ConfigModel`, then runs three
concurrent asyncio tasks on a single event loop: a uvicorn web server (host/port
from `config.web`, default `0.0.0.0:26123`, HTTP Basic auth from
`config.web.admin_username`/`admin_password`), the MQTT client, and the health
monitor. The MQTT client and health monitor are wrapped in a supervisor that
restarts them on crash; if uvicorn exits, the process exits (Docker's
`restart: unless-stopped` handles process-level restarts).

### Configuration (`config/config_model.py`, `config.json`)

All configuration is a single Pydantic `ConfigModel` loaded from `config.json`. The model has six top-level sections: `version`, `physical` (machine details, people, products), `payment` (Stripe, PayPal, MDB), `communication` (email, SMS, Snapchat gateways), `mqtt` (broker connection), and `web` (dashboard host/port/admin credentials) — plus the scalar `machine_id` field. `ConfigModel` exposes convenience properties (e.g., `config.products`, `config.machine_owner`, `config.stripe`) so consumers don't need to navigate the nested structure. Missing keys are filled from Pydantic defaults at load time. Saves via
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

FastAPI app (`server.py`) with Jinja2 templates and HTMX-driven partials. `routes.py` defines all endpoints and receives the `ConfigModel` and `VMC` instance via setter functions called from `main.py`. Templates live in `web_interface/templates/` with HTMX partial fragments in `templates/partials/`. Static assets in `web_interface/static/`.

The dashboard is HTTP Basic auth (`config.web.admin_username`/`admin_password`); `web_interface/auth.py`'s `LoginLimiter` locks out a client IP after repeated failed logins within a sliding window, trusting `X-Forwarded-For` only from `config.web.trusted_proxies`. POST routes require the `HX-Request` header (HTMX's own requests set it), which blocks a plain cross-site form post as a CSRF guard.

The System Health tab (`/health`) merges three sources: heartbeats (liveness,
uptime), each subsystem's retained `capabilities/<subsystem>` document
(`SubsystemCapabilities`: firmware, contract version, brand/model,
hardware_id, ip), and the VMC's own build identity from
`services/build_info.py` (image env vars set by CI, or `git` when run from a
checkout). Subsystems in `EXPECTED_SUBSYSTEMS` are listed even before they speak.

### Services (`services/`)

- `payment_gateway_manager.py` - manages Stripe/PayPal/Square gateways, generates QR codes via `qrcode` library
- `config_store.py` - persists config changes (add/update products) back to `config.json`
- `fsm_control.py` - translates admin commands (restart, reset, shutdown) into actions
- `availability.py` - permissive truth table (ROADMAP §3); publishes `cmd/payment/enable` on change; feeds the health tab and `/screen`
- `session_store.py` - atomic snapshot of the live sale in `data/session.json`; an open snapshot at boot raises `PAY-104` until an admin clears it
- `paths.py` - `LOG_DIR`, `LOG_FILE`, `DATA_DIR` shared by main, routes and services
- `auth_policy.py` - admin-password policy shared by first-run setup and startup checks: rejects empty/default/short passwords, generates a random first-run password, identifies loopback hosts

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
broker for local development only. `MQTT_USERNAME`/`MQTT_PASSWORD` from
`.env` are passed into the VMC and simulators and read by
`main.apply_env_overrides`, which returns an `EnvOverrides` (a `model_copy`
of `config.mqtt` with env values applied, plus the resolved trusted-proxies
list) without mutating the live `ConfigModel` — so an env-only
`MQTT_PASSWORD` can never be written back to `config.json` by a later
`save_config`. `ICE_COLDER_TRUSTED_PROXIES` is resolved the same way and
applied to the dashboard's login limiter via
`routes.login_limiter.set_trusted_proxies(...)`, called after
`routes.set_config_object(...)` so the env value wins.

## Key Patterns

- **Logging**: Uses `loguru` throughout; logs rotate daily to `LOGS/vmc.log`. State changes are prefixed with `STATE_CHANGE_PREFIX`.
- **Config mutation**: Product changes go through `services/config_store.py` which writes back to `config.json`. The in-memory `ConfigModel` is mutated directly (Pydantic models with mutable fields).
- **Web UI updates**: The dashboard uses HTMX to swap HTML partials from FastAPI endpoints. No SPA framework.
