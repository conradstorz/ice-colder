# tests/test_report_scheduler.py
"""Tests for services/report_scheduler.py.

See ``docs/superpowers/specs/2026-09-25-sales-reports-design.md`` §4.2.

``compute_next_due`` (the pure calendar function) is tested directly with no
recorder, no mailer, and no event loop -- these are the exhaustive tests the
task brief calls "the heart of the task".

``run`` (the supervised loop) is tested with:

- a real ``EventRecorder`` against a ``tmp_path`` database, so de-dup and
  catch-up assertions read genuine ``report_sent`` rows rather than a mock;
- a fake ``clock``: a zero-arg callable that returns scripted ``datetime``
  values one per call and raises ``_StopScheduler`` once exhausted -- this
  is what ends ``run``'s otherwise-infinite loop in every test, deliberately
  chosen from the brief's own suggestions ("a clock that raises a
  sentinel") because it changes nothing about ``run``'s production
  behaviour: production's clock is a plain wall-clock read that never
  raises, and the loop only special-cases the clock read itself (see
  ``report_scheduler.run``'s docstring) -- everything else in a pass stays
  under the same broad guard whether under test or in production;
- ``asyncio.sleep`` monkeypatched to a no-op via the ``fast_sleep`` fixture,
  so the bounded 60 s sleep between passes never actually waits -- the
  suite proves the *effect* of the sleep being bounded (a config change
  takes effect within one pass) never the sleep call itself.

Every test that asserts a send happened or was suppressed also asserts the
loop actually ran the pass in question: either by checking ``clock.calls``
(proving every scripted instant, including the one that mattered, was
consumed) or with an explicit positive control -- another instant, in the
very same test, under the very same harness, that DOES send -- so the
suppressing branch cannot be passing merely because it was never reached.
"""

import json
import sqlite3
from datetime import datetime, timedelta, timezone, tzinfo

import pytest

from config.config_model import ConfigModel
from services import report_scheduler
from services.event_recorder import EventRecorder

# A fixed, deterministic offset -- not the machine's own timezone -- so every
# assertion below is independent of whatever system runs the suite. Mirrors
# tests/test_reports.py's own FIXED_TZ convention.
FIXED_TZ = timezone(timedelta(hours=-5))


def _dt(year, month, day, hour=0, minute=0, second=0):
    return datetime(year, month, day, hour, minute, second, tzinfo=FIXED_TZ)


# --------------------------------------------------------------------------
# Synthetic DST tzinfo doubles for Finding 2 -- mirrors
# tests/test_reports.py's own `_SyntheticDstTz`/`_SyntheticFallBackTz`
# (duplicated here rather than imported, since this task's permitted file
# list does not include that test module, and `zoneinfo.ZoneInfo` is not
# usable on this machine -- no tzdata installed, and adding the `tzdata`
# package is forbidden, no new runtime dependency; `pyproject.toml`/
# `uv.lock` must not change). See that module's docstrings for the full
# rationale of the `fromutc`-returns-a-frozen-snapshot design (it is what
# makes these classes reproduce the *specific* bug under review, matching
# what `datetime.astimezone(None)` actually returns in production, rather
# than a "fully dynamic" tzinfo that would self-correct even under the
# unfixed code and prove nothing).
# --------------------------------------------------------------------------


class _SyntheticSpringForwardTz(tzinfo):
    """One hard-coded UTC transition instant: UTC-5 before, UTC-4 after --
    mirrors US Eastern's 2026-03-08 spring-forward (02:00 EST -> 03:00 EDT)."""

    _TRANSITION_UTC = datetime(2026, 3, 8, 7, 0, 0)  # 02:00 EST == 03:00 EDT
    _BEFORE = timedelta(hours=-5)  # EST
    _AFTER = timedelta(hours=-4)  # EDT

    def fromutc(self, dt):
        naive_utc = dt.replace(tzinfo=None)
        offset = self._BEFORE if naive_utc < self._TRANSITION_UTC else self._AFTER
        return (dt + offset).replace(tzinfo=timezone(offset))

    def utcoffset(self, dt):
        if dt is None:
            return self._BEFORE
        naive_local = dt.replace(tzinfo=None) if dt.tzinfo is not None else dt
        local_before_boundary = self._TRANSITION_UTC + self._BEFORE  # 02:00 local
        return self._BEFORE if naive_local < local_before_boundary else self._AFTER

    def dst(self, dt):
        return timedelta(0)

    def tzname(self, dt):
        return "SYN-SPRING"


SPRING_FORWARD_TZ = _SyntheticSpringForwardTz()


class _SyntheticFallBackTz(tzinfo):
    """Mirror image: offset *decreases* across the transition -- mirrors US
    Eastern's 2026-11-01 fall-back (02:00 EDT -> 01:00 EST, both == 06:00 UTC)."""

    _TRANSITION_UTC = datetime(2026, 11, 1, 6, 0, 0)  # 02:00 EDT == 01:00 EST
    _BEFORE = timedelta(hours=-4)  # EDT
    _AFTER = timedelta(hours=-5)  # EST

    def fromutc(self, dt):
        naive_utc = dt.replace(tzinfo=None)
        offset = self._BEFORE if naive_utc < self._TRANSITION_UTC else self._AFTER
        return (dt + offset).replace(tzinfo=timezone(offset))

    def utcoffset(self, dt):
        if dt is None:
            return self._BEFORE
        naive_local = dt.replace(tzinfo=None) if dt.tzinfo is not None else dt
        local_before_boundary = self._TRANSITION_UTC + self._BEFORE  # 02:00 local
        return self._BEFORE if naive_local < local_before_boundary else self._AFTER

    def dst(self, dt):
        return timedelta(0)

    def tzname(self, dt):
        return "SYN-FALLBACK"


FALL_BACK_TZ = _SyntheticFallBackTz()


