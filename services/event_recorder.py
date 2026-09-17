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
    HardwareIO,
    IceMakerEvent,
    PaymentEvent,
    SensorReading,
    SubsystemHeartbeat,
)

# Must match simulators/base.py HEARTBEAT_INTERVAL
_HEARTBEAT_INTERVAL = 10.0

# get_historical_average only covers prior *complete* periods, so its result
# changes slowly; cache it briefly to spare the DB from up to 30 SELECTs per
# call on every dashboard poll.
_HISTORICAL_AVERAGE_CACHE_TTL = 60.0

SUMMARY_KEYS = (
    "money_in",
    "products_out",
    "ice_cycles",
    "errors",
    "service_door_opens",
    "temp_exceedances",
    "uptime_pct",
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
        retention_days: int = 90,
    ):
        self._db_path = db_path
        self._temp_min = temp_min
        self._temp_max = temp_max
        self._retention_days = retention_days
        self._last_prune = 0.0
        self._historical_avg_cache: dict[int, tuple[float, dict]] = {}
        self._init_db()
        self.prune()

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

    def record(
        self, event_type: str, value: float = 1.0, metadata: Optional[dict] = None
    ):
        """Insert one event row."""
        meta_str = json.dumps(metadata) if metadata else None
        with sqlite3.connect(self._db_path) as conn:
            conn.execute(
                "INSERT INTO events (event_type, timestamp, value, metadata) VALUES (?, ?, ?, ?)",
                (event_type, time.time(), value, meta_str),
            )
        logger.debug(f"EventRecorder: {event_type} value={value}")
        if time.time() - self._last_prune > 86400:
            self.prune()

    def prune(self):
        """Delete events older than the retention window (SD-card growth guard)."""
        cutoff = time.time() - self._retention_days * 86400
        with sqlite3.connect(self._db_path) as conn:
            cur = conn.execute("DELETE FROM events WHERE timestamp < ?", (cutoff,))
        self._last_prune = time.time()
        if cur.rowcount:
            logger.info(
                f"EventRecorder: pruned {cur.rowcount} events older than "
                f"{self._retention_days} days"
            )

    def _compute_window(self, start_ts: float, end_ts: float) -> dict:
        """Compute aggregates for events in [start_ts, end_ts).

        uptime_pct measures the fraction of _HEARTBEAT_INTERVAL-sized time
        buckets in the window that have at least one heartbeat from ANY
        subsystem — i.e. "was something alive during this interval", not raw
        heartbeat row count. Counting rows overcounts when multiple
        subsystems beat concurrently (three simulators beating every 10s
        would read ~300%, clamped to 100%) while saying nothing about whether
        any *particular* subsystem was up. Counting distinct covered buckets
        is the most this metric can honestly claim; per-subsystem health is a
        separate concern (see health_monitor / subsystem_offline events).
        """
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

            covered_buckets = conn.execute(
                "SELECT COUNT(DISTINCT CAST(timestamp / ? AS INTEGER)) FROM events "
                "WHERE event_type='heartbeat' AND timestamp>=? AND timestamp<?",
                (_HEARTBEAT_INTERVAL, start_ts, end_ts),
            ).fetchone()[0]
            period_secs = end_ts - start_ts
            uptime_pct = min(
                100.0,
                covered_buckets * _HEARTBEAT_INTERVAL / period_secs * 100,
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
        """Register MQTT handlers. Multiple callers can register for the same topic.

        Note: "dispense" events are NOT recorded from a direct
        ``hardware/dispenser`` subscription here — only the VMC (see
        ``controller.vmc.VMC._handle_mqtt_dispenser``) knows whether a
        completion was actually accepted for the active sale (correct slot,
        state == dispensing). Recording directly from the raw MQTT topic would
        overcount products_out on duplicate/late completions the VMC ignores.
        The VMC calls ``record("dispense", ...)`` itself when it accepts one.
        """
        mqtt_client.register("payment/credit", self._on_payment)
        mqtt_client.register("ice_maker/event", self._on_ice_maker_event)
        mqtt_client.register("hardware/io/service_door", self._on_service_door)
        mqtt_client.register("sensors/temp/+", self._on_sensor)
        mqtt_client.register("heartbeat/+", self._on_heartbeat)

    async def _on_payment(self, topic: str, data: dict):
        event = PaymentEvent.model_validate(data)
        self.record("payment", value=event.amount)

    async def _on_ice_maker_event(self, topic: str, data: dict):
        event = IceMakerEvent.model_validate(data)
        if event.event == "ice_dropped":
            self.record("ice_cycle", value=1.0)

    async def _on_service_door(self, topic: str, data: dict):
        hw = HardwareIO.model_validate(data)
        if hw.state:
            self.record("service_door", value=1.0)

    async def _on_sensor(self, topic: str, data: dict):
        reading = SensorReading.model_validate(data)
        if not (self._temp_min <= reading.value <= self._temp_max):
            self.record(
                "temp_exceedance",
                value=reading.value,
                metadata={"location": reading.location},
            )

    async def _on_heartbeat(self, topic: str, data: dict):
        hb = SubsystemHeartbeat.model_validate(data)
        if hb.uptime_seconds < 0:
            # uptime_seconds == -1 is the MQTT Last-Will payload meaning the
            # subsystem went OFFLINE — recording it as a "heartbeat" would
            # make an outage count as uptime in _compute_window.
            self.record("subsystem_offline", metadata={"subsystem": hb.subsystem})
            return
        self.record("heartbeat", value=float(hb.uptime_seconds))

    def get_historical_average(self, period_hours: int) -> dict:
        """
        Return per-period averages over prior complete periods (up to 30).
        Only includes periods where at least one heartbeat was recorded
        (machine was running). Returns all-None if fewer than 2 such periods
        exist. Result is cached briefly per period_hours since it only
        covers prior *complete* periods and doesn't change quickly.
        """
        now = time.time()
        cached = self._historical_avg_cache.get(period_hours)
        if cached is not None:
            cached_at, cached_result = cached
            if now - cached_at < _HISTORICAL_AVERAGE_CACHE_TTL:
                return cached_result

        period_secs = period_hours * 3600
        window_start = now - period_secs

        active_windows = []
        for i in range(1, 31):
            end = window_start - (i - 1) * period_secs
            start = end - period_secs
            w = self._compute_window(start, end)
            if w["uptime_pct"] > 0:
                active_windows.append(w)

        if len(active_windows) < 2:
            result = {k: None for k in SUMMARY_KEYS}
        else:
            avg = {}
            for key in SUMMARY_KEYS:
                values = [w[key] for w in active_windows if w[key] is not None]
                avg[key] = round(sum(values) / len(values), 2) if values else None
            result = avg

        self._historical_avg_cache[period_hours] = (now, result)
        return result
