# tests/test_event_recorder.py
import json
import os
import sqlite3
import time
from pathlib import Path

import pytest

from services import event_recorder as event_recorder_module
from services.event_recorder import EventRecorder, is_cash


@pytest.fixture
def recorder(tmp_path):
    return EventRecorder(db_path=str(tmp_path / "events.db"))


@pytest.fixture
def journal_path(tmp_path, monkeypatch):
    """Point the module-level JOURNAL_PATH at a temp file for this test."""
    path = tmp_path / "sales-journal.jsonl"
    monkeypatch.setattr(event_recorder_module, "JOURNAL_PATH", path)
    return path


class TestInit:
    def test_creates_db_file(self, tmp_path):
        db = str(tmp_path / "events.db")
        EventRecorder(db_path=db)
        assert os.path.exists(db)

    def test_creates_events_table(self, tmp_path):
        db = str(tmp_path / "events.db")
        EventRecorder(db_path=db)
        conn = sqlite3.connect(db)
        tables = [
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        ]
        conn.close()
        assert "events" in tables

    def test_creates_parent_directories(self, tmp_path):
        db = str(tmp_path / "nested" / "dir" / "events.db")
        EventRecorder(db_path=db)
        assert os.path.exists(db)


class TestRecord:
    def test_payment_recorded(self, recorder):
        recorder.record("payment", value=3.00)
        summary = recorder.get_summary(24)
        assert summary["money_in"] == pytest.approx(3.00)

    def test_multiple_payments_sum(self, recorder):
        recorder.record("payment", value=1.50)
        recorder.record("payment", value=2.00)
        assert recorder.get_summary(24)["money_in"] == pytest.approx(3.50)

    def test_dispense_counted(self, recorder):
        recorder.record("dispense", value=0)
        assert recorder.get_summary(24)["products_out"] == 1

    def test_ice_cycle_counted(self, recorder):
        recorder.record("ice_cycle", value=1)
        assert recorder.get_summary(24)["ice_cycles"] == 1

    def test_error_counted(self, recorder):
        recorder.record("error", value=1)
        assert recorder.get_summary(24)["errors"] == 1

    def test_service_door_counted(self, recorder):
        recorder.record("service_door", value=1)
        assert recorder.get_summary(24)["service_door_opens"] == 1

    def test_temp_exceedance_counted(self, recorder):
        recorder.record("temp_exceedance", value=95.0)
        assert recorder.get_summary(24)["temp_exceedances"] == 1


class TestGetSummary:
    def test_empty_db_returns_zeros(self, recorder):
        s = recorder.get_summary(24)
        assert s["money_in"] == 0.0
        assert s["products_out"] == 0
        assert s["errors"] == 0
        assert s["uptime_pct"] == 0.0

    def test_old_events_excluded(self, tmp_path):
        db = str(tmp_path / "events.db")
        rec = EventRecorder(db_path=db)
        old_ts = time.time() - 25 * 3600
        conn = sqlite3.connect(db)
        conn.execute(
            "INSERT INTO events (event_type, timestamp, value) VALUES (?, ?, ?)",
            ("payment", old_ts, 5.00),
        )
        conn.commit()
        conn.close()
        assert rec.get_summary(24)["money_in"] == 0.0

    def test_uptime_with_heartbeats(self, recorder):
        # 360 heartbeats at a real 10s cadence (distinct buckets), covering
        # 3600s of a 24h (86400s) window → ~4.2%.
        now = time.time()
        conn = sqlite3.connect(recorder._db_path)
        conn.executemany(
            "INSERT INTO events (event_type, timestamp, value) VALUES ('heartbeat', ?, 100)",
            [(now - i * 10,) for i in range(360)],
        )
        conn.commit()
        conn.close()
        assert recorder.get_summary(24)["uptime_pct"] == pytest.approx(4.2, abs=0.1)

    def test_vends_failed_and_refunds(self, recorder):
        recorder.record("vend_failed", value=2.5, metadata={"code": "ICE-301"})
        recorder.record("vend_failed", value=3.0, metadata={"code": "ICE-401"})
        recorder.record("refund", value=2.5, metadata={"request_id": "r1"})
        recorder.record("refund_failed", value=3.0, metadata={"request_id": "r2"})
        s = recorder.get_summary(24)
        assert s["vends_failed"] == 2
        assert s["refunds"] == 2.5


class TestUptimeComputation:
    """uptime_pct must measure the fraction of _HEARTBEAT_INTERVAL-sized time
    buckets that have at least one heartbeat from ANY subsystem — not raw
    heartbeat rows, which overcounts when multiple subsystems beat
    concurrently (e.g. 3 simulators beating every 10s reads ~300%, clamped to
    100%, even though one of them could be silently offline the whole time)."""

    @staticmethod
    def _insert_heartbeats(db_path, timestamps):
        conn = sqlite3.connect(db_path)
        conn.executemany(
            "INSERT INTO events (event_type, timestamp, value) VALUES ('heartbeat', ?, 1)",
            [(ts,) for ts in timestamps],
        )
        conn.commit()
        conn.close()

    def test_full_coverage_by_one_subsystem_reads_100_pct(self, recorder):
        start = 1_000_000.0
        end = start + 100.0  # 10 buckets of 10s
        self._insert_heartbeats(recorder._db_path, [start + i * 10 for i in range(10)])
        assert recorder._compute_window(start, end)["uptime_pct"] == pytest.approx(
            100.0
        )

    def test_half_covered_window_reads_about_50_pct(self, recorder):
        start = 1_000_000.0
        end = start + 100.0
        self._insert_heartbeats(recorder._db_path, [start + i * 10 for i in range(5)])
        assert recorder._compute_window(start, end)["uptime_pct"] == pytest.approx(50.0)

    def test_multiple_subsystems_in_same_bucket_count_once(self, recorder):
        start = 1_000_000.0
        end = start + 100.0
        # Three subsystems all beat within the same 10s bucket.
        self._insert_heartbeats(recorder._db_path, [start + 1, start + 2, start + 3])
        # 1 of 10 buckets covered, not 3 rows worth.
        assert recorder._compute_window(start, end)["uptime_pct"] == pytest.approx(10.0)

    def test_metric_reads_100_pct_even_if_one_subsystem_silently_offline(
        self, recorder
    ):
        """Two subsystems beat every 10s for the whole window; a third never
        beats at all. uptime_pct measures 'at least one subsystem alive' per
        bucket, not per-subsystem health, so this honestly reads 100% — it no
        longer inflates past 100% (the bug), but per-subsystem outages are a
        separate metric this one doesn't claim to cover."""
        start = 1_000_000.0
        end = start + 100.0
        timestamps = []
        for i in range(10):
            timestamps.append(start + i * 10)  # subsystem A
            timestamps.append(start + i * 10 + 0.5)  # subsystem B
        self._insert_heartbeats(recorder._db_path, timestamps)
        # Still reads 100% for "at least one subsystem alive" per bucket —
        # this documents the metric's honest ceiling, not per-subsystem health.
        assert recorder._compute_window(start, end)["uptime_pct"] == pytest.approx(
            100.0
        )


