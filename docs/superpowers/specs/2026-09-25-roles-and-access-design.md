# Roles and Access — Design

**Date:** 2026-09-25
**Status:** Approved
**Series:** Dashboard v2, part 1 of 4 (roles → v2 shell → sales reports → system tests)

## Context

The dashboard is moving to a touch-first design served to a shared 7-inch
tablet mounted inside the machine and to each user's personal device over the
reverse proxy. One HTTP Basic admin credential cannot express who is standing
at the tablet or what they may touch. This spec replaces it with named users,
four roles, PIN login, per-device trust with a one-time second factor, and
owner-controlled recovery that works with no connectivity at the machine.

Decisions made in brainstorming:

- Named users, each with exactly one role: `owner`, `secretary`, `tech`, `loader`.
- Exactly one owner per machine. Secretaries are the owner's delegates.
- Credential is a PIN of 4–8 digits chosen by the user.
- Second factor is per device, once: an emailed one-time password, or an
  owner-issued eight-digit emergency code from a pre-generated pool that works
  offline.
- First owner is created by a setup wizard on first visit, unlocked by a
  setup code that only exists at the machine (startup log and the physical
  display). Ownership transfer is a pending transaction the owner starts
  with a PIN plus an unused emergency code and the incoming owner completes
  in the wizard. An owner who cannot produce a code has no software
  recovery; the controller is replaced or factory-restored.
- Everyone but the owner survives a transfer; the incoming owner reviews them
  one by one and can email a machine report.
- Shared tablet locks after idle; PIN alone resumes on a trusted device.
- Every secret entry backs off exponentially per subject; no hard attempt caps
  that let a stranger lock a legitimate user out.

## Approach

In-house session auth in a new `services/access.py`, persisted in
`data/access.json`. No new runtime dependencies: PIN hashing with
`hashlib.scrypt`, cookie values are random tokens looked up server-side (no
signing needed), OTP email through the SMTP settings already in
`config.communication.email_gateway`. HTTP Basic auth is removed.

Rejected: fastapi-users / authlib (OAuth-shaped, database-backed, fights the
PIN and emergency-code model); roles layered on HTTP Basic (no device trust,
no idle lock, no wizard).

## 1. Data model

`services/access.py` owns an `AccessStore` loaded from `data/access.json`
(`DATA_DIR` from `services/paths.py`). Writes are atomic tmp + rename, the same
pattern as `services/config_store.py`, with the temp file opened via
`os.open(..., 0o600)` so neither the temp nor the final file is ever readable
by other users regardless of umask; the store also `chmod`s an existing file
to `0600` at load and logs a warning if it had to. On Windows development
hosts the chmod is a no-op. The file contains secrets and PII and is never
merged into `config.json`.

Persisted:

| Collection | Fields |
|---|---|
| `users` | `id` (uuid4), `name`, `email` (optional, `EmailStr`), `role`, `pin_hash`, `pin_salt`, `disabled: bool`, `created_at`, `last_login_at` |
| `devices` | `id` (uuid4), `token_hash` (sha256 of the cookie value), `label`, `shared: bool`, `trusted_user_ids: list[str]`, `created_at`, `last_seen_at` |
| `emergency_codes` | `code_hash` (scrypt), `used_at`, `used_by_user_id`, `used_for` (`enroll` or `transfer`) |
| `setup` | `setup_code_hash`, `finalized: bool` — the bootstrap secret (§3.1); cleared once setup is finalized |
| `pending_transfer` | `transfer_code_hash`, `started_at`, `expires_at`, `started_by_user_id`, or null (§3.3) |

Invariants enforced by the store: at most one user with role `owner`; a
device's `trusted_user_ids` only references existing users; deleting a user
removes it from every device.

In memory only (lost on restart, which just means re-login):

| Structure | Fields |
|---|---|
| `sessions` | `id` (random 256-bit token), `user_id`, `device_id`, `created_at`, `last_active_at` |
| `pending_otps` | keyed by `(user_id, device_id)`: 6-digit `code`, `expires_at` (10 min) |
| `backoff` | keyed by `(kind, subject, client)`: `failures: int`, `next_allowed_at`; plus a per-user budget keyed by `(kind, subject)` for untrusted clients (§2.4) |

Timestamps are UTC ISO-8601 in the file, `time.monotonic()` in memory. The
store takes an injectable `clock` for tests, like `LoginLimiter` does today.

