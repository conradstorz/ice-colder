"""The remote viewer: ``current_user`` for pages the cloud renders from ``ice_ui``."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RemoteViewer:
    """``current_user`` for the remote viewer; ``base.html`` reads no
    attribute of it today (spec section 5)."""

    display_name: str = "Fleet viewer"


VIEWER = RemoteViewer()
