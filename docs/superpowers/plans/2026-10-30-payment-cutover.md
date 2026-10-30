# Payment Cutover (MDB, cash) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move the MDB simulator and the edge onto `ice_contracts.board.payment` 1.1.0 for cash (coin changer and bill validator), and remove the legacy payment topics (vending contract 3.0.0).

**Architecture:**
- The simulator moves to a package, `ice_sims.mdb`, with pure models (cash, policy, event log) under a thin entry module.
- The edge gains `ice_edge.payment.mdb`, which has four collaborators (`EventIntake`, `PolicySync`, `PayoutProtocol`, `StateConsumer`) behind an `MdbGateway` facade.
- The VMC sees only `deposit_funds`, plus a `payments` Protocol offering `pay_out` and `cash_sale`.
- Build order: dispatcher → simulator → edge units → wiring/cutover → legacy removal → web → end-to-end.

**Tech Stack:** Python 3.12, asyncio, aiomqtt, Pydantic v2, pytest, uv, mosquitto (integration).

**Spec:** `docs/superpowers/specs/2026-10-30-payment-cutover-design.md` (spec B). Read the whole spec before your task. Section numbers (§n) refer to it.

**Normative wire inputs:**
- `docs/contracts/payment/CONTRACT.md`: contract 1.1.0, delivery rules, board-side rules 1–7, reference flows 1, 2, 6, 7, 8, and the "Edge mapping, fixed here" list.
- `docs/superpowers/specs/2026-10-18-mdb-board-firmware-design.md` §11, the as-built firmware rules.
- `packages/contracts/src/ice_contracts/board/payment.py`, the models. Use its names verbatim: `PaymentEventHeader`, `PaymentEventEnvelope`, `PaymentEventAck`, `PaymentState`, `SetPolicyParams`, `PayoutParams`, `CashSaleParams`, `DeviceResetParams`, `PayoutReason`, `PAYMENT_COMMANDS`, `MDB_TESTABLE_COMMANDS`, `PAYMENT_EVENT_TOPIC`, `PAYMENT_EVENT_ACK_TOPIC`, `PAYMENT_STATE_TOPIC`, `STATE_REPUBLISH_SECONDS`, `PAYOUT_COMPLETION_TIMEOUT_SECONDS`, `DEFAULT_EDGE_TIMEOUT_SECONDS`, `EDGE_IDLE_PING_SECONDS`.

## Global Constraints

- **All money is integer cents.** Spec A is merged on this branch, and its names are:
  - `ice_contracts.money` (`Cents`, `dollars_to_cents`, `format_cents`);
  - `EscrowLedger.total_cents`/`is_empty`;
  - `Credit.cents`;
  - `SessionSnapshot.credit_escrow_cents`;
  - `VMC.credit_escrow_cents`;
  - `record_sale(price_cents=…, methods: dict[str, int])`.

  No float money anywhere new.
- Run everything through `uv`: `uv run pytest`, `uv run ruff check --fix .`, `uv run ruff format <changed files only>` (a repo-wide format touches unrelated files, so revert those), `uv run lint-imports`, `uv run python scripts/check_package_isolation.py`.
- Never chain shell commands with `&&`.
- **Package rules:**
  - `ice_sims` never imports `ice_edge` or `ice_ui`.
  - Edge tests never import `ice_sims`, and simulator tests never import `ice_edge`. They meet only in `tests/integration/` and `tests/firmware/` (the conformance target).
  - `tests/meta/test_test_isolation.py` allowlists stay empty.
- **Edge layer contract** (`pyproject.toml`): `ice_edge.payment` sits in the same layer as `vend` and `reporting`, below `sale`. It may import `faults`, `notify`, `monitoring`, `access`, `ledger`, `hal`, `config` and `supervisor`, and never `ice_edge.sale`, `views`, `web` or `app`.
  - `ice_edge.sale.vmc` must not import `ice_edge.hal.command_dispatcher`, and it reaches payments only through a `Protocol` defined in `ice_edge.payment` (contract 5 stays unwidened).
- **Tests never sleep on the wall clock.** Use `tests/fakes.py` (`FakeTaskRunner`, `settle()`), label every new timer, and fire it by label. New timer labels: `payout_retry`, `policy_retry`, `mdb_keepalive`.
- **VMC public-surface rule:** tests and routes read collaborators but mutate only through VMC or Machine methods. `tests/meta/test_no_private_access.py` budgets only go down. The `tests/meta/test_vmc_complexity.py` budgets on `vmc.py` only go down; if a change grows `vmc.py`, move logic out to a collaborator.
- `PAYMENT_BLOCKING_FAULTS` stays at seven codes. None of PAY-103..PAY-107 blocks payment.
- `/screen` (`partials/screen_body.html`) stays byte-identical. No new Tailwind classes; `tests/ui/test_static_css.py` must pass.
- Customer messages that exist today stay byte-identical, apart from the new ones the spec names ("Bill held: $5.00", "Exact change only").
- Commit at the end of each task on the current branch. End every commit message with the trailer `Assisted-by: <model> (Claude Code)`, naming the model actually writing it, and add no `Co-Authored-By` line. Never stage `docs/project-org-chart.md`.
- **Full suite:** if `uv run pytest -q` is memory-killed, run it by directory (`tests/common tests/contracts tests/ui tests/meta tests/edge tests/cloud tests/sims tests/integration tests/docker tests/firmware`).
- **Conformance suite** (from Task 4 on): `ICE_COLDER_BOARD_TESTS=1 uv run pytest tests/firmware/mdb -q`. It needs a broker; `tests/firmware` documents a throwaway mosquitto.

---

### Task 1: `CommandDispatcher.complete()`

