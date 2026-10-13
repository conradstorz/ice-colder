# Layer Contracts — Design Spec

> **Status:** Aspirational document, version 1.1. Companion to
> [`docs/vending-system-reference-architecture.md`](../../vending-system-reference-architecture.md).
> Describes the target contracts independent of the current implementation. v1.1 absorbs lessons from
> the existing codebase (see [`docs/aspirational-gap-analysis.md`](../../aspirational-gap-analysis.md) §5).

## 0. Scope and decisions

This spec defines the contracts at the three boundaries of the reference architecture:

1. **Field device ↔ subsystem board** — standards and electrical obligations (§2).
2. **Subsystem board ↔ edge controller** — a JSON-over-MQTT message contract (§3).
3. **Edge controller ↔ cloud** — a JSON-over-MQTT plus HTTPS contract (§4).

Decisions recorded here:

- **Format:** prose rules plus a message catalog with JSON examples (§6).
  - **Normative form:** the message models in the edge codebase (Pydantic or equivalent). Each boundary's
    JSON Schemas, and AsyncAPI documents where useful, are *generated* from those models and committed.
  - **Drift test:** CI fails when the committed schemas differ from what the models generate.
  - **Why not hand-written schemas:** they drift from the code; generated ones cannot.
  - **Until the models exist,** this document is normative.
- **Board model: hybrid.** Boards expose *domain verbs* (`dispense`, `payment.enable`) whose behavior is
  parameterized by *profiles* pushed from the edge, and also declare their raw I/O *channels* in a
  capabilities document for diagnostics and validation. Local safety interlocks on the board always win
  over any command. Rejected alternatives: pure domain verbs (timing changes need reflashing) and raw
  remote I/O (real-time control over MQTT is fragile and unsafe when the edge dies mid-motion).

## 1. Common rules (board↔edge and edge↔cloud)

### 1.1 Envelope

Every message is a JSON object:

```json
{
  "v": "1.2",
  "id": "0f8c2a5e-6a1b-4d0e-9a57-3f1e2b7c9d10",
  "ts": "2026-10-13T14:02:11.123Z",
  "src": "vend-01",
  "seq": 4182,
  "type": "evt.vend.result",
  "corr": "5b1d9e3a-2c47-4f8e-b0a1-7d6c5e4f3a21",
  "exp": "2026-10-13T14:02:21.123Z",
  "body": {}
}
```

| Field | Required | Meaning |
|---|---|---|
| `v` | always | Contract version of the boundary this message belongs to (semver, major.minor) |
| `id` | always | UUID; unique per message; the dedupe key for commands |
| `ts` | always | Sender time, UTC ISO-8601 with milliseconds. A board with no real-time clock sends `uptime_ms` in `body` instead and the receiver stamps arrival time |
| `src` | always | Sender identity: board hardware id, or machine id |
| `seq` | always | Per-sender monotonic counter; defines ordering within one sender |
| `type` | always | Message type, prefixed by class (§1.2) |
| `corr` | acks and results | `id` of the command this answers |
| `exp` | commands | Absolute expiry; a command received after `exp` is not executed |
| `body` | always | Type-specific payload (may be `{}`) |

### 1.2 Message classes

| Prefix | Class | Semantics |
|---|---|---|
| `cmd.*` | Command | Request. Receiver sends exactly one `ack`. Long work reports completion later as an `evt.*.result` |
| `ack` | Acknowledgement | Answer to a `cmd` (and to delivery-guaranteed events, §1.5) |
| `evt.*` | Event | Something happened. Edge-triggered, never repeated except as a delivery retry with the same `id` |
| `state.*` | State | Current truth, last-value/retained. Level-triggered |
| `tel.*` | Telemetry | Periodic samples. Lossy; may be dropped or downsampled under load |

### 1.3 Versioning

- Each boundary has its own contract version, independent of the other boundary and of any software
  version.
- **Minor** bump: additive only — new optional fields, new message types, new enum members a receiver
  may treat as unknown. Receivers **must ignore unknown fields** and must handle unknown enum values by
  rejecting that one message, not by crashing.
- **Major** bump: anything wire-breaking (renamed/removed field, changed meaning, removed message).
- Each party publishes its contract version at connect. On a **major** mismatch the commanding side
  raises a fault and treats the peer as **observe-only**: it reads state and events but sends no
  operational commands.
