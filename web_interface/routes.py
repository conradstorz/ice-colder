import asyncio
from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, Depends, FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from loguru import logger

from config.config_model import ConfigModel, Product
from contracts.vending_machine import EXPECTED_SUBSYSTEMS
from services.access import (
    OTP_DIGITS,
    AccessError,
    AccessStore,
    OwnerExistsError,
    Permission,
    Role,
)
from services.auth_policy import pin_problem
from services.config_store import (
    add_product,
    delete_product,
    save_config,
    update_product,
)
from services.fsm_control import perform_command
from services.health_monitor import HealthMonitor
from services.mailer import send_email
from services.paths import LOG_FILE
from web_interface import auth as web_auth

# Backoff subject for a user_id form value that names nobody. Copilot review
# (web_interface/routes.py:263): the raw form value was used as the backoff
# subject before checking the user existed, so a client could mint a fresh
# random id on every request and get a fresh, never-throttled counter while
# still forcing verify_user_pin's dummy-hash scrypt cost — unbounded memory
# growth (until the 24-hour prune) and a throttling bypass. Every id that
# does not name a real user collapses onto this one bounded subject instead,
# so repeated attempts against different fake ids are throttled exactly like
# repeated attempts against one id.
_UNKNOWN_USER_SUBJECT = "unknown"


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


