from ice_contracts.board.vending_machine import EXPECTED_SUBSYSTEMS, FaultCode
from ice_edge.monitoring.health_monitor import HealthMonitor
from ice_edge.views.health import faults_with_age, subsystem_rows


def test_faults_with_age_without_vmc_is_empty():
    assert faults_with_age(None, None, lambda: None) == []


def test_faults_with_age_adds_gate_and_no_pending_sale(machine):
    machine.vmc.raise_fault(FaultCode.ICE_301, sku="ICE-1")
    faults = faults_with_age(machine.vmc, None, lambda: {"sku": "x"})
    fault = next(f for f in faults if f["code"] == "ICE-301")
    assert fault["gate"] == "fulfillment"
    assert fault["since_seconds"] is None
    assert fault["pending_sale"] is None


def test_faults_with_age_joins_monitor_age(machine):
    monitor = HealthMonitor()
    machine.set_health_monitor(monitor)
    machine.vmc.raise_fault(FaultCode.ICE_301, sku="ICE-1")
    faults = faults_with_age(machine.vmc, monitor, lambda: None)
    ages = {
        f["key"]: f["since_seconds"] for f in monitor.get_summary()["active_faults"]
    }
    assert ages, "positive control: monitor tracks the fault"
    assert faults[0]["since_seconds"] is not None


def test_subsystem_rows_without_monitor_are_empty_rows():
    rows = subsystem_rows(None)
    assert list(rows) == list(EXPECTED_SUBSYSTEMS)
    assert all(r == HealthMonitor.empty_subsystem_row() for r in rows.values())
