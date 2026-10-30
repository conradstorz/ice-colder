# Money in integer cents — design

**Date:** 2026-10-30
**Status:** approved design, awaiting implementation plan
**Scope:** spec A of 2 for TODO item M3. This spec changes how money is represented, end to end, and
changes no behaviour. Spec B (`2026-10-30-payment-cutover-design.md`) moves the edge and the MDB
simulator onto `ice_contracts.board.payment` and is written against the codebase this spec produces.
Build A first.

## 1. Problem

`ice_contracts.board.payment` carries every amount as `StrictInt` cents. Everything else in the repo
carries float dollars: `Product.price`, `EscrowLedger` (with a `TOLERANCE = 0.005` and `round(…, 2)` on
every operation to hide float residue), `Credit`, `SaleContext.shares`, `SessionSnapshot`,
`PendingRefund`, the `sales.price REAL` column, `cash_collections.expected_cash REAL`, the money rows in
`events.value`, the sales journal fallback file, the report queries, the cloud report contract, and about
twenty templates that format with `"%.2f"|format(...)`. Converting at the payment boundary would keep
float arithmetic on the money path and need a rounding step on every payout. The owner chose cents
everywhere (2026-10-30).

`Product.price` also has no validation at all today (no `ge`, no precision check, the form takes
`float = Form(...)` and stores what FastAPI coerces).

## 2. Decisions

1. **One type.** `ice_contracts.money` (new module, lowest package) defines `Cents =
   Annotated[StrictInt, Field(ge=0)]` and `PositiveCents` (`gt=0`). `ice_contracts.board.payment`
   re-exports them from there instead of defining its own (same names, no wire change).
2. **Every stored or transmitted amount is integer cents.** Averages and shares (a fraction of
   revenue) stay floats, but an average of money is a float *number of cents*, named `*_cents`.
3. **Dollars exist only at the human edge:** the price form input and rendered text. Parsing uses
   `Decimal`, never `float`.
4. **No behaviour change.** Every report total, CSV column, customer message and template renders the
   same text as before for every amount that was a whole number of cents.
5. **Old data is converted, never refused,** with `round(Decimal(str(x)) * 100)` (half-even, via
   `Decimal`, so `2.675` stored as a float converts to the intended `268`, not `267`).

## 3. Field renames and types

Every money field gains a `_cents` suffix so a missed conversion fails loudly instead of silently
multiplying by 100.

| Where | Before | After |
|---|---|---|
| `ice_common.config_model.Product` | `price: float` | `price_cents: Cents` |
| `ice_edge.ledger.escrow_ledger` | `total: float`, `TOLERANCE`, `round(...,2)` | `total_cents: int`; `TOLERANCE` and all rounding removed; `is_empty_within_tolerance` becomes `is_empty` |
| `ice_edge.ledger.session_store.Credit` | `amount: float` | `cents: int` |
| `SessionSnapshot` | `credit_escrow: float`, `pending_sale_shares: dict[str, float]` | `credit_escrow_cents: int`, `pending_sale_shares: dict[str, int]` |
| `SaleContext.shares` | `dict[str, float]` | `dict[str, int]` |
| `PendingRefund` | `amount: float` | `cents: int` |
| `VMC` | `credit_escrow`, `CREDIT_TOLERANCE` | `credit_escrow_cents`; `CREDIT_TOLERANCE` removed |
| `RecoveryDetail` | `credit_escrow: float` | `credit_escrow_cents: int` |
| `ice_contracts.board.mqtt_messages.VMCStatus` | `credit_escrow: float` | `credit_escrow_cents: Cents` (vending contract minor bump, §6) |
| `SaleJournal`, `PaymentGatewayManager`, `config_store.add/update_product` | `float` args | `int` cents args |
| `ice_contracts.cloud.report` | see §7 | see §7 |

The legacy board payment models (`PaymentEvent.amount`, `PaymentRefundCommand.amount`,
`PaymentRefundResult.amount_returned`) are **not** changed: spec B deletes them. Until then the edge
converts once, at `VMC.on_payment_credit` (dollars → cents) and at `RefundProtocol._send` /
`handle_ack` (cents ↔ dollars), each through the one helper `ice_contracts.money.dollars_to_cents`.

`EscrowLedger.consume_fifo`'s divergence guard (ledger total vs credit sum) becomes an exact integer
comparison. The `{"unknown": price}` bug-guard path stays.

## 4. Config migration (`config.json`)

- `Product` gets a `model_validator(mode="before")`: a dict with `price` and no `price_cents` has
  `price` converted with `dollars_to_cents` and removed, and a warning is logged once per load naming
  the converted skus. A dict with both keeps `price_cents` and drops `price` with a warning.
- `price_cents` must be `>= 0` and is capped at `MAX_PRICE_CENTS = 100_000` ($1,000); a larger value
  loaded from disk is kept with a warning (loading never refuses, as for `MAX_SKU_LENGTH`), a larger
  value from the form is a 400.
