# tests/test_reports.py
import csv
import io
import sqlite3
import time
from datetime import datetime, timedelta, timezone, tzinfo

import pytest

from services import reports
from services.event_recorder import EventRecorder

# A fixed, DST-free offset -- deliberately not the machine's own timezone, so
# bucket-boundary assignment is proven independent of whatever system the
# tests happen to run on. Passed explicitly as `tz=` to every by_period call
# in this file.
FIXED_TZ = timezone(timedelta(hours=-5))


def _dt(year, month, day, hour=0, minute=0, second=0):
    return datetime(year, month, day, hour, minute, second, tzinfo=FIXED_TZ)


class _SyntheticDstTz(tzinfo):
    """Deterministic stand-in for a real DST spring-forward transition.

    `zoneinfo.ZoneInfo` is not usable on this machine: there is no tzdata
    installed, and `ZoneInfo("America/New_York")` raises
    `ZoneInfoNotFoundError`. Adding the `tzdata` package is forbidden (no
    new runtime dependency; `pyproject.toml`/`uv.lock` must not change).
    This hard-codes ONE UTC transition instant -- UTC-5 before it, UTC-4
    after, mirroring US Eastern's spring-forward -- using only the
    standard library. That is arguably better than a real zone for a test
    anyway: fully deterministic and immune to any future tz-database
    change.

    `fromutc` deliberately returns a datetime whose attached tzinfo is a
    plain, FROZEN `datetime.timezone` snapshot -- exactly what
    `datetime.astimezone(None)` returns in production (see
    `services.reports._to_local`) -- rather than `self`. That is what
    makes this class reproduce the *specific* bug under review: code that
    calls `_to_local` once and then advances by `timedelta` alone carries
    that stale, `dt`-independent snapshot forward and never re-derives the
    true offset for a later date. (A "fully dynamic" tzinfo whose
    `utcoffset()` genuinely reads its `dt` argument on every call would
    self-correct even under the old, unfixed loop -- via Python's own
    aware-datetime subtraction always re-deriving `.utcoffset()` from
    each operand's own fields -- which would prove nothing about the bug
    this class exists to catch. Verified empirically before writing this
    class: a naively "dynamic" tzinfo passed explicitly does NOT reproduce
    the merge: only a snapshot-per-call tzinfo, matching what
    `astimezone(None)` actually returns, does.)
    """

    _TRANSITION_UTC = datetime(2026, 3, 8, 7, 0, 0)  # 02:00 EST == 03:00 EDT
    _BEFORE = timedelta(hours=-5)  # EST
    _AFTER = timedelta(hours=-4)  # EDT

    def fromutc(self, dt):
        naive_utc = dt.replace(tzinfo=None)
        offset = self._BEFORE if naive_utc < self._TRANSITION_UTC else self._AFTER
        return (dt + offset).replace(tzinfo=timezone(offset))

    def utcoffset(self, dt):
        # Reached only when this class is attached directly to a
        # wall-clock datetime (as `_syn_midnight` below does, to build
        # ground-truth "true local midnight" instants for the test) --
        # never reached via `fromutc` above, which returns a frozen
        # `datetime.timezone` instead of `self`.
        if dt is None:
            return self._BEFORE
        naive_local = dt.replace(tzinfo=None) if dt.tzinfo is not None else dt
        local_before_boundary = self._TRANSITION_UTC + self._BEFORE  # 02:00 local
        return self._BEFORE if naive_local < local_before_boundary else self._AFTER

    def dst(self, dt):
        return timedelta(0)

    def tzname(self, dt):
        return "SYN"


SYN_TZ = _SyntheticDstTz()


def _syn_midnight(year, month, day):
    """True local midnight on the given date, in `SYN_TZ`."""
    return datetime(year, month, day, tzinfo=SYN_TZ)


class _SyntheticFallBackTz(tzinfo):
    """Deterministic stand-in for a real DST FALL-BACK transition.

    Mirror image of `_SyntheticDstTz` above: the offset *decreases* across
    the transition (-04:00 -> -05:00, mirroring US Eastern's fall back from
    EDT to EST) instead of increasing. Same rationale for existing
    (no `zoneinfo`/`tzdata` on this machine, none may be added) and the
    same `fromutc`-returns-a-frozen-offset trick so this class reproduces
    the *specific* bug under review: a loop that re-derives and re-floors
    the next boundary but judges advancement by epoch alone. On this
    fall-back direction, the re-derived, re-floored candidate can land back
    on the SAME local calendar bucket as `current` -- just under the new,
    smaller-magnitude offset -- while its epoch is still strictly greater
    (by exactly the one-hour DST delta). That is precisely the case an
    epoch-only `next_start <= current` guard cannot see.

    The transition instant: local wall time reaches 02:00 EDT (-04:00) and
    falls back to 01:00 EST (-05:00) -- both equal to 06:00 UTC on
    2026-11-01, which is `_TRANSITION_UTC` below. Only used at local
    midnight and other non-ambiguous hours in this test file, never inside
    the repeated 01:00-02:00 local hour, so there is no ambiguity to
    resolve for the instants this file actually builds.
    """

    _TRANSITION_UTC = datetime(2026, 11, 1, 6, 0, 0)  # 02:00 EDT == 01:00 EST
    _BEFORE = timedelta(hours=-4)  # EDT
    _AFTER = timedelta(hours=-5)  # EST

    def fromutc(self, dt):
        naive_utc = dt.replace(tzinfo=None)
        offset = self._BEFORE if naive_utc < self._TRANSITION_UTC else self._AFTER
        return (dt + offset).replace(tzinfo=timezone(offset))

    def utcoffset(self, dt):
        # Reached only when this class is attached directly to a
        # wall-clock datetime (as `_syn_fb_midnight` below does) -- never
        # reached via `fromutc` above, which returns a frozen
        # `datetime.timezone` instead of `self`.
        if dt is None:
            return self._BEFORE
        naive_local = dt.replace(tzinfo=None) if dt.tzinfo is not None else dt
        local_before_boundary = self._TRANSITION_UTC + self._BEFORE  # 02:00 local
        return self._BEFORE if naive_local < local_before_boundary else self._AFTER

    def dst(self, dt):
        return timedelta(0)

    def tzname(self, dt):
        return "SYNFB"


