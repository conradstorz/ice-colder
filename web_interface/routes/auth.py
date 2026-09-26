"""Login, enrollment, logout, setup and setup-code routes.

Everything that used to live on routes.py's `public` router (no session
dependency — these routes exist precisely to establish a session) moved
here verbatim, per Task 1 executor resolution 2. Handlers dereference
`context.xxx` at request time rather than binding module globals at import
(see web_interface/context.py's module docstring).
"""

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from services.access import (
    OTP_DIGITS,
    OwnerExistsError,
    Permission,
    Role,
)
from services.auth_policy import pin_problem
from services.mailer import send_email
from web_interface import auth as web_auth
from web_interface import context

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


def build_router(templates: Jinja2Templates) -> APIRouter:
    public = APIRouter()

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
                "users": context.access_store.enabled_users()
                if context.access_store
                else [],
                "selected_user_id": selected_user_id,
                "error": error,
                "wait_seconds": wait_seconds,
            },
            status_code=status_code,
            headers=headers or {},
        )

    @public.get("/login", response_class=HTMLResponse)
    async def login_page(request: Request):
        if context.access_store is None:
            raise HTTPException(status_code=503, detail="Access store not loaded")
        if context.access_store.setup_mode:
            return RedirectResponse("/setup", status_code=303)
        return templates.TemplateResponse(
            "login.html",
            {
                "request": request,
                "users": context.access_store.enabled_users(),
                "selected_user_id": None,
                "error": None,
                "wait_seconds": None,
            },
        )

    @public.post(
        "/login",
        response_class=HTMLResponse,
        dependencies=[Depends(context.require_htmx)],
    )
    async def login_submit(
        request: Request, user_id: str = Form(...), pin: str = Form(...)
    ):
        if context.access_store is None:
            raise HTTPException(status_code=503, detail="Access store not loaded")
        client = web_auth.client_key(request)
        trusted = web_auth.is_trusted_client(request, user_id)
        # See _UNKNOWN_USER_SUBJECT: bound the back-off subject to known
        # users so an arbitrary form value can't buy an unbounded, unthrottled
        # counter.
        subject = (
            user_id
            if context.access_store.get_user(user_id) is not None
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
        if not context.access_store.verify_user_pin(user_id, pin):
            web_auth.backoff.record_failure("pin", subject, client, trusted=trusted)
            return _keypad(request, selected_user_id=None, error="Wrong PIN")

        web_auth.backoff.record_success("pin", subject, client, trusted=trusted)
        device = context.access_store.device_for_token(
            request.cookies.get(web_auth.DEVICE_COOKIE)
        )
        if device is not None and user_id in device.trusted_user_ids:
            session_id = context.access_store.create_session(user_id, device.id)
            context.access_store.record_login(user_id)
            context.access_store.touch_device(device.id)
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
        user = context.access_store.get_user(user_id)
        gateway = context.config.communication.email_gateway if context.config else None
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
        device = context.access_store.device_for_token(
            request.cookies.get(web_auth.DEVICE_COOKIE)
        )
        new_device_token = None
        if device is None:
            device, new_device_token = context.access_store.create_device(
                "New device", shared=False
            )

        # Bind the enroll token to this device's id, not client_key(request):
        # a device just minted above isn't reflected in request.cookies yet
        # (that only happens on the *next* request), so client_key(request)
        # would still fall back to the IP and the token could never resolve
        # once the browser starts sending the new vmc_device cookie.
        token = context.access_store.issue_enroll_token(user_id, device.id)
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
        user_id = context.access_store.resolve_enroll_token(
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
        dependencies=[Depends(context.require_htmx)],
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

        user = context.access_store.get_user(user_id)
        gateway = context.config.communication.email_gateway
        device = context.access_store.device_for_token(
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
        code = context.access_store.issue_otp(user_id, device.id)
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
        dependencies=[Depends(context.require_htmx)],
    )
    async def enroll_device(request: Request, code: str = Form(...)):
        user_id = _enroll_user_id(request)
        client = web_auth.client_key(request)
        device = context.access_store.device_for_token(
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
            ok = context.access_store.verify_otp(user_id, device.id, code)
        else:
            ok = context.access_store.consume_emergency_code(code, user_id, "enroll")
            owner = context.access_store.owner()
            if (
                not ok
                and not context.access_store.setup_finalized
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
                ok = context.access_store.verify_setup_code(
                    code
                ) or context.access_store.verify_transfer_code(code)

        if not ok:
            web_auth.backoff.record_failure(kind, subject, client)
            return _enroll_page(request, user_id, error="That code was not accepted")

        web_auth.backoff.record_success(kind, subject, client)
        context.access_store.trust_device(device.id, user_id)
        session_id = context.access_store.create_session(user_id, device.id)
        context.access_store.record_login(user_id)
        resp = HTMLResponse("", headers={"HX-Redirect": "/"})
        web_auth.set_cookie(
            resp, request, web_auth.SESSION_COOKIE, session_id, max_age=None
        )
        web_auth.clear_cookie(resp, web_auth.ENROLL_COOKIE)
        context.access_store.clear_enroll_token(request.cookies[web_auth.ENROLL_COOKIE])
        return resp

    @public.post("/logout", dependencies=[Depends(context.require_htmx)])
    async def logout(request: Request):
        session_id = request.cookies.get(web_auth.SESSION_COOKIE)
        if session_id and context.access_store is not None:
            context.access_store.end_session(session_id)
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
                "transfer": context.access_store.pending_transfer is not None,
            },
            status_code=status_code,
            headers=headers or {},
        )

    @public.get("/setup", response_class=HTMLResponse)
    async def setup_page(request: Request):
        if context.access_store is None:
            raise HTTPException(status_code=503, detail="Access store not loaded")
        context.ensure_setup_mode()
        # Nothing to do here once an owner exists and no transfer is live —
        # send the visitor on to the normal login/dashboard flow instead of
        # showing a wizard with no code that would accept.
        if (
            not context.access_store.setup_mode
            and context.access_store.pending_transfer is None
        ):
            return RedirectResponse("/", status_code=303)
        return _setup_page(request)

    @public.post(
        "/setup",
        response_class=HTMLResponse,
        dependencies=[Depends(context.require_htmx)],
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
        if context.access_store is None:
            raise HTTPException(status_code=503, detail="Access store not loaded")

        # Task 19 completes ownership transfer through this same handler:
        # while a transfer is pending, the code being checked is the
        # transfer code, not the setup code, and back-off is tracked
        # separately so a stranger guessing transfer codes can't also burn
        # down the setup code's budget or vice versa.
        in_transfer = context.access_store.pending_transfer is not None
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
            context.access_store.verify_transfer_code(setup_code)
            if in_transfer
            else context.access_store.verify_setup_code(setup_code)
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
                owner = context.access_store.complete_transfer(name, email or None, pin)
            else:
                owner = context.access_store.create_user(
                    name, email or None, Role.owner, pin
                )
        except OwnerExistsError:
            # Two racing step-1 submissions: the store enforces one owner
            # and refuses the second, so this must read as an ordinary
            # error, never a 500 (spec §3.1).
            return _setup_page(
                request, error="This machine already has an owner.", form=form
            )

        device, token = context.access_store.create_device(
            f"{name}'s device", shared=shared
        )
        context.access_store.trust_device(device.id, owner.id)
        context.access_store.record_login(owner.id)
        session_id = context.access_store.create_session(owner.id, device.id)

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
                "codes": context._pending_codes,
                "notice": notice,
                "error": error,
                "can_email": context._can_email_owner(owner),
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
        if context.access_store is None:
            raise HTTPException(status_code=503, detail="Access store not loaded")
        # Once setup is finalized and nothing is left to show, there is
        # nothing this page can do — the plaintexts are gone by design.
        if context.access_store.setup_finalized and not context._pending_codes:
            return RedirectResponse("/", status_code=303)
        if not context._pending_codes:
            # First view only: a reload must show this same pool, never a
            # fresh one, or codes the owner already wrote down would be
            # silently invalidated.
            context._pending_codes = context.access_store.generate_emergency_codes()
        owner = web_auth.current_principal(request).user
        return _setup_codes_page(request, owner)

    @public.post(
        "/setup/codes/email",
        response_class=HTMLResponse,
        dependencies=[
            Depends(web_auth.require(Permission.manage_ownership)),
            Depends(context.require_htmx),
        ],
    )
    async def email_setup_codes(request: Request):
        owner = web_auth.current_principal(request).user
        if not context._pending_codes or not context._can_email_owner(owner):
            return _setup_codes_page(
                request,
                owner,
                error="Email is not available; copy the codes above instead.",
            )
        gateway = context.config.communication.email_gateway
        body = (
            "These 20 emergency codes let you sign in to the vending "
            "machine dashboard on a browser it has never seen before, or "
            "authorise a new owner during an ownership transfer. Each code "
            "works exactly once. Keep them somewhere other than the "
            "machine itself — they exist for when the machine cannot be "
            "reached.\n\n" + "\n".join(context._pending_codes)
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
            Depends(context.require_htmx),
        ],
    )
    async def finish_setup(request: Request):
        if context.access_store is None:
            raise HTTPException(status_code=503, detail="Access store not loaded")
        context.access_store.finalize_setup()
        context._pending_codes = []
        if context.display_controller is not None:
            context.display_controller.clear_setup_code()
        return HTMLResponse("", headers={"HX-Redirect": "/"})

    # --- Task 19: reviewing retained users after a completed transfer ---

    def _review_candidates() -> list:
        """Every user except the (new) owner, in a stable order.

        Recomputed fresh on every request — never cached — since a Keep is
        a no-op on the store and a Remove deletes exactly one entry, so
        re-deriving this list from live state is always cheap and correct.
        """
        owner = context.access_store.owner()
        owner_id = owner.id if owner else None
        return sorted(
            (u for u in context.access_store.users.values() if u.id != owner_id),
            key=lambda u: (u.name, u.id),
        )

    def _setup_review_response(request: Request, user):
        device_count = sum(
            1
            for d in context.access_store.devices.values()
            if user.id in d.trusted_user_ids
        )
        return templates.TemplateResponse(
            "setup_review_user.html",
            {
                "request": request,
                "user": _user_row(user),
                "device_count": device_count,
            },
        )

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
            candidate = context.access_store.get_user(ids[idx])
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
            Depends(context.require_htmx),
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
            Depends(context.require_htmx),
        ],
    )
    async def setup_review_remove(request: Request, user_id: str):
        order = _review_candidates()
        if user_id not in {u.id for u in order}:
            raise HTTPException(status_code=404, detail="No such user")
        # Sessions end before the user record itself is deleted.
        context.access_store.end_sessions_for_user(user_id)
        context.access_store.delete_user(user_id)
        return _review_response_after(request, order, user_id)

    return public
