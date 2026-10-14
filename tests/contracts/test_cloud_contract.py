"""Edge-cloud contract: MachineReport and the UI bundle manifest (v0.1.0)."""

import json

import pytest
from pydantic import ValidationError

from ice_contracts.cloud.report import (
    CLOUD_CONTRACT_VERSION,
    FaultView,
    MachineReport,
)
from ice_contracts.cloud.ui import BundleManifest, LevelEntry
from ice_contracts.generate import CONTRACTS, generate

CRLF, LF = bytes([13, 10]), bytes([10])

# Literal copy of cloud spec section 6.6.
SPEC_EXAMPLE = """
{
  "contract": "0.1.0", "machine_id": "m-0042", "sent_at": "2026-10-13T14:05:00Z",
  "boot_id": "6f1c2a7e-3d4b-4e0a-9b51-2c8f0d7a1e93", "seq": 118,
  "report_interval_seconds": 60, "edge_build": "6fa3512",
  "ui": {"digest": "3b9f0c2d8e41a7b65f90d3c1e28a4b7f6d05c9e1a3b2f48d7e6c5a0b9d1f2e3a", "ice_ui_version": "0.1.0"},
  "health": {
    "vmc_missing": false,
    "status": {"state": "idle", "selected_product": null, "credit_escrow": 0.0,
               "last_payment_method": "cash"},
    "is_healthy": false,
    "issues": ["ICE-301 Ice bin low (Bag of Ice)", "2 errors in last 24h"],
    "active_faults": [{"key": "ICE-10LB", "sku": "ICE-10LB", "product": "Bag of Ice",
                       "code": "ICE-301", "severity": "lockout", "scope": "product",
                       "description": "Ice bin low", "since_seconds": 412.3}],
    "payment_enabled": true, "payment_reasons": [], "machine_stopped": false,
    "maintenance": null
  },
  "kpi": {
    "summary": {"money_in": 42.0, "products_out": 14, "vends_failed": 1, "errors": 2,
                "uptime_pct": 99.3},
    "average": {"money_in": 38.5, "products_out": 12.4, "errors": 0.6, "uptime_pct": 98.9}
  },
  "faults": [{"key": "ICE-10LB", "sku": "ICE-10LB", "product": "Bag of Ice", "code": "ICE-301",
              "severity": "lockout", "scope": "product", "description": "Ice bin low",
              "since_seconds": 412.3, "gate": "fulfillment", "pending_sale": null}],
  "health_page": {
    "mqtt_connected": true, "vmc_state": "idle",
    "vmc_build": {"commit": "6fa3512c0e9d4b7a1f2e3c5d6b8a9f0e1d2c3b4a", "commit_short": "6fa3512", "source": "image",
                  "uptime_seconds": 10820, "machine_id": "m-0042"},
    "subsystems_ok": 3, "subsystems_total": 3
  }
}
"""


def _example() -> dict:
    return json.loads(SPEC_EXAMPLE)


def _full() -> MachineReport:
    data = _example()
    data["faults"][0]["pending_sale"] = {
        "sku": "ICE-10LB",
        "price": 4.5,
        "methods": {"cash": 4.5},
    }
    return MachineReport.model_validate(data)


def test_version():
    assert CLOUD_CONTRACT_VERSION == "0.1.0"


def test_spec_example_validates():
    r = MachineReport.model_validate_json(SPEC_EXAMPLE)
    assert r.faults[0].gate == "fulfillment"
    assert r.health.active_faults[0].code == "ICE-301"


def test_roundtrip_fully_populated():
    r = _full()
    assert MachineReport.model_validate_json(r.model_dump_json()) == r
    assert r.faults[0].pending_sale.methods == {"cash": 4.5}


def test_frozen():
    with pytest.raises(ValidationError):
        _full().seq = 1


@pytest.mark.parametrize(
    "path",
    [
        (),
        ("ui",),
        ("health",),
        ("health", "status"),
        ("health", "active_faults", 0),
        ("kpi",),
        ("kpi", "summary"),
        ("kpi", "average"),
        ("faults", 0),
        ("faults", 0, "pending_sale"),
        ("health_page",),
        ("health_page", "vmc_build"),
    ],
)
def test_unknown_key_rejected_everywhere(path):
    data = _full().model_dump(mode="json")
    node = data
    for p in path:
        node = node[p]
    node["bogus"] = 1
    with pytest.raises(ValidationError):
        MachineReport.model_validate(data)


@pytest.mark.parametrize(
    "digest", ["A" * 64, "a" * 63, "a" * 65, "g" * 64, "3B" + "a" * 62]
)
def test_digest_pattern_rejects(digest):
    data = _example()
    data["ui"]["digest"] = digest
    with pytest.raises(ValidationError):
        MachineReport.model_validate(data)
    with pytest.raises(ValidationError):
        BundleManifest(format=1, digest=digest, ice_ui_version="0.1.0", levels=[])


@pytest.mark.parametrize(
    "field,value",
    [
        ("machine_id", "bad id"),
        ("machine_id", ""),
        ("machine_id", "x" * 65),
        ("contract", "0.2.0"),
        ("seq", -1),
        ("report_interval_seconds", 9),
        ("report_interval_seconds", 3601),
        ("boot_id", "not-a-uuid"),
    ],
)
def test_top_level_constraints(field, value):
    data = _example()
    data[field] = value
    with pytest.raises(ValidationError):
        MachineReport.model_validate(data)


def test_gate_literal():
    data = _example()
    data["faults"][0]["gate"] = "bogus"
    with pytest.raises(ValidationError):
        MachineReport.model_validate(data)


def test_fault_views_share_base():
    from ice_contracts.cloud.report import ActiveFaultView, FaultBase

    assert issubclass(FaultView, FaultBase)
    assert issubclass(ActiveFaultView, FaultBase)


def test_offline_kpi_and_health_shapes():
    data = _example()
    data["kpi"] = {"summary": None, "average": None}
    data["health"].update(
        vmc_missing=True,
        status=None,
        is_healthy=None,
        payment_enabled=None,
        machine_stopped=None,
    )
    data["health_page"]["vmc_build"] = None
    r = MachineReport.model_validate(data)
    assert r.kpi.summary is None


def test_bundle_manifest():
    m = BundleManifest(
        format=1,
        digest="a" * 64,
        ice_ui_version="0.1.0",
        levels=[LevelEntry(title="Home", url="/", parent_url=None)],
    )
    assert BundleManifest.model_validate_json(m.model_dump_json()) == m
    with pytest.raises(ValidationError):
        BundleManifest(format=2, digest="a" * 64, ice_ui_version="x", levels=[])
    with pytest.raises(ValidationError):
        LevelEntry(title="Home", url="/", parent_url=None, extra=1)


def test_edge_cloud_registered_and_bytes_match(tmp_path):
    schema_dir, models = CONTRACTS["edge-cloud"]
    assert schema_dir.parts[-3:] == ("contracts", "edge-cloud", "schemas")
    out = tmp_path / "schemas"
    generate({"edge-cloud": (out, models)})

    def norm(d):
        return {
            p.name: p.read_bytes().replace(CRLF, LF) for p in d.glob("*.schema.json")
        }

    assert norm(schema_dir) == norm(out)
    assert set(norm(out)) == {f"{n}.schema.json" for n in models}