**Files:**
- Modify: `packages/edge/src/ice_edge/hal/command_dispatcher.py`
- Test: `tests/edge/hal/test_command_dispatcher.py` (extend the existing file)

**Interfaces:**
- Produces `CommandDispatcher.complete(request_id: str, result: CommandAck) -> None`, public:
  - It resolves a pending `send_and_await_completion` future.
  - If no future exists yet, it stores the result in the bounded early-completion buffer, exactly as `_resolve_completion` does today.
  - `_on_dispenser_report` is refactored to call `complete`, with no behaviour change.
  - Payment completions arrive as a `CommandAck` that the caller builds from a `payout_result` (status `completed`, `detail` = the payout status, plus `result` carrying `paid_cents`/`requested_cents`). Read the existing `CommandAck` fields first. If `CommandAck` cannot carry the payout body, `complete` takes `result: object`, and `send_and_await_completion` returns whatever was passed; state which in your report.
- Consumes nothing new.

- [ ] Step 1: Write the failing tests:
  - `complete` before the send resolves the later `send_and_await_completion` (early buffer).
  - `complete` after the ack resolves it.
  - An unknown `request_id` is buffered and evicted at the bound.
  - The existing dispenser-report tests still pass unchanged.
- [ ] Step 2: Run `uv run pytest tests/edge/hal -q` and see the new tests fail.
- [ ] Step 3: Implement it, then run `tests/edge/hal`, `tests/edge/vend` and `tests/edge/sale`. All pass.
- [ ] Step 4: Commit with the message `feat(hal): CommandDispatcher.complete for out-of-band completions`.

---

### Task 2: Simulator pure models (`ice_sims.mdb.cash`, `policy`, `event_log`)

**Files:**
- Create: `packages/sims/src/ice_sims/mdb/__init__.py`, `cash.py`, `policy.py`, `event_log.py`. These are pure: no MQTT, no asyncio, no clock reads. Time is passed in.
- Create the tests `tests/sims/mdb/__init__.py`, `test_cash.py`, `test_policy.py` and `test_event_log.py`.

**Interfaces (produced):**
- `cash.py`:
  - `Tube(cents: int, count: int, capacity: int)`.
  - `CashBox` holds:
    - tubes, defaulting to 5/10/25/100 with counts 40/40/40/0;
    - a stacker (count, capacity 500);
    - `bill_in_escrow_cents: int | None`;
    - `escrow_cents: int`.
  - `CashBox.accept_coin(cents) -> CoinRoute`, which tubes the coin if there is room and cashboxes it otherwise.
  - `CashBox.stack_bill(cents)`.
  - `CashBox.payout(cents) -> PayoutOutcome(status: PayoutStatus, paid_cents: int, detail: str | None)`:
    - It pays largest coin first, from tubes only.
    - It returns `partial` with the exact `paid_cents` on a shortfall, and `failed` when it pays nothing.
    - It decrements `escrow_cents` by `min(paid, escrow)`. A payout above escrow is allowed for an `admin`/`error` reason; follow the contract's payout rule text.
  - `CashBox.cash_sale(cents) -> tuple[int, bool]`. It returns `(applied_cents, clamped)`, where `clamped` means escrow was below `cents`, so escrow goes to 0.
  - `CashBox.tube_levels() -> list[TubeLevel]` and `stacker_state() -> StackerState`, both contract models.
- `policy.py`:
  - `PolicyState` holds the applied `SetPolicyParams | None` and `acked_in_boot: bool`.
  - `apply(params) -> None`.
  - `accepts_bill(cents, cash: CashBox) -> bool`, which applies the escrow cap and the exact-change bill rule (contract rule 2).
  - `accepts_coin(cents, cash) -> bool`.
  - `exact_change(cash) -> bool` compares the tubes against `change_threshold_cents`.
  - `inhibited_reason(...) -> InhibitReason | None`.
- `event_log.py`:
  - `EventLog(boot_id: str)` keeps `seq` from 1 and `next_event(kind, body: dict, ts) -> dict`, which returns the full envelope dict with `event_id = f"{boot_id}-{seq}"`.
  - The unacked log is bounded at 256. On overflow the oldest entry is dropped and the overflow is recorded.
  - `ack(boot_id, seq)` removes every entry at or below that seq for that boot (cumulative).
  - `unacked() -> list[dict]` returns entries in order.
  - `reboot(new_boot_id)`: the log survives and `seq` restarts at 1.
  - `EventLog.new_boot_id() -> str` returns 12 random chars.
  - Validate every produced envelope with `PaymentEventEnvelope` in the tests.

- [ ] Step 1: Write tests for:
  - tube math, largest-first;
  - `partial` with exact `paid_cents`;
  - `failed` on empty tubes;
  - the `cash_sale` clamp;
  - the escrow cap refusing a bill;
  - the exact-change bill refusal;
  - `exact_change` flipping at the threshold;
  - `inhibited_reason` as `no_policy` before `apply`;
  - event ids and seq;
  - cumulative ack;
  - the bounded log;
  - ordered replay across `reboot` (old-boot entries first, then new);
  - every envelope validating.
- [ ] Step 2: Run `uv run pytest tests/sims/mdb -q` and see the tests fail. Implement until they pass.
- [ ] Step 3: Run `uv run lint-imports` and `check_package_isolation`.
- [ ] Step 4: Commit with the message `feat(sims): MDB cash, policy and event-log models`.

---

### Task 3: Simulator runtime (`ice_sims.mdb.simulator`, `customer`, `faults`), thin entry, legacy topics gone

