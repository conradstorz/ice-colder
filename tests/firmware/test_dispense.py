"""dispense with the ReleaseByCatch ice profile."""

import json

import pytest

from ice_contracts.board.vending_machine import DispenseStep, DispenserOutcome
from tests.repo_root import REPO_ROOT

PROFILE = json.loads(
    (REPO_ROOT / "firmware/vending/test/fixtures/profile_ice_catch.json").read_text(
        encoding="utf-8"
    )
)
PARAMS = {"slot": 0, "mechanism": "bagged_ice", "profile": PROFILE}
DISPENSER = "hardware/dispenser"
OUTCOMES = {o.value for o in DispenserOutcome}


def _mine(rid: str):
    return lambda p: p.get("request_id") == rid


async def test_accepted_then_agitate_step_starts(board):
    since = board.mark()
    rid = await board.send("dispense", PARAMS)
    (ack,) = await board.acks(rid, 1, 5)
    assert (ack.status, ack.phase) == ("ok", "accepted")
    step = await board.wait_for(DISPENSER, _mine(rid), 10, since=since)
    assert step["state"] == DispenseStep.agitate.value
    # Let the run finish so the next test finds an idle board (the terminal
    # report is only asserted below, where the simulator drives the sensors).
    await board.wait_for(
        DISPENSER, lambda p: _mine(rid)(p) and p["state"] in OUTCOMES, 120, since=since
    )


@pytest.mark.requires_sim
async def test_steps_in_order_then_one_complete_report(board):
    since = board.mark()
    rid = await board.send("dispense", PARAMS)
    terminal = await board.wait_for(
        DISPENSER, lambda p: _mine(rid)(p) and p["state"] in OUTCOMES, 120, since=since
    )
    assert terminal["state"] == DispenserOutcome.complete.value
    states = [p["state"] for p in board.history(DISPENSER, since) if _mine(rid)(p)]
    steps = [s for s in states if s not in OUTCOMES]
    assert steps == [s.value for s in DispenseStep if s.value in steps]
    assert steps[0] == DispenseStep.agitate.value
    assert states[-1] == DispenserOutcome.complete.value
    assert sum(s in OUTCOMES for s in states) == 1


async def test_second_dispense_while_busy_is_rejected(board):
    since = board.mark()
    first = await board.send("dispense", PARAMS)
    await board.acks(first, 1, 5)
    second = await board.send("dispense", PARAMS)
    (ack,) = await board.acks(second, 1, 5)
    assert ack.status == "rejected"
    assert "busy" in (ack.detail or "")
    await board.wait_for(
        DISPENSER,
        lambda p: _mine(first)(p) and p["state"] in OUTCOMES,
        120,
        since=since,
    )