- **Bump checklist (required in review).** Any one of these is a **major** bump, however small the diff
  looks:
  - a command's params changed shape;
  - a topic or message type was removed or renamed;
  - a field became required;
  - an enum member's meaning changed.

  An "additive" change that alters what an existing receiver must send or expect is not additive.

### 1.4 Commands, idempotency and expiry

- A command's `id` is its idempotency key. The receiver caches the acks for at least its last 32 command
  ids; a duplicate `id` gets the cached ack replayed and is **not re-executed**.
- A sender retrying a command **must reuse the same `id`**. Minting a new id for a retry is a contract
  violation, because it repeats a real-world side effect.
- A command received after `exp` gets `ack{status:"expired"}` and is not executed. Senders set `exp`
  tight: seconds for actuation, minutes for remote operator actions.

### 1.5 Acks

```json
{"type": "ack", "corr": "<cmd id>", "body": {"status": "ok", "reason": null, "detail": null}}
```

| `status` | Meaning |
|---|---|
| `ok` | Accepted. **Not** completed — completion, if any, arrives as `evt.*.result` |
| `rejected` | Refused by policy, interlock, state, or authorization; `reason` gives a code |
| `expired` | Arrived after `exp` |
| `busy` | Valid but the receiver cannot take it now (e.g. a dispense is already running) |
| `unsupported` | Unknown command or not declared in capabilities |
| `error` | Receiver failed while handling it |

Delivery-guaranteed events (money and vend outcomes, §3.5) are also acked by the receiver with
`status:"ok"`; the sender re-sends the same `id` until acked.

### 1.6 Liveness

Every party publishes `state.heartbeat` every **10 s** (`body: {uptime_s, contract, state}`). Where the
transport supports it, a last-will `state.online=false` is also registered and counts as immediate
silence. Silence is judged against **two thresholds**:

| Threshold | Default | Effect |
|---|---|---|
| **Stop** | 30 s (3 missed heartbeats) | Stop selling whatever the silent peer serves; a board fails safe (§2) |
| **Alert** | 120 s | Raise the coded fault (`COM-1xx`) and notify a human |

A peer that returns between the two thresholds resumes silently. A Wi-Fi blip must not page anyone.

## 2. Field device ↔ subsystem board

This boundary is not JSON. The contract states which standard or electrical spec each device class must
meet and what the board firmware must normalize it into before it reaches §3.

| Device class | Interface | Board obligation |
|---|---|---|
| Coin changer, bill validator, cashless reader | **MDB/ICP 4.3** (9-bit serial, 9600 baud). The board is the MDB master (VMC role) | Implement poll, reset, setup, enable/disable and escrow. Map MDB status bytes to contract fault codes. Card data never leaves the cashless device |
| Card terminal (non-MDB) | Vendor SDK over USB, serial or LAN | Same normalization as an MDB cashless device. Point-to-point encryption stays inside the terminal |
| Motors, augers, solenoids, valves | Switched DC through a driver with **current sensing**, **hardware overcurrent cutoff** and a **hardware max-on-time timer** | No output can stay energized beyond its hardware timer, regardless of firmware or edge commands |
| Drop / vend-confirm sensors | IR beam or piezo on a digital input, debounce spec (pulse ≥ 5 ms) | Latch the event inside the vend window. A missed drop is reported as a vend outcome, never as a raw edge |
| Doors, tamper, service key | Dry contacts, **normally closed** (a broken wire reads as open) | Report on every change and periodically |
| Temperature, level, flow, power | Digital sensors (1-Wire, I²C, pulse counters) or 4–20 mA analog | Report calibrated SI units. A sensor fault is reported distinctly from an out-of-range reading |
| Cameras | **ONVIF Profile S/T**, RTSP H.264/H.265 | Not on the board bus. Cameras are wired to the edge or NVR as peers |
| Audit | **DEX/UCS** port | The edge produces the DEX file from its ledger; boards are not involved |

Board-level invariants:

- **Fail safe on silence.** If the edge is silent for more than **30 s**, all actuators turn off, payment
  devices are disabled, and sensors keep reporting.
- **Interlocks first.** A local interlock trip (overcurrent, door open during motion, over-temperature)
  stops the actuator **before** the board reports it.
