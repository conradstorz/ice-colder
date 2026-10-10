# VMC public surface: design

Date: 2026-10-09. Status: implemented (PRs #45-#49).

## Problem

After eight extractions (`FaultRegistry`, `EscrowLedger`, `RefundProtocol`,
`SessionRecovery`, `TelemetryRouter`, `MaintenanceLease`, `TaskRunner`,
`DispenserProfileGate`) `controller/vmc.py` is 2586 lines, but about 40 of
its ~120 methods are compatibility delegates and property aliases that exist
only because tests reach into VMC privates. A survey of `tests/` found 306
`vmc._<name>` references across 18 files. They fall into four groups:

| Group | Refs | What the test is doing |
|---|---|---|
| `_handle_mqtt_*`, `_process_payment` | ~125 | Injecting a hardware or payment event |
| `_raise_fault`, `_lockouts` | ~77 | Setting up or asserting fault state |
| `_dispense_timed_out`, `_expire_session`, `_maintenance_idle_expired`, `_maintenance_sweep_tick`, `*_task` | ~35 | Firing a timer by hand, or checking one was armed |
| `_sale_is_test`, `_pending_refunds`, `_session_store`, `_dispense_request_id`, … | ~30 | Reading internal state |

The debt is not that tests misbehave. It is that the VMC has no public
surface for events, time, or state, so tests use the only door that exists.
While that is true the alias layer cannot be removed and the internals cannot
move.

## Goal

Tests touch no `_underscore` name on the VMC. The VMC gains a public surface
for the three things tests need: injecting events, controlling time, and
reading state. Every alias property added during the extractions is deleted.
A test guard keeps the suite clean afterwards.

## Rule

Tests and routes may **read** a collaborator directly (`vmc.faults`,
`vmc.escrow`, …). Every **mutation** goes through a VMC method
(`raise_fault`, `deposit_funds`, `request_refund`, …), never by poking a
collaborator, so money and fault side effects can never be bypassed.

## 1. Public event port

The inbound handlers become public, named for the event, not the transport.
Signatures and async-ness are unchanged.

| Today | Becomes |
|---|---|
| `_handle_mqtt_payment` | `on_payment_credit(topic, data)` |
| `_handle_mqtt_button` | `on_button_press(topic, data)` |
| `_handle_mqtt_dispenser` | `on_dispenser_event(topic, data)` |
| `_handle_mqtt_refund_ack` | `on_refund_ack(topic, data)` |
| `_handle_mqtt_hardware_io` | `on_hardware_io(topic, data)` |
| `_handle_mqtt_payment_status` | `on_payment_status(topic, data)` |
| `_handle_mqtt_sensor` | `on_sensor_reading(topic, data)` |
| `_handle_mqtt_water_flow` | `on_water_flow(topic, data)` |
| `_handle_mqtt_heartbeat` | `on_heartbeat(topic, data)` |
| `_handle_mqtt_ice_maker_event` | `on_ice_maker_event(topic, data)` |
| `_handle_mqtt_capabilities` | `on_capabilities(topic, data)` |
| `_handle_mqtt_telemetry` | `on_telemetry(topic, data)` |
| `_handle_mqtt_command_ack` | `on_command_ack(topic, data)` |
| `_process_payment` | `process_payment()` |
| `_raise_fault`, `raise_data_fault` | one `raise_fault(code, *, sku=None, outcome=None)` |

`SUBSCRIPTIONS` in `controller/mqtt_inbound.py` stays the single
topic-to-method table and its pinned test is the wiring test. During the
first PR the old names remain as deprecated one-line aliases; they are
removed in the last PR.

## 2. Time under test

`VMC.__init__` gains `tasks: TaskRunner | None = None`, defaulting to a real
`TaskRunner()`. `TaskRunner.schedule` gains a keyword `label: str = ""` that
the real runner ignores. The VMC labels every timer it arms:
`"dispense_timeout"`, `"session_timeout"`, `"refund_deadline"`,
`"maintenance_idle"`, `"standby_sweep"`. The extracted classes that receive
`schedule` as a closure pass their own label.

`tests/fakes.py` provides `FakeTaskRunner` with the same surface as
`TaskRunner`: `attach(loop)`, `fire_and_forget(coro, *, persistent=False)`
(runs the coroutine to completion on the running loop), `schedule(delay,
callback, *, label="")` (records a `ScheduledCall(delay, callback, label,
task)` and returns a `FakeTask` with `done()`, `cancel()`, `cancelled`),
`drain_persistence(timeout)`, `cancel_pending()`, plus test helpers
`scheduled` (live calls), `fire(label)` (run and retire the most recent live
call with that label), `fire_all()`. A `conftest.py` fixture
`vmc_fake_time` builds a VMC on a `FakeTaskRunner` attached to the running
loop. With that in place the timer handlers stay genuinely private and the
`*_task` reads go away.

## 3. Readable state

Collaborators become public read-only properties: `faults`, `escrow`,
`refunds`, `recovery`, `lease`, `gate`, `tasks`, and the wired services
`session_store`, `mqtt_client`, `command_dispatcher`, `health_monitor`,
`availability`, `event_recorder`.

A frozen dataclass `SaleContext` in `controller/sale_context.py` holds the
in-flight sale: `product`, `shares: dict[str, float] | None`, `is_test`,
`mechanism: str | None`, `request_id: str | None`, `seq: int`,
`started_at: float`. The VMC exposes it as `vmc.sale`, `None` when no sale
is in flight, and replaces it (with `dataclasses.replace`) at sale start,
after payment consumes shares, when a dispense request id is minted, and
clears it at the end. `selected_product`, `pending_sale_shares`,
`_sale_is_test`, `_sale_mechanism`, `_dispense_request_id`, `_dispense_seq`
fold into it; `selected_product` and `pending_sale_shares` remain as
read-only properties over `sale` because the FSM callbacks and routes read
them.

Kept as public conveniences: `maintenance_hold`, `active_faults()`,
`credit_escrow`, `escrow_credits`, `has_credit`, `get_status()`.

Deleted: `_lockouts`, `_machine_faults`, `_pending_refunds`,
`_maintenance_hold` (and its setter), `_maintenance_idle_task`,
`_maintenance_sweep_task`, `_dispenser_profiles`, `_pending_tasks`,
`_persist_tasks`, `_loop`.

The one test that assigns `vmc._maintenance_hold = None` to simulate the
lease vanishing mid-run is rewritten to release the lease through
`vmc.lease.release("admin")`, which is a read of a public collaborator
followed by a method on it; that is allowed because the test is exercising
the lease, not bypassing the VMC's money or fault paths.

## 4. Sequencing

Five PRs, each green on its own, each reducing private access and never
adding to it:

1. **Public event port** (section 1). Old names kept as deprecated aliases.
   Tests repointed in the same PR. `SUBSCRIPTIONS` test updated.
2. **Public collaborators and `raise_fault`** (section 3 without
   `SaleContext`). `vmc._lockouts["X"] = code` becomes
   `vmc.raise_fault(code, sku="X")`; reads become `vmc.faults.is_locked`.
   The rule is checked case by case: a test that bypassed side effects on
   purpose is rewritten, not renamed. Alias properties deleted at the end.
3. **Fake scheduler** (section 2). Labels, `FakeTaskRunner`, the fixture,
   timer sites rewritten to `fire(label)`, `*_task` reads become
   `scheduled` assertions.
4. **`SaleContext`** (section 3's dataclass). The only PR that touches the
   money path; full suite plus a focused review of `process_payment`,
   `on_vend_failed`, `_record_sale`.
5. **Guard and cleanup**. A test in `tests/test_no_private_access.py` fails
   on any `vmc\._` match in `tests/` (excluding itself); deprecated aliases
   from PR 1 removed; CLAUDE.md describes the public surface and the rule.

## Testing

Every PR runs the full suite (2415 tests at the time of writing, 15 opt-in
browser skips). PRs 1 to 3 add no behavior and need no new behavior tests
beyond the `SUBSCRIPTIONS` pin and `FakeTaskRunner`'s own small test. PR 4
adds unit tests for `SaleContext` replacement at each transition. PR 5 adds
the guard test. Each PR is reviewed by Copilot and merged only when CI and
the review are clean.

## Out of scope

The sale-lifecycle extraction and the test-sale runner extraction. This spec
makes them possible by removing the alias layer; it does not do them.