SYN_FB_TZ = _SyntheticFallBackTz()


def _syn_fb_midnight(year, month, day):
    """True local midnight on the given date, in `SYN_FB_TZ`."""
    return datetime(year, month, day, tzinfo=SYN_FB_TZ)


@pytest.fixture
def recorder(tmp_path):
    return EventRecorder(db_path=str(tmp_path / "events.db"))


# --------------------------------------------------------------------------
# is_cash (re-exported from services.event_recorder)
# --------------------------------------------------------------------------


class TestIsCash:
    @pytest.mark.parametrize(
        "method",
        ["cash_coin", "cash_bill", "cash", "coin", "bill"],
    )
    def test_true_for_cash_class_methods(self, method):
        assert reports.is_cash(method) is True

    @pytest.mark.parametrize("method", ["card", "nfc", "test"])
    def test_false_for_non_cash_methods(self, method):
        assert reports.is_cash(method) is False


# --------------------------------------------------------------------------
# resolve_window / WINDOW_PRESETS
# --------------------------------------------------------------------------


class TestResolveWindow:
    def test_known_presets_relative_to_injectable_now(self):
        now = 1_700_000_000.0
        day = 86400.0
        assert reports.resolve_window("7d", now=now) == (now - 7 * day, now)
        assert reports.resolve_window("30d", now=now) == (now - 30 * day, now)
        assert reports.resolve_window("90d", now=now) == (now - 90 * day, now)
        assert reports.resolve_window("12m", now=now) == (now - 365 * day, now)
        assert reports.resolve_window("all", now=now) == (0.0, now)

    def test_unknown_range_falls_back_to_30d(self):
        now = 1_700_000_000.0
        assert reports.resolve_window("bogus-range", now=now) == reports.resolve_window(
            "30d", now=now
        )


# --------------------------------------------------------------------------
# Bucketing: local timezone, Monday-start weeks, boundary -> later bucket
# --------------------------------------------------------------------------


class TestByPeriodBucketing:
    def test_day_boundary_belongs_to_later_bucket(self, recorder):
        boundary = _dt(2026, 1, 15)  # midnight, fixed tz
        before = boundary - timedelta(seconds=1)

        recorder.record_sale(
            "A", "Alpha", 1, 3.00, {"cash": 3.00}, ts=boundary.timestamp()
        )
        recorder.record_sale(
            "B", "Beta", 2, 5.00, {"cash": 5.00}, ts=before.timestamp()
        )

        window = (before.timestamp() - 2 * 86400, boundary.timestamp() + 86400)
        rows = reports.by_period(recorder, window, "day", tz=FIXED_TZ)
        by_start = {r["bucket_start"]: r for r in rows}

        later_bucket = by_start[boundary.isoformat()]
        earlier_bucket = by_start[(boundary - timedelta(days=1)).isoformat()]

        # The sale exactly on the boundary lands in the bucket that STARTS
        # at the boundary (the later day), not the one ending there.
        assert later_bucket["revenue"] == pytest.approx(3.00)
        assert later_bucket["vends"] == 1
        assert earlier_bucket["revenue"] == pytest.approx(5.00)
        assert earlier_bucket["vends"] == 1

    def test_week_boundary_monday_start_belongs_to_later_bucket(self, recorder):
        monday = _dt(2026, 1, 5)  # confirmed Monday, midnight, fixed tz
        sunday_end = monday - timedelta(seconds=1)  # previous Sunday 23:59:59

        recorder.record_sale(
            "A", "Alpha", 1, 2.00, {"cash": 2.00}, ts=monday.timestamp()
        )
        recorder.record_sale(
            "B", "Beta", 2, 9.00, {"cash": 9.00}, ts=sunday_end.timestamp()
        )

        window = (sunday_end.timestamp() - 8 * 86400, monday.timestamp() + 8 * 86400)
        rows = reports.by_period(recorder, window, "week", tz=FIXED_TZ)
        by_start = {r["bucket_start"]: r for r in rows}

        later_week = by_start[monday.isoformat()]
        earlier_week = by_start[(monday - timedelta(days=7)).isoformat()]

        assert later_week["revenue"] == pytest.approx(2.00)
        assert earlier_week["revenue"] == pytest.approx(9.00)
        # Every bucket_start in a week-bucketed result must itself be a
        # Monday -- proves weeks start Monday, not merely that this one did.
        for r in rows:
            bucket_dt = datetime.fromisoformat(r["bucket_start"])
            assert bucket_dt.weekday() == 0

    def test_month_boundary_belongs_to_later_bucket(self, recorder):
        first_of_feb = _dt(2026, 2, 1)
        end_of_jan = first_of_feb - timedelta(seconds=1)

        recorder.record_sale(
            "A", "Alpha", 1, 4.00, {"cash": 4.00}, ts=first_of_feb.timestamp()
        )
        recorder.record_sale(
            "B", "Beta", 2, 6.00, {"cash": 6.00}, ts=end_of_jan.timestamp()
        )

        window = (
            end_of_jan.timestamp() - 40 * 86400,
            first_of_feb.timestamp() + 40 * 86400,
        )
        rows = reports.by_period(recorder, window, "month", tz=FIXED_TZ)
        by_start = {r["bucket_start"]: r for r in rows}

        later_month = by_start[first_of_feb.isoformat()]
        earlier_month = by_start[_dt(2026, 1, 1).isoformat()]

        assert later_month["revenue"] == pytest.approx(4.00)
        assert earlier_month["revenue"] == pytest.approx(6.00)


