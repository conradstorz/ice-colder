"""Simulator-local IM600XL dispenser profiles.

The same three profiles as `tests.dispenser_fixtures.IM600XL_GOOD` (a test
in tests/edge/vend asserts they stay equal), built straight from the board
contract so simulator tests never import edge code.
"""

from pydantic import TypeAdapter

from ice_contracts.board.dispenser_schema import SlotProfile
from ice_contracts.board.mqtt_messages import DispenseCommand

_ADAPTER: TypeAdapter[SlotProfile] = TypeAdapter(SlotProfile)

_UNMONITORED = {"stall_current_amps": "unmonitored", "current_channel": "unmonitored"}


def _water(sku: str, target_ml: float) -> dict:
    return {
        "mechanism": "water_fill",
        "product_sku": sku,
        "fill": {
            "proof": "flow_volume",
            "valve_channel": "water_valve_solenoid",
            "flow_sensor_channel": "fill_pulses",
            "target_volume_ml": target_ml,
            "pulses_per_liter": 450.0,
            "min_flow_ml_per_second": 10.0,
            "no_flow_grace_seconds": 5.0,
            "over_dispense_percent": 5.0,
            "max_fill_seconds": 90.0,
        },
    }


_RAW: dict[int, dict] = {
    0: {
        "mechanism": "bagged_ice",
        "product_sku": "SAMPLE-ICE",
        "agitate": {
            "motor_channel": "agitator_motor",
            "run_seconds": 8.0,
            **_UNMONITORED,
        },
        "fill": {
            "proof": "bag_full_sensor",
            "motor_channel": "auger_motor",
            "sensor_channel": "bag_full_sensor",
            "max_run_seconds": 45.0,
            **_UNMONITORED,
        },
        "release": {
            "proof": "catch",
            "catch_channel": "trap_door_catch",
            "drop_seconds": 2.0,
        },
        "accessories": {
            "bag_fan": {
                "channel": "bag_fan",
                "on_during": ["agitate", "fill"],
                "lead_seconds": 0.0,
                "lag_seconds": 2.0,
            }
        },
    },
    1: _water("SAMPLE-WATER-SM", 3785.0),
    2: _water("SAMPLE-WATER-LG", 18927.0),
}

IM600XL_PROFILES: dict[int, SlotProfile] = {
    slot: _ADAPTER.validate_python(raw) for slot, raw in _RAW.items()
}


def dispense_command(slot: int) -> DispenseCommand:
    """The `dispense` command the VMC would send for `slot`."""
    profile = IM600XL_PROFILES[slot]
    return DispenseCommand(slot=slot, mechanism=profile.mechanism, profile=profile)
