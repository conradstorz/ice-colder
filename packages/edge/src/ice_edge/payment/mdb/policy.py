"""Acceptance-policy sync with the MDB board and the ``cmd/mdb`` keepalive
(cutover spec 5.2).

``set_policy`` is acked ``ok`` / ``queued`` by the board (firmware design 11),
so an ack alone proves nothing: the policy is in force only once a
``payment/state`` from the acked boot echoes the current ``policy_id``.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from typing import Any, Protocol

from loguru import logger

from ice_common.config_model import MdbPolicyConfig
from ice_common.observers import Subscribers
from ice_contracts.board.payment import (
    EDGE_IDLE_PING_SECONDS,
    PaymentState,
    SetPolicyParams,
)
from ice_edge.hal.command_dispatcher import CommandTimeout
from ice_edge.supervisor.task_runner import TaskRunner

RETRY_SECONDS = 5.0
VERIFY_SECONDS = 5.0


class _Dispatcher(Protocol):
    async def send(
        self, subsystem: str, command: str, params: dict | None = None
    ) -> Any: ...


def build_policy(cfg: MdbPolicyConfig, accept: bool) -> SetPolicyParams:
    fields: dict[str, Any] = {
        "accept": accept,
        "max_escrow_cents": cfg.max_escrow_cents,
        "coin_enabled": list(cfg.coin_enabled),
        "bill_enabled": list(cfg.bill_enabled),
        "cashless_enabled": False,
        "change_threshold_cents": cfg.change_threshold_cents,
        "edge_timeout_s": cfg.edge_timeout_s,
        "return_bill_on_lever": cfg.return_bill_on_lever,
    }
    canonical = json.dumps(fields, sort_keys=True, separators=(",", ":"))
    policy_id = hashlib.sha256(canonical.encode()).hexdigest()[:16]
    return SetPolicyParams(policy_id=policy_id, **fields)


class PolicySync:
    def __init__(
        self,
        *,
        dispatcher: _Dispatcher,
        config: Callable[[], MdbPolicyConfig],
        payment_enabled: Callable[[], bool],
        tasks: TaskRunner,
        board_alive: Callable[[], bool],
    ) -> None:
        self._dispatcher = dispatcher
        self._config = config
        self._payment_enabled = payment_enabled
        self._tasks = tasks
        self._board_alive = board_alive
        self._acked = False
        self._ack_ok_id: str | None = None
        self._acked_boot: str | None = None
        self._last_boot: str | None = None
        self._last_state: PaymentState | None = None
        self._inflight = False
        self._require_fresh = False
        self._inflight_id: str | None = None
        self._resend_after: str | None = None
        self._connected = False
        self._retry: Any = None
        self._verify: Any = None
        self._keepalive: Any = None
        self._ping_inflight = False
        self._listeners: Subscribers[bool] = Subscribers()

    # -- public surface ------------------------------------------------
    @property
    def policy_acked(self) -> bool:
        return self._acked

    def subscribe_policy_acked(self, cb: Callable[[bool], None]) -> Callable[[], None]:
        return self._listeners.subscribe(cb)

    def note_command_sent(self) -> None:
        """Any ``cmd/mdb`` send resets the idle-ping clock."""
        self._arm_keepalive()

    async def sync(self, reason: str) -> None:
        """Send the current policy. One send is in flight at a time; a request
        made meanwhile is folded into a single re-send once it finishes."""
        if self._inflight:
            self._resend_after = reason
            return
        self._inflight = True
        try:
            while True:
                self._resend_after = None
                await self._send_once(reason)
                if self._resend_after is None:
                    break
                reason = self._resend_after
        finally:
            self._inflight = False

    def on_state(self, state: PaymentState) -> None:
        self._last_state = state
        self._last_boot = state.boot_id
        cur = self._current_id()
        if self._acked_boot is not None and state.boot_id != self._acked_boot:
            self._ack_ok_id = None
            self._acked_boot = None
            self._cancel_verify()
            self._set_acked(False)
            self._spawn("boot_changed")
            return
        if self._ack_ok_id == cur:
            if state.policy_id == cur:
                self._require_fresh = False
                self._acked_boot = state.boot_id
                self._cancel_verify()
                self._set_acked(True)
                return
            if not self._acked:
                # Acked but the echo is still pending: the verify timer covers a miss.
                if self._acked_boot is None:
                    self._acked_boot = state.boot_id
                return
            # Echoed earlier, but the board has since lost or replaced its policy.
            self._ack_ok_id = None
            self._acked_boot = None
            self._cancel_verify()
        self._set_acked(False)
        if self._retry is not None:  # keep the retry back-off
            return
        if self._inflight and self._inflight_id == cur:
            return  # an identical send is already on its way
        self._spawn("state_mismatch")

    def on_board_reports_no_policy(self) -> None:
        """The board rejected a command with ``no_policy``: whatever we
        believed, its policy is not in force. Drop the ack and re-send."""
        self._ack_ok_id = None
        self._acked_boot = None
        self._cancel_verify()
        # A state received before the re-send's ack cannot prove the policy is
        # in force again: forget it and wait for a fresh echo.
        self._last_state = None
        self._require_fresh = True
        self._set_acked(False)
        self._spawn("board_no_policy")

    def on_payment_enabled_changed(self, enabled: bool) -> None:
        if self._current_id() != self._ack_ok_id:
            self._set_acked(False)
        self._spawn("payment_enabled")

    def on_connected(self) -> None:
        self._connected = True
        self._arm_keepalive()
        self._spawn("connected")

    def on_disconnected(self) -> None:
        self._connected = False
        self._cancel_keepalive()

    # -- internals -----------------------------------------------------
    def _current_id(self) -> str:
        return build_policy(self._config(), self._payment_enabled()).policy_id

    async def _send_once(self, reason: str) -> None:
        params = build_policy(self._config(), self._payment_enabled())
        if params.policy_id != self._ack_ok_id:
            self._set_acked(False)
        self._inflight_id = params.policy_id
        self._cancel_retry()
        self._cancel_verify()
        logger.info(f"MDB policy sync ({reason}): {params.policy_id}")
        ok = False
        try:
            ack = await self._send("set_policy", params.model_dump(mode="json"))
            ok = getattr(ack, "status", None) == "ok"
        except CommandTimeout:
            logger.warning("MDB set_policy timed out")
        except Exception:  # noqa: BLE001 - a send error must still arm the retry
            logger.exception("MDB set_policy failed")
        if ok:
            self._ack_ok_id = params.policy_id
            self._acked_boot = self._last_boot
            last = self._last_state
            if (
                not self._require_fresh
                and last is not None
                and last.policy_id == params.policy_id
            ):
                self._acked_boot = last.boot_id
                self._set_acked(True)
            else:
                self._verify = self._tasks.schedule(
                    VERIFY_SECONDS, self._verify_due, label="policy_verify"
                )
        elif self._resend_after is None and self._board_alive():
            self._cancel_retry()
            self._retry = self._tasks.schedule(
                RETRY_SECONDS, self._retry_due, label="policy_retry"
            )

    def _spawn(self, reason: str) -> None:
        self._tasks.fire_and_forget(self.sync(reason))

    def _retry_due(self) -> None:
        self._retry = None
        if self._board_alive():
            self._spawn("retry")

    def _verify_due(self) -> None:
        self._verify = None
        if not self._acked:
            self._ack_ok_id = None
            self._acked_boot = None
            self._spawn("verify")

    def _cancel_retry(self) -> None:
        if self._retry is not None:
            self._retry.cancel()
            self._retry = None

    def _cancel_verify(self) -> None:
        if self._verify is not None:
            self._verify.cancel()
            self._verify = None

    def _set_acked(self, value: bool) -> None:
        if value != self._acked:
            self._acked = value
            self._listeners.publish(value)

    async def _send(self, command: str, params: dict | None = None) -> Any:
        self.note_command_sent()
        return await self._dispatcher.send("mdb", command, params)

    def _cancel_keepalive(self) -> None:
        if self._keepalive is not None:
            self._keepalive.cancel()
            self._keepalive = None

    def _arm_keepalive(self) -> None:
        self._cancel_keepalive()
        if not self._connected:
            return
        self._keepalive = self._tasks.schedule(
            EDGE_IDLE_PING_SECONDS, self._keepalive_due, label="mdb_keepalive"
        )

    def _keepalive_due(self) -> None:
        self._keepalive = None
        if not self._connected:
            return
        if self._ping_inflight:
            self._arm_keepalive()
            return
        self._tasks.fire_and_forget(self._ping())

    async def _ping(self) -> None:
        self._ping_inflight = True
        try:
            await self._send("ping")
        except CommandTimeout:
            logger.warning("MDB keepalive ping timed out")
        finally:
            self._ping_inflight = False
            self._arm_keepalive()
