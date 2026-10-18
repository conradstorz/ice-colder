"""What each simulator advertises, against the edge's test-command allowlist.

Moved out of `tests/edge/web/test_routes_tests.py` (Copilot review, PR 22):
a hand-picked commands list in an edge route test would pass whether or not
the real simulators advertise `ping`/`self_test`, so the check that joins
the two sides lives here, where naming both is allowed. Needs no broker."""

import pytest

from ice_edge.web.routes.tests_level.commands import (
    testable_commands as allowed_test_commands,
)
from ice_sims.ice_maker import IceMakerSimulator
from ice_sims.mdb_gateway import MDBGatewaySimulator
from ice_sims.vending_machine import VendingMachineSimulator


@pytest.mark.parametrize(
    ("subsystem", "simulator"),
    [
        ("vending", VendingMachineSimulator),
        ("mdb", MDBGatewaySimulator),
        ("ice_maker", IceMakerSimulator),
    ],
)
def test_real_simulator_advertises_the_automatic_test_commands(subsystem, simulator):
    """Run all executes `ping` then `self_test` against each alive
    subsystem, using advertised ∩ allowlist. Before the original fix that
    intersection was empty for all three, so Run all executed nothing."""
    advertised = simulator(machine_id="vmc-t").build_capabilities().commands

    assert {"ping", "self_test"} <= allowed_test_commands(subsystem, advertised)
