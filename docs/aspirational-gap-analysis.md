# Aspirational Gap Analysis — First Impression

> **Status:** Version 1.0 — first impression, 2026-10-13. Compares the current monolithic codebase (branch
> `test-suite-review`, after `beb6381`) against the two aspirational documents:
> [`vending-system-reference-architecture.md`](vending-system-reference-architecture.md) (the *architecture*)
> and [`superpowers/specs/2026-10-13-layer-contracts-design.md`](superpowers/specs/2026-10-13-layer-contracts-design.md)
> (the *contracts spec*). Built from `CLAUDE.md`, `ROADMAP.md`, `PLAN.md` and a source survey; it is a
> map for deeper review, not an audit. File references are to the current tree.

## 1. Summary

The project has built a deep, well-tested **edge controller** and a **simulated subsystem-board layer**.
For one machine, it already matches the architecture's edge tier closely: sale FSM, escrow, durable sale
recording, crash recovery, fault/availability engine, roles, local dashboard, reports. Three things are
largely absent:

1. the **cloud/fleet tier**;
2. **real hardware**: field devices, firmware, real payments;
3. **CCTV**.

The board↔edge contract exists and agrees with the spec in substance (capabilities, idempotent
`request_id`, profiles, outcomes, fault codes), but not in form: no common envelope, no expiry, no
version enforcement, no transport security.

| Area | Status |
|---|---|
| Sale FSM, escrow, vend outcomes, recovery | **Built, aligned** |
| Fault registry and availability gates | **Built, aligned (richer than spec)** |
| Roles, auth, local dashboard | **Built, partly aligned** |
| Reports (local, email) | **Built, partly aligned** |
| Board↔edge contract | **Built in substance; envelope/security/versioning gaps** |
| Subsystem boards | **Simulated only** (Python simulators, no firmware) |
| Field device layer (MDB, sensors, actuators) | **Not started** (reference stub only) |
| Payments (real cash/card/virtual) | **Simulated / stubbed** |
| Edge↔cloud contract and cloud tier | **Not started** |
| CCTV | **Not started** (old OpenCV module removed) |
| Device security, OTA, secure boot | **Not started** (Docker + Watchtower only) |

## 2. What has been done

- **Edge controller (single Python process).** `main.py` runs uvicorn, the MQTT client, the health
  monitor and the report scheduler under `services/task_supervisor.py` (restart on crash). Docker Compose
  plus Watchtower handle process restarts and image updates.
- **Sale FSM.** `controller/vmc.py`, a seven-trigger explicit FSM, composed by `controller/machine.py`
  from single-purpose collaborators: `EscrowLedger`, `RefundProtocol`, `FaultService`/`FaultRegistry`,
  `DispenseCycle`, `SessionRecovery`, `TelemetryRouter`, `MaintenanceLease`, `TestSaleRunner`. A ratchet
  test enforces the import boundary and private-access discipline.
- **Money.** The escrow FIFO ledger attributes each sale to payment methods. A failed vend restores the
  exact per-method shares. Refunds use a request/ack/one-retry protocol with the same `request_id`, and a
  failed refund raises `PAY-103`.
- **Durability.** Sales are written synchronously (SQLite WAL) before the FSM returns to idle, with a
  journal fallback. The fallback is replayed idempotently at boot. A corrupt database is renamed aside
  (`DATA-102`). The in-flight session snapshot raises `PAY-104` ("uncertain sale") at boot, and an
  operator resolves it with "Record as sale" or "Discard".
- **Faults and availability.** `DOMAIN-NNN` codes with severity and scope (`contracts/vending_machine.py`
  `FAULT_TABLE`). An availability truth table has safety, fulfillment and alert gates, and a seven-code
  payment-blocking set publishes `cmd/payment/enable`.