# --------------------------------------------------------------------------
# Finding 1 (Critical): bucket by true local midnight across a DST
# transition, not by adding a fixed timedelta to a stale offset snapshot.
# --------------------------------------------------------------------------


class TestByPeriodDstTransition:
    def test_no_merge_true_midnight_starts_contiguous_and_sum_invariant(self, recorder):
        # One $1 sale at each TRUE local midnight, 2026-03-05..2026-03-11 --
        # spans the synthetic spring-forward transition on 2026-03-08.
        days = list(range(5, 12))
        sale_ts = {d: _syn_midnight(2026, 3, d).timestamp() for d in days}
        for d in days:
            recorder.record_sale(
                f"D{d}", f"Day {d}", 1, 1.00, {"cash": 1.00}, ts=sale_ts[d]
            )

        window = (sale_ts[5], _syn_midnight(2026, 3, 12).timestamp())
        rows = reports.by_period(recorder, window, "day", tz=SYN_TZ)

        # Positive half first: exactly one bucket per true local day.
        assert len(rows) == 7

        # The specific failure this reproduces: a merged bucket would show
        # revenue 2.0 / vends 2 for 2026-03-08 (swallowing 03-09's sale
        # too) and drop the last bucket entirely. Every bucket here must
        # instead show exactly its own one sale.
        for row in rows:
            assert row["revenue"] == pytest.approx(1.00)
            assert row["vends"] == 1

        # Bucket starts are each true local midnight -- not shifted by the
        # DST offset -- with the correct per-side UTC offset attached.
        expected_starts = [_syn_midnight(2026, 3, d) for d in days]
        actual_starts = [datetime.fromisoformat(r["bucket_start"]) for r in rows]
        assert actual_starts == expected_starts

        # Contiguous and non-overlapping: consecutive bucket starts are
        # exactly one real elapsed day apart -- 23h on the transition day
        # (2026-03-08, which loses an hour to spring-forward), 24h on
        # every other day. A gap or overlap bug would show up here as a
        # wrong elapsed time; the one-hour-shift bug would make every gap
        # after the transition still read 86400 (masking the true 23h
        # short day) while the bucket_start values above would silently
        # drift -- this catches either failure mode.
        gaps = [
            (actual_starts[i + 1] - actual_starts[i]).total_seconds()
            for i in range(len(actual_starts) - 1)
        ]
        assert gaps == [86400.0, 86400.0, 86400.0, 82800.0, 86400.0, 86400.0]

        # Sum invariant: nothing double-counted, nothing dropped.
        assert sum(r["revenue"] for r in rows) == pytest.approx(7.00)
        assert sum(r["vends"] for r in rows) == 7

    def test_tz_none_production_path_contiguous_and_sum_invariant(self, recorder):
        # The default path (tz=None) over an ordinary window -- no
        # particular DST claim here (whatever this machine's own local
        # timezone happens to be); this just proves the contiguity/sum
        # invariant holds on the code path production actually calls,
        # since every other test in this file pins `tz=` explicitly.
        now = time.time()
        day = 86400.0
        sale_ts = [now - 9 * day + i * day for i in range(9)]
        for i, ts in enumerate(sale_ts):
            recorder.record_sale(f"N{i}", f"Item {i}", 1, 1.00, {"cash": 1.00}, ts=ts)

        window = (now - 10 * day, now + day)
        rows = reports.by_period(recorder, window, "day", tz=None)

        assert sum(r["revenue"] for r in rows) == pytest.approx(9.00)
        assert sum(r["vends"] for r in rows) == 9

        starts = [datetime.fromisoformat(r["bucket_start"]) for r in rows]
        for i in range(len(starts) - 1):
            gap = (starts[i + 1] - starts[i]).total_seconds()
            # A real local day is 23, 24 or 25 hours; never anything else
            # -- and never zero or negative, which would mean an overlap
            # or a non-advancing bucket.
            assert gap in (23 * 3600.0, 24 * 3600.0, 25 * 3600.0)


# --------------------------------------------------------------------------
# Finding (Critical, this round): the spring-forward fix must generalise to
# a FALL-BACK transition (offset decreases). On that direction, the
# re-derived, re-floored next boundary can land back on the same local
# calendar bucket `current` already represents -- an epoch-only
# `next_start <= current` guard never fires, and a phantom, wrongly-split
# bucket row slips through, for "day", "week" and "month" alike.
# --------------------------------------------------------------------------