def _config(
    schedule="off",
    hour=7,
    weekday=0,
    extra_recipients=None,
    owner_email="owner@example.com",
    machine_id="vmc-test",
):
    config = ConfigModel()
    config.machine_id = machine_id
    config.reports.schedule = schedule
    config.reports.hour = hour
    config.reports.weekday = weekday
    config.reports.extra_recipients = list(extra_recipients or [])
    config.physical.people.machine_owner.email = owner_email
    return config


@pytest.fixture
def recorder(tmp_path):
    return EventRecorder(db_path=str(tmp_path / "events.db"))


def _report_sent_events(recorder) -> list[dict]:
    """Read every ``report_sent`` event's metadata straight from the
    database, oldest first -- proof against the actual written state, not
    against anything ``report_scheduler`` itself computed in-process."""
    recorder.flush()
    with sqlite3.connect(recorder._db_path) as conn:
        rows = conn.execute(
            "SELECT metadata FROM events WHERE event_type='report_sent' ORDER BY id ASC"
        ).fetchall()
    return [json.loads(row[0]) for row in rows]


# --------------------------------------------------------------------------
# compute_next_due -- pure, exhaustive
# --------------------------------------------------------------------------


def test_compute_next_due_off_returns_none():
    config = _config(schedule="off", hour=7, weekday=2)
    # An hour/weekday that WOULD be due if the schedule were on, proving
    # "off" is not merely "never reached this hour" by coincidence.
    now = _dt(2026, 9, 20, 12, 0, 0)
    assert report_scheduler.compute_next_due(config, now) is None


def test_compute_next_due_daily_before_hour_is_not_yet_due():
    config = _config(schedule="daily", hour=7)
    now = _dt(2026, 9, 20, 6, 59, 59)
    due = report_scheduler.compute_next_due(config, now, tz=FIXED_TZ)
    assert due is not None
    assert due.due_at == _dt(2026, 9, 20, 7, 0, 0)
    assert now < due.due_at
    assert due.period_start == _dt(2026, 9, 19, 0, 0, 0)
    assert due.period_end == _dt(2026, 9, 20, 0, 0, 0)
    assert due.period_key == "daily:2026-09-19"


def test_compute_next_due_daily_exactly_at_hour_is_due():
    config = _config(schedule="daily", hour=7)
    now = _dt(2026, 9, 20, 7, 0, 0)
    due = report_scheduler.compute_next_due(config, now, tz=FIXED_TZ)
    assert due.due_at == now
    assert now >= due.due_at
    assert due.period_key == "daily:2026-09-19"


def test_compute_next_due_daily_just_after_hour_is_due():
    config = _config(schedule="daily", hour=7)
    now = _dt(2026, 9, 20, 9, 0, 0)
    due = report_scheduler.compute_next_due(config, now, tz=FIXED_TZ)
    assert now >= due.due_at
    assert due.due_at == _dt(2026, 9, 20, 7, 0, 0)
    assert due.period_key == "daily:2026-09-19"


def test_compute_next_due_weekly_before_hour_on_configured_weekday():
    # weekday=2 -> Wednesday (Monday-based, matches date.weekday()).
    # 2026-09-16 is a Wednesday.
    config = _config(schedule="weekly", hour=9, weekday=2)
    now = _dt(2026, 9, 16, 8, 59, 59)
    due = report_scheduler.compute_next_due(config, now, tz=FIXED_TZ)
    assert due.due_at == _dt(2026, 9, 16, 9, 0, 0)
    assert now < due.due_at
    assert due.period_start == _dt(2026, 9, 9, 0, 0, 0)
    assert due.period_end == _dt(2026, 9, 16, 0, 0, 0)
    assert due.period_key == "weekly:2026-09-09"


def test_compute_next_due_weekly_exactly_at_hour_is_due():
    config = _config(schedule="weekly", hour=9, weekday=2)
    now = _dt(2026, 9, 16, 9, 0, 0)
    due = report_scheduler.compute_next_due(config, now, tz=FIXED_TZ)
    assert now >= due.due_at
    assert due.period_key == "weekly:2026-09-09"


def test_compute_next_due_weekly_on_a_later_weekday_same_cycle():
    # 2026-09-18 is a Friday, two days after the configured Wednesday
    # occurrence -- the whole week's occurrence (and its period) must be
    # unchanged from the Wednesday itself, and always due by now regardless
    # of time-of-day (the occurrence's own hour has necessarily passed).
    config = _config(schedule="weekly", hour=9, weekday=2)
    now = _dt(2026, 9, 18, 0, 0, 1)
    due = report_scheduler.compute_next_due(config, now, tz=FIXED_TZ)
    assert due.due_at == _dt(2026, 9, 16, 9, 0, 0)
    assert now >= due.due_at
    assert due.period_key == "weekly:2026-09-09"


# --------------------------------------------------------------------------
# Finding 2 (Important): period_start/period_end must be the TRUE local day,
# re-derived DST-correctly (via services.reports's helpers), not a naive
# `timedelta` shifted under a stale offset snapshot.
# --------------------------------------------------------------------------


