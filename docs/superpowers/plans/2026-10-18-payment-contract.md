# Payment Contract (MDB board ↔ edge) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `ice_contracts.board.payment`, the normative MQTT contract between the MDB payment board and the edge, with generated schemas, a hand-written `CONTRACT.md`, the four new `PAY-105..108` fault codes, and the command-channel registrations the edge dispatcher reads.

**Architecture:** One new contract module holds every enum, event body, the sequenced event envelope and ack, the retained state doc, and the per-command param models. `ice_contracts.board.common` registers the payment commands' param validators and completion timeouts by lazy import, exactly as it does for `dispense`. `ice_contracts.board.vending_machine` gains the four fault codes as an additive 2.1.0 bump. **Nothing in `ice_edge` or `ice_sims` changes in this plan**: the legacy payment models stay importable (marked deprecated) until spec 3 retires their last consumer, and `TESTABLE_COMMANDS["mdb"]` flips to the new command set in spec 2 together with the simulator that advertises them, so the Tests tile never goes empty in between.

**Tech Stack:** Python 3.12, Pydantic v2 (`StrictInt`, discriminated unions), pytest, uv.

**Spec:** [`docs/superpowers/specs/2026-10-18-payment-contract-design.md`](../specs/2026-10-18-payment-contract-design.md). Section references (§) are to the spec.

**Plan style:** each task gives files, interfaces and the tests to write. Implementers write the code bodies, matching surrounding style (module docstring, `Field(..., description=...)` on every wire field, comments explaining *why* where the spec does).

## Global Constraints

- Every money amount is `StrictInt` cents. `Cents = Annotated[StrictInt, Field(ge=0)]`, `PositiveCents = Annotated[StrictInt, Field(gt=0)]`. A float (`1.0`) or numeric string (`"100"`) fails validation (§2.5). No `float` anywhere in `payment.py`.
- `payment.CONTRACT_VERSION == "1.0.0"`. `vending_machine.CONTRACT_VERSION` goes **2.0.0 → 2.1.0** (additive: four fault codes). The 3.0.0 removal of the legacy payment models is **deferred to spec 3** and recorded as such (Task 6). Ice-maker and edge-cloud contracts untouched.
- `PAYMENT_BLOCKING_FAULTS` stays **seven** codes; `PAY-105..108` are not members (§5, §10).
- `payment.py` imports only `ice_contracts.board.common` (never `vending_machine`, never anything above `ice_contracts`). `common.py` reaches `payment.py` only by lazy import inside a validator, as `_validate_dispense_params` does, so there is no import cycle. `uv run lint-imports` and `uv run python scripts/check_package_isolation.py` pass after every task.
- `TESTABLE_COMMANDS["mdb"]` is **unchanged** in this plan. The new set is published as `payment.MDB_TESTABLE_COMMANDS` for spec 2 to wire.
- Ids: `event_id` 8–64 chars, `boot_id` 8–32 chars, `request_id` 8–64 chars (matches `SubsystemCommand.request_id`), `session_id` 1–64 chars, `policy_id` 1–64 chars, `item` 1–64 chars.
- Timestamps use `_utc_now` from `common.py` as the default factory, like every other model.
- Run per task: `uv run pytest tests/contracts` while iterating, `uv run pytest` (whole suite) before each commit, then `uv run ruff check --fix .` and `uv run ruff format .` (always `uv run ruff`, never the global `ruff`).
- Regenerate schemas after any contract change: `uv run python -m ice_contracts.generate`; `tests/contracts/test_contract_schemas.py` fails on drift.
- Commit trailer on every commit: `Assisted-by: <model> (Claude Code)`. Commit from the worktree root; do not chain git commands with `&&`.

## File map

