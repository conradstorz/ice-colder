"""Shared module state for the web dashboard's route package.

Holds the live objects every route needs (config, VMC, health monitor,
availability, inventory manager, access store, display controller) plus
their setters, `ensure_setup_mode`, the `require_htmx` CSRF guard, log
tailing, `template_context`, and the couple of render helpers genuinely
shared by more than one area module (see their own docstrings for which).

One-way dependency: this module imports ice_edge.web.auth (for
current_principal/_template_user and to relay set_access_store); auth.py
must never import this module back — see its own module docstring.

Route modules under ice_edge/web/routes/ must read this module's state at
request time via `from ice_edge.web import context` and `context.xxx`, not
`from ice_edge.web.context import xxx`, which would capture the value
(None, at import time) forever instead of the live object main.py wires in
later.
"""

# Re-exported on purpose: tests spy on `context.asyncio.to_thread`.
import asyncio  # noqa: F401
from pathlib import Path

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse

from ice_common.config_model import ConfigModel
from loguru import logger

from ice_contracts.cloud.report import HealthSummary
from ice_edge.access.access import AccessStore
from ice_edge.uplink.identity import MachineIdentity
from ice_edge.views.health import build_health_summary
from ice_common.paths import LOG_FILE
from ice_edge.web import auth as web_auth
from ice_ui import templating
from ice_ui.views.health import VMC_MISSING_HTML

config: ConfigModel = None


def set_config_object(cfg: ConfigModel):
    global config
    config = cfg


vmc_instance = None
machine_instance = None
health_monitor = None


def set_machine_instance(machine):
    """Wire the composition root (ice_edge/app/machine.py's `Machine`,
    vmc-reduction plan, Task 6). Also sets `vmc_instance = machine.vmc` so
    the many existing `context.vmc_instance` reads across the routes
    package keep working unchanged."""
    global machine_instance, vmc_instance
    machine_instance = machine
    vmc_instance = machine.vmc if machine is not None else None


def set_health_monitor(monitor):
    global health_monitor
    health_monitor = monitor


event_recorder = None


def set_event_recorder(recorder):
    global event_recorder
    event_recorder = recorder


# The Tests level's routes-side handle on the subsystem command channel
# (system-tests design §2.1). main.py already builds a CommandDispatcher and
# hands it to the VMC via Machine.set_command_dispatcher; this is the matching
# setter on the routes side (Task 13a closes the gap main.py's own comment
# next to that call names: "The web routes get their own setter in a later
# task"). Task 13b's POST /tests/{subsystem}/{command} is the first reader.
command_dispatcher = None


def set_command_dispatcher(dispatcher) -> None:
    global command_dispatcher
    command_dispatcher = dispatcher


# The routes-side handle on the loaded dispensers.toml (plan: dispenser
# profiles, Task 2) -- main.py wires the same DispenserProfiles instance
# the Machine owns (Machine.set_dispenser_profiles) here too, via
# set_dispenser_profiles below, so a later task's Tests-level/products
# routes can read profile validity without reaching into the VMC.
dispenser_profiles = None


def set_dispenser_profiles(profiles) -> None:
    global dispenser_profiles
    dispenser_profiles = profiles


availability = None


def set_availability(avail):
    global availability
    availability = avail


inventory_manager = None


def set_inventory_manager(inv):
    global inventory_manager
    inventory_manager = inv


access_store: AccessStore | None = None


def set_access_store(store: AccessStore | None) -> None:
    global access_store
    access_store = store
    web_auth.set_access_store(store)


machine_identity: MachineIdentity | None = None


def set_machine_identity(identity: MachineIdentity | None) -> None:
    global machine_identity
    machine_identity = identity


display_controller = None


def set_display_controller(display) -> None:
    global display_controller
    display_controller = display


# The plaintext this process has already logged, so a page reload during
# setup mode (GET /setup is hit on every load of the wizard) doesn't spam
# the log with the same warning. Reset naturally when a new code replaces it.
_last_logged_setup_code: str | None = None

