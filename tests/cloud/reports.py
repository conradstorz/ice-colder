"""Builders for MachineReports used across the cloud tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from ice_contracts.cloud.report import MachineReport
from ice_ui.bundle import ui_digest

NOW = datetime(2026, 10, 13, 14, 5, tzinfo=UTC)
BOOT_A = "6f1c2a7e-3d4b-4e0a-9b51-2c8f0d7a1e93"
BOOT_B = "0b6e3f64-5a1c-4d27-8c3e-9a7d1f2b4c58"

_FAULT = {
    "key": "ICE-10LB",
    "sku": "ICE-10LB",
    "product": "Bag of Ice",
    "code": "ICE-301",
    "severity": "lockout",
    "scope": "product",
    "description": "Ice bin low",
    "since_seconds": 412.3,
}


def make_report(
    *,
    machine_id: str = "m-0042",
    seq: int = 1,
    boot_id: str = BOOT_A,
    sent_at: datetime = NOW,
    interval: int = 60,
    digest: str | None = None,
    faults: bool = True,
    **overrides: Any,
) -> MachineReport:
    active = [_FAULT] if faults else []
    data: dict[str, Any] = {
        "contract": "0.1.0",
        "machine_id": machine_id,
        "sent_at": sent_at.isoformat(),
        "boot_id": boot_id,
        "seq": seq,
        "report_interval_seconds": interval,
        "edge_build": "6fa3512",
        "ui": {"digest": digest or ui_digest(), "ice_ui_version": "0.1.0"},
        "health": {
            "vmc_missing": False,
            "status": {
                "state": "idle",
                "selected_product": None,
                "credit_escrow": 0.0,
                "last_payment_method": "cash",
            },
            "is_healthy": not faults,
            "issues": ["ICE-301 Ice bin low (Bag of Ice)"] if faults else [],
            "active_faults": active,
            "payment_enabled": True,
            "payment_reasons": [],
            "machine_stopped": False,
            "maintenance": None,
        },
        "kpi": {
            "summary": {
                "money_in": 42.0,
                "products_out": 14,
                "vends_failed": 1,
                "errors": 2,
                "uptime_pct": 99.3,
            },
            "average": {
                "money_in": 38.5,
                "products_out": 12.4,
                "errors": 0.6,
                "uptime_pct": 98.9,
            },
        },
        "faults": [{**_FAULT, "gate": "fulfillment", "pending_sale": None}]
        if faults
        else [],
        "health_page": {
            "mqtt_connected": True,
            "vmc_state": "idle",
            "vmc_build": {
                "commit": "6fa3512c0e9d4b7a1f2e3c5d6b8a9f0e1d2c3b4a",
                "commit_short": "6fa3512",
                "source": "image",
                "uptime_seconds": 10820,
                "machine_id": machine_id,
            },
            "subsystems_ok": 3,
            "subsystems_total": 3,
        },
    }
    data.update(overrides)
    return MachineReport.model_validate(data)


def later(seconds: float) -> datetime:
    return NOW + timedelta(seconds=seconds)
