# tests/test_dispensers_example.py
"""Tests for the self-documenting `dispensers.example.toml` generator (plan:
dispenser profiles, Task 5). `services.dispensers_doc.render_example()`
produces the example from the Task 2 schema models
(`services/dispenser_schema.py`) so the shipped documentation cannot drift
from the schema it illustrates; these tests guard byte-identity with the
committed file, that the example validates cleanly against the example
catalog, and that every schema field is documented somewhere in the file.
"""

import json
import re
from pathlib import Path

from config.config_model import ConfigModel
from services.dispenser_schema import (
    Accessory,
    AgitateStep,
    BaggedIceProfile,
    CurrentSense,
    IceFillBySensor,
    IceFillTimed,
    ReleaseBySensor,
    ReleaseTimed,
    WaterFillByVolume,
    WaterFillProfile,
    WaterFillTimed,
)
from services.dispensers import validate_document
from services.dispensers_doc import render_example

EXAMPLE_PATH = Path("dispensers.example.toml")
CONFIG_EXAMPLE_PATH = Path("config.example.json")

# "Every model in Task 2" (services/dispenser_schema.py): every BaseModel
# subclass that describes a slice of a dispensers.toml table. SlotProfile,
# IceFillStep, ReleaseStep and WaterFillStep are type aliases (discriminated
# unions), not models, so they are not listed here -- their member models
# (IceFillBySensor/IceFillTimed etc.) are.
_TASK_2_MODELS = (
    CurrentSense,
    AgitateStep,
    IceFillBySensor,
    IceFillTimed,
    ReleaseBySensor,
    ReleaseTimed,
    WaterFillByVolume,
    WaterFillTimed,
    Accessory,
    BaggedIceProfile,
    WaterFillProfile,
)


def test_example_is_byte_identical_to_generator():
    on_disk = EXAMPLE_PATH.read_text(encoding="utf-8")
    assert on_disk == render_example(), (
        "run: uv run python scripts/gen_dispensers_example.py"
    )


def test_example_validates_clean_against_example_config():
    raw = json.loads(CONFIG_EXAMPLE_PATH.read_text(encoding="utf-8"))
    config = ConfigModel.model_validate(raw)
    example_text = EXAMPLE_PATH.read_text(encoding="utf-8")

    report = validate_document(example_text, config.products)

    assert report.errors == []
    assert set(report.profiles.keys()) == {0, 1, 2}


def test_example_config_products_have_kinds():
    raw = json.loads(CONFIG_EXAMPLE_PATH.read_text(encoding="utf-8"))
    config = ConfigModel.model_validate(raw)
    kinds = {p.sku: p.kind for p in config.products}

    assert kinds == {
        "SAMPLE-ICE": "ice",
        "SAMPLE-WATER-SM": "water",
        "SAMPLE-WATER-LG": "water",
    }


def test_every_schema_field_is_documented():
    """Every field name declared on any Task 2 model must appear in the
    example text -- either as a (possibly commented-out) `name = value`
    assignment, or as a dotted component of a `[slot...]` table header --
    with a `#` comment somewhere in the up-to-3 lines immediately before
    it (or on the line itself, for a commented assignment)."""

    text = EXAMPLE_PATH.read_text(encoding="utf-8")
    lines = text.splitlines()

    assign_re_cache: dict[str, re.Pattern] = {}

    def assign_pattern(name: str) -> re.Pattern:
        if name not in assign_re_cache:
            assign_re_cache[name] = re.compile(rf"^\s*#?\s*{re.escape(name)}\s*=")
        return assign_re_cache[name]

    header_re = re.compile(r"^\[([\w.]+)\]\s*$")

    def has_preceding_comment(index: int) -> bool:
        window = lines[max(0, index - 3) : index]
        return any("#" in w for w in window) or lines[index].lstrip().startswith("#")

    def field_is_documented(name: str) -> bool:
        pattern = assign_pattern(name)
        for i, line in enumerate(lines):
            if pattern.match(line) and has_preceding_comment(i):
                return True
            header_match = header_re.match(line.strip())
            if (
                header_match
                and name in header_match.group(1).split(".")
                and has_preceding_comment(i)
            ):
                return True
        return False

    missing: list[str] = []
    for model in _TASK_2_MODELS:
        for field_name in model.model_fields:
            if not field_is_documented(field_name):
                missing.append(f"{model.__name__}.{field_name}")

    assert missing == []
