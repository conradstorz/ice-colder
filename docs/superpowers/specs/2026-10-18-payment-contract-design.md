# Payment contract (MDB board ↔ edge) — design

**Date:** 2026-10-18
**Status:** approved design, awaiting implementation plan
**Scope:** spec 1 of 4 for the end-to-end MDB ↔ edge work. This document defines only the
MQTT contract between the MDB payment board and the edge: `ice_contracts.board.payment`.
The three later specs build against it:

| Spec | Builds |
|---|---|
| 2 — MDB gateway simulator | `ice_sims.mdb_gateway` rewritten to this contract (tubes, stacker, escrow, cashless sessions, flash-replay) |
| 3 — Edge consumption | `ice_edge` moves `VMC`/`EscrowLedger`/`RefundProtocol`/`Availability`/faults onto this contract; cashless becomes a sale-FSM step |
| 4 — ESP32 firmware | MDB/ICP 4.3 master: level 3 changer, level 1/2 bill validator, level 1 cashless reader; NVS event log |

Related: `docs/superpowers/specs/2026-10-13-layer-contracts-design.md` §2–§3 (the aspirational
boundary this contract makes concrete), `docs/hardware/im600xl-io-list.md` (the target machine has a
bill validator, a coin changer and a Nayax cashless reader; the board replaces the PLC as MDB master).

## 1. Problem

Today's payment interface is a thin sketch: `payment/credit` carries a float dollar amount and a
free-text method with no id, `payment/status` is free text, `cmd/payment/enable` is a bare bool with no
request_id and no ack, a card tap is modelled as an unsolicited credit of an arbitrary amount, and a
refund is the only money-out path. It cannot express bill escrow, coin tube levels, exact-change,
cashless sessions, duplicate-safe delivery, or which of the three devices has failed. It is also not in
the normative contract docs at all (`payment/credit` and `payment/status` have no generated schema and are
missing from `CONTRACT.md`).

The target hardware is three real MDB peripherals. The interface has to be redesigned, not patched.

## 2. Decisions

1. **Hybrid policy split.** The edge pushes a *policy* (what to accept, how much, exact-change rule,
   enable). The board applies it on the bus in real time (stack vs return a bill, poll, tube math). The
   edge owns sale-level calls (cashless vend request, payout amount, refunds). No per-bill round trip; a
   slow edge cannot strand a bill in escrow.
2. **Delivery-guaranteed money events.** Every board→edge event has an `event_id` and a per-boot `seq`.
   The board persists unacknowledged events to flash and replays them on reconnect or reboot. The edge
   dedupes on `event_id` and acks cumulatively.
3. **Explicit cashless sessions.** Tap → session; edge issues a vend request; reader approves or denies;
   edge reports vend success or failure; board closes the session. The charge happens only on success,
   so a failed cashless vend never needs a refund.
4. **One event stream + one retained state doc.** All money facts flow on `payment/event`; the
   slow-changing truth (devices, tubes, escrow, session) is a retained `payment/state`. Every edge→board
   instruction, including enable, rides the existing `cmd/mdb` request_id/ack channel.
5. **No floats for money.** Every amount is an integer number of cents, typed `StrictInt`, so `1.0` or
   `"100"` fails validation rather than coercing. Currency is declared once in `payment/state`.

## 3. Module, versioning, topics

- New module `packages/contracts/src/ice_contracts/board/payment.py` with its own
  `CONTRACT_VERSION = "1.0.0"`. The board publishes it in `capabilities/mdb.contract_version`.
- `ice_contracts.board.vending_machine` bumps **2.0.0 → 3.0.0**: `PaymentEvent`, `PaymentStatus`,
  `PaymentEnableCommand`, `PaymentRefundCommand`, `PaymentRefundResult` and `RefundStatus` are removed,
  the `payment/credit`, `payment/status`, `cmd/payment/enable`, `cmd/payment/refund` and
  `cmd/payment/refund/ack` topics are removed, and `TESTABLE_COMMANDS["mdb"]` changes (§6).
