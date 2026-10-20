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

    async def tick(self) -> list[StoredEvent]:
        """One pass: flush due rows first, then emit `offline` once per silent machine."""
        await self._dispatcher.flush(self._now())
        pairs = await asyncio.to_thread(self._repo.claimed_machines_with_state)
        emitted: list[StoredEvent] = []
        for machine, state in pairs:
            try:
                emitted.extend(await self._check(machine, state))
            except Exception:
                logger.exception("Offline sweep failed for {}", machine.fingerprint[:8])
        return emitted

    async def _check(self, machine: Machine, state: AlertState) -> list[StoredEvent]:
        now = self._now()
        limit = timedelta(seconds=OFFLINE_INTERVALS * state.report_interval_seconds)
        if state.offline_since is not None or now - state.last_report_at <= limit:
            return []
        # Persist the mark before dispatching so a crash cannot emit twice.
        await asyncio.to_thread(
            self._repo.put_alert_state, replace(state, offline_since=now)
        )
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
        stored = await asyncio.to_thread(self._repo.record_alert_events, [row])
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
