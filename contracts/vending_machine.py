"""
Shared contract models for the vending-machine ESP32 interface (v0.8.0).

Terminal dispenser outcomes, the fault-code registry, the refund
command/ack, and the general subsystem-capabilities self-description
exchanged between ice-colder (the VMC) and the vending ESP32 / MDB payment
gateway / ice-maker monitor. JSON Schemas are generated from these models
into docs/contracts/vending-machine/schemas/ by contracts/generate.py.
The VMC and the simulators both import from here so the two sides cannot
drift apart silently. Breaking changes require a major CONTRACT_VERSION
bump; adding a FaultCode or an enum member is a minor bump.
"""

from datetime import datetime
from enum import Enum
from typing import Literal, Optional

from pydantic import BaseModel, Field

from contracts.common import ChannelDescriptor, _utc_now

# 0.4.0 -> 0.5.0: minor bump, absorbing two additive changes. (1) SVC-102
# (new FaultCode + FAULT_TABLE entry + PAYMENT_BLOCKING_FAULTS member,
# six -> seven). (2) Part 3's DATA-101 description wording fix
# ("Sale journal in use; sales are being written to a fallback file" ->
# "Sale write failed; held in fallback file") shipped without its own bump
# at the time; that deferred bump is absorbed here too.
#
# 0.5.0 -> 0.6.0 (2026-09-29): minor bump, additive. `CommandAck` gains
# `phase` ("accepted" | "completed", default "completed" -- a present-day
# ack payload with no `phase` key still validates and means exactly what
# it always meant). A long-running actuator command (`dispense`,
# `water_valve`) now acks "accepted" as soon as it starts, and reports
# completion separately -- see `contracts/common.py`'s COMPLETION_TIMEOUTS
# and docs/contracts/vending-machine/CONTRACT.md's completion table. No
# existing field changed shape or meaning; every present-day ack and
# command payload still validates unchanged.
#
# 0.6.0 -> 0.7.0 (2026-09-30): minor bump, additive. `ChannelDescriptor`
# (contracts/common.py) gains `direction` ("input" | "output", default
# "input") and `driven_by` (str | None, default None) so a board's
# capabilities document can say which channels it drives versus senses,
# and which command's refusal inhibits an output. Both fields are
# optional with defaults, so every present-day channel descriptor still
# validates unchanged.
#
# 0.7.0 -> 0.8.0 (plan 1): minor bump, additive. FaultCode gains CFG-101
# (no valid dispenser profile for a slot, product-scope) and CFG-102
# (dispensers.toml could not be read, machine-scope). Neither blocks
# payment. Plan 2 adds DispenseCommand/DispenseStep under the same
# version.
CONTRACT_VERSION = "0.8.0"


class DispenserOutcome(str, Enum):
    """Terminal states a dispense can end in, published as DispenserStatus.state.

    Any other DispenserStatus.state string is an intermediate step --
    DispenseStep (below) lists the ones this contract's boards emit
    (agitate, fill, release) -- and never ends a sale.
    """

    complete = "complete"
    bin_empty = "bin_empty"
    timeout = "timeout"
    jam = "jam"
    error = "error"
    door_open = "door_open"
    no_flow = "no_flow"
    over_dispense = "over_dispense"


class DispenseStep(str, Enum):
    """Intermediate steps a board reports mid-dispense, published as
    DispenserStatus.state before the terminal DispenserOutcome. A
    water-fill slot only ever emits `fill`; a bagged-ice slot emits all
    three, in order."""

    agitate = "agitate"
    fill = "fill"
    release = "release"


