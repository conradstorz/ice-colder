"""Every location the test suite needs, derived from module __file__s."""

from __future__ import annotations

from pathlib import Path

import ice_edge.sale.vmc
import ice_edge.web.routes
from ice_ui.templating import STATIC_DIR, TEMPLATES_DIR
from tests.repo_root import REPO_ROOT, TESTS_ROOT  # noqa: F401  (re-exported)


ROUTES_DIR = Path(ice_edge.web.routes.__file__).parent
ROUTES_PACKAGE = ice_edge.web.routes.__name__
VMC_PATH = Path(ice_edge.sale.vmc.__file__)

# (current repo-relative path, legacy paths to try at origin/main)
SCREEN_TEMPLATES: list[tuple[str, tuple[str, ...]]] = [
    (
        "packages/ui/src/ice_ui/templates/screen.html",
        ("web_interface/templates/screen.html",),
    ),
    (
        "packages/ui/src/ice_ui/templates/partials/screen_body.html",
        ("web_interface/templates/partials/screen_body.html",),
    ),
]

__all__ = [
    "REPO_ROOT",
    "TESTS_ROOT",
    "TEMPLATES_DIR",
    "STATIC_DIR",
    "ROUTES_DIR",
    "ROUTES_PACKAGE",
    "VMC_PATH",
    "SCREEN_TEMPLATES",
]
