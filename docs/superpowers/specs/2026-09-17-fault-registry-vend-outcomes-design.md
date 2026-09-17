# Fault Registry and Honest Vend Outcomes — Design

**Date:** 2026-09-17
**Status:** Approved (brainstormed with owner)
**Roadmap:** Phase C, slice 1 of 4 (`ROADMAP.md` §5, §7, §9)

## Goal

A failed vend is recorded as a failed vend, carries a stable fault code, locks
out only the product that failed, and pays the customer back through the
payment gateway rather than by zeroing a number. The always-on simulation
stack on hpz440 shows all of this happening without operator input.

## Why now

- The vending simulator reports `bin_empty`, `timeout` and `jam`, but the VMC
  only recognises `jammed` and `error`. Every simulated fault falls through to
  the log-only branch and the 60 s fallback then records a successful sale.
  The simulated auger jam reports at 90 s, after the fallback has already
  "completed" the sale.
- "Refund" today zeroes `credit_escrow` and logs. No message reaches the
  payment gateway; the MDB simulator has no refund handling.
- Alerts are free text. Nothing on the dashboard says which product is broken
  or why.

## Non-goals (later slices)

- Availability permissives and `payment/enable` gating (slice 2).
- Remote-command audit and payment power-cycle (slice 3).
- Startup self-test (slice 4).
- Full vending-machine contract prose; this slice creates the models and a
  topic-map stub only (Phase B finishes it).
- Any change to how the compose stack is deployed. The simulation stays a
  separate, always-on environment (owner decision: no automatic fallback
  between hardware and simulators).

## Decisions

1. **Contract-first.** Shared Pydantic models in `contracts/vending_machine.py`
   are imported by the VMC and the simulators, with generated JSON Schema and a
   drift test, following the ice-maker-monitor pattern.
2. **After a failed vend the customer keeps their credit.** The price returns
   to escrow, the FSM goes `dispensing → interacting_with_user`, the failed
   product is locked out, and the customer may choose another. If nothing
   sellable remains, the VMC pays out immediately and goes idle.
3. **Refund means pay-out.** Only `request_refund` sends money back, always via
   a `PaymentRefundCommand` with an ack deadline. Restoring price to escrow is
   not a refund and sends nothing.
4. **`error` state is for critical, machine-scope faults only.** No dispense
   failure enters it any more.

## 1. Contract models — `contracts/vending_machine.py` (v0.1.0)

### DispenserOutcome

```python
class DispenserOutcome(str, Enum):
    complete = "complete"
    bin_empty = "bin_empty"
    timeout = "timeout"
    jam = "jam"
    error = "error"
```

`DispenserStatus.state` stays a free string so intermediate steps
(`motor_active`, `fill_complete`, `solenoid_open`, …) keep flowing. The VMC
treats only enum members as terminal. Simulators publish enum values for
terminal states.

### FaultCode and FAULT_TABLE

`FaultCode(str, Enum)` with the values from `ROADMAP.md` §5 (`ICE-101`,
`ICE-201`, `ICE-202`, `ICE-301`, `ICE-302`, `ICE-401`, `ICE-402`, `WTR-101`
… `WTR-105`, `ENV-101` … `ENV-103`, `PAY-101`, `PAY-102`, `PWR-101`,
`PWR-102`, `COM-101` … `COM-103`, `SVC-101`) plus one new code:

| Code | Meaning | Severity | Scope |
|---|---|---|---|
| `PAY-103` | Refund not confirmed by payment gateway; needs reconciliation | warning | machine |

```python
class Severity(str, Enum):
    info = "info"
    warning = "warning"
    product_unavailable = "product_unavailable"   # clears itself
    vend_failed = "vend_failed"                   # ends the sale, no lockout
    lockout = "lockout"                           # product locked until admin clears
    critical = "critical"                         # machine-scope, never auto-clears

class Scope(str, Enum):
    product = "product"
    machine = "machine"

class FaultSpec(BaseModel):
    severity: Severity
    scope: Scope
    description: str

FAULT_TABLE: dict[FaultCode, FaultSpec]
```

Every `FaultCode` member has a `FAULT_TABLE` entry (a test enforces this).
Severities follow the roadmap table.

### Outcome → code mapping

