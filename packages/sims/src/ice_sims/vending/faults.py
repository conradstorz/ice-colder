# ice_sims/vending/faults.py
"""Vending-board fault injection, independent of the simulator class.

Each `InjectedFault` carries its own `activate`/`recover` coroutines that
act on the board only through a `BoardPort`. The simulator converts them
to `ESP32Simulator`'s `FaultDef`s.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal

from loguru import logger

from ice_sims.vending.board import BoardPort

FaultAction = Callable[[BoardPort], Awaitable[None]]


@dataclass(frozen=True)
class InjectedFault:
    name: str
    category: Literal["short", "medium", "long"]
    probability: float
    message: str
    severity: str
    activate: FaultAction
    recover: FaultAction


# --- Fault activate/recover actions ---


async def _on_auger_jam_activate(board: BoardPort) -> None:
    logger.warning("[vending] FAULT: auger jam — bag fill will time out")


async def _on_auger_jam_recover(board: BoardPort) -> None:
    logger.info("[vending] Fault cleared: auger_jam")


async def _on_bag_drop_solenoid_stuck_activate(board: BoardPort) -> None:
    logger.warning("[vending] FAULT: bag drop solenoid stuck")


async def _on_bag_drop_solenoid_stuck_recover(board: BoardPort) -> None:
    await board.set_hw("bag_full_sensor", False)
    await board.set_hw("bag_drop_solenoid", False)
    logger.info("[vending] Fault cleared: bag_drop_solenoid_stuck")


async def _on_water_valve_stuck_open_activate(board: BoardPort) -> None:
    await board.set_hw("water_valve_solenoid", True)
    await board.set_hw("water_flow_sensor", True)
    logger.warning("[vending] FAULT: water valve stuck open — flow incrementing")


async def _on_water_valve_stuck_open_recover(board: BoardPort) -> None:
    await board.set_hw("water_valve_solenoid", False)
    await board.set_hw("water_flow_sensor", False)
    logger.info("[vending] Fault cleared: water_valve_stuck_open")


async def _on_ice_bin_empty_activate(board: BoardPort) -> None:
    await board.set_hw("bin_half_full", False)
    logger.warning("[vending] FAULT: ice bin empty")


async def _on_ice_bin_empty_recover(board: BoardPort) -> None:
    await board.set_hw("bin_half_full", True)
    logger.info("[vending] Fault cleared: ice_bin_empty")


async def _on_motor_stall_activate(board: BoardPort) -> None:
    logger.warning("[vending] FAULT: motor stall")


async def _on_motor_stall_recover(board: BoardPort) -> None:
    logger.info("[vending] Fault cleared: motor_stall")


async def _on_no_water_flow_activate(board: BoardPort) -> None:
    logger.warning("[vending] FAULT: no water flow detected")


async def _on_no_water_flow_recover(board: BoardPort) -> None:
    logger.info("[vending] Fault cleared: no_water_flow")


async def _on_door_stuck_open_activate(board: BoardPort) -> None:
    logger.warning("[vending] FAULT: release gate door stuck open")


async def _on_door_stuck_open_recover(board: BoardPort) -> None:
    logger.info("[vending] Fault cleared: door_stuck_open")


async def _on_flow_runaway_activate(board: BoardPort) -> None:
    logger.warning("[vending] FAULT: water flow runaway past target volume")


async def _on_flow_runaway_recover(board: BoardPort) -> None:
    logger.info("[vending] Fault cleared: flow_runaway")


async def _on_slow_flow_activate(board: BoardPort) -> None:
    logger.warning("[vending] FAULT: water flow modestly slow")


async def _on_slow_flow_recover(board: BoardPort) -> None:
    logger.info("[vending] Fault cleared: slow_flow")


async def _on_low_flow_activate(board: BoardPort) -> None:
    logger.warning("[vending] FAULT: water flow below minimum")


async def _on_low_flow_recover(board: BoardPort) -> None:
    logger.info("[vending] Fault cleared: low_flow")


def vending_faults() -> list[InjectedFault]:
    """The vending board's injectable faults, in registration order."""
    return [
        InjectedFault(
            name="auger_jam",
            category="medium",
            probability=0.001,
            activate=_on_auger_jam_activate,
            recover=_on_auger_jam_recover,
            message="Auger motor jammed — ice bag fill timed out",
            severity="warning",
        ),
        InjectedFault(
            name="bag_drop_solenoid_stuck",
            category="medium",
            probability=0.0008,
            activate=_on_bag_drop_solenoid_stuck_activate,
            recover=_on_bag_drop_solenoid_stuck_recover,
            message="Bag drop solenoid stuck — bag not releasing",
            severity="warning",
        ),
        InjectedFault(
            name="water_valve_stuck_open",
            category="short",
            probability=0.0012,
            activate=_on_water_valve_stuck_open_activate,
            recover=_on_water_valve_stuck_open_recover,
            message="Water valve stuck open — water flowing continuously",
            severity="critical",
        ),
        InjectedFault(
            name="ice_bin_empty",
            category="medium",
            probability=0.0006,
            activate=_on_ice_bin_empty_activate,
            recover=_on_ice_bin_empty_recover,
            message="Ice bin empty — refill required",
            severity="warning",
        ),
        InjectedFault(
            name="motor_stall",
            category="medium",
            probability=0.0006,
            activate=_on_motor_stall_activate,
            recover=_on_motor_stall_recover,
            message="Dispense motor stalled",
            severity="warning",
        ),
        InjectedFault(
            name="no_water_flow",
            category="short",
            probability=0.0008,
            activate=_on_no_water_flow_activate,
            recover=_on_no_water_flow_recover,
            message="No water flow detected after valve opened",
            severity="warning",
        ),
        InjectedFault(
            name="door_stuck_open",
            category="medium",
            probability=0.0006,
            activate=_on_door_stuck_open_activate,
            recover=_on_door_stuck_open_recover,
            message="Release gate door stuck open",
            severity="critical",
        ),
        InjectedFault(
            name="flow_runaway",
            category="short",
            probability=0.0006,
            activate=_on_flow_runaway_activate,
            recover=_on_flow_runaway_recover,
            message="Water flow continued well past target volume",
            severity="warning",
        ),
        InjectedFault(
            name="slow_flow",
            category="short",
            probability=0.0006,
            activate=_on_slow_flow_activate,
            recover=_on_slow_flow_recover,
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
            activate=_on_low_flow_activate,
            recover=_on_low_flow_recover,
            message=(
                "Water flow below min_flow_ml_per_second — fill will "
                "fault as no_flow once no_flow_grace_seconds elapses"
            ),
            severity="warning",
        ),
    ]
