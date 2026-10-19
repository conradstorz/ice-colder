"""HTTP client for the cloud monitoring server (cloud spec section 7).

Plain HTTP, a short timeout, and no exceptions: a failed upload is logged and
dropped so a sale is never held up by the cloud.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

import httpx
from loguru import logger

from ice_contracts.cloud.identity import ClaimCodeNotice, RegisterMachine
from ice_contracts.cloud.report import MachineReport
from ice_contracts.cloud.signing import (
    HEADER_MACHINE,
    HEADER_SIGNATURE,
    HEADER_TIMESTAMP,
    format_timestamp,
)
from ice_edge.uplink.identity import MachineIdentity


def utc_now() -> datetime:
    return datetime.now(UTC)


class HttpUplink:
    def __init__(
        self,
        base_url: str,
        identity: MachineIdentity,
        *,
        timeout: float = 5.0,
        transport: httpx.AsyncBaseTransport | None = None,
        now: Callable[[], datetime] = utc_now,
    ) -> None:
        self._identity = identity
        self._now = now
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"), timeout=timeout, transport=transport
        )

    def _machine_path(self, suffix: str) -> str:
        return f"/api/v1/machines/{self._identity.fingerprint}/{suffix}"

    async def post_report(self, report: MachineReport) -> bool:
        return await self._send(
            "POST",
            self._machine_path("report"),
            content=report.model_dump_json().encode("utf-8"),
            content_type="application/json",
        )

    async def put_ui_bundle(self, digest: str, data: bytes) -> bool:
        return await self._send(
            "PUT",
            f"/api/v1/ui-bundles/{digest}",
            content=data,
            content_type="application/gzip",
        )

    async def register(self, label: str) -> bool:
        body = RegisterMachine(
            public_key_b64=self._identity.public_key_b64, label=label
        )
        return await self._send(
            "POST",
            "/api/v1/machines/register",
            content=body.model_dump_json().encode("utf-8"),
            content_type="application/json",
        )

    async def post_claim_code(self, notice: ClaimCodeNotice) -> bool:
        return await self._send(
            "POST",
            self._machine_path("claim-code"),
            content=notice.model_dump_json().encode("utf-8"),
            content_type="application/json",
        )

    async def delete_claim_code(self) -> bool:
        return await self._send(
            "DELETE",
            self._machine_path("claim-code"),
            content=b"",
            content_type="application/json",
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _send(
        self, method: str, path: str, *, content: bytes, content_type: str
    ) -> bool:
        try:
            timestamp = format_timestamp(self._now())
            headers = {
                "Content-Type": content_type,
                HEADER_MACHINE: self._identity.fingerprint,
                HEADER_TIMESTAMP: timestamp,
                HEADER_SIGNATURE: self._identity.sign(method, path, timestamp, content),
            }
            response = await self._client.request(
                method, path, content=content, headers=headers
            )
        except Exception as exc:  # never raise out of the uplink
            logger.warning(f"Cloud uplink {method} {path} failed: {exc!r}")
            return False
        if not response.is_success:
            logger.warning(
                f"Cloud uplink {method} {path} returned HTTP {response.status_code}"
            )
            return False
        return True