class TestRegisterHandlers:
    def _get_handlers(self, recorder):
        """Call register_handlers with a mock client, return {topic: handler} dict."""
        from unittest.mock import MagicMock

        client = MagicMock()
        recorder.register_handlers(client)
        return {call.args[0]: call.args[1] for call in client.register.call_args_list}

    @pytest.mark.asyncio
    async def test_payment_records_amount(self, recorder):
        h = self._get_handlers(recorder)
        await h["payment/credit"](
            "payment/credit",
            {
                "amount": 2.50,
                "method": "cash_coin",
                "timestamp": "2026-01-01T00:00:00+00:00",
            },
        )
        assert recorder.get_summary(24)["money_in"] == pytest.approx(2.50)

    def test_hardware_dispenser_not_registered(self, recorder):
        """The recorder must NOT listen on hardware/dispenser directly — only
        the VMC knows whether a completion was accepted for the active sale
        (see controller.vmc.VMC._handle_mqtt_dispenser). A direct subscription
        here would overcount products_out on duplicates/late completions the
        VMC ignores."""
        h = self._get_handlers(recorder)
        assert "hardware/dispenser" not in h

    @pytest.mark.asyncio
    async def test_ice_dropped_records_cycle(self, recorder):
        h = self._get_handlers(recorder)
        await h["ice_maker/event"](
            "ice_maker/event",
            {
                "event": "ice_dropped",
                "detail": None,
                "timestamp": "2026-01-01T00:00:00+00:00",
            },
        )
        assert recorder.get_summary(24)["ice_cycles"] == 1

    @pytest.mark.asyncio
    async def test_ice_power_on_not_recorded(self, recorder):
        h = self._get_handlers(recorder)
        await h["ice_maker/event"](
            "ice_maker/event",
            {
                "event": "power_on",
                "detail": None,
                "timestamp": "2026-01-01T00:00:00+00:00",
            },
        )
        assert recorder.get_summary(24)["ice_cycles"] == 0

    @pytest.mark.asyncio
    async def test_service_door_open_records_event(self, recorder):
        h = self._get_handlers(recorder)
        await h["hardware/io/service_door"](
            "hardware/io/service_door",
            {
                "device": "service_door",
                "state": True,
                "timestamp": "2026-01-01T00:00:00+00:00",
            },
        )
        assert recorder.get_summary(24)["service_door_opens"] == 1

    @pytest.mark.asyncio
    async def test_service_door_close_not_recorded(self, recorder):
        h = self._get_handlers(recorder)
        await h["hardware/io/service_door"](
            "hardware/io/service_door",
            {
                "device": "service_door",
                "state": False,
                "timestamp": "2026-01-01T00:00:00+00:00",
            },
        )
        assert recorder.get_summary(24)["service_door_opens"] == 0

    @pytest.mark.asyncio
    async def test_out_of_range_temp_records_exceedance(self, recorder):
        h = self._get_handlers(recorder)
        await h["sensors/temp/+"](
            "sensors/temp/evaporator",
            {
                "location": "evaporator",
                "value": 95.0,
                "unit": "C",
                "timestamp": "2026-01-01T00:00:00+00:00",
            },
        )
        assert recorder.get_summary(24)["temp_exceedances"] == 1

    @pytest.mark.asyncio
    async def test_normal_temp_not_recorded(self, recorder):
        h = self._get_handlers(recorder)
        await h["sensors/temp/+"](
            "sensors/temp/evaporator",
            {
                "location": "evaporator",
                "value": 22.0,
                "unit": "C",
                "timestamp": "2026-01-01T00:00:00+00:00",
            },
        )
        assert recorder.get_summary(24)["temp_exceedances"] == 0

    @pytest.mark.asyncio
    async def test_heartbeat_recorded(self, recorder):
        h = self._get_handlers(recorder)
        await h["heartbeat/+"](
            "heartbeat/vending",
            {
                "subsystem": "vending",
                "uptime_seconds": 300,
                "timestamp": "2026-01-01T00:00:00+00:00",
            },
        )
        assert recorder.get_summary(24)["uptime_pct"] > 0

    @pytest.mark.asyncio
    async def test_heartbeat_lwt_not_counted_as_uptime(self, recorder):
        """uptime_seconds == -1 is the MQTT Last-Will payload meaning OFFLINE;
        it must not be recorded as an ordinary heartbeat (which would count
        an outage as uptime)."""
        h = self._get_handlers(recorder)
        await h["heartbeat/+"](
            "heartbeat/vending",
            {
                "subsystem": "vending",
                "uptime_seconds": -1,
                "timestamp": "2026-01-01T00:00:00+00:00",
            },
        )
        assert recorder.get_summary(24)["uptime_pct"] == 0.0

    @pytest.mark.asyncio
    async def test_heartbeat_lwt_records_subsystem_offline_event(self, recorder):
        h = self._get_handlers(recorder)
        await h["heartbeat/+"](
            "heartbeat/vending",
            {
                "subsystem": "vending",
                "uptime_seconds": -1,
                "timestamp": "2026-01-01T00:00:00+00:00",
            },
        )
        recorder.flush()
        with sqlite3.connect(recorder._db_path) as conn:
            row = conn.execute("SELECT event_type, metadata FROM events").fetchone()
        assert row[0] == "subsystem_offline"
        assert json.loads(row[1]) == {"subsystem": "vending"}


class TestGetHistoricalAverage:
    def test_returns_none_with_no_data(self, recorder):
        avg = recorder.get_historical_average(24)
        assert avg["money_in"] is None
        assert avg["products_out"] is None

    def test_returns_none_with_only_one_prior_period(self, tmp_path):
        """Spec requires at least 2 complete prior periods before averaging;
        a single period is not statistically meaningful."""
        db = str(tmp_path / "events.db")
        rec = EventRecorder(db_path=db)
        # Insert data in one prior 24h period only (25-49h ago) — not enough.
        ts = time.time() - 36 * 3600
        conn = sqlite3.connect(db)
        conn.execute(
            "INSERT INTO events (event_type, timestamp, value) VALUES (?, ?, ?)",
            ("heartbeat", ts, 1),
        )
        conn.execute(
            "INSERT INTO events (event_type, timestamp, value) VALUES (?, ?, ?)",
            ("payment", ts, 5.00),
        )
        conn.commit()
        conn.close()
        avg = rec.get_historical_average(24)
        assert avg["money_in"] is None
        assert avg["products_out"] is None

    def test_averages_two_prior_periods(self, tmp_path):
        db = str(tmp_path / "events.db")
        rec = EventRecorder(db_path=db)
        now = time.time()
        period = 24 * 3600
        conn = sqlite3.connect(db)
        # Period 1: 25-49h ago → $10 + heartbeat
        conn.execute(
            "INSERT INTO events (event_type, timestamp, value) VALUES (?, ?, ?)",
            ("payment", now - 1.5 * period, 10.00),
        )
        conn.execute(
            "INSERT INTO events (event_type, timestamp, value) VALUES (?, ?, ?)",
            ("heartbeat", now - 1.5 * period, 1),
        )
        # Period 2: 49-73h ago → $6 + heartbeat
        conn.execute(
            "INSERT INTO events (event_type, timestamp, value) VALUES (?, ?, ?)",
            ("payment", now - 2.5 * period, 6.00),
        )
        conn.execute(
            "INSERT INTO events (event_type, timestamp, value) VALUES (?, ?, ?)",
            ("heartbeat", now - 2.5 * period, 1),
        )
        conn.commit()
        conn.close()
        avg = rec.get_historical_average(24)
        assert avg["money_in"] == pytest.approx(8.0)  # (10 + 6) / 2

    def test_inactive_periods_excluded(self, tmp_path):
        """Periods with no heartbeat (machine off) don't skew the average."""
        db = str(tmp_path / "events.db")
        rec = EventRecorder(db_path=db)
        now = time.time()
        period = 24 * 3600
        conn = sqlite3.connect(db)
        # Period 1 active: $10 + heartbeat
        conn.execute(
            "INSERT INTO events (event_type, timestamp, value) VALUES (?, ?, ?)",
            ("payment", now - 1.5 * period, 10.00),
        )
        conn.execute(
            "INSERT INTO events (event_type, timestamp, value) VALUES (?, ?, ?)",
            ("heartbeat", now - 1.5 * period, 1),
        )
        # Period 2 active: $8 + heartbeat
        conn.execute(
            "INSERT INTO events (event_type, timestamp, value) VALUES (?, ?, ?)",
            ("payment", now - 2.5 * period, 8.00),
        )
        conn.execute(
            "INSERT INTO events (event_type, timestamp, value) VALUES (?, ?, ?)",
            ("heartbeat", now - 2.5 * period, 1),
        )
        # Period 3 inactive: $20 payment but NO heartbeat — machine was off, exclude
        conn.execute(
            "INSERT INTO events (event_type, timestamp, value) VALUES (?, ?, ?)",
            ("payment", now - 3.5 * period, 20.00),
        )
        conn.commit()
        conn.close()
        avg = rec.get_historical_average(24)
        assert avg["money_in"] == pytest.approx(9.0)  # (10 + 8) / 2, not (10+8+20)/3


