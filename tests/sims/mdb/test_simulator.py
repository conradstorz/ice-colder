"""MdbSimulator behaviour on payment contract 1.1.0, driven with a recording client.

No wall-clock waits: time is a `FakeTime`, the payout delay is zero and
background work is awaited with `drain_background()`.
"""

import asyncio
import contextlib
import re
from datetime import timedelta

import pytest

from ice_contracts.board.common import STANDARD_COMMANDS
from ice_contracts.board.payment import (
    CONTRACT_VERSION,
    PAYMENT_COMMANDS,
    PaymentEventEnvelope,
    PaymentState,
)
from ice_sims.mdb.simulator import MdbSimulator
from tests.async_helpers import settle
from tests.sims.mdb.fakes import FakeClient, FakeTime, policy, send


@pytest.fixture
def clock():
    return FakeTime()


@pytest.fixture
def sim(clock):
    s = MdbSimulator(machine_id="vmc-t", clock=clock.monotonic, now=clock.now)
    s.PAYOUT_DELAY_RANGE = (0.0, 0.0)
    return s


@pytest.fixture
def client():
    return FakeClient()


async def armed(sim, client, **over):
    ack = await send(sim, client, "set_policy", policy(**over))
    assert ack.status == "ok"
    client.clear()


# --- capabilities, state, HA ---------------------------------------------------


def test_capabilities_advertise_eleven_commands_and_no_channels(sim):
    caps = sim.build_capabilities()
    assert caps.contract_version == CONTRACT_VERSION == "1.1.0"
    assert caps.commands == [*STANDARD_COMMANDS, *PAYMENT_COMMANDS]
    assert len(caps.commands) == 11
    assert caps.channels == []


def test_state_validates_and_starts_without_policy(sim):
    state = sim.build_state()
    PaymentState.model_validate(state.model_dump(mode="json"))
    assert state.accepting is False
    assert state.inhibited_reason.value == "no_policy"
    assert state.policy_id is None
    assert state.devices.cashless.present is False
    assert state.devices.coin.present and state.devices.bill.present
    assert state.seq_last == 0


async def test_state_after_policy_is_accepting_with_enabled_subsets(sim, client):
    await send(sim, client, "set_policy", policy(coin_enabled=[25, 100]))
    state = sim.build_state()
    assert state.accepting is True and state.inhibited_reason is None
    assert state.policy_id == "pol-0001"
    assert state.devices.coin.enabled == [25, 100]
    assert set(state.devices.coin.enabled) <= set(state.devices.coin.denominations)
    published = client.retained("payment/state")
    assert published and published[-1]["policy_id"] == "pol-0001"


async def test_force_report_republishes_state_retained(sim, client):
    ack = await send(sim, client, "force_report")
    assert ack.status == "ok"
    assert client.retained("payment/state")
    assert client.retained("capabilities/mdb")


async def test_state_is_republished_on_a_timer_loop(sim, client):
    sleeps: list[float] = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) >= 2:
            raise asyncio.CancelledError

    sim.set_sleep(fake_sleep)
    with pytest.raises(asyncio.CancelledError):
        await sim.state_loop(client)
    assert sleeps == [30, 30]
    assert len(client.retained("payment/state")) >= 2


def test_ha_entities(sim):
    entities = {e["object_id"]: e for e in sim.ha_discovery_entities()}
    assert {"coin_ready", "bill_ready", "escrow_cents", "accepting", "uptime"} <= set(
        entities
    )
    assert "cashless_ready" not in entities
    assert entities["coin_ready"]["component"] == "binary_sensor"
    assert entities["coin_ready"]["state_topic_suffix"] == "payment/state"
    assert "devices.coin.status == 'ready'" in entities["coin_ready"]["value_template"]
    assert entities["escrow_cents"]["component"] == "sensor"
    assert "escrow_cents" in entities["escrow_cents"]["value_template"]
    assert entities["accepting"]["component"] == "sensor"
    assert entities["uptime"]["state_topic_suffix"] == "heartbeat/mdb"