**Files:**
- Create: `packages/sims/src/ice_sims/mdb/simulator.py` (`MdbSimulator`), `customer.py` and `faults.py`.
- Rewrite: `packages/sims/src/ice_sims/mdb_gateway.py` as a thin entry module, modelled on `ice_sims/vending_machine.py`. The `[mdb] Connected` log line stays byte-identical, because the CI smoke step greps it. Keep the class name the entry exposes if `docker/machine/launcher.py` or compose refers to it; check both.
- Tests: rewrite `tests/sims/test_simulator_mdb.py` and add `tests/sims/mdb/test_simulator.py`. Add an `ast` test pinning the single import direction (`simulator` imports the others, and nothing imports `simulator`), mirroring the vending one. Update `tests/integration/test_simulator_commands.py` for the mdb rows.
- Base class: `packages/sims/src/ice_sims/base.py` (`ESP32Simulator`, which owns the standard commands, the 32-entry ack replay cache, capabilities and heartbeat). Reuse it.

**Interfaces / wire behaviour (§4, copied):**
- Capabilities:
  - `contract_version = payment.CONTRACT_VERSION`;
  - `commands` = the standard commands followed by `PAYMENT_COMMANDS`, 11 in total;
  - `channels = []`.
- `payment/state` is retained. It is republished on change, on `force_report`, and every `STATE_REPUBLISH_SECONDS`, with `devices.cashless.present = false`.
- Commands:
  - `set_policy` sets `acked_in_boot`.
  - `payout` is acked `accepted`, then a `payout_result` event is published after 0.5–2 s. A repeated `request_id` re-publishes the stored result and never pays twice.
  - `cash_sale` answers `ok` and `detail: "clamped"` when clamped.
  - `return_escrow` and `device_reset`.
  - `vend_request`, `vend_result` and `end_session` are acked `rejected` with `detail: "no_session"`.
  - Rule 1: `payout` with no policy in this boot is acked `rejected`, `detail: "no_policy"`.
  - Rule 4: expiry, per the contract.
- Edge silence: when no `cmd/mdb` arrives for `edge_timeout_s`, the sim disables, returns a held bill (`bill_returned{edge_silent}`) and reports `inhibited_reason: edge_silent` until a fresh acked `set_policy`.
- On (re)connect it republishes the whole unacked log in order before anything new, and consumes `payment/event/ack` (`PaymentEventAck`) into `EventLog.ack`.
- `customer.py`:
  - While the edge's retained `status` is `interacting_with_user` and the board is accepting, it inserts coins and bills toward the selected product's price.
  - About 5% of the time it overpays.
  - Occasionally it presses the lever (`escrow_return_pressed`).
  - It reads the price from the retained `status`'s selected product and the config file. Keep the existing read path and convert to cents; the price is already `price_cents` after spec A.
- `faults.py`: `InjectedFault` pairs `coin_jammed`, `bill_offline`, `changer_empty`, `board_reboot` and `drop_ack_window`, as described in §4.
- Home Assistant discovery: a `binary_sensor` per present device (`status == ready`), and `sensor`s for `escrow_cents` and `accepting`. The uptime sensor stays.
- Remove every legacy topic from the sim: `payment/credit`, `payment/status`, `cmd/payment/enable` and `cmd/payment/refund`.
- The simulator tests drive the sim with a fake MQTT client or the existing sim test harness in `tests/sims`. Copy the vending sim tests' pattern, with no wall-clock sleeps (use `queue.join()` / `settle`).

- [ ] Step 1: Write the sim tests:
  - capabilities (11 commands, no channels);
  - the state document validating as `PaymentState`;
  - payout `no_policy` before `set_policy`;
  - payout `accepted`, then a `payout_result`;
  - a repeated `request_id` not paying twice;
  - the `cash_sale` clamp;
  - the edge-silence fail-safe;
  - replay of the unacked log after reconnect and after `board_reboot`;
  - `drop_ack_window` causing replay;
  - `vend_request` rejected `no_session`;
  - `changer_empty` leading to a `partial`/`failed` payout;
  - the HA discovery entities.
- [ ] Step 2: Implement until `uv run pytest tests/sims tests/integration/test_simulator_commands.py tests/docker -q` passes.
- [ ] Step 3: Run the full suite, lint-imports and isolation.
- [ ] Step 4: Commit with the message `feat(sims): MDB simulator on payment contract 1.1.0`.

---

### Task 4: Conformance suite against the simulator

**Files:**
- Modify: `tests/firmware/mdb/conftest.py`. Add a sim target that runs an in-process `MdbSimulator` on a thread under a fresh machine id, mirroring the vending `target` fixture in `tests/firmware/conftest.py`. With `ICE_COLDER_BOARD_TESTS=1` and no `ICE_COLDER_BOARD_MACHINE_ID`, the suite runs against the simulator instead of skipping.
- Modify: `tests/skip_policy.py` to remove the `mdb_board_needs_hardware` reason.
- Cashless-only conformance tests (`tests/firmware/mdb/test_cashless.py`): a test that needs a cashless session the sim cannot open is skipped against the sim. It uses a marker mirroring the vending `requires_sim` mechanism, `requires_cashless_reader`, and its skip reason is registered in `skip_policy.py`. This is a plan-level deviation from §9, which says no MDB conformance test needs hardware; record it in the report. A test that only checks `vend_request` is rejected `no_session` must run against the sim.
- Modify: `CLAUDE.md`'s paragraph on `tests/firmware/mdb/`, and `docs/contracts/payment/CONTRACT.md`'s "Testable set" paragraph if it mentions the hardware-only skip.
- Fix any simulator gap the suite exposes, in `ice_sims.mdb`, with a sim unit test per fix.
- Check that `.github/workflows/ci.yml` already runs `tests/firmware` with the flag. If the mdb suite needs anything more, such as a broker, mirror the vending job.

