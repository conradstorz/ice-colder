# Ice Maker Monitor Contract — v1.0.0

This document, together with the JSON Schema files in `schemas/`, defines the
interface between the ice-colder VMC (this repo) and the separate,
brand-specific ice-maker monitor project. The monitor runs on its own
Raspberry Pi and conforms to this contract without importing any code from
this repo. The `schemas/*.schema.json` files are generated directly from
this repo's Pydantic models (`contracts/ice_maker_monitor.py` and
`services/mqtt_messages.py`) via `model_json_schema()`; they are the
normative payload definitions. Where prose in this document and a schema
file appear to disagree, treat that as a bug against this document — the
schema files are regenerated from source and are always current.

## Identity & versioning

- The contract is versioned with [semver](https://semver.org/), starting at
  **1.0.0**.
- The monitor reports the contract version it implements in
  `MonitorCapabilities.contract_version`.
- **Minor** version bump: additive, backward-compatible change (new optional
  field, new `IceMakerEvent.event` string, new `ChannelDescriptor.kind`).
- **Major** version bump: breaking change (renamed or removed field, renamed
  or removed topic, changed semantics of an existing field).
- The VMC accepts any `1.x` monitor. It logs a warning on unknown fields or
  unknown event strings rather than rejecting the message (consumer-side
  Pydantic models use `extra="ignore"`).

## Transport rules

- Broker: MQTT 3.1.1 or later. Production brokers require authentication;
  credential/TLS provisioning is a deployment concern outside this contract.
- Every topic is prefixed `vmc/{machine_id}/` — the monitor MUST be
  configured with the same `machine_id` as the VMC instance it serves.
- QoS 1 for events, commands, acks, and capabilities. QoS 0 is acceptable
  for high-rate sensor and telemetry readings.
- `vmc/{machine_id}/capabilities/ice_maker` is published **retained**.
  Every other topic in this contract is published **not retained**.
- **Last Will and Testament:** on connect, the monitor MUST configure an
  MQTT LWT that publishes the payload `{"subsystem": "ice_maker",
  "uptime_seconds": -1}` to `vmc/{machine_id}/heartbeat/ice_maker` if it
  disconnects uncleanly. `uptime_seconds: -1` is the reserved "offline"
  marker; consumers receiving it treat the monitor as lost immediately,
  without waiting for the staleness timeout below.
- All timestamps are ISO-8601 UTC strings in a field named `timestamp`,
  set by the producer's own clock at the moment of publish.

## Topic map

All topics below are relative to the `vmc/{machine_id}/` prefix.

| Topic | Direction | Payload schema | Notes |
|---|---|---|---|
| `sensors/temp/<location>` | monitor → VMC | [`SensorReading`](schemas/sensor_reading.schema.json) | unchanged from the pre-contract shape |
| `ice_maker/event` | monitor → VMC | [`IceMakerEvent`](schemas/ice_maker_event.schema.json) | unchanged; event registry below |
| `heartbeat/ice_maker` | monitor → VMC | [`SubsystemHeartbeat`](schemas/subsystem_heartbeat.schema.json) | every 10 s; also the LWT target |
| `capabilities/ice_maker` | monitor → VMC | [`MonitorCapabilities`](schemas/monitor_capabilities.schema.json) | retained; published on connect and on any channel change |
| `telemetry/ice_maker/<channel_id>` | monitor → VMC | [`ChannelReading`](schemas/channel_reading.schema.json) | one topic per channel declared in capabilities |
| `cmd/ice_maker` | VMC → monitor | [`MonitorCommand`](schemas/monitor_command.schema.json) | the three contract commands |
| `cmd/ice_maker/ack` | monitor → VMC | [`CommandAck`](schemas/command_ack.schema.json) | exactly one per command, correlated by `request_id` |

## Message schemas

Field tables below match the generated schema files exactly. Each schema
file is the normative source; the tables are a human-readable rendering of
it.

### SensorReading

Schema: [`schemas/sensor_reading.schema.json`](schemas/sensor_reading.schema.json)

Temperature or other sensor data, published on `sensors/temp/<location>`.

| Field | Type | Constraints | Description |
|---|---|---|---|
| `location` | string | required | Sensor location identifier (e.g., `evaporator`, `bin_top`) |
| `value` | number | required | Sensor reading value |
| `unit` | string | default `"C"` | Unit of measurement |
| `timestamp` | string (date-time) | ISO-8601 UTC | Producer-side timestamp |

### IceMakerEvent

Schema: [`schemas/ice_maker_event.schema.json`](schemas/ice_maker_event.schema.json)

Operational event from the ice maker, published on `ice_maker/event`.

| Field | Type | Constraints | Description |
|---|---|---|---|
| `event` | string | required | Event type — see registry below |
| `detail` | string \| null | default `null` | Additional detail (e.g., sensor name, cycle count) |
| `timestamp` | string (date-time) | ISO-8601 UTC | Producer-side timestamp |

**`event` registry (v1.0.0):** `power_on`, `power_off`, `ice_dropped`,
`needs_cleaning`, `failed_cycle`, `temp_out_of_bounds`, `halt`, `resume`,
and `power_cycled` (emitted after a commanded power-cycle completes).
Consumers MUST tolerate unknown event strings — log them, do not treat
them as fatal — since a minor version bump may add new event strings.

### SubsystemHeartbeat

Schema: [`schemas/subsystem_heartbeat.schema.json`](schemas/subsystem_heartbeat.schema.json)

Periodic liveness signal, published on `heartbeat/ice_maker` (also the LWT
target — see Transport rules).

| Field | Type | Constraints | Description |
|---|---|---|---|
| `subsystem` | string | required | Subsystem identifier (`"ice_maker"`) |
| `uptime_seconds` | integer | default `0`; `-1` is the reserved offline marker | Seconds since last boot |
| `timestamp` | string (date-time) | ISO-8601 UTC | Producer-side timestamp |

### ChannelDescriptor

Schema: [`schemas/channel_descriptor.schema.json`](schemas/channel_descriptor.schema.json)

One telemetry channel the monitor declares, nested inside
`MonitorCapabilities.channels`.

| Field | Type | Constraints | Description |
|---|---|---|---|
| `channel_id` | string | required; pattern `^[a-z0-9_]{1,64}$` | Slug; also the topic segment under `telemetry/ice_maker/<channel_id>` |
| `kind` | enum | required; one of `temperature`, `current`, `voltage`, `level`, `binary`, `counter` | Channel category |
| `unit` | string | default `""` | Unit, e.g. `"C"`, `"A"`, `"%"`; empty string for binary channels |
| `description` | string | default `""` | Human-readable channel description |
| `interval_seconds` | number | required; `> 0`, `<= 3600` | Declared publish cadence for this channel |

### MonitorCapabilities

Schema: [`schemas/monitor_capabilities.schema.json`](schemas/monitor_capabilities.schema.json)

Retained self-description, published on `capabilities/ice_maker`.

| Field | Type | Constraints | Description |
|---|---|---|---|
| `subsystem` | const string | default/const `"ice_maker"` | Fixed subsystem identifier |
| `contract_version` | string | required | Contract semver this monitor implements, e.g. `"1.0.0"` |
| `brand` | string | required | Ice maker brand the monitor targets |
| `model` | string | required | Ice maker model |
| `firmware` | string | required | Monitor project's own software version |
| `channels` | array of `ChannelDescriptor` | default `[]` | Declared telemetry channels |
| `commands` | array of string | default `[]` | Which of the contract commands (`power_cycle`, `force_report`, `set_interval`) this monitor supports |
| `timestamp` | string (date-time) | ISO-8601 UTC | Producer-side timestamp |

### ChannelReading

Schema: [`schemas/channel_reading.schema.json`](schemas/channel_reading.schema.json)

One reading on `telemetry/ice_maker/<channel_id>`.

| Field | Type | Constraints | Description |
|---|---|---|---|
| `channel_id` | string | required; pattern `^[a-z0-9_]{1,64}$`; must match a channel declared in `MonitorCapabilities` | Channel identifier |
| `value` | number | required | Reading value; binary channels use `0.0`/`1.0` |
| `timestamp` | string (date-time) | ISO-8601 UTC | Producer-side timestamp |

### MonitorCommand

Schema: [`schemas/monitor_command.schema.json`](schemas/monitor_command.schema.json)

VMC → monitor command, published on `cmd/ice_maker`.

| Field | Type | Constraints | Description |
|---|---|---|---|
| `request_id` | string | required; length 8–64 | UUID4 string; correlation key echoed in the matching `CommandAck` |
| `command` | enum | required; one of `power_cycle`, `force_report`, `set_interval` | Command to execute |
| `params` | object (string → number) | default `{}` | Per-command parameters — see below |
| `timestamp` | string (date-time) | ISO-8601 UTC | Producer-side timestamp |

The JSON Schema for `params` only constrains it to a `string -> number`
map — it cannot express per-command bounds. **The following bounds are
enforced by validators in this repo, not by the JSON Schema file, and this
table plus this prose are the normative source for them:**

| Command | `params` key | Bounds | Notes |
|---|---|---|---|
| `power_cycle` | `dwell_seconds` | `>= 5`, `<= 300` | How long to hold power off before restoring it |
| `force_report` | *(none)* | — | Requests an immediate full snapshot; `params` is empty |
| `set_interval` | `interval_seconds` | `>= 1`, `<= 3600` | Applies to sensor and telemetry publish cadence, not to the 10 s heartbeat |

### CommandAck

Schema: [`schemas/command_ack.schema.json`](schemas/command_ack.schema.json)

Monitor → VMC acknowledgement, published on `cmd/ice_maker/ack`.

| Field | Type | Constraints | Description |
|---|---|---|---|
| `request_id` | string | required | Echoed from the originating `MonitorCommand` |
| `command` | string | required | Echoed from the originating `MonitorCommand` |
| `status` | enum | required; one of `ok`, `rejected`, `failed`, `unsupported` | Outcome of the command |
| `detail` | string \| null | default `null` | Human-readable detail, e.g. `"lockout"` |
| `timestamp` | string (date-time) | ISO-8601 UTC | Producer-side timestamp |

## Cadence & liveness

- The monitor publishes `SubsystemHeartbeat` on `heartbeat/ice_maker` every
  **10 seconds**.
- A consumer marks the monitor **stale** after **120 seconds** without a
  heartbeat (matches this repo's `HealthMonitor` defaults), and marks it
  **offline immediately** on receipt of the LWT payload
  (`uptime_seconds: -1`) — it does not wait for the staleness timeout in
  that case.
- Each declared channel publishes `ChannelReading` at its own
  `ChannelDescriptor.interval_seconds`; different channels may use
  different cadences.
- `MonitorCapabilities` (retained) is re-published on every broker
  connection, and again whenever the monitor's channel set changes.

## Command semantics

- Commands are **idempotent per `request_id`**. A monitor that receives a
  `MonitorCommand` whose `request_id` it has already processed MUST re-send
  its previously computed `CommandAck` rather than re-executing the
  command.
- **Ack deadline: 10 seconds.** If the VMC does not receive a `CommandAck`
  within 10 seconds of publishing a `MonitorCommand`, it treats the command
  as lost and may retry it using the same `request_id` (which the
  idempotency rule above makes safe).
- **`power_cycle` safety:**
  - Minimum dwell is 5 seconds and maximum is 300 seconds
    (`dwell_seconds`, `>= 5, <= 300` — see Message schemas above; this
    bound is validator-enforced, not present in the JSON Schema file).
  - The monitor MUST enforce a **300-second (5-minute) lockout**: a second
    `power_cycle` command received within 300 seconds of the last one it
    executed is answered with `status: "rejected"` and `detail: "lockout"`,
    and MUST NOT be executed.
- A monitor that does not implement a given command (i.e., the command is
  absent from its own `MonitorCapabilities.commands` list) answers with
  `status: "unsupported"`.
- Consumers MUST tolerate unknown fields on incoming commands (forward
  compatibility with minor version bumps).

## Reference implementation

`simulators/ice_maker.py` in this repo speaks the full contract described
above — retained capabilities announcement, declared-cadence telemetry
publishing, and handlers with acks for all three commands (`power_cycle`,
`force_report`, `set_interval`), including the power-cycle lockout and
duplicate-`request_id` idempotency. It is a live conformance fixture: run
it against an MQTT broker and observe or drive it exactly as a real
brand-specific monitor would be observed or driven, to validate a monitor
implementation or a VMC-side consumer against this contract.

## Conformance checklist

A monitor implementation is conformant with contract v1.x when it:

- [ ] Publishes retained `MonitorCapabilities` to `capabilities/ice_maker`
      on every broker connection, and again on any channel change.
- [ ] Publishes `SubsystemHeartbeat` to `heartbeat/ice_maker` every 10 s.
- [ ] Configures an MQTT LWT on `heartbeat/ice_maker` with payload
      `{"subsystem": "ice_maker", "uptime_seconds": -1}`.
- [ ] Publishes each declared channel's `ChannelReading` at that channel's
      own declared `interval_seconds`.
- [ ] Publishes schema-valid payloads for every topic — validate against
      the JSON Schema files in `schemas/`.
- [ ] Acknowledges every received `MonitorCommand` with a `CommandAck`
      within 10 s.
- [ ] Enforces the `power_cycle` 300-second lockout, rejecting a second
      `power_cycle` within that window with `status: "rejected"`,
      `detail: "lockout"`.
- [ ] Re-sends the previous ack (without re-executing) for a duplicate
      `request_id`.
- [ ] Answers `status: "unsupported"` for any command not listed in its own
      `MonitorCapabilities.commands`.
- [ ] Tolerates unknown fields in incoming `MonitorCommand` messages
      without failing.