- `save_config` writes only `price_cents`. The first save after an upgrade rewrites the file; the
  rolling `config.json.bak` keeps the pre-upgrade copy.
- `config.example.json` and the fleet launcher's seeded config (`docker/machine/launcher.py`) switch
  to `price_cents`.

## 5. Edge storage

### 5.1 `events.db`

`MIGRATIONS` gains entry 2 (append-only; `user_version` 1 → 2). Because the runner splits on `;`
and runs plain SQL, the migration is SQL only:

- `sales`: add `price_cents INTEGER`, fill it with `CAST(ROUND(price * 100) AS INTEGER)`, then rebuild
  the table without `price` (SQLite `CREATE TABLE sales_new … ; INSERT … ; DROP … ; ALTER … RENAME`)
  so no reader can pick up the stale REAL column. Indexes are re-created.
- `sales.methods` JSON values: converted in the same rebuild with `json_each`/`json_group_object`
  (`ROUND(value * 100)`).
- `cash_collections.expected_cash REAL` → `expected_cash_cents INTEGER` by the same rebuild pattern.
- `events.value` for money kinds (`payment`, `refund`, `refund_failed`, `vend_failed`,
  `session_uncertain`): `UPDATE events SET value = ROUND(value * 100) WHERE kind IN (...)`. The column
  stays `REAL` because it is generic (uptime, temperature, slot numbers); money rows now hold whole
  numbers of cents in it, read back with `int(...)`.

SQLite's `ROUND` is half-away-from-zero on the stored binary value, not `Decimal` half-even; for
amounts that were written by this code (always `round(x, 2)` first) the two agree. The migration test
(§9) pins the edge cases.

A database at `user_version` 2 opened by an old build already sets `db_newer_than_code` and journals
sales (existing DATA-103 behaviour), so a rollback after this migration degrades, not corrupts.

### 5.2 Files carrying JSON money

`sales-journal.jsonl`, `sales-journal.rejected.jsonl` and `session.json` rows gain
`"units": "cents"`. Readers treat a row **without** the marker as legacy dollars and convert it with
`dollars_to_cents` (`replay_sales_journal`, `SessionStore.load`). A journal written before the upgrade
and replayed after it therefore lands correctly; a `session.json` left by the old build is still valid
PAY-104 evidence.

### 5.3 Queries

`sale_recorded_since` compares integer method dicts exactly (its local `cents()` helper goes).
`_insert_cash_collection`, `_compute_window` (`money_in`, `refunds`), `get_historical_average`, and
every function in `ice_edge.reporting.reports` sum integers. `by_method`'s `share` stays a float
fraction. Report dicts rename money keys with `_cents` (`revenue_cents`, `refunds_cents`,
`amount_cents`, `cash_since_cents`, `expected_cash_cents`, `money_in_cents`).

## 6. Contracts

- New `ice_contracts.money`: `Cents`, `PositiveCents`, `dollars_to_cents(x: float | str | Decimal)
  -> int` (half-even via `Decimal`, rejects negative and NaN), `format_cents(c: int) -> str`
  (`"$1.25"`, `"-$0.50"` for a negative delta).
- `ice_contracts.board.vending_machine` **2.3.0 → 2.4.0** (additive): `VMCStatus` gains
  `credit_escrow_cents: Cents` and keeps the old `credit_escrow: float`, populated from the cents
  value (`cents / 100`) and marked deprecated. Renaming the field outright would be a major bump, and
  spec B's 3.0.0 already owns the removal of legacy payment items, so the float field is removed
  there. No current reader uses it: the MDB simulator's `_watch_vmc_status` reads only `state` and
  `selected_product`, and Home Assistant discovery exposes no escrow entity.

## 7. Cloud contract 1.0.0

`ice_contracts.cloud.report` `CLOUD_CONTRACT_VERSION` **0.4.0 → 1.0.0** (major: types change).

| Field | 0.x | 1.0.0 |
|---|---|---|
| `StatusView` | `credit_escrow: float` | `credit_escrow_cents: Cents` |
| `PendingSaleView` | `price: float`, `methods: dict[str, float]` | `price_cents: Cents`, `methods: dict[str, Cents]` |
| `KpiWindow` | `money_in: float` | `money_in_cents: Cents` |
| `KpiAverage` | `money_in: float \| None` | `money_in_cents: float \| None` (an average of cents) |

- `MachineReport.contract` widens to `Literal["0.2.0", "0.3.0", "0.4.0", "1.0.0"]`.
- Each changed model gets a `model_validator(mode="before")` that, for input carrying the old key and
  not the new one, converts with `dollars_to_cents` and renames. The cloud therefore ingests reports
  from not-yet-upgraded edges, **and** every stored snapshot (`machines/<fp>.json`) and
  `machine_alert_state.faults_json` row re-validates on read through the same converter, so nothing
  already stored becomes "no report". `extra="forbid"` stays; the old key is consumed by the before
  validator, not left as an extra.
