"""Move-not-rewrite proof for the edge view-model builders (Task 12).

``_legacy_snapshot`` below is the body of ``ice_edge.web.context.health_snapshot``
exactly as it stood before it moved into ``ice_edge.views.health``, copied
verbatim (module globals ``vmc_instance`` / ``machine_instance`` /
``event_recorder`` / ``health_monitor`` / ``availability`` became parameters,
and ``holder_display_name(hold)`` is the injected ``holder_name`` callable).
The tests then assert the new builders yield the same values on a machine that
has one active product fault and a genuine PAY-104 pending sale.
"""

import asyncio

import pytest

from ice_common.config_model import ConfigModel
from ice_contracts.board.vending_machine import FaultCode
from ice_edge.app.machine import Machine
from ice_edge.config.config_store import add_product
from ice_edge.faults.availability import Availability
from ice_edge.ledger.event_recorder import EventRecorder
from ice_edge.ledger.session_store import SessionStore
from ice_edge.monitoring.health_monitor import HealthMonitor
from ice_edge.views.health import (
    build_fault_views,
    build_health_page_summary,
    build_health_summary,
    build_kpi_summary,
    fault_gate,
)
from ice_edge.web import context


async def _legacy_snapshot(
    *,
    vmc_instance,
    machine_instance,
    event_recorder,
    health_monitor,
    availability,
    holder_name,
) -> dict:
    """Verbatim copy of the pre-move ``health_snapshot`` computation."""
    if not vmc_instance:
        return {
            "vmc_missing": True,
            "status": None,
            "is_healthy": None,
            "issues": [],
            "active_faults": [],
            "payment_enabled": None,
            "payment_reasons": [],
            "machine_stopped": None,
            "maintenance": None,
        }

    status = vmc_instance.get_status()
    issues: list[str] = []
    active_faults = vmc_instance.active_faults()
    for f in active_faults:
        target = f["product"] or "machine"
        issues.append(f"{f['code']} {f['description']} ({target})")
        f["since_seconds"] = None

    if event_recorder:
        summary_24h = await asyncio.to_thread(event_recorder.get_summary, 24)
        errors_24h = summary_24h["errors"]
        if errors_24h > 0:
            issues.append(
                f"{errors_24h} error{'s' if errors_24h != 1 else ''} in last 24h"
            )

    if health_monitor:
        health = health_monitor.get_summary()
        ages = {f["key"]: f["since_seconds"] for f in health["active_faults"]}
        for f in active_faults:
            f["since_seconds"] = ages.get(f["key"])
        stale = [name for name, sub in health["subsystems"].items() if sub["stale"]]
        if stale:
            issues.append(f"Stale subsystems: {', '.join(stale)}")
        out_of_range = [
            loc for loc, temp in health["temperatures"].items() if not temp["in_range"]
        ]
        if out_of_range:
            issues.append(f"Temp issues: {', '.join(out_of_range)}")

    payment_enabled = availability.payment_enabled if availability else None
    payment_reasons = availability.payment_blocking_reasons() if availability else []

    is_healthy = len(issues) == 0 and payment_enabled is not False

    maintenance = None
    hold = machine_instance.maintenance_hold
    if hold is not None and any(
        f["code"] == FaultCode.SVC_102.value for f in active_faults
    ):
        maintenance = holder_name(hold)
        svc102_prefix = f"{FaultCode.SVC_102.value} "
        issues = [line for line in issues if not line.startswith(svc102_prefix)]

    return {
        "vmc_missing": False,
        "status": status,
        "is_healthy": is_healthy,
        "issues": issues,
        "active_faults": active_faults,
        "payment_enabled": payment_enabled,
        "payment_reasons": payment_reasons,
        "machine_stopped": (None if payment_enabled is None else not payment_enabled),
        "maintenance": maintenance,
    }


def _pay104_machine(tmp_path):
    """A machine carrying a genuine PAY-104 with pending-sale shares (the
    two-VMC crash-recovery setup of tests/edge/web/test_routes_health_pay104.py),
    plus one active product fault, a health monitor, an event recorder with an
    error row, and an Availability."""
    cfg = ConfigModel()
    add_product(cfg, "ICE-1", "Ice", 2.50)
    store_path = tmp_path / "session.json"
    machine1 = Machine(config=cfg)
    vmc1 = machine1.vmc
    vmc1.set_state("interacting_with_user")
    vmc1.selected_product = next(p for p in vmc1.products if p.sku == "ICE-1")
    vmc1.deposit_funds(2.00, payment_method="cash_bill")
    vmc1.deposit_funds(0.50, payment_method="card")
    vmc1.process_payment()
    assert vmc1.state == "dispensing"
    SessionStore(store_path).save(vmc1.snapshot())
    machine1.cancel_pending_tasks()

    machine = Machine(config=cfg)
    monitor = HealthMonitor()
    machine.set_health_monitor(monitor)
    avail = Availability()
    machine.set_availability(avail)
    recorder = EventRecorder(db_path=str(tmp_path / "events.db"))
    recorder.record("error")
    recorder.flush()
    machine.set_event_recorder(recorder)
    machine.set_session_store(SessionStore(store_path))  # raises PAY-104
    machine.vmc.raise_fault(FaultCode.ICE_301, sku="ICE-1")
    return machine, monitor, avail, recorder


