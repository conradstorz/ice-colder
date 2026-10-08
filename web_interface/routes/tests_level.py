"""The Tests level (system-tests design, spec §3): discovery + action routes.

Task 13 split at the discovery/actions seam (task-13-brief.md): Task 13a
built `GET /tests`, `GET /tests/{subsystem}` and `GET /tests/log` --
read-only, and entering none of them takes the maintenance lease (see
`.superpowers/sdd/task-13a-report.md` for the interface it hands off).
Task 13b (this revision) adds every action route: `GET`/`POST /tests/sale`,
`POST /tests/{subsystem}/{command}`, `POST /tests/runs/{run_id}/verdict`,
`POST /tests/run-all`, `POST /tests/end`, `POST /tests/takeover`. See
`.superpowers/sdd/task-13b-report.md` for the full account of this half.

Two things this module deliberately reuses rather than re-derives, per the
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

import asyncio
import contextlib
import json
import sqlite3
import time
from uuid import uuid4

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from contracts.common import TESTABLE_COMMANDS
from contracts.ice_maker_monitor import CONTRACT_VERSION as ICE_MAKER_CONTRACT_VERSION
from contracts.vending_machine import CONTRACT_VERSION as VENDING_CONTRACT_VERSION
from contracts.vending_machine import EXPECTED_SUBSYSTEMS
from services.access import Permission
from services.command_dispatcher import CommandTimeout, CompletionTimeout
from web_interface import auth as web_auth
from web_interface import context
from web_interface.levels import LEVEL_TESTS, LEVEL_TESTS_LOG, LEVEL_TESTS_SALE, Level
from web_interface.routes.health import _subsystem_summary

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


def _recent_test_runs(limit: int = 100) -> list[dict]:
    """The last *limit* `test_run` events, newest first, metadata merged
    into the row, `elapsed_seconds` (for the `humanize_seconds` filter,
    "N ago") added, and `duration_seconds` (system-tests design §4: the
    event's own `value` column, how LONG the run took, not how long ago it
    happened) added.

    Reads `context.event_recorder._db_path` directly rather than caching
    it or adding a public query method to services/event_recorder.py --
    the same deliberate deviation services/reports.py's module docstring
    documents (that module reads the same private attribute, at call time,
    for the same reason: EventRecorder._quarantine_corrupt_db can reassign
    it mid-process after a corruption recovery, and event_recorder.py is
    outside this task's file list to add an accessor to). Returns [] when
    no recorder is wired, rather than raising.
    """
    recorder = context.event_recorder
    if recorder is None:
        return []
    recorder.flush()
    db_path = recorder._db_path
    now = time.time()
    rows: list[dict] = []
    with contextlib.closing(sqlite3.connect(db_path)) as conn:
        cursor = conn.execute(
            "SELECT id, timestamp, value, metadata FROM events "
            "WHERE event_type = 'test_run' ORDER BY id DESC, timestamp DESC LIMIT ?",
            (limit,),
        )
        for row_id, ts, value, meta_str in cursor.fetchall():
            try:
                meta = json.loads(meta_str) if meta_str else {}
            except (TypeError, ValueError):
                meta = {}
            if not isinstance(meta, dict):
                meta = {}
            row = {
                "id": row_id,
                "timestamp": ts,
                "elapsed_seconds": max(now - ts, 0.0),
                "duration_seconds": value,
            }
            row.update(meta)
            rows.append(row)
    return rows


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

# Busy refusals from VMC.begin_maintenance (the OPPORTUNISTIC lease) that
# now point the operator at the standby button instead of their own raw
# wording -- "held by <id>" is deliberately NOT in this map (system-tests
# design §2.2a / Task 3 brief): a lease already held by someone else is a
# different situation than a busy-but-unheld machine, and keeps its own
# wording unchanged.
_BUSY_REFUSAL_WORDING = "machine is busy — take it out of service first"
_BUSY_REFUSALS = frozenset({"machine is mid-sale", "credit is still on the machine"})


def _service_state(
    vmc,
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
    hold = vmc.maintenance_hold if vmc is not None else None
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
            context.vmc_instance.dispenser_profile_for(product)
            if context.vmc_instance is not None
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


def _acquire_lease_or_refusal(vmc, principal: web_auth.Principal) -> str | None:
    """Take the maintenance lease for *principal*'s session, or return why
    not, WITHOUT ever touching the dispatcher (system-tests design §2.2,
    task brief: "takes the lease or returns the refusal inline").

    Three cases:
      - No lease held: try to grant one (`VMC.begin_maintenance`); refused
        for one of its two busy reasons ("machine is mid-sale", "credit is
        still on the machine") is remapped to `_BUSY_REFUSAL_WORDING`
        ("machine is busy — take it out of service first", Task 3, system-
        tests design §2.2a) so the refusal itself points the operator at
        the standby button rather than leaving them to guess what to do
        about a machine that just won't take the opportunistic lease.
      - Lease already held by THIS session (a second command run in the
        same maintenance visit): a no-op -- returns None (proceed) without
        calling begin_maintenance again, which would otherwise refuse with
        a spurious "held by <self>" (VMC.begin_maintenance refuses
        whenever any lease exists, regardless of who holds it).
      - Lease held by a DIFFERENT session: refused, "held by <holder>",
        matching VMC.begin_maintenance's own wording for the same case --
        deliberately NOT remapped, since "someone else has it" is a
        different situation than "the machine is busy".

    Returns None exactly when the caller's session now holds the lease
    (freshly granted or pre-existing) and it is safe to proceed to
    `vmc.maintenance_test_run()`.
    """
    session_id = principal.session.id
    hold = vmc.maintenance_hold
    if hold is not None and hold.holder_session_id == session_id:
        return None
    if hold is not None:
        return f"held by {hold.holder_user_id}"
    granted, reason = vmc.begin_maintenance(principal.user.id, session_id)
    if granted:
        return None
    return _BUSY_REFUSAL_WORDING if reason in _BUSY_REFUSALS else reason


async def _run_command(
    vmc, subsystem: str, command: str, params: dict, principal: web_auth.Principal
) -> dict:
    """Dispatch one command through the CommandDispatcher and return the
    render context for partials/test_result_card.html.

    Wraps the dispatch in `vmc.maintenance_test_run()` -- the ONLY place
    this module calls it -- so `runs_in_flight` is incremented and
    decremented around every single command run, including one that
    raises or times out (`maintenance_test_run`'s own `finally`, per its
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
    so `vmc.maintenance_test_run()`'s `runs_in_flight` -- and therefore the
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
    with vmc.maintenance_test_run():
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


def build_router(templates: Jinja2Templates) -> APIRouter:
    router = APIRouter()

    @router.get(
        "/tests",
        response_class=HTMLResponse,
        dependencies=[Depends(web_auth.require(Permission.run_tests))],
    )
    async def tests_level(request: Request):
        """One card per EXPECTED_SUBSYSTEMS entry plus the lease banner.

        Deliberately takes no lease (system-tests design §2.2, task brief):
        this handler never calls VMC.begin_maintenance -- it only reads the
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
            "tests.html",
            context.template_context(
                request,
                level=LEVEL_TESTS,
                cards=cards,
                **_service_state(context.vmc_instance, principal),
            ),
        )

    @router.get(
        "/tests/log",
        response_class=HTMLResponse,
        dependencies=[Depends(web_auth.require(Permission.run_tests))],
    )
    async def tests_log(request: Request):
        """Last 100 test_run events (system-tests design §4).

        Registered BEFORE the parameterized /tests/{subsystem} route below
        so "log" is never swallowed as a (nonexistent) subsystem name --
        see that route's own docstring for the full ordering rule, which
        Task 13b's own new /tests/* GET routes must also respect.
        """
        return templates.TemplateResponse(
            "tests_log.html",
            context.template_context(
                request, level=LEVEL_TESTS_LOG, runs=_recent_test_runs(100)
            ),
        )

    @router.get(
        "/tests/sale",
        response_class=HTMLResponse,
        dependencies=[Depends(web_auth.require(Permission.run_tests))],
    )
    async def tests_sale_picker(request: Request):
        """The simulated-sale SKU picker (Task 13b). Deliberately takes no
        lease -- same rule as `tests_level`/`tests_subsystem` above: a
        POST /tests/sale run is what acquires it, not viewing this page.

        Registered BEFORE /tests/{subsystem} below, same reason /tests/log
        is: "sale" would otherwise be swallowed as a (nonexistent)
        subsystem name by that single-segment route.
        """
        products = (
            sorted(context.config.products, key=lambda p: p.slot)
            if context.config
            else []
        )
        return templates.TemplateResponse(
            "tests_sale.html",
            context.template_context(
                request, level=LEVEL_TESTS_SALE, products=products
            ),
        )

    @router.get(
        "/tests/standby/confirm",
        response_class=HTMLResponse,
        dependencies=[Depends(web_auth.require(Permission.run_tests))],
    )
    async def tests_standby_confirm(
        request: Request, confirming: str | None = Query(default=None)
    ):
        """confirm_button.html's confirm_url contract (Task 3, matching
        routes/health.py's fault_clear_confirm): absent or anything but
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
            "partials/tests_hold_banner.html",
            context.template_context(
                request,
                **_service_state(
                    context.vmc_instance,
                    principal,
                    confirming=(confirming != "false"),
                ),
            ),
        )

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
            raise HTTPException(
                status_code=404, detail=f"Unknown subsystem '{subsystem}'"
            )
        row = _subsystem_summary()[subsystem]
        testable = testable_commands(subsystem, row["commands"])
        products = (
            sorted(context.config.products, key=lambda p: p.slot)
            if context.config
            else []
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

    # --- Task 13b: action routes ---------------------------------------
    #
    # Registration order below is load-bearing (see tests_subsystem's own
    # ORDERING WARNING above, and task-13a-report.md's "Route registration
    # order" section): every literal-path POST route under /tests/ --
    # /tests/sale, /tests/run-all, /tests/end, /tests/takeover, and
    # /tests/runs/{run_id}/verdict -- is registered BEFORE the generic
    # POST /tests/{subsystem}/{command:path} at the bottom. The verdict
    # route is the one that actually collides if this order is reversed:
    # POST /tests/runs/r1/verdict has the same shape (two path segments
    # after /tests/, subsystem="runs", command:path="r1/verdict") that the
    # generic route matches -- it would 404 (no subsystem named "runs")
    # instead of ever reaching post_verdict. /tests/sale, /tests/run-all,
    # /tests/end and /tests/takeover are single-segment POSTs and could not
    # collide with the generic route either way (it needs subsystem AND at
    # least one command segment), but are kept ahead of it here too, for
    # the same reason GET /tests/log/GET /tests/sale precede GET
    # /tests/{subsystem} above: literal paths first, catch-all last.
    #
    # POST /tests/standby (Task 3, system-tests design §2.2a) joins this
    # group for the same reason -- a single-segment literal path, kept
    # ahead of the generic route below along with the rest.

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
        session (VMC.begin_standby, Task 3 / system-tests design §2.2a):
        refunds any credit on the machine, cancels a live customer sale,
        and grants the lease with `standby=True` -- no idle-timer release;
        the VMC's own session-liveness sweep (wired via
        VMC.set_session_liveness in main.py) is what ends it if the tech
        simply walks away or locks the tablet.

        Calls `vmc.begin_standby` directly rather than going through
        `_acquire_lease_or_refusal` -- that helper's busy-wording remap
        exists to point OTHER callers (a command run, a simulated sale) at
        THIS button; begin_standby's own refusal wording ("vend finishing,
        tap again" mid-dispense, "held by <id>" for a different session's
        lease) is already the right thing to show here, verbatim, inside
        the card (Task 3 requirement).
        """
        principal = web_auth.current_principal(request)
        vmc = context.vmc_instance
        if vmc is None:
            refusal = "VMC not initialized"
        else:
            granted, reason = vmc.begin_standby(principal.user.id, principal.session.id)
            refusal = None if granted else reason
        return templates.TemplateResponse(
            "partials/tests_hold_banner.html",
            context.template_context(
                request, **_service_state(vmc, principal, refusal=refusal)
            ),
        )

    @router.post(
        "/tests/sale",
        response_class=HTMLResponse,
        dependencies=[
            Depends(web_auth.require(Permission.run_tests)),
            Depends(context.require_htmx),
        ],
    )
    async def tests_sale_run(request: Request, sku: str = Form(...)):
        """Run one simulated sale (VMC.run_test_sale) for the SKU the
        picker's form submitted.

        SKU-with-slash: the SKU arrives as a POST form field
        (application/x-www-form-urlencoded), never a URL segment -- GET and
        POST /tests/sale are the only two routes this picker defines (no
        `{sku}` route segment exists anywhere under it), so there is no
        second SKU-in-a-URL escaping mechanism to invent here:
        `sku_url_segment`/`{sku:path}` exist specifically because a raw `/`
        in a URL PATH segment 404s (see web_interface/filters.py's
        docstring); a form body has no such restriction, and FastAPI's
        `Form(...)` decodes it back to the exact original string,
        including any `/`. `tests_sale.html`'s `<option value="{{
        product.sku }}">` is likewise not a URL -- it is an HTML attribute
        value, which Jinja2's autoescaping (the default here) already
        makes safe on its own for any catalog string, `/` included.
        """
        principal = web_auth.current_principal(request)
        products = context.config.products if context.config else []
        product = next((p for p in products if p.sku == sku), None)
        if product is None:
            raise HTTPException(status_code=404, detail=f"No such product: {sku}")

        vmc = context.vmc_instance
        if vmc is None:
            return templates.TemplateResponse(
                "partials/test_refusal.html",
                context.template_context(request, reason="VMC not initialized"),
            )

        refusal = _acquire_lease_or_refusal(vmc, principal)
        if refusal:
            return templates.TemplateResponse(
                "partials/test_refusal.html",
                context.template_context(request, reason=refusal),
            )

        try:
            result = await vmc.run_test_sale(
                sku, user_id=principal.user.id, user_name=principal.user.name
            )
        except (ValueError, RuntimeError) as exc:
            # ValueError: the catalog moved under us between the lookup
            # above and this call (vmc.products, not context.config.products
            # -- see run_test_sale's own _find_product_by_sku). RuntimeError:
            # run_test_sale's own "could not select" guard (locked out,
            # sold out) or "no lease held" (a race against the check above).
            # Either way this is a refusal, not a 500.
            return templates.TemplateResponse(
                "partials/test_refusal.html",
                context.template_context(request, reason=str(exc)),
            )

        return templates.TemplateResponse(
            "partials/test_sale_result.html",
            context.template_context(request, result=result),
        )

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
        vmc = context.vmc_instance
        if vmc is None:
            return templates.TemplateResponse(
                "partials/test_run_all_table.html",
                context.template_context(
                    request, rows=[], refusal="VMC not initialized"
                ),
            )

        refusal = _acquire_lease_or_refusal(vmc, principal)
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
                rows.append(await _run_command(vmc, name, command, {}, principal))

        return templates.TemplateResponse(
            "partials/test_run_all_table.html",
            context.template_context(request, rows=rows, refusal=None),
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
        (VMC.end_maintenance already enforces both: session match and
        runs_in_flight == 0, deferring via release_requested otherwise).
        Re-renders the same #tests-hold banner tests.html swaps this into
        (outerHTML) -- empty when no lease remains, so the banner div is
        removed entirely, matching tests.html's own `{% if hold %}` guard.
        """
        principal = web_auth.current_principal(request)
        vmc = context.vmc_instance
        if vmc is not None:
            vmc.end_maintenance(principal.session.id)
        return templates.TemplateResponse(
            "partials/tests_hold_banner.html",
            context.template_context(request, **_service_state(vmc, principal)),
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
        (VMC.take_over_maintenance, system-tests design §2.2) -- refused
        (lease unchanged) while a run is in flight or before the 60s idle
        threshold. Re-renders #tests-hold either way.
        """
        principal = web_auth.current_principal(request)
        vmc = context.vmc_instance
        if vmc is not None:
            vmc.take_over_maintenance(principal.user.id, principal.session.id)
        return templates.TemplateResponse(
            "partials/tests_hold_banner.html",
            context.template_context(request, **_service_state(vmc, principal)),
        )

    @router.post(
        "/tests/runs/{run_id}/verdict",
        response_class=HTMLResponse,
        dependencies=[
            Depends(web_auth.require(Permission.run_tests)),
            Depends(context.require_htmx),
        ],
    )
    async def post_verdict(
        request: Request,
        run_id: str,
        verdict: str = Form(...),
        note: str | None = Form(None),
    ):
        """Record pass/fail + note on one test_run row, by run_id
        (system-tests design §4: EventRecorder.update_metadata, merged in
        place on the writer thread -- so `checks`/`params`/etc already on
        the row survive). Works identically for a subsystem-command row
        and a simulated-sale row (VMC.run_test_sale, Task 13b): both carry
        `run_id` in their metadata now, located the same way.

        400 for a verdict outside {"pass", "fail"} -- never silently
        coerced. A run_id matching no row (already pruned past the 90-day
        window, or simply wrong) is a no-op on the recorder side
        (update_metadata's own documented behavior) -- still 200, since
        there is nothing the caller did wrong at the HTTP level.
        """
        if verdict not in ("pass", "fail"):
            raise HTTPException(
                status_code=400, detail="verdict must be 'pass' or 'fail'"
            )
        note = note or None
        if context.event_recorder is not None:
            context.event_recorder.update_metadata(run_id, verdict=verdict, note=note)
            # Block until the write lands: a test (or an operator's very
            # next GET /tests/log) reading right back must see it -- update_
            # metadata itself only queues the merge for the writer thread.
            await asyncio.to_thread(context.event_recorder.flush)
        return templates.TemplateResponse(
            "partials/test_verdict_recorded.html",
            context.template_context(
                request, run_id=run_id, verdict=verdict, note=note
            ),
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
            raise HTTPException(
                status_code=404, detail=f"Unknown subsystem '{subsystem}'"
            )
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
        vmc = context.vmc_instance
        if vmc is None:
            return templates.TemplateResponse(
                "partials/test_refusal.html",
                context.template_context(request, reason="VMC not initialized"),
            )

        refusal = _acquire_lease_or_refusal(vmc, principal)
        if refusal:
            return templates.TemplateResponse(
                "partials/test_refusal.html",
                context.template_context(request, reason=refusal),
            )

        result = await _run_command(vmc, subsystem, command, params, principal)
        return templates.TemplateResponse(
            "partials/test_result_card.html",
            context.template_context(request, **result),
        )

    return router
