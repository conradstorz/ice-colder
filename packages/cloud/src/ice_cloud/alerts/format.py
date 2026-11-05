"""Shared formatting of alert events for the mail text and the history pages."""

from __future__ import annotations

from datetime import UTC, datetime

from ice_cloud.store.repository import AlertEventRow


def as_utc(value: datetime) -> datetime:
    """``value`` in UTC; a naive value is taken to be UTC already."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def fmt_utc(value: datetime) -> str:
    """``2026-10-19 14:02 UTC``."""
    return f"{as_utc(value):%Y-%m-%d %H:%M} UTC"


def event_row(e: AlertEventRow) -> dict[str, str]:
    """The template fields every alert history row shares."""
    return {
        "at": fmt_utc(e.at),
        "kind": e.kind,
        "code": e.code or "",
        "description": e.description or e.key or "",
        "product": e.product or "",
    }
