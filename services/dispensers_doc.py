# services/dispensers_doc.py
"""Generates the self-documenting `dispensers.example.toml` from the Task 2
schema models (`services/dispenser_schema.py`), so the shipped
documentation can never drift from the schema it illustrates: nearly every
comment line in the output is rendered from a model field's own
`description`, `json_schema_extra["unit"]`, `ge`/`le`/`gt` metadata,
discriminator `Literal` tags (every tag in the union, not just the live
variant's own), or `StringConstraints`/`min_length` metadata. The two
exceptions are hand-typed prose, kept deliberately short: the
`schema_version` comment and `_OTHER_MECHANISM_NOTE` (the sentence
pointing a reader at the slot where the other `mechanism` is shown live).

Sample *values* are the one thing not derived from the schema -- they are
hand-authored below (`_SLOT_0`/`_SLOT_1`/`_SLOT_2`) and chosen so the whole
file validates with zero errors against `config.example.json` at the
default 120 s `dispense_timeout_seconds`. `_render_table` raises `KeyError`
for any schema field with no sample value, so a field added to the schema
without updating this module fails the generator loudly instead of just
vanishing from the example.

`render_example()` is consumed by `scripts/gen_dispensers_example.py`
(writes `dispensers.example.toml`) and by `tests/test_dispensers_example.py`
(asserts byte-identity with the committed file).
"""

from __future__ import annotations

import typing
from dataclasses import dataclass
from typing import Any

import annotated_types
from pydantic import StringConstraints

from contracts.common import CHANNEL_ID_PATTERN
from services.dispenser_schema import (
    Accessory,
    AgitateStep,
    BaggedIceProfile,
    CurrentSense,
    IceFillBySensor,
    IceFillStep,
    IceFillTimed,
    ReleaseBySensor,
    ReleaseStep,
    ReleaseTimed,
    SlotProfile,
    WaterFillByVolume,
    WaterFillProfile,
    WaterFillStep,
    WaterFillTimed,
)

# Reused from services/dispensers.py: stripping one layer of `Annotated[...]`
# is a plain, correctness-neutral operation (no exclusivity to lose), so it
# is not worth re-solving here. No import cycle -- dispensers.py does not
# import this module.
from services.dispensers import _unwrap_annotated

_EN_DASH = "–"
_GE = "≥"
_LE = "≤"


# --------------------------------------------------------------------------
# Comment rendering: introspects a field's description/unit/bounds/literal
# choices straight off its Pydantic `FieldInfo`.
# --------------------------------------------------------------------------


def _literal_values(annotation: object) -> list[str] | None:
    unwrapped, _ = _unwrap_annotated(annotation)
    if typing.get_origin(unwrapped) is typing.Literal:
        return [str(v) for v in typing.get_args(unwrapped)]
    return None


def _discriminator_name(cls: type) -> str:
    for name in ("mechanism", "proof"):
        if name in cls.model_fields:
            return name
    raise ValueError(f"{cls.__name__} has no discriminator field")


def _union_members_and_tags(union_alias: object) -> tuple[tuple[type, ...], list[str]]:
    """A discriminated union's member model classes, in declaration order,
    plus every tag those members' own discriminator fields carry (also in
    declaration order) -- so the "One of:" comment for a discriminator
    field can honestly list every variant, not just the one that happens
    to be live in a given sample slot."""

    unwrapped, _ = _unwrap_annotated(union_alias)
    members = typing.get_args(unwrapped)
    tags: list[str] = []
    for member in members:
        disc_name = _discriminator_name(member)
        values = _literal_values(member.model_fields[disc_name].annotation)
        if values is None:
            raise ValueError(f"{member.__name__}.{disc_name} has no Literal values")
        tags.extend(values)
    return members, tags


