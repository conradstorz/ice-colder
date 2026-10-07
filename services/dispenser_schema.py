# services/dispenser_schema.py
"""Pydantic v2 models for one `[slot.N]` table of the hand-edited
`dispensers.toml` (plan: dispenser profiles). Each table describes the
physical parameters of one dispense slot: how its mechanism agitates/fills/
releases (bagged ice) or fills (water), and any accessories (fans, lights,
auger-clearing blowers) that run alongside those steps.

This module is schema only -- no file I/O, no TOML parsing. Task 3 loads
`dispensers.toml`, calls `TypeAdapter(SlotProfile).validate_python(table)`
on each `[slot.N]` table, and cross-checks `worst_case_seconds`,
`drive_channels`, `sense_channels` against the rest of the machine's
configuration.

Every physical field is required; the only field with a default is
`accessories` (an empty dict -- most slots have none). Every model forbids
unknown keys (`extra="forbid"`) so a typo in `dispensers.toml` is caught at
validation time rather than silently ignored. A sensor that a board does not
have is spelled out as the literal string `"unmonitored"`, never omitted --
that keeps every profile's shape self-documenting even when a cheaper board
can't report the signal.
"""

from typing import Annotated, ClassVar, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)

from contracts.common import CHANNEL_ID_PATTERN

# A sensor or current-sense pair a board does not have is spelled out with
# this literal string rather than omitted, so every profile stays
# self-documenting about what it can and cannot monitor.
UNMONITORED = Literal["unmonitored"]
UNMONITORED_VALUE = "unmonitored"


def _reject_unmonitored(value: str) -> str:
    """`"unmonitored"` matches `CHANNEL_ID_PATTERN` (it's just lowercase
    letters and underscores), so without this check it would silently
    validate for a channel field that is actually required -- skipping
    the capabilities and drive/sense cross-checks for a sensor the
    profile claims to have. Only `current_channel` (paired with
    `stall_current_amps` in `CurrentSense`) is allowed to say it has no
    sensor, via the separate `ChannelId | UNMONITORED` union below -- that
    union's `UNMONITORED` arm matches the sentinel directly and never
    reaches this validator."""

    if value == UNMONITORED_VALUE:
        raise ValueError(
            '"unmonitored" is only allowed for stall_current_amps/current_channel; '
            "this channel is required"
        )
    return value


# Every MQTT channel id referenced by a dispenser profile (motor, solenoid,
# valve, sensor, accessory) reuses the same slug pattern as the rest of the
# system (contracts/common.py). The literal "unmonitored" is syntactically a
# valid channel id under that pattern, so it's rejected separately here --
# `current_channel`'s `ChannelId | UNMONITORED` union still accepts it
# through the `UNMONITORED` arm, which never calls this validator.
ChannelId = Annotated[
    str,
    StringConstraints(pattern=CHANNEL_ID_PATTERN),
    AfterValidator(_reject_unmonitored),
]

# The one place this wording is spelled out -- both the generated example's
# comments (services/dispensers_doc.py) and the humanized validation error
# for a bad channel id (services/dispensers.py) read it from here, so they
# can never drift apart.
CHANNEL_ID_DESCRIPTION = "lowercase letters, digits and underscores, 1–64 characters"


class _StrictModel(BaseModel):
    """Shared base: every dispenser-profile model forbids unknown keys and
    validates in Pydantic's strict mode, so a quoted number
    (`run_seconds = "4.0"`) is rejected instead of silently coerced -- a
    hand-edited TOML file should say what it means. A plain TOML int for a
    float field (`run_seconds = 4`) still validates: Pydantic's strict
    mode explicitly keeps the int-to-float widening, since TOML has no
    separate "this is a float" syntax for a whole number."""

    model_config = ConfigDict(extra="forbid", strict=True)


class CurrentSense(_StrictModel):
    """Optional stall-current protection shared by motor-driven steps.

    A board that reports armature current can trip on a stalled motor
    (jammed auger, seized agitator) faster than any timeout would catch it.
    A board without a current sensor sets both fields to `"unmonitored"`
    instead of omitting them, so the profile still states plainly that this
    protection is absent.
    """

    stall_current_amps: Annotated[float, Field(ge=0.1, le=50)] | UNMONITORED = Field(
        ...,
        description=(
            "Motor current above which the board should treat the "
            'motor as stalled and stop driving it, or "unmonitored" '
            "if this board has no current sensor."
        ),
        json_schema_extra={"unit": "A"},
    )
    current_channel: ChannelId | UNMONITORED = Field(
        ...,
        description=(
            "The telemetry channel that reports this motor's current draw, "
            'or "unmonitored" if this board has no current sensor.'
        ),
    )

    @model_validator(mode="after")
    def _check_both_or_neither(self) -> "CurrentSense":
        amps_set = self.stall_current_amps != UNMONITORED_VALUE
        channel_set = self.current_channel != UNMONITORED_VALUE
        if amps_set != channel_set:
            raise ValueError(
                "stall_current_amps and current_channel must both be set "
                'or both be "unmonitored"'
            )
        return self