class TestByPeriodFallBackTransition:
    # (year, month, day) tuples at true local midnight, spanning the
    # 2026-11-01 fall-back transition -- three days before, the transition
    # day itself, two days after.
    _DAY_DATES = [
        (2026, 10, 29),
        (2026, 10, 30),
        (2026, 10, 31),
        (2026, 11, 1),
        (2026, 11, 2),
        (2026, 11, 3),
        (2026, 11, 4),
    ]
    # True local Monday-midnights spanning the same transition -- the week
    # 2026-10-26..2026-11-01 contains it (2026-11-02 is a Monday, confirmed
    # separately: `date(2026, 11, 2).weekday() == 0`).
    _WEEK_DATES = [
        (2026, 10, 12),
        (2026, 10, 19),
        (2026, 10, 26),
        (2026, 11, 2),
        (2026, 11, 9),
    ]
    # True local month-starts spanning the same transition -- November
    # 2026 contains it.
    _MONTH_DATES = [
        (2026, 9, 1),
        (2026, 10, 1),
        (2026, 11, 1),
        (2026, 12, 1),
        (2027, 1, 1),
    ]

    def test_day_no_merge_25h_day_contiguous_and_sum_invariant(self, recorder):
        dates = self._DAY_DATES
        sale_ts = {d: _syn_fb_midnight(*d).timestamp() for d in dates}
        for i, d in enumerate(dates):
            recorder.record_sale(
                f"D{i}", f"Day {d}", 1, 1.00, {"cash": 1.00}, ts=sale_ts[d]
            )

        window = (sale_ts[dates[0]], _syn_fb_midnight(2026, 11, 5).timestamp())
        rows = reports.by_period(recorder, window, "day", tz=SYN_FB_TZ)

        # Positive half first: exactly one bucket per true local day -- no
        # phantom bucket, no two rows sharing a local start. A merged/split
        # bug would show 8 rows here (one date split into two), not 7.
        assert len(rows) == len(dates)
        bucket_starts = [r["bucket_start"] for r in rows]
        assert len(set(bucket_starts)) == len(bucket_starts)

        for row in rows:
            assert row["revenue"] == pytest.approx(1.00)
            assert row["vends"] == 1

        expected_starts = [_syn_fb_midnight(*d) for d in dates]
        actual_starts = [datetime.fromisoformat(r["bucket_start"]) for r in rows]
        assert actual_starts == expected_starts

        # Contiguous: consecutive bucket starts are exactly one real
        # elapsed day apart -- 25h (90000s) on the transition day
        # (2026-11-01, which GAINS an hour to fall-back), 24h everywhere
        # else. 90000 is the mirror of the spring-forward test's 82800.
        gaps = [
            (actual_starts[i + 1] - actual_starts[i]).total_seconds()
            for i in range(len(actual_starts) - 1)
        ]
        assert gaps == [86400.0, 86400.0, 86400.0, 90000.0, 86400.0, 86400.0]

        # q_end of each row equals q_start of the next: re-derive both via
        # the module's own helpers rather than trusting bucket_start alone.
        for i in range(len(dates) - 1):
            end_of_this = reports._floor_to_bucket(
                reports._to_local(sale_ts[dates[i + 1]], SYN_FB_TZ), "day"
            )
            assert end_of_this == actual_starts[i + 1]

        # Sum invariant: nothing double-counted, nothing dropped.
        assert sum(r["revenue"] for r in rows) == pytest.approx(float(len(dates)))
        assert sum(r["vends"] for r in rows) == len(dates)

    def test_week_no_merge_contiguous_and_sum_invariant(self, recorder):
        dates = self._WEEK_DATES
        sale_ts = {d: _syn_fb_midnight(*d).timestamp() for d in dates}
        for i, d in enumerate(dates):
            recorder.record_sale(
                f"W{i}", f"Week {d}", 1, 1.00, {"cash": 1.00}, ts=sale_ts[d]
            )

        window = (sale_ts[dates[0]], _syn_fb_midnight(2026, 11, 16).timestamp())
        rows = reports.by_period(recorder, window, "week", tz=SYN_FB_TZ)

        # Positive half first: exactly one bucket per true local week.
        assert len(rows) == len(dates)
        bucket_starts = [r["bucket_start"] for r in rows]
        assert len(set(bucket_starts)) == len(bucket_starts)

        for row in rows:
            assert row["revenue"] == pytest.approx(1.00)
            assert row["vends"] == 1
            bucket_dt = datetime.fromisoformat(row["bucket_start"])
            assert bucket_dt.weekday() == 0  # every week bucket starts Monday

        expected_starts = [_syn_fb_midnight(*d) for d in dates]
        actual_starts = [datetime.fromisoformat(r["bucket_start"]) for r in rows]
        assert actual_starts == expected_starts

        # Contiguous: the week containing the fall-back (2026-10-26 ->
        # 2026-11-02) is 169h (608400s) -- one hour longer than a normal
        # 168h (604800s) week -- every other gap is the normal 604800.
        gaps = [
            (actual_starts[i + 1] - actual_starts[i]).total_seconds()
            for i in range(len(actual_starts) - 1)
        ]
        assert gaps == [604800.0, 604800.0, 608400.0, 604800.0]

        assert sum(r["revenue"] for r in rows) == pytest.approx(float(len(dates)))
        assert sum(r["vends"] for r in rows) == len(dates)

    def test_month_no_merge_contiguous_and_sum_invariant(self, recorder):
        dates = self._MONTH_DATES
        sale_ts = {d: _syn_fb_midnight(*d).timestamp() for d in dates}
        for i, d in enumerate(dates):
            recorder.record_sale(
                f"M{i}", f"Month {d}", 1, 1.00, {"cash": 1.00}, ts=sale_ts[d]
            )

        window = (sale_ts[dates[0]], _syn_fb_midnight(2027, 2, 1).timestamp())
        rows = reports.by_period(recorder, window, "month", tz=SYN_FB_TZ)

        # Positive half first: exactly one bucket per true local month.
        assert len(rows) == len(dates)
        bucket_starts = [r["bucket_start"] for r in rows]
        assert len(set(bucket_starts)) == len(bucket_starts)

        for row in rows:
            assert row["revenue"] == pytest.approx(1.00)
            assert row["vends"] == 1

        expected_starts = [_syn_fb_midnight(*d) for d in dates]
        actual_starts = [datetime.fromisoformat(r["bucket_start"]) for r in rows]
        assert actual_starts == expected_starts

        # Contiguous: November 2026 (contains the fall-back) is 721h
        # (2595600s) -- one hour longer than its normal 720h (2592000s) --
        # every other gap is the plain days-in-month * 86400.
        gaps = [
            (actual_starts[i + 1] - actual_starts[i]).total_seconds()
            for i in range(len(actual_starts) - 1)
        ]
        assert gaps == [2592000.0, 2678400.0, 2595600.0, 2678400.0]

        assert sum(r["revenue"] for r in rows) == pytest.approx(float(len(dates)))
        assert sum(r["vends"] for r in rows) == len(dates)


