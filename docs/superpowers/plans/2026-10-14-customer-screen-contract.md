# Customer Screen Contract Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the unused `cmd/display` mode push with the customer screen contract in
[`docs/superpowers/specs/2026-10-14-customer-screen-contract-design.md`](../specs/2026-10-14-customer-screen-contract-design.md):

- a retained `ScreenState` sent over MQTT and SSE;
- touch input with the same actions as the physical buttons;
- paired screen tokens;
- `SCR-101` as a payment-blocking fault;
- a browser kiosk as the first renderer.

**Architecture:**
- **Contract:** a new board contract module, `ice_contracts.board.customer_screen`.
- **Building the state:** a pure builder in `ice_edge.customer_ui` turns plain inputs into `ScreenState`.
  An assembler in `ice_edge.views` collects those inputs from the live machine.
- **Publishing:** a publisher in `ice_edge.customer_ui` sends one serialized byte string to retained MQTT
  and to every SSE subscriber.
- **Input:** one handler serves both transports.
- **Kiosk:** routes in `ice_edge.web.routes.kiosk` serve a vendored JS renderer from `ice_ui`.

**Tech Stack:**
- Python 3.12, Pydantic v2, FastAPI;
- `StreamingResponse` for SSE;
- loguru, aiomqtt, via the existing `ice_common.mqtt_client`;
- vanilla JS with no dependencies, plus Node-stdlib browser checks;
- pytest with `FakeTaskRunner`.

**Plan style:** each task gives interfaces, behavior and the tests to write first. A code block appears
only for a wire format or a non-obvious rule. Implementers write the bodies.

## Global Constraints

- Money in every screen document is **integer cents**, rounded half-up from the edge's floats at the
  boundary.
- `SCREEN_CONTRACT_VERSION = "0.1.0"`. The vending `CONTRACT_VERSION` goes `"1.0.0" → "2.0.0"`.
- `PAYMENT_BLOCKING_FAULTS` grows from 7 to **8** codes with `SCR-101`. `EXPECTED_SUBSYSTEMS` becomes
  `("vending", "mdb", "ice_maker", "screen")`.
- Screen liveness timeout: **30 s**, with a check interval of at most **10 s**. Every other subsystem keeps
  120 s.
- Timing:
  - kiosk heartbeat every **10 s**;
  - SSE keepalive every **15 s**;
  - the kiosk shows "Connecting…" after **20 s** of silence.
- Pairing code: **8 digits**, single use, valid **10 minutes**.
- Input dedupe: the last **32** `request_id`s.
- Kiosk presentation:
  - touch targets ≥ **48 px**, and WCAG 2.1 AA contrast;
  - no CDN;
  - copy only from `kiosk-strings.en.json`;
  - JS toggles the `hidden` attribute and clones `<template>`s; it never writes class strings (Tailwind
    scans only templates, `packages/ui/tailwind.config.js:20`).
- `tests/meta/test_vmc_complexity.py` budgets are full. Current: file **1005/1005**, class 687/689, largest
  method 52/54. **`packages/edge/src/ice_edge/sale/vmc.py` must not grow by a single line.** Every hook goes
  through `StatusOutputs`, `SaleJournal`, observers, or public reads.
- Layering is enforced (import-linter contract 4):
  - `ice_edge.customer_ui` sits below `sale`, `faults`, `monitoring`, `access` and `views`, so it may import
    those only under `TYPE_CHECKING`;
  - `ice_edge.views` may import `sale` and `faults`;
  - `ice_edge.web` may import everything except `app`.
- New test files have a private-access budget of 0 (`tests/private_access_budget.py`). Use public seams.
- Tests never wait on the wall clock. Use `FakeTaskRunner`, `settle()`, or an injected clock.
- Commands, per CLAUDE.md:
  - tests: `uv run pytest`;
  - lint: `ruff check --fix .` then `ruff format .`;
  - import rules: `uv run lint-imports` and `uv run python scripts/check_package_isolation.py`;
  - schemas: `uv run python -m ice_contracts.generate`;
  - Tailwind: `.tailwind/tailwindcss.exe -c packages/ui/tailwind.config.js -i packages/ui/tailwind.input.css -o packages/ui/src/ice_ui/static/app.css --minify`.
- Commit trailer: `Assisted-by: <model> (Claude Code)`. No `Co-Authored-By` line.
- Do not chain shell commands with `&&`.

## Prerequisites

- Merge branch `docs/gap-analysis-v2` (gap analysis v2.0) before Task 12. That task amends its §5.7.
- Work on a feature branch off `main`, for example `feat/customer-screen`.

## Resolutions of spec points against the code

The fact-finding pass found places where the spec meets code it did not anticipate. Each resolution keeps
the spec's intent. Record every one in CLAUDE.md in Task 12.