| File | Change |
|---|---|
| `packages/contracts/src/ice_contracts/board/payment.py` (new) | the whole payment contract: enums, event bodies, envelope, ack, state, command params, constants |
| `packages/contracts/src/ice_contracts/board/common.py` | `COMMAND_PARAM_VALIDATORS` + `COMPLETION_TIMEOUTS` entries for the payment commands |
| `packages/contracts/src/ice_contracts/board/vending_machine.py` | `PAY_105..PAY_108`, `FAULT_TABLE` rows, version 2.1.0, deprecation notes on the legacy payment models |
| `packages/contracts/src/ice_contracts/board/__init__.py` | docstring lists `payment` |
| `packages/contracts/src/ice_contracts/generate.py` | `PAYMENT_SCHEMA_DIR`, `PAYMENT_MODELS`, `CONTRACTS["payment"]` |
| `docs/contracts/payment/CONTRACT.md` (new, hand-written) + `docs/contracts/payment/schemas/*.json` (generated) | normative docs |
| `docs/contracts/vending-machine/CONTRACT.md`, `docs/contracts/vending-machine/schemas/fault_code.schema.json` | 2.1.0 section, deprecation pointers, regenerated enum |
| `tests/contracts/test_payment_contract.py` (new) | all payment-model tests |
| `tests/contracts/test_contracts.py` | `TestCompletionTimeouts` learns about long-running *control* commands |
| `tests/contracts/test_contracts_vending.py` | version pin 2.1.0, new fault rows |
| `CLAUDE.md`, `docs/superpowers/specs/2026-10-18-payment-contract-design.md` | docs and the two recorded deviations |

---

### Task 1: Enums, cents types, event bodies, envelope and ack

**Files:**
- Create: `packages/contracts/src/ice_contracts/board/payment.py`
- Create: `tests/contracts/test_payment_contract.py`

**Interfaces (produces):**

```python
CONTRACT_VERSION = "1.0.0"
PAYMENT_EVENT_TOPIC = "payment/event"          # relative to vmc/{machine_id}/
PAYMENT_EVENT_ACK_TOPIC = "payment/event/ack"
PAYMENT_STATE_TOPIC = "payment/state"

Cents = Annotated[StrictInt, Field(ge=0)]
PositiveCents = Annotated[StrictInt, Field(gt=0)]

class Device(str, Enum): coin, bill, cashless
class DeviceStatus(str, Enum): ready, disabled, busy, jammed, full, empty, tilt, offline, error
class CoinRoute(str, Enum): tubes, cashbox
class BillReturnReason(str, Enum): policy, edge_silent, customer, edge, timeout
class PayoutStatus(str, Enum): ok, partial, failed
class SessionEndReason(str, Enum): complete, cancelled, reader, edge, timeout

# Event bodies -- each has `kind: Literal["<name>"]` as its first field (§4 table, in table order)
class CoinAccepted(BaseModel):        kind; cents: PositiveCents; routed: CoinRoute
class BillEscrowed(BaseModel):        kind; cents: PositiveCents
class BillStacked(BaseModel):         kind; cents: PositiveCents
class BillReturned(BaseModel):        kind; cents: PositiveCents; reason: BillReturnReason
class EscrowReturnPressed(BaseModel): kind
class PayoutResult(BaseModel):        kind; request_id: str; requested_cents: PositiveCents;
                                      paid_cents: Cents; status: PayoutStatus; detail: str | None = None
class SessionBegun(BaseModel):        kind; session_id: str; funds_cents: Cents | None
class VendApproved(BaseModel):        kind; session_id: str; request_id: str; cents: PositiveCents
class VendDenied(BaseModel):          kind; session_id: str; request_id: str; reason: str
class SessionEnded(BaseModel):        kind; session_id: str; reason: SessionEndReason
class DeviceStatusEvent(BaseModel):   kind = "device_status"; device: Device; status: DeviceStatus;
                                      mdb_status: Annotated[StrictInt, Field(ge=0, le=255)] | None = None
class CashCollected(BaseModel):       kind; device: Device

PaymentEventBody = Annotated[Union[<all twelve>], Field(discriminator="kind")]
EVENT_KINDS: tuple[str, ...]          # the twelve kind strings, in §4 table order

class PaymentEventEnvelope(BaseModel):
    event_id: str (8–64); boot_id: str (8–32); seq: Annotated[StrictInt, Field(ge=1)]
    ts: datetime = Field(default_factory=_utc_now); event: PaymentEventBody

class PaymentEventAck(BaseModel):
    boot_id: str (8–32); seq: Annotated[StrictInt, Field(ge=1)]
    timestamp: datetime = Field(default_factory=_utc_now)
```

