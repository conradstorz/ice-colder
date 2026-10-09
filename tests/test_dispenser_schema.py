# tests/test_dispenser_schema.py
"""Tests for the dispenser-profile Pydantic schema (plan: dispenser
profiles, Task 2). Builds plain dicts and validates them through
`TypeAdapter(SlotProfile).validate_python(...)`, the same entry point
Task 3's `dispensers.toml` loader will use for each `[slot.N]` table.
"""

import copy

import pytest
from pydantic import TypeAdapter, ValidationError

from services.dispenser_schema import (
    MECHANISM_FOR_KIND,
    SlotProfile,
    drive_channels,
    sense_channels,
    worst_case_seconds,
)

_SLOT_PROFILE = TypeAdapter(SlotProfile)


def _set_path(d: dict, path: str, value) -> dict:
    """Return a deep copy of `d` with the dotted `path` set to `value`."""
    out = copy.deepcopy(d)
    node = out
    parts = path.split(".")
    for part in parts[:-1]:
        node = node[part]
    node[parts[-1]] = value
    return out


def _bagged_ice(**step_overrides) -> dict:
    base = {
        "mechanism": "bagged_ice",
        "product_sku": "ICE-10LB",
        "agitate": {
            "motor_channel": "agitator_motor",
            "run_seconds": 4,
            "stall_current_amps": "unmonitored",
            "current_channel": "unmonitored",
        },
        "fill": {
            "proof": "bag_full_sensor",
            "motor_channel": "auger_motor",
            "sensor_channel": "bag_full_sensor",
            "max_run_seconds": 25,
            "stall_current_amps": "unmonitored",
            "current_channel": "unmonitored",
        },
        "release": {
            "proof": "door_sensor",
            "solenoid_channel": "bag_drop_solenoid",
            "sensor_channel": "door_sensor",
            "pulse_seconds": 1.5,
            "open_timeout_seconds": 3,
            "close_timeout_seconds": 5,
        },
        "accessories": {},
    }
    for path, value in step_overrides.items():
        base = _set_path(base, path, value)
    return base


def _water_fill_timed(**overrides) -> dict:
    base = {
        "mechanism": "water_fill",
        "product_sku": "WTR-20OZ",
        "fill": {
            "proof": "timed",
            "valve_channel": "fill_valve",
            "max_fill_seconds": 90,
        },
        "accessories": {},
    }
    for path, value in overrides.items():
        base = _set_path(base, path, value)
    return base


def _water_fill_by_volume(**overrides) -> dict:
    base = {
        "mechanism": "water_fill",
        "product_sku": "WTR-20OZ",
        "fill": {
            "proof": "flow_volume",
            "valve_channel": "fill_valve",
            "flow_sensor_channel": "fill_flow_sensor",
            "target_volume_ml": 500,
            "pulses_per_liter": 450,
            "min_flow_ml_per_second": 5,
            "no_flow_grace_seconds": 3,
            "over_dispense_percent": 5,
            "max_fill_seconds": 60,
        },
        "accessories": {},
    }
    for path, value in overrides.items():
        base = _set_path(base, path, value)
    return base


def test_bagged_ice_minimal_valid():
    profile = _SLOT_PROFILE.validate_python(_bagged_ice())
    assert profile.mechanism == "bagged_ice"
    assert profile.accessories == {}


def test_quoted_number_rejected():
    """Strict mode (M4) rejects a TOML string standing in for a number
    (`run_seconds = "4.0"`) even though a plain TOML int for a float field
    (`run_seconds = 4`, used throughout this schema's own samples) must
    keep validating."""
    data = _bagged_ice(**{"agitate.run_seconds": "4.0"})
    with pytest.raises(ValidationError):
        _SLOT_PROFILE.validate_python(data)


def test_unknown_field_rejected():
    data = _water_fill_by_volume()
    del data["fill"]["pulses_per_liter"]
    data["fill"]["pulses_per_litre"] = 450
    with pytest.raises(ValidationError) as exc_info:
        _SLOT_PROFILE.validate_python(data)
    errors = exc_info.value.errors()
    assert any(
        e["type"] == "extra_forbidden"
        and "fill" in e["loc"]
        and e["loc"][-1] == "pulses_per_litre"
        for e in errors
    )


def test_timed_fill_rejects_sensor_channel():
    data = _bagged_ice()
    data["fill"] = {
        "proof": "timed",
        "motor_channel": "auger_motor",
        "sensor_channel": "bag_full_sensor",
        "max_run_seconds": 25,
        "stall_current_amps": "unmonitored",
        "current_channel": "unmonitored",
    }
    with pytest.raises(ValidationError) as exc_info:
        _SLOT_PROFILE.validate_python(data)
    errors = exc_info.value.errors()
    assert any(
        e["type"] == "extra_forbidden" and e["loc"][-1] == "sensor_channel"
        for e in errors
    )


def test_current_sense_requires_both():
    data = _bagged_ice(
        **{
            "agitate.stall_current_amps": 5.0,
            "agitate.current_channel": "unmonitored",
        }
    )
    with pytest.raises(ValidationError) as exc_info:
        _SLOT_PROFILE.validate_python(data)
    assert 'both be set or both be "unmonitored"' in str(exc_info.value)