def test_compute_next_due_daily_period_is_dst_correct_across_spring_forward():
    # "sent the morning after a spring-forward" -- 2026-03-08 is the
    # transition date (02:00 EST -> 03:00 EDT); `now` is the next morning,
    # entirely EDT, matching the finding's own reproduction exactly.
    config = _config(schedule="daily", hour=7)
    now = datetime(2026, 3, 9, 9, 0, 0, tzinfo=SPRING_FORWARD_TZ)

    due = report_scheduler.compute_next_due(config, now, tz=SPRING_FORWARD_TZ)

    assert due is not None
    # period_end: today's (March 9's) true local midnight -- no transition
    # on this day itself, included as a sanity anchor.
    assert due.period_end == datetime(2026, 3, 9, 0, 0, 0, tzinfo=SPRING_FORWARD_TZ)

    # period_start: March 8's TRUE local midnight is EST (the transition
    # happens at 02:00 that day, after midnight) -- not EDT. The bug this
    # guards against tagged it EDT instead, exactly 3600s off (matching the
    # finding's own measured reproduction: true=1772946000.0, buggy
    # (tagged EDT)=1772942400.0, difference -3600.0).
    true_start = datetime(2026, 3, 8, 0, 0, 0, tzinfo=timezone(timedelta(hours=-5)))
    assert due.period_start.timestamp() == true_start.timestamp()
    assert due.period_start.timestamp() == 1772946000.0

    # The window is exactly the true local day: 23h (82800s) on this
    # transition day -- never a naive 24h (86400s) merging the hour that
    # was skipped.
    window_seconds = due.period_end.timestamp() - due.period_start.timestamp()
    assert window_seconds == 82800.0

    assert due.period_key == "daily:2026-03-08"


def test_compute_next_due_daily_period_is_dst_correct_across_fall_back():
    # Mirror image: 2026-11-01 fall-back (02:00 EDT -> 01:00 EST); `now` is
    # the next morning, entirely EST.
    config = _config(schedule="daily", hour=7)
    now = datetime(2026, 11, 2, 9, 0, 0, tzinfo=FALL_BACK_TZ)

    due = report_scheduler.compute_next_due(config, now, tz=FALL_BACK_TZ)

    assert due is not None
    assert due.period_end == datetime(2026, 11, 2, 0, 0, 0, tzinfo=FALL_BACK_TZ)

    # period_start: November 1's true local midnight is EDT (the fall-back
    # to EST happens at 02:00 that day, after midnight).
    true_start = datetime(2026, 11, 1, 0, 0, 0, tzinfo=timezone(timedelta(hours=-4)))
    assert due.period_start.timestamp() == true_start.timestamp()

    # The window is exactly the true local day: 25h (90000s) on this
    # transition day -- the mirror of the spring-forward test's 82800s --
    # never a naive 24h (86400s) splitting the extra hour into a phantom
    # second bucket.
    window_seconds = due.period_end.timestamp() - due.period_start.timestamp()
    assert window_seconds == 90000.0

    assert due.period_key == "daily:2026-11-01"


def test_compute_next_due_weekly_period_is_dst_correct_across_spring_forward():
    # A weekly schedule whose period_start walks a full 7 days back and
    # lands EXACTLY ON the 2026-03-08 transition day itself -- proves the
    # fix generalises beyond a single-day walk (`period_length_days=7`,
    # `_ADVANCE_MARGIN_DAYS` headroom, and `_shift_local_day`'s forward
    # stepping all exercised over a longer span) while still hitting the
    # transition-day edge case a naive single-jump anchor (e.g. local noon
    # of the target day) gets wrong. 2026-03-15 and 2026-03-08 are both
    # Sundays.
    config = _config(schedule="weekly", hour=9, weekday=6)  # Sunday
    now = datetime(2026, 3, 15, 10, 0, 0, tzinfo=SPRING_FORWARD_TZ)

    due = report_scheduler.compute_next_due(config, now, tz=SPRING_FORWARD_TZ)

    assert due is not None
    assert due.period_end == datetime(2026, 3, 15, 0, 0, 0, tzinfo=SPRING_FORWARD_TZ)
    # period_start: 2026-03-08's true local midnight -- EST (the transition
    # to EDT happens at 02:00 that day, after midnight). The bug this
    # guards against would instead tag it with `now`'s own EDT offset
    # (carried back across the transition), 3600s off truth -- matching
    # the finding's own measured reproduction.
    true_start = datetime(2026, 3, 8, 0, 0, 0, tzinfo=timezone(timedelta(hours=-5)))
    assert due.period_start.timestamp() == true_start.timestamp()
    assert due.period_start.timestamp() == 1772946000.0
    # The 7-day window spans the transition: 167h (601200s), one hour
    # short of a normal 168h (604800s) week.
    window_seconds = due.period_end.timestamp() - due.period_start.timestamp()
    assert window_seconds == 604800.0 - 3600.0
    assert due.period_key == "weekly:2026-03-08"


# --------------------------------------------------------------------------
# tz threading -- this round's fix: `compute_next_due`/`run` must derive
# local boundaries from an explicit, optional `tz` parameter, never from
# `now.tzinfo` (which in production is a frozen, date-invariant offset --
# see the module docstring).
# --------------------------------------------------------------------------


def test_compute_next_due_threads_tz_param_not_now_tzinfo(monkeypatch):
    """The threading test -- this round's key evidence.

    Spies on ``report_scheduler._to_local`` (imported from
    ``services.reports`` into this module's namespace, so patching the
    module-level name here intercepts every call ``_advance_one_local_day``/
    ``_shift_local_day`` make) while still delegating to the real
    implementation, so ``compute_next_due``'s actual behaviour is
    unaffected -- only the ``tz`` argument each call received is recorded.
    A spy was chosen over asserting the resulting boundaries match
    ``_to_local(ts, None)`` independently, because the boundaries alone
    cannot discriminate this bug in general (they coincidentally agree
    whenever there is no DST transition in the walked range, which most
    ``now`` values hit) -- watching the actual argument passed proves
    directly that ``None`` (the per-instant OS-resolution path), not
    ``now.tzinfo``, reaches ``_to_local``, regardless of whether this
    particular ``now`` straddles a transition.

    ``now.tzinfo`` here is ``FIXED_TZ`` -- a real, distinct-from-``None``
    object -- so this MUST FAIL against the code as it stood before this
    round (``tz = now.tzinfo`` in ``compute_next_due``): every recorded
    call would carry ``FIXED_TZ``, not ``None``.
    """
    calls: list = []
    real_to_local = report_scheduler._to_local

    def _spy(ts, tz):
        calls.append(tz)
        return real_to_local(ts, tz)

    monkeypatch.setattr(report_scheduler, "_to_local", _spy)

    config = _config(schedule="daily", hour=7)
    now = _dt(2026, 9, 20, 9, 0, 0)  # aware in FIXED_TZ, not None

    due = report_scheduler.compute_next_due(config, now)  # tz defaults to None

    assert due is not None
    # Positive control: the backward day-walk (period_start) genuinely
    # invoked _to_local at least once -- proving the spy was actually
    # exercised, not merely installed and bypassed.
    assert calls, "expected _to_local to be invoked by the backward day-walk"
    assert all(tz is None for tz in calls)
    assert FIXED_TZ not in calls


