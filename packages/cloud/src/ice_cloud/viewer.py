"""The remote viewer: ``current_user`` for pages the cloud renders from ``ice_ui``."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RemoteViewer:
    """``current_user`` for the remote viewer; ``base.html`` reads ``name`` and
    ``role_label``, ``me.html`` also ``email``."""

    display_name: str = "Fleet viewer"
    email: str | None = None
    role_label: str | None = None

    @property
    def name(self) -> str:
        return self.display_name


VIEWER = RemoteViewer()
