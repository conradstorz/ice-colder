# tests/test_event_recorder.py
import json
import os
import sqlite3
import time

import pytest

from services.event_recorder import EventRecorder


@pytest.fixture
def recorder(tmp_path):
    return EventRecorder(db_path=str(tmp_path / "events.db"))


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
