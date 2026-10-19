"""MDB board conformance: hardware only (no MDB simulator until spec 2).

Runs after the parent conftest's collection hook. The parent applies the
opt-in skip when ``ICE_COLDER_BOARD_TESTS`` is unset; this one skips every mdb
test except the golden-wire test when the flag is set but no hardware target
(``ICE_COLDER_BOARD_MACHINE_ID``) is given. It deliberately imports no
simulator: there is none for the MDB board.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tests.firmware.board_client import BoardClient, broker_address

NEEDS_HARDWARE_REASON = (
    "MDB board conformance needs hardware until the MDB simulator ships "
    "(set ICE_COLDER_BOARD_MACHINE_ID)"
)
_HERE = Path(__file__).parent


def pytest_collection_modifyitems(config, items):
    enabled = os.environ.get("ICE_COLDER_BOARD_TESTS") == "1"
    has_hardware = bool(os.environ.get("ICE_COLDER_BOARD_MACHINE_ID"))
    if not enabled or has_hardware:
        return
    for item in items:
        path = Path(str(item.fspath))
        if _HERE in path.parents and path.name != "test_golden_wire.py":
            item.add_marker(pytest.mark.skip(reason=NEEDS_HARDWARE_REASON))


@pytest.fixture
async def board():
    host, port = broker_address()
    machine_id = os.environ["ICE_COLDER_BOARD_MACHINE_ID"]
    async with BoardClient(host, port, machine_id, subsystem="mdb") as client:
        yield client
