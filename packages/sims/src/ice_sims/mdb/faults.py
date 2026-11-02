# ice_sims/mdb/faults.py
"""MDB-board fault injection, independent of the simulator class.

Each `InjectedFault` carries `activate`/`recover` coroutines that act on the
board only through an `MdbBoardPort`. The simulator converts them to
`ESP32Simulator`'s `FaultDef`s.
"""

from typing import Protocol

from ice_contracts.board.payment import Device, DeviceStatus
from ice_sims.base import InjectedFault

__all__ = ["InjectedFault", "MdbBoardPort", "mdb_faults"]


class MdbBoardPort(Protocol):
    async def set_device_status(self, device: Device, status: DeviceStatus) -> None: ...

    async def drain_tubes(self) -> None: ...

    async def restore_tubes(self) -> None: ...

    async def reboot(self) -> None: ...

    def set_ack_drop(self, dropping: bool) -> None: ...

    async def replay(self) -> None: ...


def _device_status(device: Device, fault_status: DeviceStatus) -> dict:
    """`activate`/`recover` that set *device* to *fault_status* and back to ready."""

    async def activate(board: MdbBoardPort) -> None:
        await board.set_device_status(device, fault_status)

    async def recover(board: MdbBoardPort) -> None:
        await board.set_device_status(device, DeviceStatus.ready)

    return {"activate": activate, "recover": recover}


async def _changer_empty_on(board: MdbBoardPort) -> None:
    await board.drain_tubes()
    await board.set_device_status(Device.coin, DeviceStatus.empty)


async def _changer_empty_off(board: MdbBoardPort) -> None:
    await board.restore_tubes()
    await board.set_device_status(Device.coin, DeviceStatus.ready)


async def _board_reboot_on(board: MdbBoardPort) -> None:
    await board.reboot()


async def _drop_ack_window_on(board: MdbBoardPort) -> None:
    board.set_ack_drop(True)


async def _drop_ack_window_off(board: MdbBoardPort) -> None:
    board.set_ack_drop(False)
    await board.replay()


def mdb_faults() -> list[InjectedFault]:
    return [
        InjectedFault(
            "coin_jammed",
            "short",
            0.0015,
            "Coin acceptor jammed - coins rejected",
            "warning",
            **_device_status(Device.coin, DeviceStatus.jammed),
        ),
        InjectedFault(
            "bill_offline",
            "short",
            0.0012,
            "Bill validator offline - bills not accepted",
            "warning",
            **_device_status(Device.bill, DeviceStatus.offline),
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
