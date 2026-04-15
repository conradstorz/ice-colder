# Dashboard Activity Panel Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a 3-column activity panel (24h / 7d / 30d) to the VMC web dashboard showing revenue, products dispensed, ice cycles, errors, service door events, and temperature exceedances — each with current-period and historical-average figures.

**Architecture:** A new `EventRecorder` service persists MQTT events to SQLite (`data/events.db`). The VMC calls it on error transitions. FastAPI gains a `/activity` HTMX partial polled every 60 seconds. All other dashboard panels are unchanged.

**Tech Stack:** Python stdlib `sqlite3`, existing FastAPI + Jinja2 + HTMX, existing `MQTTClient` handler registration pattern.

---

## File Map

| File | Action | Responsibility |
|------|--------|----------------|
| `services/event_recorder.py` | Create | SQLite persistence, MQTT handlers, summary queries |
| `tests/test_event_recorder.py` | Create | Full test coverage for EventRecorder |
| `controller/vmc.py` | Modify | Add `set_event_recorder()`, call recorder in `on_error()` |
| `main.py` | Modify | Instantiate EventRecorder, wire to MQTT, VMC, routes |
| `web_interface/routes.py` | Modify | Add `set_event_recorder()`, add `/activity` route |
| `web_interface/templates/partials/activity_fragment.html` | Create | 3-column activity panel Jinja2 template |
| `web_interface/templates/dashboard.html` | Modify | Add activity panel with HTMX polling |
| `docker-compose.yml` | Modify | Mount `./data` directory for events.db persistence |

---

## Task 1: EventRecorder — core (schema, record, get_summary)

**Files:**
- Create: `services/event_recorder.py`
- Create: `tests/test_event_recorder.py`

- [ ] **Step 1: Write failing tests for init and record()**

```python
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
```

- [ ] **Step 2: Run tests to confirm they fail**

```
uv run pytest tests/test_event_recorder.py -v
```
Expected: `ModuleNotFoundError: No module named 'services.event_recorder'`

- [ ] **Step 3: Implement EventRecorder init, record(), and get_summary()**

```python
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
        return self._compute_window(now - period_hours * 3600, now)

    def register_handlers(self, mqtt_client):
        pass  # implemented in Task 2

    def get_historical_average(self, period_hours: int) -> dict:
        return {k: None for k in SUMMARY_KEYS}  # implemented in Task 3
```

- [ ] **Step 4: Run tests to confirm they pass**

```
uv run pytest tests/test_event_recorder.py::TestInit tests/test_event_recorder.py::TestRecord tests/test_event_recorder.py::TestGetSummary -v
```
Expected: all pass

- [ ] **Step 5: Commit**

```bash
git add services/event_recorder.py tests/test_event_recorder.py
git commit -m "feat: add EventRecorder with SQLite persistence and get_summary"
```

---

## Task 2: EventRecorder — MQTT handlers

**Files:**
- Modify: `services/event_recorder.py`
- Modify: `tests/test_event_recorder.py`

- [ ] **Step 1: Add failing tests for register_handlers()**

Add this class to `tests/test_event_recorder.py`:

