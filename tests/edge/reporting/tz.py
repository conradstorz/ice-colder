"""Deterministic timezone doubles shared by the reporting tests.

`zoneinfo.ZoneInfo` is not usable on this machine: there is no tzdata
installed, and `ZoneInfo("America/New_York")` raises
`ZoneInfoNotFoundError`. Adding the `tzdata` package is forbidden (no new
runtime dependency; `pyproject.toml`/`uv.lock` must not change). The two
classes below hard-code ONE UTC transition instant each, mirroring US
Eastern's spring-forward and fall-back, using only the standard library.
That is arguably better than a real zone for these tests anyway: fully
deterministic and immune to any future tz-database change.

Both classes return a plain, FROZEN `datetime.timezone` snapshot from
`fromutc` -- exactly what `datetime.astimezone(None)` returns in production
(see `ice_edge.reporting.reports.to_local`) -- rather than `self`. That is
what lets a test reproduce the *specific* bug under review: code that calls
`to_local` once and then advances by `timedelta` alone carries that stale,
`dt`-independent snapshot forward and never re-derives the true offset for
a later date. A "fully dynamic" tzinfo whose `utcoffset()` reads its `dt`
argument on every call would self-correct even under the unfixed code, via
Python's own aware-datetime subtraction, and so would prove nothing.
"""

from datetime import datetime, timedelta, timezone, tzinfo

# A fixed, DST-free offset -- deliberately not the machine's own timezone, so
# bucket-boundary assignment is proven independent of whatever system the
# tests happen to run on. Passed explicitly as `tz=` wherever a test needs it.
FIXED_TZ = timezone(timedelta(hours=-5))


def fixed_dt(year, month, day, hour=0, minute=0, second=0):
    """A wall-clock datetime in `FIXED_TZ`."""
    return datetime(year, month, day, hour, minute, second, tzinfo=FIXED_TZ)


class SyntheticDstTz(tzinfo):
    """Deterministic stand-in for a real DST spring-forward transition.

    One hard-coded UTC transition instant: UTC-5 before it, UTC-4 after,
    mirroring US Eastern's 2026-03-08 spring-forward (02:00 EST -> 03:00 EDT,
    both 07:00 UTC).
    """

    _TRANSITION_UTC = datetime(2026, 3, 8, 7, 0, 0)  # 02:00 EST == 03:00 EDT
    _BEFORE = timedelta(hours=-5)  # EST
    _AFTER = timedelta(hours=-4)  # EDT

    def fromutc(self, dt):
        naive_utc = dt.replace(tzinfo=None)
        offset = self._BEFORE if naive_utc < self._TRANSITION_UTC else self._AFTER
        return (dt + offset).replace(tzinfo=timezone(offset))

    def utcoffset(self, dt):
        # Reached only when this class is attached directly to a wall-clock
        # datetime (as a test's "true local midnight" helper does) -- never
        # via `fromutc` above, which returns a frozen `datetime.timezone`.
        if dt is None:
            return self._BEFORE
        naive_local = dt.replace(tzinfo=None) if dt.tzinfo is not None else dt
        local_before_boundary = self._TRANSITION_UTC + self._BEFORE  # 02:00 local
        return self._BEFORE if naive_local < local_before_boundary else self._AFTER

    def dst(self, dt):
        return timedelta(0)

    def tzname(self, dt):
        return "SYN"


class SyntheticFallBackTz(tzinfo):
    """Deterministic stand-in for a real DST FALL-BACK transition.

    Mirror image of `SyntheticDstTz`: the offset *decreases* across the
    transition (-04:00 -> -05:00, mirroring US Eastern's 2026-11-01 fall back
    from EDT to EST, 02:00 EDT == 01:00 EST == 06:00 UTC).

    This reproduces the bug a loop that re-derives and re-floors the next
    boundary but judges advancement by epoch alone would hit: on this
    direction the re-derived candidate can land back on the SAME local
    calendar bucket while its epoch is still strictly greater, which an
    epoch-only `next_start <= current` guard cannot see. Only used at local
    midnight and other non-ambiguous hours, never inside the repeated
    01:00-02:00 local hour.
    """

    _TRANSITION_UTC = datetime(2026, 11, 1, 6, 0, 0)  # 02:00 EDT == 01:00 EST
    _BEFORE = timedelta(hours=-4)  # EDT
    _AFTER = timedelta(hours=-5)  # EST

    def fromutc(self, dt):
        naive_utc = dt.replace(tzinfo=None)
        offset = self._BEFORE if naive_utc < self._TRANSITION_UTC else self._AFTER
        return (dt + offset).replace(tzinfo=timezone(offset))

    def utcoffset(self, dt):
        # Reached only when attached directly to a wall-clock datetime, never
        # via `fromutc`, which returns a frozen `datetime.timezone`.
        if dt is None:
            return self._BEFORE
        naive_local = dt.replace(tzinfo=None) if dt.tzinfo is not None else dt
        local_before_boundary = self._TRANSITION_UTC + self._BEFORE  # 02:00 local
        return self._BEFORE if naive_local < local_before_boundary else self._AFTER

    def dst(self, dt):
        return timedelta(0)

    def tzname(self, dt):
        return "SYNFB"
