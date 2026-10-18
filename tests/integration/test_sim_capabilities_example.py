"""The vending simulator's declared channels against the shipped dispenser
example, validated by the edge. Lives here because it is the one check that
joins `ice_sims` and `ice_edge`; neither side's own tests may name the other
(`tests/meta/test_test_isolation.py`). Needs no broker."""

import json

from ice_common.config_model import ConfigModel
from ice_edge.vend.dispensers import validate_document
from ice_sims.vending_machine import VendingMachineSimulator
from tests.repo_root import REPO_ROOT


def test_example_toml_validates_with_zero_warnings_against_simulator_capabilities():
    """The simulator's own declared channels must clear every
    capabilities-cross-check finding for the shipped example: every channel
    the example drives is a declared output, every one it senses a declared
    input or counter."""
    caps = VendingMachineSimulator().build_capabilities()
    raw = json.loads((REPO_ROOT / "config.example.json").read_text(encoding="utf-8"))
    config = ConfigModel.model_validate(raw)
    text = (REPO_ROOT / "dispensers.example.toml").read_text(encoding="utf-8")

    report = validate_document(text, config.products, capabilities=caps)

    assert report.errors == []
    assert report.warnings == []
