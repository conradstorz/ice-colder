"""
Shared contract models for the vending-machine ESP32 interface (v0.2.0).

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
from typing import Optional

from pydantic import BaseModel, Field

from contracts.common import ChannelDescriptor, _utc_now

CONTRACT_VERSION = "0.2.0"


class DispenserOutcome(str, Enum):
    """Terminal states a dispense can end in, published as DispenserStatus.state.

    Any other DispenserStatus.state string is an intermediate step
    (motor_active, fill_complete, solenoid_open, ...) and never ends a sale.
    """

    complete = "complete"
    bin_empty = "bin_empty"
    timeout = "timeout"
    jam = "jam"
    error = "error"


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
    PWR_101 = "PWR-101"
    PWR_102 = "PWR-102"
    COM_101 = "COM-101"
    COM_102 = "COM-102"
    COM_103 = "COM-103"
    SVC_101 = "SVC-101"


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
        description="Dispense/agitator motor fault",
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
}

# Which fault a terminal dispenser outcome raises. `complete` is not a fault.
OUTCOME_FAULTS: dict[DispenserOutcome, FaultCode] = {
    DispenserOutcome.bin_empty: FaultCode.ICE_101,
    DispenserOutcome.timeout: FaultCode.ICE_301,
    DispenserOutcome.jam: FaultCode.ICE_401,
    DispenserOutcome.error: FaultCode.ICE_302,
}


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