```python
OUTCOME_FAULTS: dict[DispenserOutcome, FaultCode] = {
    DispenserOutcome.bin_empty: FaultCode.ICE_101,   # product_unavailable
    DispenserOutcome.timeout:   FaultCode.ICE_301,   # lockout
    DispenserOutcome.jam:       FaultCode.ICE_401,   # lockout
    DispenserOutcome.error:     FaultCode.ICE_302,   # lockout
}
```

A VMC-side dispense timeout (no terminal report at all) maps to `PAY-102`
(`vend_failed`, product scope, no lockout). A test asserts every non-`complete`
outcome has a mapping.

### Refund messages

```python
class PaymentRefundCommand(BaseModel):        # VMC → gateway, cmd/payment/refund
    request_id: str = Field(..., min_length=8, max_length=64)
    amount: float = Field(..., gt=0)
    reason: str        # a FaultCode value, "session_timeout", "cancel", or "admin"
    timestamp: datetime

class RefundStatus(str, Enum):
    ok = "ok"
    failed = "failed"
    unsupported = "unsupported"

class PaymentRefundResult(BaseModel):         # gateway → VMC, cmd/payment/refund/ack
    request_id: str
    status: RefundStatus
    amount_returned: float = 0.0
    detail: str | None = None
    timestamp: datetime
```

`request_id` is an opaque correlation key (UUID4 by the VMC); the gateway must
answer a repeated `request_id` with its previous result and must not pay twice.

### VMCAlert change (`services/mqtt_messages.py`)

`VMCAlert` gains `code: FaultCode | None = None` and
`product_sku: str | None = None`.

### Generated artefacts

`contracts/generate.py` also writes `docs/contracts/vending-machine/schemas/`
(`dispenser_outcome`, `fault_code`, `payment_refund_command`,
`payment_refund_result`). `docs/contracts/vending-machine/CONTRACT.md` is a
stub: version `0.1.0`, topic map for `hardware/dispenser`, `cmd/payment/refund`,
`cmd/payment/refund/ack`, and a pointer to `ROADMAP.md` §4–§5 for the prose to
come in Phase B. `tests/test_contract_schemas.py` covers the new schemas.

## 2. VMC changes — `controller/vmc.py`

### Transitions

Add:

```python
{"trigger": "vend_failed", "source": "dispensing",
 "dest": "interacting_with_user", "before": "on_vend_failed"}
```

`complete_transaction`, `cancel_sale`, `error_occurred`, `reset_state`
unchanged.

### Dispense timeout

- Duration comes from config: `PhysicalDetails.dispense_timeout_seconds: float
  = 120.0` (`ge=10`). Rationale: the simulated jam reports at 90 s; real
  firmware will have its own bounded fill time under this.
- On expiry with state still `dispensing`: raise `PAY-102` for the selected
  SKU, then `vend_failed()`. It never calls `_finish_dispensing`.

### Terminal outcome handling in `_handle_mqtt_dispenser`

1. Existing guards stay: ignore unless `state == "dispensing"` and the slot
   matches `selected_product.slot`.
2. Parse `data["state"]` as `DispenserOutcome`; on `ValueError` log as an
   intermediate step and return.
3. `complete` → as today (cancel timeout, record `dispense`, finish).
4. Any other member → cancel timeout, look up `OUTCOME_FAULTS`, call
   `_raise_fault(code, sku)`, then `vend_failed()`.

### `on_vend_failed`

- `credit_escrow += selected_product.price` (restoring what `_process_payment`
  deducted).
- Record `vend_failed` event: `value=price`, metadata `{code, sku, outcome}`.
- Customer message names the product and says credit is retained.
- `selected_product = None`, `last_insufficient_message = ""`.
- If `_sellable_products()` is empty: `request_refund(reason=code)` then
  `machine.set_state("idle")` (same pattern `_expire_session` uses), publish
  status, display idle. Otherwise reset the session timeout so the customer
  gets the normal inactivity window to choose again.
- Publish status, update display (`interacting` or `idle`), refresh UI.

### Lockouts

```python
self._lockouts: dict[str, FaultCode]      # sku -> code
```

- `_raise_fault(code, sku)`: look up `FAULT_TABLE[code]`. For
  `lockout` and `product_unavailable` with product scope, set
  `_lockouts[sku] = code` and record `lockout_set`. For every code, fire the
  alert (§4) and push the active-fault snapshot to the health monitor.
- `_clear_fault(sku)`: remove the entry, record `lockout_cleared`, push
  snapshot, publish status.
