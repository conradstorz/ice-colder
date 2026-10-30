# Money in Integer Cents Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every stored or transmitted amount of money in the repo becomes integer cents, with no change in behaviour or rendered text.

**Architecture:** A new `ice_contracts.money` module is the one source of the `Cents` type and the two conversion helpers. The rename runs bottom-up: catalog price, then storage and reports, then the sale core, then the cloud contract. Each task leaves the full suite green. Where a not-yet-converted neighbour still speaks dollars, the boundary gets a one-line conversion tagged `# cents-shim(T<n>)`, where `n` is the task that removes it.

**Tech Stack:** Python 3.12, Pydantic v2, SQLite (`events.db`), Jinja2 (`ice_ui`), FastAPI, pytest, uv.

**Spec:** `docs/superpowers/specs/2026-10-30-money-in-cents-design.md` (spec A). Read the whole spec before your task. Section numbers below (§n) refer to it.

## Global Constraints

- Use `uv` for everything: `uv run pytest`, `uv run ruff check --fix .`, `uv run ruff format .`, `uv run lint-imports`, `uv run python scripts/check_package_isolation.py`. Never run a bare `ruff`, which is an older global binary.
- Do not chain shell commands with `&&`.
- Package dependency order is `ice_contracts` → `ice_common` → `ice_ui` → (`ice_edge` | `ice_cloud` | `ice_sims`). `ice_ui` imports only `ice_contracts`.
- **No behaviour change.** Every customer message, log line that tests assert on, template, CSV and email body renders byte-identical text for every whole-cent amount.
- **Dollars exist only at the human edge.** Dollar text is parsed with `Decimal`, never `float`. Conversion goes through `ice_contracts.money.dollars_to_cents` only, and display through `format_cents` (or the `money` filter).
- Every money field name ends in `_cents`. Averages of money are float cents named `*_cents`. Shares (fractions) stay plain floats.
- `MAX_PRICE_CENTS = 100_000`.
- Legacy board payment models (`PaymentEvent.amount`, `PaymentRefundCommand.amount`, `PaymentRefundResult.amount_returned`) are **not** changed. Spec B deletes them.
- `/screen` (`partials/screen_body.html`) stays byte-identical to `origin/main`. Its route passes dollar floats computed as `cents / 100`.
- No new Tailwind classes; `app.css` is not rebuilt.
- `MIGRATIONS` in `event_recorder.py` is append-only. Never edit entry 1.
- The events table's columns are `event_type`, `timestamp`, `value`, `metadata`. The spec's word "kind" means `event_type`.
- Tests never sleep on the wall clock. Use the existing fakes (`tests/fakes.py`).
- Commit at the end of each task on the current branch. End each message with the trailer `Assisted-by: Claude Sonnet 5.5 (Claude Code)`, naming the model actually writing it, and add no `Co-Authored-By` line.
- `tests/meta/test_no_private_access.py` budgets move down only. Do not add private access.

## Shared templates (edge and cloud render the same files)

`partials/status_fragment.html`, `partials/kpi_fragment.html` and `health_faults.html` are rendered by the edge **and** by the cloud portal, which draws them from the cloud report models (`StatusView`, `KpiWindow`, `KpiAverage`, `PendingSaleView`). Those models keep their dollar field names until Task 5. These three templates therefore keep their current field names and `"%.2f"` formatting through Tasks 3 and 4. Until then the edge supplies the dollar names they read (`credit_escrow`, `money_in`, `pending_sale.price`, `d.credit_escrow`) as `cents / 100` in its own render context, tagged `cents-shim(T5)`. Task 5 switches the three templates, the models and the portal together. Every gate in Tasks 2 to 5 includes `tests/cloud/test_ui_parity.py tests/cloud/test_portal.py`.

## Running the full suite

If a local `uv run pytest -q` is memory-killed (exit 137 or the process vanishes), that is not a test failure. Rerun by package directory (`tests/edge`, `tests/cloud`, ...), or use the hpz440 container harness. That harness has 4 known failures that come from the missing git checkout, and they are not regressions.

## Shim discipline