def attach_routes(app: FastAPI, templates: Jinja2Templates):
    public = APIRouter()

    # Runs before every request (module-level `access_store`/`corrupt` are
    # read live, so this reflects whatever set_access_store() last set).
    # /static/* is excluded — it's a Starlette Mount that can't take
    # dependencies, and the wizard and error page both need it unstyled.
    @app.middleware("http")
    async def access_gate(request: Request, call_next):
        path = request.url.path
        if path.startswith("/static/"):
            return await call_next(request)

        store = access_store
        if store is not None:
            if store.corrupt:
                # Spec §6: a corrupt access file must never silently become
                # an open setup wizard, so every path — /setup included —
                # gets the same error page instead of just this one route
                # refusing to load.
                return HTMLResponse(
                    "<h1>Access file is corrupt</h1>"
                    "<p>The machine's access file could not be read, so the "
                    "dashboard is unavailable until an operator repairs or "
                    "removes it at the machine. The vending machine itself "
                    "keeps running.</p>",
                    status_code=503,
                )
            if store.setup_mode and path not in ("/setup", "/setup/codes"):
                return RedirectResponse("/setup", status_code=303)

        return await call_next(request)

    def _keypad(
        request: Request,
        *,
        selected_user_id=None,
        error=None,
        wait_seconds=None,
        status_code=200,
        headers=None,
    ):
        return templates.TemplateResponse(
            "partials/keypad.html",
            {
                "request": request,
                "users": access_store.enabled_users() if access_store else [],
                "selected_user_id": selected_user_id,
                "error": error,
                "wait_seconds": wait_seconds,
            },
            status_code=status_code,
            headers=headers or {},
        )

    @public.get("/login", response_class=HTMLResponse)
    async def login_page(request: Request):
        if access_store is None:
            raise HTTPException(status_code=503, detail="Access store not loaded")
        if access_store.setup_mode:
            return RedirectResponse("/setup", status_code=303)
        return templates.TemplateResponse(
            "login.html",
            {
                "request": request,
                "users": access_store.enabled_users(),
                "selected_user_id": None,
                "error": None,
                "wait_seconds": None,
            },
        )

    @public.post(
        "/login", response_class=HTMLResponse, dependencies=[Depends(require_htmx)]
    )
    async def login_submit(
        request: Request, user_id: str = Form(...), pin: str = Form(...)
    ):
        if access_store is None:
            raise HTTPException(status_code=503, detail="Access store not loaded")
        client = web_auth.client_key(request)
        trusted = web_auth.is_trusted_client(request, user_id)
        # See _UNKNOWN_USER_SUBJECT: bound the back-off subject to known
        # users so an arbitrary form value can't buy an unbounded, unthrottled
        # counter.
        subject = (
            user_id
            if access_store.get_user(user_id) is not None
            else _UNKNOWN_USER_SUBJECT
        )

        remaining = web_auth.backoff.check("pin", subject, client, trusted=trusted)
        if remaining is not None:
            return _keypad(
                request,
                selected_user_id=None,
                wait_seconds=int(remaining) + 1,
                status_code=429,
                headers={"Retry-After": str(int(remaining) + 1)},
            )

        # Identical response for a wrong PIN, an unknown user and a disabled
        # one: the picker already leaks names, nothing else should leak state.
        # selected_user_id is never echoed back here — the submitted id would
        # only mark an <option> "selected" when it names an enabled user,
        # which is itself an enumeration oracle.
        if not access_store.verify_user_pin(user_id, pin):
            web_auth.backoff.record_failure("pin", subject, client, trusted=trusted)
            return _keypad(request, selected_user_id=None, error="Wrong PIN")

        web_auth.backoff.record_success("pin", subject, client, trusted=trusted)
        device = access_store.device_for_token(
            request.cookies.get(web_auth.DEVICE_COOKIE)
        )
        if device is not None and user_id in device.trusted_user_ids:
            session_id = access_store.create_session(user_id, device.id)
            access_store.record_login(user_id)
            access_store.touch_device(device.id)
            resp = HTMLResponse("", headers={"HX-Redirect": "/"})
            web_auth.set_cookie(
                resp, request, web_auth.SESSION_COOKIE, session_id, max_age=None
            )
            web_auth.set_cookie(
                resp,
                request,
                web_auth.DEVICE_COOKIE,
                request.cookies[web_auth.DEVICE_COOKIE],
                max_age=web_auth.DEVICE_COOKIE_MAX_AGE,
            )
            return resp

        return _enrollment_response(request, user_id)

    def _enroll_page(
        request: Request,
        user_id: str,
        *,
        error=None,
        notice=None,
        wait_seconds=None,
        status_code=200,
        headers=None,
    ):
        user = access_store.get_user(user_id)
        gateway = config.communication.email_gateway if config else None
        return templates.TemplateResponse(
            "enroll.html",
            {
                "request": request,
                "user": user,
                "error": error,
                "notice": notice,
                "wait_seconds": wait_seconds,
                "can_email": bool(
                    user and user.email and gateway and gateway.is_configured
                ),
            },
            status_code=status_code,
            headers=headers or {},
        )

    def _enrollment_response(request: Request, user_id: str, error: str | None = None):
        """Second factor: the PIN is proven, now prove the device (spec §2.3)."""
        device = access_store.device_for_token(
            request.cookies.get(web_auth.DEVICE_COOKIE)
        )
        new_device_token = None
        if device is None:
            device, new_device_token = access_store.create_device(
                "New device", shared=False
            )

        # Bind the enroll token to this device's id, not client_key(request):
        # a device just minted above isn't reflected in request.cookies yet
        # (that only happens on the *next* request), so client_key(request)
        # would still fall back to the IP and the token could never resolve
        # once the browser starts sending the new vmc_device cookie.
        token = access_store.issue_enroll_token(user_id, device.id)
        resp = _enroll_page(request, user_id, error=error)
        web_auth.set_cookie(
            resp,
            request,
            web_auth.ENROLL_COOKIE,
            token,
            max_age=web_auth.ENROLL_COOKIE_MAX_AGE,
        )
        if new_device_token is not None:
            web_auth.set_cookie(
                resp,
                request,
                web_auth.DEVICE_COOKIE,
                new_device_token,
                max_age=web_auth.DEVICE_COOKIE_MAX_AGE,
            )
        return resp

    def _enroll_user_id(request: Request) -> str:
        """The user whose PIN this browser just proved, or 401."""
        user_id = access_store.resolve_enroll_token(
            request.cookies.get(web_auth.ENROLL_COOKIE), web_auth.client_key(request)
        )
        if user_id is None:
            # htmx does not touch the DOM on a non-2xx response, so without
            # HX-Redirect a lapsed enrollment window (or a device that
            # skipped enrollment entirely) would leave the user staring at
            # an unchanged screen with no error and no way forward.
            raise HTTPException(
                status_code=401,
                detail="Enrollment expired; sign in again",
                headers={"HX-Redirect": "/login"},
            )
        return user_id

    @public.post(
        "/login/enroll/send",
        response_class=HTMLResponse,
        dependencies=[Depends(require_htmx)],
    )
    async def send_enroll_otp(request: Request):
        user_id = _enroll_user_id(request)
        client = web_auth.client_key(request)
        # Every send counts as a failure, so repeated sends slow down (spec §2.3).
        remaining = web_auth.backoff.check("otp_send", user_id, client)
        if remaining is not None:
            return _enroll_page(
                request,
                user_id,
                error=f"Wait {int(remaining) + 1} s before asking for another code.",
                status_code=429,
                headers={"Retry-After": str(int(remaining) + 1)},
            )
        web_auth.backoff.record_failure("otp_send", user_id, client)

        user = access_store.get_user(user_id)
        gateway = config.communication.email_gateway
        device = access_store.device_for_token(
            request.cookies.get(web_auth.DEVICE_COOKIE)
        )
        if (
            user is None
            or not user.email
            or not gateway.is_configured
            or device is None
        ):
            return _enroll_page(
                request, user_id, error="Email is not available, use an emergency code"
            )
        code = access_store.issue_otp(user_id, device.id)
        ok = await send_email(
            gateway,
            user.email,
            "Vending machine sign-in code",
            f"Your one-time code is {code}. It expires in ten minutes.",
        )
        if not ok:
            return _enroll_page(
                request, user_id, error="Email could not be sent, use an emergency code"
            )
        return _enroll_page(request, user_id, notice="Code sent. Check your email.")

    @public.post(
        "/login/enroll",
        response_class=HTMLResponse,
        dependencies=[Depends(require_htmx)],
    )
    async def enroll_device(request: Request, code: str = Form(...)):
        user_id = _enroll_user_id(request)
        client = web_auth.client_key(request)
        device = access_store.device_for_token(
            request.cookies.get(web_auth.DEVICE_COOKIE)
        )
        if device is None:
            raise HTTPException(
                status_code=401,
                detail="Device record missing; sign in again",
                headers={"HX-Redirect": "/login"},
            )

        code = code.strip()
        kind = "otp" if len(code) == OTP_DIGITS else "emergency"
        subject = user_id if kind == "otp" else "pool"
        remaining = web_auth.backoff.check(kind, subject, client)
        if remaining is not None:
            return _enroll_page(
                request,
                user_id,
                error="Too many attempts.",
                wait_seconds=int(remaining) + 1,
                status_code=429,
                headers={"Retry-After": str(int(remaining) + 1)},
            )

        if kind == "otp":
            ok = access_store.verify_otp(user_id, device.id, code)
        else:
            ok = access_store.consume_emergency_code(code, user_id, "enroll")
            owner = access_store.owner()
            if (
                not ok
                and not access_store.setup_finalized
                and owner is not None
                and owner.id == user_id
            ):
                # Until Done, the setup code and the transfer code also enroll
                # the owner — and only the owner (Copilot review,
                # web_interface/routes.py:474): spec §3.1 step 1 and §3.3
                # step 4 both describe this as recovery for the owner whose
                # own step-1 response was lost, not a general-purpose code
                # any logged-in user can redeem. Without the owner check, a
                # tech or loader could enroll their own device with the
                # machine-visible setup code before Done, or a retained user
                # could enroll with the incoming owner's transfer code after
                # a transfer.
                ok = access_store.verify_setup_code(
                    code
                ) or access_store.verify_transfer_code(code)

        if not ok:
            web_auth.backoff.record_failure(kind, subject, client)
            return _enroll_page(request, user_id, error="That code was not accepted")

        web_auth.backoff.record_success(kind, subject, client)
        access_store.trust_device(device.id, user_id)
        session_id = access_store.create_session(user_id, device.id)
        access_store.record_login(user_id)
        resp = HTMLResponse("", headers={"HX-Redirect": "/"})
        web_auth.set_cookie(
            resp, request, web_auth.SESSION_COOKIE, session_id, max_age=None
        )
        web_auth.clear_cookie(resp, web_auth.ENROLL_COOKIE)
        access_store.clear_enroll_token(request.cookies[web_auth.ENROLL_COOKIE])
        return resp

    @public.post("/logout", dependencies=[Depends(require_htmx)])
    async def logout(request: Request):
        session_id = request.cookies.get(web_auth.SESSION_COOKIE)
        if session_id and access_store is not None:
            access_store.end_session(session_id)
        resp = HTMLResponse("", headers={"HX-Redirect": "/login"})
        web_auth.clear_cookie(resp, web_auth.SESSION_COOKIE)
        return resp

    def _setup_page(
        request: Request,
        *,
        error: str | None = None,
        form: dict | None = None,
        status_code: int = 200,
        headers: dict | None = None,
    ):
        return templates.TemplateResponse(
            "setup.html",
            {
                "request": request,
                "error": error,
                "form": form or {"name": "", "email": ""},
                "transfer": access_store.pending_transfer is not None,
            },
            status_code=status_code,
            headers=headers or {},
        )

    @public.get("/setup", response_class=HTMLResponse)
    async def setup_page(request: Request):
        if access_store is None:
            raise HTTPException(status_code=503, detail="Access store not loaded")
        ensure_setup_mode()
        # Nothing to do here once an owner exists and no transfer is live —
        # send the visitor on to the normal login/dashboard flow instead of
        # showing a wizard with no code that would accept.
        if not access_store.setup_mode and access_store.pending_transfer is None:
            return RedirectResponse("/", status_code=303)
        return _setup_page(request)

    @public.post(
        "/setup", response_class=HTMLResponse, dependencies=[Depends(require_htmx)]
    )
    async def setup_submit(
        request: Request,
        setup_code: str = Form(...),
        name: str = Form(...),
        email: str = Form(""),
        pin: str = Form(...),
        pin_confirm: str = Form(...),
        shared_device: str | None = Form(None),
    ):
        if access_store is None:
            raise HTTPException(status_code=503, detail="Access store not loaded")

        # Task 19 completes ownership transfer through this same handler:
        # while a transfer is pending, the code being checked is the
        # transfer code, not the setup code, and back-off is tracked
        # separately so a stranger guessing transfer codes can't also burn
        # down the setup code's budget or vice versa.
        in_transfer = access_store.pending_transfer is not None
        kind = "transfer" if in_transfer else "setup"
        form = {"name": name, "email": email}
        client = web_auth.client_key(request)

        remaining = web_auth.backoff.check(kind, kind, client)
        if remaining is not None:
            return _setup_page(
                request,
                error=f"Too many attempts. Try again in {int(remaining) + 1} s.",
                form=form,
                status_code=429,
                headers={"Retry-After": str(int(remaining) + 1)},
            )

        code_ok = (
            access_store.verify_transfer_code(setup_code)
            if in_transfer
            else access_store.verify_setup_code(setup_code)
        )
        if not code_ok:
            web_auth.backoff.record_failure(kind, kind, client)
            return _setup_page(request, error="That code was not accepted.", form=form)
        web_auth.backoff.record_success(kind, kind, client)

        # A wrong code above is the one thing worth slowing a stranger down
        # for; a mismatched confirmation or a weak PIN is the owner's own
        # typo at the machine, so neither touches back-off (spec's ordering).
        if pin != pin_confirm:
            return _setup_page(request, error="PINs do not match.", form=form)

        problem = pin_problem(pin)
        if problem:
            return _setup_page(request, error=problem, form=form)

        shared = shared_device is not None
        try:
            if in_transfer:
                owner = access_store.complete_transfer(name, email or None, pin)
            else:
                owner = access_store.create_user(name, email or None, Role.owner, pin)
        except OwnerExistsError:
            # Two racing step-1 submissions: the store enforces one owner
            # and refuses the second, so this must read as an ordinary
            # error, never a 500 (spec §3.1).
            return _setup_page(
                request, error="This machine already has an owner.", form=form
            )

        device, token = access_store.create_device(f"{name}'s device", shared=shared)
        access_store.trust_device(device.id, owner.id)
        access_store.record_login(owner.id)
        session_id = access_store.create_session(owner.id, device.id)

        # A completed transfer still has the retained users to walk (spec
        # §3.3 step 3) before the codes step; ordinary first-owner setup
        # goes straight to the codes step as before.
        next_step = "/setup/review" if in_transfer else "/setup/codes"
        resp = HTMLResponse("", headers={"HX-Redirect": next_step})
        web_auth.set_cookie(
            resp, request, web_auth.SESSION_COOKIE, session_id, max_age=None
        )
        web_auth.set_cookie(
            resp,
            request,
            web_auth.DEVICE_COOKIE,
            token,
            max_age=web_auth.DEVICE_COOKIE_MAX_AGE,
        )
        return resp

    def _can_email_owner(owner) -> bool:
        gateway = config.communication.email_gateway if config else None
        return bool(owner and owner.email and gateway and gateway.is_configured)

    def _setup_codes_page(
        request: Request,
        owner,
        *,
        notice: str | None = None,
        error: str | None = None,
        status_code: int = 200,
        headers: dict | None = None,
    ):
        # This page (and any re-render of it) always carries the plaintext
        # emergency-code pool while it is pending — Copilot review,
        # web_interface/routes.py:647: a cacheable GET response would let a
        # browser or reverse proxy retain/replay it after Done, contrary to
        # the spec's one-time-display intent. no-store is unconditional
        # here rather than gated on `_pending_codes` being non-empty, so a
        # cache entry from before Done can never be served stale either.
        resp_headers = {"Cache-Control": "no-store", **(headers or {})}
        return templates.TemplateResponse(
            "setup_codes.html",
            {
                "request": request,
                "codes": _pending_codes,
                "notice": notice,
                "error": error,
                "can_email": _can_email_owner(owner),
            },
            status_code=status_code,
            headers=resp_headers,
        )

    @public.get(
        "/setup/codes",
        response_class=HTMLResponse,
        dependencies=[Depends(web_auth.require(Permission.manage_ownership))],
    )
    async def setup_codes_page(request: Request):
        if access_store is None:
            raise HTTPException(status_code=503, detail="Access store not loaded")
        global _pending_codes
        # Once setup is finalized and nothing is left to show, there is
        # nothing this page can do — the plaintexts are gone by design.
        if access_store.setup_finalized and not _pending_codes:
            return RedirectResponse("/", status_code=303)
        if not _pending_codes:
            # First view only: a reload must show this same pool, never a
            # fresh one, or codes the owner already wrote down would be
            # silently invalidated.
            _pending_codes = access_store.generate_emergency_codes()
        owner = web_auth.current_principal(request).user
        return _setup_codes_page(request, owner)

    @public.post(
        "/setup/codes/email",
        response_class=HTMLResponse,
        dependencies=[
            Depends(web_auth.require(Permission.manage_ownership)),
            Depends(require_htmx),
        ],
    )
    async def email_setup_codes(request: Request):
        owner = web_auth.current_principal(request).user
        if not _pending_codes or not _can_email_owner(owner):
            return _setup_codes_page(
                request,
                owner,
                error="Email is not available; copy the codes above instead.",
            )
        gateway = config.communication.email_gateway
        body = (
            "These 20 emergency codes let you sign in to the vending "
            "machine dashboard on a browser it has never seen before, or "
            "authorise a new owner during an ownership transfer. Each code "
            "works exactly once. Keep them somewhere other than the "
            "machine itself — they exist for when the machine cannot be "
            "reached.\n\n" + "\n".join(_pending_codes)
        )
        ok = await send_email(
            gateway, owner.email, "Vending machine emergency codes", body
        )
        if not ok:
            return _setup_codes_page(
                request,
                owner,
                error="Email could not be sent; copy the codes above instead.",
            )
        return _setup_codes_page(
            request, owner, notice=f"Codes emailed to {owner.email}."
        )

    @public.post(
        "/setup/codes/done",
        dependencies=[
            Depends(web_auth.require(Permission.manage_ownership)),
            Depends(require_htmx),
        ],
    )
    async def finish_setup(request: Request):
        if access_store is None:
            raise HTTPException(status_code=503, detail="Access store not loaded")
        global _pending_codes
        access_store.finalize_setup()
        _pending_codes = []
        if display_controller is not None:
            display_controller.clear_setup_code()
        return HTMLResponse("", headers={"HX-Redirect": "/"})

    # --- Task 19: reviewing retained users after a completed transfer ---

    def _review_candidates() -> list:
        """Every user except the (new) owner, in a stable order.

        Recomputed fresh on every request — never cached — since a Keep is
        a no-op on the store and a Remove deletes exactly one entry, so
        re-deriving this list from live state is always cheap and correct.
        """
        owner = access_store.owner()
        owner_id = owner.id if owner else None
        return sorted(
            (u for u in access_store.users.values() if u.id != owner_id),
            key=lambda u: (u.name, u.id),
        )

    def _setup_review_response(request: Request, user):
        device_count = sum(
            1 for d in access_store.devices.values() if user.id in d.trusted_user_ids
        )
        return templates.TemplateResponse(
            "setup_review_user.html",
            {
                "request": request,
                "user": _user_row(user),
                "device_count": device_count,
            },
        )

    def _review_response_after(request: Request, order: list, user_id: str):
        """Render whichever candidate in *order* comes after *user_id*, or
        answer HX-Redirect to /setup/codes when that was the last one.

        *order* is the candidate list computed before the decision on
        *user_id* was applied — a Keep leaves it accurate as-is; a Remove
        only ever deletes *user_id* itself, so every later entry still
        resolves.
        """
        ids = [u.id for u in order]
        idx = ids.index(user_id) + 1
        while idx < len(ids):
            candidate = access_store.get_user(ids[idx])
            if candidate is not None:
                return _setup_review_response(request, candidate)
            idx += 1
        return HTMLResponse("", headers={"HX-Redirect": "/setup/codes"})

    @public.get(
        "/setup/review",
        response_class=HTMLResponse,
        dependencies=[Depends(web_auth.require(Permission.manage_ownership))],
    )
    async def setup_review_page(request: Request):
        candidates = _review_candidates()
        if not candidates:
            return RedirectResponse("/setup/codes", status_code=303)
        # A reload always restarts from the first remaining user — fine,
        # because Keep is idempotent (spec's own allowance).
        return _setup_review_response(request, candidates[0])

    @public.post(
        "/setup/review/{user_id}/keep",
        response_class=HTMLResponse,
        dependencies=[
            Depends(web_auth.require(Permission.manage_ownership)),
            Depends(require_htmx),
        ],
    )
    async def setup_review_keep(request: Request, user_id: str):
        order = _review_candidates()
        if user_id not in {u.id for u in order}:
            raise HTTPException(status_code=404, detail="No such user")
        # Keep is a no-op on the store: the user is simply not touched.
        return _review_response_after(request, order, user_id)

    @public.post(
        "/setup/review/{user_id}/remove",
        response_class=HTMLResponse,
        dependencies=[
            Depends(web_auth.require(Permission.manage_ownership)),
            Depends(require_htmx),
        ],
    )
    async def setup_review_remove(request: Request, user_id: str):
        order = _review_candidates()
        if user_id not in {u.id for u in order}:
            raise HTTPException(status_code=404, detail="No such user")
        # Sessions end before the user record itself is deleted.
        access_store.end_sessions_for_user(user_id)
        access_store.delete_user(user_id)
        return _review_response_after(request, order, user_id)

    router = APIRouter()

    @router.post(
        "/inventory/add",
        response_class=HTMLResponse,
        dependencies=[
            Depends(web_auth.require(Permission.edit_catalog)),
            Depends(require_htmx),
        ],
    )
    async def add_new_product(
        request: Request,
        sku: str = Form(...),
        name: str = Form(...),
        price: float = Form(...),
        slot: str | None = Form(None),
        kind: str = Form("other"),
    ):
        parsed_slot = int(slot) if slot not in (None, "") else None
        success = add_product(config, sku, name, price, slot=parsed_slot, kind=kind)
        if success and inventory_manager:
            inventory_manager.add_sku(sku, 0, tracked=False)

        return _render_inventory_table(request)

    @router.get(
        "/",
        response_class=HTMLResponse,
        dependencies=[Depends(web_auth.require(Permission.view_status))],
    )
    async def dashboard(request: Request):
        return templates.TemplateResponse(
            "dashboard.html", web_auth.template_context(request)
        )

    @router.get(
        "/config/machine",
        response_class=HTMLResponse,
        dependencies=[Depends(web_auth.require(Permission.edit_contacts))],
    )
    async def machine_info(request: Request):
        return templates.TemplateResponse(
            "partials/machine_info.html",
            web_auth.template_context(request, details=config.physical),
        )

    @router.get(
        "/config/contacts",
        response_class=HTMLResponse,
        dependencies=[Depends(web_auth.require(Permission.edit_contacts))],
    )
    async def contact_info(request: Request):
        return templates.TemplateResponse(
            "partials/contacts.html",
            web_auth.template_context(request, people=config.physical.people),
        )

    @router.get(
        "/config/payments",
        response_class=HTMLResponse,
        dependencies=[Depends(web_auth.require(Permission.edit_secrets))],
    )
    async def payment_config(request: Request):
        return templates.TemplateResponse(
            "partials/payments.html",
            web_auth.template_context(request, payment=config.payment),
        )

    @router.get(
        "/config/comms",
        response_class=HTMLResponse,
        dependencies=[Depends(web_auth.require(Permission.edit_secrets))],
    )
    async def comms_config(request: Request):
        return templates.TemplateResponse(
            "partials/comms.html",
            web_auth.template_context(request, comm=config.communication),
        )

    def _locked_skus() -> dict[str, str]:
        if not vmc_instance:
            return {}
        return {
            f["sku"]: f["code"]
            for f in vmc_instance.active_faults()
            if f["scope"] == "product"
        }

    async def _render_status(request: Request):
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
                loc
                for loc, temp in health["temperatures"].items()
                if not temp["in_range"]
            ]
            if out_of_range:
                issues.append(f"Temp issues: {', '.join(out_of_range)}")

        payment_enabled = availability.payment_enabled if availability else None
        payment_reasons = (
            availability.payment_blocking_reasons() if availability else []
        )

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
            web_auth.template_context(
                request,
                status=status,
                is_healthy=is_healthy,
                issues=issues,
                active_faults=active_faults,
                payment_enabled=payment_enabled,
                payment_reasons=payment_reasons,
                machine_stopped=(
                    None if payment_enabled is None else not payment_enabled
                ),
            ),
        )

    @router.get(
        "/status",
        response_class=HTMLResponse,
        dependencies=[Depends(web_auth.require(Permission.view_status))],
    )
    async def status_fragment(request: Request):
        return await _render_status(request)

    @router.post(
        "/faults/{key}/clear",
        response_class=HTMLResponse,
        dependencies=[
            Depends(web_auth.require(Permission.clear_faults)),
            Depends(require_htmx),
        ],
    )
    async def clear_fault(request: Request, key: str):
        if not vmc_instance or not vmc_instance.clear_fault(key, by="admin"):
            raise HTTPException(
                status_code=404, detail=f"No active fault with key {key}"
            )
        return await _render_status(request)

    @router.post(
        "/action/{command}",
        dependencies=[
            Depends(web_auth.require(Permission.machine_controls)),
            Depends(require_htmx),
        ],
    )
    async def control_action(command: str):
        result = perform_command(command, vmc_instance)
        return HTMLResponse(f"<p>{result}</p>")

    @router.get(
        "/logs",
        response_class=HTMLResponse,
        dependencies=[Depends(web_auth.require(Permission.view_logs))],
    )
    async def view_logs(request: Request):
        lines = await asyncio.to_thread(tail, LOG_PATH, 10)
        return templates.TemplateResponse(
            "partials/logs_fragment.html",
            web_auth.template_context(request, logs=lines),
        )

    @router.get(
        "/health",
        response_class=HTMLResponse,
        dependencies=[Depends(web_auth.require(Permission.view_status))],
    )
    async def health_summary(request: Request):
        if not health_monitor:
            return HTMLResponse("<div>Health monitor not initialized</div>")
        health = health_monitor.get_summary()
        for name in EXPECTED_SUBSYSTEMS:
            health["subsystems"].setdefault(name, HealthMonitor.empty_subsystem_row())
        health["availability"] = availability.table() if availability else []
        health["payment_enabled"] = (
            availability.payment_enabled if availability else None
        )
        return templates.TemplateResponse(
            "partials/health_fragment.html",
            web_auth.template_context(request, health=health),
        )

    @router.get(
        "/activity",
        response_class=HTMLResponse,
        dependencies=[Depends(web_auth.require(Permission.view_status))],
    )
    async def activity_fragment(request: Request, period: int = Query(default=24)):
        if not event_recorder:
            return HTMLResponse(
                '<div class="bg-white rounded-xl border border-gray-200 shadow-sm p-5">'
                '<p class="text-gray-400 text-sm">Activity data not available yet.</p></div>'
            )
        if period not in (24, 168, 720):
            period = 24
        summary = await asyncio.to_thread(event_recorder.get_summary, period)
        average = await asyncio.to_thread(event_recorder.get_historical_average, period)
        return templates.TemplateResponse(
            "partials/activity_fragment.html",
            web_auth.template_context(
                request, period=period, summary=summary, average=average
            ),
        )

    @router.get(
        "/kpi",
        response_class=HTMLResponse,
        dependencies=[Depends(web_auth.require(Permission.view_status))],
    )
    async def kpi_fragment(request: Request):
        if event_recorder:
            summary = await asyncio.to_thread(event_recorder.get_summary, 24)
            average = await asyncio.to_thread(event_recorder.get_historical_average, 24)
        else:
            summary = None
            average = None
        return templates.TemplateResponse(
            "partials/kpi_fragment.html",
            web_auth.template_context(request, summary=summary, average=average),
        )

    @router.get(
        "/inventory",
        response_class=HTMLResponse,
        dependencies=[Depends(web_auth.require(Permission.view_status))],
    )
    async def inventory_view(request: Request):
        return _render_inventory_table(request)

    @router.get(
        "/inventory/edit/{sku}/catalog",
        response_class=HTMLResponse,
        dependencies=[Depends(web_auth.require(Permission.edit_catalog))],
    )
    async def edit_inventory_catalog(request: Request, sku: str):
        product = next((p for p in config.products if p.sku == sku), None)
        return templates.TemplateResponse(
            "partials/inventory_catalog_form.html",
            web_auth.template_context(request, product=product),
        )

    @router.post(
        "/inventory/update/{sku}/catalog",
        response_class=HTMLResponse,
        dependencies=[
            Depends(web_auth.require(Permission.edit_catalog)),
            Depends(require_htmx),
        ],
    )
    async def update_inventory_catalog(
        request: Request,
        sku: str,
        name: str = Form(...),
        price: float = Form(...),
        kind: str = Form("other"),
    ):
        # slot=None leaves the stored slot untouched (services/config_store.py
        # update_product) — this endpoint owns name/price/kind only.
        update_product(config, sku, name, price, slot=None, kind=kind)

        return _render_inventory_table(request)

    def _inventory_count(product: Product) -> int:
        if inventory_manager:
            return inventory_manager.get_count(product.sku)
        return product.inventory_count

    def _is_tracked(product: Product) -> bool:
        if inventory_manager:
            return inventory_manager.is_tracked(product.sku)
        return product.track_inventory

    def _render_inventory_table(request: Request):
        # Counts live in InventoryManager, not on Product (see
        # _inventory_count above) — every render of this partial must read
        # through it so a loader's placement POST is reflected immediately
        # instead of showing the stale/zero value still on Product.
        return templates.TemplateResponse(
            "partials/inventory_table.html",
            web_auth.template_context(
                request,
                products=config.products,
                locked=_locked_skus(),
                inventory_counts={p.sku: _inventory_count(p) for p in config.products},
            ),
        )

    @router.get(
        "/inventory/edit/{sku}/placement",
        response_class=HTMLResponse,
        dependencies=[Depends(web_auth.require(Permission.edit_placement))],
    )
    async def edit_inventory_placement(request: Request, sku: str):
        product = next((p for p in config.products if p.sku == sku), None)
        return templates.TemplateResponse(
            "partials/inventory_placement_form.html",
            web_auth.template_context(
                request,
                product=product,
                inventory_count=_inventory_count(product) if product else 0,
                tracked=_is_tracked(product) if product else False,
            ),
        )

    @router.post(
        "/inventory/update/{sku}/placement",
        response_class=HTMLResponse,
        dependencies=[
            Depends(web_auth.require(Permission.edit_placement)),
            Depends(require_htmx),
        ],
    )
    async def update_inventory_placement(
        request: Request,
        sku: str,
        slot: int = Form(...),
        inventory_count: int = Form(...),
        track_inventory: str | None = Form(None),
    ):
        product = next((p for p in config.products if p.sku == sku), None)
        tracked = track_inventory is not None
        if product:
            if inventory_count < 0:
                # Mirror services/config_store.py's slot < 0 guard: reject
                # the whole write and log a warning rather than let a
                # negative stock level silently corrupt InventoryManager —
                # restocking is the loader's job, and this is exactly the
                # workflow that must not be allowed to corrupt data.
                logger.warning(
                    f"Cannot update placement SKU={sku}: inventory_count "
                    f"{inventory_count} is invalid (must be >= 0)"
                )
                return _render_inventory_table(request)

            # This endpoint owns slot/count/tracking only — pass the
            # product's own stored name/price/kind through unchanged so a
            # placement POST can never smuggle a catalog change, regardless
            # of what a hostile form body contains.
            slot_ok = update_product(
                config, sku, product.name, product.price, slot=slot, kind=None
            )
            # update_product returns False when the requested slot is
            # already in use (or negative); that used to be ignored, so a
            # rejected slot change still wrote the count/tracking below,
            # leaving placement state half-applied (Copilot review,
            # web_interface/routes.py:1202). A validation failure must leave
            # every field of this form unchanged, not just the slot.
            if not slot_ok:
                return _render_inventory_table(request)
            if inventory_manager:
                inventory_manager.add_sku(sku, inventory_count, tracked=tracked)
            else:
                product.inventory_count = inventory_count
                product.track_inventory = tracked
                save_config(config)

        return _render_inventory_table(request)

    @router.post(
        "/inventory/delete/{sku}",
        response_class=HTMLResponse,
        dependencies=[
            Depends(web_auth.require(Permission.edit_catalog)),
            Depends(require_htmx),
        ],
    )
    async def delete_inventory_item(request: Request, sku: str):
        success = delete_product(config, sku)
        if success and inventory_manager:
            inventory_manager.remove_sku(sku)
        return _render_inventory_table(request)

    @router.get(
        "/inventory/new",
        response_class=HTMLResponse,
        dependencies=[Depends(web_auth.require(Permission.edit_catalog))],
    )
    async def new_product_form(request: Request):
        # Blank form, random temporary SKU
        random_sku = f"SKU-{uuid4().hex[:6].upper()}"
        product = Product(sku=random_sku, name="", price=0.0, inventory_count=0)
        return templates.TemplateResponse(
            "partials/inventory_add_form.html",
            web_auth.template_context(request, product=product, mode="new"),
        )

    @router.get(
        "/inventory/copy/{sku}",
        response_class=HTMLResponse,
        dependencies=[Depends(web_auth.require(Permission.edit_catalog))],
    )
    async def copy_product_form(request: Request, sku: str):
        base = next((p for p in config.products if p.sku == sku), None)
        if base:
            new_sku = f"SKU-{uuid4().hex[:6].upper()}"
            copied = Product(
                sku=new_sku,
                name=f"{base.name} Copy",
                price=base.price,
                inventory_count=base.inventory_count,
                description=base.description,
                image_url=base.image_url,
                track_inventory=base.track_inventory,
                kind=base.kind,
            )
            return templates.TemplateResponse(
                "partials/inventory_add_form.html",
                web_auth.template_context(request, product=copied, mode="copy"),
            )

    def _guard_owner_target(principal: web_auth.Principal, user_id: str) -> None:
        """403 when *user_id* is the owner and the caller lacks manage_ownership.

        A secretary is the owner's delegate — manage_users lets them touch
        every other user, but the spec (§4) carves the owner out of that:
        any write whose target is the owner needs manage_ownership. Called
        first, before any store mutation, by every one of the four writes
        that take a user id (disable, enable, reset-pin, delete).
        """
        owner = access_store.owner()
        if (
            owner is not None
            and owner.id == user_id
            and Permission.manage_ownership not in principal.perms
        ):
            raise HTTPException(status_code=403, detail="Not permitted")

    def _guard_owner_self_lockout(principal: web_auth.Principal, user_id: str) -> None:
        """403 when *user_id* is the owner disabling or deleting themselves.

        Copilot review, web_interface/routes.py:1277: _guard_owner_target
        only blocks a *non-owner* from targeting the owner; the owner always
        holds manage_ownership, so it let the owner disable or delete their
        own account. Deleting the sole owner leaves setup_finalized true
        with no owner, and ensure_setup_mode()'s begin_setup() then raises
        AccessError("setup has already been finalized") instead of
        recovering — verified directly by
        test_access.py::TestSetupCode::test_begin_setup_after_finalize_raises_and_stays_finalized,
        which already covers exactly this "finalized, no fresh code" state
        with no try/except anywhere on the request path (access_gate,
        setup_page, ensure_setup_mode), so the dashboard would serve a 500
        on every route until someone deletes data/access.json by hand.
        Disabling self is a milder version of the same lockout. Called only
        by disable and delete, in addition to _guard_owner_target;
        reset-pin and enable carry no such risk and are unaffected.
        """
        owner = access_store.owner()
        if owner is not None and owner.id == user_id and principal.user.id == user_id:
            raise HTTPException(
                status_code=403, detail="The owner cannot do this to their own account"
            )

    def _guard_owner_device(principal: web_auth.Principal, device) -> None:
        """403 when *device* trusts the owner and the caller lacks
        manage_ownership.

        Sibling to _guard_owner_target for the device case: same
        permission logic (manage_users covers everyone except the owner,
        manage_ownership is required to touch the owner), but the target
        here is "a device the owner is trusted on" rather than "the owner's
        user record" — spec §4's "remove devices" line sits inside the
        manage_users row whose secretary cell reads "never the owner", and
        that carve-out has to reach both device writes (forget, shared) the
        same way it reaches the four user writes. Called after the caller
        has already confirmed *device* exists (a 404 for an unknown id must
        not depend on ownership), before any store mutation.
        """
        owner = access_store.owner()
        if (
            owner is not None
            and owner.id in device.trusted_user_ids
            and Permission.manage_ownership not in principal.perms
        ):
            raise HTTPException(status_code=403, detail="Not permitted")

    def _user_row(user) -> dict:
        # A plain dict with only what a template needs — never pin_hash or
        # pin_salt, the same hazard web_auth.TemplateUser exists to avoid
        # for current_user (see its docstring).
        return {
            "id": user.id,
            "name": user.name,
            "email": user.email,
            "role": user.role.value,
            "disabled": user.disabled,
            "last_login_at": user.last_login_at,
        }

    def _render_users_list(
        request: Request,
        *,
        error: str | None = None,
        notice: str | None = None,
        status_code: int = 200,
        headers: dict[str, str] | None = None,
        transfer_code: str | None = None,
        new_codes: list[str] | None = None,
    ):
        owner = access_store.owner()
        users = sorted(access_store.users.values(), key=lambda u: u.name)
        device_counts = {
            u.id: sum(
                1 for d in access_store.devices.values() if u.id in d.trusted_user_ids
            )
            for u in users
        }
        # This partial is the render target for /users/transfer and
        # /users/codes/regenerate, which pass transfer_code/new_codes —
        # plaintext secrets shown exactly once (Copilot review,
        # web_interface/routes.py:647, extended per its own note to "any
        # response that renders plaintext codes"). no-store unconditionally
        # rather than only when those are set, so the same header applies
        # every time this function is the response, not just sometimes.
        resp_headers = {"Cache-Control": "no-store", **(headers or {})}
        return templates.TemplateResponse(
            "partials/users_list.html",
            web_auth.template_context(
                request,
                users=[_user_row(u) for u in users],
                owner_id=owner.id if owner else None,
                device_counts=device_counts,
                unused_codes=access_store.unused_emergency_code_count(),
                pending_transfer=access_store.pending_transfer,
                error=error,
                notice=notice,
                # Each shown exactly once, in the response that created it —
                # never persisted, never re-rendered on a later request.
                transfer_code=transfer_code,
                new_codes=new_codes,
            ),
            status_code=status_code,
            headers=resp_headers,
        )

    @router.get(
        "/users",
        response_class=HTMLResponse,
        dependencies=[Depends(web_auth.require(Permission.manage_users))],
    )
    async def users_list(request: Request):
        return _render_users_list(request)

    def _new_user_form(
        request: Request,
        principal: web_auth.Principal,
        *,
        error: str | None = None,
        form: dict | None = None,
    ):
        # A secretary may create every role except owner — there is exactly
        # one owner, and only manage_ownership can mint one (spec §4.1).
        roles = [
            r
            for r in Role
            if r is not Role.owner or Permission.manage_ownership in principal.perms
        ]
        return templates.TemplateResponse(
            "partials/user_form.html",
            web_auth.template_context(
                request,
                roles=roles,
                error=error,
                form=form or {"name": "", "email": "", "role": ""},
            ),
        )

    @router.get(
        "/users/new",
        response_class=HTMLResponse,
        dependencies=[Depends(web_auth.require(Permission.manage_users))],
    )
    async def new_user_form(request: Request):
        principal = web_auth.current_principal(request)
        return _new_user_form(request, principal)

    @router.post(
        "/users/new",
        response_class=HTMLResponse,
        dependencies=[
            Depends(web_auth.require(Permission.manage_users)),
            Depends(require_htmx),
        ],
    )
    async def create_user(
        request: Request,
        name: str = Form(...),
        email: str = Form(""),
        role: str = Form(...),
        pin: str = Form(...),
    ):
        principal = web_auth.current_principal(request)
        try:
            role_enum = Role(role)
        except ValueError:
            return _render_users_list(request, error="Invalid role.")
        # A secretary submitting role=owner directly (bypassing the hidden
        # <option>) must still be refused server-side — the form only hides
        # the control, it is never the authority.
        if (
            role_enum is Role.owner
            and Permission.manage_ownership not in principal.perms
        ):
            raise HTTPException(status_code=403, detail="Not permitted")

        problem = pin_problem(pin)
        if problem:
            # Spec §6: a PIN that fails pin_problem returns with the reason
            # and creates nobody — the list is the surface this renders
            # back into, so that is what carries the error here.
            return _render_users_list(request, error=problem)

        try:
            access_store.create_user(name, email or None, role_enum, pin)
        except OwnerExistsError:
            return _render_users_list(
                request, error="This machine already has an owner."
            )
        return _render_users_list(request, notice=f"{name} added.")

    @router.post(
        "/users/{user_id}/disable",
        response_class=HTMLResponse,
        dependencies=[
            Depends(web_auth.require(Permission.manage_users)),
            Depends(require_htmx),
        ],
    )
    async def disable_user(request: Request, user_id: str):
        principal = web_auth.current_principal(request)
        _guard_owner_target(principal, user_id)
        _guard_owner_self_lockout(principal, user_id)
        try:
            access_store.set_user_disabled(user_id, True)
        except AccessError:
            raise HTTPException(status_code=404, detail="No such user")
        # A disabled user must not keep an open tab working until it idles
        # out on its own (spec's intent behind disable existing at all).
        access_store.end_sessions_for_user(user_id)
        return _render_users_list(request)

    @router.post(
        "/users/{user_id}/enable",
        response_class=HTMLResponse,
        dependencies=[
            Depends(web_auth.require(Permission.manage_users)),
            Depends(require_htmx),
        ],
    )
    async def enable_user(request: Request, user_id: str):
        principal = web_auth.current_principal(request)
        _guard_owner_target(principal, user_id)
        try:
            access_store.set_user_disabled(user_id, False)
        except AccessError:
            raise HTTPException(status_code=404, detail="No such user")
        return _render_users_list(request)

    @router.post(
        "/users/{user_id}/reset-pin",
        response_class=HTMLResponse,
        dependencies=[
            Depends(web_auth.require(Permission.manage_users)),
            Depends(require_htmx),
        ],
    )
    async def reset_user_pin(request: Request, user_id: str, pin: str = Form(...)):
        principal = web_auth.current_principal(request)
        _guard_owner_target(principal, user_id)
        problem = pin_problem(pin)
        if problem:
            return _render_users_list(request, error=problem)
        try:
            # set_user_pin rehashes and drops the user from every device, so
            # the next login re-enrolls (spec §4.1) — AccessStore already
            # does both halves of that.
            access_store.set_user_pin(user_id, pin)
        except AccessError:
            raise HTTPException(status_code=404, detail="No such user")
        return _render_users_list(request, notice="PIN reset.")

    @router.post(
        "/users/{user_id}/delete",
        response_class=HTMLResponse,
        dependencies=[
            Depends(web_auth.require(Permission.manage_users)),
            Depends(require_htmx),
        ],
    )
    async def delete_user_route(request: Request, user_id: str):
        principal = web_auth.current_principal(request)
        _guard_owner_target(principal, user_id)
        _guard_owner_self_lockout(principal, user_id)
        try:
            access_store.delete_user(user_id)
        except AccessError:
            raise HTTPException(status_code=404, detail="No such user")
        access_store.end_sessions_for_user(user_id)
        return _render_users_list(request)

    def _check_owner_pin(
        request: Request, principal: web_auth.Principal, pin: str
    ) -> tuple[str, int | None] | None:
        """Verify the caller's own PIN; None on success, else (message,
        retry_after_seconds) on failure.

        `retry_after_seconds` is the whole-second wait to report as a
        Retry-After header (and a 429 status, at the call site) when the
        PIN back-off has tripped; it is None for an ordinary wrong-PIN
        refusal, which stays a plain re-render — the shape every other
        back-off site in this file already uses (see /login's PIN check,
        /login/enroll/send and /login/enroll, and /setup): a bare error
        message cannot carry a status code, so a caller that only got a
        string would fall through to the route's default 200.

        Always the caller's own user id — never some other user's — under
        back-off kind "pin", the same kind and subject a login attempt for
        this user uses, so a stranger burning down this budget on the
        login page also slows an attacker here and vice versa. `trusted`
        follows the caller's own device, exactly as login does.
        """
        client = web_auth.client_key(request)
        trusted = web_auth.is_trusted_client(request, principal.user.id)
        remaining = web_auth.backoff.check(
            "pin", principal.user.id, client, trusted=trusted
        )
        if remaining is not None:
            retry_after = int(remaining) + 1
            return f"Too many attempts. Try again in {retry_after} s.", retry_after
        if not access_store.verify_user_pin(principal.user.id, pin):
            web_auth.backoff.record_failure(
                "pin", principal.user.id, client, trusted=trusted
            )
            return "Wrong PIN.", None
        web_auth.backoff.record_success(
            "pin", principal.user.id, client, trusted=trusted
        )
        return None

    def _owner_pin_problem_response(request: Request, problem: tuple[str, int | None]):
        """Render the users list for a _check_owner_pin failure.

        A tripped back-off (retry_after is not None) answers 429 with
        Retry-After, matching every other back-off site in this file; an
        ordinary wrong PIN stays a plain 200 re-render with the error.
        """
        message, retry_after = problem
        if retry_after is not None:
            return _render_users_list(
                request,
                error=message,
                status_code=429,
                headers={"Retry-After": str(retry_after)},
            )
        return _render_users_list(request, error=message)

    @router.post(
        "/users/transfer",
        response_class=HTMLResponse,
        dependencies=[
            Depends(web_auth.require(Permission.manage_ownership)),
            Depends(require_htmx),
        ],
    )
    async def start_ownership_transfer(
        request: Request, pin: str = Form(...), emergency_code: str = Form(...)
    ):
        # Guard first, before the PIN check and before the code check, so a
        # re-entrant call while a transfer is already pending can spend
        # nothing: it must not consume a second emergency code, must not
        # overwrite the existing pending_transfer, and must not invalidate
        # the transfer code already handed to the incoming owner. The
        # template hides this form while a transfer is pending, but that is
        # not the authority (spec §4) — this check is.
        if access_store.pending_transfer is not None:
            return _render_users_list(
                request,
                error=(
                    "A transfer is already pending. Cancel it before starting another."
                ),
            )

        # Spec §3.3 step 1's whole point: check first, consume only on
        # success, never the other way round — a wrong PIN must leave the
        # emergency-code pool untouched, or a typo in the owner's own
        # browser could burn down the machine's only offline recovery.
        principal = web_auth.current_principal(request)
        problem = _check_owner_pin(request, principal, pin)
        if problem:
            return _owner_pin_problem_response(request, problem)

        client = web_auth.client_key(request)
        remaining = web_auth.backoff.check("transfer", "pool", client)
        if remaining is not None:
            return _render_users_list(
                request,
                error=f"Too many attempts. Try again in {int(remaining) + 1} s.",
                status_code=429,
            )

        # consume_emergency_code only mutates the pool on a match, so a
        # wrong code both fails this check and consumes nothing — the PIN
        # check above already ran, so this is the only mutation gated on
        # both proofs passing.
        if not access_store.consume_emergency_code(
            emergency_code.strip(), principal.user.id, "transfer"
        ):
            web_auth.backoff.record_failure("transfer", "pool", client)
            return _render_users_list(request, error="That code was not accepted.")
        web_auth.backoff.record_success("transfer", "pool", client)

        # Nothing else changes here: the current owner stays fully in
        # control until the incoming owner completes the wizard.
        transfer_code = access_store.start_transfer(principal.user.id)
        return _render_users_list(
            request,
            notice="Ownership transfer started. Give this code to the new owner.",
            transfer_code=transfer_code,
        )

    @router.post(
        "/users/transfer/cancel",
        response_class=HTMLResponse,
        dependencies=[
            Depends(web_auth.require(Permission.manage_ownership)),
            Depends(require_htmx),
        ],
    )
    async def cancel_ownership_transfer(request: Request, pin: str = Form(...)):
        principal = web_auth.current_principal(request)
        problem = _check_owner_pin(request, principal, pin)
        if problem:
            return _owner_pin_problem_response(request, problem)
        access_store.cancel_transfer()
        return _render_users_list(request, notice="Ownership transfer cancelled.")

    @router.post(
        "/users/codes/regenerate",
        response_class=HTMLResponse,
        dependencies=[
            Depends(web_auth.require(Permission.manage_ownership)),
            Depends(require_htmx),
        ],
    )
    async def regenerate_emergency_codes_route(request: Request, pin: str = Form(...)):
        principal = web_auth.current_principal(request)
        problem = _check_owner_pin(request, principal, pin)
        if problem:
            return _owner_pin_problem_response(request, problem)
        # Replaces the whole pool, used codes included; the old codes stop
        # working immediately (AccessStore.generate_emergency_codes).
        new_codes = access_store.generate_emergency_codes()
        return _render_users_list(
            request, notice="Emergency codes regenerated.", new_codes=new_codes
        )

    @router.post(
        "/users/report",
        response_class=HTMLResponse,
        dependencies=[
            Depends(web_auth.require(Permission.manage_ownership)),
            Depends(require_htmx),
        ],
    )
    async def email_machine_report_route(request: Request):
        principal = web_auth.current_principal(request)
        owner = principal.user
        if not _can_email_owner(owner):
            return _render_users_list(
                request,
                error="Email is not available; check the email gateway settings.",
            )
        gateway = config.communication.email_gateway
        # machine_report already omits every hash and PIN (spec §3.4).
        report = access_store.machine_report(config)
        ok = await send_email(
            gateway, owner.email, "Vending machine access report", report
        )
        if not ok:
            return _render_users_list(request, error="Email could not be sent.")
        return _render_users_list(request, notice=f"Report emailed to {owner.email}.")

    def _device_row(device) -> dict:
        # A plain dict with only what the template needs — never token_hash,
        # the fingerprint of the cookie value has no business on a page.
        return {
            "id": device.id,
            "label": device.label,
            "shared": device.shared,
            "trusted_user_ids": list(device.trusted_user_ids),
            "last_seen_at": device.last_seen_at,
        }

    def _render_devices_list(request: Request, *, notice: str | None = None):
        owner = access_store.owner()
        devices = sorted(access_store.devices.values(), key=lambda d: d.label)
        user_names = {u.id: u.name for u in access_store.users.values()}
        return templates.TemplateResponse(
            "partials/devices_list.html",
            web_auth.template_context(
                request,
                devices=[_device_row(d) for d in devices],
                user_names=user_names,
                owner_id=owner.id if owner else None,
                notice=notice,
            ),
        )

    @router.get(
        "/devices",
        response_class=HTMLResponse,
        dependencies=[Depends(web_auth.require(Permission.manage_users))],
    )
    async def devices_list(request: Request):
        return _render_devices_list(request)

    @router.post(
        "/devices/{device_id}/forget",
        response_class=HTMLResponse,
        dependencies=[
            Depends(web_auth.require(Permission.manage_users)),
            Depends(require_htmx),
        ],
    )
    async def forget_device_route(request: Request, device_id: str):
        device = access_store.devices.get(device_id)
        if device is None:
            raise HTTPException(status_code=404, detail="No such device")
        # Existence is checked first so a 404 for an unknown id never
        # depends on ownership; the owner-device guard runs only once we
        # know there is a device to reason about.
        principal = web_auth.current_principal(request)
        _guard_owner_device(principal, device)
        # Order matters: a forgotten device must not keep whoever is using it
        # logged in one request longer than necessary (resolve_session would
        # eventually refuse it once the device record is gone, but ending the
        # session here is immediate and keeps the in-memory table from
        # growing with sessions nothing will ever resolve again).
        access_store.end_sessions_for_device(device_id)
        access_store.forget_device(device_id)
        return _render_devices_list(request, notice="Device forgotten.")

    @router.post(
        "/devices/{device_id}/shared",
        response_class=HTMLResponse,
        dependencies=[
            Depends(web_auth.require(Permission.manage_users)),
            Depends(require_htmx),
        ],
    )
    async def toggle_device_shared_route(request: Request, device_id: str):
        device = access_store.devices.get(device_id)
        if device is None:
            raise HTTPException(status_code=404, detail="No such device")
        principal = web_auth.current_principal(request)
        _guard_owner_device(principal, device)
        access_store.set_device_shared(device_id, not device.shared)
        return _render_devices_list(request)

    def _screen_context(request: Request) -> dict:
        status = (
            vmc_instance.get_status()
            if vmc_instance
            else {"state": "unknown", "credit_escrow": 0.0}
        )
        faults = vmc_instance.active_faults() if vmc_instance else []
        health = (
            health_monitor.get_summary()
            if health_monitor
            else {"subsystems": {}, "mqtt_connected": False}
        )
        for name in EXPECTED_SUBSYSTEMS:
            health["subsystems"].setdefault(name, HealthMonitor.empty_subsystem_row())
        kinds = {}
        for kind in ("ice", "water"):
            ok, failing = (
                availability.sale_available(kind) if availability else (None, [])
            )
            kinds[kind] = {"ok": ok, "failing": failing}
        return web_auth.template_context(
            request,
            status=status,
            faults=faults,
            health=health,
            kinds=kinds,
            payment_enabled=availability.payment_enabled if availability else None,
            payment_reasons=(
                availability.payment_blocking_reasons() if availability else []
            ),
        )

    @router.get(
        "/screen",
        response_class=HTMLResponse,
        dependencies=[Depends(web_auth.require(Permission.view_status))],
    )
    async def screen(request: Request):
        return templates.TemplateResponse(
            "screen.html", web_auth.template_context(request)
        )

    @router.get(
        "/screen/body",
        response_class=HTMLResponse,
        dependencies=[Depends(web_auth.require(Permission.view_status))],
    )
    async def screen_body(request: Request):
        ctx = _screen_context(request)
        if event_recorder:
            summary = await asyncio.to_thread(event_recorder.get_summary, 24)
            ctx["money_24h"] = summary["money_in"]
            ctx["vends_24h"] = summary["products_out"]
        else:
            ctx["money_24h"] = None
            ctx["vends_24h"] = None
        return templates.TemplateResponse("partials/screen_body.html", ctx)

    app.include_router(router)
    app.include_router(public)
