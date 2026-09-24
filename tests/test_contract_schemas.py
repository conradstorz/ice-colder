"""Committed JSON Schemas must match the live Pydantic models (drift guard)."""

import json

import pytest

from contracts.generate import CONTRACTS, schema_for

_CASES = [
    (contract, name, schema_dir, models[name])
    for contract, (schema_dir, models) in CONTRACTS.items()
    for name in sorted(models)
]


@pytest.mark.parametrize("contract", sorted(CONTRACTS))
def test_schema_dir_has_exactly_the_expected_files(contract):
    schema_dir, models = CONTRACTS[contract]
    expected = {f"{name}.schema.json" for name in models}
    actual = {p.name for p in schema_dir.glob("*.schema.json")}
    assert actual == expected


@pytest.mark.parametrize(
    "contract,name,schema_dir,model", _CASES, ids=[f"{c}/{n}" for c, n, _, _ in _CASES]
)
def test_committed_schema_matches_model(contract, name, schema_dir, model):
    committed = json.loads(
        (schema_dir / f"{name}.schema.json").read_text(encoding="utf-8")
    )
    assert committed == schema_for(model), (
        f"{contract}/{name} schema drifted — run: uv run python -m contracts.generate"
    )
