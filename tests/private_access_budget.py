"""The private-access ratchet's data file. See `tests/test_no_private_access.py`
for what this budgets and how to lower a number: fix the tests in that
file until its count drops, run `uv run python tests/test_no_private_access.py`
to get the new count, then edit that one number here. Budgets only move
down; a file not listed here has an implicit budget of 0.
"""

BUDGET: dict[str, int] = {
    "tests/conftest.py": 1,
    "tests/fakes.py": 1,
    "tests/test_access.py": 7,
    "tests/test_command_dispatcher.py": 8,
    "tests/test_contracts.py": 2,
    "tests/test_dispensers_example.py": 2,
    "tests/test_dispensers_service.py": 1,
    "tests/test_dispensers_validation.py": 1,
    "tests/test_event_recorder.py": 13,
    "tests/test_fault_registry.py": 1,
    "tests/test_health_monitor.py": 19,
    "tests/test_integration_e2e.py": 1,
    "tests/test_inventory_manager.py": 1,
    "tests/test_maintenance_standby.py": 1,
    "tests/test_mqtt.py": 19,
    "tests/test_notifier.py": 6,
    "tests/test_outputs.py": 1,
    "tests/test_report_scheduler.py": 6,
    "tests/test_reports.py": 4,
    "tests/test_routes_tests.py": 5,
    "tests/test_simulator_base.py": 40,
    "tests/test_simulator_ice_maker.py": 28,
    "tests/test_simulator_mdb.py": 23,
    "tests/test_simulator_vending.py": 53,
    "tests/test_test_sale_runner.py": 1,
    "tests/test_vmc_flows.py": 13,
    "tests/test_web_routes.py": 12,
}
