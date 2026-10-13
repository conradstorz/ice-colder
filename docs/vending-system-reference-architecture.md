# Vending Machine Software System — Reference Architecture

> **Status:** Aspirational document, version 1.0. Describes the target system independent of current implementation.

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
3. **Escrow ledger.** An append-only, double-entry ledger of every credit, vend, refund and cash collection, tagged with payment method. Write it durably (SQLite WAL with synchronous writes) before acknowledging a vend. Snapshot the in-flight sale to disk so a power loss mid-vend is flagged for human review, never guessed.
4. **Vend verification.** Drop/optical sensors confirm product delivery. A failed vend auto-refunds or restores credit and locks out that slot.
5. **Fault and availability engine.** A registry of coded faults with severity and scope (machine, product, payment). A truth table derives what is allowed: payment accepted, product sellable, alert only. Faults auto-clear on recovery where it is safe to.
6. **Environment and physical monitoring.** Temperature, door and tamper switches, motor current, power quality, coin/bill box fill levels, inventory counts, water/ice levels where applicable. Food-safety temperature excursions block sales.
7. **Customer UI.** Touchscreen or button panel, accessible (ADA reach and contrast, audio), multilingual, with clear error and refund messages and an optional e-receipt QR.
8. **Local service UI.** A web dashboard on the machine's LAN or Wi-Fi AP, plus a physical service-mode switch or key.
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

| Capability | Owner | Technician | Route/service person | Accountant/viewer |
|---|---|---|---|---|
| Live status and alerts | all machines | assigned machines | assigned route | read-only |
| Restock, inventory counts | ✓ | ✓ | ✓ | — |
| Cash collection (with counted-amount entry) | ✓ | — | ✓ | — |
| Diagnostics, test vends, maintenance mode | ✓ | ✓ | limited (clear jams) | — |
| Prices, catalog, planogram | ✓ | — | — | — |
| Payment and secret configuration | ✓ | — | — | — |
| Financial reports | ✓ | — | own collections only | ✓ |
| Video | ✓ | service-door clips | own visits only | — |
| User management | ✓ | — | — | — |

Authentication:

- **Cloud:** SSO or password plus TOTP/WebAuthn, with device trust.
- **On-machine:** PIN plus a physical service key or NFC badge. This works offline, using credentials cached and signed by the cloud.
- **Audit:** every privileged action goes to an append-only audit log, and service visits are bracketed by door-open and door-close events with video.

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
- **Local and remote use the same API:** the on-machine dashboard is a subset of the cloud portal, filtered by the same permission model.

## 8. Cloud/fleet backend

- MQTT broker (AWS IoT, EMQX), a device shadow for desired versus reported config, and an API service with Postgres and a time-series database.
- Object storage for video clips, a web portal plus a mobile app for route drivers (offline-capable picklists), and webhooks in from payment processors.
- Multi-tenant from day one if you'll ever manage machines for others.

## 9. Reliability and testing

- Design for offline operation, power loss mid-vend, a stuck coin, a jammed motor and a full bill box. Each has a coded fault and a defined safe behavior.
- **Simulators** for every board, so CI runs end-to-end sales.
- **Hardware-in-the-loop** bench rigs, plus fault-injection tests: kill power mid-dispense, drop MQTT, duplicate webhooks.
- **Contract tests** on every bus message schema.

## 10. Build order

1. HAL, simulators and the bus contracts
2. Sale FSM, ledger and vend verification with cash/MDB
3. Fault/availability engine and the local service UI with roles
4. Cashless terminal and virtual payments with reconciliation
5. Cloud uplink, fleet dashboard, alert routing and reports
6. CCTV with event tagging
7. Security hardening, OTA, PCI/legal review, pilot machine, then fleet rollout
