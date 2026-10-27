"""The remote viewer: ``current_user`` for pages the cloud renders from ``ice_ui``, and the two hat-switch links."""

from __future__ import annotations

from dataclasses import dataclass

from ice_cloud.auth import is_admin
from ice_cloud.settings import CloudSettings
from ice_cloud.store.repository import Account


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

# The two values of the ``hat_switch`` template variable (``base.html`` renders
# it as one link in the bar). The hat is the URL: under ``/admin`` the switch
# points back to My machines, everywhere else it offers Admin to an admin.
ADMIN_SWITCH = {"label": "Admin", "url": "/admin"}
MY_MACHINES_SWITCH = {"label": "My machines", "url": "/fleet"}


def admin_switch(settings: CloudSettings, account: Account) -> dict | None:
    """The ``hat_switch`` for a non-``/admin`` page: Admin for an admin, else nothing."""
    return ADMIN_SWITCH if is_admin(settings, account) else None
