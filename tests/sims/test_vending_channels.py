"""The vending channel table is the IM600XL's I/O list (spec §5.1)."""

from ice_contracts.board.common import ChannelDescriptor
from ice_contracts.board.vending_machine import VendingChannel
from ice_sims.vending.channels import (
    BUTTON_CHANNELS,
    CHANNEL_ROWS,
    CHANNELS,
    HARDWARE_DEVICES,
    INPUT_CHANNELS,
    OUTPUT_CHANNELS,
)
from ice_sims.vending.simulator import VendingMachineSimulator

EXPECTED_IDS = [
    "bag_full_sensor",
    "button_ice",
    "button_water_1gal",
    "button_water_5gal",
    "ro_tank_full",
    "ro_tank_empty",
    "water_pressure_ok",
    "sump_high",
    "service_door",
    "agitator_motor",
    "auger_motor",
    "bag_fan",
    "trap_door_catch",
    "water_valve_solenoid",
    "ro_production",
    "heater_power",
    "fill_pulses",
    "ambient",
]
EXPECTED_OUTPUTS = {
    "agitator_motor",
    "auger_motor",
    "bag_fan",
    "trap_door_catch",
    "water_valve_solenoid",
    "ro_production",
    "heater_power",
}
EXPECTED_DRIVEN_BY = {
    "agitator_motor": "dispense",
    "auger_motor": "dispense",
    "bag_fan": "dispense",
    "trap_door_catch": "dispense",
    "water_valve_solenoid": "water_valve",
}
EXPECTED_KINDS = {"fill_pulses": "counter", "ambient": "temperature"}
REMOVED_IDS = {
    "cabinet",
    "bin_half_full",
    "door_sensor",
    "fan",
    "vending_now_light",
    "agitator_current",
    "auger_current",
    "water_flow",
    "water_flow_sensor",
    "heater_relay",
    "bag_drop_solenoid",
}


def test_channel_ids_are_the_im600xl_set_in_order():
    assert [c.channel_id for c in CHANNELS] == EXPECTED_IDS


def test_kinds_outputs_and_driven_by_match_the_spec_table():
    by_id = {c.channel_id: c for c in CHANNELS}
    for channel_id, kind in EXPECTED_KINDS.items():
        assert by_id[channel_id].kind == kind
    assert {c.channel_id for c in CHANNELS if c.kind == "binary"} == (
        set(EXPECTED_IDS) - set(EXPECTED_KINDS)
    )
    assert {c.channel_id for c in CHANNELS if c.direction == "output"} == (
        EXPECTED_OUTPUTS
    )
    assert {c.channel_id: c.driven_by for c in CHANNELS if c.driven_by} == (
        EXPECTED_DRIVEN_BY
    )
    # ro_production and heater_power are autonomous: no command drives them.
    assert by_id["ro_production"].driven_by is None
    assert by_id["heater_power"].driven_by is None


def test_hardware_devices_hold_every_binary_channel_with_its_idle_default():
    binary = {c.channel_id for c in CHANNELS if c.kind == "binary"}
    assert set(HARDWARE_DEVICES) == binary
    assert HARDWARE_DEVICES["water_pressure_ok"] is True
    assert [k for k, v in HARDWARE_DEVICES.items() if v] == ["water_pressure_ok"]


def test_removed_ids_are_gone_everywhere():
    assert not REMOVED_IDS & {c.channel_id for c in CHANNELS}
    assert not REMOVED_IDS & set(HARDWARE_DEVICES)


def test_every_vending_channel_the_im600xl_has_is_declared_under_its_exact_id():
    declared = {c.channel_id for c in CHANNELS}
    assert {member.value for member in VendingChannel} - {"bin_half_full"} <= declared
    assert "bin_half_full" not in declared  # the IM600XL has no bin sensor


def test_output_and_input_sets_derive_from_the_table():
    assert OUTPUT_CHANNELS == EXPECTED_OUTPUTS
    assert INPUT_CHANNELS == set(EXPECTED_IDS) - EXPECTED_OUTPUTS


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