- [ ] Step 1: Run `ICE_COLDER_BOARD_TESTS=1 uv run pytest tests/firmware/mdb -q` against the new target and list the failures.
- [ ] Step 2: Fix them in the simulator, with unit tests, until the whole mdb suite passes or is skipped only for `requires_cashless_reader`.
- [ ] Step 3: Run the full suite.
- [ ] Step 4: Commit with the message `test(firmware): MDB conformance suite runs against the simulator`.

---

### Task 5: Policy config and `EventIntake` with the cursor store

**Files:**
- Modify: `packages/common/src/ice_common/config_model.py`. Add `MdbPolicyConfig` with the defaults from §5.2 (`coin_enabled` [5, 10, 25, 100], `bill_enabled` [100, 500, 1000, 2000], `max_escrow_cents` 2000, `change_threshold_cents` 500, `edge_timeout_s` 30, `return_bill_on_lever` true). Add `MDBDevicesConfig.policy: MdbPolicyConfig = Field(default_factory=MdbPolicyConfig)`. Leave the existing fields alone. Missing keys fill from defaults.
- Create: `packages/edge/src/ice_edge/payment/mdb/__init__.py`, which is empty for now; Task 8 fills the facade.
- Create: `packages/edge/src/ice_edge/payment/mdb/intake.py` (`EventIntake`) and `packages/edge/src/ice_edge/ledger/payment_cursor.py` (`PaymentCursorStore`).
- Tests: `tests/edge/payment/mdb/__init__.py`, `test_intake.py` and `test_cursor_store.py`; `tests/common/test_config_model.py`.

**Interfaces (produced):**
- `PaymentCursorStore(path: Path = DATA_DIR / "payment_cursor.json")`:
  - `load() -> CursorState`. A missing file gives an empty state. A corrupt file is renamed `.corrupt-<ts>`, logged, and returns `CursorState(corrupt=True)`.
  - `save(state: CursorState) -> None` is atomic (tmp + rename).
  - `async save_async(state)` runs through `asyncio.to_thread`.
  - `CursorState` holds `seqs: dict[str, int]`, which keeps at most the current and previous `boot_id`; `recent_ids: deque[str]` (maxlen 64); and `corrupt: bool`.
- `EventIntake.__init__` takes these keyword arguments:
  - `cursor: PaymentCursorStore`;
  - `publish_ack: Callable[[PaymentEventAck], None]`;
  - `deposit: Callable[[int, str, str], None]`, as `(cents, method, event_id)`;
  - `persist_session: Callable[[], Awaitable[None]]`, which awaits the snapshot save the deposit caused;
  - `on_lever: Callable[[], None]`;
  - `on_payout_result: Callable[[str, PayoutResult], None]`, as `(request_id, body)`;
  - `on_device_status: Callable[[DeviceStatusEvent], None]`;
  - `message: Callable[[str], None]`;
  - `journal: Callable[[str, dict], None]`, for the bill_returned and cash_collected lines;
  - `record_event: Callable[[str, int, dict | None], None]`, as `(event_type, value, metadata)`;
  - `faults` (`FaultService`).
- Further methods:
  - `seed(snapshot_credit_event_ids: Iterable[str])` adds ids from a loaded `session.json` to the recent set at boot.
  - `async handle(topic: str, data: dict) -> None` is the `payment/event` handler and follows §5.1 steps 1–7 exactly.
  - Properties `caught_up_to(boot_id) -> int` and `last_processed_ts`.
- Methods: `coin_accepted` → `cash_coin`; `bill_stacked` → `cash_bill`. Both are `is_cash` already (verify).
- PAY-105 is raised once while active. Its outcome text counts gaps and unknown kinds. It is raised for a corrupt cursor at boot.
- A failed cursor save raises DATA-104, and the ack is still sent. Order: deposit, then `await persist_session()`, then `await cursor.save_async()`, then `publish_ack`.
- Out-of-order (lower seq, not a duplicate id) is treated as a duplicate.
- Cashless kinds are logged and ignored, then acked.
- Credit kinds record the `payment` row with `value` = cents.
- `escrow_return_pressed` calls `on_lever()`. The FSM-state check (ignored while `dispensing`) belongs to the VMC side of `on_lever`, which Task 9 wires.

- [ ] Step 1: Write the `PaymentCursorStore` tests: round-trip, missing, corrupt renamed aside, atomic write (no partial file on an exception mid-write).
- [ ] Step 2: Write the `EventIntake` tests, with fakes for every callable:
  - first event credited and acked;
  - duplicate `event_id` re-acked, not credited;
  - `seq <= cursor` re-acked;
  - gap → PAY-105 and processed;
  - unseen boot with `seq > 1` → PAY-105;
  - unknown kind → PAY-105 and acked;
  - malformed body → PAY-105 and acked;
  - invalid header → dropped, no ack;
  - the ack published only after the cursor save completes (assert ordering with a recording fake);
  - cursor save failure → DATA-104 and ack still sent;
  - replay after a restart: a new `EventIntake` over the same cursor file plus `seed()` from snapshot credit ids acks without crediting;
  - `bill_escrowed` → message "Bill held: $5.00";
  - lever → `on_lever`;
  - `payout_result` → `on_payout_result`;
  - `device_status` → `on_device_status`;
  - a cashless kind → ignored and acked;
  - `cash_collected` → event row only.
- [ ] Step 3: Config tests: defaults apply, and an old `config.json` without `policy` loads.
- [ ] Step 4: Implement until the tests pass. Then run lint-imports and the full `tests/edge tests/common` suites.
- [ ] Step 5: Commit with the message `feat(payment): EventIntake with persistent event cursor`.

---

