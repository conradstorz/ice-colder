# Test suite follow-ups — TODO

These are the follow-ups to `2026-10-13-test-suite-review-todo.md`, which is fully closed (merged in PR #73, `98a4762`). They are leftovers and gaps found while fixing that list. None has been through a dedicated review pass, so confirm each one against the code before fixing it.

Paths were updated on 2026-10-15 to the layered-workspace layout (`tests/<package>/...`, `packages/*/src/...`). Bare filenames in 4.14 are unique under `tests/`. The cited line numbers and the private-access budgets in §4 were re-checked against the code at that date and still match.

## How to use this file (for the implementing agent)

- Work in priority order: section 1 first.
- Read the cited code and confirm the gap before fixing it. If it doesn't hold, tick the item and append `— rejected: <reason>`.
- When an item is done, change `- [ ]` to `- [x]` and append `— <commit sha>`. Commit this file alongside the fix.
- A fixed test must fail against the bug it guards. Break the production code temporarily, watch the test go red, then restore it.
- Never raise a number in `tests/private_access_budget.py` or `tests/meta/test_vmc_complexity.py`. Lower it when a fix drops the count.
- Run the e2e tests only against a broker (CI has one). Never start Docker on this workstation.

## 1. Coverage gaps

- [ ] 1.1 Permission matrix (`tests/edge/web/test_web_routes.py`, `_MATRIX_ROUTES`): there are no role-denial cases for `POST /reports/email`, `POST /users/codes/regenerate` or `POST /tests/run-all`. They were left out because a dummy body isn't safe. Fix: stub the mailer, regenerate and dispatcher side effects, then add each route with its hard-coded allowed roles, asserting that a denied role gets 403 and no side effect happens.
- [ ] 1.2 `packages/edge/src/ice_edge/web/routes/products/catalog.py:131,191,233` and `placement.py:120` guard on `context.machine_instance`, but no test sets the machine to None (review item 1.14 covered only `vmc_instance`). Fix: one test per mutating products route with `machine_instance` monkeypatched to None, asserting a 200 and that the catalog change was saved.
- [ ] 1.3 `reports.by_period` hangs forever on the DST fall-back case when `_floor_to_bucket` floors to the previous day (found in review item 3.4). A broken helper should fail a test, not hang the suite. Fix: add a pytest timeout (or an iteration guard asserted in the test) around the DST fall-back `by_period` tests. Separately, decide whether `by_period`'s bucket loop should have a production-side bound and raise instead of hanging.
- [ ] 1.4 `packages/edge/src/ice_edge/sale/fsm_control.py`: `restart` and `shutdown` are logging stubs, and `tests/edge/sale/test_fsm_control.py` pins only their messages (review item 4.10). There's no test to fix until real behaviour exists. When the stubs are implemented, replace the message-only tests with assertions on the actual effect (patch the exit or signal call). Track this here so the stubs aren't mistaken for tested behaviour.
- [ ] 1.5 `tests/ui/test_feedback_assets.py:~58-85` and `tests/ui/test_templating.py`: substring checks on source text (`'new Audio("/static/click.wav")'`, `.play()`) and `get_template(...) is not None`. The browser tests cover the real behaviour. Fix: delete what the touch-feedback browser test already covers, and keep only the checks it can't express (e.g. that the asset file exists and is a valid WAV).

## 2. Proof gaps (fixes that have no red run)

- [ ] 2.1 Review items 4.3 and 4.4 (`tests/edge/web/test_web_routes.py`: the `/login/enroll` status pinned to 401; idle-clock `>`): their agent reported no red run. Fix: run the mutation (route returns 403; refresh writes an older `last_active_at`), confirm the test goes red, and record it here.
- [ ] 2.2 Two of the 2.5 sites in `tests/edge/sale/test_vmc_flows.py`, `test_on_error_without_credit_says_contact_support_only` and `test_deposit_funds_test_method_accepted_other_methods_refunded`: `drain_pending` was added, but no red run proves the negative assertion is no longer vacuous. Fix: find a mutation that produces a delayed stray refund without breaking the earlier positive assertion, or add a positive control.
- [ ] 2.3 `tests/sims/test_simulator_ice_maker.py` (review item 3.11): the red proof changed `COMPRESSOR_OFF_TIME`, and the tests followed the change, so it proved derivation but not detection. Fix: show that the new "still off one tick before the boundary" assertion fails when the compressor turns on early, e.g. by temporarily shortening the transition condition rather than the constant.

## 3. Timing in the e2e tests (now running in CI)

- [ ] 3.1 `tests/integration/test_integration_e2e.py:216`: `await asyncio.sleep(0.3)`. It is the documented residual, which CLAUDE.md cites as "Deliberate residual fixed sleep". Now that these tests run on every PR, it is a flake risk. Fix: expose a public observable for the dispatcher having consumed the ack (or poll the sale's settled state with a bounded loop), remove the sleep, and update CLAUDE.md.
- [ ] 3.2 `tests/integration/test_integration_e2e.py:140, 169, 275, 293, 362`: 50–100 ms poll intervals inside bounded loops. These are sanctioned, but check that each loop has a bound and a clear failure message, and that none is a fixed wait in disguise.
- [ ] 3.3 Watch the first ~10 CI runs of the e2e tests for flakes before relying on them, and record any failures here.

## 4. Private-access ratchet (lower the budgets)

Current budgets in `tests/private_access_budget.py` (only files with a non-zero budget are listed). Lower each one by promoting the private name to a public seam, or by testing through the public surface. Each line is its own item, done when the budget reaches 0, or reaches a floor whose remaining hits each carry a `# private: <reason>` marker.

- [ ] 4.1 `tests/sims/test_simulator_vending.py`: 53
- [ ] 4.2 `tests/sims/test_simulator_base.py`: 38
- [ ] 4.3 `tests/sims/test_simulator_ice_maker.py`: 28
- [ ] 4.4 `tests/sims/test_simulator_mdb.py`: 23
- [ ] 4.5 `tests/edge/monitoring/test_health_monitor.py`: 19
- [ ] 4.6 `tests/common/test_mqtt.py`: 19
- [ ] 4.7 `tests/edge/ledger/test_event_recorder.py`: 13. Includes the `EventRecorder.__new__` + hand-set private attributes harness at ~1022-1050, which bypasses `__init__`.
- [ ] 4.8 `tests/edge/access/test_access.py`: 7. Includes `_failures` and `_sessions`.
- [ ] 4.9 `tests/edge/reporting/test_report_scheduler.py`: 5
- [ ] 4.10 `tests/edge/web/test_web_routes.py`: 5
- [ ] 4.11 `tests/edge/hal/test_command_dispatcher.py`: 4
- [ ] 4.12 `tests/edge/web/test_routes_tests.py`: 3
- [ ] 4.13 `tests/edge/web/test_route_context.py`: 2. `getattr(context, "_pending_codes")` at lines 20 and 31. This budget went up 0 → 2 when the ratchet became AST-based. Fix: give `context` a public accessor for pending codes, or add it to `route_context.GLOBALS` handling.
- [ ] 4.14 The remaining budget-1 and budget-2 files: `fakes.py`, `test_contracts.py`, `test_dispensers_example.py`, `test_dispensers_service.py`, `test_dispensers_validation.py`, `test_inventory_manager.py`, `test_maintenance_standby.py`, `test_notifier.py`, `test_test_sale_runner.py`, `test_vmc_flows.py`.

## 5. Second review pass

- [ ] 5.1 The original review sampled each file rather than reading every test body. Run a second pass over the files with the most remaining risk: `tests/edge/ledger/test_event_recorder.py` (2068 lines), `tests/sims/test_simulator_vending.py`, `tests/sims/test_simulator_base.py`, `tests/edge/reporting/test_report_scheduler.py`, `tests/edge/hal/test_command_dispatcher.py`, `tests/edge/access/test_access.py`. Use the same checklist (assertions that can't fail, mirrored expectations, vacuous negatives, leaked state, wall-clock waits). Append the findings to this file as section 6.
- [ ] 5.2 Re-check the review items rejected as "low/speculative" in the original TODO, in case later changes made them real: 2.8 (complexity budget), 2.14 (`discoverable_globals`), 3.5 (the `tz=None` path), 4.9 (the TOML comment checks), 4.11 (the level-count pin).
