# services/event_recorder.py
"""
Event recorder — persists machine activity to SQLite for the dashboard.

Records payment, dispense, ice_cycle, error, service_door, temp_exceedance,
and heartbeat events. Provides time-windowed aggregate summaries.
"""
import json
import sqlite3
import time
from pathlib import Path
from typing import Optional

from loguru import logger

from services.mqtt_messages import (
    DispenserStatus, HardwareIO, IceMakerEvent, PaymentEvent, SensorReading,
)

# Must match simulators/base.py HEARTBEAT_INTERVAL
_HEARTBEAT_INTERVAL = 10.0

SUMMARY_KEYS = (
    "money_in", "products_out", "ice_cycles", "errors",
    "service_door_opens", "temp_exceedances", "uptime_pct",
)


class EventRecorder:
    """
    Persists machine events to SQLite and provides time-windowed summaries.

    Usage:
        recorder = EventRecorder(db_path="data/events.db")
        recorder.register_handlers(mqtt_client)
        vmc.set_event_recorder(recorder)  # for FSM error events
        summary = recorder.get_summary(24)
        avg = recorder.get_historical_average(24)
    """

    def __init__(
        self,
        db_path: str = "data/events.db",
        temp_min: float = -20.0,
        temp_max: float = 80.0,
    ):
        self._db_path = db_path
        self._temp_min = temp_min
        self._temp_max = temp_max
        self._init_db()

    def _init_db(self):
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self._db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS events (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_type TEXT NOT NULL,
                    timestamp  REAL NOT NULL,
                    value      REAL,
                    metadata   TEXT
                )
            """)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_type_ts ON events (event_type, timestamp)"
            )

    def record(self, event_type: str, value: float = 1.0, metadata: Optional[dict] = None):
        """Insert one event row."""
        meta_str = json.dumps(metadata) if metadata else None
        with sqlite3.connect(self._db_path) as conn:
            conn.execute(
                "INSERT INTO events (event_type, timestamp, value, metadata) VALUES (?, ?, ?, ?)",
                (event_type, time.time(), value, meta_str),
            )
        logger.debug(f"EventRecorder: {event_type} value={value}")

    def _compute_window(self, start_ts: float, end_ts: float) -> dict:
        """Compute aggregates for events in [start_ts, end_ts)."""
        with sqlite3.connect(self._db_path) as conn:
            def count(etype):
                return conn.execute(
                    "SELECT COUNT(*) FROM events WHERE event_type=? AND timestamp>=? AND timestamp<?",
                    (etype, start_ts, end_ts),
                ).fetchone()[0]

            def total(etype):
                return conn.execute(
                    "SELECT COALESCE(SUM(value), 0.0) FROM events WHERE event_type=? AND timestamp>=? AND timestamp<?",
                    (etype, start_ts, end_ts),
                ).fetchone()[0]

            heartbeat_count = count("heartbeat")
            period_secs = end_ts - start_ts
            uptime_pct = min(
                100.0,
                round(heartbeat_count * _HEARTBEAT_INTERVAL / period_secs * 100, 1),
            )
            return {
                "money_in": round(total("payment"), 2),
                "products_out": count("dispense"),
                "ice_cycles": count("ice_cycle"),
                "errors": count("error"),
                "service_door_opens": count("service_door"),
                "temp_exceedances": count("temp_exceedance"),
                "uptime_pct": uptime_pct,
            }

    def get_summary(self, period_hours: int) -> dict:
        """Return aggregate metrics for the last period_hours."""
        now = time.time()
        # Add 1 ms so events inserted at exactly `now` are included by the
        # half-open interval [start, end) used in _compute_window.
        return self._compute_window(now - period_hours * 3600, now + 0.001)

    def register_handlers(self, mqtt_client):
        pass  # implemented in Task 2

    def get_historical_average(self, period_hours: int) -> dict:
        return {k: None for k in SUMMARY_KEYS}  # implemented in Task 3
