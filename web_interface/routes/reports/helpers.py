"""Range, bucket and formatting helpers shared by the Reports sub-modules; no routes."""

from datetime import datetime

from services import reports as reports_service
from web_interface import context

# Bucket defaults per range preset (design §3's "By period" row): day for
# 7d/30d, week for 90d, month for 12m/all.
_BUCKET_DEFAULTS = {
    "7d": "day",
    "30d": "day",
    "90d": "week",
    "12m": "month",
    "all": "month",
}
_VALID_BUCKETS = ("day", "week", "month")

PERIOD_HEADER = [
    "bucket_start",
    "revenue",
    "vends",
    "failed_vends",
    "refunds",
    "uptime_pct",
]
PRODUCT_HEADER = ["sku", "name", "units", "revenue", "failed_vends"]
METHOD_HEADER = ["method", "amount", "count", "share", "is_cash"]
COLLECTIONS_HEADER = ["ts", "user_id", "user_name", "expected_cash"]


def _effective_range(range_key: str) -> str:
    """The range key actually used: `range_key` itself when it is one of
    the known presets, else the same 30d fallback `resolve_window` applies
    -- computed here too so the *displayed* range tab and the bucket
    default agree with what `resolve_window` will actually query, and an
    unknown `?range=` never 422s (it falls back, exactly like `period` on
    the pre-existing `/reports` above)."""
    return (
        range_key
        if range_key in reports_service.WINDOW_PRESETS
        else reports_service.DEFAULT_WINDOW
    )


def _default_bucket(range_key: str) -> str:
    return _BUCKET_DEFAULTS.get(range_key, "day")


def resolve_bucket(range_key: str, bucket: str | None) -> str:
    """An unrecognized or absent bucket falls back to the range's default,
    never a 422 -- the same fallback shape as `_effective_range`.

    `range="all"` + `bucket="day"` is refused server-side too, not merely
    hidden in the UI (reports_period.html disables that tab, but a typed
    or bookmarked URL bypasses a UI-only guard): `by_period` walks one
    bucket -- and, per its own docstring, several SQL queries -- per real
    day from the epoch to now, tens of thousands of rows and well over
    100,000 queries on a long-lived machine, blocking the report worker
    and hammering the SD-card database for one request. Falls back to the
    range's own default bucket (month for "all"), the same shape an
    unrecognized bucket already gets, rather than a 422 -- a stale or
    hand-edited query string must never break the page.
    """
    if bucket in _VALID_BUCKETS and not (range_key == "all" and bucket == "day"):
        return bucket
    return _default_bucket(range_key)


def _find_product(sku: str):
    return next((p for p in context.config.products if p.sku == sku), None)


def _ts_display(ts: float) -> str:
    """A human timestamp for one epoch float -- computed in the route, not
    the template, matching routes/inventory.py's own
    datetime.fromtimestamp(...).strftime(...) convention."""
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


def _fmt(value, *, money: bool = False) -> str:
    """Plain-text rendering of a possibly-`None` event-derived value: `—`
    for `None` (never 0 -- see the module docstring and design §5), `$x.xx`
    for a money value, else the bare value."""
    if value is None:
        return "—"
    return f"${value:.2f}" if money else str(value)


def _totalize_period(rows: list[dict]) -> dict:
    """The total row `/reports/period` adds per design §3's table.

    Revenue and vends are always known (from `sales`, never pruned) and
    summed directly. The three event-derived columns follow the same
    None-means-unknown rule as each row: summed only across rows where that
    column is not None, and None (not 0) when *no* row in the window has a
    value -- a plain 0 would misreport a fully-out-of-retention window as
    fault-free, the exact misreading design §5 calls out for a single row,
    and a total row is no exception.
    """
    revenue = round(sum(r["revenue"] for r in rows), 2)
    vends = sum(r["vends"] for r in rows)
    failed = [r["failed_vends"] for r in rows if r["failed_vends"] is not None]
    refunds = [r["refunds"] for r in rows if r["refunds"] is not None]
    uptime = [r["uptime_pct"] for r in rows if r["uptime_pct"] is not None]
    return {
        "revenue": revenue,
        "vends": vends,
        "failed_vends": sum(failed) if failed else None,
        "refunds": round(sum(refunds), 2) if refunds else None,
        "uptime_pct": round(sum(uptime) / len(uptime), 1) if uptime else None,
    }