- **Electrical boundary.** I/O is protected and isolated. Mains loads switch through rated relays or
  contactors, never directly from MCU pins.

## 3. Subsystem board ↔ edge controller

### 3.1 Transport

- MQTT 3.1.1 or 5 on the edge's own local broker, on an isolated VLAN or wired segment.
- Authentication: per-board TLS client certificate where the MCU can afford it; otherwise a per-board
  username and password. Either way, broker ACLs confine each board to publishing under its own subtree
  and subscribing only to its own `cmd` topics.
- Topic layout: `m/<machine>/b/<board>/<class>/<name>`, for example
  `m/m-0042/b/vend-01/cmd/dispense`, `m/m-0042/b/pay-01/evt/credit`,
  `m/m-0042/b/env-01/state/capabilities`. Acks are published to `m/<machine>/b/<board>/ack`
  (board → edge) and `m/<machine>/edge/ack/<board>` (edge → board).

### 3.2 Lifecycle

1. **Announce.** On boot the board publishes a retained `state.capabilities` (§6.1) and registers a
   last-will `state.online=false`.
2. **Validate.** The edge checks the capabilities against its configuration: expected role, contract
   major version, and every channel the profiles reference. On mismatch the board is observe-only and the
   edge raises a fault.
3. **Configure.** The edge sends `cmd.configure` with the board's profiles, which the board caches.
   Until it acks `ok`, the board rejects every operational command with `reason:"unconfigured"`, except a
   `cmd.dispense` that carries its own inline profile (§3.3). The inline profile makes each dispense
   self-contained, so the edge can always vend with the exact profile it validated.
4. **Operate.** Commands, events, state and telemetry per §3.3–§3.5.
5. **Re-announce** on reconnect, reboot, or firmware change. The edge re-validates and re-configures.

### 3.3 Commands (edge → board)

| Group | Command | Body | Completion |
|---|---|---|---|
| Standard (every board) | `cmd.ping` | `{}` | ack only |
| | `cmd.self_test` | `{}` | `evt.self_test.result` |
| | `cmd.force_report` | `{}` | republishes all `state.*` |
| | `cmd.configure` | `{profiles_version, profiles}` | ack only |
| | `cmd.reboot` | `{}` | re-announce |
| | `cmd.ota.begin` | `{url, sha256, signature, version}` | `evt.ota.result` |
| Payment | `cmd.payment.enable` | `{accept: bool, methods?: [...]}` | ack only |
| | `cmd.payment.payout` | `{amount, sale_id}` | `evt.payout.result` |
| | `cmd.payment.refund` | `{credit_id, amount}` | `evt.refund.result` |
| | `cmd.cashless.approve` / `cmd.cashless.deny` | `{session, amount}` | `evt.cashless.result` |
| Dispense | `cmd.dispense` | `{sale_id, slot, profile_ref}` or `{sale_id, slot, profile}` — an inline `profile` wins over the cached one | `evt.vend.result` (exactly one) |
| | `cmd.abort` | `{sale_id}` | `evt.vend.result{outcome:"aborted"}` |
| Environment | `cmd.setpoint` | `{channel, value}` | ack only |
| | `cmd.output` | `{channel, on, max_s}` — service and test use only | ack only |

A board answers any command not in its declared capabilities with `unsupported`.

### 3.4 Events, state and telemetry (board → edge)

| Type | Body | Notes |
|---|---|---|
| `evt.credit` | `{credit_id, method, amount, external_id?}` | Cash or coin inserted, or cashless session approved. **Delivery-guaranteed** |
| `evt.vend.result` | `{sale_id, mechanism, outcome, steps[]}` | `outcome` is drawn from the mechanism's outcome table (§5.1). **Delivery-guaranteed** |
| `evt.payout.result`, `evt.refund.result`, `evt.cashless.result` | `{…, status, amount}` | **Delivery-guaranteed** |
| `evt.fault` | `{code, action: raised \| cleared, detail}` | The board's own interlock and device faults |
| `evt.input` | `{channel, value}` | Change of a declared input |
| `state.<channel>` | `{value, since}` | Retained current value of doors, levels, setpoints |
| `state.heartbeat` | `{uptime_s, contract, state}` | Every 10 s |
| `tel.<channel>` | `{samples: [[t, value], …]}` | At the configured interval |

### 3.5 Rules