| # | Spec says | Code reality | Resolution |
|---|---|---|---|
| R1 | VMC call sites also pass a `MessageCode` (spec §2) | VMC file budget has 0 lines left | Codes come from three places. (a) The builder derives them from `phase` and `mode`. (b) `SaleJournal` emits *notices* through a new `notice` callable (the journal is not ratcheted). (c) The publisher subscribes to `vmc.subscribe_sale_settled` for `thank_you` and `take_product`. `vmc.py` is unchanged. |
| R2 | `select` refusals map to `sold_out`, `locked_out`, `insufficient_credit`, `busy` | `VMC.select_product(index)` returns `None`. Insufficient credit is not a refusal: the selection stands and `process_payment` asks for more. | The input handler pre-checks through public reads, in this order: `busy` (FSM `dispensing`), `locked_out` (`vmc.faults.is_locked`), `unavailable` (availability), `sold_out` (inventory). Then it calls `vmc.select_product(index)` via `vmc.find_product(sku)`. `insufficient_credit` stays in the enum, unused by 0.1.0 (reserved). The selection is acked `ok`, and the screen then shows `insert_more`. |
| R3 | `cancel` calls `request_refund(reason="customer")` | `request_refund` refunds but keeps the FSM state and selection; `cancel_sale()` sends the wrong customer message | `cancel` calls `vmc.request_refund(reason="customer")` only. The session timer returns the FSM to idle as it does today. `"customer"` is added to `PaymentRefundCommand.reason`'s description. |
| R4 | 30 s screen timeout; boot raise if never seen | `HealthMonitor` has one timeout, never treats a never-seen subsystem as stale, and uses `time.monotonic`/`asyncio.sleep` directly | Add `subsystem_timeouts: Mapping[str, float]`, `must_appear: frozenset[str]` (a boot grace equal to that subsystem's timeout), an injectable `clock`, and a public `check_once()`. When any override exists, the loop sleeps `min(check_interval, 10.0)`. |
| R5 | Major mismatch counts as offline, with the mismatch in the fault detail | Liveness clears `SCR-101` on every heartbeat | `Machine` wraps the liveness callback: for `screen`, `alive and screen_compatible`. `FaultService.on_subsystem_liveness` gains `outcome: str = "heartbeat_lost"`, so a mismatch raises with `outcome="contract_mismatch"`. A capabilities change re-evaluates. |
| R6 | `ScreenCapabilities` | `TelemetryRouter.handle_capabilities` always validates `SubsystemCapabilities` | `ScreenCapabilities` **subclasses** `SubsystemCapabilities`, adding `touch`, `inputs`, `locales` and `size_class`. The router gains a per-subsystem model map `{"screen": ScreenCapabilities}`. |
| R7 | The kiosk sends capabilities "with the first heartbeat" | There is no HTTP heartbeat model | `POST /kiosk/heartbeat` takes `KioskHeartbeat{heartbeat: SubsystemHeartbeat, capabilities: ScreenCapabilities \| null}`, defined in the contract. Capabilities are sent on the first beat and whenever they change. |
| R8 | No role holds `screen_view`/`screen_input` | `ROLE_PERMISSIONS[owner] = frozenset(Permission)`, pinned by `tests/edge/access/test_access.py:303` | Add `SCREEN_PERMISSIONS = frozenset({screen_view, screen_input})`. Owner becomes `frozenset(Permission) - SCREEN_PERMISSIONS`, and that test is updated. |
| R9 | Pairing stores a `screen` device | `Device` has no kind, and `prune_devices()` drops untrusted devices after 24 h | Add `Device.kind: Literal["browser", "screen"] = "browser"`, persisted. `prune_devices()` skips `kind == "screen"`. |
| R10 | SameSite=Strict cookie | `web_auth.set_cookie` hard-codes `samesite="lax"` | Add a `samesite: str = "lax"` keyword. The kiosk passes `"strict"`. |
| R11 | Kiosk works during setup mode | `attach_routes.access_gate` sends everything except `/setup*` and `/static/*` to `/setup` | Exempt the `/kiosk` prefix from the setup redirect. The routes still enforce their own gate (loopback or token). |
| R12 | `screen` in `EXPECTED_SUBSYSTEMS` | `tests/contracts/test_contracts.py:331` requires `set(TESTABLE_COMMANDS) == set(EXPECTED_SUBSYSTEMS)` | `TESTABLE_COMMANDS["screen"] = ()`. A kiosk has no MQTT command path, and the Tests page must render "no commands" for it. |
| R13 | `currency` in `ScreenState` | No currency setting exists | Add `PaymentConfig.currency: str = "USD"`, with pattern `^[A-Z]{3}$`. |

---

### Task 1: Screen contract module and schemas

**Model:** Sonnet.

**Files:**
- Create: `packages/contracts/src/ice_contracts/board/customer_screen.py`
- Create: `docs/contracts/customer-screen/CONTRACT.md`
- Create: `docs/contracts/customer-screen/schemas/*.schema.json` (generated)
- Modify: `packages/contracts/src/ice_contracts/generate.py` (add `CUSTOMER_SCREEN_SCHEMA_DIR`, `CUSTOMER_SCREEN_MODELS`, `CONTRACTS["customer-screen"]`)
- Modify: `packages/common/src/ice_common/config_model.py` (`PaymentConfig.currency`, R13)
- Test: `tests/contracts/test_customer_screen_contract.py`; `tests/contracts/test_contract_schemas.py` picks the new contract up automatically.

**Interfaces (produces):**
- `SCREEN_CONTRACT_VERSION: str = "0.1.0"`.
- Enums (`str, Enum`):
  - `ScreenMode`: `selling`, `out_of_service`, `setup`;
  - `ScreenPhase`: `idle`, `credit`, `selected`, `dispensing`, `refunding`, `error`;
  - `ProductReason`: `sold_out`, `locked_out`, `unavailable`;
  - `MessageCode`: the 16 codes in spec §3.3;
  - `InputType`: `select`, `cancel`;
  - `InputAckStatus`: `ok`, `rejected`, `unsupported`;
  - `InputRejectReason`: the 9 members in spec §3.4;
  - `RefundStatus`: `pending`, `done`, `failed`.
- Models (frozen, `extra="forbid"`):
  - `ScreenProduct`;
  - `ScreenSelection`;
  - `ScreenQr{gateway, url}`;
  - `ScreenPayment{accepting, qr}`;
  - `ScreenRefund{status, amount_cents}`;
  - `ScreenMessage{code, params}`;
  - `ScreenState` (spec §3.2 field list, exact names);
  - `ScreenSetup{code}`;
  - `ScreenInput{request_id, ts, type, sku}`;
  - `ScreenInputAck{request_id, status, reason}`;
  - `ScreenCapabilities(SubsystemCapabilities)` adding `touch: bool`, `inputs: list[InputType]`,
    `locales: list[str]` and `size_class: Literal["tablet"]` (R6);
  - `KioskHeartbeat{heartbeat: SubsystemHeartbeat, capabilities: ScreenCapabilities | None}` (R7).
- `MESSAGE_PARAMS: dict[MessageCode, type[BaseModel]]` maps each code to its params model. The
  no-parameter codes map to one shared empty model. `ScreenMessage` validates `params` against
  `MESSAGE_PARAMS[code]` in a model validator.
- `money_to_cents(amount: float) -> int` rounds half-up, using `Decimal(str(amount)).quantize(Decimal("1"), ROUND_HALF_UP)` on `amount * 100`.
- `ScreenInput` rules: `select` requires `sku`; `cancel` forbids it.
- `ScreenCapabilities` rules: with `touch=False`, `inputs` must be empty.
- `ScreenState.serialize() -> bytes` is the **single** serialization both transports use:
  `model_dump_json(exclude_none=False).encode()`.

Wire example (the canonical fixture in the contract tests and `CONTRACT.md`):

```json
{"contract":"0.1.0","boot_id":"…","seq":7,"ts":"2026-10-14T15:02:11.123Z","mode":"selling",
 "phase":"credit","currency":"USD","credit_cents":200,
 "products":[{"sku":"ICE-10","slot":1,"name":"10 lb ice","price_cents":300,"sellable":true,"reason":null}],
 "selection":null,"payment":{"accepting":true,"qr":null},"refund":null,
 "messages":[{"code":"insert_more","params":{"needed_cents":100}}]}
```

- [ ] **Step 1:** Write `tests/contracts/test_customer_screen_contract.py`. Cover:
  - The version constant.
  - Every `MessageCode` has a `MESSAGE_PARAMS` entry.
  - `insert_more` without `needed_cents` is invalid.
  - `select` without `sku` and `cancel` with `sku` are invalid.
  - `touch=False` with non-empty `inputs` is invalid.
  - A `ScreenCapabilities` validates as a `SubsystemCapabilities`.
  - `money_to_cents(2.675) == 268` and `money_to_cents(0.1 + 0.2) == 30`.
  - `serialize()` round-trips the fixture above.
  - Unknown extra fields are rejected on edge models.
- [ ] **Step 2:** Run `uv run pytest tests/contracts/test_customer_screen_contract.py -v`. Expect FAIL (import error).
- [ ] **Step 3:** Implement the module, register it in `generate.py`, add `currency`, run
  `uv run python -m ice_contracts.generate`, and write `CONTRACT.md`. Model `CONTRACT.md` on
  `docs/contracts/vending-machine/CONTRACT.md`: a topic map (spec §3.1), the decision table, the message
  code table, and extensibility rules (spec §3.6).
- [ ] **Step 4:** Run `uv run pytest tests/contracts -v`. Expect PASS, including the drift tests.
- [ ] **Step 5:** Commit: `feat(contracts): customer screen contract v0.1.0`.

---

### Task 2: Vending contract 2.0.0 (SCR-101, payment blocking, expected subsystem)

**Model:** Sonnet.

**Files:**
- Modify: `packages/contracts/src/ice_contracts/board/vending_machine.py`. Update:
  - the version-history comment block;
  - `CONTRACT_VERSION = "2.0.0"`;
  - the module docstring "(v2.0.0)";
  - `FaultCode.SCR_101`;
  - its `FAULT_TABLE` entry (`critical`, `Scope.machine`, "customer screen offline");
  - `PAYMENT_BLOCKING_FAULTS` (add `SCR_101`, extend the rationale comment);
  - `EXPECTED_SUBSYSTEMS` (add `"screen"`).
- Modify: `packages/contracts/src/ice_contracts/board/common.py` (`TESTABLE_COMMANDS["screen"] = ()`, R12).
- Modify: `docs/contracts/vending-machine/CONTRACT.md`. Bump the title to v2.0.0, add a `## Semantics fixed in 2.0.0 (breaking)` section listing the changes (the `cmd/display` removal lands in Task 6, same PR), and add `screen/*` rows to the topic map.
- Regenerate: `docs/contracts/vending-machine/schemas/fault_code.schema.json`.
- Modify pinned tests:
  - `tests/contracts/test_contracts_vending.py:34,197,200,221,227-238,272,318`;
  - `tests/edge/faults/test_availability.py:425,438-439,506`.

**Interfaces (produces):** `FaultCode.SCR_101` (`"SCR-101"`), member of `PAYMENT_BLOCKING_FAULTS`.

- [ ] **Step 1:** Update the pinned tests to the new values (8 codes, `"2.0.0"`, four subsystems). Add a
  test that `SCR_101` is machine scope, critical, and payment-blocking.
- [ ] **Step 2:** Run `uv run pytest tests/contracts tests/edge/faults -v`. Expect FAIL.
- [ ] **Step 3:** Make the contract edits and regenerate the schemas.
- [ ] **Step 4:** Run the full suite with `uv run pytest`. Every test that iterates `EXPECTED_SUBSYSTEMS` must pass:
  - Health subsystems;
  - the `/screen` owner page;
  - the Tests overview and commands.

  Fix any that need a `screen` row. The Tests page must show the screen card with no command buttons
  (R12). Expect PASS.
- [ ] **Step 5:** Commit: `feat(contracts): vending contract 2.0.0 adds SCR-101 and the screen subsystem`.

---

### Task 3: Screen liveness (HealthMonitor overrides, boot grace, SCR-101 wiring)

**Model:** Sonnet.

**Files:**
- Modify: `packages/edge/src/ice_edge/monitoring/health_monitor.py` (R4)
- Modify: `packages/edge/src/ice_edge/faults/fault_service.py`: add `_LIVENESS_FAULTS["screen"] = FaultCode.SCR_101`, and give `on_subsystem_liveness` its `outcome` keyword (R5).
- Modify: `packages/edge/src/ice_edge/app/machine.py`: `set_health_monitor` installs the screen-compat wrapper (R5); add `set_screen_compatible(ok: bool)`.
- Modify: `packages/edge/src/ice_edge/__main__.py`: `HealthMonitor(machine_id=..., subsystem_timeouts={"screen": 30.0}, must_appear=frozenset({"screen"}))`.
- Test: `tests/edge/monitoring/test_health_monitor_overrides.py`, `tests/edge/faults/test_screen_liveness.py`.

**Interfaces:**
- `HealthMonitor.__init__(..., subsystem_timeouts: Mapping[str, float] = {}, must_appear: frozenset[str] = frozenset(), clock: Callable[[], float] = time.monotonic)`:
  - `timeout_for(subsystem) -> float`;
  - `async check_once() -> None` (the public body of the old `_check`; `run()` calls it);
  - the boot reference is the clock value at construction.
- **Rule:** a `must_appear` subsystem never seen by `boot + timeout_for(name)` gets `_notify_liveness(name, False)` once. A later heartbeat notifies `True` as usual. `get_summary()` reports each subsystem's own timeout.
- `FaultService.on_subsystem_liveness(subsystem: str, alive: bool, *, outcome: str = "heartbeat_lost") -> None`.
- `Machine.set_screen_compatible(ok: bool) -> None`. It stores the flag and re-runs the liveness decision
  for `screen` with the last known heartbeat state. Incompatible plus alive raises `SCR-101` with
  `outcome="contract_mismatch"`. Task 7 calls this from the capabilities path.

- [ ] **Step 1:** Write the tests, using a fake clock list and `check_once()`; no sleeps:
  - The screen goes stale at 30 s while vending is still alive at 100 s.
  - `must_appear` raises at 30 s with no heartbeat, exactly once.
  - A heartbeat after that clears it.
  - Through a `machine_fake_time` `Machine` with the real `FaultService` and `Availability`:
    - liveness `False` for `screen` raises `SCR-101` and makes `availability.payment_enabled` false;
    - `True` clears it;
    - `set_screen_compatible(False)` while alive raises `SCR-101` with "contract_mismatch" in the active
      fault text, and `True` clears it.
  - Vending still maps to `COM-101` with its old outcome text.
- [ ] **Step 2:** Run `uv run pytest tests/edge/monitoring/test_health_monitor_overrides.py tests/edge/faults/test_screen_liveness.py -v`. Expect FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run `uv run pytest tests/edge/monitoring tests/edge/faults tests/edge/app -v`, then `uv run pytest`. Expect PASS.
- [ ] **Step 5:** Commit: `feat(edge): 30 s screen liveness with boot grace raises SCR-101`.

---

### Task 4: Pure screen-state builder

**Model:** Sonnet.

**Files:**
- Create: `packages/edge/src/ice_edge/customer_ui/screen_state.py`
- Test: `tests/edge/customer_ui/test_screen_state.py`; golden fixtures in `tests/edge/customer_ui/golden/*.json`.

**Interfaces (produces):**
- `@dataclass(frozen=True) class ScreenInputs`. Plain values only, no edge imports:
  - `setup_active: bool`;
  - `blocking_codes: frozenset[str]` (active payment-blocking codes);
  - `lease_held: bool`;
  - `fsm_state: str`;
  - `credit: float`;
  - `currency: str`;
  - `products: tuple[ProductView, ...]`;
  - `selection: tuple[str, float] | None`;
  - `payment_accepting: bool`;
  - `qr: tuple[str, str] | None`;
  - `refund: tuple[RefundStatus, float] | None`;
  - `notices: tuple[ScreenMessage, ...]`.
- `ProductView(sku, slot, name, price, sellable, reason: ProductReason | None)`.
- `build_screen_state(inputs: ScreenInputs, *, boot_id: str, seq: int, ts: datetime) -> ScreenState`.
- `content_key(state: ScreenState) -> bytes`: the serialization with `seq` and `ts` blanked, used for
  change detection.

**Rules:**
- **`mode`:** spec §5.2. `out_of_service` when any of these holds:
  - `blocking_codes - {"SCR-101"}` is non-empty;
  - `lease_held`;
  - `fsm_state == "error"`.
- **`phase`:** the spec §5.2 table, first match wins.
- **Messages:** notices first, then one phase/mode default.

  | Situation | Default message |
  |---|---|
  | `setup` / `out_of_service` | none / `out_of_service` |
  | not `payment_accepting` while selling | `payment_unavailable` |
  | idle | `welcome` |
  | credit | `select_product` |
  | selected with credit below price | `insert_more{needed_cents}` |
  | dispensing | `dispensing{sku}` |
  | refunding | `refunding{amount_cents}` |

  A notice with the same code as the default suppresses the duplicate.
- **QR:** `qr` becomes `payment.qr` only while `mode == selling`.
- **Money:** every amount goes through `money_to_cents`.

- [ ] **Step 1:** Write table-driven tests:
  - every `mode` × `phase` row;
  - each out-of-service cause, including a test sale (`lease_held=True`);
  - `SCR-101` alone does **not** give `out_of_service`;
  - notice ordering and dedupe;
  - cents rounding;
  - `content_key` equal across different `seq`/`ts`;
  - three golden JSON fixtures (idle selling, insert_more, out_of_service) compared byte-for-byte with
    `serialize()`.
- [ ] **Step 2:** Run `uv run pytest tests/edge/customer_ui/test_screen_state.py -v`. Expect FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run the same test file, plus `uv run lint-imports`. Expect PASS.
- [ ] **Step 5:** Commit: `feat(edge): pure ScreenState builder`.

---

### Task 5: Publisher, input assembly and sale hooks

**Model:** Sonnet.

**Files:**
- Create: `packages/edge/src/ice_edge/customer_ui/screen_publisher.py`
- Create: `packages/edge/src/ice_edge/views/screen.py`
- Modify: `packages/edge/src/ice_edge/sale/outputs.py`:
  - add `attach_screen(publisher)`, the `screen` property, `screen_changed()` and `notice(message: ScreenMessage)`;
  - `display`, `message`, `show_qr` and `refresh` each also call `screen_changed()`;
  - keep `attach_display` until Task 6.
- Modify: `packages/edge/src/ice_edge/sale/sale_journal.py`. Add a constructor keyword `notice: Callable[[ScreenMessage], None] | None = None`. Emit (R1):
  - `sold_out{sku}` from `sold_out`;
  - `product_unavailable{sku}` from `locked_out` and `unavailable`;
  - `insert_more{needed_cents}` from `payment_insufficient`;
  - `vend_failed_credit_restored{amount_cents}` from `vend_failed`;
  - `refunding{amount_cents}` from `refund_requested`;
  - `refund_done{amount_cents}` from `refund_confirmed`;
  - `refund_failed_contact_owner` from `refund_failed`.
- Modify: `packages/edge/src/ice_edge/faults/fault_service.py`: `push_active_faults()` also calls `self._outputs.screen_changed()`.
- Modify: `packages/edge/src/ice_edge/app/machine.py`. Pass `notice=self._outputs.notice` to `SaleJournal`. Add `set_screen_publisher(publisher)`, which:
  - attaches it to the outputs;
  - subscribes `vmc.subscribe_sale_settled` so that `"dispensed"` produces notices `take_product`, then `thank_you`;
  - sets the publisher's source to `lambda: gather_screen_inputs(self, publisher)`.
- Test: `tests/edge/customer_ui/test_screen_publisher.py`, `tests/edge/views/test_screen_inputs.py`, and additions to `tests/edge/sale/test_outputs.py` and `tests/edge/sale/test_sale_journal.py`.

**Interfaces (produces):**
- `ScreenPublisher(*, tasks, clock=datetime.now(UTC), boot_id: str | None = None)`. Members:
  - `set_source(fn: Callable[[], ScreenInputs])`;
  - `set_mqtt(client)`;
  - `changed() -> None` (coalesced: schedules at most one rebuild per loop turn via `tasks.fire_and_forget`);
  - `add_notice(message)`;
  - `clear_notices()`;
  - `current() -> ScreenState`, `current_bytes() -> bytes`;
  - `subscribe() -> asyncio.Queue[bytes]`, `unsubscribe(q)`;
  - `set_setup_code(code: str | None)`, `setup_code -> str | None`;
  - `notices -> tuple[ScreenMessage, ...]`;
  - `republish()`.
- **Rebuild:** `build_screen_state(source(), ...)`; skip if `content_key` is unchanged; otherwise
  `seq += 1`, publish `serialize()` to `screen/state` (retain, QoS 1), and `put_nowait` the same bytes on
  every subscriber queue. Each queue holds at most 8 items; on overflow, drop the oldest.
- **Notices:** cleared when `phase` becomes `idle` *after* having been non-idle, except `thank_you` and
  `take_product`, which survive exactly one idle rebuild.
- **Setup code:**
  - `set_setup_code(code)` publishes retained `screen/setup` as `ScreenSetup`;
  - `set_setup_code(None)` publishes an empty retained payload, then calls `changed()`.
- `gather_screen_inputs(machine: Machine, publisher: ScreenPublisher) -> ScreenInputs` (in `views`). `setup_active` is `publisher.setup_code is not None`. A pure read of:
  - `machine.vmc` (state, credit, sale, products);
  - `machine.faults.active_faults()` (filtered to `PAYMENT_BLOCKING_FAULTS`);
  - `machine.lease.hold`;
  - `machine.availability.payment_enabled` and `product_sellable`;
  - `machine.inventory`;
  - `machine.refunds.pending`;
  - the publisher's notices;
  - `config.payment.currency`.

  `refund` is `pending` while `refunds.pending` is non-empty; otherwise it comes from the latest
  `refund_done`/`refund_failed_contact_owner` notice.

- [ ] **Step 1:** Write the tests:
  - **Publisher**, with `FakeTaskRunner`, `FakeMqtt` and `settle()`:
    - two `changed()` calls in one turn publish once;
    - an unchanged state publishes nothing;
    - `seq` increments;
    - the MQTT payload equals the bytes put on a subscriber queue;
    - the queue overflow drops the oldest;
    - setup code set and clear (empty retained payload).
  - **Journal:** each listed method emits its notice; with no `notice`, behavior is unchanged.
  - **Outputs:** `message()` triggers `screen_changed()`.
  - **Assembler**, with a `machine_fake_time` sale:
    - deposit, then `phase=credit`;
    - select with low credit, then `insert_more`;
    - dispense success, then `take_product` and `thank_you`;
    - a held lease gives `out_of_service`.
  - `tests/meta/test_vmc_complexity.py` still passes.
- [ ] **Step 2:** Run `uv run pytest tests/edge/customer_ui tests/edge/views/test_screen_inputs.py tests/edge/sale -v`. Expect FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run `uv run pytest`, then `uv run lint-imports`. Expect PASS. `vmc.py` is unchanged: confirm `git diff --stat packages/edge/src/ice_edge/sale/vmc.py` is empty.
- [ ] **Step 5:** Commit: `feat(edge): ScreenPublisher with sale notices`.

---

### Task 6: Wire the publisher and remove DisplayController

**Model:** Sonnet.

**Files:**
- Modify: `packages/edge/src/ice_edge/__main__.py`:
  - build `ScreenPublisher`;
  - `set_mqtt`;
  - `machine.set_screen_publisher`;
  - `routes.set_screen_publisher`;
  - `publisher.republish()` in `_on_mqtt_connection` (replacing `display.republish()`).
- Modify: `packages/edge/src/ice_edge/web/context.py`:
  - global `screen_publisher` replaces `display_controller`;
  - `set_screen_publisher`;
  - `ensure_setup_mode()` uses `screen_publisher.set_setup_code(code)` / `set_setup_code(None)`, keeping its
    log-once fallback.
- Modify: `packages/edge/src/ice_edge/web/routes/__init__.py` (re-export), `packages/edge/src/ice_edge/web/routes/auth/setup.py:256-257`.
- Modify: `packages/edge/src/ice_edge/sale/outputs.py`: remove `attach_display`, `display_controller` and `display()`'s controller call. `display(state)` now only calls `screen_changed()`.
- Modify: `packages/edge/src/ice_edge/app/machine.py`: remove `set_display_controller` and the `display_controller` property.
- Delete: `packages/edge/src/ice_edge/customer_ui/display_controller.py`, `tests/edge/customer_ui/test_display_controller.py`.
- Modify: `packages/contracts/src/ice_contracts/board/mqtt_messages.py`: delete `DisplayMode` and `DisplayCommand`, and update the module docstring.
- Modify: `pyproject.toml`. In import-linter contract 5, replace `ice_edge.customer_ui.display_controller` with `ice_edge.customer_ui.screen_publisher`.
- Modify tests:
  - `tests/common/test_mqtt.py:25-26,85-86`;
  - `tests/edge/web/test_web_routes.py:12,1261-1263,1763-1781` (setup code now asserted on the publisher);
  - `tests/edge/sale/test_outputs.py:302-309`;
  - `tests/edge/app/test_machine.py:98`;
  - `tests/route_context.py:27` (`"screen_publisher"` in `GLOBALS`).
- Modify: `scripts/refactor/move_map.py:125-126,243` (drop the deleted module).

**Interfaces:** `context.screen_publisher: ScreenPublisher | None`; `context.set_screen_publisher(p)`.

- [ ] **Step 1:** Rewrite the setup-mode tests in `tests/edge/web/test_web_routes.py`:
  - first boot puts the code on `screen/setup` (via `FakeMqtt`) exactly once;
  - finishing setup publishes an empty retained `screen/setup`;
  - a reconnect republishes the code.

  Add a test that no module references `cmd/display`: grep `packages/` in the test, as
  `tests/meta` already does for similar guards.
- [ ] **Step 2:** Run `uv run pytest tests/edge/web/test_web_routes.py -k setup -v`. Expect FAIL.
- [ ] **Step 3:** Implement the wiring and deletions.
- [ ] **Step 4:** Run `uv run pytest`, then `uv run lint-imports`, then `uv run python scripts/check_package_isolation.py`. Expect PASS.
- [ ] **Step 5:** Commit: `refactor(edge): ScreenPublisher replaces DisplayController; cmd/display removed`.

---

### Task 7: Input handler, MQTT screen topics and capabilities

**Model:** Sonnet.

**Files:**
- Create: `packages/edge/src/ice_edge/customer_ui/screen_input.py`
- Modify: `packages/edge/src/ice_edge/hal/mqtt_inbound.py`:
  - `TelemetryRouter` uses a per-subsystem capabilities model map (R6) and calls `on_capabilities_validated(subsystem, caps)` for `screen` too;
  - add `("screen/input", "screen", "handle_mqtt")` to `SUBSCRIPTIONS`, with owner `"screen"` resolving to the input handler.
- Modify: `packages/edge/src/ice_edge/app/machine.py`:
  - build `ScreenInputHandler`, exposed as the read-only `screen_input` property;
  - `set_mqtt_client` resolves owner `"screen"`;
  - the capabilities callback dispatches `screen` to `screen_input.set_capabilities(caps)` and `set_screen_compatible(major(caps.contract_version) == major(SCREEN_CONTRACT_VERSION))`; other subsystems keep going to `gate.on_vending_capabilities`.
- Modify: `packages/contracts/src/ice_contracts/board/vending_machine.py`: add `"customer"` to the `PaymentRefundCommand.reason` description (R3).
- Test: `tests/edge/customer_ui/test_screen_input.py`, `tests/edge/hal/test_screen_topics.py`.

**Interfaces (produces):**
- `ScreenInputHandler(*, vmc, availability, inventory, screen_mode: Callable[[], ScreenMode], compatible: Callable[[], bool])`. `Machine` passes `screen_mode=lambda: publisher.current().mode` once a publisher is attached, and `selling` before that. The edge types are imported under `TYPE_CHECKING` only. Members:
  - `set_capabilities(caps: ScreenCapabilities | None)`;
  - `handle(raw: dict) -> ScreenInputAck | None`. It returns `None` only when the body has no usable `request_id`, and runs the spec §5.4 order with the R2/R3 pre-checks;
  - `async handle_mqtt(topic, data)`, which publishes the ack on `screen/input/ack`.
- The dedupe cache is an `OrderedDict` of 32 entries, `request_id → ack`. A replay returns the cached ack
  without calling the VMC.

- [ ] **Step 1:** Write tests for each spec §5.4 step in order, on a `machine_fake_time` machine:
  - invalid body;
  - duplicate replay: the VMC is not called twice, asserted via `vmc.state` and `credit_escrow` unchanged and the same ack;
  - `contract_mismatch`;
  - `not_declared` with `touch=False`;
  - `out_of_service` under a lease;
  - `unknown_sku`;
  - `busy` while dispensing;
  - `locked_out`;
  - `sold_out`;
  - a valid select reaching the same state as `on_button_press` with that product's index;
  - `cancel` with credit refunds with reason `"customer"` (assert on `vmc.refunds.pending`);
  - `cancel` with no credit is `no_credit`.

  Router tests:
  - a screen capabilities payload validates as `ScreenCapabilities` and reaches the handler;
  - a contract `"1.0.0"` makes the machine raise `SCR-101` `contract_mismatch` while heartbeats continue.
- [ ] **Step 2:** Run `uv run pytest tests/edge/customer_ui/test_screen_input.py tests/edge/hal/test_screen_topics.py -v`. Expect FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run `uv run pytest`, then `uv run lint-imports`. Expect PASS; `vmc.py` diff is empty.
- [ ] **Step 5:** Commit: `feat(edge): screen input handler and MQTT screen topics`.

---

### Task 8: Screen pairing and the screen principal

**Model:** Sonnet.

**Files:**
- Modify: `packages/contracts/src/ice_contracts/access.py`: add the permissions `screen_view` and `screen_input`, and `SCREEN_PERMISSIONS`; owner gets `frozenset(Permission) - SCREEN_PERMISSIONS` (R8).
- Modify: `packages/edge/src/ice_edge/access/access.py`:
  - `Device.kind` (R9), persisted in `_document`/`load`, defaulting `"browser"` for old files;
  - `prune_devices()` skips screens;
  - `issue_screen_pairing_code() -> str` (8 digits, 10-minute TTL, in-memory, single use, one live code at a time);
  - `redeem_screen_pairing(code: str, label: str) -> tuple[Device, str] | None`;
  - `screen_for_token(token) -> Device | None` (kind must be `"screen"`).
- Modify: `packages/edge/src/ice_edge/web/auth.py`:
  - `set_cookie(..., samesite: str = "lax")` (R10);
  - `SCREEN_COOKIE = "vmc_screen"`;
  - `@dataclass ScreenPrincipal(device: Device | None, loopback: bool)`;
  - dependency `require_screen(*permissions) -> ScreenPrincipal`: allow loopback (`is_loopback(request.client.host)`), or a valid screen token whose permissions include the requested ones; otherwise `_unauthenticated`. A user session never satisfies it.
- Modify: `packages/edge/src/ice_edge/web/routes/users/devices.py`. On `/devices`:
  - add a "Pair a screen" section showing the live code and its expiry;
  - add `POST /devices/screens/pair-code` (`manage_users` + `require_htmx`);
  - list screen devices with a Forget button (the existing forget route).
  - Logging: pairing-code issue and revoke log the acting user's id and name at INFO via loguru, and record an event row if `context.recorder` exists. Follow the `test_run` user-attribution pattern in `web/routes/tests_level/commands.py:295`.
- Modify: `packages/ui/src/ice_ui/templates/devices.html` (or its partial). Rebuild `app.css` if new classes are used.
- Modify: `tests/edge/access/test_access.py:303`.
- Test: `tests/edge/access/test_screen_pairing.py`, `tests/edge/web/test_screen_principal.py`.

- [ ] **Step 1:** Write tests:
  - a code is 8 digits;
  - it expires at 10 minutes (injected clock);
  - it is single use;
  - issuing a new code invalidates the old one;
  - redeem creates `kind="screen"`;
  - prune keeps screens;
  - an old `access.json` without `kind` loads as `"browser"`;
  - owner lacks both screen permissions;
  - `require_screen` accepts a screen token, rejects a user session cookie, and rejects a forgotten
    screen (401);
  - a loopback-host request passes (build the request with `client=("127.0.0.1", 1)`);
  - `/devices` shows the pair section only to `manage_users`, and the POST requires `HX-Request`.
- [ ] **Step 2:** Run `uv run pytest tests/edge/access/test_screen_pairing.py tests/edge/web/test_screen_principal.py -v`. Expect FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run `uv run pytest`. Expect PASS. Includes `tests/ui/test_static_css.py`.
- [ ] **Step 5:** Commit: `feat(edge): screen pairing tokens and require_screen`.

---

### Task 9: Kiosk routes (SSE, input, heartbeat, pair)

**Model:** Sonnet.

**Files:**
- Create: `packages/edge/src/ice_edge/web/routes/kiosk.py`, with a module-level `router` and no nested decorated functions (`tests/meta/test_routes_shape.py`).
- Modify: `packages/edge/src/ice_edge/web/routes/__init__.py`. Include `kiosk.router`, and exempt the `/kiosk` prefix in `access_gate`'s setup redirect (R11).
- Modify: `packages/edge/src/ice_edge/web/auth.py`. `is_polling_path` returns true for `/kiosk/*`, so the idle clock is untouched.
- Test: `tests/edge/web/test_kiosk_routes.py`, `tests/edge/web/test_screen_transport_parity.py`.

**Routes:**

| Route | Gate | Behavior |
|---|---|---|
| `GET /kiosk` | `require_screen(screen_view)`; on 401, redirect to `/kiosk/pair` | Renders `kiosk.html` (Task 10; until then a minimal placeholder template) |
| `GET /kiosk/pair` | none | Pairing form |
| `POST /kiosk/pair` | `require_htmx`, `Backoff` kind `"screen_pair"` | Redeem; set `SCREEN_COOKIE` (`samesite="strict"`, HttpOnly, max-age 10 years); `HX-Redirect: /kiosk` |
| `GET /kiosk/state` | `require_screen(screen_view)` | `StreamingResponse`, `text/event-stream`. Immediately sends `event: state` with `current_bytes()`, and `event: setup` if the principal is loopback and a code is held. Then each queued update. Every 15 s it sends both a `: keepalive` comment and an `event: ping` with empty data, so the renderer can observe liveness. Unsubscribes on disconnect. |
| `POST /kiosk/input` | `require_screen(screen_input)`, `require_htmx` | `machine.screen_input.handle(body)`; returns the `ScreenInputAck` JSON (400 when `None`) |
| `POST /kiosk/heartbeat` | `require_screen(screen_view)`, `require_htmx` | Validates `KioskHeartbeat`; calls `machine.telemetry.handle_heartbeat("heartbeat/screen", ...)` and, if present, `handle_capabilities("capabilities/screen", ...)`; returns 204 |

SSE framing (the parity test compares `data:` bytes with the MQTT payload):

```
event: state
data: <ScreenState.serialize() bytes, single line>

```

The keepalive interval is injectable through a module-level constant, so tests patch it rather than
sleep. The stream generator takes an `is_disconnected` callable.

- [ ] **Step 1:** Write tests:
  - `/kiosk` without a token redirects to `/kiosk/pair`;
  - pairing sets a strict cookie;
  - a wrong code goes through back-off;
  - during setup mode `/kiosk/pair` is not redirected to `/setup`;
  - `/kiosk/input` without `HX-Request` gets 403, and with a token gets an ack;
  - a heartbeat marks `screen` alive in the health monitor;
  - **Parity:** for one machine state, the first SSE `data:` line equals the last `FakeMqtt` payload on `screen/state`, byte-for-byte;
  - a token client never receives `event: setup`, and a loopback client does.

  To read the SSE stream, drive the generator function directly with a stub `is_disconnected` and a
  bounded number of items. Do not use `TestClient` streaming, which blocks on infinite streams.
- [ ] **Step 2:** Run `uv run pytest tests/edge/web/test_kiosk_routes.py tests/edge/web/test_screen_transport_parity.py -v`. Expect FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run `uv run pytest`. Expect PASS, including `tests/meta/test_routes_shape.py`.
- [ ] **Step 5:** Commit: `feat(web): kiosk routes with SSE state and input`.

---

### Task 10: Kiosk renderer and browser test

**Model:** Sonnet.

**Files:**
- Create: `packages/ui/src/ice_ui/templates/kiosk.html` (standalone, not extending `base.html`; one `<template>` per region: products, selection, credit, messages, QR, overlays)
- Create: `packages/ui/src/ice_ui/static/kiosk.js`, `packages/ui/src/ice_ui/static/kiosk-strings.en.json`
- Modify: `packages/ui/src/ice_ui/static/app.css` (rebuilt)
- Create: `tests/browser/kiosk_check.mjs`, `tests/edge/web/test_kiosk_browser.py`
- Modify:
  - `.github/workflows/ci.yml` comments at lines 46 and 80 ("five" → "six" opt-in browser tests);
  - the `tests/browser_harness.py` docstring;
  - `CLAUDE.md` browser-test command and prose, done in Task 12 but listed here so the reviewer checks it.

**Renderer behavior (`kiosk.js`):**
- **State stream.** Open `EventSource('/kiosk/state')`. For each `state` event, drop it if `seq` is at or
  below the last seen within the same `boot_id`; otherwise re-render from the document.
- **Rendering.**
  - Products become buttons, disabled with a reason label when not sellable.
  - Credit and price are formatted with `Intl.NumberFormat('en-US', {style:'currency', currency})`.
  - Messages come from the strings table with `{param}` substitution; `*_cents` params are formatted as
    money.
  - An unknown code shows the generic fallback (spec §3.3).
- **Touch input.** Only when the page's capabilities say `touch` (from `matchMedia('(pointer: coarse)')`):
  - tapping a sellable product posts `select`;
  - the Cancel button posts `cancel`;
  - each post has a new `crypto.randomUUID()` and the `HX-Request: true` header;
  - a rejected ack shows a 3 s hint from `kiosk-strings.en.json` `reasons.*`.
- **Heartbeat.** `POST /kiosk/heartbeat` every 10 s, with `capabilities` on the first beat and when touch
  detection changes. Firmware string: `"kiosk/0.1.0"`.
- **Silence.** If 20 s pass with no event or keepalive, show the "Connecting…" overlay; remove it on the
  next event. The timer resets on `open`, on every `state`/`setup` event, and on every `ping` event.
  Task 9 sends `ping` every 15 s, because SSE comments are invisible to `EventSource`.
- **Setup event:** a full-screen overlay "Setup code: 1234 5678", removed when `screen/setup` is cleared
  (an empty `setup` event).
- **Presentation:** 48 px minimum targets and AA contrast, using existing palette classes in the template
  only.

- [ ] **Step 1:** Write `tests/browser/kiosk_check.mjs`, modeled on `tests/browser/touch_feedback_check.mjs`. Load `/kiosk` as a loopback client and check:
  - it renders a product from the live state;
  - after a forced deposit (via a test-only seam in the Python test: call `machine.vmc.deposit_funds` on the live server's machine) the credit text updates;
  - tapping the product posts `select` and gets `ok`;
  - after the server stops streaming, the "Connecting…" overlay appears within 25 s.

  Write `tests/edge/web/test_kiosk_browser.py` with `pytestmark = BROWSER_OPT_IN`. Give it a patched
  short keepalive and silence timeout via query parameters `/kiosk?silence_ms=2000`, honored only for
  loopback clients.
- [ ] **Step 2:** Run `$env:ICE_COLDER_BROWSER_TESTS='1'; uv run pytest tests/edge/web/test_kiosk_browser.py -v`. Expect FAIL.
- [ ] **Step 3:** Implement the template, JS and strings; rebuild `app.css`.
- [ ] **Step 4:** Run the browser test and the four existing browser tests (command in CLAUDE.md), then `uv run pytest tests/ui -v`. Expect PASS.
- [ ] **Step 5:** Commit: `feat(ui): kiosk renderer for the customer screen`.

---

### Task 11: Screen simulator, Compose and end-to-end tests

**Model:** Sonnet.

**Files:**
- Create: `packages/sims/src/ice_sims/customer_screen.py`. `CustomerScreenSimulator(ESP32Simulator)`:
  - `subsystem_name="screen"`, `CONTRACT_VERSION = SCREEN_CONTRACT_VERSION`;
  - `build_capabilities()` returns `ScreenCapabilities(touch=True, inputs=[select, cancel], locales=["en"], size_class="tablet")`;
  - `run_simulation` subscribes `screen/state` and `screen/setup`, logs each document's `mode`/`phase`/first message code, and consumes `screen/input/ack`;
  - `inject_input(type, sku=None)` publishes a `ScreenInput`, also reachable via `cmd/sim/inject_fault`-style topic `cmd/sim/screen_input`;
  - it ends with `ESP32Simulator.entry_point(CustomerScreenSimulator)`.
- Modify: `packages/sims/src/ice_sims/__main__.py` (list the module).
- Modify: `docker-compose.yml`. Add service `sim-screen`, copying `sim-mdb`'s shape with `command: ["uv","run","python","-m","ice_sims.customer_screen","--broker","mosquitto"]` and `container_name: ice-colder-sim-screen`. Check `docker/docker-compose.yml` for a sims list and add it there too if sims appear.
- Test: `tests/sims/test_simulator_customer_screen.py`, plus additions to `tests/integration/test_integration_e2e.py`.

- [ ] **Step 1:** Write tests:
  - **Simulator unit test** (pattern of existing `tests/sims/test_simulator_*.py`): capabilities validate as `ScreenCapabilities` and the heartbeat payload is well formed.
  - **Integration** (broker-gated like the existing e2e tests; model it on `test_vending_heartbeat_loss_blocks_sale_not_payment_enable`, line 669):
    1. With the screen simulator live, a full sale publishes a `screen/state` sequence passing through `credit`, `dispensing`, then `idle` with `thank_you`.
    2. With the screen simulator stopped, after the (shrunk, injected) screen timeout, `SCR-101` is active and `cmd/payment/enable` carries `accept=false`.
    3. A simulator `inject_input("select", sku)` gets an `ok` ack.
- [ ] **Step 2:** Run `uv run pytest tests/sims/test_simulator_customer_screen.py tests/integration -v`. Expect FAIL (the integration tests skip without a broker; run them where mosquitto is reachable).
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run `uv run pytest` and `docker compose -f docker-compose.yml --env-file .env.example config --quiet`. Expect PASS.
- [ ] **Step 5:** Commit: `feat(sims): customer screen simulator and e2e coverage`.

---

### Task 12: Documentation

**Model:** Haiku.

**Files:**
- Modify: `CLAUDE.md`. Cover:
  - the screen subsystem, the `ice_contracts.board.customer_screen` contract, `/kiosk` routes and pairing;
  - `SCR-101`, and eight payment-blocking codes (update every "seven" mention);
  - vending contract 2.0.0;
  - removal of `DisplayController` (lines 45, 169-177);
  - the import-linter contract 5 list;
  - `StatusOutputs` (`attach_screen`, `screen_changed`, `notice`);
  - `HealthMonitor` overrides;
  - the six browser tests, in the command and the prose;
  - the R1–R13 resolutions as a "Deviations recorded" paragraph.
- Modify: `docs/vending-system-reference-architecture.md` §2.7. The customer screen is a contract-defined subsystem with pluggable renderers, and a dead screen stops payment.
- Modify: `docs/aspirational-gap-analysis.md` (requires the prerequisite merge). Mark §5.7 as addressed by the spec, and note §2.3's "customer display" correction.
- Modify: `packages/edge/src/ice_edge/web/routes/screen.py`. Change the docstring to "read-only owner status screen".
- Modify: `PLAN.md:66,135` (display references).

- [ ] **Step 1:** Make the edits.
- [ ] **Step 2:** Run `uv run pytest tests/meta -v`. Expect PASS. Then grep that no doc says "seven" payment-blocking codes or `cmd/display`: `rg -n "cmd/display|seven codes|six → seven" CLAUDE.md docs PLAN.md`. Expect only historical changelog lines.
- [ ] **Step 3:** Commit: `docs: customer screen contract, SCR-101 and kiosk`.

---

## Spec coverage check

| Spec section | Task(s) |
|---|---|
| §1 D1–D6 | 1, 4–11 |
| §2 components | 1, 4, 5, 6, 7, 8, 9, 10, 11 |
| §3.1 topics | 5 (state, setup), 7 (input, ack, capabilities), 9 (kiosk equivalents) |
| §3.2–3.5 documents | 1 |
| §3.6 extensibility | 1 (`CONTRACT.md`), 10 (unknown-code fallback) |
| §4 access | 8, 9 |
| §5.1 liveness | 3, 7 (version check) |
| §5.2 mode/phase | 4 |
| §5.3 SSE | 9, 10 |
| §5.4 inputs | 7 |
| §5.5 presentation | 10 |
| §6 versioning | 1, 2, 6 |
| §7 testing | every task; parity test in 9, browser in 10, e2e in 11 |
| §8 docs | 12 |