`WebConfig` loses `admin_username` and `admin_password`. `services/auth_policy.py`
loses the password functions and gains `pin_problem(pin) -> str | None`:
must be 4–8 characters, all digits, not all the same digit, not an ascending
or descending run (`1234`, `87654321`). `is_loopback` stays. The startup check
in `main.py` that warned about the default admin password is replaced by a
warning when no owner exists yet ("dashboard is in setup mode").

## 2. Login and device enrollment

### 2.1 Cookies

Both `HttpOnly`, `SameSite=Lax`, `Path=/`. `Secure` is set when
`request.url.scheme == "https"` or the request came from a trusted proxy
with `X-Forwarded-Proto: https`.

| Cookie | Value | Lifetime |
|---|---|---|
| `vmc_device` | random 256-bit token, hashed in the store | 365 days, refreshed on every successful login |
| `vmc_session` | session id | browser session; validity decided server-side |

Session validity: idle timeout 5 minutes on a `shared` device, 8 hours on a
personal one; absolute maximum 24 hours. An expired session redirects to
`/login`. On a shared device the login page shows the user picker, so a locked
tablet is one tap and a PIN away from resuming.

### 2.2 `GET /login` and `POST /login`

Touch-sized page: a picker listing enabled users by display name (no typed
usernames on a tablet) and a numeric keypad for the PIN. The same page serves
remote users. The picker exposes names only, which is accepted: anyone at the
machine can already read the contact sheet.

`POST /login` fields: `user_id`, `pin`.

1. Back-off check for `(pin, user_id, client)` (§2.4). If not yet allowed,
   respond 429 with the seconds remaining; the page shows a countdown.
2. Verify the PIN. On failure record a back-off failure and re-render the
   keypad with a generic "wrong PIN" message. The response is identical for
   an unknown or disabled user id.
3. On success clear the back-off entry. If a `vmc_device` cookie is present,
   valid, and lists this user in `trusted_user_ids`: create a session, set
   cookies, update `last_login_at` and `last_seen_at`, redirect to `/`.
4. Otherwise render the enrollment page (§2.3) and set a `vmc_enroll` cookie
   (random token, 10 minutes, bound server-side to the user id and client)
   that proves the PIN was just verified, so the second step never resends
   the PIN.

### 2.3 Enrollment page and `POST /login/enroll`

Two ways to prove the second factor. The page shows a numeric keypad and a
single code field that accepts 6 digits (OTP) or 8 digits (emergency code);
the length decides the path.

- **Email me a code** button: shown only when the user has an email and
  `config.communication.email_gateway.is_configured`. Generates a 6-digit
  OTP, stores it under `(user_id, device_id)` with a 10-minute expiry, and
  sends it through a new `services/mailer.py` (`send_email(to, subject, body)`
  running `smtplib` in a thread, extracted from the SMTP code in
  `services/notifier.py`, which then calls it). A resend replaces the pending
  OTP and is itself subject to back-off keyed `(otp_send, user_id, client)`,
  where every send counts as a "failure" so repeated sends slow down.
- **Emergency code**: any unused code from the pool. On success it is marked
  used with `used_for = "enroll"` and the user id.

If no `vmc_device` cookie exists when enrollment starts, a device record is
created and the cookie issued at that moment, so the pending OTP always has a
device id to bind to. On a correct code: add the user to `trusted_user_ids`,
create the session, clear `vmc_enroll`, redirect to `/`. A device record that
never completes enrollment is pruned after 24 hours with no trusted users.

### 2.4 Exponential back-off

Every path that checks a secret uses the same rule. Failures are counted per
`(kind, subject, client)` where `kind` is `pin`, `otp`, `emergency`,
`transfer`, `setup`, or `otp_send`; `subject` is the user id (or the string
`pool` for emergency codes and `setup` for the setup code, which are not per
user); `client` is the **stored device id** when the `vmc_device` cookie
resolves to a device record in the store, otherwise the client IP as
resolved by the existing trusted-proxy logic. A cookie that does not
resolve is ignored for keying, so an attacker minting random cookie values
gets one counter per IP, not one per cookie.

After `n` consecutive failures the next attempt is allowed no sooner than
`min(2 ** (n - 1), 3600)` seconds later: 1 s, 2 s, 4 s, … capped at one hour.
A success resets the counter for that key. Entries idle for 24 hours are
pruned.

