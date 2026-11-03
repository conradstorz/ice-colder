# ice_sims/mdb/faults.py
"""MDB-board fault injection, independent of the simulator class.

Each `InjectedFault` carries `activate`/`recover` coroutines that act on the
board only through an `MdbBoardPort`. The simulator converts them to
`ESP32Simulator`'s `FaultDef`s.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal, Protocol

from loguru import logger

from ice_contracts.board.payment import Device, DeviceStatus


class MdbBoardPort(Protocol):
    async def set_device_status(self, device: Device, status: DeviceStatus) -> None: ...

    async def drain_tubes(self) -> None: ...

    async def restore_tubes(self) -> None: ...

    async def reboot(self) -> None: ...

    def set_ack_drop(self, dropping: bool) -> None: ...

    async def replay(self) -> None: ...


FaultAction = Callable[[MdbBoardPort], Awaitable[None]]


@dataclass(frozen=True)
class InjectedFault:
    name: str
    category: Literal["short", "medium", "long"]
    probability: float
    message: str
    severity: str
    activate: FaultAction
    recover: FaultAction


async def _coin_jammed_on(board: MdbBoardPort) -> None:
    await board.set_device_status(Device.coin, DeviceStatus.jammed)
    logger.warning("[mdb] FAULT: coin acceptor jammed")


async def _coin_jammed_off(board: MdbBoardPort) -> None:
    await board.set_device_status(Device.coin, DeviceStatus.ready)
    logger.info("[mdb] Fault cleared: coin_jammed")


async def _bill_offline_on(board: MdbBoardPort) -> None:
    await board.set_device_status(Device.bill, DeviceStatus.offline)
    logger.warning("[mdb] FAULT: bill validator offline")


async def _bill_offline_off(board: MdbBoardPort) -> None:
    await board.set_device_status(Device.bill, DeviceStatus.ready)
    logger.info("[mdb] Fault cleared: bill_offline")


async def _changer_empty_on(board: MdbBoardPort) -> None:
    await board.drain_tubes()
    await board.set_device_status(Device.coin, DeviceStatus.empty)
    logger.warning("[mdb] FAULT: changer empty - payouts will fail or fall short")


async def _changer_empty_off(board: MdbBoardPort) -> None:
    await board.restore_tubes()
    await board.set_device_status(Device.coin, DeviceStatus.ready)
    logger.info("[mdb] Fault cleared: changer_empty")


async def _board_reboot_on(board: MdbBoardPort) -> None:
    await board.reboot()
    logger.warning("[mdb] FAULT: board rebooted - new boot_id, policy lost")


async def _board_reboot_off(board: MdbBoardPort) -> None:
    logger.info("[mdb] Fault cleared: board_reboot")


async def _drop_ack_window_on(board: MdbBoardPort) -> None:
    board.set_ack_drop(True)
    logger.warning("[mdb] FAULT: event acks are being dropped")


async def _drop_ack_window_off(board: MdbBoardPort) -> None:
    board.set_ack_drop(False)
    await board.replay()
    logger.info("[mdb] Fault cleared: drop_ack_window (unacked log replayed)")


def mdb_faults() -> list[InjectedFault]:
    return [
        InjectedFault(
            "coin_jammed",
            "short",
            0.0015,
            "Coin acceptor jammed - coins rejected",
            "warning",
            _coin_jammed_on,
            _coin_jammed_off,
        ),
        InjectedFault(
            "bill_offline",
            "short",
            0.0012,
            "Bill validator offline - bills not accepted",
            "warning",
            _bill_offline_on,
            _bill_offline_off,
        ),
        InjectedFault(
            "changer_empty",
            "medium",
            0.0003,
            "Coin changer empty - payouts cannot be paid in full",
            "warning",
            _changer_empty_on,
            _changer_empty_off,
        ),
        InjectedFault(
            "board_reboot",
            "short",
            0.0002,
            "MDB board rebooted - policy lost, unacked events replay",
            "warning",
            _board_reboot_on,
            _board_reboot_off,
        ),
        InjectedFault(
            "drop_ack_window",
            "short",
            0.0005,
            "Event acks dropped - events will be replayed",
            "warning",
            _drop_ack_window_on,
            _drop_ack_window_off,
        ),
    ]
