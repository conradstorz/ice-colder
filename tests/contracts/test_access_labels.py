"""Plain-words permission labels shared by the edge and the cloud portal."""

from ice_contracts.access import PERMISSION_LABELS, Permission


def test_every_permission_has_a_non_empty_label():
    for permission in Permission:
        assert PERMISSION_LABELS[permission].strip(), permission


def test_labels_are_distinct():
    labels = list(PERMISSION_LABELS.values())
    assert len(set(labels)) == len(labels)


def test_label_keys_follow_permission_definition_order():
    assert list(PERMISSION_LABELS) == list(Permission)