class FaultCode(str, Enum):
    """Stable fault codes (ROADMAP.md §5). Never renumber; only add."""

    ICE_101 = "ICE-101"
    ICE_201 = "ICE-201"
    ICE_202 = "ICE-202"
    ICE_301 = "ICE-301"
    ICE_302 = "ICE-302"
    ICE_401 = "ICE-401"
    ICE_402 = "ICE-402"
    WTR_101 = "WTR-101"
    WTR_102 = "WTR-102"
    WTR_103 = "WTR-103"
    WTR_104 = "WTR-104"
    WTR_105 = "WTR-105"
    ENV_101 = "ENV-101"
    ENV_102 = "ENV-102"
    ENV_103 = "ENV-103"
    PAY_101 = "PAY-101"
    PAY_102 = "PAY-102"
    PAY_103 = "PAY-103"
    PAY_104 = "PAY-104"
    PWR_101 = "PWR-101"
    PWR_102 = "PWR-102"
    COM_101 = "COM-101"
    COM_102 = "COM-102"
    COM_103 = "COM-103"
    SVC_101 = "SVC-101"
    SVC_102 = "SVC-102"
    DATA_101 = "DATA-101"
    DATA_102 = "DATA-102"
    CFG_101 = "CFG-101"
    CFG_102 = "CFG-102"


class Severity(str, Enum):
    info = "info"  # logged only
    warning = "warning"  # alerts the owner
    product_unavailable = "product_unavailable"  # locks a product; clears itself
    vend_failed = "vend_failed"  # ends the sale; no lockout
    lockout = "lockout"  # locks a product until an admin clears it
    critical = "critical"  # machine-scope; never auto-clears


class Scope(str, Enum):
    product = "product"
    machine = "machine"


class FaultSpec(BaseModel):
    severity: Severity
    scope: Scope
    description: str


FAULT_TABLE: dict[FaultCode, FaultSpec] = {
    FaultCode.ICE_101: FaultSpec(
        severity=Severity.product_unavailable,
        scope=Scope.product,
        description="Ice unavailable (hopper low / maker bin empty)",
    ),
    FaultCode.ICE_201: FaultSpec(
        severity=Severity.product_unavailable,
        scope=Scope.product,
        description="Bag not detected",
    ),
    FaultCode.ICE_202: FaultSpec(
        severity=Severity.vend_failed,
        scope=Scope.product,
        description="Bag lost during fill",
    ),
    FaultCode.ICE_301: FaultSpec(
        severity=Severity.lockout,
        scope=Scope.product,
        description="Fill timeout (full-bag sensor never tripped)",
    ),
    FaultCode.ICE_302: FaultSpec(
        severity=Severity.lockout,
        scope=Scope.product,
        description=(
            "Dispense actuator fault reported by the board "
            "(motor stall, valve driver, over-current)"
        ),
    ),
    FaultCode.ICE_401: FaultSpec(
        severity=Severity.lockout,
        scope=Scope.product,
        description="Trap door / bag release failed to open",
    ),
    FaultCode.ICE_402: FaultSpec(
        severity=Severity.critical,
        scope=Scope.machine,
        description="Trap door failed to close",
    ),
    FaultCode.WTR_101: FaultSpec(
        severity=Severity.vend_failed,
        scope=Scope.product,
        description="No flow after valve open",
    ),
    FaultCode.WTR_102: FaultSpec(
        severity=Severity.lockout,
        scope=Scope.product,
        description="Over-dispense (flow pulses exceeded)",
    ),
    FaultCode.WTR_103: FaultSpec(
        severity=Severity.critical,
        scope=Scope.machine,
        description="Flow continues after valve close",
    ),
    FaultCode.WTR_104: FaultSpec(
        severity=Severity.critical,
        scope=Scope.machine,
        description="Leak / overflow detected",
    ),
    FaultCode.WTR_105: FaultSpec(
        severity=Severity.product_unavailable,
        scope=Scope.product,
        description="Water pressure or treatment status failed",
    ),
    FaultCode.ENV_101: FaultSpec(
        severity=Severity.warning,
        scope=Scope.machine,
        description="Cabinet below freeze threshold",
    ),
    FaultCode.ENV_102: FaultSpec(
        severity=Severity.critical,
        scope=Scope.machine,
        description="Heater ineffective (low temperature persists)",
    ),
    FaultCode.ENV_103: FaultSpec(
        severity=Severity.critical,
        scope=Scope.machine,
        description="Heater high-limit tripped",
    ),
    FaultCode.PAY_101: FaultSpec(
        severity=Severity.product_unavailable,
        scope=Scope.machine,
        description="Payment device offline",
    ),
    FaultCode.PAY_102: FaultSpec(
        severity=Severity.vend_failed,
        scope=Scope.product,
        description="No dispense report within the timeout after credit taken",
    ),
    FaultCode.PAY_103: FaultSpec(
        severity=Severity.warning,
        scope=Scope.machine,
        description="Refund not confirmed by payment gateway; needs reconciliation",
    ),
    FaultCode.PAY_104: FaultSpec(
        severity=Severity.warning,
        scope=Scope.machine,
        description="Transaction uncertain after VMC restart; operator must reconcile",
    ),
    FaultCode.PWR_101: FaultSpec(
        severity=Severity.info,
        scope=Scope.machine,
        description="Power restored after loss",
    ),
    FaultCode.PWR_102: FaultSpec(
        severity=Severity.critical,
        scope=Scope.machine,
        description="24 V control supply bad",
    ),
    FaultCode.COM_101: FaultSpec(
        severity=Severity.product_unavailable,
        scope=Scope.machine,
        description="Vending ESP32 heartbeat lost",
    ),
    FaultCode.COM_102: FaultSpec(
        severity=Severity.warning,
        scope=Scope.machine,
        description="Ice-maker monitor heartbeat lost",
    ),
    FaultCode.COM_103: FaultSpec(
        severity=Severity.warning,
        scope=Scope.machine,
        description="MQTT broker unreachable",
    ),
    FaultCode.SVC_101: FaultSpec(
        severity=Severity.info,
        scope=Scope.machine,
        description="Service door open / service mode",
    ),
    FaultCode.SVC_102: FaultSpec(
        severity=Severity.warning,
        scope=Scope.machine,
        description="Maintenance test in progress",
    ),
    FaultCode.DATA_101: FaultSpec(
        severity=Severity.warning,
        scope=Scope.machine,
        description="Sale write failed; held in fallback file",
    ),
    FaultCode.DATA_102: FaultSpec(
        severity=Severity.warning,
        scope=Scope.machine,
        description=(
            "Event database was reset after corruption; history before the "
            "reset is lost"
        ),
    ),
    FaultCode.CFG_101: FaultSpec(
        severity=Severity.product_unavailable,
        scope=Scope.product,
        description="No valid dispenser profile for this slot (see dispensers.toml)",
    ),
    FaultCode.CFG_102: FaultSpec(
        severity=Severity.warning,
        scope=Scope.machine,
        description="dispensers.toml could not be read (missing or syntax error)",
    ),
}

