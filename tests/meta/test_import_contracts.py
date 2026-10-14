"""The import-linter contracts in pyproject.toml hold for the whole repo."""

from __future__ import annotations

import subprocess

from tests.paths import REPO_ROOT


def test_lint_imports_passes():
    result = subprocess.run(
        ["uv", "run", "lint-imports"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