- `ice_contracts.generate` emits `docs/contracts/payment/CONTRACT.md` and
  `docs/contracts/payment/schemas/*.json` exactly as it does for the other two contracts; a drift test
  byte-checks them.

Topics, all under `vmc/{machine_id}/`:

| Topic | Direction | QoS / retain | Payload |
|---|---|---|---|
| `payment/event` | board → edge | 1 / no | `PaymentEventEnvelope` (§4) |
| `payment/event/ack` | edge → board | 1 / no | `PaymentEventAck{boot_id, seq}` — cumulative: "I have processed every event with this `boot_id` and `seq` ≤ this" |
| `payment/state` | board → edge | 1 / **retained** | `PaymentState` (§5) |
| `cmd/mdb`, `cmd/mdb/ack` | edge → board, board → edge | existing | `SubsystemCommand` / `CommandAck` (§6) |
| `heartbeat/mdb`, `capabilities/mdb` | board → edge | unchanged | `SubsystemHeartbeat`, `SubsystemCapabilities` (device detail lives in `payment/state`, not in `channels`) |

**Deviations recorded during implementation** (rather than silently drifting from the text above):

- `CONTRACT.md` is hand-written and `schemas/` generated, matching the two existing contracts;
  `generate.py` does not emit Markdown. The bullet above that says it emits `CONTRACT.md` is superseded.
- The 3.0.0 removal of the legacy vending-contract payment models lands with spec 3 (edge migration),
  when their last importer disappears; this plan ships the additive 2.1.0 instead so `ice_edge` and
  `ice_sims` keep working unchanged in between. `TESTABLE_COMMANDS["mdb"]` likewise flips in spec 2 with
  the simulator that advertises the new commands (`payment.MDB_TESTABLE_COMMANDS` is defined now).
- `VendRequestParams.session_id` and `PayoutParams.sale_id` default to `None`, so omitting the key is
  equivalent to sending `null` (price-first `vend_request`; payout with no sale attached).

### 3.1 Delivery

- `boot_id`: random 8–32 char id minted at board boot. `seq`: `StrictInt ≥ 1`, monotonic within a
  `boot_id`, starting at 1.
- The board appends every event to a flash-backed log before publishing it and removes it only when an
  ack with `seq ≥` its own arrives. On reconnect or reboot it republishes the whole unacked log in order
  before anything new. An ack covers only log entries with the SAME `boot_id`; entries from an earlier
  boot stay in the log until acked under their own `boot_id` (the board replays them first, in order).
- The edge dedupes on `event_id`, processes in `seq` order, and acks after processing (not on receipt).
  It never blocks a sale waiting for its own ack to be delivered. A duplicate `event_id` is not
  reprocessed but is acked again; the board's replay and lost-ack recovery depend on it (clarified by
  firmware spec 4).
- A `seq` gap within a known `boot_id`, or a never-seen `boot_id` whose first event has `seq > 1`, raises
  `PAY-105 payment event gap` (machine scope, `warning`, not payment-blocking). The edge still processes
  what it has; the fault tells the operator the cash count may be off.
- An unknown `event.kind` (a newer board) is logged, acked so the stream is not blocked, and counted in
  `PAY-105`'s detail.

### 3.2 Fail-safe on edge silence

The policy carries `edge_timeout_s` (default 30). The edge sends `ping` on `cmd/mdb` at least every 10 s
while idle. If the board sees no `cmd/mdb` traffic for `edge_timeout_s` it: disables all acceptance,
returns any bill in escrow (`bill_returned{reason: edge_silent}`), ends any open cashless session
(`session_ended{reason: edge}`), and sets `inhibited_reason: edge_silent`. Acceptance resumes only on a
fresh acked `set_policy`.

## 4. Events (`payment/event`)

```
PaymentEventEnvelope
  event_id: str (8–64)        # unique per event, board-minted
  boot_id:  str (8–32)
  seq:      StrictInt ≥ 1
  ts:       datetime
  event:    PaymentEventBody   # discriminated union on `kind`
```