A shim is one expression, such as `product.price_cents / 100`, at a boundary to code a later task converts. It carries the trailing comment `# cents-shim(T<n>)`. Task n's last step greps for `cents-shim(T<n>)` and must find nothing. Task 6 greps for `cents-shim` and must find nothing.

---

### Task 1: Money module, `money` filter, and the type ratchet

**Files:**
- Create: `packages/contracts/src/ice_contracts/money.py`
- Modify: `packages/contracts/src/ice_contracts/board/payment.py` (drop its own `Cents`/`PositiveCents`; import and re-export them from `ice_contracts.money`; no wire or schema change)
- Modify: `packages/ui/src/ice_ui/filters.py`, `packages/ui/src/ice_ui/templating.py` (register `money` in `make_templates`, so both the normal and the sandboxed environments get it)
- Create: `tests/contracts/test_money.py`, `tests/meta/test_money_types.py`
- Modify: `tests/ui/test_filters.py`, `tests/ui/test_templating.py`

**Interfaces:**
- Produces:
  - `ice_contracts.money.Cents = Annotated[StrictInt, Field(ge=0)]` and `PositiveCents` (`gt=0`).
  - `dollars_to_cents(x: float | int | str | Decimal) -> int`: `Decimal(str(x))` times 100, quantized half-even to an integer. Raises `ValueError` on negative, NaN, infinity, or an unparsable string. It does **not** reject more than two decimals, because legacy floats are rounded. The form's stricter check is Task 2's.
  - `format_cents(c: int) -> str`: `"$1.25"`, `"$0.00"`, `"-$0.50"`, and `"$1000.00"` for 100000. There is no thousands separator, because today's `"%.2f"` has none.
  - `cents_to_dollar_text(c: int) -> str`: `"1.25"`, the number without the `$`. Used by the CSV, the email body, the form pre-fill and the gateway URL.
  - `ice_ui.filters.money(cents: int) -> str` delegates to `format_cents`. `None`, or any non-int, raises `TypeError`, so a missed rename fails loudly instead of rendering blank. Nullable sites guard with `is not none` in the template.
- The ratchet `tests/meta/test_money_types.py`:
  - Imports every module under `packages/*/src` and collects every Pydantic `BaseModel` subclass and `@dataclass`.
  - Fails on any field whose name matches `(?i)price|amount|credit|revenue|cash|money|refund` and whose annotation is or contains `float`.
  - Has an `ALLOWLIST: dict[str, str]` of `"module.Class.field": reason`. It starts with **every current offender**. The test also fails when an allowlisted entry is no longer an offender ("remove it from ALLOWLIST"), so the list only shrinks.
  - Mark which entries are permanent: the legacy payment models (until spec B) and the average fields.

- [ ] **Step 1:** Write `tests/contracts/test_money.py` covering:
  - `dollars_to_cents(2.675) == 268`, `("1.10") == 110`, `(0.1 + 0.2) == 30`, `(3) == 300`, `(Decimal("0.005")) == 0` (half-even), and `(Decimal("0.015")) == 2`.
  - `-1`, `float("nan")`, `float("inf")` and `"abc"` each raise `ValueError`.
  - The `format_cents` cases above, and `cents_to_dollar_text(5) == "0.05"`.
  - A model field typed `Cents` rejects `1.0`, `True` and `-1`.
  - `ice_contracts.board.payment.Cents is ice_contracts.money.Cents`.
- [ ] **Step 2:** Run `uv run pytest tests/contracts/test_money.py -v`. Expected: FAIL (`ModuleNotFoundError`).
- [ ] **Step 3:** Implement `money.py` and the payment re-export.
- [ ] **Step 4:** Add the filter tests: `money(125) == "$1.25"`, and a sandboxed `make_templates(..., sandboxed=True)` renders `{{ 125|money }}`. Implement the filter.
- [ ] **Step 5:** Write the ratchet. To seed `ALLOWLIST`, run the collector once and paste its offender list.
- [ ] **Step 6:** Run `uv run python -m ice_contracts.generate`. `git diff docs/contracts` must be empty.
- [ ] **Step 7:** Run `uv run pytest tests/contracts tests/ui tests/meta -q`, `uv run lint-imports` and `uv run ruff check .`. Expected: all pass.
- [ ] **Step 8:** Commit with the message `feat(contracts): ice_contracts.money, money filter, money type ratchet`.

