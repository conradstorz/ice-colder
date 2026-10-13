"""Locate the repository root from this file's own location."""

from __future__ import annotations

from pathlib import Path


def repo_root() -> Path:
    """First ancestor directory holding both pyproject.toml and uv.lock."""
    for parent in Path(__file__).resolve().parents:
        if (parent / "pyproject.toml").is_file() and (parent / "uv.lock").is_file():
            return parent
    raise RuntimeError("repo root not found (no pyproject.toml + uv.lock above)")