def test_compute_next_due_daily_dst_window_controlled_by_tz_param_not_now_tzinfo():
    """Proves the ``tz`` PARAMETER -- not ``now.tzinfo`` -- is what
    controls DST re-derivation, using values that would produce a visibly
    DIFFERENT (wrong) answer if ``now.tzinfo`` were still consulted.

    ``now`` is tagged with a PLAIN, non-DST-aware fixed UTC-4 offset --
    exactly the frozen-offset shape ``datetime.now().astimezone()``
    produces in production (see the module docstring) -- describing the
    SAME instant the spring-forward test above uses (2026-03-09 09:00 EDT
    == 2026-03-09 09:00 under this fixed UTC-4 tzinfo). The DST-aware
    synthetic zone is supplied ONLY via the ``tz=`` parameter. If the
    window were still (wrongly) derived from ``now.tzinfo``, the walk-back
    would see no transition at all and produce a naive 24h (86400s)
    period; deriving it from ``tz`` instead reproduces the true,
    DST-shortened 82800s window from the spring-forward test above. A
    parameter that were NOT actually in control could not produce this
    result from a ``now.tzinfo`` that has no transition to find.
    """
    config = _config(schedule="daily", hour=7)
    now = datetime(2026, 3, 9, 9, 0, 0, tzinfo=timezone(timedelta(hours=-4)))

    due = report_scheduler.compute_next_due(config, now, tz=SPRING_FORWARD_TZ)

    assert due is not None
    assert due.period_end == datetime(
        2026, 3, 9, 0, 0, 0, tzinfo=timezone(timedelta(hours=-4))
    )
    true_start = datetime(2026, 3, 8, 0, 0, 0, tzinfo=timezone(timedelta(hours=-5)))
    assert due.period_start.timestamp() == true_start.timestamp()
    window_seconds = due.period_end.timestamp() - due.period_start.timestamp()
    assert window_seconds == 82800.0  # DST-correct via tz; a naive 86400
    # would mean now.tzinfo (no transition) was consulted instead.


def test_compute_next_due_today_boundary_correct_when_now_read_on_transition_day_itself():
    """Copilot review (PR 21, Family D): every DST test above reads `now`
    the day AFTER its transition, where `now.tzinfo`'s frozen offset
    (production's `datetime.now().astimezone()` shape) already happens to
    be correct for that day's own midnight -- so none of them can catch a
    bug specific to `today`'s own derivation. This test reads `now` ON the
    spring-forward day itself (2026-03-08, transition 02:00 EST -> 03:00
    EDT), after the transition instant, with a frozen -4 (EDT) tzinfo --
    exactly what `astimezone()` gives once the OS clock has crossed into
    EDT. `today`'s TRUE local midnight is still EST (-5): the transition
    happens at 02:00, after midnight. Naively flooring `now` (keeping its
    frozen -4 tzinfo) tags midnight -4 instead -- one hour of that day's
    sales would fall outside `[period_start, period_end)` for the *next*
    day's report (which starts at this wrongly-early `period_end`), and
    the *same* hour would also be excluded from *this* day's own report,
    since here `today` becomes `period_end`, the window's exclusive upper
    bound -- either way an hour of real sales is silently dropped from
    whichever report bounds on this instant.
    """
    config = _config(schedule="daily", hour=7)
    now = datetime(2026, 3, 8, 9, 0, 0, tzinfo=timezone(timedelta(hours=-4)))

    due = report_scheduler.compute_next_due(config, now, tz=SPRING_FORWARD_TZ)

    assert due is not None
    true_period_end = datetime(
        2026, 3, 8, 0, 0, 0, tzinfo=timezone(timedelta(hours=-5))
    )
    assert due.period_end.timestamp() == true_period_end.timestamp()

    # The bug this guards against tags midnight with now.tzinfo's frozen
    # -4 instead of the true -5 -- exactly 3600s EARLIER in true UTC terms
    # (a -4 tag on the same wall-clock midnight resolves to an earlier UTC
    # instant than a -5 tag). Asserted explicitly so a regression back to
    # the naive floor fails loudly here rather than merely not-matching
    # the line above.
    buggy_period_end = datetime(
        2026, 3, 8, 0, 0, 0, tzinfo=timezone(timedelta(hours=-4))
    )
    assert buggy_period_end.timestamp() - true_period_end.timestamp() == -3600.0
    assert due.period_end.timestamp() != buggy_period_end.timestamp()

    # period_start (March 7, entirely pre-transition) is unaffected either
    # way -- confirms the fix didn't disturb the already-correct backward
    # shift, and pins the window to a plain 24h day.
    true_period_start = datetime(
        2026, 3, 7, 0, 0, 0, tzinfo=timezone(timedelta(hours=-5))
    )
    assert due.period_start.timestamp() == true_period_start.timestamp()
    window_seconds = due.period_end.timestamp() - due.period_start.timestamp()
    assert window_seconds == 86400.0


# --------------------------------------------------------------------------
# Item 1 (round-3 hardening): now/tz incoherence must warn, never raise, and
# must stay silent on both legitimate pairings.
# --------------------------------------------------------------------------