Common types: `Cents = StrictInt ≥ 0`; `Device = coin | bill | cashless`;
`DeviceStatus = ready | disabled | busy | jammed | full | empty | tilt | offline | error`.

| `kind` | Fields | Meaning | Credit? |
|---|---|---|---|
| `coin_accepted` | `cents`, `routed: tubes \| cashbox` | A coin dropped and is in the machine | **yes** |
| `bill_escrowed` | `cents` | The validator holds a bill pending the policy verdict | no |
| `bill_stacked` | `cents` | The bill went into the stacker | **yes** |
| `bill_returned` | `cents`, `reason: policy \| edge_silent \| customer \| edge \| timeout` | The escrowed bill was given back | no |
| `escrow_return_pressed` | — | Customer pressed the coin-return lever | no; edge decides |
| `payout_result` | `request_id`, `requested_cents`, `paid_cents`, `status: ok \| partial \| failed`, `detail` | Terminal result of a `payout` command | no |
| `session_begun` | `session_id`, `funds_cents \| null` | Cashless tap; `null` = reader does not disclose funds | no |
| `vend_approved` | `session_id`, `request_id`, `cents` | Reader approved the edge's `vend_request` | provisional |
| `vend_denied` | `session_id`, `request_id`, `reason` | Reader denied it | no |
| `session_ended` | `session_id`, `reason: complete \| cancelled \| reader \| edge \| timeout` | Session closed | no |
| `device_status` | `device`, `status: DeviceStatus`, `mdb_status: StrictInt \| null` | The board's mapping of an MDB status byte; raw byte kept for diagnostics | no |
| `cash_collected` | `device` | Cashbox removed or tubes emptied by a tech | no |

Credit arithmetic on the edge (binding for spec 3): only `coin_accepted` and `bill_stacked` create an
escrow `Credit`. `vend_approved` creates a *provisional* credit that `vend_result{success}` finalises and
`vend_result{failure}` discards. `bill_escrowed` may be shown to the customer but is never spendable, so
a bill the policy then returns needs no refund. `partial` is explicit and is never folded into `ok`.

`revalue_result` is deliberately absent from 1.0 (§9).

## 5. State (`payment/state`, retained)

Republished on every change, on `force_report`, and every 30 s. Heartbeats remain the liveness source;
a retained state doc never means the board is alive.

```
PaymentState
  boot_id, seq_last                 # reboot detection and replay horizon
  currency: "USD"
  policy_id: str | null             # policy in force (§6 set_policy)
  accepting: bool
  inhibited_reason: null | no_policy | edge_silent | policy | device_fault
  escrow_cents: Cents               # physically held, not yet spent: coins + stacked bills
  bill_in_escrow_cents: Cents | null
  devices: {coin: DeviceState, bill: DeviceState, cashless: DeviceState}
  tubes: [{cents, count, full: bool}]
  exact_change: bool                # board's verdict from tubes vs policy.change_threshold_cents
  stacker: {count, full: bool} | null
  session: null | {session_id, funds_cents | null, started_ts}
  ts

DeviceState
  present: bool                     # declared in policy and answered a poll since boot
  status: DeviceStatus
  level: 1 | 2 | 3 | null           # MDB feature level negotiated at SETUP
  model: str | null
  serial: str | null
  denominations: [Cents]            # what it can accept (coin/bill) or pay (coin)
  enabled: [Cents]                  # subset currently enabled by policy
```

Edge mapping, decided here and inherited by spec 3:

- `payment_alive` stays on the heartbeat (`PAY-101 payment board offline`, unchanged).
- `payment_devices_ready` becomes three rows, one per device the policy declares present, failing on
  `offline | error | tilt | jammed`; `full` and `empty` are alert-only. A missing cashless reader fails
  only cashless sales; cash still sells.
- New machine-scope `warning` faults, raised from `device_status`: `PAY-106 coin changer fault`,
  `PAY-107 bill validator fault`, `PAY-108 cashless reader fault`.
- `exact_change: true` → the edge shows "exact change only"; the board's policy has already stopped it
  accepting bills above `change_threshold_cents`. Both sides read the same flag.

