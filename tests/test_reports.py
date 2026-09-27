# tests/test_reports.py
import csv
import io
import sqlite3
import time
from datetime import datetime, timedelta, timezone

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