- **Subsystem contract.** Pydantic contract modules (`contracts/`) generate JSON Schemas
  (`docs/contracts/*/schemas`, with a drift test). A retained `capabilities/<subsystem>` document declares
  channels with `direction`/`driven_by`. Commands go through one `CommandDispatcher` with `request_id`
  correlation and a same-id retry. Heartbeats run every 10 s, with LWT `online`.
- **Dispenser profiles.** `dispensers.toml`, validated against the catalog and board capabilities. The
  profile is sent inside each `dispense` command. Mechanism-specific outcomes (bagged ice, water fill) map
  to faults and lockouts (`CFG-101`/`CFG-102`).
- **Simulators.** `simulators/` covers the vending board, ice-maker monitor and MDB gateway, with fault
  injection and an idempotency cache. They run end-to-end in Compose and in tests.
- **Access control.**
  - Four roles (`owner`, `secretary`, `tech`, `loader`) with permission-gated routes.
  - 4–8 digit PINs with a device-trust second factor (email OTP or offline emergency code).
  - First-boot setup mode, exponential back-off, and a CSRF guard (`HX-Request`).
- **Dashboard v2.** A touch-first tablet UI with offline-vendored assets: Health (subsystem windows, faults,
  availability, logs), Products, Inventory/restock, Reports, Controls, Tests (maintenance lease, standby,
  test sales, per-command tests), Users, and Settings.
- **Reports.** Period, product, method and collections reports with CSV export. A scheduled sales-summary
  email is de-duplicated per period.
- **Remote exposure.** The dashboard is published through Traefik (TLS) with hardening (trusted proxies,
  back-off).

## 3. Done the same way as the aspirational documents

| Aspirational element | Existing implementation |
|---|---|
| Explicit sale FSM, only component that moves money/starts a vend | `controller/vmc.py` + `Machine` composition root |
| Durable write before acknowledging a vend; in-flight snapshot flagged for human review, never guessed | Synchronous sale insert + journal fallback; `PAY-104` evidence file and recovery UI |
| Failed vend restores credit and locks out the slot | `vend_failed` restores FIFO shares; `FAULT_TABLE` product lockouts |
| Coded faults with severity, scope, auto-clear; truth table deriving what is allowed | `FaultRegistry`/`FaultService`; `services/availability.py` three gates |
| Hybrid board model: domain verbs + edge-pushed profiles + declared channels | `dispense` with an inline `SlotProfile`; capabilities `channels[]` with `direction`/`driven_by`; profiles validated against capabilities |
| Idempotent commands; retry reuses the same id; receiver replays cached ack (32) | `CommandDispatcher` same-id retry; `simulators/base.py` 32-entry ack cache |
| Heartbeat every 10 s, last-will offline | `HEARTBEAT_INTERVAL = 10.0`; LWT `online` |
| Retained capabilities document at boot | `capabilities/<subsystem>` retained |
| Simulators implement every board so the stack runs without hardware | `simulators/` + Compose + tests |
| Contract tests on message schemas | Pydantic → JSON Schema generation with a drift test |
| Role-based access, server-side checks, controls not rendered when not permitted | `require(Permission)` on every route; `perms` in templates |
| Offline-capable local logins | Emergency codes; PIN auth needs no network |
| Maintenance/service mode blocks sales | `SVC-102` lease in the payment-blocking set |
| Supervisor restarts crashed services | `task_supervisor.supervise`, Docker `restart: unless-stopped` |
| Sales/product/method/collections reports, CSV, scheduled email | `services/reports.py`, `report_scheduler.py` |

**Same goal, different method:**

- **Ledger.** A FIFO credit ledger plus a `sales` table, not a double-entry ledger.
- **Profile delivery.** The profile travels inline in every `dispense` command rather than once through
  `cmd.configure`.
- **Completion reporting.**
  - Current code: a late completion overwrites the cached ack; the dispatcher has no third state.
  - Spec: separates `ack` (accepted) from `evt.*.result` (completed).
- **Liveness.**
  - Current code: a subsystem is dead after 120 s of silence (or immediately via LWT).
  - Spec: 30 s.
