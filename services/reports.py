# services/reports.py
"""Sales report queries: windows, bucketing, per-product/method breakdowns,
cash collections, and CSV rendering for the dashboard's Reports level.

See ``docs/superpowers/specs/2026-09-25-sales-reports-design.md`` §1.3 and
§2 for the authoritative spec this module implements.

Design notes / deliberate deviations (recorded here per the task brief):

- ``is_cash`` lives in ``services/event_recorder.py`` (it is needed there by
  ``_insert_cash_collection`` before this module existed) and is re-exported
  from here under the same name so callers have one obvious home for report
  code. This module imports from ``event_recorder``, never the reverse, so
  there is no import cycle. This arrangement was directed by the task brief,
  not chosen independently.
- There is no public accessor for the recorder's database path. Every
  function below reads ``recorder._db_path`` **at call time** rather than
  caching it, because ``EventRecorder._quarantine_corrupt_db`` can reassign
  ``self._db_path`` mid-process when it falls back to a ``.new-<timestamp>``
  file after a corruption recovery -- a cached path would silently keep
  reading the wrong (possibly deleted, possibly stale) database for the rest
  of the process's life. Reading this private attribute across modules in
  the same package is a deliberate, directed deviation; ``event_recorder.py``
  is outside this task's permitted files, so no property was added there.
"""

import contextlib
import csv
import io
import json
import sqlite3
import time
from datetime import datetime, timedelta, timezone, tzinfo
from typing import Optional

from services.event_recorder import _HEARTBEAT_INTERVAL, is_cash

__all__ = [
    "is_cash",
    "WINDOW_PRESETS",
    "DEFAULT_WINDOW",
    "resolve_window",
    "by_period",
    "by_product",
    "by_method",
    "collections",
    "summary",
    "render_csv",
    "report_filename",
]

_DAY = 86400.0

# Must agree with EventRecorder's default retention_days=90 (see design
# §2/§1.3): the `events` table is pruned to this window, but `sales` and
# `cash_collections` are never pruned. This constant is not read off the
# recorder because, unlike `_db_path`, it never changes after construction
# and the brief only sanctions reading `_db_path` across modules -- so it is
# simply kept in agreement with the documented default here.
RETENTION_DAYS = 90

# Window presets selected by the dashboard's `?range=` query param: each
# maps to a duration (seconds) subtracted from `now`, or None for "all
# time" (from the epoch). Resolved via `resolve_window`, which takes an
# injectable `now` so tests are deterministic.
WINDOW_PRESETS: dict[str, Optional[float]] = {
    "7d": 7 * _DAY,
    "30d": 30 * _DAY,
    "90d": 90 * _DAY,
    "12m": 365 * _DAY,
    "all": None,
}
DEFAULT_WINDOW = "30d"


def resolve_window(range_key: str, now: Optional[float] = None) -> tuple[float, float]:
    """Resolve a `?range=` preset key to a (start_ts, end_ts) window.

    `now` is injectable (defaults to the real wall clock via `time.time()`)
    so callers -- and tests -- can pin the reference instant. An unknown
    `range_key` falls back to `DEFAULT_WINDOW` ("30d") rather than raising,
    since a bad or stale query param must never break the page.
    """
    if now is None:
        now = time.time()
    duration = WINDOW_PRESETS.get(range_key, WINDOW_PRESETS[DEFAULT_WINDOW])
    start = 0.0 if duration is None else now - duration
    return (start, now)


# --------------------------------------------------------------------------
# Local-timezone bucketing
# --------------------------------------------------------------------------


def _to_local(ts: float, tz: Optional[tzinfo]) -> datetime:
    """Convert an epoch timestamp to an aware local datetime.

    Built from a UTC-aware datetime and then `.astimezone(tz)`'d, per the
    design's "bucketing uses the machine's local timezone via
    `datetime.astimezone()`". `tz=None` means "the system's local timezone"
    (the standard meaning of `astimezone(None)`); tests pass a fixed tzinfo
    instead so bucket assignment is deterministic regardless of the machine
    that runs them.
    """
    return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(tz)


def _floor_to_bucket(dt: datetime, bucket: str) -> datetime:
    """Floor an aware local datetime to the start of its day/week/month.

    A timestamp exactly on a boundary (e.g. midnight) floors to itself,
    which is what makes it belong to the *later* bucket: the earlier
    bucket's range is `[prior_start, this_start)`, a half-open interval
    that excludes a value equal to `this_start`.
    """
    day_start = dt.replace(hour=0, minute=0, second=0, microsecond=0)
    if bucket == "day":
        return day_start
    if bucket == "week":
        # Monday-start week: datetime.weekday() is Monday=0 .. Sunday=6.
        return day_start - timedelta(days=day_start.weekday())
    if bucket == "month":
        return day_start.replace(day=1)
    raise ValueError(f"unknown bucket: {bucket!r}")