# --- envelope wire format ------------------------------------------------------


async def test_events_validate_and_timestamps_match_the_golden_format(sim, client):
    await armed(sim, client)
    assert await sim.insert_coin(client, 25) is True
    raw = client.raw("payment/event")[0]
    env = PaymentEventEnvelope.model_validate_json(raw)
    assert env.event.kind == "coin_accepted"
    assert re.search(r'"ts":"2026-10-30T12:00:00Z"', raw)
    assert env.seq == 1 and env.boot_id == sim.boot_id


# --- replay-log backpressure (firmware: inhibit at 192 pending, resume at 128) ----


async def test_log_pressure_inhibits_acceptance_and_keeps_credits_replayable(
    sim, client
):
    await armed(sim, client)
    accepted = 0
    while await sim.insert_coin(client, 25):
        accepted += 1
        assert accepted <= 256, "acceptance never inhibited"
    assert accepted == 192
    state = sim.build_state()
    assert state.accepting is False
    assert state.inhibited_reason.value == "device_fault"
    assert await sim.insert_bill(client, 500) is False
    # every accepted credit is still replayable, oldest first
    replay = sim.log.unacked()
    assert [e["seq"] for e in replay] == list(range(1, 193))
    # acking down to 128 pending resumes acceptance
    sim.handle_event_ack({"boot_id": sim.boot_id, "seq": 64})
    assert sim.build_state().accepting is True
    assert await sim.insert_coin(client, 25) is True


# --- rule 1: no policy, no money -----------------------------------------------


async def test_nothing_is_accepted_without_a_policy(sim, client):
    assert await sim.insert_coin(client, 25) is False
    assert await sim.insert_bill(client, 500) is False
    assert client.events() == []


async def test_payout_without_policy_is_rejected_no_policy(sim, client):
    ack = await send(sim, client, "payout", {"cents": 100, "reason": "change"})
    assert (ack.status, ack.detail) == ("rejected", "no_policy")
    assert client.events() == []


async def test_vend_request_without_policy_is_rejected_no_policy(sim, client):
    ack = await send(sim, client, "vend_request", {"cents": 100, "item": "ice"})
    assert (ack.status, ack.detail) == ("rejected", "no_policy")


async def test_set_policy_sets_acked_in_boot(sim, client):
    assert sim.policy.acked_in_boot is False
    await send(sim, client, "set_policy", policy())
    assert sim.policy.acked_in_boot is True


# --- payouts -------------------------------------------------------------------


async def test_payout_is_accepted_then_a_payout_result_follows(sim, client):
    await armed(sim, client)
    await sim.insert_coin(client, 100)
    await sim.insert_coin(client, 25)
    client.clear()
    ack = await send(
        sim,
        client,
        "payout",
        {"cents": 125, "reason": "change"},
        request_id="pay-0001a",
    )
    assert (ack.status, ack.phase) == ("ok", "accepted")
    assert client.events() == []
    await sim.drain_background()
    result = client.events()[-1]["event"]
    assert result["kind"] == "payout_result"
    assert result["request_id"] == "pay-0001a"
    assert (result["requested_cents"], result["paid_cents"], result["status"]) == (
        125,
        125,
        "ok",
    )
    assert sim.cash.escrow_cents == 0
    assert client.retained("payment/state")[-1]["escrow_cents"] == 0


async def test_repeated_payout_request_id_replays_and_never_pays_twice(sim, client):
    await armed(sim, client)
    for _ in range(4):
        await sim.insert_coin(client, 100)
    await send(
        sim,
        client,
        "payout",
        {"cents": 100, "reason": "change"},
        request_id="pay-dup001",
    )
    await sim.drain_background()
    paid_tubes = [t.count for t in sim.cash.tubes]
    first = [e for e in client.events() if e["event"]["kind"] == "payout_result"]
    assert len(first) == 1
    client.clear()

    await send(
        sim,
        client,
        "payout",
        {"cents": 100, "reason": "change"},
        request_id="pay-dup001",
    )
    await sim.drain_background()
    replay = client.events()
    assert [e["event"]["kind"] for e in replay] == ["payout_result"]
    assert replay[0]["event_id"] == first[0]["event_id"]
    assert [t.count for t in sim.cash.tubes] == paid_tubes