- **Roles.**
  - Current code: `secretary` is a role, and `collect_cash` belongs to all four roles.
  - Spec: has `accountant/viewer` instead, and limits cash collection to owner and route.
- **Topics.**
  - Current code: flat `vmc/<machine_id>/<suffix>`.
  - Spec: hierarchical `m/<machine>/b/<board>/<class>/<name>`.

## 4. Not yet touched (needed for the aspirational system)

### 4.1 Field device and board layer

- **No firmware.** The repo has no ESP32 or other MCU firmware, only Python simulators (ROADMAP Phase D).
- **No real MDB.** `hardware/mdb_interface.py` is a reference stub. There is no real coin, bill or
  cashless device handling.
- **No board failsafe.** No simulator turns actuators off or disables payment when the VMC goes silent.
  The spec's "fail safe on silence" and hardware max-on-time timers exist only as ROADMAP Phase D
  requirements.
- **Electrical and interlock specs** for actuators and sensors (§2 of the contracts spec) are not
  documented anywhere.

### 4.2 Board ↔ edge contract form

- **No common envelope.**
  - Missing fields: `v`, `id`, `src`, `seq`, `corr`, `exp`.
  - Correlation is `request_id` on commands and acks only.
  - Events carry only `timestamp`.
- **No command expiry.** Only an ack deadline (10 s) and per-command completion timeouts exist.
- **No contract-version enforcement.**
  - Boards report `contract_version` and the VMC only logs it.
  - Nothing makes a board observe-only on a major-version mismatch.
- **No delivery guarantee on money and vend events.** Board→edge `credit` and vend results are not
  re-sent until acked.
- **Board ack caches are in-memory,** lost on board restart.

### 4.3 Payments

- **Virtual payments are display-only.**
  - Stripe, PayPal and Square are empty gateway classes, and the URLs are `example.com` placeholders.
  - There are no webhooks, no signed single-use credits, and no `payment_id` dedupe.
  - Credits have no expiry, and grants have no outcome events.
- **No certified card terminal (P2PE) integration.**
- **No reconciliation** against processor settlements. The current "reconcile" is the local journal
  replay; ROADMAP Phase H lists a reconciliation report.
- **No counted cash.** Cash collection records system-computed `expected_cash` only, with no counted amount
  and no variance.

### 4.4 Edge ↔ cloud and the cloud tier

- **None of the cloud tier exists:**
  - fleet backend, multi-machine or multi-tenant model
  - event outbox with `seq` watermark, store-and-forward, telemetry upload
  - desired/reported shadow
  - signed remote commands
  - mobile route app
  - the machine's mTLS identity

  Remote access today is the single machine's own dashboard through Traefik.
- **Alerting.**
  - Only email notifications are implemented; SMS and Snapchat are logged stubs, and there is no push.
  - No per-role alert routing and no escalation.

### 4.5 CCTV

- Nothing exists: no cameras, NVR, ONVIF/RTSP, event-tagged clips, retention or video permissions. The
  old OpenCV `camera_monitor` was removed as off-architecture.

### 4.6 Security and operations

- **MQTT is not secured.**
  - No TLS on the broker.
  - Dev allows anonymous access.
  - Prod uses one shared username/password for all services.
  - No per-device credentials, no ACLs.
- **Not started:** TPM or secure element, disk encryption, secure boot, signed OTA with A/B slots for the
  edge or the boards, DEX/UCS export. The edge updates by Watchtower pulling `latest`.
- **No complete audit log.**
  - `clear_fault` records a hardcoded `by="admin"` rather than the real user.
  - Settings and config changes are not user-attributed.
  - Cash collections do record the user.
  - ROADMAP Phase C lists a remote-command audit.
- **Customer-side UX** (accessibility, multilingual, e-receipts) is not addressed beyond the `/screen`
  page.

## 5. What the aspirational documents should learn from the existing work

The existing code has solved real problems the aspirational documents gloss over. Proposed amendments:

