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

from ice_contracts.board.common import STANDARD_COMMANDS, _utc_now

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
    kind: Literal["coin_accepted"]
    cents: PositiveCents = Field(..., description="Coin value in cents")
    routed: CoinRoute = Field(..., description="Where the coin was routed")


class BillEscrowed(BaseModel):
    kind: Literal["bill_escrowed"]
    cents: PositiveCents = Field(..., description="Bill value in cents")


class BillStacked(BaseModel):
    kind: Literal["bill_stacked"]
    cents: PositiveCents = Field(..., description="Bill value in cents")


class BillReturned(BaseModel):
    kind: Literal["bill_returned"]
    cents: PositiveCents = Field(..., description="Bill value in cents")
    reason: BillReturnReason


class EscrowReturnPressed(BaseModel):
    kind: Literal["escrow_return_pressed"]


class PayoutResult(BaseModel):
    kind: Literal["payout_result"]
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
    kind: Literal["session_begun"]
    session_id: str = Field(..., min_length=1, max_length=64)
    funds_cents: Optional[Cents]


class VendApproved(BaseModel):
    kind: Literal["vend_approved"]
    session_id: str = Field(..., min_length=1, max_length=64)
    request_id: str = Field(..., min_length=8, max_length=64)
    cents: PositiveCents


class VendDenied(BaseModel):
    kind: Literal["vend_denied"]
    session_id: str = Field(..., min_length=1, max_length=64)
    request_id: str = Field(..., min_length=8, max_length=64)
    reason: str


class SessionEnded(BaseModel):
    kind: Literal["session_ended"]
    session_id: str = Field(..., min_length=1, max_length=64)
    reason: SessionEndReason


class DeviceStatusEvent(BaseModel):
    kind: Literal["device_status"]
    device: Device
    status: DeviceStatus
    mdb_status: Optional[Annotated[StrictInt, Field(ge=0, le=255)]] = None


class CashCollected(BaseModel):
    kind: Literal["cash_collected"]
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


class PaymentEventHeader(BaseModel):
    """Lenient view of an event envelope: everything but the body is typed.

    `PaymentEventEnvelope.event` is a closed union, so an envelope carrying a
    `kind` from a newer 1.x board fails strict validation as a whole. The edge
    therefore parses this header FIRST and acks from its `boot_id`/`seq`, then
    parses the body with `PaymentEventEnvelope`. If that fails because of an
    unknown `kind`, the edge logs it, acks it (already done) and counts it
    toward PAY-105's detail.
    """

    event_id: str = Field(..., min_length=8, max_length=64)
    boot_id: str = Field(..., min_length=8, max_length=32)
    seq: Annotated[StrictInt, Field(ge=1)]
    ts: datetime = Field(default_factory=_utc_now)
    event: dict = Field(..., description="Event body, any shape")


class PaymentEventAck(BaseModel):
    """Edge -> board acknowledgement on `payment/event/ack`.

    Cumulative, but only within `boot_id`: it covers log entries with the SAME
    `boot_id` and `seq` <= this one. Entries from an earlier boot stay in the
    board's log until acked under their own `boot_id` (the board replays them
    first, in order).
    """

    boot_id: str = Field(
        ...,
        min_length=8,
        max_length=32,
        description="Boot whose log entries this ack covers; earlier boots are unaffected",
    )
    seq: Annotated[StrictInt, Field(ge=1)] = Field(
        ..., description="Covers entries of this boot_id with seq <= this"
    )
    timestamp: datetime = Field(default_factory=_utc_now)


# --- Retained state -----------------------------------------------------------

# The board republishes the retained `payment/state` document on change, on
# `force_report`, and every this-many seconds. A retained document never means
# the board is alive: heartbeats stay the liveness source.
STATE_REPUBLISH_SECONDS = 30


class InhibitReason(str, Enum):
    no_policy = "no_policy"
    edge_silent = "edge_silent"
    policy = "policy"
    device_fault = "device_fault"


class TubeLevel(BaseModel):
    cents: PositiveCents = Field(..., description="Coin denomination of this tube")
    count: Annotated[StrictInt, Field(ge=0)] = Field(
        ..., description="Coins currently in the tube"
    )
    full: bool = Field(..., description="Tube reports full")


class StackerState(BaseModel):
    count: Annotated[StrictInt, Field(ge=0)] = Field(
        ..., description="Bills in the stacker"
    )
    full: bool = Field(..., description="Stacker reports full")


class CashlessSession(BaseModel):
    session_id: str = Field(..., min_length=1, max_length=64)
    funds_cents: Optional[Cents] = Field(
        ..., description="Funds available on the session, if known"
    )
    started_ts: datetime = Field(..., description="When the session began")


