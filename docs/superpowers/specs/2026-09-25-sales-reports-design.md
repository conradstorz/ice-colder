# Sales Reports — Design

**Date:** 2026-09-25
**Status:** Approved
**Series:** Dashboard v2, part 3 of 4 (roles → v2 shell → **sales reports** → system tests)
**Depends on:** part 1 (permissions, mailer, `current_user`), part 2 (Reports and Inventory levels, `base.html`)

## Context

Today's recorder stores a dispense as a slot number and a payment as an
amount. Nothing ties a sale to a SKU, a price, or a payment method, and every
row is pruned after 90 days. The Reports tile in v2 needs real sales history.

Decisions made in brainstorming:

- Reports: sales by period, by product, by payment method, and cash
  collection reconciliation.
- Raw sales are kept forever; other events keep the 90-day prune.
- Delivery: on screen, email on demand, and a scheduled summary (off, daily,
  weekly) to the owner plus extra addresses.
- Reports are visible to `view_reports` (owner, secretary).
- Any role may record a cash collection from the Inventory level; only a
  timestamp and who, no counted amount.

## Approach

New `sales` and `cash_collections` tables in the existing `data/events.db`,
inserted through the recorder's writer thread and excluded from pruning. A
new `services/reports.py` holds the queries; `services/report_scheduler.py`
sends scheduled summaries.

Rejected: deriving sales from the existing `dispense` and `payment` rows (no
SKU or method, pruned); a separate `sales.db` (one more file to back up for
no benefit).

## 1. Sale record

### 1.1 Escrow credits in the VMC

`VMC` gains `escrow_credits: list[Credit]` (`Credit(method: str, amount:
float, ts: float)`) alongside `credit_escrow`, which stays the authoritative
total. `deposit_funds` appends a credit. A refund clears the list. When a
price is deducted from escrow, credits are consumed first-in-first-out and
the consumed shares form the method breakdown for that sale: a $2.50 sale
after $2.00 cash then $1.00 card yields `{"cash": 2.00, "card": 0.50}` and
leaves a $0.50 card credit. `vend_failed` restores the price to escrow as a
single credit with the sale's dominant method, which is the closest honest
reconstruction. The session snapshot in `services/session_store.py` gains
the credits list so a restart mid-sale keeps the breakdown.

Method strings come from `PaymentEvent.method` unchanged (`cash`, `coin`,
`card`, or whatever the MDB firmware reports); reports group by the raw
string and the simulator is checked to emit the three canonical values.

### 1.2 Tables

