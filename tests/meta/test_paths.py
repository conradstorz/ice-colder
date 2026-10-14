"""tests.paths derives every location from module __file__, never from cwd."""

import os
import subprocess
import sys
from pathlib import Path

from tests import paths
from tests.paths import REPO_ROOT, TEMPLATES_DIR, VMC_PATH


def test_every_path_exists():
    for name in ("REPO_ROOT", "TEMPLATES_DIR", "STATIC_DIR", "ROUTES_DIR", "VMC_PATH"):
        assert getattr(paths, name).exists(), name
    for current, _legacy in paths.SCREEN_TEMPLATES:
        assert (REPO_ROOT / current).is_file(), current


def test_templates_and_vmc():
    assert (TEMPLATES_DIR / "base.html").is_file()
    assert VMC_PATH.name == "vmc.py"
    assert isinstance(paths.ROUTES_PACKAGE, str)


def test_cwd_independent(tmp_path: Path):
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from ice_ui.templating import templates; "
            "templates.get_template('base.html')",
        ],
        cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": str(REPO_ROOT)},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