**Per-user budget for untrusted clients.** A distributed attacker with many
IPs would otherwise get many independent counters against one user's PIN.
So failures from clients on which the subject user is *not* trusted also
count toward a second key `(kind, subject)`; after 20 such failures in a
rolling hour the same exponential delay applies to every untrusted client
for that user, capped at one hour. Attempts from a device where the user is
already trusted are keyed only per device and never consult the per-user
budget, so a stranger cannot slow the legitimate user on their own tablet
or phone, only on devices the user has never enrolled. Both counters are
in memory; a restart clears them.

There are no hard caps, no per-user disabling, and no IP lockout.
`LoginLimiter` is replaced by a `Backoff` class in `services/access.py`
that keeps `client_ip` and the trusted-proxy handling; `main.py` calls
`access.backoff.set_trusted_proxies(...)` where it called
`routes.login_limiter.set_trusted_proxies(...)`.

Brute-force arithmetic: a 6-digit OTP in a 10-minute window allows about 12
guesses per client before the delay exceeds the window; an 8-digit emergency
code allows about 17 guesses per client per day; a 4-digit PIN from
untrusted clients is bounded by the per-user budget to roughly 20 guesses
plus 17 per day thereafter across all attacker IPs combined. All are
negligible against the code spaces.

### 2.5 Logout

`POST /logout` (HTMX-guarded like every POST) deletes the session and clears
`vmc_session`; the device cookie is kept. A shared tablet returns to the
picker.

## 3. Setup wizard and ownership transfer

### 3.1 Setup mode

While the store has no user with role `owner`, every route except `/setup`
and `/static/*` redirects to `/setup`. The wizard is reachable from the
network, so it is locked with a **setup code** that only exists at the
machine:

- On entering setup mode (first boot, or a completed transfer, §3.3) the
  store generates an 8-digit setup code, stores its hash, writes the
  plaintext to the startup log at warning level, and publishes it to the
  customer display through `services/display_controller.py` (maintenance
  mode, "Setup code: 1234 5678") for as long as setup mode lasts. Someone
  standing at the machine, or reading `docker logs`, has it; a remote
  stranger does not.
- `POST /setup` requires the setup code and is subject to back-off kind
  `setup`.

Setup is two durable steps so a lost response cannot strand the owner:

1. `POST /setup` fields: `setup_code`, `name`, `email`, `pin`,
   `pin_confirm`, `shared_device: bool` ("This browser is the machine's own
   tablet"). Creates the owner atomically (the store rejects a second owner,
   so two racing submissions cannot both succeed), creates a device record
   with the `shared` flag, trusts the owner on it, creates a session, sets
   cookies, and redirects to `/setup/codes`. The setup code stays valid,
   and until step 2 completes it is also accepted as an enrollment code for
   the owner (§2.3): if this response is lost, the owner logs in with their
   PIN on any browser and enrolls with the setup code.
2. `GET /setup/codes` (owner session required) generates the 20 emergency
   codes on first view and renders §3.2. **Done** finalizes setup: the setup
   code is invalidated, the display returns to normal, and `setup.finalized`
   is set. Reloading before Done shows the same codes again (they are held
   in memory until finalized, then only their hashes remain).

### 3.2 Emergency codes page

Shows the 20 eight-digit codes in plain text exactly once, with an **Email
these to me** button (via `services/mailer.py`, disabled when the gateway is
not configured) and a **Done** button. Codes are generated with
`secrets.randbelow(10**8)` zero-padded, deduplicated, and stored as scrypt
hashes. The owner can regenerate the pool at any time from the Users area,
which replaces all codes, used or not; the Users area also shows how many
remain unused.

### 3.3 Transfer of ownership

Transfer is a pending transaction; the current owner keeps full control
until the incoming owner has completed the wizard, so an abandoned handover
never leaves the machine ownerless or claimable.

1. **Start** (owner only): `POST /users/transfer` fields: `pin`,
   `emergency_code`, checked with back-off kinds `pin` and `transfer`. On
   success the store records `pending_transfer` with a fresh 8-digit
   transfer code (hash stored, plaintext shown once to the outgoing owner to
   hand to the incoming one) and a 7-day expiry, and consumes the emergency
   code with `used_for = "transfer"`. Nothing else changes. The Users level
   shows the pending transfer with **Cancel** (owner only, PIN required).
2. **Complete** (incoming owner, any browser): while a transfer is pending,
   `/setup` is reachable alongside the normal login and requires the
   transfer code instead of a setup code. The incoming owner fills the
   owner form (§3.1 step 1 fields). The store then atomically: creates the
   new owner, deletes the old owner user and removes them from every device,
   deletes every emergency code, ends every session, and clears
   `pending_transfer`. The new owner's session and device trust are created
   in the same write.