## 6. Commands (`cmd/mdb`)

All commands use `SubsystemCommand`/`CommandAck` with the existing request_id, ack, one-same-id-retry and
32-entry replay-cache rules. Param validators join `COMMAND_PARAM_VALIDATORS`; `payout` and
`vend_request` join `COMPLETION_TIMEOUTS`.

| Command | Params | Completion |
|---|---|---|
| `set_policy` | `policy_id`, `accept: bool`, `max_escrow_cents`, `coin_enabled: [Cents]`, `bill_enabled: [Cents]`, `cashless_enabled: bool`, `change_threshold_cents`, `edge_timeout_s` (5–120), `return_bill_on_lever: bool` | ack only; `state.policy_id` echoes it |
| `payout` | `cents > 0`, `reason: PayoutReason`, `sale_id: str \| null` | ack `accepted`, then `payout_result` (completion timeout 60 s) |
| `vend_request` | `session_id: str \| null`, `cents > 0`, `item: str` | ack `accepted`, then `vend_approved` / `vend_denied` (completion timeout 45 s) |
| `vend_result` | `session_id`, `request_id` (of the approved vend), `outcome: success \| failure` | ack only; board sends SESSION COMPLETE → `session_ended{complete}` |
| `end_session` | `session_id` | ack only → `session_ended{reason: edge}` |
| `return_escrow` | `{}` | ack only → `bill_returned{reason: edge}` if a bill was held, else ack `ok` with `detail: "no_bill"` |
| `device_reset` | `device: Device` | ack only → `device_status` once the device answers |
| `ping`, `self_test`, `force_report` | standard | unchanged |

`PayoutReason = change | session_timeout | cancel | vend_failed | maintenance | admin | error | customer`.
`payout` replaces `refund`; the enum includes `maintenance` and `customer`, which code uses today but the
old contract never listed.

Board-side rules fixed by the contract:

- **No policy, no money.** Until a `set_policy` is acked `ok`, `inhibited_reason` is `no_policy` and
  nothing is accepted. The edge sends `set_policy` on its own boot, whenever `state.boot_id` changes or
  `state.policy_id` is null, and whenever its `payment_enabled` verdict flips. `accept: false` is the
  acked replacement for the old `cmd/payment/enable false`.
- **Escrow cap and exact change.** A bill that would push `escrow_cents + bill` past `max_escrow_cents`,
  or that exceeds `change_threshold_cents` while `exact_change` is true, is returned by the board itself
  (`bill_returned{reason: policy}`). Coins are never refused for this reason (they cannot be returned
  from escrow once accepted).
- **Lever.** With `return_bill_on_lever: true` the board returns an escrowed bill on the lever itself
  (`bill_returned{reason: customer}`) and also emits `escrow_return_pressed`; the edge then decides
  whether to `payout` the rest.
- **Expiry.** A `payout` or `vend_request` whose `timestamp` is older than `edge_timeout_s` is acked
  `rejected` with `detail: "expired"`.
- **Idempotent payout.** A repeated `payout` request_id replays the stored `payout_result` and never pays
  twice. The dispatcher's single same-id retry therefore only helps after a lost ack, which is the
  intended semantics (an explicit `failed` is final for that request_id; the edge mints a new one if it
  decides to try again).
- **Vend result is mandatory.** The edge sends exactly one `vend_result` after every `vend_approved`.
  If `edge_timeout_s` passes with neither, the board sends VEND FAILURE on the edge's behalf and emits
  `session_ended{reason: timeout}`. `failure` charges nothing.
- **Tube math is the board's.** `payout` pays from tubes using level 3 `PAYOUT` or level 2 per-tube
  `DISPENSE`; a shortfall is `partial` with the exact `paid_cents`.

`TESTABLE_COMMANDS["mdb"]` becomes `STANDARD_COMMANDS | {device_reset, payout}`. The three
`bill_acceptor_test` / `coin_return_test` / `card_reader_test` counters are removed. The Tests level caps
a test `payout` at one smallest enabled coin (an edge rule, recorded in spec 3).