# --------------------------------------------------------------------------
# Finding 1 (Task 10 follow-up): by_period's optional sku= filter, added so
# /reports/product/{sku} can show a per-product breakdown by period without
# duplicating this module's DST-aware bucketing in the route.
# --------------------------------------------------------------------------


class TestByPeriodSkuFilter:
    def test_filters_revenue_and_vends_to_one_sku_across_two_buckets(self, recorder):
        # Two SKUs, two day-buckets each -- proves the filter narrows to
        # one SKU's own revenue/vends AND that bucketing still works
        # (each SKU's sales land in the correct, separate bucket).
        day0 = _dt(2026, 4, 1)
        day1 = _dt(2026, 4, 2)

        recorder.record_sale("A", "Alpha", 1, 3.00, {"cash": 3.00}, ts=day0.timestamp())
        recorder.record_sale("B", "Beta", 2, 5.00, {"cash": 5.00}, ts=day0.timestamp())
        recorder.record_sale("A", "Alpha", 1, 4.00, {"cash": 4.00}, ts=day1.timestamp())
        recorder.record_sale("B", "Beta", 2, 9.00, {"cash": 9.00}, ts=day1.timestamp())

        window = (day0.timestamp() - 1, day1.timestamp() + 86400)
        rows = reports.by_period(recorder, window, "day", tz=FIXED_TZ, sku="A")
        by_start = {r["bucket_start"]: r for r in rows}

        assert by_start[day0.isoformat()]["revenue"] == pytest.approx(3.00)
        assert by_start[day0.isoformat()]["vends"] == 1
        assert by_start[day1.isoformat()]["revenue"] == pytest.approx(4.00)
        assert by_start[day1.isoformat()]["vends"] == 1

        # Beta's revenue (5.00, 9.00) must never leak into Alpha's filtered
        # rows -- the whole point of the filter.
        assert sum(r["revenue"] for r in rows) == pytest.approx(7.00)
        assert sum(r["vends"] for r in rows) == 2

    def test_unfiltered_call_is_unaffected_by_the_new_parameter(self, recorder):
        # sku defaults to None -- an existing caller (no sku= at all) must
        # see totals across every SKU, unchanged by this parameter's
        # addition.
        now = time.time()
        recorder.record_sale("A", "Alpha", 1, 3.00, {"cash": 3.00}, ts=now - 20)
        recorder.record_sale("B", "Beta", 2, 5.00, {"cash": 5.00}, ts=now - 10)

        window = (now - 3600, now + 1)
        rows = reports.by_period(recorder, window, "day", tz=FIXED_TZ)

        assert sum(r["revenue"] for r in rows) == pytest.approx(8.00)
        assert sum(r["vends"] for r in rows) == 2

    def test_failed_vends_filtered_to_sku_refunds_and_uptime_stay_none(self, recorder):
        # failed_vends IS attributable per-sku (vend_failed's metadata
        # carries one, per VMC.on_vend_failed) so it is filtered; refunds
        # and uptime_pct are NOT attributable to one product, so they must
        # render "-" (None) even inside retention, not a real number that
        # would actually be machine-wide.
        now = time.time()
        recorder.record_sale("A", "Alpha", 1, 2.00, {"cash": 2.00}, ts=now - 20)

        with sqlite3.connect(recorder._db_path) as conn:
            conn.execute(
                "INSERT INTO events (event_type, timestamp, value, metadata) "
                "VALUES ('vend_failed', ?, 1.0, ?)",
                (now - 15, '{"sku": "A"}'),
            )
            conn.execute(
                "INSERT INTO events (event_type, timestamp, value, metadata) "
                "VALUES ('vend_failed', ?, 1.0, ?)",
                (now - 12, '{"sku": "B"}'),
            )
            conn.execute(
                "INSERT INTO events (event_type, timestamp, value) "
                "VALUES ('refund', ?, 1.0)",
                (now - 10,),
            )
            conn.commit()

        window = (now - 3600, now + 1)
        rows = reports.by_period(recorder, window, "day", tz=FIXED_TZ, sku="A")

        # Positive half first: the sku's own revenue is present and correct.
        assert sum(r["revenue"] for r in rows) == pytest.approx(2.00)

        # Only A's own vend_failed (1) is counted, not B's -- and refunds
        # /uptime are None despite the window being fully inside retention.
        assert sum(r["failed_vends"] for r in rows) == 1
        assert all(r["refunds"] is None for r in rows)
        assert all(r["uptime_pct"] is None for r in rows)


# --------------------------------------------------------------------------
# by_product
# --------------------------------------------------------------------------


