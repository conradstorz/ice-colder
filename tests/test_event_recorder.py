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
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()]
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
