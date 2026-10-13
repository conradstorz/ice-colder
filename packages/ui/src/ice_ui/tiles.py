"""The Home tile table: static UI definition shared by the machine and cloud.

The edge route computes each tile's live context line and passes it in as
``lines`` (keyed by tile title); the cloud supplies the same from a report.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from ice_contracts.access import Permission

from ice_ui.levels import (
    LEVEL_CONTROLS,
    LEVEL_HEALTH,
    LEVEL_INVENTORY,
    LEVEL_PRODUCTS,
    LEVEL_REPORTS,
    LEVEL_REPORTS_COLLECTIONS,
    LEVEL_REPORTS_METHOD,
    LEVEL_REPORTS_PERIOD,
    LEVEL_REPORTS_PRODUCT,
    LEVEL_SETTINGS,
    LEVEL_TESTS,
    LEVEL_USERS,
)


@dataclass(frozen=True)
class TileDef:
    title: str
    url: str
    icon: str
    permissions: frozenset[Permission]
    coming_soon: bool = False


TILES: tuple[TileDef, ...] = (
    TileDef("Health", LEVEL_HEALTH.url, "health", frozenset({Permission.view_status})),
    TileDef(
        "Products",
        LEVEL_PRODUCTS.url,
        "products",
        frozenset({Permission.edit_catalog, Permission.edit_placement}),
    ),
    TileDef(
        "Inventory",
        LEVEL_INVENTORY.url,
        "inventory",
        frozenset({Permission.edit_placement}),
    ),
    TileDef(
        "Reports", LEVEL_REPORTS.url, "reports", frozenset({Permission.view_reports})
    ),
    TileDef(
        "Controls",
        LEVEL_CONTROLS.url,
        "controls",
        frozenset({Permission.machine_controls}),
    ),
    TileDef("Tests", LEVEL_TESTS.url, "tests", frozenset({Permission.run_tests})),
    TileDef("Users", LEVEL_USERS.url, "users", frozenset({Permission.manage_users})),
    TileDef(
        "Settings",
        LEVEL_SETTINGS.url,
        "settings",
        frozenset({Permission.edit_contacts, Permission.edit_secrets}),
    ),
)


def build_tiles(perms: frozenset, lines: Mapping[str, str]) -> list[dict]:
    """Build all eight tile dicts. Every tile is always present; ``enabled``
    (True when *perms* intersects the tile's permission set) is what
    home.html uses to decide whether to render it."""
    return [
        {
            "title": t.title,
            "url": t.url,
            "icon": t.icon,
            "context": lines.get(t.title, ""),
            "enabled": bool(perms & t.permissions),
            "coming_soon": t.coming_soon,
        }
        for t in TILES
    ]


# The four sub-tiles design §3 adds below the Reports level's existing
# activity table -- shape matches partials/tile.html (health.html's own
# tiles), reused verbatim so no new Tailwind class is introduced. Every
# level here is already gated on view_reports by /reports itself, so every
# tile is always "enabled" for a viewer who can reach this page at all.
REPORT_TILES = [
    {
        "title": LEVEL_REPORTS_PERIOD.title,
        "url": LEVEL_REPORTS_PERIOD.url,
        "icon": "reports",
        "context": "Revenue, vends and uptime by day, week or month",
        "enabled": True,
        "coming_soon": False,
    },
    {
        "title": LEVEL_REPORTS_PRODUCT.title,
        "url": LEVEL_REPORTS_PRODUCT.url,
        "icon": "reports",
        "context": "Units and revenue per product",
        "enabled": True,
        "coming_soon": False,
    },
    {
        "title": LEVEL_REPORTS_METHOD.title,
        "url": LEVEL_REPORTS_METHOD.url,
        "icon": "reports",
        "context": "Revenue split by payment method",
        "enabled": True,
        "coming_soon": False,
    },
    {
        "title": LEVEL_REPORTS_COLLECTIONS.title,
        "url": LEVEL_REPORTS_COLLECTIONS.url,
        "icon": "reports",
        "context": "The last 50 cash collections",
        "enabled": True,
        "coming_soon": False,
    },
]