class TestHistoricalAverageCache:
    """get_historical_average runs up to 30 sqlite queries per metric window;
    a short TTL cache keeps repeated dashboard polls from hammering the DB."""

    @staticmethod
    def _seed_two_active_periods(db, now, period):
        conn = sqlite3.connect(db)
        conn.execute(
            "INSERT INTO events (event_type, timestamp, value) VALUES (?, ?, ?)",
            ("payment", now - 1.5 * period, 10.00),
        )
        conn.execute(
            "INSERT INTO events (event_type, timestamp, value) VALUES (?, ?, ?)",
            ("heartbeat", now - 1.5 * period, 1),
        )
        conn.execute(
            "INSERT INTO events (event_type, timestamp, value) VALUES (?, ?, ?)",
            ("payment", now - 2.5 * period, 6.00),
        )
        conn.execute(
            "INSERT INTO events (event_type, timestamp, value) VALUES (?, ?, ?)",
            ("heartbeat", now - 2.5 * period, 1),
        )
        conn.commit()
        conn.close()

    def test_cache_returns_cached_value_within_ttl(self, tmp_path, monkeypatch):
        db = str(tmp_path / "events.db")
        rec = EventRecorder(db_path=db)
        base_time = time.time()
        period = 24 * 3600
        monkeypatch.setattr(time, "time", lambda: base_time)

        self._seed_two_active_periods(db, base_time, period)

        avg1 = rec.get_historical_average(24)
        assert avg1["money_in"] == pytest.approx(8.0)

        # Insert data that WOULD change the result if recomputed.
        with sqlite3.connect(db) as conn:
            conn.execute(
                "INSERT INTO events (event_type, timestamp, value) VALUES (?, ?, ?)",
                ("payment", base_time - 1.5 * period, 100.00),
            )

        # Still within TTL (no time advance) — must serve the cached value.
        avg2 = rec.get_historical_average(24)
        assert avg2 == avg1

    def test_cache_recomputes_after_ttl_expiry(self, tmp_path, monkeypatch):
        db = str(tmp_path / "events.db")
        rec = EventRecorder(db_path=db)
        base_time = time.time()
        period = 24 * 3600
        monkeypatch.setattr(time, "time", lambda: base_time)

        self._seed_two_active_periods(db, base_time, period)

        avg1 = rec.get_historical_average(24)
        assert avg1["money_in"] == pytest.approx(8.0)

        with sqlite3.connect(db) as conn:
            conn.execute(
                "INSERT INTO events (event_type, timestamp, value) VALUES (?, ?, ?)",
                ("payment", base_time - 1.5 * period, 100.00),
            )

        # Advance past the TTL — must recompute and pick up the new data.
        monkeypatch.setattr(time, "time", lambda: base_time + 61)
        avg2 = rec.get_historical_average(24)
        assert avg2["money_in"] == pytest.approx(58.0)  # (110 + 6) / 2


class TestRetention:
    def test_prune_removes_events_older_than_retention(self, tmp_path):
        rec = EventRecorder(db_path=str(tmp_path / "e.db"), retention_days=1)
        old_ts = time.time() - 2 * 86400
        with sqlite3.connect(str(tmp_path / "e.db")) as conn:
            conn.execute(
                "INSERT INTO events (event_type, timestamp, value) VALUES (?, ?, ?)",
                ("payment", old_ts, 1.0),
            )
        rec.record("payment", 1.0)
        rec.flush()
        rec.prune()
        with sqlite3.connect(str(tmp_path / "e.db")) as conn:
            count = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        assert count == 1  # only the fresh event survives

    def test_init_prunes_existing_old_events(self, tmp_path):
        db = str(tmp_path / "e.db")
        _rec = EventRecorder(db_path=db, retention_days=1)
        old_ts = time.time() - 2 * 86400
        with sqlite3.connect(db) as conn:
            conn.execute(
                "INSERT INTO events (event_type, timestamp, value) VALUES (?, ?, ?)",
                ("payment", old_ts, 1.0),
            )
        _rec2 = EventRecorder(db_path=db, retention_days=1)
        with sqlite3.connect(db) as conn:
            count = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        assert count == 0


class TestWriterThread:
    def test_record_returns_before_row_is_visible_then_flush_makes_it_visible(
        self, tmp_path
    ):
        db = str(tmp_path / "events.db")
        rec = EventRecorder(db_path=db)
        rec.record("payment", value=2.0)
        rec.flush()
        conn = sqlite3.connect(db)
        assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1

    def test_get_summary_flushes_pending_rows(self, recorder):
        recorder.record("payment", value=1.5)
        assert recorder.get_summary(24)["money_in"] == 1.5

    def test_writer_survives_bad_row(self, tmp_path, monkeypatch):
        """The writer thread's `except Exception` branch must really run —
        sqlite happily stores NaN as NULL without raising, so that doesn't
        exercise it. Instead, wrap the writer thread's connection so its
        first INSERT raises sqlite3.OperationalError, then delegates
        normally; the thread must log the failure, drop that row, and keep
        processing the queue."""
        real_connect = sqlite3.connect

        class _FailFirstInsertConnection:
            def __init__(self, conn):
                self._conn = conn
                self._insert_calls = 0

            def execute(self, sql, *args, **kwargs):
                if sql.strip().upper().startswith("INSERT"):
                    self._insert_calls += 1
                    if self._insert_calls == 1:
                        raise sqlite3.OperationalError("boom")
                return self._conn.execute(sql, *args, **kwargs)

            def __getattr__(self, name):
                return getattr(self._conn, name)

        def fake_connect(*args, **kwargs):
            conn = real_connect(*args, **kwargs)
            if kwargs.get("check_same_thread") is False:
                # Only the writer thread connects with check_same_thread=False;
                # _init_db/prune's connections must behave normally.
                return _FailFirstInsertConnection(conn)
            return conn

        monkeypatch.setattr(sqlite3, "connect", fake_connect)

        db = tmp_path / "events.db"
        rec = EventRecorder(db_path=str(db))
        rec.record("payment", value=1.0)  # this row's INSERT will raise
        rec.record("dispense", value=1.0)  # this row must still land
        rec.flush()
        assert rec.get_summary(24)["products_out"] == 1
        assert rec.get_summary(24)["money_in"] == 0.0  # the failed row never landed


class TestIsCash:
    @pytest.mark.parametrize(
        "method, expected",
        [
            ("cash", True),
            ("coin", True),
            ("bill", True),
            ("cash_coin", True),
            ("cash_bill", True),
            ("CASH_COIN", True),  # case-insensitive
            ("card", False),
            ("nfc", False),
            ("test", False),
        ],
    )
    def test_pinned_values(self, method, expected):
        assert is_cash(method) is expected


class TestSalesRetentionScope:
    def test_prune_does_not_touch_sales(self, tmp_path):
        """A sale older than the retention window must survive prune()."""
        db = str(tmp_path / "e.db")
        rec = EventRecorder(db_path=db, retention_days=1)
        old_ts = time.time() - 2 * 86400  # well outside the 1-day retention
        rec.record_sale("SKU1", "Cola", 1, 1.50, {"cash": 1.50}, ts=old_ts)

        rec.prune()

        with sqlite3.connect(db) as conn:
            count = conn.execute("SELECT COUNT(*) FROM sales").fetchone()[0]
        assert count == 1


class TestRecordSale:
    def test_writes_documented_shape_and_is_durable_from_second_connection(
        self, tmp_path
    ):
        db = str(tmp_path / "e.db")
        rec = EventRecorder(db_path=db)
        methods = {"cash": 1.00, "card": 0.50}

        rec.record_sale("SKU1", "Cola", 3, 1.50, methods, ts=1234.5)

        # A second, separate connection -- not the one record_sale used --
        # is the point: it proves the row is really on disk, not merely
        # buffered in the connection that wrote it.
        conn2 = sqlite3.connect(db)
        try:
            row = conn2.execute(
                "SELECT ts, sku, name, slot, price, methods FROM sales"
            ).fetchone()
        finally:
            conn2.close()

        assert row is not None
        ts, sku, name, slot, price, methods_json = row
        assert ts == pytest.approx(1234.5)
        assert sku == "SKU1"
        assert name == "Cola"
        assert slot == 3
        assert price == pytest.approx(1.50)
        assert json.loads(methods_json) == methods

    def test_default_ts_is_current_time(self, tmp_path):
        db = str(tmp_path / "e.db")
        rec = EventRecorder(db_path=db)
        before = time.time()

        rec.record_sale("SKU9", "Water", None, 1.00, {"cash": 1.00})

        after = time.time()
        conn = sqlite3.connect(db)
        try:
            (ts,) = conn.execute("SELECT ts FROM sales").fetchone()
        finally:
            conn.close()
        assert before <= ts <= after


