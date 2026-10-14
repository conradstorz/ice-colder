import asyncio
import contextlib
import json
import sqlite3
import time

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse

from ice_edge.access.access import Permission
from ice_edge.web import auth as web_auth
from ice_edge.web import context
from ice_ui.levels import LEVEL_TESTS_LOG
from ice_ui.templating import templates


router = APIRouter()


def recent_test_runs(limit: int = 100) -> list[dict]:
    """The last *limit* `test_run` events, newest first, metadata merged
    into the row, `elapsed_seconds` (for the `humanize_seconds` filter,
    "N ago") added, and `duration_seconds` (system-tests design §4: the
    event's own `value` column, how LONG the run took, not how long ago it
    happened) added.

    Reads `context.event_recorder.db_path` -- `EventRecorder`'s public
    read-only property for the live database file -- at call time rather
    than caching it, the same convention ice_edge/reporting/reports.py's module
    docstring documents (that module reads the same public property, at
    call time, for the same reason: `EventRecorder._quarantine_corrupt_db`
    can reassign the underlying path mid-process after a corruption
    recovery, and a cached path would silently keep reading the wrong
    database). Returns [] when no recorder is wired, rather than raising.
    """
    recorder = context.event_recorder
    if recorder is None:
        return []
    recorder.flush()
    db_path = recorder.db_path
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
        request,
        "tests_log.html",
        context.template_context(
            request, level=LEVEL_TESTS_LOG, runs=recent_test_runs(100)
        ),
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
    and a simulated-sale row (TestSaleRunner.run_test_sale, Task 13b): both carry
    `run_id` in their metadata now, located the same way.

    400 for a verdict outside {"pass", "fail"} -- never silently
    coerced. A run_id matching no row (already pruned past the 90-day
    window, or simply wrong) is a no-op on the recorder side
    (update_metadata's own documented behavior) -- still 200, since
    there is nothing the caller did wrong at the HTTP level.
    """
    if verdict not in ("pass", "fail"):
        raise HTTPException(status_code=400, detail="verdict must be 'pass' or 'fail'")
    note = note or None
    if context.event_recorder is not None:
        context.event_recorder.update_metadata(run_id, verdict=verdict, note=note)
        # Block until the write lands: a test (or an operator's very
        # next GET /tests/log) reading right back must see it -- update_
        # metadata itself only queues the merge for the writer thread.
        await asyncio.to_thread(context.event_recorder.flush)
    return templates.TemplateResponse(
        request,
        "partials/test_verdict_recorded.html",
        context.template_context(request, run_id=run_id, verdict=verdict, note=note),
    )
