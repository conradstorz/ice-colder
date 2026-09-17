# Ice Maker Monitor Contract — Design

**Date:** 2026-09-13
**Status:** Approved approach: extend the existing subsystem contract (Approach A).

## Goal

Define the shared, versioned interface between **ice-colder** (the VMC) and the
separate, already-underway project that monitors a specific brand of ice maker
from its own Raspberry Pi. The contract is the deliverable: a spec document
plus machine-readable JSON Schemas that both projects pin to. This repo remains
the source of truth; the monitor project conforms without importing our code.

## Decisions (from brainstorming)

1. **Artifact:** versioned markdown spec + JSON Schemas generated from
   Pydantic models in this repo.
2. **Signals:** rich — temperatures, analog channels (e.g., compressor
   current, water level), and binary/state channels (solenoids, status LEDs).
3. **Direction:** telemetry plus three commands (`power_cycle`,
   `force_report`, `set_interval`), each acknowledged.
4. **Channel declaration:** self-describing — the monitor announces its
   channel set in a retained capabilities message; adding a probe never
   requires a contract change.
5. **Compatibility:** today's live topics (`sensors/temp/+`,
   `ice_maker/event`, `heartbeat/+`) keep their exact current shape.

## Non-Goals

- Implementing the brand-specific monitor itself (other project).
- Changing the vending-machine or MDB subsystem contracts.
- Broker TLS/credential provisioning (deployment concern; the contract only
  states that production brokers require authentication).
