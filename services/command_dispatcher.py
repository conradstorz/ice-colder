# services/command_dispatcher.py
"""VMC-side dispatcher for the subsystem command channel (§2.1 of the
system-tests design).

Sends a ``SubsystemCommand`` to ``vmc/<machine_id>/cmd/<subsystem>`` and
correlates the matching ``CommandAck`` from ``vmc/<machine_id>/cmd/<subsystem>/ack``
by ``request_id``. A single wildcard subscription (``cmd/+/ack``) is shared
by every in-flight ``send()`` call; each call tracks only its own
``request_id`` and ignores every ack that is not it — including acks for
commands this dispatcher never sent (the ice-maker's own pre-existing
``cmd/ice_maker/ack`` handler in ``controller/vmc.py`` receives the same
wildcard traffic; see the module docstring note below).

On timeout, the command is retried exactly once with the **same**
``request_id`` — never a fresh one — so a subsystem's idempotency cache
replays the cached ack instead of repeating the underlying action. A retry
that minted a new id would, for example, fire a second ``dispense`` cycle
on real hardware.

``clock`` is injected so tests can control the passage of time without
sleeping for real. See ``Clock`` / ``_RealClock`` below.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Optional, Protocol

from loguru import logger

from contracts.common import ACK_TIMEOUT_SECONDS, CommandAck, SubsystemCommand


class Clock(Protocol):
    """Everything the dispatcher needs from time. ``_RealClock`` is the
    production implementation; tests inject a fake that resolves ``sleep``
    without a real delay so a timeout test does not take 10 real seconds.
    """

    async def sleep(self, seconds: float) -> None: ...


class _RealClock:
    """Default clock: a thin wrapper over ``asyncio.sleep``."""

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


class CommandTimeout(Exception):
    """No ack arrived for *command* on *subsystem* after every attempt
    (including retries) was exhausted, or the broker was unreachable when
    ``send`` was called.
    """

    def __init__(self, subsystem: str, command: str):
        self.subsystem = subsystem
        self.command = command
        super().__init__(
            f"No ack from subsystem={subsystem!r} for command={command!r} "
            "after all attempts"
        )


class CommandDispatcher:
    """Sends subsystem commands and awaits their acks, by request_id.

    Usage::

        dispatcher = CommandDispatcher(mqtt_client)
        ack = await dispatcher.send("ice_maker", "ping")
    """

    def __init__(
        self,
        mqtt_client,
        timeout: float = ACK_TIMEOUT_SECONDS,
        retries: int = 1,
        clock: Optional[Clock] = None,
    ):
        self._mqtt = mqtt_client
        self._timeout = timeout
        self._retries = retries
        self._clock: Clock = clock or _RealClock()
        # request_id -> the Future the currently in-flight attempt for that
        # request_id will resolve. A retry replaces the entry with a fresh
        # Future (the previous one is simply abandoned, never cancelled from
        # here — see send()) but the request_id, and therefore the dict key
        # and the wire value the subsystem sees, never changes.
        self._pending: dict[str, asyncio.Future] = {}
        self._mqtt.register("cmd/+/ack", self._on_ack)

    async def _on_ack(self, topic_suffix: str, payload: dict) -> None:
        """Handler for `cmd/+/ack`. Shared wildcard traffic: an ack for a
        request_id this dispatcher does not know about (another handler's
        command, or a stray/duplicate) is ignored silently, never an error.
        """
        try:
            ack = CommandAck.model_validate(payload)
        except Exception:
            logger.debug(
                f"CommandDispatcher: ignoring unparseable ack on {topic_suffix}"
            )
            return

        future = self._pending.get(ack.request_id)
        if future is None or future.done():
            return
        future.set_result(ack)

    async def send(
        self, subsystem: str, command: str, params: dict | None = None
    ) -> CommandAck:
        """Send *command* to *subsystem* and await its ack.

        Raises ``CommandTimeout`` if the broker is unreachable (checked
        immediately, before any publish or wait — the whole point is not to
        wait out the timeout when there is no chance of an answer) or if no
        ack arrives after the initial attempt plus ``retries`` retries, each
        using the same ``request_id``.
        """
        if not self._mqtt.connected:
            logger.warning(
                f"CommandDispatcher: broker not connected; refusing to send "
                f"{command!r} to {subsystem!r}"
            )
            raise CommandTimeout(subsystem, command)

        request_id = uuid.uuid4().hex
        cmd = SubsystemCommand(
            request_id=request_id, command=command, params=params or {}
        )
        topic = f"cmd/{subsystem}"

        try:
            for attempt in range(self._retries + 1):
                loop = asyncio.get_running_loop()
                future: asyncio.Future = loop.create_future()
                self._pending[request_id] = future

                await self._mqtt.publish(topic, cmd)

                sleep_task = asyncio.ensure_future(self._clock.sleep(self._timeout))
                done, _pending = await asyncio.wait(
                    {future, sleep_task}, return_when=asyncio.FIRST_COMPLETED
                )

                if future in done:
                    sleep_task.cancel()
                    return future.result()

                # Timed out this attempt: abandon this Future (leave it
                # pending/unresolved — a late ack for it is simply never
                # observed, since a fresh Future replaces it in self._pending
                # on the next loop iteration) and, if attempts remain, retry
                # with the *same* request_id/cmd.
                logger.warning(
                    f"CommandDispatcher: {subsystem!r} did not ack "
                    f"{command!r} (request_id={request_id}) within "
                    f"{self._timeout}s (attempt {attempt + 1}/{self._retries + 1})"
                )

            raise CommandTimeout(subsystem, command)
        finally:
            self._pending.pop(request_id, None)