class AgitateStep(CurrentSense):
    """The agitate step of a bagged-ice slot: breaks up clumped ice before
    it is fed toward the release gate."""

    motor_channel: ChannelId = Field(
        ..., description="The channel that drives the agitator motor."
    )
    run_seconds: Annotated[float, Field(ge=0.5, le=60)] = Field(
        ...,
        description="How long the agitator motor runs for this step.",
        json_schema_extra={"unit": "s"},
    )


class IceFillBySensor(CurrentSense):
    """The fill step of a bagged-ice slot, proven complete by a bag-full
    sensor rather than by elapsed time alone."""

    proof: Literal["bag_full_sensor"] = Field(
        ..., description="This fill step is proven complete by a sensor."
    )
    motor_channel: ChannelId = Field(
        ..., description="The channel that drives the auger/fill motor."
    )
    sensor_channel: ChannelId = Field(
        ...,
        description="The channel that reports the bag as full.",
    )
    max_run_seconds: Annotated[float, Field(ge=1, le=120)] = Field(
        ...,
        description=(
            "The longest the fill motor is allowed to run before the VMC "
            "gives up on this step even if the bag-full sensor never fires."
        ),
        json_schema_extra={"unit": "s"},
    )


class IceFillTimed(CurrentSense):
    """The fill step of a bagged-ice slot, proven complete by elapsed time
    alone -- used when the slot has no bag-full sensor."""

    proof: Literal["timed"] = Field(
        ..., description="This fill step is proven complete by elapsed time."
    )
    motor_channel: ChannelId = Field(
        ..., description="The channel that drives the auger/fill motor."
    )
    max_run_seconds: Annotated[float, Field(ge=1, le=120)] = Field(
        ...,
        description="How long the fill motor runs for this step.",
        json_schema_extra={"unit": "s"},
    )


IceFillStep = Annotated[IceFillBySensor | IceFillTimed, Field(discriminator="proof")]


class ReleaseBySensor(_StrictModel):
    """The release step of a bagged-ice slot, proven open and closed again
    by a door sensor rather than by elapsed time alone."""

    proof: Literal["door_sensor"] = Field(
        ...,
        description="This release step is proven open/closed by a sensor.",
    )
    solenoid_channel: ChannelId = Field(
        ..., description="The channel that drives the release-gate solenoid."
    )
    sensor_channel: ChannelId = Field(
        ..., description="The channel that reports the release gate's state."
    )
    pulse_seconds: Annotated[float, Field(ge=0.1, le=10)] = Field(
        ...,
        description="How long the solenoid is pulsed to open the gate.",
        json_schema_extra={"unit": "s"},
    )
    open_timeout_seconds: Annotated[float, Field(ge=0.5, le=30)] = Field(
        ...,
        description=(
            "The longest the VMC waits for the door sensor to report open "
            "before treating the release as failed."
        ),
        json_schema_extra={"unit": "s"},
    )
    close_timeout_seconds: Annotated[float, Field(ge=0.5, le=60)] = Field(
        ...,
        description=(
            "The longest the VMC waits for the door sensor to report "
            "closed again before treating the release as failed."
        ),
        json_schema_extra={"unit": "s"},
    )


class ReleaseTimed(_StrictModel):
    """The release step of a bagged-ice slot, proven open and closed again
    by elapsed time alone -- used when the slot has no door sensor."""

    proof: Literal["timed"] = Field(
        ...,
        description="This release step is proven open/closed by elapsed time.",
    )
    solenoid_channel: ChannelId = Field(
        ..., description="The channel that drives the release-gate solenoid."
    )
    pulse_seconds: Annotated[float, Field(ge=0.1, le=10)] = Field(
        ...,
        description="How long the solenoid is pulsed to open the gate.",
        json_schema_extra={"unit": "s"},
    )


ReleaseStep = Annotated[ReleaseBySensor | ReleaseTimed, Field(discriminator="proof")]


