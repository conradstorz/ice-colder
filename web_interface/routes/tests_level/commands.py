"""Two things this module deliberately reuses rather than re-derives, per the
task brief's "fix applied in one file but not its siblings" warning:

- `_subsystem_summary` is imported from `web_interface.routes.health`
  (read-only reuse -- health.py is not in this task's file list to modify)
  rather than copied, so the two levels can never compute a subsystem's
  alive/firmware/contract_version/commands rows differently.
- `testable_commands` below (advertised-∩-allowlist) is the ONE place that
  intersection is computed. Task 13b's POST re-checks the allowlist by
  calling this same function again with the request's own subsystem/
  command, never by re-deriving the intersection a second way.
"""

import time
from uuid import uuid4

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse

from contracts.common import TESTABLE_COMMANDS
from contracts.ice_maker_monitor import CONTRACT_VERSION as ICE_MAKER_CONTRACT_VERSION
from contracts.vending_machine import CONTRACT_VERSION as VENDING_CONTRACT_VERSION
from contracts.vending_machine import EXPECTED_SUBSYSTEMS
from services.access import Permission
from services.command_dispatcher import CommandTimeout, CompletionTimeout
from web_interface import auth as web_auth
from web_interface import context
from web_interface.levels import LEVEL_TESTS, Level
from web_interface.routes.health import _subsystem_summary
from web_interface.routes.tests_level.lease import _acquire_lease_or_refusal
from web_interface.templating import templates


router = APIRouter()


# Standard commands every subsystem answers (system-tests design §1.2),
# rendered in their own group on /tests/{subsystem}; every other testable
# command is an "actuator" command (§1.3).
AUTOMATIC_COMMANDS: frozenset[str] = frozenset({"ping", "self_test", "force_report"})


# Which contract a subsystem's reported contract_version is compared
# against for the /tests card's "contract match" field. "vending" and
# "mdb" both speak the vending-machine contract; "ice_maker" speaks its
# own -- see contracts/vending_machine.py's EXPECTED_SUBSYSTEMS and
# contracts/ice_maker_monitor.py's own CONTRACT_VERSION.
#
# No existing code computes this: web_interface/routes/health.py (the
# closest sibling, and the one the task brief points at for "reuse its
# contract-mismatch determination") only ever *displays* the raw
# contract_version string -- it has no mismatch predicate to reuse. This
# is therefore the one place that determination lives, so a later change
# to health.py (or a part of this program) can import and reuse THIS
# rather than growing a second, possibly-diverging copy. See
# task-13a-report.md.
_EXPECTED_CONTRACT_VERSION: dict[str, str] = {
    "vending": VENDING_CONTRACT_VERSION,
    "mdb": VENDING_CONTRACT_VERSION,
    "ice_maker": ICE_MAKER_CONTRACT_VERSION,
}


def contract_match(subsystem: str, row: dict) -> bool | None:
    """Whether *row* (a _subsystem_summary() row) reports the contract
    version this VMC build expects for *subsystem*.

    None -- not a mismatch -- when the subsystem has never published a
    capabilities document (row["contract_version"] is None) or *subsystem*
    is not one this VMC knows how to compare (not in
    _EXPECTED_CONTRACT_VERSION); True/False otherwise.
    """
    expected = _EXPECTED_CONTRACT_VERSION.get(subsystem)
    reported = row.get("contract_version")
    if expected is None or reported is None:
        return None
    return reported == expected


def testable_commands(subsystem: str, advertised) -> frozenset[str]:
    """Advertised ∩ allowlist (system-tests design §1.3).

    `contracts.common.TESTABLE_COMMANDS` is the server-side allowlist;
    `advertised` is a subsystem's `SubsystemCapabilities.commands` (which
    may list control commands, e.g. `refund`, that must never get a test
    button). A command exists as a test only when it is in both sets.
    Task 13b's `POST /tests/{subsystem}/{command}` re-checks the allowlist
    by calling this exact function again with the live, freshly-read
    advertised list -- never by trusting what a client claims, and never
    by re-deriving the intersection a second way.
    """
    allowed = TESTABLE_COMMANDS.get(subsystem, frozenset())
    return allowed & set(advertised or [])


