"""Offline sweep: flag machines that stopped reporting (spec 2.3)."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import datetime, timedelta

from loguru import logger

from ice_cloud.alerts.dispatcher import AlertDispatcher
from ice_cloud.store import AlertEventRow, Machine, Repository, StoredEvent
from ice_cloud.store.repository import AlertState

SWEEP_SECONDS = 60
OFFLINE_INTERVALS = 3


class OfflineSweep:
    """Flags machines silent for more than 3 report intervals, once each.

    Startup grace: the sweep records when it was built and never flags a machine
    while `now - started_at <= 3 * that machine's interval`, so a cloud restart
    after downtime does not email the whole fleet "offline" and then "online".
    """

    def __init__(
        self,
        repo: Repository,
        dispatcher: AlertDispatcher,
        *,
        now: Callable[[], datetime],
    ) -> None:
        self._repo = repo
        self._dispatcher = dispatcher
        self._now = now
        self._started_at = now()

    async def tick(self) -> list[StoredEvent]:
        """One pass: emit `offline` once per silent machine, then flush leftovers.

        Dispatch folds a recipient's due rows into the new email, so the older
        rows and the offline event leave together; flushing first would send the
        older rows alone and leave the offline event held by the cooldown.
        """
        pairs = await asyncio.to_thread(self._repo.claimed_machines_with_state)
        emitted: list[StoredEvent] = []
        for machine, state in pairs:
            try:
                emitted.extend(await self._check(machine, state))
            except Exception:
                logger.exception("Offline sweep failed for {}", machine.fingerprint[:8])
        await self._dispatcher.flush(self._now())
        return emitted

    async def _check(self, machine: Machine, state: AlertState) -> list[StoredEvent]:
        now = self._now()
        limit = timedelta(seconds=OFFLINE_INTERVALS * state.report_interval_seconds)
        if state.offline_since is not None or now - state.last_report_at <= limit:
            return []
        if now - self._started_at <= limit:
            return []
        row = AlertEventRow(
            fingerprint=machine.fingerprint,
            kind="offline",
            at=now,
            key=None,
            code=None,
            severity=None,
            gate=None,
            description=None,
            sku=None,
            product=None,
        )
        # Event and mark commit together, before dispatching, so a crash cannot emit twice.
        stored = await asyncio.to_thread(
            self._repo.record_alert_pass, [row], replace(state, offline_since=now)
        )
        await self._dispatcher.dispatch(machine.fingerprint, machine, stored)
        return stored

    async def run(
        self, sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
    ) -> None:
        while True:
            try:
                await self.tick()
            except Exception:
                logger.exception("Offline sweep pass failed")
            await sleep(SWEEP_SECONDS)