3. **Review**: the wizard walks the retained users one at a time: name,
   role, email, last login, number of trusted devices, with **Keep** and
   **Remove**. Each decision is applied immediately; leaving mid-review just
   leaves the remaining users kept.
4. **Codes**: `/setup/codes` as in §3.1 step 2, plus **Email the machine
   report to me**. The transfer code remains valid as an enrollment code for
   the new owner until Done, covering a lost response exactly as the setup
   code does.

An expired or cancelled transfer leaves everything as it was, except the
consumed emergency code.

### 3.4 Machine report

Plain-text email: machine id and name from `config.physical`, owner name and
email, every user with role, email, disabled flag, last login and device
count, every device with label, shared flag, trusted user names and last
seen, and the number of unused emergency codes. Available on demand to the
owner from the Users area, not only during transfer. Sales figures are out of
scope until the sales-reports spec.

## 4. Permissions

`Permission` is a `str` enum in `services/access.py`; `ROLE_PERMISSIONS` is a
static `dict[Role, frozenset[Permission]]`.

| Permission | owner | secretary | tech | loader |
|---|---|---|---|---|
| `view_status` — home, health tab, `/screen`, KPI, activity | ✓ | ✓ | ✓ | ✓ |
| `clear_faults` | ✓ | – | ✓ | – |
| `view_logs` | ✓ | – | ✓ | – |
| `machine_controls` — restart, reset, shutdown | ✓ | – | ✓ | – |
| `run_tests` — reserved for the system-tests spec | ✓ | – | ✓ | – |
| `edit_catalog` — create, copy, delete, price, name, kind | ✓ | ✓ | – | – |
| `edit_placement` — slot/button, inventory count, tracking | ✓ | ✓ | ✓ | ✓ |
| `view_reports` — reserved for the sales-reports spec | ✓ | ✓ | – | – |
| `edit_contacts` — people and machine info | ✓ | ✓ | – | – |
| `edit_secrets` — payment, comms, MQTT settings | ✓ | – | – | – |
| `manage_users` — create, edit, disable, reset PIN, remove devices | ✓ | ✓ (never the owner) | – | – |
| `manage_ownership` — transfer, regenerate emergency codes, machine report | ✓ | – | – | – |

Enforcement:

- `require(Permission.x)` is a FastAPI dependency that resolves the session
  (redirect to `/login` for page requests; for HTMX requests a 401 carrying
  the `HX-Redirect: /login` header so the partial swap turns into a full
  navigation) and returns 403 when the role lacks the permission.
- The `secretary` restriction on the owner is enforced in the user-management
  routes: any write whose target user is the owner requires `manage_ownership`.
- Every template gets `perms: frozenset[Permission]` and `current_user` in its
  context through a shared `_ctx(request, **extra)` helper, so buttons a user
  cannot press are not rendered. Server-side checks remain the authority.
- The product edit form splits into `/inventory/edit/{sku}/catalog` and
  `/inventory/edit/{sku}/placement`, each with its own POST and permission.
  The current single form is removed. The add and copy forms need
  `edit_catalog`.
- The existing `require_htmx` CSRF guard stays on every POST. Cookie auth
  makes it load-bearing now, not just defensive.

### 4.1 User management routes

`GET /users` list; `GET /users/new` and `POST /users/new` (name, email, role,
initial PIN; a secretary may not choose `owner`); `POST /users/{id}/disable`,
`/enable`, `/reset-pin` (sets a new PIN typed by the admin and drops the user
from every device so the next login re-enrolls); `POST /users/{id}/delete`;
`GET /devices` list with `POST /devices/{id}/forget` and
`POST /devices/{id}/shared` toggle. Owner-only: `POST /users/transfer`,
`POST /users/transfer/cancel`, `POST /users/codes/regenerate`,
`POST /users/report`.

These render as partials in the current dashboard's content panel under a
new **Users** tab. The v2 shell spec will re-home them as tiles.

## 5. Files

