from dataclasses import dataclass

import pytest

from ice_cloud.alerts.router import Recipient, muted_ids, recipients_for


@dataclass(frozen=True)
class Ev:
    kind: str
    gate: str | None = None


OWNER = Recipient("o1", "o@example.com", "owner")
TECH = Recipient("t1", "t@example.com", "tech")


@pytest.mark.parametrize(
    ("event", "owner_gets", "tech_gets"),
    [
        (Ev("raised", "safety"), True, True),
        (Ev("raised", "fulfillment"), True, True),
        (Ev("raised", "alert"), True, False),
        (Ev("offline"), True, True),
        (Ev("online"), True, True),
        (Ev("cleared", "safety"), False, False),
        (Ev("cleared", "alert"), False, False),
    ],
)
def test_role_table(event, owner_gets, tech_gets):
    got = recipients_for([event], [(OWNER, False), (TECH, False)])
    assert (got.get("o1") == [event]) is owner_gets
    assert (got.get("t1") == [event]) is tech_gets
    assert set(got) == {a for a, g in (("o1", owner_gets), ("t1", tech_gets)) if g}


def test_cleared_never_triggers():
    assert recipients_for([Ev("cleared", "safety")], [(OWNER, False)]) == {}


def test_muted_gets_nothing():
    got = recipients_for([Ev("raised", "safety")], [(OWNER, True), (TECH, False)])
    assert set(got) == {"t1"}


def test_empty_when_no_triggering_events():
    assert recipients_for([], [(OWNER, False)]) == {}


def test_tech_skips_alert_gate_owner_gets_it():
    e1, e2 = Ev("raised", "alert"), Ev("raised", "safety")
    got = recipients_for([e1, e2], [(OWNER, False), (TECH, False)])
    assert got == {"o1": [e1, e2], "t1": [e2]}


def test_muted_ids():
    assert muted_ids([(OWNER, True), (TECH, False)]) == {"o1"}


def test_everyone_muted_routes_nothing():
    got = recipients_for([Ev("raised", "safety")], [(OWNER, True), (TECH, True)])
    assert got == {}
    assert muted_ids([(OWNER, True), (TECH, True)]) == {"o1", "t1"}