def test_now_tz_incoherent_mismatch_warns(caplog):
    """The mismatch case: `now.tzinfo` is a plain, frozen fixed offset (the
    exact shape `datetime.now().astimezone()` produces) while `tz=` is a
    genuinely dynamic zone -- the specific future mistake Item 1 guards
    against. Must log exactly one WARNING naming both tzinfos, and must NOT
    raise (compute_next_due still returns a NextDue)."""
    config = _config(schedule="daily", hour=7)
    now = datetime(2026, 3, 9, 9, 0, 0, tzinfo=timezone(timedelta(hours=-4)))

    with caplog.at_level("WARNING"):
        due = report_scheduler.compute_next_due(config, now, tz=SPRING_FORWARD_TZ)

    assert due is not None  # never raises

    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1
    message = warnings[0].message
    assert "now.tzinfo" in message
    assert "tz=" in message
    # Names both tzinfo reprs so an operator can tell which is which.
    assert repr(now.tzinfo) in message
    assert repr(SPRING_FORWARD_TZ) in message


def test_now_tz_incoherent_silent_on_production_frozen_now_with_tz_none(caplog):
    """Legitimate pairing 1: production's own shape -- a frozen `now.tzinfo`
    with `tz=None`. Must NOT warn: `tz=None` is itself the correct, DST-live
    choice, never incoherent with anything."""
    config = _config(schedule="daily", hour=7)
    now = _dt(2026, 9, 20, 9, 0, 0)  # FIXED_TZ -- a frozen fixed offset

    with caplog.at_level("WARNING"):
        due = report_scheduler.compute_next_due(config, now)  # tz defaults to None

    assert due is not None
    assert [r for r in caplog.records if r.levelname == "WARNING"] == []


def test_now_tz_incoherent_silent_on_dynamic_now_with_matching_dynamic_tz(caplog):
    """Legitimate pairing 2: a test's dynamic `now` (a DST-aware synthetic
    zone) paired with that SAME dynamic zone as `tz=` -- exactly how the
    Finding-2 DST tests above call `compute_next_due`. Must NOT warn."""
    config = _config(schedule="daily", hour=7)
    now = datetime(2026, 3, 9, 9, 0, 0, tzinfo=SPRING_FORWARD_TZ)

    with caplog.at_level("WARNING"):
        due = report_scheduler.compute_next_due(config, now, tz=SPRING_FORWARD_TZ)

    assert due is not None
    assert [r for r in caplog.records if r.levelname == "WARNING"] == []


def test_tzinfo_offset_is_frozen_classifies_fixed_and_dynamic_zones():
    """Direct unit test of the frozen/dynamic detector itself: a plain fixed
    offset (frozen) vs. the synthetic DST zones (dynamic), each probed
    around an instant that actually straddles ITS OWN hard-coded
    transition (the two synthetic zones transition on different dates, so
    each needs its own `now` for the +/-182-day probe to see it)."""
    assert (
        report_scheduler._tzinfo_offset_is_frozen(
            FIXED_TZ, datetime(2026, 3, 9, 9, 0, 0)
        )
        is True
    )
    assert (
        report_scheduler._tzinfo_offset_is_frozen(
            SPRING_FORWARD_TZ, datetime(2026, 3, 9, 9, 0, 0)
        )
        is False
    )
    assert (
        report_scheduler._tzinfo_offset_is_frozen(
            FALL_BACK_TZ, datetime(2026, 11, 2, 9, 0, 0)
        )
        is False
    )


def test_local_now_returns_an_aware_datetime():
    """`local_now` is the scheduler's production clock: it must return an
    aware `datetime` (a naive one would break every `tz`-aware boundary
    calculation it feeds)."""
    now = report_scheduler.local_now()
    assert now.tzinfo is not None
    assert now.utcoffset() is not None


# --------------------------------------------------------------------------
# Item 4 (round-3 hardening): the dedup query's bound is now explicit
# (LIMIT 1 + an in-SQL period match), not merely implicit in the shared
# table's pruning -- behaviour must be unchanged.
# --------------------------------------------------------------------------


def test_period_already_sent_true_for_a_matching_row(recorder):
    recorder.record(
        "report_sent",
        metadata={
            "period": "daily:2026-09-19",
            "schedule": "daily",
            "period_start": "2026-09-19",
            "period_end": "2026-09-20",
        },
    )
    recorder.flush()
    assert report_scheduler._period_already_sent(recorder, "daily:2026-09-19") is True
    assert report_scheduler._period_already_sent(recorder, "daily:2026-09-20") is False


def test_period_already_sent_tolerates_malformed_metadata_row(recorder):
    """A malformed `metadata` value (corruption, never this module's own
    write) must be skipped, not raise -- proving `json_valid` actually
    guards `json_extract` rather than merely looking like it should."""
    conn = sqlite3.connect(recorder._db_path)
    with conn:
        conn.execute(
            "INSERT INTO events (event_type, timestamp, value, metadata) "
            "VALUES ('report_sent', 0, 1.0, ?)",
            ("not json",),
        )
    conn.close()

    assert report_scheduler._period_already_sent(recorder, "daily:2026-09-19") is False

    # A genuine match alongside the malformed row is still found.
    recorder.record(
        "report_sent",
        metadata={
            "period": "daily:2026-09-19",
            "schedule": "daily",
            "period_start": "2026-09-19",
            "period_end": "2026-09-20",
        },
    )
    recorder.flush()
    assert report_scheduler._period_already_sent(recorder, "daily:2026-09-19") is True


# --------------------------------------------------------------------------
# run() -- the supervised loop
# --------------------------------------------------------------------------


class _StopScheduler(Exception):
    """Raised by the fake clock once its scripted values are exhausted --
    the sentinel that ends `run`'s otherwise-infinite loop in tests. Never
    raised by any real clock; see the module docstring."""


