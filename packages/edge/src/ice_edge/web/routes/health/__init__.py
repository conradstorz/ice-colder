"""The six Health levels (spec §2): the /health landing page and its four
sub-levels (Subsystems, Faults, Availability, Logs), plus the per-subsystem
detail page and the fault-clear flow.

Data sources are exactly the ones the old routes/legacy.py `GET /health`
fragment used (health_monitor.get_summary(), HealthMonitor.empty_subsystem_
row(), availability.table()/.payment_enabled/.payment_blocking_reasons(),
vmc.active_faults()) -- this module only splits that single fragment's
content across the six levels; see .superpowers/sdd/part2/task-6-brief.md.
"""

from fastapi import APIRouter

from ice_edge.web.routes.health import (
    availability,
    faults,
    logs,
    overview,
    recovery,
    subsystems,
)
from ice_edge.views.health import fault_gate
from ice_edge.web.routes.health.faults import dom_safe_key
from ice_edge.web.routes.health.subsystems import _subsystem_summary

__all__ = ["_subsystem_summary", "dom_safe_key", "fault_gate", "router"]

router = APIRouter()

# No sub-router's parametrised path shadows another's static one
# (/health/subsystems/{name} has no static sibling; /health/faults/{key}/clear
# and /health/faults/PAY-104/record-sale|discard differ after the key), so the
# original file order is kept.
router.include_router(overview.router)
router.include_router(subsystems.router)
router.include_router(faults.router)
router.include_router(recovery.router)
router.include_router(availability.router)
router.include_router(logs.router)
