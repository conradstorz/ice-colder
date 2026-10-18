# ice_sims/vending/board.py
"""The narrow hardware/publish surface the vending simulator's behaviour
modules (dispense, customer, faults) talk to, instead of the simulator
class. No I/O and no imports from the rest of the package."""

import asyncio
from collections.abc import Coroutine
from typing import Any, Protocol

from pydantic import BaseModel


class BoardPort(Protocol):
    subsystem_name: str

    async def set_hw(self, device: str, state: bool) -> None: ...

    async def publish(self, topic_suffix: str, payload: BaseModel) -> None: ...

    async def publish_current(
        self, channel: str, amps: float
    ) -> None: ...  # removed in Task 10

    async def sleep(self, seconds: float) -> None: ...

    def spawn(self, coro: Coroutine[Any, Any, None]) -> asyncio.Task: ...

    def active_faults(self) -> set[str]: ...
