# Vending Machine Software System — Reference Architecture

> **Status:** Aspirational document, version 1.1. Describes the target system independent of current implementation.
> v1.1 absorbs lessons from the existing codebase (see [`aspirational-gap-analysis.md`](aspirational-gap-analysis.md) §5).

Build it as three tiers: dumb hardware boards, an always-on edge controller in the machine, and a cloud fleet service. Each tier keeps working when the tier above it fails. Money and the audit trail are the system of record, and everything else serves them.

## 1. Physical topology

| Tier | Hardware | Responsibility |
|---|---|---|
| **Field devices** | MDB coin mech, bill validator, cashless reader, motors/augers, drop sensors, door switches, temp probes, power monitor, cameras | Sense and actuate only |
| **Subsystem boards** | Microcontrollers (ESP32/STM32) per domain: payment, dispense, environment | Real-time I/O, local safety interlocks, report over a bus |
| **Edge controller** | Linux SBC or industrial PC with UPS/supercap | Sale logic, ledger, fault model, local UI, local video, store-and-forward |
| **Cloud** | Fleet backend | Multi-machine dashboards, remote access, reporting, OTA, long-term storage |

The internal bus is MQTT on a private segment (or CAN/RS-485 for harsh wiring). Every message carries a schema version and a `request_id`. Commands are idempotent, so a retry never vends twice.

## 2. Edge controller components

1. **Hardware abstraction layer.** One driver per device class behind a stable interface (`dispense(slot)`, `accept_bills(bool)`). A simulator implements each interface, so the whole stack runs without hardware.
2. **Sale state machine.** Explicit states (idle → selecting → paying → dispensing → settling → idle, plus error and maintenance), with timeouts on every state. It is the only component allowed to move money or start a vend.
3. **Escrow ledger.** An append-only, double-entry ledger of every credit, vend, refund and cash collection, tagged with payment method.
   - **Durability:** write it durably (SQLite WAL with synchronous writes) before acknowledging a vend. If the write fails, fall back to a journal file and replay it idempotently at boot. A corrupt database is renamed aside and a fresh one created, with an alert. The machine keeps selling.
   - **FIFO attribution:** escrow is a first-in-first-out list of credits. A sale consumes credits in order and records its per-method *shares*. A failed vend restores exactly those shares with their original methods, so money is never reclassified. If the ledger and the escrow total ever disagree, the sale is booked to `unknown` with a warning (a bug guard, not a path).
   - **Uncertain sales:** snapshot the in-flight sale to disk so a power loss mid-vend is flagged for human review ("record as sale" or "discard"), never guessed. The evidence file is held untouched until an operator resolves it.
4. **Vend verification.** Verification is **mechanism-specific**: a drop beam for spiral/auger products, flow-meter pulses for water fill, a door sensor for bagged ice.
   - **Outcome table:** each mechanism has an explicit table mapping every outcome to a customer result (success or fail) and a fault. An outcome can be both: a bag delivered with the door left open is a customer success *and* a fault. An unmapped outcome falls back to that mechanism's generic error mapping rather than crashing.
   - **Failure:** a failed vend restores credit (or refunds) and locks out that product.
5. **Fault and availability engine.** A registry of coded faults with severity and scope (machine, product, payment). A truth table derives what is allowed, in three gates:
   - **safety** rows block payment and all sales;
   - **fulfillment** rows block only the affected sale;
   - **alert** rows block nothing.

   Faults auto-clear on recovery where it is safe to.
   - **Payment blocking is set membership, not severity:** an explicit list of codes may withdraw payment. A non-critical code (the maintenance lease) can be in it, and data-integrity faults (sale write failed, database reset) are deliberately never in it. Losing history must never stop the machine taking money.
   - **Two liveness thresholds:** a short one to stop selling what a silent board serves, and a longer one before alerting a human, so Wi-Fi blips do not page anyone.
   - **Monitor-only subsystems:** some equipment is watched, not commanded (a third-party ice maker: bin level, compressor, faults). Its faults feed availability like any other.