async def test_payout_replay_survives_the_ack_cache_eviction(sim, client):
    await armed(sim, client)
    for _ in range(3):
        await sim.insert_coin(client, 100)
    await send(
        sim,
        client,
        "payout",
        {"cents": 100, "reason": "change"},
        request_id="pay-evict1",
    )
    await sim.drain_background()
    tubes = [t.count for t in sim.cash.tubes]
    for _ in range(40):
        await send(sim, client, "ping")
    client.clear()
    await send(
        sim,
        client,
        "payout",
        {"cents": 100, "reason": "change"},
        request_id="pay-evict1",
    )
    await sim.drain_background()
    assert [t.count for t in sim.cash.tubes] == tubes
    assert client.kinds() == ["payout_result"]


async def test_payout_with_another_result_held_is_busy_until_it_ends(sim, client):
    await armed(sim, client)
    for _ in range(4):
        await sim.insert_coin(client, 100)
    release = asyncio.Event()

    async def gated(_seconds):
        await release.wait()

    sim.set_sleep(gated)
    pay = {"cents": 100, "reason": "change"}
    first = await send(sim, client, "payout", pay, request_id="pay-busy01")
    assert (first.status, first.phase) == ("ok", "accepted")

    other = await send(sim, client, "payout", pay, request_id="pay-busy02")
    assert (other.status, other.detail) == ("rejected", "busy")
    repeat = await send(sim, client, "payout", pay, request_id="pay-busy01")
    assert (repeat.status, repeat.phase) == ("ok", "accepted")

    release.set()
    await sim.drain_background()
    again = await send(sim, client, "payout", pay, request_id="pay-busy03")
    assert (again.status, again.phase) == ("ok", "accepted")
    await sim.drain_background()
    assert client.kinds().count("payout_result") == 2


async def test_expired_payout_is_rejected_expired(sim, client, clock):
    await armed(sim, client, edge_timeout_s=30)
    stale = clock.now() - timedelta(seconds=31)
    ack = await send(
        sim, client, "payout", {"cents": 100, "reason": "change"}, timestamp=stale
    )
    assert (ack.status, ack.detail) == ("rejected", "expired")
    fresh = clock.now() - timedelta(seconds=5)
    ack = await send(
        sim, client, "payout", {"cents": 100, "reason": "change"}, timestamp=fresh
    )
    assert ack.status == "ok"
    await sim.drain_background()


async def test_changer_empty_makes_payouts_fail(sim, client):
    await armed(sim, client)
    await sim.insert_coin(client, 100)
    fault = next(f for f in sim.fault_defs if f.name == "changer_empty")
    await sim.activate_fault(client, fault, recover_in=9e9)
    client.clear()
    await send(sim, client, "payout", {"cents": 100, "reason": "change"})
    await sim.drain_background()
    result = client.events()[-1]["event"]
    assert result["status"] == "failed" and result["paid_cents"] == 0


async def test_short_tubes_give_a_partial_payout(sim, client):
    await armed(sim, client)
    await sim.insert_coin(client, 100)
    for tube in sim.cash.tubes:
        tube.count = 0
    sim.cash.tubes[2].count = 2  # two quarters left
    client.clear()
    await send(sim, client, "payout", {"cents": 100, "reason": "cancel"})
    await sim.drain_background()
    result = client.events()[-1]["event"]
    assert (result["status"], result["paid_cents"]) == ("partial", 50)


async def test_changer_empty_emits_device_status_and_recovers(sim, client):
    await armed(sim, client)
    fault = next(f for f in sim.fault_defs if f.name == "changer_empty")
    await sim.activate_fault(client, fault, recover_in=9e9)
    status = [
        e["event"] for e in client.events() if e["event"]["kind"] == "device_status"
    ]
    assert status[-1]["device"] == "coin" and status[-1]["status"] == "empty"
    assert sim.build_state().exact_change is True
    await fault.on_recover(client)
    assert sim.build_state().devices.coin.status.value == "ready"
    assert sim.cash.tube_total_cents() > 0


