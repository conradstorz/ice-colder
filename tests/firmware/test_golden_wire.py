"""Firmware golden JSON validates against the contract models (pure file checks).

Always runs: no broker, no flag. Pydantic ignores extra keys, so each golden's
exact key set is asserted separately.
"""

import json

import pytest

from ice_contracts.board.common import CommandAck
from ice_contracts.board.ice_maker_monitor import ChannelReading
from ice_contracts.board.mqtt_messages import (
    ButtonPress,
    DispenserStatus,
    HardwareIO,
    SensorReading,
    SubsystemHeartbeat,
)
from ice_contracts.board.vending_machine import (
    DispenseStep,
    DispenserOutcome,
    SubsystemCapabilities,
)
from tests.firmware.board_client import EXPECTED_CHANNEL_IDS, EXPECTED_COMMANDS
from tests.repo_root import REPO_ROOT

GOLDEN = REPO_ROOT / "firmware" / "vending" / "test" / "golden"

ACK_KEYS = {"request_id", "command", "status", "phase"}
# golden file stem -> (model, exact key set)
MODELS = {
    "capabilities": (
        SubsystemCapabilities,
        {"subsystem", "firmware", "contract_version", "brand", "model",
         "hardware_id", "ip", "channels", "commands"},
    ),
    "heartbeat": (SubsystemHeartbeat, {"subsystem", "uptime_seconds"}),
    "lwt": (SubsystemHeartbeat, {"subsystem", "uptime_seconds"}),
    "ack_ok_accepted": (CommandAck, ACK_KEYS),
    "ack_ok_completed": (CommandAck, ACK_KEYS | {"result"}),
    "ack_failed": (CommandAck, ACK_KEYS | {"detail"}),
    "ack_rejected": (CommandAck, ACK_KEYS | {"detail"}),
    "ack_unsupported": (CommandAck, ACK_KEYS | {"detail"}),
    "hardware_io": (HardwareIO, {"device", "state"}),
    "button_press": (ButtonPress, {"button", "action"}),
    "dispenser_step": (DispenserStatus, {"slot", "state", "request_id"}),
    "dispenser_outcome": (DispenserStatus, {"slot", "state", "request_id", "detail"}),
    "telemetry_fill_pulses": (ChannelReading, {"channel_id", "value"}),
    "sensor_ambient": (SensorReading, {"location", "value", "unit"}),
}  # fmt: skip
CHANNEL_KEYS = {"channel_id", "kind", "unit", "description", "interval_seconds",
                "direction", "driven_by"}  # fmt: skip
HA_REQUIRED = {"name", "unique_id", "state_topic", "value_template", "device"}
HA_OPTIONAL = {"device_class", "unit_of_measurement", "state_class",
               "payload_on", "payload_off", "expire_after"}  # fmt: skip
HA_DEVICE_KEYS = {"identifiers", "name", "manufacturer", "model", "via_device"}


def load(stem: str) -> dict:
    return json.loads((GOLDEN / f"{stem}.json").read_text(encoding="utf-8"))


def test_every_golden_file_is_covered():
    assert {p.stem for p in GOLDEN.glob("*.json")} == set(MODELS) | {"ha_discovery"}


@pytest.mark.parametrize("stem", sorted(MODELS))
def test_golden_validates_with_exact_keys(stem):
    model, keys = MODELS[stem]
    data = load(stem)
    model.model_validate(data)
    assert set(data) == keys


def test_capabilities_golden_lists_exactly_the_channels_and_commands():
    caps = SubsystemCapabilities.model_validate(load("capabilities"))
    assert len(caps.channels) == 18
    assert {c.channel_id for c in caps.channels} == EXPECTED_CHANNEL_IDS
    assert set(caps.commands) == EXPECTED_COMMANDS
    for channel in load("capabilities")["channels"]:
        assert set(channel) == CHANNEL_KEYS


def test_button_press_golden_uses_a_channel_id_string():
    press = ButtonPress.model_validate(load("button_press"))
    assert isinstance(press.button, str)
    assert press.button in EXPECTED_CHANNEL_IDS


def test_dispenser_golden_states_are_contract_enum_values():
    DispenseStep(load("dispenser_step")["state"])
    DispenserOutcome(load("dispenser_outcome")["state"])


def test_ha_discovery_golden_has_the_shape_the_simulator_emits():
    doc = load("ha_discovery")
    assert HA_REQUIRED <= set(doc) <= HA_REQUIRED | HA_OPTIONAL
    assert set(doc["device"]) == HA_DEVICE_KEYS