### Task 6: `PolicySync` and the keepalive

**Files:**
- Create: `packages/edge/src/ice_edge/payment/mdb/policy.py`
- Test: `tests/edge/payment/mdb/test_policy_sync.py`

**Interfaces (produced):**
- `build_policy(cfg: MdbPolicyConfig, accept: bool) -> SetPolicyParams`:
  - `cashless_enabled` is `False`.
  - `policy_id` is the first 16 hex characters of the SHA-256 of the canonical JSON (sorted keys, no whitespace) of every param except `policy_id`.
- `PolicySync.__init__` takes keyword arguments:
  - `dispatcher` (an object with `async send(subsystem, command, params) -> CommandAck`);
  - `config: Callable[[], MdbPolicyConfig]`;
  - `payment_enabled: Callable[[], bool]`;
  - `tasks: TaskRunner`;
  - `board_alive: Callable[[], bool]`.
- Methods and properties:
  - `async sync(reason: str)` sends now.
  - `on_state(state: PaymentState)` re-sends when `boot_id` differs from the acked boot, or when `policy_id` is null or differs. It sets `policy_acked` when the state from the acked boot echoes the current `policy_id`.
  - `on_payment_enabled_changed(enabled: bool)` rebuilds and sends.
  - `on_connected()` sends once subscribed.
  - `policy_acked: bool`.
  - `subscribe_policy_acked(cb) -> unsubscribe`, which fires on the transitions false→true and true→false. `PayoutProtocol` uses it.
  - `note_command_sent()`, which any `cmd/mdb` send calls (or wrap the dispatcher, which is simpler). It resets the keepalive clock.
- `policy_acked` requires an `ok` ack **and** the state echo. A non-`ok` reply or a `CommandTimeout` re-sends after 5 s while `board_alive()`, on timer label `policy_retry`.
- Keepalive: send `ping` on `mdb` when no `cmd/mdb` has gone out for `EDGE_IDLE_PING_SECONDS`, on timer label `mdb_keepalive`, only while MQTT is connected (`on_connected` / `on_disconnected`).

- [ ] Step 1: Write the tests:
  - The policy id is deterministic and changes when any param changes.
  - `accept` follows `payment_enabled`.
  - A send happens on connect, on a new `boot_id`, on a null or different `state.policy_id`, and on a `payment_enabled` flip.
  - An `ok "queued"` ack alone leaves `policy_acked` false, and a subsequent state echo makes it true.
  - A state from an older `boot_id` does not count.
  - A timeout retries on `policy_retry` only while the board is alive.
  - The keepalive pings after 10 s of silence, not while disconnected, and is reset by any send.
- [ ] Step 2: Implement until the tests pass, then run lint-imports.
- [ ] Step 3: Commit with the message `feat(payment): PolicySync and cmd/mdb keepalive`.

---

### Task 7: `StateConsumer`, availability rows, PAY-106/107

**Files:**
- Create: `packages/edge/src/ice_edge/payment/mdb/state.py`
- Modify: `packages/edge/src/ice_edge/faults/availability.py`:
  - Replace the `payment_devices_ready` row with `payment_coin_ready` and `payment_bill_ready` (gate `alert`), plus `cash_accepting` (gate `fulfillment`).
  - Add `Availability.set_payment_state(state: PaymentState | None)`.
  - Keep `set_payment_device` until Task 10 deletes it, but stop using it in the new path.
- Tests: `tests/edge/payment/mdb/test_state_consumer.py` and `tests/edge/faults/test_availability*.py`, updated for the new row names.

**Interfaces (produced):**
- `StateConsumer.__init__` takes keyword arguments: `faults`, `availability: Callable[[], Availability | None]`, `policy: PolicySync`, `message: Callable[[str], None]`, `exact_change_message: str = "Exact change only"`.
- Methods and properties:
  - `async handle(topic, data)` validates `PaymentState`. Invalid input is logged and dropped.
  - `state: PaymentState | None` and `state_ts: float`.
  - `on_device_status(evt: DeviceStatusEvent)`.
  - `subscribe_state(cb) -> unsubscribe`.
- **Rows:**
  - A device row fails on `offline | error | tilt | jammed`. `full` and `empty` are alert-only in detail.
  - `cash_accepting` fails when no present cash device is usable, or when `inhibited_reason` is `device_fault` or `edge_silent`. `policy` and `no_policy` never fail it.
  - With no state yet, the rows show "no report yet", matching the existing row conventions.
- **Faults:**
  - PAY-106 is the coin changer and PAY-107 the bill validator.
  - Raise on `offline | error | tilt | jammed`.
  - Clear `by="auto"` on `ready | disabled | busy`.
  - Raising skips an active code; clearing skips an inactive one.
- **Exact change:** on the transition to true, call `message("Exact change only")`.

- [ ] Step 1: Write the tests:
  - Each device status maps to the right row result.
  - One jammed device leaves `cash_accepting` passing.
  - Both unusable fails it.
  - `inhibited_reason` `edge_silent` fails it, and `policy` does not.
  - PAY-106/107 raise and clear from both the state document and `device_status`.
  - The exact-change message fires once on a flip.
  - `PolicySync.on_state` is called.
- [ ] Step 2: Implement until the tests pass. Then run `tests/edge/faults`, `tests/edge/web` (health availability page) and `tests/cloud/test_ui_parity.py`.
- [ ] Step 3: Commit with the message `feat(payment): payment/state consumer, per-device availability, PAY-106/107`.

---

### Task 8: `PayoutProtocol`, `cash_sale`, the `MdbGateway` facade, reconciliation

