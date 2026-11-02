"""The template context every cloud-rendered page shares."""

from __future__ import annotations

from types import SimpleNamespace

from fastapi import Request

from ice_contracts.access import REMOTE_VIEWER_PERMISSIONS
from ice_ui.levels import Level


def badge(
    label: str = "Cloud",
    *,
    url: str = "/fleet",
    tone: str = "cloud",
    title: str | None = None,
    external: bool = False,
) -> SimpleNamespace:
    """The bar's context badge; the defaults are the "Cloud" badge."""
    return SimpleNamespace(
        label=label, url=url, tone=tone, title=title, external=external
    )


_make_badge = badge
_CLOUD = object()


def page_context(
    request: Request,
    *,
    viewer: object | None,
    level: Level | None,
    hat_switch: dict | None = None,
    badge: SimpleNamespace | None | object = _CLOUD,
) -> dict:
    """The base context: ``request``, ``current_user``, ``perms``, ``level``,
    ``hat_switch`` and ``badge``. ``badge`` defaults to the "Cloud" badge;
    pass ``None`` for a page that wears none (claim, login)."""
    ctx = {
        "request": request,
        "current_user": viewer,
        "perms": REMOTE_VIEWER_PERMISSIONS,
        "level": level,
        "hat_switch": hat_switch,
    }
    if badge is _CLOUD:
        badge = _make_badge()
    if badge is not None:
        ctx["badge"] = badge
    return ctx
