"""Login, device enrollment and logout routes."""

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from ice_edge.access.access import (
    OTP_DIGITS,
)
from ice_edge.notify.mailer import send_email
from ice_edge.web import auth as web_auth
from ice_edge.web import context
from ice_ui.templating import templates

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


router = APIRouter()


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
        request,
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


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    if context.access_store is None:
        raise HTTPException(status_code=503, detail="Access store not loaded")
    if context.access_store.setup_mode:
        return RedirectResponse("/setup", status_code=303)
    return templates.TemplateResponse(
        request,
        "login.html",
        context.template_context(
            request,
            users=context.access_store.enabled_users(),
            selected_user_id=None,
            error=None,
            wait_seconds=None,
        ),
    )


@router.post(
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
        request,
        "enroll.html",
        context.template_context(
            request,
            user=user,
            error=error,
            notice=notice,
            wait_seconds=wait_seconds,
            can_email=bool(user and user.email and gateway and gateway.is_configured),
        ),
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
    # Task 14 fix round 2, finding A: this is the response to the
    # keypad's own `#login-form` POST (partials/keypad.html:
    # hx-target="#login-form", hx-swap="outerHTML", no hx-select — that
    # must stay exactly as-is, because the *other* two shapes
    # login_submit can answer (wrong PIN, back-off wait) are bare
    # partials/keypad.html re-renders that rely on those exact static
    # attributes and must keep swapping into #login-form unmodified).
    # But enroll.html now extends base.html, so *this* response also
    # carries the OOB `<header id="bar">` plus a `<main>`. Left alone,
    # htmx's OOB pass patches the live bar and drops the header node
    # from the response regardless of target; the outerHTML swap then
    # replaces #login-form (nested inside login.html's own live
    # <main>) with the response's whole <main> — nesting a <main>
    # inside a <main>, and leaving a later swap from inside it
    # targeting the wrong (outer) <main> too.
    #
    # HX-Retarget/HX-Reselect are htmx response headers: they override
    # the triggering element's target/select for this one response
    # only, leaving #login-form's own static attributes untouched for
    # its other two response shapes. Retargeting at "main" (the
    # document's live <main>, ambient body default) and reselecting
    # "main" from the parsed response reproduces round 1's
    # target="main" hx-select="main" hx-swap="outerHTML" pattern
    # (hx-swap stays "outerHTML" from the form's own static attribute,
    # no HX-Reswap needed) — just applied per-response via headers
    # instead of per-element via static attributes.
    resp.headers["HX-Retarget"] = "main"
    resp.headers["HX-Reselect"] = "main"
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


@router.post(
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
    if user is None or not user.email or not gateway.is_configured or device is None:
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


@router.post(
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


@router.post("/logout", dependencies=[Depends(context.require_htmx)])
async def logout(request: Request):
    session_id = request.cookies.get(web_auth.SESSION_COOKIE)
    if session_id and context.access_store is not None:
        context.access_store.end_session(session_id)
    resp = HTMLResponse("", headers={"HX-Redirect": "/login"})
    web_auth.clear_cookie(resp, web_auth.SESSION_COOKIE)
    return resp
