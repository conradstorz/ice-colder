# tests/test_event_recorder.py
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
        # 360 heartbeats × 10s = 3600s in a 24h (86400s) window → ~4.2%
        for _ in range(360):
            recorder.record("heartbeat", value=100)
        assert recorder.get_summary(24)["uptime_pct"] == pytest.approx(4.2, abs=0.1)


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

    @pytest.mark.asyncio
    async def test_dispenser_complete_records_dispense(self, recorder):
        h = self._get_handlers(recorder)
        await h["hardware/dispenser"](
            "hardware/dispenser",
            {"slot": 0, "state": "complete", "timestamp": "2026-01-01T00:00:00+00:00"},
        )
        assert recorder.get_summary(24)["products_out"] == 1

    @pytest.mark.asyncio
    async def test_dispenser_motor_active_not_recorded(self, recorder):
        h = self._get_handlers(recorder)
        await h["hardware/dispenser"](
            "hardware/dispenser",
            {
                "slot": 0,
                "state": "motor_active",
                "timestamp": "2026-01-01T00:00:00+00:00",
            },
        )
        assert recorder.get_summary(24)["products_out"] == 0

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


class TestGetHistoricalAverage:
    def test_returns_none_with_no_data(self, recorder):
        avg = recorder.get_historical_average(24)
        assert avg["money_in"] is None
        assert avg["products_out"] is None

    def test_returns_averages_with_one_prior_period(self, tmp_path):
        db = str(tmp_path / "events.db")
        rec = EventRecorder(db_path=db)
        # Insert data in one prior 24h period only (25-49h ago) — one period is enough
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
        assert avg["money_in"] == 5.0

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
