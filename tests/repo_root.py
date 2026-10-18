"""Repo-root and tests-root locations (pathlib only), importable without
pulling in edge code. `tests/paths.py` re-exports these."""

from __future__ import annotations

from pathlib import Path


def _repo_root() -> Path:
    for parent in Path(__file__).resolve().parents:
        if (parent / "pyproject.toml").is_file() and (parent / "uv.lock").is_file():
            return parent
    raise RuntimeError("repo root not found")


REPO_ROOT = _repo_root()
# The tests/ root; this module stays there when tests move into subdirectories.
TESTS_ROOT = Path(__file__).resolve().parent

__all__ = ["REPO_ROOT", "TESTS_ROOT"]