def _next_bucket(dt: datetime, bucket: str) -> datetime:
    """Return the start of the bucket immediately following `dt`."""
    if bucket == "day":
        return dt + timedelta(days=1)
    if bucket == "week":
        return dt + timedelta(days=7)
    if bucket == "month":
        if dt.month == 12:
            return dt.replace(year=dt.year + 1, month=1)
        return dt.replace(month=dt.month + 1)
    raise ValueError(f"unknown bucket: {bucket!r}")


def _cash_since(conn: sqlite3.Connection, since_ts: Optional[float]) -> float:
    """Sum the cash-class shares of `sales.methods` with ts > since_ts.

    `since_ts=None` means "all time" (the first collection ever). Mirrors
    `EventRecorder._insert_cash_collection`'s own computation so a fresh
    collection and this "live" read agree.
    """
    if since_ts is None:
        cursor = conn.execute("SELECT methods FROM sales")
    else:
        cursor = conn.execute("SELECT methods FROM sales WHERE ts > ?", (since_ts,))
    total = 0.0
    for (methods_json,) in cursor.fetchall():
        methods = json.loads(methods_json)
        for method, amount in methods.items():
            if is_cash(method):
                total += amount
    return total


def by_period(
    recorder,
    window: tuple[float, float],
    bucket: str,
    tz: Optional[tzinfo] = None,
) -> list[dict]:
    """Rows of bucket start, revenue, vends, failed vends, refunds, uptime %.

    Revenue and vends come from `sales` (never pruned) and are always
    correct, however old the bucket. Failed vends, refunds and uptime come
    from `events` (90-day retention, see `RETENTION_DAYS`): for a bucket
    whose entire range predates the retention cutoff, those three columns
    are `None` (rendered `—` by the template) rather than 0 -- a 0 would
    misreport "no failures" when the truth is "we no longer know".

    For a bucket that *straddles* the cutoff (its start predates it but its
    end does not), the three event-derived columns are computed normally
    over the bucket's full range, not withheld -- but rows older than the
    cutoff have already been deleted by pruning, so those numbers cover only
    the retained (newer) portion of the bucket while looking like a
    complete count. This is a deliberate choice (returning `None` for a
    partially-covered bucket would discard otherwise-usable data) and is
    pinned by a test; it is not a bug.

    Bucket boundaries are derived from the local wall clock at each
    boundary's own instant (via `_to_local`), not by adding a fixed
    `timedelta` to a stale offset -- so a bucket always starts and ends at
    true local midnight (or week/month start) even across a DST
    transition, and a day that is locally 23 or 25 hours long is queried
    over that true span rather than a naive 24.
    """
    recorder.flush()
    start_ts, end_ts = window
    if start_ts >= end_ts:
        return []
    # Read at call time -- see module docstring: _db_path can be reassigned
    # mid-process by corrupt-database recovery, so it must never be cached.
    db_path = recorder._db_path

    now = time.time()
    retention_cutoff = now - RETENTION_DAYS * _DAY

    current = _floor_to_bucket(_to_local(start_ts, tz), bucket)
    end_local = _to_local(end_ts, tz)

    rows: list[dict] = []
    with contextlib.closing(sqlite3.connect(db_path)) as conn:
        while current < end_local:
            # Re-derive the next boundary from its own epoch instant rather
            # than carrying `current`'s tzinfo snapshot forward: `tz=None`
            # (the production default) resolves to a *fixed*-offset
            # snapshot for one instant (see `_to_local`), so advancing by
            # `timedelta` alone and reusing that same offset would silently
            # merge or split a day across a DST transition. Re-deriving on
            # every iteration, then re-flooring, lands exactly on true
            # local midnight/week/month-start regardless.
            unfloored_next = _next_bucket(current, bucket)
            next_start = _floor_to_bucket(
                _to_local(unfloored_next.timestamp(), tz), bucket
            )
            if next_start <= current:
                # Pathological non-advance guard (e.g. a degenerate tzinfo):
                # force progress with the unfloored boundary so the loop
                # cannot spin forever.
                next_start = unfloored_next

            q_start = max(current.timestamp(), start_ts)
            # `q_end` MUST come from the same re-floored `next_start` used
            # to seed the next iteration's `current` -- not from
            # `unfloored_next` -- or the two buckets would overlap by the
            # DST offset and double-count the sales in that overlap.
            q_end = min(next_start.timestamp(), end_ts)

            revenue = conn.execute(
                "SELECT COALESCE(SUM(price), 0.0) FROM sales WHERE ts>=? AND ts<?",
                (q_start, q_end),
            ).fetchone()[0]
            vends = conn.execute(
                "SELECT COUNT(*) FROM sales WHERE ts>=? AND ts<?",
                (q_start, q_end),
            ).fetchone()[0]

            if q_end <= retention_cutoff:
                failed_vends = None
                refunds = None
                uptime_pct = None
            else:
                failed_vends = conn.execute(
                    "SELECT COUNT(*) FROM events WHERE event_type='vend_failed' "
                    "AND timestamp>=? AND timestamp<?",
                    (q_start, q_end),
                ).fetchone()[0]
                refunds = round(
                    conn.execute(
                        "SELECT COALESCE(SUM(value), 0.0) FROM events "
                        "WHERE event_type='refund' AND timestamp>=? AND timestamp<?",
                        (q_start, q_end),
                    ).fetchone()[0],
                    2,
                )
                covered_buckets = conn.execute(
                    "SELECT COUNT(DISTINCT CAST(timestamp / ? AS INTEGER)) FROM events "
                    "WHERE event_type='heartbeat' AND timestamp>=? AND timestamp<?",
                    (_HEARTBEAT_INTERVAL, q_start, q_end),
                ).fetchone()[0]
                span = q_end - q_start
                uptime_pct = (
                    min(100.0, covered_buckets * _HEARTBEAT_INTERVAL / span * 100)
                    if span > 0
                    else 0.0
                )

            rows.append(
                {
                    "bucket_start": current.isoformat(),
                    "revenue": round(revenue, 2),
                    "vends": vends,
                    "failed_vends": failed_vends,
                    "refunds": refunds,
                    "uptime_pct": uptime_pct,
                }
            )
            current = next_start
    return rows


