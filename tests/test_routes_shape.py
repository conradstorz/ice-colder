"""Shape guard for the route modules under web_interface/routes/.

The rule: every route module exposes a module-level `router`, no route
handler is nested inside another function (a nested function counts only if
it carries a decorator), and `build_router` must not exist. A
`build_router(templates)` closure hides each handler from import, tracebacks
and readers alike. Plain undecorated closures are allowed.

The guard is closed: `ROUTE_MODULES` is discovered recursively from every `*.py`
under the routes directory except the top-level `__init__.py`, as dotted names
(`tests_level` for a package's `__init__.py`, `tests_level.commands` for a
sub-module), so a new module is checked the moment it exists.
"""

import ast
import importlib
from pathlib import Path

import pytest
from fastapi import APIRouter

ROUTES_DIR = Path(__file__).parent.parent / "web_interface" / "routes"


def _module_name(path: Path) -> str:
    """Dotted name relative to ROUTES_DIR, suffix dropped, trailing `.__init__` removed."""
    dotted = ".".join(path.relative_to(ROUTES_DIR).with_suffix("").parts)
    return dotted.removesuffix(".__init__")


ROUTE_MODULES: list[str] = sorted(
    _module_name(path)
    for path in ROUTES_DIR.rglob("*.py")
    if path != ROUTES_DIR / "__init__.py" and "__pycache__" not in path.parts
)


def _path_for(module_name: str) -> Path:
    """File behind a dotted module name: `<a>/<b>.py`, else `<a>/<b>/__init__.py`."""
    base = ROUTES_DIR.joinpath(*module_name.split("."))
    module_file = base.with_name(base.name + ".py")
    return module_file if module_file.exists() else base / "__init__.py"


# auth.py is the one module whose router is the unauthenticated `public` one.
ROUTER_ATTR = {"auth": "public"}


def test_discovery_finds_route_modules():
    """Guards the parametrised tests below: an empty discovery (e.g. a mistyped ROUTES_DIR) would skip them silently."""
    assert "auth" in ROUTE_MODULES
    assert "home" in ROUTE_MODULES
    assert "tests_level.commands" in ROUTE_MODULES


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
    path = _path_for(module_name)
    rel = path.relative_to(ROUTES_DIR.parent.parent).as_posix()
    tree = ast.parse(path.read_text(encoding="utf-8"))
    build_router = [
        f"{rel}:{node.lineno}"
        for node in ast.walk(tree)
        if isinstance(node, _FUNCS) and node.name == "build_router"
    ]
    nested: dict[int, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, _FUNCS):
            for inner in _nested_in(node):
                if inner.decorator_list:
                    nested[inner.lineno] = f"{rel}:{inner.lineno}"
    return build_router, [nested[k] for k in sorted(nested)]


@pytest.mark.parametrize("module_name", ROUTE_MODULES)
def test_no_build_router(module_name: str) -> None:
    build_router, _ = _hits(module_name)
    assert not build_router, f"{module_name} still defines build_router: {build_router}"


@pytest.mark.parametrize("module_name", ROUTE_MODULES)
def test_no_nested_functions(module_name: str) -> None:
    _, nested = _hits(module_name)
    assert not nested, f"{module_name} nests functions: {nested}"


@pytest.mark.parametrize("module_name", ROUTE_MODULES)
def test_every_module_exposes_a_router(module_name: str) -> None:
    module = importlib.import_module(f"web_interface.routes.{module_name}")
    attr = ROUTER_ATTR.get(module_name, "router")
    assert isinstance(getattr(module, attr, None), APIRouter), (
        f"{module_name} must expose a module-level APIRouter named {attr!r}"
    )