class _FailingSalesInsertConn:
    """Wraps a real sqlite3.Connection so its first `INSERT INTO sales`
    raises, then behaves normally for everything else -- proving the
    insert-failure branch of record_sale is actually entered (as opposed to,
    say, the connect() call failing, which the implementation might handle
    differently)."""

    def __init__(self, conn):
        self._conn = conn
        self._insert_calls = 0

    def execute(self, sql, *args, **kwargs):
        if sql.strip().upper().startswith("INSERT INTO SALES"):
            self._insert_calls += 1
            if self._insert_calls == 1:
                raise sqlite3.OperationalError("boom")
        return self._conn.execute(sql, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._conn, name)


class TestRecordSaleIdempotentParam:
    """Part 3 review, round 4: record_sale gains an opt-in `idempotent`
    parameter (last, default False) so a caller with a deterministic `ts`
    (the PAY-104 recovery route's session-snapshot `saved_at`) can insert
    through the same `INSERT ... WHERE NOT EXISTS` form
    `replay_sales_journal` already relies on -- while every existing
    caller (a live sale from the FSM's dispense path) keeps today's plain,
    non-deduplicated insert by default, since two genuine sales of the
    same SKU must never be silently merged into one row."""

    def test_idempotent_true_same_ts_and_sku_twice_writes_one_row(self, tmp_path):
        db = str(tmp_path / "e.db")
        rec = EventRecorder(db_path=db)
        methods = {"cash": 2.50}

        rec.record_sale("ICE-1", "Ice", 1, 2.50, methods, ts=1000.0, idempotent=True)
        rec.record_sale("ICE-1", "Ice", 1, 2.50, methods, ts=1000.0, idempotent=True)

        with sqlite3.connect(db) as conn:
            rows = conn.execute(
                "SELECT ts, sku FROM sales WHERE ts = ? AND sku = ?",
                (1000.0, "ICE-1"),
            ).fetchall()
        assert len(rows) == 1  # the second attempt inserted nothing

    def test_idempotent_true_same_sku_different_ts_writes_two_rows(self, tmp_path):
        """A genuine repeat sale of the same SKU (a second, distinct
        dispense) must never be swallowed by the opt-in -- only an exact
        (ts, sku) match is treated as a replay of the *same* sale."""
        db = str(tmp_path / "e.db")
        rec = EventRecorder(db_path=db)
        methods = {"cash": 2.50}

        rec.record_sale("ICE-1", "Ice", 1, 2.50, methods, ts=1000.0, idempotent=True)
        rec.record_sale("ICE-1", "Ice", 1, 2.50, methods, ts=2000.0, idempotent=True)

        with sqlite3.connect(db) as conn:
            rows = conn.execute(
                "SELECT ts FROM sales WHERE sku = ? ORDER BY ts", ("ICE-1",)
            ).fetchall()
        assert [r[0] for r in rows] == [1000.0, 2000.0]

    def test_default_path_still_writes_two_rows_for_identical_looking_sales(
        self, tmp_path
    ):
        """The default (idempotent unset, i.e. False) path is untouched:
        two calls with the exact same ts and sku -- which would collapse
        to one row if idempotent=True were somehow applied by default --
        must still both land, proving the opt-in changes nothing for
        every existing caller."""
        db = str(tmp_path / "e.db")
        rec = EventRecorder(db_path=db)
        methods = {"cash": 2.50}

        rec.record_sale("ICE-1", "Ice", 1, 2.50, methods, ts=1000.0)
        rec.record_sale("ICE-1", "Ice", 1, 2.50, methods, ts=1000.0)

        with sqlite3.connect(db) as conn:
            rows = conn.execute(
                "SELECT ts, sku FROM sales WHERE ts = ? AND sku = ?",
                (1000.0, "ICE-1"),
            ).fetchall()
        assert len(rows) == 2  # both plain inserts landed, no deduplication


class TestRecordSaleFailureJournal:
    def test_failed_insert_appends_exactly_one_fsynced_journal_line(
        self, tmp_path, monkeypatch, journal_path
    ):
        db = str(tmp_path / "e.db")
        rec = EventRecorder(db_path=db)

        real_connect = sqlite3.connect

        def fake_connect(path, *args, **kwargs):
            conn = real_connect(path, *args, **kwargs)
            if str(path) == db and kwargs.get("check_same_thread") is not False:
                return _FailingSalesInsertConn(conn)
            return conn

        monkeypatch.setattr(sqlite3, "connect", fake_connect)

        methods = {"cash": 2.00}
        with pytest.raises(sqlite3.OperationalError):
            rec.record_sale("SKU2", "Chips", 5, 2.00, methods, ts=999.0)

        assert journal_path.exists()
        lines = journal_path.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 1  # exactly one line, not zero and not a partial retry
        entry = json.loads(lines[0])
        assert entry["sku"] == "SKU2"
        assert entry["name"] == "Chips"
        assert entry["slot"] == 5
        assert entry["price"] == pytest.approx(2.00)
        assert entry["methods"] == methods
        assert entry["ts"] == pytest.approx(999.0)

        # The row must NOT have landed in the database -- the insert really
        # failed rather than partially succeeding. Use real_connect, since
        # sqlite3.connect is still patched at this point in the test.
        with real_connect(db) as conn:
            count = conn.execute("SELECT COUNT(*) FROM sales").fetchone()[0]
        assert count == 0


class TestReplaySalesJournal:
    def test_is_noop_when_file_absent(self, tmp_path, journal_path):
        rec = EventRecorder(db_path=str(tmp_path / "e.db"))
        assert not journal_path.exists()

        count = rec.replay_sales_journal()

        assert count == 0
        assert not journal_path.exists()

    def test_is_noop_when_file_empty(self, tmp_path, journal_path):
        journal_path.write_text("", encoding="utf-8")
        rec = EventRecorder(db_path=str(tmp_path / "e.db"))

        count = rec.replay_sales_journal()

        assert count == 0

    def test_inserts_rows_and_leaves_file_empty(self, tmp_path, journal_path):
        db = str(tmp_path / "e.db")
        rec = EventRecorder(db_path=db)
        entries = [
            {
                "ts": 1.0,
                "sku": "A",
                "name": "Alpha",
                "slot": 1,
                "price": 1.0,
                "methods": {"cash": 1.0},
            },
            {
                "ts": 2.0,
                "sku": "B",
                "name": "Beta",
                "slot": 2,
                "price": 2.0,
                "methods": {"card": 2.0},
            },
        ]
        journal_path.write_text(
            "\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8"
        )

        count = rec.replay_sales_journal()

        assert count == 2
        assert journal_path.read_text(encoding="utf-8") == ""
        with sqlite3.connect(db) as conn:
            rows = conn.execute(
                "SELECT sku, name, slot, price, methods FROM sales ORDER BY sku"
            ).fetchall()
        assert [r[0] for r in rows] == ["A", "B"]
        assert json.loads(rows[0][4]) == {"cash": 1.0}
        assert json.loads(rows[1][4]) == {"card": 2.0}

    def test_partial_final_line_does_not_lose_earlier_good_lines(
        self, tmp_path, journal_path
    ):
        db = str(tmp_path / "e.db")
        rec = EventRecorder(db_path=db)
        good = {
            "ts": 1.0,
            "sku": "A",
            "name": "Alpha",
            "slot": 1,
            "price": 1.0,
            "methods": {"cash": 1.0},
        }
        # second line is a truncated write: no closing brace/quote, no newline
        content = json.dumps(good) + '\n{"ts": 2.0, "sku": "B", "name": "Bet'
        journal_path.write_text(content, encoding="utf-8")

        count = rec.replay_sales_journal()

        assert count == 1
        with sqlite3.connect(db) as conn:
            rows = conn.execute("SELECT sku FROM sales").fetchall()
        assert rows == [("A",)]
        assert journal_path.read_text(encoding="utf-8") == ""


