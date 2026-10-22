"""Platform admin (identity spec 5.5, fleet dashboard spec): the fleet levels,
unbind machines, suspend accounts.

There is deliberately no route that sets an owner: ownership only ever passes
through a code minted at the machine.
"""

from __future__ import annotations

from fastapi import APIRouter

from ice_cloud.admin import accounts, alerts, machines, overview, owners
from ice_cloud.admin.common import (
    LEVEL_ADMIN,
    LEVEL_ADMIN_ACCOUNTS,
    LEVEL_ADMIN_ALERTS,
    LEVEL_ADMIN_MACHINES,
    LEVEL_ADMIN_OWNERS,
)

router = APIRouter()
router.include_router(overview.router)
router.include_router(machines.router)
router.include_router(owners.router)
router.include_router(accounts.router)
router.include_router(alerts.router)

__all__ = [
    "LEVEL_ADMIN",
    "LEVEL_ADMIN_ACCOUNTS",
    "LEVEL_ADMIN_ALERTS",
    "LEVEL_ADMIN_MACHINES",
    "LEVEL_ADMIN_OWNERS",
    "router",
]