**Files:**
- Create: `packages/edge/src/ice_edge/payment/mdb/payout.py` and `reconcile.py`.
- Fill: `packages/edge/src/ice_edge/payment/mdb/__init__.py` (`MdbGateway`).
- Create: `packages/edge/src/ice_edge/payment/ports.py`, which holds the `Payments` Protocol.
- Tests: `tests/edge/payment/mdb/test_payout.py`, `test_reconcile.py` and `test_gateway.py`.

**Interfaces (produced):**
- `ice_edge.payment.ports.Payments(Protocol)` has:
  - `pay_out(cents: int, reason: PayoutReason, sale_id: str | None) -> None`;
  - `cash_sale(cents: int, sale_id: str | None) -> None`;
  - `pending_payouts() -> list[PendingPayout]`.
- `PendingPayout` is a frozen dataclass with `request_id: str`, `kind: Literal["payout", "cash_sale"]`, `cents: int`, `reason: str | None` and `sale_id: str | None`.
- `PayoutProtocol.__init__` takes keyword arguments:
  - `dispatcher` (`send_and_await_completion`);
  - `policy_acked: Callable[[], bool]`;
  - `board_alive: Callable[[], bool]`;
  - `tasks`;
  - `faults`;
  - `on_paid: Callable[[PendingPayout, int], None]`, as `(item, paid_cents)`, where the VMC journals `refund_confirmed` and persists;
  - `on_changed: Callable[[], None]`, which persists the snapshot whenever the queue changes.
- The queue and its sending rules:
  - It is an ordered queue of `PendingPayout`, sent one at a time.
  - `request_id` is minted at enqueue time and never re-minted.
  - It holds while `not policy_acked()` or `not board_alive()`.
  - `kick()` is called on `policy_acked` true, on PAY-101 clearing, and after each completion.
- `payout` outcomes:
  - Any non-reject ack waits for completion.
  - `busy` requeues at the head after 2 s (`payout_retry`).
  - `rejected` with `no_policy`/`expired` goes back to the head and waits for the next `policy_acked`.
  - `ok` → `on_paid(item, paid)`.
  - `partial` (including `detail: "scaling"`) → `on_paid(item, paid)` plus PAY-103 `partial:<paid>/<requested>`.
  - `failed` → PAY-103 `failed:<detail>`.
  - `CompletionTimeout` (`PAYOUT_COMPLETION_TIMEOUT_SECONDS`) → PAY-103 `unconfirmed`.
  - It never retries with a new id.
- `cash_sale` goes through the same queue. An `ok` counts as delivered, including `detail: "queued"`. On `detail: "clamped"` it logs a warning and raises PAY-104 `escrow_mismatch:cash_sale_clamped`.
- Properties: `idle: bool` (the queue is empty and nothing is in flight), and `pending() -> list[PendingPayout]` (in-flight first, then queued).
- `seed(pending: list[PendingPayout])` re-queues the payouts from a loaded snapshot. Do not call it at boot when PAY-104 is raised. A snapshot's pending payouts are PAY-104 evidence, so the operator decides. Document this in a test.
- `Reconciler.__init__` takes keyword arguments: `state: Callable[[], PaymentState | None]`, `intake: EventIntake`, `payouts: PayoutProtocol`, `fsm_idle: Callable[[], bool]`, `pay104_active: Callable[[], bool]`, `edge_cash_escrow: Callable[[], int]` and `faults`.
- `arm(trigger: str)`; `maybe_run()` is called on every state, event, FSM change and fault change.
  - It runs once per arm, when all the §5.5 guards hold.
  - When the escrows are equal it does nothing.
  - When the board is higher it calls `pay_out(diff, PayoutReason.error, None)`.
  - When the board is lower it raises PAY-104 `escrow_mismatch:<edge>/<board>`.
  - A PAY-104-blocked arm stays armed.
- `MdbGateway` composes `EventIntake`, `PolicySync`, `StateConsumer`, `PayoutProtocol` and `Reconciler`, and implements `Payments`.
  - Read-only: `state`, `policy_acked`.
  - It exposes `intake`, `policy`, `state_consumer`, `payouts` and `reconciler` as read-only properties for wiring and tests.

- [ ] Step 1: Write the payout tests:
  - FIFO ordering of a payout and a `cash_sale`.
  - Hold while the board is offline or the policy is not acked, and send in order after `kick`.
  - An `ok "queued"` ack still waits for completion.
  - `busy` requeues on `payout_retry`.
  - Each of `ok`, `partial`, `scaling`, `failed`, timeout and `no_policy`/`expired` maps as above.
  - Repeated sends reuse the same `request_id`.
  - `cash_sale` clamped raises PAY-104.
  - `on_changed` fires on every queue change.
- [ ] Step 2: Write the reconcile tests:
  - The three branches.
  - Each guard: not idle, PAY-104 active (including the crash-after-dispense case, where an uncertain snapshot and the board escrow holding the price produce no payout), intake not caught up to `seq_last`, queue not empty, and the state older than the last event.
  - Runs once per arm.
  - A PAY-104-blocked arm runs after PAY-104 clears.
  - A partial-shortfall retry on the next trigger.
- [ ] Step 3: Gateway test: construction with fakes, and that `pay_out` and `cash_sale` delegate.
- [ ] Step 4: Implement until the tests pass, then run lint-imports.
- [ ] Step 5: Commit with the message `feat(payment): PayoutProtocol, cash_sale, reconciliation, MdbGateway`.

---

### Task 9: Cutover: wire `MdbGateway` into `Machine` and the VMC

This is the behaviour change. The legacy inbound topics stop being consumed. The legacy models still exist until Task 10.