- **The edge decides money.** A board reports credit; only the edge accepts, vends, or refunds. A board
  never vends against its own credit.
- **One dispense at a time.** A second `cmd.dispense` while one is running gets `busy`.
- **Delivery guarantee.** `evt.credit`, `evt.vend.result` and the payment `*.result` events are re-sent
  with the same `id` until the edge acks them; the edge dedupes by `id`.
- **Monitor-only boards.** A board with `role: "monitor"` watches equipment the machine does not control,
  such as a third-party ice maker (bin level, compressor, harvest cycles, its own fault lights).
  - It declares only `direction: "in"` channels, plus outputs it *observes* (e.g. `compressor_run`) marked
    `driven_by: null`.
  - It accepts only the standard commands.
  - Its `evt.fault` codes feed availability exactly like any other board's (for example, bin-empty locks
    ice products; bin-half-full clears that lockout).
- **Channel ids are unique within a board.** The edge attributes a reading to a board by `(id, kind)`, so
  two boards declaring the same id under different kinds can never steal each other's readings.
- **Board firmware comes only from the edge** via `cmd.ota.begin`. The board verifies the signature and
  SHA-256, writes the inactive A/B slot, boots it, and rolls back unless the edge confirms health within
  **5 minutes**.

## 4. Edge controller ↔ cloud

### 4.1 Transport

- MQTT 5 over TLS 1.3 with mutual TLS. The machine's client certificate and key live in a TPM or secure
  element. Connections are **outbound only**; nothing on the machine listens on the WAN.
- HTTPS with the same mTLS identity carries bulk payloads — video clips, DEX files, log bundles, OTA
  images. MQTT messages carry only a pointer (URL, size, SHA-256).
- Topic layout: `fleet/<tenant>/<machine>/<channel>/<name>`.

### 4.2 Channels

| Channel | Direction | Semantics |
|---|---|---|
| **Event stream** `evt.*` | edge → cloud | Every ledger, sale, refund, fault, door, collection, service-visit and audit event, each with the machine's monotonic `seq`. Kept in a durable outbox until the cloud acks a watermark. |
| **Telemetry** `tel.*` | edge → cloud | Downsampled sensor series, batched every 60 s. Lossy. |
| **Shadow** | both | `desired` (cloud-written) and `reported` (edge-written) documents. Versioned sections: `catalog`, `prices`, `planogram`, `profiles`, `alert_rules`, `users`, `schedules`, `firmware_target`. |
| **Commands** `cmd.*` | cloud → edge | Remote operator actions (§4.4). |
| **Payment credits** `credit.grant` | cloud → edge | Virtual payments approved by a processor webhook (§4.5). |

### 4.3 Event stream delivery

- The edge publishes events in `seq` order, in batches of up to 100.
- The cloud acknowledges with `ack{through_seq}`; the edge may then trim its outbox up to that point.
- The cloud dedupes on `(machine, seq)`. On a gap it sends `cmd.resend{from_seq}`.
- The outbox holds at least **90 days** of events; on overflow the edge raises a fault and keeps the most
  recent money events in preference to non-money events.

### 4.4 Shadow and commands

- **Shadow apply** is all-or-nothing per section: the edge validates the whole section, applies it, and
  writes `reported.<section> = {version, applied_at}`, or reports `{version, rejected: [reasons]}` and
  keeps the previous version. Half-applied sections are forbidden.
- **Commands:** `cmd.service_mode`, `cmd.clear_fault`, `cmd.test_vend`, `cmd.refund`,
  `cmd.collect_clip{camera, from, to}`, `cmd.upload_logs`, `cmd.reboot`, `cmd.resend`.
- **Authorization on both sides.** Every command carries `body.actor = {user_id, role, scope}` and a
  cloud signature over the envelope. The cloud checks the user's permission and machine scope; the edge
  re-checks against its cached, cloud-signed role table (the `users` shadow section) and rejects with
  `reason:"forbidden"` if they disagree. `cmd.refund` and `cmd.test_vend` carry an amount or count that
  the edge caps independently.

### 4.5 Virtual payment credits

```json
{"type": "credit.grant", "exp": "2026-10-13T14:07:11Z",
 "body": {"payment_id": "pi_3Q…", "provider": "stripe", "amount": 3.00, "currency": "USD",
          "machine": "m-0042", "sig": "<cloud payment-key signature>"}}
```

