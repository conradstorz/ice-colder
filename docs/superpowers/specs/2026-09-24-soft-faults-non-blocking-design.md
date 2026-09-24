# Soft faults must never stop the machine

**Date:** 2026-09-24
**Status:** Approved, pending implementation plan

## Problem

A VMC restart with an open sale raises `PAY-104` ("Transaction uncertain after
VMC restart; operator must reconcile"). Its severity is `lockout` and its scope
is `machine`, so `Availability` fails the `no_critical_fault` permissive and
publishes `cmd/payment/enable false`. The machine stops taking money until an
operator walks up to the dashboard and clicks Clear.

That is the wrong trade for an unattended machine. `PAY-104` is a bookkeeping
doubt about a sale that already happened. It says nothing about whether the
machine can safely sell the next bag of ice. A restart at 2 a.m. currently
costs a full night of revenue to protect a record that an operator will
reconcile in the morning either way.

The same reasoning applies to heartbeat and comms faults. A thirty-second MQTT
reconnect or a briefly missed ESP32 heartbeat currently cycles payment off and
back on. Those are transient conditions, not reasons to refuse money.

## Core idea

`Availability` currently answers one question and reuses the answer for two
purposes. Split them:

- **`payment_enabled`** drives `cmd/payment/enable` — may the machine accept
  money at all? Gated **only** by physical safety.
- **`product_sellable(product)`** drives the gate in `VMC.select_product`
  (`controller/vmc.py:1237`) — may *this* selection proceed right now? Gated by
  safety **plus** fulfillment: subsystem liveness, stock, product lockouts.

Money keeps flowing in. An individual selection that cannot be fulfilled is
refused at the point of selection, the customer is told to try later, and
escrow is refunded on session timeout.

## Design

### 1. `contracts/vending_machine.py`

`PAY-104` severity changes from `lockout` to `warning`. It keeps its code, its
scope, and its description; it still appears in Active Faults and still
requires an operator to clear it.

Add an explicit whitelist beside `FAULT_TABLE`:

```python
PAYMENT_BLOCKING_FAULTS = frozenset({
    FaultCode.ICE_402,  # trap door failed to close
    FaultCode.WTR_103,  # flow continues after valve close
    FaultCode.WTR_104,  # leak / overflow detected
    FaultCode.ENV_102,  # heater ineffective
    FaultCode.ENV_103,  # heater high-limit tripped
    FaultCode.PWR_102,  # 24 V control supply bad
})
```

Membership in this set — not severity — decides whether payment stops. This is
the load-bearing change: adding a fault code can no longer silently stop the
machine, because stopping the machine now requires a deliberate edit to this
frozenset.

`CONTRACT_VERSION` goes `0.3.0` -> `0.4.0`. The severity change is behavioral
and the simulators read severity from the same table.

### 2. `services/availability.py`

Each `Permissive` gains a `gate` field: `safety`, `fulfillment`, or `alert`.

| gate | rows |
|---|---|
| `safety` | `no_critical_fault`, `no_leak`, `water_valve_closed`, `trap_door_closed`, `control_power_ok`, `service_door_closed` |
| `fulfillment` | `mqtt_connected`, `vending_alive`, `payment_alive`, `payment_devices_ready`, `ice_maker_alive`, `ice_available`, `fsm_ok`, `bag_present`, `water_pressure_ok`, `water_treatment_ok` |
| `alert` | `transaction_certain` |

Most `fulfillment` and `safety` stub rows are still `instrumented=False` and
always pass; the gate assignment decides what happens when they are wired up
later, which is the point of keeping them in the table.

Changes to the class:

- `set_active_faults` computes `no_critical_fault` from
  `PAYMENT_BLOCKING_FAULTS` membership instead of
  `severity in {"critical", "lockout"}`. `_BLOCKING_SEVERITIES` is removed.
- `payment_enabled` considers only `safety` rows. It no longer depends on the
  product list, because safety is machine-wide — a machine with zero products
  configured is not a safety fault and should not report "no products" as a
  reason payment is off.
- `sale_available(kind)` considers safety + fulfillment rows for that kind, and
  no longer considers `alert` rows. `PAY-104` therefore stops blocking
  selection as well as payment, which is the intent: it is a record of a past
  sale and says nothing about the next one. `product_sellable` keeps layering
  per-SKU lockouts on top, unchanged.
- `blocking_reasons()` splits into `payment_blocking_reasons()` (failing safety
  rows) and the existing sale-level reasons, so the dashboard and `/screen` can
  say which kind of trouble the machine is in.
- `table()` exports `gate` alongside the existing fields.
- `_recompute` keeps its publish-on-change behaviour unchanged; it reads the
  narrower `payment_enabled` and logs `payment_blocking_reasons()`. It now
  fires far less often, since only safety rows can change the answer.

`alert` rows are carried in the table and rendered on the health tab, but no
consumer gates on them. `transaction_certain` becomes purely informational —
`set_transaction_certain(False)` still records the condition and still shows
red on the permissives table.

### 3. Evidence file

`PAY-104` and `data/session.json` survive until an admin clears the fault.
`VMC._persist_session` keeps its existing early return
(`controller/vmc.py:359`), so a live sale running alongside an unreconciled one
writes no snapshot.

Accepted trade-off: a second restart inside that window loses the second
session's evidence. The operator already has an open ticket for the first one,
and preserving the original evidence matters more than capturing a second
overlapping case.

### 4. Dashboard

`web_interface/templates/partials/status_fragment.html:74` currently renders
"Issues Detected" in red whenever any fault is active, next to a red
"Disabled". Split the two states:

- **"Machine Stopped"**, red — `payment_enabled` is false. A safety permissive
  failed; nobody is buying anything until it is resolved.
- **"Issues Detected — still selling"**, amber — faults are active but payment
  is enabled.

`partials/health_fragment.html:82` gains a gate column on the permissives table
so an operator can tell at a glance which red rows actually stop the machine.
`partials/screen_body.html` keeps its existing enabled/disabled colours; it now
flips to red far less often.

### 5. Accepted risk

With no liveness row gating payment, a customer can insert cash while the
vending ESP32 is offline. The sequence:

1. Cash is accepted, escrow rises.
2. `select_product` refuses every product (`sale_available` fails on
   `vending_alive`) and messages the customer.
3. The session timeout fires and calls
   `request_refund(reason="session_timeout")` (`controller/vmc.py:1368`).

If the broker is also down, that refund command cannot be published, `PAY-103`
("Refund not confirmed by payment gateway; needs reconciliation") raises, and
it becomes an operator reconciliation. This is the intended trade: alert the
operator, never stop the machine.

## Testing

`tests/test_availability.py`:

- Each of the six codes in `PAYMENT_BLOCKING_FAULTS` sets `payment_enabled`
  false when raised machine-scope.
- `PAY-104` active leaves `payment_enabled` true.
- Every `fulfillment` row set false individually leaves `payment_enabled` true
  while `sale_available` returns false with that row named.
- A machine-scope fault that is `critical` but absent from
  `PAYMENT_BLOCKING_FAULTS` does not disable payment.
- `table()` rows carry a `gate` value for every permissive.

`tests/test_vmc*.py`:

- Boot with an open session snapshot raises `PAY-104`, records
  `session_uncertain`, and leaves `payment_enabled` true.
- `clear_fault("PAY-104")` still removes the evidence file and still fails
  closed if the file cannot be removed (existing behaviour, must not regress).
- `select_product` is refused while `vending_alive` is false, and the refusal
  message names the failing permissive.

`tests/test_web_routes.py`:

- The status fragment renders "Machine Stopped" when a safety fault is active
  and the amber "still selling" variant when only `PAY-104` is.

## Out of scope

- Automatic reconciliation of `PAY-104` against the payment gateway.
  `VMC.reconcile_session` stays a stub; the contract has no credit-query
  message yet.
- Changing any product-scope fault behaviour. Per-SKU lockouts keep working
  exactly as they do today.
- Re-tuning heartbeat timeouts.
