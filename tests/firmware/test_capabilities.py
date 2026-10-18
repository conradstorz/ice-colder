"""The board's retained capabilities document."""

from ice_contracts.board.vending_machine import SubsystemCapabilities
from tests.firmware.board_client import EXPECTED_CHANNEL_IDS, EXPECTED_COMMANDS


async def test_retained_capabilities_validate(board):
    caps = SubsystemCapabilities.model_validate(
        await board.retained("capabilities/vending")
    )
    assert caps.subsystem == "vending"


async def test_capabilities_list_the_standard_and_actuator_commands(board):
    caps = SubsystemCapabilities.model_validate(
        await board.retained("capabilities/vending")
    )
    assert EXPECTED_COMMANDS <= set(caps.commands)


async def test_capabilities_declare_exactly_the_im600xl_channels(board):
    caps = SubsystemCapabilities.model_validate(
        await board.retained("capabilities/vending")
    )
    assert {c.channel_id for c in caps.channels} == EXPECTED_CHANNEL_IDS
    assert len(caps.channels) == len(EXPECTED_CHANNEL_IDS)
