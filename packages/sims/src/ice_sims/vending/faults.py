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

from ice_contracts.board.vending_machine import VendingChannel
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


async def _on_water_valve_stuck_open_activate(board: BoardPort) -> None:
    await board.set_hw("water_valve_solenoid", True)
    logger.warning("[vending] FAULT: water valve stuck open — flow incrementing")


async def _on_water_valve_stuck_open_recover(board: BoardPort) -> None:
    await board.set_hw("water_valve_solenoid", False)
    logger.info("[vending] Fault cleared: water_valve_stuck_open")


async def _on_ice_bin_empty_activate(board: BoardPort) -> None:
    # The IM600XL has no bin sensor: the fault shows up only as a fill that
    # runs to its maximum time (see `dispense`), so no channel is touched.
    logger.warning("[vending] FAULT: ice bin empty")


async def _on_ice_bin_empty_recover(board: BoardPort) -> None:
    logger.info("[vending] Fault cleared: ice_bin_empty")


async def _on_motor_stall_activate(board: BoardPort) -> None:
    logger.warning("[vending] FAULT: motor stall")


async def _on_motor_stall_recover(board: BoardPort) -> None:
    logger.info("[vending] Fault cleared: motor_stall")


async def _on_no_water_flow_activate(board: BoardPort) -> None:
    logger.warning("[vending] FAULT: no water flow detected")


async def _on_no_water_flow_recover(board: BoardPort) -> None:
    logger.info("[vending] Fault cleared: no_water_flow")


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


async def _on_ro_tank_empty_activate(board: BoardPort) -> None:
    board.ro_tank.fill_blocked = True
    # Drain through the board so the level switches are published.
    await board.draw_water(board.ro_tank.level_gal)
    logger.warning("[vending] FAULT: RO tank empty — no source water")


async def _on_ro_tank_empty_recover(board: BoardPort) -> None:
    board.ro_tank.fill_blocked = False
    logger.info("[vending] Fault cleared: ro_tank_empty")


async def _on_low_pressure_activate(board: BoardPort) -> None:
    await board.set_hw(VendingChannel.water_pressure_ok.value, False)
    logger.warning("[vending] FAULT: water supply pressure low")


async def _on_low_pressure_recover(board: BoardPort) -> None:
    await board.set_hw(VendingChannel.water_pressure_ok.value, True)
    logger.info("[vending] Fault cleared: low_pressure")


async def _on_sump_high_activate(board: BoardPort) -> None:
    await board.set_hw(VendingChannel.sump_high.value, True)
    logger.warning("[vending] FAULT: sump pit high level")


async def _on_sump_high_recover(board: BoardPort) -> None:
    await board.set_hw(VendingChannel.sump_high.value, False)
    logger.info("[vending] Fault cleared: sump_high")


async def _on_service_door_open_activate(board: BoardPort) -> None:
    await board.set_hw(VendingChannel.service_door.value, True)
    logger.warning("[vending] FAULT: service door open")


async def _on_service_door_open_recover(board: BoardPort) -> None:
    await board.set_hw(VendingChannel.service_door.value, False)
    logger.info("[vending] Fault cleared: service_door_open")


async def _on_ro_fill_stuck_activate(board: BoardPort) -> None:
    board.ro_tank.fill_stuck = True
    logger.warning("[vending] FAULT: RO fill stuck — pump runs, level does not rise")


async def _on_ro_fill_stuck_recover(board: BoardPort) -> None:
    board.ro_tank.fill_stuck = False
    # Stands in for a technician's power cycle clearing the latched cutoff.
    board.ro_tank.reset_cutoff()
    logger.info("[vending] Fault cleared: ro_fill_stuck")


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
        InjectedFault(
            name="ro_tank_empty",
            category="long",
            probability=0.0003,
            activate=_on_ro_tank_empty_activate,
            recover=_on_ro_tank_empty_recover,
            message="RO storage tank empty — no source water to refill it",
            severity="warning",
        ),
        InjectedFault(
            name="low_pressure",
            category="medium",
            probability=0.0004,
            activate=_on_low_pressure_activate,
            recover=_on_low_pressure_recover,
            message="Water supply pressure low",
            severity="warning",
        ),
        InjectedFault(
            name="sump_high",
            category="medium",
            probability=0.0003,
            activate=_on_sump_high_activate,
            recover=_on_sump_high_recover,
            message="Sump pit high level — possible leak",
            severity="warning",
        ),
        InjectedFault(
            name="service_door_open",
            category="short",
            probability=0.0006,
            activate=_on_service_door_open_activate,
            recover=_on_service_door_open_recover,
            message="Service door open",
            severity="warning",
        ),
        InjectedFault(
            name="ro_fill_stuck",
            category="long",
            probability=0.0002,
            activate=_on_ro_fill_stuck_activate,
            recover=_on_ro_fill_stuck_recover,
            message="RO fill stuck — pump runs but the tank level does not rise",
            severity="warning",
        ),
    ]
