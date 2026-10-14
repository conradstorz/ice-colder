from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from ice_edge.access.access import Permission
from web_interface import auth as web_auth
from web_interface import context
from ice_ui.levels import LEVEL_TESTS
from web_interface.routes.health import _subsystem_summary
from web_interface.routes.tests_level.commands import contract_match, testable_commands
from web_interface.routes.tests_level.lease import _service_state
from ice_ui.templating import templates

router = APIRouter()


@router.get(
    "/tests",
    response_class=HTMLResponse,
    dependencies=[Depends(web_auth.require(Permission.run_tests))],
)
async def tests_level(request: Request):
    """One card per EXPECTED_SUBSYSTEMS entry plus the lease banner.

    Deliberately takes no lease (system-tests design §2.2, task brief):
    this handler never calls `begin_maintenance` -- it only reads the
    read-only `maintenance_hold` property.
    """
    subsystems = _subsystem_summary()
    cards = [
        {
            "name": name,
            "row": row,
            "match": contract_match(name, row),
            "testable_count": len(testable_commands(name, row["commands"])),
        }
        for name, row in subsystems.items()
    ]

    principal = web_auth.current_principal(request)
    return templates.TemplateResponse(
        request,
        "tests.html",
        context.template_context(
            request,
            level=LEVEL_TESTS,
            cards=cards,
            **_service_state(context.machine_instance, principal),
        ),
    )