---

### Task 2: Catalog price in cents (`Product.price_cents`)

**Files:**
- Modify: `packages/common/src/ice_common/config_model.py` (`Product`, `MAX_PRICE_CENTS`)
- Modify: `packages/edge/src/ice_edge/config/config_store.py` (`add_product`/`update_product` take `price_cents: int`)
- Modify: `packages/edge/src/ice_edge/web/routes/products/{helpers,catalog,overview,placement,__init__}.py` and `routes/inventory.py` (wherever `.price` is read)
- Modify: `packages/edge/src/ice_edge/payment/payment_gateway_manager.py` (`amount_cents: int` everywhere; the URL query and log text keep dollar text via `cents_to_dollar_text`)
- Modify the templates `product.html`, `products.html`, `product_catalog.html`, `product_form.html`. Each `${{ "%.2f"|format(x) }}` becomes `{{ x|money }}`. Note the `$` moves into the filter, so delete the literal `$` before the braces. The form inputs keep `name="price"` and are pre-filled with `cents_to_dollar_text(price_cents)`, passed from the route as `price_text`.
- Modify: `config.example.json` and `docker/machine/launcher.py`, which use `price_cents` (300, 50, 200 and 300, 100, 200).
- Shim: every sale-path reader of `product.price` (`vmc.py`, `test_sale.py`, `dispense_cycle.py`, `machine.py`, `views/*`) becomes `product.price_cents / 100  # cents-shim(T4)`.
- Tests: `tests/common/test_config_model.py`, `tests/edge/config/*`, `tests/edge/web/` product route tests, and `tests/docker/test_machine_launcher.py` if it asserts prices. Change every `Product(..., price=X)` fixture to `price_cents=` across `tests/` (search for `price=`). Leave a `price=` keyword argument passed to a non-`Product` call alone.

**Interfaces:**
- Consumes `dollars_to_cents`, `cents_to_dollar_text` and the `money` filter from Task 1.
- Produces:
  - `Product.price_cents: Cents`.
  - `ice_common.config_model.MAX_PRICE_CENTS = 100_000`.
  - `Product`'s `model_validator(mode="before")` behaves as follows:
    - A dict with `price` and no `price_cents` gets `price_cents = dollars_to_cents(price)`, with `price` removed.
    - A dict with both keeps `price_cents`, drops `price`, and warns.
    - Each converted sku is logged once with a warning naming it.
    - A value over the cap is kept with a warning, as when loading an over-long sku.
  - An unparsable or negative legacy `price` is refused as a validation error. Today's model accepts any float, but a negative price was never valid.
  - `save_config` writes `price_cents` only. `model_dump` naturally does this.
- Form parse helper in `routes/products/helpers.py`: `parse_price_form(text: str) -> int`. It raises `ValueError` on:
  - empty input;
  - more than two decimal places (`Decimal(text).as_tuple().exponent < -2`);
  - a negative value;
  - a value over `MAX_PRICE_CENTS`.
  The routes turn `ValueError` into a 400 and leave the stored product untouched. The `price` form field is received as `str = Form(...)`.

- [ ] **Step 1:** Write the failing config tests:
  - A legacy `{"price": 2.5}` loads as 250 and logs one warning naming the sku.
  - Both keys present keeps `price_cents`.
  - `price_cents=200_000` loads with a warning.
  - `model_dump()` has no `price` key.
  - `{"price": 2.675}` loads as 268.
- [ ] **Step 2:** Write the failing route tests. POST `price="2.5"` stores 250. `"2.505"`, `"-1"`, `"1000.01"` and `""` each answer 400, with the product's `price_cents` unchanged and `config.json` not rewritten.
- [ ] **Step 3:** Run both sets. Expected: FAIL.
- [ ] **Step 4:** Implement the model, the store, the routes, the gateway manager, the templates, the example config and the launcher, plus the shims.
- [ ] **Step 5:** Run `uv run pytest -q -x`. Every existing test that asserts a rendered price (for example `$2.50`) must pass **unchanged**. That is the behaviour check. If one fails, fix the code, not the expected string. There is one sanctioned exception: the price input's pre-fill changes from `value="2.5"` to `value="2.50"` (`cents_to_dollar_text`). Update any test that asserts the old pre-fill.
- [ ] **Step 6:** Run `uv run ruff check --fix .`, `uv run ruff format .` and `uv run lint-imports`. Shrink the Task 1 `ALLOWLIST` (`Product.price` is gone).
- [ ] **Step 7:** Commit with the message `feat: catalog price in integer cents (Product.price_cents)`.

