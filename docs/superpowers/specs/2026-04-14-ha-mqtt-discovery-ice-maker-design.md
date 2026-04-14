# Home Assistant MQTT Auto-Discovery for Ice Maker Simulator

## Goal

Make the ice maker simulator's temperature sensors and compressor state automatically appear as entities in Home Assistant via MQTT auto-discovery, grouped under a single HA device.

## Architecture

Each ESP32 simulator owns its own HA discovery. The base class (`ESP32Simulator`) provides the discovery publishing mechanism; subclasses declare their entities by overriding a hook method.

On every MQTT connect (including reconnects), the base class:
1. Calls `ha_discovery_entities()` on the subclass to get a list of entity definitions
2. Builds the HA discovery config JSON for each entity, including a shared `device` block
3. Publishes each config as a **retained** message to `homeassistant/{component}/{node_id}/{object_id}/config`

The `device` block groups all entities under one device in HA. It uses:
- `identifiers`: `["{machine_id}_{subsystem_name}"]` (e.g., `["vmc-0001_ice_maker"]`)
- `name`: `"{config.common_name} {subsystem_display_name}"` (e.g., `"Cold as ICE VMC Ice Maker"`)
- `manufacturer`: `"ice-colder"`
- `model`: `"ESP32 {subsystem_name} simulator"`
- `via_device`: `"{machine_id}"` (links subsystem devices to the parent VMC device)

### Where code changes live

- **`simulators/base.py`** — Add `ha_discovery_entities()` hook (returns empty list by default), `_build_ha_device()` helper, and `_publish_ha_discovery()` method called during the connect phase in `run()`.
- **`simulators/ice_maker.py`** — Override `ha_discovery_entities()` to return 11 entity definitions.
- **`tests/test_simulator_base.py`** — Test discovery publishing mechanics.
- **`tests/test_simulator_ice_maker.py`** — Test that ice maker returns correct entity definitions.

No changes to `services/mqtt_messages.py`, `main.py`, or other simulators.

## Entity Definitions

### Discovery Topic Format

```
homeassistant/{component}/{node_id}/{object_id}/config
```

Where:
- `component` = `sensor` or `binary_sensor`
- `node_id` = `{machine_id}_{subsystem_name}` (e.g., `vmc-0001_ice_maker`)
- `object_id` = entity-specific ID (e.g., `water_inlet_temp`)

### HA Unique ID Format

`{machine_id}_{subsystem_name}_{object_id}` (e.g., `vmc-0001_ice_maker_water_inlet_temp`)

This ensures uniqueness across multiple machines and subsystems.

### 9 Temperature Sensors

All share: `component: "sensor"`, `device_class: "temperature"`, `unit_of_measurement: "°C"`, `state_class: "measurement"`.

| object_id | name | state_topic (relative to `vmc/{machine_id}/`) | value_template |
|---|---|---|---|
| `water_inlet_temp` | Ice Maker Water Inlet Temperature | `sensors/temp/water_inlet` | `{{ value_json.value }}` |
| `water_bath_temp` | Ice Maker Water Bath Temperature | `sensors/temp/water_bath` | `{{ value_json.value }}` |
| `compressor_temp` | Ice Maker Compressor Temperature | `sensors/temp/compressor` | `{{ value_json.value }}` |
| `exhaust_air_temp` | Ice Maker Exhaust Air Temperature | `sensors/temp/exhaust_air` | `{{ value_json.value }}` |
| `ambient_air_temp` | Ice Maker Ambient Air Temperature | `sensors/temp/ambient_air` | `{{ value_json.value }}` |
| `refrigerant_high_temp` | Ice Maker Refrigerant High Temperature | `sensors/temp/refrigerant_high` | `{{ value_json.value }}` |
| `refrigerant_low_temp` | Ice Maker Refrigerant Low Temperature | `sensors/temp/refrigerant_low` | `{{ value_json.value }}` |
| `purge_water_temp` | Ice Maker Purge Water Temperature | `sensors/temp/purge_water` | `{{ value_json.value }}` |
| `hot_gas_valve_temp` | Ice Maker Hot Gas Valve Temperature | `sensors/temp/hot_gas_valve` | `{{ value_json.value }}` |

### 1 Compressor Binary Sensor

| Field | Value |
|---|---|
| component | `binary_sensor` |
| object_id | `compressor` |
| name | Ice Maker Compressor |
| device_class | `running` |
| state_topic | `vmc/{machine_id}/ice_maker/event` |
| value_template | `{{ 'ON' if value_json.event == 'power_on' else 'OFF' }}` |
| payload_on | `ON` |
| payload_off | `OFF` |

Note: This entity updates only when a power_on/power_off event is published. Between events, HA retains the last known state. The ice maker publishes these events on every compressor cycle (10 min on / 5 min off), so the state stays reasonably current.

### 1 Uptime Sensor

| Field | Value |
|---|---|
| component | `sensor` |
| object_id | `uptime` |
| name | Ice Maker Uptime |
| device_class | `duration` |
| unit_of_measurement | `s` |
| state_class | `total_increasing` |
| state_topic | `vmc/{machine_id}/heartbeat/ice_maker` |
| value_template | `{{ value_json.uptime_seconds }}` |

## Entity Definition Data Structure

Each entry returned by `ha_discovery_entities()` is a dict:

```python
{
    "component": "sensor",           # HA platform
    "object_id": "water_inlet_temp", # unique suffix
    "name": "Ice Maker Water Inlet Temperature",
    "state_topic_suffix": "sensors/temp/water_inlet",  # relative to topic_prefix
    "value_template": "{{ value_json.value }}",
    # Optional fields (included only when set):
    "device_class": "temperature",
    "unit_of_measurement": "°C",
    "state_class": "measurement",
    "payload_on": "ON",      # binary_sensor only
    "payload_off": "OFF",    # binary_sensor only
}
```

The base class reads these dicts and builds the full HA config JSON, adding `unique_id`, `device`, `availability`, and the full `state_topic` (prepending `vmc/{machine_id}/`).

## Availability

Each discovery config includes an `availability` block so HA marks entities as unavailable when the simulator disconnects:

```json
{
    "availability_topic": "vmc/{machine_id}/heartbeat/ice_maker",
    "payload_available": "",
    "availability_mode": "latest"
}
```

Since the heartbeat publishes every 10 seconds, HA's default `expire_after` behavior handles staleness. We set `expire_after: 30` on sensor entities (3 missed heartbeats = unavailable).

## Testing

- **Base class tests**: Mock MQTT client, call `_publish_ha_discovery()`, verify it publishes to correct `homeassistant/...` topics with `retain=True`, verify `device` block structure, verify `unique_id` format.
- **Ice maker tests**: Call `ha_discovery_entities()`, verify 11 entities returned, verify each has required fields, verify `state_topic_suffix` matches what `run_simulation()` actually publishes to.
- **Default behavior test**: Base class `ha_discovery_entities()` returns empty list; `ConcreteSimulator` (test subclass) publishes no discovery.

## Out of Scope

- Event entities (ice_dropped, temp_out_of_bounds) — future work
- Discovery for other simulators (MDB, vending) — same pattern, separate tasks
- HA `device_automation` or `device_trigger` — future work
- MQTT birth/will messages — could enhance availability but not needed for v1
