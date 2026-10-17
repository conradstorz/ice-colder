# Customer Screen Contract — Design Spec

> **Status:** Approved design, 2026-10-14. Defines the contract between the edge and the customer-facing
> screen, the edge components that implement it, and a browser kiosk as the first renderer. It closes the
> architecture §2.7 gap recorded in [`docs/aspirational-gap-analysis.md`](../../aspirational-gap-analysis.md)
> v2.0. Today the edge publishes `cmd/display`, and nothing subscribes to it.

## 1. Decisions

| # | Question | Decision |
|---|---|---|
| D1 | What renders the screen? | A **renderer-neutral contract**. The first renderer is a **browser kiosk**; a dedicated display board can implement the same contract later. |
| D2 | What can a customer do on it? | With touch: **the same actions as the physical buttons**, select a product and cancel. The contract is extensible to screen-only extras (language, e-receipt, QR payment, help). |
| D3 | What does the edge send? | A **retained screen-state document**: the complete truth of what the customer should see. Messages are **codes with parameters**, never sentences. The renderer owns layout and wording. |
| D4 | Transport | **The same JSON over two paths.** MQTT for a board renderer; Server-Sent Events plus `POST` from the edge web server for the kiosk. The customer-facing device never reaches the board bus. |
| D5 | Who may connect? | A **paired screen token**: revocable, with only `screen_view` and `screen_input`. A kiosk on the edge box itself (loopback) is trusted without one. |
| D6 | Screen dead | **Stop taking money.** `SCR-101` is in `PAYMENT_BLOCKING_FAULTS`. |

The screen is a subsystem like `vending`, `mdb` and `ice_maker`: it is in `EXPECTED_SUBSYSTEMS`, it has a
retained capabilities document, it heartbeats, and it has a liveness fault.

## 2. Components

| Unit | Location | Purpose |
|---|---|---|
| Screen contract | `packages/contracts/src/ice_contracts/board/customer_screen.py` (new) | Pydantic models in §3 and `SCREEN_CONTRACT_VERSION = "0.1.0"`. Schemas are generated into `docs/contracts/customer-screen/` by `ice_contracts.generate`. |
| State builder | `packages/edge/src/ice_edge/customer_ui/screen_state.py` (new) | A pure function, `build_screen_state(inputs) -> ScreenState`, over the sale, escrow, catalog, availability, lease, refund status, setup mode and pending message. It does no I/O, in the same style as the `ice_edge.views` builders. |
| Publisher | `packages/edge/src/ice_edge/customer_ui/screen_publisher.py` (new; replaces `display_controller.py`) | Calls the builder when the state may have changed (below). Publishes only if the content changed, then sends the **same serialized bytes** to retained MQTT `screen/state` and to every SSE subscriber. Owns `seq` and `boot_id`. Also publishes `screen/setup`. `republish()` runs on MQTT (re)connect, as `DisplayController.republish` does today. |
| Input handler | `packages/edge/src/ice_edge/customer_ui/screen_input.py` (new) | One validator for both transports (§5.4). Dedupes the last 32 `request_id`s, then calls VMC public methods only. |
| Kiosk routes | `packages/edge/src/ice_edge/web/routes/kiosk.py` (new) | `GET /kiosk`, `GET /kiosk/state` (SSE), `POST /kiosk/input`, `POST /kiosk/heartbeat`, `GET`/`POST /kiosk/pair`. |
| Kiosk renderer | `packages/ui/src/ice_ui/templates/kiosk.html`, `packages/ui/src/ice_ui/static/kiosk.js`, `packages/ui/src/ice_ui/static/kiosk-strings.en.json` (new) | Vendored with no CDN. It renders only from `ScreenState`, maps message codes to English through the strings table, and does not extend `base.html` (customers have no navigation bar). |
| Screen pairing | `packages/edge/src/ice_edge/access/access.py` | A new device kind, `screen`: issue a pairing code, redeem it for a token, revoke. A "Pair a screen" section on `/devices`. |
| Fault | `packages/contracts/src/ice_contracts/board/vending_machine.py` | `SCR-101`, "customer screen offline": `critical` severity, `Scope.machine`, safety gate, a member of `PAYMENT_BLOCKING_FAULTS`, auto-clears on recovery. |
| Simulator | `packages/sims/src/ice_sims/customer_screen.py` (new) | An MQTT screen board. It publishes capabilities and heartbeats, logs each `ScreenState` it receives, and can inject `select`/`cancel` inputs. Added to Compose. |

**When the publisher rebuilds.** `StatusOutputs` already routes everything the old display received:

- `display(state)` and `state_changed`;
- `message(text)`;
- `show_qr(image)`;
- `refresh()`.