class _FakeClock:
    """Zero-arg callable returning scripted datetimes, one per call.

    An item may be a plain ``datetime`` or a ``(datetime, callback)`` pair;
    the callback (if given) runs first, letting a test mutate the live
    config "between passes" -- exactly what a config edit reaching the
    scheduler between one bounded sleep and the next looks like in
    production. Counts every call (including the one that raises) so tests
    can prove the loop actually walked every scripted instant.
    """

    def __init__(self, steps):
        self._steps = list(steps)
        self.calls = 0

    def __call__(self):
        self.calls += 1
        if not self._steps:
            raise _StopScheduler()
        step = self._steps.pop(0)
        if isinstance(step, tuple):
            value, callback = step
            callback()
            return value
        return step


class _StubMailer:
    """Async stand-in for ``services.mailer.send_email``. Records every
    call's arguments (never log text) so tests assert on behaviour."""

    def __init__(self, results=None):
        self._results = list(results) if results is not None else None
        self.calls: list[dict] = []

    async def __call__(self, email_config, to, subject, body):
        self.calls.append(
            {
                "email_config": email_config,
                "to": to,
                "subject": subject,
                "body": body,
            }
        )
        if self._results is None:
            return True
        return self._results.pop(0) if self._results else True


@pytest.fixture
def fast_sleep(monkeypatch):
    """Neutralize the loop's real ``asyncio.sleep(60)`` so tests run
    instantly; the loop's *effect* (what happened by the time it next reads
    the clock) is what every test actually asserts on."""

    async def _noop(_seconds):
        return None

    monkeypatch.setattr(report_scheduler.asyncio, "sleep", _noop)


async def _drain(config, recorder, mailer, clock, tz=FIXED_TZ):
    """Run the scheduler until the fake clock's sentinel ends it.

    ``tz`` defaults to ``FIXED_TZ`` -- every ``run`` test below uses ``_dt``
    (``FIXED_TZ``-aware) ``now`` values, and, like ``compute_next_due``
    itself, would otherwise fall through to ``tz=None`` (the machine's own
    real timezone) and become host-dependent, exactly what this task warns
    against. Production (``main.py``) never passes ``tz`` at all, so it
    still gets ``None``, unaffected by this test default.
    """
    with pytest.raises(_StopScheduler):
        await report_scheduler.run(config, recorder, mailer, clock, tz)


async def test_run_off_schedule_sends_nothing(recorder, fast_sleep):
    config = _config(schedule="off", hour=7)
    mailer = _StubMailer()
    clock = _FakeClock(
        [
            _dt(2026, 9, 20, 6, 0),
            _dt(2026, 9, 20, 7, 0),
            # 9am is well past hour=7 -- if `off` were somehow ignored this
            # instant would send.
            _dt(2026, 9, 20, 9, 0),
        ]
    )

    await _drain(config, recorder, mailer, clock)

    # Every scripted instant (plus the sentinel call) was consumed, so the
    # 9am pass genuinely ran and still sent nothing.
    assert clock.calls == 4
    assert mailer.calls == []
    assert _report_sent_events(recorder) == []

    # Positive control: at that same instant, a daily schedule at this hour
    # really would be due -- proving `off` is what suppressed the send,
    # not that 9am was never going to be "due" in the first place.
    due = report_scheduler.compute_next_due(
        _config(schedule="daily", hour=7), _dt(2026, 9, 20, 9, 0), tz=FIXED_TZ
    )
    assert due is not None
    assert _dt(2026, 9, 20, 9, 0) >= due.due_at


async def test_run_daily_sends_once_and_dedupes_same_period(recorder, fast_sleep):
    config = _config(
        schedule="daily", hour=7, owner_email="owner@example.com", machine_id="vmc-1"
    )
    mailer = _StubMailer()
    clock = _FakeClock(
        [
            _dt(2026, 9, 20, 6, 0),  # not yet due
            _dt(2026, 9, 20, 9, 0),  # due -> sends
            _dt(2026, 9, 20, 11, 0),  # same period, already sent -> no resend
        ]
    )

    await _drain(config, recorder, mailer, clock)

    assert clock.calls == 4
    assert len(mailer.calls) == 1
    call = mailer.calls[0]
    assert call["to"] == "owner@example.com"
    assert call["subject"] == "vmc-1 sales summary (2026-09-19)"
    assert "Revenue: 0.00" in call["body"]
    assert "Vends: 0" in call["body"]

    events = _report_sent_events(recorder)
    assert len(events) == 1
    assert events[0]["period"] == "daily:2026-09-19"
    assert events[0]["schedule"] == "daily"
    assert events[0]["period_start"] == "2026-09-19"
    assert events[0]["period_end"] == "2026-09-20"


def _make_recorder_with_dead_writer(tmp_path, monkeypatch) -> EventRecorder:
    """An EventRecorder whose writer thread's initial connect fails, so
    every ``record()`` call queues forever and ``flush()`` returns
    immediately without error (see EventRecorder.flush's own docstring).
    Mirrors tests/test_event_recorder.py's own
    ``TestWriterThreadDeadGuard._make_recorder_with_dead_writer`` --
    duplicated here rather than imported, for the same reason this file
    duplicates the DST synthetic tzinfo classes above (this task's
    permitted file list does not include that test module).
    """
    real_connect = sqlite3.connect

    def fake_connect(*args, **kwargs):
        if kwargs.get("check_same_thread") is False:
            raise sqlite3.OperationalError(
                "simulated: writer thread could not open database"
            )
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", fake_connect)
    db = str(tmp_path / "events.db")
    rec = EventRecorder(db_path=db)
    # Give the daemon thread a moment to actually run and die -- avoids a
    # race where is_alive() is checked before it has even tried to connect.
    rec._writer.join(timeout=2.0)
    assert not rec._writer.is_alive(), (
        "test setup bug: the writer thread did not die as intended"
    )
    return rec


