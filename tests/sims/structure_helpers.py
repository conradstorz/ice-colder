"""Shared AST helper for the simulator package's import-direction tests.

Stdlib only: the sims tests must not pull in ``ice_edge`` (see
``tests/meta/test_test_isolation.py``), so nothing here imports the edge code.
"""

import ast
from pathlib import Path


def imported_modules(path: Path) -> set[str]:
    """Every module a file imports, as dotted names.

    ``import a.b`` yields ``a.b``; ``from a import b`` yields ``a`` and
    ``a.b``, so a test can match on either the package or the name pulled in.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
            found.update(f"{node.module}.{alias.name}" for alias in node.names)
    return found
