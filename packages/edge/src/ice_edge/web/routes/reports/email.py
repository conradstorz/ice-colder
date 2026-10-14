"""The email action: POST /reports/email."""

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse

from ice_edge.reporting import reports as reports_service
from ice_edge.access.access import Permission
from ice_edge.notify.mailer import send_email
from ice_edge.web import auth as web_auth
from ice_edge.web import context
from .compute import (
    _compute_collections,
    _compute_method,
    _compute_period,
    _compute_product,
    _compute_product_sku,
    _render,
)
from .helpers import _effective_range, resolve_bucket

router = APIRouter()

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
        bucket_key = resolve_bucket(range_key, bucket)
        data = await _compute_period(range_key, bucket_key)
    elif report == "product":
        data = await _compute_product(range_key)
    elif report == "product_sku":
        if not sku:
            raise HTTPException(
                status_code=400, detail="sku is required for product_sku report"
            )
        bucket_key = resolve_bucket(range_key, bucket)
        data = await _compute_product_sku(range_key, bucket_key, sku)
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
