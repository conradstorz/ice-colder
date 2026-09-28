# Ice Maker Monitor Contract — v1.2.0

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
- **1.1.0** (2026-09-18): `MonitorCapabilities` gains optional `hardware_id`
  and `ip`; the model is now the shared `SubsystemCapabilities` with
  `subsystem` fixed to `ice_maker`.
- **1.2.0** (2026-09-25): The `MonitorCommand` and `CommandAck` models move
  to `contracts/common.py` as `SubsystemCommand` and `CommandAck` and are
  re-exported here under their original names (same classes, wire format
  unchanged). The ack gains an optional `result` field for command-specific
  return data. This enables the shared subsystem command channel (see below).

## Transport rules

- Broker: MQTT 3.1.1 or later. Production brokers require authentication;
  credential/TLS provisioning is a deployment concern outside this contract.
- Every topic is prefixed `vmc/{machine_id}/` — the monitor MUST be
  configured with the same `machine_id` as the VMC instance it serves.
- **QoS 1** for events, commands, acks, capabilities, and heartbeats
  (including Last-Will publication). **QoS 0** is acceptable only for
  high-rate sensor and telemetry readings.
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
| `cmd/ice_maker` | VMC → monitor | [`SubsystemCommand`](schemas/monitor_command.schema.json) (aliased as `MonitorCommand`) | test and control commands with parameters; **answer deadline 10 seconds** |
| `cmd/ice_maker/ack` | monitor → VMC | [`CommandAck`](schemas/command_ack.schema.json) | exactly one per command, correlated by `request_id`; idempotency: monitor keeps the last 32 `request_id`s and replays cached acks on duplicates |

**Production topics are unchanged.** `sensors/temp/<location>`, `ice_maker/event`, `heartbeat/ice_maker`, `capabilities/ice_maker`, and `telemetry/ice_maker/<channel_id>` all keep working exactly as before, whether or not a monitor implements the command channel. `cmd/ice_maker` / `cmd/ice_maker/ack` are purely additive: a monitor that ignores the command channel keeps making ice and simply advertises no tests (`MonitorCapabilities.commands` stays empty).

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
| `hardware_id` | string \| null | default `null`; added in 1.1.0 | MAC address or serial number of the monitor board |
| `ip` | string \| null | default `null`; added in 1.1.0 | The monitor's LAN address |
| `channels` | array of `ChannelDescriptor` | default `[]` | Declared telemetry channels |
| `commands` | array of string | default `[]` | Every command this monitor supports: the three standard commands every subsystem answers (`ping`, `self_test`, `force_report`) plus its ice-maker-specific commands (`power_cycle`, `set_interval`) |
| `timestamp` | string (date-time) | ISO-8601 UTC | Producer-side timestamp |

### ChannelReading

Schema: [`schemas/channel_reading.schema.json`](schemas/channel_reading.schema.json)

One reading on `telemetry/ice_maker/<channel_id>`.

| Field | Type | Constraints | Description |
|---|---|---|---|
| `channel_id` | string | required; pattern `^[a-z0-9_]{1,64}$`; must match a channel declared in `MonitorCapabilities` | Channel identifier |
| `value` | number | required | Reading value; binary channels use `0.0`/`1.0` |
| `timestamp` | string (date-time) | ISO-8601 UTC | Producer-side timestamp |

### SubsystemCommand (formerly MonitorCommand)

Schema: [`schemas/monitor_command.schema.json`](schemas/monitor_command.schema.json)