class DeviceState(BaseModel):
    present: bool = Field(..., description="Device detected on the MDB bus")
    status: DeviceStatus
    level: Optional[Literal[1, 2, 3]] = Field(None, description="MDB feature level")
    model: Optional[str] = None
    serial: Optional[str] = None
    denominations: list[PositiveCents] = Field(
        default_factory=list, description="Denominations the device can accept"
    )
    enabled: list[PositiveCents] = Field(
        default_factory=list, description="Denominations currently enabled"
    )

    @model_validator(mode="after")
    def _check_enabled(self):
        if not set(self.enabled) <= set(self.denominations):
            raise ValueError("enabled denominations must be a subset of denominations")
        return self


class PaymentDevices(BaseModel):
    coin: DeviceState
    bill: DeviceState
    cashless: DeviceState


class PaymentState(BaseModel):
    """Retained board state on `payment/state`.

    Republished on change, on `force_report`, and every
    `STATE_REPUBLISH_SECONDS`. A retained document never means the board is
    alive (heartbeats do).
    """

    boot_id: str = Field(..., min_length=8, max_length=32)
    seq_last: Annotated[StrictInt, Field(ge=0)] = Field(
        ..., description="Last event seq sent this boot (0 = none yet)"
    )
    currency: Literal["USD"] = "USD"
    policy_id: Optional[str] = Field(..., min_length=1, max_length=64)
    accepting: bool = Field(..., description="Board is accepting money")
    inhibited_reason: Optional[InhibitReason]
    escrow_cents: Cents
    bill_in_escrow_cents: Optional[Cents]
    devices: PaymentDevices
    tubes: list[TubeLevel]
    exact_change: bool
    stacker: Optional[StackerState]
    session: Optional[CashlessSession]
    ts: datetime = Field(default_factory=_utc_now)

    @model_validator(mode="after")
    def _check_accepting(self):
        if self.accepting != (self.inhibited_reason is None):
            raise ValueError(
                "accepting is true if and only if inhibited_reason is None"
            )
        return self


# --- Commands (edge -> board, on the shared `cmd/mdb` channel) ----------------


class PayoutReason(str, Enum):
    change = "change"
    session_timeout = "session_timeout"
    cancel = "cancel"
    vend_failed = "vend_failed"
    maintenance = "maintenance"
    admin = "admin"
    error = "error"
    customer = "customer"


class VendOutcome(str, Enum):
    success = "success"
    failure = "failure"


class SetPolicyParams(BaseModel):
    policy_id: str = Field(..., min_length=1, max_length=64)
    accept: bool
    max_escrow_cents: PositiveCents
    coin_enabled: list[PositiveCents]
    bill_enabled: list[PositiveCents]
    cashless_enabled: bool
    change_threshold_cents: Cents
    edge_timeout_s: Annotated[StrictInt, Field(ge=5, le=120)]
    return_bill_on_lever: bool


class PayoutParams(BaseModel):
    cents: PositiveCents
    reason: PayoutReason
    sale_id: Optional[str] = None


class VendRequestParams(BaseModel):
    session_id: Optional[str] = None
    cents: PositiveCents
    item: str = Field(..., min_length=1, max_length=64)


class VendResultParams(BaseModel):
    session_id: str = Field(..., min_length=1, max_length=64)
    request_id: str = Field(..., min_length=8, max_length=64)
    outcome: VendOutcome


class EndSessionParams(BaseModel):
    session_id: str = Field(..., min_length=1, max_length=64)


class DeviceResetParams(BaseModel):
    device: Device


PAYMENT_COMMANDS: tuple[str, ...] = (
    "set_policy",
    "payout",
    "vend_request",
    "vend_result",
    "end_session",
    "return_escrow",
    "device_reset",
)

# `return_escrow` takes no params, so it has no model.
PARAM_MODELS: dict[str, type[BaseModel]] = {
    "set_policy": SetPolicyParams,
    "payout": PayoutParams,
    "vend_request": VendRequestParams,
    "vend_result": VendResultParams,
    "end_session": EndSessionParams,
    "device_reset": DeviceResetParams,
}

# The future testable set for the `mdb` subsystem. `TESTABLE_COMMANDS["mdb"]`
# in common.py is flipped to this by spec 2, not here.
MDB_TESTABLE_COMMANDS: frozenset[str] = frozenset(STANDARD_COMMANDS) | {
    "device_reset",
    "payout",
}

# Fixed completion timeouts (durations, not money), read by
# `common.COMPLETION_TIMEOUTS`.
PAYOUT_COMPLETION_TIMEOUT_SECONDS = 60.0
VEND_REQUEST_COMPLETION_TIMEOUT_SECONDS = 45.0

DEFAULT_EDGE_TIMEOUT_SECONDS = 30
EDGE_IDLE_PING_SECONDS = 10
