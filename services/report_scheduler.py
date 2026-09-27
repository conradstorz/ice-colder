# services/report_scheduler.py
"""Scheduled sales-summary email.

See ``docs/superpowers/specs/2026-09-25-sales-reports-design.md`` §4.1 (the
``ReportsConfig`` shape) and §4.2 (this module's contract) for the
authoritative spec.

``run(config, recorder, mailer, clock)`` is started by ``main.py`` under
``_supervise("report scheduler", ...)``, alongside the MQTT client and health
monitor. It loops forever on a bounded sleep of at most 60 s, re-reading the
*live* ``config`` object every pass and recomputing the next due time from
scratch via :func:`compute_next_due` -- a pure, module-level function with no
I/O, deliberately kept separate from the loop so it can be tested
exhaustively without an event loop, a recorder, or a mailer. Because the
config is re-read every pass rather than captured once, a schedule flipped
to ``"off"`` stops the next send within one bounded sleep, and a schedule
flipped on schedules from the settings in force *at that pass*, never a
stale, previously computed interval.

Sends are de-duplicated by period: before sending, the loop reads the most
recent ``report_sent`` event's ``metadata["period"]`` and compares it to the
period :func:`compute_next_due` says is currently due. A match suppresses
the send. This single comparison is also what limits a startup (or a
schedule just turned back on) to *exactly one* catch-up send: each pass only
ever computes the *single most recently completed* period as of the current
``now`` -- never a list of every period missed while the schedule was off or
the process was down -- so there is nothing to iterate over and no way for a
backlog to accumulate.

The period identifier stored in ``report_sent`` (``NextDue.period_key``) is
``"<schedule>:<period_start-local-date>"``, e.g. ``"daily:2026-09-19"`` or
``"weekly:2026-09-14"``. A later reader recovers the exact covered interval
from this alone: the schedule name gives the period's length (one day, or
seven days starting on the configured weekday), and the date is that
period's local start (inclusive); the end (exclusive) is one day, or seven
days, later. The event's ``metadata`` also carries ``period_start`` and
``period_end`` as plain ISO dates for a reader who would rather not parse
the key.

Every read of the events database goes through :func:`asyncio.to_thread`
(``summary()`` and the ``report_sent`` lookup below), matching
``services/reports.py``'s own convention of reading ``recorder._db_path`` at
call time rather than caching it, since corrupt-database recovery can
reassign it mid-process.

The loop never raises out of its own body: the whole per-pass workload (the
de-dup query, ``summary()``, the send, the ``report_sent`` write) is wrapped
in a single broad ``except Exception`` so one bad pass -- a query error, a
mailer that raises instead of returning ``False``, anything -- logs and is
retried next pass rather than taking the scheduler down (``_supervise``
would restart it, but a scheduler that crashes every pass sends nothing and
hides the fault). The one deliberate exception to "never raises" is the
``clock`` callable itself, read *outside* that guard at the top of each
iteration: production's clock (a plain wall-clock read) never raises, but a
test's fake clock may raise a sentinel once its scripted values are
exhausted, which is what stops the loop in tests -- see the test file for
how that combines with a stubbed, no-op ``asyncio.sleep`` to make the whole
suite instant.
"""

import asyncio
import contextlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Awaitable, Callable, Optional

from loguru import logger

from services.reports import summary

__all__ = ["NextDue", "compute_next_due", "run"]

_BOUNDED_SLEEP_SECONDS = 60.0
_DAILY_PERIOD = timedelta(days=1)
_WEEKLY_PERIOD = timedelta(days=7)


@dataclass(frozen=True)
class NextDue:
    """One scheduled occurrence, as computed by :func:`compute_next_due`.

    ``due_at`` is the scheduled send instant for the *current* cycle -- for
    ``daily`` that is today's configured hour; for ``weekly`` it is the
    configured weekday's occurrence in the most recently started (or
    current) 7-day cycle. It can be in the future relative to the ``now``
    it was computed from (the hour hasn't arrived yet today, or on the
    configured weekday before that hour) -- callers decide whether it is
    *actually* due by comparing ``now >= due_at`` themselves; this dataclass
    only reports what the next occurrence is and what it would cover.

    ``period_start``/``period_end`` is the half-open ``[period_start,
    period_end)`` interval ``due_at`` covers -- the previous completed day
    or week, ending at ``due_at``'s own local midnight/week-start (never at
    ``due_at`` itself, which carries an hour-of-day offset).

    ``period_key`` is the stable, comparable de-dup identifier described in
    the module docstring.
    """

    due_at: datetime
    period_start: datetime
    period_end: datetime
    period_key: str


