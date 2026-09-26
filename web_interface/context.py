"""Shared module state for the web dashboard's route package.

Holds the live objects every route needs (config, VMC, health monitor,
availability, inventory manager, access store, display controller) plus
their setters, `ensure_setup_mode`, the `require_htmx` CSRF guard, log
tailing, `template_context`, and the couple of render helpers genuinely
shared by more than one area module (see their own docstrings for which).

One-way dependency: this module imports web_interface.auth (for
current_principal/_template_user and to relay set_access_store); auth.py
must never import this module back — see its own module docstring.

Route modules under web_interface/routes/ must read this module's state at
request time via `from web_interface import context` and `context.xxx`, not
`from web_interface.context import xxx`, which would capture the value
(None, at import time) forever instead of the live object main.py wires in
later.
"""

import asyncio
from pathlib import Path

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse

from config.config_model import ConfigModel
from loguru import logger

from services.access import AccessStore
from services.paths import LOG_FILE
from web_interface import auth as web_auth

config: ConfigModel = None


def set_config_object(cfg: ConfigModel):
    global config
    config = cfg


vmc_instance = None
health_monitor = None


def set_vmc_instance(vmc):
    global vmc_instance
    vmc_instance = vmc


def set_health_monitor(monitor):
    global health_monitor
    health_monitor = monitor


event_recorder = None


def set_event_recorder(recorder):
    global event_recorder
    event_recorder = recorder


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
# Mutated from routes/auth.py's setup routes via `context._pending_codes =
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


def tail(file_path: Path, lines: int = 50) -> list[str]:
    if not file_path.exists():
        return ["[Log file not found]"]

    with file_path.open("rb") as f:
        f.seek(0, 2)
        end = f.tell()
        buffer = bytearray()
        count = 0

        for pos in range(end - 1, -1, -1):
            f.seek(pos)
            char = f.read(1)
            if char == b"\n":
                count += 1
                if count >= lines:
                    break
            buffer.extend(char)
        result = buffer[::-1].decode("utf-8", errors="replace")
        return result.strip().splitlines()


def template_context(request: Request, level: str | None = None, **extra) -> dict:
    """Every template gets current_user and perms, so it never renders a
    button the server would refuse. Server-side checks remain the authority.

    current_user is a TemplateUser, not the full User — see TemplateUser's
    docstring in web_interface/auth.py. Code that needs the real User
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

    Shared by routes/auth.py (the setup-codes page and its email button)
    and routes/legacy.py (the users list's email-report button) — the only
    helper used by two area modules besides template_context and
    _render_status, per Task 1 executor resolution 3.
    """
    gateway = config.communication.email_gateway if config else None
    return bool(owner and owner.email and gateway and gateway.is_configured)


async def _render_status(templates, request: Request):
    """The /status fragment body.

    Shared by routes/home.py's GET /status and routes/legacy.py's POST
    /faults/{key}/clear (which re-renders this same fragment after clearing
    a fault) — per Task 1 executor resolution 3, a helper used by two area
    modules lives here rather than in either one. `templates` is passed in
    explicitly since this function lives outside any build_router(templates)
    closure.
    """
    if not vmc_instance:
        return HTMLResponse(
            '<div class="bg-red-50 rounded-xl border border-red-200 shadow-sm p-5">'
            '<p class="text-red-600 font-semibold">VMC not initialized</p></div>'
        )

    status = vmc_instance.get_status()
    issues: list[str] = []
    active_faults = vmc_instance.active_faults()
    for f in active_faults:
        target = f["product"] or "machine"
        issues.append(f"{f['code']} {f['description']} ({target})")
        f["since_seconds"] = None

    if event_recorder:
        summary_24h = await asyncio.to_thread(event_recorder.get_summary, 24)
        errors_24h = summary_24h["errors"]
        if errors_24h > 0:
            issues.append(
                f"{errors_24h} error{'s' if errors_24h != 1 else ''} in last 24h"
            )

    if health_monitor:
        health = health_monitor.get_summary()
        ages = {f["key"]: f["since_seconds"] for f in health["active_faults"]}
        for f in active_faults:
            f["since_seconds"] = ages.get(f["key"])
        stale = [name for name, sub in health["subsystems"].items() if sub["stale"]]
        if stale:
            issues.append(f"Stale subsystems: {', '.join(stale)}")
        out_of_range = [
            loc for loc, temp in health["temperatures"].items() if not temp["in_range"]
        ]
        if out_of_range:
            issues.append(f"Temp issues: {', '.join(out_of_range)}")

    payment_enabled = availability.payment_enabled if availability else None
    payment_reasons = availability.payment_blocking_reasons() if availability else []

    # A safety permissive (e.g. service_door_closed) can drive
    # payment_enabled false without ever adding to `issues` — no fault is
    # raised, just a permissive row failing. Fold that into the health
    # predicate directly rather than reformatting payment_reasons (bare
    # permissive names, not the fault table's human descriptions) into
    # `issues`: the Payment panel in the template already renders those
    # reasons whenever payment_enabled is false. `None` (no Availability
    # attached) is unknown, not unhealthy, so it must not flip this.
    is_healthy = len(issues) == 0 and payment_enabled is not False

    return templates.TemplateResponse(
        "partials/status_fragment.html",
        template_context(
            request,
            status=status,
            is_healthy=is_healthy,
            issues=issues,
            active_faults=active_faults,
            payment_enabled=payment_enabled,
            payment_reasons=payment_reasons,
            machine_stopped=(None if payment_enabled is None else not payment_enabled),
        ),
    )
