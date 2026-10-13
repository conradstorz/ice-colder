# VMC Diet — Design

Date: 2026-10-12. Scope: `controller/vmc.py` and the collaborators it grows.

## 1. Problem

`class VMC` is 1,621 lines, of which 823 are code, 352 comment-only lines, 327
docstring lines. Of the code, ~230 lines are logging and customer-message
call sites that repeat the same fact to `logger`, `txn_log` and `vend_log`,
and ~100 lines are the same settle choreography (cancel the dispense cycle,
restore or refund escrow, clear the selection, `state_changed`, `display`,
`refresh`, message) retyped in six FSM hooks. The `transitions` library is
fought in two places (`_fail_vend`, `_expire_session` call `set_state`
directly). The seven FSM hooks are string-named callbacks a reader cannot
follow from a call site.

## 2. Goal

Halve the VMC's code and comments without changing any observable behaviour:
same log lines to the same sinks, same event-recorder rows, same customer
messages in the same order, same `StatusOutputs` calls, same money movement
(FIFO share restore, refund-only-through-`request_refund`, test-credit
never refunded, `is_test` chokepoints untouched).

## 3. Design

### 3.1 `controller/sale_journal.py` — `SaleJournal`

One class owning every log line, event-recorder row and (where it is adjacent
to the log call today) the customer message the sale FSM emits. One method
per domain event, named for the event, taking plain values. The VMC calls
`self._journal.<event>(...)` instead of two or three logger calls. Strings are
byte-identical to today's. Constructed by `Machine` with the same
`recorder` callable the VMC already receives and the `outputs.message`
callback. Nothing in it reads FSM state.

### 3.2 Settlement helpers on the VMC

- `_drop_sale()`: cancel the cycle, `self._cycle = None`,
  `selected_product = None`, `last_insufficient_message = ""`.
- `_settle(outcome, fault_code, run)`: capture `self._sale`, call `run()`
  (the transition that clears it), then `_notify_settled` with the captured
  sale. Replaces the three copy-pasted "capture before, notify after" sites.
- `on_dispenser_event` split into `_dispense_succeeded(report, slot)` and
  `_dispense_failed_report(report, slot)`.
- `_expire_session` collapsed to one body: no-op in `dispensing`, no-op at
  zero escrow outside `interacting_with_user`, else journal + refund +
  `_drop_sale` + land (force idle only from `interacting_with_user`).

### 3.3 `controller/session_timer.py` — `SessionTimer`

`SessionTimer(tasks, seconds, on_expire)` with `arm()`, `cancel()`,
`armed`. Label stays `"session_timeout"` so `FakeTaskRunner.fire` keeps
working. Replaces `_session_timeout_task`, `_reset_session_timeout`,
`_cancel_session_timeout`.

### 3.4 Explicit transitions, no `transitions` dependency

`self.state` becomes a plain attribute. The seven triggers become explicit
methods on the VMC (`start_interaction`, `dispense_product`,
`complete_transaction`, `cancel_sale`, `vend_failed`, `error_occurred`,
`reset_state`), each one line over `_transition(sources, dest, before)`,
which checks the source set, runs the hook, assigns the state and calls
`_after_state_change`. An invalid trigger raises `InvalidTransition`
(`RuntimeError`), as `transitions.MachineError` did. `set_state(state)` is
the public seam the two in-FSM bypasses and ~50 tests use; `vmc.machine` is
gone. `TRANSITIONS` the table is gone; the state diagram comment moves onto
the trigger methods.

### 3.5 Comment diet and dead code

Comments that narrate history (PR numbers, review-finding ids, task
numbers, "design section N" citations) are removed. Comments restating the
code are removed. A "why" comment is kept and cut to its point. Docstrings
state contract and invariants only. Dead code goes: `owner_contact`,
`_update_selection_message`, `reconcile_session` (and its stub test), the
debug dump of the whole config at init.

### 3.6 Ratchet

`tests/test_vmc_complexity.py` measures, with `ast` and `tokenize`, the
VMC class's code lines (non-blank, non-comment, non-docstring), its total
lines, and the largest method's code lines, against budgets in the test
file. Over budget fails; more than 10% under budget fails with "lower the
budget to N". Each stage lowers the numbers.

## 4. Non-goals

The effects-list ("decide then apply") split is not in scope. The money
paths (`on_vend_failed`, `_fail_vend`, `request_refund`, `begin_test_sale`,
`end_test_sale`) keep their exact statement order. No new persistence path.

## 5. Delivery

One branch and one PR per stage. Each PR gets a Copilot review and CI; a
gate script polls both, surfaces Copilot's comments for action, and the PR
is merged only when CI is green and every Copilot thread is resolved.
