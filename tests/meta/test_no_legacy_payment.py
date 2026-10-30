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


def _hits() -> list[str]:
    found: list[str] = []
    for root in ROOTS:
        for path in sorted(root.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            rel = path.relative_to(REPO_ROOT).as_posix()
            for node in ast.walk(tree):
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


def test_no_legacy_payment_names_in_edge_or_sims() -> None:
    hits = _hits()
    assert not hits, "legacy payment names remain:\n" + "\n".join(hits)
