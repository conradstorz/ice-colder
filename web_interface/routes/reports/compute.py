"""The per-report compute builders and the shared renderer; no routes."""

import asyncio

from fastapi import HTTPException, Request

from services import reports as reports_service
from web_interface import context
from web_interface.filters import sku_url_segment
from web_interface.levels import (
    LEVEL_REPORTS_COLLECTIONS,
    LEVEL_REPORTS_METHOD,
    LEVEL_REPORTS_PERIOD,
    LEVEL_REPORTS_PRODUCT,
    Level,
)
from web_interface.templating import templates
from .helpers import (
    COLLECTIONS_HEADER,
    METHOD_HEADER,
    PERIOD_HEADER,
    PRODUCT_HEADER,
    _find_product,
    _fmt,
    _totalize_period,
    _ts_display,
)

# ------------------------------------------------------------------
# Per-report "compute" helpers: each returns a dict shared by the GET
# level route (renders with no email notice) and POST /reports/email
# (renders the same page with a notice/error appended) -- so the two
# can never drift out of sync on what a report actually shows.
# ------------------------------------------------------------------


async def _compute_period(range_key: str, bucket_key: str) -> dict:
    rows: list[dict] = []
    if context.event_recorder:
        window = reports_service.resolve_window(range_key)
        rows = await asyncio.to_thread(
            reports_service.by_period, context.event_recorder, window, bucket_key
        )
    total = _totalize_period(rows)
    csv_rows = [*rows, {"bucket_start": "Total", **total}]

    lines = [
        f"{r['bucket_start']}: revenue {_fmt(r['revenue'], money=True)}, "
        f"vends {r['vends']}, failed {_fmt(r['failed_vends'])}, "
        f"refunds {_fmt(r['refunds'], money=True)}, "
        f"uptime {_fmt(r['uptime_pct'])}"
        for r in rows
    ]
    lines.append(
        f"Total: revenue {_fmt(total['revenue'], money=True)}, "
        f"vends {total['vends']}, failed {_fmt(total['failed_vends'])}, "
        f"refunds {_fmt(total['refunds'], money=True)}, "
        f"uptime {_fmt(total['uptime_pct'])}"
    )

    return {
        "rows": rows,
        "total": total,
        "csv_rows": csv_rows,
        "header": PERIOD_HEADER,
        "subject": f"Sales report: by period ({range_key}, {bucket_key})",
        "body": "\n".join(lines),
        "level": LEVEL_REPORTS_PERIOD,
        "template": "reports_period.html",
        "extra": {"range": range_key, "bucket": bucket_key},
        "label": "period",
    }


async def _compute_product(range_key: str) -> dict:
    rows: list[dict] = []
    if context.event_recorder:
        window = reports_service.resolve_window(range_key)
        rows = await asyncio.to_thread(
            reports_service.by_product, context.event_recorder, window
        )
    lines = [
        f"{r['name']} ({r['sku']}): units {r['units']}, "
        f"revenue ${r['revenue']:.2f}, failed {r['failed_vends']}"
        for r in rows
    ]
    return {
        "rows": rows,
        "csv_rows": rows,
        "header": PRODUCT_HEADER,
        "subject": f"Sales report: by product ({range_key})",
        "body": "\n".join(lines) or "No sales in this range.",
        "level": LEVEL_REPORTS_PRODUCT,
        "template": "reports_product.html",
        "extra": {"range": range_key},
        "label": "product",
    }