6. **Environment and physical monitoring.** Temperature, door and tamper switches, motor current, power quality, coin/bill box fill levels, inventory counts, water/ice levels where applicable. Food-safety temperature excursions block sales.
7. **Customer UI.** Touchscreen or button panel, accessible (ADA reach and contrast, audio), multilingual, with clear error and refund messages and an optional e-receipt QR.
8. **Local service UI.** A web dashboard on the machine's LAN or Wi-Fi AP, plus a physical service-mode switch or key. All assets are vendored so it works with no internet.
   - **Maintenance lease:** service work holds a lease that takes the machine out of service.
     - It is granted only when the machine is idle with empty escrow; an explicit "standby" request first refunds credit and cancels a live sale.
     - It withdraws payment machine-wide.
     - Credit arriving during the lease is refunded, never escrowed.
     - It is released by the holder, by an idle timer, or when the holder's login session dies.
     - It is never persisted, so a restart clears it.
   - **Test sales:** a test sale runs the real dispense path.
     - The test flag lives on the sale, not the lease, and is carried into the crash snapshot.
     - A test sale is never written as a sale and can never be recovered as one.
     - It is exempt only from its own lease's fault; every other safety fault still blocks it.
9. **Store-and-forward uplink.** Queue every event locally, sync when online, and never block a sale on the network.
10. **Supervisor.** Hardware and software watchdogs, crash-restarting services, signed OTA with A/B partitions and automatic rollback.

## 3. Payments

- **Cash and coin:** MDB level 3 coin changer and bill validator. Track escrowed versus stacked money and change-payout tubes.
- **Cards and NFC wallets:** use a certified, P2PE-validated unattended terminal (Nayax, Cantaloupe, or a Stripe/Adyen unattended reader) attached as an MDB cashless device or through its SDK. Your software never sees card numbers, which takes the controller out of most of PCI DSS scope. This is the single most important security decision.
- **Virtual payments:** QR or app payments (Stripe Payment Links, PayPal, Venmo, Cash App, loyalty credit). The cloud receives the webhook and pushes a signed, single-use credit to the machine. Credits expire and are deduplicated by payment ID.
- **One abstraction:** every method produces a `Credit(method, amount, external_id)` into escrow. Refunds go back through the originating method.
- **Reconciliation:** run a daily automated match of ledger, processor settlement reports and collected cash. Discrepancies raise alerts.

## 4. CCTV

- Use IP cameras (ONVIF/RTSP) for the customer face, the delivery bin, the service door interior and the surroundings. A local NVR (e.g., Frigate) on the edge box or a small companion box records continuously to a ring buffer.
- **Tag video with events:** vend, failed vend, door open, cash collection, tamper and refund events bookmark clips. Every ledger entry links to video, so disputes and cash audits become "click the row, watch the clip."
- Upload clips only for flagged events, on demand, or on a schedule, to save bandwidth. Encrypt video at rest and in transit, and set a retention policy (e.g., 30 days, longer for flagged incidents).
- Legal: post signage, check local recording laws, and avoid audio unless clearly permitted. Restrict who can view video by role.

## 5. Roles and access control

Use role-based access scoped to specific machines and locations: a route driver sees only their route. Every action is checked server-side, both at the edge and in the cloud.

| Capability | Owner | Technician | Route/service person | Secretary/accountant |
|---|---|---|---|---|
| Live status and alerts | all machines | assigned machines | assigned route | read-only |
| Restock, inventory counts | ✓ | ✓ | ✓ | — |
| Cash collection (with counted-amount entry) | ✓ | grantable | ✓ | grantable |
| Diagnostics, test vends, maintenance mode | ✓ | ✓ | limited (clear jams) | — |
| Prices, catalog | ✓ | — | — | — |
| Slot placement, stock counts | ✓ | ✓ | ✓ | — |
| Contacts, report schedule | ✓ | — | — | ✓ |
| Payment and secret configuration | ✓ | — | — | — |
| Financial reports | ✓ | — | own collections only | ✓ |
| Video | ✓ | service-door clips | own visits only | — |
| User management | ✓ | — | — | — |

"Grantable" means the owner may extend it per user. A small operator often lets everyone who opens the machine record a collection.

Authentication:

- **Cloud:** SSO or password plus TOTP/WebAuthn, with device trust.
- **On-machine:** a PIN of 4–8 digits with no repeats or runs, plus a physical service key or NFC badge. It works offline, using credentials cached and signed by the cloud.
  - **New device:** first login on a new device needs a second factor, either an emailed one-time code or an offline emergency code.
  - **First boot:** setup mode redirects everything to a setup page behind a code that exists only at the machine.
  - **Failed attempts:** exponential back-off per subject and client, with no hard lockout that could strand a tech in the field.
  - **Mutating requests:** require a same-origin request marker (CSRF guard).