class TestByProduct:
    def test_sorted_by_revenue_descending(self, recorder):
        now = time.time()
        recorder.record_sale("LOW", "Low Seller", 1, 1.00, {"cash": 1.00}, ts=now - 30)
        recorder.record_sale(
            "HIGH", "High Seller", 2, 50.00, {"cash": 50.00}, ts=now - 20
        )
        recorder.record_sale(
            "MID", "Mid Seller", 3, 10.00, {"cash": 10.00}, ts=now - 10
        )

        window = (now - 3600, now + 1)
        rows = reports.by_product(recorder, window)
        skus_in_order = [r["sku"] for r in rows]

        assert skus_in_order == ["HIGH", "MID", "LOW"]

    def test_latest_seen_name_after_rename(self, recorder):
        now = time.time()
        recorder.record_sale("SKU1", "Old Name", 1, 1.00, {"cash": 1.00}, ts=now - 200)
        recorder.record_sale("SKU1", "New Name", 1, 1.00, {"cash": 1.00}, ts=now - 100)

        window = (now - 3600, now + 1)
        rows = reports.by_product(recorder, window)

        assert len(rows) == 1
        assert rows[0]["name"] == "New Name"
        assert rows[0]["units"] == 2
        assert rows[0]["revenue"] == pytest.approx(2.00)

    def test_latest_seen_name_tie_broken_by_id_on_identical_ts(self, recorder):
        # record_sale's default ts=time.time() has finite resolution, so
        # two rows genuinely can share an identical ts. Without an
        # explicit tie-break, "latest seen" falls back to whatever order
        # SQLite happens to return -- not any guarantee. Pin the later
        # INSERT (higher id) as the winner regardless of ts ordering.
        same_ts = time.time() - 500
        recorder.record_sale("SKU1", "Old Name", 1, 1.00, {"cash": 1.00}, ts=same_ts)
        recorder.record_sale("SKU1", "New Name", 1, 1.00, {"cash": 1.00}, ts=same_ts)

        window = (same_ts - 3600, same_ts + 3600)
        rows = reports.by_product(recorder, window)

        assert len(rows) == 1
        assert rows[0]["name"] == "New Name"
        assert rows[0]["units"] == 2


# --------------------------------------------------------------------------
# by_method
# --------------------------------------------------------------------------


class TestByMethod:
    def test_amounts_sum_to_total_revenue_invariant(self, recorder):
        now = time.time()
        recorder.record_sale(
            "A", "Alpha", 1, 1.00, {"cash_coin": 0.75, "card": 0.25}, ts=now - 30
        )
        recorder.record_sale("B", "Beta", 2, 2.00, {"cash_bill": 2.00}, ts=now - 20)
        recorder.record_sale(
            "C", "Gamma", 3, 1.50, {"card": 1.00, "nfc": 0.50}, ts=now - 10
        )

        window = (now - 3600, now + 1)
        rows = reports.by_method(recorder, window)
        total_revenue = 1.00 + 2.00 + 1.50

        # The invariant: whatever the breakdown, the per-method amounts
        # account for the whole -- no money double-counted or dropped.
        assert sum(r["amount"] for r in rows) == pytest.approx(total_revenue)
        # And the share column is genuinely a fraction of that same total.
        assert sum(r["share"] for r in rows) == pytest.approx(1.0)

    def test_raw_method_strings_are_not_merged(self, recorder):
        now = time.time()
        recorder.record_sale("A", "Alpha", 1, 1.00, {"cash_coin": 1.00}, ts=now - 20)
        recorder.record_sale("B", "Beta", 2, 2.00, {"cash_bill": 2.00}, ts=now - 10)

        window = (now - 3600, now + 1)
        rows = reports.by_method(recorder, window)
        by_method_name = {r["method"]: r for r in rows}

        assert set(by_method_name) == {"cash_coin", "cash_bill"}
        assert by_method_name["cash_coin"]["amount"] == pytest.approx(1.00)
        assert by_method_name["cash_bill"]["amount"] == pytest.approx(2.00)
        assert by_method_name["cash_coin"]["is_cash"] is True
        assert by_method_name["cash_bill"]["is_cash"] is True


# --------------------------------------------------------------------------
# Retention: event-derived columns None outside the 90-day window, while
# revenue (from the never-pruned sales table) stays correct.
# --------------------------------------------------------------------------


class TestRetentionNoneVersusZero:
    def test_old_bucket_revenue_correct_but_event_columns_none(self, recorder):
        old_ts = time.time() - 100 * 86400  # well past the 90-day cutoff
        recorder.record_sale("OLD", "Old Item", 1, 9.00, {"cash": 9.00}, ts=old_ts)

        # Seed REAL vend_failed/refund/heartbeat rows in the same old
        # bucket, directly into events (never pruned mid-test), so a None
        # result below proves the code withholds them by *date policy* --
        # not merely because no matching row happens to exist.
        with sqlite3.connect(recorder._db_path) as conn:
            conn.execute(
                "INSERT INTO events (event_type, timestamp, value, metadata) "
                "VALUES ('vend_failed', ?, 1.0, ?)",
                (old_ts + 60, '{"sku": "OLD"}'),
            )
            conn.execute(
                "INSERT INTO events (event_type, timestamp, value) VALUES ('refund', ?, 1.5)",
                (old_ts + 60,),
            )
            conn.execute(
                "INSERT INTO events (event_type, timestamp, value) VALUES ('heartbeat', ?, 1.0)",
                (old_ts + 60,),
            )
            conn.commit()

        window = (old_ts - 2 * 86400, old_ts + 2 * 86400)
        rows = reports.by_period(recorder, window, "day", tz=FIXED_TZ)

        old_day_start = reports._floor_to_bucket(
            reports._to_local(old_ts, FIXED_TZ), "day"
        ).isoformat()
        matching = [r for r in rows if r["bucket_start"] == old_day_start]

        # Positive half FIRST: the bucket exists and its revenue is right --
        # proof the fixture didn't just fail to produce this row at all.
        assert len(matching) == 1
        old_bucket = matching[0]
        assert old_bucket["revenue"] == pytest.approx(9.00)
        assert old_bucket["vends"] == 1

        # Negative half: event-derived columns are None, not 0, even though
        # real rows for them exist in the table right now.
        assert old_bucket["failed_vends"] is None
        assert old_bucket["refunds"] is None
        assert old_bucket["uptime_pct"] is None


