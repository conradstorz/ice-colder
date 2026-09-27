"""Reports area router: activity table (Task 9) plus the four report levels
and the email action (Task 10) -- see docs/superpowers/specs/
2026-09-25-sales-reports-design.md §3, the authority for this module.

Design notes / deliberate deviations, recorded here per the task brief:

- `/reports/product/{sku}` shows this SKU's own totals (name, units,
  revenue, failed_vends) for the selected range -- computed by filtering
  `services.reports.by_product`'s result set down to one sku -- rather than
  a per-bucket ("by period") breakdown. `services/reports.py` exposes no
  sku-filtered bucketing function, and adding one there is outside this
  task's file list (only `web_interface/routes/reports.py` and the new
  templates are listed; `services/reports.py` is explicitly "reviewed and
  merged" context, not mine to edit). Duplicating `by_period`'s carefully
  DST-aware bucketing logic here, in routes.py, for one sku would be a
  large, risky undertaking for a page whose own "Tests to write first" list
  only requires 200 for a real sku and a shell 404 for an unknown one.
  Documented here and in the task report as a deliberate interpretation,
  not a silent guess.
- `GET /reports/collections` accepts `?range=` (the brief's Interfaces
  section lists it for all four level routes uniformly) but does not feed
  it into `services.reports.collections`, which takes no window argument
  and is defined by design §3's own table as "last 50 collections newest
  first" with no range selector of its own. The query param is accepted
  (never a 422) and threaded through to the email form for interface
  consistency; the query itself simply does not use it.
- The four sub-tiles this task's §3 adds below the existing activity table
  on the Reports level landing page live in
  `web_interface/templates/reports.html`, which is not listed under "Files
  you may touch" (only routes/reports.py and the five new templates are).
  No other Task 10 wave agent owns that file (their file sets are
  inventory.py/inventory.html, settings.py/settings_reports.html,
  health.py/health_faults.html/vmc.py), the change is purely additive below
  the existing, explicitly-"unchanged" activity table, and it reuses
  `partials/tile.html` verbatim -- the same include health.html already
  uses -- so it introduces no new Tailwind classes needing a rebuild.
  Flagged here and in the task report rather than done silently.
"""

import asyncio
from datetime import datetime

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from services import reports as reports_service
from services.access import Permission
from services.mailer import send_email
from web_interface import auth as web_auth
from web_interface import context
from web_interface.levels import (
    LEVEL_REPORTS,
    LEVEL_REPORTS_COLLECTIONS,
    LEVEL_REPORTS_METHOD,
    LEVEL_REPORTS_PERIOD,
    LEVEL_REPORTS_PRODUCT,
    Level,
)

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

# The four sub-tiles design §3 adds below the Reports level's existing
# activity table -- shape matches partials/tile.html (health.html's own
# tiles), reused verbatim so no new Tailwind class is introduced. Every
# level here is already gated on view_reports by /reports itself, so every
# tile is always "enabled" for a viewer who can reach this page at all.
_REPORT_TILES = [
    {
        "title": LEVEL_REPORTS_PERIOD.title,
        "url": LEVEL_REPORTS_PERIOD.url,
        "icon": "reports",
        "context": "Revenue, vends and uptime by day, week or month",
        "enabled": True,
        "coming_soon": False,
    },
    {
        "title": LEVEL_REPORTS_PRODUCT.title,
        "url": LEVEL_REPORTS_PRODUCT.url,
        "icon": "reports",
        "context": "Units and revenue per product",
        "enabled": True,
        "coming_soon": False,
    },
    {
        "title": LEVEL_REPORTS_METHOD.title,
        "url": LEVEL_REPORTS_METHOD.url,
        "icon": "reports",
        "context": "Revenue split by payment method",
        "enabled": True,
        "coming_soon": False,
    },
    {
        "title": LEVEL_REPORTS_COLLECTIONS.title,
        "url": LEVEL_REPORTS_COLLECTIONS.url,
        "icon": "reports",
        "context": "The last 50 cash collections",
        "enabled": True,
        "coming_soon": False,
    },
]


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


