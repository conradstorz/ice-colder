"""Role and permission model shared by the edge and (later) the cloud portal."""

from __future__ import annotations

from enum import Enum


class Role(str, Enum):
    owner = "owner"
    secretary = "secretary"
    tech = "tech"
    loader = "loader"


class Permission(str, Enum):
    view_status = "view_status"
    clear_faults = "clear_faults"
    view_logs = "view_logs"
    machine_controls = "machine_controls"
    run_tests = "run_tests"
    edit_catalog = "edit_catalog"
    edit_placement = "edit_placement"
    view_reports = "view_reports"
    edit_contacts = "edit_contacts"
    edit_secrets = "edit_secrets"
    manage_users = "manage_users"
    manage_ownership = "manage_ownership"
    collect_cash = "collect_cash"


ROLE_PERMISSIONS: dict[Role, frozenset[Permission]] = {
    Role.owner: frozenset(Permission),
    Role.secretary: frozenset(
        {
            Permission.view_status,
            Permission.edit_catalog,
            Permission.edit_placement,
            Permission.view_reports,
            Permission.edit_contacts,
            Permission.manage_users,
            Permission.collect_cash,
        }
    ),
    Role.tech: frozenset(
        {
            Permission.view_status,
            Permission.clear_faults,
            Permission.view_logs,
            Permission.machine_controls,
            Permission.run_tests,
            Permission.edit_placement,
            Permission.collect_cash,
        }
    ),
    Role.loader: frozenset(
        {
            Permission.view_status,
            Permission.edit_placement,
            Permission.collect_cash,
        }
    ),
}


# What a read-only remote (cloud) viewer may do.
REMOTE_VIEWER_PERMISSIONS: frozenset[Permission] = frozenset({Permission.view_status})
