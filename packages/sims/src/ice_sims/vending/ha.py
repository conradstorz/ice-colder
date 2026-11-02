# ice_sims/vending/ha.py
"""Home Assistant discovery entities for the vending board."""

from ice_contracts.board.common import ChannelDescriptor
from ice_sims.base import temperature_entity, uptime_entity
from ice_sims.vending.channels import BUTTON_PREFIX

# HA device classes for the channels where "running" / "occupancy" would
# mislead; every other output is "running" and every other input "occupancy".
_DEVICE_CLASSES = {
    "sump_high": "moisture",
    "service_door": "door",
}


def _is_button(channel: ChannelDescriptor) -> bool:
    # Customer buttons are the binary inputs the board names `button_*`.
    return channel.channel_id.startswith(BUTTON_PREFIX)


def _title(channel_id: str) -> str:
    return channel_id.replace("_", " ").title()


def ha_discovery_entities(channels: list[ChannelDescriptor]) -> list[dict]:
    """Return HA discovery definitions derived from the channel list.

    One binary sensor per binary channel that reports on `hardware/io/<id>`
    (customer buttons are excluded: they arrive as `ButtonPress` events and
    never publish a state), one temperature sensor per temperature channel
    (`sensors/temp/<id>`), plus the board's uptime."""
    entities: list[dict] = []

    for channel in channels:
        if channel.kind == "binary" and not _is_button(channel):
            default = "running" if channel.direction == "output" else "occupancy"
            entities.append(
                {
                    "component": "binary_sensor",
                    "object_id": channel.channel_id,
                    "name": f"Vending {_title(channel.channel_id)}",
                    "state_topic_suffix": f"hardware/io/{channel.channel_id}",
                    "value_template": "{{ 'ON' if value_json.state else 'OFF' }}",
                    "device_class": _DEVICE_CLASSES.get(channel.channel_id, default),
                    "payload_on": "ON",
                    "payload_off": "OFF",
                }
            )
        elif channel.kind == "temperature":
            entities.append(
                temperature_entity(
                    channel.channel_id,
                    f"Vending {_title(channel.channel_id)} Temperature",
                )
            )

    entities.append(uptime_entity("vending", "Vending Machine Uptime"))

    return entities
