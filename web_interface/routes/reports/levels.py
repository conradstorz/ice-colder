"""The five report levels: /reports/period, /reports/product, /reports/product/{sku}, /reports/method and /reports/collections."""

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse

from services import reports as reports_service
from services.access import Permission
from web_interface import auth as web_auth
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
    bucket_key = resolve_bucket(range_key, bucket)
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
    # `{sku:path}` (not plain `{sku}`) -- see sku_url_segment's
    # docstring (web_interface/filters.py): a `str`-converter route
    # 404s on a percent-encoded `/` (`%2F`) since the ASGI server
    # decodes it before Starlette's router sees it, so a SKU
    # containing `/` needs the `path` converter to route at all.
    "/reports/product/{sku:path}",
    response_class=HTMLResponse,
    dependencies=[Depends(web_auth.require(Permission.view_reports))],
)
async def reports_product_sku_level(
    request: Request,
    sku: str,
    range: str = Query(default=reports_service.DEFAULT_WINDOW),
    bucket: str | None = Query(default=None),
):
    range_key = _effective_range(range)
    bucket_key = resolve_bucket(range_key, bucket)
    data = await _compute_product_sku(range_key, bucket_key, sku)
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