async def _compute_product_sku(range_key: str, bucket_key: str, sku: str) -> dict:
    # Catalog lookup runs -- and 404s -- BEFORE any query, same as
    # every other level's shell 404: a real SKU with zero sales in the
    # window must still render 200 with a zero row, never a 404, and
    # this ordering is what guarantees that.
    product = _find_product(sku)
    if product is None:
        raise HTTPException(status_code=404, detail=f"No such product: {sku}")

    rows: list[dict] = []
    if context.event_recorder:
        window = reports_service.resolve_window(range_key)
        rows = await asyncio.to_thread(
            reports_service.by_period,
            context.event_recorder,
            window,
            bucket_key,
            None,
            sku,
        )
    total = _totalize_period(rows)
    csv_rows = [*rows, {"bucket_start": "Total", **total}]

    # The breadcrumb uses the CATALOG's product name -- never a name
    # derived from query rows, which carry no `name` field at all for
    # this by_period shape (and are simply absent for a no-sales SKU).
    # sku_url_segment: `sku` is an arbitrary catalog string, but this
    # becomes the breadcrumb/Back URL for a `{sku}` single-segment
    # route -- see its docstring (web_interface/filters.py). The same
    # function backs the `sku_url_segment` Jinja filter used by
    # reports_product.html and reports_product_sku.html so all three
    # links for a SKU agree.
    level = Level.child(
        LEVEL_REPORTS_PRODUCT,
        product.name or sku,
        f"/reports/product/{sku_url_segment(sku)}",
    )

    lines = [
        f"{r['bucket_start']}: revenue {_fmt(r['revenue'], money=True)}, "
        f"vends {r['vends']}, failed {_fmt(r['failed_vends'])}, "
        f"refunds {_fmt(r['refunds'], money=True)}, "
        f"uptime {_fmt(r['uptime_pct'])}"
        for r in rows
    ]
    lines.append(
        f"Total: revenue {_fmt(total['revenue'], money=True)}, "
        f"vends {total['vends']}, failed {_fmt(total['failed_vends'])}, "
        f"refunds {_fmt(total['refunds'], money=True)}, "
        f"uptime {_fmt(total['uptime_pct'])}"
    )
    body = f"{product.name} ({sku}) by period:\n" + "\n".join(lines)

    return {
        "rows": rows,
        "total": total,
        "csv_rows": csv_rows,
        "header": PERIOD_HEADER,
        "subject": (
            f"Sales report: {product.name} by period ({range_key}, {bucket_key})"
        ),
        "body": body,
        "level": level,
        "template": "reports_product_sku.html",
        "extra": {
            "range": range_key,
            "bucket": bucket_key,
            "sku": sku,
            "product_name": product.name,
        },
        "label": f"product-{sku}",
    }


async def _compute_method(range_key: str) -> dict:
    rows: list[dict] = []
    if context.event_recorder:
        window = reports_service.resolve_window(range_key)
        rows = await asyncio.to_thread(
            reports_service.by_method, context.event_recorder, window
        )
    lines = [
        f"{r['method']}: ${r['amount']:.2f} ({r['share'] * 100:.1f}%), "
        f"{r['count']} sales, {'cash' if r['is_cash'] else 'other'}"
        for r in rows
    ]
    return {
        "rows": rows,
        "csv_rows": rows,
        "header": METHOD_HEADER,
        "subject": f"Sales report: by method ({range_key})",
        "body": "\n".join(lines) or "No sales in this range.",
        "level": LEVEL_REPORTS_METHOD,
        "template": "reports_method.html",
        "extra": {"range": range_key},
        "label": "method",
    }


async def _compute_collections(range_key: str) -> dict:
    raw_rows: list[dict] = []
    if context.event_recorder:
        raw_rows = await asyncio.to_thread(
            reports_service.collections, context.event_recorder, 50
        )
    # `ts_display` is added for the template only (a human timestamp).
    # Unlike the earlier version of this helper, it is added by
    # building new dicts here rather than mutating the service's
    # returned rows in place -- `raw_rows` (what `render_csv` uses)
    # stays exactly what `reports_service.collections` returned, and
    # `render_csv`'s `DictWriter(..., extrasaction="ignore")` would
    # have ignored the extra key either way, so this is a tidiness fix
    # with no behavior change.
    rows = [{**row, "ts_display": _ts_display(row["ts"])} for row in raw_rows]
    lines = [
        f"{_ts_display(r['ts'])} {r['user_name']}: expected ${r['expected_cash']:.2f}"
        for r in raw_rows
    ]
    return {
        "rows": rows,
        "csv_rows": raw_rows,
        "header": COLLECTIONS_HEADER,
        "subject": "Sales report: cash collections",
        "body": "\n".join(lines) or "No collections recorded.",
        "level": LEVEL_REPORTS_COLLECTIONS,
        "template": "reports_collections.html",
        "extra": {"range": range_key},
        "label": "collections",
    }


def _render(request: Request, data: dict, *, email_notice=None, email_error=None):
    ctx = {
        "level": data["level"],
        "rows": data["rows"],
        "email_notice": email_notice,
        "email_error": email_error,
        **data["extra"],
    }
    if "total" in data:
        ctx["total"] = data["total"]
    return templates.TemplateResponse(
        request, data["template"], context.template_context(request, **ctx)
    )