- Home Assistant discovery semantics (the monitor MAY also publish HA
  discovery like our simulators do; the contract doesn't require it).

## Deliverable 1: the contract package (`docs/contracts/ice-maker-monitor/`)

```
docs/contracts/ice-maker-monitor/
  CONTRACT.md          # human-readable spec, version 1.0.0
  schemas/             # generated JSON Schemas, one file per message
    sensor_reading.schema.json
    ice_maker_event.schema.json
    subsystem_heartbeat.schema.json
    channel_descriptor.schema.json
    monitor_capabilities.schema.json
    channel_reading.schema.json
    monitor_command.schema.json
    command_ack.schema.json
```

`CONTRACT.md` sections: identity & versioning, transport rules, topic map,
message schemas (tables + links to the JSON files), cadence & liveness,
command semantics, conformance checklist for the monitor project.

### Versioning

- Semver, starting **1.0.0**, carried in `MonitorCapabilities.contract_version`.
- **Minor** bump: additive (new optional field, new event string, new channel
  `kind`). **Major** bump: renamed/removed field or topic, changed semantics.
- The VMC accepts any `1.x`; it logs a warning on unknown fields/events
  rather than rejecting (Pydantic `extra="ignore"` on the consumer side).

### Transport rules

- Broker: MQTT 3.1.1+, authenticated in production.
- All topics prefixed `{machine_id}/` — the monitor is configured with the
  same `machine_id` as the VMC it serves.
- QoS 1 for events, commands, acks, and capabilities; QoS 0 acceptable for
  high-rate sensor/telemetry readings.
- `capabilities/ice_maker` is published **retained**; everything else is not.
- **Last Will:** the monitor sets an MQTT LWT publishing
  `{"subsystem": "ice_maker", "uptime_seconds": -1}` to
  `{machine_id}/heartbeat/ice_maker` so an unclean disconnect is visible
  immediately (uptime `-1` = offline marker; consumers treat it as
  "monitor lost").
- All timestamps ISO-8601 UTC, field name `timestamp`, producer-side clock.

### Topic map (all under `{machine_id}/`)

| Topic | Dir | Payload schema | Notes |
|---|---|---|---|
| `sensors/temp/<location>` | mon → VMC | `SensorReading` | unchanged from today |
| `ice_maker/event` | mon → VMC | `IceMakerEvent` | unchanged; event strings below |
| `heartbeat/ice_maker` | mon → VMC | `SubsystemHeartbeat` | every 10 s; also the LWT target |
| `capabilities/ice_maker` | mon → VMC | `MonitorCapabilities` | retained; on connect and on any channel change |
| `telemetry/ice_maker/<channel_id>` | mon → VMC | `ChannelReading` | analog/binary channels declared in capabilities |
| `cmd/ice_maker` | VMC → mon | `MonitorCommand` | the three commands |
| `cmd/ice_maker/ack` | mon → VMC | `CommandAck` | one per command, matching `request_id` |

### Message schemas (new Pydantic models; existing three unchanged)

`ChannelDescriptor`:
- `channel_id: str` (slug, `^[a-z0-9_]{1,64}$`; becomes the topic segment)
- `kind: Literal["temperature", "current", "voltage", "level", "binary", "counter"]`
- `unit: str` (e.g., `"C"`, `"A"`, `"%"`, `""` for binary)
- `description: str`
- `interval_seconds: float` (declared publish cadence, `gt=0, le=3600`)

`MonitorCapabilities`:
- `subsystem: Literal["ice_maker"]`
- `contract_version: str` (semver)
- `brand: str`, `model: str`, `firmware: str` (monitor project's own version)
- `channels: list[ChannelDescriptor]`
- `commands: list[str]` (which of the contract commands this monitor supports)
- `timestamp`

`ChannelReading`:
- `channel_id: str` (must match a declared channel)
- `value: float` (binary channels use 0.0/1.0)
- `timestamp`

`MonitorCommand`:
- `request_id: str` (UUID4 string, correlation key)
- `command: Literal["power_cycle", "force_report", "set_interval"]`
- `params: dict[str, float]` — per command:
  - `power_cycle`: `dwell_seconds` (`ge=5, le=300`)
  - `force_report`: none
  - `set_interval`: `interval_seconds` (`ge=1, le=3600`) — applies to sensor
    and telemetry cadence, not heartbeats
- `timestamp`

`CommandAck`:
- `request_id: str` (echoed)
- `command: str`
- `status: Literal["ok", "rejected", "failed", "unsupported"]`
- `detail: str | None`
- `timestamp`

`IceMakerEvent.event` registry (v1.0.0): `power_on`, `power_off`,
`ice_dropped`, `needs_cleaning`, `failed_cycle`, `temp_out_of_bounds`,
`halt`, `resume` — plus `power_cycled` (emitted after a commanded
power-cycle completes). Unknown event strings must be tolerated (logged,
not fatal) by consumers.

### Cadence & liveness

- Heartbeat every 10 s; consumer marks the monitor stale after 120 s
  (matches `HealthMonitor` defaults) and offline immediately on the LWT.
- Each channel publishes at its declared `interval_seconds`.
- Capabilities re-published (retained) on every connect.

### Command semantics

- Commands are idempotent per `request_id`; a monitor receiving a duplicate
  `request_id` re-sends its previous ack without re-executing.
- Ack deadline 10 s; no ack → VMC treats the command as lost (it may retry
  with the same `request_id`).
- `power_cycle` safety: minimum dwell 5 s (schema-enforced) and a monitor-side
  lockout — a second `power_cycle` within 5 minutes of the last is answered
  `rejected` with detail `"lockout"` and not executed.
- A monitor that doesn't implement a command (per its `commands` list)
  answers `unsupported`.

## Deliverable 2: repo changes backing the contract

1. **`contracts/` package** — `contracts/ice_maker_monitor.py` with the five
   new Pydantic models (existing three stay in `services/mqtt_messages.py`
   and are re-exported/referenced, not duplicated); `contracts/generate.py`
   writes every schema to `docs/contracts/ice-maker-monitor/schemas/` via
   `model_json_schema()`. Constant `CONTRACT_VERSION = "1.0.0"`.
2. **Schema drift test** — regenerates schemas in-memory and asserts equality
   with the committed files, so model edits without regeneration fail CI.
3. **Simulator as reference implementation** — `simulators/ice_maker.py`
   gains: retained capabilities announce (its 10 temp channels plus two
   demo telemetry channels, `compressor_current` and `bin_level`), telemetry
   publishing, and handlers for the three commands with acks (power_cycle
   restarts its compressor cycle after the dwell; set_interval changes
   `PUBLISH_INTERVAL`; force_report publishes a full snapshot immediately).
   This makes the simulator a live conformance fixture the other project can
   run against.
4. **VMC additions (minimal)** — subscribe `capabilities/+` and
   `telemetry/ice_maker/+`: store capabilities for the dashboard, feed
   binary/analog channel readings into `HealthMonitor` generically (no
   per-channel code), and log acks. No dashboard UI work in this scope
   beyond what the existing health fragment already renders from
   `get_summary()`.

## Testing

- Model bounds tests (dwell/interval limits, channel_id pattern, ack statuses).
- Schema generation round-trip + drift test.
- Simulator conformance: capabilities retained flag, command → ack
  correlation, lockout rejection, duplicate `request_id` idempotency,
  LWT configured on connect.
- VMC: capabilities stored, telemetry routed to health monitor, unknown
  event string tolerated.

## Conformance checklist (goes in CONTRACT.md for the monitor project)

Publish retained capabilities on connect · heartbeat every 10 s with LWT ·
declared cadence per channel · schema-valid payloads (validate against the
JSON files) · ack every command within 10 s · power-cycle lockout ·
tolerate unknown fields in commands.