# The only faults that may inhibit payment. Everything else — bookkeeping
# doubt (PAY-104), heartbeat loss, broker loss, an empty bin — alerts the
# operator and blocks the individual sale, but never stops the machine taking
# money. Membership here, not severity, is the gate: adding a fault code can
# never silently stop the machine, because stopping it requires editing this
# frozenset on purpose.
#
# SVC-102 is the one member here that is not `critical`: it is a deliberate
# operator-held maintenance lease (services/availability.py's `safety` gate),
# not a hardware failure, and it clears itself the moment the lease is
# released — the opposite of `critical`'s "never auto-clears". `warning` is
# the closest fit of the existing severities (it already covers every other
# machine-scope condition that "alerts the operator" without implying a
# lockout or an unclearable state); its membership here, not its severity,
# is what makes it block payment.
PAYMENT_BLOCKING_FAULTS: frozenset[FaultCode] = frozenset(
    {
        FaultCode.ICE_402,  # trap door failed to close
        FaultCode.WTR_103,  # flow continues after valve close
        FaultCode.WTR_104,  # leak / overflow detected
        FaultCode.ENV_102,  # heater ineffective
        FaultCode.ENV_103,  # heater high-limit tripped
        FaultCode.PWR_102,  # 24 V control supply bad
        FaultCode.SVC_102,  # maintenance test in progress (operator lease)
    }
)

# The two dispense mechanisms a slot profile can declare (plan 1,
# services/dispenser_schema.py's SlotProfile discriminator). Kept here,
# not imported from services, so contracts never depends on services.
Mechanism = Literal["bagged_ice", "water_fill"]
MECHANISMS: tuple[str, ...] = ("bagged_ice", "water_fill")