# Param-widget ranges for the actuator commands (system-tests design §1.3):
# `water_valve`'s `seconds` and `power_cycle`'s `dwell_seconds`. These
# literals mirror contracts/common.py's COMMAND_PARAM_VALIDATORS exactly --
# that module is outside this task's file list (see task-13a-report.md),
# and it exposes validator *functions*, not reusable range constants, so a
# second copy of the bounds is unavoidable here. Kept from silently
# drifting apart by tests/test_routes_tests.py, which calls the real
# validators at these exact boundary values and fails loudly if they ever
# disagree.
WATER_VALVE_SECONDS_RANGE: tuple[int, int] = (1, 10)


POWER_CYCLE_DWELL_RANGE: tuple[int, int] = (5, 300)


POWER_CYCLE_DWELL_DEFAULT: int = 30


def _params_widget(command: str, products: list) -> dict | None:
    """The params widget spec for *command*, or None for a command that
    takes no params (every automatic command, and mdb's three actuator
    commands, which are all bare triggers per §1.3's table)."""
    if command == "dispense":
        return {"kind": "slot", "products": products}
    if command == "water_valve":
        lo, hi = WATER_VALVE_SECONDS_RANGE
        return {"kind": "seconds", "min": lo, "max": hi}
    if command == "power_cycle":
        lo, hi = POWER_CYCLE_DWELL_RANGE
        return {
            "kind": "dwell_seconds",
            "min": lo,
            "max": hi,
            "default": POWER_CYCLE_DWELL_DEFAULT,
        }
    return None


# --- Task 13b: action-route helpers -----------------------------------


