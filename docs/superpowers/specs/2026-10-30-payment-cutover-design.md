# Payment cutover: MDB simulator and edge onto the payment contract (cash) — design

**Date:** 2026-10-30
**Status:** approved design, awaiting implementation plan
**Scope:** spec B of 2 for TODO item M3, and specs 2 and 3 of the payment series in
`2026-10-18-payment-contract-design.md` combined, **cash only** (coin changer and bill validator).
Cashless sessions (`session_begun`, `vend_request`, `vend_approved`/`vend_denied`, `vend_result`,
`end_session`, PAY-108) are a follow-on spec. Requires spec A (`2026-10-30-money-in-cents-design.md`):
every amount below is integer cents.

Normative inputs: `docs/contracts/payment/CONTRACT.md` (contract 1.1.0, its delivery rules, the
board-side rules 1–7 and the reference flows 1, 2, 6, 7, 8), the "Edge mapping, fixed here" list in the
same document, and the as-built firmware rules in `2026-10-18-mdb-board-firmware-design.md` §11
(`event_id = f"{boot_id}-{seq}"`, `payout`/`vend_request` rejected `no_policy` until a `set_policy` is
acked in the current board boot, the edge must re-ack duplicate `event_id`s).

## 1. Problem

The edge still consumes the legacy payment topics: `payment/credit` (a float with a free-text method,
no id), `payment/status`, `cmd/payment/enable` (no request id, no ack) and `cmd/payment/refund`.
Nothing in `ice_edge` or `ice_sims` imports `ice_contracts.board.payment`. Consequences:

- A QoS-1 redelivery of `payment/credit` calls `deposit_funds` twice for one coin; there is no
  `event_id`, no `seq`, no gap detection, and PAY-105..PAY-107 are never raised.
- The real MDB board (firmware spec 4) inhibits acceptance until it sees an acked `set_policy` and a
  `cmd/mdb` message at least every `edge_timeout_s`; today's edge sends neither, so the board would
  never accept a coin.
- `CommandDispatcher.send_and_await_completion` has no `payment/event` completion source, so an `mdb`
  `payout` acked `accepted` would wait for a completed-phase ack the contract never sends and time out.
- The simulator speaks only the legacy topics, so the sim stack, CI and the hpz440 fleet cannot
  exercise the new contract, and `tests/firmware/mdb/` skips without hardware.

## 2. Decisions (owner, 2026-10-30)

1. **One spec for simulator and edge,** cutting both over at once and removing the legacy payment
   topics (vending contract 3.0.0). No dual-stack period.
2. **Cash only.** Cashless is the next spec.
3. **A dedicated MDB adapter,** `ice_edge.payment.mdb`, owns the whole board conversation. The sale
   FSM sees credits arriving plus two calls, `pay_out` and `cash_sale`, and never an envelope, a
   `boot_id` or a policy. MDB stays one gateway among peers (Stripe/PayPal QR credit would feed the
   same `EscrowLedger` through its own adapter).
4. **Persist the event cursor,** so a replay after an edge restart is acked without being credited
   twice; money lost from the edge's memory by the restart is recovered from the board's
   `escrow_cents` (reconciliation, §5.5).
5. **Leftover credit becomes change at once** (contract flow 1, already binding). The multi-vend
   balance and the post-vend session timer are dropped.

## 3. Edge architecture

New package `ice_edge.payment.mdb` (layer: same as `ice_edge.payment`; imports `ice_contracts`,
`ice_common`, `ice_edge.hal` for the dispatcher, `ice_edge.ledger` for the cursor store; never
`ice_edge.sale` or `ice_edge.app`). Four collaborators plus a facade, all built by `Machine`:

| Unit | File | Owns |
|---|---|---|
| `EventIntake` | `intake.py` | `payment/event` parsing, dedup, ordering, PAY-105, acks, the cursor |
| `PolicySync` | `policy.py` | `set_policy` (build, send, re-send), the idle `ping` keepalive, "policy acked in this board boot" |
| `PayoutProtocol` | `payout.py` (replaces `ice_edge/payment/refund_protocol.py`) | `payout` commands, their completion, the offline hold queue, PAY-103 |
| `StateConsumer` | `state.py` | retained `payment/state`: device rows, PAY-106/107, exact change, reconciliation triggers |
| `MdbGateway` | `__init__.py` | the facade `Machine` and the VMC talk to: `pay_out`, `cash_sale`, read-only `state`, `policy_acked` |