VMC → monitor command, published on `cmd/ice_maker`. The model is shared by all subsystems (see the vending-machine contract's Command channel section); this section documents the ice-maker-specific commands and validation rules.

| Field | Type | Constraints | Description |
|---|---|---|---|
| `request_id` | string | required; length 8–64 | Opaque correlation key, unique per command; UUID4 recommended but not enforced. Echoed in the matching `CommandAck` |
| `command` | string | required | Command name; ice-maker accepts `power_cycle` and `set_interval`, plus the three standard commands every subsystem answers (`ping`, `self_test`, `force_report`) |
| `params` | object | default `{}` | Per-command parameters — see below |
| `timestamp` | string (date-time) | ISO-8601 UTC | Producer-side timestamp |

**Per-command parameter validation** (enforced at the VMC layer, not in the JSON Schema):

| Command | `params` key | Bounds | Notes |
|---|---|---|---|
| `ping` | *(none)* | — | Standard command; ack status `ok` with no result |
| `self_test` | *(none)* | — | Standard command; ack status `ok` with result `{checks: [...]}`; one check per declared telemetry channel and internal self-test |
| `force_report` | *(none)* | — | Standard command; ack status `ok` with no result; monitor republishes all sensors, channels, and heartbeat immediately |
| `power_cycle` | `dwell_seconds` | `>= 5`, `<= 300` | Power-off duration in seconds. Validation failure returns ack status `rejected` with message `"power_cycle requires dwell_seconds in [5, 300]"`. The monitor also enforces a 300-second lockout: a second `power_cycle` within 300 s of the last one is answered `rejected` with detail `"lockout"` |
| `set_interval` | `interval_seconds` | `>= 1`, `<= 3600` | Sensor and telemetry publish cadence in seconds (does not apply to the 10 s heartbeat). Validation failure returns ack status `rejected` with message `"set_interval requires interval_seconds in [1, 3600]"`. After a successful `set_interval`, the monitor MUST republish its capabilities with the new interval |

**Both validation-failure outcomes matter.** A `SubsystemCommand` whose `params` fail the bounds above is answered with status `rejected` and the validation message **only when the raw payload carries a usable `request_id`** (a non-empty string) to correlate the ack to. When the raw payload has no usable `request_id` — missing, empty, or not a string — there is nothing to correlate an ack to, so the monitor drops the command with **no ack at all**; the VMC will see this as a timeout after `ACK_TIMEOUT_SECONDS`. A firmware author who implements only the `rejected` half will find some malformed commands never get an answer.

### CommandAck

Schema: [`schemas/command_ack.schema.json`](schemas/command_ack.schema.json)

Monitor → VMC acknowledgement, published on `cmd/ice_maker/ack`.

| Field | Type | Constraints | Description |
|---|---|---|---|
| `request_id` | string | required | Echoed from the originating `SubsystemCommand` |
| `command` | string | required | Echoed from the originating `SubsystemCommand` |
| `status` | enum | required; one of `ok`, `rejected`, `failed`, `unsupported` | Outcome of the command |
| `detail` | string \| null | default `null` | Human-readable detail, e.g. `"lockout"`, `"power_cycle requires dwell_seconds in [5, 300]"` |
| `result` | object \| null | optional; default `null` | Command-specific return data (v1.2.0 addition); e.g. `self_test` returns `{checks: [...]}` |
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
  Capabilities MUST also be re-published (retained) whenever any declared
  channel property changes — including `interval_seconds` after a
  successful `set_interval` command — so the retained document always
  reflects current behavior.

## Command semantics

- **Idempotency is a contract requirement on real firmware.** A monitor that
  receives a `SubsystemCommand` whose `request_id` it has already processed
  MUST keep that `request_id` (up to 32 recent ones) and re-send the cached
  `CommandAck` rather than re-executing the command. This is essential: if a
  timeout and retry re-ran the action, two power cycles, two dispenses, or
  two motor tests would occur.
- **Ack deadline: 10 seconds.** (`ACK_TIMEOUT_SECONDS` in `contracts/common.py`.)
  If the VMC does not receive a `CommandAck` within 10 seconds of publishing
  a `SubsystemCommand`, it treats the command as lost and may retry it using
  the same `request_id` (which the idempotency rule above makes safe).
- **`power_cycle` safety:**
  - Minimum dwell is 5 seconds and maximum is 300 seconds
    (`dwell_seconds`, `>= 5, <= 300` — enforced by validators in the VMC layer).
  - The monitor MUST enforce a **300-second (5-minute) lockout**: a second
    `power_cycle` command received within 300 seconds of the last one it
    executed is answered with `status: "rejected"` and `detail: "lockout"`,
    and MUST NOT be executed.
- Unknown commands (commands not listed in `MonitorCapabilities.commands`)
  are answered with `status: "unsupported"`.
- The monitor's `commands` list (published in `MonitorCapabilities`) includes
  all commands it supports: the three standard ones (`ping`, `self_test`,
  `force_report`), the test commands (`power_cycle`), and any control commands
  (`set_interval`). The VMC maintains a server-side allowlist so a test button
  appears only for standard and test commands, ensuring control commands cannot
  be invoked through the test UI.
- Consumers MUST tolerate unknown fields on incoming commands (forward
  compatibility with minor version bumps).

## Reference implementation

The ice-colder repository's `simulators/ice_maker.py` is the reference
implementation of this contract: it announces retained capabilities, publishes
the `compressor_current` and `bin_level` telemetry channels alongside its
temperature sensors, and handles `power_cycle`, `force_report`, and
`set_interval` with acks, the 300-second power-cycle lockout, and
request-id idempotency, over an MQTT connection with a Last-Will heartbeat.
Running it against an MQTT broker provides a live conformance fixture:
observe its published traffic and drive commands at it to compare behavior.

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
- [ ] SHOULD include `hardware_id` and `ip` in `MonitorCapabilities` so the
      dashboard can match a board to a row.
- [ ] Tolerates unknown fields in incoming `MonitorCommand` messages
      without failing.
