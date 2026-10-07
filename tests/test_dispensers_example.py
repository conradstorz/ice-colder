# tests/test_dispensers_example.py
"""Tests for the self-documenting `dispensers.example.toml` generator (plan:
dispenser profiles, Task 5). `services.dispensers_doc.render_example()`
produces the example from the Task 2 schema models
(`services/dispenser_schema.py`) so the shipped documentation cannot drift
from the schema it illustrates; these tests guard byte-identity with the
committed file, that the example validates cleanly against the example
catalog, that every generated comment reads like operator language rather
than Python internals, that discriminator fields honestly list every tag in
their union, that every channel field states its allowed characters, that
every assignment is immediately preceded by a comment, and that a schema
field with no sample value fails the generator loudly instead of silently
vanishing from the example.
"""

import json
import re
from pathlib import Path

import pytest

from config.config_model import ConfigModel
from services.dispenser_schema import AgitateStep
from services.dispensers import validate_document
from services.dispensers_doc import _render_table, render_example

EXAMPLE_PATH = Path("dispensers.example.toml")
CONFIG_EXAMPLE_PATH = Path("config.example.json")


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


def test_no_python_identifiers_in_comments():
    """No comment line may leak a Python/Pydantic internal name at a
    machine technician who does not read Python: no `validate_document`,
    no `_`-prefixed identifier, no `.py`, no `Pydantic`. The header block
    (above `schema_version`) is exempt from the `.py` check alone -- it
    legitimately names `scripts/gen_dispensers_example.py` and
    `services/dispenser_schema.py` as the regeneration instructions."""

    text = EXAMPLE_PATH.read_text(encoding="utf-8")
    lines = text.splitlines()
    underscore_prefixed_re = re.compile(r"(?<!\w)_\w+")

    in_header = True
    for i, line in enumerate(lines):
        if in_header and line.strip() == "":
            in_header = False

        stripped = line.strip()
        if not stripped.startswith("#"):
            continue

        assert "validate_document" not in line, f"line {i}: {line!r}"
        assert "Pydantic" not in line, f"line {i}: {line!r}"
        assert not underscore_prefixed_re.search(line), f"line {i}: {line!r}"
        if not in_header:
            assert ".py" not in line, f"line {i}: {line!r}"


def test_discriminator_comments_list_every_variant():
    """A discriminator field's comment must name every tag in its union,
    not just the live variant's own tag -- `mechanism`, ice `fill.proof`,
    `release.proof`, and water `fill.proof` each have exactly two."""

    text = EXAMPLE_PATH.read_text(encoding="utf-8")

    for expected in (
        "One of: bagged_ice, water_fill.",
        "One of: bag_full_sensor, timed.",
        "One of: door_sensor, timed.",
        "One of: flow_volume, timed.",
    ):
        assert expected in text, f"missing {expected!r}"


def test_channel_fields_state_allowed_characters():
    """Every line assigning a `*_channel` key (live or shown as the
    commented alternate-variant block) is immediately preceded by a
    comment stating the allowed characters."""

    text = EXAMPLE_PATH.read_text(encoding="utf-8")
    lines = text.splitlines()
    assign_re = re.compile(r"^\s*#?\s*([A-Za-z_][A-Za-z0-9_]*)\s*=")

    found_any = False
    for i, line in enumerate(lines):
        match = assign_re.match(line)
        if not match:
            continue
        name = match.group(1)
        if not name.endswith("_channel"):
            continue
        found_any = True

        j = i - 1
        while j >= 0 and not lines[j].strip():
            j -= 1
        assert j >= 0, f"no preceding content for line {i}: {line!r}"
        assert "lowercase letters, digits and underscores" in lines[j], (
            f"line {i} ({line!r}) immediately preceded by {lines[j]!r}"
        )

    assert found_any, "no *_channel assignment found in the example"


def test_every_assignment_is_immediately_preceded_by_a_comment():
    """Every non-comment, non-blank `key = value` line (not a `[...]`
    table header) must have a comment line (`#...`) directly above it,
    with no blank line in between."""

    text = EXAMPLE_PATH.read_text(encoding="utf-8")
    lines = text.splitlines()
    assign_re = re.compile(r"^\s*[A-Za-z_][A-Za-z0-9_]*\s*=")

    checked_any = False
    for i, line in enumerate(lines):
        if line.lstrip().startswith("#"):
            continue
        if not line.strip():
            continue
        if not assign_re.match(line):
            continue
        checked_any = True

        j = i - 1
        while j >= 0 and not lines[j].strip():
            j -= 1
        assert j >= 0, f"no preceding content for line {i}: {line!r}"
        assert lines[j].lstrip().startswith("#"), (
            f"line {i} ({line!r}) not immediately preceded by a comment; "
            f"got {lines[j]!r}"
        )

    assert checked_any, "no assignment line found in the example"


def test_missing_sample_value_fails_loudly():
    """A schema field with no hand-authored sample value must fail the
    generator loudly (`KeyError` naming both the model and the field),
    never silently vanish from the example."""

    sample_without_run_seconds = {
        "stall_current_amps": "unmonitored",
        "current_channel": "unmonitored",
        "motor_channel": "agitator_motor",
    }

    with pytest.raises(KeyError) as exc_info:
        _render_table(AgitateStep, sample_without_run_seconds, [], ["x"])

    assert "AgitateStep.run_seconds" in str(exc_info.value)
