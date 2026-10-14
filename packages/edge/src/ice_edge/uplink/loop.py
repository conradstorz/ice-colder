"""The supervised uplink task: UI bundle PUT first, then reports forever."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from loguru import logger

from ice_common.config_model import ConfigModel
from ice_edge.uplink.client import HttpUplink
from ice_edge.uplink.report import build_machine_report
from ice_ui.bundle import build_bundle, ui_digest

if TYPE_CHECKING:
    from ice_edge.app.machine import Machine


def utc_now() -> datetime:
    return datetime.now(UTC)


async def run_uplink(
    machine: Machine,
    config: ConfigModel,
    uplink: HttpUplink,
    *,
    sleep: Callable[[float], Awaitable[object]] = asyncio.sleep,
    now: Callable[[], datetime] = utc_now,
    holder_name: Callable[[object], str | None] | None = None,
) -> None:
    """Upload the UI bundle (retried once per interval until accepted), then
    post a report every ``config.cloud.report_interval_seconds`` (re-read each
    pass). ``seq`` increments per attempt; ``boot_id`` is fixed per call.
    Failures are logged and dropped; only cancellation ends the loop."""
    boot_id = str(uuid.uuid4())

    while True:
        try:
            if await uplink.put_ui_bundle(ui_digest(), build_bundle()):
                break
        except Exception:
            logger.exception("UI bundle upload attempt failed")
        await sleep(config.cloud.report_interval_seconds)

    seq = 0
    while True:
        try:
            report = await build_machine_report(
                machine,
                config=config,
                seq=seq,
                boot_id=boot_id,
                now=now(),
                holder_name=holder_name,
            )
            await uplink.post_report(report)
        except Exception:
            logger.exception("Cloud report attempt failed")
        seq += 1
        await sleep(config.cloud.report_interval_seconds)