# Every discriminated union in the Task 2 schema. Built once from the
# schema's own union aliases (never a hard-coded tag list) so a future
# variant added to any of these unions is picked up automatically.
_UNION_TAG_TABLE: list[tuple[frozenset[type], list[str]]] = [
    (frozenset(members), tags)
    for members, tags in (
        _union_members_and_tags(SlotProfile),
        _union_members_and_tags(IceFillStep),
        _union_members_and_tags(ReleaseStep),
        _union_members_and_tags(WaterFillStep),
    )
]

_SLOT_PROFILE_TAGS = _UNION_TAG_TABLE[0][1]


def _variant_tags_for(cls_a: type, cls_b: type) -> list[str]:
    """The full, declaration-ordered tag list for the one discriminated
    union whose members are exactly `{cls_a, cls_b}`."""

    key = frozenset((cls_a, cls_b))
    for members, tags in _UNION_TAG_TABLE:
        if members == key:
            return tags
    raise ValueError(
        f"no known discriminated union has exactly {{{cls_a.__name__}, "
        f"{cls_b.__name__}}} as its members"
    )


def _scan_bounds(
    metadata: tuple,
) -> tuple[float | None, bool, float | None, bool]:
    """One metadata tuple's `Ge`/`Gt`/`Le`/`Lt` bounds, exclusivity kept
    distinct -- `services.dispensers._range_from_field_info` deliberately
    collapses `Gt`/`Lt` into the same slot as `Ge`/`Le`, which is fine for
    its own error messages but would misdocument a `gt=0` field (e.g.
    `pulses_per_liter`) as accepting 0."""

    lo: float | None = None
    lo_exclusive = False
    hi: float | None = None
    hi_exclusive = False
    for m in metadata:
        if isinstance(m, annotated_types.Ge):
            lo, lo_exclusive = m.ge, False
        elif isinstance(m, annotated_types.Gt):
            lo, lo_exclusive = m.gt, True
        elif isinstance(m, annotated_types.Le):
            hi, hi_exclusive = m.le, False
        elif isinstance(m, annotated_types.Lt):
            hi, hi_exclusive = m.lt, True
    return lo, lo_exclusive, hi, hi_exclusive


def _bounds_and_unit(
    field_info: Any,
) -> tuple[float | None, bool, float | None, bool, str]:
    """`(lo, lo_exclusive, hi, hi_exclusive, unit)`, digging into the
    `Annotated` arm of a `float | Literal["unmonitored"]` union (the
    current-sense fields) when the field itself carries no bounds."""

    unit = ""
    if field_info.json_schema_extra:
        unit = field_info.json_schema_extra.get("unit", "")

    lo, lo_exclusive, hi, hi_exclusive = _scan_bounds(field_info.metadata)
    if lo is None and hi is None:
        for arg in typing.get_args(field_info.annotation):
            _, metadata = _unwrap_annotated(arg)
            lo, lo_exclusive, hi, hi_exclusive = _scan_bounds(metadata)
            if lo is not None or hi is not None:
                break

    return lo, lo_exclusive, hi, hi_exclusive, unit


def _format_number(n: float) -> str:
    if isinstance(n, float) and n.is_integer():
        return str(int(n))
    return str(n)


def _join_sentence(description: str, extra: str) -> str:
    """Join a field's description with one trailing clause, adding the
    separating period only when the description does not already end
    with one -- so a description that was hand-written without a
    trailing period never ends up with a clause mashed onto it."""

    if not extra:
        return description
    sep = " " if description.endswith(".") else ". "
    return f"{description}{sep}{extra}"


def _scan_string_constraints(metadata: tuple) -> tuple[str | None, int | None]:
    """One metadata tuple's `StringConstraints.pattern` and
    `annotated_types.MinLen.min_length`, if present."""

    pattern: str | None = None
    min_length: int | None = None
    for m in metadata:
        if isinstance(m, StringConstraints) and m.pattern:
            pattern = m.pattern
        elif isinstance(m, annotated_types.MinLen):
            min_length = m.min_length
    return pattern, min_length