```python
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
            {"amount": 2.50, "method": "cash_coin", "timestamp": "2026-01-01T00:00:00+00:00"},
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
            {"slot": 0, "state": "motor_active", "timestamp": "2026-01-01T00:00:00+00:00"},
        )
        assert recorder.get_summary(24)["products_out"] == 0

    @pytest.mark.asyncio
    async def test_ice_dropped_records_cycle(self, recorder):
        h = self._get_handlers(recorder)
        await h["ice_maker/event"](
            "ice_maker/event",
            {"event": "ice_dropped", "detail": None, "timestamp": "2026-01-01T00:00:00+00:00"},
        )
        assert recorder.get_summary(24)["ice_cycles"] == 1

    @pytest.mark.asyncio
    async def test_ice_power_on_not_recorded(self, recorder):
        h = self._get_handlers(recorder)
        await h["ice_maker/event"](
            "ice_maker/event",
            {"event": "power_on", "detail": None, "timestamp": "2026-01-01T00:00:00+00:00"},
        )
        assert recorder.get_summary(24)["ice_cycles"] == 0

    @pytest.mark.asyncio
    async def test_service_door_open_records_event(self, recorder):
        h = self._get_handlers(recorder)
        await h["hardware/io/service_door"](
            "hardware/io/service_door",
            {"device": "service_door", "state": True, "timestamp": "2026-01-01T00:00:00+00:00"},
        )
        assert recorder.get_summary(24)["service_door_opens"] == 1

    @pytest.mark.asyncio
    async def test_service_door_close_not_recorded(self, recorder):
        h = self._get_handlers(recorder)
        await h["hardware/io/service_door"](
            "hardware/io/service_door",
            {"device": "service_door", "state": False, "timestamp": "2026-01-01T00:00:00+00:00"},
        )
        assert recorder.get_summary(24)["service_door_opens"] == 0

    @pytest.mark.asyncio
    async def test_out_of_range_temp_records_exceedance(self, recorder):
        h = self._get_handlers(recorder)
        await h["sensors/temp/+"](
            "sensors/temp/evaporator",
            {"location": "evaporator", "value": 95.0, "unit": "C", "timestamp": "2026-01-01T00:00:00+00:00"},
        )
        assert recorder.get_summary(24)["temp_exceedances"] == 1

    @pytest.mark.asyncio
    async def test_normal_temp_not_recorded(self, recorder):
        h = self._get_handlers(recorder)
        await h["sensors/temp/+"](
            "sensors/temp/evaporator",
            {"location": "evaporator", "value": 22.0, "unit": "C", "timestamp": "2026-01-01T00:00:00+00:00"},
        )
        assert recorder.get_summary(24)["temp_exceedances"] == 0

    @pytest.mark.asyncio
    async def test_heartbeat_recorded(self, recorder):
        h = self._get_handlers(recorder)
        await h["heartbeat/+"](
            "heartbeat/vending",
            {"subsystem": "vending", "uptime_seconds": 300, "timestamp": "2026-01-01T00:00:00+00:00"},
        )
        assert recorder.get_summary(24)["uptime_pct"] > 0
```

- [ ] **Step 2: Run tests to confirm they fail**

```
uv run pytest tests/test_event_recorder.py::TestRegisterHandlers -v
```
Expected: FAIL — handlers dict empty (register_handlers is a no-op)

- [ ] **Step 3: Implement register_handlers() and async handlers in event_recorder.py**

Replace the `register_handlers` stub and add handler methods:

```python
def register_handlers(self, mqtt_client):
    """Register MQTT handlers. Multiple callers can register for the same topic."""
    mqtt_client.register("payment/credit", self._on_payment)
    mqtt_client.register("hardware/dispenser", self._on_dispenser)
    mqtt_client.register("ice_maker/event", self._on_ice_maker_event)
    mqtt_client.register("hardware/io/service_door", self._on_service_door)
    mqtt_client.register("sensors/temp/+", self._on_sensor)
    mqtt_client.register("heartbeat/+", self._on_heartbeat)

async def _on_payment(self, topic: str, data: dict):
    event = PaymentEvent.model_validate(data)
    self.record("payment", value=event.amount)

async def _on_dispenser(self, topic: str, data: dict):
    status = DispenserStatus.model_validate(data)
    if status.state == "complete":
        self.record("dispense", value=float(status.slot))

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
        self.record("temp_exceedance", value=reading.value,
                    metadata={"location": reading.location})

async def _on_heartbeat(self, topic: str, data: dict):
    uptime = float(data.get("uptime_seconds", 0))
    self.record("heartbeat", value=uptime)
```

- [ ] **Step 4: Run all event recorder tests**

```
uv run pytest tests/test_event_recorder.py -v
```
Expected: all pass

- [ ] **Step 5: Commit**

```bash
git add services/event_recorder.py tests/test_event_recorder.py
git commit -m "feat: add EventRecorder MQTT handlers for all event types"
```

---

## Task 3: EventRecorder — historical average