**Files:**
- Modify: `packages/edge/src/ice_edge/app/machine.py`:
  - Build `MdbGateway` with `CommandDispatcher` via `set_command_dispatcher`. Its order relative to the VMC is up to you, but the VMC gets `payments=` as a required keyword (a closure is acceptable).
  - Wire every callable listed in Tasks 5–8.
  - Arm reconciliation on edge start, MQTT reconnect (`FaultService.on_mqtt_connection`), PAY-101 clearing (`subscribe_fault_change`) and a new `boot_id`.
  - Seed intake dedup from the loaded snapshot's credit `event_id`s in `set_session_store`.
  - `Availability.republish` and `payment_enabled` changes go to `PolicySync.on_payment_enabled_changed`, replacing `outputs.publish_payment_enable`.
  - Delete the `RefundProtocol` construction and the `refunds` property (update its readers).
- Modify: `packages/edge/src/ice_edge/hal/mqtt_inbound.py` `SUBSCRIPTIONS`:
  - Remove `payment/credit`, `cmd/payment/refund/ack` and `payment/status`.
  - Add `("payment/event", "payments", "intake.handle")` and `("payment/state", "payments", "state_consumer.handle")`, or an equivalent the resolver supports. Extend `Machine.set_mqtt_client`'s owner resolution. `EventIntake` publishes acks on `PAYMENT_EVENT_ACK_TOPIC` via the MQTT client.
- Modify: `packages/edge/src/ice_edge/sale/vmc.py` per §3.1:
  - The `payments` collaborator.
  - `deposit_funds(cents, payment_method, *, event_id=None)`.
  - `request_refund(reason)` pays out only the cash-method portion through `payments.pay_out`. A non-cash credit is logged at error and raises PAY-103 `non_cash_refund`.
  - `on_complete_transaction` pays leftover escrow as change immediately and always ends in `idle`. Delete `_post_dispense_dest` and the post-vend `session_timer.arm()`.
  - After a recorded, non-test dispense, call `payments.cash_sale(sum of cash shares, sale.sale_id)` when the sum is greater than 0.
  - `_fail_vend` payout reason `vend_failed`, with the fault code in the journal line.
  - Lever: `on_payment_lever()` requests a refund with reason `customer`, unless the state is `dispensing`, in which case it is logged and ignored.
  - Delete `on_payment_credit`, `on_refund_ack`, `_refund_confirmed`/`_refund_failed` (replaced by `on_payout_paid(item, paid_cents)` and the PAY-103 path in `PayoutProtocol`).
  - `vmc.py` must stay within its complexity ratchet.
- Modify: `packages/edge/src/ice_edge/sale/sale_context.py` (`sale_id: str`, uuid4 hex minted with the context).
- Modify: `packages/edge/src/ice_edge/ledger/session_store.py`:
  - `Credit.event_id: str | None`.
  - `SessionSnapshot.pending_payouts: list[dict]` replaces `pending_refund_request_id`. A legacy file with `pending_refund_request_id` loads as one pending payout entry with unknown cents, or keep the field read-only for legacy. Choose, and test that a legacy file still yields PAY-104.
  - `is_open` is true while `pending_payouts` is non-empty.
- Modify: `packages/edge/src/ice_edge/ledger/session_recovery.py` and `RecoveryDetail` (pending payouts in place of the refund id).
- Modify: `packages/edge/src/ice_edge/web/routes/health/recovery.py` and `health_faults.html`:
  - "Record as sale" sends `cash_sale(cash share, None)` after recording.
  - "Discard" sends nothing.
  - The leftover-credit chip text says the machine will pay it out.
  - The pending-payout chip replaces the refund-id chip.
- Modify: `packages/edge/src/ice_edge/ledger/event_recorder.py` to remove the `payment/credit` handler. `EventIntake` now records `payment` rows.
- Modify: `packages/edge/src/ice_edge/sale/outputs.py` to remove `publish_payment_enable`/`publish_refund` and their callers.
- Tests:
  - `tests/edge/sale/*` (the change-paid-immediately test replaces the leftover-timer test; `complete_transaction` always ends in idle; lever during dispensing ignored; `vend_failed` reason; a test sale never calls `payments`; non-cash refund → PAY-103).
  - `tests/edge/app/*`, `tests/edge/ledger/*`, `tests/edge/hal/test_mqtt_inbound.py`, `tests/edge/payment/test_refund_protocol.py` (delete it), `tests/edge/web/*` recovery tests.
  - `tests/integration/test_integration_e2e.py`: publish `PaymentEventEnvelope`s on `payment/event`, and assert `cmd/mdb` `payout`/`set_policy`.
  - Use a fake `Payments` in VMC unit tests (add it to `tests/fakes.py`).
- Delete: `packages/edge/src/ice_edge/payment/refund_protocol.py`.

- [ ] Step 1: Write the new VMC and recovery tests listed above. Run them and see them fail.
- [ ] Step 2: Implement the wiring and VMC changes. Convert the existing tests that used `on_payment_credit` to `deposit_funds` (the unit level) or to publishing on `payment/event` (the integration level).
- [ ] Step 3: Run the full suite, lint-imports (contract 5 unchanged), the meta ratchets and `test_ui_parity`.
- [ ] Step 4: Commit with the message `feat(edge)!: sale FSM pays through MdbGateway; consume payment/event and payment/state`.

---

### Task 10: Legacy removal and vending contract 3.0.0