# --------------------------------------------------------------------------
# Finding 3 (Important): a bucket straddling the retention cutoff computes
# event-derived columns normally, over its WHOLE range -- documented and
# pinned, not withheld like a fully-outside-retention bucket.
# --------------------------------------------------------------------------


class TestRetentionStraddlingBucket:
    def test_straddling_bucket_counts_both_sides_of_the_cutoff(self, recorder):
        # by_period's docstring now says a straddling bucket's event-derived
        # columns cover only the *retained* portion in production, because
        # `_prune_with` has already deleted rows older than the cutoff
        # elsewhere. This module itself applies no per-row cutoff filter
        # inside a straddling bucket -- it only gates on the whole-bucket
        # `q_end <= retention_cutoff` check -- so pin that mechanism
        # directly: seed one event on EACH side of the cutoff, inside the
        # SAME bucket, and show the query counts both. (In production the
        # before-cutoff row would already be gone by the time this runs;
        # it is that absence -- not a filter in this module -- that makes
        # the real-world result partial.)
        cutoff_ts = time.time() - reports.RETENTION_DAYS * 86400
        bucket_start = reports._floor_to_bucket(
            reports._to_local(cutoff_ts, FIXED_TZ), "day"
        )
        bucket_end = reports._next_bucket(bucket_start, "day")

        before_ts = (
            bucket_start.timestamp() + (cutoff_ts - bucket_start.timestamp()) / 2
        )
        after_ts = cutoff_ts + (bucket_end.timestamp() - cutoff_ts) / 2
        assert (
            bucket_start.timestamp()
            < before_ts
            < cutoff_ts
            < after_ts
            < bucket_end.timestamp()
        )

        recorder.record_sale("STR", "Straddler", 1, 5.00, {"cash": 5.00}, ts=after_ts)
        with sqlite3.connect(recorder._db_path) as conn:
            conn.execute(
                "INSERT INTO events (event_type, timestamp, value, metadata) "
                "VALUES ('vend_failed', ?, 1.0, ?)",
                (before_ts, '{"sku": "STR"}'),
            )
            conn.execute(
                "INSERT INTO events (event_type, timestamp, value, metadata) "
                "VALUES ('vend_failed', ?, 1.0, ?)",
                (after_ts, '{"sku": "STR"}'),
            )
            conn.execute(
                "INSERT INTO events (event_type, timestamp, value) "
                "VALUES ('refund', ?, 1.0)",
                (before_ts,),
            )
            conn.execute(
                "INSERT INTO events (event_type, timestamp, value) "
                "VALUES ('refund', ?, 2.0)",
                (after_ts,),
            )
            conn.commit()

        window = (
            bucket_start.timestamp() - 2 * 86400,
            bucket_end.timestamp() + 2 * 86400,
        )
        rows = reports.by_period(recorder, window, "day", tz=FIXED_TZ)
        matching = [r for r in rows if r["bucket_start"] == bucket_start.isoformat()]

        assert len(matching) == 1
        straddling = matching[0]

        # Positive half: a straddling bucket is computed, not withheld --
        # it is not None like a fully-outside-retention bucket would be.
        assert straddling["failed_vends"] is not None
        assert straddling["refunds"] is not None
        assert straddling["uptime_pct"] is not None

        # Pinned mechanism: BOTH the before- and after-cutoff rows are
        # counted, because the query has no per-row cutoff filter of its
        # own inside a straddling bucket.
        assert straddling["failed_vends"] == 2
        assert straddling["refunds"] == pytest.approx(3.0)


# --------------------------------------------------------------------------
# Empty database
# --------------------------------------------------------------------------


class TestEmptyDatabase:
    def test_all_five_functions_return_empty_or_zero_never_raise(self, recorder):
        now = time.time()
        window = reports.resolve_window("30d", now=now)

        period_rows = reports.by_period(recorder, window, "day", tz=FIXED_TZ)
        assert isinstance(period_rows, list)
        for row in period_rows:
            assert row["revenue"] == 0.0
            assert row["vends"] == 0
            # A recent, empty window is still inside retention: zero, not None.
            assert row["failed_vends"] == 0
            assert row["refunds"] == 0.0
            assert row["uptime_pct"] == 0.0

        assert reports.by_product(recorder, window) == []
        assert reports.by_method(recorder, window) == []
        assert reports.collections(recorder, limit=10) == []

        summary = reports.summary(recorder, window)
        assert summary["revenue"] == 0.0
        assert summary["vends"] == 0
        assert summary["failed"] == 0
        assert summary["refunds"] == 0.0
        assert summary["by_method"] == []
        assert summary["cash_since_last_collection"] == 0.0
        assert summary["revenue"] is not None
        assert summary["cash_since_last_collection"] is not None


# --------------------------------------------------------------------------
# collections: live newest row vs stored older rows
# --------------------------------------------------------------------------


