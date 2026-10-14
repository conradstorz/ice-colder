"""Shared pill / health-tile logic moved into ice_ui (cloud spec section 2)."""

import pytest

from ice_contracts.access import Permission
from ice_contracts.cloud.report import ActiveFaultView, HealthSummary
from ice_ui.levels import (
    LEVEL_HEALTH_AVAILABILITY,
    LEVEL_HEALTH_FAULTS,
    LEVEL_HEALTH_LOGS,
    LEVEL_HEALTH_SUBSYSTEMS,
)
from ice_ui.tiles import availability_line, fault_count_line, health_tiles
from ice_ui.views.health import SEVERITY_RANK, highest_priority_fault, pill_state


def _fault(code, severity, key=None):
    return ActiveFaultView(
        key=key or code,
        sku=None,
        product=None,
        code=code,
        severity=severity,
        scope="machine",
        description="d",
        since_seconds=None,
    )


def _health(**kw) -> HealthSummary:
    base = dict(
        vmc_missing=False,
        status=None,
        is_healthy=True,
        issues=[],
        active_faults=[],
        payment_enabled=True,
        payment_reasons=[],
        machine_stopped=False,
        maintenance=None,
    )
    base.update(kw)
    return HealthSummary(**base)


def test_pill_neutral_when_vmc_missing():
    assert pill_state(_health(vmc_missing=True, is_healthy=None)) == ("neutral", "—")


def test_pill_ok_when_healthy():
    assert pill_state(_health()) == ("ok", "OK")


def test_pill_names_top_fault_by_severity_then_order():
    faults = [
        _fault("WRN-1", "warning"),
        _fault("CRT-2", "critical"),
        _fault("CRT-3", "critical"),
    ]
    h = _health(is_healthy=False, active_faults=faults)
    assert pill_state(h) == ("fault", "CRT-2")


def test_pill_issue_when_unhealthy_without_faults():
    assert pill_state(_health(is_healthy=False)) == ("fault", "ISSUE")


def test_unknown_severity_ranks_last():
    faults = [_fault("X-1", "bogus"), _fault("I-1", "info")]
    assert highest_priority_fault(faults).code == "I-1"
    assert highest_priority_fault([]) is None
    assert list(SEVERITY_RANK) == [
        "critical",
        "lockout",
        "vend_failed",
        "product_unavailable",
        "warning",
        "info",
    ]


@pytest.mark.parametrize(
    ("count", "text"),
    [
        (None, "—"),
        (0, "0 active faults"),
        (1, "1 active fault"),
        (3, "3 active faults"),
    ],
)
def test_fault_count_line(count, text):
    assert fault_count_line(count) == text


@pytest.mark.parametrize(
    ("enabled", "reasons", "text"),
    [
        (None, 0, "—"),
        (True, 0, "Payment enabled"),
        (False, 2, "Payment disabled (2)"),
    ],
)
def test_availability_line(enabled, reasons, text):
    assert availability_line(enabled, reasons) == text


def test_health_tiles_shape_and_logs_permission():
    kw = dict(subsystems="3/3 OK", faults="1 active fault", availability="—")
    tiles = health_tiles(**kw, logs="Last 50 lines", perms=frozenset())
    assert [t["title"] for t in tiles] == [
        "Subsystems",
        "Faults",
        "Availability",
        "Logs",
    ]
    assert [t["url"] for t in tiles] == [
        LEVEL_HEALTH_SUBSYSTEMS.url,
        LEVEL_HEALTH_FAULTS.url,
        LEVEL_HEALTH_AVAILABILITY.url,
        LEVEL_HEALTH_LOGS.url,
    ]
    assert all(t["icon"] == "health" and t["coming_soon"] is False for t in tiles)
    assert [t["enabled"] for t in tiles] == [True, True, True, False]
    assert [t["context"] for t in tiles] == [
        "3/3 OK",
        "1 active fault",
        "—",
        "Last 50 lines",
    ]
    with_logs = health_tiles(
        **kw, logs="Last 50 lines", perms=frozenset({Permission.view_logs})
    )
    assert with_logs[3]["enabled"] is True
