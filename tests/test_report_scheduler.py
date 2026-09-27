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
from datetime import datetime, timedelta, timezone

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
    due = report_scheduler.compute_next_due(config, now)
    assert due is not None
    assert due.due_at == _dt(2026, 9, 20, 7, 0, 0)
    assert now < due.due_at
    assert due.period_start == _dt(2026, 9, 19, 0, 0, 0)
    assert due.period_end == _dt(2026, 9, 20, 0, 0, 0)
    assert due.period_key == "daily:2026-09-19"


def test_compute_next_due_daily_exactly_at_hour_is_due():
    config = _config(schedule="daily", hour=7)
    now = _dt(2026, 9, 20, 7, 0, 0)
    due = report_scheduler.compute_next_due(config, now)
    assert due.due_at == now
    assert now >= due.due_at
    assert due.period_key == "daily:2026-09-19"


def test_compute_next_due_daily_just_after_hour_is_due():
    config = _config(schedule="daily", hour=7)
    now = _dt(2026, 9, 20, 9, 0, 0)
    due = report_scheduler.compute_next_due(config, now)
    assert now >= due.due_at
    assert due.due_at == _dt(2026, 9, 20, 7, 0, 0)
    assert due.period_key == "daily:2026-09-19"


def test_compute_next_due_weekly_before_hour_on_configured_weekday():
    # weekday=2 -> Wednesday (Monday-based, matches date.weekday()).
    # 2026-09-16 is a Wednesday.
    config = _config(schedule="weekly", hour=9, weekday=2)
    now = _dt(2026, 9, 16, 8, 59, 59)
    due = report_scheduler.compute_next_due(config, now)
    assert due.due_at == _dt(2026, 9, 16, 9, 0, 0)
    assert now < due.due_at
    assert due.period_start == _dt(2026, 9, 9, 0, 0, 0)
    assert due.period_end == _dt(2026, 9, 16, 0, 0, 0)
    assert due.period_key == "weekly:2026-09-09"


def test_compute_next_due_weekly_exactly_at_hour_is_due():
    config = _config(schedule="weekly", hour=9, weekday=2)
    now = _dt(2026, 9, 16, 9, 0, 0)
    due = report_scheduler.compute_next_due(config, now)
    assert now >= due.due_at
    assert due.period_key == "weekly:2026-09-09"


def test_compute_next_due_weekly_on_a_later_weekday_same_cycle():
    # 2026-09-18 is a Friday, two days after the configured Wednesday
    # occurrence -- the whole week's occurrence (and its period) must be
    # unchanged from the Wednesday itself, and always due by now regardless
    # of time-of-day (the occurrence's own hour has necessarily passed).
    config = _config(schedule="weekly", hour=9, weekday=2)
    now = _dt(2026, 9, 18, 0, 0, 1)
    due = report_scheduler.compute_next_due(config, now)
    assert due.due_at == _dt(2026, 9, 16, 9, 0, 0)
    assert now >= due.due_at
    assert due.period_key == "weekly:2026-09-09"


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


async def _drain(config, recorder, mailer, clock):
    """Run the scheduler until the fake clock's sentinel ends it."""
    with pytest.raises(_StopScheduler):
        await report_scheduler.run(config, recorder, mailer, clock)


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
        _config(schedule="daily", hour=7), _dt(2026, 9, 20, 9, 0)
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
        _config(schedule="daily", hour=7), _dt(2026, 9, 20, 9, 0)
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


async def test_run_send_failure_does_not_stop_loop_and_retries(recorder, fast_sleep):
    config = _config(schedule="daily", hour=7)
    mailer = _StubMailer(results=[False, True])
    clock = _FakeClock(
        [
            _dt(2026, 9, 20, 9, 0),  # due -> first attempt fails
            _dt(2026, 9, 20, 9, 30),  # still due (nothing recorded) -> succeeds
            _dt(2026, 9, 20, 10, 0),  # already sent now -> no third attempt
        ]
    )

    await _drain(config, recorder, mailer, clock)

    assert clock.calls == 4
    # Exactly two attempts: the loop kept going after the first failure
    # (proving it doesn't stop) and stopped calling once dedup took over.
    assert len(mailer.calls) == 2
    events = _report_sent_events(recorder)
    assert len(events) == 1
    assert events[0]["period"] == "daily:2026-09-19"


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
