"""The simulator-local IM600XL profiles are valid contract models."""

import pytest

from ice_contracts.board.dispenser_schema import (
    BaggedIceProfile,
    ReleaseByCatch,
    WaterFillByVolume,
    WaterFillProfile,
)
from tests.sims.vending_profiles import IM600XL_PROFILES, dispense_command


def test_three_slots():
    assert sorted(IM600XL_PROFILES) == [0, 1, 2]


def test_slot_0_is_catch_released_bagged_ice():
    profile = IM600XL_PROFILES[0]
    assert isinstance(profile, BaggedIceProfile)
    assert isinstance(profile.release, ReleaseByCatch)
    assert profile.release.catch_channel == "trap_door_catch"


@pytest.mark.parametrize("slot, ml", [(1, 3785.0), (2, 18927.0)])
def test_water_slots_count_fill_pulses(slot, ml):
    profile = IM600XL_PROFILES[slot]
    assert isinstance(profile, WaterFillProfile)
    assert isinstance(profile.fill, WaterFillByVolume)
    assert profile.fill.flow_sensor_channel == "fill_pulses"
    assert profile.fill.target_volume_ml == ml


@pytest.mark.parametrize("slot", [0, 1, 2])
def test_dispense_command_carries_the_slot_profile(slot):
    cmd = dispense_command(slot)
    assert cmd.slot == slot
    assert cmd.mechanism == IM600XL_PROFILES[slot].mechanism
    assert cmd.profile == IM600XL_PROFILES[slot]