`PayoutResult` carries a `model_validator(mode="after")`: `ok` ⇒ `paid_cents == requested_cents`; `partial` ⇒ `0 <= paid_cents < requested_cents`; `failed` ⇒ `paid_cents == 0`. The error message is one line (the dispatcher's ack `detail` is one line; match `_validate_dispense_params`'s convention).

- [ ] **Step 1: Write failing tests** in `tests/contracts/test_payment_contract.py`:
  - `test_contract_version` — `CONTRACT_VERSION == "1.0.0"`.
  - `TestCents`: `CoinAccepted(kind="coin_accepted", cents=25, routed="tubes")` ok; `cents=25.0` raises `ValidationError`; `cents="25"` raises; `cents=0` raises (positive); `BillStacked(..., cents=-100)` raises.
  - `TestEventUnion`: parametrize over one minimal valid dict per `kind` (twelve cases) — `PaymentEventEnvelope.model_validate({...})` round-trips through `model_dump(mode="json")` and `model_validate_json` with `event.kind` preserved and every int still an `int`; an unknown `kind` (`"coin_rejected"`) raises; `EVENT_KINDS` equals the twelve §4 names in order and equals `{m.model_fields["kind"].default for m in union members}` (so the tuple and the union cannot drift).
  - `TestEnvelope`: `seq=0` raises; `seq=1.0` raises; `event_id` of 7 chars raises; `boot_id` of 33 chars raises; `ts` defaults to a tz-aware UTC datetime.
  - `TestPayoutResultConsistency`: `ok` with `paid != requested` raises; `partial` with `paid == requested` raises; `partial` with `paid == 0` is **valid** (nothing paid but the board could still try); `failed` with `paid > 0` raises; valid examples of all three pass. Each error message has no newline.
  - `TestEnums`: each enum's `{m.value for m in Enum}` equals the §4 literal set (`DeviceStatus` nine values, `BillReturnReason` five, `PayoutStatus` three, `SessionEndReason` five, `CoinRoute` two, `Device` three).
  - `TestAck`: valid; `seq=0` raises.
- [ ] **Step 2: Run** `uv run pytest tests/contracts/test_payment_contract.py -q` — fails with `ModuleNotFoundError`.
- [ ] **Step 3: Implement** `payment.py` up to and including `PaymentEventAck`. Module docstring: what the contract covers, which spec, the version rule (breaking = major), and that amounts are `StrictInt` cents with `currency` declared in `PaymentState`.
- [ ] **Step 4: Run** the file's tests — pass. Run `uv run pytest tests/contracts -q` — pass.
- [ ] **Step 5: Lint, then commit** `feat(contracts): payment event stream models (ice_contracts.board.payment)`.

---

### Task 2: `PaymentState` and its sub-models

**Files:**
- Modify: `packages/contracts/src/ice_contracts/board/payment.py`
- Test: `tests/contracts/test_payment_contract.py`

**Interfaces (produces):**

```python
class InhibitReason(str, Enum): no_policy, edge_silent, policy, device_fault

class TubeLevel(BaseModel):      cents: PositiveCents; count: Annotated[StrictInt, Field(ge=0)]; full: bool
class StackerState(BaseModel):   count: Annotated[StrictInt, Field(ge=0)]; full: bool
class CashlessSession(BaseModel): session_id: str; funds_cents: Cents | None; started_ts: datetime
class DeviceState(BaseModel):
    present: bool; status: DeviceStatus; level: Literal[1, 2, 3] | None = None
    model: str | None = None; serial: str | None = None
    denominations: list[PositiveCents] = []; enabled: list[PositiveCents] = []
    # validator: set(enabled) <= set(denominations)
class PaymentDevices(BaseModel): coin: DeviceState; bill: DeviceState; cashless: DeviceState
class PaymentState(BaseModel):
    boot_id: str (8–32); seq_last: Annotated[StrictInt, Field(ge=0)]
    currency: Literal["USD"] = "USD"
    policy_id: str | None; accepting: bool; inhibited_reason: InhibitReason | None
    escrow_cents: Cents; bill_in_escrow_cents: Cents | None
    devices: PaymentDevices; tubes: list[TubeLevel]; exact_change: bool
    stacker: StackerState | None; session: CashlessSession | None
    ts: datetime = Field(default_factory=_utc_now)
    # validator: accepting is True  <=> inhibited_reason is None
```

