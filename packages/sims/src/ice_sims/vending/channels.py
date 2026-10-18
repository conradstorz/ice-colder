# ice_sims/vending/channels.py
"""The vending board's one declarative channel table.

Each `ChannelRow` is a single I/O point; `CHANNELS` (the capabilities
`ChannelDescriptor` list) and `HARDWARE_DEVICES` (binary id -> idle state)
are both derived from it, so they cannot drift apart. The rows are the
Kooler Ice IM600XL's I/O list (IM600XL simulator spec §5.1). No MQTT, no I/O.
"""

from dataclasses import dataclass
from typing import Literal

from ice_contracts.board.common import ChannelDescriptor
from ice_contracts.board.vending_machine import VendingChannel

SENSOR_PUBLISH_INTERVAL = 10.0  # seconds between periodic sensor publishes
_BINARY_CHANNEL_INTERVAL = 1.0

Kind = Literal["temperature", "current", "voltage", "level", "binary", "counter"]


@dataclass(frozen=True)
class ChannelRow:
    id: str
    kind: Kind
    description: str
    interval_seconds: float
    role: Literal["sensor", "output", "button"]
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


def _button(id: str, description: str) -> ChannelRow:
    return ChannelRow(
        id,
        "binary",
        description,
        _BINARY_CHANNEL_INTERVAL,
        "button",
        idle=False,
    )


# Spec §5.1, in declaration order: binary inputs, binary outputs, the
# `fill_pulses` counter, the `ambient` temperature. Ids with edge meaning
# come from `VendingChannel`; the rest are this board's own ids (they match
# docs/hardware/im600xl-io-list.md).
CHANNEL_ROWS: tuple[ChannelRow, ...] = (
    _binary_in("bag_full_sensor", "Ice bag full sensor"),
    _button("button_ice", "Bagged ice customer button"),
    _button("button_water_1gal", "1 gallon water customer button"),
    _button("button_water_5gal", "5 gallon water customer button"),
    _binary_in(VendingChannel.ro_tank_full.value, "RO storage tank full switch"),
    _binary_in(VendingChannel.ro_tank_empty.value, "RO storage tank empty switch"),
    _binary_in(
        VendingChannel.water_pressure_ok.value,
        "Water supply pressure OK (false = low pressure)",
        idle=True,
    ),
    _binary_in(VendingChannel.sump_high.value, "Sump pit high-level switch"),
    _binary_in(VendingChannel.service_door.value, "Service door open switch"),
    _binary_out("agitator_motor", "Ice agitator motor", "dispense"),
    _binary_out("auger_motor", "Ice auger motor", "dispense"),
    _binary_out("bag_fan", "Bag-opening fan", "dispense"),
    _binary_out(
        "trap_door_catch",
        "Trap-door magnetic catch (energized holds the door shut)",
        "dispense",
    ),
    _binary_out("water_valve_solenoid", "Water valve solenoid", "water_valve"),
    _binary_out(
        VendingChannel.ro_production.value,
        "RO production (inlet solenoid and booster pump; autonomous)",
    ),
    _binary_out("heater_power", "Cabinet heater power (autonomous)"),
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
    ChannelRow(
        "ambient",
        "temperature",
        "Ambient (machine room) temperature",
        SENSOR_PUBLISH_INTERVAL,
        "sensor",
        unit="C",
    ),
)

CHANNELS: list[ChannelDescriptor] = [row.descriptor() for row in CHANNEL_ROWS]

# Customer-button ids; the board knows these, not the edge's catalog.
BUTTON_CHANNELS: tuple[str, ...] = tuple(
    row.id for row in CHANNEL_ROWS if row.role == "button"
)

# All binary hardware devices and their idle states.
HARDWARE_DEVICES: dict[str, bool] = {
    row.id: row.idle for row in CHANNEL_ROWS if row.idle is not None
}

# What a real board can drive / read, for declared-channel enforcement in
# `dispense`: outputs are drivable; inputs, counters and temperatures are
# readable (a profile may sense a customer button or the fill counter).
OUTPUT_CHANNELS: frozenset[str] = frozenset(
    row.id for row in CHANNEL_ROWS if row.direction == "output"
)
INPUT_CHANNELS: frozenset[str] = frozenset(
    row.id for row in CHANNEL_ROWS if row.direction == "input"
)
