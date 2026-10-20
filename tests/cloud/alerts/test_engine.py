from datetime import datetime, timedelta

from ice_cloud.alerts.engine import KIND_ORDER, diff_reports
from ice_contracts.cloud.report import MachineReport

from tests.cloud.reports import FP, NOW, make_report

LATER = NOW + timedelta(minutes=7)


def _machine_fault(code: str, gate: str = "safety") -> dict:
    return {
        "key": code,
        "sku": None,
        "product": None,
        "code": code,
        "severity": "critical",
        "scope": "machine",
        "description": f"{code} description",
        "since_seconds": 1.0,
        "gate": gate,
        "pending_sale": None,
    }


def _report(*faults: dict, sent_at: datetime = NOW):
    report = make_report(faults=False, sent_at=sent_at)
    data = report.model_dump(mode="json")
    data["faults"] = list(faults)
    return MachineReport.model_validate(data)


def test_first_report_raises_every_fault():
    report = make_report()
    events = diff_reports([], report, now=LATER, fingerprint=FP)
    assert [e.kind for e in events] == ["raised"]
    assert events[0].key == "ICE-10LB"
    assert events[0].fingerprint == FP
    assert events[0].code == "ICE-301"


def test_unchanged_emits_nothing():
    report = make_report()
    assert diff_reports(list(report.faults), report, now=LATER, fingerprint=FP) == []


def test_removed_key_is_cleared_with_fields_from_previous():
    previous = _report(_machine_fault("WTR-104", gate="safety")).faults
    report = make_report(faults=False)
    (ev,) = diff_reports(previous, report, now=LATER, fingerprint=FP)
    assert ev.kind == "cleared"
    assert ev.key == "WTR-104"
    assert ev.code == "WTR-104"
    assert ev.severity == "critical"
    assert ev.gate == "safety"
    assert ev.description == "WTR-104 description"
    assert ev.sku is None and ev.product is None


def test_mixed_orders_cleared_then_raised_by_key():
    report = _report(_machine_fault("WTR-106"), _machine_fault("ENV-102"))
    previous = _report(_machine_fault("WTR-104"), _machine_fault("PWR-102")).faults
    events = diff_reports(previous, report, now=LATER, fingerprint=FP)
    assert [(e.kind, e.key) for e in events] == [
        ("cleared", "PWR-102"),
        ("cleared", "WTR-104"),
        ("raised", "ENV-102"),
        ("raised", "WTR-106"),
    ]
    assert KIND_ORDER == ("cleared", "raised")


def test_at_is_now_not_sent_at():
    report = make_report(sent_at=NOW)
    previous = _report(_machine_fault("WTR-104")).faults
    events = diff_reports(previous, report, now=LATER, fingerprint=FP)
    assert {e.at for e in events} == {LATER}


def test_product_fault_carries_sku_and_product():
    (ev,) = diff_reports([], make_report(), now=LATER, fingerprint=FP)
    assert ev.sku == "ICE-10LB"
    assert ev.product == "Bag of Ice"
    assert ev.severity == "lockout"
    assert ev.gate == "fulfillment"
    assert ev.description == "Ice bin low"