- `product_unavailable` auto-clear: the VMC does not subscribe to
  `hardware/io/+` today. Add `client.register("hardware/io/+",
  self._handle_mqtt_hardware_io)`; the handler parses `HardwareIO` and, for
  device `bin_half_full` with `state == True`, clears any `ICE-101` lockouts.
  Other devices are logged at debug level and otherwise ignored in this slice.
- `select_product` refuses a locked SKU: customer message
  `"<name> is unavailable (<code>). Please choose another product."`, no state
  change. `_sellable_products()` = catalog minus locked SKUs.
- Admin clear: `POST /faults/{sku}/clear` calls `vmc.clear_fault(sku)`.

### `error` state

Unchanged mechanics; no code path in this slice enters it. `on_error` keeps
its full-escrow refund, which now goes through `request_refund` (§3) so it
pays out.

## 3. Refunds — VMC ↔ MDB gateway

### `request_refund(reason: str = "admin")`

Single pay-out path. All callers (`_expire_session`, `on_cancel_sale`,
`on_error`, `on_vend_failed` all-locked branch, admin refund endpoint) pass a
reason.

1. If `credit_escrow <= 0`: customer message "No funds to refund", return.
2. `amount = credit_escrow`; `credit_escrow = 0.0`.
3. `request_id = uuid4().hex`; publish `PaymentRefundCommand(request_id,
   amount, reason)` on `cmd/payment/refund` (QoS 1).
4. Store `self._pending_refunds[request_id] = PendingRefund(amount, reason,
   attempts=1, deadline_task)`; schedule a 10 s deadline via `_schedule`.
5. Customer message and `txn_log` line as today, worded "refund requested".

### Ack handling — `_handle_mqtt_refund_ack` on `cmd/payment/refund/ack`

- Unknown `request_id`: log and ignore.
- `ok`: cancel deadline, record `refund` event (`value=amount_returned`,
  metadata `{request_id, reason}`), `txn_log` "REFUND CONFIRMED", drop the
  pending entry.
- `failed` / `unsupported`: treat as deadline expiry below.

### Deadline expiry / failure

- First time (`attempts == 1`): republish the identical command (same
  `request_id`), `attempts = 2`, new 10 s deadline.
- Second time: record `refund_failed` (`value=amount`, metadata
  `{request_id, reason, detail}`), `_raise_fault(FaultCode.PAY_103,
  sku=None)`, drop the pending entry. Escrow stays zero; reconciliation is an
  operator task surfaced by the alert and the event row.

### Escrow restore is not a refund

`on_vend_failed` restoring the price to escrow sends no command. Only
`request_refund` does.

### MDB gateway simulator — `simulators/mdb_gateway.py`

- Subscribes to `cmd/payment/refund`. Keeps `_refund_results:
  OrderedDict[request_id, PaymentRefundResult]` bounded to 256 like the
  ice-maker's `_acked`.
- Known `request_id` → re-publish the stored result.
- New `request_id` → after `random.uniform(0.5, 2.0)` s publish `ok` with
  `amount_returned = amount`, log `"[mdb] Refund <id>: paid out $x.xx
  (<reason>)"`, store.
- New injectable fault `changer_empty` (category `medium`, low probability,
  recovers after 5–15 min): while active, answers `failed` with
  `detail="changer_empty"` and `amount_returned = 0.0`.

## 4. Alerts, dashboard, event history

### Alerts

- `_raise_fault` builds `Alert(level, source="vmc", message, code, sku)` where
  `level` maps from severity (`info→info`, `warning→warning`,
  `product_unavailable→warning`, `vend_failed→warning`, `lockout→error`,
  `critical→critical`) and hands it to the health monitor's existing alert
  path, so dedup and the email notifier are reused. Dedup key: `f"{code}:{sku}"`.
- `_clear_fault` discards that dedup key so a recurrence alerts again.
- `Alert` (health monitor) and `VMCAlert` (MQTT) both carry `code` and
  `product_sku`; the MQTT publish on `vmc/{id}/alerts` includes them.

### Health monitor

- `set_active_faults(faults: dict[str, str])` (sku → code, plus
  `"__machine__"` key for machine-scope codes such as `PAY-103`) stores a
  snapshot with a `since` timestamp per entry; `get_summary()` exposes
  `active_faults: [{sku, code, severity, since_seconds}]`.
- The VMC pushes the snapshot after every `_raise_fault` / `_clear_fault`.

### Dashboard

- **Status fragment:** "Active faults" list (code, product name or "machine",
  severity badge, since). Lockout rows get a Clear button
  (`hx-post="/faults/{sku}/clear"`, swaps the fragment). Empty state: "No
  active faults".