- [ ] **Step 1: Write failing tests** (add to the same file; a module-level `state_dict()` helper returns a full valid payload the tests mutate):
  - `TestPaymentState`: full payload validates and round-trips with ints intact; `currency="EUR"` raises; `accepting=True, inhibited_reason="policy"` raises; `accepting=False, inhibited_reason=None` raises; `escrow_cents=1.5` raises; `seq_last=0` valid (fresh boot, nothing sent yet).
  - `TestDeviceState`: `enabled=[500]` with `denominations=[100, 200]` raises; `level=4` raises; defaults (`level`, `model`, `serial` None; lists empty).
  - `TestTubeLevel`: `count=-1` raises; `cents=0` raises.
  - `TestInhibitReason`: values equal `{"no_policy", "edge_silent", "policy", "device_fault"}`.
- [ ] **Step 2: Run** — fail on import.
- [ ] **Step 3: Implement.** Comment on `PaymentState`: a retained doc never means the board is alive (heartbeats do), republished on change, on `force_report`, and every 30 s (`STATE_REPUBLISH_SECONDS = 30`, a module constant for spec 2's simulator).
- [ ] **Step 4: Run** `uv run pytest tests/contracts -q` — pass.
- [ ] **Step 5: Lint, commit** `feat(contracts): PaymentState retained state doc`.

---

### Task 3: Command param models and command-channel registration

**Files:**
- Modify: `packages/contracts/src/ice_contracts/board/payment.py`
- Modify: `packages/contracts/src/ice_contracts/board/common.py` (`COMMAND_PARAM_VALIDATORS` at ~127, `COMPLETION_TIMEOUTS` at ~284)
- Modify: `tests/contracts/test_contracts.py` (`TestCompletionTimeouts`, ~396–439)
- Test: `tests/contracts/test_payment_contract.py`

**Interfaces (produces, in `payment.py`):**

```python
class PayoutReason(str, Enum): change, session_timeout, cancel, vend_failed, maintenance, admin, error, customer
class VendOutcome(str, Enum): success, failure

class SetPolicyParams(BaseModel):
    policy_id: str (1–64); accept: bool; max_escrow_cents: PositiveCents
    coin_enabled: list[PositiveCents]; bill_enabled: list[PositiveCents]; cashless_enabled: bool
    change_threshold_cents: Cents; edge_timeout_s: Annotated[StrictInt, Field(ge=5, le=120)]
    return_bill_on_lever: bool
class PayoutParams(BaseModel):      cents: PositiveCents; reason: PayoutReason; sale_id: str | None = None
class VendRequestParams(BaseModel): session_id: str | None; cents: PositiveCents; item: str (1–64)
class VendResultParams(BaseModel):  session_id: str; request_id: str (8–64); outcome: VendOutcome
class EndSessionParams(BaseModel):  session_id: str
class DeviceResetParams(BaseModel): device: Device

PAYMENT_COMMANDS: tuple[str, ...] = ("set_policy", "payout", "vend_request", "vend_result",
                                     "end_session", "return_escrow", "device_reset")
PARAM_MODELS: dict[str, type[BaseModel]]   # every PAYMENT_COMMANDS entry except return_escrow (no params)
MDB_TESTABLE_COMMANDS: frozenset[str] = frozenset(STANDARD_COMMANDS) | {"device_reset", "payout"}
PAYOUT_COMPLETION_TIMEOUT_SECONDS = 60.0
VEND_REQUEST_COMPLETION_TIMEOUT_SECONDS = 45.0
DEFAULT_EDGE_TIMEOUT_SECONDS = 30
EDGE_IDLE_PING_SECONDS = 10
```

**In `common.py`:** one generic validator factory, `_payment_params_validator(command: str) -> Callable[[dict], None]`, which lazily imports `ice_contracts.board.payment.PARAM_MODELS`, validates `params` with the command's model, and re-raises the *first* Pydantic error as a one-line `ValueError(f"{command} params invalid: {loc} — {msg}")` (same shape as `_validate_dispense_params`'s fallback). `COMMAND_PARAM_VALIDATORS` gains one entry per `PARAM_MODELS` key (write them out explicitly; do not build the dict by iteration at import time, because that would import `payment` eagerly and create the cycle the lazy import exists to avoid). `COMPLETION_TIMEOUTS` gains `("mdb", "payout")` → constant 60 s and `("mdb", "vend_request")` → constant 45 s, both lazy-importing the constants from `payment`.