@pytest.fixture
def scenario(tmp_path):
    machine, monitor, avail, recorder = _pay104_machine(tmp_path)
    codes = {f["code"] for f in machine.vmc.active_faults()}
    assert {"PAY-104", "ICE-301"} <= codes, "positive control: both faults active"
    assert machine.pending_sale_for_recovery() is not None
    yield machine, monitor, avail, recorder
    machine.cancel_pending_tasks()


def _freeze(monitor, monkeypatch):
    """HealthMonitor.get_summary() rounds ``since_seconds`` off the clock; pin
    one result so the legacy computation and the builder see the same ages."""
    frozen = monitor.get_summary()
    monkeypatch.setattr(monitor, "get_summary", lambda: frozen)
    return frozen


async def test_legacy_copy_equals_current_health_snapshot(scenario, monkeypatch):
    """The verbatim copy really is the code that ran: it agrees with
    ``context.health_snapshot`` (now a delegate) on this scenario."""
    machine, monitor, avail, recorder = scenario
    _freeze(monitor, monkeypatch)
    context.set_machine_instance(machine)
    context.set_health_monitor(monitor)
    context.set_availability(avail)
    context.set_event_recorder(recorder)
    legacy = await _legacy_snapshot(
        vmc_instance=machine.vmc,
        machine_instance=machine,
        event_recorder=recorder,
        health_monitor=monitor,
        availability=avail,
        holder_name=context.holder_display_name,
    )
    live = (await context.health_snapshot()).model_dump(mode="json")
    assert live == legacy
    assert any(
        "errors in last 24h" in line or "error in last 24h" in line
        for line in legacy["issues"]
    )
    assert len(legacy["active_faults"]) == 2
    assert legacy["is_healthy"] is False
    assert all(f["since_seconds"] is not None for f in legacy["active_faults"])


async def test_build_health_summary_equals_legacy_dict(scenario, monkeypatch):
    """``model_dump(mode="json")`` of the new HealthSummary equals the legacy
    dict exactly: HealthSummary carries exactly the legacy keys (cloud spec
    section 6.2), so nothing is dropped or projected on this path."""
    machine, monitor, avail, recorder = scenario
    _freeze(monitor, monkeypatch)
    legacy = await _legacy_snapshot(
        vmc_instance=machine.vmc,
        machine_instance=machine,
        event_recorder=recorder,
        health_monitor=monitor,
        availability=avail,
        holder_name=context.holder_display_name,
    )
    built = await build_health_summary(
        vmc=machine.vmc,
        event_recorder=recorder,
        health_monitor=monitor,
        availability=avail,
        maintenance_holder=lambda: context.holder_display_name(
            machine.maintenance_hold
        ),
    )
    assert built.model_dump(mode="json") == legacy
    assert built.maintenance is None
    assert {f.code for f in built.active_faults} == {"PAY-104", "ICE-301"}


async def test_build_health_summary_equals_legacy_under_maintenance_lease():
    """SVC-102 active with a real hold: the issues line is replaced by the
    holder's name, identically in both implementations."""
    machine = Machine(config=ConfigModel())
    try:
        granted, _ = machine.lease.begin_maintenance("u-tech", "s-1")
        assert granted
        legacy = await _legacy_snapshot(
            vmc_instance=machine.vmc,
            machine_instance=machine,
            event_recorder=None,
            health_monitor=None,
            availability=None,
            holder_name=context.holder_display_name,
        )
        built = await build_health_summary(
            vmc=machine.vmc,
            event_recorder=None,
            health_monitor=None,
            availability=None,
            maintenance_holder=lambda: context.holder_display_name(
                machine.maintenance_hold
            ),
        )
        assert built.model_dump(mode="json") == legacy
        assert built.maintenance == "u-tech"
        assert not any(i.startswith("SVC-102 ") for i in built.issues)
    finally:
        machine.cancel_pending_tasks()


