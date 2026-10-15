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

Source: `2026-10-13-test-suite-followups-todo.md`, which is still uncommitted. **Its file paths predate the workspace reorg** (`tests/test_web_routes.py` is now under `tests/edge/web/`, `services/` is now `ice_edge/...`, and so on). Fix the paths first.

- [x] B.0 Commit the follow-ups todo, with its paths updated to the new layout (quick win)
- [ ] B.1 §1 Coverage gaps (1.1–1.5); deep work, and 1.3, the DST fall-back hang in `by_period`, is a real bug, so do it first
- [ ] B.2 §2 Proof gaps (2.1–2.3): get red runs for fixes that never had one
- [ ] B.3 §3 e2e timing (3.1–3.3); 3.3 is passive, so watch CI and record any flakes
- [ ] B.4 §4 Private-access ratchet (4.1–4.14): one file per quick-win session, budgets only go down
- [ ] B.5 §5 Second review pass (5.1–5.2)

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

- 2026-10-15 — done: created this file; B.0 (follow-ups paths updated and committed) | stopped at: B.0 | next: A.0 or B.1 (start with 1.3, the DST hang)