Those calls now notify the publisher. Fault and availability changes notify it too, through the existing
`push_active_faults` path. `message(text)` keeps its English text for the owner logs; the VMC call sites
that produce customer messages also pass a `MessageCode` (§3.3).

**Removed:** `DisplayController`, `DisplayCommand`, `DisplayMode` and the `cmd/display` topic.

## 3. Documents

All topics sit under the existing `vmc/<machine_id>/` prefix. Money is **integer cents**; the edge converts
from its internal floats at the boundary by rounding half-up to the nearest cent.

### 3.1 Topics and their kiosk equivalents

| MQTT topic | Kiosk path | Direction | Payload |
|---|---|---|---|
| `screen/state` (retained, QoS 1) | SSE event `state` on `GET /kiosk/state` | edge → screen | `ScreenState` |
| `screen/setup` (retained; an empty retained payload clears it) | SSE event `setup`, **loopback clients only** | edge → screen | `ScreenSetup{code}` |
| `screen/input` | `POST /kiosk/input` (JSON body) | screen → edge | `ScreenInput` |
| `screen/input/ack` | the response body of that `POST` | edge → screen | `ScreenInputAck` |
| `capabilities/screen` (retained) | the body of the kiosk's first `POST /kiosk/heartbeat`, and again whenever it changes | screen → edge | `ScreenCapabilities` |
| `heartbeat/screen` | `POST /kiosk/heartbeat` every 10 s | screen → edge | `SubsystemHeartbeat` (existing model) |

The setup code is a separate document so that `ScreenState` is byte-identical on both paths.

### 3.2 `ScreenState`

| Field | Type | Meaning |
|---|---|---|
| `contract` | str | `SCREEN_CONTRACT_VERSION` |
| `boot_id` | str | A UUID per edge process |
| `seq` | int | Monotonic per `boot_id`. A renderer ignores a document whose `seq` is at or below the last one it saw for the same `boot_id`. |
| `ts` | str | UTC ISO-8601 |
| `mode` | `selling` \| `out_of_service` \| `setup` | §5.2 |
| `phase` | `idle` \| `credit` \| `selected` \| `dispensing` \| `refunding` \| `error` | The customer-facing phase (§5.2). Not an FSM state name. |
| `currency` | str | ISO 4217, from config |
| `credit_cents` | int | The current escrow total |
| `products` | list of `ScreenProduct` | Catalog order |
| `selection` | `{sku, price_cents}` \| null | |
| `payment` | `{accepting: bool, qr: {gateway, url} \| null}` | `accepting` mirrors `Availability.payment_enabled` |
| `refund` | `{status: pending \| done \| failed, amount_cents}` \| null | The most recent refund in this session |
| `messages` | list of `ScreenMessage` | Most important first |

`ScreenProduct` has the fields `sku`, `slot`, `name`, `price_cents`, `sellable: bool`, and
`reason: ProductReason | null`. `ProductReason` is one of `sold_out`, `locked_out` or `unavailable`.

The models are frozen and `extra="forbid"` on the edge side. Renderers must ignore unknown fields
(contracts spec §1.3).

### 3.3 `ScreenMessage` and `MessageCode` 0.1.0

A `ScreenMessage` is `{code: MessageCode, params: {...}}`. Each code's params are typed by their own model.

| Code | Params |
|---|---|
| `welcome` | — |
| `insert_money` | — |
| `insert_more` | `needed_cents` |
| `select_product` | — |
| `sold_out` | `sku` |
| `product_unavailable` | `sku` |
| `dispensing` | `sku` |
| `take_product` | — |
| `thank_you` | — |
| `vend_failed_credit_restored` | `amount_cents` |
| `refunding` | `amount_cents` |
| `refund_done` | `amount_cents` |
| `refund_failed_contact_owner` | — |
| `out_of_service` | — |
| `scan_qr` | `gateway` |
| `payment_unavailable` | — |

A renderer shows a **generic fallback** ("Please wait" while `phase` is `dispensing` or `refunding`,
otherwise "Please see attendant") for any code missing from its strings table.

### 3.4 `ScreenInput` and `ScreenInputAck`

- **`ScreenInput`:**
  - `request_id` (str, UUID);
  - `ts`;
  - `type: select | cancel`;
  - `sku` (required for `select`, forbidden for `cancel`).
- **`ScreenInputAck`:**
  - `request_id`;
  - `status: ok | rejected | unsupported`;
  - `reason: InputRejectReason | null`.
- **`InputRejectReason`** is one of:
  - `not_declared`
  - `unknown_sku`
  - `sold_out`
  - `locked_out`
  - `insufficient_credit`
  - `busy`
  - `out_of_service`
  - `no_credit`
  - `contract_mismatch`

