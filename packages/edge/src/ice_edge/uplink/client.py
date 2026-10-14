"""HTTP client for the cloud monitoring server (cloud spec section 7).

Plain HTTP, a short timeout, and no exceptions: a failed upload is logged and
dropped so a sale is never held up by the cloud.
"""

from __future__ import annotations

import httpx
from loguru import logger

from ice_contracts.cloud.report import MachineReport


class HttpUplink:
    def __init__(
        self,
        base_url: str,
        machine_id: str,
        *,
        timeout: float = 5.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._machine_id = machine_id
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"), timeout=timeout, transport=transport
        )

    async def post_report(self, report: MachineReport) -> bool:
        return await self._send(
            "POST",
            f"/api/v1/machines/{self._machine_id}/report",
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

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _send(
        self, method: str, path: str, *, content: bytes, content_type: str
    ) -> bool:
        try:
            response = await self._client.request(
                method, path, content=content, headers={"Content-Type": content_type}
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
