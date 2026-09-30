# Maintenance Standby — Implementation Plan

**Date:** 2026-09-29
**Spec:** `docs/superpowers/specs/2026-09-25-system-tests-design.md` §2.2a
**Branch:** `feat/maintenance-standby` from `main` at 08dde84

Plans give each task its interface, behaviour and tests. No code bodies.

## Why

Every test on the Tests level refuses with "machine is mid-sale" whenever a
customer (or the simulator's synthetic customer) has credit on the machine.
The lease from §2.2 is opportunistic and can only be taken on an idle
machine. A tech at the machine needs to *make* it idle: refund whatever is
on the display, hold the machine out of service for the whole of their
login, and hand it back with one tap (or by locking the tablet).

## Waves

| Wave | Tasks | Files touched (disjoint within a wave) |
|---|---|---|
| 1 | Task 1 (VMC standby lease), Task 2 (AccessStore liveness + wiring) | `controller/vmc.py`, `tests/test_maintenance*.py` / `services/access.py`, `main.py`, `tests/test_access*.py` |
| 2 | Task 3 (Tests level + hero) | `web_interface/routes/tests_level.py`, `web_interface/routes/home.py`, `web_interface/context.py`, `web_interface/templates/tests.html`, `partials/tests_hold_banner.html`, `partials/test_refusal.html`, `partials/status_fragment.html`, `web_interface/static/app.css`, `tests/test_web_routes.py` |
| 3 | Task 4 (whole-branch review), Task 5 (docs) | review only / `CLAUDE.md` |

Models: Tasks 1–3 Sonnet (FSM, auth and route logic), Task 4 Sonnet, Task 5 Haiku.

## Task 1 — VMC: `begin_standby` and the session-bound lease

**Interface** (`controller/vmc.py`):

- `MaintenanceHold` gains `standby: bool = False`.
- `VMC.begin_standby(user_id: str, session_id: str) -> tuple[bool, str | None]`.
- `VMC.set_session_liveness(predicate: Callable[[str], bool] | None) -> None`.
- `VMC.STANDBY_SWEEP_SECONDS = 30.0` (class attribute, so tests can shrink it).

**Behaviour** (spec §2.2a, read it first):

- `begin_standby` refuses only in `dispensing` ("vend finishing, tap again")
  and when another session holds the lease ("held by <id>"). If the caller's
  own session already holds an opportunistic lease it is upgraded in place:
  `standby = True`, idle timer cancelled, sweep armed. Otherwise:
  - `interacting_with_user`: refund escrow via `request_refund(reason="maintenance")`,
    then fire the existing `cancel_sale` transition (whose `on_cancel_sale`
    already clears the selection, cancels the session timer and refreshes
    the display — check whether it refunds too, and refund exactly once).
  - `idle` with escrow: refund via the same path, cancel the session timer.
  - `error`: refund the same way; stay in `error`.
  - Then grant the lease exactly as `begin_maintenance` does (raise `SVC-102`,
    log) but with `standby=True`, **no** idle timer, and the sweep armed.
- The sweep: a repeating `_schedule(STANDBY_SWEEP_SECONDS, ...)` chain that
  runs only while `_maintenance_hold is not None and hold.standby`. Each
  tick calls the liveness predicate with `hold.holder_session_id`; when it
  returns False, release with `by="session_ended"` — deferred through
  `release_requested` if `runs_in_flight > 0`, exactly like the idle timer.
  If no predicate is wired, `begin_standby` arms the ordinary idle timer
  instead of the sweep (degrades to the opportunistic lease) and logs a
  warning once.
- `_maintenance_idle_expired` returns immediately for a standby lease (belt
  and braces — the timer should never be armed for one).
- `take_over_maintenance` keeps its rule but carries `standby` across to the
  new holder and re-arms the sweep for the new session id.
- `_release_maintenance_hold` also cancels the sweep task.
- `begin_maintenance` is unchanged in behaviour. Its refusal strings stay
  as they are; the route maps them to the operator wording (Task 3).

**Tests first** (`tests/test_maintenance_standby.py`, using whatever fixture
`tests/test_maintenance_hold.py` or the nearest existing lease test uses to
build a VMC with an event loop and a fake refund path):

1. Standby from `interacting_with_user` with $1.00 escrow: one refund of
   $1.00 with reason `maintenance`, FSM `idle`, escrow 0, lease held with
   `standby=True`, `SVC-102` active.
2. Standby from `idle` with stranded escrow: refunded, session timer
   cancelled, lease held.
3. Standby from `error` with escrow: refunded, FSM still `error`, lease held.
4. Standby during `dispensing`: refused with "vend finishing, tap again",
   nothing refunded, no lease.
5. Standby while another session holds the lease: refused "held by <id>".
6. Standby upgrading the caller's own opportunistic lease: same hold object,
   `standby` now True, idle timer task cancelled.
7. No idle release: advance past `MAINTENANCE_IDLE_TIMEOUT_SECONDS`; lease
   still held.
8. Sweep releases on a dead session: predicate returns True then False;
   after one sweep interval the lease is gone, `SVC-102` cleared, log says
   `session_ended`.
9. Sweep defers while a run is in flight: predicate False inside
   `maintenance_test_run()`; lease remains until the run finishes, then
   releases.
10. No predicate wired: standby falls back to the idle timer.
11. Takeover of a standby lease keeps `standby=True` and sweeps the new
    session id.

Run `uv run pytest tests/test_maintenance_standby.py tests/test_vmc*.py
tests/test_maintenance*.py` green, then `ruff check --fix .` and `ruff format .`.

## Task 2 — AccessStore liveness predicate and wiring

**Interface** (`services/access.py`): `AccessStore.session_is_live(session_id: str | None) -> bool`.

**Behaviour:** True iff `resolve_session(session_id)` would return a session
right now, computed with the same rules (unknown id, missing user or
device, disabled user, idle limit by device kind, absolute cap) but
**without** touching `last_active_at` and without deleting the expired
entry. Factor the rule into one private helper both methods use so the two
can never drift.

**Wiring** (`main.py`): right after the VMC is constructed and
`routes.set_vmc_instance(vmc)`, call
`vmc.set_session_liveness(access_store.session_is_live)`.

**Tests first** (`tests/test_access_sessions.py` or the existing session
test module):

1. Live session → True, and `last_active_at` unchanged after the call.
2. Idled-out shared-device session → False, and the entry is still present
   (so `resolve_session` remains the one that forgets it).
3. Ended session (`end_session`) → False.
4. Disabled user → False.
5. `None` and unknown ids → False.

## Task 3 — Tests level, refusal wording, home hero

Depends on Tasks 1 and 2 being merged into the branch.

**Routes** (`web_interface/routes/tests_level.py`):

- `GET /tests/standby/confirm` (gate `run_tests`) renders the service-state
  card in its confirming or plain state, honouring the `confirming` query
  parameter exactly as `partials/confirm_button.html` requires (absent or
  anything but `"false"` → Confirm/Cancel pair).
- `POST /tests/standby` (gate `run_tests`, `require_htmx`) calls
  `vmc.begin_standby(principal.user.id, principal.session.id)` and
  re-renders the service-state card; a refusal renders inside the card.
- `_acquire_lease_or_refusal` maps the two busy refusals from
  `begin_maintenance` ("machine is mid-sale", "credit is still on the
  machine") to "machine is busy — take it out of service first"; the
  "held by" wording is unchanged.
- `/tests/end` and `/tests/takeover` re-render the service-state card
  (which replaces `tests_hold_banner.html`; keep the partial's filename or
  rename it, but the `#tests-hold` target id stays).

**Templates:**

- `partials/tests_hold_banner.html` becomes the service-state card: in
  service → the two-tap **Take out of service** button
  (`confirm_button.html` with `post_url=/tests/standby`,
  `confirm_url=/tests/standby/confirm`, `target=#tests-hold`); out of
  service → "Out of service since HH:MM, held by <name>" (resolve the name
  through `context.access_store.users`; fall back to the id), **Return to
  service** for the holder (posts `/tests/end`), **Take over** otherwise.
  A standby lease and an opportunistic one render the same card; the
  opportunistic one adds "(releases after 5 min idle)".
- `tests.html` always renders the card (no more `{% if hold %}` around it).
- `partials/test_refusal.html` unchanged; the busy wording comes from the
  route.
- `partials/status_fragment.html`: when `maintenance` is set in the
  snapshot, the hero reads "Out of service — maintenance by <name>" and
  the `SVC-102` line is dropped from `issues`. Add `maintenance` (holder
  display name or None) to `context.health_snapshot()`; it must not change
  `is_healthy`.
- Rebuild `web_interface/static/app.css` with the Tailwind CLI if any new
  class is used, and commit it. `tests/test_static_css.py` must pass.

**Tests first** (`tests/test_web_routes.py`, using the existing `login_as`
fixture and the fake VMC pattern the Tests routes tests already use):

1. `GET /tests` as tech shows "Take out of service" when no lease.
2. `GET /tests/standby/confirm` renders Confirm/Cancel; with
   `?confirming=false` renders the plain button.
3. `POST /tests/standby` calls `begin_standby` with the caller's user and
   session ids and renders "Out of service"; the response contains
   "Return to service".
4. `POST /tests/standby` refused (fake returns `(False, "vend finishing, tap again")`)
   renders that reason inside `#tests-hold`.
5. `POST /tests/run-all` with `begin_maintenance` refusing "machine is
   mid-sale" renders "take it out of service first".
6. Loader (no `run_tests`) gets 403 on both standby routes.
7. `GET /status` with an active `SVC-102` and a holder renders "Out of
   service — maintenance by <name>".
8. The existing `test_no_tile_is_coming_soon` and all Tests-level tests
   still pass.

## Task 4 — Whole-branch review

One Sonnet reviewer reads the full `git diff main...feat/maintenance-standby`
against spec §2.2a with these questions: Is escrow refunded exactly once on
every standby path? Can the sweep ever refresh the session it checks? Can
a standby lease outlive its session by more than one sweep interval plus
an in-flight run? Does any path leave `SVC-102` raised with no hold, or a
hold with no `SVC-102`? Does `_acquire_lease_or_refusal` still refuse a
different session's lease? Is a customer `select_product` during standby
still refused (`_sale_is_test` false)? Findings are fixed by the reviewer
before the PR is opened.

## Task 5 — Docs

Update `CLAUDE.md`'s maintenance-lease paragraphs: the `standby` flag,
`begin_standby`, the session sweep and `session_is_live`, the two new
routes in the URL table (`/tests/standby/confirm`, `POST /tests/standby`).
Keep it to the facts a future contributor needs; no narrative.

## Acceptance

On the simulation host with the vending simulator pressing buttons every
30–90 s: open Tests as the owner, tap Take out of service twice, see "Out
of service", press Run all and get results for every subsystem, press Lock,
log back in within 30 s and see the machine back in service.
