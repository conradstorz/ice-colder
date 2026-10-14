"""The Tests level (system-tests design, spec §3): discovery + action routes.

Task 13 split at the discovery/actions seam (task-13-brief.md): Task 13a
built `GET /tests`, `GET /tests/{subsystem}` and `GET /tests/log` --
read-only, and entering none of them takes the maintenance lease (see
`.superpowers/sdd/task-13a-report.md` for the interface it hands off).
Task 13b (this revision) adds every action route: `GET`/`POST /tests/sale`,
`POST /tests/{subsystem}/{command}`, `POST /tests/runs/{run_id}/verdict`,
`POST /tests/run-all`, `POST /tests/end`, `POST /tests/takeover`. See
`.superpowers/sdd/task-13b-report.md` for the full account of this half.
"""

from fastapi import APIRouter

from ice_edge.web.routes.tests_level import commands, lease, log, overview, sale
from ice_edge.web.routes.tests_level.commands import (
    POWER_CYCLE_DWELL_DEFAULT,
    POWER_CYCLE_DWELL_RANGE,
    WATER_VALVE_SECONDS_RANGE,
    _run_command,
    contract_match,
    testable_commands,
)
from ice_edge.web.routes.tests_level.log import recent_test_runs

__all__ = [
    "POWER_CYCLE_DWELL_DEFAULT",
    "POWER_CYCLE_DWELL_RANGE",
    "WATER_VALVE_SECONDS_RANGE",
    "_run_command",
    "contract_match",
    "recent_test_runs",
    "router",
    "testable_commands",
]

router = APIRouter()

# Order matters: commands.router holds the parametrised /tests/{subsystem}
# and /tests/{subsystem}/{command:path} routes, which would otherwise capture
# /tests/log, /tests/sale, /tests/standby/confirm, /tests/run-all, /tests/end,
# /tests/takeover, and /tests/runs/{run_id}/verdict. It goes last.
router.include_router(overview.router)
router.include_router(log.router)
router.include_router(sale.router)
router.include_router(lease.router)
router.include_router(commands.router)