class TestCollections:
    def test_newest_row_is_live_older_rows_are_stored(self, recorder):
        # record_cash_collection always stamps with the real wall clock, so
        # every timestamp here is real time.time() too (never a synthetic
        # offset) -- otherwise an artificially-future sale ts could sort
        # ahead of a later, real-time collection and invert the ordering
        # this test relies on. Tiny sleeps guarantee strictly increasing
        # timestamps regardless of OS clock resolution.
        recorder.record_sale("A", "Alpha", 1, 1.00, {"cash": 1.00})
        time.sleep(0.02)
        recorder.record_cash_collection("u1", "Alice")
        recorder.flush()

        with sqlite3.connect(recorder._db_path) as conn:
            first_stored = conn.execute(
                "SELECT expected_cash FROM cash_collections WHERE user_id='u1'"
            ).fetchone()[0]

        time.sleep(0.02)
        recorder.record_sale("B", "Beta", 2, 2.00, {"cash": 2.00})
        time.sleep(0.02)
        recorder.record_cash_collection("u2", "Bob")
        recorder.flush()

        # A sale that arrives AFTER the newest collection -- proves the
        # newest row's figure is live, not frozen at insert time.
        time.sleep(0.02)
        recorder.record_sale("C", "Gamma", 3, 7.00, {"cash": 7.00})

        rows = reports.collections(recorder, limit=10)
        by_user = {r["user_id"]: r for r in rows}

        assert by_user["u2"]["expected_cash"] == pytest.approx(7.00)
        assert by_user["u1"]["expected_cash"] == pytest.approx(first_stored)

        # It keeps changing as still more sales arrive.
        time.sleep(0.02)
        recorder.record_sale("D", "Delta", 4, 3.00, {"cash": 3.00})
        rows_again = reports.collections(recorder, limit=10)
        by_user_again = {r["user_id"]: r for r in rows_again}
        assert by_user_again["u2"]["expected_cash"] == pytest.approx(10.00)
        assert by_user_again["u1"]["expected_cash"] == pytest.approx(first_stored)

    def test_tied_ts_newest_by_id_gets_the_live_figure(self, recorder, monkeypatch):
        """Finding 1 (whole-branch review, part 3): `collections()`'s
        `ORDER BY ts DESC LIMIT ?` had no tie-break on `id`, so two rows
        sharing an identical `ts` were labelled newest/oldest by SQLite's
        incidental tie order rather than by which was truly inserted last.

        The tie is forced genuinely, not hoped for: `time.time()` is
        monkeypatched (same technique as
        `tests/test_routes_inventory_collect.py`'s
        `TestConcurrentCollectionsShowOwnRow`) so both `record_cash_
        collection` calls are queued with the exact same `ts`. Alice is
        queued first (gets `id=1`), Bob second (`id=2`, the true newest) --
        mirroring the review's own Alice/Bob reproduction.
        """
        frozen_ts = time.time()

        # A sale from well before either collection: it must count in
        # Alice's all-time stored total (she is the very first collection
        # ever) but never in any "since <frozen_ts>" total. Passed via the
        # explicit `ts` param so this does not depend on wall-clock luck.
        recorder.record_sale("A", "Alpha", 1, 1.00, {"cash": 1.00}, ts=frozen_ts - 10)

        monkeypatch.setattr("services.event_recorder.time.time", lambda: frozen_ts)
        recorder.record_cash_collection("alice", "Alice")  # id=1
        recorder.record_cash_collection("bob", "Bob")  # id=2, the true newest
        recorder.flush()
        monkeypatch.undo()

        with sqlite3.connect(recorder._db_path) as conn:
            id_rows = conn.execute(
                "SELECT id, user_id, ts, expected_cash FROM cash_collections "
                "ORDER BY id ASC"
            ).fetchall()
        assert [r[1] for r in id_rows] == ["alice", "bob"], (
            "test setup requires alice=id1, bob=id2"
        )
        assert id_rows[0][2] == id_rows[1][2] == frozen_ts, (
            "test setup requires both rows to share a tied ts"
        )
        # Sanity on the stored figures each row was actually given at
        # insert time, independent of the bug under test.
        assert id_rows[0][3] == pytest.approx(1.00)  # alice: all sales so far
        assert id_rows[1][3] == pytest.approx(0.00)  # bob: nothing since alice

        # A sale strictly after the tied ts -- only the TRUE newest row
        # (bob, id=2) should ever reflect it live.
        recorder.record_sale("B", "Beta", 2, 5.00, {"cash": 5.00}, ts=frozen_ts + 10)

        rows = reports.collections(recorder, limit=10)

        # bob is the true newest collection (greatest id at the tied ts)
        # and must be first, carrying the live figure; alice must show her
        # stored figure, unaffected by the sale recorded after her.
        assert rows[0]["user_id"] == "bob"
        assert rows[0]["expected_cash"] == pytest.approx(5.00)
        assert rows[1]["user_id"] == "alice"
        assert rows[1]["expected_cash"] == pytest.approx(1.00)


# --------------------------------------------------------------------------
# CSV rendering and filenames
# --------------------------------------------------------------------------


class TestCsvRendering:
    def test_round_trip_and_none_renders_as_empty_field(self):
        header = ["bucket_start", "revenue", "vends", "failed_vends"]
        rows = [
            {
                "bucket_start": "2026-01-01T00:00:00-05:00",
                "revenue": 12.5,
                "vends": 3,
                "failed_vends": None,
            },
            {
                "bucket_start": "2026-01-02T00:00:00-05:00",
                "revenue": 5.0,
                "vends": 1,
                "failed_vends": 0,
            },
        ]

        data = reports.render_csv(rows, header)
        assert isinstance(data, bytes)

        parsed = list(csv.DictReader(io.StringIO(data.decode("utf-8"))))

        assert parsed[0]["bucket_start"] == rows[0]["bucket_start"]
        assert float(parsed[0]["revenue"]) == pytest.approx(rows[0]["revenue"])
        assert int(parsed[0]["vends"]) == rows[0]["vends"]
        assert parsed[0]["failed_vends"] == ""  # None -> empty field, not "None"

        assert int(parsed[1]["failed_vends"]) == 0

    def test_filename_shape(self):
        assert (
            reports.report_filename("VMC-01", "by-product", "30d")
            == "VMC-01-by-product-30d.csv"
        )
