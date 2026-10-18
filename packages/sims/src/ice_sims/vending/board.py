# ice_sims/vending/board.py
"""The narrow hardware/publish surface the vending simulator's behaviour
modules (dispense, customer, faults) talk to, instead of the simulator
class. No I/O; the only package imports are the two pure models its handles are typed with."""

from typing import Protocol

from pydantic import BaseModel

from ice_sims.vending.climate import Climate
from ice_sims.vending.ro_tank import RoTank


class BoardPort(Protocol):
    subsystem_name: str
    ro_tank: RoTank
    climate: Climate

    async def set_hw(self, device: str, state: bool) -> None: ...

    async def draw_water(self, gallons: float) -> None: ...

    async def publish(self, topic_suffix: str, payload: BaseModel) -> None: ...

    async def sleep(self, seconds: float) -> None: ...

    def active_faults(self) -> set[str]: ...