- **Inventory table:** a "locked" badge with the code next to a locked
  product.
- **Activity / KPI fragments:** add `vends_failed` and `refunds` counters.
- Routes: `POST /faults/{sku}/clear` (admin auth like every other route);
  machine-scope faults are cleared with `sku="__machine__"`.

### Event recorder

New event types and summary keys:

| event_type | value | metadata |
|---|---|---|
| `vend_failed` | price | `code`, `sku`, `outcome` |
| `refund` | amount returned | `request_id`, `reason` |
| `refund_failed` | amount requested | `request_id`, `reason`, `detail` |
| `lockout_set` | 1.0 | `code`, `sku` |
| `lockout_cleared` | 1.0 | `code`, `sku`, `by` (`auto` / `admin`) |

`SUMMARY_KEYS` gains `vends_failed` (count) and `refunds` (sum of `refund`
value). Historical averages pick them up automatically.

## 5. Testing

### Contract (`tests/test_contracts.py`, `tests/test_contract_schemas.py`)

- Every `FaultCode` has a `FAULT_TABLE` entry; every non-`complete`
  `DispenserOutcome` has an `OUTCOME_FAULTS` entry.
- Generated schemas for the four new models match the models (drift test).
- `PaymentRefundCommand` rejects `amount <= 0` and short `request_id`.

### VMC flows (`tests/test_vmc_flows.py`)

For each of `timeout`, `jam`, `bin_empty`, `error`, and the no-report
timeout: state ends `interacting_with_user`, escrow equals the price again,
the expected code is recorded as `vend_failed`, lockout set for
lockout/product_unavailable codes and not for `PAY-102`, no refund command
published.

Also:

- `select_product` refuses a locked SKU with no state change.
- All products locked → refund command published, state `idle`.
- `bin_half_full=True` hardware IO clears an `ICE-101` lockout and records
  `lockout_cleared` with `by=auto`.
- `clear_fault` clears, records, and re-arms the alert dedup key.
- Late, duplicate, or mismatched-slot outcomes are still ignored (existing
  tests stay green).
- `on_error` now publishes a refund command.

### Refunds (`tests/test_vmc_flows.py`)

- Ack `ok` records `refund` and drops the pending entry.
- Ack `failed` → one retry with the same `request_id`, then `refund_failed` +
  `PAY-103` alert.
- No ack → same path via the deadline.
- Unknown `request_id` ack is ignored.

### Simulators

- `tests/test_simulator_mdb.py`: refund acked `ok` with amount; same
  `request_id` twice yields the stored result and one pay-out log;
  `changer_empty` yields `failed`; result cache bounded.
- `tests/test_simulator_vending.py`: every terminal `DispenserStatus` the
  simulator publishes parses as `DispenserOutcome`.

### Dashboard (`tests/test_web_routes.py`)

- Status fragment renders active faults and the Clear button for lockouts.
- `POST /faults/{sku}/clear` clears and returns the refreshed fragment.
- KPI fragment shows `vends_failed` and `refunds`.

### End-to-end (`tests/test_integration_e2e.py`)

Inject `auger_jam`, run a sale, assert the dashboard shows `ICE-301` on that
product and `vends_failed` incremented, clear the lockout, sell the product
again successfully.

### Continuous simulation

No compose changes. The simulators already inject faults at random, so the
hpz440 stack will show lockouts appearing, refunds paying out, and occasional
`PAY-103` reconciliation alerts unattended.

## Files touched

| Area | Files |
|---|---|
| Contract | `contracts/vending_machine.py` (new), `contracts/generate.py`, `docs/contracts/vending-machine/` (new), `services/mqtt_messages.py` |
| Config | `config/config_model.py` (`dispense_timeout_seconds`), `config.example.json` |
| VMC | `controller/vmc.py` |
| Services | `services/health_monitor.py`, `services/event_recorder.py` |
| Web | `web_interface/routes.py`, `templates/partials/status_fragment.html`, `inventory_table.html`, `kpi_fragment.html`, `activity_fragment.html` |
| Simulators | `simulators/mdb_gateway.py`, `simulators/vending_machine.py` |
| Docs | `ROADMAP.md` (§5 adds `PAY-103`; §2 note about the 60 s fallback becomes past tense), `CLAUDE.md` (FSM states/transitions line) |
| Tests | as listed in §5 |
