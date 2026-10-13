import time

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse

from services.access import Permission
from web_interface import auth as web_auth
from web_interface import context
from web_interface.templating import templates


router = APIRouter()


# Busy refusals from MaintenanceLease.begin_maintenance (the OPPORTUNISTIC
# lease) that now point the operator at the standby button instead of
# their own raw wording -- "held by <id>" is deliberately NOT in this map
# (system-tests design §2.2a / Task 3 brief): a lease already held by someone else is a
# different situation than a busy-but-unheld machine, and keeps its own
# wording unchanged.
_BUSY_REFUSAL_WORDING = "machine is busy — take it out of service first"


_BUSY_REFUSALS = frozenset({"machine is mid-sale", "credit is still on the machine"})


# --- Task 3 (system-tests design §2.2a): the service-state card --------
#
# The card that replaced the old plain maintenance-lease banner
# (partials/tests_hold_banner.html): "Take out of service" when no lease
# is held, or "Out of service since HH:MM, held by <name>" plus
# Return-to-service/Take-over once one is. Every route that shows or
# re-renders it -- GET /tests, GET /tests/standby/confirm, POST
# /tests/standby, POST /tests/end, POST /tests/takeover -- builds its
# context through _service_state so none of them can drift from another
# about what the card looks like for the same VMC state.


def _service_state(
    machine,
    principal: web_auth.Principal | None,
    *,
    confirming: bool = False,
    refusal: str | None = None,
) -> dict:
    """The render context for partials/tests_hold_banner.html.

    ``hold`` is the live MaintenanceHold or None; ``held_by_me`` is only
    True when *principal* is not None and its session matches the hold's
    holder (never inferred from a None principal). ``holder_name`` resolves
    through context.holder_display_name (shared with the Home hero's
    `maintenance` field so both agree on the same name for the same hold).
    ``started_at_hhmm`` is computed here, in Python, rather than adding a
    Jinja time filter for one caller. ``confirming``/``refusal`` are passed
    straight through for the templates that need them (GET .../confirm and
    POST /tests/standby respectively); every other caller leaves them at
    their defaults, which the template ignores whenever a lease is held.
    """
    hold = machine.maintenance_hold if machine is not None else None
    held_by_me = bool(
        hold is not None
        and principal is not None
        and hold.holder_session_id == principal.session.id
    )
    started_at_hhmm = (
        time.strftime("%H:%M", time.localtime(hold.started_at)) if hold else None
    )
    return {
        "hold": hold,
        "held_by_me": held_by_me,
        "holder_name": context.holder_display_name(hold),
        "started_at_hhmm": started_at_hhmm,
        "confirming": confirming,
        "refusal": refusal,
    }


def _acquire_lease_or_refusal(machine, principal: web_auth.Principal) -> str | None:
    """Take the maintenance lease for *principal*'s session, or return why
    not, WITHOUT ever touching the dispatcher (system-tests design §2.2,
    task brief: "takes the lease or returns the refusal inline").

    Three cases:
      - No lease held: try to grant one (`MaintenanceLease.begin_maintenance`);
        refused for one of its two busy reasons ("machine is mid-sale",
        "credit is still on the machine") is remapped to
        `_BUSY_REFUSAL_WORDING` ("machine is busy — take it out of service
        first", Task 3, system-tests design §2.2a) so the refusal itself
        points the operator at the standby button rather than leaving them
        to guess what to do about a machine that just won't take the
        opportunistic lease.
      - Lease already held by THIS session (a second command run in the
        same maintenance visit): a no-op -- returns None (proceed) without
        calling begin_maintenance again, which would otherwise refuse with
        a spurious "held by <self>" (`begin_maintenance` refuses whenever
        any lease exists, regardless of who holds it).
      - Lease held by a DIFFERENT session: refused, "held by <holder>",
        matching `begin_maintenance`'s own wording for the same case --
        deliberately NOT remapped, since "someone else has it" is a
        different situation than "the machine is busy".

    Returns None exactly when the caller's session now holds the lease
    (freshly granted or pre-existing) and it is safe to proceed to
    `machine.lease.test_run()`.
    """
    session_id = principal.session.id
    hold = machine.maintenance_hold
    if hold is not None and hold.holder_session_id == session_id:
        return None
    if hold is not None:
        return f"held by {hold.holder_user_id}"
    granted, reason = machine.lease.begin_maintenance(principal.user.id, session_id)
    if granted:
        return None
    return _BUSY_REFUSAL_WORDING if reason in _BUSY_REFUSALS else reason


