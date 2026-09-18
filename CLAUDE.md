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

`VMC` is a finite state machine built on the `transitions` library. States: `idle` -> `interacting_with_user` -> `dispensing` -> back to `idle` (or `error` from any state). Extra transitions: `cancel_sale` (interacting → idle, catalog edit removed the selection) and `vend_failed` (dispensing → interacting, price restored to escrow, product locked out per `contracts/vending_machine.py` `FAULT_TABLE`). Refunds are real: `request_refund` publishes `cmd/payment/refund` and tracks the ack. The transition table is defined as a list of dicts (`TRANSITIONS`) at module level. Business logic (deposit funds, select product, dispense, refund) lives as methods on `VMC`. The VMC holds a reference to the live `ConfigModel` and a `PaymentGatewayManager`.

### Web Dashboard (`web_interface/`)

FastAPI app (`server.py`) with Jinja2 templates and HTMX-driven partials. `routes.py` defines all endpoints and receives the `ConfigModel` and `VMC` instance via setter functions called from `main.py`. Templates live in `web_interface/templates/` with HTMX partial fragments in `templates/partials/`. Static assets in `web_interface/static/`.

### Services (`services/`)

- `payment_gateway_manager.py` - manages Stripe/PayPal/Square gateways, generates QR codes via `qrcode` library
- `config_store.py` - persists config changes (add/update products) back to `config.json`
- `fsm_control.py` - translates admin commands (restart, reset, shutdown) into actions

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

## Key Patterns

- **Logging**: Uses `loguru` throughout; logs rotate daily to `LOGS/vmc.log`. State changes are prefixed with `STATE_CHANGE_PREFIX`.
- **Config mutation**: Product changes go through `services/config_store.py` which writes back to `config.json`. The in-memory `ConfigModel` is mutated directly (Pydantic models with mutable fields).
- **Web UI updates**: The dashboard uses HTMX to swap HTML partials from FastAPI endpoints. No SPA framework.
