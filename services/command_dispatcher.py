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

**Completion, not just acceptance (2026-09-29 amendment).** ``send()``
above resolves on the FIRST ack for a ``request_id`` and nothing more — for
a long-running command that ack now means "accepted", not "done"
(``CommandAck.phase``, ``contracts/common.py``), so ``send()`` alone is not
enough to know a maintenance-lease-held test actually finished before the
lease can safely release. ``send_and_await_completion()`` below reuses
``send()`` unchanged for the accept phase (so the ack timeout still fires
in ~``ACK_TIMEOUT_SECONDS`` per attempt, exactly as before) and then, only
for a command listed in ``contracts.common.COMPLETION_TIMEOUTS``, waits —
with a separate, per-command, params-derived timeout — for that command's
own completion signal: a second ``phase="completed"`` ack on the same
``cmd/<subsystem>/ack`` topic (``water_valve``, ``power_cycle``), or
``dispense``'s terminal ``hardware/dispenser`` report, correlated by
``request_id``.
"""

from __future__ import annotations

import asyncio
import uuid
from collections import OrderedDict
from typing import Optional, Protocol

from loguru import logger

from contracts.common import (
    ACK_TIMEOUT_SECONDS,
    COMPLETION_TIMEOUTS,
    CommandAck,
    SubsystemCommand,
)
from contracts.vending_machine import DispenserOutcome

# Terminal `hardware/dispenser` states — see DispenserOutcome. Any other
# `state` string is an intermediate step (motor_active, fill_complete, ...)
# and never resolves a completion-wait.
_DISPENSE_TERMINAL_STATES = frozenset(o.value for o in DispenserOutcome)

# Bound on _early_completions (see CommandDispatcher._resolve_completion),
# mirroring the simulators' own IDEMPOTENCY_CACHE_SIZE.
_EARLY_COMPLETIONS_MAX = 32


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


class CompletionTimeout(Exception):
    """*command* on *subsystem* was accepted (its ack arrived), but no
    completion signal arrived within its own, separate completion timeout
    (``contracts.common.COMPLETION_TIMEOUTS``).

    Distinct from ``CommandTimeout`` on purpose: a caller (e.g.
    ``web_interface/routes/tests_level.py``'s ``_run_command``) needs to
    tell "the subsystem never even answered" apart from "it answered,
    started the work, and then never reported finishing it" — the two
    failures mean very different things to an operator.
    """

    def __init__(self, subsystem: str, command: str):
        self.subsystem = subsystem
        self.command = command
        super().__init__(
            f"{subsystem!r} accepted {command!r} but never reported completion"
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
        # request_id -> the Future a pending send_and_await_completion() call
        # is waiting on for a command's COMPLETION (not merely its accept
        # ack) — resolved either by a second, phase="completed" ack seen in
        # _on_ack (water_valve, power_cycle) or by _on_dispenser_report
        # (dispense's terminal hardware/dispenser report). Separate from
        # self._pending above: an immediate command's single ack resolves
        # self._pending only, and never touches this dict at all.
        self._pending_completions: dict[str, asyncio.Future] = {}
        # A completion signal can arrive before send_and_await_completion
        # gets a chance to register its Future in self._pending_completions
        # — the accept ack resolving send()'s own Future is a call_soon
        # away, not synchronous, and a near-instant command (e.g. the
        # vending simulator's ice_bin_empty fault path, which has no
        # `await asyncio.sleep` on its abort branch at all) can complete
        # inside that gap. Bounded like the simulators' own idempotency
        # cache; see _resolve_completion.
        self._early_completions: OrderedDict[str, CommandAck] = OrderedDict()
        self._mqtt.register("cmd/+/ack", self._on_ack)
        self._mqtt.register("hardware/dispenser", self._on_dispenser_report)

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
        if future is not None and not future.done():
            future.set_result(ack)

        # A phase="completed" ack is ALSO a completion signal for
        # water_valve/power_cycle (§ completion table): resolve it whether
        # or not the line above did anything, since send_and_await_completion
        # only starts listening for completion after the accept ack has
        # already resolved self._pending and popped it.
        if ack.phase == "completed":
            self._resolve_completion(ack.request_id, ack)

    async def _on_dispenser_report(self, topic_suffix: str, payload: dict) -> None:
        """Completion signal for the command-channel `dispense` (completion
        table): the vending simulator's terminal `hardware/dispenser`
        report, correlated by the `request_id` it carries when the dispense
        was reached through this command channel. A production `cmd/dispense`
        sale's reports carry no `request_id` (see
        `services/mqtt_messages.py`'s `DispenserStatus`) and never match
        anything pending here — this handler is a pure addition alongside
        the VMC's own, pre-existing `hardware/dispenser` listener
        (`controller/vmc.py`'s `_handle_mqtt_dispenser`), which keeps
        working unchanged since both are registered on the same MQTT client
        and both simply receive every message on the topic.
        """
        request_id = payload.get("request_id")
        state = payload.get("state")
        if not request_id or state not in _DISPENSE_TERMINAL_STATES:
            return
        is_complete = state == DispenserOutcome.complete.value
        status = "ok" if is_complete else "failed"
        if is_complete:
            detail = None
        else:
            board_detail = payload.get("detail")
            if board_detail:
                detail = board_detail
            elif state == DispenserOutcome.door_open.value:
                detail = "bag released but door did not close"
            else:
                detail = f"outcome {state}"
        ack = CommandAck(
            request_id=request_id,
            command="dispense",
            status=status,
            detail=detail,
            phase="completed",
        )
        self._resolve_completion(request_id, ack)

    def _resolve_completion(self, request_id: str, ack: CommandAck) -> None:
        future = self._pending_completions.get(request_id)
        if future is not None and not future.done():
            future.set_result(ack)
            return
        # Nobody is waiting yet: stash it so send_and_await_completion's
        # imminent registration finds it immediately instead of waiting out
        # the full completion timeout for a signal that already happened —
        # see self._early_completions' own docstring above.
        self._early_completions[request_id] = ack
        if len(self._early_completions) > _EARLY_COMPLETIONS_MAX:
            self._early_completions.popitem(last=False)

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

    async def send_and_await_completion(
        self, subsystem: str, command: str, params: dict | None = None
    ) -> CommandAck:
        """Send *command* and wait for it to actually FINISH, not merely to
        be accepted (2026-09-29 completion-table amendment).

        For an immediate command (its ack's `phase` is "completed" — every
        command NOT in `contracts.common.COMPLETION_TIMEOUTS`) this is
        identical to `send()`: the ack IS completion, so every existing
        caller of `send()` — and `ping` in particular — is completely
        unaffected by this method's existence.

        For a long-running command, the first ack means "accepted": `send()`
        is reused UNCHANGED for that phase, so a subsystem that never even
        accepts still fails in ~`ACK_TIMEOUT_SECONDS` per attempt via the
        same `CommandTimeout` as before — this method adds a SECOND, SEPARATE
        wait after that succeeds, for the command's own completion signal,
        with its own timeout derived from *params*
        (`contracts.common.COMPLETION_TIMEOUTS`). Raises `CommandTimeout` if
        the accept ack itself never arrives (from the reused `send()`
        call); raises `CompletionTimeout` if accepted but no completion
        signal arrives within its own timeout.
        """
        accept_ack = await self.send(subsystem, command, params)
        if accept_ack.phase != "accepted":
            return accept_ack

        timeout_fn = COMPLETION_TIMEOUTS.get((subsystem, command))
        if timeout_fn is None:
            # Accepted but nothing registered to wait for completion of --
            # return the accept ack rather than hang forever on a signal
            # that will never come. Every long-running entry in
            # TESTABLE_COMMANDS has a COMPLETION_TIMEOUTS entry
            # (tests/test_contracts_common.py asserts it), so this is a
            # defensive fallback, not an expected path.
            logger.warning(
                f"CommandDispatcher: {subsystem!r} accepted {command!r} "
                "(phase=accepted) but no completion timeout is registered "
                "for it; returning the accept ack as final"
            )
            return accept_ack

        request_id = accept_ack.request_id

        early = self._early_completions.pop(request_id, None)
        if early is not None:
            return early

        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        self._pending_completions[request_id] = future
        timeout = timeout_fn(params or {})
        try:
            sleep_task = asyncio.ensure_future(self._clock.sleep(timeout))
            done, _pending = await asyncio.wait(
                {future, sleep_task}, return_when=asyncio.FIRST_COMPLETED
            )
            if future in done:
                sleep_task.cancel()
                return future.result()

            logger.warning(
                f"CommandDispatcher: {subsystem!r} accepted {command!r} "
                f"(request_id={request_id}) but never reported completion "
                f"within {timeout}s"
            )
            raise CompletionTimeout(subsystem, command)
        finally:
            self._pending_completions.pop(request_id, None)