### 3.5 `ScreenCapabilities`

Fields:

- `firmware` (str);
- `contract_version` (str);
- `touch` (bool);
- `inputs`: a list drawn from `select` and `cancel`;
- `locales`: a list of BCP 47 tags;
- `size_class: "tablet"`.

A screen with `touch: false` declares `inputs: []`.

### 3.6 Extensibility

New message codes, input types, `ProductReason`/`InputRejectReason` members and optional fields are
**minor** bumps of `SCREEN_CONTRACT_VERSION`. Screen-only extras are each one such bump. Examples:

- language choice: input `set_locale`, field `locale`;
- e-receipt: input `request_receipt`, field `receipt`;
- QR payment start: input `start_qr`;
- help: input `help`.

## 4. Access

- **Pairing.** A new "Pair a screen" section on `/devices`, gated on `manage_users`.
  - It issues a single-use **8-digit pairing code** valid for 10 minutes.
  - The kiosk shows `GET /kiosk/pair` when it has no valid token; `POST /kiosk/pair` redeems the code for a
    token.
  - The token is stored in `data/access.json` as a `screen` device and set as an HttpOnly, SameSite=Strict
    cookie.
  - Redeeming the code goes through the existing `Backoff` under a `screen_pair` kind.
- **Token permissions.** Exactly `Permission.screen_view` and `Permission.screen_input`, two new
  permissions that no role holds. The token never grants a user session.
- **Revocation.** From `/devices`. The kiosk's next request returns 401, and the page falls back to
  `/kiosk/pair`.
- **Idle clock.** `/kiosk/*` is exempt from the session idle clock (`is_polling_path`), because the token is
  not a user session.
- **Loopback.** A loopback client (`auth_policy.is_loopback`) is trusted without a token on all `/kiosk/*`
  routes. It is the **only** client that receives the SSE `setup` event.
- **CSRF.** `POST /kiosk/input`, `POST /kiosk/heartbeat` and `POST /kiosk/pair` require the `HX-Request`
  header. The vendored `kiosk.js` sets it, as HTMX does.
- **Audit.** Pairing and revocation record the acting user.

## 5. Behavior

### 5.1 Liveness and `SCR-101`

- **Alive** means a `SubsystemHeartbeat` arrived on either path within the screen's timeout. An open SSE
  connection alone does not count.
- **Timeout.** The screen's timeout is **30 s**, a per-subsystem override in `HealthMonitor`. Other
  subsystems keep 120 s. The `HealthMonitor` check interval for the screen is at most 10 s, so detection
  takes 30–40 s.
- **At boot**, a screen that has not spoken within 30 s raises `SCR-101`. A machine without a working
  screen does not take money (D6).
- **Recovery.** A returning heartbeat clears `SCR-101` and republishes `screen/state`.
- **Mid-sale**, `SCR-101` behaves like any payment-blocking fault: `cmd/payment/enable` is withdrawn, and
  an in-flight sale finishes or times out (and refunds) under the existing session rules.
- **Version check.** Capabilities whose `contract_version` major differs from `SCREEN_CONTRACT_VERSION`'s
  major mean the screen is treated as offline. `SCR-101` is raised, with the mismatch in its detail, and
  every input from it is acked `rejected/contract_mismatch`.

### 5.2 Mode and phase derivation

**`mode`:** the first of these that applies.

1. `setup` while setup mode is active.
2. `out_of_service` while any of these is true:
   - any `PAYMENT_BLOCKING_FAULTS` code other than `SCR-101` is active;
   - the maintenance lease is held;
   - the FSM is in `error`.
3. Otherwise `selling`.

`SCR-101` does not set `out_of_service`: a screen that can read the state is by definition not offline.

**Test sales** run under the lease, so the screen shows `out_of_service` throughout. A customer never sees a
test vend.

**`phase`:** the first of these that applies.

| `phase` | When |
|---|---|
| `error` | The FSM is in `error` |
| `refunding` | A refund is pending |
| `dispensing` | The FSM is in `dispensing` |
| `selected` | A selection exists |
| `credit` | `credit_cents > 0` |
| `idle` | Otherwise |

### 5.3 SSE

- **On connect,** the server sends the current `state` (and `setup`, if it applies) immediately, then a
  `: keepalive` comment every 15 s.
- **Payload.** Each `state` event's `data:` is exactly the bytes published to MQTT.
- **Reconnect.** The renderer uses `EventSource`'s automatic reconnect.
- **Silence.** If 20 s pass with no event or keepalive, the renderer covers the page with "Connecting…". It
  never shows stale prices or credit.

### 5.4 Inputs

Validation runs in this order, and the first failure decides the ack:

