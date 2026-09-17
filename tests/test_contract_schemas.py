"""Committed JSON Schemas must match the live Pydantic models (drift guard)."""

import json

import pytest

from contracts.generate import MODELS, SCHEMA_DIR


def test_schema_dir_has_exactly_the_expected_files():
    expected = {f"{name}.schema.json" for name in MODELS}
    actual = {p.name for p in SCHEMA_DIR.glob("*.schema.json")}
    assert actual == expected


@pytest.mark.parametrize("name", sorted(MODELS))
def test_committed_schema_matches_model(name):
    committed = json.loads(
        (SCHEMA_DIR / f"{name}.schema.json").read_text(encoding="utf-8")
    )
    assert committed == MODELS[name].model_json_schema(), (
        f"{name} schema drifted — run: uv run python -m contracts.generate"
    )