def _string_constraints(field_info: Any) -> tuple[str | None, int | None]:
    """`(pattern, min_length)`, digging into the `Annotated` arm of a
    `ChannelId | Literal["unmonitored"]` union (e.g. `current_channel`)
    when the field itself carries no string constraints."""

    pattern, min_length = _scan_string_constraints(field_info.metadata)
    if pattern is None and min_length is None:
        for arg in typing.get_args(field_info.annotation):
            _, metadata = _unwrap_annotated(arg)
            pattern, min_length = _scan_string_constraints(metadata)
            if pattern is not None or min_length is not None:
                break
    return pattern, min_length


def _string_constraint_clause(
    annotation: object, pattern: str | None, min_length: int | None
) -> str:
    if pattern is not None:
        if pattern == CHANNEL_ID_PATTERN:
            return (
                "Allowed: lowercase letters, digits and underscores, "
                f"1{_EN_DASH}64 characters."
            )
        return f"Must match: {pattern}."

    if typing.get_origin(annotation) is list:
        return "At least one entry."
    return "Must not be empty."


def _comment_for(field_info: Any, variant_tags: list[str] | None = None) -> str:
    """`# <description>. Unit: <unit>. Range: <lo>-<hi>.` for a bounded
    numeric field, `# <description>. One of: a, b.` for a discriminator
    field (every tag in its union, via `variant_tags` when given), `#
    <description>. Allowed: ...`/`Must match: ...`/`Must not be empty.`/
    `At least one entry.` for a string-constrained field, else just `#
    <description>`."""

    description = (field_info.description or "").strip()

    literal_values = (
        variant_tags
        if variant_tags is not None
        else _literal_values(field_info.annotation)
    )
    if literal_values is not None:
        extra = f"One of: {', '.join(literal_values)}."
        return f"# {_join_sentence(description, extra)}"

    pattern, min_length = _string_constraints(field_info)
    if pattern is not None or min_length is not None:
        extra = _string_constraint_clause(field_info.annotation, pattern, min_length)
        return f"# {_join_sentence(description, extra)}"

    lo, lo_exclusive, hi, hi_exclusive, unit = _bounds_and_unit(field_info)
    if lo is None and hi is None:
        return f"# {description}"

    if lo is not None and hi is not None:
        range_text = f"{_format_number(lo)}{_EN_DASH}{_format_number(hi)}"
    elif lo is not None:
        range_text = (
            f"greater than {_format_number(lo)}"
            if lo_exclusive
            else f"{_GE} {_format_number(lo)}"
        )
    else:
        range_text = (
            f"less than {_format_number(hi)}"
            if hi_exclusive
            else f"{_LE} {_format_number(hi)}"
        )

    unit_part = f"Unit: {unit}. " if unit else ""
    extra = f"{unit_part}Range: {range_text}."
    return f"# {_join_sentence(description, extra)}"


def _toml_scalar(value: Any) -> str:
    if isinstance(value, str):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return _format_number(value)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, list):
        return "[" + ", ".join(_toml_scalar(v) for v in value) + "]"
    raise TypeError(f"unsupported sample value: {value!r}")


# --------------------------------------------------------------------------
# Sample-value containers. `_Table` is one sub-model's worth of hand-picked
# values (keyed by field name); `_UnionTable` is a discriminated-union
# field's live variant plus the other variant's sample values (rendered as
# a trailing commented block).
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class _Table:
    cls: type
    values: dict[str, Any]


@dataclass(frozen=True)
class _UnionTable:
    live: _Table
    other: _Table


# The "mechanism" field's other tag is never shown inline (unlike "proof",
# whose other variant is a small step model rendered as a trailing
# commented block) -- the two mechanisms are whole different profile
# shapes. This points the reader at the slot that shows the other one
# live instead.
_OTHER_MECHANISM_NOTE: dict[str, str] = {
    "bagged_ice": (
        "# The other mechanism's fields are shown under a slot that uses "
        "it (see slot 1 for water_fill)."
    ),
    "water_fill": (
        "# The other mechanism's fields are shown under a slot that uses "
        "it (see slot 0 for bagged_ice)."
    ),
}


