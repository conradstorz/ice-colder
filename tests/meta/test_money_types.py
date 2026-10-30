"""Ratchet: no money-named field may be (or contain) a float.

Imports every module under packages/*/src, collects every pydantic model and
dataclass, and fails on a field whose name looks like money and whose
annotation mentions `float`. ALLOWLIST is shrink-only: an entry that is no
longer an offender must be removed.
"""

import dataclasses
import importlib
import inspect
import pkgutil
import re
import sys
import typing
from pathlib import Path

from pydantic import BaseModel

REPO = Path(__file__).resolve().parents[2]
NAME_RE = re.compile(r"(?i)price|amount|credit|revenue|cash|money|refund")

# Modules that cannot be imported for a meaningful scan.
SKIPPED_MODULES: list[str] = []

# "module.Class.field": reason.  PERMANENT entries are marked as such.
ALLOWLIST: dict[str, str] = {
    # PERMANENT until spec B deletes the legacy board payment models.
    "ice_contracts.board.mqtt_messages.PaymentEvent.amount": "PERMANENT (legacy board model, spec B)",
    "ice_contracts.board.vending_machine.PaymentRefundCommand.amount": "PERMANENT (legacy board model, spec B)",
    "ice_contracts.board.vending_machine.PaymentRefundResult.amount_returned": "PERMANENT (legacy board model, spec B)",
    # PERMANENT: an average of money is float cents (renamed *_cents later, entry key changes then).
    "ice_contracts.cloud.report.KpiAverage.money_in_cents": "PERMANENT (average; float cents)",
    # Converted by later tasks; remove each entry when its rename lands.
    "ice_contracts.board.mqtt_messages.VMCStatus.credit_escrow": "PERMANENT (deprecated; removed in vending contract 3.0.0)",
}


def _contains_float(ann) -> bool:
    if ann is float:
        return True
    return any(_contains_float(a) for a in typing.get_args(ann))


def _iter_modules():
    roots = sorted((REPO / "packages").glob("*/src"))
    for root in roots:
        sys.path.insert(0, str(root))
        for pkg in sorted(p for p in root.iterdir() if (p / "__init__.py").exists()):
            yield pkg.name
            for info in pkgutil.walk_packages([str(pkg)], prefix=f"{pkg.name}."):
                yield info.name


def collect_offenders() -> set[str]:
    found: set[str] = set()
    skipped: list[str] = []
    for name in _iter_modules():
        if name.rsplit(".", 1)[-1] == "__main__":
            continue
        try:
            mod = importlib.import_module(name)
        except Exception:  # noqa: BLE001 - optional deps / side effects
            skipped.append(name)
            continue
        for cls_name, cls in vars(mod).items():
            if not inspect.isclass(cls) or cls.__module__ != name:
                continue
            if issubclass(cls, BaseModel):
                hints = {f: i.annotation for f, i in cls.model_fields.items()}
            elif dataclasses.is_dataclass(cls):
                hints = {f.name: f.type for f in dataclasses.fields(cls)}
                try:
                    hints = typing.get_type_hints(cls)
                except Exception:  # noqa: BLE001
                    pass
            else:
                continue
            for field, ann in hints.items():
                if isinstance(ann, str):
                    is_float = "float" in ann
                else:
                    is_float = _contains_float(ann)
                if is_float and NAME_RE.search(field):
                    found.add(f"{name}.{cls_name}.{field}")
    global SKIPPED_MODULES
    SKIPPED_MODULES = skipped
    return found


def test_no_new_float_money_fields():
    offenders = collect_offenders()
    new = sorted(offenders - ALLOWLIST.keys())
    assert not new, "float money fields (use integer *_cents): " + ", ".join(new)


def test_allowlist_only_shrinks():
    offenders = collect_offenders()
    stale = sorted(ALLOWLIST.keys() - offenders)
    assert not stale, "no longer offenders, remove from ALLOWLIST: " + ", ".join(stale)


def test_scan_covers_modules():
    collect_offenders()
    assert SKIPPED_MODULES == [], f"modules skipped: {SKIPPED_MODULES}"


if __name__ == "__main__":
    for o in sorted(collect_offenders()):
        print(o)
    print("SKIPPED:", SKIPPED_MODULES)
