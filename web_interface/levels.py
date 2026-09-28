"""The static navigation tree for the dashboard v2 shell.

This is a pure data module: no FastAPI, no Jinja, no `web_interface.context`
import. It defines `Level`, a frozen dataclass describing one node of the
navigation hierarchy (a title, a URL, and a reference to its parent), plus a
module-level constant for each of the 29 named levels in spec §2.

Parameterized levels (a specific product, subsystem or user) have no module
constant here; routes build them at request time with `Level.child`.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Level:
    """One node of the dashboard v2 navigation tree."""

    title: str
    url: str
    parent: "Level | None" = None

    @property
    def crumbs(self) -> list[tuple[str, str]]:
        """Root-first breadcrumb trail, including this level as the last entry."""
        trail: list[tuple[str, str]] = []
        node: Level | None = self
        while node is not None:
            trail.append((node.title, node.url))
            node = node.parent
        trail.reverse()
        return trail

    @property
    def parent_url(self) -> str:
        """The parent's URL, or "/" when this level has no parent."""
        if self.parent is None:
            return "/"
        return self.parent.url

    @classmethod
    def child(cls, parent: "Level", title: str, url: str) -> "Level":
        """Build a parameterized child level (a SKU, a subsystem, a user id)."""
        return cls(title=title, url=url, parent=parent)


# --- The static level tree (spec §2) ---------------------------------------

LEVEL_HOME = Level(title="Home", url="/")

LEVEL_HEALTH = Level(title="Health", url="/health", parent=LEVEL_HOME)
LEVEL_HEALTH_SUBSYSTEMS = Level(
    title="Subsystems", url="/health/subsystems", parent=LEVEL_HEALTH
)
LEVEL_HEALTH_FAULTS = Level(title="Faults", url="/health/faults", parent=LEVEL_HEALTH)
LEVEL_HEALTH_AVAILABILITY = Level(
    title="Availability", url="/health/availability", parent=LEVEL_HEALTH
)
LEVEL_HEALTH_LOGS = Level(title="Logs", url="/health/logs", parent=LEVEL_HEALTH)

LEVEL_PRODUCTS = Level(title="Products", url="/products", parent=LEVEL_HOME)
LEVEL_PRODUCTS_NEW = Level(title="New", url="/products/new", parent=LEVEL_PRODUCTS)

LEVEL_INVENTORY = Level(title="Inventory", url="/inventory", parent=LEVEL_HOME)
LEVEL_REPORTS = Level(title="Reports", url="/reports", parent=LEVEL_HOME)
LEVEL_REPORTS_PERIOD = Level(
    title="By period", url="/reports/period", parent=LEVEL_REPORTS
)
LEVEL_REPORTS_PRODUCT = Level(
    title="By product", url="/reports/product", parent=LEVEL_REPORTS
)
LEVEL_REPORTS_METHOD = Level(
    title="By method", url="/reports/method", parent=LEVEL_REPORTS
)
LEVEL_REPORTS_COLLECTIONS = Level(
    title="Cash collections", url="/reports/collections", parent=LEVEL_REPORTS
)
LEVEL_CONTROLS = Level(title="Controls", url="/controls", parent=LEVEL_HOME)
LEVEL_TESTS = Level(title="Tests", url="/tests", parent=LEVEL_HOME)

LEVEL_USERS = Level(title="Users", url="/users", parent=LEVEL_HOME)
LEVEL_USERS_NEW = Level(title="New", url="/users/new", parent=LEVEL_USERS)
# Devices sits under Users in the navigation tree even though its URL is not
# under /users/ -- the tree is a navigation hierarchy, not a URL-prefix
# hierarchy, and spec §2 lists Devices as a Users sub-level.
LEVEL_DEVICES = Level(title="Devices", url="/devices", parent=LEVEL_USERS)
LEVEL_USERS_CODES = Level(
    title="Emergency codes", url="/users/codes", parent=LEVEL_USERS
)
LEVEL_USERS_OWNERSHIP = Level(
    title="Ownership", url="/users/ownership", parent=LEVEL_USERS
)

LEVEL_SETTINGS = Level(title="Settings", url="/settings", parent=LEVEL_HOME)
LEVEL_SETTINGS_MACHINE = Level(
    title="Machine", url="/settings/machine", parent=LEVEL_SETTINGS
)
LEVEL_SETTINGS_CONTACTS = Level(
    title="Contacts", url="/settings/contacts", parent=LEVEL_SETTINGS
)
LEVEL_SETTINGS_PAYMENTS = Level(
    title="Payments", url="/settings/payments", parent=LEVEL_SETTINGS
)
LEVEL_SETTINGS_COMMS = Level(
    title="Comms", url="/settings/comms", parent=LEVEL_SETTINGS
)
LEVEL_SETTINGS_MQTT = Level(title="MQTT", url="/settings/mqtt", parent=LEVEL_SETTINGS)
LEVEL_SETTINGS_WEB = Level(title="Web", url="/settings/web", parent=LEVEL_SETTINGS)
LEVEL_SETTINGS_REPORTS = Level(
    title="Reports", url="/settings/reports", parent=LEVEL_SETTINGS
)
