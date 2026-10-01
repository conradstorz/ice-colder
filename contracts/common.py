# contracts/common.py
"""Pieces shared by every contract module. Contracts import from here, never
from each other.

The subsystem command channel (§1.1 of the system-tests design) lives here
too: every subsystem subscribes to `vmc/<machine_id>/cmd/<subsystem>` and
acks on `.../ack` using `SubsystemCommand`/`CommandAck`. The ice-maker
monitor module re-exports these under their original names
(`MonitorCommand`, `CommandAck`) so every existing import keeps working —
they are the *same* classes, not copies or subclasses.
"""

from collections.abc import Callable
from datetime import datetime, timezone
from typing import Literal, Optional

from pydantic import BaseModel, Field, model_validator

CHANNEL_ID_PATTERN = r"^[a-z0-9_]{1,64}$"

# A subsystem must ack within this many seconds of a command being sent, or
# the dispatcher (services/command_dispatcher.py, a later task) treats it as
# a timeout. The contract owns this constant so the dispatcher and the docs
# cannot drift apart.
ACK_TIMEOUT_SECONDS = 10.0


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class ChannelDescriptor(BaseModel):
    """One telemetry channel the monitor declares in its capabilities."""

    channel_id: str = Field(
        ..., pattern=CHANNEL_ID_PATTERN, description="Slug; also the topic segment"
    )
    kind: Literal["temperature", "current", "voltage", "level", "binary", "counter"]
    unit: str = Field("", description="Unit, e.g. 'C', 'A', '%'; empty for binary")
    description: str = Field("", description="Human-readable channel description")
    interval_seconds: float = Field(
        ..., gt=0, le=3600, description="Declared publish cadence"
    )
    direction: Literal["input", "output"] = Field(
        "input",
        description=(
            "Output = something the board drives (motor, solenoid, relay, "
            "compressor). Input = something it senses."
        ),
    )
    driven_by: str | None = Field(
        None,
        description="Command whose refusal by the VMC inhibits this signal",
    )


# --- Subsystem command channel (§1.1) ---------------------------------------
#
# Per-command param validation, keyed by command name. A new command that
# needs validation adds one entry here instead of growing a model_validator
# chain that only knows about one subsystem. A command with no entry (or an
# entry not yet registered — an unknown command name) constructs fine: the
# subsystem answers `unsupported` at runtime, the model never rejects it.


def _validate_power_cycle(params: dict) -> None:
    dwell = params.get("dwell_seconds")
    if dwell is None or not (5 <= dwell <= 300):
        raise ValueError("power_cycle requires dwell_seconds in [5, 300]")


def _validate_set_interval(params: dict) -> None:
    interval = params.get("interval_seconds")
    if interval is None or not (1 <= interval <= 3600):
        raise ValueError("set_interval requires interval_seconds in [1, 3600]")


def _validate_water_valve(params: dict) -> None:
    seconds = params.get("seconds")
    if seconds is None or not (1 <= seconds <= 10):
        raise ValueError("water_valve requires seconds in [1, 10]")


COMMAND_PARAM_VALIDATORS: dict[str, Callable[[dict], None]] = {
    "power_cycle": _validate_power_cycle,
    "set_interval": _validate_set_interval,
    "water_valve": _validate_water_valve,
}


class SubsystemCommand(BaseModel):
    """VMC -> subsystem command on `vmc/<machine_id>/cmd/<subsystem>`.

    Wire-identical to the ice-maker monitor's original `MonitorCommand`
    (that name is now an alias for this class): `command` widens from a
    three-value `Literal` to `str` so every subsystem can share one model,
    and `params` widens from `dict[str, float]` to a plain `dict`. Per-command
    validation moves to `COMMAND_PARAM_VALIDATORS` above so widening the type
    does not lose `power_cycle`'s or `set_interval`'s existing rules.
    """

    request_id: str = Field(..., min_length=8, max_length=64)
    command: str = Field(..., min_length=1)
    params: dict = Field(default_factory=dict)
    timestamp: datetime = Field(default_factory=_utc_now)

    @model_validator(mode="after")
    def _check_params(self):
        validator = COMMAND_PARAM_VALIDATORS.get(self.command)
        if validator is not None:
            validator(self.params)
        return self


class CommandAck(BaseModel):
    """Subsystem -> VMC acknowledgement on `.../cmd/<subsystem>/ack`.

    Every field present in the original ice-maker `CommandAck` is unchanged;
    `result` was the first addition (optional, default None). `phase` is the
    second (completion-table amendment, 2026-09-29): "completed" (the
    default) means this ack IS the command's outcome -- true for every
    command whose handler finishes within the ack, and the only value a
    present-day subsystem that has never heard of `phase` will ever
    implicitly send, so an old ack payload with no `phase` key still
    validates and still means what it always meant. "accepted" means the
    opposite: the command was only *started*, not finished -- a long-running
    actuator command (`dispense`, `water_valve`, `power_cycle`) sends this
    first, then reports completion separately (its own terminal event, for
    `dispense`, or a second "completed"-phase ack on this same topic and
    `request_id` for `water_valve`/`power_cycle`) once the real work is
    done. See `COMPLETION_TIMEOUTS` below and both CONTRACT.md files'
    completion tables.
    """

    request_id: str = Field(..., description="Echoed from the command")
    command: str
    status: Literal["ok", "rejected", "failed", "unsupported"]
    detail: Optional[str] = None
    result: Optional[dict] = None
    phase: Literal["accepted", "completed"] = "completed"
    timestamp: datetime = Field(default_factory=_utc_now)


