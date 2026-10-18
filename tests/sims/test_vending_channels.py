"""The vending channel table is a declarative snapshot of the pre-split values."""

from ice_contracts.board.common import ChannelDescriptor
from ice_sims.vending.channels import (
    BUTTON_CHANNELS,
    CHANNEL_ROWS,
    CHANNELS,
    HARDWARE_DEVICES,
)
from ice_sims.vending.simulator import VendingMachineSimulator

# Captured from ice_sims/vending_machine.py before the split.
EXPECTED_CHANNELS = [
    {
        "channel_id": "cabinet",
        "kind": "temperature",
        "unit": "C",
        "description": "Cabinet temperature",
        "interval_seconds": 10.0,
        "direction": "input",
        "driven_by": None,
    },
    {
        "channel_id": "water_flow",
        "kind": "counter",
        "unit": "gal",
        "description": "Cumulative water dispensed",
        "interval_seconds": 10.0,
        "direction": "input",
        "driven_by": None,
    },
    {
        "channel_id": "button_ice",
        "kind": "binary",
        "unit": "",
        "description": "Bagged ice customer button",
        "interval_seconds": 1.0,
        "direction": "input",
        "driven_by": None,
    },
    {
        "channel_id": "button_water_1gal",
        "kind": "binary",
        "unit": "",
        "description": "1 gallon water customer button",
        "interval_seconds": 1.0,
        "direction": "input",
        "driven_by": None,
    },
    {
        "channel_id": "button_water_5gal",
        "kind": "binary",
        "unit": "",
        "description": "5 gallon water customer button",
        "interval_seconds": 1.0,
        "direction": "input",
        "driven_by": None,
    },
    {
        "channel_id": "bag_full_sensor",
        "kind": "binary",
        "unit": "",
        "description": "Ice bag full sensor",
        "interval_seconds": 1.0,
        "direction": "input",
        "driven_by": None,
    },
    {
        "channel_id": "water_flow_sensor",
        "kind": "binary",
        "unit": "",
        "description": "Water flow sensor",
        "interval_seconds": 1.0,
        "direction": "input",
        "driven_by": None,
    },
    {
        "channel_id": "bin_half_full",
        "kind": "binary",
        "unit": "",
        "description": "Ice bin half-full detector",
        "interval_seconds": 1.0,
        "direction": "input",
        "driven_by": None,
    },
    {
        "channel_id": "auger_motor",
        "kind": "binary",
        "unit": "",
        "description": "Ice auger motor",
        "interval_seconds": 1.0,
        "direction": "output",
        "driven_by": "dispense",
    },
    {
        "channel_id": "agitator_motor",
        "kind": "binary",
        "unit": "",
        "description": "Ice agitator motor",
        "interval_seconds": 1.0,
        "direction": "output",
        "driven_by": "dispense",
    },
    {
        "channel_id": "bag_drop_solenoid",
        "kind": "binary",
        "unit": "",
        "description": "Bag drop solenoid",
        "interval_seconds": 1.0,
        "direction": "output",
        "driven_by": "dispense",
    },
    {
        "channel_id": "water_valve_solenoid",
        "kind": "binary",
        "unit": "",
        "description": "Water valve solenoid",
        "interval_seconds": 1.0,
        "direction": "output",
        "driven_by": "water_valve",
    },
    {
        "channel_id": "fan",
        "kind": "binary",
        "unit": "",
        "description": "Cabinet fan (autonomous)",
        "interval_seconds": 1.0,
        "direction": "output",
        "driven_by": None,
    },
    {
        "channel_id": "heater_relay",
        "kind": "binary",
        "unit": "",
        "description": "Cabinet heater relay (autonomous)",
        "interval_seconds": 1.0,
        "direction": "output",
        "driven_by": None,
    },
    {
        "channel_id": "bag_fan",
        "kind": "binary",
        "unit": "",
        "description": "Ice-bag cooling fan accessory",
        "interval_seconds": 1.0,
        "direction": "output",
        "driven_by": "dispense",
    },
    {
        "channel_id": "vending_now_light",
        "kind": "binary",
        "unit": "",
        "description": "Vending-in-progress indicator light",
        "interval_seconds": 1.0,
        "direction": "output",
        "driven_by": "dispense",
    },
    {
        "channel_id": "door_sensor",
        "kind": "binary",
        "unit": "",
        "description": "Release-gate door sensor",
        "interval_seconds": 1.0,
        "direction": "input",
        "driven_by": None,
    },
    {
        "channel_id": "agitator_current",
        "kind": "current",
        "unit": "A",
        "description": "Agitator motor current draw",
        "interval_seconds": 1.0,
        "direction": "input",
        "driven_by": None,
    },
    {
        "channel_id": "auger_current",
        "kind": "current",
        "unit": "A",
        "description": "Auger motor current draw",
        "interval_seconds": 1.0,
        "direction": "input",
        "driven_by": None,
    },
    {
        "channel_id": "fill_pulses",
        "kind": "counter",
        "unit": "pulses",
        "description": "Flow-meter pulses during the current fill; divide by the slot "
        "profile's pulses_per_liter for litres",
        "interval_seconds": 1.0,
        "direction": "input",
        "driven_by": None,
    },
]
EXPECTED_HARDWARE_DEVICES = {
    "button_ice": False,
    "button_water_1gal": False,
    "button_water_5gal": False,
    "auger_motor": False,
    "agitator_motor": False,
    "fan": False,
    "bag_full_sensor": False,
    "bag_drop_solenoid": False,
    "water_valve_solenoid": False,
    "water_flow_sensor": False,
    "bin_half_full": True,
    "heater_relay": False,
    "bag_fan": False,
    "vending_now_light": False,
    "door_sensor": False,
}


def test_channels_equal_pre_split_snapshot():
    assert [c.model_dump() for c in CHANNELS] == EXPECTED_CHANNELS


def test_hardware_devices_equal_pre_split_snapshot():
    assert HARDWARE_DEVICES == EXPECTED_HARDWARE_DEVICES


def test_entry_module_reexports_same_objects():
    import ice_sims.vending_machine as entry

    assert entry.HARDWARE_DEVICES is HARDWARE_DEVICES
    assert entry.VendingMachineSimulator is VendingMachineSimulator


def test_every_row_has_a_role_and_idle_only_on_binary():
    for row in CHANNEL_ROWS:
        assert row.role in {"sensor", "output", "button"}
        assert (row.idle is not None) == (row.kind == "binary")
        assert (row.role == "output") == (row.direction == "output")


def test_simulator_declares_the_table():
    assert VendingMachineSimulator.CHANNELS is CHANNELS
    assert all(isinstance(c, ChannelDescriptor) for c in CHANNELS)


def test_button_channels_derive_from_button_role_rows():
    assert BUTTON_CHANNELS == ("button_ice", "button_water_1gal", "button_water_5gal")
    assert BUTTON_CHANNELS == tuple(r.id for r in CHANNEL_ROWS if r.role == "button")
    declared = {c.channel_id: c for c in CHANNELS}
    for button in BUTTON_CHANNELS:
        assert declared[button].kind == "binary"
        assert declared[button].direction == "input"
