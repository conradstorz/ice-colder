"""The uv workspace layout: shared packages import, and access is split cleanly."""

from __future__ import annotations


def test_contracts_and_common_import():
    import ice_common.config_model  # noqa: F401
    import ice_contracts.board.vending_machine  # noqa: F401


def test_remote_viewer_permissions_is_view_status_only():
    from ice_contracts.access import REMOTE_VIEWER_PERMISSIONS, Permission

    assert REMOTE_VIEWER_PERMISSIONS == frozenset({Permission.view_status})


def test_services_access_reexports_the_same_permission():
    import ice_contracts.access as contracts_access
    import ice_edge.access.access as legacy_access

    assert legacy_access.Permission is contracts_access.Permission
    assert legacy_access.Role is contracts_access.Role
    assert legacy_access.ROLE_PERMISSIONS is contracts_access.ROLE_PERMISSIONS
