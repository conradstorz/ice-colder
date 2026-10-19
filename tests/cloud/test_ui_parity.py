"""The owner's core requirement in executable form: the cloud renders the
machine's own dashboard, from the machine's own templates, identically, even
while the machine is offline.

Each page is rendered twice -- once by the edge app for a user whose
permissions are narrowed to ``REMOTE_VIEWER_PERMISSIONS``, once by the cloud
portal from a ``MachineReport`` built off the same machine -- and compared.
Full pages compare the inner HTML of ``<main>`` (the remote banner renders
outside it); fragments have no ``<main>`` and compare the whole body.
"""

from __future__ import annotations

import time as real_time
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from ice_cloud.app import create_app
from tests.cloud.conftest import make_settings
from ice_contracts.access import REMOTE_VIEWER_PERMISSIONS
from ice_contracts.board.vending_machine import FaultCode
from ice_edge.access.access import Role
from ice_edge.faults.availability import Availability
from ice_edge.ledger.event_recorder import EventRecorder
from ice_edge.monitoring.health_monitor import HealthMonitor
from ice_edge.uplink.report import build_machine_report
from ice_edge.web import auth as web_auth
from ice_edge.web import context
from tests.cloud.reports import BOOT_A, FP, NOW
from tests.dom_utils import main_html

FULL_PAGES = ["/", "/health", "/health/faults"]
FRAGMENTS = ["/status", "/pill", "/kpi"]


class _FrozenClock:
    """Stands in for the ``time`` module inside the health monitor and the
    event recorder, so every time-relative value is computed at one instant."""

    def __init__(self) -> None:
        self.mono = 1000.0
        self.wall = NOW.timestamp()

    def monotonic(self) -> float:
        return self.mono

    def time(self) -> float:
        return self.wall

    def advance(self, seconds: float) -> None:
        self.mono += seconds
        self.wall += seconds

    def __getattr__(self, name):
        return getattr(real_time, name)


@pytest.fixture
def frozen(monkeypatch):
    clock = _FrozenClock()
    monkeypatch.setattr("ice_edge.monitoring.health_monitor.time", clock)
    monkeypatch.setattr("ice_edge.ledger.event_recorder.time", clock)
    return clock


@pytest.fixture
def narrowed_owner(monkeypatch):
    """The edge's signed-in user holds only the remote viewer's permissions."""
    monkeypatch.setitem(
        web_auth.ROLE_PERMISSIONS, Role.owner, REMOTE_VIEWER_PERMISSIONS
    )


@pytest.fixture
async def edge_and_report(wired, login_as, frozen, narrowed_owner, tmp_path):
    """(edge client, MachineReport) for one machine with an active fault, a
    stale subsystem, a health monitor, availability and an event recorder."""
    cfg, _vmc, _inv, _store = wired
    cfg.machine_id = "m-0042"
    machine = context.machine_instance

    monitor = HealthMonitor(machine_id=cfg.machine_id)
    machine.set_health_monitor(monitor)
    context.set_health_monitor(monitor)
    availability = Availability()
    machine.set_availability(availability)
    context.set_availability(availability)
    recorder = EventRecorder(db_path=str(tmp_path / "events.db"))
    machine.set_event_recorder(recorder)
    context.set_event_recorder(recorder)

    monitor.record_heartbeat("vending")
    frozen.advance(130)  # past the 120 s subsystem timeout: "vending" is stale
    machine.vmc.raise_fault(FaultCode.ICE_301, sku="ICE-1")
    frozen.advance(412)
    monitor.record_heartbeat("ice_maker")
    monitor.record_heartbeat("mdb")
    recorder.flush()

    report = await build_machine_report(
        machine, config=cfg, seq=1, boot_id=BOOT_A, now=NOW
    )
    return login_as(Role.owner), report


