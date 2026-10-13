"""The reorganisation move map: data only. All logic lives in codemod.py.

Every row carries the phase (PR) that executes it. Module rows are old dotted
name -> new dotted name; ``package=True`` marks a package row, whose children
move with it unless a more specific row claims them.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ModuleMove:
    phase: int
    old: str
    new: str
    package: bool


@dataclass(frozen=True)
class FileMove:
    phase: int
    old: str
    new: str


@dataclass(frozen=True)
class TestMove:
    __test__ = False  # not a pytest class despite the name

    phase: int
    file: str
    dest_dir: str


MODULE_MOVES: list[ModuleMove] = [
    ModuleMove(2, "contracts.common", "ice_contracts.board.common", False),
    ModuleMove(
        2, "contracts.vending_machine", "ice_contracts.board.vending_machine", False
    ),
    ModuleMove(
        2, "contracts.ice_maker_monitor", "ice_contracts.board.ice_maker_monitor", False
    ),
    ModuleMove(2, "contracts.generate", "ice_contracts.generate", False),
    ModuleMove(2, "contracts", "ice_contracts", True),
    ModuleMove(2, "services.mqtt_messages", "ice_contracts.board.mqtt_messages", False),
    ModuleMove(
        2, "services.dispenser_schema", "ice_contracts.board.dispenser_schema", False
    ),
    ModuleMove(2, "services.build_info", "ice_common.build_info", False),
    ModuleMove(2, "services.paths", "ice_common.paths", False),
    ModuleMove(2, "services.logging_setup", "ice_common.logging_setup", False),
    ModuleMove(2, "services.task_supervisor", "ice_common.task_supervisor", False),
    ModuleMove(2, "services.task_lifecycle", "ice_common.task_lifecycle", False),
    ModuleMove(2, "services.mqtt_client", "ice_common.mqtt_client", False),
    ModuleMove(2, "config.config_model", "ice_common.config_model", False),
    ModuleMove(3, "web_interface.levels", "ice_ui.levels", False),
    ModuleMove(3, "web_interface.filters", "ice_ui.filters", False),
    ModuleMove(3, "web_interface.templating", "ice_ui.templating", False),
    ModuleMove(
        3, "web_interface.subsystem_window", "ice_ui.views.subsystem_window", False
    ),
    ModuleMove(4, "main", "ice_edge.__main__", False),
    ModuleMove(4, "controller.machine", "ice_edge.app.machine", False),
    ModuleMove(4, "services.startup_config", "ice_edge.app.startup_config", False),
    ModuleMove(
        4, "services.startup_dispensers", "ice_edge.app.startup_dispensers", False
    ),
    ModuleMove(4, "services.startup_recovery", "ice_edge.app.startup_recovery", False),
    ModuleMove(4, "web_interface.server", "ice_edge.web.server", False),
    ModuleMove(4, "web_interface.auth", "ice_edge.web.auth", False),
    ModuleMove(4, "web_interface.context", "ice_edge.web.context", False),
    ModuleMove(4, "web_interface.routes", "ice_edge.web.routes", True),
    ModuleMove(4, "web_interface", "ice_edge.web", True),
    ModuleMove(4, "controller.vmc", "ice_edge.sale.vmc", False),
    ModuleMove(4, "controller.sale_context", "ice_edge.sale.sale_context", False),
    ModuleMove(4, "controller.session_timer", "ice_edge.sale.session_timer", False),
    ModuleMove(4, "controller.outputs", "ice_edge.sale.outputs", False),
    ModuleMove(4, "controller.sale_journal", "ice_edge.sale.sale_journal", False),
    ModuleMove(4, "controller.test_sale", "ice_edge.sale.test_sale", False),
    ModuleMove(
        4, "controller.maintenance_lease", "ice_edge.sale.maintenance_lease", False
    ),
    ModuleMove(4, "services.fsm_control", "ice_edge.sale.fsm_control", False),
    ModuleMove(4, "controller.dispense_cycle", "ice_edge.vend.dispense_cycle", False),
    ModuleMove(4, "controller.dispenser_gate", "ice_edge.vend.dispenser_gate", False),
    ModuleMove(4, "services.dispensers", "ice_edge.vend.dispensers", False),
    ModuleMove(4, "services.dispensers_doc", "ice_edge.vend.dispensers_doc", False),
    ModuleMove(
        4, "controller.refund_protocol", "ice_edge.payment.refund_protocol", False
    ),
    ModuleMove(
        4,
        "services.payment_gateway_manager",
        "ice_edge.payment.payment_gateway_manager",
        False,
    ),
    ModuleMove(4, "services.reports", "ice_edge.reporting.reports", False),
    ModuleMove(
        4, "services.report_scheduler", "ice_edge.reporting.report_scheduler", False
    ),
    ModuleMove(4, "controller.fault_registry", "ice_edge.faults.fault_registry", False),
    ModuleMove(4, "controller.fault_service", "ice_edge.faults.fault_service", False),
    ModuleMove(4, "services.availability", "ice_edge.faults.availability", False),
    ModuleMove(4, "services.mailer", "ice_edge.notify.mailer", False),
    ModuleMove(4, "services.notifier", "ice_edge.notify.notifier", False),
    ModuleMove(
        4, "services.health_monitor", "ice_edge.monitoring.health_monitor", False
    ),
    ModuleMove(
        4, "services.inventory_manager", "ice_edge.monitoring.inventory_manager", False
    ),
    ModuleMove(4, "services.access", "ice_edge.access.access", False),
    ModuleMove(4, "services.auth_policy", "ice_edge.access.auth_policy", False),
    ModuleMove(4, "controller.escrow_ledger", "ice_edge.ledger.escrow_ledger", False),
    ModuleMove(4, "services.event_recorder", "ice_edge.ledger.event_recorder", False),
    ModuleMove(4, "services.session_store", "ice_edge.ledger.session_store", False),
    ModuleMove(
        4, "controller.session_recovery", "ice_edge.ledger.session_recovery", False
    ),
    ModuleMove(
        4,
        "services.display_controller",
        "ice_edge.customer_ui.display_controller",
        False,
    ),
    ModuleMove(4, "controller.mqtt_inbound", "ice_edge.hal.mqtt_inbound", False),
    ModuleMove(
        4, "services.command_dispatcher", "ice_edge.hal.command_dispatcher", False
    ),
    ModuleMove(4, "services.config_store", "ice_edge.config.config_store", False),
    ModuleMove(4, "controller.task_runner", "ice_edge.supervisor.task_runner", False),
    ModuleMove(4, "simulators", "ice_sims", True),
]

PACKAGE_ROOTS: dict[str, str] = {
    "ice_contracts": "packages/contracts/src/ice_contracts",
    "ice_common": "packages/common/src/ice_common",
    "ice_ui": "packages/ui/src/ice_ui",
    "ice_edge": "packages/edge/src/ice_edge",
    "ice_cloud": "packages/cloud/src/ice_cloud",
    "ice_sims": "packages/sims/src/ice_sims",
}

# A trailing slash means "the whole tree".
FILE_MOVES: list[FileMove] = [
    FileMove(3, "web_interface/templates/", "packages/ui/src/ice_ui/templates/"),
    FileMove(3, "web_interface/static/", "packages/ui/src/ice_ui/static/"),
    FileMove(3, "web_interface/tailwind.config.js", "packages/ui/tailwind.config.js"),
    FileMove(3, "web_interface/tailwind.input.css", "packages/ui/tailwind.input.css"),
]

_TEST_DIRS: dict[str, tuple[str, ...]] = {
    "tests/meta/": (
        "test_no_private_access",
        "test_routes_shape",
        "test_vmc_complexity",
        "test_env_isolation",
        "test_skip_policy",
        "test_fakes",
        "test_paths",
        "test_codemod",
        "test_workspace",
        "test_import_contracts",
        "test_layout",
        "test_ui_package",
    ),
    "tests/contracts/": (
        "test_contract_schemas",
        "test_contracts",
        "test_contracts_vending",
        "test_dispenser_schema",
        "test_mqtt_messages_validation",
    ),
    "tests/common/": (
        "test_async_lifecycle",
        "test_build_info",
        "test_config_model",
        "test_logging_setup",
        "test_mqtt",
    ),
    "tests/ui/": (
        "test_filters",
        "test_levels",
        "test_subsystem_window",
        "test_templating",
        "test_static_css",
        "test_feedback_assets",
    ),
    "tests/sims/": (
        "test_simulator_base",
        "test_simulator_ice_maker",
        "test_simulator_mdb",
        "test_simulator_vending",
    ),
    "tests/integration/": ("test_integration_e2e",),
    "tests/edge/app/": (
        "test_first_run",
        "test_machine",
        "test_startup_dispensers",
        "test_startup_policy",
    ),
    "tests/edge/sale/": (
        "test_vmc_fsm",
        "test_vmc_flows",
        "test_vmc_observers",
        "test_vmc_dispense_profiles",
        "test_sale_context",
        "test_sale_journal",
        "test_session_timer",
        "test_maintenance_lease",
        "test_maintenance_standby",
        "test_test_sale_runner",
        "test_fsm_control",
        "test_outputs",
    ),
    "tests/edge/vend/": (
        "test_dispense_cycle",
        "test_dispenser_gate",
        "test_dispensers_cli",
        "test_dispensers_example",
        "test_dispensers_service",
        "test_dispensers_validation",
    ),
    "tests/edge/payment/": ("test_refund_protocol",),
    "tests/edge/faults/": (
        "test_availability",
        "test_fault_registry",
        "test_fault_service",
    ),
    "tests/edge/monitoring/": ("test_health_monitor", "test_inventory_manager"),
    "tests/edge/notify/": ("test_mailer", "test_notifier"),
    "tests/edge/access/": ("test_access", "test_auth_policy"),
    "tests/edge/ledger/": (
        "test_escrow_ledger",
        "test_event_recorder",
        "test_session_recovery",
        "test_session_store",
    ),
    "tests/edge/reporting/": ("test_reports", "test_report_scheduler"),
    "tests/edge/customer_ui/": ("test_display_controller",),
    "tests/edge/hal/": ("test_command_dispatcher", "test_mqtt_inbound"),
    "tests/edge/config/": ("test_config_store",),
    "tests/edge/supervisor/": ("test_task_runner",),
    "tests/edge/web/": (
        "test_route_context",
        "test_routes_controls",
        "test_routes_health",
        "test_routes_health_pay104",
        "test_routes_inventory",
        "test_routes_inventory_collect",
        "test_routes_products",
        "test_routes_reports",
        "test_routes_reports_levels",
        "test_routes_settings",
        "test_routes_settings_reports",
        "test_routes_tests",
        "test_routes_tests_level",
        "test_routes_users",
        "test_web_auth",
        "test_web_routes",
        "test_dashboard_v2_home_browser",
        "test_dashboard_v2_boosted_nav_browser",
        "test_users_codes_pin_browser",
        "test_touch_feedback_browser",
    ),
}

# `file` is the file name including ".py".
TEST_MOVES: list[TestMove] = [
    TestMove(4, name + ".py", dest)
    for dest, names in _TEST_DIRS.items()
    for name in names
]

TEXT_REWRITE_FILES: list[str] = [
    "CLAUDE.md",
    "README.md",
    "Dockerfile",
    "docker-compose.yml",
    "docker/docker-compose.yml",
    "docker/docker-compose.prod.yml",
    ".github/workflows/ci.yml",
    ".vscode/launch.json",
    ".vscode/tasks.json",
    ".dockerignore",
    ".gitignore",
    "packages/ui/tailwind.config.js",
    "docs/contracts/*/CONTRACT.md",
    "tests/private_access_budget.py",
    "pyproject.toml",
    "packages/*/pyproject.toml",
]


def module_file(dotted: str, *, package: bool) -> Path:
    """Repo-relative file of a *new* dotted module, resolved through PACKAGE_ROOTS."""
    root, _, rest = dotted.partition(".")
    if root not in PACKAGE_ROOTS:
        raise ValueError(f"{dotted!r} does not start with a package root")
    base = Path(PACKAGE_ROOTS[root])
    parts = rest.split(".") if rest else []
    if package:
        return base.joinpath(*parts, "__init__.py")
    if not parts:
        raise ValueError(f"{dotted!r} is a bare package root, not a module")
    return base.joinpath(*parts[:-1], parts[-1] + ".py")


def old_module_file(dotted: str, *, package: bool) -> Path:
    """Repo-relative file of an *old* dotted module at today's repo-root location."""
    parts = dotted.split(".")
    if package:
        return Path(*parts, "__init__.py")
    return Path(*parts[:-1], parts[-1] + ".py")