**`TestCompletionTimeouts` change:** today it asserts `set(COMPLETION_TIMEOUTS) == _LONG_RUNNING` and that every entry is testable. `vend_request` is long-running but a *control* command (never testable, §6), and `payout` only becomes testable in spec 2. Split the class's data into `_LONG_RUNNING_TESTABLE` (the existing three) and `_LONG_RUNNING_CONTROL = {("mdb", "payout"), ("mdb", "vend_request")}`; assert `set(COMPLETION_TIMEOUTS) == _LONG_RUNNING_TESTABLE | _LONG_RUNNING_CONTROL`, `all_testable == _IMMEDIATE | _LONG_RUNNING_TESTABLE` (unchanged meaning), and `_IMMEDIATE.isdisjoint(COMPLETION_TIMEOUTS)`. Update the class docstring: "every long-running *testable* command has an entry; a long-running control command may have one too (the dispatcher's `send_and_await_completion` reads this table for production commands as well, e.g. `dispense`)". Add `test_payout_and_vend_request_timeouts_are_fixed` (60.0 / 45.0 regardless of params).

- [ ] **Step 1: Write failing tests** in `test_payment_contract.py`:
  - `TestParamModels`: a valid `SetPolicyParams`; `edge_timeout_s=4` and `=121` raise; `edge_timeout_s=30.0` raises; `PayoutParams(cents=0, ...)` raises; `PayoutParams.reason` enum equals the eight §6 values; `VendRequestParams(session_id=None, cents=250, item="button_ice")` valid (price-first); `VendResultParams.outcome` equals `{"success", "failure"}`; `DeviceResetParams(device="hopper")` raises.
  - `TestCommandChannelRegistration`: for every key in `PARAM_MODELS`, `COMMAND_PARAM_VALIDATORS[key]` exists; `set(PARAM_MODELS) == set(PAYMENT_COMMANDS) - {"return_escrow"}`; `SubsystemCommand(request_id="r-12345678", command="payout", params={"cents": 1.0, "reason": "change"})` raises `ValidationError` whose message contains `"payout params invalid"` and has no newline; `SubsystemCommand(..., command="return_escrow", params={})` constructs; `SubsystemCommand(..., command="set_policy", params={})` raises (missing fields); a valid `payout` params dict constructs.
  - `TestTestableAndTimeouts`: `MDB_TESTABLE_COMMANDS == {"ping", "self_test", "force_report", "device_reset", "payout"}`; `COMPLETION_TIMEOUTS[("mdb", "payout")]({}) == 60.0`; `COMPLETION_TIMEOUTS[("mdb", "vend_request")]({"cents": 250}) == 45.0`; `("mdb", "vend_result")` and `("mdb", "set_policy")` are **not** in `COMPLETION_TIMEOUTS` (ack-only); `TESTABLE_COMMANDS["mdb"]` still contains `"bill_acceptor_test"` (the flip is spec 2's) — with a comment naming spec 2 so the assertion is deleted there, not "fixed".
  - Update `TestCompletionTimeouts` in `tests/contracts/test_contracts.py` as described above.
- [ ] **Step 2: Run** `uv run pytest tests/contracts -q` — new tests fail; the two modified `TestCompletionTimeouts` tests fail until `common.py` changes.
- [ ] **Step 3: Implement** the models and constants in `payment.py`, then the `common.py` registrations.
- [ ] **Step 4: Run** `uv run pytest -q` (whole suite: `tests/edge/hal/test_command_dispatcher.py` and `tests/edge/web/test_routes_tests.py` read these tables) — pass. `uv run lint-imports` and `uv run python scripts/check_package_isolation.py` — pass.
- [ ] **Step 5: Lint, commit** `feat(contracts): payment command params; register validators and completion timeouts`.

---

### Task 4: Fault codes `PAY-105..108`, vending contract 2.1.0, deprecation notes

**Files:**
- Modify: `packages/contracts/src/ice_contracts/board/vending_machine.py` (`FaultCode` ~95–129, `FAULT_TABLE` ~152–319, version comment block + `CONTRACT_VERSION` ~63, legacy payment models ~427–461)
- Modify: `tests/contracts/test_contracts_vending.py` (`test_contract_version` ~29–37; add fault tests)
- Regenerate: `docs/contracts/vending-machine/schemas/fault_code.schema.json`

**Produces:**

| Code | Severity | Scope | Description |
|---|---|---|---|
| `PAY_105 = "PAY-105"` | `warning` | `machine` | `Payment event gap; cash count may be off (board replay missed or unknown event kind)` |
| `PAY_106 = "PAY-106"` | `warning` | `machine` | `Coin changer fault` |
| `PAY_107 = "PAY-107"` | `warning` | `machine` | `Bill validator fault` |
| `PAY_108 = "PAY-108"` | `warning` | `machine` | `Cashless reader fault` |

Also: `CONTRACT_VERSION = "2.1.0"` with a comment block entry (`2.0.0 -> 2.1.0 (2026-10-18): minor bump, additive. PAY-105..108 for the payment contract (ice_contracts.board.payment, spec 2026-10-18-payment-contract-design.md). Not payment-blocking.`). On `PaymentRefundCommand`, `PaymentRefundResult`, `RefundStatus` (here) and `PaymentEvent`, `PaymentStatus`, `PaymentEnableCommand` (`mqtt_messages.py`): a leading docstring line `**Deprecated** (2026-10-18): superseded by ice_contracts.board.payment; removed, with a 3.0.0 bump, when ice_edge/ice_sims stop importing it (payment spec 3).` No code change to them.

- [ ] **Step 1: Write failing tests** in `test_contracts_vending.py`: `test_contract_version` pins `"2.1.0"` (extend its comment history); `test_payment_contract_faults_registered` — each of the four is in `FaultCode`, has a `FAULT_TABLE` row with `Severity.warning`/`Scope.machine`, and is absent from `PAYMENT_BLOCKING_FAULTS`; `test_payment_blocking_faults_still_seven` — `len(PAYMENT_BLOCKING_FAULTS) == 7` (if an equivalent assertion already exists elsewhere in the file, extend it rather than duplicating).
- [ ] **Step 2: Run** — fail.
- [ ] **Step 3: Implement**; run `uv run python -m ice_contracts.generate`.
- [ ] **Step 4: Run** `uv run pytest -q` — pass (`test_every_fault_code_has_a_table_entry`, the schema drift test, and any edge test enumerating `FaultCode` all green).
- [ ] **Step 5: Lint, commit** `feat(contracts): PAY-105..108 payment device faults; vending contract 2.1.0`.

---

### Task 5: Schema generation for the payment contract

**Files:**
- Modify: `packages/contracts/src/ice_contracts/generate.py` (imports ~13–36, `CONTRACTS` ~89–93)
- Modify: `packages/contracts/src/ice_contracts/board/__init__.py` (docstring)
- Create (generated): `docs/contracts/payment/schemas/*.schema.json`

**Produces:** `PAYMENT_SCHEMA_DIR = _REPO_ROOT / "docs/contracts/payment/schemas"` and

```python
PAYMENT_MODELS = {
    "payment_event_envelope": PaymentEventEnvelope,
    "payment_event_ack": PaymentEventAck,
    "payment_state": PaymentState,
    "set_policy_params": SetPolicyParams,
    "payout_params": PayoutParams,
    "vend_request_params": VendRequestParams,
    "vend_result_params": VendResultParams,
    "end_session_params": EndSessionParams,
    "device_reset_params": DeviceResetParams,
    "device_status": DeviceStatus,
    "payout_reason": PayoutReason,
}
CONTRACTS["payment"] = (PAYMENT_SCHEMA_DIR, PAYMENT_MODELS)
```

The envelope schema embeds all twelve event bodies via `$defs`, so they need no separate files. `test_contract_schemas.py` is parametrised over `CONTRACTS` and needs no edit.

- [ ] **Step 1: Write the failing test**: add to `test_payment_contract.py` — `test_generate_registers_payment_contract`: `"payment" in CONTRACTS`, `set(CONTRACTS["payment"][1]) == {the eleven keys}`, and the envelope schema's `$defs` contain every class in the union (compare against `EVENT_KINDS` length: exactly twelve body defs whose `properties.kind.const` values equal `set(EVENT_KINDS)`).
- [ ] **Step 2: Run** `uv run pytest tests/contracts/test_payment_contract.py tests/contracts/test_contract_schemas.py -q` — fail (`KeyError: 'payment'`, then "schema dir has exactly the expected files" once registered but not generated).
- [ ] **Step 3: Implement** `generate.py`; run `uv run python -m ice_contracts.generate`; confirm eleven files appear under `docs/contracts/payment/schemas/`; update the `__init__.py` docstring to list `payment`.
- [ ] **Step 4: Run** `uv run pytest tests/contracts -q` — pass.
- [ ] **Step 5: Lint, commit** `feat(contracts): generate payment contract schemas` (include the generated JSON).

---

### Task 6: Normative docs, CLAUDE.md, spec deviations

**Files:**
- Create: `docs/contracts/payment/CONTRACT.md`
- Modify: `docs/contracts/vending-machine/CONTRACT.md` (header version → 2.1.0; topic map rows 20–22; MDB-specific commands 96–100; a new "2.1.0" section)
- Modify: `CLAUDE.md` (Target layout `contracts/board/` line; Fault Codes section; Hardware/MDB mentions)
- Modify: `docs/superpowers/specs/2026-10-18-payment-contract-design.md` (§3)

**`docs/contracts/payment/CONTRACT.md`** follows the ice-maker CONTRACT.md's shape (identity & versioning, topic map, then sections). Contents, all drawn from the spec, no new rules:
1. Header `# Payment Contract — v1.0.0`; which Pydantic module generates `schemas/`; prose-vs-schema precedence sentence copied from the ice-maker doc.
2. Identity & versioning: semver, reported in `capabilities/mdb.contract_version`, minor = additive, major = breaking; edge accepts any `1.x`.
3. Topic map (spec §3 table) with each payload linked to its schema file.
4. Delivery (§3.1): `boot_id`/`seq`, flash log, cumulative ack, dedupe, `PAY-105` rule, unknown-kind rule.
5. Fail-safe on edge silence (§3.2), including `EDGE_IDLE_PING_SECONDS` and `DEFAULT_EDGE_TIMEOUT_SECONDS`.
6. Events (§4 table) and the credit-arithmetic paragraph; `PayoutResult` consistency rule from Task 1.
7. State (§5) with the field list and the edge-mapping bullets.
8. Commands (§6 table), `PayoutReason`, the seven board-side rules, the completion table rows (`payout` 60 s, `vend_request` 45 s, everything else ack-only), the idempotency requirement (same 32-id rule as the vending contract, by reference), and the note that `TESTABLE_COMMANDS["mdb"]` becomes `MDB_TESTABLE_COMMANDS` when the simulator ships (spec 2).
9. Reference flows (§7, all eight, numbered).
10. Error handling (§8 table).
11. Out of scope (§9).

**`docs/contracts/vending-machine/CONTRACT.md`:** title → v2.1.0; a "## 2.1.0 (2026-10-18, additive)" section listing `PAY-105..108`; in the topic map, the three payment rows (20–22) gain a leading "**Deprecated** — superseded by [`../payment/CONTRACT.md`](../payment/CONTRACT.md); removed in 3.0.0 once the edge and simulator migrate" and the MDB-specific commands section gets the same note. Also add rows for `payment/credit` and `payment/status` (today missing from the map entirely) marked deprecated the same way, so the doc finally lists every topic the gateway uses.

**`CLAUDE.md`:**
- Target layout: `board/` line adds `payment` to the module list.
- Fault Codes section: a paragraph — vending 2.0.0 → 2.1.0 adds `PAY-105` (payment event gap) and `PAY-106/107/108` (coin changer / bill validator / cashless reader fault), all machine-scope `warning`, not payment-blocking; `PAYMENT_BLOCKING_FAULTS` still seven. One sentence pointing at `ice_contracts.board.payment` 1.0.0 as the new MDB ↔ edge contract, with the legacy payment models deprecated and the 3.0.0 removal deferred to the edge-migration spec.
- Commands table: no change (`generate` already covers the new contract).

**Spec §3 amendment** (two deviations, recorded rather than silently drifted, in the style of CLAUDE.md's dispenser-profiles paragraph):
- "`CONTRACT.md` is hand-written and `schemas/` generated, matching the two existing contracts; `generate.py` does not emit Markdown."
- "The 3.0.0 removal of the legacy vending-contract payment models lands with spec 3 (edge migration), when their last importer disappears; this plan ships the additive 2.1.0 instead so `ice_edge` and `ice_sims` keep working unchanged in between. `TESTABLE_COMMANDS["mdb"]` likewise flips in spec 2 with the simulator that advertises the new commands."

- [ ] **Step 1:** Write `docs/contracts/payment/CONTRACT.md`. Cross-check every enum literal and number against `payment.py`, not from memory: run `uv run python -c "from ice_contracts.board import payment as p; print([e.value for e in p.DeviceStatus]); ..."` for each enum and paste.
- [ ] **Step 2:** Edit the vending `CONTRACT.md`, `CLAUDE.md`, and the spec as above.
- [ ] **Step 3: Verify** `uv run pytest -q` (whole suite; `tests/meta` has doc-shape tests), `uv run lint-imports`, `uv run python scripts/check_package_isolation.py`, `uv run ruff check .`, `uv run ruff format --check .` — all pass. `git status` shows only the intended files.
- [ ] **Step 4: Commit** `docs: payment contract 1.0.0 CONTRACT.md; vending 2.1.0 notes; CLAUDE.md`.

---

## Self-review (done while writing; recorded for the executor)

- **Spec coverage:** §2.5 → Task 1 (`StrictInt`); §3 topics/version → Tasks 1, 5, 6; §3.1 delivery → Tasks 1 (envelope/ack) and 6 (rules in prose; `PAY-105` in Task 4); §3.2 fail-safe → Task 3 constants + Task 6 prose; §4 events → Task 1; §5 state + faults → Tasks 2, 4; §6 commands/validators/timeouts/testable set → Task 3; §7–§9 → Task 6 prose; §10 tests → Tasks 1–5 (round-trip, union, enums, validators, timeouts, schemas, faults, isolation). The two items the plan does *not* do as literally written in the spec (3.0.0 removal; generated Markdown) are recorded in the spec itself by Task 6.
- **Type consistency:** `Cents`/`PositiveCents` are defined once in Task 1 and reused in Tasks 2–3; `Device`/`DeviceStatus` from Task 1 are reused by `DeviceState` (Task 2) and `DeviceResetParams` (Task 3); `PARAM_MODELS` keys (Task 3) are the `COMMAND_PARAM_VALIDATORS` keys and the `PAYMENT_MODELS` `*_params` entries (Task 5) minus `return_escrow`.
- **Nothing outside `ice_contracts`, `docs/` and `tests/contracts` changes.** Edge and simulator tests must stay green without edits; if one breaks, the cause is a `FaultCode` enumeration or a `COMPLETION_TIMEOUTS` assumption and belongs in this plan's Task 3/4 fix, not in an edge edit.