def _ordered_field_names(cls: type) -> list[str]:
    """`cls.model_fields`, but with any fields inherited from the
    `CurrentSense` mixin moved to the end (kept adjacent, in their own
    declared order) -- Pydantic's `model_fields` otherwise lists inherited
    fields first, which would render a motor table as current-sense
    fields, then motor channel, then timing, instead of motor channel,
    timing, then the current-sense pair last."""

    field_names = list(cls.model_fields.keys())
    if issubclass(cls, CurrentSense) and cls is not CurrentSense:
        mixin_names = [n for n in field_names if n in CurrentSense.model_fields]
        own_names = [n for n in field_names if n not in CurrentSense.model_fields]
        return own_names + mixin_names
    return field_names


def _render_table(
    cls: type,
    values: dict[str, Any],
    lines: list[str],
    path: list[str],
    discriminator_tags: list[str] | None = None,
) -> None:
    """Render one `[path]` TOML table for `cls` using `values`. Emits every
    scalar field of `cls` first (required by TOML -- a table's own keys
    must precede any of its sub-tables), then recurses into nested
    `_Table`/`_UnionTable`/dict-of-`_Table` fields in declaration order.

    `discriminator_tags`, when given, is the full, declaration-ordered tag
    list for `cls`'s own `mechanism`/`proof` field (see
    `_variant_tags_for`) -- supplied by the caller, which alone knows
    which discriminated union `cls` belongs to."""

    lines.append(f"[{'.'.join(path)}]")
    deferred: list[tuple[str, Any, Any]] = []

    for name in _ordered_field_names(cls):
        field_info = cls.model_fields[name]
        if name not in values:
            raise KeyError(
                f"{cls.__name__}.{name} has no sample value in dispensers_doc.py"
            )
        value = values[name]

        if isinstance(value, (_Table, _UnionTable)):
            deferred.append((name, field_info, value))
            continue
        if isinstance(value, dict):
            # dict[str, _Table] -- an accessories-style field. It has no
            # scalar line of its own; defer it (comment included) to the
            # second pass so the comment lands immediately above its own
            # first sub-table rather than stranded up here.
            deferred.append((name, field_info, value))
            continue

        if name in ("mechanism", "proof") and discriminator_tags is not None:
            lines.append(_comment_for(field_info, variant_tags=discriminator_tags))
        else:
            lines.append(_comment_for(field_info))
        if name == "mechanism":
            lines.append(_OTHER_MECHANISM_NOTE[value])
        lines.append(f"{name} = {_toml_scalar(value)}")

    for name, field_info, value in deferred:
        if isinstance(value, _Table):
            lines.append("")
            lines.append(_comment_for(field_info))
            _render_table(value.cls, value.values, lines, path + [name])

        elif isinstance(value, _UnionTable):
            lines.append("")
            lines.append(_comment_for(field_info))
            tags = _variant_tags_for(value.live.cls, value.other.cls)
            _render_table(
                value.live.cls,
                value.live.values,
                lines,
                path + [name],
                discriminator_tags=tags,
            )

            other_cls = value.other.cls
            disc = _discriminator_name(other_cls)
            other_tag_values = _literal_values(other_cls.model_fields[disc].annotation)
            if other_tag_values is None:
                raise ValueError(f"{other_cls.__name__}.{disc} has no Literal values")
            other_tag = other_tag_values[0]
            lines.append("")
            lines.append(f'# If {disc} = "{other_tag}" instead, the fields are:')
            for fname in _ordered_field_names(other_cls):
                finfo = other_cls.model_fields[fname]
                if fname not in value.other.values:
                    raise KeyError(
                        f"{other_cls.__name__}.{fname} has no sample value "
                        "in dispensers_doc.py"
                    )
                if fname in ("mechanism", "proof"):
                    lines.append(_comment_for(finfo, variant_tags=tags))
                else:
                    lines.append(_comment_for(finfo))
                lines.append(f"# {fname} = {_toml_scalar(value.other.values[fname])}")

        else:  # dict[str, _Table]
            lines.append("")
            lines.append(_comment_for(field_info))
            if not value:
                lines.append(f"# {name}: none for this slot.")
            for i, (key, entry) in enumerate(value.items()):
                if i > 0:
                    lines.append("")
                _render_table(entry.cls, entry.values, lines, path + [name, key])


