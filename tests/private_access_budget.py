"""The private-access ratchet's data file. See `tests/meta/test_no_private_access.py`
for what this budgets and how to lower a number: fix the tests in that
file until its count drops, run `uv run python tests/meta/test_no_private_access.py`
to get the new count, then edit that one number here. Budgets only move
down; a file not listed here has an implicit budget of 0.
"""

BUDGET: dict[str, int] = {
    "tests/fakes.py": 1,
    "tests/edge/access/test_access.py": 7,
    "tests/edge/hal/test_command_dispatcher.py": 4,
    "tests/contracts/test_contracts.py": 2,
    "tests/edge/vend/test_dispensers_example.py": 2,
    "tests/edge/vend/test_dispensers_service.py": 1,
    "tests/edge/vend/test_dispensers_validation.py": 1,
    "tests/edge/ledger/test_event_recorder.py": 13,
    "tests/edge/monitoring/test_health_monitor.py": 19,
    "tests/edge/monitoring/test_inventory_manager.py": 1,
    "tests/edge/sale/test_maintenance_standby.py": 1,
    "tests/common/test_mqtt.py": 19,
    "tests/edge/notify/test_notifier.py": 1,
    "tests/edge/reporting/test_report_scheduler.py": 5,
    "tests/edge/web/test_route_context.py": 2,
    "tests/edge/web/test_routes_tests.py": 3,
    "tests/sims/test_simulator_base.py": 38,
    "tests/sims/test_simulator_ice_maker.py": 28,
    "tests/sims/test_simulator_mdb.py": 23,
    "tests/sims/test_simulator_vending.py": 47,
    "tests/edge/sale/test_test_sale_runner.py": 1,
    "tests/edge/sale/test_vmc_flows.py": 2,
    "tests/edge/web/test_web_routes.py": 5,
}