def test_unmonitored_rejected_for_required_sensor():
    expected_msg = (
        '"unmonitored" is only allowed for stall_current_amps/current_channel; '
        "this channel is required"
    )

    ice_fill = _bagged_ice(**{"fill.sensor_channel": "unmonitored"})
    with pytest.raises(ValidationError) as exc_info:
        _SLOT_PROFILE.validate_python(ice_fill)
    assert expected_msg in str(exc_info.value)

    water_fill = _water_fill_by_volume(**{"fill.flow_sensor_channel": "unmonitored"})
    with pytest.raises(ValidationError) as exc_info:
        _SLOT_PROFILE.validate_python(water_fill)
    assert expected_msg in str(exc_info.value)

    release = _bagged_ice(**{"release.sensor_channel": "unmonitored"})
    with pytest.raises(ValidationError) as exc_info:
        _SLOT_PROFILE.validate_python(release)
    assert expected_msg in str(exc_info.value)

    drive = _bagged_ice(**{"agitate.motor_channel": "unmonitored"})
    with pytest.raises(ValidationError) as exc_info:
        _SLOT_PROFILE.validate_python(drive)
    assert expected_msg in str(exc_info.value)


def test_unmonitored_still_accepted_for_current_sense():
    profile = _SLOT_PROFILE.validate_python(_bagged_ice())
    assert profile.agitate.current_channel == "unmonitored"
    assert profile.agitate.stall_current_amps == "unmonitored"


def test_accessory_on_during_must_name_mechanism_steps():
    data = _water_fill_timed()
    data["accessories"] = {
        "label_light": {
            "channel": "label_light",
            "on_during": ["agitate"],
            "lead_seconds": 0,
            "lag_seconds": 0,
        }
    }
    with pytest.raises(ValidationError) as exc_info:
        _SLOT_PROFILE.validate_python(data)
    assert "valid steps for water_fill are fill" in str(exc_info.value)


def test_accessory_all_alone():
    data = _water_fill_timed()
    data["accessories"] = {
        "label_light": {
            "channel": "label_light",
            "on_during": ["all", "fill"],
            "lead_seconds": 0,
            "lag_seconds": 0,
        }
    }
    with pytest.raises(ValidationError):
        _SLOT_PROFILE.validate_python(data)

    data["accessories"]["label_light"]["on_during"] = ["all"]
    profile = _SLOT_PROFILE.validate_python(data)
    assert profile.accessories["label_light"].on_during == ["all"]


def test_accessory_on_during_must_be_contiguous():
    """Copilot review (PR #32) finding C6: the simulator reduces
    on_during to a first/last span when deciding whether the accessory
    is active, so ["agitate", "release"] would silently also cover
    "fill" at runtime -- the schema must reject a gap outright."""
    data = _bagged_ice()
    data["accessories"] = {
        "bag_fan": {
            "channel": "bag_fan",
            "on_during": ["agitate", "release"],
            "lead_seconds": 0,
            "lag_seconds": 0,
        }
    }
    with pytest.raises(ValidationError) as exc_info:
        _SLOT_PROFILE.validate_python(data)
    assert (
        'accessory "bag_fan": on_during steps must be consecutive '
        "(got agitate, release; fill is skipped)" in str(exc_info.value)
    )


def test_accessory_on_during_contiguous_but_unordered_is_accepted():
    """Order in the list doesn't matter, only which steps are named --
    ["release", "fill"] (fill, release are adjacent) must validate."""
    data = _bagged_ice()
    data["accessories"] = {
        "bag_fan": {
            "channel": "bag_fan",
            "on_during": ["release", "fill"],
            "lead_seconds": 0,
            "lag_seconds": 0,
        }
    }
    profile = _SLOT_PROFILE.validate_python(data)
    assert profile.accessories["bag_fan"].on_during == ["release", "fill"]


@pytest.mark.parametrize(
    "factory,path,value",
    [
        (_bagged_ice, "agitate.run_seconds", 0.4),
        (_bagged_ice, "fill.max_run_seconds", 121),
        (_bagged_ice, "release.pulse_seconds", 0),
        (_water_fill_by_volume, "fill.target_volume_ml", 49),
        (_bagged_ice, "agitate.stall_current_amps", 0.05),
    ],
)
def test_ranges(factory, path, value):
    data = factory(**{path: value})
    with pytest.raises(ValidationError):
        _SLOT_PROFILE.validate_python(data)


def test_worst_case_seconds_bagged_ice():
    data = _bagged_ice()
    data["accessories"] = {
        "bag_fan": {
            "channel": "bag_fan",
            "on_during": ["fill"],
            "lead_seconds": 2,
            "lag_seconds": 0.5,
        }
    }
    profile = _SLOT_PROFILE.validate_python(data)
    assert worst_case_seconds(profile) == 41.0


def test_worst_case_seconds_water_timed():
    profile = _SLOT_PROFILE.validate_python(_water_fill_timed())
    assert worst_case_seconds(profile) == 90.0


def test_drive_and_sense_channels():
    data = _bagged_ice()
    data["accessories"] = {
        "bag_fan": {
            "channel": "bag_fan",
            "on_during": ["fill"],
            "lead_seconds": 0,
            "lag_seconds": 0,
        }
    }
    profile = _SLOT_PROFILE.validate_python(data)
    assert drive_channels(profile) == {
        "agitator_motor",
        "auger_motor",
        "bag_drop_solenoid",
        "bag_fan",
    }
    assert sense_channels(profile) == {"bag_full_sensor", "door_sensor"}


def test_mechanism_for_kind():
    assert MECHANISM_FOR_KIND == {"ice": "bagged_ice", "water": "water_fill"}
