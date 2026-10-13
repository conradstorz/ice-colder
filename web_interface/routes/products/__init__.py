"""The Products levels: /products, /products/new, /products/{sku},
/products/{sku}/catalog, /products/{sku}/placement, /products/{sku}/copy and
/products/{sku}/delete.

Replaces the old /inventory/* catalog and placement forms (routes/legacy.py),
which keep answering until Task 15 removes them — see
.superpowers/sdd/part2/task-7-brief.md.

The read-only rule this task exists to enforce (brief resolution 4): a
placement-only role (loader, tech) holds edit_placement but not
edit_catalog. They reach GET /products and GET /products/{sku} (gated on
either permission) and see name, price and kind as plain read-only text —
GET /products/{sku} renders no <input> at all — while GET and POST
/products/{sku}/catalog are gated on edit_catalog alone and 403 for them.
"""

from fastapi import APIRouter

from . import catalog, overview, placement

__all__ = ["router"]

router = APIRouter()
# `overview` last: its GET /products/{sku} would otherwise capture
# GET /products/new, which lives in `catalog`.
router.include_router(catalog.router)
router.include_router(placement.router)
router.include_router(overview.router)