def _cloud_client(tmp_path, report, now):
    clock = {"now": now}
    app = create_app(make_settings(tmp_path / "cloud"), now=lambda: clock["now"])
    client = TestClient(app, follow_redirects=False)
    r = client.post(
        f"/api/v1/machines/{FP}/report",
        content=report.model_dump_json(),
        headers={"content-type": "application/json"},
    )
    assert r.status_code == 204
    assert client.post(f"/fleet/select/{FP}").status_code == 303
    return client


def _assert_parity(edge, cloud, *, stale):
    for path in FULL_PAGES:
        e, c = edge.get(path), cloud.get(path)
        assert e.status_code == c.status_code == 200, path
        assert main_html(e.text) == main_html(c.text), path
        assert "remote-banner" not in e.text, "the machine never shows the banner"
        assert "remote-banner" in c.text
        assert "remote-banner" not in main_html(c.text)
        assert ("Offline — showing last report" in c.text) is stale, path
    for path in FRAGMENTS:
        e, c = edge.get(path), cloud.get(path)
        assert e.status_code == c.status_code == 200, path
        assert e.text == c.text, path
        assert "remote-banner" not in c.text, path


@pytest.mark.parametrize("stale", [False, True], ids=["online", "stale"])
async def test_cloud_renders_what_the_machine_renders(edge_and_report, tmp_path, stale):
    edge, report = edge_and_report
    now = NOW + timedelta(hours=1) if stale else NOW
    with _cloud_client(tmp_path, report, now) as cloud:
        _assert_parity(edge, cloud, stale=stale)


async def test_parity_pages_are_not_vacuous(edge_and_report, tmp_path):
    """Guard the comparison itself: the pages carry the data under test."""
    edge, report = edge_and_report
    with _cloud_client(tmp_path, report, NOW) as cloud:
        faults = main_html(cloud.get("/health/faults").text)
        assert "ICE-301" in faults and "412" not in faults and "6m" in faults
        status = cloud.get("/status").text
        assert "ICE-301" in status and "Stale subsystems: vending" in status
        assert "ICE-301" in cloud.get("/pill").text
        assert "1 active fault" in main_html(cloud.get("/").text)
        health = main_html(cloud.get("/health").text)
        assert "2/3 OK" in health and "Payment" in health
        assert "Clear" not in faults and "Record sale" not in faults


@pytest.mark.parametrize("stale", [False, True], ids=["online", "stale"])
async def test_parity_when_the_machine_has_no_vmc(edge_and_report, tmp_path, stale):
    """vmc_missing end to end: /status serves VMC_MISSING_HTML, the pill is
    neutral, /health shows '0 active faults' and '—' for availability, KPI
    cards are placeholders -- identically on the edge and in the cloud."""
    from ice_contracts.cloud.report import HealthSummary, KpiSummary
    from ice_edge.views.health import build_health_page_summary, subsystem_rows

    edge, report = edge_and_report
    # Unwire through the context setters, not monkeypatch: the autouse
    # route-context restore runs before monkeypatch teardown and would be
    # undone by it.
    context.set_machine_instance(None)
    context.set_health_monitor(None)
    context.set_availability(None)
    context.set_event_recorder(None)
    report = report.model_copy(
        update={
            "health": HealthSummary(
                vmc_missing=True,
                status=None,
                is_healthy=None,
                issues=[],
                active_faults=[],
                payment_enabled=None,
                payment_reasons=[],
                machine_stopped=None,
                maintenance=None,
            ),
            "kpi": KpiSummary(summary=None, average=None),
            "faults": [],
            "health_page": build_health_page_summary(None, subsystem_rows(None)),
        }
    )
    now = NOW + timedelta(hours=1) if stale else NOW
    with _cloud_client(tmp_path, report, now) as cloud:
        _assert_parity(edge, cloud, stale=stale)
        assert "VMC not initialized" in cloud.get("/status").text
        assert "0 active faults" in main_html(cloud.get("/health").text)
        assert "—" in cloud.get("/pill").text
