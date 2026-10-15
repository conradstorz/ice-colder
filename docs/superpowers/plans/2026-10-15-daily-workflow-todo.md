# Daily workflow — TODO

A daily checklist for moving ice-colder forward. Each session has the same shape: a green check, a task pick sized to the time available, the work itself, a separation-of-concerns review, then commit and log. The task queue below draws on two active streams, the customer screen and the test-suite follow-ups. Firmware and cloud hardening each get one slot per week.

## How to use this file

- Run **Start of session** every time, even for a 20-minute session.
- Pick work from **Task queue** to fit the time you have. A quick win takes under an hour; a deep-work item is a full plan task or a cluster of follow-ups.
- Run the **daily SoC review** before you commit, on any day the code changed.
- Finish with **End of session**. The log line is the hand-off to tomorrow, so say where you stopped.
- Each source plan or todo stays the source of truth for its own items. Tick items there, with the commit sha, and mirror the progress here.

---

## Start of session

- [ ] `git pull` on the current branch; `git status` is clean, or the WIP is known
- [ ] `uv sync`
- [ ] `uv run pytest` is green; if it isn't, fixing that is today's first task
- [ ] `ruff check .` and `ruff format --check .` are clean
- [ ] Read the last **Progress log** entry
- [ ] Pick today's tasks:
  - **Short on time (< 1 h):** one quick win (a ratchet item, a single coverage gap, a doc fix)
  - **Normal session (1–3 h):** one customer-screen plan task, or one follow-up section
  - **Long session (half day+):** two or three consecutive plan tasks, with a review between them

## Daily separation-of-concerns review

Run this after the work and before the commit.

**Automated guards**
- [ ] `uv run lint-imports` passes (package layers and `ice_edge` internal layer order)
- [ ] `uv run python scripts/check_package_isolation.py` passes
- [ ] `uv run pytest tests/meta` passes (private-access budget, VMC size ratchet, routes shape)

