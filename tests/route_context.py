"""Snapshot/restore for every mutable `web_interface.context` global.

Route tests wire a fresh config/VMC/access store/etc. into
`web_interface.context` through its `set_*` functions, then used to tear
each one back down by hand. This module gives `tests/conftest.py`'s
autouse fixture a single place to do that for *all* of them, and a guard
(`discoverable_globals`) that fails loudly if a 14th global is added to
`web_interface/context.py` without being added here too.
"""

import inspect

from web_interface import auth as web_auth
from web_interface import context

GLOBALS: tuple[str, ...] = (
    "config",
    "vmc_instance",
    "machine_instance",
    "health_monitor",
    "event_recorder",
    "command_dispatcher",
    "dispenser_profiles",
    "availability",
    "inventory_manager",
    "access_store",
    "display_controller",
    "_last_logged_setup_code",
    "_pending_codes",
)

# Names in `vars(context)` that are plain import bindings, not module
# state, but still slip past the "not callable, not a module, not a
# class" filter below: `logger` (`from loguru import logger`) is a
# `loguru.Logger` instance -- not callable, not a module, not a class --
# yet it is never the target of a `global` statement anywhere in
# context.py, unlike every name in GLOBALS. Listed explicitly, by name,
# with the reason, so a *second* such leak fails `discoverable_globals()`
# loudly instead of being silently absorbed here.
_IMPORT_BINDINGS: frozenset[str] = frozenset({"logger"})


def snapshot() -> dict[str, object]:
    """Capture the current value of every name in `GLOBALS`.

    A `list` value (`_pending_codes`) is copied, so the returned snapshot
    is immune to later in-place mutation and `restore` puts back a
    distinct list object rather than the live one.
    """
    snap: dict[str, object] = {}
    for name in GLOBALS:
        value = getattr(context, name)
        if isinstance(value, list):
            value = list(value)
        snap[name] = value
    return snap


def restore(snap: dict[str, object]) -> None:
    """Put every captured global back via plain `setattr` -- never through
    a `context.set_*` function, since some of those have side effects
    beyond the one global they appear to own (`set_machine_instance` also
    sets `vmc_instance`; `set_access_store` also pokes `web_auth`).

    `web_interface.auth` keeps its own module-level access-store reference
    (`_store`, synced by `context.set_access_store`'s call to
    `web_auth.set_access_store`) that a plain `setattr` on `context` never
    touches, so it is re-synced here directly. The login back-off
    singleton is reset and its trusted-proxy list cleared for the same
    reason `tests/conftest.py`'s `wired` fixture used to do it on exit.
    """
    for name, value in snap.items():
        setattr(context, name, value)
    web_auth.set_access_store(snap["access_store"])
    web_auth.backoff.reset()
    web_auth.backoff.set_trusted_proxies([])


def discoverable_globals() -> set[str]:
    """Names in `vars(context)` that are not dunder, not callable, not a
    module or class, not an ALL_CAPS constant (`LOG_PATH`,
    `LOW_STOCK_THRESHOLD`), and not a known non-state import binding (see
    `_IMPORT_BINDINGS` above). What `GLOBALS` must equal -- the guard
    against a 14th global landing in `context.py` unlisted.
    """
    names: set[str] = set()
    for name, value in vars(context).items():
        if name.startswith("__") and name.endswith("__"):
            continue
        if callable(value):
            continue
        if inspect.ismodule(value) or inspect.isclass(value):
            continue
        if name == name.upper():
            continue
        if name in _IMPORT_BINDINGS:
            continue
        names.add(name)
    return names