# The 20 emergency-code plaintexts, held only between GET /setup/codes'
# first render and Done (spec §3.1 step 2). Cleared by /setup/codes/done;
# after that only their scrypt hashes exist anywhere, so a reload of
# /setup/codes must never regenerate the pool while this list is non-empty.
#
# Mutated from routes/auth/setup.py's setup routes via `context._pending_codes =
# ...` — a `global _pending_codes` statement in that module would not reach
# this name, since it lives here, not there (Task 1 executor resolution 4).
_pending_codes: list[str] = []


def ensure_setup_mode() -> None:
    """Keep the setup code alive, logged and on the display while the store
    has no owner; clear the display once one exists (spec §3.1).

    Safe to call on every request that reaches /setup: begin_setup() is
    idempotent while a code is live, and both the log line and the display
    publish are gated so they happen once per code, not once per call.
    """
    global _last_logged_setup_code
    if access_store is None or access_store.corrupt:
        return
    if not access_store.setup_mode:
        if display_controller is not None:
            display_controller.clear_setup_code()
        return

    code = access_store.begin_setup()
    if display_controller is not None:
        # show_setup_code() logs the plaintext itself (services/display_
        # controller.py), so when a display is wired that single call is
        # both the log line and the display publish; the setup_code check
        # keeps it to once per code rather than once per /setup load.
        if display_controller.setup_code != code:
            display_controller.show_setup_code(code)
    elif _last_logged_setup_code != code:
        # No display wired (e.g. before main.py attaches one) — this is the
        # only place the plaintext would otherwise land, so log it directly.
        _last_logged_setup_code = code
        logger.warning(f"Setup code: {code[:4]} {code[4:]}")


# Listed ahead of require_htmx on every mutating route, so an unauthenticated
# cross-site POST is turned away by the session check before the CSRF guard
# even runs. Both must pass to reach a handler; the order is intentional.
def require_htmx(request: Request):
    """CSRF guard for mutating routes.

    Cookie auth is replayed by the browser on cross-site requests same as
    Basic auth was, so a hostile page could still POST to /action/* or
    /inventory/delete/*. HTMX sends HX-Request: true on every request it
    makes; a cross-site form cannot add it, and a cross-origin fetch with a
    custom header needs a CORS preflight this app never answers.
    """
    if request.headers.get("HX-Request") != "true":
        raise HTTPException(status_code=403, detail="HTMX request required")


LOG_PATH = LOG_FILE


_TAIL_BLOCK = 8192


def tail(file_path: Path, lines: int = 50) -> list[str]:
    """Return the last ``lines`` lines of a file, reading 8 KiB blocks backwards."""
    if not file_path.exists():
        return ["[Log file not found]"]

    with file_path.open("rb") as f:
        pos = f.seek(0, 2)
        chunks: list[bytes] = []
        newlines = 0
        # One extra newline is needed to know the oldest wanted line is whole;
        # a trailing newline ends the last line rather than starting another.
        while pos > 0 and newlines <= lines:
            size = min(_TAIL_BLOCK, pos)
            pos -= size
            f.seek(pos)
            chunk = f.read(size)
            chunks.append(chunk)
            newlines += chunk.count(b"\n")
    data = b"".join(reversed(chunks))
    return data.decode("utf-8", errors="replace").strip().splitlines()[-lines:]


def template_context(request: Request, level: str | None = None, **extra) -> dict:
    """Every template gets current_user and perms, so it never renders a
    button the server would refuse. Server-side checks remain the authority.

    current_user is a TemplateUser, not the full User — see TemplateUser's
    docstring in ice_edge/web/auth.py. Code that needs the real User
    (pin_hash/pin_salt included) should use current_principal(request).user
    instead.

    `level` is the dashboard-v2 URL level (e.g. "products", "reports") a
    template can use to highlight the current nav entry; None for pages
    that don't have one yet.
    """
    principal = web_auth.current_principal(request)
    ctx = {
        "request": request,
        "current_user": web_auth._template_user(principal.user) if principal else None,
        "perms": principal.perms if principal else frozenset(),
        "level": level,
    }
    ctx.update(extra)
    return ctx


