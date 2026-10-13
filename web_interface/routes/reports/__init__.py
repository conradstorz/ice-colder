"""Reports area router: activity table (Task 9) plus the four report levels
and the email action (Task 10) -- see docs/superpowers/specs/
2026-09-25-sales-reports-design.md §3, the authority for this module.

Design notes / deliberate deviations, recorded here per the task brief:

- `/reports/product/{sku}` shows this SKU's sales **by period** (Task 10
  follow-up, Finding 1): one row per bucket via `services.reports.by_period`
  with its `sku=` filter, the same day/week/month bucketing and range/bucket
  tabs as `/reports/period`, plus a total row. The event-derived columns
  `refunds` and `uptime_pct` are always `None` (rendered `—`) on this page,
  never a real number: a `refund` event's metadata carries no SKU (a refund
  pays back a customer's whole escrow, not one product) and uptime is an
  inherently machine-wide liveness measure, so showing either here would
  misattribute a machine-wide figure to one product. `failed_vends` IS
  attributable per SKU (`vend_failed`'s metadata carries one, see
  `VMC.on_vend_failed`) and so is filtered and shown. See
  `services/reports.py::by_period`'s docstring for the full reasoning.
  The catalog lookup (`_find_product`) still runs, and 404s, before any
  query -- a real SKU with zero sales in the window still renders 200 with
  an empty/zero table, never a 404 -- and the breadcrumb still uses the
  catalog's product name, never a name derived from query rows (which do
  not carry one).
- `GET /reports/collections` accepts `?range=` (the brief's Interfaces
  section lists it for all four level routes uniformly) but does not feed
  it into `services.reports.collections`, which takes no window argument
  and is defined by design §3's own table as "last 50 collections newest
  first" with no range selector of its own. The query param is accepted
  (never a 422) and threaded through to the email form for interface
  consistency; the query itself simply does not use it.
- The four sub-tiles this task's §3 adds below the existing activity table
  on the Reports level landing page live in
  `packages/ui/src/ice_ui/templates/reports.html`, which is not listed under "Files
  you may touch" (only routes/reports.py and the five new templates are).
  No other Task 10 wave agent owns that file (their file sets are
  inventory.py/inventory.html, settings.py/settings_reports.html,
  health.py/health_faults.html/vmc.py), the change is purely additive below
  the existing, explicitly-"unchanged" activity table, and it reuses
  `partials/tile.html` verbatim -- the same include health.html already
  uses -- so it introduces no new Tailwind classes needing a rebuild.
  Flagged here and in the task report rather than done silently.
"""

from fastapi import APIRouter

from . import email, levels, overview
from .helpers import resolve_bucket

__all__ = ["resolve_bucket", "router"]

router = APIRouter()
# No sub-router's parametrised path shadows another's static one
# (GET /reports/product/{sku:path} only matches under /reports/product/),
# so the original file order is kept.
router.include_router(overview.router)
router.include_router(levels.router)
router.include_router(email.router)
