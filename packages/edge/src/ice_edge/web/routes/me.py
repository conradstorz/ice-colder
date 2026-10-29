"""The You level: who is signed in, which machine, and what they may do."""

from fastapi import APIRouter, Depends, Request

from ice_contracts.access import PERMISSION_LABELS, Permission
from ice_edge.web import auth as web_auth
from ice_edge.web import context
from ice_ui.levels import LEVEL_ME
from ice_ui.templating import templates
from ice_ui.views.identity import MachineLabel

router = APIRouter()


@router.get(
    "/me",
    dependencies=[Depends(web_auth.require(Permission.view_status))],
)
async def get_me(request: Request):
    """Render the signed-in user's identity, machine and permissions."""
    cfg = context.config
    machine_label = (
        MachineLabel(common_name=cfg.physical.common_name, machine_id=cfg.machine_id)
        if cfg is not None
        else None
    )
    ctx = context.template_context(request, level=LEVEL_ME)
    ctx["logout_label"] = "Sign out"
    ctx["machine_label"] = machine_label
    ctx["permission_lines"] = [
        PERMISSION_LABELS[p] for p in Permission if p in ctx["perms"]
    ]
    return templates.TemplateResponse(request, "me.html", ctx)