def compute_next_due(config, now: datetime) -> Optional[NextDue]:
    """Pure calendar computation: no I/O, no wall-clock read of its own.

    Given the live ``config`` (only ``config.reports`` is consulted:
    ``schedule``, ``hour``, ``weekday``) and an aware, already-local ``now``,
    return the current cycle's scheduled occurrence and the period it
    covers, or ``None`` when ``schedule == "off"``.

    Purity is what makes this function's tests the heart of the task: given
    the same ``config`` and ``now``, it always returns the same answer,
    touches no recorder, sends no email, and needs no event loop -- so its
    tests can be exhaustive over every hour/weekday/now combination that
    matters without any of that machinery.

    ``now`` is expected to already be in the local timezone the schedule's
    ``hour``/``weekday`` are meant against (mirroring how
    ``services/reports.py`` treats an already-localized datetime); this
    function does no timezone conversion of its own; it also does not
    attempt to special-case a DST transition (out of scope for this task's
    behaviour list, unlike ``services/reports.py``'s bucketing).
    """
    schedule = config.reports.schedule
    if schedule == "off":
        return None

    today = now.replace(hour=0, minute=0, second=0, microsecond=0)

    if schedule == "daily":
        occurrence_day = today
        period_length = _DAILY_PERIOD
    elif schedule == "weekly":
        target_weekday = config.reports.weekday
        days_back = (today.weekday() - target_weekday) % 7
        occurrence_day = today - timedelta(days=days_back)
        period_length = _WEEKLY_PERIOD
    else:
        # Unreachable given ReportsConfig.schedule's Literal type, but a
        # pure function must not raise on an unexpected value either.
        return None

    due_at = occurrence_day + timedelta(hours=config.reports.hour)
    period_end = occurrence_day
    period_start = occurrence_day - period_length
    period_key = f"{schedule}:{period_start.date().isoformat()}"
    return NextDue(
        due_at=due_at,
        period_start=period_start,
        period_end=period_end,
        period_key=period_key,
    )


def _recipients(config) -> list[str]:
    """Owner plus ``extra_recipients``, de-duplicated (owner listed twice
    counts once), order preserved (owner first)."""
    seen: set[str] = set()
    result: list[str] = []
    for addr in [config.machine_owner.email, *config.reports.extra_recipients]:
        if not addr:
            continue
        addr = addr.strip()
        if addr and addr not in seen:
            seen.add(addr)
            result.append(addr)
    return result


def _last_sent_period_key(recorder) -> Optional[str]:
    """Synchronous DB read -- run through ``asyncio.to_thread`` from `run`.

    Reads ``recorder._db_path`` at call time, never cached: see
    ``services/reports.py``'s module docstring for why (corrupt-database
    recovery can reassign it mid-process).
    """
    recorder.flush()
    db_path = recorder._db_path
    with contextlib.closing(sqlite3.connect(db_path)) as conn:
        row = conn.execute(
            "SELECT metadata FROM events WHERE event_type='report_sent' "
            "ORDER BY timestamp DESC LIMIT 1"
        ).fetchone()
    if not row or not row[0]:
        return None
    try:
        meta = json.loads(row[0])
    except (TypeError, ValueError):
        return None
    period = meta.get("period")
    return period if isinstance(period, str) else None


def _period_label(due: NextDue) -> str:
    """Human-readable range for the email subject: a single date for a
    one-day period, or "start to end" (both inclusive) for a longer one."""
    start = due.period_start.date()
    last_day = (due.period_end - timedelta(days=1)).date()
    if start == last_day:
        return start.isoformat()
    return f"{start.isoformat()} to {last_day.isoformat()}"


def _compose_body(data: dict) -> str:
    lines = [
        f"Revenue: {data['revenue']:.2f}",
        f"Vends: {data['vends']}",
        f"Failed vends: {data['failed']}",
        f"Refunds: {data['refunds']:.2f}",
        f"Cash since last collection: {data['cash_since_last_collection']:.2f}",
        "",
        "By method:",
    ]
    by_method = data.get("by_method") or []
    if not by_method:
        lines.append("  (no sales in this period)")
    for entry in by_method:
        lines.append(
            f"  {entry['method']}: {entry['amount']:.2f} "
            f"({entry['count']} sale(s), {entry['share'] * 100:.1f}% of revenue)"
        )
    return "\n".join(lines)


async def run(
    config,
    recorder,
    mailer: Callable[..., Awaitable[bool]],
    clock: Callable[[], datetime],
) -> None:
    """The supervised scheduler loop. See the module docstring."""
    while True:
        now = clock()  # may raise a test sentinel; see module docstring
        try:
            due = compute_next_due(config, now)
            if due is not None and now >= due.due_at:
                last_sent = await asyncio.to_thread(_last_sent_period_key, recorder)
                if last_sent != due.period_key:
                    window = (due.period_start.timestamp(), due.period_end.timestamp())
                    data = await asyncio.to_thread(summary, recorder, window)
                    to = ", ".join(_recipients(config))
                    subject = (
                        f"{config.machine_id} sales summary ({_period_label(due)})"
                    )
                    body = _compose_body(data)
                    email_config = config.communication.email_gateway
                    sent = await mailer(email_config, to, subject, body)
                    if sent:
                        recorder.record(
                            "report_sent",
                            metadata={
                                "period": due.period_key,
                                "schedule": config.reports.schedule,
                                "period_start": due.period_start.date().isoformat(),
                                "period_end": due.period_end.date().isoformat(),
                            },
                        )
                    else:
                        logger.warning(
                            "report scheduler: send failed for period "
                            f"{due.period_key}; will retry at the next due check"
                        )
        except Exception:
            logger.exception("report scheduler: pass failed; will retry next pass")
        await asyncio.sleep(_BOUNDED_SLEEP_SECONDS)
