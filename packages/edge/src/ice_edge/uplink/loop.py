"""The supervised uplink task: UI bundle PUT first, then reports forever."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
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


async def _send_notice(uplink: HttpUplink, identity: MachineIdentity) -> None:
    """Deliver the pending claim-code notice (or its DELETE) once; ack on 2xx."""
    notice = identity.notice
    if notice is None or notice.acked:
        return
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
        identity.mark_notice_acked()


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
    an unacknowledged claim-code notice, upload the UI bundle (until accepted),
    then post a report. The interval is re-read every pass. ``seq`` increments
    per report attempt; ``boot_id`` is fixed per call. Failures are logged and
    dropped; only cancellation ends the loop."""
    boot_id = str(uuid.uuid4())
    digest, bundle = ui_digest(), build_bundle()
    registered = False
    bundle_sent = False
    seq = 0

    while True:
        try:
            if not registered:
                registered = await uplink.register(config.machine_id)
            if registered:
                try:
                    await _send_notice(uplink, identity)
                except Exception:
                    logger.exception("Claim-code notice attempt failed")
                if not bundle_sent:
                    try:
                        bundle_sent = await uplink.put_ui_bundle(digest, bundle)
                    except Exception:
                        logger.exception("UI bundle upload attempt failed")
                if bundle_sent:
                    report = await build_machine_report(
                        machine,
                        config=config,
                        seq=seq,
                        boot_id=boot_id,
                        now=now(),
                        holder_name=holder_name,
                    )
                    seq += 1
                    await uplink.post_report(report)
        except Exception:
            logger.exception("Cloud uplink attempt failed")
        await sleep(config.cloud.report_interval_seconds)