---

### Task 3: Storage and reports in cents (`events.db` migration 2, journal, reports, report rendering)

**Files:**
- Modify: `packages/edge/src/ice_edge/ledger/event_recorder.py`:
  - `MIGRATIONS[1]`, plus the `record_sale`, `_insert_sale_row`, `replay_sales_journal`, `sale_recorded_since`, `_insert_cash_collection`, `_compute_window`, `get_historical_average`, `SUMMARY_KEYS` and `_on_payment` changes listed below.
- Modify: `packages/edge/src/ice_edge/reporting/reports.py` and `report_scheduler.py`.
- Modify: `packages/edge/src/ice_edge/web/routes/reports/{helpers,compute,overview,levels,email}.py` and `routes/inventory.py` (cash collect result).
- Modify the templates `reports*.html`, `partials/cash_collection_result.html` and `partials/collect_cash_button.html`. Each `"%.2f"` money site becomes `|money`, with the leading literal `$` removed. `reports_method.html`'s share percentage is unchanged.
- Modify: `packages/edge/src/ice_edge/web/routes/screen.py` (`money_24h` = `summary["money_in_cents"] / 100`). This is permanent and not a shim, because `/screen` is pinned.
- Add shims at the callers that still hold dollars: `dispense_cycle.record()` (price and shares), `SaleJournal`'s `vend_failed`/`refund`/`refund_failed` rows, and `machine.py`'s `session_uncertain` row. Each converts with `dollars_to_cents(...)  # cents-shim(T4)`. `views/health.py`'s KPI to the cloud `KpiWindow`/`KpiAverage` becomes `x / 100  # cents-shim(T5)`. The edge's `/kpi` route context adds `money_in` (and `average.money_in`) as `cents / 100  # cents-shim(T5)` for the shared `kpi_fragment.html` (see "Shared templates"). `reports.html` is edge-only, so it switches to `|money` now.
- Tests: `tests/edge/ledger/test_event_recorder.py`, `tests/edge/reporting/test_reports.py`, `test_report_scheduler.py`, the report and inventory route tests, and a new `tests/edge/ledger/test_migration_cents.py`.

**Interfaces:**
- `MIGRATIONS[1]` is plain SQL statements separated by `;`. The runner splits on `;`, so no statement may contain a literal `;` inside a string. It does the following:
  - Rebuild `sales` as `sales_new(id, ts, sku, name, slot, price_cents INTEGER NOT NULL, methods TEXT NOT NULL)`:
    - `price_cents` is filled with `CAST(ROUND(price * 100) AS INTEGER)`.
    - `methods` is filled with `(SELECT json_group_object(key, CAST(ROUND(value * 100) AS INTEGER)) FROM json_each(sales.methods))`. Preserve `'{}'` for an empty object; `json_group_object` over zero rows returns `'{}'`, but verify it in the test.
    - Keep `id`s.
    - Then `DROP TABLE sales` and `ALTER TABLE sales_new RENAME TO sales`, and re-create `idx_sales_ts` and `idx_sales_sku_ts`.
  - Rebuild `cash_collections` the same way, with `expected_cash_cents INTEGER NOT NULL`.
  - `UPDATE events SET value = ROUND(value * 100) WHERE event_type IN ('payment','refund','refund_failed','vend_failed','session_uncertain') AND value IS NOT NULL`.
  - `_migrate` already wraps each migration plus the `user_version` bump in one transaction. Confirm it, don't add one.