1. **Generate schemas from code.** The code-first contract (Pydantic → JSON Schema with a drift test)
   works and stays honest. The contracts spec should make *code-generated schemas* the normative form
   rather than hand-written AsyncAPI.
2. **Make version bumps a review gate.** The 0.8.0 → 1.0.0 history (a "minor" change that a review found
   wire-breaking) shows bumps need a checklist in review: param shape changed or topic removed means a
   major bump.
3. **Spec monitor-only boards.** The ice maker is a third-party appliance that is *monitored*, not
   commanded (`ice_maker_monitor` contract). The contracts spec assumes boards are commanded. Add a
   `monitor` board role with events and telemetry but no actuation, and define how its faults (bin full,
   compressor) feed availability.
4. **Make vend verification mechanism-specific.** Ice and water vending don't use drop beams. Water fill
   is verified by flow pulses; bagged ice has a door whose `door_open` is *a customer success plus a
   fault*. The spec's outcome list should be per mechanism with an explicit (outcome → customer result,
   fault) table, as `OUTCOME_FAULTS`/`fault_for_outcome` already do, including the fallback for an
   unmapped outcome.
5. **Specify the maintenance lease.** The lease and standby design is more precise than the spec's
   "service mode":
   - granted only when idle with empty escrow;
   - credit arriving during the lease is refunded;
   - the lease is never persisted;
   - an idle timer and a session-liveness sweep release it.

   Adopt it.
6. **Keep test sales out of the books.** Test sales must never become money:
   - `is_test` lives on the sale, not the lease;
   - it is carried into the crash snapshot;
   - it is checked at two recovery chokepoints.

   The spec should require that any persistence path for pending sales carries `is_test`.
7. **Exempt test sales from their own lease fault only.** A test sale ignores the lease's `SVC-102` and
   nothing else; every other safety fault still blocks it. State this rule explicitly.
8. **Gate payment by set membership, not severity.** Payment blocking is decided by membership in an
   explicit set, not by severity (`SVC-102` blocks without being critical). Data-integrity faults
   (`DATA-101`/`102`) must *never* block taking money. The spec's single "payment-blocking flag" should
   carry this rationale.
9. **Attribute money by method (FIFO).** Restoring the exact per-method shares on a failed vend matters
   for reconciliation; the spec's `shares` field should be defined this way, with the divergence fallback
   (`unknown`).
10. **Revisit the liveness timings.** The code chose 120 s to declare a board dead, the spec 30 s. Decide
    deliberately, and consider separate thresholds for "stop selling" (short) and "alert a human" (longer)
    to avoid alert noise on Wi-Fi blips.
11. **Settle where profiles live.** Inline per-command profiles make each dispense self-contained and
    remove the "configured?" state. Weigh this against the spec's `cmd.configure`, or allow both
    (configure caches, `dispense` may override).
12. **Learn from local auth.** Setup mode with a code shown only at the machine, device-trust second
    factor, offline emergency codes, back-off without hard lockouts, `HX-Request` as a CSRF guard, and a
    `secretary` role are all field-tested choices the architecture's role table should absorb.
13. **Require durable, de-duplicated reporting.** Per-period de-dupe, at most one catch-up, and a
    bounded-sleep loop that sees config changes within a minute are good requirements for the cloud
    report scheduler too.
14. **Keep the local dashboard internet-free.** Vendored assets mean the tablet works without internet;
    the architecture should state this for local service UIs.
15. **Lock structure down with ratchet tests.** Import-boundary, private-access and complexity ratchets
    keep the monolith decomposable; they are the practical path toward the spec's per-board conformance
    kits.

## 6. Suggested next questions

- Do the board↔edge envelope fields, expiry and version enforcement get retrofitted into the current
  `vmc/<id>/...` contract (one more major bump), or wait for real firmware?
- Is the cloud tier in scope for this repository, or a separate project consuming an edge outbox?
- Which comes first toward real deployment: MDB/firmware (ROADMAP Phase D) or MQTT transport security?
