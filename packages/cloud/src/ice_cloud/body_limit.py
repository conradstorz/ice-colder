"""Pure-ASGI request body size limit, applied before any route or auth runs.

Bytes are counted as they arrive from the server, so a chunked body or a lying
``Content-Length`` is cut off at the limit instead of being buffered whole.
"""

from __future__ import annotations

from starlette.requests import ClientDisconnect
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from ice_ui.bundle import MAX_UNCOMPRESSED_BYTES

MAX_JSON_BODY_BYTES = 256 * 1024
BUNDLE_PREFIX = "/api/v1/ui-bundles/"


def limit_for(method: str, path: str) -> int:
    if method == "PUT" and path.startswith(BUNDLE_PREFIX):
        return MAX_UNCOMPRESSED_BYTES
    return MAX_JSON_BODY_BYTES


class BodyLimitMiddleware:
    """Answer 413 and report a disconnect to the app the moment the limit is passed.

    Raising from ``receive`` is not enough: FastAPI turns a body-read exception
    into a 400, so the 413 is sent here and the app just sees the client leave.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        limit = limit_for(scope["method"], scope["path"])
        declared = dict(scope["headers"]).get(b"content-length", b"")
        if declared.isdigit() and int(declared) > limit:
            await _reject(send)
            return

        total = 0
        rejected = False

        async def counting_receive() -> Message:
            nonlocal total, rejected
            if rejected:
                return {"type": "http.disconnect"}
            message = await receive()
            if message["type"] == "http.request":
                total += len(message.get("body", b""))
                if total > limit:
                    rejected = True
                    await _reject(send)
                    return {"type": "http.disconnect"}
            return message

        async def guarded_send(message: Message) -> None:
            if not rejected:
                await send(message)

        try:
            await self.app(scope, counting_receive, guarded_send)
        except ClientDisconnect:
            if not rejected:
                raise


async def _reject(send: Send) -> None:
    await send({"type": "http.response.start", "status": 413, "headers": []})
    await send({"type": "http.response.body", "body": b""})