1. **Schema.** An invalid body is acked `rejected` (it is dropped if there is no usable `request_id`).
2. **Duplicate `request_id`.** The cached ack is replayed and the input is not re-executed.
3. **Contract mismatch:** `rejected/contract_mismatch`.
4. **Not declared.** A `type` not in the screen's declared `inputs` (including any touch input from a
   `touch: false` screen) is acked `unsupported/not_declared`.
5. **Out of service.** If `mode` is not `selling`: `rejected/out_of_service`.
6. **`select`:**
   - An unknown sku is `rejected/unknown_sku`.
   - Otherwise it goes down **the same `VMC.select_product` path a button press takes**, by sku.
   - Its refusals map to `sold_out`, `locked_out`, `insufficient_credit`, or `busy` (FSM in `dispensing`).
7. **`cancel`:**
   - With credit: `VMC.request_refund(reason="customer")`.
   - With no credit and no selection: `rejected/no_credit`.
   - During `dispensing`: `rejected/busy`.

A rejection never changes the state. The renderer shows a brief hint chosen by `reason`.

One `screen` subsystem is expected. Any number of paired viewers may connect; each receives the state, and
any of them may send input or heartbeats.

### 5.5 Kiosk presentation requirements

- Touch targets are at least 48 px.
- Text meets WCAG 2.1 AA contrast.
- It works with no internet; all assets are vendored.
- It shows nothing an owner page shows (faults, KPIs, escrow history).
- Copy comes only from `kiosk-strings.en.json`.

## 6. Versioning

- **Vending contract `1.0.0 → 2.0.0` (major).**
  - Why major: removing `cmd/display` and `DisplayCommand` removes a topic and a message, a major bump per
    the contracts spec §1.3 checklist. No consumer exists, so nothing breaks in practice.
  - The same bump adds `SCR-101`, takes `PAYMENT_BLOCKING_FAULTS` from seven codes to **eight**, and adds
    `screen` to `EXPECTED_SUBSYSTEMS`.
- **The screen contract starts at `0.1.0`.**

## 7. Testing

| Area | Tests |
|---|---|
| Contract | Schemas generated and drift-tested under `docs/contracts/customer-screen/`. Every `MessageCode` has a params model. `select` without `sku` and `cancel` with `sku` are invalid. |
| State builder | Every `mode` × `phase` row in §5.2; message ordering; each out-of-service cause (fault, lease, `error`, test sale); cents rounding; golden JSON fixtures. |
| Transport parity (conformance) | For one machine state, the MQTT payload bytes equal the SSE `data:` bytes. A loopback client receives `setup`; a token client never does. |
| Input handler | Each step of §5.4 in order, including dedupe replay without re-execution. `select` reaches the same `select_product` path as `on_button_press`; `cancel` calls `request_refund`. |
| Liveness | On `FakeTaskRunner`: the 30 s override, the boot raise, raise and clear on silence and return, `payment_enabled` going false, a major mismatch counting as offline, other subsystems still on 120 s. |
| Access | Pairing code issue, single use and expiry; `screen_pair` back-off; token revocation leading to 401; route gating; idle-clock exemption; a token never resolving to a user session. |
| Browser (opt-in) | `tests/edge/web/test_kiosk_browser.py` with a `tests/browser/kiosk.mjs` checker: renders a state, shows "Connecting…" after silence, and a tap posts `select`. Added to the CI guard list and the CLAUDE.md browser-test command. |
| End-to-end | An integration-test sale with `ice_sims.customer_screen` live. A test with the screen simulator stopped shows payment withdrawn. |
| Ratchets | Import-linter contract 5 replaces `ice_edge.customer_ui.display_controller` with `ice_edge.customer_ui.screen_publisher`. New test files have a private-access budget of 0. `test_static_css` covers `kiosk.html`. |

## 8. Documentation updates

- **`CLAUDE.md`:**
  - the screen subsystem, `/kiosk` routes and pairing;
  - `SCR-101`;
  - the eight payment-blocking codes;
  - the 2.0.0 bump;
  - the new browser test;
  - removal of `DisplayController`.
- **`docs/vending-system-reference-architecture.md` §2.7:** the customer screen is a contract-defined
  subsystem with pluggable renderers, and a dead screen stops payment.
- **`docs/aspirational-gap-analysis.md`:** mark §5.7 as addressed by this spec.
- **`packages/edge/src/ice_edge/web/routes/screen.py`:** correct the docstring. `/screen` is the owner
  status page and is unchanged.

## 9. Out of scope

Each item is a later minor bump of the screen contract:

- locales other than English;
- e-receipts;
- real QR payment flows;
- help and report-a-problem pages;
- a full accessibility audit;
- audio.

Also out of scope:

- a dedicated display-board firmware;
- changes to the owner `/screen` page.