### 3.1 The VMC's view

The VMC gets one new injected collaborator, `payments` (the `MdbGateway`, typed by a `Protocol` in
`ice_edge.payment` so the import-linter contract 5 on `vmc.py` is not widened), and keeps no MDB
knowledge:

- `deposit_funds(cents, payment_method, *, event_id=None)` stays the single credit entry point.
  `EventIntake` calls it for `coin_accepted` (method `cash_coin`) and `bill_stacked` (method
  `cash_bill`); both are already cash under `event_recorder.is_cash`, so reports and collections
  need no change. `Credit` gains `event_id: str | None` (persisted in the snapshot, §5.1).
- `request_refund(reason)` keeps its name and is still the only money-out path; it now calls
  `payments.pay_out(cents, reason, sale_id)` for the **cash** portion of escrow. A non-cash credit has
  no source today (the QR gateways never deposit); if one is ever present at refund time it is left
  out of the payout, logged as an error and raises PAY-103 with outcome `non_cash_refund`, never paid
  in coins.
- `on_complete_transaction`: if escrow is non-zero after the sale's shares were consumed, it calls
  `request_refund(reason="change")` immediately, then goes to `idle`. `complete_transaction` therefore
  always ends in `idle`; `_post_dispense_dest` and the post-vend `session_timer.arm()` are removed.
- After a successful, recorded, non-test dispense the VMC calls
  `payments.cash_sale(cash_cents, sale_id)` where `cash_cents` is the sum of the sale's cash-method
  shares (the board holds only cash). Skipped when that sum is 0.
- `SaleContext` gains `sale_id` (uuid4 hex, minted with the context). It is sent on `payout` and
  `cash_sale` and logged; it is not stored in `sales`.