# --- cash_sale -----------------------------------------------------------------


async def test_cash_sale_lowers_escrow(sim, client):
    await armed(sim, client)
    await sim.insert_coin(client, 100)
    await sim.insert_coin(client, 100)
    ack = await send(sim, client, "cash_sale", {"cents": 150, "sale_id": "s1"})
    assert (ack.status, ack.detail) == ("ok", None)
    assert sim.cash.escrow_cents == 50


async def test_cash_sale_clamps_at_zero_and_says_so(sim, client):
    await armed(sim, client)
    await sim.insert_coin(client, 100)
    ack = await send(sim, client, "cash_sale", {"cents": 300})
    assert (ack.status, ack.detail) == ("ok", "clamped")
    assert sim.cash.escrow_cents == 0


# --- sessions, vend ------------------------------------------------------------


async def test_session_commands_are_rejected_no_session(sim, client):
    await armed(sim, client)
    ack = await send(sim, client, "vend_request", {"cents": 100, "item": "ice"})
    assert (ack.status, ack.detail) == ("rejected", "no_session")
    ack = await send(
        sim,
        client,
        "vend_result",
        {"session_id": "s-1", "request_id": "vend-0001", "outcome": "success"},
    )
    assert (ack.status, ack.detail) == ("rejected", "no_session")
    ack = await send(sim, client, "end_session", {"session_id": "s-1"})
    assert (ack.status, ack.detail) == ("rejected", "no_session")


async def test_unknown_command_is_unsupported(sim, client):
    ack = await send(sim, client, "teleport")
    assert ack.status == "unsupported"


# --- bills, coins, escrow cap --------------------------------------------------


async def test_bill_is_escrowed_then_stacked(sim, client):
    await armed(sim, client)
    assert await sim.insert_bill(client, 500) is True
    assert sim.build_state().bill_in_escrow_cents == 500
    assert sim.build_state().escrow_cents == 0
    await sim.stack_bill(client)
    assert client.kinds() == ["bill_escrowed", "bill_stacked"]
    state = sim.build_state()
    assert state.bill_in_escrow_cents is None and state.escrow_cents == 500
    assert state.stacker.count == 1


async def test_bill_over_the_cap_is_returned_by_policy_but_coins_are_not_refused(
    sim, client
):
    await armed(sim, client, max_escrow_cents=600)
    await sim.insert_bill(client, 500)
    await sim.stack_bill(client)
    client.clear()
    assert await sim.insert_bill(client, 500) is False
    events = client.events()
    assert [e["event"]["kind"] for e in events] == ["bill_escrowed", "bill_returned"]
    assert events[1]["event"]["reason"] == "policy"
    assert await sim.insert_coin(client, 100) is True
    assert (
        await sim.insert_coin(client, 100) is True
    )  # past the cap: coins never refused


async def test_lever_returns_a_held_bill_when_policy_says_so(sim, client):
    await armed(sim, client, return_bill_on_lever=True)
    await sim.insert_bill(client, 500)
    client.clear()
    await sim.press_lever(client)
    events = [e["event"] for e in client.events()]
    assert [e["kind"] for e in events] == ["bill_returned", "escrow_return_pressed"]
    assert events[0]["reason"] == "customer"
    assert sim.build_state().bill_in_escrow_cents is None


async def test_lever_keeps_the_bill_without_return_bill_on_lever(sim, client):
    await armed(sim, client, return_bill_on_lever=False)
    await sim.insert_bill(client, 500)
    client.clear()
    await sim.press_lever(client)
    assert client.kinds() == ["escrow_return_pressed"]
    assert sim.build_state().bill_in_escrow_cents == 500


