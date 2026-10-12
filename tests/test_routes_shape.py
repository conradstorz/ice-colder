"""Shape guard for the route modules under web_interface/routes/.

The rule: every route module exposes a module-level `router`, and no handler
or helper is nested inside another function. A `build_router(templates)`
closure hides each handler from import, tracebacks and readers alike.

`CONVERTED` lists the modules (basenames, no `.py`) already flattened. Each
conversion task adds its module name here first (the red), then converts the
module (the green). With the set empty both tests are skipped by pytest's
empty parametrisation.
"""

import ast
from pathlib import Path

import pytest

ROUTES_DIR = Path(__file__).parent.parent / "web_interface" / "routes"

CONVERTED: frozenset[str] = frozenset(
    {
        "controls",
        "home",
        "screen",
        "inventory",
        "health",
        "products",
        "reports",
        "settings",
    }
)

_FUNCS = (ast.FunctionDef, ast.AsyncFunctionDef)


def _nested_in(func: ast.AST) -> list[ast.AST]:
    """Functions defined anywhere inside `func`'s body, skipping classes."""
    found: list[ast.AST] = []
    stack = list(func.body)
    while stack:
        node = stack.pop()
        if isinstance(node, ast.ClassDef):
            continue
        if isinstance(node, _FUNCS):
            found.append(node)
        stack.extend(ast.iter_child_nodes(node))
    return found


def _hits(module_name: str) -> tuple[list[str], list[str]]:
    path = ROUTES_DIR / f"{module_name}.py"
    rel = path.relative_to(ROUTES_DIR.parent.parent).as_posix()
    tree = ast.parse(path.read_text(encoding="utf-8"))
    build_router = [
        f"{rel}:{node.lineno}"
        for node in ast.walk(tree)
        if isinstance(node, _FUNCS) and node.name == "build_router"
    ]
    nested: dict[int, str] = {}
    for node in ast.walk(tree):
        # _require_any is a dependency factory: its inner `dependency`
        # closes over *permissions*, so it cannot be hoisted.
        if isinstance(node, _FUNCS) and node.name != "_require_any":
            for inner in _nested_in(node):
                nested[inner.lineno] = f"{rel}:{inner.lineno}"
    return build_router, [nested[k] for k in sorted(nested)]


@pytest.mark.parametrize("module_name", sorted(CONVERTED))
def test_no_build_router(module_name: str) -> None:
    build_router, _ = _hits(module_name)
    assert not build_router, f"{module_name} still defines build_router: {build_router}"


@pytest.mark.parametrize("module_name", sorted(CONVERTED))
def test_no_nested_functions(module_name: str) -> None:
    _, nested = _hits(module_name)
    assert not nested, f"{module_name} nests functions: {nested}"
