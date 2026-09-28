"""The Tests level (system-tests design, spec §3): discovery routes.

Task 13 split at the discovery/actions seam (task-13-brief.md): this module
(Task 13a) builds `GET /tests`, `GET /tests/{subsystem}` and `GET
/tests/log` -- read-only, and entering none of them takes the maintenance
lease. Task 13b adds the POST routes (run a command, verdict, run-all, the
simulated sale, end, takeover) to this same module; see
`.superpowers/sdd/task-13a-report.md` for the interface it hands off.

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

import contextlib
import json
import sqlite3
import time

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from contracts.common import TESTABLE_COMMANDS
from contracts.ice_maker_monitor import CONTRACT_VERSION as ICE_MAKER_CONTRACT_VERSION
from contracts.vending_machine import CONTRACT_VERSION as VENDING_CONTRACT_VERSION
from contracts.vending_machine import EXPECTED_SUBSYSTEMS
from services.access import Permission
from web_interface import auth as web_auth
from web_interface import context
from web_interface.levels import LEVEL_TESTS, LEVEL_TESTS_LOG, Level
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
    into the row and `elapsed_seconds` (for the `humanize_seconds` filter)
    added.

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
            "SELECT id, timestamp, metadata FROM events "
            "WHERE event_type = 'test_run' ORDER BY id DESC, timestamp DESC LIMIT ?",
            (limit,),
        )
        for row_id, ts, meta_str in cursor.fetchall():
            try:
                meta = json.loads(meta_str) if meta_str else {}
            except (TypeError, ValueError):
                meta = {}
            if not isinstance(meta, dict):
                meta = {}
            row = {"id": row_id, "timestamp": ts, "elapsed_seconds": max(now - ts, 0.0)}
            row.update(meta)
            rows.append(row)
    return rows


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

        hold = context.vmc_instance.maintenance_hold if context.vmc_instance else None
        held_by_me = False
        if hold is not None:
            principal = web_auth.current_principal(request)
            held_by_me = bool(
                principal and hold.holder_session_id == principal.session.id
            )

        return templates.TemplateResponse(
            "tests.html",
            context.template_context(
                request,
                level=LEVEL_TESTS,
                cards=cards,
                hold=hold,
                held_by_me=held_by_me,
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

    return router
