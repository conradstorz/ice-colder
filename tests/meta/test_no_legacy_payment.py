"""Vending contract 3.0.0 removed the legacy payment models and topics.

An ``ast`` walk over the edge and simulator sources fails on any import of,
or attribute/name use of, a removed name, and on any string constant that is
exactly a removed topic or command.
"""

from __future__ import annotations

import ast

from tests.repo_root import REPO_ROOT

ROOTS = [
    REPO_ROOT / "packages" / "edge" / "src",
    REPO_ROOT / "packages" / "sims" / "src",
]

REMOVED_NAMES = {
    "PaymentRefundCommand",
    "PaymentRefundResult",
    "RefundStatus",
    "PaymentEvent",
    "PaymentStatus",
    "PaymentEnableCommand",
    "RefundProtocol",
    "handle_payment_status",
    "subscribe_payment_status",
    "on_payment_status",
    "set_payment_device",
}
REMOVED_STRINGS = {
    "payment/credit",
    "payment/status",
    "payment/enable",
    "cmd/payment/enable",
    "cmd/payment/refund",
    "cmd/payment/refund/ack",
}


def _flatten(node: ast.AST) -> str | None:
    """The literal text of an f-string or a ``+`` concatenation of strings,
    with a NUL character standing for every interpolated or non-literal part; ``None``
    for any other node."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(
            v.value
            if isinstance(v, ast.Constant) and isinstance(v.value, str)
            else "\0"
            for v in node.values
        )
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _flatten(node.left), _flatten(node.right)
        if left is None and right is None:
            return None
        return (left if left is not None else "\0") + (
            right if right is not None else "\0"
        )
    return None


def _composed_string_hits(node: ast.AST) -> list[str]:
    """Removed strings hidden in an f-string or a concatenation."""
    if not isinstance(node, (ast.JoinedStr, ast.BinOp)):
        return []
    text = _flatten(node)
    if text is None:
        return []
    return [s for s in REMOVED_STRINGS if s in text]


def _dynamic_name_hit(node: ast.AST) -> str | None:
    """``getattr(obj, "removed_name")`` and its set/has/del siblings."""
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in {"getattr", "setattr", "hasattr", "delattr"}
        and len(node.args) >= 2
        and isinstance(node.args[1], ast.Constant)
        and node.args[1].value in REMOVED_NAMES
    ):
        return node.args[1].value
    return None


def _hits() -> list[str]:
    found: list[str] = []
    for root in ROOTS:
        for path in sorted(root.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            rel = path.relative_to(REPO_ROOT).as_posix()
            for node in ast.walk(tree):
                for composed in _composed_string_hits(node):
                    found.append(f"{rel}:{node.lineno} composed string {composed!r}")
                dynamic = _dynamic_name_hit(node)
                if dynamic is not None:
                    found.append(f"{rel}:{node.lineno} dynamic name {dynamic}")
                name = None
                if isinstance(node, ast.Name):
                    name = node.id
                elif isinstance(node, ast.Attribute):
                    name = node.attr
                elif isinstance(
                    node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
                ):
                    name = node.name
                elif isinstance(node, ast.alias):
                    name = node.name.split(".")[-1]
                elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                    if node.value in REMOVED_STRINGS:
                        found.append(f"{rel}:{node.lineno} string {node.value!r}")
                    continue
                if name in REMOVED_NAMES:
                    found.append(f"{rel}:{getattr(node, 'lineno', 0)} {name}")
    return found


def _parse_hits(source: str) -> list[str]:
    tree = ast.parse(source)
    found: list[str] = []
    for node in ast.walk(tree):
        found += _composed_string_hits(node)
        dynamic = _dynamic_name_hit(node)
        if dynamic is not None:
            found.append(dynamic)
    return found


def test_the_guard_sees_composed_strings_and_dynamic_names() -> None:
    assert _parse_hits('t = f"{base}/cmd/payment/enable"')
    assert _parse_hits('t = "cmd/" + "payment/enable"')
    assert _parse_hits('t = prefix + "payment/status"')
    assert _parse_hits('getattr(x, "RefundProtocol")')
    assert _parse_hits('hasattr(x, "set_payment_device")')
    assert not _parse_hits('t = f"{base}/payments/ok"')


def test_no_legacy_payment_names_in_edge_or_sims() -> None:
    hits = _hits()
    assert not hits, "legacy payment names remain:\n" + "\n".join(hits)
