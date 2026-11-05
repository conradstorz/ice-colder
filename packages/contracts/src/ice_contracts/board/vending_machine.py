"""
Shared contract models for the vending-machine ESP32 interface (v1.0.0).

Terminal dispenser outcomes, the fault-code registry, the refund
command/ack, and the general subsystem-capabilities self-description
exchanged between ice-colder (the VMC) and the vending ESP32 / MDB payment
gateway / ice-maker monitor. JSON Schemas are generated from these models
into docs/contracts/vending-machine/schemas/ by ice_contracts/generate.py.
The VMC and the simulators both import from here so the two sides cannot
drift apart silently. Breaking changes require a major CONTRACT_VERSION
bump; adding a FaultCode or an enum member is a minor bump.
"""

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Literal, Optional

from pydantic import BaseModel, Field

from ice_contracts.board.common import ChannelDescriptor, _utc_now

# Version history: docs/architecture-history.md (Contract version history).
CONTRACT_VERSION = "3.0.0"


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
    WTR_106 = "WTR-106"
    WTR_107 = "WTR-107"
    ENV_101 = "ENV-101"
    ENV_102 = "ENV-102"
    ENV_103 = "ENV-103"
    PAY_101 = "PAY-101"
    PAY_102 = "PAY-102"
    PAY_103 = "PAY-103"
    PAY_104 = "PAY-104"
    PAY_105 = "PAY-105"
    PAY_106 = "PAY-106"
    PAY_107 = "PAY-107"
    PAY_108 = "PAY-108"
    PWR_101 = "PWR-101"
    PWR_102 = "PWR-102"
    COM_101 = "COM-101"
    COM_102 = "COM-102"
    COM_103 = "COM-103"
    SVC_101 = "SVC-101"
    SVC_102 = "SVC-102"
    DATA_101 = "DATA-101"
    DATA_102 = "DATA-102"
    DATA_103 = "DATA-103"
    DATA_104 = "DATA-104"
    DATA_105 = "DATA-105"
    DATA_106 = "DATA-106"
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
        severity=Severity.warning,
        scope=Scope.machine,
        description="Water pressure or treatment status failed",
    ),
    FaultCode.WTR_106: FaultSpec(
        severity=Severity.warning,
        scope=Scope.machine,
        description="RO storage tank empty",
    ),
    FaultCode.WTR_107: FaultSpec(
        severity=Severity.warning,
        scope=Scope.machine,
        description="RO production overran its maximum run time",
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
    FaultCode.PAY_105: FaultSpec(
        severity=Severity.warning,
        scope=Scope.machine,
        description="Payment event gap; cash count may be off (board replay missed or unknown event kind)",
    ),
    FaultCode.PAY_106: FaultSpec(
        severity=Severity.warning,
        scope=Scope.machine,
        description="Coin changer fault",
    ),
    FaultCode.PAY_107: FaultSpec(
        severity=Severity.warning,
        scope=Scope.machine,
        description="Bill validator fault",
    ),
    FaultCode.PAY_108: FaultSpec(
        severity=Severity.warning,
        scope=Scope.machine,
        description="Cashless reader fault",
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
    FaultCode.DATA_103: FaultSpec(
        severity=Severity.warning,
        scope=Scope.machine,
        description=(
            "Event database could not be initialised; writes are still "
            "attempted and a failed sale write is journalled to the fallback "
            "file"
        ),
    ),
    FaultCode.DATA_104: FaultSpec(
        severity=Severity.warning,
        scope=Scope.machine,
        description=(
            "Session snapshot could not be saved; a crash now would lose the "
            "live sale's evidence"
        ),
    ),
    FaultCode.DATA_105: FaultSpec(
        severity=Severity.warning,
        scope=Scope.machine,
        description=(
            "Event row could not be written; KPIs and history may be incomplete"
        ),
    ),
    FaultCode.DATA_106: FaultSpec(
        severity=Severity.warning,
        scope=Scope.machine,
        description=(
            "Inventory counts could not be saved; stock levels revert on restart"
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
# operator-held maintenance lease (ice_edge/faults/availability.py's `safety` gate),
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
# ice_contracts/board/dispenser_schema.py's SlotProfile discriminator). Kept here,
# not imported from services, so contracts never depends on services.
Mechanism = Literal["bagged_ice", "water_fill"]


class VendingChannel(str, Enum):
    """Channel ids on the vending board that carry meaning to the edge.

    The single source for these strings, so the simulator and the edge can
    never disagree. All are optional: a board that declares none of them
    is valid. The enum also names sim-only channels the edge does not
    interpret (e.g. `heater_power`). Adding a member is additive and needs
    no contract version bump."""

    bin_half_full = "bin_half_full"
    sump_high = "sump_high"
    water_pressure_ok = "water_pressure_ok"
    ro_tank_empty = "ro_tank_empty"
    ro_tank_full = "ro_tank_full"
    ro_production = "ro_production"
    heater_power = "heater_power"
    service_door = "service_door"


@dataclass(frozen=True)
class InputFault:
    """A binary input channel whose reported state raises a fault."""

    channel: VendingChannel
    faulted_when: bool  # the reported state that means "faulted"
    code: FaultCode


# `ro_production`'s WTR-107 is a duration rule, not a state, so it is not
# listed here. Every code is machine-scope and never locks a product.
INPUT_FAULTS: tuple[InputFault, ...] = (
    InputFault(VendingChannel.sump_high, True, FaultCode.WTR_104),
    InputFault(VendingChannel.water_pressure_ok, False, FaultCode.WTR_105),
    InputFault(VendingChannel.ro_tank_empty, True, FaultCode.WTR_106),
)


# Which fault a terminal dispenser outcome raises, keyed by (mechanism,
# outcome). `complete` is never a key -- fault_for_outcome raises KeyError
# if asked for it. Not every (mechanism, outcome) combination appears:
# `no_flow`/`over_dispense` are water-only outcomes and `jam`/`door_open`
# are bagged-ice-only outcomes, matching what each mechanism's board can
# actually report (spec §6.4). `bin_empty` maps to ICE-101 for bagged ice and WTR-106 for water.
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
    ("water_fill", DispenserOutcome.bin_empty): FaultCode.WTR_106,
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