- `record_sale(sku, name, slot, price_cents: int, methods: dict[str, int], ...)` keeps its existing other parameters. The journal row written on failure gains `"units": "cents"` and `"price_cents"`.
- `replay_sales_journal` reads a row without `"units"` as legacy, converting `price` and every `methods` value with `dollars_to_cents`. The same rule applies to `sales-journal.rejected.jsonl` if anything re-reads it.
- `sale_recorded_since(sku, since_ts, methods: dict[str, int], window_seconds)` compares exactly and drops its local `cents()` helper.
- `_compute_window` returns `money_in_cents: int` and `refunds_cents: int`. Read SUM as an integer with `int(round(x))`, since the column is REAL. `SUMMARY_KEYS` renames to match.
- `get_historical_average` returns `money_in_cents: float | None` for the average and stops rounding to 2 places for money keys. Other keys are unchanged.
- `_on_payment` records `value=dollars_to_cents(event.amount)`, a legacy-boundary conversion that is permanent until spec B and not a shim.
- `reports.py` renames the money keys to `revenue_cents`, `refunds_cents`, `amount_cents`, `expected_cash_cents` and `cash_since_last_collection_cents`, all ints. `by_method`'s `share` stays a float fraction. `_cash_since` and `cash_since_last_collection` return `int`.
- `render_csv` keeps the CSV header names. Money cells are written with `cents_to_dollar_text`. The column-to-key mapping lives in the route that calls it, so update it there.
- `report_scheduler._compose_body` formats with one helper, `_amount(c: int) -> str` (`cents_to_dollar_text`), so the email text is byte-identical.

- [ ] **Step 1:** Write `test_migration_cents.py`:
  - Build a v1 database by executing `MIGRATIONS[0]` only and setting `user_version=1`.
  - Insert sales with prices `2.5`, `0.1+0.2`, `2.675` (stored float) and `0.0`, and methods `{"cash": 0.30000000000000004, "card": 2.2}` and `{}`.
  - Insert a cash collection of `12.34`, and events `payment 1.25`, `refund 0.5`, `heartbeat 60.0`, `temp_exceedance 41.5` and `dispense 1.0`.
  - Open it with `EventRecorder`. Assert `user_version == 2`, the exact ints, `{}` preserved, non-money events untouched, no `price` column (`PRAGMA table_info`), and both indexes present.
  - Pin the case where `ROUND` and half-even disagree (`2.675` stored as a float is `267.49999…`, which gives 267). Document in the test why that is acceptable: this code always wrote `round(x, 2)` and the float of a 2-decimal value rounds back correctly. If the result differs, assert what SQLite actually produces and note it.
- [ ] **Step 2:** Write journal tests:
  - A legacy row `{"price": 2.5, "methods": {"cash": 2.5}}` replays to 250/250.
  - A new row carries `"units": "cents"` and replays unchanged.
  - A mixed file replays both.
- [ ] **Step 3:** Update `test_reports.py` and `test_report_scheduler.py` to the `_cents` keys with integer fixtures. Email body and CSV byte expectations stay **unchanged**.
- [ ] **Step 4:** Run the new and changed tests. Expected: FAIL.
- [ ] **Step 5:** Implement the recorder, the reports, the scheduler, the routes, the templates and the shims.
- [ ] **Step 6:** Run `uv run pytest -q`. Every report, KPI, collection and `/screen` rendering test passes with its rendered text unchanged.
- [ ] **Step 7:** Run ruff, `lint-imports` and the ratchet, and shrink `ALLOWLIST`.
- [ ] **Step 8:** Commit with the message `feat(ledger): events.db migration 2 and reports in integer cents`.

---

### Task 4: Sale core in cents (ledger, snapshot, FSM, refunds, journal, status)

This task is large. If the implementer reports it cannot finish in one pass, split it. Part (a) is the ledger, the session store and recovery, with a `cents-shim(T4)` at the VMC's use of the ledger. Part (b) is the FSM, refunds, the journal, status and the edge contexts.

**Files:**
- Modify: `packages/edge/src/ice_edge/ledger/escrow_ledger.py`:
  - `total_cents: int`.
  - Remove `TOLERANCE` and every `round`.
  - Rename `is_empty_within_tolerance` to `is_empty`.
  - The divergence guard compares exactly.
  - `consume_fifo(price_cents: int) -> dict[str, int]`, `restore(shares, ts, price_cents)`, `restore_shares(shares) -> int`, `take_all() -> int`.