# Which fault a terminal dispenser outcome raises, keyed by (mechanism,
# outcome). `complete` is never a key -- fault_for_outcome raises KeyError
# if asked for it. Not every (mechanism, outcome) combination appears:
# `no_flow`/`over_dispense` are water-only outcomes and `jam`/`door_open`
# are bagged-ice-only outcomes, matching what each mechanism's board can
# actually report (spec §6.4). `bin_empty` maps to ICE-101 for both.
OUTCOME_FAULTS: dict[tuple[str, DispenserOutcome], FaultCode] = {
    ("bagged_ice", DispenserOutcome.timeout): FaultCode.ICE_301,
    ("bagged_ice", DispenserOutcome.error): FaultCode.ICE_302,
    ("bagged_ice", DispenserOutcome.jam): FaultCode.ICE_401,
    ("bagged_ice", DispenserOutcome.door_open): FaultCode.ICE_402,
    ("water_fill", DispenserOutcome.no_flow): FaultCode.WTR_101,
    ("water_fill", DispenserOutcome.over_dispense): FaultCode.WTR_102,
    ("water_fill", DispenserOutcome.timeout): FaultCode.WTR_101,
    ("water_fill", DispenserOutcome.error): FaultCode.ICE_302,
    ("bagged_ice", DispenserOutcome.bin_empty): FaultCode.ICE_101,
    ("water_fill", DispenserOutcome.bin_empty): FaultCode.ICE_101,
}


def fault_for_outcome(mechanism: str, outcome: DispenserOutcome) -> FaultCode:
    """Which fault a terminal dispenser outcome raises for one mechanism.

    The only reader of OUTCOME_FAULTS -- callers must not index that dict
    directly. `complete` is never mapped; raises KeyError naming both the
    mechanism and the outcome when the pair has no mapping.
    """

    try:
        return OUTCOME_FAULTS[(mechanism, outcome)]
    except KeyError:
        raise KeyError(
            f"no fault mapped for mechanism {mechanism!r}, outcome {outcome!r}"
        ) from None


class RefundStatus(str, Enum):
    ok = "ok"
    failed = "failed"
    unsupported = "unsupported"


class PaymentRefundCommand(BaseModel):
    """VMC -> payment gateway, published on cmd/payment/refund (QoS 1)."""

    request_id: str = Field(
        ...,
        min_length=8,
        max_length=64,
        description="Opaque correlation key, unique per refund; UUID4 hex by the VMC",
    )
    amount: float = Field(..., gt=0, description="Amount to pay out, USD")
    reason: str = Field(
        ...,
        description="A FaultCode value, or 'session_timeout', 'cancel', 'error', 'admin'",
    )
    timestamp: datetime = Field(default_factory=_utc_now)


class PaymentRefundResult(BaseModel):
    """Payment gateway -> VMC, published on cmd/payment/refund/ack (QoS 1).

    Exactly one per command; a repeated request_id is answered with the
    previously computed result and never paid twice.
    """

    request_id: str = Field(..., description="Echoed from the command")
    status: RefundStatus
    amount_returned: float = Field(0.0, ge=0, description="Amount actually paid out")
    detail: Optional[str] = Field(None, description="e.g. 'changer_empty'")
    timestamp: datetime = Field(default_factory=_utc_now)


class SubsystemCapabilities(BaseModel):
    """Retained self-description on capabilities/<subsystem>.

    Published on connect and whenever any declared property changes. A
    retained document never means the subsystem is alive — only heartbeats
    do — it means "this is what that board is, if and when it is up".
    """

    subsystem: str = Field(..., pattern=r"^[a-z0-9_]{1,32}$")
    firmware: str = Field(..., description="Software/firmware version string")
    contract_version: str = Field(..., description="Contract semver implemented")
    brand: str = Field("", description="Hardware brand, if meaningful")
    model: str = Field("", description="Hardware model, if meaningful")
    hardware_id: Optional[str] = Field(None, description="MAC or serial number")
    ip: Optional[str] = Field(None, description="IPv4/IPv6 address on the LAN")
    channels: list[ChannelDescriptor] = Field(default_factory=list)
    commands: list[str] = Field(default_factory=list)
    timestamp: datetime = Field(default_factory=_utc_now)


# Subsystems the dashboard always lists, even before they have ever spoken.
EXPECTED_SUBSYSTEMS: tuple[str, ...] = ("vending", "mdb", "ice_maker")
