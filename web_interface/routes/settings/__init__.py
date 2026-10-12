"""The Settings levels: /settings and its six sub-levels — Machine,
Contacts, Payments, Comms, MQTT and Web (spec §2, §5).

Two of the six pages never existed before this task: /config/payments and
/config/comms (routes/legacy.py) were left without working templates in
part 1, and their coverage in tests/test_web_routes.py has been
@pytest.mark.skip'd ever since. This module's /settings/payments and
/settings/comms are the real, working replacements — see
.superpowers/sdd/part2/task-13-brief.md, executor resolution 2, for why
those two skips could not be deleted from this task (tests/test_web_routes.py
is owned by another task in this wave).
"""

from fastapi import APIRouter

from web_interface.routes.settings import (
    comms,
    contacts,
    machine,
    mqtt,
    overview,
    payments,
    reports,
    web,
)
from web_interface.routes.settings.secrets import SECRET_MASK

__all__ = ["SECRET_MASK", "router"]

router = APIRouter()

# No parametrised paths exist in any sub-router, so nothing can shadow another
# sub-router's static path and the original file order is kept.
router.include_router(overview.router)
router.include_router(machine.router)
router.include_router(contacts.router)
router.include_router(payments.router)
router.include_router(comms.router)
router.include_router(mqtt.router)
router.include_router(web.router)
router.include_router(reports.router)
