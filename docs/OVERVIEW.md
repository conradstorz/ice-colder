# ice-colder — Project Overview

ice-colder is the control software for a real, unattended **ice-and-water vending machine** whose factory
controller is being replaced. It runs the sale, keeps the money honest, watches the hardware, tells the
owner when something is wrong, and gives operators a touch-friendly dashboard — and it is built to keep
selling when anything around it fails.

This page is the map. Each section says what the project does in a few lines and links to the document
that explains it in full. For setup and day-to-day running, start with the [README](../README.md).

## Contents

- [The machine](#the-machine)
- [Architecture at a glance](#architecture-at-a-glance)
- [Repository layout](#repository-layout)
- [What it does](#what-it-does)
  - [Selling: the sale state machine](#selling-the-sale-state-machine)
  - [Money: escrow, refunds and durable sales](#money-escrow-refunds-and-durable-sales)
  - [Dispensing and vend verification](#dispensing-and-vend-verification)
  - [Faults and availability](#faults-and-availability)
  - [Health monitoring and subsystem windows](#health-monitoring-and-subsystem-windows)
  - [The operator dashboard](#the-operator-dashboard)
  - [Roles, access and remote exposure](#roles-access-and-remote-exposure)
  - [System tests and maintenance standby](#system-tests-and-maintenance-standby)
  - [Sales reports and alerting](#sales-reports-and-alerting)
  - [Customer screen](#customer-screen)
  - [Cloud fleet portal](#cloud-fleet-portal)
- [Contracts between layers](#contracts-between-layers)
- [Simulators and Home Assistant](#simulators-and-home-assistant)
- [Running and deploying](#running-and-deploying)
- [Developing and testing](#developing-and-testing)
- [Status and roadmap](#status-and-roadmap)
- [Document index](#document-index)

## The machine

A commercial ice vending machine: a stand-alone ice maker filling an insulated hopper, an agitator and
dispense motor feeding a bagging station (inflation fan, bag-full sensor, trap door), a metered water
dispenser (1- and 5-gallon), an autonomous cash/MDB payment system, a freeze-protection heater, and a
customer display. The guiding safety rule: **the computer supervises, but it is never the only thing that
stops a motor, closes a valve, or limits a heater** — hardwired interlocks do that.

The real machine is a Kooler Ice IM600XL; its first-iteration inputs and outputs, from replacing its
Siemens PLC, are listed in the [IM600XL I/O list](hardware/im600xl-io-list.md).

→ [ROADMAP §1 The machine](../ROADMAP.md#1-the-machine) · [ROADMAP §2 Responsibility split](../ROADMAP.md#2-responsibility-split) ·
[IM600XL I/O list](hardware/im600xl-io-list.md)

## Architecture at a glance

Four tiers, each of which keeps working when the tier above it fails:

| Tier | What it is | In this repo |
|---|---|---|
| Field devices | Motors, valves, sensors, MDB payment devices | — (physical) |
| Subsystem boards | ESP32s that own real-time I/O and local interlocks | `firmware/` (future), simulated by `ice_sims` |
| Edge controller | Raspberry Pi: sale logic, ledger, faults, dashboard | `ice_edge` |
| Cloud | Fleet monitoring portal | `ice_cloud` (dev-only stub) |

Boards and the edge talk over **MQTT**; every command carries a `request_id` and is idempotent, so a retry
never vends twice. Home Assistant may observe the bus but never controls the machine.

→ [Reference architecture](vending-system-reference-architecture.md) (the target system) ·
[Layer contracts spec](superpowers/specs/2026-10-13-layer-contracts-design.md) ·
[Gap analysis](aspirational-gap-analysis.md) (how far the code is from the target) ·
[PLAN.md](../PLAN.md) (the original asyncio/MQTT migration)

## Repository layout

A `uv` workspace of layered packages; a package may only import packages below it (enforced by
`uv run lint-imports` and `scripts/check_package_isolation.py`).

| Package | Role |
|---|---|
| `packages/contracts` → `ice_contracts` | Pydantic models for every wire contract, roles/permissions, schema generator |
| `packages/common` → `ice_common` | Config model, logging, MQTT client, task supervisor/lifecycle |
| `packages/ui` → `ice_ui` | Jinja templates, static assets and view helpers shared by edge and cloud |
| `packages/edge` → `ice_edge` | The vending machine controller itself (`uv run python -m ice_edge`) |
| `packages/cloud` → `ice_cloud` | Fleet portal stub |
| `packages/sims` → `ice_sims` | Simulated ESP32 boards |

→ [CLAUDE.md — Layout](../CLAUDE.md#layout) (full target tree and mapping to the architecture) ·
[Layered workspace reorg plan](superpowers/plans/2026-10-13-layered-workspace-reorg.md)

## What it does

### Selling: the sale state machine

An explicit finite-state machine (`idle → interacting_with_user → dispensing → idle`, plus `error`) is the
only component allowed to move money or start a vend. It holds no transport handles; everything it
publishes goes through one outbound channel, and every timer is labelled so tests can fire it directly.
`Machine` is the composition root that builds it and all its collaborators.

→ [VMC as the sale FSM](superpowers/specs/2026-10-10-vmc-sale-fsm-core-design.md) ·
[VMC public surface](superpowers/specs/2026-10-09-vmc-public-surface-design.md) ·
[VMC diet (collaborator extraction)](superpowers/specs/2026-10-12-vmc-diet-design.md) ·
[CLAUDE.md — FSM Core](../CLAUDE.md#fsm-core-packagesedgesrcice_edgesalevmcpy-packagesedgesrcice_edgeappmachinepy)

### Money: escrow, refunds and durable sales

- **FIFO escrow** — credits are consumed first-in-first-out and a failed vend restores exactly the same
  per-method shares, so money is never reclassified.
- **Refunds** — request → ack → one retry with the *same* `request_id` → `PAY-103` if still unconfirmed.
- **Durable sales** — each sale is written synchronously to SQLite (WAL) before the machine returns to idle,
  with a journal fallback replayed idempotently at boot; a corrupt database is set aside and selling continues.
- **Crash recovery** — an in-flight sale is snapshotted to disk; a power loss mid-vend raises `PAY-104` for
  an operator to "Record as sale" or "Discard" — never guessed.

→ [Unattended operation](superpowers/specs/2026-09-21-unattended-operation-design.md) ·
[Soft faults never stop the machine](superpowers/specs/2026-09-24-soft-faults-non-blocking-design.md) ·
[ROADMAP §7 Payment and refund policy](../ROADMAP.md#7-payment-and-refund-policy)

### Dispensing and vend verification

Each physical slot has a hand-edited mechanism profile in `dispensers.toml` (agitate/fill/release timings,
channels, accessories), validated against the product catalog and the board's declared capabilities. The
edge sends the whole profile with each dispense command, and verifies the outcome per mechanism — bagged ice
and water fill each have their own outcome table mapping to a customer result and a fault. A failed vend
restores credit and locks out the product. Check a profile file with
`uv run python -m ice_edge.vend.dispensers --check`.

→ [Dispenser profiles design](superpowers/specs/2026-10-06-dispenser-profiles-design.md) ·
[Schema plan](superpowers/plans/2026-10-07-dispenser-profiles-schema.md) ·
[Runtime plan](superpowers/plans/2026-10-08-dispenser-profiles-runtime.md) ·
[`dispensers.example.toml`](../dispensers.example.toml) ·
[ROADMAP §4 Product sequences and interlocks](../ROADMAP.md#4-product-sequences-and-interlocks)

### Faults and availability

Every problem is a coded fault (`PAY-102`, `ICE-402`, `CFG-101`, …) with a severity and a scope (machine or
product). A permissive truth table turns active faults into three gates: **safety** rows block payment and
sales, **fulfillment** rows block one sale, **alert** rows block nothing. Only seven codes can ever stop the
machine taking money. Heartbeat and broker loss raise and auto-clear their own faults.

→ [Fault registry and honest vend outcomes](superpowers/specs/2026-09-17-fault-registry-vend-outcomes-design.md) ·
[Soft faults never stop the machine](superpowers/specs/2026-09-24-soft-faults-non-blocking-design.md) ·
[ROADMAP §3 Availability permissives](../ROADMAP.md#3-availability-permissives) ·
[ROADMAP §5 Fault codes](../ROADMAP.md#5-fault-codes) ·
[Vending-machine contract (fault table)](contracts/vending-machine/CONTRACT.md)

### Health monitoring and subsystem windows

Every board publishes heartbeats and a retained capabilities document (firmware, contract version, model,
declared input/output channels). The Health level shows each board's identity and a live window of exactly
the channels it declares — inputs as circles, outputs as squares, with dwell times and an "inhibited" mark
when the VMC would currently refuse the command that drives an output.

→ [Subsystem identity on the Health tab](superpowers/specs/2026-09-18-subsystem-identity-health-tab-design.md) ·
[Subsystem windows](superpowers/specs/2026-09-30-subsystem-windows-design.md)

### The operator dashboard

A FastAPI + HTMX dashboard designed for a 7-inch tablet with no internet (Tailwind and HTMX are vendored).
Home is a status strip and a grid of tiles — Health, Products, Inventory, Reports, Controls, Tests, Users,
Settings — and every level is a real, bookmarkable URL with breadcrumbs and a Back button that goes to the
parent. Every control gives instant press/busy feedback and a click sound.

→ [Dashboard v2 shell](superpowers/specs/2026-09-25-dashboard-v2-shell-design.md) ·
[Dashboard v2 program plan](superpowers/plans/2026-09-25-dashboard-v2-program.md) ·
[Touch feedback](superpowers/specs/2026-09-30-touch-feedback-design.md) ·
[First-run startup and product lifecycle](superpowers/specs/2026-09-12-first-run-startup-design.md) ·
[CLAUDE.md — Web Dashboard](../CLAUDE.md#web-dashboard-ice_edgeweb) (every URL and its permission) ·
[README — Using the Dashboard](../README.md#using-the-dashboard)

### Roles, access and remote exposure

Four roles — owner, secretary, tech, loader — each signing in with a 4–8 digit PIN. A new device needs a
second factor once (an emailed code, or an offline emergency code). First boot enters setup mode behind a
code shown only at the machine. Login uses exponential back-off rather than hard lockouts; POSTs require an
HTMX header as a CSRF guard; the MQTT broker is password-protected and bound to the LAN.

→ [Roles and access](superpowers/specs/2026-09-25-roles-and-access-design.md) ·
[Remote exposure hardening](superpowers/specs/2026-09-22-remote-exposure-hardening-design.md) ·
[README — Authentication and Access Control](../README.md#authentication-and-access-control) ·
[README — Credentials](../README.md#credentials)

### System tests and maintenance standby

A tech can prove each subsystem works without making a real sale: ping, self-test and actuator commands
(from a server-side allowlist), plus a full **test sale** that dispenses without recording revenue. Any test
takes a maintenance lease that withdraws payment machine-wide; "standby" makes a busy machine idle (refunding
any credit) and holds it out of service for the whole login session.

→ [System tests](superpowers/specs/2026-09-25-system-tests-design.md) ·
[Maintenance standby plan](superpowers/plans/2026-09-29-maintenance-standby-plan.md)

### Sales reports and alerting

Reports by period, product, payment method and cash collection, with CSV export and a scheduled
(daily/weekly) summary email. Faults raise alerts on MQTT and the dashboard; email/SMS gateways notify the
owner.

→ [Sales reports](superpowers/specs/2026-09-25-sales-reports-design.md) ·
[Dashboard activity panel](superpowers/specs/2026-04-15-dashboard-activity-design.md) ·
[ROADMAP §6 Remote commands](../ROADMAP.md#6-remote-commands)

### Customer screen

The customer-facing display (`/screen`) shows what the machine can sell and guides the purchase. A formal
edge ↔ screen contract is designed and planned.

→ [Customer screen contract](superpowers/specs/2026-10-14-customer-screen-contract-design.md) ·
[Implementation plan](superpowers/plans/2026-10-14-customer-screen-contract.md)

### Cloud fleet portal

When `cloud.enabled` is set, the edge pushes a periodic `MachineReport` and its UI bundle to `ice_cloud`,
which renders the machine's **own** dashboard templates read-only, with an Offline banner when reports stop.
It is a dev-only, no-auth stub bound to localhost until auth and mTLS exist.

→ [Cloud monitoring server](superpowers/specs/2026-10-13-cloud-monitoring-server-design.md) ·
[Edge-cloud contract](contracts/edge-cloud/CONTRACT.md) ·
[CLAUDE.md — Cloud](../CLAUDE.md#cloud-ice_cloud)

## Contracts between layers

Every message between layers is a versioned Pydantic model in `ice_contracts`, and the published JSON
schemas under `docs/contracts/` are generated from them (`uv run python -m ice_contracts.generate`) and
drift-tested in CI.

| Contract | Between |
|---|---|
| [Vending machine](contracts/vending-machine/CONTRACT.md) | Edge ↔ vending/MDB boards (dispense, refunds, faults, capabilities) |
| [Ice-maker monitor](contracts/ice-maker-monitor/CONTRACT.md) | Edge ↔ ice-maker monitor board |
| [Edge-cloud](contracts/edge-cloud/CONTRACT.md) | Edge ↔ fleet portal |

→ [Layer contracts spec](superpowers/specs/2026-10-13-layer-contracts-design.md) ·
[Ice-maker monitor contract design](superpowers/specs/2026-09-13-ice-maker-monitor-contract-design.md) ·
[firmware/README](../firmware/README.md)

## Simulators and Home Assistant

No physical boards exist yet. `ice_sims` simulates the vending, MDB and ice-maker boards on the real MQTT
topics and contracts — including autonomous behaviour and injectable faults — so the whole stack runs
without hardware. The ice-maker simulator announces itself to Home Assistant via MQTT discovery.

→ [ESP32 simulators](superpowers/specs/2026-04-12-esp32-simulators-design.md) ·
[Simulator fault injection](superpowers/specs/2026-04-19-simulator-fault-injection-design.md) ·
[Dual hot-gas valves](superpowers/specs/2026-09-13-dual-hot-gas-valves-design.md) ·
[Home Assistant MQTT discovery](superpowers/specs/2026-04-14-ha-mqtt-discovery-ice-maker-design.md) ·
[IM600XL vending simulator](superpowers/specs/2026-10-17-im600xl-simulator-design.md)

## Running and deploying

```
uv sync
uv run python -m ice_edge          # dashboard on http://localhost:26123
docker compose up --build          # VMC + simulators + mosquitto
```

CI (ruff, pytest, headless-Chrome browser tests) runs on every push; `main` publishes
`ghcr.io/conradstorz/ice-colder`, which Watchtower rolls out to the simulation host.

→ [README — Running](../README.md#running) ·
[CLAUDE.md — Docker](../CLAUDE.md#docker) ·
[CLAUDE.md — Configuration](../CLAUDE.md#configuration-packagescommonsrcice_commonconfig_modelpy-configjson)

## Developing and testing

| Task | Command |
|---|---|
| Tests | `uv run pytest` |
| Lint / format | `ruff check --fix .` then `ruff format .` |
| Import-layer rules | `uv run lint-imports` and `uv run python scripts/check_package_isolation.py` |
| Regenerate contract schemas | `uv run python -m ice_contracts.generate` |

The suite enforces its own discipline: no wall-clock waits, no reaching into private attributes (a budget
that only ratchets down), size ratchets on the FSM, and parity tests proving edge and cloud render identical
pages.

→ [CLAUDE.md](../CLAUDE.md) (the authoritative developer reference) ·
[Test-suite hygiene plan](superpowers/plans/2026-10-10-test-suite-hygiene.md) ·
[Routes split design](superpowers/specs/2026-10-12-routes-split-design.md)

## Status and roadmap

Built and tested: the edge controller (sale FSM, escrow, durable sales, crash recovery, faults and
availability, dispenser profiles, roles, dashboard, reports, system tests) and the simulated board layer.
Not yet built: real firmware and hardware, real payment gateways, CCTV, and a production cloud tier.

→ [ROADMAP](../ROADMAP.md) (phases, open questions, out of scope) ·
[Gap analysis](aspirational-gap-analysis.md) ·
[Daily workflow todo](superpowers/plans/2026-10-15-daily-workflow-todo.md)

## Document index

**Top level** — [README](../README.md) · [CLAUDE.md](../CLAUDE.md) · [ROADMAP](../ROADMAP.md) · [PLAN](../PLAN.md)

**Architecture** — [Reference architecture](vending-system-reference-architecture.md) ·
[Layer contracts](superpowers/specs/2026-10-13-layer-contracts-design.md) ·
[Gap analysis](aspirational-gap-analysis.md)

**Hardware** — [IM600XL iteration 1 I/O list](hardware/im600xl-io-list.md)

**Design specs** (`docs/superpowers/specs/`, each with a matching plan in
[`docs/superpowers/plans/`](superpowers/plans/))

| Date | Spec |
|---|---|
| 2026-04-12 | [ESP32 simulators](superpowers/specs/2026-04-12-esp32-simulators-design.md) |
| 2026-04-14 | [Home Assistant MQTT discovery](superpowers/specs/2026-04-14-ha-mqtt-discovery-ice-maker-design.md) |
| 2026-04-15 | [Dashboard activity panel](superpowers/specs/2026-04-15-dashboard-activity-design.md) |
| 2026-04-17 | [Dashboard redesign](superpowers/specs/2026-04-17-dashboard-redesign-design.md) |
| 2026-04-19 | [Simulator fault injection](superpowers/specs/2026-04-19-simulator-fault-injection-design.md) |
| 2026-09-12 | [First-run startup](superpowers/specs/2026-09-12-first-run-startup-design.md) |
| 2026-09-13 | [Dual hot-gas valves](superpowers/specs/2026-09-13-dual-hot-gas-valves-design.md) |
| 2026-09-13 | [Ice-maker monitor contract](superpowers/specs/2026-09-13-ice-maker-monitor-contract-design.md) |
| 2026-09-17 | [Fault registry and vend outcomes](superpowers/specs/2026-09-17-fault-registry-vend-outcomes-design.md) |
| 2026-09-18 | [Subsystem identity on Health](superpowers/specs/2026-09-18-subsystem-identity-health-tab-design.md) |
| 2026-09-21 | [Unattended operation](superpowers/specs/2026-09-21-unattended-operation-design.md) |
| 2026-09-22 | [Remote exposure hardening](superpowers/specs/2026-09-22-remote-exposure-hardening-design.md) |
| 2026-09-24 | [Soft faults non-blocking](superpowers/specs/2026-09-24-soft-faults-non-blocking-design.md) |
| 2026-09-25 | [Dashboard v2 shell](superpowers/specs/2026-09-25-dashboard-v2-shell-design.md) |
| 2026-09-25 | [Roles and access](superpowers/specs/2026-09-25-roles-and-access-design.md) |
| 2026-09-25 | [Sales reports](superpowers/specs/2026-09-25-sales-reports-design.md) |
| 2026-09-25 | [System tests](superpowers/specs/2026-09-25-system-tests-design.md) |
| 2026-09-30 | [Subsystem windows](superpowers/specs/2026-09-30-subsystem-windows-design.md) |
| 2026-09-30 | [Touch feedback](superpowers/specs/2026-09-30-touch-feedback-design.md) |
| 2026-10-06 | [Dispenser profiles](superpowers/specs/2026-10-06-dispenser-profiles-design.md) |
| 2026-10-09 | [VMC public surface](superpowers/specs/2026-10-09-vmc-public-surface-design.md) |
| 2026-10-10 | [VMC sale FSM core](superpowers/specs/2026-10-10-vmc-sale-fsm-core-design.md) |
| 2026-10-11 | [Routes flattening](superpowers/specs/2026-10-11-routes-flattening-design.md) |
| 2026-10-12 | Routes split [1](superpowers/specs/2026-10-12-routes-split-design.md) · [2](superpowers/specs/2026-10-12-routes-split-2-design.md) · [3](superpowers/specs/2026-10-12-routes-split-3-design.md) · [4](superpowers/specs/2026-10-12-routes-split-4-design.md) |
| 2026-10-12 | [VMC diet](superpowers/specs/2026-10-12-vmc-diet-design.md) |
| 2026-10-13 | [Cloud monitoring server](superpowers/specs/2026-10-13-cloud-monitoring-server-design.md) |
| 2026-10-13 | [Layer contracts](superpowers/specs/2026-10-13-layer-contracts-design.md) |
| 2026-10-14 | [Customer screen contract](superpowers/specs/2026-10-14-customer-screen-contract-design.md) |
| 2026-10-17 | [IM600XL simulator](superpowers/specs/2026-10-17-im600xl-simulator-design.md) |
