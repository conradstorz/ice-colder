# ice_edge/vend/schema_introspect.py
"""Pydantic schema introspection shared by `dispensers` (error humanising) and
`dispensers_doc` (example generation): unwrapping `Annotated`, walking unions,
reading discriminator tags and numeric bounds off a `FieldInfo`."""

from __future__ import annotations

import types
import typing
from typing import Any

import annotated_types
from pydantic import BaseModel
from pydantic.fields import FieldInfo

# The two discriminator field names used anywhere in the SlotProfile tree.
_DISCRIMINATOR_FIELDS = ("mechanism", "proof")


def unwrap_annotated(tp: object) -> tuple[object, tuple]:
    """Strip one layer of `Annotated[...]`, returning (real_type, metadata)."""

    if hasattr(tp, "__metadata__"):
        args = typing.get_args(tp)
        return args[0], args[1:]
    return tp, ()


def union_variants(tp: object) -> tuple | None:
    """Return a Union's member types, or `None` if `tp` is not a union."""

    origin = typing.get_origin(tp)
    if origin is types.UnionType or origin is typing.Union:
        return typing.get_args(tp)
    return None


def discriminator_field(model_cls: type[BaseModel]) -> FieldInfo | None:
    for name in _DISCRIMINATOR_FIELDS:
        if name in model_cls.model_fields:
            return model_cls.model_fields[name]
    return None


def union_tag_order(alias: object) -> tuple[str, ...]:
    """Every discriminator tag declared by a union's member models, in
    declaration order. Empty when `alias` is not a union of tagged models."""

    unwrapped, _ = unwrap_annotated(alias)
    variants = union_variants(unwrapped)
    if variants is None:
        return ()
    tags: list[str] = []
    for variant in variants:
        variant_type, _ = unwrap_annotated(variant)
        if not (isinstance(variant_type, type) and issubclass(variant_type, BaseModel)):
            continue
        disc = discriminator_field(variant_type)
        if disc is None:
            continue
        tags.extend(str(v) for v in typing.get_args(disc.annotation))
    return tuple(tags)


def union_tags(alias: object) -> frozenset[str]:
    """The set form of `union_tag_order`."""

    return frozenset(union_tag_order(alias))


def _scan_bounds(
    metadata: tuple,
) -> tuple[Any, bool, Any, bool]:
    """One metadata tuple's `Ge`/`Gt`/`Le`/`Lt` bounds, exclusivity kept
    distinct so a `gt=0` field is never documented as accepting 0."""

    lo = hi = None
    lo_exclusive = hi_exclusive = False
    for m in metadata:
        # `Field(ge=..., le=...)` used *inside* an `Annotated[...]` arm
        # (the current-sense fields' `float | Literal["unmonitored"]`
        # shape) stores its bounds on a nested FieldInfo, not as bare
        # annotated_types instances -- recurse into it.
        if isinstance(m, FieldInfo):
            nested = _scan_bounds(m.metadata)
            if nested[0] is not None or nested[2] is not None:
                lo, lo_exclusive, hi, hi_exclusive = nested
            continue
        if isinstance(m, annotated_types.Ge):
            lo, lo_exclusive = m.ge, False
        elif isinstance(m, annotated_types.Gt):
            lo, lo_exclusive = m.gt, True
        elif isinstance(m, annotated_types.Le):
            hi, hi_exclusive = m.le, False
        elif isinstance(m, annotated_types.Lt):
            hi, hi_exclusive = m.lt, True
    return lo, lo_exclusive, hi, hi_exclusive


def bounds_and_unit(field_info: Any) -> tuple[Any, bool, Any, bool, str]:
    """`(lo, lo_exclusive, hi, hi_exclusive, unit)`, digging into the
    `Annotated` arm of a `float | Literal["unmonitored"]` union (the
    current-sense fields) when the field itself carries no bounds."""

    unit = ""
    if field_info.json_schema_extra:
        unit = field_info.json_schema_extra.get("unit", "")

    lo, lo_exclusive, hi, hi_exclusive = _scan_bounds(field_info.metadata)
    if lo is None and hi is None:
        for arg in typing.get_args(field_info.annotation):
            _, metadata = unwrap_annotated(arg)
            lo, lo_exclusive, hi, hi_exclusive = _scan_bounds(metadata)
            if lo is not None or hi is not None:
                break

    return lo, lo_exclusive, hi, hi_exclusive, unit
