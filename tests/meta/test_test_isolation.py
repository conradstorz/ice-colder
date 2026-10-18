"""Simulator tests must not name edge code, and edge tests must not name
simulator code -- directly or through a `tests.*` helper they import.

The walk follows import statements only (ast), transitively through
`tests.*` modules. `tests/conftest.py` loading `ice_edge` at run time is out
of scope: the rule is that no simulator test *names* edge code.

Allowlists are shrink-only, like `tests/private_access_budget.py`.
"""

from __future__ import annotations

import ast
from pathlib import Path

from tests.repo_root import REPO_ROOT, TESTS_ROOT

# Shrink-only: remove an entry when its task lands, never add one.
SIMS_ALLOWLIST: set[str] = set()
EDGE_ALLOWLIST = {"tests/edge/web/test_routes_tests.py"}  # until Task 12


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                found.add(node.module)
                found.update(f"{node.module}.{a.name}" for a in node.names)
    return found


def _module_path(module: str) -> Path | None:
    """The file for a `tests.*` module name, or None for anything else."""
    if module != "tests" and not module.startswith("tests."):
        return None
    base = REPO_ROOT / Path(*module.split("."))
    if base.with_suffix(".py").is_file():
        return base.with_suffix(".py")
    if (base / "__init__.py").is_file():
        return base / "__init__.py"
    return None


def _reaches(start: Path, forbidden: str) -> list[str]:
    """Import chain from *start* to a module under *forbidden*, else []."""
    seen: set[Path] = set()
    stack: list[tuple[Path, list[str]]] = [(start, [start.name])]
    while stack:
        path, chain = stack.pop()
        if path in seen:
            continue
        seen.add(path)
        for module in sorted(_imports(path)):
            if module == forbidden or module.startswith(forbidden + "."):
                return [*chain, module]
            target = _module_path(module)
            if target is not None:
                stack.append((target, [*chain, target.name]))
    return []


def _violations(subdir: str, forbidden: str, allowlist: set[str]) -> list[str]:
    bad = []
    for path in sorted((TESTS_ROOT / subdir).rglob("*.py")):
        rel = path.relative_to(REPO_ROOT).as_posix()
        if rel in allowlist:
            continue
        chain = _reaches(path, forbidden)
        if chain:
            bad.append(f"{rel}: {' -> '.join(chain)}")
    return bad


def test_simulator_tests_never_reach_edge_code():
    assert _violations("sims", "ice_edge", SIMS_ALLOWLIST) == []


def test_edge_tests_never_reach_simulator_code():
    assert _violations("edge", "ice_sims", EDGE_ALLOWLIST) == []


def test_allowlist_entries_still_violate():
    """Shrink-only: an entry that no longer violates must be deleted."""
    for rel in sorted(SIMS_ALLOWLIST):
        assert _reaches(REPO_ROOT / rel, "ice_edge"), f"drop {rel} from SIMS_ALLOWLIST"
    for rel in sorted(EDGE_ALLOWLIST):
        assert _reaches(REPO_ROOT / rel, "ice_sims"), f"drop {rel} from EDGE_ALLOWLIST"


def test_allowlists_are_not_grown():
    assert len(SIMS_ALLOWLIST) <= 1
    assert len(EDGE_ALLOWLIST) <= 1


def test_detector_follows_helpers_transitively():
    """tests/fakes.py imports ice_edge, so a sims test importing it is caught."""
    assert _reaches(REPO_ROOT / "tests/fakes.py", "ice_edge")
    assert not _reaches(REPO_ROOT / "tests/async_helpers.py", "ice_edge")
    assert not _reaches(REPO_ROOT / "tests/repo_root.py", "ice_edge")
