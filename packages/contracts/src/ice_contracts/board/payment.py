# ice_contracts/board/payment.py
"""MQTT contract between the MDB payment board and the edge (VMC).

Covers the board -> edge event stream (`payment/event`, acknowledged on
`payment/event/ack`) and the retained board state (`payment/state`), all
relative to `vmc/<machine_id>/`. The wire format is defined by the
payment-contract design spec (docs/superpowers/specs, "payment contract (MDB
board <-> edge)").

Version rule: `CONTRACT_VERSION` follows semver. Any breaking wire change
(renamed/removed field, changed type or semantics, removed kind) bumps the
major version; additive changes bump the minor.

Money: every amount is an integer number of cents, validated as `StrictInt`
so a float (`1.0`) or numeric string (`"100"`) is rejected rather than
coerced. The currency is not carried per message; it is declared once in
`PaymentState`.

This module imports only `ice_contracts.board.common`.
"""

from datetime import datetime
from enum import Enum
from typing import Annotated, Literal, Optional, Union

from pydantic import BaseModel, Field, StrictInt, model_validator

from ice_contracts.board.common import _utc_now

CONTRACT_VERSION = "1.0.0"

# Relative to `vmc/<machine_id>/`.
PAYMENT_EVENT_TOPIC = "payment/event"
PAYMENT_EVENT_ACK_TOPIC = "payment/event/ack"
PAYMENT_STATE_TOPIC = "payment/state"

Cents = Annotated[StrictInt, Field(ge=0)]
PositiveCents = Annotated[StrictInt, Field(gt=0)]


class Device(str, Enum):
    coin = "coin"
    bill = "bill"
    cashless = "cashless"


class DeviceStatus(str, Enum):
    ready = "ready"
    disabled = "disabled"
    busy = "busy"
    jammed = "jammed"
    full = "full"
    empty = "empty"
    tilt = "tilt"
    offline = "offline"
    error = "error"


class CoinRoute(str, Enum):
    tubes = "tubes"
    cashbox = "cashbox"


class BillReturnReason(str, Enum):
    policy = "policy"
    edge_silent = "edge_silent"
    customer = "customer"
    edge = "edge"
    timeout = "timeout"


class PayoutStatus(str, Enum):
    ok = "ok"
    partial = "partial"
    failed = "failed"


class SessionEndReason(str, Enum):
    complete = "complete"
    cancelled = "cancelled"
    reader = "reader"
    edge = "edge"
    timeout = "timeout"


# --- Event bodies -------------------------------------------------------------


class CoinAccepted(BaseModel):
    kind: Literal["coin_accepted"] = "coin_accepted"
    cents: PositiveCents = Field(..., description="Coin value in cents")
    routed: CoinRoute = Field(..., description="Where the coin was routed")


class BillEscrowed(BaseModel):
    kind: Literal["bill_escrowed"] = "bill_escrowed"
    cents: PositiveCents = Field(..., description="Bill value in cents")


class BillStacked(BaseModel):
    kind: Literal["bill_stacked"] = "bill_stacked"
    cents: PositiveCents = Field(..., description="Bill value in cents")


class BillReturned(BaseModel):
    kind: Literal["bill_returned"] = "bill_returned"
    cents: PositiveCents = Field(..., description="Bill value in cents")
    reason: BillReturnReason


class EscrowReturnPressed(BaseModel):
    kind: Literal["escrow_return_pressed"] = "escrow_return_pressed"


class PayoutResult(BaseModel):
    kind: Literal["payout_result"] = "payout_result"
    request_id: str = Field(..., min_length=8, max_length=64)
    requested_cents: PositiveCents
    paid_cents: Cents
    status: PayoutStatus
    detail: Optional[str] = None

    @model_validator(mode="after")
    def _check_consistency(self):
        if self.status == PayoutStatus.ok and self.paid_cents != self.requested_cents:
            raise ValueError("payout status ok requires paid_cents == requested_cents")
        if self.status == PayoutStatus.partial and not (
            0 <= self.paid_cents < self.requested_cents
        ):
            raise ValueError(
                "payout status partial requires 0 <= paid_cents < requested_cents"
            )
        if self.status == PayoutStatus.failed and self.paid_cents != 0:
            raise ValueError("payout status failed requires paid_cents == 0")
        return self


class SessionBegun(BaseModel):
    kind: Literal["session_begun"] = "session_begun"
    session_id: str = Field(..., min_length=1, max_length=64)
    funds_cents: Optional[Cents]


class VendApproved(BaseModel):
    kind: Literal["vend_approved"] = "vend_approved"
    session_id: str = Field(..., min_length=1, max_length=64)
    request_id: str = Field(..., min_length=8, max_length=64)
    cents: PositiveCents


class VendDenied(BaseModel):
    kind: Literal["vend_denied"] = "vend_denied"
    session_id: str = Field(..., min_length=1, max_length=64)
    request_id: str = Field(..., min_length=8, max_length=64)
    reason: str


class SessionEnded(BaseModel):
    kind: Literal["session_ended"] = "session_ended"
    session_id: str = Field(..., min_length=1, max_length=64)
    reason: SessionEndReason


class DeviceStatusEvent(BaseModel):
    kind: Literal["device_status"] = "device_status"
    device: Device
    status: DeviceStatus
    mdb_status: Optional[Annotated[StrictInt, Field(ge=0, le=255)]] = None


class CashCollected(BaseModel):
    kind: Literal["cash_collected"] = "cash_collected"
    device: Device


PaymentEventBody = Annotated[
    Union[
        CoinAccepted,
        BillEscrowed,
        BillStacked,
        BillReturned,
        EscrowReturnPressed,
        PayoutResult,
        SessionBegun,
        VendApproved,
        VendDenied,
        SessionEnded,
        DeviceStatusEvent,
        CashCollected,
    ],
    Field(discriminator="kind"),
]

EVENT_KINDS: tuple[str, ...] = (
    "coin_accepted",
    "bill_escrowed",
    "bill_stacked",
    "bill_returned",
    "escrow_return_pressed",
    "payout_result",
    "session_begun",
    "vend_approved",
    "vend_denied",
    "session_ended",
    "device_status",
    "cash_collected",
)


class PaymentEventEnvelope(BaseModel):
    """Board -> edge event on `payment/event`."""

    event_id: str = Field(..., min_length=8, max_length=64)
    boot_id: str = Field(..., min_length=8, max_length=32)
    seq: Annotated[StrictInt, Field(ge=1)]
    ts: datetime = Field(default_factory=_utc_now)
    event: PaymentEventBody


class PaymentEventAck(BaseModel):
    """Edge -> board acknowledgement on `payment/event/ack`."""

    boot_id: str = Field(..., min_length=8, max_length=32)
    seq: Annotated[StrictInt, Field(ge=1)]
    timestamp: datetime = Field(default_factory=_utc_now)
