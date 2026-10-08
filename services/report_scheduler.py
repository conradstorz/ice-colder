# services/report_scheduler.py
"""Scheduled sales-summary email.

See ``docs/superpowers/specs/2026-09-25-sales-reports-design.md`` §4.1 (the
``ReportsConfig`` shape) and §4.2 (this module's contract) for the
authoritative spec.

``run(config, recorder, mailer, clock, tz=None)`` is started by ``main.py``
under ``supervise("report scheduler", ...)``, alongside the MQTT client and
health monitor. It loops forever on a bounded sleep of at most 60 s,
re-reading the *live* ``config`` object every pass and recomputing the next
due time from scratch via :func:`compute_next_due` -- a pure, module-level
function with no I/O, deliberately kept separate from the loop so it can be
tested exhaustively without an event loop, a recorder, or a mailer. Because
the config is re-read every pass rather than captured once, a schedule
flipped to ``"off"`` stops the next send within one bounded sleep, and a
schedule flipped on schedules from the settings in force *at that pass*,
never a stale, previously computed interval.

``tz`` is optional and last on both ``run`` and ``compute_next_due``, and
defaults to ``None`` on both -- production (``main.py``) passes neither
argument and gets ``None`` all the way down to ``services.reports``'s
``_to_local``/``_floor_to_bucket``/``_next_bucket``/``_bucket_key``, whose
``tz=None`` path means "resolve each epoch's offset through the OS, fresh,
for that specific instant" (see ``_to_local``'s own docstring) -- which is
exactly the DST-correct behaviour, and exactly how ``by_period`` gets it
right on its own default path. This module previously derived a tzinfo from
``now.tzinfo`` and threaded *that* through instead; that broke in production
specifically because ``main.py``'s clock (``datetime.now().astimezone()``)
attaches a frozen, date-invariant ``datetime.timezone`` fixed offset for the
one instant it was read at, and handing that frozen offset to
``_to_local``/``_floor_to_bucket`` for an *earlier* boundary (a previous
midnight or week-start) cannot re-derive that earlier instant's true offset
-- it just reapplies the current instant's offset, which is wrong exactly
across a DST transition. Tests pass their synthetic zone explicitly as
``tz=`` instead, so the existing DST window assertions stay meaningful. A
caller must keep ``now`` and ``tz`` consistent: ``now`` decides *whether*
something is due and *which* calendar day it is, while ``tz`` governs how
local boundaries are re-derived -- passing a synthetic-zone ``now`` together
with ``tz=None`` would ask the OS's real zone to reinterpret an instant built
under a made-up one, which is incoherent. The two are always paired: this
module never manufactures a ``tz`` independent of the ``now`` it receives.

Nothing in the types enforces that pairing, so :func:`compute_next_due`
also detects the one incoherent shape a future caller could plausibly
introduce by accident -- precisely because this docstring explains that a
dynamic ``tz`` fixes DST, someone could wire a real dynamic zone into
``tz=`` while still feeding it a ``now`` built the production way (a
frozen, date-invariant tzinfo) and believe that alone was enough.
:func:`_warn_if_now_tz_incoherent` compares ``now.tzinfo``'s and ``tz``'s
reported UTC offset six months apart to tell "frozen" from "date-varying"
apart, and logs a ``logger.warning`` naming both tzinfos when ``now.tzinfo``
looks frozen and ``tz`` looks genuinely dynamic -- never on ``tz=None``
(production's own path) or on a dynamic ``now`` paired with the matching
dynamic ``tz`` (a test's own path). It only ever logs; it never raises --
see below for why neither this pure function nor ``run``'s loop may ever
raise, and the failure mode here is silent bad data, not a crash, so a
warning is the right severity: it makes the mistake visible in the logs
without turning a previously-non-raising pure function into one that can
now blow up ``run``'s loop on a bad but non-fatal argument combination.

Sends are de-duplicated by period: before sending, the loop asks whether
*any* ``report_sent`` event's ``metadata["period"]`` matches the period
:func:`compute_next_due` says is currently due (:func:`_period_already_sent`)
-- not merely whether the *most recent* row matches, which would miss a
period buried under a later send for a *different* schedule (e.g. an admin
tries ``weekly``, then switches back to ``daily`` while the original
``daily`` period is still due: the ``weekly`` send becomes the newest row,
and comparing only against it would let the ``daily`` period resend). A
match suppresses the send. The write (below) and this read both key off the
exact same ``due.period_key`` string -- there is only ever one place that
builds that string (:func:`compute_next_due`) -- so there is no way for the
two to drift apart. This is also what limits a startup (or a schedule just
turned back on) to *exactly one* catch-up send: each pass only ever computes
the *single most recently completed* period as of the current ``now`` --
never a list of every period missed while the schedule was off or the
process was down -- so there is nothing to iterate over and no way for a
backlog to accumulate.

The write itself only *queues* the marker on ``recorder``'s writer thread,
not commits it synchronously, so ``run`` also keeps an in-memory
``last_sent_period`` guard (Family D, mirroring ``last_failed_period``
immediately below): ``_period_already_sent``'s own ``flush()`` call returns
immediately, without error, both when the writer thread has died and when
it is merely delayed past its timeout, so the very next pass's de-dup query
can miss a marker that a send genuinely just wrote. ``run`` attempts to
flush and re-verify the marker right after recording it and logs an error
if it still didn't land, but it is ``last_sent_period`` -- not that
durability attempt -- that actually prevents a duplicate send for the rest
of this process's run.

A failed send is retried at the *next due occurrence*, not on every
subsequent 60 s pass, matching the design's §4.2 "a failed send logs and
retries at the next due time": ``run`` keeps an in-memory
``last_failed_period`` (the period whose send most recently failed) across
loop iterations and skips attempting a send again for that same period until
:func:`compute_next_due` reports a *different* period is due. This state is
deliberately in-memory only, not persisted: a process restart naturally
forgets it and retries once more on the next pass, which is reasonable (a
transient restart shouldn't leave a failed send stuck) and never produces
the log-flood / repeated-SMTP-connection cadence a persisted or absent guard
would.

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
retried next pass rather than taking the scheduler down (``supervise``
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
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, tzinfo
from typing import Awaitable, Callable, Optional

from loguru import logger

# `_to_local`, `_floor_to_bucket`, `_next_bucket`, `_bucket_key` and
# `_MAX_BUCKET_KEY_RETRIES` are underscore-private in services/reports.py,
# but importing them here is a deliberate, directed reuse -- not an
# accident -- for the same reason services/reports.py itself reads
# `recorder._db_path` across modules (see that module's docstring):
# `services/reports.py` already contains the ONE correct, tested
# implementation of "re-derive true local midnight from an epoch instant,
# DST-correct in both directions, advancing one real day at a time" (backing
# `by_period`'s own bucket-boundary math -- see its module docstring and
# loop comments). `compute_next_due` below needs exactly that same
# local-midnight math for `period_start`/`period_end` (see
# `_advance_one_local_day` / `_shift_local_day`) -- writing a second,
# independent implementation here would be strictly worse: two places that
# must independently get DST arithmetic right (including the fall-back
# "phantom non-advance" subtlety `_bucket_key`/`_MAX_BUCKET_KEY_RETRIES`
# guard against), rather than one, tested implementation two callers share.
# Do not "fix" this by re-inlining the arithmetic.
from services.reports import (
    _MAX_BUCKET_KEY_RETRIES,
    _bucket_key,
    _floor_to_bucket,
    _next_bucket,
    _to_local,
    summary,
)

__all__ = ["NextDue", "compute_next_due", "run"]

_BOUNDED_SLEEP_SECONDS = 60.0
_DAILY_PERIOD = timedelta(days=1)
_WEEKLY_PERIOD = timedelta(days=7)
_DAY_SECONDS = 86400.0


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


def _advance_one_local_day(current: datetime, tz) -> datetime:
    """The true local midnight one real day after ``current`` (itself a
    true local midnight), re-derived from its own epoch instant rather than
    trusted-across-the-gap ``timedelta`` arithmetic.

    This is a straight copy of ``services.reports.by_period``'s own
    single-step advance-and-retry: compute the naive next-day candidate,
    convert its epoch back through ``_to_local`` (which re-resolves the
    offset fresh for that specific instant), floor it, and -- since a
    FALL-BACK transition can make that re-derived, re-floored candidate
    land back on the SAME local calendar day (just under the new, smaller
    offset) rather than genuinely advancing -- keep retrying from the new
    candidate, bounded by the same ``_MAX_BUCKET_KEY_RETRIES``, until the
    calendar bucket key itself moves forward. See ``by_period``'s loop
    comments for the full argument; duplicated here rather than imported as
    a callable because ``by_period`` only exposes this logic inlined in its
    loop, not as a separate function.

    Stepping FORWARD one day at a time this way is what's safe here: a DST
    transition is never exactly at local midnight, so a day's own (already
    correctly resolved) midnight offset always still correctly describes
    the very next midnight too, regardless of whether a transition happens
    later that next day. The reverse is NOT true (a later day's offset does
    not safely describe an earlier midnight) -- which is why
    :func:`_shift_local_day` below always walks forward from an anchor
    placed BEFORE its target, never backward from ``now``.
    """
    current_key = _bucket_key(current, "day")
    candidate = _next_bucket(current, "day")
    next_start = _floor_to_bucket(_to_local(candidate.timestamp(), tz), "day")
    retries = 0
    while (
        _bucket_key(next_start, "day") <= current_key
        and retries < _MAX_BUCKET_KEY_RETRIES
    ):
        candidate = _next_bucket(next_start, "day")
        next_start = _floor_to_bucket(_to_local(candidate.timestamp(), tz), "day")
        retries += 1
    if next_start <= current:
        next_start = candidate
    return next_start


# Comfortably more than any single real DST transition's magnitude (at
# most a couple of hours), so the margin anchor in `_shift_local_day` below
# always lands at least a full calendar day before its target -- see there.
_ADVANCE_MARGIN_DAYS = 2


def _shift_local_day(reference: datetime, days_before: int, tz) -> datetime:
    """The true local midnight ``days_before`` days before ``reference``.

    ``reference`` must already be a true local midnight (correctly resolved
    for its OWN date); ``tz`` is the tzinfo to re-derive under -- every
    caller passes the explicit ``tz`` PARAMETER threaded down from
    :func:`compute_next_due` (``None`` in production), never
    ``reference.tzinfo`` or ``now.tzinfo`` -- see :func:`compute_next_due`'s
    own docstring for why the parameter, not either datetime's own tzinfo,
    must govern every boundary derived here.

    Plain ``timedelta`` subtraction on an aware datetime keeps its existing
    tzinfo/offset attached to the shifted result without ever asking "is
    this still the right offset for the EARLIER date?" -- the exact failure
    class ``services/reports.py``'s module docstring describes, and that
    this module's Finding 2 (see the module docstring) reproduced: a fixed
    offset carried across a DST boundary merges or splits a day by the
    transition's delta. Simply reusing ``_to_local``/``_floor_to_bucket``
    for a single direct jump has the same problem in a subtler form: an
    anchor placed at, say, local NOON of the target day resolves (via
    ``_to_local``) to whatever offset is correct for NOON -- which, on the
    transition day itself, can differ from the offset that is correct for
    that SAME day's MIDNIGHT (e.g. a spring-forward's 2 AM transition means
    noon is already on the new side while midnight was still on the old
    side); flooring noon's correctly-resolved-for-noon offset down to
    hour 0 does not correct for that.

    So instead this walks forward, one real day at a time via
    :func:`_advance_one_local_day` (safe in that direction -- see its
    docstring), starting from an anchor placed comfortably (via
    ``_ADVANCE_MARGIN_DAYS``) BEFORE the target date, until the target
    CALENDAR date (computed with plain, tz-independent ``date`` arithmetic,
    so this part cannot itself be wrong) is reached. This is a direct reuse
    of ``by_period``'s own advance logic (see ``_advance_one_local_day``),
    not a new, independently-DST-aware algorithm.
    """
    if days_before <= 0:
        return reference
    target_date = reference.date() - timedelta(days=days_before)
    margin_ts = (
        reference.timestamp() - (days_before + _ADVANCE_MARGIN_DAYS) * _DAY_SECONDS
    )
    # Bounded well past the number of real days being walked -- generous
    # headroom for the margin plus any per-step fall-back retries -- so a
    # pathological tz can never spin this loop forever (mirroring
    # `_MAX_BUCKET_KEY_RETRIES`'s own role one level up).
    max_steps = days_before + _ADVANCE_MARGIN_DAYS + _MAX_BUCKET_KEY_RETRIES
    return _walk_to_local_midnight(target_date, margin_ts, tz, max_steps)


def _walk_to_local_midnight(
    target_date, anchor_ts: float, tz, max_steps: int
) -> datetime:
    """True local midnight of ``target_date``, found by walking forward one
    real day at a time from ``anchor_ts`` (a timestamp expected to floor to
    at or before ``target_date``) via :func:`_advance_one_local_day`, which
    is DST-safe by construction (see its own docstring: forward-only is the
    safe direction). Shared by :func:`_shift_local_day` (walking back from
    a later reference) and :func:`_true_local_today` (walking forward from
    a safely-earlier anchor to re-derive *today's* own true midnight), so
    every local-midnight boundary this module produces goes through the
    same DST-correct algorithm rather than two independently-written ones
    that could quietly disagree.
    """
    current = _floor_to_bucket(_to_local(anchor_ts, tz), "day")
    steps = 0
    while current.date() < target_date and steps < max_steps:
        current = _advance_one_local_day(current, tz)
        steps += 1
    return current


def _true_local_today(now: datetime, tz) -> datetime:
    """The true local midnight for ``now``'s own calendar date, re-derived
    via ``tz`` rather than trusting ``now.tzinfo`` for that earlier
    wall-clock instant.

    ``now``'s calendar *date* needs no re-derivation -- a single reading of
    ``now`` cannot itself straddle a transition, so ``now.date()`` is
    already correct. What CAN be wrong is which UTC offset midnight of
    that date carries: in production ``now.tzinfo`` is a frozen,
    date-invariant offset (``datetime.now().astimezone()`` -- see the
    module docstring), correct for the moment `now` was read but not
    necessarily for midnight of that same day. On a spring-forward day,
    reading ``now`` after the transition (e.g. 09:00 EDT) and simply
    zeroing its hour/minute/second (``_floor_to_bucket`` alone, the
    previous behaviour) keeps that post-transition EDT offset attached to
    a midnight that was actually still EST -- an hour wrong. Unlike
    ``due_at`` (see ``compute_next_due``'s docstring, which explains why
    THAT hour-scale error is harmless there), this value flows straight
    into ``period_end``/``period_start`` for a ``daily`` schedule and is
    used as a literal report-window boundary (``.timestamp()`` in
    ``run``), where an hour-wrong offset genuinely drops or double-counts
    an hour of sales -- so it must be correct, not merely "close enough
    for a comparison."

    Walks forward from a safely-earlier anchor via
    :func:`_walk_to_local_midnight`, the same mechanism
    :func:`_shift_local_day` uses -- this is that same algorithm applied
    with ``days_before=0`` relative to ``now`` itself, which
    :func:`_shift_local_day` cannot do (it returns ``reference`` unchanged
    for ``days_before <= 0``, precisely because its callers only ever want
    that fast path when re-derivation is unnecessary; here it is not).
    """
    target_date = now.date()
    margin_ts = now.timestamp() - _ADVANCE_MARGIN_DAYS * _DAY_SECONDS
    max_steps = _ADVANCE_MARGIN_DAYS + _MAX_BUCKET_KEY_RETRIES
    return _walk_to_local_midnight(target_date, margin_ts, tz, max_steps)


# Comfortably more than any real DST transition's magnitude, and -- paired
# with the yearly cadence most real zones observe DST on -- virtually
# guaranteed to straddle at least one transition when probing six months
# either side of any `now` a real zone would produce. Used only by
# `_tzinfo_offset_is_frozen` below, never by any actual boundary
# computation.
_FROZEN_TZ_PROBE_SPAN = timedelta(days=182)


def _tzinfo_offset_is_frozen(tz: tzinfo, near: datetime) -> bool:
    """Best-effort: True when ``tz`` reports the SAME UTC offset roughly six
    months before and after ``near`` -- the signature of a frozen,
    date-invariant tzinfo (exactly what ``datetime.now().astimezone()``
    attaches -- see the module docstring) rather than a real zone, which
    would show a different offset across an intervening DST transition for
    almost any six-month span. Used only to decide whether
    :func:`_warn_if_now_tz_incoherent` logs a warning -- never to change any
    actual boundary computation -- so a ``tz`` this heuristic misjudges only
    ever affects a log line, never a computed window.

    Any exception during the probe (an unusual tzinfo that rejects a naive
    datetime, say) is treated as "cannot tell" and answered as NOT frozen,
    so a misbehaving tzinfo can never manufacture a false warning here --
    only, at worst, miss a real one.
    """
    naive = near.replace(tzinfo=None)
    try:
        before = tz.utcoffset(naive - _FROZEN_TZ_PROBE_SPAN)
        after = tz.utcoffset(naive + _FROZEN_TZ_PROBE_SPAN)
    except Exception:
        return False
    return before == after


def _warn_if_now_tz_incoherent(now: datetime, tz: Optional[tzinfo]) -> None:
    """Log (never raise) when ``now.tzinfo`` looks frozen while ``tz`` is a
    genuinely different, date-varying zone -- see the module docstring's
    "now/tz incoherence" paragraph.

    Silent on both legitimate pairings:

    - production's frozen ``now`` (``datetime.now().astimezone()``) with
      ``tz=None`` -- ``tz is None`` returns below before either tzinfo is
      even probed, since ``None`` is itself the correct, DST-live choice
      (see :func:`compute_next_due`'s docstring) and is never incoherent
      with anything.
    - a dynamic ``now`` (a real or synthetic DST-aware tzinfo) paired with
      that SAME dynamic zone as ``tz`` -- ``now.tzinfo`` does not look
      frozen, so the function returns before ``tz`` is even inspected.

    Only warns when ``now.tzinfo`` looks frozen AND ``tz`` is given AND
    ``tz`` itself does NOT look frozen -- exactly the shape of "a real
    dynamic zone was wired into ``tz=`` while ``now`` is still built the
    production way" the review flagged as the plausible future mistake.
    Never raises: this is called from a pure function that must not raise,
    feeding a loop that must not raise either (see the module docstring),
    and the mistake being guarded against is silent bad data, not a crash
    -- a warning makes it visible in the logs without turning either
    contract into one that can blow up on a bad-but-non-fatal argument
    combination.
    """
    now_tz = now.tzinfo
    if now_tz is None or tz is None:
        return
    if not _tzinfo_offset_is_frozen(now_tz, now):
        return
    if _tzinfo_offset_is_frozen(tz, now):
        return
    logger.warning(
        f"report scheduler: now.tzinfo ({now_tz!r}) looks like a frozen, "
        f"date-invariant offset while tz= ({tz!r}) is date-varying -- tz "
        "governs every boundary computed here (see compute_next_due's "
        "docstring), so if `now` was built under a different, dynamic zone "
        "than `tz` describes, the returned window can be silently wrong. "
        "Pass a `now`/`tz` pair that agree, or omit `tz` to let each "
        "boundary resolve through the OS."
    )


def compute_next_due(
    config, now: datetime, tz: Optional[tzinfo] = None
) -> Optional[NextDue]:
    """Pure calendar computation: no I/O, no wall-clock read of its own.

    Given the live ``config`` (only ``config.reports`` is consulted:
    ``schedule``, ``hour``, ``weekday``), an aware, already-local ``now``,
    and an optional ``tz``, return the current cycle's scheduled occurrence
    and the period it covers, or ``None`` when ``schedule == "off"``.

    ``tz`` -- optional and last, defaulting to ``None`` -- is the tzinfo
    handed to ``services.reports``'s ``_to_local``/``_floor_to_bucket`` for
    every BACKWARD-walking boundary this function derives (see
    :func:`_shift_local_day` below). It is deliberately NOT derived from
    ``now.tzinfo``: ``now.tzinfo`` may be a frozen, date-invariant fixed
    offset (exactly what ``datetime.now().astimezone()`` produces -- see the
    module docstring), which cannot re-derive an EARLIER boundary's true
    offset across a DST transition. Passing ``tz=None`` (production's
    default) instead asks ``_to_local`` to resolve each epoch through the OS
    fresh, for that specific instant -- DST-correct. Callers must keep
    ``now`` and ``tz`` consistent -- see the module docstring.

    Purity is what makes this function's tests the heart of the task: given
    the same ``config`` and ``now``, it always returns the same answer,
    touches no recorder, sends no email, and needs no event loop -- so its
    tests can be exhaustive over every hour/weekday/now combination that
    matters without any of that machinery.

    ``now`` is expected to already be in the local timezone the schedule's
    ``hour``/``weekday`` are meant against (mirroring how
    ``services/reports.py`` treats an already-localized datetime). ``today``
    (same calendar day as ``now``) is re-derived via :func:`_true_local_today`
    rather than a plain floor of ``now``: the *calendar date* needs no DST
    care (a single reading of ``now`` cannot itself straddle a transition),
    but the UTC offset midnight of that date carries can still differ from
    ``now.tzinfo``'s own offset when ``now`` is read after a transition that
    already occurred earlier the same day -- see :func:`_true_local_today`'s
    docstring for the mechanism and why this matters here specifically
    (unlike ``due_at`` below, ``today``/``period_end`` for a ``daily``
    schedule is used as a literal report-window boundary, not merely a
    comparand). Any boundary that walks BACKWARD to a different calendar day
    -- ``occurrence_day`` for a ``weekly`` schedule (up to 6 days back) and
    ``period_start`` for both schedules (1 or 7 days back) -- goes through
    :func:`_shift_local_day`, which re-derives that day's true local
    midnight via ``services.reports``'s DST-correct
    ``_to_local``/``_floor_to_bucket`` rather than naive ``timedelta``
    subtraction, so a spring-forward or
    fall-back transition between the two dates cannot merge or split the
    reported window (see the module docstring's Finding 2 note and
    ``_shift_local_day``'s own docstring). ``due_at`` is intentionally left
    as same-day arithmetic on the (possibly re-derived) ``occurrence_day``
    -- tagged with ``occurrence_day``'s own tzinfo rather than independently
    re-resolved for its own hour-of-day -- and that is safe despite being
    off, on the transition day itself, by up to the DST delta (a
    backward-shifted ``weekly`` occurrence's ``due_at`` can carry an
    hour-wrong UTC offset for that literal instant on a transition day)
    because the ONLY thing callers ever do with ``due_at`` is compare
    ``now >= due.due_at``, and that comparison's boolean outcome cannot
    flip either way:

    - For ``daily``, and for a ``weekly`` occurrence that falls on
      ``now``'s own weekday, ``occurrence_day == today``, so ``due_at``
      shares ``today``'s (i.e. ``now``'s) own live, continuously-refreshed
      offset -- the comparison is self-consistent within the single
      instant ``now`` already represents, the same reason ``today`` itself
      needs no re-derivation above.
    - For a backward-shifted ``weekly`` occurrence (``occurrence_day <
      today``), ``now`` is, by construction, at least one full calendar
      day after ``occurrence_day``; an hour-scale tagging error on
      ``due_at`` cannot make ``now >= due_at`` disagree with the true
      answer, since the true gap between them is measured in days, not
      hours.

    One consequence a future reader must not miss: ``due_at``'s literal
    value is therefore NOT reliable as an audit or log timestamp on the
    transition day itself (it can display the wrong UTC offset for that
    instant) -- it is only guaranteed correct as a fire/don't-fire
    *comparand* against ``now``. ``period_key`` stays derived from the calendar
    *date* of ``period_start``, never from an instant, which is what keeps
    the de-dup in :func:`run` immune to any of this DST re-derivation
    changing an already-recorded key's spelling.
    """
    _warn_if_now_tz_incoherent(now, tz)

    schedule = config.reports.schedule
    if schedule == "off":
        return None

    today = _true_local_today(now, tz)

    if schedule == "daily":
        occurrence_day = today
        period_length_days = _DAILY_PERIOD.days
    elif schedule == "weekly":
        target_weekday = config.reports.weekday
        days_back = (today.weekday() - target_weekday) % 7
        occurrence_day = _shift_local_day(today, days_back, tz)
        period_length_days = _WEEKLY_PERIOD.days
    else:
        # Unreachable given ReportsConfig.schedule's Literal type, but a
        # pure function must not raise on an unexpected value either.
        return None

    due_at = occurrence_day + timedelta(hours=config.reports.hour)
    period_end = occurrence_day
    period_start = _shift_local_day(occurrence_day, period_length_days, tz)
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


def _period_already_sent(recorder, period_key: str) -> bool:
    """True if ANY ``report_sent`` event's stored period matches ``period_key``.

    Synchronous DB read -- run through ``asyncio.to_thread`` from `run`.
    Deliberately checks every matching row rather than only the most recent
    one (see the module docstring's Finding 1 note): comparing only the
    latest row misses a match that got buried under a later send for a
    *different* schedule, letting the buried period resend. ``period_key``
    is always ``due.period_key`` as built by :func:`compute_next_due` -- the
    same string :func:`run` later writes into ``metadata["period"]`` on a
    successful send -- so there is exactly one place that constructs this
    key, never two independently-built strings that could drift apart.

    The query's bound is explicit, not merely implicit in the shared
    ``events`` table's own 90-day pruning (``EventRecorder.prune``, driven
    by every event type's writes, not this module -- see the module
    docstring): it matches ``period_key`` in SQL via SQLite's JSON1
    ``json_extract`` (guarded by ``json_valid``, so a malformed
    ``metadata`` value -- which this module never itself writes, only
    corruption could produce one -- is skipped rather than raising
    ``OperationalError``, the SQL-side equivalent of the ``try/except``
    this replaced) and stops at the first hit with ``LIMIT 1``: this
    function only ever needs to know whether ANY matching row exists, never
    how many there are or which one it finds. The retention window itself
    is untouched -- this adds an explicit match-and-stop bound on TOP of
    it, not a replacement for it.

    Reads ``recorder._db_path`` at call time, never cached: see
    ``services/reports.py``'s module docstring for why (corrupt-database
    recovery can reassign it mid-process).
    """
    recorder.flush()
    db_path = recorder._db_path
    with contextlib.closing(sqlite3.connect(db_path)) as conn:
        row = conn.execute(
            "SELECT 1 FROM events WHERE event_type='report_sent' "
            "AND metadata IS NOT NULL AND json_valid(metadata) "
            "AND json_extract(metadata, '$.period')=? "
            "LIMIT 1",
            (period_key,),
        ).fetchone()
    return row is not None


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
    tz: Optional[tzinfo] = None,
) -> None:
    """The supervised scheduler loop. See the module docstring.

    ``tz`` is optional and last, defaulting to ``None``, and is passed
    straight through to :func:`compute_next_due` unchanged every pass --
    production (``main.py``) never passes it, so every boundary is resolved
    per instant through the OS (DST-correct); tests pass their synthetic
    zone explicitly. See the module docstring for why ``tz=None`` is
    DST-correct and why ``now``/``tz`` must stay consistent.
    """
    # Both in-memory only -- deliberately not persisted; see the module
    # docstring's retry-cadence paragraph (Finding 3). `last_failed_period`
    # holds the period_key of the most recent send that failed, so a
    # failure is retried at the next due occurrence rather than on every
    # subsequent bounded-sleep pass. `last_sent_period` is this same
    # in-process guard's mirror for a SUCCESSFUL send (Copilot review,
    # Family D): `recorder.record("report_sent", ...)` below only queues
    # the de-dup marker for the writer thread -- `_period_already_sent`'s
    # own `flush()` returns immediately, without error, both when the
    # writer thread has died and when it is merely delayed past its
    # timeout (see EventRecorder.flush's docstring), so the marker can be
    # invisible to the very next pass's de-dup query even though the send
    # genuinely happened. Without this guard that would re-send the same
    # summary on the next bounded-sleep pass; checking it here prevents
    # that for the remainder of this process's run, the same acceptable
    # scope `last_failed_period` already has (a restart before the marker
    # lands would still resend once -- see below).
    last_failed_period: Optional[str] = None
    last_sent_period: Optional[str] = None
    while True:
        now = clock()  # may raise a test sentinel; see module docstring
        try:
            due = compute_next_due(config, now, tz)
            if (
                due is not None
                and now >= due.due_at
                and due.period_key != last_failed_period
                and due.period_key != last_sent_period
            ):
                already_sent = await asyncio.to_thread(
                    _period_already_sent, recorder, due.period_key
                )
                if not already_sent:
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
                        # Attempt to make the marker durable before the
                        # next pass could possibly run, and surface it
                        # plainly (Family D's "or ... surface a
                        # persistence failure") when it did not land --
                        # last_sent_period below is what actually prevents
                        # a duplicate send either way.
                        landed = await asyncio.to_thread(
                            _period_already_sent, recorder, due.period_key
                        )
                        if not landed:
                            logger.error(
                                "report scheduler: report_sent marker for "
                                f"period {due.period_key} did not persist "
                                "after flush (writer thread delayed past "
                                "its timeout, or it has died) -- relying "
                                "on this process's in-memory guard to "
                                "avoid an immediate duplicate send; a "
                                "restart before the marker lands would "
                                "resend once"
                            )
                        last_sent_period = due.period_key
                        last_failed_period = None
                    else:
                        logger.warning(
                            "report scheduler: send failed for period "
                            f"{due.period_key}; will retry at the next due "
                            "occurrence, not on every pass"
                        )
                        last_failed_period = due.period_key
        except Exception:
            logger.exception("report scheduler: pass failed; will retry next pass")
        await asyncio.sleep(_BOUNDED_SLEEP_SECONDS)