async def test_run_writer_thread_dead_does_not_resend_within_same_process(
    tmp_path, monkeypatch, fast_sleep
):
    """Family D (Copilot review): report_sent is only QUEUED on the writer
    thread by `recorder.record(...)`. If that thread has died,
    `_period_already_sent`'s own `flush()` returns immediately (by design,
    see EventRecorder.flush) without ever writing the marker, so the very
    next pass's de-dup query sees no matching row. A naive implementation
    relying solely on that DB read would resend the same summary on every
    subsequent pass; the in-process `last_sent_period` guard must prevent
    that for the rest of this run, even though the database itself can
    never durably confirm the send happened.
    """
    recorder = _make_recorder_with_dead_writer(tmp_path, monkeypatch)
    config = _config(schedule="daily", hour=7, owner_email="owner@example.com")
    mailer = _StubMailer()
    clock = _FakeClock(
        [
            _dt(2026, 9, 20, 9, 0),  # due -> sends; marker can never land
            _dt(2026, 9, 20, 11, 0),  # same period -- must not resend
            _dt(2026, 9, 20, 13, 0),  # same period again -- must not resend
        ]
    )

    await _drain(config, recorder, mailer, clock)

    assert clock.calls == 4  # every scripted pass genuinely ran
    # Exactly one send across all three passes, despite the durable marker
    # never landing.
    assert len(mailer.calls) == 1

    # Proves the marker genuinely never made it into the database -- the
    # in-process guard under test is the ONLY thing that prevented a
    # resend here, not a lucky durable write this test failed to break.
    with sqlite3.connect(recorder._db_path) as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM events WHERE event_type='report_sent'"
        ).fetchone()[0]
    assert count == 0


async def test_run_dedup_checks_all_sent_periods_not_only_the_latest_row(
    recorder, fast_sleep
):
    """Finding 1 (Critical): daily -> weekly -> daily must not resend a
    period already sent, even though the weekly send in between becomes the
    newest ``report_sent`` row. Reproduces the reviewer's exact sequence:
    ``daily`` sends ``daily:2026-09-19``; the admin switches to ``weekly``,
    which sends a *different* period and becomes the most recent row; the
    admin switches back to ``daily`` while ``daily:2026-09-19`` is still the
    due period -- comparing only against the latest row would resend it.
    """
    config = _config(schedule="daily", hour=7)
    mailer = _StubMailer()

    def _switch_to_weekly():
        config.reports.schedule = "weekly"
        config.reports.weekday = 6  # Sunday -- 2026-09-20 is a Sunday
        config.reports.hour = 10

    def _switch_back_to_daily():
        config.reports.schedule = "daily"
        config.reports.hour = 7

    clock = _FakeClock(
        [
            _dt(2026, 9, 20, 9, 0),  # pass 1: daily due -> sends daily:2026-09-19
            # pass 2: switches to weekly right before this instant, which is
            # due for the weekly cycle -> sends a DIFFERENT period, becoming
            # the newest report_sent row.
            (_dt(2026, 9, 20, 10, 0), _switch_to_weekly),
            # pass 3: switches back to daily right before this instant --
            # daily:2026-09-19 (pass 1's period) is STILL the due period
            # (same calendar day, before the next day's due_at arrives).
            (_dt(2026, 9, 20, 11, 0), _switch_back_to_daily),
        ]
    )

    await _drain(config, recorder, mailer, clock)

    # Every scripted instant (plus the sentinel call) was consumed, so pass
    # 3 genuinely ran.
    assert clock.calls == 4
    # Exactly two sends -- daily:2026-09-19 once, and the weekly period
    # once -- never a THIRD (duplicate) send of daily:2026-09-19 at pass 3.
    assert len(mailer.calls) == 2
    assert mailer.calls[0]["subject"] == "vmc-test sales summary (2026-09-19)"

    events = _report_sent_events(recorder)
    daily_events = [e for e in events if e["period"] == "daily:2026-09-19"]
    assert len(daily_events) == 1  # never duplicated
    weekly_events = [e for e in events if e["period"] == "weekly:2026-09-13"]
    assert len(weekly_events) == 1

    # Positive control: pass 3's instant, under `daily` as it stood right
    # after the switch-back, genuinely WAS due for daily:2026-09-19 again --
    # proving the dedup (not "never reached due") is what stopped the
    # resend.
    due_at_pass3 = report_scheduler.compute_next_due(
        _config(schedule="daily", hour=7), _dt(2026, 9, 20, 11, 0), tz=FIXED_TZ
    )
    assert due_at_pass3.period_key == "daily:2026-09-19"
    assert _dt(2026, 9, 20, 11, 0) >= due_at_pass3.due_at


async def test_run_switch_daily_to_off_suppresses_a_send_that_was_due(
    recorder, fast_sleep
):
    config = _config(schedule="daily", hour=7)
    mailer = _StubMailer()

    def _turn_off():
        config.reports.schedule = "off"

    clock = _FakeClock(
        [
            _dt(2026, 9, 20, 6, 0),  # pass 1: not yet due (still daily)
            # pass 2: config flips to off right before this instant, which
            # would otherwise be due.
            (_dt(2026, 9, 20, 9, 0), _turn_off),
        ]
    )

    await _drain(config, recorder, mailer, clock)

    assert clock.calls == 3
    assert mailer.calls == []
    assert _report_sent_events(recorder) == []

    # Positive control: the very same instant, under the schedule as it
    # stood right before the flip, genuinely was due.
    due = report_scheduler.compute_next_due(
        _config(schedule="daily", hour=7), _dt(2026, 9, 20, 9, 0), tz=FIXED_TZ
    )
    assert _dt(2026, 9, 20, 9, 0) >= due.due_at


