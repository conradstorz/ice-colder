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
    `result` is the only addition (optional, default None), so a present-day
    ack payload with no `result` key still validates.
    """

    request_id: str = Field(..., description="Echoed from the command")
    command: str
    status: Literal["ok", "rejected", "failed", "unsupported"]
    detail: Optional[str] = None
    result: Optional[dict] = None
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
