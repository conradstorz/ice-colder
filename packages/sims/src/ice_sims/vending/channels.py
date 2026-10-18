# ice_sims/vending/channels.py
"""The vending board's one declarative channel table.

Each `ChannelRow` is a single I/O point; `CHANNELS` (the capabilities
`ChannelDescriptor` list) and `HARDWARE_DEVICES` (binary id -> idle state)
are both derived from it, so they cannot drift apart. Spec §4.2's vending
channel table, in declaration order, plus the dispenser-profile channels
(plan: dispenser profiles, Task 5) appended at the end. No MQTT, no I/O.
"""

from dataclasses import dataclass
from typing import Literal

from ice_contracts.board.common import ChannelDescriptor

SENSOR_PUBLISH_INTERVAL = 10.0  # seconds between periodic sensor publishes
_BINARY_CHANNEL_INTERVAL = 1.0

Kind = Literal["temperature", "current", "voltage", "level", "binary", "counter"]


@dataclass(frozen=True)
class ChannelRow:
    id: str
    kind: Kind
    description: str
    interval_seconds: float
    role: Literal["sensor", "output"]  # "button" arrives in Task 3
    unit: str = ""
    direction: Literal["input", "output"] = "input"
    driven_by: str | None = None
    idle: bool | None = None  # idle default; binary channels only

    def descriptor(self) -> ChannelDescriptor:
        return ChannelDescriptor(
            channel_id=self.id,
            kind=self.kind,
            unit=self.unit,
            description=self.description,
            interval_seconds=self.interval_seconds,
            direction=self.direction,
            driven_by=self.driven_by,
        )


def _binary_in(id: str, description: str, *, idle: bool = False) -> ChannelRow:
    return ChannelRow(
        id, "binary", description, _BINARY_CHANNEL_INTERVAL, "sensor", idle=idle
    )


def _binary_out(id: str, description: str, driven_by: str | None = None) -> ChannelRow:
    return ChannelRow(
        id,
        "binary",
        description,
        _BINARY_CHANNEL_INTERVAL,
        "output",
        direction="output",
        driven_by=driven_by,
        idle=False,
    )


CHANNEL_ROWS: tuple[ChannelRow, ...] = (
    ChannelRow(
        "cabinet",
        "temperature",
        "Cabinet temperature",
        SENSOR_PUBLISH_INTERVAL,
        "sensor",
        unit="C",
    ),
    ChannelRow(
        "water_flow",
        "counter",
        "Cumulative water dispensed",
        SENSOR_PUBLISH_INTERVAL,
        "sensor",
        unit="gal",
    ),
    _binary_in("bag_full_sensor", "Ice bag full sensor"),
    _binary_in("water_flow_sensor", "Water flow sensor"),
    # assume the bin starts with ice
    _binary_in("bin_half_full", "Ice bin half-full detector", idle=True),
    _binary_out("auger_motor", "Ice auger motor", "dispense"),
    _binary_out("agitator_motor", "Ice agitator motor", "dispense"),
    _binary_out("bag_drop_solenoid", "Bag drop solenoid", "dispense"),
    _binary_out("water_valve_solenoid", "Water valve solenoid", "water_valve"),
    _binary_out("fan", "Cabinet fan (autonomous)"),
    _binary_out("heater_relay", "Cabinet heater relay (autonomous)"),
    # --- Dispenser-profile channels (plan: dispenser profiles, Task 5) ---
    _binary_out("bag_fan", "Ice-bag cooling fan accessory", "dispense"),
    _binary_out("vending_now_light", "Vending-in-progress indicator light", "dispense"),
    _binary_in("door_sensor", "Release-gate door sensor"),  # True = door open
    ChannelRow(
        "agitator_current",
        "current",
        "Agitator motor current draw",
        _BINARY_CHANNEL_INTERVAL,
        "sensor",
        unit="A",
    ),
    ChannelRow(
        "auger_current",
        "current",
        "Auger motor current draw",
        _BINARY_CHANNEL_INTERVAL,
        "sensor",
        unit="A",
    ),
    ChannelRow(
        "fill_pulses",
        "counter",
        (
            "Flow-meter pulses during the current fill; divide by the slot "
            "profile's pulses_per_liter for litres"
        ),
        _BINARY_CHANNEL_INTERVAL,
        "sensor",
        unit="pulses",
    ),
)

CHANNELS: list[ChannelDescriptor] = [row.descriptor() for row in CHANNEL_ROWS]

# All binary hardware devices and their idle states.
HARDWARE_DEVICES: dict[str, bool] = {
    row.id: row.idle for row in CHANNEL_ROWS if row.idle is not None
}
