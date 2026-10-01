# Subsystem windows — design

**Date:** 2026-09-30
**Scope:** `/health/subsystems/{name}` for the three boards in
`EXPECTED_SUBSYSTEMS` (`vending`, `mdb`, `ice_maker`), plus the contract,
simulator, health-monitor and availability changes needed to feed it.
`/health/subsystems` (the list), `/screen`, the Tests level and every other
page are unchanged.

## 1. Problem

The subsystem detail page is the operator's window into one board. Today it
shows the board's identity, then every temperature the machine has ever seen
regardless of which board reported it — `web_interface/routes/health.py:302`
documents this as a stand-in because no subsystem-to-sensor mapping existed.
It shows no binary IO state, no telemetry other than temperature, and none of
the board's own controls.

## 2. Requirement

Each board's window shows only what comes from that board, live:

1. **Monitors** — every channel the board declares, with its latest value,
   unit, the wall-clock time of the last measurement and its age.
2. **Digital signals** — an obvious on/off indication for every binary input
   and output, and the dwell time (time since the signal last changed).
3. **Outputs** — the output's name inside a shape tinted green (active), red
   (inactive) or gray (no live signal), with a distinct marking when the VMC
   is currently refusing the command that drives it (inhibited).
4. **Controls** — the board's actuator commands, marked inhibited the same
   way, plus its standard commands. No actuation from this page.
5. **Live** — the readings refresh every 2 s without a page reload.

## 3. Principle: the board's capabilities document is the only source of what its window shows

Every board publishes a retained `capabilities/<name>` document listing its
`channels` and `commands`. The window renders exactly those channels and
commands, in declaration order. A reading arriving on the bus is attributed
to a board only if that board declared the channel; readings for undeclared
channels are still consumed by the VMC for control (nothing in the control
path changes) but never shown on any window. A board that declares nothing
shows an empty Monitors section, not another board's data.

## 4. Components

### 4.1 Contract (`contracts/common.py`, both CONTRACT.md files, JSON schemas)

`ChannelDescriptor` gains two optional, additive fields:

| Field | Type | Default | Meaning |
|---|---|---|---|
| `direction` | `"input" \| "output"` | `"input"` | Output = something the board drives (motor, solenoid, relay, compressor). Input = something it senses. |
| `driven_by` | `str \| None` | `None` | For an output (or a payment device): the command name whose refusal by the VMC means this signal is inhibited. |

Both contracts bump a minor version: `contracts/vending_machine.py`
`0.6.0 → 0.7.0`, `contracts/ice_maker_monitor.py` `1.3.0 → 1.4.0`. Each
CONTRACT.md gets a "Semantics fixed in" entry naming the two fields.
`docs/contracts/*/schemas/channel_descriptor.schema.json` and
`monitor_capabilities` / `subsystem_capabilities` are regenerated with
`uv run python -m contracts.generate` (the drift test in
`tests/test_contract_schemas.py` enforces this). The
`subsystem_capabilities` model is not yet in the vending schema set; it is
added to `VENDING_MODELS` so the vending contract documents its own
capabilities shape.

### 4.2 Simulators declare their channels

`simulators/base.py` `build_capabilities()` gains a `CHANNELS: list[ChannelDescriptor]`
class attribute (default empty) that every subclass fills, so the base
passes `channels=self.CHANNELS` alongside `commands`. Declared channel ids
must equal the id the board actually publishes under.

**Vending** (`simulators/vending_machine.py`), publish interval as today:

| channel_id | kind | unit | direction | driven_by |
|---|---|---|---|---|
| `cabinet` | temperature | C | input | — |
| `water_flow` | counter | gal | input | — |
| `bag_full_sensor` | binary | | input | — |
| `water_flow_sensor` | binary | | input | — |
| `bin_half_full` | binary | | input | — |
| `auger_motor` | binary | | output | `dispense` |
| `agitator_motor` | binary | | output | `dispense` |
| `bag_drop_solenoid` | binary | | output | `dispense` |
| `water_valve_solenoid` | binary | | output | `water_valve` |
| `fan` | binary | | output | — (autonomous) |
| `heater_relay` | binary | | output | — (autonomous) |

**MDB** (`simulators/mdb_gateway.py`): `coin_acceptor`, `bill_validator`,
`card_reader` — kind binary, direction input, `driven_by="payment/enable"`.
Their readiness text (`ready`/`disabled`/`error`) is carried as the signal's
`text`; value is 1.0 for `ready`, 0.0 otherwise.

**Ice maker** (`simulators/ice_maker.py`): the existing ten temperature
channels and two telemetry channels keep `direction="input"`; add
`compressor_run` — kind binary, direction output (the board already has a `compressor` temperature input, so the output needs its own id; channel ids are unique per board), `driven_by=None` — whose value
is derived by the VMC from the board's `power_on`/`power_off` events. The
simulator does not publish a new topic for it; the events are the signal.

