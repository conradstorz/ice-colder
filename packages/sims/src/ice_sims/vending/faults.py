# ice_sims/vending/faults.py
"""Vending-board fault injection, independent of the simulator class.

The faults are `InjectedFault` rows (`ice_sims.base`); only the two that move
the RO tank model need real `activate`/`recover` coroutines, the rest are
alerts or drive one channel (`set_channel`). Actions reach the board only
through a `BoardPort`. The simulator converts the rows to `FaultDef`s.
"""

from ice_contracts.board.vending_machine import VendingChannel
from ice_sims.base import InjectedFault, set_channel
from ice_sims.vending.board import BoardPort

__all__ = ["InjectedFault", "vending_faults"]


async def _ro_tank_empty_activate(board: BoardPort) -> None:
    board.ro_tank.fill_blocked = True
    # Drain through the board so the level switches are published.
    await board.draw_water(board.ro_tank.level_gal)


async def _ro_tank_empty_recover(board: BoardPort) -> None:
    board.ro_tank.fill_blocked = False


async def _ro_fill_stuck_activate(board: BoardPort) -> None:
    board.ro_tank.fill_stuck = True


async def _ro_fill_stuck_recover(board: BoardPort) -> None:
    board.ro_tank.fill_stuck = False
    # Stands in for a technician's power cycle clearing the latched cutoff.
    board.ro_tank.reset_cutoff()


def vending_faults() -> list[InjectedFault]:
    """The vending board's injectable faults, in registration order."""
    return [
        InjectedFault(
            name="auger_jam",
            category="medium",
            probability=0.001,
            message="Auger motor jammed — ice bag fill timed out",
            severity="warning",
        ),
        InjectedFault(
            name="water_valve_stuck_open",
            category="short",
            probability=0.0012,
            **set_channel("water_valve_solenoid", True),
            message="Water valve stuck open — water flowing continuously",
            severity="critical",
        ),
        InjectedFault(
            # The IM600XL has no bin sensor: the fault shows up only as a
            # fill that runs to its maximum time (see `dispense`), so no
            # channel is touched.
            name="ice_bin_empty",
            category="medium",
            probability=0.0006,
            message="Ice bin empty — refill required",
            severity="warning",
        ),
        InjectedFault(
            name="motor_stall",
            category="medium",
            probability=0.0006,
            message="Dispense motor stalled",
            severity="warning",
        ),
        InjectedFault(
            name="no_water_flow",
            category="short",
            probability=0.0008,
            message="No water flow detected after valve opened",
            severity="warning",
        ),
        InjectedFault(
            name="flow_runaway",
            category="short",
            probability=0.0006,
            message="Water flow continued well past target volume",
            severity="warning",
        ),
        InjectedFault(
            name="slow_flow",
            category="short",
            probability=0.0006,
            message=(
                "Water flow modestly slow (just above the minimum) — "
                "may time out before reaching the target volume"
            ),
            severity="warning",
        ),
        InjectedFault(
            name="low_flow",
            category="short",
            # Copilot review (PR #32) finding C7: deliberately 0 --
            # this fault exists to be triggered explicitly (a test, or
            # an operator via the Tests level), not rolled at random.
            # It represents a flow rate *below* min_flow_ml_per_second,
            # distinct from slow_flow (just above the minimum, which
            # may or may not reach the target in time) and from
            # no_water_flow (zero flow, the other way a fill can be
            # starved of water).
            probability=0.0,
            message=(
                "Water flow below min_flow_ml_per_second — fill will "
                "fault as no_flow once no_flow_grace_seconds elapses"
            ),
            severity="warning",
        ),
        InjectedFault(
            name="ro_tank_empty",
            category="long",
            probability=0.0003,
            activate=_ro_tank_empty_activate,
            recover=_ro_tank_empty_recover,
            message="RO storage tank empty — no source water to refill it",
            severity="warning",
        ),
        InjectedFault(
            name="low_pressure",
            category="medium",
            probability=0.0004,
            **set_channel(VendingChannel.water_pressure_ok.value, False),
            message="Water supply pressure low",
            severity="warning",
        ),
        InjectedFault(
            name="sump_high",
            category="medium",
            probability=0.0003,
            **set_channel(VendingChannel.sump_high.value, True),
            message="Sump pit high level — possible leak",
            severity="warning",
        ),
        InjectedFault(
            name="service_door_open",
            category="short",
            probability=0.0006,
            **set_channel(VendingChannel.service_door.value, True),
            message="Service door open",
            severity="warning",
        ),
        InjectedFault(
            name="ro_fill_stuck",
            category="long",
            probability=0.0002,
            activate=_ro_fill_stuck_activate,
            recover=_ro_fill_stuck_recover,
            message="RO fill stuck — pump runs but the tank level does not rise",
            severity="warning",
        ),
    ]