- The edge always sends 1.0.0. Deploy order is the usual one: cloud first. A 0.4.0 cloud answers a
  1.0.0 report with 422, which the uplink logs and retries, never blocking a sale.
- `ice_cloud.fleet_view`'s revenue headline sums cents and formats with `format_cents`.
- **Uploaded UI bundles.** `ice_ui.templating`'s one factory registers filters for both the normal
  and the `SandboxedEnvironment`, so registering `money` there covers bundles too. A bundle from a
  pre-1.0.0 edge (the machine's own installed digest, or one in `ICE_CLOUD_TRUSTED_UI_DIGESTS`)
  still references `credit_escrow`, `price` and `money_in`. The portal's render context therefore
  adds those legacy keys as dollar floats (`cents / 100`) beside the `_cents` keys whenever it
  renders from a report. The aliases are display-only and are removed once no 0.x edge reports
  remain; a test renders a 0.4.0 bundle's `status_fragment.html` against a 1.0.0 report.
- `docs/contracts/edge-cloud/CONTRACT.md` gets the 1.0.0 entry and the upgrade order; schemas
  regenerate.

## 8. Rendering

- `ice_ui.filters.money(cents) -> str` (delegates to `format_cents`), registered in
  `ice_ui.templating`. Every `"%.2f"|format(x)` money site in `packages/ui/src/ice_ui/templates`
  (`health_faults.html`, `product.html`, `products.html`, `reports*.html`,
  `partials/kpi_fragment.html`, `status_fragment.html`, `cash_collection_result.html`,
  `collect_cash_button.html`) becomes `{{ x|money }}`.
  `health_faults.html`'s hard-coded `d.credit_escrow > 0.005` becomes `d.credit_escrow_cents > 0`.
  `reports_method.html`'s share percentage is not money and stays.
- **`/screen` is pinned** byte-identical to `origin/main` (CLAUDE.md), so `partials/screen_body.html`
  does not change: the `/screen` route (`web/routes/screen.py`) keeps passing `escrow` and
  `money_24h` as dollar floats, computed there as `cents / 100`. That float is display-only and
  never re-enters the money path.
- Python formatting (`SaleJournal`, `reports/helpers._fmt`, `reports/compute.py`,
  `report_scheduler._compose_body`, `products/overview.py`, `health/recovery.py`,
  `dispense_cycle.py`, `machine.py`, `vmc.py`, `escrow_ledger.py` log lines) uses `format_cents`.
  `report_scheduler`'s email body keeps its current no-`$` style by formatting `cents / 100` with
  `:.2f` through one helper, so the text is byte-identical.
- CSV (`render_csv`) keeps its column names and dollar values: money columns are written as
  `f"{c // 100}.{c % 100:02d}"`, so a downloaded CSV is unchanged.
- Product form: the `price` input stays dollars (`step="0.01"`, label "Price (USD)"), is received as
  `str = Form(...)`, and parsed by `dollars_to_cents`; more than two decimals, a negative value, or a
  value over `MAX_PRICE_CENTS` is a 400 with the stored value unchanged. The form is pre-filled from
  `price_cents` with `format_cents` minus the `$`.

The Tailwind class set does not change (no new classes), so `app.css` needs no rebuild.

## 9. Testing

- **Contracts:** `dollars_to_cents` (half-even, `2.675` → 268, `"1.10"` → 110, negative/NaN refused),
  `format_cents`; cloud models ingest a 0.4.0 fixture and a 1.0.0 fixture to equal objects; a stored
  0.4.0 snapshot file re-validates.
- **Ratchet:** a new `tests/meta/test_money_types.py` walks every Pydantic model and dataclass in
  `packages/*/src` and fails on a field whose name matches `price|amount|credit|revenue|cash|money|
  refund` (minus an explicit allowlist: averages, shares, and the legacy payment models until spec B
  deletes them) typed `float`.
- **Config:** a legacy `price` loads as `price_cents` with the warning; both keys present; over-cap
  load kept with a warning; save writes only `price_cents`.
- **Storage:** migration 2 on a fixture database with REAL prices (including `0.1 + 0.2` residue
  values) yields the expected integers in `sales`, `methods`, `cash_collections` and money `events`;
  non-money `events.value` rows are untouched; a legacy journal row and a legacy `session.json`
  convert on read; a new row carries `"units": "cents"`.
- **Ledger and FSM:** existing `tests/edge/sale` and `tests/edge/ledger` suites pass with integer
  literals; the float-residue tests become exact-equality tests.
- **Rendering parity:** every test asserting a formatted dollar string (11 files) passes unchanged,
  which is the behaviour-preservation check. `tests/cloud/test_ui_parity.py` passes.
- **Product form:** `"2.5"` stores 250 cents; `"2.505"`, `"-1"` and an over-cap value each answer
  HTTP 400 with the stored price unchanged.
- About 34 test files change float literals to integers; this is mechanical and lands with the
  module it tests, not in one sweep.

## 10. Out of scope

Payment behaviour (spec B), multi-currency (the currency stays implicit USD; `payment/state` declares
it), price display localisation.