# --------------------------------------------------------------------------
# Hand-authored sample data -- three slots matching config.example.json:
# slot 0 bagged ice (sensor proofs, unmonitored current, two accessories),
# slot 1 water fill by volume (1 gallon = 3785 ml), slot 2 water fill timed
# (so both water fill variants appear live). Values are chosen so every
# slot's worst_case_seconds stays comfortably under 120 s - 5 s margin.
# --------------------------------------------------------------------------

_SLOT_0_AGITATE = _Table(
    AgitateStep,
    {
        "stall_current_amps": "unmonitored",
        "current_channel": "unmonitored",
        "motor_channel": "agitator_motor",
        "run_seconds": 8.0,
    },
)

_SLOT_0_FILL = _UnionTable(
    live=_Table(
        IceFillBySensor,
        {
            "stall_current_amps": "unmonitored",
            "current_channel": "unmonitored",
            "proof": "bag_full_sensor",
            "motor_channel": "auger_motor",
            "sensor_channel": "bag_full_sensor",
            "max_run_seconds": 45.0,
        },
    ),
    other=_Table(
        IceFillTimed,
        {
            "stall_current_amps": "unmonitored",
            "current_channel": "unmonitored",
            "proof": "timed",
            "motor_channel": "auger_motor",
            "max_run_seconds": 45.0,
        },
    ),
)

_SLOT_0_RELEASE = _UnionTable(
    live=_Table(
        ReleaseBySensor,
        {
            "proof": "door_sensor",
            "solenoid_channel": "bag_drop_solenoid",
            "sensor_channel": "door_sensor",
            "pulse_seconds": 1.0,
            "open_timeout_seconds": 5.0,
            "close_timeout_seconds": 5.0,
        },
    ),
    other=_Table(
        ReleaseTimed,
        {
            "proof": "timed",
            "solenoid_channel": "bag_drop_solenoid",
            "pulse_seconds": 1.0,
        },
    ),
)

_SLOT_0 = {
    "mechanism": "bagged_ice",
    "product_sku": "SAMPLE-ICE",
    "agitate": _SLOT_0_AGITATE,
    "fill": _SLOT_0_FILL,
    "release": _SLOT_0_RELEASE,
    "accessories": {
        "bag_fan": _Table(
            Accessory,
            {
                "channel": "bag_fan",
                "on_during": ["agitate", "fill"],
                "lead_seconds": 0.0,
                "lag_seconds": 2.0,
            },
        ),
        "vending_now_light": _Table(
            Accessory,
            {
                "channel": "vending_now_light",
                "on_during": ["all"],
                "lead_seconds": 0.0,
                "lag_seconds": 0.0,
            },
        ),
    },
}

# worst_case_seconds(slot 0) = agitate.run_seconds (8) + fill.max_run_seconds
# (45) + release.pulse_seconds (1) + release.open_timeout_seconds (5) +
# release.close_timeout_seconds (5) + max accessory lag (2) = 66 s.

_SLOT_1_FILL = _UnionTable(
    live=_Table(
        WaterFillByVolume,
        {
            "proof": "flow_volume",
            "valve_channel": "water_valve_solenoid",
            "flow_sensor_channel": "water_flow_sensor",
            "target_volume_ml": 3785.0,
            "pulses_per_liter": 450.0,
            "min_flow_ml_per_second": 10.0,
            "no_flow_grace_seconds": 5.0,
            "over_dispense_percent": 5.0,
            "max_fill_seconds": 90.0,
        },
    ),
    other=_Table(
        WaterFillTimed,
        {
            "proof": "timed",
            "valve_channel": "water_valve_solenoid",
            "max_fill_seconds": 90.0,
        },
    ),
)