**Diff review** (`git diff main...HEAD`, or the day's commits)
- [ ] No layer leak: a lower package never imports a higher one, `ice_ui` never imports `ice_common`, and `ice_sims` never imports `ice_ui`
- [ ] No god-object regrowth: new behaviour goes to the owning collaborator (`FaultService`, `EscrowLedger`, `RefundProtocol`, `DispenseCycle`, ...), not to `VMC` or `Machine`
- [ ] The VMC still holds no transport or service handle; its outbound effects go through `StatusOutputs` and its faults through `FaultService`
- [ ] Routes and tests read collaborators but mutate only through public `vmc`/`machine` methods, with no new `._private` reach-ins
- [ ] Route handlers stay thin, with logic in services or view builders; `ice_cloud` renders `ice_ui` only and never reaches into `ice_edge`

**Docs drift**
- [ ] Update CLAUDE.md wherever the day's change contradicts it (paths, counts, contract versions, route table)
- [ ] Contract changes bumped `CONTRACT_VERSION` correctly (major for a wire break) and `uv run python -m ice_contracts.generate` was re-run
- [ ] After any template change, rebuild and commit `app.css`

## End of session

- [ ] Commit or push the WIP, even half-done work, on a feature branch, never `main`
- [ ] Open or update the PR when a plan task or follow-up section finishes
- [ ] Tick the items finished in the source plan or todo, with the commit sha
- [ ] Add a dated line to the **Progress log**: what got done, where you stopped, and the next step

---

## Task queue

### Stream A — Customer screen contract

Source: `2026-10-14-customer-screen-contract.md` (design: `../specs/2026-10-14-customer-screen-contract-design.md`). Branch: `docs/customer-screen-spec`, then an implementation branch.

- [ ] A.0 Merge or PR the spec and plan docs; cut an implementation branch
- [ ] A.1 Task 1: screen contract module and schemas
- [ ] A.2 Task 2: vending contract 2.0.0 (SCR-101, payment blocking, expected subsystem)
- [ ] A.3 Task 3: screen liveness (HealthMonitor overrides, boot grace, SCR-101 wiring)
- [ ] A.4 Task 4: pure screen-state builder
- [ ] A.5 Task 5: publisher, input assembly and sale hooks
- [ ] A.6 Task 6: wire the publisher and remove DisplayController
- [ ] A.7 Task 7: input handler, MQTT screen topics and capabilities
- [ ] A.8 Task 8: screen pairing and the screen principal
- [ ] A.9 Task 9: kiosk routes (SSE, input, heartbeat, pair)
- [ ] A.10 Task 10: kiosk renderer and browser test
- [ ] A.11 Task 11: screen simulator, Compose and end-to-end tests
- [ ] A.12 Task 12: documentation

Tasks 2 and 6 touch the contract version and the VMC's collaborators, so give them a full SoC review.

### Stream B — Test-suite follow-ups

These items were folded in from `2026-10-13-test-suite-followups-todo.md`, which was triaged against the code on 2026-10-15 and then retired (it is in git history, `5b7a3a1`). That file was the follow-up list to the closed review todo, PR #73.

**Rules for this stream:**
- Confirm each gap against the code before fixing it. If it no longer holds, tick it with `— rejected: <reason>`.
- A fixed test must fail against the bug it guards. Break the production code, watch the test go red, then restore it.
- Never raise a number in `tests/private_access_budget.py` or `tests/meta/test_vmc_complexity.py`; lower it whenever a fix drops the count.
- Run the e2e tests only against a broker (CI has one). Never start Docker on this workstation.
- Tick finished items here with `— <commit sha>`.

**Resolved at triage:**
- [x] B.0 Commit the follow-ups todo with post-reorg paths — `5b7a3a1`
- [x] The `by_period` DST fall-back hang (was 1.3) — rejected: `reports.py` now bounds the loop with `_MAX_BUCKET_KEY_RETRIES` plus a forced-progress guard, so it can no longer hang
- [x] Watch the first ~10 e2e CI runs for flakes (was 3.3) — 14 consecutive `ci.yml` runs (2026-10-13 to 10-14) succeeded, with one cancelled and no flakes

**Coverage gaps**
- [x] B.1 Permission matrix (`tests/edge/web/test_web_routes.py`, `_MATRIX_ROUTES`): add role-denial cases for `POST /reports/email`, `POST /users/codes/regenerate` and `POST /tests/run-all`. Stub the mailer, the regenerate and the dispatcher side effects, then assert that a denied role gets 403 and nothing else happens. — `d5a47e5` (PR #79)
- [x] B.2 `packages/edge/src/ice_edge/web/routes/products/catalog.py:131,191,233` and `placement.py:120` guard on `context.machine_instance`, but no test sets it to None. Add one test per mutating products route with the machine set to None, asserting a 200 and that the change was saved. — `8ef8b5f` (PR #79)
- [x] B.3 `tests/ui/test_feedback_assets.py` (`test_feedback_js_exists_and_registers_pointerdown`, `test_base_html_loads_feedback_script_in_head`, `test_boosted_swap_cannot_duplicate_audio`) and `tests/ui/test_templating.py::test_base_template_loads` check for substrings in source text. Delete whatever the touch-feedback browser test already covers, and keep the asset checks (a valid WAV, a reproducible generator, the files being served). — `29010f4` (PR #79; kept `test_boosted_swap_cannot_duplicate_audio`, which no browser test covers)

**Proof gaps** (these fixes never had a red run)
- [ ] B.4 `tests/edge/web/test_web_routes.py`: the `/login/enroll` status pinned to 401, and the idle-clock `>` check. Mutate each one (the route returns 403; the refresh writes an older `last_active_at`), watch the test go red, and record it.
- [ ] B.5 `tests/edge/sale/test_vmc_flows.py`: `test_on_error_without_credit_says_contact_support_only` and `test_deposit_funds_test_method_accepted_other_methods_refunded`. Prove the `drain_pending` negative assertion isn't vacuous: use a mutation that causes a delayed stray refund, or add a positive control.
- [ ] B.6 `tests/sims/test_simulator_ice_maker.py`: show that the "still off one tick before the boundary" assertion fails when the compressor turns on early, by changing the transition condition rather than `COMPRESSOR_OFF_TIME`.

**e2e timing** (`tests/integration/test_integration_e2e.py`)
- [ ] B.7 Line 216, `await asyncio.sleep(0.3)` (CLAUDE.md's "deliberate residual fixed sleep"): replace it with a public observable, or a bounded poll on the sale's settled state, and update CLAUDE.md.
- [ ] B.8 Lines 140, 169, 275, 293, 362: check that each poll loop has a bound and a clear failure message, and that none is a fixed wait in disguise.

**Private-access ratchet** (quick wins, one file per session). Counts were verified on 2026-10-15. Each item is done at 0, or at a floor where every remaining hit carries a `# private: <reason>` marker.
- [ ] B.9 `tests/sims/test_simulator_vending.py`: 53
- [ ] B.10 `tests/sims/test_simulator_base.py`: 38
- [ ] B.11 `tests/sims/test_simulator_ice_maker.py`: 28
- [ ] B.12 `tests/sims/test_simulator_mdb.py`: 23
- [ ] B.13 `tests/edge/monitoring/test_health_monitor.py`: 19
- [ ] B.14 `tests/common/test_mqtt.py`: 19
- [ ] B.15 `tests/edge/ledger/test_event_recorder.py`: 13 (includes the `EventRecorder.__new__` harness at ~1029-1050, which bypasses `__init__`)
- [ ] B.16 `tests/edge/access/test_access.py`: 7 (`_failures`, `_sessions`)
- [ ] B.17 `tests/edge/reporting/test_report_scheduler.py`: 5
- [ ] B.18 `tests/edge/web/test_web_routes.py`: 5
- [ ] B.19 `tests/edge/hal/test_command_dispatcher.py`: 4
- [ ] B.20 `tests/edge/web/test_routes_tests.py`: 3
- [ ] B.21 `tests/edge/web/test_route_context.py`: 2 (`getattr(context, "_pending_codes")` at lines 20 and 31; give `context` a public accessor)
- [ ] B.22 The budget-1 and budget-2 files: `tests/fakes.py`, `tests/contracts/test_contracts.py`, `tests/edge/vend/test_dispensers_{example,service,validation}.py`, `tests/edge/monitoring/test_inventory_manager.py`, `tests/edge/sale/test_{maintenance_standby,test_sale_runner,vmc_flows}.py`, `tests/edge/notify/test_notifier.py`

**Second review pass**
- [ ] B.23 The original review sampled files rather than reading every test. Read these in full against the same checklist (assertions that can't fail, mirrored expectations, vacuous negatives, leaked state, wall-clock waits): `tests/edge/ledger/test_event_recorder.py`, `tests/sims/test_simulator_{vending,base}.py`, `tests/edge/reporting/test_report_scheduler.py`, `tests/edge/hal/test_command_dispatcher.py`, `tests/edge/access/test_access.py`. Add the findings here as new B items.
- [ ] B.24 Re-check the old review items rejected as low or speculative, in case later changes made them real: 2.8 (complexity budget), 2.14 (`discoverable_globals`), 3.5 (the `tz=None` path), 4.9 (the TOML comment checks), 4.11 (the level-count pin). The original todo is in git history (`2026-10-13-test-suite-review-todo.md`).

**Parked** (not actionable yet)
- `packages/edge/src/ice_edge/sale/fsm_control.py`: `restart` and `shutdown` are logging stubs, and `tests/edge/sale/test_fsm_control.py` pins only their messages. When real behaviour lands, replace those tests with assertions on the effect (patch the exit or signal call).

### Weekly slot — Firmware and cloud hardening

One item per week, alternating between the two tracks. The output is research or a spec, not code, until a track is promoted to a stream.

- [ ] W.1 Firmware: survey ESPHome for an MDB gateway and the vending I/O board against `docs/contracts/vending-machine`
- [ ] W.2 Cloud: sketch auth and mTLS for `ice_cloud` (spec §8 prerequisites)
- [ ] W.3 Firmware: pick an ESP32 dev board, list the GPIO/channel mapping for one dispense slot
- [ ] W.4 Cloud: design the store-and-forward outbox for `ice_edge.uplink`
- [ ] W.5 Decide whether either track is ready to become a full stream

---

## Progress log

Newest entry first. Format: `YYYY-MM-DD — done: … | stopped at: … | next: …`

- 2026-10-15 — done: B.1–B.3 (coverage gaps), PR #79 open | stopped at: B.3 | next: merge #79; A.0 or B.4 (proof gaps)

- 2026-10-15 — done: triaged the follow-ups todo (two items already resolved), folded the rest into Stream B, retired the file | stopped at: — | next: A.0 or B.1
- 2026-10-15 — done: created this file; B.0 (follow-ups paths updated and committed) | stopped at: B.0 | next: A.0 or B.1 (start with 1.3, the DST hang)