### 4.3 Health monitor: per-board signal store (`services/health_monitor.py`)

```python
@dataclass
class Signal:
    value: float
    text: str | None          # MDB readiness word; None otherwise
    updated_mono: float       # time.monotonic() of the last reading
    updated_wall: float       # time.time() of the last reading
    transition_mono: float    # monotonic of the last value change (binary only)
    transition_wall: float
    transitions_seen: int     # 0 until the first change after VMC start
```

`record_signal(subsystem: str, channel_id: str, value: float, *, text: str | None = None) -> None`
stores or updates `self._signals[subsystem][channel_id]`. A value change on a
channel whose declared kind is `binary` (or whose previous value differs and
no descriptor is known) sets `transition_*` and increments
`transitions_seen`; the first reading after start sets `transition_*` to the
reading time with `transitions_seen == 0`, so dwell reads "since VMC start"
until a real change is observed.

`record_temperature(location, value)` keeps its signature and its
out-of-range alerting, and **also** calls `record_signal` for the board that
declared a `temperature` channel with `channel_id == location`, found via
`self._subsystems[*].capabilities["channels"]`. If no board declares it, the
reading is kept in `_temperatures` (for `/screen` and alerts) and not
attributed. `record_channel(channel_id, value)` keeps its signature and
attributes the same way for any kind. The existing `_channels` dict stays for
`get_summary()["channels"]` compatibility.

`get_summary()` adds:

```
"signals": {
  "<subsystem>": {
    "<channel_id>": {
      "value": float, "text": str | None,
      "age_seconds": float, "updated_at": float (epoch seconds),
      "dwell_seconds": float, "transition_at": float (epoch seconds),
      "transitions_seen": int
    }
  }
}
```

and each subsystem row gains `"channels": list[dict]` — the declared
descriptors as plain dicts (id, kind, unit, description, direction,
driven_by), in declaration order — next to the existing `channel_count`.

### 4.4 VMC ingestion (`controller/vmc.py`)

| Topic | Board | Call |
|---|---|---|
| `sensors/temp/+` | attributed by declaration (see 4.3) | `record_temperature(location, value)` (unchanged call) |
| `sensors/water_flow` | vending | new handler registration → `record_channel("water_flow", value)`; today this topic is not subscribed |
| `hardware/io/+` | vending | existing handler additionally calls `record_signal("vending", device, 1.0 if state else 0.0)` |
| `payment/status` | mdb | existing handler additionally calls `record_signal("mdb", device, 1.0 if state == "ready" else 0.0, text=state)` |
| `telemetry/ice_maker/+` | ice_maker | existing `record_channel` (unchanged) |
| `ice_maker/event` `power_on`/`power_off` | ice_maker | existing handler additionally calls `record_signal("ice_maker", "compressor_run", 1.0/0.0)` |

Every call is guarded by `if self._health_monitor`, as today. No control
logic changes.

### 4.5 Inhibited (`services/availability.py`)

```python
def command_inhibited(self, command: str) -> bool
```

| command | inhibited when |
|---|---|
| `dispense` | neither `sale_available("ice")` nor `sale_available("water")` passes |
| `water_valve` | `sale_available("water")` fails |
| `payment/enable` | `payment_enabled()` is False |
| anything else | never |

A signal with `driven_by` set is inhibited iff `command_inhibited(driven_by)`.
With no `Availability` wired (tests, or `/health` before startup wiring),
nothing is inhibited.

### 4.6 Route (`web_interface/routes/health.py`)

`GET /health/subsystems/{name}` (gate `view_status`, unchanged) renders
`health_subsystem.html`: the identity card exactly as today, then

```html
<div id="live" hx-get="/health/subsystems/{name}/live"
     hx-trigger="load, every 2s" hx-swap="innerHTML" hx-target="this">
  {% include "partials/subsystem_live.html" %}
</div>
```

(the include gives a first paint with no blank; the stable wrapper owns every
htmx attribute, the fragment carries none — the pattern the pill should have
used.) The temperature stand-in and its comment are deleted.

`GET /health/subsystems/{name}/live` (gate `view_status`; 404 for a name not
in `EXPECTED_SUBSYSTEMS`) renders `partials/subsystem_live.html` only. It is
added to `web_auth.POLLING_PATHS` so polling never refreshes the session's
idle clock.

A pure helper `build_window(row, signals, availability, now) -> dict` in a
new module `web_interface/subsystem_window.py` turns one board's summary row
plus its signals into the template's view model, so it is unit-testable
without HTTP:

