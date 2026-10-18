"""Heartbeats and the last-will offline marker."""

import aiomqtt
import pytest

from ice_contracts.board.mqtt_messages import SubsystemHeartbeat
from tests.firmware.board_client import mqtt_auth

HEARTBEAT = "heartbeat/vending"
# The simulator's fixed MQTT client id (ice_sims.base.ESP32Simulator.run).
SIM_CLIENT_ID = "sim-vending"


async def test_heartbeats_arrive_within_twelve_seconds_with_rising_uptime(board):
    first = SubsystemHeartbeat.model_validate(
        await board.wait_for(HEARTBEAT, lambda p: p["uptime_seconds"] >= 0, 12)
    )
    second = SubsystemHeartbeat.model_validate(
        await board.wait_for(
            HEARTBEAT,
            lambda p: p["uptime_seconds"] > first.uptime_seconds,
            12,
            since=board.mark(),
        )
    )
    assert first.subsystem == second.subsystem == "vending"


@pytest.mark.requires_sim
async def test_unclean_disconnect_publishes_the_lwt(board, target):
    """Taking over the simulator's client id makes the broker drop it uncleanly."""
    since = board.mark()
    async with aiomqtt.Client(
        hostname=target.host,
        port=target.port,
        identifier=SIM_CLIENT_ID,
        **mqtt_auth(),
    ):
        pass
    lwt = await board.wait_for(
        HEARTBEAT, lambda p: p["uptime_seconds"] == -1, 10, since=since
    )
    assert SubsystemHeartbeat.model_validate(lwt).subsystem == "vending"

    # The simulator reconnects by itself; wait until it answers again so later
    # tests do not race the reconnect.
    since = board.mark()
    await board.wait_for(HEARTBEAT, lambda p: p["uptime_seconds"] >= 0, 15, since=since)
    for _ in range(10):
        rid = await board.send("ping")
        try:
            await board.acks(rid, 1, 2)
            return
        except TimeoutError:
            continue
    pytest.fail("simulator did not answer commands after reconnecting")