- `PayoutReason` covers every reason the VMC uses today: `change`, `session_timeout`, `cancel`,
  `vend_failed` (replaces today's `reason=code.value` in `_fail_vend`), `maintenance`, `admin`,
  `error`, `customer`. The fault code that caused a `vend_failed` payout goes in the journal line, not
  the wire.

Test sales are unchanged: their `"test"` credit never reaches `payments`, as today it never reaches a
refund.

## 4. Simulator (`ice_sims.mdb`)

`ice_sims.mdb_gateway` becomes a thin entry module (like `ice_sims.vending_machine`) so
`docker-compose.yml`, `docker/machine/launcher.py` and the CI smoke step's `[mdb] Connected` log line
are unchanged. The implementation moves to a package `ice_sims.mdb` with an `ast`-pinned single import
direction:

| Module | Pure? | Models |
|---|---|---|
| `cash.py` | yes | tubes (`{cents, count, capacity}`; defaults 5/10/25/100 with 40/40/40/0), stacker (count, capacity 500), bill escrow, `escrow_cents`; payout tube math (largest-first, `partial` on shortfall with exact `paid_cents`), `cash_sale` clamp |
| `policy.py` | yes | `set_policy` application, the escrow cap and exact-change bill rule (contract rule 2), `exact_change` from tubes vs `change_threshold_cents`, `inhibited_reason` |
| `event_log.py` | yes | `boot_id` (random 12 chars at sim start), `seq`, `event_id = f"{boot_id}-{seq}"`, unacked log (in memory, bounded at 256 like the firmware's NVS), cumulative ack removal by `(boot_id, seq)`, ordered replay of everything unacked |
| `customer.py` | no | while the edge's retained `status` is `interacting_with_user` and the board is accepting: insert coins and bills toward the selected product's price; occasionally (5%) overpay to exercise change, occasionally press the lever |
| `faults.py` | no | `InjectedFault` pairs: `coin_jammed` (`device_status coin jammed`), `bill_offline`, `changer_empty` (empties tubes, so payouts go `partial`/`failed`), `board_reboot` (new `boot_id`, `seq` restarts at 1, the unacked log survives, policy lost → `no_policy`), `drop_ack_window` (ignore the next N acks, to exercise replay) |
| `simulator.py` | no | composes the above; owns MQTT, publishing and the command handlers |

Wire behaviour:

- Capabilities: `contract_version = payment.CONTRACT_VERSION` (1.1.0), `commands` = standard first
  then `PAYMENT_COMMANDS` (11 total), `channels = []`.
- `payment/state` retained, republished on change, on `force_report`, and every
  `STATE_REPUBLISH_SECONDS`. `devices.cashless.present = false`.
- Commands: `set_policy`, `payout` (ack `accepted`, then `payout_result` after 0.5–2 s; replay of a
  repeated `request_id` re-publishes the stored result and never pays twice), `cash_sale`,
  `return_escrow`, `device_reset`, plus the standard three. Board-side rules 1 (no policy, no money),
  4 (expiry) and 5 (idempotent payout) are enforced. `vend_request` is acked `rejected`,
  `detail: "no_session"`; `vend_result` and `end_session` are acked `rejected`, `detail:
  "no_session"`.
- Edge-silence fail-safe: no `cmd/mdb` for `edge_timeout_s` → disable, return a held bill
  (`bill_returned{edge_silent}`), `inhibited_reason: edge_silent` until a fresh acked `set_policy`.
- On (re)connect the sim republishes the whole unacked log in order before anything new.
- Home Assistant discovery: `binary_sensor` per present device (`status == ready`), `sensor` for
  `escrow_cents` and `accepting`, from `payment/state`; the uptime sensor stays.

The simulator's log is not persisted (its `data/` mount is read-only); a sim *process* restart loses
the log, unlike the firmware. `board_reboot` is the fault that models a real reboot.

## 5. Edge components

### 5.1 `EventIntake`

Subscribes `payment/event`. For each message:

1. Parse `PaymentEventHeader`. Invalid → log and drop (no ack possible).
2. **Duplicate** (`event_id` in the recent set, or `seq <= cursor[boot_id]`) → re-ack, do nothing
   else.
3. **Gap** (a known `boot_id` with `seq > cursor + 1`, or an unseen `boot_id` whose `seq > 1`) →
   raise PAY-105 (once while active; its outcome text counts gaps and unknown kinds), then process.
4. Parse the body with `PaymentEventEnvelope`. A failure whose cause is an unknown `kind` → log, count
   in PAY-105, ack. Any other body failure → log, PAY-105, ack (the board cannot fix a malformed
   event by resending it).
5. Dispatch by kind: `coin_accepted`/`bill_stacked` → `deposit_funds`; `bill_escrowed` → customer
   message only ("Bill held: $5.00"); `bill_returned` → journal line; `escrow_return_pressed` →
   `request_refund(reason="customer")` unless the FSM is `dispensing` (then logged and ignored, as
   the lever has no meaning mid-vend); `payout_result` → `PayoutProtocol`; `device_status` →
   `StateConsumer`; `cash_collected` → event row only; any cashless kind → log and ignore (no reader
   is present).
6. Record the `payment` event row (`value` = cents) for credit kinds; this replaces
   `EventRecorder`'s legacy `payment/credit` subscription, so `money_in` keeps working.
7. Persist, then ack (§5.1.1).

Events are processed strictly in arrival order per `boot_id`; an out-of-order event (a lower `seq`
than one already processed but not a duplicate id) is treated as a duplicate.

**5.1.1 Cursor persistence.** The cursor is `{boot_id: last_processed_seq}` for the current and
previous board boot, plus the last 64 `event_id`s. It lives in its own file,
`data/payment_cursor.json`, written atomically (tmp + rename) on the `SessionStore` worker thread, so
it is ordered after the snapshot write the deposit triggered. The ack is published only after the
cursor write completes. This deviates from the brainstorm wording ("in the session snapshot"):
`session.json` is deleted whenever the sale closes, so it cannot hold a cursor that must survive idle.
Each `Credit` in the snapshot carries its `event_id`, which gives the same guarantee across the
crash window between the two writes: at boot, `EventIntake` seeds its recent-id set from the cursor
file **and** from the credits of a loaded `session.json`, so a replay of an event whose credit is
held as PAY-104 evidence is acked without crediting it again.

The cursor file is not PAY-104 evidence and is written even while PAY-104 holds the session file.
A failed cursor write raises DATA-104 (same alert as a failed snapshot) and the ack is **still sent**:
withholding it would make the board replay forever and fill its log, which is worse than a possible
double credit after a second crash inside the failure window. A corrupt or unreadable cursor file at
boot is renamed aside (`.corrupt-<ts>`), logged, and PAY-105 is raised (the edge cannot tell which
replays are new).

### 5.2 `PolicySync`

- Builds `SetPolicyParams` from a new config section `payment.mdb.policy` (`MdbPolicyConfig`):
  `coin_enabled` (default `[5, 10, 25, 100]`), `bill_enabled` (`[100, 500, 1000, 2000]`),
  `max_escrow_cents` (2000), `change_threshold_cents` (500), `edge_timeout_s` (30),
  `return_bill_on_lever` (true). `accept` is `availability.payment_enabled`. `policy_id` is the first
  16 hex characters of the SHA-256 of the canonical params JSON. The existing `MDBDevicesConfig`
  fields (`polling_interval`, `devices`) are unused and left alone. Missing keys fill from defaults,
  so a deployed `config.json` needs no edit. The section is shown read-only on `/settings/payments`.
- Sends `set_policy` through the dispatcher: at edge start once the MQTT client is subscribed; when
  `payment/state.boot_id` differs from the last acked policy's board boot; when `state.policy_id` is
  null or differs from the current policy's id; and when `availability.payment_enabled` flips (this
  replaces `outputs.publish_payment_enable` and `Availability.republish` targets `PolicySync`).
  `policy_acked` is true only once an `ok` ack has been received **and** a `payment/state` from the
  same board `boot_id` echoes the policy's `policy_id`. The firmware may ack `ok` with
  `detail: "queued"` while the command is still waiting for the bus (firmware spec §11, "Ack model"),
  so the ack alone does not prove the policy is in force. A send that fails
  (`CommandTimeout`, non-`ok`) is retried every 5 s while the board is alive.
- Keepalive: a supervised task sends `ping` on `cmd/mdb` whenever no `cmd/mdb` command was sent for
  `EDGE_IDLE_PING_SECONDS` (10). It runs only while MQTT is connected.

### 5.3 `PayoutProtocol`

- `pay_out(cents, reason, sale_id)` queues a payout. Payouts are sent one at a time, in order, and
  only while `policy_acked` (the board rejects `payout` with `no_policy` otherwise) and the board is
  alive (no PAY-101). While either is false the queue holds, so coins owed are never dropped by an
  outage; the queue is in memory and its contents are also in the session snapshot as
  `pending_payouts: list[{request_id, cents, reason, sale_id}]` (replacing
  `pending_refund_request_id`), so an edge restart with a queued payout is PAY-104 evidence exactly as
  a pending refund is today.
- Sending: `dispatcher.send_and_await_completion("mdb", "payout", params)`. The dispatcher's single
  same-id retry covers a lost ack; the board's rule 5 makes that safe.
- **Dispatcher change:** `CommandDispatcher` gains a public `complete(request_id, result)` that
  resolves a pending `send_and_await_completion` (or stores an early completion, as the existing
  `_early_completions` buffer does). `EventIntake` calls it for `payout_result`. The existing
  `hardware/dispenser` path is refactored onto the same method.
- **The ack never settles a payout.** Any non-rejecting ack (`accepted`, or `ok` with
  `detail: "queued"`, which the firmware may send while the command waits for the bus) means "wait
  for `payout_result`". Only `payout_result` (through `complete`) or the 60 s completion timeout ends
  it. A `busy` ack (the board is still holding another payout's result) requeues it after 2 s.
- Outcomes: `ok` → journal `refund_confirmed` (cents), persist. `partial` (including the
  firmware's `detail: "scaling"`, an amount the changer cannot represent) → journal with `paid_cents`,
  PAY-103 with outcome `partial:<paid>/<requested>`. `failed` → PAY-103
  `failed:<detail>`. A `rejected` ack (`no_policy`, `expired`) → back to the head of the queue and
  retried after the next `policy_acked`. `CompletionTimeout` (60 s) → PAY-103 `unconfirmed`; no
  automatic retry with a new `request_id` (it could pay twice). The `sales`/`refund` event rows keep
  their current names; `refund` rows record `paid_cents`.

### 5.4 `cash_sale`

`cash_sale(cents, sale_id)` goes through the same ordered queue as payouts (so a change payout and the
sale's `cash_sale` reach the board in the order the sale produced them) and is held under the same
conditions. An `ok` with `detail: "clamped"` (the board held less cash than the edge thought) is
logged as a warning and raises PAY-104 with outcome `escrow_mismatch:cash_sale_clamped`, the same
fault reconciliation uses when the board holds less than the edge; PAY-105 is reserved for event-stream
gaps. Never retried with a new id. An `ok` with `detail: "queued"` counts as delivered.

### 5.5 `StateConsumer` and reconciliation

- Validates retained `payment/state` (`PaymentState`), keeps the latest as `MdbGateway.state`, and
  notifies `PolicySync` (boot or policy change).
- **Availability** (the contract's fixed edge mapping): the legacy `payment_devices_ready` row is
  replaced by one row per device the policy declares present, `payment_coin_ready` and
  `payment_bill_ready` (`payment_cashless_ready` arrives with the cashless spec), each failing on
  `offline | error | tilt | jammed`; `full`/`empty` are alert-only. Gate: a single device row is
  `alert` (a jammed coin changer leaves bills usable, and the reverse), and the derived row
  `cash_accepting` is `fulfillment`, failing when no present cash device is usable or when
  `inhibited_reason` is `device_fault` or `edge_silent`. `inhibited_reason: policy` (the edge's own
  `accept: false`) and `no_policy` never fail it, since the edge caused them. `payment_alive`
  (heartbeat) is unchanged.
- **Faults:** `device_status` and each state document raise PAY-106 (coin) / PAY-107 (bill) when that
  device is `offline | error | tilt | jammed`, and clear them `by="auto"` when it is `ready`,
  `disabled` or `busy`.
- **Exact change:** when `exact_change` flips true the VMC's customer message is "Exact change only"
  (through `outputs.message`); the `/screen` page is pinned and does not change.
- **Reconciliation** (contract flows 7 and 8). Armed by four triggers: edge start, a new board
  `boot_id`, MQTT reconnect, and PAY-101 clearing. An armed reconciliation runs once, at the first
  moment all of these hold: the FSM is `idle`; **PAY-104 is not active** (an uncertain snapshot is an
  open sale in the sense of contract flow 7: the board's escrow may hold the price of a product that
  was dispensed but never `cash_sale`d, and paying it out would refund a delivered product);
  `EventIntake` has processed through `state.seq_last` for `state.boot_id`; the payout/`cash_sale`
  queue is empty and nothing is in flight; the state document is newer than the last processed event.
  It then compares `state.escrow_cents` with the edge's cash escrow (cash-method credits in
  `EscrowLedger`):
  - equal → nothing;
  - board higher → `pay_out(diff, "error", None)` (coins accepted while the edge was silent or before
    it restarted are returned);
  - board lower → PAY-104 with outcome `escrow_mismatch:<edge>/<board>`; no money moves.
  Not running while a sale or payout is in progress is what keeps a `cash_sale` still on its way from
  being mistaken for an excess. A reconciliation blocked by PAY-104 stays armed and runs after the
  operator resolves PAY-104 (below).

  A `partial` change payout leaves the shortfall in the board's escrow. The next reconciliation pays
  it out again (`error`), which is intended: the customer is owed it, and the tubes may have been
  refilled. If the tubes are still short the retry is `partial` or `failed` again and PAY-103 stays
  raised; each trigger causes at most one attempt, so there is no loop.

- **PAY-104 recovery drives the board.** Once payouts are automatic, the operator no longer refunds
  "at the payment device". The two PAY-104 actions (`web/routes/health/recovery.py`) gain board
  effects, and the "leftover credit" chip text changes to say the machine will pay it out:
  - **Record as sale** records the row as today, then sends `cash_sale(cash share of the pending
    sale, None)` so the board's escrow drops by the price the customer spent.
  - **Discard** sends nothing for the sale itself.
  - Either action clears PAY-104, which releases the armed reconciliation; any cash still in the
    board's escrow beyond what the edge holds (the leftover credit) is then paid out as `error`.

## 6. Web

- **Tests level:** `TESTABLE_COMMANDS["mdb"] = payment.MDB_TESTABLE_COMMANDS` (standard +
  `device_reset`, `payout`). `_EXPECTED_CONTRACT_VERSION["mdb"] = payment.CONTRACT_VERSION`.
  `_params_widget`/`_parse_command_params` gain `device_reset` (a device select: coin, bill) and
  `payout` (no input: the amount is fixed server-side to the smallest coin in
  `policy.coin_enabled`, reason `admin`, so a test can never empty the tubes).
- **`/health/subsystems/mdb`:** the board declares no channels, so the live window reads "Nothing
  declared by this board." A new partial, `partials/payment_state.html`, rendered under it from
  `MdbGateway.state`, shows accepting / inhibited reason, policy id, escrow, bill in escrow, each
  device's status, model and level, the tube table, stacker count and exact-change flag. It reuses
  existing Tailwind classes only; `tests/ui/test_static_css.py` enforces that.
- `/settings/payments` shows `payment.mdb.policy` read-only (as the MDB section is today).
- Every `cmd/payment/enable` mention in availability's `command_inhibited` becomes `set_policy`; the
  vending simulator stops advertising `payment/enable`.

## 7. Legacy removal

- `ice_contracts.board.vending_machine` **2.4.0 → 3.0.0** (major): remove `PaymentRefundCommand`,
  `PaymentRefundResult`, `RefundStatus`; from `mqtt_messages` remove `PaymentEvent`, `PaymentStatus`,
  `PaymentEnableCommand` and `VMCStatus.credit_escrow` (the deprecated float spec A kept). Topics
  `payment/credit`, `payment/status`, `cmd/payment/enable`, `cmd/payment/refund`,
  `cmd/payment/refund/ack` are removed from `CONTRACT.md`, `SUBSCRIPTIONS`, the generator and the
  schemas. `docs/contracts/vending-machine/CONTRACT.md` gets the 3.0.0 entry.
- Edge deletions: `VMC.on_payment_credit`, `on_refund_ack`, `TelemetryRouter.handle_payment_status`
  and `subscribe_payment_status`, `HealthTelemetry.on_payment_status`,
  `Availability.set_payment_device`, `StatusOutputs.publish_payment_enable`/`publish_refund`,
  `ice_edge/payment/refund_protocol.py`, `EventRecorder`'s `payment/credit` handler.
- `docs/contracts/payment/CONTRACT.md` "Testable set" paragraph: the flip has happened.
- `CLAUDE.md` sections that describe the refund protocol, `cmd/payment/enable`, the `mdb` simulator
  and the `tests/firmware/mdb/` skip are updated.

## 8. Error handling summary

| Condition | Behaviour |
|---|---|
| Duplicate `event_id` / `seq <= cursor` | re-ack, no effect |
| `seq` gap, unseen `boot_id` with `seq > 1`, unknown kind, malformed body | PAY-105, ack, keep going |
| Cursor write fails | DATA-104, ack anyway |
| Cursor file corrupt at boot | rename aside, PAY-105 |
| Board offline (PAY-101) or policy not acked | payouts and `cash_sale` held in order; deposits still credited if they arrive |
| Deposit while `payment_enabled` is false (a race with `set_policy accept:false`) | unchanged from today: paid out with `maintenance` during a lease, otherwise credited with a journal line |
| `payout` `partial` / `failed` / completion timeout | PAY-103 with the amounts; no new-id retry |
| `payout` rejected `no_policy` / `expired` | requeued after the next acked policy |
| `cash_sale` clamped | warning, PAY-104 `escrow_mismatch:cash_sale_clamped` |
| Board escrow > edge cash escrow at reconciliation (PAY-104 not active) | payout `error` of the difference |
| Reconciliation armed while PAY-104 is active | waits; runs after Record as sale / Discard |
| Board escrow < edge cash escrow at reconciliation | PAY-104 `escrow_mismatch` |
| Coin/bill device fault | PAY-106 / PAY-107; `cash_accepting` fails only when no cash device is usable |

`PAYMENT_BLOCKING_FAULTS` is unchanged (seven codes); none of PAY-103..PAY-107 blocks payment.

## 9. Testing

- **Simulator unit tests** (`tests/sims/mdb/`): tube math including `partial` and `changer_empty`;
  escrow cap and exact-change bill returns; `no_policy` rejection; expiry; idempotent payout replay;
  `cash_sale` clamp; edge-silence fail-safe; event log ack removal and ordered replay across a
  `board_reboot`; capabilities exactly 11 commands and no channels.
- **Conformance suite against the simulator:** `tests/firmware/mdb/conftest.py` gains a sim target
  (an in-process `MdbSimulator` on a thread under a fresh machine id, mirroring the vending `target`
  fixture); with `ICE_COLDER_BOARD_TESTS=1` and no `ICE_COLDER_BOARD_MACHINE_ID` the suite now runs
  against it instead of skipping, and CI runs it. The `mdb_board_needs_hardware` reason is removed from
  `tests/skip_policy.py`, since no MDB conformance test needs hardware any more.
- **Edge unit tests** (`tests/edge/payment/mdb/`, `FakeTaskRunner`, a fake dispatcher): duplicate and
  replay re-ack without credit; replay after a simulated restart (cursor file and snapshot credits
  both seed dedup); gap, unseen boot, unknown kind → PAY-105; ack published only after the cursor
  write; cursor write failure → DATA-104 and ack; corrupt cursor file; `PolicySync` send triggers and
  `policy_acked` only after the state echo (an `ok "queued"` ack alone leaves it false); keepalive
  cadence; payout queue ordering, hold while offline, an `ok "queued"` ack still waiting for
  `payout_result`, `busy` requeue, `ok`/`partial`/`scaling`/`failed`/timeout/`no_policy`; `cash_sale`
  after dispense with the cash share only, clamped → PAY-104; reconciliation's three branches and its
  idle/PAY-104/caught-up/empty-queue guards, including the crash-after-dispense case (uncertain
  snapshot, board escrow holding the price: no payout until the operator acts); Record as sale sends
  `cash_sale`, after which the armed reconciliation pays out only the leftover; PAY-106/107 raise and
  clear; per-device rows and the derived `cash_accepting` row.
- **VMC tests:** change paid out immediately after a vend with overpayment (replaces the M1 leftover
  timer test); `complete_transaction` always reaches `idle`; lever during `dispensing` ignored;
  `vend_failed` payout reason; a test sale never calls `payments`.
- **Integration:** `tests/integration/test_integration_e2e.py` publishes `PaymentEventEnvelope`s on
  `payment/event` instead of `PaymentEvent` on `payment/credit`, and asserts `cmd/mdb` `payout` /
  `set_policy` instead of `cmd/payment/refund` / `cmd/payment/enable`. One new test runs the real
  `MdbSimulator` and the edge against mosquitto: policy acked, coins inserted for a selection, vend,
  change paid, `cash_sale` sent, board `escrow_cents` back to 0, no faults.
- **Ratchets:** a meta test fails if any `ice_edge` or `ice_sims` module imports a removed legacy
  payment name; the test-isolation allowlists stay empty.
- **Drift:** regenerated vending 3.0.0 schemas; payment goldens unchanged.

## 10. Rollout

- Build order inside the plan: dispatcher `complete()` → simulator → edge adapter → VMC wiring →
  legacy removal → web. The simulator and edge land on one branch and merge together.
- No data migration beyond spec A; `payment.mdb.policy` fills from defaults; the cursor file is
  created on first ack.
- The fleet containers and the hpz440 sim stack pick it up with the next image; the cloud is
  unaffected (no cloud contract change).

## 11. Deviations from the approved brainstorm

- **Cursor file, not the session snapshot** (§5.1.1): `session.json` is deleted when a sale closes,
  so it cannot hold a cursor that must survive idle. Each snapshot `Credit` carries its `event_id`
  instead, so the snapshot still covers the crash window between the two writes.
- **Availability rows** (§5.5): the brainstorm showed one row per device. The spec keeps the
  per-device rows the contract fixes, as `alert` rows, and adds the derived `cash_accepting`
  `fulfillment` row, so one jammed device does not stop sales the other device can still take.
- **Reconciliation waits for PAY-104** and the PAY-104 actions now drive the board (§5.5), found in
  review: without the guard a crash after dispense would refund a delivered product.
- **Fresh `request_id` after a `rejected` ack, only if the item was never possibly delivered** (found in
  implementation and review): the firmware caches every ack by `request_id` in RAM (`comms_task.cpp`),
  rejections included, so a retry of a rejected id replays the rejection forever, and the item is
  re-minted. But `Master::do_payout` checks `expired`, `no_policy` and `cents` *before* the persisted
  `find_payout` replay, and a reboot clears the ack cache, so after a lost ack a rejection proves nothing
  about the earlier send. `PendingPayout.maybe_delivered` (persisted; set after a `CommandTimeout`, an
  unexpected send failure, and on every item reloaded from a snapshot) therefore forbids re-minting:
  such an item raises PAY-103 `unconfirmed:<cents>:<request_id>` (payout) or PAY-104
  `escrow_mismatch:cash_sale_unconfirmed` (`cash_sale`) and is dropped.
- **`expired` retries after 2 s** with a fresh id (never a delivered one), at most 5 attempts per item,
  then PAY-103 `expired:<cents>:<request_id>` / PAY-104 `escrow_mismatch:cash_sale_expired` and the item is dropped.
- **The board's escrow is per boot** (a correction of a spec premise): the firmware spec (§11, "Escrow
  restarts at 0 on every boot. It is RAM only.") means a rebooted board holds none of the cash the edge
  credited on its previous boot. Reconciliation (§5.5) compares `state.escrow_cents` only with edge cash
  credits whose `event_id` starts with `<state.boot_id>-`, and a sale's `cash_sale` carries only the cash
  consumed from current-boot credits (`SaleContext.pieces`, kept through a failed-vend restore);
  earlier-boot cash is paid back as change from the tubes (the board floors escrow at 0), so a reboot
  never raises PAY-104 `escrow_mismatch` by itself. The simulator now matches: `board_reboot` zeroes
  escrow, clears the RAM ack cache and drops an in-flight payout, and payout gates run before the replay.
- **`no_policy` rejection** makes `PolicySync` re-send `set_policy` and require a fresh `payment/state`
  echo before the queue resumes.
- **Uncertain-boot payouts are re-queued when PAY-104 is resolved**, with the same ids, before
  reconciliation is released, so the queue is non-idle when it evaluates.
- **Reconciliation waits for a `payment/state` newer than the last completed queue item**; an older
  retained state still shows the escrow from before that payout or `cash_sale`.
- **`DATA-104` has two sources** (the session snapshot and the payment cursor), owned by `Machine`;
  neither source can clear the other's alert.
- **`PolicySync` verifies with a 5 s `policy_verify` timer** after an `ok` ack: the policy is in force
  only once a `payment/state` from the acked boot echoes the `policy_id`, and the timer re-sends if none does.

- **Session evidence is frozen on held evidence, not on `PAY-104`** (final review I1): the edge raises
  `PAY-104` at runtime too (escrow mismatch, `cash_sale` outcomes), and freezing persistence on the fault
  silently dropped deposits and pending payouts while one stood, and its clearing deleted the live session
  file. `Machine.evidence_held` is set only by an uncertain boot snapshot and by `SaleRecordingFailed`;
  `StatusOutputs` freezes on it, and clearing `PAY-104` removes the session file only while it is held.
- **Boot `cash_sale` recovery on the closed path** (final review I3): when a boot snapshot's sale already has
  a `sales` row (so no `PAY-104`), `Machine` queues the snapshot's cash share as a `cash_sale` inside
  `set_session_store`, before edge-start reconciliation can evaluate. Without it, a crash between the
  `sales` insert and the `cash_sale` enqueue made reconciliation pay the delivered product's price back
  out. The snapshot has no event ids, so the full cash share is sent.
- **Re-queued boot `cash_sale`s rely only on the board's 32-entry RAM ack cache**: the firmware's
  `do_cash_sale` has no idempotency store (unlike `payout`), so a late duplicate is clamped by the board's
  escrow floor and the edge raises `PAY-104`. Visible to the operator, and it moves no money.
- **Unsent commands are not "maybe delivered"** (final round C): `CommandDispatcher.send` (broker down) and the
  not-yet-attached `_LateDispatcher` raise `CommandNotSent`, a `CommandTimeout` subclass, before anything is
  published; `PayoutProtocol` requeues the item under the same id without setting `maybe_delivered` and arms
  `payout_retry`, so a later `rejected` ack may still re-mint it.
- **A queued `cash_sale` carries the board boot its amount was computed for** (`PendingPayout.boot_id`,
  persisted, `None` for old snapshots or an unknown boot). If the board's current boot differs at send time the
  cash is gone from its RAM escrow, so the item is dropped with an info log (no fault); a payout is never dropped
  this way. Residual: if the board is "alive" before its new-boot `payment/state` arrives, the stale item is
  still sent and the board clamps it (visible `cash_sale_clamped` PAY-104).
- **Recovery cards need held evidence** (final round C): `pending_sale_for_recovery` and `recovery_detail` are
  gated on `evidence_held`, not on `PAY-104` being active, so a runtime `PAY-104` over a live dispensing
  snapshot never offers "Record as sale".
- **Known residuals:** the closed-path boot `cash_sale` can re-send one already published in the milliseconds
  between publish and the fire-and-forget snapshot write (visible clamp); a boot `PAY-104` freezes persistence
  of new sales until the operator resolves it (pre-existing); recovery `cash_sale`s (boot closed path, Record
  as sale) send the full cash share because the snapshot holds no pieces, so a board reboot in between yields a
  visible clamp.
- **Firmware recommendations (not implemented):** check the persisted payout result before the
  `expired`/`no_policy` gates, and do not cache rejections in the RAM ack cache. Either would let the edge
  drop the `maybe_delivered` PAY-103 fallback.

## 12. Out of scope

Cashless (next spec, including PAY-108 and the cashless availability row), virtual-gateway credit and
its refunds, bill recycler, revalue, DEX audit, editing the payment policy from the UI.
