# System Tests Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking. Dispatch by **wave** — see "Execution schedule"; this is not a flat task list.

**Goal:** Let a tech prove a subsystem works without making a sale, and stop the machine selling while they do it.

**Architecture:** The ice maker's private command/ack pattern is generalised into a shared channel every subsystem speaks, with the models moved to `contracts/common.py` and re-exported so today's firmware keeps validating. One `CommandDispatcher` on the VMC correlates acks by `request_id` and retries with the *same* id, which the subsystems' idempotency caches make safe. A maintenance **lease** raises `SVC-102`, which `availability.py` treats as a safety row, so payment is inhibited for the duration. Test-ness lives on the individual sale, not on the global hold.

**Tech Stack:** Python 3.12, aiomqtt, Pydantic v2, FastAPI, Jinja2, HTMX 1.9.10 (vendored), SQLite, loguru, pytest, `uv`, `ruff` **0.11.2 pinned — always `uv run ruff`, never bare `ruff`**.

**Spec:** `docs/superpowers/specs/2026-09-25-system-tests-design.md`
**Program plan:** `docs/superpowers/plans/2026-09-25-dashboard-v2-program.md` (part 4 of 4)
**Builds on:** parts 1 (#19), 2 (#20) and 3 (#21), merged as `5e95448`

## How this plan is written

Two owner-approved rules from program plan §3.1 and §3.2 govern this document and **override** the `writing-plans` skill's default of embedding code bodies.

1. **Prose-plan rule.** Each task gives its spec section, the files it may touch, the interface (exact names, signatures, topics, payload shapes, routes), the behavior in prose, and the tests to write first. **The implementer writes the code.** A code block appears only for a wire format, SQL, a shell command, or a genuinely non-obvious algorithm.
2. **Wave rule.** Tasks are grouped into numbered waves whose "files it may touch" sets are pairwise disjoint and whose tests share no fixture being edited in the same wave. A wave's implementers run in parallel, then its reviewers in parallel, then one full `uv run pytest` and `uv run ruff check .` before the next wave. A reviewer failure returns only its own task. Anything touching a shared file is a **serial step**.

## Verification rules for this part

Parts 1–3 produced nineteen hollow tests or lying-green signals between them. Part 4 touches the MQTT contract that real firmware speaks and a fault that stops the machine selling, so:

- **Behaviour, not strings.** A dispatcher test must prove correlation by actually delivering an ack; an idempotency test must prove the side effect happened **once**, not that two acks arrived; a lease test must prove payment was actually inhibited, not that a flag was set.
- **Every new test must be shown to fail against the pre-change implementation.** Break it, watch it fail, restore, watch it pass, report both observations.
- **Beware the fixture that makes the branch unreachable** — the commonest hollow test in this program.
- **Wire compatibility is a test, not a claim.** Today's ice-maker ack payload, byte for byte, must still validate against the moved model, and the production `cmd/dispense` path must still vend with a simulator that knows nothing about the command channel.
- **One agent per worktree at a time.** Within a wave, parallel implementers are safe *only* because their file sets are disjoint. Never dispatch an out-of-band fix agent while a wave is in flight.

## Global Constraints

Copied from the spec. Every task's requirements implicitly include this section.

- **Production topics are untouched.** `cmd/dispense` for real sales, `cmd/payment/enable`, `cmd/payment/refund` stay exactly as they are. The command channel is **additive**; deployed firmware that ignores it simply advertises no tests. Any diff that changes a production topic's shape or timing is a defect.
- **Wire compatibility.** `SubsystemCommand` and `CommandAck` move to `contracts/common.py` with the ice-maker module re-exporting them under the old names (`MonitorCommand`, `CommandAck`). Every existing field survives, including required `command` and optional `detail`. The **only** addition is an optional `result` on the ack.
- **The `command` literal widens to `str`**, with per-command param validation moved to a registry keyed by command name. The ice maker's `power_cycle` rule — `dwell_seconds` in **5–300** — is preserved, as is `set_interval`'s.
- **Topics:** every subsystem subscribes to `vmc/<machine_id>/cmd/<subsystem>` and acks on `vmc/<machine_id>/cmd/<subsystem>/ack`. Unknown commands answer `unsupported`. A subsystem answers within **10 s** or the VMC treats it as a timeout.
- **Idempotency is part of the contract.** Every subsystem keeps the last **32** `request_id`s with their acks and, on a duplicate, republishes the cached ack **without repeating the side effect**. The dispatcher's retry depends on this.
- **Dispatcher:** `timeout=10.0`, `retries=1`, retry reuses the **same `request_id`**, raises `CommandTimeout(subsystem, command)` after the last attempt. Refunds keep their own path (spec §9).
- **`TESTABLE_COMMANDS` is a server-side allowlist** in `contracts/common.py`, containing exactly §1.2's standard commands and §1.3's actuator commands. A button exists only for a command both allowlisted **and** advertised; `POST /tests/{subsystem}/{command}` re-checks the allowlist so a crafted request cannot invoke `refund` or `payment/enable`.
- **`SVC-102 maintenance test in progress`**: scope machine, gate class **safety**, added to `PAYMENT_BLOCKING_FAULTS` (taking it from six members to seven). `SVC-101` is service-door and stays untouched.
- **The hold is a lease**, not a flag: `MaintenanceHold(holder_user_id, holder_session_id, started_at, last_activity_at, runs_in_flight)`. Granted only when the FSM is `idle` with zero escrow and no lease exists. Released only by the holder's session and only when `runs_in_flight == 0`, else marked `release_requested` and released by the last run settling. Idle timer is **5 minutes** since `last_activity_at` and behaves like a release request — it never clears a lease with runs in flight. **Take over** is enabled only when `runs_in_flight == 0` and the lease has been idle **60 s**, and records who did it.
- **Credit during a lease is refunded, never escrowed**, through the existing `request_refund(reason="maintenance")` path, and the `refund` event is recorded.
- **Test-ness is a property of the sale.** The in-flight sale context gains `is_test: bool`, set only by `run_test_sale`. The dispense completion handler consults *the sale's own flag*: a test sale records neither `sale` nor `dispense`, and writes `test_run` instead. A lease release or timer expiry cannot flip a sale from test to production.
- **`run_test_sale`** deposits the price as one credit with method `test` (the only credit `deposit_funds` accepts during a lease), runs the **normal** dispense path so the real FSM and production `cmd/dispense` are exercised, and clears escrow **without** a refund command.
- **Test log:** `test_run` events in the existing `events` table, 90-day retention; `value` is duration in seconds, `metadata` is `{run_id, user_id, user_name, subsystem, command, params, status, checks, verdict, note}`. `get_summary` ignores `test_run`. A run whose verdict was never entered shows `verdict: none`.
- **Gate `run_tests`** (owner, tech) on every Tests route.
- **The maintenance lease is never persisted** — a restart clears it.
- **Every POST keeps `require_htmx`.** No CDN reference may enter `web_interface/` beyond the existing ones in `templates/screen.html`, which stays byte-identical to `origin/main` along with `partials/screen_body.html`.
- **Commands:** `uv run pytest`, `uv sync`, `uv run ruff check --fix .` then `uv run ruff format .`. Never chain shell commands with `&&`. Never run Docker.
- **Out of scope:** migrating refunds onto the dispatcher, scheduled/automatic self-tests, real ESP32 firmware, remote fault injection from the dashboard.

## Carry-forwards from earlier parts

Three debts land as early tasks, because two of them are contract changes that later tasks would otherwise have to work around.

1. **`CONTRACT_VERSION` bump.** Part 3 added `DATA-101`/`DATA-102` to `contracts/vending_machine.py` without bumping, deliberately: `simulators/base.py` imports and advertises the constant, so bumping alone would have made the simulators advertise a version real firmware does not, lighting the health tab's mismatch warning for a dashboard-only change. Part 4 changes the contract for real, so it absorbs both bumps.
2. **`session_store`'s `saved_at`.** Incidental before part 3; now money-load-bearing, because `PAY-104` recovery reads the snapshot to decide whether a pending sale is recorded. It needs a direct guard.
3. **`DATA-101`'s description** reads "sale journal in use", accurate for a failed insert but wrong for the rejected-evidence path that also raises it.

## File Structure

| File | Responsibility |
|---|---|
| `contracts/common.py` | `SubsystemCommand`, `CommandAck` (+ optional `result`), the per-command param registry, `TESTABLE_COMMANDS` |
| `contracts/ice_maker_monitor.py` | Re-exports `MonitorCommand`/`CommandAck`; version bump; command list |
| `contracts/vending_machine.py` | `SVC-102`, version bump, `DATA-101` description fix, command list |
| `services/command_dispatcher.py` | New: `CommandDispatcher`, `CommandTimeout` |
| `services/availability.py` | `SVC-102` as a safety row |
| `services/event_recorder.py` | `test_run`, `update_metadata`, excluded from summaries |
| `services/session_store.py` | `saved_at` guard |
| `controller/vmc.py` | Maintenance lease, refund-during-lease, per-sale `is_test`, `run_test_sale` |
| `simulators/base.py` | Shared command loop, idempotency cache, `ping`/`self_test`/`force_report` |
| `simulators/vending_machine.py`, `ice_maker.py`, `mdb_gateway.py` | Actuator handlers, capabilities |
| `main.py` | Construct the dispatcher, hand it to the VMC and routes |
| `web_interface/routes/tests_level.py`, `templates/tests*.html` | The Tests level |
| `web_interface/levels.py` | Tests sub-level constants |
| `docs/contracts/*/CONTRACT.md`, `CLAUDE.md`, `ROADMAP.md` | Documentation |
| `tests/test_command_dispatcher.py`, `tests/test_routes_tests.py` | New |

## Execution schedule

| Step | Kind | Tasks | Why |
|---|---|---|---|
| 1 | **Wave 1** | 1, 2 | `session_store` + its test vs. `DATA-101`'s description + its test. Disjoint, and both are carry-forwards independent of the feature |
| 2 | **Serial** | 3 | `contracts/common.py` + both contract modules + generated schemas — the shared foundation, including both version bumps and `SVC-102` |
| 3 | **Serial** | 4 | `services/availability.py` + `PAYMENT_BLOCKING_FAULTS` wiring for `SVC-102` |
| 4 | **Serial** | 5 | `services/command_dispatcher.py` + `main.py` |
| 5 | **Serial** | 6 | `simulators/base.py` — the shared command loop and idempotency cache |
| 6 | **Wave 2** | 7, 8, 9 | The three simulators, one file each, all consuming Task 6's base |
| 7 | **Serial** | 10 | `controller/vmc.py` — the maintenance lease and refund-during-lease |
| 8 | **Serial** | 11 | `controller/vmc.py` again — per-sale `is_test` and `run_test_sale` |
| 9 | **Serial** | 12 | `services/event_recorder.py` — `test_run` and `update_metadata` |
| 10 | **Wave 3** | 13, 14 | Tests level routes + templates vs. `docs/contracts/*/CONTRACT.md`. Disjoint |
| 11 | **Serial** | 15 | `CLAUDE.md` + `ROADMAP.md` |

**3 waves, 8 serial steps, 15 tasks.** Tasks 10 and 11 are both `controller/vmc.py` and are deliberately sequential: the lease must exist before a test sale can require it, and one wave would put two implementers in a 1835-line file at once. Tasks 7–9 are a wave only because each simulator is its own file; they all *read* Task 6's base class but none modifies it.

**Model policy** (program plan §3.1): **Sonnet** for tasks 1, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13 (contracts, FSM, dispatcher, simulators, recorder, routes); **Haiku** for tasks 2, 14, 15 (a description string, contract docs, project docs). Reviewers **always Sonnet**.

**Whole-branch review before the PR** (program plan §3.4, added for this part): after Task 15 and before step D, a fresh Sonnet reviewer reads the entire branch diff against `origin/main` in one pass, hunting specifically for repeats of a class already fixed elsewhere in this branch or in an earlier part. Its findings go through the normal fix loop before the pull request opens.

---

## Wave 1 — tasks 1 and 2 in parallel

Both are carry-forwards from part 3, independent of the feature work and of each other.

### Task 1: Guard `session_store`'s `saved_at`

**Carry-forward from part 3** (recorded in that part's ledger), not in this spec.

**Files:**
- Modify: `services/session_store.py`
- Test: `tests/test_session_store.py`

**Why.** `SessionSnapshot.saved_at` defaults to `time.time()` at construction. That was incidental until part 3 made it money-load-bearing: `PAY-104` recovery reads the snapshot to decide whether a pending sale is offered for recording, and `controller/vmc.py` reasons explicitly about whether `saved_at` was rewritten in place. A snapshot whose `saved_at` is missing, zero, in the future, or silently re-defaulted on a reload would make that recovery path reason about the wrong moment.

**Interfaces — Produces:** no new public API. `saved_at` gains a guard so a snapshot loaded from disk keeps the value it was written with, and an absent or non-finite value is detected rather than silently re-defaulted to "now". Decide and state which of these you implement — reject, or preserve-and-flag — and why; the requirement is that a corrupt value is **visible**, not that it is papered over.

**Behaviour to get right:** a round-trip through `save` then `load` must preserve `saved_at` exactly, including across a process boundary (write, construct a fresh `SessionStore`, read). The existing `error` field is the established channel for an unreadable snapshot; use it rather than inventing a second signalling mechanism. Do not change `is_open()`'s semantics — part 1's `PAY-104` path depends on them.

**Tests to write first:** `saved_at` survives a save/load round trip exactly; a snapshot file with `saved_at` absent is handled the way you documented and does not silently become "now"; a non-finite or negative `saved_at` is likewise; `is_open()` behaviour is unchanged for both an open and a cleared snapshot.

**Done when:** `uv run pytest tests/test_session_store.py` and the full suite green, each new test shown to fail against the pre-change code. Commit: `fix(session): guard saved_at now that PAY-104 recovery depends on it`.

---

### Task 2: Correct `DATA-101`'s description

**Carry-forward from part 3.** Haiku.

**Files:**
- Modify: `contracts/vending_machine.py` (the `FAULT_TABLE` description string only)
- Modify: `ROADMAP.md` §5 if it quotes the description
- Test: `tests/test_contracts_vending.py` if it asserts the text

**Why.** The description reads "Sale journal in use", accurate when a `sales` insert failed and the row went to `data/sales-journal.jsonl`, but the same fault is also raised on the rejected-evidence path, where the row failed *both* the insert and the journal. An operator reading "journal in use" for that case is told the opposite of what happened.

**What to change:** the description text so it covers both cases — a sale could not be written to the database and is being held elsewhere. Keep it short enough for the fault card. **Change nothing else**: not the code, not the severity, not the scope, and `DATA-101` must stay out of `PAYMENT_BLOCKING_FAULTS`.

**Check before you write.** `docs/contracts/vending-machine/schemas/*.json` are committed generated artifacts and `tests/test_contract_schemas.py` enforces them byte-for-byte. If the description appears in a generated schema, regenerate with the project's own `contracts.generate` rather than hand-editing, and say so. Part 3's Task 3 hit exactly this and it is the likeliest way this one-line task fails.

**Tests to write first:** none new unless an existing test asserts the old string — in that case update it and say which. Prove you changed nothing functional: `PAYMENT_BLOCKING_FAULTS` still excludes `DATA-101`, its severity and scope are unchanged, and the full suite passes.

**Done when:** full suite green, `uv run ruff check .` clean. Commit: `docs(contracts): DATA-101 covers the rejected-evidence path too`.

---

## Serial step 2

### Task 3: The shared command contract

**Spec:** §1.1 (channel, payloads, idempotency requirement), §1.2 and §1.3 (command tables), §2.2 (`SVC-102`), §8 (files).

The most delicate task in the part: it changes models that **real firmware validates against**. A mistake here is not a dashboard bug, it is a fleet that stops acking.

**Files:**
- Modify: `contracts/common.py`, `contracts/ice_maker_monitor.py`, `contracts/vending_machine.py`
- Modify: the committed generated schemas under `docs/contracts/*/schemas/`, **regenerated with `contracts.generate`, never hand-edited**
- Test: `tests/test_contracts.py`, `tests/test_contracts_vending.py`, `tests/test_contract_schemas.py`

**Interfaces — Produces, in `contracts/common.py`:**
- `SubsystemCommand` — `request_id: str` (min 8, max 64), `command: str` (widened from the ice maker's `Literal`), `params: dict`, `timestamp: datetime`. Per-command validation moves to a registry keyed by command name, preserving `power_cycle`'s `dwell_seconds` in **5–300** and `set_interval`'s existing rule.
- `CommandAck` — `request_id`, `command: str`, `status: Literal["ok","rejected","failed","unsupported"]`, `detail: str | None`, **new** `result: dict | None = None`, `timestamp`.
- The param registry, so a new command declares its validation in one place rather than in a growing `model_validator` chain.
- `TESTABLE_COMMANDS: dict[str, frozenset[str]]` — exactly §1.2's three standard commands for every subsystem plus §1.3's actuator commands per subsystem, and **nothing else**. `payment/enable`, `refund` and `set_interval` must be absent; a test asserts their absence by name.
- `ACK_TIMEOUT_SECONDS = 10.0` as the contract's own constant, so the dispatcher and the docs cannot drift.

**In `contracts/ice_maker_monitor.py`:** re-export `SubsystemCommand as MonitorCommand` and `CommandAck`, so every existing import keeps working. Bump `CONTRACT_VERSION` from `1.1.0` (minor — the ack gains an optional field and the command channel is additive). Extend its advertised command list.

**In `contracts/vending_machine.py`:** add `FaultCode.SVC_102` with `FaultSpec(severity=…, scope=Scope.machine, description="Maintenance test in progress")`, add it to `PAYMENT_BLOCKING_FAULTS` (six members → **seven**), and bump `CONTRACT_VERSION` from `0.4.0` — this absorbs part 3's deferred bump as well, so the message should say both changes are in it.

**Behaviour to get right:**
- **Wire compatibility is the whole task.** A real ice-maker ack captured today — every existing field, no `result` — must validate unchanged. So must a `power_cycle` command with `dwell_seconds`, and `set_interval`. Prove it with literal payload dicts in the test, not with model round-trips, because a round-trip through your own new model proves only that it is self-consistent.
- Widening `command` to `str` must not lose `power_cycle`'s validation. A `power_cycle` missing `dwell_seconds`, or with `dwell_seconds=4` or `301`, must still raise.
- `severity` for `SVC-102`: it is a safety-gated machine fault that blocks payment while it is held. Choose from the existing `Severity` enum and justify the choice in the commit; do not invent a new member.
- Bumping `CONTRACT_VERSION` will change what `simulators/base.py` advertises. That is intended here (it is the "deliberate version bump" program plan §2 goal 6 reserves for part 4), but check whether any test pins the old value and update it deliberately rather than loosening the assertion.

**Tests to write first:** a literal present-day ice-maker ack payload validates against the moved `CommandAck`; the same ack with a `result` also validates; `power_cycle` without `dwell_seconds` raises, and 4 and 301 raise while 5, 30 and 300 pass; `set_interval`'s rule is preserved; an unknown command name constructs fine (the subsystem answers `unsupported`, the model does not reject); `TESTABLE_COMMANDS` contains `ping`/`self_test`/`force_report` for every subsystem and each §1.3 actuator command under the right subsystem; **`payment/enable`, `refund` and `set_interval` are absent from every entry**; `MonitorCommand` is still importable from `contracts.ice_maker_monitor` and is the same class; `SVC-102` exists, is in `PAYMENT_BLOCKING_FAULTS`, and that set now has exactly seven members; both `CONTRACT_VERSION`s changed; the committed schemas match the models.

**Done when:** full suite green, schemas regenerated with the project's tool, every new test shown to fail first. Commit: `feat(tests): shared subsystem command contract, SVC-102, and the deferred version bumps`.

---

## Serial step 3

### Task 4: `SVC-102` inhibits payment

**Spec:** §2.2 (availability), §8.

**Files:**
- Modify: `services/availability.py`
- Test: `tests/test_availability.py`

**Interfaces — Consumes:** Task 3's `FaultCode.SVC_102` and its membership in `PAYMENT_BLOCKING_FAULTS`.

**Behaviour to get right:** `SVC-102` becomes a **safety** row, so raising it publishes `cmd/payment/enable false` and blocks sales, and clearing it republishes `true` — provided nothing else is blocking. Follow the module's existing row structure rather than special-casing the code; the whole point of `PAYMENT_BLOCKING_FAULTS` is that membership, not bespoke logic, is the gate. Do not change the behaviour of the other six codes, and do not make `SVC-102` block anything a safety row does not already block.

**Tests to write first:** raising `SVC-102` publishes payment-disable exactly once and `payment_enabled` becomes False; clearing it republishes enable when nothing else blocks; with a *second* blocking fault also raised, clearing `SVC-102` alone does **not** re-enable payment; `SVC-102` appears in the permissives table with the safety gate class; the six pre-existing blocking codes behave exactly as before (assert against the existing expectations rather than rewriting them).

**Done when:** full suite green. Commit: `feat(tests): SVC-102 is a safety row that inhibits payment`.

---

## Serial step 4

### Task 5: `CommandDispatcher`

**Spec:** §2.1, §6 (broker down).

**Files:**
- Create: `services/command_dispatcher.py`, `tests/test_command_dispatcher.py`
- Modify: `main.py`

**Interfaces — Produces:**
- `CommandTimeout(Exception)` carrying `subsystem` and `command`.
- `CommandDispatcher(mqtt_client, timeout: float = 10.0, retries: int = 1, clock=…)` with `async send(subsystem: str, command: str, params: dict | None = None) -> CommandAck`.
- Registers `cmd/+/ack` **once** and correlates by `request_id`.
- Constructed in `main.py` and handed to the VMC and the routes, alongside the existing wiring.

**Behaviour to get right:**
- **The retry reuses the same `request_id`.** That is what makes it safe: the subsystem's idempotency cache replays the cached ack rather than repeating the action. A retry that minted a fresh id would fire an actuator twice, which for `dispense` means a second bag of ice. Test this explicitly by asserting the second publish carries the first id.
- An ack for an unknown or foreign `request_id` is ignored, not an error — other traffic shares the wildcard subscription.
- Concurrent `send`s to different subsystems must not cross-correlate. Test with two in flight at once.
- `clock` is injectable so timeout tests do not sleep 10 real seconds.
- Broker down: raise immediately rather than waiting out the timeout, so the Tests level can show every subsystem unreachable (spec §6).
- Use `ACK_TIMEOUT_SECONDS` from the contract as the default rather than repeating `10.0`.

**Tests to write first:** an ack delivered for the sent `request_id` resolves `send` and returns the parsed `CommandAck`; an ack with a foreign id is ignored and the original still times out; a timeout triggers exactly one retry and the retry publishes the **same** `request_id`; after the retry also times out, `CommandTimeout` is raised naming subsystem and command; two concurrent sends to different subsystems each receive their own ack; a `rejected`/`failed`/`unsupported` ack resolves normally (it is an answer, not an error); with the broker unavailable `send` raises promptly rather than after the full timeout.

**Done when:** full suite green; `main.py` wiring does not change startup ordering in a way that breaks `tests/test_main_supervise.py`. Commit: `feat(tests): CommandDispatcher with id-stable retry`.

---

## Serial step 5

### Task 6: The shared simulator command loop

**Spec:** §1.1 (idempotency), §1.2 (standard commands), §5 (`simulators/base.py`).

**Files:**
- Modify: `simulators/base.py`
- Test: `tests/test_simulator_base.py`

**Interfaces — Produces on `ESP32Simulator`:**
- A generic command loop subscribing to `cmd/<subsystem>` and acking on `cmd/<subsystem>/ack`, replacing the ice maker's private one.
- An idempotency cache of the last **32** `request_id`s with their acks; a duplicate republishes the cached ack **without** re-running the handler. The ice maker's existing `_acked` `OrderedDict` and its eviction are the model — lift them here rather than writing a second mechanism.
- `ping` — acks `ok`, no result.
- `self_test` — returns `{"checks": [{"name", "pass", "detail"}]}` with **one check per registered `FaultDef`**, failing the check whose fault is currently injected. This makes the existing fault-injection machinery double as the test fixture.
- `force_report` — republishes every sensor, channel and the heartbeat immediately, acks `ok`.
- A registration hook so a subclass adds its own commands (Tasks 7–9) without editing this file.
- An unknown command acks `unsupported`.

**Behaviour to get right:**
- The idempotency cache must key on `request_id` alone and must **not** re-run the side effect. Prove it with a command that has an observable effect, not with `ping`.
- `self_test`'s per-`FaultDef` checks must reflect the *current* injection state at the moment of the call, not a snapshot from startup.
- The loop must not swallow handler exceptions silently: a handler that raises should ack `failed` with the detail, so a broken command is visible rather than a timeout.
- Do not break `_handle_inject_command` or the fault/recovery loops; they share this class.

**Tests to write first:** `ping` acks `ok`; `self_test` returns one check per registered fault and fails exactly the injected one; injecting a *different* fault changes which check fails; `force_report` republishes and acks; an unknown command acks `unsupported`; a duplicate `request_id` republishes the identical ack and the side effect runs **once** (assert the observable effect's count); the cache evicts beyond 32 and a request id older than that is treated as new; a handler that raises acks `failed` rather than timing out.

**Done when:** `uv run pytest tests/test_simulator_base.py` and the full suite green. Commit: `feat(tests): shared simulator command loop with idempotency cache`.

---

## Wave 2 — tasks 7, 8 and 9 in parallel

One simulator each. All three consume Task 6's base class; **none modifies it**. If an implementer finds it needs a base-class change, it stops and reports rather than editing a file another task in the wave also depends on.

### Task 7: Vending simulator commands

**Spec:** §1.3 (vending rows), §5.

**Files:**
- Modify: `simulators/vending_machine.py`
- Test: `tests/test_simulator_vending.py`

**Interfaces — Produces:** `dispense` with `{"slot": int}` and `water_valve` with `{"seconds": 1–10}` on the command channel, plus both in the capabilities `commands` list.

**Behaviour to get right:**
- **`cmd/dispense` keeps working unchanged.** It is the production topic real sales use, and part 4's whole premise is that deployed firmware keeps vending. The channel's `dispense` and the production topic must share the motor code so they cannot drift, and a test must prove the production path still vends with no command-channel involvement.
- Channel `dispense` publishes `hardware/dispenser` exactly as a sale does.
- `water_valve` validates `seconds` in 1–10 and acks `rejected` outside it.
- Both go through the base class's idempotency cache, so a duplicate `request_id` must not run the motor twice — the most consequential duplicate in the system.

**Tests to write first:** channel `dispense` runs the motor once and publishes `hardware/dispenser`; a duplicate `request_id` for `dispense` acks identically and runs the motor **once**; production `cmd/dispense` still vends and is unaffected by the channel; `water_valve` at 1 and 10 acks `ok`, at 0 and 11 acks `rejected`; both commands appear in capabilities; an unknown command acks `unsupported`.

**Done when:** its test file and the full suite green. Commit: `feat(tests): vending simulator dispense and water_valve commands`.

---

### Task 8: Ice maker simulator onto the shared loop

**Spec:** §5 (ice maker bullet).

**Files:**
- Modify: `simulators/ice_maker.py`
- Test: `tests/test_simulator_ice_maker.py`

**Interfaces — Produces:** `power_cycle` and `set_interval` moved onto the shared loop with their existing param validation intact; the private `_acked` cache and private command loop **removed** in favour of the base class's.

**Behaviour to get right:** this is a refactor, and its success criterion is that **nothing observable changes**. `power_cycle` still validates `dwell_seconds` 5–300 and still power-cycles; `set_interval` still validates and still changes the interval; the topic and ack shape are unchanged; every existing ice-maker simulator test passes **unmodified** except where it reaches into `_acked` directly. If a test does reach into `_acked`, retarget it at the base class's cache rather than keeping the old attribute alive.

**Tests to write first:** no new behaviour, so the proof is the existing file passing. Add only: `ping`, `self_test` and `force_report` now work on the ice maker too (they come free from the base class and did not exist before), and a duplicate `power_cycle` request id does not power-cycle twice.

**Done when:** its test file and the full suite green, with the diff showing the private loop deleted rather than left orphaned. Commit: `refactor(tests): ice maker uses the shared command loop`.

---

### Task 9: MDB gateway simulator commands

**Spec:** §1.3 (mdb rows), §5.

**Files:**
- Modify: `simulators/mdb_gateway.py`
- Test: `tests/test_simulator_mdb.py`

**Interfaces — Produces:** `bill_acceptor_test`, `coin_return_test` and `card_reader_test`, each logging and acking `ok`, with `card_reader_test` returning the reader's status text in the ack's new `result` field. All three added to the capabilities `commands` list.

**Behaviour to get right:** these actuate hardware on a real machine, so they go through the idempotency cache like everything else. `card_reader_test` is the only one with a `result` payload — shape it as the spec says (a status text) and keep it a dict so the ack model stays uniform. Do not touch the payment topics (`cmd/payment/enable`, `cmd/payment/refund`); they are production control, explicitly not tests, and `TESTABLE_COMMANDS` must not list them.

**Tests to write first:** each of the three acks `ok`; `card_reader_test`'s ack carries a non-empty `result`; a duplicate request id replays the ack without re-actuating; all three appear in capabilities; the payment topics are untouched and absent from this subsystem's `TESTABLE_COMMANDS` entry.

**Done when:** its test file and the full suite green. Commit: `feat(tests): MDB gateway actuator test commands`.

---

## Serial step 7

### Task 10: The maintenance lease

**Spec:** §2.2 (the whole section), §6 (lease never persisted).

**Files:**
- Modify: `controller/vmc.py`
- Test: `tests/test_vmc_flows.py`

Task 11 also edits `controller/vmc.py`; these two are sequential for that reason. Do not start both.

**Interfaces — Produces:**
- `MaintenanceHold` — `holder_user_id`, `holder_session_id`, `started_at`, `last_activity_at`, `runs_in_flight: int`, plus a `release_requested` flag.
- `VMC.begin_maintenance(user, session) -> bool` — grants only when the FSM is `idle`, escrow is **zero**, and no lease exists. On refusal the caller must be able to say *why* ("machine is mid-sale", "held by <name>"), so expose the reason rather than a bare `False` if that reads better — state your choice in the interface.
- `VMC.end_maintenance(session) -> bool` — releases only for the holder's session and only when `runs_in_flight == 0`; otherwise sets `release_requested` and the last settling run performs the release.
- Run accounting: each test start increments `runs_in_flight` and refreshes `last_activity_at`; each completion, timeout **or failure** decrements it. A leak here pins the machine out of service until restart, so the decrement belongs in a `finally`.
- An idle timer — 5 minutes since `last_activity_at` — that behaves exactly like a release request and **never** clears a lease with runs in flight.
- Take-over: permitted only when `runs_in_flight == 0` and the lease has been idle **60 s**; records who took it.
- Raising the lease raises `SVC-102` through the fault registry (so Task 4's availability wiring inhibits payment), and releasing it clears the fault.
- `deposit_funds` during a lease **refunds** through `request_refund(reason="maintenance")` and records the `refund` event, instead of escrowing. The `test` method credit from Task 11 is the one exception.

**Behaviour to get right:**
- **Prove payment was actually inhibited**, not that a flag flipped: assert `availability.payment_enabled` is False and the disable was published while the lease is held.
- The refund-during-lease path covers the race between the disable command and a coin already in the mechanism. Money that arrives must never become spendable escrow after the hold ends — test that escrow is still zero afterwards.
- The lease is **never persisted**; a restart clears it, matching the FSM's reset semantics. Do not add it to the session snapshot.
- `begin_maintenance` must refuse mid-sale. Escrow greater than zero is a refusal even in `idle`.

**Tests to write first:** granted when idle with zero escrow; refused when the FSM is not idle; refused when escrow is non-zero; refused when a lease exists, naming the holder; `SVC-102` raised on grant and cleared on release; `payment_enabled` False while held and True after release; a credit arriving during a lease is **refunded**, the `refund` event recorded, and escrow remains zero; `end_maintenance` by a non-holder session is refused; release with a run in flight defers and happens when the last run settles; the idle timer never releases with a run in flight; take-over refused while a run is in flight or before 60 s idle, permitted after, and records who; a run that **fails** still decrements `runs_in_flight`.

**Done when:** full suite green, every new test shown to fail first. Commit: `feat(tests): maintenance lease that inhibits payment and refunds stray credit`.

---

## Serial step 8

### Task 11: Per-sale `is_test` and `run_test_sale`

**Spec:** §2.3.

**Files:**
- Modify: `controller/vmc.py`
- Test: `tests/test_vmc_flows.py`

**Interfaces — Consumes:** Task 10's lease; part 3's `pending_sale_shares` and `record_sale`.

**Interfaces — Produces:**
- `is_test: bool` on the **in-flight sale context** — the selected product plus part 3's pending shares — not a VMC-global flag.
- `VMC.run_test_sale(sku) -> TestSaleResult` carrying the FSM path taken and the outcome (`dispensed`, `vend_failed <code>`, `timeout`).

**Behaviour to get right — this is the task most likely to corrupt part 3's ledger:**
- **The dispense completion handler consults the sale's own flag.** A test sale records neither `sale` nor `dispense`; it writes `test_run` instead (Task 12 owns the recorder side). Because the flag lives on the sale, a lease release or timer expiry mid-run **cannot** flip it to production — and Task 10's rule that a lease cannot be released with a run in flight is the belt to this braces. Test exactly that: release the lease mid-run and assert no `sale` row appears.
- `run_test_sale` requires the lease, increments `runs_in_flight`, deposits the price as **one credit with method `test`** (the only credit `deposit_funds` accepts during a lease), selects the product, and runs the **normal** dispense path — the real FSM and the production `cmd/dispense` command — so the test exercises what a sale exercises.
- Escrow is cleared **without** a refund command at the end; a test sale is not a refund and must not emit one.
- It awaits dispenser completion using the existing dispense timeout, and returns the outcome for all three terminal cases.
- Part 3's guarantee still holds for real sales: a production sale still records its row with the FIFO shares. Assert that in the same file so a regression is caught here rather than in part 3's tests.

**Tests to write first:** a test sale records **no** `sale` row and **no** `dispense` event; it does record a `test_run`; the FSM path and outcome are returned for `dispensed`, for `vend_failed` with its code, and for a dispenser `timeout`; escrow is zero afterwards with **no** refund command published; `deposit_funds` with method `test` is accepted during a lease while any other method is refunded; releasing the lease mid-run still records no sale; a **production** sale immediately after a test sale records normally with its FIFO shares; `run_test_sale` without a lease is refused.

**Done when:** full suite green. Commit: `feat(tests): simulated sale that exercises the FSM without recording a sale`.

---

## Serial step 9

### Task 12: `test_run` events and `update_metadata`

**Spec:** §4.

**Files:**
- Modify: `services/event_recorder.py`
- Test: `tests/test_event_recorder.py`

**Interfaces — Produces:**
- `test_run` events in the existing `events` table (90-day retention, **not** the never-pruned `sales` table): `value` is the duration in seconds, `metadata` is `{run_id, user_id, user_name, subsystem, command, params, status, checks, verdict, note}`.
- `EventRecorder.update_metadata(run_id, **fields) -> None` — updates the row's metadata **in place by `run_id`**, executed on the writer thread like every other write.
- `get_summary` **ignores** `test_run`, so a test session cannot move the KPIs.

**Behaviour to get right:**
- `update_metadata` runs on the writer thread; it must not open a competing connection. A verdict recorded for a `run_id` that does not exist is a no-op with a warning, not an exception — the run may have been pruned.
- Merge semantics: `update_metadata(run_id, verdict="pass")` must leave the other metadata keys intact. A wholesale replace would discard the checks and params the log renders.
- `test_run` rows are pruned with `events` at 90 days. Do **not** add them to the never-pruned set — the spec puts them in `events` deliberately.
- Read the row back on a **separate connection** to prove the update landed; part 3's rule applies.

**Tests to write first:** a `test_run` row is written with the documented metadata shape and its duration in `value`; `get_summary` over a window containing it is unchanged by its presence (assert against a summary taken without it); `update_metadata` sets `verdict` and `note` while leaving `checks` and `params` intact, verified from a second connection; an unknown `run_id` is a no-op with a warning; a `test_run` older than the retention window **is** pruned, and a `sales` row of the same age still is not.

**Done when:** full suite green. Commit: `feat(tests): test_run events and in-place metadata updates`.

---

## Wave 3 — tasks 13 and 14 in parallel

### Task 13: The Tests level

**Spec:** §3 (the URL table), §6 (error handling).

**Files:**
- Modify: `web_interface/routes/tests_level.py`, `web_interface/levels.py`
- Create: `web_interface/templates/tests_subsystem.html`, `tests_sale.html`, `tests_log.html`, `tests/test_routes_tests.py`
- Modify: `web_interface/templates/tests.html` (part 2's placeholder body)

This is the largest task in the part. If the implementer reports it is more than one sitting, split it at the seam between **discovery** (`/tests`, `/tests/{subsystem}`, `/tests/log`) and **actions** (run, verdict, run-all, sale, end, takeover) rather than by template.

**Interfaces — Produces**, all gated `run_tests`, all POSTs additionally `require_htmx`:

| Route | Behaviour |
|---|---|
| `GET /tests` | One card per subsystem: alive state, firmware, contract match, count of testable commands (**advertised ∩ allowlist**); Run-all button; Simulated sale and Test log tiles; the current lease holder if any. **Entering does not take the lease** |
| `GET /tests/{subsystem}` | Testable commands in two groups — automatic (`ping`, `self_test`, `force_report`) and actuator, each with its params widget and a Run button |
| `POST /tests/{subsystem}/{command}` | **Re-checks the allowlist and 403s outside it**; takes the lease or returns the refusal inline; dispatches; swaps in a result card |
| `POST /tests/runs/{run_id}/verdict` | Records `pass`/`fail` and the note |
| `POST /tests/run-all` | `ping` and `self_test` on every alive subsystem in sequence; one result table |
| `GET /tests/sale`, `POST /tests/sale` | SKU picker, then Task 11's `run_test_sale`; shows FSM path and outcome with Pass/Fail |
| `POST /tests/end` | Releases the caller's own lease once nothing is in flight |
| `POST /tests/takeover` | Transfers an idle lease per Task 10's rules |
| `GET /tests/log` | Last 100 `test_run` events with verdict and note |

Level constants for the sub-levels go in `web_interface/levels.py`.

**Behaviour to get right:**
- **The allowlist re-check on POST is the security boundary**, not the button rendering. Part 2 shipped a guard enforced only in a template and Copilot caught it; part 3 shipped another and Copilot caught that too. Test a direct POST for `refund` and for `payment/enable` and assert **403**.
- A command that is allowlisted but **not advertised** by that subsystem gets no button and its POST is refused.
- Each result card is written to the log **as it happens**, so a run whose verdict was never entered shows `verdict: none`.
- Leaving the level posts `/tests/end`; the idle timer is the backstop for a closed tab. The level must not take the lease merely by being viewed.
- Timeout renders "no answer from <subsystem> after 2 attempts"; `rejected`/`failed`/`unsupported` show their message verbatim; broker down shows every subsystem unreachable.
- Params widgets honour the contract's ranges — slot from the catalog, `seconds` 1–10, `dwell_seconds` 5–300 defaulting to 30.
- **SKUs are not URL-safe.** Part 3 fixed this class for reports with a `sku_url_segment` filter and a `{sku:path}` route; the SKU picker here must use the same mechanism, not a second one.
- Rebuild `web_interface/static/app.css` if new classes appear and keep `tests/test_static_css.py` green.

**Tests to write first:** every route 200 for owner and tech, 403 for secretary and loader; discovery renders only advertised-∩-allowlisted commands; a direct POST for `refund` and for `payment/enable` is **403**; a POST for an allowlisted-but-unadvertised command is refused; viewing `/tests` takes no lease while a run does; run-all covers every alive subsystem in sequence and renders one table; a verdict updates that run's log row; `/tests/end` releases only the caller's lease; a second tech sees "held by" and can take over only when idle; the simulated-sale card shows path and outcome; timeout, `rejected` and `unsupported` each render their documented message; a SKU containing `/` works end to end.

**Done when:** its test file and the full suite green. Commit: `feat(tests): the Tests level`.

---

### Task 14: Contract documentation

**Spec:** §8 (`docs/contracts/*/CONTRACT.md`). Haiku.

**Files:**
- Modify: `docs/contracts/vending-machine/CONTRACT.md`, `docs/contracts/ice-maker-monitor/CONTRACT.md`

**What must be written:** the command channel (`cmd/<subsystem>` and its `/ack`, the request and ack payloads with the new optional `result`, the 10-second answer deadline); the three standard commands from §1.2 with their result shapes; the per-subsystem actuator commands from §1.3 with their params and ranges; and — stated as a **requirement on real firmware, not an implementation note** — that a subsystem keeps its last 32 `request_id`s and replays the cached ack for a duplicate without repeating the side effect, because the VMC's retry depends on it.

Also state plainly that **production topics are unchanged** and that firmware which ignores the command channel keeps vending and simply advertises no tests. That sentence is the one a firmware engineer most needs.

**Do not** restate the models field by field where the generated schemas already do; link to them. Do not edit the generated schemas here — Task 3 owns them.

**Tests to write first:** none; this is documentation. Verify the documented payloads validate against the models (paste them through the model in a throwaway check and say you did), and that the versions quoted match Task 3's bumped `CONTRACT_VERSION`s.

**Done when:** suite and ruff still green; quoted versions match the code. Commit: `docs(contracts): document the shared command channel`.

---

## Serial step 11

### Task 15: Project documentation

**Spec:** §8 (`CLAUDE.md`, `ROADMAP.md`). Haiku.

**Files:** `CLAUDE.md`, `ROADMAP.md`

**What must be written:**
- **`CLAUDE.md` Services:** `services/command_dispatcher.py` — one dispatcher, correlates acks by `request_id`, retries once **with the same id** because subsystems cache their last 32 and replay rather than re-act.
- **`CLAUDE.md` FSM section:** the maintenance lease — what grants it, that it raises `SVC-102` which `availability.py` treats as a safety row so payment is inhibited, that it is a lease with `runs_in_flight` rather than a flag, that it is never persisted, and that credit arriving during it is refunded rather than escrowed.
- **Test-ness lives on the sale**, not on the hold: a test sale records neither `sale` nor `dispense` and writes `test_run`, and a lease release mid-run cannot reclassify it.
- **The level tree table** gains the Tests sub-levels with their `run_tests` gate.
- **`TESTABLE_COMMANDS` is a server-side allowlist** re-checked on POST, deliberately narrower than what capabilities advertise, so a control command like `refund` can never be invoked from the Tests tile.
- **`ROADMAP.md` §5** gains `SVC-102`, and the `DATA-101` row matches Task 2's corrected description.
- Note both `CONTRACT_VERSION` bumps and that part 3's deferred bump is absorbed here.

**Done when:** `grep -n "SVC-102\|command_dispatcher\|TESTABLE_COMMANDS\|maintenance lease" CLAUDE.md` finds each claim; suite and ruff green. Commit: `docs: document the command channel, maintenance lease and Tests level`.

---

## Before the pull request: whole-branch review

Program plan §3.4, added for this part. After Task 15 and **before** step D opens the PR, dispatch one fresh **Sonnet** reviewer over the entire `origin/main..HEAD` diff in a single pass.

Its brief is not a second general review. It hunts for **repeats of a class already fixed elsewhere** in this branch or in parts 1–3, because per-task reviewers structurally cannot see them. Give it the deviations ledger and the list of defects already fixed. Classes worth naming explicitly, all of which have already bitten this program at least once:

- a guard enforced in a template but not re-checked on the server (part 2, part 3);
- a fix applied in one file and not its siblings (part 3's DST bug reached `reports.py` but not the scheduler);
- an arbitrary string interpolated somewhere it must be escaped — URL segment, CSS selector, HTML (parts 2 and 3);
- a duplicate-side-effect path where idempotency was assumed rather than enforced (part 3's `PAY-104`, and this part's whole retry design);
- a test whose fixture makes the asserted branch unreachable;
- a failure path that leaves a counter or lease incremented, pinning the machine.

Findings go through the normal fix loop — implementer, fresh reviewer, test that fails first — before the PR opens. **The PR description records what it found, including "nothing", which is itself a result.**

## Definition of Done

1. `uv run pytest` green; `uv run ruff check .` and `uv run ruff format --check .` clean.
2. `pyproject.toml` and `uv.lock` unchanged — no new runtime dependency.
3. `git diff origin/main -- web_interface/templates/screen.html web_interface/templates/partials/screen_body.html` is empty; CDN references only in `screen.html`.
4. **Production topics unchanged**: the vending simulator still vends on `cmd/dispense` with no command-channel involvement, and `cmd/payment/enable` / `cmd/payment/refund` are untouched.
5. **Wire compatibility**: a present-day ice-maker ack payload validates against the moved `CommandAck`, and `MonitorCommand` is still importable from `contracts.ice_maker_monitor`.
6. `PAYMENT_BLOCKING_FAULTS` has exactly **seven** members, including `SVC-102`.
7. Both `CONTRACT_VERSION`s bumped, committed schemas regenerated with `contracts.generate`, and `tests/test_contract_schemas.py` green.
8. `TESTABLE_COMMANDS` excludes `payment/enable`, `refund` and `set_interval`, and a direct POST for each is 403.
9. A duplicate `request_id` runs an actuator **once**, proven by an observable side effect.
10. A test sale records neither `sale` nor `dispense`, even when the lease is released mid-run.
11. Parts 1–3 do not regress: identical login-failure bodies, `no-store` on plaintext codes, owner self-lockout refused, placement writes unable to touch catalog fields, one `<main>` and one `#pill` after a boosted navigation, a sale surviving `prune()`, the three browser tests still passing, and the CI skip-guard still failing on an unrecognised skip.
12. Every new test shown to fail against the pre-change implementation, reported per task.
13. The whole-branch review has run and its findings are resolved.

## Program goals claimed (program plan §2)

**8** — a tech can prove a subsystem works without making a sale, and the machine cannot sell while they do it. **6** — production topics are unchanged and the contract bump is additive and documented, so deployed firmware keeps vending. **9** — the lease is never persisted, a failed run cannot pin the machine, and a test session cannot move the KPIs. Goals 1–5 and 7 belong to parts 1–3 and must not regress.

## Spec coverage check

| Spec section | Task(s) |
|---|---|
| §1.1 channel, payloads, wire compatibility, idempotency requirement | 3, 6 |
| §1.2 standard commands | 6 |
| §1.3 actuator commands | 7, 8, 9 |
| §1.3 `TESTABLE_COMMANDS` allowlist | 3, 13 |
| §2.1 `CommandDispatcher` | 5 |
| §2.2 `SVC-102`, maintenance lease, credit during a hold | 3, 4, 10 |
| §2.3 per-sale `is_test`, `run_test_sale` | 11 |
| §3 Tests level | 13 |
| §4 test log, `update_metadata` | 12 |
| §5 simulators | 6, 7, 8, 9 |
| §6 error handling | 5 (timeout, broker), 6 (unsupported), 10 (lease refusals), 13 (rendering) |
| §7 testing | every task |
| §8 files | 1–15 |
| §9 out of scope | nothing built |
| Carry-forwards from part 3 | 1 (`saved_at`), 2 (`DATA-101` text), 3 (version bumps) |