@router.get(
    "/tests/standby/confirm",
    response_class=HTMLResponse,
    dependencies=[Depends(web_auth.require(Permission.run_tests))],
)
async def tests_standby_confirm(
    request: Request, confirming: str | None = Query(default=None)
):
    """confirm_button.html's confirm_url contract (Task 3, matching
    routes/health/faults.py's fault_clear_confirm): absent or anything but
    the literal string "false" renders the confirming (Confirm/Cancel)
    state; "false" renders the plain first-tap button. Re-renders the
    WHOLE service-state card (#tests-hold), not just the button --
    every route that touches this card swaps that same outer id.

    Two segments deep (/tests/standby/confirm), so it can never
    collide with the single-segment GET /tests/{subsystem} catch-all
    below regardless of registration order -- registered ahead of it
    anyway, matching this file's "literal paths before catch-alls"
    convention (see tests_subsystem's own ORDERING WARNING).
    """
    principal = web_auth.current_principal(request)
    return templates.TemplateResponse(
        request,
        "partials/tests_hold_banner.html",
        context.template_context(
            request,
            **_service_state(
                context.machine_instance,
                principal,
                confirming=(confirming != "false"),
            ),
        ),
    )


# --- Task 13b: action routes ---------------------------------------


@router.post(
    "/tests/standby",
    response_class=HTMLResponse,
    dependencies=[
        Depends(web_auth.require(Permission.run_tests)),
        Depends(context.require_htmx),
    ],
)
async def tests_standby(request: Request):
    """Take the machine out of service for the caller's whole web
    session (`MaintenanceLease.begin_standby`, Task 3 / system-tests
    design §2.2a): refunds any credit on the machine, cancels a live
    customer sale, and grants the lease with `standby=True` -- no
    idle-timer release; the lease's own session-liveness sweep (wired
    via Machine.set_session_liveness in main.py) is what ends it if
    the tech simply walks away or locks the tablet.

    Calls `machine.lease.begin_standby` directly rather than going
    through `_acquire_lease_or_refusal` -- that helper's busy-wording
    remap exists to point OTHER callers (a command run, a simulated
    sale) at THIS button; begin_standby's own refusal wording ("vend
    finishing, tap again" mid-dispense, "held by <id>" for a different
    session's lease) is already the right thing to show here,
    verbatim, inside the card (Task 3 requirement).
    """
    principal = web_auth.current_principal(request)
    machine = context.machine_instance
    if machine is None:
        refusal = "VMC not initialized"
    else:
        granted, reason = machine.lease.begin_standby(
            principal.user.id, principal.session.id
        )
        refusal = None if granted else reason
    return templates.TemplateResponse(
        request,
        "partials/tests_hold_banner.html",
        context.template_context(
            request, **_service_state(machine, principal, refusal=refusal)
        ),
    )


@router.post(
    "/tests/end",
    response_class=HTMLResponse,
    dependencies=[
        Depends(web_auth.require(Permission.run_tests)),
        Depends(context.require_htmx),
    ],
)
async def tests_end(request: Request):
    """Release the CALLER's OWN lease, once nothing is in flight
    (`MaintenanceLease.end_maintenance` already enforces both: session
    match and runs_in_flight == 0, deferring via release_requested
    otherwise). Re-renders the same #tests-hold banner tests.html swaps
    this into (outerHTML) -- empty when no lease remains, so the
    banner div is removed entirely, matching tests.html's own
    `{% if hold %}` guard.
    """
    principal = web_auth.current_principal(request)
    machine = context.machine_instance
    if machine is not None:
        machine.lease.end_maintenance(principal.session.id)
    return templates.TemplateResponse(
        request,
        "partials/tests_hold_banner.html",
        context.template_context(request, **_service_state(machine, principal)),
    )


@router.post(
    "/tests/takeover",
    response_class=HTMLResponse,
    dependencies=[
        Depends(web_auth.require(Permission.run_tests)),
        Depends(context.require_htmx),
    ],
)
async def tests_takeover(request: Request):
    """Transfer an idle, run-free lease to the caller
    (`MaintenanceLease.take_over_maintenance`, system-tests design
    §2.2) -- refused (lease unchanged) while a run is in flight or
    before the 60s idle threshold. Re-renders #tests-hold either way.
    """
    principal = web_auth.current_principal(request)
    machine = context.machine_instance
    if machine is not None:
        machine.lease.take_over_maintenance(principal.user.id, principal.session.id)
    return templates.TemplateResponse(
        request,
        "partials/tests_hold_banner.html",
        context.template_context(request, **_service_state(machine, principal)),
    )
