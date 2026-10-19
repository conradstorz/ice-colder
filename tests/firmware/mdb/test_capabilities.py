"""The MDB board's retained capabilities document and heartbeat."""

from ice_contracts.board.mqtt_messages import SubsystemHeartbeat
from ice_contracts.board.payment import CONTRACT_VERSION
from ice_contracts.board.vending_machine import SubsystemCapabilities

EXPECTED_COMMANDS = {
    "ping", "self_test", "force_report", "set_policy", "payout", "vend_request",
    "vend_result", "end_session", "return_escrow", "device_reset", "cash_sale",
}  # fmt: skip


async def test_retained_capabilities_validate(board):
    caps = SubsystemCapabilities.model_validate(
        await board.retained("capabilities/mdb")
    )
    assert caps.subsystem == "mdb"
    assert caps.contract_version == CONTRACT_VERSION == "1.1.0"


async def test_capabilities_list_exactly_the_mdb_commands_and_no_channels(board):
    caps = SubsystemCapabilities.model_validate(
        await board.retained("capabilities/mdb")
    )
    assert set(caps.commands) == EXPECTED_COMMANDS
    assert len(caps.commands) == len(EXPECTED_COMMANDS)
    assert caps.channels == []


async def test_heartbeat_arrives_within_twelve_seconds(board):
    beat = SubsystemHeartbeat.model_validate(
        await board.wait_for("heartbeat/mdb", lambda p: p["uptime_seconds"] >= 0, 12)
    )
    assert beat.subsystem == "mdb"