def _parse_command_params(
    command: str,
    *,
    slot: str | None,
    seconds: str | None,
    dwell_seconds: str | None,
    valid_slots: frozenset[int] = frozenset(),
) -> dict:
    """Build the params dict for *command* from its raw form fields,
    validated against the SAME bounds the widget renders
    (WATER_VALVE_SECONDS_RANGE / POWER_CYCLE_DWELL_RANGE, above, and --
    Copilot review, PR 22, id=4128088598 -- *valid_slots* for `dispense`,
    the current catalog's `Product.slot` values, matching the `<select>`
    the widget renders) -- so a crafted POST outside those bounds is
    refused here rather than reaching the dispatcher. Every other
    testable command (ping, self_test, force_report, the three bare mdb
    actuator commands) takes no params.

    Raises ValueError, with a message safe to show the operator, for a
    missing/non-integer/out-of-range value; the caller (post_test_command)
    turns that into a 400 before the maintenance lease is ever touched.

    For "dispense" specifically (Task 4, dispenser-profiles plan 2):
    `contracts.common.COMMAND_PARAM_VALIDATORS["dispense"]` now requires a
    full `DispenseCommand(slot, mechanism, profile)` on the wire, so once
    the slot itself is known-good (in the current catalog) this also
    looks up that slot's product and its `dispenser_profile_for` result.
    A slot with no valid profile (profiles not wired, a `kind="other"`
    product no profile ever covers, a missing/mismatched table) raises
    RuntimeError -- not ValueError -- with the exact CFG-101 wording, so
    the caller renders it through partials/test_refusal.html like every
    other maintenance-lease refusal (never a 500) rather than a plain
    400; this runs BEFORE post_test_command ever calls
    _acquire_lease_or_refusal, so no lease is taken (and none needs
    releasing) on this path.
    """
    if command == "dispense":
        try:
            value = int(slot)
        except (TypeError, ValueError) as exc:
            raise ValueError("slot must be an integer") from exc
        if value not in valid_slots:
            raise ValueError(f"slot {value} is not in the current catalog")
        product = next(p for p in context.config.products if p.slot == value)
        profile = (
            context.machine_instance.gate.profile_for(product)
            if context.machine_instance is not None
            else None
        )
        if profile is None:
            raise RuntimeError(
                f"slot {value} ({product.sku}) has no valid dispenser profile (CFG-101)"
            )
        return {
            "slot": value,
            "mechanism": profile.mechanism,
            "profile": profile.model_dump(mode="json"),
        }
    if command == "water_valve":
        lo, hi = WATER_VALVE_SECONDS_RANGE
        try:
            value = int(seconds)
        except (TypeError, ValueError) as exc:
            raise ValueError("seconds must be an integer") from exc
        if not (lo <= value <= hi):
            raise ValueError(f"seconds must be between {lo} and {hi}")
        return {"seconds": value}
    if command == "power_cycle":
        lo, hi = POWER_CYCLE_DWELL_RANGE
        raw = (
            dwell_seconds
            if dwell_seconds not in (None, "")
            else POWER_CYCLE_DWELL_DEFAULT
        )
        try:
            value = int(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError("dwell_seconds must be an integer") from exc
        if not (lo <= value <= hi):
            raise ValueError(f"dwell_seconds must be between {lo} and {hi}")
        return {"dwell_seconds": value}
    return {}


async def _run_command(
    subsystem: str, command: str, params: dict, principal: web_auth.Principal
) -> dict:
    """Dispatch one command through the CommandDispatcher and return the
    render context for partials/test_result_card.html.

    Wraps the dispatch in `machine.lease.test_run()` -- the ONLY place
    this module calls it -- so `runs_in_flight` is incremented and
    decremented around every single command run, including one that
    raises or times out (`test_run`'s own `finally`, per its
    docstring: "a run that fails still frees the lease's run count"). The
    caller MUST already hold the lease (via `_acquire_lease_or_refusal`)
    before calling this; it never grants one itself.

    Writes the `test_run` log row here, once the outcome is known -- "each
    result card is written to the log as it happens" (task brief) -- with
    `verdict`/`note` both None, so POST /tests/runs/{run_id}/verdict has a
    row to update and a run nobody verdicts renders "verdict: none".

    Timeout (including "no dispatcher wired", treated the same as
    "unreachable" -- there is nothing to send to) renders the exact spec
    §6 wording: "no answer from <subsystem> after 2 attempts", with the
    attempt count read from the dispatcher's own `_retries` (defaulting to
    1, i.e. 2 attempts, when no dispatcher is wired) rather than a second
    hardcoded literal.

    Dispatches via `send_and_await_completion` (completion-table amendment,
    2026-09-29), not the older `send` -- for an immediate command (ping,
    self_test, force_report, the three mdb actuator tests) this behaves
    exactly like `send` always did (the ack IS completion). For a
    long-running actuator (dispense, water_valve, power_cycle) the awaited
    call does not return until the command's own completion signal arrives,
    so `machine.lease.test_run()`'s `runs_in_flight` -- and therefore the
    maintenance lease and its SVC-102 fault -- stays held for the
    actuator's REAL lifetime, not just until it starts. This is the fix for
    Copilot review PR 22 id=4128088504: a previous agent proved the lease
    released while a simulated motor was still running because the old
    `send()` call here returned (or timed out) long before the motor
    actually stopped.

    `CompletionTimeout` (accepted but never finished) is handled as its own
    branch, distinct from `CommandTimeout` (never even accepted) -- both
    render as `status="timeout"` in the result card, but with different
    detail text, since they mean different things to the tech reading it.
    """
    run_id = uuid4().hex
    started = time.time()
    dispatcher = context.command_dispatcher
    checks = None
    machine = context.machine_instance
    with machine.lease.test_run():
        try:
            if dispatcher is None:
                raise CommandTimeout(subsystem, command)
            ack = await dispatcher.send_and_await_completion(subsystem, command, params)
            status = ack.status
            detail = ack.detail
            if isinstance(ack.result, dict):
                checks = ack.result.get("checks")
        except CommandTimeout:
            retries = (
                getattr(dispatcher, "_retries", 1) if dispatcher is not None else 1
            )
            status = "timeout"
            detail = f"no answer from {subsystem} after {retries + 1} attempts"
        except CompletionTimeout:
            status = "timeout"
            detail = f"{subsystem} accepted {command} but never reported completion"
    elapsed = round(time.time() - started, 3)

    metadata = {
        "run_id": run_id,
        "user_id": principal.user.id,
        "user_name": principal.user.name,
        "subsystem": subsystem,
        "command": command,
        "params": params,
        "status": status,
        "checks": checks,
        "verdict": None,
        "note": None,
    }
    if "mechanism" in params:
        # Only a dispense run's params carry "mechanism" (Task 4,
        # dispenser-profiles plan 2) -- every other testable command's
        # params never do, so this key is absent for them rather than
        # present-but-None.
        metadata["mechanism"] = params["mechanism"]
    if context.event_recorder is not None:
        context.event_recorder.record("test_run", value=elapsed, metadata=metadata)

    return {
        "run_id": run_id,
        "subsystem": subsystem,
        "command": command,
        "status": status,
        "detail": detail,
        "checks": checks,
        "elapsed": elapsed,
        "automatic": command in AUTOMATIC_COMMANDS,
    }


@router.get(
    "/tests/{subsystem}",
    response_class=HTMLResponse,
    dependencies=[Depends(web_auth.require(Permission.run_tests))],
)
async def tests_subsystem(request: Request, subsystem: str):
    """Testable commands for one subsystem, split into the automatic
    and actuator groups (system-tests design §1.2/§1.3).

    ORDERING WARNING for Task 13b: this route matches ANY single path
    segment under /tests/, including "sale", "run-all", "end" and
    "takeover" -- it 404s on those today only because none of
    EXPECTED_SUBSYSTEMS is named that. Every literal-path GET route
    Task 13b adds under /tests/ (GET /tests/sale in particular) MUST
    be registered on this router BEFORE this route, exactly like
    /tests/log above, or this handler will shadow it.
    """
    if subsystem not in EXPECTED_SUBSYSTEMS:
        raise HTTPException(status_code=404, detail=f"Unknown subsystem '{subsystem}'")
    row = _subsystem_summary()[subsystem]
    testable = testable_commands(subsystem, row["commands"])
    products = (
        sorted(context.config.products, key=lambda p: p.slot) if context.config else []
    )

    automatic = [
        {"name": c, "widget": _params_widget(c, products)}
        for c in sorted(testable & AUTOMATIC_COMMANDS)
    ]
    actuator = [
        {"name": c, "widget": _params_widget(c, products)}
        for c in sorted(testable - AUTOMATIC_COMMANDS)
    ]

    level = Level.child(LEVEL_TESTS, subsystem, f"/tests/{subsystem}")
    return templates.TemplateResponse(
        "tests_subsystem.html",
        context.template_context(
            request,
            level=level,
            subsystem=subsystem,
            row=row,
            automatic=automatic,
            actuator=actuator,
            post_base=f"/tests/{subsystem}",
        ),
    )


# Route registration order across the Tests sub-routers is fixed in
# web_interface/routes/tests_level/__init__.py. The parametrised routes in this
# module go last because /tests/{subsystem}... would otherwise shadow literal paths.


@router.post(
    "/tests/run-all",
    response_class=HTMLResponse,
    dependencies=[
        Depends(web_auth.require(Permission.run_tests)),
        Depends(context.require_htmx),
    ],
)
async def tests_run_all(request: Request):
    """`ping` then `self_test` on every alive subsystem, IN SEQUENCE
    (task brief) -- one `await _run_command(...)` at a time, never
    `asyncio.gather`, so two subsystems' commands can never interleave
    on the wire. Renders one result table (or a single refusal
    fragment if the lease can't be taken at all).
    """
    principal = web_auth.current_principal(request)
    machine = context.machine_instance
    if machine is None:
        return templates.TemplateResponse(
            "partials/test_run_all_table.html",
            context.template_context(request, rows=[], refusal="VMC not initialized"),
        )

    refusal = _acquire_lease_or_refusal(machine, principal)
    if refusal:
        return templates.TemplateResponse(
            "partials/test_run_all_table.html",
            context.template_context(request, rows=[], refusal=refusal),
        )

    subsystems = _subsystem_summary()
    rows: list[dict] = []
    for name, row in subsystems.items():
        if not row["alive"]:
            continue
        testable = testable_commands(name, row["commands"])
        for command in ("ping", "self_test"):
            if command not in testable:
                continue
            rows.append(await _run_command(name, command, {}, principal))

    return templates.TemplateResponse(
        "partials/test_run_all_table.html",
        context.template_context(request, rows=rows, refusal=None),
    )


@router.post(
    # {command:path} (not plain {command}), matching web_interface/
    # filters.py's sku_url_segment/{sku:path} reasoning: a command name
    # can itself contain "/" (e.g. the illustrative "payment/enable" a
    # capabilities doc might advertise -- see TestDiscoveryIntersection
    # in tests/test_routes_tests.py), and a plain str-converter segment
    # 404s on a literal "/" the same way a SKU would. This is what lets
    # a direct POST /tests/vending/payment/enable actually REACH this
    # handler (and get refused by the allowlist check below, 403)
    # instead of 404ing before the security check ever runs -- a 404
    # here would look like the command "doesn't exist" rather than
    # "exists and is refused," which is the wrong signal to prove the
    # allowlist re-check is a real boundary.
    "/tests/{subsystem}/{command:path}",
    response_class=HTMLResponse,
    dependencies=[
        Depends(web_auth.require(Permission.run_tests)),
        Depends(context.require_htmx),
    ],
)
async def post_test_command(
    request: Request,
    subsystem: str,
    command: str,
    slot: str | None = Form(None),
    seconds: str | None = Form(None),
    dwell_seconds: str | None = Form(None),
):
    """Run one command test (system-tests design §3's "Run" row).

    THE SECURITY BOUNDARY (task brief): re-checks `testable_commands`
    (the advertised-∩-allowlist intersection, the one place that
    computation lives -- see this module's docstring) against the
    LIVE, freshly-read `_subsystem_summary()[subsystem]["commands"]`,
    never trusting anything the client claims about what buttons it
    was shown. `command` is not in `testable` for `refund` or
    `payment/enable` on every subsystem (contracts.common.
    TESTABLE_COMMANDS never lists either, for any subsystem, whether
    or not that subsystem's capabilities doc happens to advertise it)
    -- 403, unconditionally, before the maintenance lease is ever
    touched and before the dispatcher is ever called. A command that
    IS allowlisted but NOT advertised by this particular subsystem is
    refused the same way (it is simply absent from the intersection).
    """
    if subsystem not in EXPECTED_SUBSYSTEMS:
        raise HTTPException(status_code=404, detail=f"Unknown subsystem '{subsystem}'")
    row = _subsystem_summary()[subsystem]
    testable = testable_commands(subsystem, row["commands"])
    if command not in testable:
        raise HTTPException(
            status_code=403,
            detail=f"{command!r} is not a testable command for {subsystem!r}",
        )

    valid_slots = (
        frozenset(p.slot for p in context.config.products)
        if context.config
        else frozenset()
    )
    try:
        params = _parse_command_params(
            command,
            slot=slot,
            seconds=seconds,
            dwell_seconds=dwell_seconds,
            valid_slots=valid_slots,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        # A slot with no valid dispenser profile (CFG-101) -- a
        # refusal, not a 400/500, and raised before any lease is ever
        # taken (see _parse_command_params's docstring), so there is
        # nothing to release here.
        return templates.TemplateResponse(
            "partials/test_refusal.html",
            context.template_context(request, reason=str(exc)),
        )

    principal = web_auth.current_principal(request)
    machine = context.machine_instance
    if machine is None:
        return templates.TemplateResponse(
            "partials/test_refusal.html",
            context.template_context(request, reason="VMC not initialized"),
        )

    refusal = _acquire_lease_or_refusal(machine, principal)
    if refusal:
        return templates.TemplateResponse(
            "partials/test_refusal.html",
            context.template_context(request, reason=refusal),
        )

    result = await _run_command(subsystem, command, params, principal)
    return templates.TemplateResponse(
        "partials/test_result_card.html",
        context.template_context(request, **result),
    )