def by_product(recorder, window: tuple[float, float]) -> list[dict]:
    """Rows per SKU: name (latest seen), units, revenue, failed vends.

    Sorted by revenue descending. Empty database (or empty window) returns
    an empty list, never a raise. "Latest seen" is ordered `ts ASC, id ASC`
    -- the `id ASC` tie-break makes the later-inserted row win deterministic
    even when two rows share an identical `ts` (finite `time.time()`
    resolution makes that a real possibility), rather than falling back to
    SQLite's incidental, unguaranteed row-scan order.
    """
    recorder.flush()
    start_ts, end_ts = window
    db_path = recorder._db_path

    entries: dict[str, dict] = {}
    with contextlib.closing(sqlite3.connect(db_path)) as conn:
        sale_rows = conn.execute(
            "SELECT sku, name, price FROM sales WHERE ts>=? AND ts<? "
            "ORDER BY ts ASC, id ASC",
            (start_ts, end_ts),
        ).fetchall()
        for sku, name, price in sale_rows:
            entry = entries.setdefault(
                sku,
                {
                    "sku": sku,
                    "name": name,
                    "units": 0,
                    "revenue": 0.0,
                    "failed_vends": 0,
                },
            )
            # Rows are visited oldest-first, so the last write for a sku is
            # the latest-seen name (covers a mid-window rename).
            entry["name"] = name
            entry["units"] += 1
            entry["revenue"] += price

        failed_rows = conn.execute(
            "SELECT metadata FROM events WHERE event_type='vend_failed' "
            "AND timestamp>=? AND timestamp<?",
            (start_ts, end_ts),
        ).fetchall()
        for (meta_json,) in failed_rows:
            if not meta_json:
                continue
            meta = json.loads(meta_json)
            sku = meta.get("sku")
            if sku is None:
                continue
            if sku in entries:
                entries[sku]["failed_vends"] += 1
            else:
                # A sku that failed but never completed a sale in this
                # window still deserves a row.
                entries[sku] = {
                    "sku": sku,
                    "name": sku,
                    "units": 0,
                    "revenue": 0.0,
                    "failed_vends": 1,
                }

    rows_out = list(entries.values())
    for row in rows_out:
        row["revenue"] = round(row["revenue"], 2)
    rows_out.sort(key=lambda r: r["revenue"], reverse=True)
    return rows_out