def _can_email_owner(owner) -> bool:
    """True when *owner* has an email address and the email gateway is
    configured.

    Shared by routes/auth/setup.py (the setup-codes page and its email button)
    and routes/users/ownership.py (the users list's email-report button, moved
    verbatim from the now-deleted routes/legacy.py by Task 12) — one of the
    couple of helpers used by more than one area module besides
    template_context and _render_status, per Task 1 executor resolution 3.
    """
    gateway = config.communication.email_gateway if config else None
    return bool(owner and owner.email and gateway and gateway.is_configured)


# Spec §1.3 asks the Inventory tile for "products below their low-stock
# line" without ever defining what that line is: there is no `low_stock`
# field on Product, none in ConfigModel, and no threshold in
# ice_edge/monitoring/inventory_manager.py, and adding one would be a config.json
# schema change beyond what this part's spec lists (a hard stop — see
# Task 5 executor resolution 1). This module-level constant is part 2's
# reversible placeholder instead: a tracked product is "low" when its
# InventoryManager count is at or below this many units. Making the line
# per-product configurable is future work, tracked outside this part.
LOW_STOCK_THRESHOLD = 3


def holder_display_name(hold) -> str | None:
    """The maintenance lease holder's display name (Task 3, system-tests
    design §2.2a): resolves ``hold.holder_user_id`` through
    ``access_store.users`` (a dict of ``User`` records with a ``name``
    field) and returns that name, falling back to the raw id when the
    store has no such user (a test-only id, or a user deleted since) or
    ``access_store`` isn't wired at all (None in most route tests). Shared
    by `health_snapshot`'s `maintenance` field and the Tests level's
    service-state card (`ice_edge/web/routes/tests_level/lease.py`) so both
    agree on the same name for the same hold rather than growing two
    copies of this lookup. Returns None when *hold* itself is None.
    """
    if hold is None:
        return None
    if access_store is not None:
        user = access_store.users.get(hold.holder_user_id)
        if user is not None:
            return user.name
    return hold.holder_user_id


async def health_snapshot() -> HealthSummary:
    """The single health computation shared by /status's hero banner and
    /pill's bar indicator (Task 5): a one-line delegate onto
    ``ice_edge.views.health.build_health_summary`` that passes this module's
    live globals. See that function for the field semantics; the lease
    holder's name comes from ``holder_display_name`` (below)."""
    return await build_health_summary(
        vmc=vmc_instance,
        event_recorder=event_recorder,
        health_monitor=health_monitor,
        availability=availability,
        maintenance_holder=lambda: holder_display_name(
            machine_instance.maintenance_hold
        ),
    )


async def _render_status(request: Request):
    """The /status fragment body.

    Task 15 deleted routes/legacy.py's POST /faults/{key}/clear, which used
    to re-render this same fragment after clearing a fault (Home's Clear
    button went with it — executor resolution 2 — clearing now lives
    entirely on /health/faults). The only caller left is routes/home.py's
    GET /status; this function still lives here rather than in home.py per
    Task 1 executor resolution 3's original reasoning.
    It reads the shared `templating.templates` singleton rather than taking it
    as a parameter.

    The health computation itself lives in `health_snapshot()` (Task 5),
    shared with GET /pill, so the hero and the bar pill can never disagree
    about what "healthy" means — this function only turns that snapshot
    into the existing response shape.
    """
    snap = await health_snapshot()
    if snap.vmc_missing:
        return HTMLResponse(VMC_MISSING_HTML)

    return templating.templates.TemplateResponse(
        request,
        "partials/status_fragment.html",
        template_context(
            request,
            status=snap.status,
            is_healthy=snap.is_healthy,
            issues=snap.issues,
            active_faults=snap.active_faults,
            payment_enabled=snap.payment_enabled,
            payment_reasons=snap.payment_reasons,
            machine_stopped=snap.machine_stopped,
            maintenance=snap.maintenance,
        ),
    )