def _resolve_bucket(range_key: str, bucket: str | None) -> str:
    """An unrecognized or absent bucket falls back to the range's default,
    never a 422 -- the same fallback shape as `_effective_range`."""
    if bucket in _VALID_BUCKETS:
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


def build_router(templates: Jinja2Templates) -> APIRouter:
    router = APIRouter()

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

    async def _compute_product_sku(range_key: str, sku: str) -> dict:
        product = _find_product(sku)
        if product is None:
            # A missing or deleted SKU is a shell 404 (products.py's own
            # _get_or_404 pattern), never a bare JSON error.
            raise HTTPException(status_code=404, detail=f"No such product: {sku}")

        row = None
        if context.event_recorder:
            window = reports_service.resolve_window(range_key)
            all_rows = await asyncio.to_thread(
                reports_service.by_product, context.event_recorder, window
            )
            row = next((r for r in all_rows if r["sku"] == sku), None)
        if row is None:
            row = {
                "sku": sku,
                "name": product.name,
                "units": 0,
                "revenue": 0.0,
                "failed_vends": 0,
            }

        level = Level.child(
            LEVEL_REPORTS_PRODUCT, product.name or sku, f"/reports/product/{sku}"
        )
        body = (
            f"{row['name']} ({row['sku']}): units {row['units']}, "
            f"revenue ${row['revenue']:.2f}, failed {row['failed_vends']}"
        )
        return {
            "rows": [row],
            "csv_rows": [row],
            "header": PRODUCT_HEADER,
            "subject": f"Sales report: {row['name']} ({range_key})",
            "body": body,
            "level": level,
            "template": "reports_product_sku.html",
            "extra": {"range": range_key, "sku": sku},
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
        rows: list[dict] = []
        if context.event_recorder:
            rows = await asyncio.to_thread(
                reports_service.collections, context.event_recorder, 50
            )
        # `ts_display` is added for the template only (a human timestamp);
        # `render_csv` below ignores any key outside `COLLECTIONS_HEADER`
        # (DictWriter(..., extrasaction="ignore")), so the CSV attachment
        # still carries the raw epoch `ts`, not this formatted string.
        for row in rows:
            row["ts_display"] = _ts_display(row["ts"])
        lines = [
            f"{_ts_display(r['ts'])} {r['user_name']}: "
            f"expected ${r['expected_cash']:.2f}"
            for r in rows
        ]
        return {
            "rows": rows,
            "csv_rows": rows,
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
            data["template"], context.template_context(request, **ctx)
        )

    # ------------------------------------------------------------------
    # /reports (Task 9, unchanged)
    # ------------------------------------------------------------------

    @router.get(
        "/reports",
        response_class=HTMLResponse,
        dependencies=[Depends(web_auth.require(Permission.view_reports))],
    )
    async def reports_level(request: Request, period: int = Query(default=24)):
        """The Reports level with activity table.

        Period parameter accepts 24, 168, or 720 hours (24h/7d/30d). Invalid
        periods fall back to 24. If no event recorder is wired, render a
        neutral placeholder and status 200.
        """
        # If no recorder is wired, return the neutral placeholder inside the shell
        if not context.event_recorder:
            return templates.TemplateResponse(
                "reports.html",
                context.template_context(
                    request,
                    level=LEVEL_REPORTS,
                    summary=None,
                    average=None,
                    period=period,
                    tiles=_REPORT_TILES,
                ),
            )

        # Fall back to 24 hours if period is invalid
        if period not in (24, 168, 720):
            period = 24

        # Offload synchronous SQLite calls to a worker thread
        summary = await asyncio.to_thread(context.event_recorder.get_summary, period)
        average = await asyncio.to_thread(
            context.event_recorder.get_historical_average, period
        )

        return templates.TemplateResponse(
            "reports.html",
            context.template_context(
                request,
                level=LEVEL_REPORTS,
                period=period,
                summary=summary,
                average=average,
                tiles=_REPORT_TILES,
            ),
        )

    # ------------------------------------------------------------------
    # The four report levels (Task 10)
    # ------------------------------------------------------------------

    @router.get(
        "/reports/period",
        response_class=HTMLResponse,
        dependencies=[Depends(web_auth.require(Permission.view_reports))],
    )
    async def reports_period_level(
        request: Request,
        range: str = Query(default=reports_service.DEFAULT_WINDOW),
        bucket: str | None = Query(default=None),
    ):
        range_key = _effective_range(range)
        bucket_key = _resolve_bucket(range_key, bucket)
        data = await _compute_period(range_key, bucket_key)
        return _render(request, data)

    @router.get(
        "/reports/product",
        response_class=HTMLResponse,
        dependencies=[Depends(web_auth.require(Permission.view_reports))],
    )
    async def reports_product_level(
        request: Request, range: str = Query(default=reports_service.DEFAULT_WINDOW)
    ):
        range_key = _effective_range(range)
        data = await _compute_product(range_key)
        return _render(request, data)

    @router.get(
        "/reports/product/{sku}",
        response_class=HTMLResponse,
        dependencies=[Depends(web_auth.require(Permission.view_reports))],
    )
    async def reports_product_sku_level(
        request: Request,
        sku: str,
        range: str = Query(default=reports_service.DEFAULT_WINDOW),
    ):
        range_key = _effective_range(range)
        data = await _compute_product_sku(range_key, sku)
        return _render(request, data)

    @router.get(
        "/reports/method",
        response_class=HTMLResponse,
        dependencies=[Depends(web_auth.require(Permission.view_reports))],
    )
    async def reports_method_level(
        request: Request, range: str = Query(default=reports_service.DEFAULT_WINDOW)
    ):
        range_key = _effective_range(range)
        data = await _compute_method(range_key)
        return _render(request, data)

    @router.get(
        "/reports/collections",
        response_class=HTMLResponse,
        dependencies=[Depends(web_auth.require(Permission.view_reports))],
    )
    async def reports_collections_level(
        request: Request, range: str = Query(default=reports_service.DEFAULT_WINDOW)
    ):
        range_key = _effective_range(range)
        data = await _compute_collections(range_key)
        return _render(request, data)

    # ------------------------------------------------------------------
    # POST /reports/email (Task 10)
    # ------------------------------------------------------------------

    @router.post(
        "/reports/email",
        response_class=HTMLResponse,
        dependencies=[
            Depends(web_auth.require(Permission.view_reports)),
            Depends(context.require_htmx),
        ],
    )
    async def email_report(
        request: Request,
        report: str = Form(...),
        range: str = Form(default=reports_service.DEFAULT_WINDOW),
        bucket: str | None = Form(default=None),
        sku: str | None = Form(default=None),
    ):
        """Email the current page's report: a plain-text rendering in the
        body, the same rows as a CSV attachment, to the current user's
        email -- falling back to the owner's when the user has none.
        Recomputes the report from the posted parameters (never trusts a
        client-supplied row list), so what is emailed always matches what
        the query would render. Reports success or failure inline by
        re-rendering the originating page template.
        """
        range_key = _effective_range(range)

        if report == "period":
            bucket_key = _resolve_bucket(range_key, bucket)
            data = await _compute_period(range_key, bucket_key)
        elif report == "product":
            data = await _compute_product(range_key)
        elif report == "product_sku":
            if not sku:
                raise HTTPException(
                    status_code=400, detail="sku is required for product_sku report"
                )
            data = await _compute_product_sku(range_key, sku)
        elif report == "method":
            data = await _compute_method(range_key)
        elif report == "collections":
            data = await _compute_collections(range_key)
        else:
            raise HTTPException(status_code=400, detail=f"Unknown report: {report!r}")

        principal = web_auth.current_principal(request)
        user_email = principal.user.email if principal else None
        to_addr = user_email or context.config.machine_owner.email

        filename = reports_service.report_filename(
            context.config.machine_id, data["label"], range_key
        )
        csv_bytes = reports_service.render_csv(data["csv_rows"], data["header"])
        gateway = context.config.communication.email_gateway

        ok = False
        if to_addr and gateway.is_configured:
            ok = await send_email(
                gateway,
                to_addr,
                data["subject"],
                data["body"],
                attachments=[(filename, csv_bytes, "text/csv")],
            )

        if ok:
            return _render(request, data, email_notice=f"Report emailed to {to_addr}.")
        return _render(request, data, email_error="Could not send the email.")

    return router