class WaterFillByVolume(_StrictModel):
    """The fill step of a water slot, proven complete by a flow meter that
    measures the actual volume dispensed."""

    proof: Literal["flow_volume"] = Field(
        ..., description="This fill step is proven complete by a flow meter."
    )
    valve_channel: ChannelId = Field(
        ..., description="The channel that drives the fill valve."
    )
    flow_sensor_channel: ChannelId = Field(
        ..., description="The channel that reports flow-meter pulses."
    )
    target_volume_ml: Annotated[float, Field(ge=50, le=50000)] = Field(
        ...,
        description="The volume this slot dispenses on a successful fill.",
        json_schema_extra={"unit": "ml"},
    )
    pulses_per_liter: Annotated[float, Field(gt=0)] = Field(
        ...,
        description="The flow meter's calibration constant.",
        json_schema_extra={"unit": "pulses/L"},
    )
    min_flow_ml_per_second: Annotated[float, Field(gt=0)] = Field(
        ...,
        description=(
            'The slowest flow rate still considered "flowing"; below '
            "this, the no-flow grace timer starts."
        ),
        json_schema_extra={"unit": "ml/s"},
    )
    no_flow_grace_seconds: Annotated[float, Field(ge=0.5, le=30)] = Field(
        ...,
        description=(
            "How long flow may stay below the minimum rate (a clogged "
            "valve, an empty supply) before the VMC treats the fill as "
            "failed."
        ),
        json_schema_extra={"unit": "s"},
    )
    over_dispense_percent: Annotated[float, Field(ge=0, le=50)] = Field(
        ...,
        description=(
            "How far past the target volume the VMC tolerates (a sticking "
            "valve closing late) before treating the fill as a fault "
            "rather than a successful dispense."
        ),
        json_schema_extra={"unit": "%"},
    )
    max_fill_seconds: Annotated[float, Field(ge=1, le=600)] = Field(
        ...,
        description=(
            "The longest the fill valve is allowed to stay open before the "
            "VMC gives up on this step even if the target volume was "
            "never reached."
        ),
        json_schema_extra={"unit": "s"},
    )


class WaterFillTimed(_StrictModel):
    """The fill step of a water slot, proven complete by elapsed time alone
    -- used when the slot has no flow meter."""

    proof: Literal["timed"] = Field(
        ..., description="This fill step is proven complete by elapsed time."
    )
    valve_channel: ChannelId = Field(
        ..., description="The channel that drives the fill valve."
    )
    max_fill_seconds: Annotated[float, Field(ge=1, le=600)] = Field(
        ...,
        description="How long the fill valve stays open for this step.",
        json_schema_extra={"unit": "s"},
    )


WaterFillStep = Annotated[
    WaterFillByVolume | WaterFillTimed, Field(discriminator="proof")
]


class Accessory(_StrictModel):
    """A device that runs alongside a slot's main steps -- a cooling fan,
    an indicator light, an auger-clearing blower -- rather than performing
    the dispense itself.

    `on_during` names which of the owning mechanism's steps this accessory
    should be active for; this class only checks that the list makes sense
    on its own (no duplicates, and `"all"` never mixed with named steps) --
    checking the names against the owning mechanism's actual step names
    happens in that profile's own model validator, since `Accessory` alone
    does not know which mechanism it belongs to.
    """

    channel: ChannelId = Field(
        ..., description="The channel that drives this accessory."
    )
    on_during: list[str] = Field(
        ...,
        min_length=1,
        description=(
            'Which steps this accessory runs during, by name, or ["all"] '
            "to run for the whole slot cycle."
        ),
    )
    lead_seconds: Annotated[float, Field(ge=0, le=30)] = Field(
        ...,
        description="How long before its step(s) start this accessory turns on.",
        json_schema_extra={"unit": "s"},
    )
    lag_seconds: Annotated[float, Field(ge=0, le=30)] = Field(
        ...,
        description="How long after its step(s) end this accessory stays on.",
        json_schema_extra={"unit": "s"},
    )

    @model_validator(mode="after")
    def _check_on_during(self) -> "Accessory":
        if len(set(self.on_during)) != len(self.on_during):
            raise ValueError("on_during entries must be unique")
        if "all" in self.on_during and self.on_during != ["all"]:
            raise ValueError('"all" must appear alone in on_during')
        return self


class BaggedIceProfile(_StrictModel):
    """A bagged-ice dispense slot: agitate, then fill, then release."""

    STEP_NAMES: ClassVar[tuple[str, ...]] = ("agitate", "fill", "release")

    mechanism: Literal["bagged_ice"] = Field(
        ..., description="This slot dispenses pre-bagged ice."
    )
    product_sku: str = Field(
        ...,
        min_length=1,
        description="The product SKU this slot dispenses.",
    )
    agitate: AgitateStep = Field(
        ..., description="How this slot breaks up clumped ice before filling."
    )
    fill: IceFillStep = Field(
        ..., description="How this slot feeds ice toward the release gate."
    )
    release: ReleaseStep = Field(
        ..., description="How this slot opens and closes its release gate."
    )
    accessories: dict[str, Accessory] = Field(
        default_factory=dict,
        description="Devices that run alongside this slot's steps, by name.",
    )

    @model_validator(mode="after")
    def _check_accessory_steps(self) -> "BaggedIceProfile":
        _check_accessories_name_known_steps(
            self.accessories, self.STEP_NAMES, self.mechanism
        )
        return self


