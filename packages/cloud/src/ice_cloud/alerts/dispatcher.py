"""Alert delivery: route events to people, bundle per recipient, honour cooldown (spec 3.3)."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from datetime import datetime, timedelta

from ice_contracts.cloud.signing import short_fingerprint
from loguru import logger

from ice_cloud.alerts.mail import Mailer, render_email
from ice_cloud.alerts.router import muted_ids, recipients_for
from ice_cloud.store import Delivery, Machine, Repository, StoredEvent
from ice_cloud.store.repository import Recipient

COOLDOWN_SECONDS = 600
MAX_ATTEMPTS = 5
_ERROR_LIMIT = 500


class _MailEvent:
    """A stored event as the mail renderer sees it (the row has no scope)."""

    scope = None

    def __init__(self, event: StoredEvent) -> None:
        self.kind = event.kind
        self.at = event.at
        self.code = event.code
        self.severity = event.severity
        self.description = event.description
        self.sku = event.sku
        self.product = event.product


class AlertDispatcher:
    def __init__(
        self,
        repo: Repository,
        mailer: Mailer,
        *,
        now: Callable[[], datetime],
        public_url: str | None,
        from_addr: str,
    ) -> None:
        self._repo = repo
        self._mailer = mailer
        self._now = now
        self._public_url = public_url
        self._from_addr = from_addr

    async def dispatch(
        self, fingerprint: str, machine: Machine, events: Sequence[StoredEvent]
    ) -> None:
        if not events:
            return
        now = self._now()
        people = await asyncio.to_thread(self._repo.people_for_alerts, fingerprint)
        by_id = {p.account_id: p for p, _ in people}
        routed = recipients_for(events, people)

        muted = muted_ids(people)
        unmuted = [(p, False) for p, _ in people if p.account_id in muted]
        for account_id, mine in recipients_for(events, unmuted).items():
            await asyncio.to_thread(
                self._repo.create_delivery,
                fingerprint,
                account_id,
                [e.id for e in mine],
                "muted",
                now,
            )

        for account_id, mine in routed.items():
            person = by_id[account_id]
            delivery = await asyncio.to_thread(
                self._repo.create_delivery,
                fingerprint,
                account_id,
                [e.id for e in mine],
                "pending",
                now,
            )
            if await self._cooled_down(fingerprint, account_id, now):
                await self._attempt(delivery, person, machine, mine)
            else:
                logger.info(
                    "Alert for {} held by cooldown ({} event(s))", account_id, len(mine)
                )

    async def flush(self, now: datetime) -> None:
        due = await asyncio.to_thread(self._repo.pending_deliveries, before=now)
        for delivery in due:
            try:
                await self._flush_one(delivery, now)
            except Exception:
                logger.exception("Alert flush failed for delivery {}", delivery.id)

    async def _flush_one(self, delivery: Delivery, now: datetime) -> None:
        fp = delivery.fingerprint
        if not await self._cooled_down(fp, delivery.account_id, now):
            return
        machine = await asyncio.to_thread(self._repo.machine, fp)
        people = await asyncio.to_thread(self._repo.people_for_alerts, fp)
        entry = next(
            (x for x in people if x[0].account_id == delivery.account_id), None
        )
        if machine is None or entry is None:
            await asyncio.to_thread(
                self._repo.mark_delivery,
                delivery.id,
                status="failed",
                now=now,
                error="machine or recipient no longer available",
            )
            return
        person, is_muted = entry
        if is_muted:
            await asyncio.to_thread(
                self._repo.mark_delivery, delivery.id, status="muted", now=now
            )
            return
        mine = await asyncio.to_thread(
            self._repo.alert_events_by_ids, delivery.event_ids
        )
        await self._attempt(delivery, person, machine, mine)

    async def _cooled_down(
        self, fingerprint: str, account_id: str, now: datetime
    ) -> bool:
        last = await asyncio.to_thread(self._repo.last_sent_at, fingerprint, account_id)
        return last is None or last <= now - timedelta(seconds=COOLDOWN_SECONDS)

    async def _attempt(
        self,
        delivery: Delivery,
        person: Recipient,
        machine: Machine,
        triggering: Sequence[StoredEvent],
    ) -> None:
        fp = delivery.fingerprint
        try:
            cleared = await asyncio.to_thread(
                self._repo.cleared_since_last_sent, fp, person.account_id
            )
            state = await asyncio.to_thread(self._repo.alert_state, fp)
            msg = render_email(
                label=machine.label,
                short=short_fingerprint(fp),
                role=person.role,
                triggering=[_MailEvent(e) for e in triggering],
                cleared=[_MailEvent(e) for e in cleared],
                public_url=self._public_url,
                from_addr=self._from_addr,
                to=person.email,
                last_report_at=state.last_report_at if state else None,
            )
            await self._mailer.send(msg)
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"[:_ERROR_LIMIT]
            logger.warning("Alert email to {} failed: {}", person.account_id, error)
            await asyncio.to_thread(
                self._repo.mark_delivery,
                delivery.id,
                status="failed",
                now=self._now(),
                error=error,
            )
            return
        await asyncio.to_thread(
            self._repo.mark_delivery, delivery.id, status="sent", now=self._now()
        )
        logger.info("Alert email sent to {} for {}", person.account_id, fp[:8])