async def test_return_escrow_with_and_without_a_bill(sim, client):
    await armed(sim, client)
    ack = await send(sim, client, "return_escrow")
    assert (ack.status, ack.detail) == ("ok", "no_bill")
    await sim.insert_bill(client, 500)
    client.clear()
    ack = await send(sim, client, "return_escrow")
    assert ack.status == "ok"
    ev = client.events()[-1]["event"]
    assert (ev["kind"], ev["reason"], ev["cents"]) == ("bill_returned", "edge", 500)


async def test_device_reset_reports_the_device_status(sim, client):
    await armed(sim, client)
    ack = await send(sim, client, "device_reset", {"device": "coin"})
    assert ack.status == "ok"
    ev = client.events()[-1]["event"]
    assert (ev["kind"], ev["device"], ev["status"]) == (
        "device_status",
        "coin",
        "ready",
    )
    ack = await send(sim, client, "device_reset", {"device": "cashless"})
    assert (ack.status, ack.detail) == ("rejected", "not_present")


async def test_coin_jam_blocks_coins(sim, client):
    await armed(sim, client)
    jam = next(f for f in sim.fault_defs if f.name == "coin_jammed")
    await sim.activate_fault(client, jam, recover_in=9e9)
    assert await sim.insert_coin(client, 25) is False
    ev = client.events()[-1]["event"]
    assert (ev["kind"], ev["device"], ev["status"]) == (
        "device_status",
        "coin",
        "jammed",
    )
    assert await sim.insert_bill(client, 500) is True


async def test_bill_offline_returns_a_held_bill_as_timeout(sim, client):
    await armed(sim, client)
    await sim.insert_bill(client, 500)
    client.clear()
    off = next(f for f in sim.fault_defs if f.name == "bill_offline")
    await sim.activate_fault(client, off, recover_in=9e9)
    events = [e["event"] for e in client.events()]
    assert [e["kind"] for e in events] == ["bill_returned", "device_status"]
    assert events[0]["reason"] == "timeout"
    assert await sim.insert_bill(client, 500) is False


# --- edge silence --------------------------------------------------------------


async def test_edge_silence_disables_and_returns_a_held_bill(sim, client, clock):
    await armed(sim, client, edge_timeout_s=30)
    await sim.insert_bill(client, 500)
    client.clear()
    clock.advance(29)
    await sim.check_edge_silence(client)
    assert sim.build_state().accepting is True
    clock.advance(2)
    await sim.check_edge_silence(client)
    ev = client.events()[-1]["event"]
    assert (ev["kind"], ev["reason"]) == ("bill_returned", "edge_silent")
    state = client.retained("payment/state")[-1]
    assert state["accepting"] is False and state["inhibited_reason"] == "edge_silent"
    assert await sim.insert_coin(client, 25) is False
    assert await sim.insert_bill(client, 500) is False


async def test_traffic_keeps_the_edge_alive_and_set_policy_resumes(sim, client, clock):
    await armed(sim, client, edge_timeout_s=30)
    clock.advance(20)
    await send(sim, client, "ping")
    clock.advance(20)
    await sim.check_edge_silence(client)
    assert sim.build_state().accepting is True
    clock.advance(31)
    await sim.check_edge_silence(client)
    assert sim.build_state().inhibited_reason.value == "edge_silent"
    await send(sim, client, "ping")  # traffic alone does not resume acceptance
    assert sim.build_state().inhibited_reason.value == "edge_silent"
    await send(sim, client, "set_policy", policy())
    assert sim.build_state().accepting is True
    assert await sim.insert_coin(client, 25) is True


# --- delivery: ack, replay, reboot ---------------------------------------------


async def test_acks_remove_events_cumulatively_within_a_boot(sim, client):
    await armed(sim, client)
    for _ in range(3):
        await sim.insert_coin(client, 25)
    sim.handle_event_ack({"boot_id": sim.boot_id, "seq": 2})
    client.clear()
    await sim.replay(client)
    assert [e["seq"] for e in client.events()] == [3]


