"""Shared HTMX CSRF guard for the edge dashboard and the cloud portal.

Cookie auth is replayed by the browser on cross-site requests, so a hostile
page could still POST to a mutating route. HTMX sends ``HX-Request: true`` on
every request it makes; a cross-site form cannot add it, and a cross-origin
fetch with a custom header needs a CORS preflight neither app answers.
"""

from fastapi import HTTPException, Request


def require_htmx(request: Request) -> None:
    """FastAPI dependency: refuse any request without ``HX-Request: true``."""
    if request.headers.get("HX-Request") != "true":
        raise HTTPException(status_code=403, detail="HTMX request required")
