"""Every location the test suite needs, derived from module __file__s."""

from __future__ import annotations

from pathlib import Path

import controller.vmc
import web_interface.routes
from ice_ui.templating import STATIC_DIR, TEMPLATES_DIR


def _repo_root() -> Path:
    for parent in Path(__file__).resolve().parents:
        if (parent / "pyproject.toml").is_file() and (parent / "uv.lock").is_file():
            return parent
    raise RuntimeError("repo root not found")


REPO_ROOT = _repo_root()
# The tests/ root; this module stays there when tests move into subdirectories.
TESTS_ROOT = Path(__file__).resolve().parent
ROUTES_DIR = Path(web_interface.routes.__file__).parent
ROUTES_PACKAGE = web_interface.routes.__name__
VMC_PATH = Path(controller.vmc.__file__)

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