## 7. Reference flows (normative, reproduced in CONTRACT.md)

1. **Cash, pay-first.** `coin_accepted` / `bill_stacked` → edge escrow → button → dispense →
   if escrow > price, `payout{change, sale_id}` → `payout_result`. Leftover becomes change at once; it is
   not held as a balance. (Today's leftover-stays-escrowed multi-vend is dropped: it makes the
   exact-change state unreasonable. A customer who wants two items pays again.)
2. **Cash, price-first.** Button → edge arms the session → coins/bills → as flow 1.
3. **Cashless, tap-first.** `session_begun` → button → `vend_request{session_id, price}` →
   `vend_approved` → dispense → `vend_result{success}` → `session_ended{complete}`. On `vend_denied` the
   edge informs the customer; the session stays open until `end_session` or the reader's own timeout.
4. **Cashless, price-first.** Button → `vend_request{session_id: null}`. The board acks `rejected`,
   `detail: "no_session"` unless the reader negotiated always-idle, in which case the reader prompts for
   a tap and the flow continues as 3. The edge treats `no_session` as "wait for a tap".
5. **Vend failed after cashless approval.** `vend_result{failure}`: no charge, no refund; `PAY-102` as
   today.
6. **Vend failed after cash.** `payout{vend_failed, full escrow}`. The ledger restores the FIFO shares
   for its own row as today; the money physically goes back.
7. **Edge silent ≥ `edge_timeout_s`.** Board returns the escrowed bill, ends the session, inhibits.
   Coins already accepted are still owed: on reconnect the edge sees `state.escrow_cents > 0` with no
   open sale and issues `payout{error}`.
8. **Board reboot mid-sale.** New `boot_id`; unacked events replay from flash; the edge dedupes, re-sends
   `set_policy`, and reconciles `state.escrow_cents` against its ledger, raising `PAY-104` only if they
   disagree.

## 8. Error handling summary

| Condition | Behaviour |
|---|---|
| Malformed command params | ack `rejected` with the Pydantic message (as `dispense` does today) |
| Unknown command | ack `unsupported` |
| Unknown `event.kind` | log, ack, count in `PAY-105` detail |
| `seq` gap / unseen `boot_id` with `seq > 1` | `PAY-105`, keep processing |
| `payout_result.status != ok` | edge raises `PAY-103` (today a partial passes as confirmed) |
| Device `offline \| error \| tilt \| jammed` | that device's availability row fails; `PAY-106/107/108` |
| Edge silent | §3.2 |
| No policy acked | `inhibited_reason: no_policy`, nothing accepted |

## 9. Out of scope for 1.0

Cashless revalue, bill recycler (payout from bills), multi-currency, token / free-vend, DEX/EVA-DTS
audit, cashless level 2/3 options beyond always-idle, a second cashless reader (0x60).

## 10. Testing (contract package)

- Every model round-trips through JSON with ints intact; a float or numeric string in any `Cents` field
  fails validation.
- The event union accepts exactly the twelve `kind`s above and the strict envelope rejects an unknown `kind`; `PaymentEventHeader` parses any envelope so the edge can still ack it.
- Every enum (`DeviceStatus`, `PayoutReason`, `bill_returned.reason`, `session_ended.reason`,
  `inhibited_reason`, `payout_result.status`) is pinned by a test so a value cannot drift from this doc.
- `COMMAND_PARAM_VALIDATORS` has entries for every §6 command with params; `COMPLETION_TIMEOUTS` has
  `payout` (60 s) and `vend_request` (45 s); `TESTABLE_COMMANDS["mdb"]` equals §6.
- Generated `CONTRACT.md` and schemas are byte-checked, like the vending and ice-maker contracts.
- `PAY-105..108` are registered in `FAULT_TABLE` with the scopes and severities above and are absent from
  `PAYMENT_BLOCKING_FAULTS` (which stays at seven codes).
- The removed vending-contract names no longer import, and `tests/meta` package-isolation rules are
  unchanged (`ice_contracts` still imports nothing above it).
