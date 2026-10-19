# Payment Contract — v1.1.0

This document, together with the JSON Schema files in `schemas/`, defines the
interface between the ice-colder VMC (the edge) and the MDB payment board: the
ESP32 (or simulator) that is MDB master for the coin changer, bill validator
and cashless reader. The `schemas/*.schema.json` files are generated from
`packages/contracts/src/ice_contracts/board/payment.py` by
`uv run python -m ice_contracts.generate`; they are the normative payload
definitions. This `CONTRACT.md` is hand-written (the generator emits only the
schemas). Where prose in this document and a schema file appear to disagree,
treat that as a bug against this document — the schema files are regenerated
from source and are always current.

The design rationale lives in
`docs/superpowers/specs/2026-10-18-payment-contract-design.md`. This contract
supersedes the payment topics of the [vending-machine contract](../vending-machine/CONTRACT.md)
(`payment/credit`, `payment/status`, `cmd/payment/enable`, `cmd/payment/refund`),
which are deprecated.

## Identity & versioning

- The contract is versioned with [semver](https://semver.org/), starting at
  **1.0.0**; the current version is **1.1.0** (`CONTRACT_VERSION` in
  `payment.py`).
- **1.1.0 (additive).** New command `cash_sale{cents, sale_id}`: the edge tells
  the board a sale completed so the board lowers its `escrow_cents` by `cents`
  (clamped at zero) and, when a cashless reader is present, sends MDB CASH SALE
  to it. A 1.0.0 board simply does not advertise the command.
- The board reports the contract version it implements in
  `capabilities/mdb.contract_version`.
- **Minor** version bump: additive, backward-compatible change (new optional
  field, new event `kind`, new enum member).
- **Major** version bump: breaking change (renamed or removed field, renamed
  or removed topic or kind, changed type or semantics of an existing field).
- The edge accepts any `1.x` board.
- **Money:** every amount is an integer number of cents, validated as
  `StrictInt`, so `1.0` or `"100"` is rejected rather than coerced. `Cents` is
  an integer `>= 0`; `PositiveCents` is an integer `> 0`. The currency is not
  carried per message; it is declared once in `payment/state` (`"USD"`).

## Topic map

All topics are relative to `vmc/{machine_id}/`.

| Topic | Direction | QoS / retain | Payload |
|---|---|---|---|
| `payment/event` | board → edge | 1 / not retained | [`PaymentEventEnvelope`](schemas/payment_event_envelope.schema.json) |
| `payment/event/ack` | edge → board | 1 / not retained | [`PaymentEventAck`](schemas/payment_event_ack.schema.json) `{boot_id, seq, timestamp}` — cumulative within one boot: "I have processed every event with this `boot_id` and `seq` <= this" |
| `payment/state` | board → edge | 1 / **retained** | [`PaymentState`](schemas/payment_state.schema.json) |
| `cmd/mdb` | edge → board | 1 / not retained | `SubsystemCommand` (vending contract, [schema](../vending-machine/schemas/subsystem_command.schema.json)); per-command params below |
| `cmd/mdb/ack` | board → edge | 1 / not retained | `CommandAck` (vending contract, [schema](../vending-machine/schemas/command_ack.schema.json)) |
| `heartbeat/mdb`, `capabilities/mdb` | board → edge | unchanged | `SubsystemHeartbeat`, `SubsystemCapabilities`; device detail lives in `payment/state`, not in `channels` |

The board stays source-agnostic: it reports and lowers only the cash it holds
in escrow, while the edge may also hold virtual-gateway credit that never
touches these topics.

Topic constants: `PAYMENT_EVENT_TOPIC`, `PAYMENT_EVENT_ACK_TOPIC`,
`PAYMENT_STATE_TOPIC` in `payment.py`.

## Delivery

- `boot_id` is a random 8–32 character id minted at board boot. `seq` is a
  `StrictInt >= 1`, monotonic within a `boot_id`, starting at 1.
- The board appends every event to a flash-backed log before publishing it
  and removes it only when an ack with the SAME `boot_id` and `seq >=` its own
  arrives. An ack covers only log entries with its own `boot_id`; entries from
  an earlier boot stay in the log until acked under their own `boot_id`. On
  reconnect or reboot the board republishes the whole unacked log in order
  (earlier boots first) before anything new.
- The edge dedupes on `event_id`, processes in `seq` order, and acks
  cumulatively **after processing** (not on receipt). It never blocks a sale
  waiting for its own ack to be delivered. A duplicate `event_id` is not
  reprocessed but is acked again (the board's replay and lost-ack recovery
  depend on it).
- A `seq` gap within a known `boot_id`, or a never-seen `boot_id` whose first
  event has `seq > 1`, raises `PAY-105` (payment event gap; machine scope,
  `warning`, not payment-blocking). The edge still processes what it has; the
  fault tells the operator the cash count may be off.
- An unknown `event.kind` (a newer board) is logged, acked so the stream is
  not blocked, and counted in `PAY-105`'s detail. The strict
  `PaymentEventEnvelope` rejects such an envelope as a whole, so the edge
  parses [`PaymentEventHeader`](schemas/payment_event_header.schema.json)
  (`event_id`, `boot_id`, `seq`, `ts`, and `event` as any object) first and
  acks from it, then parses the body with `PaymentEventEnvelope`; a body
  failure caused by an unknown `kind` is logged and counted, not retried.

## Fail-safe on edge silence

The policy carries `edge_timeout_s` (5–120; `DEFAULT_EDGE_TIMEOUT_SECONDS` =
30). The edge sends `ping` on `cmd/mdb` at least every
`EDGE_IDLE_PING_SECONDS` (10) seconds while idle. If the board sees no
`cmd/mdb` traffic for `edge_timeout_s` it:

1. disables all acceptance;
2. returns any bill in escrow (`bill_returned{reason: edge_silent}`);
3. ends any open cashless session (`session_ended{reason: edge}`);
4. sets `inhibited_reason: edge_silent`.

Acceptance resumes only on a fresh acked `set_policy`.

## Events (`payment/event`)

Every event is a [`PaymentEventEnvelope`](schemas/payment_event_envelope.schema.json):

```
PaymentEventEnvelope
  event_id: str (8–64)        # unique per event, board-minted
  boot_id:  str (8–32)
  seq:      StrictInt >= 1
  ts:       datetime
  event:    PaymentEventBody   # discriminated union on `kind`
```

Nullable fields in events and state are required-but-nullable: the board
emits an explicit `null`, never omits the key. The only exceptions are
`vend_request.session_id` and `payout.sale_id` (command params), which may be
omitted. Every event body's `kind` is required.

Enums: `Device` = `coin` | `bill` | `cashless`; `DeviceStatus` = `ready` |
`disabled` | `busy` | `jammed` | `full` | `empty` | `tilt` | `offline` |
`error` ([schema](schemas/device_status.schema.json)).

| `kind` | Fields | Meaning | Credit? |
|---|---|---|---|
| `coin_accepted` | `cents` (> 0), `routed`: `tubes` \| `cashbox` | A coin dropped and is in the machine | **yes** |
| `bill_escrowed` | `cents` (> 0) | The validator holds a bill pending the policy verdict | no |
| `bill_stacked` | `cents` (> 0) | The bill went into the stacker | **yes** |
| `bill_returned` | `cents` (> 0), `reason`: `policy` \| `edge_silent` \| `customer` \| `edge` \| `timeout` | The escrowed bill was given back | no |
| `escrow_return_pressed` | — | Customer pressed the coin-return lever | no; edge decides |
| `payout_result` | `request_id` (8–64), `requested_cents` (> 0), `paid_cents` (>= 0), `status`: `ok` \| `partial` \| `failed`, `detail` (optional) | Terminal result of a `payout` command | no |
| `session_begun` | `session_id` (1–64), `funds_cents` (>= 0 or `null`) | Cashless tap; `null` = reader does not disclose funds | no |
| `vend_approved` | `session_id`, `request_id`, `cents` (> 0) | Reader approved the edge's `vend_request` | provisional |
| `vend_denied` | `session_id`, `request_id`, `reason` | Reader denied it | no |
| `session_ended` | `session_id`, `reason`: `complete` \| `cancelled` \| `reader` \| `edge` \| `timeout` | Session closed | no |
| `device_status` | `device`, `status`: `DeviceStatus`, `mdb_status` (integer 0–255 or `null`) | The board's mapping of an MDB status byte; raw byte kept for diagnostics | no |
| `cash_collected` | `device` | Cashbox removed or tubes emptied by a tech | no |

**Credit arithmetic on the edge (binding):** only `coin_accepted` and
`bill_stacked` create an escrow `Credit`. `vend_approved` creates a
*provisional* credit that `vend_result{success}` finalises and
`vend_result{failure}` discards. `bill_escrowed` may be shown to the customer
but is never spendable, so a bill the policy then returns needs no refund.
`partial` is explicit and is never folded into `ok`.

**`payout_result` consistency** (enforced by the model): `ok` requires
`paid_cents == requested_cents`; `partial` requires
`0 <= paid_cents < requested_cents`; `failed` requires `paid_cents == 0`.

A `payout_result` also lowers `state.escrow_cents` by `paid_cents` (the board
debits the coins it just paid out; clarified in firmware spec 4, no version
bump).

`revalue_result` is deliberately absent from 1.0 (see Out of scope).

## State (`payment/state`, retained)

[`PaymentState`](schemas/payment_state.schema.json) is republished on every
change, on `force_report`, and every `STATE_REPUBLISH_SECONDS` (30) seconds.
Heartbeats remain the liveness source; a retained state document never means
the board is alive.

```
PaymentState
  boot_id (8–32), seq_last >= 0     # reboot detection and replay horizon (0 = none yet)
  currency: "USD"
  policy_id: str | null             # policy in force (set_policy)
  accepting: bool
  inhibited_reason: null | no_policy | edge_silent | policy | device_fault
  escrow_cents: Cents               # physically held, not yet spent: coins + stacked bills
  bill_in_escrow_cents: Cents | null
  devices: {coin: DeviceState, bill: DeviceState, cashless: DeviceState}
  tubes: [{cents > 0, count >= 0, full: bool}]
  exact_change: bool                # board's verdict from tubes vs policy.change_threshold_cents
  stacker: {count >= 0, full: bool} | null
  session: null | {session_id, funds_cents | null, started_ts}
  ts

DeviceState
  present: bool                     # declared in policy and answered a poll since boot
  status: DeviceStatus
  level: 1 | 2 | 3 | null           # MDB feature level negotiated at SETUP
  model: str | null
  serial: str | null
  denominations: [PositiveCents]    # what it can accept (coin/bill) or pay (coin)
  enabled: [PositiveCents]          # subset currently enabled by policy (validated)
```

`accepting` is true if and only if `inhibited_reason` is `null` (enforced by
the model).

Edge mapping, fixed here and inherited by the edge-migration spec:

- `payment_alive` stays on the heartbeat (`PAY-101 payment board offline`,
  unchanged).
- `payment_devices_ready` becomes three rows, one per device the policy
  declares present, failing on `offline | error | tilt | jammed`; `full` and
  `empty` are alert-only. A missing cashless reader fails only cashless
  sales; cash still sells.
- New machine-scope `warning` faults, raised from `device_status`:
  `PAY-106` coin changer fault, `PAY-107` bill validator fault, `PAY-108`
  cashless reader fault. None is payment-blocking.
- `exact_change: true` → the edge shows "exact change only"; the board's
  policy has already stopped it accepting bills above
  `change_threshold_cents`. Both sides read the same flag.

## Commands (`cmd/mdb`)

All commands use `SubsystemCommand` / `CommandAck` with the existing
`request_id`, ack, one-same-id-retry and 32-entry replay-cache rules (see
"Idempotency requirement on real firmware" in the
[vending-machine contract](../vending-machine/CONTRACT.md): the board MUST
keep the last 32 `request_id`s with their acks and republish the cached ack,
without repeating the side effect, on a duplicate). Param validators are
registered in `COMMAND_PARAM_VALIDATORS`; `payout` and `vend_request` are
registered in `COMPLETION_TIMEOUTS`.

| Command | Params | Completion |
|---|---|---|
| `set_policy` | [`SetPolicyParams`](schemas/set_policy_params.schema.json): `policy_id` (1–64), `accept: bool`, `max_escrow_cents` (> 0), `coin_enabled: [PositiveCents]`, `bill_enabled: [PositiveCents]`, `cashless_enabled: bool`, `change_threshold_cents` (>= 0), `edge_timeout_s` (5–120), `return_bill_on_lever: bool` | ack only; `state.policy_id` echoes it |
| `payout` | [`PayoutParams`](schemas/payout_params.schema.json): `cents` (> 0), `reason`: [`PayoutReason`](schemas/payout_reason.schema.json), `sale_id: str \| null` (default `null`) | ack `accepted`, then `payout_result` (completion timeout 60 s) |
| `vend_request` | [`VendRequestParams`](schemas/vend_request_params.schema.json): `session_id: str \| null` (default `null`; omitting the key means price-first), `cents` (> 0), `item` (1–64) | ack `accepted`, then `vend_approved` / `vend_denied` (completion timeout 45 s); rejected with `detail: "price_scale"` when `cents` is not a multiple of the reader's scale factor |
| `vend_result` | [`VendResultParams`](schemas/vend_result_params.schema.json): `session_id`, `request_id` (of the approved vend), `outcome`: `success` \| `failure` | ack only; board sends SESSION COMPLETE → `session_ended{complete}` |
| `end_session` | [`EndSessionParams`](schemas/end_session_params.schema.json): `session_id` | ack only → `session_ended{reason: edge}` |
| `return_escrow` | `{}` (no params model) | ack only → `bill_returned{reason: edge}` if a bill was held, else ack `ok` with `detail: "no_bill"` |
| `device_reset` | [`DeviceResetParams`](schemas/device_reset_params.schema.json): `device`: `coin` \| `bill` \| `cashless` | ack only → `device_status` once the device answers |
| `cash_sale` | [`CashSaleParams`](schemas/cash_sale_params.schema.json): `cents` (> 0), `sale_id: str \| null` (default `null`) | ack only; board lowers `escrow_cents` by `cents`, clamped at zero (ack `ok` with `detail: "clamped"` when it clamps), and sends MDB CASH SALE to the reader when present |
| `ping`, `self_test`, `force_report` | standard | unchanged |

Two params default to `null`: `PayoutParams.sale_id` (no sale attached) and
`VendRequestParams.session_id` (omitting the key, like sending `null`, means a
price-first request; see flow 4).

`PayoutReason` = `change` | `session_timeout` | `cancel` | `vend_failed` |
`maintenance` | `admin` | `error` | `customer`. `payout` replaces the legacy
`refund`; the enum includes `maintenance` and `customer`, which code uses
today but the old contract never listed.

**Completion table** (extends the vending contract's):

| Subsystem | Command | Kind | Completion signal | Completion timeout |
|---|---|---|---|---|
| mdb | `payout` | **long-running** | ack `phase="accepted"`, then a terminal `payout_result` event | fixed 60 s (`PAYOUT_COMPLETION_TIMEOUT_SECONDS`) |
| mdb | `vend_request` | **long-running** | ack `phase="accepted"`, then `vend_approved` or `vend_denied` | fixed 45 s (`VEND_REQUEST_COMPLETION_TIMEOUT_SECONDS`) |
| mdb | `set_policy`, `vend_result`, `end_session`, `return_escrow`, `device_reset`, `cash_sale`, `ping`, `self_test`, `force_report` | immediate | the ack itself | — (10 s ack deadline only) |

**Board-side rules fixed by the contract:**

1. **No policy, no money.** Until a `set_policy` is acked `ok`,
   `inhibited_reason` is `no_policy` and nothing is accepted, and `payout` and
   `vend_request` are acked `rejected` with `detail: "no_policy"` (clarified in
   firmware spec 4). The edge sends
   `set_policy` on its own boot, whenever `state.boot_id` changes or
   `state.policy_id` is null, and whenever its `payment_enabled` verdict
   flips. `accept: false` is the acked replacement for the old
   `cmd/payment/enable false`.
2. **Escrow cap and exact change.** A bill that would push
   `escrow_cents + bill` past `max_escrow_cents`, or that exceeds
   `change_threshold_cents` while `exact_change` is true, is returned by the
   board itself (`bill_returned{reason: policy}`). Coins are never refused for
   this reason (they cannot be returned from escrow once accepted).
3. **Lever.** With `return_bill_on_lever: true` the board returns an escrowed
   bill on the lever itself (`bill_returned{reason: customer}`) and also
   emits `escrow_return_pressed`; the edge then decides whether to `payout`
   the rest.
4. **Expiry.** A `payout` or `vend_request` whose `timestamp` is older than
   `edge_timeout_s` is acked `rejected` with `detail: "expired"`.
5. **Idempotent payout.** A repeated `payout` `request_id` replays the stored
   `payout_result` and never pays twice. The dispatcher's single same-id retry
   therefore only helps after a lost ack, which is the intended semantics (an
   explicit `failed` is final for that `request_id`; the edge mints a new one
   if it decides to try again).
6. **Vend result is mandatory.** The edge sends exactly one `vend_result`
   after every `vend_approved`. If `edge_timeout_s` passes with neither, the
   board sends VEND FAILURE on the edge's behalf and emits
   `session_ended{reason: timeout}`. `failure` charges nothing.
7. **Tube math is the board's.** `payout` pays from tubes using level 3
   `PAYOUT` or level 2 per-tube `DISPENSE`; a shortfall is `partial` with the
   exact `paid_cents`.

**Testable set.** `payment.MDB_TESTABLE_COMMANDS` (`STANDARD_COMMANDS` plus
`device_reset` and `payout`) is the future allowlist for the Tests level.
`TESTABLE_COMMANDS["mdb"]` in `common.py` still holds the legacy counters
today; it flips to `MDB_TESTABLE_COMMANDS` when the simulator that advertises
the new commands ships (spec 2). The Tests level caps a test `payout` at one
smallest enabled coin (an edge rule, recorded in the edge-migration spec).

## Reference flows (normative)

1. **Cash, pay-first.** `coin_accepted` / `bill_stacked` → edge escrow →
   button → dispense → if escrow > price, `payout{change, sale_id}` →
   `payout_result`; the edge also sends `cash_sale{price, sale_id}` once the
   dispense completes. Leftover becomes change at once; it is not held as a
   balance. (The old leftover-stays-escrowed multi-vend is dropped: it makes
   the exact-change state unreasonable. A customer who wants two items pays
   again.)
2. **Cash, price-first.** Button → edge arms the session → coins/bills → as
   flow 1.
3. **Cashless, tap-first.** `session_begun` → button →
   `vend_request{session_id, price}` → `vend_approved` → dispense →
   `vend_result{success}` → `session_ended{complete}`. On `vend_denied` the
   edge informs the customer; the session stays open until `end_session` or
   the reader's own timeout.
4. **Cashless, price-first.** Button → `vend_request{session_id: null}`
   (or with the key omitted). The board acks `rejected`,
   `detail: "no_session"` unless the reader negotiated always-idle, in which
   case the reader prompts for a tap and the flow continues as 3. The edge
   treats `no_session` as "wait for a tap".
5. **Vend failed after cashless approval.** `vend_result{failure}`: no
   charge, no refund; `PAY-102` as today.
6. **Vend failed after cash.** `payout{vend_failed, full escrow}`. The ledger
   restores the FIFO shares for its own row as today; the money physically
   goes back.
7. **Edge silent >= `edge_timeout_s`.** Board returns the escrowed bill, ends
   the session, inhibits. Coins already accepted are still owed: on reconnect
   the edge sees `state.escrow_cents > 0` with no open sale and issues
   `payout{error}`.
8. **Board reboot mid-sale.** New `boot_id`; unacked events replay from
   flash; the edge dedupes, re-sends `set_policy`, and reconciles
   `state.escrow_cents` against its ledger, raising `PAY-104` only if they
   disagree.

## Error handling

| Condition | Behaviour |
|---|---|
| Malformed command params | ack `rejected` with the Pydantic message (as `dispense` does today) |
| Unknown command | ack `unsupported` |
| Unknown `event.kind` | log, ack, count in `PAY-105` detail |
| `seq` gap / unseen `boot_id` with `seq > 1` | `PAY-105`, keep processing |
| `payout_result.status != ok` | edge raises `PAY-103` (today a partial passes as confirmed) |
| Device `offline \| error \| tilt \| jammed` | that device's availability row fails; `PAY-106` / `PAY-107` / `PAY-108` |
| Edge silent | see "Fail-safe on edge silence" |
| No policy acked | `inhibited_reason: no_policy`, nothing accepted |

## Out of scope for 1.0

Cashless revalue, bill recycler (payout from bills), multi-currency, token /
free-vend, DEX/EVA-DTS audit, cashless level 2/3 options beyond always-idle,
a second cashless reader (0x60).
