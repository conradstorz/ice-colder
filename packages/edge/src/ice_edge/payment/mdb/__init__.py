"""Edge side of the MDB payment board: the composed gateway."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from ice_contracts.board.common import CommandAck
from ice_contracts.board.payment import (
    PaymentEventAck,
    PaymentState,
    PayoutReason,
    PayoutResult,
)
from ice_edge.payment.mdb.intake import EventIntake
from ice_edge.payment.mdb.payout import PayoutProtocol
from ice_edge.payment.mdb.policy import PolicySync
from ice_edge.payment.mdb.reconcile import Reconciler
from ice_edge.payment.mdb.state import StateConsumer
from ice_edge.payment.ports import PendingPayout


class _KeepaliveDispatcher:
    """Every ``cmd/mdb`` send resets the idle-ping clock."""

    def __init__(self, inner: Any, policy: PolicySync) -> None:
        self._inner = inner
        self._policy = policy

    async def send_and_await_completion(self, *args: Any, **kwargs: Any) -> Any:
        self._policy.note_command_sent()
        return await self._inner.send_and_await_completion(*args, **kwargs)


class MdbGateway:
    def __init__(
        self,
        *,
        dispatcher: Any,
        config: Callable[[], Any],
        payment_enabled: Callable[[], bool],
        tasks: Any,
        faults: Any,
        board_alive: Callable[[], bool],
        availability: Callable[[], Any],
        deposit: Callable[[int, str, str], None],
        persist_session: Callable[[], Awaitable[None]],
        on_lever: Callable[[], None],
        message: Callable[[str], None],
        journal: Callable[[str, dict], None],
        record_event: Callable[[str, int, dict | None], None],
        on_paid: Callable[[PendingPayout, int], None],
        on_changed: Callable[[], None],
        on_cursor_saved: Callable[[bool], None],
        fsm_idle: Callable[[], bool],
        pay104_active: Callable[[], bool],
        edge_cash_escrow: Callable[[], int],
        publish_ack: Callable[[PaymentEventAck], None],
        cursor: Any,
    ) -> None:
        self._dispatcher = dispatcher
        self._policy = PolicySync(
            dispatcher=dispatcher,
            config=config,
            payment_enabled=payment_enabled,
            tasks=tasks,
            board_alive=board_alive,
        )
        self._state = StateConsumer(
            faults=faults,
            availability=availability,
            policy=self._policy,
            message=message,
        )
        self._payouts = PayoutProtocol(
            dispatcher=_KeepaliveDispatcher(dispatcher, self._policy),
            policy_acked=lambda: self._policy.policy_acked,
            board_alive=board_alive,
            tasks=tasks,
            faults=faults,
            on_paid=on_paid,
            on_changed=lambda: (on_changed(), self._reconciler.maybe_run()),
            on_no_policy=self._policy.on_board_reports_no_policy,
        )

        def cursor_saved(ok: bool) -> None:
            on_cursor_saved(ok)
            self._reconciler.maybe_run()

        self._intake = EventIntake(
            cursor=cursor,
            publish_ack=publish_ack,
            deposit=deposit,
            persist_session=persist_session,
            on_lever=on_lever,
            on_payout_result=self._complete_payout,
            on_device_status=self._state.on_device_status,
            message=message,
            journal=journal,
            record_event=record_event,
            faults=faults,
            on_cursor_saved=cursor_saved,
        )
        self._reconciler = Reconciler(
            state=lambda: self._state.state,
            state_ts=lambda: self._state.state_ts,
            intake=self._intake,
            payouts=self._payouts,
            fsm_idle=fsm_idle,
            pay104_active=pay104_active,
            edge_cash_escrow=edge_cash_escrow,
            faults=faults,
        )
        self._reconciler.arm("edge_start")
        self._state.subscribe_state(self._reconciler.on_state)
        self._policy.subscribe_policy_acked(self._on_policy_acked)

    # -- wiring --------------------------------------------------------
    def _complete_payout(self, request_id: str, result: PayoutResult) -> None:
        self._dispatcher.complete(
            request_id,
            CommandAck(
                request_id=request_id,
                command="payout",
                status="ok",
                detail=result.status.value,
                result=result.model_dump(mode="json"),
                phase="completed",
            ),
        )

    def _on_policy_acked(self, acked: bool) -> None:
        if acked:
            self._payouts.kick()
            self._reconciler.maybe_run()

    def on_pay101_cleared(self) -> None:
        self._payouts.kick()
        self._reconciler.arm("pay101_cleared")
        self._reconciler.maybe_run()

    def on_mqtt_reconnected(self) -> None:
        self._reconciler.arm("mqtt_reconnect")
        self._reconciler.maybe_run()

    # -- Payments ------------------------------------------------------
    def pay_out(self, cents: int, reason: PayoutReason, sale_id: str | None) -> None:
        self._payouts.pay_out(cents, reason, sale_id)

    def cash_sale(self, cents: int, sale_id: str | None) -> None:
        self._payouts.cash_sale(cents, sale_id)

    def pending_payouts(self) -> list[PendingPayout]:
        return self._payouts.pending()

    # -- read-only ------------------------------------------------------
    @property
    def state(self) -> PaymentState | None:
        return self._state.state

    @property
    def policy_acked(self) -> bool:
        return self._policy.policy_acked

    @property
    def intake(self) -> EventIntake:
        return self._intake

    @property
    def policy(self) -> PolicySync:
        return self._policy

    @property
    def state_consumer(self) -> StateConsumer:
        return self._state

    @property
    def payouts(self) -> PayoutProtocol:
        return self._payouts

    @property
    def reconciler(self) -> Reconciler:
        return self._reconciler