- Single use, deduped by `payment_id`; the signature covers `payment_id`, `amount`, `currency`,
  `machine` and `exp`.
- Every grant gets exactly one outcome event: `evt.credit.outcome{payment_id, outcome}` with
  `outcome ∈ vended, refunded, expired, rejected`. The cloud refunds through the processor any grant
  whose outcome is not `vended`, or that has no outcome after `exp` plus a grace period.

### 4.6 Rules

- **Offline-first.** The edge never waits on the cloud to sell cash or card. Cached signed credentials
  keep local logins working for **30 days** offline. Virtual payments are unavailable while offline, and a
  grant arriving after `exp` is rejected and reported.
- **Cash accountability.** A collection event carries both `expected` (from the ledger) and `counted`
  (entered by the collector), plus the collector's identity and the door-open/door-close event ids.
- **Data classes.**
  - Never leave the machine: card data (the edge never holds any), continuous raw video, secrets.
  - Leave on request or flagged event: video clips, log bundles.
  - Always synced: events, telemetry, reported shadow state.
- **Edge updates.** Signed A/B images, applied only when the sale FSM is `idle` with zero escrow. Rollout is
  staged by `firmware_target` in the shadow. The edge reports health after boot; the cloud halts the
  rollout on a failed health report.

## 5. Shared registries, failure behavior, conformance

### 5.1 Shared registries

Versioned alongside the contracts, owned by the edge codebase, and published to both other layers.

- **Fault codes** `DOMAIN-NNN` (e.g. `PAY-102`, `ENV-201`), each with severity
  (`critical | product_unavailable | warning | info`), scope (`machine | product | payment`), gate
  (`safety | fulfillment | alert`), and auto-clear rule.
- **Payment-blocking set:** an explicit list of codes that withdraw payment machine-wide.
  - Membership, not severity, decides. A non-critical operator-held code such as the maintenance lease
    belongs in it.
  - Data-integrity codes (sale write failed, event database reset) must **never** be members.
- **Vend outcome tables, one per mechanism.** Each row is `(mechanism, outcome) → customer result, fault`:

  | Mechanism | Outcome | Customer result | Fault |
  |---|---|---|---|
  | `auger` | `delivered` | success | — |
  | `auger` | `no_drop`, `jam` | fail (credit restored) | product lockout |
  | `bagged_ice` | `delivered` | success | — |
  | `bagged_ice` | `door_open` | **success** | door fault |
  | `water_fill` | `delivered` | success | — |
  | `water_fill` | `low_flow`, `no_flow` | fail (credit restored) | product or water fault |
  | any | `aborted`, `error` | fail (credit restored) | product lockout |

  An outcome a board reports that has no row for its mechanism maps to that mechanism's `error` row,
  never an exception.
- **Payment method strings:** `cash`, `coin`, `card`, `nfc`, `qr:<provider>`, `test`.
- **Sale shares:** every sale event carries `shares: {method: amount}`.
  - Shares come from consuming escrow credits first-in-first-out.
  - A failed vend restores exactly those shares with their original methods.
  - A ledger/escrow disagreement books `{"unknown": price}` with a warning.
- **Test flag:** any message or persisted record describing a pending or completed sale carries `is_test`.
  A test sale never produces a sale record, an `evt.sale`, or a recoverable uncertain-sale card.
- **Channel kinds and units.**

### 5.2 Failure behavior

| Failure | Board | Edge | Cloud |
|---|---|---|---|
| Board silent > stop threshold (30 s) | Fails safe: actuators off, payment disabled | Blocks affected sales; refunds open credit | — |
| Board silent > alert threshold (120 s) | — | Raises `COM-1xx` | Alerts the technician |
| Edge crash mid-vend | Finishes or aborts its own cycle; re-sends the result until acked | On boot, holds the in-flight snapshot as an *uncertain sale* fault; never guesses | Shows a recovery card to the owner |
| Cloud unreachable | — | Keeps selling cash and card; virtual payments unavailable; outbox grows | Marks the machine stale after 3 missed heartbeats |
| Contract major mismatch | Observe-only | Raises a fault; sends no operational commands | Blocks the rollout |
| Duplicate or replayed message | Replays the cached ack | Dedupes by `id` / `payment_id` | Dedupes by `(machine, seq)` |

### 5.3 Conformance

