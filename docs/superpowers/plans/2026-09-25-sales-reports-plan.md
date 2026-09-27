# Sales Reports Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking. Dispatch by **wave** — see "Execution schedule"; this is not a flat task list.

**Goal:** Record every sale forever with its SKU, price and how it was paid, and give the owner reports on screen, by email on demand, and on a schedule.

**Architecture:** `VMC` gains a FIFO credit ledger so a sale's payment-method split is derived from the credits actually consumed, never re-classified. `EventRecorder` gains never-pruned `sales` and `cash_collections` tables, with sale inserts written **durably and awaited** rather than queued, and a journal fallback if the insert fails. `services/reports.py` holds the queries and CSV rendering; `services/report_scheduler.py` sends the scheduled summary. Four new report levels extend part 2's `/reports`.

**Tech Stack:** Python 3.12, SQLite (WAL), FastAPI, Jinja2, HTMX 1.9.10 (vendored), Pydantic v2, loguru, pytest, `uv`, `ruff` **0.11.2 pinned — always `uv run ruff`, never bare `ruff`**.

**Spec:** `docs/superpowers/specs/2026-09-25-sales-reports-design.md`
**Program plan:** `docs/superpowers/plans/2026-09-25-dashboard-v2-program.md` (part 3 of 4)
**Builds on:** part 1 (#19) and part 2 (#20), merged as `766c50d`

## How this plan is written

Two owner-approved rules from program plan §3.1 and §3.2 govern this document, and they **override** the `writing-plans` skill's default of embedding code bodies.

1. **Prose-plan rule.** Each task gives its spec section, the files it may touch, the interface (exact names, signatures, routes, SQL, template variables), the behavior in prose, and the tests to write first. **The implementer writes the code.** A code block appears only for SQL, a wire format, a shell command, or a genuinely non-obvious algorithm.
2. **Wave rule.** Tasks are grouped into numbered waves whose "files it may touch" sets are pairwise disjoint and whose tests share no fixture being edited in the same wave. A wave's implementers run in parallel, then its reviewers in parallel, then one full `uv run pytest` and `uv run ruff check .` before the next wave. A reviewer failure returns only its own task. Anything touching a shared file is a **serial step**.

## Verification rules specific to this part

Parts 1 and 2 produced nine tests that could not fail and five cases of a green signal that was lying. This part handles money and history, where a test that silently checks nothing is worse than no test. So, in addition to the above:

- **Behaviour, not strings.** A recorder test must prove the row is **actually on disk** — open a separate connection and read it back — not that a method returned without raising. A journal test must prove the journal file contains the record and that a later startup **actually replayed and truncated it**. A durability test must prove ordering (the row exists *before* the FSM returns to idle), not merely that both happened.
- **Every test must be shown to be able to fail.** For each accepted deliverable, break the implementation, watch the new test fail, restore it, watch it pass. Report both observations. A reviewer that cannot see this proof in the implementer's report must ask for it before passing the task.
- **Beware the fixture that makes the branch unreachable.** The most common hollow test in parts 1–2 was a fixture that did not wire the dependency the asserted branch needed, so the handler short-circuited before the assertion ran. For every test asserting a *non-default* branch, confirm the branch is actually entered.
- **One agent per worktree at a time.** The orchestrator dispatched two agents onto one worktree in part 2 and they edited the same file concurrently. Within a wave, parallel implementers are safe **only** because their file sets are disjoint; never dispatch an out-of-band fix agent while a wave is in flight.

## Global Constraints

Values copied verbatim from the spec. Every task's requirements implicitly include this section.

- **`sales` and `cash_collections` are never pruned.** `_prune_with` touches only `events`. The existing `dispense` event stays, for the existing KPIs.
- **Sales are written durably, not queued.** `record_sale(...)` is a synchronous insert on its **own** connection (WAL mode, `synchronous=NORMAL`) that the VMC awaits through `asyncio.to_thread`, at the point it records `dispense` today, **before `_finish_dispensing`**. The event loop is never blocked; the row is on disk before the FSM returns to idle.
- **Failure ladder for a sale, in order:** insert → else append one JSON line to `data/sales-journal.jsonl` (append + fsync) and raise alert-class `DATA-101 sale journal in use` → else the session snapshot's existing `PAY-104` path. At startup the recorder replays and truncates the journal, then clears `DATA-101`.
- **Method strings are stored raw**, straight from `PaymentEvent.method`. Classification happens at query time only, never by rewriting a stored value. The MDB simulator emits `cash_coin`, `cash_bill`, `card`, `nfc`; real firmware may differ.
- **`is_cash(method)`** is true for `cash`, `coin`, `bill`, and any method whose lowercase name starts with `cash_` or `coin_`. Everything else (`card`, `nfc`, `test`) is not cash. A unit test pins the four simulator values.
- **FIFO:** credits are consumed first-in-first-out; the consumed shares are the sale's method breakdown. `$2.50` after `$2.00 cash` then `$1.00 card` yields `{"cash": 2.00, "card": 0.50}` and leaves a `$0.50 card` credit. `vend_failed` restores **exactly the consumed shares as separate credits**, so money is never reclassified. A refund clears the list.
- **Guard:** if `escrow_credits` and `credit_escrow` disagree, record the sale as `{"unknown": price}` and log a warning. That is a bug guard, not an expected path.
- **`expected_cash`** is computed **inside the writer thread at insert time**, as the sum of cash-class shares of `sales.methods` with `ts` greater than the previous collection's `ts` (all time for the first row).
- **Window presets** `7d`, `30d`, `90d`, `12m`, `all` via `?range=`, default `30d`. Bucketing uses the machine's local timezone; buckets are `day`, `week` (Monday start), `month`.
- **Event-derived columns outside the 90-day retention show `—`, never zero**, so an old month is not misread as fault-free.
- **New permission `collect_cash`**, granted to **all four roles**. `POST /inventory/collect` with a two-tap confirm; the response shows the recorded time and the expected amount.
- **Reports are gated `view_reports`** (owner, secretary) — except `/inventory/collect`, which is `collect_cash`.
- **Corrupt `events.db` must not stop the machine.** Rename to `events.db.corrupt-<timestamp>`, create a fresh database, raise alert-class `DATA-102 event database was reset`, clear on admin acknowledgement.
- **The scheduler never raises out of its loop**, sleeps at most **60 s** per pass, re-reads the live config each pass, de-duplicates by period via a `report_sent` event, and on startup sends **at most one** catch-up for the most recent completed period — never a backlog.
- **Tables** are the one place part 2 allows a real `<table>`; each sits in an `overflow-x: auto` container. Currency uses the existing template filters. **No charts** (spec §8).
- **Every POST keeps `require_htmx`.** No CDN reference may enter `web_interface/` (`templates/screen.html` excepted — spec-mandated, leave it alone).
- **Commands:** `uv run pytest`, `uv sync`, `uv run ruff check --fix .` then `uv run ruff format .`. Never chain shell commands with `&&`. Never run Docker.
- **Out of scope:** charts, counted-cash entry and variance, accounting-system export, tax/multi-currency/multi-location, backfilling sales from old `dispense` rows.

## File Structure

| File | Responsibility |
|---|---|
| `services/reports.py` | New. `is_cash`, window/bucket helpers, `by_period`, `by_product`, `by_method`, `collections`, `summary`, CSV rendering |
| `services/report_scheduler.py` | New. `run(config, recorder, mailer, clock)` and next-due computation |
| `services/event_recorder.py` | Two new tables, durable `record_sale`, journal replay, `record_cash_collection`, prune scope, corrupt-file recovery |
| `controller/vmc.py` | `escrow_credits`, FIFO deduction, `pending_sale_shares`, the awaited `record_sale` call |
| `services/session_store.py` | Snapshot carries credits and pending shares |
| `contracts/vending_machine.py` | `DATA-101`, `DATA-102` |
| `services/mailer.py` | Optional `attachments` |
| `services/access.py` | `collect_cash` permission |
| `config/config_model.py` | `ReportsConfig` |
| `main.py` | Start the scheduler under `_supervise` |
| `web_interface/routes/reports.py` | Four report levels plus `POST /reports/email` |
| `web_interface/routes/inventory.py` | `POST /inventory/collect` |
| `web_interface/routes/settings.py` | `/settings/reports` |
| `web_interface/levels.py` | Level constants for the four report levels and Settings › Reports |
| `web_interface/templates/reports_*.html`, `settings_reports.html` | New |
| `tests/test_reports.py`, `tests/test_report_scheduler.py` | New |
| `.github/workflows/ci.yml`, `tests/conftest.py` | The durable skip-guard (Task 1) |

## Execution schedule

| Step | Kind | Tasks | Why |
|---|---|---|---|
| 1 | **Wave 1** | 1, 2 | CI guard (workflow + conftest) vs. a template comment. Fully disjoint, and both are independent of the feature work |
| 2 | **Serial** | 3 | `contracts/vending_machine.py` + `services/access.py` + `config/config_model.py` — three shared files everything later reads |
| 3 | **Serial** | 4 | `controller/vmc.py` + `services/session_store.py` — the FIFO ledger, which the recorder task's tests depend on |
| 4 | **Serial** | 5 | `services/event_recorder.py` — tables, durable `record_sale`, journal, prune scope, corrupt recovery |
| 5 | **Serial** | 6 | `controller/vmc.py` again — wire the awaited `record_sale` call and the snapshot's pending sale |
| 6 | **Wave 2** | 7, 8 | `services/reports.py` + its tests vs. `services/mailer.py` attachments + its tests. Disjoint |
| 7 | **Serial** | 9 | `services/report_scheduler.py` + `main.py` |
| 8 | **Wave 3** | 10, 11, 12 | Report levels (`routes/reports.py` + `reports_*.html`) vs. cash collection (`routes/inventory.py`) vs. Settings › Reports (`routes/settings.py` + `settings_reports.html`). Disjoint except `levels.py`, which Task 3 pre-populates |
| 9 | **Serial** | 13 | `CLAUDE.md` |

**3 waves, 6 serial steps, 13 tasks.** Tasks 4, 5 and 6 are serial *and* sequential on purpose: the ledger must exist before the recorder can be tested against it, and the recorder must exist before the VMC can await it. Splitting them into one wave would let two implementers edit `controller/vmc.py` at once.

**Level constants are pre-created in Task 3** so no wave-3 task edits `web_interface/levels.py` — the same trick part 2 used for router registration.

**Model policy** (program plan §3.1): **Sonnet** for tasks 3, 4, 5, 6, 7, 9, 10, 12 (FSM, recorder, money, contracts, scheduler, config writes); **Haiku** for tasks 1, 2, 8, 11, 13 (CI config, a comment, a mailer parameter, one route plus template, docs). Reviewers **always Sonnet**.

---

## Wave 1 — tasks 1 and 2 in parallel

Neither touches feature code. They exist here because both are debts carried out of part 2 and both are cheap; getting them in first means the rest of part 3 is developed against a CI that cannot lie.

### Task 1: Replace the CI skip-guard's grep allowlist with a structural check

**Spec:** not in the sales-reports spec — carried from part 2's PR as a known limitation, owner-directed.

**Files:**
- Modify: `.github/workflows/ci.yml`
- Create or modify: `tests/conftest.py` (a hook), and a small helper module if the hook needs one
- Test: whatever proves the guard itself works — see below

**The problem.** The `test` job's guard step greps the pytest output for two fixed skip-reason strings (`ICE_COLDER_BROWSER_TESTS=1 to run` and `git diff against origin/main failed`). That is an allowlist of exactly two known holes, not a structural check. If either message's wording changes, or a future opt-in test is added without a matching grep line, that hole reopens **silently behind a green check**. Part 2 produced five separate instances of a green signal that was lying; this is the one it knowingly left behind.

**Interfaces — Produces:**
- A pytest mechanism that reports skips in a **machine-readable** form the workflow can assert on, rather than scraping `-q` console output. The natural shape is a `conftest.py` hook (`pytest_report_teststatus`, or `pytest_sessionfinish` reading `terminalreporter.stats["skipped"]`) that writes a JSON file — for example `skip-report.json` — listing each skipped test's nodeid and reason.
- A declared, reviewable **policy** of which skips are legitimate, expressed as data rather than as grep lines: MQTT integration skips when no broker is reachable, and POSIX-only file-mode skips on a non-POSIX host. Anything else is a hole and fails the build.
- The workflow step reads that JSON and fails with a message naming the offending nodeids.

**Behaviour to get right:**
- **Default-deny, not default-allow.** An unrecognised skip must **fail**. That inversion is the entire point: a future opt-in test added with no policy entry should break the build, not vanish.
- **It must stay usable locally.** On a developer machine without Chrome, without a broker, and on Windows, the *suite* must still pass — the guard is a CI step, not a test that fails locally. Say explicitly in your report how a developer sees the difference.
- Do not ban all skips, and do not hard-code the current skip *count*: a count is as brittle as a grep and breaks on every legitimate test addition.
- Keep `set -o pipefail` and everything else part 2's CI work established. Read the committed `ci.yml` before editing so you do not silently drop it.

**Tests to write first:** prove the guard is a discriminator, and report both observations. (a) Introduce a temporary test that skips with an unrecognised reason, run the guard logic, confirm it **fails** and names that test; remove it. (b) Run the guard against the real suite and confirm it **passes**. If you can only exercise the workflow step's shell in CI, do (a) and (b) locally against the same JSON the step consumes, and say so.

**Done when:** the full suite passes locally; the guard fails on an unrecognised skip and passes on the real one; `uv run ruff check .` and `uv run ruff format --check .` clean. Commit: `ci: fail on unrecognised skips via a structural guard`.

---

### Task 2: Correct the misleading comment in `codes_regenerate.html`

**Spec:** not in the sales-reports spec — carried from part 2's Copilot round, owner-directed.

**Files:**
- Modify: `web_interface/templates/partials/codes_regenerate.html`

**The problem.** Its header comment asserts that "htmx includes the values of the closest enclosing form on every request from an element inside it (including the plain hx-post/hx-get buttons confirm_button.html renders), so the PIN reaches …". **That is wrong**, and it is the likely source of a false Copilot finding on PR 20 which cost a round of investigation. htmx 1.9.10's `getInputValues` only walks the closest enclosing form when the verb is **not** GET. The PIN therefore does **not** reach the confirm/cancel GETs, which was proved with a real-browser CDP probe capturing the outgoing request URLs — that probe is committed as `tests/test_users_codes_pin_browser.py` and is now part of CI.

**What to change:** the comment only. Describe the actual behaviour: form values are serialised for the POST, and **not** for the confirm/cancel GETs, so the PIN reaches the POST body and never a query string. Reference `tests/test_users_codes_pin_browser.py` as the executable proof so the next reader checks the test rather than re-deriving the mechanism. Keep the rest of the comment's genuinely useful content — why this partial exists rather than `confirm_button.html`, and the three render paths.

**Do not change any markup, attribute, id or template logic.** If you believe the markup is wrong, report it; do not act.

**Tests to write first:** none — this is a comment. Prove you changed nothing functional: `git diff` must show only comment lines, and the full suite must be unchanged. Run `ICE_COLDER_BROWSER_TESTS=1 uv run pytest tests/test_users_codes_pin_browser.py` and confirm it still passes, since that test is the claim's evidence.

**Done when:** the diff is comment-only and the suite is unchanged. Commit: `docs: correct codes_regenerate.html's claim about htmx GET serialisation`.

---

## Serial step 2

### Task 3: Faults, permission, config section, and level constants

**Spec:** §1.2 (`DATA-101`), §5 (`DATA-102`), §1.3 (`collect_cash`), §4.1 (`ReportsConfig`), §3 (the four report URLs).

Four shared files in one task, deliberately: each change is a handful of lines, every later task reads them, and splitting them would make four later tasks all edit the same files.

**Files:**
- Modify: `contracts/vending_machine.py`, `services/access.py`, `config/config_model.py`, `web_interface/levels.py`
- Test: `tests/test_contracts_vending.py`, `tests/test_access.py`, `tests/test_config_model.py`, `tests/test_levels.py`

**Interfaces — Produces:**
- `FaultCode.DATA_101 = "DATA-101"` and `FaultCode.DATA_102 = "DATA-102"`, each with a `FaultSpec` of `severity=Severity.warning`, `scope=Scope.machine`, and descriptions "Sale journal in use; sales are being written to a fallback file" and "Event database was reset after corruption; history before the reset is lost". **Neither may be added to `PAYMENT_BLOCKING_FAULTS`** — both are alert-class and must never stop the machine taking money.
- `Permission.collect_cash` in `services/access.py`, added to **all four** role sets in `ROLE_PERMISSIONS`.
- `ReportsConfig` in `config/config_model.py` with `schedule: Literal["off","daily","weekly"] = "off"`, `hour: int = 7` (0–23), `weekday: int = 0` (Monday-based), `extra_recipients: list[str] = []`; exposed as `config.reports`, and a `ConfigModel.reports` convenience property if the other sections have one.
- Level constants in `web_interface/levels.py`: `LEVEL_REPORTS_PERIOD` (`/reports/period`), `LEVEL_REPORTS_PRODUCT` (`/reports/product`), `LEVEL_REPORTS_METHOD` (`/reports/method`), `LEVEL_REPORTS_COLLECTIONS` (`/reports/collections`), and `LEVEL_SETTINGS_REPORTS` (`/settings/reports`) — all children of the existing `LEVEL_REPORTS` / `LEVEL_SETTINGS`. Pre-created here so no wave-3 task edits this file.

**Behaviour to get right:** adding a fault code must not change any existing gate — `PAYMENT_BLOCKING_FAULTS` membership is the gate, and part 1's tests assert exactly six codes there; confirm that count still holds. Adding a permission must not widen any role beyond `collect_cash`: part 1's `ROLE_PERMISSIONS` matrix tests assert exact frozensets, so update them deliberately rather than loosening them to `>=`. A new config section must round-trip through `save_config` with defaults and must not appear in any masked-secret list, since none of its fields is a secret.

**Tests to write first:** both fault codes exist with alert-class severity and are **absent** from `PAYMENT_BLOCKING_FAULTS`, which still has exactly six members; every role holds `collect_cash` and the four role frozensets are otherwise unchanged; `ConfigModel()` exposes `reports` with the documented defaults and round-trips through `save_config` into a temp path; `hour` rejects 24 and `-1`; the five new level constants have the right parents and crumb depth, and the existing "every constant's parent_url matches its parent" test still passes over the enlarged tree.

**Done when:** full suite green. Commit: `feat(reports): DATA faults, collect_cash, ReportsConfig and report levels`.

---

## Serial step 3

### Task 4: The FIFO credit ledger in the VMC

**Spec:** §1.1.

The single most delicate task in this part: it decides how money is attributed, and a mistake here silently misreports every future sale. No route or report work belongs in it.

**Files:**
- Modify: `controller/vmc.py`, `services/session_store.py`
- Test: `tests/test_vmc_flows.py` (or `tests/test_vmc.py`, whichever holds the escrow tests), `tests/test_session_store.py`

**Interfaces — Produces:**
- `Credit` — a small dataclass with `method: str`, `amount: float`, `ts: float`.
- `VMC.escrow_credits: list[Credit]`, alongside `VMC.credit_escrow`, which **stays the authoritative total**.
- `VMC.pending_sale_shares: dict[str, float] | None` — the consumed shares of the in-flight sale, held between deduction and dispense outcome.
- A deduction helper that, given a price, consumes credits FIFO and returns the consumed shares as `dict[str, float]`, mutating `escrow_credits` to leave any partial remainder as a credit **of its original method**.
- `SessionSnapshot` gains the credits list and the pending shares, round-tripping through `save`/`load`.

**Behaviour to get right:**
- `deposit_funds` appends a `Credit` **and** adds to `credit_escrow`. The two must never diverge; the spec treats divergence as a bug guard, not a path.
- Consumption is strictly FIFO by insertion order. The spec's worked example is the acceptance case: `$2.50` after `$2.00 cash` then `$1.00 card` → `{"cash": 2.00, "card": 0.50}`, leaving one `$0.50` **card** credit.
- `vend_failed` restores the price to escrow **as separate credits with exactly the consumed shares** — the example returns a `$2.00 cash` credit and a `$0.50 card` credit, not one `$2.50` blob and not a re-classified total. This is what stops the ledger laundering cash into card.
- A refund clears the credit list as well as the total.
- Rounding: money is float here, as everywhere in this codebase. Consume with a tolerance so a `$0.01` residue from float subtraction does not leave a phantom credit or produce shares that fail to sum to the price. State the tolerance you chose and test the boundary.
- Do **not** call the recorder from this task. Task 6 wires that up; keeping them separate is what lets this task's tests be about arithmetic only.

**Tests to write first:** the spec's worked example, asserting both the returned shares and the exact remaining credit list including its method; an exact-match sale consuming one credit entirely and leaving an empty list; a sale spanning three credits; `vend_failed` restoring two separate credits with the consumed shares and the resulting `credit_escrow`; a refund clearing both list and total; the divergence guard producing `{"unknown": price}` and logging a warning when `escrow_credits` is emptied behind the total's back; snapshot round-trip of credits **and** pending shares, including the empty and `None` cases; float-boundary case for the tolerance you chose.

**Done when:** full suite green, with every new test shown to fail against the pre-change implementation. Commit: `feat(reports): FIFO escrow credit ledger with method attribution`.

---

## Serial step 4

### Task 5: Sales tables, durable `record_sale`, journal, prune scope, corrupt recovery

**Spec:** §1.2, §1.3, §5 (corrupt database).

**Files:**
- Modify: `services/event_recorder.py`
- Test: `tests/test_event_recorder.py`

**Interfaces — Produces:**
- The two tables, created in `_init_db`, exactly as the spec's DDL gives them:

```sql
CREATE TABLE IF NOT EXISTS sales (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         REAL NOT NULL,
    sku        TEXT NOT NULL,
    name       TEXT NOT NULL,
    slot       INTEGER,
    price      REAL NOT NULL,
    methods    TEXT NOT NULL      -- JSON {method: amount}
);
CREATE INDEX IF NOT EXISTS idx_sales_ts ON sales (ts);
CREATE INDEX IF NOT EXISTS idx_sales_sku_ts ON sales (sku, ts);

CREATE TABLE IF NOT EXISTS cash_collections (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            REAL NOT NULL,
    user_id       TEXT NOT NULL,
    user_name     TEXT NOT NULL,
    expected_cash REAL NOT NULL
);
```

- `EventRecorder.record_sale(sku: str, name: str, slot: int | None, price: float, methods: dict[str, float], ts: float | None = None) -> None` — **synchronous, durable**, on its own connection with WAL and `synchronous=NORMAL`. Returns only once the row is committed. On failure it appends one JSON line to `data/sales-journal.jsonl` with append + fsync and re-raises or signals so the caller can raise `DATA-101`; the task must state which and why.
- `EventRecorder.record_cash_collection(user_id: str, user_name: str) -> None` — enqueued on the existing writer queue like any event, with `expected_cash` computed **inside the writer thread at insert time**.
- `EventRecorder.replay_sales_journal() -> int` — at startup, inserts any journalled sales, truncates the file, returns how many were replayed so the caller can clear `DATA-101`.
- Corrupt-database recovery in `__init__`: rename to `events.db.corrupt-<timestamp>`, create fresh, and expose the fact so `main.py` can raise `DATA-102` — a flag or a returned value, not a raise.
- `JOURNAL_PATH` as a module constant next to the db path so tests can point it at a temp dir.

**Behaviour to get right:**
- `_prune_with` must touch **only** `events`. A test must seed a sale older than the retention window, call `prune()`, and prove it is still there.
- `expected_cash` is the sum of cash-class shares of `sales.methods` with `ts` strictly greater than the previous collection's `ts`, all time for the first row. Computing it in the writer thread is what makes it consistent with everything already written — do not compute it in the route.
- The cash classification used here must be the **same** `is_cash` the reports use. Task 7 owns `services/reports.py`; to avoid a circular import, define `is_cash` where both can reach it and state where. If you place it in `reports.py`, the recorder must import it lazily or the module boundary must be arranged so `reports` does not import `event_recorder` at module scope. Explain your choice.
- `record_sale` must not use the writer queue: the whole point is that the row is on disk before the FSM moves on.

**Tests to write first — behaviour, not strings:** a sale older than retention **survives** `prune()`; `record_sale` writes the documented JSON shape and the row is readable **from a second connection** immediately after it returns; a `record_sale` whose insert is made to fail appends exactly one line to the journal, that line contains the sale's fields, and the file is fsynced; `replay_sales_journal` inserts the journalled rows and leaves the file **empty**, and is a no-op on an absent or empty file; a partially-written final journal line does not lose the earlier good lines; `expected_cash` is all-time cash for the first collection and only cash since the previous `ts` for the second, **including `cash_coin` and `cash_bill` shares and excluding `card` and `nfc`**; a corrupt `events.db` is renamed with a timestamped suffix, a fresh database is created, the recorder is usable afterwards, and the flag for `DATA-102` is set.

**Done when:** full suite green, every new test shown to fail against the pre-change implementation. Commit: `feat(reports): durable sales table, cash collections, journal and corrupt recovery`.

---

## Serial step 5

### Task 6: Wire the awaited sale write into the dispense path

**Spec:** §1.2 (ordering and the `PAY-104` metadata).

**Files:**
- Modify: `controller/vmc.py`, `services/session_store.py` (snapshot metadata only), `main.py` (journal replay and the two faults at startup)
- Test: `tests/test_vmc_flows.py`, `tests/test_main_supervise.py` or the startup test module

**Interfaces — Consumes:** Task 4's `pending_sale_shares`, Task 5's `record_sale` / `replay_sales_journal` / corrupt flag, Task 3's `DATA_101` / `DATA_102`.

**Behaviour to get right:**
- The sale is recorded **where `dispense` is recorded today, and before `_finish_dispensing`**, awaited via `asyncio.to_thread`. The `dispense` event stays — the existing KPIs read it.
- **Ordering is the property under test**, not co-occurrence: the row must exist before the FSM returns to idle. Prove it by observing state at the moment of the write (for example a recorder stub that captures `vmc.state` when called, or asserting the row is readable from a second connection at that instant) rather than by checking both are true at the end.
- On a `record_sale` failure: journal the record, raise `DATA-101`, and let the sale complete — a storage problem must not fail the vend or stop the machine.
- At startup `main.py` replays the journal and clears `DATA-101` when the journal drains; if the recorder reports a corrupt-database reset, it raises `DATA-102`. Neither may exit the process, and both must leave the VMC and MQTT client running (program goal 9).
- The `PAY-104` snapshot metadata gains the pending sale so an operator clearing it can be offered "record this sale" or "discard". **This task only carries the data into the snapshot and exposes it**; building the operator choice into the UI is not in the spec's route list, so if you find no route for it, record that as a spec gap and do not invent one.

**Tests to write first:** a successful vend writes exactly one sale row with the FIFO shares from Task 4, and that row is readable **before** the FSM reaches idle; a failing `record_sale` still completes the vend, journals the record and raises `DATA-101`; startup replays a non-empty journal, clears `DATA-101`, and leaves no rows behind; a corrupt database at startup raises `DATA-102` and the process continues with a working VMC and MQTT client; `vend_failed` after a deduction writes **no** sale row and restores the credits (guards against double-counting a failed vend); the snapshot's `PAY-104` metadata contains the pending sale.

**Done when:** full suite green. Commit: `feat(reports): record each sale durably before the FSM returns to idle`.

---

## Wave 2 — tasks 7 and 8 in parallel

### Task 7: `services/reports.py` — queries, classification and CSV

**Spec:** §1.3 (`is_cash`), §2 (all five query functions, window presets, bucketing).

**Files:**
- Create: `services/reports.py`, `tests/test_reports.py`

**Interfaces — Produces:**
- `is_cash(method: str) -> bool` — true for `cash`, `coin`, `bill`, and any method whose lowercase name starts with `cash_` or `coin_`; false otherwise. (If Task 5 placed this function elsewhere to avoid a circular import, re-export it here under this name so callers have one obvious home, and say so.)
- `WINDOW_PRESETS` mapping `7d`, `30d`, `90d`, `12m`, `all` to a `(start_ts, end_ts)` window relative to an injectable now; default `30d`. An unknown value falls back to `30d` rather than raising.
- `by_period(recorder, window, bucket) -> list[dict]` — bucket start, revenue, vends, failed vends, refunds, uptime %. Failed/refund/uptime come from `events` and are **`None` (rendered `—`) for buckets outside the 90-day retention**, never zero.
- `by_product(recorder, window) -> list[dict]` — per SKU: name (latest seen), units, revenue, failed vends; sorted by revenue descending.
- `by_method(recorder, window) -> list[dict]` — per **raw** method string: amount, share of revenue, count of sales the method contributed to, and a cash/other class column from `is_cash`.
- `collections(recorder, limit) -> list[dict]` — most recent collections: ts, user, expected cash, and cash accepted since that collection — a **live** figure for the newest row.
- `summary(recorder, window) -> dict` — revenue, vends, failed, refunds, per-method split, cash since last collection.
- CSV rendering: one function turning any of the above row lists plus a header into CSV bytes, and a filename helper producing `<machine_id>-<report>-<range>.csv`.
- Every query calls `recorder.flush()` first and is safe to run inside `asyncio.to_thread`; none of them may be `async`.

**Behaviour to get right:**
- **Bucketing uses the machine's local timezone** via `datetime.astimezone()`, weeks start **Monday**, and a sale exactly on a boundary belongs to the later bucket. A test must seed sales either side of a day, week and month boundary **in a fixed timezone** and assert the assignment — this is the single easiest thing in the part to get subtly wrong, and it will be invisible until someone questions a month's revenue.
- `by_method`'s shares must sum to total revenue. Test that as an invariant, not as a fixed expected list.
- Methods are grouped by their **raw** stored string; `cash_coin` and `cash_bill` are separate rows that both classify as cash. Do not merge them.
- Empty database returns empty rows and zero totals, never a raise or a `None` total.
- `collections`' newest-row live figure changes as new sales arrive; the older rows' figures are the stored `expected_cash`. Test both halves.

**Tests to write first, in `tests/test_reports.py`:** the four simulator method values pinned against `is_cash` (`cash_coin`, `cash_bill` → true; `card`, `nfc` → false) plus `cash`, `coin`, `bill` → true and `test` → false; seeded sales across day, week and month boundaries in a fixed timezone with asserted bucket assignment; a Monday-start week boundary specifically; `by_product` ordering and the latest-seen name when a SKU was renamed; `by_method` shares summing to revenue; `—`/`None` for event-derived columns in a bucket older than retention while revenue for that bucket is still correct (the point of never-pruned sales); empty-database behaviour for all five functions; `collections` live newest row versus stored older rows; CSV round-trip parsing back to the same rows and the filename shape; an unknown `range` falling back to `30d`.

**Done when:** `uv run pytest tests/test_reports.py` green and the full suite green. Commit: `feat(reports): sales queries, cash classification and CSV rendering`.

---

### Task 8: Email attachments in the mailer

**Spec:** §3 ("The `send_email` signature gains an optional `attachments: list[(filename, bytes, mime)]`").

**Files:**
- Modify: `services/mailer.py`
- Test: `tests/test_mailer.py`

**Interfaces — Produces:** `send_email(email_config, to, subject, body, attachments: list[tuple[str, bytes, str]] | None = None) -> bool` — the existing three-positional-argument calls must keep working untouched, so the parameter is optional and last. Each tuple is `(filename, payload, mime)` where mime is a full type like `text/csv`; split it into maintype/subtype for `EmailMessage.add_attachment`.

**Behaviour to get right:**
- With no attachments the message must remain **exactly** what it is today — a plain-text single-part message. A multipart wrapper appearing on every notifier alert would be a regression; part 1's `Notifier` and part 3's scheduler both call this.
- The 15-second-per-socket-operation timeout and the run-in-executor behaviour are unchanged. `send_email` still never raises: it logs and returns `False`.
- An attachment whose mime lacks a `/` must not crash the send; decide and state the behaviour (reject with a log and `False`, or fall back to `application/octet-stream`).

**Tests to write first:** a call with no `attachments` produces a non-multipart message with the same body as today (assert on the built message, not on a substring of a log line); a call with one CSV attachment produces a multipart message whose part has the given filename, the given bytes **decoded back byte-identically**, and `text/csv`; two attachments both arrive; a malformed mime behaves as you documented; the existing four mailer tests pass unchanged.

**Done when:** `uv run pytest tests/test_mailer.py tests/test_notifier.py` green and the full suite green. Commit: `feat(reports): optional attachments in send_email`.

---

## Serial step 7

### Task 9: The scheduled summary

**Spec:** §4.2.

**Files:**
- Create: `services/report_scheduler.py`, `tests/test_report_scheduler.py`
- Modify: `main.py`

**Interfaces — Produces:**
- `async run(config, recorder, mailer, clock) -> None` — the supervised loop. `clock` is injectable for tests; `mailer` is the `send_email` callable so tests can stub it.
- A pure, separately testable next-due computation: given the live config and "now", return the next send time and the period it would cover. Keep it a module-level function, not a closure — its tests are the heart of this task.
- Started in `main.py` under `_supervise("report scheduler", ...)`, alongside the MQTT client and health monitor.

**Behaviour to get right:**
- **Bounded sleep of at most 60 s**, re-reading the **live** config each pass and recomputing the next due time. Turning the schedule off stops the next send within a minute; turning it on schedules from the new settings, not from a previously computed interval. Do not sleep until the next due time in one go — that is what makes a config change take effect only after the old interval.
- **De-duplicated by period:** a `report_sent` event recording the period about to be covered suppresses the send. Record it after a send, with the period covered.
- **At most one catch-up on startup:** if the schedule is on and the last `report_sent` inside the 90-day event window is older than one period, send one summary for the **most recent completed** period. Never a backlog.
- It emails `summary()` for the previous completed day or week to the owner **plus `extra_recipients`**.
- A failed send logs and retries at the next due time. **The loop never raises** — `_supervise` would restart it, but a scheduler that crashes on every pass would email nothing and hide the fault.

**Tests to write first, with an injected clock:** next-due for `daily` at a given hour, including when now is exactly the hour and when it has just passed; next-due for `weekly` on the configured Monday-based weekday; `off` schedules nothing and sends nothing; switching from `daily` to `off` between passes suppresses a send that was due, within one bounded sleep; switching from `off` to `daily` schedules from the **new** hour; a period is never sent twice, proved by a pre-existing `report_sent` for that period suppressing the send; startup catch-up sends exactly **one** summary when the last send is several periods old, and covers the most recent completed period; a send failure does not stop the loop and the next due time still fires; recipients are the owner plus `extra_recipients`, deduplicated if the owner is also listed.

**Done when:** `uv run pytest tests/test_report_scheduler.py` green and the full suite green. Commit: `feat(reports): scheduled summary with bounded sleep and catch-up`.

---

## Wave 3 — tasks 10, 11 and 12 in parallel

Level constants come from Task 3, so no task here edits `web_interface/levels.py`.

### Task 10: The four report levels and the email action

**Spec:** §3.

**Files:**
- Modify: `web_interface/routes/reports.py`
- Create: `web_interface/templates/reports_period.html`, `reports_product.html`, `reports_product_sku.html`, `reports_method.html`, `reports_collections.html`, `tests/test_routes_reports_levels.py`

**Interfaces — Produces:**
- `GET /reports/period`, `/reports/product`, `/reports/method`, `/reports/collections` — all gated `view_reports`. Each takes `?range=` (default `30d`); `/reports/period` also takes a bucket, defaulting to day for `7d`/`30d`, week for `90d`, month for `12m`/`all`.
- `GET /reports/product/{sku}` — that SKU by period; a sub-level of `/reports/product` built with `Level.child`.
- `POST /reports/email` — gated `view_reports` plus `require_htmx`. Takes the page's parameters, sends a plain-text rendering in the body **and** the same rows as a CSV attachment named `<machine_id>-<report>-<range>.csv`, to the current user's email, **falling back to the owner's if the user has none**. Reports success or failure inline.
- Part 2's existing `/reports` body is unchanged above, with the four sub-tiles added below it.

**Behaviour to get right:**
- Queries run through `asyncio.to_thread`, as `/activity` does. Do not call them synchronously in the handler.
- `—` is rendered for `None` event-derived columns. A zero there would misreport an old month as fault-free, which is the specific misreading the spec calls out.
- Each table sits in an `overflow-x: auto` container; currency uses the existing template filters. No charts.
- An unknown SKU on `/reports/product/{sku}` renders part 2's shell 404, not a bare JSON error.
- Rebuild `web_interface/static/app.css` if new classes appear, and keep `tests/test_static_css.py` green. (If the executor rebuilds `app.css` once per wave as in part 2, follow that convention and say so.)

**Tests to write first:** each of the five levels 200 for owner and secretary, 403 for tech and loader; breadcrumbs matching the level tree; the bucket default per range; an invalid `range` falling back to `30d` rather than erroring; a bucket outside retention rendering `—` and not `0`; `/reports/product/{sku}` for a real SKU and a shell 404 for an unknown one; `POST /reports/email` calling a **stubbed** mailer with a CSV attachment whose filename matches the documented shape and whose bytes parse back to the rendered rows; the fallback to the owner's address when the current user has no email; the email POST 403 without `HX-Request`; a table wrapped in an `overflow-x` container.

**Done when:** the new test file and the full suite are green. Commit: `feat(reports): by-period, by-product, by-method and collections levels`.

---

### Task 11: Cash collection from the Inventory level

**Spec:** §1.3.

**Files:**
- Modify: `web_interface/routes/inventory.py`, `web_interface/templates/inventory.html`
- Create: `tests/test_routes_inventory_collect.py`

**Interfaces — Produces:**
- `POST /inventory/collect` — gated `collect_cash` (all four roles) plus `require_htmx`, with a **two-tap confirm** using part 2's `partials/confirm_button.html`. Honour that partial's contract, including the `confirming` query parameter its Cancel re-requests with.
- A confirm endpoint for the first tap, following the same shape part 2's fault Clear and code Regenerate use.
- The response shows the **recorded time and the expected amount** so the collector can compare against the box.
- Records the row via `recorder.record_cash_collection(user_id, user_name)` using the current principal's id and name.

**Behaviour to get right:** the amount shown is the `expected_cash` the recorder computed at insert time, not a figure recomputed in the route — otherwise the two can disagree. The recorder enqueues on the writer thread, so the route must `flush()` (in a thread) before reading the row back to display it, or must obtain the value another way; state which you chose and why. A loader must succeed — this is the one money-adjacent action every role may take.

**Tests to write first:** `POST /inventory/collect` is 200 for **all four** roles and records exactly one row each time; the response contains the recorded time and the expected amount; the expected amount counts `cash_coin` and `cash_bill` shares and excludes `card` and `nfc`; a second collection's expected amount counts only cash since the first; the first tap returns a confirm control that does not record a row; Cancel returns to the initial state without recording; the POST is 403 without `HX-Request`.

**Done when:** the new test file and the full suite are green. Commit: `feat(reports): record a cash collection from the Inventory level`.

---

### Task 12: Settings › Reports

**Spec:** §4.1.

**Files:**
- Modify: `web_interface/routes/settings.py`
- Create: `web_interface/templates/settings_reports.html`, `tests/test_routes_settings_reports.py`

**Interfaces — Produces:**
- `GET /settings/reports` and `POST /settings/reports` — gated **`edit_contacts`** (per the spec, not `edit_secrets`), saved through `services/config_store.save_config`.
- A Reports sub-tile on `/settings`, rendered only for a role holding `edit_contacts`.
- Fields: `schedule` (off / daily / weekly), `hour` (0–23), `weekday` (Monday-based), `extra_recipients` (one address per line or comma-separated — state which and be consistent with how the template renders them back).

**Behaviour to get right:** follow the established Settings pattern exactly — mutate the live `ConfigModel`, call `save_config`, and on failure return the form with the error text while **leaving the in-memory model as the user submitted it** (part 2's spec §5). None of these fields is a secret, so the masking machinery must not be applied to them. Invalid `hour` or `weekday` returns the form with a reason rather than a 500 or a silently clamped value. An empty `extra_recipients` must round-trip as `[]`, not as `[""]` — that is the kind of value that later makes the scheduler try to email an empty address.

**Tests to write first:** 200 for owner and secretary, 403 for tech and loader; the sub-tile appears on `/settings` for owner and secretary and not for tech or loader; a POST round-trips every field through `save_config` into a temp config path; `hour=24` and `weekday=7` are rejected with a reason and change nothing; blank `extra_recipients` round-trips to `[]`; a `save_config` failure returns the form with the error and does not revert the submitted values; the POST is 403 without `HX-Request`.

**Done when:** the new test file and the full suite are green. Commit: `feat(reports): Settings › Reports for the scheduled summary`.

---

## Serial step 9

### Task 13: Documentation

**Spec:** §7 (`CLAUDE.md` documents the sales tables and the scheduler).

Documentation only. **Haiku implementer.** Verify with the full suite and `uv run ruff check .` still green.

**Files:** `CLAUDE.md`

**What must be written:**
- **Services section:** `services/reports.py` (queries, `is_cash`, CSV) and `services/report_scheduler.py` (bounded-sleep loop, per-period de-duplication, one catch-up at startup) as new bullets, and the `event_recorder.py` bullet extended to say that `sales` and `cash_collections` are **never pruned** while `events` keeps the 90-day window.
- **The durability contract**, stated plainly because it is the part most likely to be broken by a later change: a sale is inserted synchronously on its own WAL connection and awaited via `asyncio.to_thread` before the FSM returns to idle; on failure it goes to `data/sales-journal.jsonl` and raises `DATA-101`, replayed at startup; a corrupt `events.db` is set aside and `DATA-102` raised so the machine still runs.
- **FIFO method attribution:** what `escrow_credits` is, that `credit_escrow` remains the authoritative total, and that `vend_failed` restores the exact consumed shares so money is never reclassified.
- **Configuration:** the new `config.reports` section and that it is edited at `/settings/reports`.
- **The two new fault codes** and that both are alert-class and deliberately absent from `PAYMENT_BLOCKING_FAULTS`.
- **Level tree:** the four new report URLs and Settings › Reports.

**Done when:** `grep -n "never pruned\|sales-journal\|DATA-101\|DATA-102\|report_scheduler" CLAUDE.md` finds each claim; suite and ruff green. Commit: `docs: document the sales tables, durability contract and scheduler`.

---

## Definition of Done

1. `uv run pytest` green; `uv run ruff check .` and `uv run ruff format --check .` clean.
2. `pyproject.toml` and `uv.lock` unchanged — no new runtime dependency.
3. A sale older than the retention window survives `prune()`.
4. A sale row is on disk **before** the FSM returns to idle, proved by ordering rather than co-occurrence.
5. `PAYMENT_BLOCKING_FAULTS` still has exactly six members; `DATA-101` and `DATA-102` are not among them.
6. `git diff origin/main -- web_interface/templates/screen.html web_interface/templates/partials/screen_body.html` is empty.
7. `grep -rn "unpkg\|cdn.tailwindcss.com" web_interface/` matches only `templates/screen.html`.
8. Part 1 and part 2 properties do not regress: identical login-failure bodies, `no-store` on plaintext-code responses, owner self-lockout refused, owner-targeted writes gated on `manage_ownership`, placement writes unable to touch catalog fields, exactly one `<main>` and one `#pill` after a boosted navigation.
9. The CI skip-guard fails on an unrecognised skip (Task 1), and the three browser tests still run in CI.
10. Every new test has been shown to fail against the pre-change implementation, and the executor's report says so per task.

## Program goals claimed (program plan §2)

**7** — sales are recorded per sale, forever, with SKU, price and how it was paid, and the owner can get them on screen or by email without a developer. **9** — a corrupt events database now sets the file aside, starts fresh and raises an alert fault, so from this part on it degrades history rather than the machine; sale recording never blocks the event loop and never loses a row silently. Goals 1–6 belong to parts 1 and 2 and must not regress. Goal 8 is part 4's.

## Spec coverage check

| Spec section | Task(s) |
|---|---|
| §1.1 escrow credits, FIFO, `vend_failed` restore, snapshot | 4 |
| §1.2 tables, durable `record_sale`, journal, prune scope | 5, 6 |
| §1.2 `DATA-101`; §5 `DATA-102` | 3, 5, 6 |
| §1.3 `expected_cash`, `is_cash`, `collect_cash`, `POST /inventory/collect` | 3, 5, 7, 11 |
| §2 all five queries, windows, bucketing | 7 |
| §3 four report levels, product sub-level, email with CSV | 10 |
| §3 `send_email` attachments | 8 |
| §4.1 `ReportsConfig`, `/settings/reports` | 3, 12 |
| §4.2 scheduler | 9 |
| §5 error handling | 5 (corrupt, journal), 6 (ladder, startup), 7 (empty db, `—`), 10 (inline email failure), 12 (`save_config` failure) |
| §6 testing | every task; `tests/test_reports.py` in 7, `tests/test_report_scheduler.py` in 9 |
| §7 files | 1–13 |
| §8 out of scope | nothing built |
| Part 2 debts (owner-directed) | 1 (skip-guard), 2 (comment) |

