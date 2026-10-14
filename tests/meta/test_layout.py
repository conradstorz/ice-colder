"""Repo layout guards for the package reorganisation."""

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
EDGE = ROOT / "packages" / "edge" / "src" / "ice_edge"

EDGE_LAYERS = [
    "app",
    "web",
    "uplink",
    "views",
    "sale",
    "vend",
    "payment",
    "reporting",
    "faults",
    "notify",
    "monitoring",
    "access",
    "ledger",
    "customer_ui",
    "hal",
    "config",
    "supervisor",
    "cctv",
]

LEGACY = [
    "controller",
    "services",
    "config",
    "web_interface",
    "simulators",
    "contracts",
    "hardware",
    "main.py",
    "hello.py",
]


def test_legacy_roots_gone():
    present = [name for name in LEGACY if (ROOT / name).exists()]
    assert present == []


def test_every_edge_layer_documented():
    for layer in EDGE_LAYERS:
        init = EDGE / layer / "__init__.py"
        assert init.is_file(), f"missing {init}"
        doc = ast.get_docstring(ast.parse(init.read_text(encoding="utf-8")))
        assert doc and "§" in doc, f"{layer} docstring lacks a § reference"