_SLOT_1 = {
    "mechanism": "water_fill",
    "product_sku": "SAMPLE-WATER-SM",
    "fill": _SLOT_1_FILL,
    "accessories": {},
}

# worst_case_seconds(slot 1) = fill.max_fill_seconds (90) +
# fill.no_flow_grace_seconds (5) = 95 s.

_SLOT_2_FILL = _UnionTable(
    live=_Table(
        WaterFillTimed,
        {
            "proof": "timed",
            "valve_channel": "water_valve_solenoid",
            "max_fill_seconds": 90.0,
        },
    ),
    other=_Table(
        WaterFillByVolume,
        {
            "proof": "flow_volume",
            "valve_channel": "water_valve_solenoid",
            "flow_sensor_channel": "water_flow_sensor",
            "target_volume_ml": 18927.0,
            "pulses_per_liter": 450.0,
            "min_flow_ml_per_second": 10.0,
            "no_flow_grace_seconds": 5.0,
            "over_dispense_percent": 5.0,
            "max_fill_seconds": 90.0,
        },
    ),
)

_SLOT_2 = {
    "mechanism": "water_fill",
    "product_sku": "SAMPLE-WATER-LG",
    "fill": _SLOT_2_FILL,
    "accessories": {},
}

# worst_case_seconds(slot 2) = fill.max_fill_seconds (90) = 90 s (the timed
# variant has no no_flow_grace_seconds field).


_HEADER_LINES = [
    "# dispensers.example.toml",
    "#",
    "# A worked example of dispensers.toml, the hand-edited file that gives",
    "# each physical dispense slot (one [slot.N] table) its own mechanical",
    "# parameters: how it agitates, fills, and releases (bagged ice) or",
    "# fills (water), plus any accessories -- fans, lights, blowers -- that",
    "# run alongside those steps.",
    "#",
    "# dispensers.toml lives at the repo root (or wherever",
    "# ICE_COLDER_DISPENSERS points), next to config.json. Each [slot.N]",
    "# table's product_sku must name a product in the catalog (config.json's",
    '# physical.products), and N must equal that product\'s own "slot" number.',
    "#",
    "# Validate any dispensers.toml with:",
    "#     uv run python -m services.dispensers --check",
    "#",
    "# This file is GENERATED by scripts/gen_dispensers_example.py from the",
    "# models in services/dispenser_schema.py -- every comment below is",
    "# rendered from that schema's own Field descriptions, units, and",
    "# ranges. Do not hand-edit this example; edit the schema instead, then",
    "# regenerate:",
    "#     uv run python scripts/gen_dispensers_example.py",
]


def render_example() -> str:
    """Render the full `dispensers.example.toml` text (``\\n`` newlines
    only). Deterministic: same schema + same sample values always produce
    the same bytes."""

    lines: list[str] = list(_HEADER_LINES)
    lines.append("")
    lines.append(
        "# Format version of this file. Must be 1; the VMC refuses any other value."
    )
    lines.append("schema_version = 1")

    slots = (
        ("0", "bagged ice (SAMPLE-ICE)", BaggedIceProfile, _SLOT_0),
        (
            "1",
            "water, fill proven by a flow meter (SAMPLE-WATER-SM)",
            WaterFillProfile,
            _SLOT_1,
        ),
        (
            "2",
            "water, fill proven by elapsed time (SAMPLE-WATER-LG)",
            WaterFillProfile,
            _SLOT_2,
        ),
    )

    rule = "# " + "-" * 70
    for slot_num, label, cls, values in slots:
        lines.append("")
        lines.append(rule)
        lines.append(f"# Slot {slot_num} -- {label}")
        lines.append(rule)
        _render_table(
            cls,
            values,
            lines,
            ["slot", slot_num],
            discriminator_tags=_SLOT_PROFILE_TAGS,
        )

    return "\n".join(lines) + "\n"
