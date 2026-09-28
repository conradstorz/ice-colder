"""Jinja filters for the dashboard."""

import math
from urllib.parse import quote


def sku_url_segment(sku: str) -> str:
    """Percent-encode `sku` for safe use in a URL path, matching the
    `{sku:path}` converter on the routes that take one.

    `Product.sku` is an arbitrary catalog string with no character
    restrictions. A raw `?`, `#`, space, or `&` in the SKU would start a
    query string, start a fragment, or otherwise be misparsed by the
    *client* before the request is even sent -- true regardless of how the
    server matches its route. `quote(sku)` (default `safe="/"`) encodes
    all of those.

    `/` is deliberately left un-encoded rather than turned into `%2F`:
    verified against this project's FastAPI/Starlette version, a `%2F` in
    a request path is rejected by a plain `{sku}` (`str`-converter) route
    with a 404 -- ASGI servers decode it before Starlette's router ever
    sees it, so there is no working percent-encoding for `/` against a
    `str` converter. The routes that take a SKU therefore use
    `{sku:path}`, which matches a literal `/` (and everything else) as
    part of the captured value; Jinja's own `urlencode` filter also
    leaves `/` unescaped, for the same reason (it targets a whole URL,
    not one opaque segment) -- `quote`'s default matches that here, not a
    coincidence.

    This is the one encoding mechanism for a SKU used in a URL across the
    dashboard -- every such link (a report table row, a per-SKU page's
    range/bucket tabs, `Level.child`'s breadcrumb/Back URL) must go
    through this same function, and every route that takes a SKU must use
    `{sku:path}`, so they can never disagree with each other.
    """
    return quote(sku)


def humanize_seconds(value) -> str:
    """45s, 12m, 3h 03m, 2d 5h; '—' for None/inf/negative."""
    if value is None:
        return "—"
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return "—"
    if math.isinf(seconds) or math.isnan(seconds) or seconds < 0:
        return "—"
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    minutes, _ = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h {minutes:02d}m"
    days, hours = divmod(hours, 24)
    return f"{days}d {hours}h"
