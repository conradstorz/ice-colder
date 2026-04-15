# Dashboard Activity Panel Design

**Goal:** Add a machine activity overview to the VMC web dashboard showing revenue, product output, ice cycles, errors, service door events, and temperature exceedances across three time windows (24h, 7d, 30d), each with current-period and historical-average figures.

**Architecture:** A new `EventRecorder` service persists MQTT events to a local SQLite database. The existing FastAPI dashboard gains a new `/activity` HTMX partial that queries the recorder for aggregates. No new infrastructure — SQLite file is bind-mounted in Docker.

**Tech Stack:** SQLite (stdlib `sqlite3`), existing FastAPI + Jinja2 + HTMX dashboard, existing `MQTTClient` handler registration pattern.

---

## Data Model

Single table `events` in `events.db`:

| column | type | notes |
|--------|------|-------|
| `id` | INTEGER PRIMARY KEY | autoincrement |
| `event_type` | TEXT NOT NULL | one of the types below |
| `timestamp` | REAL NOT NULL | Unix timestamp (UTC) |
| `value` | REAL | numeric quantity |
| `metadata` | TEXT | JSON blob for extra context |

### Event Types

| event_type | MQTT trigger | value field |
|------------|-------------|-------------|
| `payment` | `PaymentEvent` on `payment/credit` | dollar amount |
| `dispense` | `DispenserStatus` state=`complete` on `hardware/dispenser` | slot number |
| `ice_cycle` | `IceMakerEvent` event=`ice_dropped` on `ice_maker/event` | 1 |
| `error` | VMC FSM transition to `error` state | 1 |
| `service_door` | `HardwareIO` device=`service_door` state=`True` on `hardware/io/service_door` | 1 |
| `temp_exceedance` | `SensorReading` outside normal range on `sensors/temp/+` | actual temp value |

Uptime is derived at query time from heartbeat event timestamps — no separate row type needed. A heartbeat row is recorded per subsystem heartbeat received; uptime % is calculated as (time windows covered by heartbeats) / (total window length).

---

## EventRecorder Service

**File:** `services/event_recorder.py`

### Responsibilities
- Create and own the SQLite connection
- Register MQTT handlers via `register_handlers(mqtt_client)`
- Record events from VMC FSM directly via `record(event_type, value, metadata)`
- Provide `get_summary(period_hours) -> dict` for current-period aggregates
- Provide `get_historical_average(period_hours) -> dict` for per-period historical averages

### Summary dict shape (same structure from both methods)
```python
{
    "money_in": 14.50,        # sum of payment values
    "products_out": 6,        # count of dispense events
    "ice_cycles": 12,         # count of ice_cycle events
    "errors": 0,              # count of error events
    "service_door_opens": 1,  # count of service_door events
    "temp_exceedances": 2,    # count of temp_exceedance events
    "uptime_pct": 98.5,       # % of period covered by heartbeats
}
```

### Historical average calculation
For `get_historical_average(period_hours)`: look back `N` complete prior periods of the same length (N=30 for 24h periods, N=12 for 7d periods, N=12 for 30d periods), compute the per-period average of each metric. If fewer than 2 complete prior periods exist, return `None` for each metric (displayed as `—` in the UI).

### Temperature out-of-norm thresholds
Reuse the same thresholds already defined in `HealthMonitor` — pass a reference or share a constants module. No new config fields.

### Wiring in main.py
```python
recorder = EventRecorder(db_path="events.db")
recorder.register_handlers(mqtt)
vmc.set_event_recorder(recorder)   # so FSM can call recorder.record("error", ...)
routes.set_event_recorder(recorder)
```

---

## Dashboard UI

### New route
`GET /activity` → returns `partials/activity_fragment.html`

Route queries:
- `recorder.get_summary(24)`, `recorder.get_historical_average(24)`
- `recorder.get_summary(168)`, `recorder.get_historical_average(168)`
- `recorder.get_summary(720)`, `recorder.get_historical_average(720)`

Passes all six dicts to the template.

### Poll interval
`hx-trigger="every 60s"` — owner view, no need for sub-second updates.

### Layout
Three equal columns: **Last 24 Hours** | **Last 7 Days** | **Last 30 Days**

Each column contains a card with seven metric rows:

```
Uptime          98.5%    avg 99.1%
Money In        $14.50   avg $18.20
Products Out    6        avg 8
Ice Cycles      12       avg 15
Errors          0        avg 0.3
Service Door    1        avg 0.8
Temp Issues     2        avg 1.1
```

Current value is displayed prominently; historical average is smaller muted text alongside it. If historical average is `None` (insufficient history), display `—`.

### Dashboard placement
The activity panel is inserted between the existing status panel and the action buttons. The health, inventory, logs, and config panels are unchanged.

---

## Docker

`events.db` bind-mounted in `docker-compose.yml`:
```yaml
volumes:
  - ./events.db:/app/events.db
```

The file is created automatically by `EventRecorder` on first run if it doesn't exist.

---

## Out of Scope
- Charting or graphs (text table only)
- Per-product revenue breakdown
- Export / CSV download
- Push notifications
- Authentication / access control