async def test_replay_republishes_the_whole_unacked_log_in_order(sim, client):
    await armed(sim, client)
    for cents in (5, 10, 25):
        await sim.insert_coin(client, cents)
    original = client.events()
    client.clear()
    await sim.replay(client)
    assert client.events() == original


async def test_run_simulation_replays_the_log_before_anything_new(sim, client):
    await armed(sim, client)
    await sim.insert_coin(client, 25)
    await sim.insert_coin(client, 10)
    client.clear()
    task = asyncio.create_task(sim.run_simulation(client))
    await settle(5)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    topics = [t for t, _, _ in client.published]
    first_events = [i for i, t in enumerate(topics) if t.endswith("payment/event")]
    assert [client.events()[i]["seq"] for i in range(2)] == [1, 2]
    assert first_events[:2] == [0, 1]


async def test_board_reboot_mints_a_boot_id_and_replays_old_events_first(sim, client):
    await armed(sim, client)
    await sim.insert_coin(client, 25)
    old_boot = sim.boot_id
    client.clear()
    fault = next(f for f in sim.fault_defs if f.name == "board_reboot")
    await sim.activate_fault(client, fault, recover_in=9e9)
    assert sim.boot_id != old_boot
    state = client.retained("payment/state")[-1]
    assert state["boot_id"] == sim.boot_id and state["policy_id"] is None
    assert state["inhibited_reason"] == "no_policy"
    assert state["escrow_cents"] == 0  # escrow is RAM only: zero on every boot
    replayed = client.events()
    assert [(e["boot_id"], e["seq"]) for e in replayed] == [(old_boot, 1)]
    # new events carry the new boot and restart at seq 1
    await send(sim, client, "set_policy", policy())
    await sim.insert_coin(client, 10)
    new = client.events()[-1]
    assert (new["boot_id"], new["seq"]) == (sim.boot_id, 1)
    # an ack under the new boot does not clear the old boot's entries
    sim.handle_event_ack({"boot_id": sim.boot_id, "seq": 99})
    client.clear()
    await sim.replay(client)
    assert [(e["boot_id"], e["seq"]) for e in client.events()] == [(old_boot, 1)]
    sim.handle_event_ack({"boot_id": old_boot, "seq": 1})
    client.clear()
    await sim.replay(client)
    assert client.events() == []


async def test_drop_ack_window_ignores_acks_then_replays_on_recovery(sim, client):
    await armed(sim, client)
    await sim.insert_coin(client, 25)
    fault = next(f for f in sim.fault_defs if f.name == "drop_ack_window")
    await sim.activate_fault(client, fault, recover_in=9e9)
    sim.handle_event_ack({"boot_id": sim.boot_id, "seq": 1})
    client.clear()
    await fault.on_recover(client)
    assert [e["seq"] for e in client.events()] == [1]
    sim.handle_event_ack({"boot_id": sim.boot_id, "seq": 1})
    client.clear()
    await sim.replay(client)
    assert client.events() == []


def test_a_malformed_ack_is_ignored(sim):
    sim.handle_event_ack({"boot_id": "x", "seq": "nope"})


# --- legacy surface ------------------------------------------------------------


def test_fault_set(sim):
    assert {f.name for f in sim.fault_defs} == {
        "coin_jammed",
        "bill_offline",
        "changer_empty",
        "board_reboot",
        "drop_ack_window",
    }


async def test_stale_payout_without_policy_is_expired_not_no_policy(sim, client, clock):
    # Firmware order (Master::do_payout): expiry is checked before the policy.
    stale = clock.now() - timedelta(seconds=31)
    ack = await send(
        sim, client, "payout", {"cents": 100, "reason": "change"}, timestamp=stale
    )
    assert (ack.status, ack.detail) == ("rejected", "expired")


# --- firmware fidelity: gate order, reboot (final-fix A) -------------------------


async def _evict_ack_cache(sim, client):
    for _ in range(40):
        await send(sim, client, "ping")