**Files:**
- Modify: `services/event_recorder.py`
- Modify: `tests/test_event_recorder.py`

- [ ] **Step 1: Add failing tests for get_historical_average()**

Add this class to `tests/test_event_recorder.py`:

```python
class TestGetHistoricalAverage:
    def test_returns_none_with_no_data(self, recorder):
        avg = recorder.get_historical_average(24)
        assert avg["money_in"] is None
        assert avg["products_out"] is None

    def test_returns_none_with_only_one_prior_period(self, tmp_path):
        db = str(tmp_path / "events.db")
        rec = EventRecorder(db_path=db)
        # Insert data in one prior 24h period only (25-49h ago)
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
```

- [ ] **Step 2: Run tests to confirm they fail**

```
uv run pytest tests/test_event_recorder.py::TestGetHistoricalAverage -v
```
Expected: FAIL — `get_historical_average` returns all None regardless

- [ ] **Step 3: Implement get_historical_average() in event_recorder.py**

Replace the stub `get_historical_average` method:

```python
def get_historical_average(self, period_hours: int) -> dict:
    """
    Return per-period averages over prior complete periods (up to 30).
    Only includes periods where at least one heartbeat was recorded
    (machine was running). Returns all-None if fewer than 2 such periods exist.
    """
    period_secs = period_hours * 3600
    now = time.time()
    window_start = now - period_secs

    active_windows = []
    for i in range(1, 31):
        end = window_start - (i - 1) * period_secs
        start = end - period_secs
        w = self._compute_window(start, end)
        if w["uptime_pct"] > 0:
            active_windows.append(w)

    if len(active_windows) < 2:
        return {k: None for k in SUMMARY_KEYS}

    avg = {}
    for key in SUMMARY_KEYS:
        values = [w[key] for w in active_windows if w[key] is not None]
        avg[key] = round(sum(values) / len(values), 2) if values else None
    return avg
```

- [ ] **Step 4: Run all event recorder tests**

```
uv run pytest tests/test_event_recorder.py -v
```
Expected: all pass

- [ ] **Step 5: Commit**

```bash
git add services/event_recorder.py tests/test_event_recorder.py
git commit -m "feat: add EventRecorder.get_historical_average with inactive-period exclusion"
```

---

## Task 4: VMC wiring — on_error records to EventRecorder

**Files:**
- Modify: `controller/vmc.py`
- Modify: `tests/test_vmc_fsm.py`

- [ ] **Step 1: Add failing test**

Add this class to `tests/test_vmc_fsm.py`:

```python
class TestEventRecorder:
    def test_error_transition_calls_recorder(self):
        from unittest.mock import MagicMock
        config = ConfigModel()
        vmc = VMC(config=config)
        recorder = MagicMock()
        vmc.set_event_recorder(recorder)
        vmc.error_occurred()
        recorder.record.assert_called_once_with("error", value=1.0)
```

- [ ] **Step 2: Run to confirm failure**

```
uv run pytest tests/test_vmc_fsm.py::TestEventRecorder -v
```
Expected: FAIL — `VMC has no attribute 'set_event_recorder'`

- [ ] **Step 3: Add set_event_recorder and recorder call in on_error()**

In `controller/vmc.py`, in `__init__` after `self._inventory`:
```python
self._event_recorder = None  # Set via set_event_recorder()
```

After `set_inventory_manager()`, add:
```python
def set_event_recorder(self, recorder):
    """Attach an EventRecorder so FSM error events are persisted."""
    self._event_recorder = recorder
    logger.debug("VMC attached event recorder.")
```

At the top of `on_error()`, after the `logger.error(...)` line, add:
```python
if self._event_recorder:
    self._event_recorder.record("error", value=1.0)
```

- [ ] **Step 4: Run tests**

```
uv run pytest tests/test_vmc_fsm.py -v
```
Expected: all pass

- [ ] **Step 5: Commit**

```bash
git add controller/vmc.py tests/test_vmc_fsm.py
git commit -m "feat: wire EventRecorder into VMC for error event persistence"
```

---