- Modify: `packages/edge/src/ice_edge/ledger/session_store.py`:
  - `Credit.cents: int`.
  - `SessionSnapshot.credit_escrow_cents: int` and `pending_sale_shares: dict[str, int] | None`.
  - `save` writes `"units": "cents"`. `load` converts a dict without `units` (the legacy keys `credit_escrow`, credit `amount`, and the share values) through `dollars_to_cents`.
  - `is_open` uses `> 0`.
- Modify: `packages/edge/src/ice_edge/ledger/session_recovery.py` (`RecoveryDetail.credit_escrow_cents`, the leftover check `> 0` in place of the `TOLERANCE` comparison, and the `pending_sale_for_recovery` view's price and methods as ints).
- Modify: `packages/edge/src/ice_edge/sale/{sale_context,vmc,sale_journal,outputs,test_sale}.py`:
  - `VMC.credit_escrow_cents` (read/write convenience). Remove `CREDIT_TOLERANCE` and `credit_escrow`.
  - `on_payment_credit` converts the legacy `PaymentEvent.amount` once with `dollars_to_cents`. This stays permanent until spec B.
  - `deposit_funds(method, cents: int, ...)`.
  - `get_status()` returns `credit_escrow_cents`.
  - `SaleJournal` methods take cents and format with `format_cents`. Customer messages must be byte-identical.
- Modify: `packages/edge/src/ice_edge/payment/refund_protocol.py`:
  - `PendingRefund.cents`, `begin(cents: int, reason)`.
  - `_send` builds the legacy `PaymentRefundCommand(amount=cents / 100)`, a permanent legacy boundary with a comment pointing to spec B.
  - `handle_ack` converts `amount_returned` with `dollars_to_cents`.
  - `on_confirmed(pending, returned_cents: int)`.
- Modify: `packages/edge/src/ice_edge/vend/dispense_cycle.py` (price = `sum(sale.shares.values())`, int) and `packages/edge/src/ice_edge/app/machine.py` (the `session_uncertain` value, `recovery_detail`, `_flag_uncertain_session`).
- Modify: `packages/contracts/src/ice_contracts/board/mqtt_messages.py`, where `VMCStatus` gains `credit_escrow_cents: Cents`. The existing `credit_escrow: float` stays, with its description marked deprecated ("removed in 3.0.0; use credit_escrow_cents").
- Modify: `vending_machine.py` `CONTRACT_VERSION` 2.3.0 → **2.4.0**, plus the contract doc changelog entry in `docs/contracts/vending-machine/CONTRACT.md`. Regenerate the schemas.
- Modify: `outputs.state_changed` publishes both fields (`credit_escrow=cents / 100`).
- Modify: `packages/edge/src/ice_edge/web/routes/health/recovery.py` (the "Record as sale" handler passes int price and methods to `record_sale`) and `routes/screen.py` (`escrow = status["credit_escrow_cents"] / 100`, permanent).
- Do **not** modify `health_faults.html` or `partials/status_fragment.html` yet (see "Shared templates"). The edge contexts that render them (`/status`, `/health/faults`) supply `credit_escrow`, `pending_sale.price` and `d.credit_escrow` as `cents / 100  # cents-shim(T5)`.
- Modify: `packages/edge/src/ice_edge/views/health.py`, which builds the cloud `StatusView`/`PendingSaleView` with `# cents-shim(T5)` dollar conversions.
- Remove every `cents-shim(T4)` from Tasks 2 and 3.
- Tests: `tests/edge/ledger/*`, `tests/edge/sale/*`, `tests/edge/payment/*`, `tests/edge/vend/*`, `tests/edge/app/*`, `tests/integration/*`, the health, recovery and status route tests, `tests/contracts/test_contracts_vending.py` and `test_mqtt_messages_validation.py`. Float literals become ints. Tests of float residue such as `0.1 + 0.2` become exact-equality tests over ints.

**Interfaces:**
- Consumes the names from Tasks 1 to 3: `Product.price_cents`, `record_sale(price_cents, methods)`, and `sale_recorded_since(methods: dict[str, int])`.
- Produces the names above. `EscrowLedger.is_empty`, `VMC.credit_escrow_cents`, `SessionSnapshot.credit_escrow_cents` and `RecoveryDetail.credit_escrow_cents` are what the CLAUDE.md update in Task 6 documents.

- [ ] **Step 1:** Rewrite `tests/edge/ledger/test_escrow_ledger.py` over ints:
  - FIFO split across two methods.
  - A partial credit shrinks in place.
  - Restore keeps methods and order.
  - The divergence guard fires on a one-cent mismatch and books `{"unknown": price}`.
  - `take_all` returns the int.
- [ ] **Step 2:** Session store tests:
  - Round-trip with `"units": "cents"`.
  - A legacy `session.json` (`credit_escrow: 1.25`, credits with `amount`, `pending_sale_shares: {"cash": 2.5}`) loads as 125, 125 and 250.
  - A legacy snapshot still raises PAY-104 at boot (`SessionRecovery.evaluate_at_boot` returns `uncertain`).
- [ ] **Step 3:** `VMCStatus` contract test: both fields validate; `credit_escrow_cents` rejects floats.
- [ ] **Step 4:** Run them. Expected: FAIL.
- [ ] **Step 5:** Implement. Then run `uv run pytest tests/edge tests/integration tests/contracts tests/cloud/test_ui_parity.py tests/cloud/test_portal.py -q`, convert float fixtures in the failing tests to ints, and rerun until green. Assertions on customer messages, journal lines and rendered dollar text must not change.
- [ ] **Step 6:** Run `grep -rn "cents-shim(T4)" packages`. Expected: no output. Run `grep -rn "TOLERANCE\|is_empty_within_tolerance\|CREDIT_TOLERANCE" packages tests`. Expected: no output.
- [ ] **Step 7:** Run the full `uv run pytest -q`, ruff, `lint-imports` and `check_package_isolation`. Shrink `ALLOWLIST`.
- [ ] **Step 8:** Commit with the message `feat(sale): escrow, snapshot and refunds in integer cents; vending contract 2.4.0`.

---

### Task 5: Cloud contract 1.0.0

**Files:**
- Modify: `packages/contracts/src/ice_contracts/cloud/report.py`:
  - `CLOUD_CONTRACT_VERSION = "1.0.0"`.
  - `StatusView.credit_escrow_cents: Cents`.
  - `PendingSaleView.price_cents: Cents` and `methods: dict[str, Cents]`.
  - `KpiWindow.money_in_cents: Cents`.
  - `KpiAverage.money_in_cents: float | None`.
  - `MachineReport.contract: Literal["0.2.0", "0.3.0", "0.4.0", "1.0.0"]`.
  - Each of the four models gets a `model_validator(mode="before")`. When the input is a dict with the old key and without the new one, the old key is popped and converted with `dollars_to_cents`. `KpiAverage` multiplies by 100 as a float and does not quantize, since it is an average. `None` stays `None`. `extra="forbid"` is kept.
- Modify: `packages/edge/src/ice_edge/views/health.py` and the uplink report builder to drop the `cents-shim(T5)` lines and send cents. The edge always sends `"1.0.0"`.
- Modify the shared templates `partials/status_fragment.html` (`status.credit_escrow_cents|money`, both sites), `partials/kpi_fragment.html` (`summary.money_in_cents|money`, `average.money_in_cents` rounded to an int before `|money`) and `health_faults.html` (`d.credit_escrow_cents > 0`, and `|money` for the leftover and `f.pending_sale.price_cents`). Remove the edge-side `cents-shim(T5)` dollar aliases.
- Modify: `packages/cloud/src/ice_cloud/fleet_view.py` (the revenue headline uses `format_cents(sum(s.money_in_cents ...))`). Check that the text matches the old `f"${x:.2f}"` and keep its test expectation unchanged.
- Modify: the cloud portal's render-context builder (`packages/cloud/src/ice_cloud/portal.py`, or wherever it builds `status`/`summary`/`average`/fault context from a report). Beside each `_cents` key, add the legacy display aliases `credit_escrow`, `price` and `money_in` as `cents / 100` floats, with a comment saying they are for pre-1.0.0 uploaded bundles and are removed once no 0.x edge reports.
- Modify: `docs/contracts/edge-cloud/CONTRACT.md` (1.0.0 entry, field table, and the deploy order: cloud first, since a 0.4.0 cloud answers 422 and the uplink retries). Regenerate with `uv run python -m ice_contracts.generate`.
- Tests: `tests/contracts/test_cloud_contract.py`, `tests/cloud/test_ingest.py`, `test_portal.py`, `test_fleet_view.py`, `test_ui_parity.py`, `test_store.py`/`test_repository.py` where they hold report fixtures, and `tests/cloud/reports.py` (the report fixture factory).

**Interfaces:**
- Consumes the edge `get_status()["credit_escrow_cents"]` and the summary keys `money_in_cents` from Tasks 3 and 4.
- Produces the 1.0.0 models. Old-key input is accepted only through the before-validators.

- [ ] **Step 1:** Write contract tests:
  - A 0.4.0 report fixture (dollar keys) and the equivalent 1.0.0 fixture validate to equal objects, apart from `contract`.
  - `KpiAverage(money_in=1.235)` gives `money_in_cents == 123.5`.
  - A dict with both old and new keys keeps the new one and still fails `extra="forbid"` on nothing.
  - A float for `credit_escrow_cents` is rejected.
- [ ] **Step 2:** Write cloud tests:
  - A stored 0.4.0 snapshot file under `machines/` re-validates on read and renders `/status` with the same dollar text.
  - A `machine_alert_state.faults_json` row holding a 0.4.0 `PendingSaleView` reads back.
  - Ingest accepts a 0.4.0 and a 1.0.0 report.
  - **Old bundle:** write a bundle whose `partials/status_fragment.html` is the pre-change template (copy the old file content into the test fixture, using `"%.2f"|format(status.credit_escrow)`). Mark it trusted, render it against a 1.0.0 report, and assert the dollar text.
- [ ] **Step 3:** Run them. Expected: FAIL.
- [ ] **Step 4:** Implement.
- [ ] **Step 5:** Run `grep -rn "cents-shim(T5)" packages`. Expected: no output.
- [ ] **Step 6:** Run `uv run pytest tests/contracts tests/cloud tests/edge/uplink tests/edge/views -q`, then the full suite, ruff, `lint-imports` and `check_package_isolation`. Shrink `ALLOWLIST`.
- [ ] **Step 7:** Commit with the message `feat(cloud)!: edge-cloud contract 1.0.0, money in cents`.

---

### Task 6: Close-out (ratchet final state, docs)

**Files:**
- Modify: `tests/meta/test_money_types.py`. `ALLOWLIST` now holds only the legacy board payment models (each with the reason "deleted by spec B") and the average fields.
- Modify `CLAUDE.md` and replace every stale money statement:
  - `EscrowLedger.TOLERANCE` / `is_empty_within_tolerance`, which become exact ints.
  - `credit_escrow`, which becomes `credit_escrow_cents`.
  - `Product.price`, which becomes `price_cents` with `MAX_PRICE_CENTS`.
  - The `SessionRecovery` "within `EscrowLedger.TOLERANCE`" sentence.
  - The cloud contract 0.4.0 note, which gains a 1.0.0 sentence (cents, before-validators, legacy aliases, cloud first).
  - The vending contract 2.4.0 line in the Fault Codes section.
  - The events.db migration 2 note under `event_recorder.py`.
- Modify: `TODO.md`, recording that M3 spec A is done and spec B is next.

- [ ] **Step 1:** Run `grep -rn "cents-shim" packages tests`. Expected: no output.
- [ ] **Step 2:** Run `grep -rnE '"%\.2f"\|format' packages/ui/src/ice_ui/templates`. Expected: only `partials/screen_body.html`.
- [ ] **Step 3:** Run the full gate: `uv run pytest -q`, `uv run ruff check .`, `uv run ruff format --check .`, `uv run lint-imports`, `uv run python scripts/check_package_isolation.py` and `uv run python -m ice_contracts.generate` (then `git diff --exit-code docs/contracts`). Expected: all pass.
- [ ] **Step 4:** Commit with the message `docs: CLAUDE.md and TODO for money in cents`.