# Standard commands every subsystem answers (§1.2). Public (not
# underscore-prefixed): simulators/base.py imports this directly so the
# three standard handlers it registers on every subsystem
# (register_command("ping", ...) etc. in ESP32Simulator.__init__) and the
# commands a subsystem's capabilities document actually *advertises* can
# never drift apart the way TESTABLE_COMMANDS and
# SubsystemCapabilities.commands did before (Copilot review, PR 22,
# id=4128088689): the three handlers were registered and answered on the
# wire, but the advertised commands list omitted them, so the Tests
# routes' advertised-∩-allowlist intersection silently dropped every
# automatic test.
#
# A tuple (not a frozenset): simulators/base.py splices this, in order,
# into the front of every SubsystemCapabilities.commands list, and
# capabilities tests assert on that list's exact contents; a set's
# iteration order is not a contract to build a wire payload from.
STANDARD_COMMANDS: tuple[str, ...] = ("ping", "self_test", "force_report")
_STANDARD_COMMANDS_SET: frozenset[str] = frozenset(STANDARD_COMMANDS)

# Server-side allowlist for the Tests level (§1.3): exactly the standard
# commands plus each subsystem's actuator commands, and nothing else —
# `SubsystemCapabilities.commands` may advertise control commands such as
# `payment/enable`, `refund`, or `set_interval`, but a test button only ever
# exists for a command that also appears here. `POST /tests/{subsystem}/
# {command}` re-checks this allowlist so a crafted request cannot reach a
# control command through the test tile.
TESTABLE_COMMANDS: dict[str, frozenset[str]] = {
    "vending": _STANDARD_COMMANDS_SET | frozenset({"dispense", "water_valve"}),
    "ice_maker": _STANDARD_COMMANDS_SET | frozenset({"power_cycle"}),
    "mdb": _STANDARD_COMMANDS_SET
    | frozenset({"bill_acceptor_test", "coin_return_test", "card_reader_test"}),
}


# --- Completion table (2026-09-29 amendment) --------------------------------
#
# A previous Copilot review (PR 22, id=4128088504) proved that acking
# `dispense` only once the whole motor cycle finished let the dispatcher's
# ack timeout (10 s, two attempts) give up and release a tech's maintenance
# lease while the simulated motor was still running (`_run_ice_dispense` can
# legitimately run past 20 s, and up to 90 s on the jam path).
#
# The fix: for a long-running command, the ack now means "accepted and
# started", not "completed" (`CommandAck.phase`, above). The ack timeout
# above still governs "did the subsystem accept this at all" and is
# unchanged. A SEPARATE, per-command completion timeout governs "did the
# accepted command actually finish" -- generous, because real actuation
# takes real time, and never a single shared constant, because the commands
# it covers have wildly different real durations (`dispense`'s auger-jam
# path alone is 90 s; `power_cycle`'s `dwell_seconds` is caller-chosen, up
# to 300 s).
#
# Every command NOT in this dict is immediate: its ack IS its completion
# (`CommandAck.phase` stays "completed", the default) -- true today for the
# three standard commands (§1.2) and, verified against each simulator
# handler rather than assumed, the three MDB actuator commands
# (`bill_acceptor_test`/`coin_return_test`/`card_reader_test`, all a single
# counter increment with no `await asyncio.sleep`). A command's presence
# here is what `services/command_dispatcher.py`'s
# `send_and_await_completion` uses to decide whether to wait past the
# accept ack at all -- data, not a per-command `if` branch.

DISPENSE_COMPLETION_TIMEOUT_SECONDS = 120.0  # dispense has no caller-chosen
# duration parameter to derive from; 120 s is a fixed margin over the worst
# case in `simulators/vending_machine.py` today (the 90 s auger-jam path).

WATER_VALVE_COMPLETION_MARGIN_SECONDS = 5.0  # added to `seconds` (1-10):
# 6-15 s total, comfortably above the valve's own open duration.

POWER_CYCLE_COMPLETION_MARGIN_SECONDS = 30.0  # added to `dwell_seconds`
# (5-300): 35-330 s total. `dwell_seconds` can legitimately be 300 s (the
# ice-maker's own lockout window) -- a fixed 120 s timeout would spuriously
# fail that legitimate case, which is why this is derived from the
# parameter instead of a constant.


def _dispense_completion_timeout(params: dict) -> float:
    return DISPENSE_COMPLETION_TIMEOUT_SECONDS


def _water_valve_completion_timeout(params: dict) -> float:
    seconds = params.get("seconds", 10)
    return float(seconds) + WATER_VALVE_COMPLETION_MARGIN_SECONDS


def _power_cycle_completion_timeout(params: dict) -> float:
    dwell = params.get("dwell_seconds", 30)
    return float(dwell) + POWER_CYCLE_COMPLETION_MARGIN_SECONDS


# (subsystem, command) -> params -> completion timeout, seconds. Absence of
# an entry means "immediate" (see the module note above). Every long-running
# entry in TESTABLE_COMMANDS must have a matching entry here --
# tests/test_contracts_common.py asserts that so the two tables cannot drift
# apart the way TESTABLE_COMMANDS and SubsystemCapabilities.commands did
# before (Copilot review, PR 22, id=4128088689).
COMPLETION_TIMEOUTS: dict[tuple[str, str], Callable[[dict], float]] = {
    ("vending", "dispense"): _dispense_completion_timeout,
    ("vending", "water_valve"): _water_valve_completion_timeout,
    ("ice_maker", "power_cycle"): _power_cycle_completion_timeout,
}