**Files:**
- Modify: `packages/contracts/src/ice_contracts/board/vending_machine.py` (`CONTRACT_VERSION = "3.0.0"`). Remove `PaymentRefundCommand`, `PaymentRefundResult` and `RefundStatus`.
- Modify: `packages/contracts/src/ice_contracts/board/mqtt_messages.py`. Remove `PaymentEvent`, `PaymentStatus`, `PaymentEnableCommand` and `VMCStatus.credit_escrow` (the deprecated float), and the publisher that fills it.
- Remove the topics `payment/credit`, `payment/status`, `cmd/payment/enable`, `cmd/payment/refund` and `cmd/payment/refund/ack` from the generator and from `docs/contracts/vending-machine/CONTRACT.md` (adding a 3.0.0 entry), then regenerate the schemas.
- Edge deletions remaining after Task 9:
  - `TelemetryRouter.handle_payment_status`/`subscribe_payment_status`;
  - `HealthTelemetry.on_payment_status`;
  - `Availability.set_payment_device`;
  - anything else that still names a removed model.
- The vending simulator stops advertising `payment/enable`. Check `ice_sims/vending/*` and the `TESTABLE_COMMANDS` entries.
- `Availability.command_inhibited`: `payment/enable` becomes `set_policy`. Update its callers and the subsystem window's `driven_by` usage.
- Create: `tests/meta/test_no_legacy_payment.py`, an `ast` walk over `packages/edge/src` and `packages/sims/src` that fails on any import of, or attribute named after, the removed names.
- `tests/meta/test_money_types.py`: remove the now-deleted legacy payment allowlist entries.
- `docs/contracts/payment/CONTRACT.md` "Testable set" paragraph: the flip has happened.

- [ ] Step 1: Write `test_no_legacy_payment.py` and see it fail on the current tree.
- [ ] Step 2: Delete, then fix every importer.
- [ ] Step 3: Regenerate with `uv run python -m ice_contracts.generate`. Commit the real changes and revert any CRLF-only noise (check with `git diff --ignore-cr-at-eol`).
- [ ] Step 4: Run the full suite, the conformance suite, lint-imports and isolation.
- [ ] Step 5: Commit with the message `feat(contracts)!: vending contract 3.0.0 removes legacy payment topics`.

---

### Task 11: Web: Tests level, payment state partial, settings

**Files:**
- Modify: `packages/contracts/src/ice_contracts/board/common.py`, setting `TESTABLE_COMMANDS["mdb"]` to `payment.MDB_TESTABLE_COMMANDS`. Check the import direction: `common` must not import `payment` if `payment` imports `common`. If it does, set it where `TESTABLE_COMMANDS` is consumed, and state that in the report.
- Modify: `packages/edge/src/ice_edge/web/routes/tests_level/*`:
  - `_EXPECTED_CONTRACT_VERSION["mdb"] = payment.CONTRACT_VERSION`.
  - `_params_widget`/`_parse_command_params` gain `device_reset` (a select of coin or bill) and `payout`, which has no input. The amount is fixed server-side to `min(policy.coin_enabled)` with reason `admin`. A client-sent amount is ignored. The allowlist re-check stays the security boundary.
- Create: `packages/ui/src/ice_ui/templates/partials/payment_state.html`. Render it under `/health/subsystems/mdb` from `MdbGateway.state`, showing:
  - accepting / inhibited reason;
  - policy id;
  - escrow (`|money`);
  - bill in escrow;
  - each device's status, model and level;
  - the tube table;
  - the stacker count;
  - the exact-change flag;
  - "No payment state yet." when it is `None`.

  Use existing Tailwind classes only. If the cloud portal renders `health_subsystem.html`, the partial must render with the state absent; check `test_ui_parity`.
- Modify: `/settings/payments` to show `payment.mdb.policy` read-only.
- Tests: `tests/edge/web/test_routes_tests*.py` (a payout test command sends the smallest coin with reason `admin`, ignoring a posted amount; `device_reset` validates the device; an unlisted command still gets 403), the subsystem window route tests, settings tests and `tests/ui/test_static_css.py`.

- [ ] Step 1: Write the failing route tests. Implement until they pass.
- [ ] Step 2: Run the full suite and `test_ui_parity`.
- [ ] Step 3: Commit with the message `feat(web): MDB test commands, payment state panel, policy settings`.

---

### Task 12: End-to-end against the real simulator, docs close-out

**Files:**
- Create: `tests/integration/test_mdb_cash_sale_e2e.py`. It runs the real `MdbSimulator` and the edge (`Machine` plus `CommandDispatcher` plus the MQTT client) against mosquitto, the same way the existing integration tests get a broker, and skips per their rule when none is available. Scenario:
  1. The policy is acked.
  2. A selection.
  3. Coins inserted, overpaying.
  4. The vend, completed through the vending path that the existing e2e test uses.
  5. Change paid.
  6. `cash_sale` sent.
  7. Board `escrow_cents` back to 0.
  8. No active PAY-10x faults.

  Drive the customer deterministically through the simulator's public methods, not the random `customer.py` loop.
- Modify: `CLAUDE.md`. Update the sections on:
  - the refund protocol (now `PayoutProtocol` in `ice_edge.payment.mdb`);
  - `cmd/payment/enable` (now `set_policy`);
  - `StatusOutputs` methods;
  - `SUBSCRIPTIONS` owners;
  - the `mdb` simulator package;
  - the `tests/firmware/mdb/` skip text;
  - the vending contract 3.0.0 paragraph;
  - the availability rows;
  - the `Machine` collaborator list.

  Verify each sentence against the code.
- Modify: `TODO.md` to mark M3 done.

- [ ] Step 1: Write the e2e test and make it pass. Fix in the owning unit with a unit test, not in the e2e test.
- [ ] Step 2: Update the docs.
- [ ] Step 3: Run the full gate: the full suite, the conformance suite, `uv run ruff check .`, lint-imports, isolation, and a generate with no diff (ignore CR noise).
- [ ] Step 4: Commit with the message `test(integration): MDB cash sale end to end; docs for the payment cutover`.