class TestExpectedCash:
    def test_all_time_then_since_previous_collection(self, tmp_path):
        db = str(tmp_path / "e.db")
        rec = EventRecorder(db_path=db)

        rec.record_sale(
            "A", "Alpha", 1, 1.00, {"cash_coin": 0.75, "card": 0.25}, ts=100.0
        )
        rec.record_sale("B", "Beta", 2, 2.00, {"cash_bill": 2.00}, ts=200.0)
        rec.record_sale("C", "Gamma", 3, 1.50, {"card": 1.00, "nfc": 0.50}, ts=300.0)

        rec.record_cash_collection("u1", "Alice")
        rec.flush()

        with sqlite3.connect(db) as conn:
            first_rows = conn.execute(
                "SELECT ts, user_id, user_name, expected_cash FROM cash_collections ORDER BY id"
            ).fetchall()
        assert len(first_rows) == 1
        first_ts, first_user, _, first_expected = first_rows[0]
        assert first_user == "u1"
        # all-time cash: cash_coin (0.75) + cash_bill (2.00); card/nfc excluded
        assert first_expected == pytest.approx(0.75 + 2.00)

        # Sales after the first collection: mix of cash and non-cash.
        rec.record_sale("D", "Delta", 4, 3.00, {"cash": 3.00}, ts=first_ts + 10)
        rec.record_sale("E", "Epsilon", 5, 1.00, {"card": 1.00}, ts=first_ts + 20)
        rec.record_sale("F", "Zeta", 6, 0.50, {"coin": 0.50}, ts=first_ts + 30)
        # A sale timestamped before the previous collection must not count,
        # even though it is inserted after it.
        rec.record_sale("G", "Old", 7, 5.00, {"cash": 5.00}, ts=first_ts - 5)

        rec.record_cash_collection("u2", "Bob")
        rec.flush()

        with sqlite3.connect(db) as conn:
            second_rows = conn.execute(
                "SELECT user_id, expected_cash FROM cash_collections ORDER BY id"
            ).fetchall()
        assert len(second_rows) == 2
        second_user, second_expected = second_rows[1]
        assert second_user == "u2"
        assert second_expected == pytest.approx(3.00 + 0.50)