async def test_repeated_payout_is_gated_before_the_stored_result_replay(sim, client):
    # Firmware Master::do_payout: expired, then no_policy, THEN find_payout.
    await armed(sim, client)
    for _ in range(3):
        await sim.insert_coin(client, 100)
    pay = {"cents": 100, "reason": "change"}
    await send(sim, client, "payout", pay, request_id="pay-gate01")
    await sim.drain_background()
    await _evict_ack_cache(sim, client)
    sim.policy.reset()
    client.clear()
    ack = await send(sim, client, "payout", pay, request_id="pay-gate01")
    await sim.drain_background()
    assert (ack.status, ack.detail) == ("rejected", "no_policy")
    assert client.kinds() == []  # no stored result republished


async def test_repeated_payout_that_is_expired_is_rejected_not_replayed(
    sim, client, clock
):
    await armed(sim, client)
    for _ in range(3):
        await sim.insert_coin(client, 100)
    pay = {"cents": 100, "reason": "change"}
    await send(sim, client, "payout", pay, request_id="pay-gate02")
    await sim.drain_background()
    await _evict_ack_cache(sim, client)
    client.clear()
    stale = clock.now() - timedelta(seconds=31)
    ack = await send(
        sim, client, "payout", pay, request_id="pay-gate02", timestamp=stale
    )
    await sim.drain_background()
    assert (ack.status, ack.detail) == ("rejected", "expired")
    assert client.kinds() == []


async def test_a_rejection_is_cached_by_request_id_like_the_firmware(sim, client):
    pay = {"cents": 100, "reason": "change"}
    first = await send(sim, client, "payout", pay, request_id="pay-rej001")
    assert (first.status, first.detail) == ("rejected", "no_policy")
    await armed(sim, client)
    again = await send(sim, client, "payout", pay, request_id="pay-rej001")
    assert (again.status, again.detail) == ("rejected", "no_policy")  # replayed
    fresh = await send(sim, client, "payout", pay, request_id="pay-rej002")
    assert fresh.status == "ok"
    await sim.drain_background()


async def test_reboot_zeroes_escrow(sim, client):
    await armed(sim, client)
    await sim.insert_coin(client, 25)
    await sim.reboot(client)
    assert client.retained("payment/state")[-1]["escrow_cents"] == 0


async def test_reboot_loses_the_ram_ack_cache_but_keeps_stored_results(sim, client):
    pay = {"cents": 100, "reason": "change"}
    # A rejection cached under the old boot ...
    rej = await send(sim, client, "payout", pay, request_id="pay-ram001")
    assert (rej.status, rej.detail) == ("rejected", "no_policy")
    await sim.reboot(client)
    await send(sim, client, "set_policy", policy())
    # ... is gone after the reboot: the same id is handled afresh.
    ok = await send(sim, client, "payout", pay, request_id="pay-ram001")
    assert ok.status == "ok"
    await sim.drain_background()
    assert client.kinds().count("payout_result") == 1
    # The persisted result survives a further reboot and is replayed.
    await sim.reboot(client)
    await send(sim, client, "set_policy", policy())
    client.clear()
    await send(sim, client, "payout", pay, request_id="pay-ram001")
    await sim.drain_background()
    assert client.kinds() == ["payout_result"]


async def test_reboot_drops_an_in_flight_payout_and_keeps_stored_results(sim, client):
    await armed(sim, client)
    for _ in range(4):
        await sim.insert_coin(client, 100)
    release = asyncio.Event()

    async def gated(_seconds):
        await release.wait()

    sim.set_sleep(gated)
    pay = {"cents": 100, "reason": "change"}
    await send(sim, client, "payout", pay, request_id="pay-fly001")
    tubes = [t.count for t in sim.cash.tubes]
    await sim.reboot(client)
    release.set()
    await sim.drain_background()
    assert [t.count for t in sim.cash.tubes] == tubes  # never completed
    assert "payout_result" not in client.kinds()
    await send(sim, client, "set_policy", policy())
    other = await send(sim, client, "payout", pay, request_id="pay-fly002")
    assert (other.status, other.phase) == ("ok", "accepted")  # not busy
    await sim.drain_background()