## Task 5: main.py + routes wiring + docker-compose

**Files:**
- Modify: `main.py`
- Modify: `web_interface/routes.py`
- Modify: `docker-compose.yml`
- Modify: `tests/test_web_routes.py`

- [ ] **Step 1: Add failing test for /activity route**

Add this class to `tests/test_web_routes.py`:

```python
class TestActivityEndpoint:
    def test_activity_returns_html(self, client):
        response = client.get("/activity")
        assert response.status_code == 200
        assert "24" in response.text or "Hours" in response.text

    def test_activity_without_recorder_returns_fallback(self, client):
        # The test client fixture doesn't set an event recorder
        response = client.get("/activity")
        assert response.status_code == 200
```

- [ ] **Step 2: Run to confirm failure**

```
uv run pytest tests/test_web_routes.py::TestActivityEndpoint -v
```
Expected: FAIL — 404 Not Found (route doesn't exist yet)

- [ ] **Step 3: Add set_event_recorder and /activity route to routes.py**

In `web_interface/routes.py`, after `health_monitor = None` and `set_health_monitor`:

```python
event_recorder = None

def set_event_recorder(recorder):
    global event_recorder
    event_recorder = recorder
```

Inside `attach_routes`, after the `/health` route, add:

```python
@router.get("/activity", response_class=HTMLResponse)
async def activity_fragment(request: Request):
    if not event_recorder:
        return HTMLResponse(
            '<div class="bg-gray-800 p-4 rounded text-gray-500">Activity data not available yet.</div>'
        )
    summaries = {
        24: event_recorder.get_summary(24),
        168: event_recorder.get_summary(168),
        720: event_recorder.get_summary(720),
    }
    averages = {
        24: event_recorder.get_historical_average(24),
        168: event_recorder.get_historical_average(168),
        720: event_recorder.get_historical_average(720),
    }
    return templates.TemplateResponse("partials/activity_fragment.html", {
        "request": request,
        "summaries": summaries,
        "averages": averages,
    })
```

- [ ] **Step 4: Wire EventRecorder in main.py**

Add import at the top of `main.py`:
```python
from services.event_recorder import EventRecorder
```

In `main()`, after the MQTT client is created (after line `vmc.set_health_monitor(health)`), add:
```python
# Create event recorder and wire to MQTT, VMC, and routes
recorder = EventRecorder(db_path="data/events.db")
recorder.register_handlers(mqtt)
vmc.set_event_recorder(recorder)
routes.set_event_recorder(recorder)
logger.info("Event recorder wired up")
```

- [ ] **Step 5: Add data volume to docker-compose.yml**

In `docker-compose.yml`, add to the `vmc` service volumes:
```yaml
    volumes:
      - ./config.json:/app/config.json:ro
      - ./LOGS:/app/LOGS
      - ./data:/app/data
```

- [ ] **Step 6: Run tests**

```
uv run pytest tests/ -v
```
Expected: all pass (or same skips as before)

- [ ] **Step 7: Commit**

```bash
git add main.py web_interface/routes.py docker-compose.yml tests/test_web_routes.py
git commit -m "feat: wire EventRecorder into main, routes, and Docker volume"
```

---

## Task 6: Templates — activity panel

**Files:**
- Create: `web_interface/templates/partials/activity_fragment.html`
- Modify: `web_interface/templates/dashboard.html`

- [ ] **Step 1: Create activity_fragment.html**

```html
{# web_interface/templates/partials/activity_fragment.html #}
<div class="bg-gray-800 p-4 rounded space-y-3">
  <h3 class="text-lg font-semibold">Machine Activity</h3>

  <div class="grid grid-cols-3 gap-4 text-sm">

    {% for hours, label in [(24, "Last 24 Hours"), (168, "Last 7 Days"), (720, "Last 30 Days")] %}
    {% set s = summaries[hours] %}
    {% set a = averages[hours] %}
    <div class="bg-gray-700 p-3 rounded space-y-2">
      <div class="font-semibold text-gray-300 border-b border-gray-600 pb-1">{{ label }}</div>

      <div class="flex justify-between">
        <span class="text-gray-400">Uptime</span>
        <span>
          {{ s.uptime_pct }}%
          {% if a.uptime_pct is not none %}
            <span class="text-gray-500 text-xs">avg {{ a.uptime_pct }}%</span>
          {% else %}
            <span class="text-gray-600 text-xs">avg —</span>
          {% endif %}
        </span>
      </div>

      <div class="flex justify-between">
        <span class="text-gray-400">Money In</span>
        <span>
          ${{ "%.2f"|format(s.money_in) }}
          {% if a.money_in is not none %}
            <span class="text-gray-500 text-xs">avg ${{ "%.2f"|format(a.money_in) }}</span>
          {% else %}
            <span class="text-gray-600 text-xs">avg —</span>
          {% endif %}
        </span>
      </div>

      <div class="flex justify-between">
        <span class="text-gray-400">Products Out</span>
        <span>
          {{ s.products_out }}
          {% if a.products_out is not none %}
            <span class="text-gray-500 text-xs">avg {{ a.products_out }}</span>
          {% else %}
            <span class="text-gray-600 text-xs">avg —</span>
          {% endif %}
        </span>
      </div>

      <div class="flex justify-between">
        <span class="text-gray-400">Ice Cycles</span>
        <span>
          {{ s.ice_cycles }}
          {% if a.ice_cycles is not none %}
            <span class="text-gray-500 text-xs">avg {{ a.ice_cycles }}</span>
          {% else %}
            <span class="text-gray-600 text-xs">avg —</span>
          {% endif %}
        </span>
      </div>

      <div class="flex justify-between">
        <span class="text-gray-400">Errors</span>
        <span class="{{ 'text-red-400' if s.errors > 0 else '' }}">
          {{ s.errors }}
          {% if a.errors is not none %}
            <span class="text-gray-500 text-xs">avg {{ a.errors }}</span>
          {% else %}
            <span class="text-gray-600 text-xs">avg —</span>
          {% endif %}
        </span>
      </div>

      <div class="flex justify-between">
        <span class="text-gray-400">Service Door</span>
        <span>
          {{ s.service_door_opens }}
          {% if a.service_door_opens is not none %}
            <span class="text-gray-500 text-xs">avg {{ a.service_door_opens }}</span>
          {% else %}
            <span class="text-gray-600 text-xs">avg —</span>
          {% endif %}
        </span>
      </div>

      <div class="flex justify-between">
        <span class="text-gray-400">Temp Issues</span>
        <span class="{{ 'text-yellow-400' if s.temp_exceedances > 0 else '' }}">
          {{ s.temp_exceedances }}
          {% if a.temp_exceedances is not none %}
            <span class="text-gray-500 text-xs">avg {{ a.temp_exceedances }}</span>
          {% else %}
            <span class="text-gray-600 text-xs">avg —</span>
          {% endif %}
        </span>
      </div>

    </div>
    {% endfor %}

  </div>
</div>
```

- [ ] **Step 2: Add activity panel to dashboard.html**

Insert between the `status-panel` div and the action buttons div (after the `</div>` closing the status panel, before `<div class="mt-6 space-x-2">`):

```html
        <div id="activity-panel"
             class="mt-4"
             hx-get="/activity"
             hx-trigger="load, every 60s"
             hx-swap="innerHTML">
            <div class="bg-gray-800 p-4 rounded text-gray-500 text-sm">Loading activity...</div>
        </div>
```

- [ ] **Step 3: Run full test suite**

```
uv run pytest tests/ -v
```
Expected: all pass

- [ ] **Step 4: Rebuild Docker and verify dashboard**

```
docker compose down
docker compose up -d --build
```

Open `http://localhost:26123` — verify the activity panel appears with three columns showing zeros (no historical average yet, all `—`).

- [ ] **Step 5: Commit**

```bash
git add web_interface/templates/partials/activity_fragment.html web_interface/templates/dashboard.html
git commit -m "feat: add activity panel template with 3-column 24h/7d/30d layout"
```