| File | Change |
|---|---|
| `services/access.py` | New: `AccessStore`, `Role`, `Permission`, `ROLE_PERMISSIONS`, `Backoff`, PIN hashing, code generation, session and OTP handling |
| `services/mailer.py` | New: `send_email`; `notifier.py` delegates its SMTP send to it |
| `services/auth_policy.py` | Replace password functions with `pin_problem`; keep `is_loopback` |
| `web_interface/auth.py` | `LoginLimiter` removed; `require(...)`, session resolution, cookie helpers live here |
| `web_interface/routes.py` | `require_auth` → `require(Permission...)` per route; new login, enroll, logout, setup, users, devices routes; catalog/placement split |
| `web_interface/templates/login.html`, `enroll.html`, `setup.html`, `setup_codes.html`, `setup_review_user.html` | New full pages with viewport meta |
| `services/display_controller.py` | Show and clear the setup code in maintenance mode |
| `web_interface/templates/partials/keypad.html`, `users_list.html`, `user_form.html`, `devices_list.html`, `inventory_catalog_form.html`, `inventory_placement_form.html` | New partials |
| `web_interface/templates/partials/inventory_edit_form.html` | Removed |
| `config/config_model.py` | Drop `admin_username` / `admin_password` from `WebConfig` |
| `main.py` | Wire `AccessStore` into routes; replace the admin-password startup warning with a setup-mode warning; trusted proxies go to `Backoff` |
| `.env.example`, `docker-compose*.yml`, `README.md`, `CLAUDE.md` | Remove admin credential references; document `data/access.json` and setup mode |
| `tests/test_access.py` | New unit tests |
| `tests/test_web_routes.py` | Rewritten auth fixtures |

## 6. Error handling

- `data/access.json` missing: setup mode. Unreadable or invalid JSON: log at
  error level and serve a static "access file is corrupt" page on every
  route instead of the setup wizard (a corrupt access file must not silently
  become an open setup wizard). The MQTT client and VMC still run; this is a
  dashboard-only failure.
- Setup code or transfer code lost before Done: the owner re-enrolls with
  it (§3.1); if both the code and the log are gone, the machine is in the
  no-recovery case by design.
- SMTP failure during OTP send: log, show "Email could not be sent, use an
  emergency code" on the page. Never block on the network: the send runs in a
  thread with a 15-second timeout.
- A session whose device or user no longer exists is treated as expired.
- Setting a PIN that fails `pin_problem` returns the form with the reason.

## 7. Testing

`tests/test_access.py`, all with an injected clock and a temp `DATA_DIR`:

- PIN policy accepts and rejects the documented shapes.
- scrypt round-trip; two users with the same PIN have different hashes.
- Single-owner invariant raised on a second owner.
- Back-off delays follow 1, 2, 4, … capped at 3600, reset on success, and are
  independent per client and per kind; an unresolvable device cookie keys on
  IP; the per-user budget slows untrusted clients after 20 failures in an
  hour and never affects a trusted device.
- `access.json` and its temp file are created `0600` (skipped on Windows).
- Session idle timeout differs for shared and personal devices; absolute cap.
- Emergency code consumption is single-use; regenerate wipes the pool.
- Transfer start leaves the owner in control; complete swaps owner, ends
  sessions, keeps other users; cancel and expiry restore the pending state
  to null; a second owner creation during a race is rejected.
- Setup code is accepted for enrollment until Done, then rejected.
- Deleting a user removes them from every device.

`tests/test_web_routes.py` with a `login_as(role, shared=False)` helper that
seeds a user and a trusted device and returns a client with both cookies:

- Setup mode redirects every route to `/setup`; the wizard rejects a wrong
  setup code with back-off, creates the owner with the right one, and shows
  20 codes on `/setup/codes`; a lost step-1 response is recovered by logging
  in and enrolling with the setup code; the display controller was asked to
  show the code and to clear it after Done.
- Login with correct PIN on an untrusted device shows enrollment; OTP path
  with a stubbed mailer; emergency-code path; wrong code backs off with a 429.
- Locked shared session resumes with PIN only.
- A permission matrix test: for every `(route, role)` pair the response is
  200 or 403 exactly as `ROLE_PERMISSIONS` predicts.
- Secretary cannot edit, disable, or delete the owner.
- Catalog vs placement split: loader can change slot and count, gets 403 on
  price.
- Transfer flow end to end: start shows a transfer code, the old owner still
  works, `/setup` with the transfer code completes the swap, review-users
  step, cancel path.
- HTMX guard still rejects POSTs without the header.

## 8. Out of scope

- The v2 tile shell, sales reports, and system tests (parts 2–4).
- SMS or Snapchat delivery of OTPs; only email.
- Password managers, WebAuthn, or any credential other than a PIN.
- Auditing who changed what (would need an event-recorder change; the
  `current_user` is available to add it later).
- Any change to MQTT, ESP32 contracts, or `/screen` content beyond auth.
