"""The supervised uplink task: UI bundle PUT first, then reports forever."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from loguru import logger

from ice_common.config_model import ConfigModel
from ice_contracts.cloud.identity import ClaimCodeNotice
from ice_edge.uplink.client import HttpUplink
from ice_edge.uplink.identity import MachineIdentity
from ice_edge.uplink.report import build_machine_report
from ice_ui.bundle import build_bundle, ui_digest

if TYPE_CHECKING:
    from ice_edge.app.machine import Machine
    from ice_edge.sale.maintenance_lease import MaintenanceHold


def utc_now() -> datetime:
    return datetime.now(UTC)


async def _send_notice(uplink: HttpUplink, identity: MachineIdentity) -> bool:
    """Deliver the pending claim-code notice (or its DELETE) once; ack on 2xx.

    Returns False only when a delivery was attempted and failed.
    """
    notice = identity.notice
    if notice is None or notice.acked:
        return True
    if notice.claim_code_hash is None:
        ok = await uplink.delete_claim_code()
    else:
        expires = (
            datetime.fromisoformat(notice.expires_at) if notice.expires_at else None
        )
        ok = await uplink.post_claim_code(
            ClaimCodeNotice(claim_code_hash=notice.claim_code_hash, expires_at=expires)
        )
    if ok:
        identity.mark_notice_acked(notice)
    return ok


async def run_uplink(
    machine: Machine,
    config: ConfigModel,
    uplink: HttpUplink,
    *,
    identity: MachineIdentity,
    sleep: Callable[[float], Awaitable[object]] = asyncio.sleep,
    now: Callable[[], datetime] = utc_now,
    holder_name: Callable[[MaintenanceHold], str | None] | None = None,
) -> None:
    """Each pass: register (until accepted; nothing else is sent before), retry
    an unacknowledged claim-code notice, try the UI bundle upload (retried each pass
    until accepted; a 401 or 409 refusal is retried only after
    report_interval_seconds * 60 or a new registration; the cloud shows the
    installed UI with a mismatch banner meanwhile), then always post a report. The interval is re-read every pass. ``seq`` increments
    per report attempt; ``boot_id`` is fixed per call. Failures are logged and
    dropped; only cancellation ends the loop."""
    boot_id = str(uuid.uuid4())
    digest, bundle = ui_digest(), build_bundle()
    registered = False  # reset on a 401: the cloud no longer knows this machine
    bundle_sent = False
    ever_registered = False
    next_bundle_attempt: datetime | None = None
    seq = 0

    while True:
        try:
            if not registered:
                registered = await uplink.register(config.machine_id)
                if registered:
                    last = uplink.last_register
                    identity.alerts_handled = bool(last and last.alerts_handled)
                    next_bundle_attempt = None
                    if ever_registered:
                        # Registering again means the cloud forgot us (401): it
                        # needs the bundle and the live claim notice once more.
                        bundle_sent = False
                        identity.reset_ack()
                    ever_registered = True
            if registered:
                try:
                    if not await _send_notice(uplink, identity):
                        registered = registered and uplink.last_status != 401
                except Exception:
                    logger.exception("Claim-code notice attempt failed")
                if not bundle_sent and (
                    next_bundle_attempt is None or now() >= next_bundle_attempt
                ):
                    try:
                        bundle_sent = await uplink.put_ui_bundle(digest, bundle)
                        if not bundle_sent and uplink.last_status in (401, 409):
                            # Refused outright (unclaimed, or at the cap): retry
                            # roughly hourly, not every pass.
                            next_bundle_attempt = now() + timedelta(
                                seconds=config.cloud.report_interval_seconds * 60
                            )
                    except Exception:
                        logger.exception("UI bundle upload attempt failed")
                report = await build_machine_report(
                    machine,
                    config=config,
                    seq=seq,
                    boot_id=boot_id,
                    now=now(),
                    holder_name=holder_name,
                )
                seq += 1
                if await uplink.post_report(report):
                    identity.last_report_ok_at = now()
                    # The cloud recomputes this on every report; an old cloud
                    # (204) or an unreadable body counts as "not handled".
                    last = uplink.last_report
                    identity.alerts_handled = bool(last and last.alerts_handled)
                else:
                    registered = registered and uplink.last_status != 401
        except Exception:
            logger.exception("Cloud uplink attempt failed")
        if not registered:
            identity.alerts_handled = False  # the cloud no longer vouches for us
        await sleep(config.cloud.report_interval_seconds)