- **Audit:** every privileged action goes to an append-only audit log *with the real acting user*, never a generic "admin", covering fault clears, config changes, refunds, collections and remote commands. Service visits are bracketed by door-open and door-close events with video.

## 6. Data security

- **Device identity:** a per-machine certificate in a TPM or secure element, with mTLS to the cloud. Run secure boot, encrypt the disk, and never use shared passwords.
- **Network:** keep the internal bus on an isolated VLAN or wired segment. The machine makes outbound-only connections to the cloud and has no inbound ports on the WAN. The local service UI is reachable only on the service network.
- **Secrets:** keep them in a vault or KMS, injected at runtime, never in config files or logs.
- **Data minimization:** store no card data and no customer PII beyond what receipts need. Run the cloud with least-privilege IAM, encrypted backups and log retention.
- **Updates:** sign firmware and app updates, use staged fleet rollouts, and keep a rollback path.
- **Threat model:** physical tampering, cash skimming by insiders (handled by reconciliation plus video), payment replay (handled by idempotent signed credits), and a compromised cloud (the machine validates signatures and enforces limits locally).

## 7. Reporting and remote monitoring

- **Telemetry:** heartbeats, sensors, faults and inventory flow to the cloud as time series.
- **Alert routing by role and channel** (push, SMS, email):
  - Technician: hardware faults
  - Route driver: low stock and full cash box
  - Owner: revenue anomalies, tamper events, reconciliation mismatches
  - Escalation rules apply if nobody acknowledges an alert.
- **Reports:**
  - Sales by period, product, slot and payment method
  - Cash accountability: expected versus collected per visit
  - Uptime and fault history, spoilage and temperature compliance logs
  - Restock forecasting (pre-kitting), commission statements for location hosts
  - Industry-standard DEX/UCS audit export
  - All available on screen, as CSV, and as scheduled email.
- **Scheduled sends are de-duplicated per period:**
  - A sent report is recorded, and any record for the period suppresses a resend.
  - At most one catch-up is sent after downtime, never a backlog.
  - The scheduler re-reads its config at least every minute, so turning it off takes effect promptly.
  - A failed send waits for the next occurrence rather than retrying in a tight loop.
- **Report time rules:** reports bucket by the machine's local timezone, weeks start Monday, and a sale exactly on a boundary belongs to the later bucket.
- **Local and remote use the same API:** the on-machine dashboard is a subset of the cloud portal, filtered by the same permission model.

## 8. Cloud/fleet backend

- MQTT broker (AWS IoT, EMQX), a device shadow for desired versus reported config, and an API service with Postgres and a time-series database.
- Object storage for video clips, a web portal plus a mobile app for route drivers (offline-capable picklists), and webhooks in from payment processors.
- Multi-tenant from day one if you'll ever manage machines for others.

## 9. Reliability and testing

- Design for offline operation, power loss mid-vend, a stuck coin, a jammed motor and a full bill box. Each has a coded fault and a defined safe behavior.
- **Simulators** for every board, so CI runs end-to-end sales.
- **Hardware-in-the-loop** bench rigs, plus fault-injection tests: kill power mid-dispense, drop MQTT, duplicate webhooks.
- **Contract tests** on every bus message schema. Schemas are generated from the code's own message models and committed; a drift test fails CI when they disagree.
- **Structure ratchets:** tests that enforce module import boundaries, forbid reaching into private internals, and cap the size of core classes. Budgets only move down. They keep a growing codebase decomposable into the tiers above.
- **No wall-clock waits in tests:** timers are injected and fired by label, so timeout paths are tested deterministically.

## 10. Build order

1. HAL, simulators and the bus contracts
2. Sale FSM, ledger and vend verification with cash/MDB
3. Fault/availability engine and the local service UI with roles
4. Cashless terminal and virtual payments with reconciliation
5. Cloud uplink, fleet dashboard, alert routing and reports
6. CCTV with event tagging
7. Security hardening, OTA, PCI/legal review, pilot machine, then fleet rollout