async def test_run_switch_off_to_daily_schedules_from_the_new_hour(
    recorder, fast_sleep
):
    config = _config(schedule="off", hour=7)
    mailer = _StubMailer()

    def _turn_on_with_new_hour():
        config.reports.schedule = "daily"
        config.reports.hour = 20

    clock = _FakeClock(
        [
            # 8am is past the OLD default hour (7) but the schedule is off.
            _dt(2026, 9, 20, 8, 0),
            # Flips on with a NEW hour (20) right before this instant, which
            # is before the new hour -- must NOT send on the stale hour=7.
            (_dt(2026, 9, 20, 8, 30), _turn_on_with_new_hour),
            # Past the new hour -> sends.
            _dt(2026, 9, 20, 21, 0),
        ]
    )

    await _drain(config, recorder, mailer, clock)

    assert clock.calls == 4
    assert len(mailer.calls) == 1  # not at pass 2 (stale hour), only pass 3
    events = _report_sent_events(recorder)
    assert len(events) == 1
    assert events[0]["period"] == "daily:2026-09-19"


async def test_run_period_already_sent_is_suppressed_next_period_sends(
    recorder, fast_sleep
):
    config = _config(schedule="daily", hour=7)
    # Pre-existing report_sent for the period about to be due.
    recorder.record(
        "report_sent",
        metadata={
            "period": "daily:2026-09-19",
            "schedule": "daily",
            "period_start": "2026-09-19",
            "period_end": "2026-09-20",
        },
    )
    recorder.flush()

    mailer = _StubMailer()
    clock = _FakeClock(
        [
            _dt(2026, 9, 20, 9, 0),  # due for daily:2026-09-19 -- already sent
            _dt(2026, 9, 21, 9, 0),  # due for daily:2026-09-20 -- must send
        ]
    )

    await _drain(config, recorder, mailer, clock)

    assert clock.calls == 3
    # Exactly one send -- the second pass (positive control), never the
    # first (already-sent period).
    assert len(mailer.calls) == 1
    events = _report_sent_events(recorder)
    assert len(events) == 2
    assert events[0]["period"] == "daily:2026-09-19"  # the pre-seeded one
    assert events[1]["period"] == "daily:2026-09-20"  # the new send


async def test_run_startup_catchup_sends_exactly_one_most_recent_period(
    recorder, fast_sleep
):
    config = _config(schedule="daily", hour=7)
    # A stale send from many days back -- the process (or the schedule)
    # was off for a long stretch.
    recorder.record(
        "report_sent",
        metadata={
            "period": "daily:2026-09-01",
            "schedule": "daily",
            "period_start": "2026-09-01",
            "period_end": "2026-09-02",
        },
    )
    recorder.flush()

    mailer = _StubMailer()
    clock = _FakeClock(
        [
            _dt(2026, 9, 20, 9, 0),  # "startup": many days after the stale send
            _dt(2026, 9, 20, 9, 30),  # same day/period -- must NOT resend
        ]
    )

    await _drain(config, recorder, mailer, clock)

    assert clock.calls == 3
    assert len(mailer.calls) == 1  # exactly one catch-up, never a backlog
    events = _report_sent_events(recorder)
    assert len(events) == 2  # the stale one, plus exactly one new send
    assert events[-1]["period"] == "daily:2026-09-19"  # most recent completed
    # day, never 09-02, 09-03, ... -- proof there is no backlog iteration.


async def test_run_send_failure_retries_at_next_due_period_not_next_pass(
    recorder, fast_sleep
):
    """Finding 3 (Important, spec-conformance): §4.2 says a failed send
    "logs and retries at the next due TIME" -- for `daily` that is the
    following day's occurrence, not sixty seconds later. This test
    (authorised by the task brief to replace the old
    `test_run_send_failure_does_not_stop_loop_and_retries`, which asserted
    the OLD, spec-contradicting every-pass retry cadence) asserts: the loop
    survives the failure, the SAME period is not retried on the very next
    pass (still the same day, still due), and a retry does happen once the
    NEXT period becomes due (the following day).
    """
    config = _config(schedule="daily", hour=7)
    mailer = _StubMailer(results=[False, True])
    clock = _FakeClock(
        [
            _dt(2026, 9, 20, 9, 0),  # due for daily:2026-09-19 -> fails
            _dt(2026, 9, 20, 9, 30),  # SAME period, still due -> must NOT retry
            _dt(2026, 9, 21, 9, 0),  # NEXT period due (daily:2026-09-20) -> retries
        ]
    )

    await _drain(config, recorder, mailer, clock)

    # Every scripted instant (plus the sentinel call) was consumed, so the
    # 9:30 pass -- the one that must NOT retry -- genuinely ran.
    assert clock.calls == 4
    # Exactly two attempts total: the initial failure, and the retry once
    # the NEXT period became due -- never a third, pass-driven attempt at
    # 9:30 for the SAME period. This also proves the loop survives the
    # failure (a second attempt happens at all) without resorting to the
    # every-60s cadence the spec forbids.
    assert len(mailer.calls) == 2
    assert mailer.calls[0]["subject"] == "vmc-test sales summary (2026-09-19)"
    assert mailer.calls[1]["subject"] == "vmc-test sales summary (2026-09-20)"

    # The failed period never wrote a report_sent row; only the retried,
    # successful one did.
    events = _report_sent_events(recorder)
    assert len(events) == 1
    assert events[0]["period"] == "daily:2026-09-20"


async def test_run_recipients_are_owner_plus_extra_deduplicated(recorder, fast_sleep):
    config = _config(
        schedule="daily",
        hour=7,
        owner_email="owner@example.com",
        extra_recipients=["owner@example.com", "second@example.com"],
    )
    mailer = _StubMailer()
    clock = _FakeClock([_dt(2026, 9, 20, 9, 0)])

    await _drain(config, recorder, mailer, clock)

    assert clock.calls == 2
    assert len(mailer.calls) == 1
    assert mailer.calls[0]["to"] == "owner@example.com, second@example.com"