Created by `EventRecorder._init_db`:

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
    expected_cash REAL NOT NULL    -- cash-method sale shares since the previous row
);
```

`record_sale(sku, name, slot, price, methods)` and
`record_cash_collection(user_id, user_name)` enqueue rows on the existing
writer queue. `_prune_with` touches only `events`. The VMC calls
`record_sale` at the same point it records `dispense` today; the `dispense`
event stays for the existing KPIs.

### 1.3 Cash collection

`expected_cash` is computed inside the writer thread at insert time, so it
is consistent with every sale already written: the sum of the `cash` and
`coin` shares of `sales.methods` with `ts` greater than the previous
collection's `ts` (or all time for the first). Which method strings count
as cash is the tuple `CASH_METHODS = ("cash", "coin")` in `services/reports.py`.

New permission `collect_cash` in part 1's `Permission` enum, granted to all
four roles. The action is `POST /inventory/collect` with a two-tap confirm
on the Inventory level; the response shows the recorded time and the
expected amount so the collector can compare with the box.

## 2. Queries (`services/reports.py`)

All functions take the recorder (for the db path) and a `(start_ts, end_ts)`
window, call `flush()` first, and run in a thread via
`asyncio.to_thread` from routes, as `/activity` does today. Bucketing uses
the machine's local timezone (`datetime.astimezone()`), and a `bucket` is
`day`, `week` (Monday start), or `month`.

| Function | Returns |
|---|---|
| `by_period(window, bucket)` | rows of bucket start, revenue, vends, failed vends, refunds, uptime % (uptime and failed/refund counts come from `events` and are only available inside the 90-day retention; older buckets show `—`) |
| `by_product(window)` | rows per SKU: name (latest seen), units, revenue, failed vends |
| `by_method(window)` | rows per method: amount, share of revenue, sale count where the method contributed |
| `collections(limit)` | most recent collections: ts, user, expected cash, and the cash accepted since it (live figure for the newest row) |
| `summary(window)` | totals used by the email: revenue, vends, failed, refunds, per-method split, cash since last collection |

Window presets: `7d`, `30d`, `90d`, `12m`, `all`, selected by a `?range=`
query param; default `30d`.

## 3. Reports level

Extends part 2's `/reports`. The level shows the moved activity table on
top (24 h / 7 d / 30 d, unchanged) and four sub-tiles:

| Level | URL | Body |
|---|---|---|
| By period | `/reports/period` | Range selector plus bucket selector (day for 7d/30d, week for 90d, month for 12m/all); one row per bucket; a total row |
| By product | `/reports/product` | Range selector; rows sorted by revenue; tapping a row opens `/reports/product/{sku}` showing that SKU by period |
| By method | `/reports/method` | Range selector; rows per method with amount and share |
| Cash collections | `/reports/collections` | Last 50 collections newest first; the top row shows "cash since this collection" live |

All gated by `view_reports`. Every page has **Email this report**
(`POST /reports/email` with the page's parameters): the mailer from part 1
sends a plain-text rendering of the table in the body and the same rows as
a CSV attachment named `<machine_id>-<report>-<range>.csv` to the current
user's email, falling back to the owner's if the user has none. The
`send_email` signature gains an optional `attachments: list[(filename,
bytes, mime)]`.

Tables are the one place part 2 allows a real `<table>`; each sits in an
`overflow-x: auto` container. Currency is formatted with the existing
template filters; no charts in this spec.

## 4. Scheduled summary

### 4.1 Config

New `ReportsConfig` section in `ConfigModel` (`config.reports`):

| Field | Default | Meaning |
|---|---|---|
| `schedule` | `"off"` | `off`, `daily`, `weekly` |
| `hour` | `7` | local hour (0–23) to send |
| `weekday` | `0` | Monday-based weekday for weekly |
| `extra_recipients` | `[]` | additional addresses beyond the owner |

Edited at `/settings/reports` (part 2 Settings level, gate `edit_contacts`),
saved through `save_config`.

### 4.2 Scheduler (`services/report_scheduler.py`)

A coroutine `run(config, recorder, mailer, clock)` started in `main.py`
under `_supervise("report scheduler", ...)`. It sleeps until the next due
time computed from the live config (re-read every loop, so a Settings change
takes effect without restart), then emails `summary()` for the previous
completed day or week to the owner plus `extra_recipients`, and records a
`report_sent` event with the period covered. On startup, if the schedule is
on and the last `report_sent` (within the 90-day event window) is older than
one period, it sends one catch-up covering the most recent completed
period, never a backlog. A failed send logs and retries at the next due
time; it never raises out of the loop.

## 5. Error handling

- Sale recording never blocks the FSM: it is a queue put, like every event.
- If `escrow_credits` and `credit_escrow` disagree (should not happen; a
  bug guard), the sale is recorded with `{"unknown": price}` and a warning.
- Report queries on an empty database return empty rows and zero totals.
- Email failures show inline on the page; the scheduler logs them.
- A window older than the events retention shows `—` for event-derived
  columns rather than zero, so old months are not misread as fault-free.

## 6. Testing

- `tests/test_vmc.py` additions: FIFO allocation across two methods,
  leftover credit keeps its method, refund clears credits, `vend_failed`
  restores one credit, snapshot round-trips credits.
- `tests/test_event_recorder.py` additions: `sales` rows survive `prune`,
  `record_sale` JSON shape, `expected_cash` for first and subsequent
  collections including a coin share.
- `tests/test_reports.py`: seeded sales across a day, week, and month
  boundary in a fixed timezone; by_product ordering; by_method shares sum to
  revenue; `—` for buckets outside retention.
- `tests/test_report_scheduler.py`: injected clock, next-due computation for
  daily and weekly, catch-up-once on startup, no send when off, failure does
  not stop the loop.
- Route tests: each report page 200 for owner and secretary, 403 for tech
  and loader; `/inventory/collect` 200 for loader and records a row; email
  action calls the stubbed mailer with a CSV attachment; `/settings/reports`
  round-trips.

## 7. Files

| File | Change |
|---|---|
| `controller/vmc.py` | `escrow_credits`, FIFO deduction, `record_sale` call |
| `services/session_store.py` | Snapshot carries credits |
| `services/event_recorder.py` | Two tables, `record_sale`, `record_cash_collection`, prune scope |
| `services/reports.py` | New: queries and CSV rendering |
| `services/report_scheduler.py` | New |
| `services/mailer.py` | Attachments |
| `services/access.py` | `collect_cash` permission |
| `config/config_model.py` | `ReportsConfig` |
| `main.py` | Start the scheduler |
| `web_interface/routes/reports.py`, `inventory.py`, `settings.py` | New levels and actions |
| `web_interface/templates/reports_*.html`, `settings_reports.html` | New |
| `tests/test_reports.py`, `tests/test_report_scheduler.py`, additions to existing test files | New and extended |
| `CLAUDE.md` | Document the sales tables and the scheduler |

## 8. Out of scope

- Charts (the dataviz treatment can come once the tables exist).
- Counted-cash entry and variance.
- Exporting to accounting systems; CSV by email is the export.
- Tax, multi-currency, or per-location aggregation.
- Backfilling sales from pre-existing `dispense` events.
