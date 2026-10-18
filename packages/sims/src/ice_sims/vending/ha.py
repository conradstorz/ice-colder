# ice_sims/vending/ha.py
"""Home Assistant discovery entities for the vending board."""

from ice_contracts.board.common import ChannelDescriptor


def ha_discovery_entities(channels: list[ChannelDescriptor]) -> list[dict]:
    """Return HA discovery definitions for vending machine hardware.

    Today's entities are hard-coded; `channels` is accepted so a later
    task can derive them from the channel table."""
    entities = []

    # Binary sensors for hardware I/O
    binary_devices = [
        ("auger_motor", "Auger Motor", "running"),
        ("agitator_motor", "Agitator Motor", "running"),
        ("fan", "Fan", "running"),
        ("bag_full_sensor", "Bag Full Sensor", "occupancy"),
        ("bag_drop_solenoid", "Bag Drop Solenoid", "running"),
        ("water_valve_solenoid", "Water Valve Solenoid", "running"),
        ("water_flow_sensor", "Water Flow Sensor", "running"),
        ("bin_half_full", "Ice Bin Half Full", "occupancy"),
        ("heater_relay", "Cabinet Heater", "running"),
    ]
    for device_id, display_name, device_class in binary_devices:
        entities.append(
            {
                "component": "binary_sensor",
                "object_id": device_id,
                "name": f"Vending {display_name}",
                "state_topic_suffix": f"hardware/io/{device_id}",
                "value_template": "{{ 'ON' if value_json.state else 'OFF' }}",
                "device_class": device_class,
                "payload_on": "ON",
                "payload_off": "OFF",
            }
        )

    # Cabinet temperature sensor
    entities.append(
        {
            "component": "sensor",
            "object_id": "cabinet_temp",
            "name": "Vending Cabinet Temperature",
            "state_topic_suffix": "sensors/temp/cabinet",
            "value_template": "{{ value_json.value }}",
            "device_class": "temperature",
            "unit_of_measurement": "°C",
            "state_class": "measurement",
            "expire_after": 30,
        }
    )

    # Water flow total sensor
    entities.append(
        {
            "component": "sensor",
            "object_id": "water_flow_total",
            "name": "Vending Water Flow Total",
            "state_topic_suffix": "sensors/water_flow",
            "value_template": "{{ value_json.value }}",
            "device_class": "water",
            "unit_of_measurement": "gal",
            "state_class": "total_increasing",
        }
    )

    # Uptime
    entities.append(
        {
            "component": "sensor",
            "object_id": "uptime",
            "name": "Vending Machine Uptime",
            "state_topic_suffix": "heartbeat/vending",
            "value_template": "{{ value_json.uptime_seconds }}",
            "device_class": "duration",
            "unit_of_measurement": "s",
            "state_class": "total_increasing",
        }
    )

    return entities