class WaterFillProfile(_StrictModel):
    """A water dispense slot: fill, and nothing else."""

    STEP_NAMES: ClassVar[tuple[str, ...]] = ("fill",)

    mechanism: Literal["water_fill"] = Field(
        ..., description="This slot dispenses water."
    )
    product_sku: str = Field(
        ...,
        min_length=1,
        description="The product SKU this slot dispenses.",
    )
    fill: WaterFillStep = Field(..., description="How this slot fills the cup/bottle.")
    accessories: dict[str, Accessory] = Field(
        default_factory=dict,
        description="Devices that run alongside this slot's steps, by name.",
    )

    @model_validator(mode="after")
    def _check_accessory_steps(self) -> "WaterFillProfile":
        _check_accessories_name_known_steps(
            self.accessories, self.STEP_NAMES, self.mechanism
        )
        return self


def _check_accessories_name_known_steps(
    accessories: dict[str, Accessory],
    step_names: tuple[str, ...],
    mechanism: str,
) -> None:
    valid = ", ".join(step_names)
    for name, accessory in accessories.items():
        if accessory.on_during == ["all"]:
            continue
        for step in accessory.on_during:
            if step not in step_names:
                raise ValueError(
                    f'accessory "{name}": on_during contains "{step}"; '
                    f'valid steps for {mechanism} are {valid} (or ["all"])'
                )


SlotProfile = Annotated[
    BaggedIceProfile | WaterFillProfile, Field(discriminator="mechanism")
]

# Maps a physical dispenser kind (as named elsewhere in the config) to the
# `mechanism` discriminator value used in dispensers.toml.
MECHANISM_FOR_KIND: dict[str, str] = {"ice": "bagged_ice", "water": "water_fill"}


def worst_case_seconds(profile: SlotProfile) -> float:
    """The longest this slot could legitimately take to complete one
    dispense, per the time-budget formula (plan §5.2): every step's own
    worst-case timeout, plus the slowest accessory's lead and lag."""

    leads = [accessory.lead_seconds for accessory in profile.accessories.values()]
    lags = [accessory.lag_seconds for accessory in profile.accessories.values()]
    max_lead = max(leads) if leads else 0.0
    max_lag = max(lags) if lags else 0.0

    if isinstance(profile, BaggedIceProfile):
        total = (
            profile.agitate.run_seconds
            + profile.fill.max_run_seconds
            + profile.release.pulse_seconds
        )
        if isinstance(profile.release, ReleaseBySensor):
            total += (
                profile.release.open_timeout_seconds
                + profile.release.close_timeout_seconds
            )
    else:
        total = profile.fill.max_fill_seconds
        if isinstance(profile.fill, WaterFillByVolume):
            total += profile.fill.no_flow_grace_seconds

    return total + max_lead + max_lag


def drive_channels(profile: SlotProfile) -> set[str]:
    """Every channel this slot drives: motors, solenoids, valves, and
    accessories."""

    channels: set[str] = set()
    if isinstance(profile, BaggedIceProfile):
        channels.add(profile.agitate.motor_channel)
        channels.add(profile.fill.motor_channel)
        channels.add(profile.release.solenoid_channel)
    else:
        channels.add(profile.fill.valve_channel)

    for accessory in profile.accessories.values():
        channels.add(accessory.channel)

    return channels


def sense_channels(profile: SlotProfile) -> set[str]:
    """Every channel this slot reads: sensors, flow meters, and current
    sensors. `current_channel` is the only sense field that can be
    `"unmonitored"` (the schema now rejects that sentinel for every other
    sensor field, since they're required), so it's the only one that
    needs to skip it here."""

    channels: set[str] = set()

    def _add_current(value: str) -> None:
        if value != UNMONITORED_VALUE:
            channels.add(value)

    if isinstance(profile, BaggedIceProfile):
        _add_current(profile.agitate.current_channel)
        _add_current(profile.fill.current_channel)
        if isinstance(profile.fill, IceFillBySensor):
            channels.add(profile.fill.sensor_channel)
        if isinstance(profile.release, ReleaseBySensor):
            channels.add(profile.release.sensor_channel)
    else:
        if isinstance(profile.fill, WaterFillByVolume):
            channels.add(profile.fill.flow_sensor_channel)

    return channels