- **Contract test kits per boundary:** a board simulator, an edge stub, and a cloud stub. Each side is
  tested against the other side's stub, never against the real implementation.
- **Required scenarios:** duplicate command; expired command; lost ack with retry on the same `id`;
  power loss mid-dispense; edge silence causing board failsafe; major-version mismatch; outbox replay
  with a gap; expired payment credit; unauthorized remote command; a vend outcome that is a customer
  success plus a fault; an outcome unmapped for its mechanism; a test sale crashing mid-dispense (must
  never become a recoverable sale); a peer returning between the stop and alert thresholds.
- **Certification:** a board firmware or cloud release ships only if it passes the kit for the contract
  version it claims.

## 6. Message catalog — examples

### 6.1 `state.capabilities` (board → edge, retained)

```json
{"v": "1.0", "id": "…", "ts": "…", "src": "vend-01", "seq": 1, "type": "state.capabilities",
 "body": {
   "hardware_id": "esp32-a4cf12…", "role": "dispense", "firmware": "2.3.1", "contract": "1.0",
   "commands": ["ping", "self_test", "force_report", "configure", "reboot", "ota.begin",
                "dispense", "abort"],
   "channels": [
     {"id": "auger_1", "kind": "digital", "direction": "out", "driven_by": "dispense"},
     {"id": "auger_1_current", "kind": "analog", "direction": "in", "unit": "A", "range": [0, 5]},
     {"id": "drop_beam", "kind": "digital", "direction": "in"},
     {"id": "bin_door", "kind": "digital", "direction": "in"}
   ]}}
```

### 6.2 `cmd.configure` (edge → board)

```json
{"type": "cmd.configure", "exp": "…",
 "body": {"profiles_version": 7, "profiles": {
   "slot.1": {"mechanism": "auger", "drive": "auger_1", "sense": "drop_beam",
              "run_ms": 1800, "drop_window_ms": 2500, "max_current_a": 3.2}}}}
```

### 6.3 `cmd.dispense` → `ack` → `evt.vend.result`

```json
{"id": "5b1d…", "type": "cmd.dispense", "exp": "2026-10-13T14:02:21Z",
 "body": {"sale_id": "s-20261013-0193", "slot": 1, "profile_ref": "slot.1@7"}}

{"type": "ack", "corr": "5b1d…", "body": {"status": "ok"}}

{"id": "0f8c…", "type": "evt.vend.result", "corr": "5b1d…",
 "body": {"sale_id": "s-20261013-0193", "mechanism": "auger", "outcome": "delivered",
          "steps": [{"step": "run", "ms": 1790}, {"step": "drop", "ms": 412}]}}
```

### 6.4 `evt.credit` (board → edge, delivery-guaranteed)

```json
{"type": "evt.credit", "src": "pay-01",
 "body": {"credit_id": "c-8812", "method": "cash", "amount": 5.00}}
```

### 6.5 `evt.fault` (board → edge)

```json
{"type": "evt.fault", "src": "vend-01",
 "body": {"code": "DSP-201", "action": "raised", "detail": "auger_1 overcurrent 4.1 A"}}
```

### 6.6 Event stream batch and watermark (edge ↔ cloud)

```json
{"type": "evt.batch", "src": "m-0042",
 "body": {"events": [
   {"seq": 90211, "type": "evt.sale", "ts": "…",
    "body": {"sale_id": "s-20261013-0193", "sku": "ICE-10", "price": 3.00,
             "shares": {"cash": 3.00}}},
   {"seq": 90212, "type": "evt.door", "ts": "…", "body": {"door": "service", "open": true}}]}}

{"type": "ack", "body": {"through_seq": 90212}}
```

### 6.7 Shadow section (cloud desired, edge reported)

```json
{"desired":  {"prices": {"version": 14, "items": {"ICE-10": 3.00, "ICE-20": 5.00}}},
 "reported": {"prices": {"version": 14, "applied_at": "2026-10-13T14:05:00Z"}}}
```

### 6.8 Remote command with actor (cloud → edge)

```json
{"type": "cmd.refund", "exp": "…",
 "body": {"actor": {"user_id": "u-17", "role": "owner", "scope": ["m-0042"]},
          "sale_id": "s-20261013-0193", "amount": 3.00, "reason": "customer_report",
          "sig": "<cloud signature>"}}
```