def by_method(recorder, window: tuple[float, float]) -> list[dict]:
    """Rows per raw method string: amount, share of revenue, sale count,
    and a cash/other class from `is_cash`.

    Methods are grouped by their raw stored string -- `cash_coin` and
    `cash_bill` are separate rows that both classify as cash, never merged.
    """
    recorder.flush()
    start_ts, end_ts = window
    db_path = recorder._db_path

    totals: dict[str, dict] = {}
    total_revenue = 0.0
    with contextlib.closing(sqlite3.connect(db_path)) as conn:
        rows = conn.execute(
            "SELECT price, methods FROM sales WHERE ts>=? AND ts<?",
            (start_ts, end_ts),
        ).fetchall()
    for price, methods_json in rows:
        total_revenue += price
        methods = json.loads(methods_json)
        for method, amount in methods.items():
            entry = totals.setdefault(
                method,
                {
                    "method": method,
                    "amount": 0.0,
                    "count": 0,
                    "is_cash": is_cash(method),
                },
            )
            entry["amount"] += amount
            entry["count"] += 1

    result = list(totals.values())
    for entry in result:
        entry["amount"] = round(entry["amount"], 2)
        entry["share"] = (entry["amount"] / total_revenue) if total_revenue else 0.0
    result.sort(key=lambda r: r["amount"], reverse=True)
    return result


def collections(recorder, limit: int = 50) -> list[dict]:
    """Most recent cash collections: ts, user, expected cash.

    The newest row's expected-cash figure is computed live (cash-class
    sales since that collection's ts, as of right now) rather than read
    from the stored column, since sales keep arriving after the last
    collection was recorded. Older rows show the stored `expected_cash`
    exactly as `record_cash_collection` computed it at insert time.
    """
    recorder.flush()
    db_path = recorder._db_path

    result: list[dict] = []
    with contextlib.closing(sqlite3.connect(db_path)) as conn:
        rows = conn.execute(
            "SELECT ts, user_id, user_name, expected_cash FROM cash_collections "
            "ORDER BY ts DESC LIMIT ?",
            (limit,),
        ).fetchall()
        for i, (ts, user_id, user_name, expected_cash) in enumerate(rows):
            if i == 0:
                expected_cash = round(_cash_since(conn, ts), 2)
            result.append(
                {
                    "ts": ts,
                    "user_id": user_id,
                    "user_name": user_name,
                    "expected_cash": expected_cash,
                }
            )
    return result


def summary(recorder, window: tuple[float, float]) -> dict:
    """Totals used by the scheduled email: revenue, vends, failed, refunds,
    per-method split, and cash since the last collection.

    Empty database returns zero totals and an empty `by_method` list, never
    a raise and never a `None` total.
    """
    recorder.flush()
    start_ts, end_ts = window
    db_path = recorder._db_path

    with contextlib.closing(sqlite3.connect(db_path)) as conn:
        revenue = conn.execute(
            "SELECT COALESCE(SUM(price), 0.0) FROM sales WHERE ts>=? AND ts<?",
            (start_ts, end_ts),
        ).fetchone()[0]
        vends = conn.execute(
            "SELECT COUNT(*) FROM sales WHERE ts>=? AND ts<?",
            (start_ts, end_ts),
        ).fetchone()[0]
        failed = conn.execute(
            "SELECT COUNT(*) FROM events WHERE event_type='vend_failed' "
            "AND timestamp>=? AND timestamp<?",
            (start_ts, end_ts),
        ).fetchone()[0]
        refunds = conn.execute(
            "SELECT COALESCE(SUM(value), 0.0) FROM events "
            "WHERE event_type='refund' AND timestamp>=? AND timestamp<?",
            (start_ts, end_ts),
        ).fetchone()[0]

        last_collection = conn.execute(
            "SELECT ts FROM cash_collections ORDER BY ts DESC LIMIT 1"
        ).fetchone()
        since_ts = last_collection[0] if last_collection else None
        cash_since_last_collection = _cash_since(conn, since_ts)

    return {
        "revenue": round(revenue, 2),
        "vends": vends,
        "failed": failed,
        "refunds": round(refunds, 2),
        "by_method": by_method(recorder, window),
        "cash_since_last_collection": round(cash_since_last_collection, 2),
    }


# --------------------------------------------------------------------------
# CSV rendering
# --------------------------------------------------------------------------


def render_csv(rows: list[dict], header: list[str]) -> bytes:
    """Render any of the row lists above (plus its header) as CSV bytes.

    `None` values (the retention `—` columns) render as an empty
    field, matching the `csv` module's own default for `None`. Encoded as
    UTF-8, suitable for an email attachment or a download response body.
    """
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=header, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    return buf.getvalue().encode("utf-8")


def report_filename(machine_id: str, report: str, range_key: str) -> str:
    """Build the `<machine_id>-<report>-<range>.csv` attachment filename."""
    return f"{machine_id}-{report}-{range_key}.csv"