```python
{
  "alive": bool,
  "inputs":   [SignalView, ...],   # declared inputs, declaration order
  "outputs":  [SignalView, ...],
  "controls": {"actuators": [CommandView, ...], "standard": ["ping", ...]},
}
SignalView = {
  "id": str, "label": str,            # label = description or id with underscores spaced
  "kind": str, "unit": str,
  "digital": bool,                    # kind == "binary"
  "state": "on" | "off" | "none",     # none: never reported, or board not alive
  "value": float | None, "text": str | None,
  "updated_clock": "14:02:37" | None, "age_seconds": float | None,
  "dwell_seconds": float | None, "transition_clock": str | None,
  "dwell_since_start": bool,          # transitions_seen == 0
  "inhibited": bool,
  "in_range": bool | None,            # temperature only, from temp min/max
}
CommandView = {"name": str, "inhibited": bool}
```

`state` is `"none"` whenever the board is not alive (stale or never seen),
even if a stale reading exists — a dead board's last state is not shown as
live. Clock strings use the machine's local time, `HH:MM:SS`.

### 4.7 Template (`templates/health_subsystem.html`, `templates/partials/subsystem_live.html`)

Sections in order, each with the existing uppercase gray heading style:
**Inputs**, **Outputs**, **Controls**. A section with nothing declared shows
"Nothing declared by this board." Each digital signal is one row:

- a shape holding the label: inputs `rounded-full`, outputs `rounded-md`;
  fill `bg-green-600 text-white` (on), `bg-red-600 text-white` (off),
  `bg-gray-200 text-gray-500` (none); inhibited adds
  `ring-2 ring-offset-2 ring-dashed ring-amber-500` and an "inhibited" word
  after the row's text;
- text: `on for 12 s` / `off for 3 min` (or `off since VMC start`), then the
  last-transition clock; `—` when `state == "none"`.

Each analog signal is one row: label, `value unit`, an `OK`/`Out of range`
word for temperatures, then `14:02:37 · 3 s ago`. Controls: actuator commands
as `rounded-md` chips (`bg-slate-800 text-white`, dashed amber ring when
inhibited), standard commands as one muted line. Ages and dwell use
`humanize_seconds`.

`ring-dashed` is not a Tailwind utility; it is a two-line `@layer components`
class in `tailwind.input.css` (`outline: 2px dashed theme(colors.amber.500); outline-offset: 2px`)
named `.ring-dashed` and safelisted like `touch-target`. `app.css` is rebuilt
and committed.

### 4.8 Docs

CLAUDE.md: the Health level paragraph describes the window; the fragment
list gains `/health/subsystems/{name}/live` (2 s); the fault-code section
notes the two contract bumps. Both CONTRACT.md files updated (4.1).

## 5. Testing

| Area | Test | Proves |
|---|---|---|
| contract | `tests/test_contracts.py`, `tests/test_contract_schemas.py` | new fields default correctly, `driven_by` accepted, schema files regenerated, `subsystem_capabilities` schema present for vending |
| simulators | `tests/test_simulator_{vending,mdb,ice_maker}.py` | each `build_capabilities().channels` lists exactly the table in 4.2 with the right direction/driven_by; every id a sim publishes under is declared |
| health monitor | `tests/test_health_monitor.py` | `record_signal` stores value/text/timestamps; a binary change updates transition and count, an unchanged value does not; first reading has `transitions_seen == 0`; `record_temperature` attributes to the declaring board and to no board when undeclared; `get_summary()["signals"]` and row `channels` shapes |
| availability | `tests/test_availability.py` | `command_inhibited` for the four rows of 4.5 |
| view model | `tests/test_subsystem_window.py` (new) | `build_window`: declaration order kept; digital vs analog split; `state == "none"` when board not alive even with a reading; inhibited only via `driven_by`; dwell "since start" flag; clock formatting; empty sections |
| routes | `tests/test_routes_health.py` | an ice-maker temperature never appears on the vending page and vice versa; MDB page shows three devices with readiness text; the page carries exactly one `hx-trigger` besides the pill and the `#live` wrapper self-targets; `/live` is 200 for the three names, 404 otherwise, renders no `hx-` attribute, and does not touch `last_active_at` (POLLING_PATHS); gray/dashed classes appear for stale / inhibited cases |
| CSS | `tests/test_static_css.py` | existing coverage test picks up the new classes after rebuild; `ring-dashed` present |
| VMC | `tests/test_vmc_*.py` (existing IO/payment/event handler tests) | each handler calls the health monitor's `record_signal` with the mapped board and value |

## 6. Out of scope

- Actuating anything from the window (Tests level owns that).
- Persisting dwell/transition history across VMC restarts.
- Analog channel alarm thresholds other than the existing global temperature range.
- Real ESP32 firmware; only the simulators declare channels here. Real
  firmware that declares nothing simply shows an empty Monitors section.
- `/screen`, `/health/subsystems` list page, Home tiles.
