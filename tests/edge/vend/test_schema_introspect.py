"""Schema introspection helpers: derived tags must equal the old hand-typed set."""

from ice_contracts.board.dispenser_schema import (
    IceFillStep,
    ReleaseStep,
    SlotProfile,
    WaterFillStep,
)
from ice_edge.vend.schema_introspect import union_tags

# The set the deleted `_DISCRIMINATOR_TAGS` was typed out by hand as.
_OLD_HAND_TYPED_TAGS = frozenset(
    {
        "bagged_ice",
        "water_fill",
        "bag_full_sensor",
        "timed",
        "door_sensor",
        "catch",
        "flow_volume",
    }
)


def test_derived_tags_equal_the_old_hand_typed_set():
    derived = frozenset().union(
        *(
            union_tags(alias)
            for alias in (SlotProfile, IceFillStep, ReleaseStep, WaterFillStep)
        )
    )
    assert derived == _OLD_HAND_TYPED_TAGS


def test_union_tags_of_slot_profile():
    assert union_tags(SlotProfile) == {"bagged_ice", "water_fill"}


def test_union_tags_of_a_non_union_is_empty():
    assert union_tags(int) == frozenset()