class TestCorruptDatabaseRecovery:
    def test_normal_db_is_not_flagged_corrupt(self, tmp_path):
        rec = EventRecorder(db_path=str(tmp_path / "events.db"))
        assert rec.db_was_corrupt is False
        assert rec.corrupt_backup_path is None

    def test_corrupt_db_is_quarantined_and_recorder_is_usable_after(self, tmp_path):
        db_path = tmp_path / "events.db"
        garbage = b"this is not a valid sqlite database, just garbage bytes"
        db_path.write_bytes(garbage)

        rec = EventRecorder(db_path=str(db_path))

        # The flag for DATA-102 is set, not raised.
        assert rec.db_was_corrupt is True
        assert rec.corrupt_backup_path is not None
        backup = Path(rec.corrupt_backup_path)
        # Proves the corrupt branch was truly entered (an actual rename of
        # the actual bad bytes), not just a flag flipped without action.
        assert backup.exists()
        assert backup.name.startswith("events.db.corrupt-")
        assert backup.read_bytes() == garbage
        assert not (backup == db_path)

        # A fresh, valid database now lives at the original path.
        assert db_path.exists()
        with sqlite3.connect(str(db_path)) as conn:
            tables = {
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
        assert {"events", "sales", "cash_collections"} <= tables

        # The recorder is fully usable afterwards.
        rec.record("payment", value=1.0)
        rec.flush()
        assert rec.get_summary(24)["money_in"] == pytest.approx(1.0)
        rec.record_sale("SKU1", "Cola", 1, 1.50, {"cash": 1.50}, ts=time.time())
        with sqlite3.connect(str(db_path)) as conn:
            count = conn.execute("SELECT COUNT(*) FROM sales").fetchone()[0]
        assert count == 1

    def test_wal_and_shm_sidecars_are_quarantined_too(self, tmp_path):
        """A real corruption event (e.g. power loss) is exactly when
        un-checkpointed -wal/-shm sidecars from the previous run are also
        most likely present. They must move with the main file, not be left
        sitting beside the fresh replacement database.

        Calls _quarantine_corrupt_db directly on a bare instance (via
        __new__, skipping __init__ entirely -- this suite already calls
        private helpers directly elsewhere, e.g. TestUptimeComputation on
        _compute_window), for two reasons: sqlite3's own WAL auto-recovery
        on open would "heal" (checkpoint over) most hand-crafted corruption
        that leaves a real, readable WAL file beside it, defeating the setup
        rather than exercising it; and a fully-constructed EventRecorder's
        own writer thread holds a long-lived open handle on db_path that
        intermittently wins a race against renaming that same path out from
        under it -- a race that cannot occur in real use, since
        _quarantine_corrupt_db only ever runs from __init__, before the
        writer thread exists. This still proves the sidecar rename genuinely
        happens -- real files, real bytes, real new paths.
        """
        rec = EventRecorder.__new__(EventRecorder)
        rec._db_path = str(tmp_path / "events.db")
        rec.db_was_corrupt = False
        rec.corrupt_backup_path = None
        Path(rec._db_path).write_bytes(b"placeholder db bytes")
        db_path = Path(rec._db_path)
        wal_path = db_path.with_name(db_path.name + "-wal")
        shm_path = db_path.with_name(db_path.name + "-shm")
        wal_path.write_bytes(b"stale wal bytes")
        shm_path.write_bytes(b"stale shm bytes")

        rec._quarantine_corrupt_db()

        assert rec.db_was_corrupt is True
        backup = Path(rec.corrupt_backup_path)
        assert backup.exists()

        # The sidecars are gone from beside the fresh database...
        assert not wal_path.exists()
        assert not shm_path.exists()
        # ...and their actual bytes reappear beside the quarantined main file,
        # proving a real rename occurred rather than a delete or a no-op.
        backup_wal = backup.with_name(backup.name + "-wal")
        backup_shm = backup.with_name(backup.name + "-shm")
        assert backup_wal.exists()
        assert backup_shm.exists()
        assert backup_wal.read_bytes() == b"stale wal bytes"
        assert backup_shm.read_bytes() == b"stale shm bytes"


class TestCorruptDatabaseRenameFailure:
    def test_rename_failure_falls_back_to_new_path_and_constructor_does_not_raise(
        self, tmp_path, monkeypatch
    ):
        """Reproduces the reviewer's finding directly: os.replace raising
        (e.g. Windows PermissionError from another open handle) during
        quarantine must not crash the constructor -- exactly the pre-task
        behaviour §5 exists to eliminate."""
        db_path = tmp_path / "events.db"
        garbage = b"this is not a valid sqlite database, just garbage bytes"
        db_path.write_bytes(garbage)

        def fake_replace(_src, _dst):
            raise PermissionError("simulated: another handle has this file open")

        monkeypatch.setattr(os, "replace", fake_replace)

        rec = EventRecorder(db_path=str(db_path))  # must not raise

        # Proves the rename genuinely failed and was genuinely attempted --
        # the corrupt bytes are still exactly where they were, byte for
        # byte, rather than having been moved or the branch never entered.
        assert db_path.read_bytes() == garbage

        assert rec.db_was_corrupt is True
        assert rec.corrupt_backup_path is None  # nothing was actually preserved aside

        # The recorder fell back to a different, usable path in the same
        # directory rather than retrying the same corrupt path.
        assert rec._db_path != str(db_path)
        assert Path(rec._db_path).parent == tmp_path
        assert Path(rec._db_path).exists()
        with sqlite3.connect(rec._db_path) as conn:
            tables = {
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
        assert {"events", "sales", "cash_collections"} <= tables

        # The recorder is fully usable afterward, against the fallback path.
        rec.record("payment", value=1.0)
        rec.flush()
        assert rec.get_summary(24)["money_in"] == pytest.approx(1.0)


class TestCorruptDetectionNarrowedToGenuineCorruption:
    """A locked or otherwise-unreadable-but-not-corrupt database must never
    be quarantined -- that would rename away a perfectly healthy database
    and permanently lose its history."""

    @staticmethod
    def _make_failing_connect(db_path, message, real_connect):
        """Return a sqlite3.connect replacement whose first .execute() call
        on a connection to db_path raises sqlite3.OperationalError(message),
        then delegates normally -- proving the specific branch (a genuine
        OperationalError reaching __init__'s except clause) is entered,
        rather than some other failure."""

        class _RaiseOnFirstExecute:
            def __init__(self, conn):
                self._conn = conn
                self._first = True

            def execute(self, sql, *args, **kwargs):
                if self._first:
                    self._first = False
                    raise sqlite3.OperationalError(message)
                return self._conn.execute(sql, *args, **kwargs)

            def __getattr__(self, name):
                return getattr(self._conn, name)

        def fake_connect(path, *args, **kwargs):
            conn = real_connect(path, *args, **kwargs)
            if str(path) == str(db_path):
                return _RaiseOnFirstExecute(conn)
            return conn

        return fake_connect

    @pytest.mark.parametrize(
        "message",
        ["database is locked", "unable to open database file"],
    )
    def test_locked_or_permission_denied_db_is_not_quarantined(
        self, tmp_path, monkeypatch, message
    ):
        db_path = tmp_path / "events.db"
        # A normal, valid, already-initialized database.
        EventRecorder(db_path=str(db_path))
        original_bytes = db_path.read_bytes()

        real_connect = sqlite3.connect
        fake_connect = self._make_failing_connect(db_path, message, real_connect)
        monkeypatch.setattr(sqlite3, "connect", fake_connect)

        with pytest.raises(sqlite3.OperationalError):
            EventRecorder(db_path=str(db_path))

        # Proves the branch was truly entered as "not corruption": no
        # quarantine sibling was created and the original file is untouched,
        # byte for byte.
        assert list(tmp_path.glob("events.db.corrupt-*")) == []
        assert db_path.read_bytes() == original_bytes


class TestSalesJournalIdempotency:
    """Finding 1: replay_sales_journal must be safe to run twice against the
    same already-committed line (e.g. a crash between the DB commit and the
    journal truncation)."""

    def test_replaying_same_line_twice_does_not_duplicate_the_sale(
        self, tmp_path, journal_path
    ):
        db = str(tmp_path / "e.db")
        rec = EventRecorder(db_path=db)
        entry = {
            "ts": 555.5,
            "sku": "DUPTEST",
            "name": "Dup",
            "slot": 1,
            "price": 1.25,
            "methods": {"cash": 1.25},
        }
        journal_path.write_text(json.dumps(entry) + "\n", encoding="utf-8")

        first_count = rec.replay_sales_journal()
        assert first_count == 1

        # Simulate the crash the reviewer demonstrated: the commit already
        # landed, but the truncation step never ran (or the line reappears,
        # e.g. from a backup) -- so replay runs again against a line whose
        # sale is already durably on disk.
        journal_path.write_text(json.dumps(entry) + "\n", encoding="utf-8")
        second_count = rec.replay_sales_journal()

        assert second_count == 0  # nothing new inserted
        with sqlite3.connect(db) as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM sales WHERE ts = ? AND sku = ?",
                (555.5, "DUPTEST"),
            ).fetchone()[0]
        assert count == 1  # exactly one row, not two

    def test_two_distinct_sales_of_same_sku_both_land(self, tmp_path, journal_path):
        db = str(tmp_path / "e.db")
        rec = EventRecorder(db_path=db)
        entries = [
            {
                "ts": 10.0,
                "sku": "SAME",
                "name": "Thing",
                "slot": 1,
                "price": 1.0,
                "methods": {"cash": 1.0},
            },
            {
                "ts": 20.0,
                "sku": "SAME",
                "name": "Thing",
                "slot": 1,
                "price": 1.0,
                "methods": {"cash": 1.0},
            },
        ]
        journal_path.write_text(
            "\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8"
        )

        count = rec.replay_sales_journal()

        assert count == 2
        with sqlite3.connect(db) as conn:
            rows = conn.execute(
                "SELECT ts FROM sales WHERE sku = ? ORDER BY ts", ("SAME",)
            ).fetchall()
        assert [r[0] for r in rows] == [10.0, 20.0]


class TestSalesJournalRejectedRows:
    """Finding 5: one bad row must not block every good one, forever."""

    def test_bad_row_is_set_aside_and_does_not_block_good_rows(
        self, tmp_path, journal_path
    ):
        db = str(tmp_path / "e.db")
        rec = EventRecorder(db_path=db)
        good_before = {
            "ts": 1.0,
            "sku": "GOOD1",
            "name": "One",
            "slot": 1,
            "price": 1.0,
            "methods": {"cash": 1.0},
        }
        # sku=None violates the NOT NULL column -- well-formed JSON, but
        # uninsertable.
        bad = {
            "ts": 2.0,
            "sku": None,
            "name": "Bad",
            "slot": 2,
            "price": 1.0,
            "methods": {"cash": 1.0},
        }
        good_after = {
            "ts": 3.0,
            "sku": "GOOD2",
            "name": "Two",
            "slot": 3,
            "price": 1.0,
            "methods": {"cash": 1.0},
        }
        journal_path.write_text(
            "\n".join(json.dumps(e) for e in (good_before, bad, good_after)) + "\n",
            encoding="utf-8",
        )

        count = rec.replay_sales_journal()

        assert count == 2  # both good rows, despite the bad one between them
        with sqlite3.connect(db) as conn:
            skus = {r[0] for r in conn.execute("SELECT sku FROM sales").fetchall()}
        assert skus == {"GOOD1", "GOOD2"}

        # The journal is fully drained -- the bad row is not retried forever,
        # which would otherwise block DATA-101 from ever clearing.
        assert journal_path.read_text(encoding="utf-8") == ""

        # But it is not silently gone -- it is set aside as evidence.
        rejected_path = journal_path.with_name(
            f"{journal_path.stem}.rejected{journal_path.suffix}"
        )
        assert rejected_path.exists()
        rejected_lines = rejected_path.read_text(encoding="utf-8").splitlines()
        assert len(rejected_lines) == 1
        assert json.loads(rejected_lines[0])["name"] == "Bad"


class TestCorruptRecoveryRetryFailure:
    """Round 3, Finding A (CRITICAL): the post-quarantine retry of
    _init_db()/prune() inside __init__ must not be allowed to raise --
    a disk-full or permission fault at exactly that moment is realistic
    (it is a plausible cause of the original corruption too), not
    contrived."""

    def test_retry_connect_failure_does_not_crash_constructor(
        self, tmp_path, monkeypatch, journal_path
    ):
        db_path = tmp_path / "events.db"
        garbage = b"this is not a valid sqlite database, just garbage bytes"
        db_path.write_bytes(garbage)

        real_connect = sqlite3.connect
        call_count = {"n": 0}

        def fake_connect(path, *args, **kwargs):
            if str(path) == str(db_path):
                call_count["n"] += 1
                if call_count["n"] == 2:
                    # Call #1 is the original attempt against the still-
                    # corrupt file (real corruption raises later, from
                    # conn.execute, not from connect() itself). Quarantine
                    # renames the corrupt file away, clearing this path.
                    # Call #2 is the retry's connect against that now-clear
                    # path -- this is the exact moment we simulate a
                    # transient fault (e.g. a full disk) failing.
                    raise sqlite3.OperationalError("unable to open database file")
            return real_connect(path, *args, **kwargs)

        monkeypatch.setattr(sqlite3, "connect", fake_connect)

        rec = EventRecorder(db_path=str(db_path))  # must not raise

        # Proves the retry branch was genuinely entered and genuinely
        # failed -- not skipped, not some other call miscounted.
        assert call_count["n"] >= 2

        assert rec.db_was_corrupt is True
        assert rec.corrupt_backup_path is not None
        assert rec.db_unavailable is True
        # The original corrupt file really was quarantined -- this is a
        # genuine "quarantine worked, the fresh-database retry didn't"
        # case, not quarantine itself failing (that's Finding 2/round 2).
        backup = Path(rec.corrupt_backup_path)
        assert backup.read_bytes() == garbage

        # --- Consequence chain: verified, not assumed. ---

        # record() must not raise even though the database is unavailable.
        rec.record("payment", value=1.0)

        # flush() must return promptly (the writer thread's per-item
        # try/except/finally: task_done() swallows the resulting insert
        # failure) rather than hang to its timeout.
        start = time.monotonic()
        rec.flush(timeout=5.0)
        elapsed = time.monotonic() - start
        assert elapsed < 4.0, (
            f"flush() took {elapsed:.2f}s -- looks like it hit the timeout "
            "instead of the writer thread promptly calling task_done()"
        )

        # record_sale must still journal the sale (never lose it) and
        # re-raise, exactly per its documented contract, so the caller can
        # raise DATA-101 and still finish the dispense.
        with pytest.raises(sqlite3.OperationalError):
            rec.record_sale("SKU1", "Cola", 1, 1.50, {"cash": 1.50}, ts=12345.0)
        assert journal_path.exists()
        lines = journal_path.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 1
        assert json.loads(lines[0])["sku"] == "SKU1"


class TestSalesJournalRejectedWriteFailure:
    """Round 3, Finding B (IMPORTANT): a failure writing the rejected-row
    evidence file must not escape replay_sales_journal as an unhandled
    exception -- no data is lost either way, but the docstring's own claim
    ("replay itself completes") must actually hold."""

    def test_reject_write_failure_does_not_raise_and_keeps_row_for_retry(
        self, tmp_path, journal_path, monkeypatch
    ):
        db = str(tmp_path / "e.db")
        rec = EventRecorder(db_path=db)
        good_before = {
            "ts": 1.0,
            "sku": "GOOD1",
            "name": "One",
            "slot": 1,
            "price": 1.0,
            "methods": {"cash": 1.0},
        }
        bad = {  # sku=None violates NOT NULL -- well-formed JSON, uninsertable
            "ts": 2.0,
            "sku": None,
            "name": "Bad",
            "slot": 2,
            "price": 1.0,
            "methods": {"cash": 1.0},
        }
        good_after = {
            "ts": 3.0,
            "sku": "GOOD2",
            "name": "Two",
            "slot": 3,
            "price": 1.0,
            "methods": {"cash": 1.0},
        }
        journal_path.write_text(
            "\n".join(json.dumps(e) for e in (good_before, bad, good_after)) + "\n",
            encoding="utf-8",
        )

        def failing_append(_payload):
            raise OSError("simulated: disk full writing rejected-sale evidence")

        monkeypatch.setattr(
            event_recorder_module, "_append_rejected_sale_line", failing_append
        )

        count = rec.replay_sales_journal()  # must not raise

        assert count == 2  # both good rows still landed
        with sqlite3.connect(db) as conn:
            skus = {r[0] for r in conn.execute("SELECT sku FROM sales").fetchall()}
        assert skus == {"GOOD1", "GOOD2"}

        # The bad row could not be inserted AND could not be set aside as
        # evidence (the write failed) -- it must stay in the journal for a
        # later retry rather than being silently discarded.
        remaining = journal_path.read_text(encoding="utf-8")
        remaining_rows = [json.loads(ln) for ln in remaining.splitlines() if ln.strip()]
        assert len(remaining_rows) == 1
        assert remaining_rows[0]["name"] == "Bad"

        # Proves the failing branch was genuinely entered: the rejected
        # file was never actually written (the append raised before
        # anything landed there), not that it silently succeeded anyway.
        rejected_path = journal_path.with_name(
            f"{journal_path.stem}.rejected{journal_path.suffix}"
        )
        assert not rejected_path.exists()


class TestJournalCountVsDrainSignal:
    """Round 3, Finding C (IMPORTANT): the integer return value cannot
    tell "nothing to do" apart from "fully drained, nothing landed" --
    the journal's post-call state (absent/empty vs. non-empty) is the
    signal a caller must use instead."""

    def test_zero_return_does_not_imply_the_journal_is_resolved(
        self, tmp_path, journal_path, monkeypatch
    ):
        """count == 0 in isolation is ambiguous: here it happens while one
        row is genuinely still stuck (reject-write failed) -- proving a
        caller must check journal drainage, not the count, before
        clearing DATA-101."""
        db = str(tmp_path / "e.db")
        rec = EventRecorder(db_path=db)

        # First, get one sale genuinely committed so we can replay an
        # already-committed duplicate of it (idempotent insert -> 0 rows).
        rec.record_sale("DUP", "Dup", 1, 1.0, {"cash": 1.0}, ts=500.0)
        duplicate_of_committed = {
            "ts": 500.0,
            "sku": "DUP",
            "name": "Dup",
            "slot": 1,
            "price": 1.0,
            "methods": {"cash": 1.0},
        }
        bad = {  # uninsertable, and its rejected-evidence write will fail too
            "ts": 600.0,
            "sku": None,
            "name": "StuckBad",
            "slot": 2,
            "price": 1.0,
            "methods": {"cash": 1.0},
        }
        journal_path.write_text(
            "\n".join(json.dumps(e) for e in (duplicate_of_committed, bad)) + "\n",
            encoding="utf-8",
        )

        def failing_append(_payload):
            raise OSError("simulated: disk full writing rejected-sale evidence")

        monkeypatch.setattr(
            event_recorder_module, "_append_rejected_sale_line", failing_append
        )

        count = rec.replay_sales_journal()  # must not raise

        assert count == 0  # the duplicate inserted 0, the bad row inserted 0

        # The ambiguous integer says "nothing happened" -- but the journal
        # is NOT resolved: the bad row is still stuck in it. A caller that
        # cleared DATA-101 on `count > 0` would be wrong here in the
        # opposite direction too (0 does not mean safe); the journal state
        # is what must gate the fault.
        remaining = journal_path.read_text(encoding="utf-8")
        remaining_rows = [json.loads(ln) for ln in remaining.splitlines() if ln.strip()]
        assert len(remaining_rows) == 1
        assert remaining_rows[0]["name"] == "StuckBad"

    def test_all_resolved_returns_zero_but_journal_is_drained(
        self, tmp_path, journal_path
    ):
        """The other half of Finding C: when everything in a non-empty
        journal is fully resolved (here: entirely an already-committed
        duplicate) the return value is still 0 -- indistinguishable by
        itself from the no-op case -- but the journal is drained to
        empty, which is what makes drainage (not the count) the correct
        DATA-101 signal. Contrast with test_is_noop_when_file_absent,
        where 0 leaves the file untouched/absent instead."""
        db = str(tmp_path / "e.db")
        rec = EventRecorder(db_path=db)
        entry = {
            "ts": 42.0,
            "sku": "X",
            "name": "Thing",
            "slot": 1,
            "price": 1.0,
            "methods": {"cash": 1.0},
        }
        journal_path.write_text(json.dumps(entry) + "\n", encoding="utf-8")
        first = rec.replay_sales_journal()
        assert first == 1

        # Recreate the crash-before-rewrite window: the already-committed
        # line reappears in the journal.
        journal_path.write_text(json.dumps(entry) + "\n", encoding="utf-8")

        second = rec.replay_sales_journal()

        assert second == 0  # the ambiguous integer, taken alone
        assert journal_path.exists()
        assert journal_path.read_text(encoding="utf-8") == ""


class TestReplayDuplicateSkipIsLogged:
    """Round 3, Finding D (IMPORTANT): a genuine (ts, sku) collision must
    never be silently indistinguishable from a sale dropped without a
    trace -- the idempotent skip (rowcount 0) must log a distinct,
    recognisable message naming ts and sku."""

    def test_duplicate_skip_logs_ts_and_sku_and_does_not_change_row_count(
        self, tmp_path, journal_path, caplog
    ):
        db = str(tmp_path / "e.db")
        rec = EventRecorder(db_path=db)
        entry = {
            "ts": 777.25,
            "sku": "DUPLOG",
            "name": "Thing",
            "slot": 1,
            "price": 1.0,
            "methods": {"cash": 1.0},
        }
        journal_path.write_text(json.dumps(entry) + "\n", encoding="utf-8")
        rec.replay_sales_journal()
        with sqlite3.connect(db) as conn:
            before = conn.execute("SELECT COUNT(*) FROM sales").fetchone()[0]

        # Same already-committed line reappears (the crash-before-rewrite
        # window).
        journal_path.write_text(json.dumps(entry) + "\n", encoding="utf-8")

        caplog.clear()
        with caplog.at_level("INFO"):
            second_count = rec.replay_sales_journal()

        assert second_count == 0
        with sqlite3.connect(db) as conn:
            after = conn.execute("SELECT COUNT(*) FROM sales").fetchone()[0]
        assert after == before  # behaviour, not just the log: nothing new landed

        assert "777.25" in caplog.text


class TestAtomicJournalRewrite:
    """Task 5, follow-up Fix 1 (CRITICAL): replay_sales_journal's final
    rewrite of JOURNAL_PATH must go through a sibling temp file + fsync +
    os.replace, never a truncate-then-write -- the rows it rewrites there
    can be the *only* remaining copy of a sale (one that failed both a
    `sales` insert and the rejected-evidence write). Proven here by making
    the rename step itself fail and showing the *original* journal content
    survives completely intact rather than being lost or truncated -- which
    is only possible if the rewrite never touches JOURNAL_PATH until a
    single atomic os.replace call."""

    def test_replace_failure_leaves_original_journal_intact_remaining_lines_branch(
        self, tmp_path, journal_path, monkeypatch
    ):
        """Exercises the `remaining_lines` (non-empty rewrite) branch."""
        db = str(tmp_path / "e.db")
        rec = EventRecorder(db_path=db)
        bad = {  # sku=None violates NOT NULL -- well-formed JSON, uninsertable
            "ts": 2.0,
            "sku": None,
            "name": "Bad",
            "slot": 2,
            "price": 1.0,
            "methods": {"cash": 1.0},
        }
        original_content = json.dumps(bad) + "\n"
        journal_path.write_text(original_content, encoding="utf-8")

        # Force the bad row into `remaining_lines`: it must fail to insert
        # AND fail to be set aside as rejected evidence (see
        # TestSalesJournalRejectedWriteFailure above for the same shape).
        def failing_append(_payload):
            raise OSError("simulated: disk full writing rejected-sale evidence")

        monkeypatch.setattr(
            event_recorder_module, "_append_rejected_sale_line", failing_append
        )

        def failing_replace(*args, **kwargs):
            raise OSError("simulated: rename failed (e.g. antivirus handle open)")

        monkeypatch.setattr(os, "replace", failing_replace)

        with pytest.raises(OSError, match="rename failed"):
            rec.replay_sales_journal()

        # The original journal content must be completely intact: neither
        # lost nor truncated to a partial/empty state.
        assert journal_path.read_text(encoding="utf-8") == original_content

        # Proof the mechanism really is temp-file-then-rename: the new
        # content was written to a sibling temp file (which is what
        # os.replace was about to move into place) rather than never being
        # written at all.
        tmp_sibling = journal_path.with_name(f"{journal_path.name}.tmp")
        assert tmp_sibling.exists()
        assert tmp_sibling.read_text(encoding="utf-8") == original_content

    def test_replace_failure_leaves_original_journal_intact_empty_branch(
        self, tmp_path, journal_path, monkeypatch
    ):
        """Exercises the "nothing remains" (empty rewrite) branch -- the
        fix must handle this the same atomic way, not fall back to a plain
        truncating write for the empty case."""
        db = str(tmp_path / "e.db")
        rec = EventRecorder(db_path=db)
        good = {
            "ts": 1.0,
            "sku": "GOOD1",
            "name": "One",
            "slot": 1,
            "price": 1.0,
            "methods": {"cash": 1.0},
        }
        original_content = json.dumps(good) + "\n"
        journal_path.write_text(original_content, encoding="utf-8")

        def failing_replace(*args, **kwargs):
            raise OSError("simulated: rename failed")

        monkeypatch.setattr(os, "replace", failing_replace)

        with pytest.raises(OSError, match="rename failed"):
            rec.replay_sales_journal()

        # The row was already durably inserted into `sales` (each row
        # commits in its own transaction before the final rewrite even
        # starts) -- but the journal itself must still show its original,
        # unmodified content, not a truncated/empty file, since the rename
        # that would have cleared it never completed.
        assert journal_path.read_text(encoding="utf-8") == original_content
        with sqlite3.connect(db) as conn:
            assert conn.execute("SELECT sku FROM sales").fetchall() == [("GOOD1",)]

    def test_successful_rewrite_still_produces_correct_final_content(
        self, tmp_path, journal_path
    ):
        """Sanity/GREEN companion to the two failure tests above: with no
        fault injected, the atomic rewrite still produces exactly the same
        final content the old direct-write code produced, and leaves no
        stray temp file behind."""
        db = str(tmp_path / "e.db")
        rec = EventRecorder(db_path=db)
        good = {
            "ts": 1.0,
            "sku": "GOOD1",
            "name": "One",
            "slot": 1,
            "price": 1.0,
            "methods": {"cash": 1.0},
        }
        journal_path.write_text(json.dumps(good) + "\n", encoding="utf-8")

        count = rec.replay_sales_journal()

        assert count == 1
        assert journal_path.read_text(encoding="utf-8") == ""
        tmp_sibling = journal_path.with_name(f"{journal_path.name}.tmp")
        assert not tmp_sibling.exists()


class TestWriterThreadDeadGuard:
    """Task 5, follow-up Fix 2 (IMPORTANT): a writer thread whose initial
    connect fails must exit cleanly (logged) rather than crashing silently,
    and flush() must detect that and return immediately rather than
    spinning to its full timeout on every call thereafter."""

    @staticmethod
    def _make_recorder_with_dead_writer(tmp_path, monkeypatch):
        """An EventRecorder whose writer thread's initial connect fails,
        while __init__'s own connections (_init_db/prune) succeed normally
        -- isolated the same way TestWriterThread.test_writer_survives_bad_row
        isolates the writer thread's connection, by keying off the
        check_same_thread=False kwarg that only _writer_loop passes."""
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
        # Give the daemon thread a moment to actually run and die -- avoids
        # a race where is_alive() is checked before the thread has even
        # attempted its connect.
        rec._writer.join(timeout=2.0)
        assert not rec._writer.is_alive(), (
            "test setup bug: the writer thread is still alive, so it did "
            "not fail its initial connect as intended"
        )
        return rec

    def test_record_does_not_raise_when_writer_thread_is_dead(
        self, tmp_path, monkeypatch
    ):
        rec = self._make_recorder_with_dead_writer(tmp_path, monkeypatch)
        # record() must never raise, even though nothing will ever drain
        # the queue it puts rows onto.
        rec.record("payment", value=1.0)
        rec.record_cash_collection("u1", "Alice")

    def test_flush_returns_promptly_when_writer_thread_is_dead(
        self, tmp_path, monkeypatch
    ):
        rec = self._make_recorder_with_dead_writer(tmp_path, monkeypatch)
        rec.record("payment", value=1.0)  # queues a row nothing will ever drain
        assert rec._queue.unfinished_tasks == 1

        start = time.monotonic()
        rec.flush(timeout=5.0)
        elapsed = time.monotonic() - start

        # A prompt return, not a full spin to the 5s timeout.
        assert elapsed < 1.0, (
            f"flush() took {elapsed:.2f}s -- it spun to (near) the timeout "
            "instead of detecting the dead writer thread and returning "
            "immediately"
        )
        # The row really was never drained -- this is a fast bail-out, not
        # a coincidental fast drain.
        assert rec._queue.unfinished_tasks == 1

    def test_healthy_flush_still_waits_for_queued_rows(self, tmp_path, monkeypatch):
        """Companion GREEN check: a healthy writer thread's flush() must
        still block until the row is actually written -- the new dead-writer
        fast path must not fire for a live, merely-busy writer thread."""
        real_connect = sqlite3.connect

        class _SlowInsertConnection:
            def __init__(self, conn):
                self._conn = conn

            def execute(self, sql, *args, **kwargs):
                if sql.strip().upper().startswith("INSERT"):
                    time.sleep(0.5)  # simulate a slow write
                return self._conn.execute(sql, *args, **kwargs)

            def __getattr__(self, name):
                return getattr(self._conn, name)

        def fake_connect(*args, **kwargs):
            conn = real_connect(*args, **kwargs)
            if kwargs.get("check_same_thread") is False:
                return _SlowInsertConnection(conn)
            return conn

        monkeypatch.setattr(sqlite3, "connect", fake_connect)
        db = str(tmp_path / "events.db")
        rec = EventRecorder(db_path=db)
        rec.record("payment", value=1.0)

        start = time.monotonic()
        rec.flush(timeout=5.0)
        elapsed = time.monotonic() - start

        assert rec._writer.is_alive()  # genuinely took the healthy path
        assert elapsed >= 0.4  # actually waited for the slow insert (some slack)
        with sqlite3.connect(db) as conn:
            assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1