async def test_build_health_summary_without_vmc_equals_legacy():
    legacy = await _legacy_snapshot(
        vmc_instance=None,
        machine_instance=None,
        event_recorder=None,
        health_monitor=None,
        availability=None,
        holder_name=context.holder_display_name,
    )
    built = await build_health_summary(
        vmc=None,
        event_recorder=None,
        health_monitor=None,
        availability=None,
        maintenance_holder=lambda: None,
    )
    assert built.model_dump(mode="json") == legacy
    assert built.vmc_missing is True


def _legacy_faults_with_age(machine, monitor) -> list[dict]:
    """Verbatim copy of ``_faults_with_age`` in
    ``ice_edge.web.routes.health.faults`` (parameters instead of context
    globals; ``dom_key`` stubbed with the raw key)."""
    faults = machine.vmc.active_faults()
    ages = {
        f["key"]: f["since_seconds"] for f in monitor.get_summary()["active_faults"]
    }
    for f in faults:
        f["since_seconds"] = ages.get(f["key"])
        f["gate"] = fault_gate(f)
        f["dom_key"] = f["key"]
        f["pending_sale"] = (
            machine.pending_sale_for_recovery()
            if f["code"] == FaultCode.PAY_104.value
            else None
        )
    return faults


def test_build_fault_views_equals_projected_legacy_faults(scenario, monkeypatch):
    """The legacy fault dicts also carry the edge-only ``dom_key`` and the
    pending sale's ``name``/``slot``/``saved_at``; FaultView drops exactly
    those (cloud spec section 6.4). So the expected value is each legacy dict
    restricted to the FaultView fields, with ``pending_sale`` restricted to
    ``sku``/``price``/``methods``."""
    machine, monitor, _avail, _recorder = scenario
    _freeze(monitor, monkeypatch)
    legacy = _legacy_faults_with_age(machine, monitor)
    fields = "key sku product code severity scope description since_seconds gate"
    expected = []
    for f in legacy:
        row = {k: f[k] for k in fields.split()}
        ps = f["pending_sale"]
        row["pending_sale"] = (
            {k: ps[k] for k in ("sku", "price", "methods")} if ps else None
        )
        expected.append(row)
    # positive control: the keys being dropped really exist in the legacy shape
    pay = next(f for f in legacy if f["code"] == "PAY-104")
    assert {"name", "slot", "saved_at"} <= set(pay["pending_sale"])
    assert "dom_key" in pay

    built = build_fault_views(legacy)
    assert [v.model_dump(mode="json") for v in built] == expected
    assert next(v for v in built if v.code == "PAY-104").pending_sale is not None
    assert next(v for v in built if v.code == "ICE-301").gate == "fulfillment"


def test_build_fault_views_derives_gate_when_absent():
    views = build_fault_views(
        [
            {
                "key": "ICE-402",
                "sku": None,
                "product": None,
                "code": "ICE-402",
                "severity": "critical",
                "scope": "machine",
                "description": "d",
            }
        ]
    )
    assert views[0].gate == "safety"
    assert views[0].since_seconds is None
    assert views[0].pending_sale is None


async def test_build_kpi_summary_projects_the_cards_keys(scenario):
    _machine, _monitor, _avail, recorder = scenario
    raw_summary = recorder.get_summary(24)
    raw_average = recorder.get_historical_average(24)
    kpi = await build_kpi_summary(recorder)
    keys = ("money_in", "products_out", "vends_failed", "errors", "uptime_pct")
    assert kpi.summary.model_dump() == {k: raw_summary[k] for k in keys}
    akeys = ("money_in", "products_out", "errors", "uptime_pct")
    assert kpi.average.model_dump() == {k: raw_average[k] for k in akeys}
    assert kpi.summary.errors >= 1


async def test_build_kpi_summary_without_recorder_is_all_none():
    kpi = await build_kpi_summary(None)
    assert kpi.summary is None
    assert kpi.average is None


def test_build_health_page_summary_projects_monitor_summary(scenario):
    _machine, monitor, _avail, _recorder = scenario
    raw = monitor.get_summary()
    rows = {
        "a": {"alive": True, "stale": False},
        "b": {"alive": True, "stale": True},
        "c": {"alive": False, "stale": False},
    }
    page = build_health_page_summary(raw, rows)
    assert page.mqtt_connected == raw["mqtt_connected"]
    assert page.vmc_state == raw["vmc_state"]
    keys = ("commit", "commit_short", "source", "uptime_seconds", "machine_id")
    assert page.vmc_build.model_dump() == {k: raw["vmc"][k] for k in keys}
    assert (page.subsystems_ok, page.subsystems_total) == (1, 3)


def test_build_health_page_summary_without_monitor():
    page = build_health_page_summary(None, {})
    assert page.model_dump() == {
        "mqtt_connected": None,
        "vmc_state": None,
        "vmc_build": None,
        "subsystems_ok": 0,
        "subsystems_total": 0,
    }
