# Roles and Access Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the dashboard's single HTTP Basic admin credential with named users, four roles, PIN login, per-device trust with a one-time second factor, a setup wizard, and owner-controlled transfer — all working with no internet at the machine.

**Architecture:** A new `services/access.py` owns every piece of access state: a `Backoff` rate limiter (replacing `web_interface/auth.py`'s `LoginLimiter`), PIN/code hashing with `hashlib.scrypt`, the `Permission` enum and `ROLE_PERMISSIONS` table, and an `AccessStore` persisted atomically to `data/access.json` (0600). `web_interface/auth.py` shrinks to cookie helpers, session resolution and a `require(Permission)` FastAPI dependency. `web_interface/routes.py` gains public login/enroll/setup routes on an ungated router and declares a permission on every other route.

**Tech Stack:** Python 3.12, FastAPI, Starlette `TestClient`, Jinja2, HTMX 1.9, Pydantic v2, loguru, pytest, `uv`, `ruff`. **No new runtime dependencies** — `hashlib.scrypt`, `secrets`, `smtplib` are stdlib.

**Spec:** `docs/superpowers/specs/2026-09-25-roles-and-access-design.md`
**Program plan:** `docs/superpowers/plans/2026-09-25-dashboard-v2-program.md` (part 1 of 4)

## Global Constraints

Every task's requirements implicitly include this section. Values are copied verbatim from the spec.

- **No new runtime dependency.** PIN hashing is `hashlib.scrypt`; cookie values are random tokens looked up server-side (no signing); OTP email goes through `config.communication.email_gateway`. Adding a dependency is a program-plan §3.5 stop condition.
- **Roles:** exactly `owner`, `secretary`, `tech`, `loader`. Exactly one `owner` per machine.
- **PIN:** 4–8 characters, all digits, not all the same digit, not an ascending or descending run (`1234`, `87654321`).
- **Codes:** emergency, setup and transfer codes are **8 digits**; OTPs are **6 digits**. Emergency pool size is **20**. OTP expiry **10 minutes**. Enrollment (`vmc_enroll`) token expiry **10 minutes**. Transfer expiry **7 days**.
- **Back-off:** after `n` consecutive failures the next attempt is allowed no sooner than `min(2 ** (n - 1), 3600)` seconds after the last failure. A success resets the counter. Entries idle 24 hours are pruned. No hard caps, no per-user disabling, no IP lockout.
- **Back-off keys:** `kind` is one of `pin`, `otp`, `emergency`, `transfer`, `setup`, `otp_send`. `subject` is the user id, or the literal string `pool` for emergency codes and `setup` for the setup code. `client` is the **stored device id** when the `vmc_device` cookie resolves to a device record, otherwise the client IP from the trusted-proxy logic.
- **Per-user budget:** failures from clients on which the subject user is *not* trusted also count toward key `(kind, subject)`; after **20** such failures in a rolling **hour** the same exponential delay applies to every untrusted client for that user, capped at one hour. A client where the user *is* trusted never consults or feeds this budget.
- **Sessions:** idle timeout **300 s** on a `shared` device, **28800 s** (8 h) on a personal one; absolute maximum **86400 s** (24 h).
- **Cookies:** `vmc_device` (365 days), `vmc_session` (browser session), `vmc_enroll` (10 minutes). All `HttpOnly`, `SameSite=Lax`, `Path=/`. `Secure` when `request.url.scheme == "https"` or a trusted proxy sent `X-Forwarded-Proto: https`.
- **File:** `data/access.json`, atomic tmp + rename, temp opened via `os.open(..., 0o600)`, existing file `chmod`ed to `0600` at load with a warning when it had to be. chmod is a no-op on Windows; 0600 tests are `@pytest.mark.skipif(os.name != "posix", ...)`.
- **Timestamps:** UTC ISO-8601 in the file; `time.monotonic()` in memory. `AccessStore` and `Backoff` take injectable clocks for tests.
- **Every POST keeps the existing `require_htmx` CSRF guard.**
- **Never block on the network:** SMTP runs in a thread with a 15-second timeout.
- **Commands:** `uv run pytest`, `uv sync`, `ruff check --fix .` then `ruff format .`. Never chain shell commands with `&&`. Never run Docker.
- **Out of scope (do not build):** the v2 tile shell, sales reports, system tests, SMS/Snapchat OTP delivery, WebAuthn, audit logging, any MQTT/ESP32/`/screen` content change beyond auth.

## File Structure

| File | Responsibility |
|---|---|
| `services/access.py` | New. `Role`, `Permission`, `ROLE_PERMISSIONS`, PIN/secret hashing, code generation, `Backoff`, `AccessStore` (users, devices, sessions, OTPs, emergency codes, setup, transfer), `AccessError` / `OwnerExistsError` |
| `services/mailer.py` | New. `send_email(...)` — blocking SMTP in a thread with a 15 s timeout |
| `services/auth_policy.py` | `pin_problem`; `is_loopback` kept; password functions removed (Task 20) |
| `services/notifier.py` | Delegates its SMTP send to `services/mailer.py` |
| `services/display_controller.py` | Shows and clears the setup code in maintenance mode |
| `web_interface/auth.py` | `LoginLimiter` removed. Cookie helpers, session resolution, `require(Permission)`, `template_context` |
| `web_interface/routes.py` | Public router (login/enroll/logout/setup) + gated router with a `Permission` per route; users, devices, transfer routes; catalog/placement split |
| `web_interface/templates/login.html`, `enroll.html`, `setup.html`, `setup_codes.html`, `setup_review_user.html` | New full pages with viewport meta |
| `web_interface/templates/partials/keypad.html`, `users_list.html`, `user_form.html`, `devices_list.html`, `inventory_catalog_form.html`, `inventory_placement_form.html` | New partials |
| `web_interface/templates/partials/inventory_edit_form.html` | Deleted (Task 12) |
| `web_interface/templates/dashboard.html` | Users tab added; tabs filtered by permission |
| `config/config_model.py` | `WebConfig` drops `admin_username` / `admin_password` |
| `main.py` | Wires `AccessStore` into routes, setup-mode warning, trusted proxies to `Backoff` |
| `tests/test_access.py` | New. Tasks 2–6 |
| `tests/test_mailer.py` | New. Task 7 |
| `tests/test_web_auth.py` | New. Task 8 |
| `tests/test_web_routes.py` | Rewritten fixtures (Task 11), route tests throughout |
| `tests/test_login_limiter.py` | Deleted (Task 20) |

**Deviation from spec §7 recorded up front:** the spec names two test files. This plan adds `tests/test_mailer.py` and `tests/test_web_auth.py` so a single task never needs more than one new test file (program plan §3.1). No behavior differs.

**Pre-existing condition, not this part's job:** `/config/payments` and `/config/comms` render `partials/payments.html` and `partials/comms.html`, which do not exist; their tests carry `@pytest.mark.skip`. Leave the skips in place. The permission matrix test (Task 11) must exclude those two routes and say why in a comment.

---

### Task 1: PIN policy in `auth_policy`

**Spec:** §1 (final paragraph — `pin_problem`).

**Files:**
- Modify: `services/auth_policy.py`
- Test: `tests/test_auth_policy.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `services.auth_policy.pin_problem(pin: str) -> str | None` — the reason a PIN is unacceptable, or `None`. Module constants `MIN_PIN_LENGTH = 4`, `MAX_PIN_LENGTH = 8`. `is_loopback` unchanged. `password_problem` / `generate_admin_password` stay until Task 20.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_auth_policy.py`:

```python
from services.auth_policy import pin_problem


@pytest.mark.parametrize("pin", ["1379", "9042", "13795", "90426183"])
def test_good_pins_accepted(pin):
    assert pin_problem(pin) is None


@pytest.mark.parametrize(
    "pin,fragment",
    [
        ("", "4"),
        ("123", "4"),
        ("123456789", "8"),
        ("12a4", "digits"),
        ("1111", "same digit"),
        ("1234", "run"),
        ("87654321", "run"),
        ("3456", "run"),
        ("4321", "run"),
    ],
)
def test_bad_pins_rejected(pin, fragment):
    problem = pin_problem(pin)
    assert problem is not None
    assert fragment in problem
```

`tests/test_auth_policy.py` already imports `pytest`; do not import it twice.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_auth_policy.py -v`
Expected: FAIL — `ImportError: cannot import name 'pin_problem'`.

- [ ] **Step 3: Implement**

Append to `services/auth_policy.py`:

```python
MIN_PIN_LENGTH = 4
MAX_PIN_LENGTH = 8


def pin_problem(pin: str) -> str | None:
    """Why this PIN is unacceptable, or None. Never echoes the value."""
    if len(pin) < MIN_PIN_LENGTH:
        return f"PIN must be at least {MIN_PIN_LENGTH} digits"
    if len(pin) > MAX_PIN_LENGTH:
        return f"PIN must be at most {MAX_PIN_LENGTH} digits"
    if not (pin.isdigit() and pin.isascii()):
        return "PIN must be digits only"
    if len(set(pin)) == 1:
        return "PIN must not be the same digit repeated"
    digits = [int(c) for c in pin]
    ascending = all(b - a == 1 for a, b in zip(digits, digits[1:]))
    descending = all(a - b == 1 for a, b in zip(digits, digits[1:]))
    if ascending or descending:
        return "PIN must not be a run of consecutive digits"
    return None
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_auth_policy.py -v`
Expected: PASS — 13 new cases alongside the existing password tests.

- [ ] **Step 5: Lint and commit**

```bash
ruff check --fix .
ruff format .
git add services/auth_policy.py tests/test_auth_policy.py
git commit -m "feat(access): PIN policy in auth_policy"
```

---

### Task 2: `Backoff` with clock injection

**Spec:** §2.4.

**Files:**
- Create: `services/access.py`
- Test (create): `tests/test_access.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `services.access.Backoff(clock: Callable[[], float] = time.monotonic, trusted_proxies: list[str] | None = None)`
  - `Backoff.set_trusted_proxies(cidrs: list[str]) -> None`
  - `Backoff.client_ip(request) -> str` — the trusted-proxy rule taken from the removed `LoginLimiter`
  - `Backoff.is_https(request) -> bool` — for the `Secure` cookie flag
  - `Backoff.check(kind: str, subject: str, client: str, *, trusted: bool = False) -> float | None` — seconds still to wait, or `None` when allowed now
  - `Backoff.record_failure(kind, subject, client, *, trusted: bool = False) -> None`
  - `Backoff.record_success(kind, subject, client, *, trusted: bool = False) -> None`
  - Constants `BACKOFF_CAP_SECONDS = 3600.0`, `BUDGET_THRESHOLD = 20`, `BUDGET_WINDOW_SECONDS = 3600.0`, `BACKOFF_PRUNE_SECONDS = 86400.0`

`trusted=True` means "the subject user is already trusted on this client": keyed per client only, never touching the per-user budget.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_access.py`:

```python
"""Tests for services/access.py."""

import pytest

from services.access import BACKOFF_CAP_SECONDS, Backoff


class FakeClock:
    """Monotonic clock under test control."""

    def __init__(self, start: float = 1000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeRequest:
    """Minimal stand-in for a starlette Request for client_ip()/is_https()."""

    class _Client:
        def __init__(self, host):
            self.host = host

    class _Url:
        def __init__(self, scheme):
            self.scheme = scheme

    def __init__(self, peer="10.0.0.5", headers=None, scheme="http"):
        self.client = self._Client(peer)
        self.headers = headers or {}
        self.url = self._Url(scheme)


class TestBackoffDelays:
    def test_first_attempt_is_allowed(self):
        b = Backoff(clock=FakeClock())
        assert b.check("pin", "u1", "dev1") is None

    def test_delays_double_from_one_second(self):
        clock = FakeClock()
        b = Backoff(clock=clock)
        for want in (1.0, 2.0, 4.0, 8.0, 16.0):
            b.record_failure("pin", "u1", "dev1", trusted=True)
            assert b.check("pin", "u1", "dev1", trusted=True) == pytest.approx(want)
            clock.advance(want)
            assert b.check("pin", "u1", "dev1", trusted=True) is None

    def test_delay_caps_at_one_hour(self):
        b = Backoff(clock=FakeClock())
        for _ in range(30):
            b.record_failure("pin", "u1", "dev1", trusted=True)
        assert b.check("pin", "u1", "dev1", trusted=True) == pytest.approx(
            BACKOFF_CAP_SECONDS
        )

    def test_success_resets_the_counter(self):
        b = Backoff(clock=FakeClock())
        for _ in range(5):
            b.record_failure("pin", "u1", "dev1", trusted=True)
        b.record_success("pin", "u1", "dev1", trusted=True)
        assert b.check("pin", "u1", "dev1", trusted=True) is None

    def test_clients_are_independent(self):
        b = Backoff(clock=FakeClock())
        for _ in range(4):
            b.record_failure("pin", "u1", "dev1", trusted=True)
        assert b.check("pin", "u1", "dev1", trusted=True) is not None
        assert b.check("pin", "u1", "dev2", trusted=True) is None

    def test_kinds_are_independent(self):
        b = Backoff(clock=FakeClock())
        for _ in range(4):
            b.record_failure("pin", "u1", "dev1", trusted=True)
        assert b.check("otp", "u1", "dev1", trusted=True) is None

    def test_entries_idle_a_day_are_pruned(self):
        clock = FakeClock()
        b = Backoff(clock=clock)
        for _ in range(4):
            b.record_failure("pin", "u1", "dev1", trusted=True)
        clock.advance(86401)
        assert b.check("pin", "u1", "dev1", trusted=True) is None
        assert b._failures == {}


class TestPerUserBudget:
    def test_untrusted_failures_slow_every_untrusted_client(self):
        clock = FakeClock()
        b = Backoff(clock=clock)
        # 20 failures across 20 distinct untrusted clients: each client's own
        # counter is 1 (a 1 s delay, long expired) but the budget has tripped.
        for i in range(20):
            if i:
                clock.advance(2)
            b.record_failure("pin", "u1", f"ip{i}")
        assert b.check("pin", "u1", "fresh-ip") == pytest.approx(1.0)

    def test_budget_delay_grows_and_caps(self):
        b = Backoff(clock=FakeClock())
        for i in range(30):
            b.record_failure("pin", "u1", f"ip{i}")
        # 30 failures -> 11 over the threshold -> 2 ** 10 == 1024 s.
        assert b.check("pin", "u1", "fresh-ip") == pytest.approx(1024.0)
        for i in range(30, 45):
            b.record_failure("pin", "u1", f"ip{i}")
        assert b.check("pin", "u1", "fresh-ip") == pytest.approx(BACKOFF_CAP_SECONDS)

    def test_budget_failures_age_out_of_the_hour(self):
        clock = FakeClock()
        b = Backoff(clock=clock)
        for i in range(20):
            b.record_failure("pin", "u1", f"ip{i}")
        clock.advance(3601)
        assert b.check("pin", "u1", "fresh-ip") is None

    def test_trusted_client_never_reads_the_budget(self):
        b = Backoff(clock=FakeClock())
        for i in range(25):
            b.record_failure("pin", "u1", f"ip{i}")
        # The legitimate tablet, where u1 is trusted, is unaffected.
        assert b.check("pin", "u1", "tablet", trusted=True) is None

    def test_trusted_failures_do_not_raise_the_budget(self):
        clock = FakeClock()
        b = Backoff(clock=clock)
        for _ in range(25):
            b.record_failure("pin", "u1", "tablet", trusted=True)
            b.record_success("pin", "u1", "tablet", trusted=True)
        assert b.check("pin", "u1", "fresh-ip") is None


class TestBackoffClientIp:
    def test_forwarded_header_ignored_from_untrusted_peer(self):
        b = Backoff(clock=FakeClock())
        req = FakeRequest("10.0.0.5", {"x-forwarded-for": "1.2.3.4"})
        assert b.client_ip(req) == "10.0.0.5"

    def test_rightmost_hop_used_from_trusted_proxy(self):
        b = Backoff(clock=FakeClock(), trusted_proxies=["10.0.0.0/24"])
        req = FakeRequest("10.0.0.5", {"x-forwarded-for": "1.2.3.4, 9.9.9.9"})
        assert b.client_ip(req) == "9.9.9.9"

    def test_trusted_proxy_without_header_falls_back_to_peer(self):
        b = Backoff(clock=FakeClock(), trusted_proxies=["10.0.0.0/24"])
        assert b.client_ip(FakeRequest("10.0.0.5")) == "10.0.0.5"

    def test_invalid_cidr_is_ignored_not_fatal(self):
        b = Backoff(clock=FakeClock(), trusted_proxies=["not-a-cidr", "10.0.0.0/24"])
        req = FakeRequest("10.0.0.5", {"x-forwarded-for": "9.9.9.9"})
        assert b.client_ip(req) == "9.9.9.9"


class TestBackoffIsHttps:
    def test_direct_https_scheme(self):
        b = Backoff(clock=FakeClock())
        assert b.is_https(FakeRequest(scheme="https")) is True

    def test_forwarded_proto_only_from_trusted_proxy(self):
        b = Backoff(clock=FakeClock(), trusted_proxies=["10.0.0.0/24"])
        req = FakeRequest("10.0.0.5", {"x-forwarded-proto": "https"})
        assert b.is_https(req) is True

    def test_forwarded_proto_from_untrusted_peer_is_ignored(self):
        b = Backoff(clock=FakeClock())
        req = FakeRequest("8.8.8.8", {"x-forwarded-proto": "https"})
        assert b.is_https(req) is False
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_access.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'services.access'`.

- [ ] **Step 3: Implement**

Create `services/access.py`:

```python
"""Named users, roles, PIN login, device trust and back-off for the dashboard.

Everything access-related lives here: the ``Backoff`` rate limiter, PIN and
code hashing, the permission table, and the ``AccessStore`` persisted to
``data/access.json``. That file holds secrets and PII and is never merged
into config.json.
"""

from __future__ import annotations

import ipaddress
import time
from collections import deque
from typing import Callable, Optional

from loguru import logger

BACKOFF_CAP_SECONDS = 3600.0
BUDGET_THRESHOLD = 20
BUDGET_WINDOW_SECONDS = 3600.0
BACKOFF_PRUNE_SECONDS = 86400.0


class Backoff:
    """Exponential back-off per (kind, subject, client), plus a per-user budget.

    After ``n`` consecutive failures the next attempt waits
    ``min(2 ** (n - 1), 3600)`` seconds. A success resets that key. Failures
    from clients where the subject user is not trusted also feed a per-user
    budget, so a distributed attacker cannot buy fresh counters with fresh
    IPs; attempts from a client the user is trusted on never touch it, so a
    stranger can never slow the legitimate user on their own tablet.

    There are no hard caps, no per-user disabling and no IP lockout.
    """

    def __init__(
        self,
        clock: Callable[[], float] = time.monotonic,
        trusted_proxies: Optional[list[str]] = None,
    ):
        self._clock = clock
        # (kind, subject, client) -> (consecutive_failures, last_failure_at)
        self._failures: dict[tuple[str, str, str], tuple[int, float]] = {}
        # (kind, subject) -> timestamps of untrusted failures inside the window
        self._budget: dict[tuple[str, str], deque[float]] = {}
        self._networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
        self.set_trusted_proxies(trusted_proxies or [])

    # --- configuration ---

    def set_trusted_proxies(self, cidrs: list[str]) -> None:
        self._networks = []
        for cidr in cidrs:
            try:
                self._networks.append(ipaddress.ip_network(cidr.strip(), strict=False))
            except ValueError:
                logger.warning(f"Backoff: ignoring invalid trusted proxy CIDR {cidr!r}")

    def _is_trusted_proxy(self, peer: str) -> bool:
        try:
            addr = ipaddress.ip_address(peer)
        except ValueError:
            return False
        return any(addr in net for net in self._networks)

    def client_ip(self, request) -> str:
        """The address to key on when no stored device resolves.

        Only when the socket peer is a configured proxy is X-Forwarded-For
        consulted, and then its rightmost entry: the hop that proxy appended.
        Anything a client supplied itself sits to the left and is ignored.
        """
        peer = request.client.host if request.client else "unknown"
        if not self._is_trusted_proxy(peer):
            return peer
        forwarded = request.headers.get("x-forwarded-for", "")
        hops = [h.strip() for h in forwarded.split(",") if h.strip()]
        return hops[-1] if hops else peer

    def is_https(self, request) -> bool:
        """True when the browser's own hop was TLS, for the Secure cookie flag."""
        if request.url.scheme == "https":
            return True
        peer = request.client.host if request.client else "unknown"
        if not self._is_trusted_proxy(peer):
            return False
        return request.headers.get("x-forwarded-proto", "").lower() == "https"

    # --- accounting ---

    def _prune(self, now: float) -> None:
        for key, (_, last) in list(self._failures.items()):
            if now - last > BACKOFF_PRUNE_SECONDS:
                del self._failures[key]
        for key, stamps in list(self._budget.items()):
            while stamps and now - stamps[0] > BUDGET_WINDOW_SECONDS:
                stamps.popleft()
            if not stamps:
                del self._budget[key]

    @staticmethod
    def _delay(failures: int) -> float:
        return min(2.0 ** (failures - 1), BACKOFF_CAP_SECONDS)

    def check(
        self, kind: str, subject: str, client: str, *, trusted: bool = False
    ) -> float | None:
        """Seconds still to wait before another attempt, or None if allowed."""
        now = self._clock()
        self._prune(now)
        waits: list[float] = []

        entry = self._failures.get((kind, subject, client))
        if entry:
            failures, last = entry
            waits.append(last + self._delay(failures) - now)

        if not trusted:
            stamps = self._budget.get((kind, subject))
            if stamps and len(stamps) >= BUDGET_THRESHOLD:
                over = len(stamps) - BUDGET_THRESHOLD + 1
                waits.append(stamps[-1] + self._delay(over) - now)

        remaining = max(waits, default=0.0)
        return remaining if remaining > 0 else None

    def record_failure(
        self, kind: str, subject: str, client: str, *, trusted: bool = False
    ) -> None:
        now = self._clock()
        self._prune(now)
        failures, _ = self._failures.get((kind, subject, client), (0, 0.0))
        self._failures[(kind, subject, client)] = (failures + 1, now)
        if not trusted:
            self._budget.setdefault((kind, subject), deque()).append(now)

    def record_success(
        self, kind: str, subject: str, client: str, *, trusted: bool = False
    ) -> None:
        """Clear this key's counter.

        The per-user budget is deliberately left alone: one correct guess must
        not wipe the cost an attack has already accumulated.
        """
        self._prune(self._clock())
        self._failures.pop((kind, subject, client), None)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_access.py -v`
Expected: PASS — 18 tests.

- [ ] **Step 5: Lint and commit**

```bash
ruff check --fix .
ruff format .
git add services/access.py tests/test_access.py
git commit -m "feat(access): Backoff with clock injection and per-user budget"
```

---

### Task 3: Roles, permissions and secret hashing

**Spec:** §1 (hashing), §4 (permission table).

**Files:**
- Modify: `services/access.py`
- Test: `tests/test_access.py`

**Interfaces:**
- Consumes: nothing from Task 2.
- Produces:
  - `class Role(str, Enum)`: `owner`, `secretary`, `tech`, `loader`
  - `class Permission(str, Enum)`: `view_status`, `clear_faults`, `view_logs`, `machine_controls`, `run_tests`, `edit_catalog`, `edit_placement`, `view_reports`, `edit_contacts`, `edit_secrets`, `manage_users`, `manage_ownership`
  - `ROLE_PERMISSIONS: dict[Role, frozenset[Permission]]`
  - `hash_pin(pin: str, salt: str | None = None) -> tuple[str, str]` returning `(pin_hash_hex, salt_hex)`
  - `verify_pin(pin: str, pin_hash: str, pin_salt: str) -> bool`
  - `hash_secret(value: str) -> str` returning `"scrypt$<salt_hex>$<digest_hex>"`
  - `verify_secret(value: str, stored: str) -> bool`
  - `generate_code(digits: int) -> str` — zero-padded `secrets.randbelow(10 ** digits)`
  - `generate_token() -> str` — `secrets.token_urlsafe(32)`
  - `token_fingerprint(token: str) -> str` — sha256 hex, what `devices.token_hash` stores

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_access.py`:

```python
from services.access import (
    ROLE_PERMISSIONS,
    Permission,
    Role,
    generate_code,
    generate_token,
    hash_pin,
    hash_secret,
    token_fingerprint,
    verify_pin,
    verify_secret,
)


class TestHashing:
    def test_pin_round_trip(self):
        h, s = hash_pin("1379")
        assert verify_pin("1379", h, s)
        assert not verify_pin("1380", h, s)

    def test_same_pin_two_users_different_hashes(self):
        h1, s1 = hash_pin("1379")
        h2, s2 = hash_pin("1379")
        assert s1 != s2
        assert h1 != h2

    def test_pin_hash_is_hex_and_not_the_pin(self):
        h, s = hash_pin("1379")
        bytes.fromhex(h)
        bytes.fromhex(s)
        assert "1379" not in h

    def test_secret_round_trip(self):
        stored = hash_secret("12345678")
        assert stored.startswith("scrypt$")
        assert verify_secret("12345678", stored)
        assert not verify_secret("12345679", stored)

    def test_verify_secret_rejects_garbage_without_raising(self):
        assert verify_secret("12345678", "") is False
        assert verify_secret("12345678", "nonsense") is False
        assert verify_secret("12345678", "scrypt$zz$zz") is False


class TestCodeGeneration:
    def test_generate_code_length_and_digits(self):
        for _ in range(50):
            code = generate_code(8)
            assert len(code) == 8
            assert code.isdigit()

    def test_generate_code_six_digits(self):
        assert len(generate_code(6)) == 6

    def test_tokens_are_unique_and_long(self):
        tokens = {generate_token() for _ in range(100)}
        assert len(tokens) == 100
        assert all(len(t) >= 40 for t in tokens)

    def test_token_fingerprint_is_stable_sha256(self):
        assert token_fingerprint("abc") == token_fingerprint("abc")
        assert len(token_fingerprint("abc")) == 64
        assert token_fingerprint("abc") != token_fingerprint("abd")


class TestPermissionTable:
    def test_every_role_has_an_entry(self):
        assert set(ROLE_PERMISSIONS) == set(Role)

    def test_everyone_sees_status_and_edits_placement(self):
        for role in Role:
            assert Permission.view_status in ROLE_PERMISSIONS[role]
            assert Permission.edit_placement in ROLE_PERMISSIONS[role]

    def test_owner_has_every_permission(self):
        assert ROLE_PERMISSIONS[Role.owner] == frozenset(Permission)

    def test_secretary_matrix(self):
        assert ROLE_PERMISSIONS[Role.secretary] == frozenset(
            {
                Permission.view_status,
                Permission.edit_catalog,
                Permission.edit_placement,
                Permission.view_reports,
                Permission.edit_contacts,
                Permission.manage_users,
            }
        )

    def test_tech_matrix(self):
        assert ROLE_PERMISSIONS[Role.tech] == frozenset(
            {
                Permission.view_status,
                Permission.clear_faults,
                Permission.view_logs,
                Permission.machine_controls,
                Permission.run_tests,
                Permission.edit_placement,
            }
        )

    def test_loader_matrix(self):
        assert ROLE_PERMISSIONS[Role.loader] == frozenset(
            {Permission.view_status, Permission.edit_placement}
        )

    def test_only_owner_manages_ownership_or_secrets(self):
        for role in (Role.secretary, Role.tech, Role.loader):
            assert Permission.manage_ownership not in ROLE_PERMISSIONS[role]
            assert Permission.edit_secrets not in ROLE_PERMISSIONS[role]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_access.py -v`
Expected: FAIL — `ImportError: cannot import name 'Permission' from 'services.access'`.

- [ ] **Step 3: Implement**

Add `import hashlib`, `import secrets` and `from enum import Enum` to the imports of `services/access.py`, then insert this above the `Backoff` class:

```python
SCRYPT_N = 2**14
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_DKLEN = 32


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
        }
    ),
    Role.loader: frozenset({Permission.view_status, Permission.edit_placement}),
}


def _scrypt(value: str, salt: bytes) -> bytes:
    return hashlib.scrypt(
        value.encode("utf-8"),
        salt=salt,
        n=SCRYPT_N,
        r=SCRYPT_R,
        p=SCRYPT_P,
        dklen=SCRYPT_DKLEN,
    )


def hash_pin(pin: str, salt: str | None = None) -> tuple[str, str]:
    """Return (pin_hash_hex, salt_hex); a fresh random salt unless one is given."""
    salt_bytes = bytes.fromhex(salt) if salt else secrets.token_bytes(16)
    return _scrypt(pin, salt_bytes).hex(), salt_bytes.hex()


def verify_pin(pin: str, pin_hash: str, pin_salt: str) -> bool:
    try:
        candidate, _ = hash_pin(pin, pin_salt)
    except ValueError:
        return False
    return secrets.compare_digest(candidate, pin_hash)


def hash_secret(value: str) -> str:
    """Salted scrypt for codes stored without a separate salt column."""
    salt = secrets.token_bytes(16)
    return f"scrypt${salt.hex()}${_scrypt(value, salt).hex()}"


def verify_secret(value: str, stored: str) -> bool:
    """Constant-time check against hash_secret output. Garbage returns False."""
    try:
        scheme, salt_hex, digest_hex = stored.split("$")
        if scheme != "scrypt":
            return False
        salt = bytes.fromhex(salt_hex)
    except (ValueError, AttributeError):
        return False
    return secrets.compare_digest(_scrypt(value, salt).hex(), digest_hex)


def generate_code(digits: int) -> str:
    """A zero-padded random decimal code of exactly *digits* digits."""
    return str(secrets.randbelow(10**digits)).zfill(digits)


def generate_token() -> str:
    """A random 256-bit URL-safe token for cookies and session ids."""
    return secrets.token_urlsafe(32)


def token_fingerprint(token: str) -> str:
    """sha256 of a cookie value — what the store keeps instead of the token."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_access.py -v`
Expected: PASS — 18 from Task 2 plus 18 new.

- [ ] **Step 5: Lint and commit**

```bash
ruff check --fix .
ruff format .
git add services/access.py tests/test_access.py
git commit -m "feat(access): roles, permission table and scrypt hashing"
```

---

### Task 4: `AccessStore` persistence — users and devices

**Spec:** §1 (data model, invariants, file permissions), §6 (corrupt file).

**Files:**
- Modify: `services/access.py`
- Test: `tests/test_access.py`

**Interfaces:**
- Consumes: `hash_pin`, `verify_pin`, `generate_token`, `token_fingerprint`, `Role` (Task 3).
- Produces:
  - `ACCESS_FILE_NAME = "access.json"`; `access_path() -> Path` (= `DATA_DIR / ACCESS_FILE_NAME`, resolved at call time)
  - `class AccessError(Exception)`; `class OwnerExistsError(AccessError)`
  - `@dataclass User`: `id, name, email, role: Role, pin_hash, pin_salt, disabled: bool, created_at: str, last_login_at: str | None`
  - `@dataclass Device`: `id, token_hash, label, shared: bool, trusted_user_ids: list[str], created_at: str, last_seen_at: str`
  - `AccessStore(path: Path | None = None, clock: Callable[[], float] = time.monotonic, wall_clock: Callable[[], datetime] | None = None)`
  - `store.corrupt: bool`, `store.users: dict[str, User]`, `store.devices: dict[str, Device]`
  - `store.owner() -> User | None`, `store.enabled_users() -> list[User]`, `store.get_user(user_id) -> User | None`
  - `store.create_user(name: str, email: str | None, role: Role, pin: str) -> User` (raises `OwnerExistsError`)
  - `store.update_user(user_id, *, name=None, email=None, role=None) -> User`
  - `store.set_user_pin(user_id: str, pin: str) -> None` (rehashes **and** drops the user from every device)
  - `store.set_user_disabled(user_id: str, disabled: bool) -> None`
  - `store.delete_user(user_id: str) -> None`
  - `store.verify_user_pin(user_id: str, pin: str) -> bool` (False for unknown or disabled users)
  - `store.create_device(label: str, shared: bool) -> tuple[Device, str]` → `(device, raw_token)`
  - `store.device_for_token(token: str | None) -> Device | None`
  - `store.trust_device(device_id: str, user_id: str) -> None`
  - `store.forget_device(device_id: str) -> None`, `store.set_device_shared(device_id: str, shared: bool) -> None`
  - `store.touch_device(device_id: str) -> None`, `store.record_login(user_id: str) -> None`
  - `store.prune_devices() -> int` (removes devices older than 24 h with no trusted users)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_access.py` (add `import json`, `import os`, `from datetime import datetime, timedelta, timezone`, `from pathlib import Path` to the module imports):

```python
from services.access import AccessStore, OwnerExistsError, Role


class FakeWallClock:
    def __init__(self, start: datetime | None = None):
        self.now = start or datetime(2026, 9, 25, 12, 0, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs) -> None:
        self.now += timedelta(**kwargs)


@pytest.fixture
def store(tmp_path):
    return AccessStore(path=tmp_path / "access.json", clock=FakeClock(), wall_clock=FakeWallClock())


class TestStorePersistence:
    def test_missing_file_is_empty_not_corrupt(self, store):
        assert store.users == {}
        assert store.devices == {}
        assert store.corrupt is False
        assert store.owner() is None

    def test_created_user_survives_a_reload(self, tmp_path):
        path = tmp_path / "access.json"
        s1 = AccessStore(path=path, clock=FakeClock(), wall_clock=FakeWallClock())
        user = s1.create_user("Ada", "ada@example.com", Role.owner, "1379")
        s2 = AccessStore(path=path, clock=FakeClock(), wall_clock=FakeWallClock())
        assert s2.get_user(user.id).name == "Ada"
        assert s2.get_user(user.id).role is Role.owner
        assert s2.verify_user_pin(user.id, "1379")

    def test_pin_is_never_written_in_clear(self, tmp_path):
        path = tmp_path / "access.json"
        s = AccessStore(path=path, clock=FakeClock(), wall_clock=FakeWallClock())
        s.create_user("Ada", None, Role.owner, "1379")
        assert "1379" not in path.read_text(encoding="utf-8")

    def test_invalid_json_marks_the_store_corrupt(self, tmp_path):
        path = tmp_path / "access.json"
        path.write_text("{not json", encoding="utf-8")
        s = AccessStore(path=path, clock=FakeClock(), wall_clock=FakeWallClock())
        assert s.corrupt is True
        assert s.users == {}

    def test_a_corrupt_store_refuses_to_write(self, tmp_path):
        path = tmp_path / "access.json"
        path.write_text("{not json", encoding="utf-8")
        s = AccessStore(path=path, clock=FakeClock(), wall_clock=FakeWallClock())
        with pytest.raises(AccessError):
            s.create_user("Ada", None, Role.owner, "1379")
        assert path.read_text(encoding="utf-8") == "{not json"

    def test_timestamps_are_utc_iso8601(self, tmp_path):
        path = tmp_path / "access.json"
        s = AccessStore(path=path, clock=FakeClock(), wall_clock=FakeWallClock())
        s.create_user("Ada", None, Role.owner, "1379")
        raw = json.loads(path.read_text(encoding="utf-8"))
        created = list(raw["users"].values())[0]["created_at"]
        assert created == "2026-09-25T12:00:00+00:00"

    @pytest.mark.skipif(os.name != "posix", reason="POSIX file modes only")
    def test_file_is_created_0600(self, tmp_path):
        path = tmp_path / "access.json"
        s = AccessStore(path=path, clock=FakeClock(), wall_clock=FakeWallClock())
        s.create_user("Ada", None, Role.owner, "1379")
        assert (path.stat().st_mode & 0o777) == 0o600

    @pytest.mark.skipif(os.name != "posix", reason="POSIX file modes only")
    def test_loose_permissions_are_tightened_with_a_warning(self, tmp_path, caplog):
        path = tmp_path / "access.json"
        path.write_text('{"users": {}, "devices": {}}', encoding="utf-8")
        path.chmod(0o644)
        AccessStore(path=path, clock=FakeClock(), wall_clock=FakeWallClock())
        assert (path.stat().st_mode & 0o777) == 0o600
        assert "0600" in caplog.text

    @pytest.mark.skipif(os.name != "posix", reason="POSIX file modes only")
    def test_no_temp_file_is_left_behind(self, tmp_path):
        path = tmp_path / "access.json"
        s = AccessStore(path=path, clock=FakeClock(), wall_clock=FakeWallClock())
        s.create_user("Ada", None, Role.owner, "1379")
        assert list(tmp_path.glob("*.tmp")) == []


class TestUsers:
    def test_second_owner_is_rejected(self, store):
        store.create_user("Ada", None, Role.owner, "1379")
        with pytest.raises(OwnerExistsError):
            store.create_user("Bob", None, Role.owner, "2468")

    def test_second_owner_rejection_does_not_persist_the_user(self, store):
        store.create_user("Ada", None, Role.owner, "1379")
        with pytest.raises(OwnerExistsError):
            store.create_user("Bob", None, Role.owner, "2468")
        assert [u.name for u in store.users.values()] == ["Ada"]

    def test_other_roles_may_repeat(self, store):
        store.create_user("T1", None, Role.tech, "1379")
        store.create_user("T2", None, Role.tech, "2468")
        assert len(store.users) == 2

    def test_promoting_a_second_user_to_owner_is_rejected(self, store):
        store.create_user("Ada", None, Role.owner, "1379")
        bob = store.create_user("Bob", None, Role.tech, "2468")
        with pytest.raises(OwnerExistsError):
            store.update_user(bob.id, role=Role.owner)

    def test_disabled_user_fails_pin_verification(self, store):
        u = store.create_user("Ada", None, Role.owner, "1379")
        store.set_user_disabled(u.id, True)
        assert store.verify_user_pin(u.id, "1379") is False

    def test_unknown_user_fails_pin_verification(self, store):
        assert store.verify_user_pin("nope", "1379") is False

    def test_enabled_users_excludes_disabled(self, store):
        a = store.create_user("Ada", None, Role.owner, "1379")
        b = store.create_user("Bob", None, Role.tech, "2468")
        store.set_user_disabled(b.id, True)
        assert [u.id for u in store.enabled_users()] == [a.id]

    def test_reset_pin_changes_the_hash_and_untrusts_every_device(self, store):
        u = store.create_user("Ada", None, Role.owner, "1379")
        dev, _ = store.create_device("Tablet", shared=True)
        store.trust_device(dev.id, u.id)
        store.set_user_pin(u.id, "2468")
        assert store.verify_user_pin(u.id, "2468")
        assert store.verify_user_pin(u.id, "1379") is False
        assert store.devices[dev.id].trusted_user_ids == []

    def test_deleting_a_user_removes_them_from_every_device(self, store):
        u = store.create_user("Ada", None, Role.owner, "1379")
        d1, _ = store.create_device("Tablet", shared=True)
        d2, _ = store.create_device("Phone", shared=False)
        store.trust_device(d1.id, u.id)
        store.trust_device(d2.id, u.id)
        store.delete_user(u.id)
        assert store.get_user(u.id) is None
        assert store.devices[d1.id].trusted_user_ids == []
        assert store.devices[d2.id].trusted_user_ids == []

    def test_trusting_an_unknown_user_is_refused(self, store):
        dev, _ = store.create_device("Tablet", shared=True)
        with pytest.raises(AccessError):
            store.trust_device(dev.id, "nobody")


class TestDevices:
    def test_token_resolves_to_its_device_and_is_not_stored_raw(self, tmp_path):
        path = tmp_path / "access.json"
        s = AccessStore(path=path, clock=FakeClock(), wall_clock=FakeWallClock())
        dev, token = s.create_device("Tablet", shared=True)
        assert s.device_for_token(token).id == dev.id
        assert token not in path.read_text(encoding="utf-8")

    def test_unknown_or_missing_token_resolves_to_none(self, store):
        store.create_device("Tablet", shared=True)
        assert store.device_for_token("bogus") is None
        assert store.device_for_token(None) is None

    def test_forget_and_shared_toggle(self, store):
        dev, _ = store.create_device("Tablet", shared=True)
        store.set_device_shared(dev.id, False)
        assert store.devices[dev.id].shared is False
        store.forget_device(dev.id)
        assert dev.id not in store.devices

    def test_stale_unenrolled_devices_are_pruned_after_a_day(self, tmp_path):
        wall = FakeWallClock()
        s = AccessStore(path=tmp_path / "access.json", clock=FakeClock(), wall_clock=wall)
        user = s.create_user("Ada", None, Role.owner, "1379")
        kept, _ = s.create_device("Tablet", shared=True)
        s.trust_device(kept.id, user.id)
        abandoned, _ = s.create_device("Drive-by", shared=False)
        wall.advance(hours=25)
        assert s.prune_devices() == 1
        assert kept.id in s.devices
        assert abandoned.id not in s.devices

    def test_fresh_unenrolled_devices_survive(self, tmp_path):
        wall = FakeWallClock()
        s = AccessStore(path=tmp_path / "access.json", clock=FakeClock(), wall_clock=wall)
        fresh, _ = s.create_device("Drive-by", shared=False)
        wall.advance(hours=23)
        assert s.prune_devices() == 0
        assert fresh.id in s.devices
```

Also import `AccessError` at the top of the test module.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_access.py -v`
Expected: FAIL — `ImportError: cannot import name 'AccessStore' from 'services.access'`.

- [ ] **Step 3: Implement**

Add to the imports of `services/access.py`:

```python
import json
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from services.paths import DATA_DIR
```

Append to `services/access.py`:

```python
ACCESS_FILE_NAME = "access.json"
DEVICE_PRUNE_HOURS = 24


def access_path() -> Path:
    """The access file location, resolved at call time so tests can patch DATA_DIR."""
    return DATA_DIR / ACCESS_FILE_NAME


class AccessError(Exception):
    """A refused access-store operation."""


class OwnerExistsError(AccessError):
    """At most one user may hold the owner role."""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class User:
    id: str
    name: str
    email: str | None
    role: Role
    pin_hash: str
    pin_salt: str
    disabled: bool = False
    created_at: str = ""
    last_login_at: str | None = None


@dataclass
class Device:
    id: str
    token_hash: str
    label: str
    shared: bool = False
    trusted_user_ids: list[str] = field(default_factory=list)
    created_at: str = ""
    last_seen_at: str = ""


class AccessStore:
    """Users, devices, sessions, codes and setup state for the dashboard.

    Persisted parts land in ``data/access.json`` atomically (tmp + rename)
    with mode 0600; sessions, OTPs and enrollment tokens live in memory only,
    so a restart merely means re-login.

    A file that will not parse sets ``corrupt``; every write then raises
    ``AccessError`` rather than overwriting whatever is there, and the
    dashboard serves an error page instead of an open setup wizard (spec §6).
    """

    def __init__(
        self,
        path: Path | None = None,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], datetime] | None = None,
    ):
        self._path = path if path is not None else access_path()
        self._clock = clock
        self._wall = wall_clock or _utc_now
        self.corrupt = False
        self.users: dict[str, User] = {}
        self.devices: dict[str, Device] = {}
        self._raw_extra: dict = {}
        self.load()

    # --- persistence ---

    @property
    def path(self) -> Path:
        return self._path

    def _stamp(self) -> str:
        return self._wall().isoformat()

    def load(self) -> None:
        self.users = {}
        self.devices = {}
        self._raw_extra = {}
        self.corrupt = False
        if not self._path.exists():
            return
        self._tighten_permissions()
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("access file is not a JSON object")
            for uid, data in raw.get("users", {}).items():
                self.users[uid] = User(
                    id=uid,
                    name=data["name"],
                    email=data.get("email"),
                    role=Role(data["role"]),
                    pin_hash=data["pin_hash"],
                    pin_salt=data["pin_salt"],
                    disabled=bool(data.get("disabled", False)),
                    created_at=data.get("created_at", ""),
                    last_login_at=data.get("last_login_at"),
                )
            for did, data in raw.get("devices", {}).items():
                self.devices[did] = Device(
                    id=did,
                    token_hash=data["token_hash"],
                    label=data.get("label", ""),
                    shared=bool(data.get("shared", False)),
                    trusted_user_ids=[
                        u for u in data.get("trusted_user_ids", []) if u in self.users
                    ],
                    created_at=data.get("created_at", ""),
                    last_seen_at=data.get("last_seen_at", ""),
                )
            self._raw_extra = {
                k: v for k, v in raw.items() if k not in ("users", "devices")
            }
        except Exception as e:
            self.corrupt = True
            self.users = {}
            self.devices = {}
            logger.error(
                f"AccessStore: {self._path} is unreadable or invalid ({e}); the "
                "dashboard will serve an error page. The VMC and MQTT client "
                "are unaffected."
            )

    def _tighten_permissions(self) -> None:
        if os.name != "posix":
            return
        try:
            mode = self._path.stat().st_mode & 0o777
        except OSError:
            return
        if mode != 0o600:
            try:
                self._path.chmod(0o600)
                logger.warning(
                    f"AccessStore: {self._path} had mode {mode:04o}; tightened to 0600"
                )
            except OSError as e:
                logger.warning(f"AccessStore: could not chmod {self._path}: {e}")

    def _document(self) -> dict:
        doc = dict(self._raw_extra)
        doc["users"] = {
            u.id: {
                "name": u.name,
                "email": u.email,
                "role": u.role.value,
                "pin_hash": u.pin_hash,
                "pin_salt": u.pin_salt,
                "disabled": u.disabled,
                "created_at": u.created_at,
                "last_login_at": u.last_login_at,
            }
            for u in self.users.values()
        }
        doc["devices"] = {
            d.id: {
                "token_hash": d.token_hash,
                "label": d.label,
                "shared": d.shared,
                "trusted_user_ids": list(d.trusted_user_ids),
                "created_at": d.created_at,
                "last_seen_at": d.last_seen_at,
            }
            for d in self.devices.values()
        }
        return doc

    def save(self) -> None:
        """Atomic tmp + rename. The temp file is opened 0600 so neither it nor
        the final file is ever world-readable regardless of umask."""
        if self.corrupt:
            raise AccessError("access file is corrupt; refusing to overwrite it")
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_name(self._path.name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self._document(), f, indent=2)
                f.flush()
                os.fsync(f.fileno())
        except Exception:
            tmp.unlink(missing_ok=True)
            raise
        os.replace(tmp, self._path)
        self._tighten_permissions()

    # --- users ---

    def owner(self) -> User | None:
        return next((u for u in self.users.values() if u.role is Role.owner), None)

    def get_user(self, user_id: str) -> User | None:
        return self.users.get(user_id)

    def enabled_users(self) -> list[User]:
        return sorted(
            (u for u in self.users.values() if not u.disabled), key=lambda u: u.name
        )

    def create_user(
        self, name: str, email: str | None, role: Role, pin: str
    ) -> User:
        role = Role(role)
        if role is Role.owner and self.owner() is not None:
            raise OwnerExistsError("this machine already has an owner")
        pin_hash, pin_salt = hash_pin(pin)
        user = User(
            id=str(uuid.uuid4()),
            name=name,
            email=email or None,
            role=role,
            pin_hash=pin_hash,
            pin_salt=pin_salt,
            created_at=self._stamp(),
        )
        self.users[user.id] = user
        try:
            self.save()
        except Exception:
            del self.users[user.id]
            raise
        logger.info(f"AccessStore: created user {name} ({role.value})")
        return user

    def update_user(
        self,
        user_id: str,
        *,
        name: str | None = None,
        email: str | None = None,
        role: Role | None = None,
    ) -> User:
        user = self._require_user(user_id)
        if role is not None:
            role = Role(role)
            current_owner = self.owner()
            if (
                role is Role.owner
                and current_owner is not None
                and current_owner.id != user_id
            ):
                raise OwnerExistsError("this machine already has an owner")
            user.role = role
        if name is not None:
            user.name = name
        if email is not None:
            user.email = email or None
        self.save()
        return user

    def set_user_pin(self, user_id: str, pin: str) -> None:
        """New PIN, and the user is dropped from every device so the next
        login re-enrolls (spec §4.1)."""
        user = self._require_user(user_id)
        user.pin_hash, user.pin_salt = hash_pin(pin)
        for device in self.devices.values():
            if user_id in device.trusted_user_ids:
                device.trusted_user_ids.remove(user_id)
        self.save()

    def set_user_disabled(self, user_id: str, disabled: bool) -> None:
        self._require_user(user_id).disabled = bool(disabled)
        self.save()

    def delete_user(self, user_id: str) -> None:
        self._require_user(user_id)
        del self.users[user_id]
        for device in self.devices.values():
            if user_id in device.trusted_user_ids:
                device.trusted_user_ids.remove(user_id)
        self.save()

    def verify_user_pin(self, user_id: str, pin: str) -> bool:
        user = self.users.get(user_id)
        if user is None or user.disabled:
            return False
        return verify_pin(pin, user.pin_hash, user.pin_salt)

    def record_login(self, user_id: str) -> None:
        user = self._require_user(user_id)
        user.last_login_at = self._stamp()
        self.save()

    def _require_user(self, user_id: str) -> User:
        user = self.users.get(user_id)
        if user is None:
            raise AccessError(f"no such user: {user_id}")
        return user

    # --- devices ---

    def create_device(self, label: str, shared: bool) -> tuple[Device, str]:
        """Return the record and the raw cookie token (never stored)."""
        token = generate_token()
        device = Device(
            id=str(uuid.uuid4()),
            token_hash=token_fingerprint(token),
            label=label,
            shared=bool(shared),
            created_at=self._stamp(),
            last_seen_at=self._stamp(),
        )
        self.devices[device.id] = device
        self.save()
        return device, token

    def device_for_token(self, token: str | None) -> Device | None:
        if not token:
            return None
        fingerprint = token_fingerprint(token)
        return next(
            (d for d in self.devices.values() if d.token_hash == fingerprint), None
        )

    def trust_device(self, device_id: str, user_id: str) -> None:
        device = self._require_device(device_id)
        self._require_user(user_id)
        if user_id not in device.trusted_user_ids:
            device.trusted_user_ids.append(user_id)
        device.last_seen_at = self._stamp()
        self.save()

    def forget_device(self, device_id: str) -> None:
        self._require_device(device_id)
        del self.devices[device_id]
        self.save()

    def set_device_shared(self, device_id: str, shared: bool) -> None:
        self._require_device(device_id).shared = bool(shared)
        self.save()

    def touch_device(self, device_id: str) -> None:
        device = self.devices.get(device_id)
        if device is None:
            return
        device.last_seen_at = self._stamp()
        self.save()

    def prune_devices(self) -> int:
        """Drop devices older than 24 h that never completed enrollment."""
        cutoff = self._wall() - timedelta(hours=DEVICE_PRUNE_HOURS)
        removed = 0
        for device in list(self.devices.values()):
            if device.trusted_user_ids:
                continue
            try:
                created = datetime.fromisoformat(device.created_at)
            except ValueError:
                continue
            if created < cutoff:
                del self.devices[device.id]
                removed += 1
        if removed:
            self.save()
        return removed

    def _require_device(self, device_id: str) -> Device:
        device = self.devices.get(device_id)
        if device is None:
            raise AccessError(f"no such device: {device_id}")
        return device
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_access.py -v`
Expected: PASS. On Windows the four `skipif(os.name != "posix")` tests are skipped; on CI (Linux) they run.

- [ ] **Step 5: Lint and commit**

```bash
ruff check --fix .
ruff format .
git add services/access.py tests/test_access.py
git commit -m "feat(access): AccessStore persistence for users and devices"
```

---

### Task 5: `AccessStore` sessions, OTPs and enrollment tokens

**Spec:** §1 (in-memory structures), §2.1 (session validity), §2.3 (OTP, `vmc_enroll`).

**Files:**
- Modify: `services/access.py`
- Test: `tests/test_access.py`

**Interfaces:**
- Consumes: `AccessStore`, `Device`, `User` (Task 4); `generate_code`, `generate_token` (Task 3).
- Produces:
  - Constants `SHARED_IDLE_SECONDS = 300.0`, `PERSONAL_IDLE_SECONDS = 28800.0`, `SESSION_MAX_SECONDS = 86400.0`, `OTP_TTL_SECONDS = 600.0`, `OTP_DIGITS = 6`, `ENROLL_TTL_SECONDS = 600.0`
  - `@dataclass Session`: `id, user_id, device_id, created_at: float, last_active_at: float`
  - `store.create_session(user_id: str, device_id: str) -> str` (the session id, used as the `vmc_session` cookie value)
  - `store.resolve_session(session_id: str | None) -> Session | None` — applies idle and absolute limits, refreshes `last_active_at`, returns `None` (and forgets the session) when expired or when its user or device is gone
  - `store.end_session(session_id: str) -> None`, `store.end_all_sessions() -> None`, `store.end_sessions_for_user(user_id: str) -> None`
  - `store.issue_otp(user_id: str, device_id: str) -> str` (6 digits; replaces any pending OTP for that pair)
  - `store.verify_otp(user_id: str, device_id: str, code: str) -> bool` (single use; expired codes fail)
  - `store.issue_enroll_token(user_id: str, client: str) -> str`
  - `store.resolve_enroll_token(token: str | None, client: str) -> str | None` — the user id when the token is live and bound to this client
  - `store.clear_enroll_token(token: str) -> None`

These structures are memory-only: nothing here writes to disk.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_access.py`:

```python
class TestSessions:
    def test_session_resolves_to_its_user_and_device(self, store):
        u = store.create_user("Ada", None, Role.owner, "1379")
        d, _ = store.create_device("Phone", shared=False)
        sid = store.create_session(u.id, d.id)
        session = store.resolve_session(sid)
        assert session.user_id == u.id
        assert session.device_id == d.id

    def test_unknown_session_resolves_to_none(self, store):
        assert store.resolve_session("nope") is None
        assert store.resolve_session(None) is None

    def test_shared_device_idles_out_after_five_minutes(self, tmp_path):
        clock = FakeClock()
        s = AccessStore(path=tmp_path / "access.json", clock=clock, wall_clock=FakeWallClock())
        u = s.create_user("Ada", None, Role.owner, "1379")
        d, _ = s.create_device("Tablet", shared=True)
        sid = s.create_session(u.id, d.id)
        clock.advance(299)
        assert s.resolve_session(sid) is not None
        clock.advance(301)
        assert s.resolve_session(sid) is None

    def test_personal_device_idles_out_after_eight_hours(self, tmp_path):
        clock = FakeClock()
        s = AccessStore(path=tmp_path / "access.json", clock=clock, wall_clock=FakeWallClock())
        u = s.create_user("Ada", None, Role.owner, "1379")
        d, _ = s.create_device("Phone", shared=False)
        sid = s.create_session(u.id, d.id)
        clock.advance(28799)
        assert s.resolve_session(sid) is not None
        clock.advance(28801)
        assert s.resolve_session(sid) is None

    def test_activity_refreshes_the_idle_clock(self, tmp_path):
        clock = FakeClock()
        s = AccessStore(path=tmp_path / "access.json", clock=clock, wall_clock=FakeWallClock())
        u = s.create_user("Ada", None, Role.owner, "1379")
        d, _ = s.create_device("Tablet", shared=True)
        sid = s.create_session(u.id, d.id)
        for _ in range(10):
            clock.advance(200)
            assert s.resolve_session(sid) is not None

    def test_absolute_cap_ends_a_busy_session_at_a_day(self, tmp_path):
        clock = FakeClock()
        s = AccessStore(path=tmp_path / "access.json", clock=clock, wall_clock=FakeWallClock())
        u = s.create_user("Ada", None, Role.owner, "1379")
        d, _ = s.create_device("Phone", shared=False)
        sid = s.create_session(u.id, d.id)
        for _ in range(500):
            clock.advance(200)
            s.resolve_session(sid)
        assert s.resolve_session(sid) is None

    def test_session_dies_with_its_user(self, store):
        u = store.create_user("Ada", None, Role.owner, "1379")
        d, _ = store.create_device("Phone", shared=False)
        sid = store.create_session(u.id, d.id)
        store.delete_user(u.id)
        assert store.resolve_session(sid) is None

    def test_session_dies_with_its_device(self, store):
        u = store.create_user("Ada", None, Role.owner, "1379")
        d, _ = store.create_device("Phone", shared=False)
        sid = store.create_session(u.id, d.id)
        store.forget_device(d.id)
        assert store.resolve_session(sid) is None

    def test_end_session_and_end_all(self, store):
        u = store.create_user("Ada", None, Role.owner, "1379")
        d, _ = store.create_device("Phone", shared=False)
        s1 = store.create_session(u.id, d.id)
        s2 = store.create_session(u.id, d.id)
        store.end_session(s1)
        assert store.resolve_session(s1) is None
        assert store.resolve_session(s2) is not None
        store.end_all_sessions()
        assert store.resolve_session(s2) is None


class TestOtps:
    def test_otp_round_trip(self, store):
        u = store.create_user("Ada", None, Role.owner, "1379")
        d, _ = store.create_device("Phone", shared=False)
        code = store.issue_otp(u.id, d.id)
        assert len(code) == 6 and code.isdigit()
        assert store.verify_otp(u.id, d.id, code) is True

    def test_otp_is_single_use(self, store):
        u = store.create_user("Ada", None, Role.owner, "1379")
        d, _ = store.create_device("Phone", shared=False)
        code = store.issue_otp(u.id, d.id)
        assert store.verify_otp(u.id, d.id, code) is True
        assert store.verify_otp(u.id, d.id, code) is False

    def test_otp_expires_after_ten_minutes(self, tmp_path):
        clock = FakeClock()
        s = AccessStore(path=tmp_path / "access.json", clock=clock, wall_clock=FakeWallClock())
        u = s.create_user("Ada", None, Role.owner, "1379")
        d, _ = s.create_device("Phone", shared=False)
        code = s.issue_otp(u.id, d.id)
        clock.advance(601)
        assert s.verify_otp(u.id, d.id, code) is False

    def test_resend_replaces_the_pending_otp(self, store):
        u = store.create_user("Ada", None, Role.owner, "1379")
        d, _ = store.create_device("Phone", shared=False)
        first = store.issue_otp(u.id, d.id)
        second = store.issue_otp(u.id, d.id)
        assert store.verify_otp(u.id, d.id, first) is False
        assert store.verify_otp(u.id, d.id, second) is True

    def test_otp_is_bound_to_its_device(self, store):
        u = store.create_user("Ada", None, Role.owner, "1379")
        d1, _ = store.create_device("Phone", shared=False)
        d2, _ = store.create_device("Laptop", shared=False)
        code = store.issue_otp(u.id, d1.id)
        assert store.verify_otp(u.id, d2.id, code) is False


class TestEnrollTokens:
    def test_enroll_token_round_trip(self, store):
        u = store.create_user("Ada", None, Role.owner, "1379")
        token = store.issue_enroll_token(u.id, "ip-1")
        assert store.resolve_enroll_token(token, "ip-1") == u.id

    def test_enroll_token_is_bound_to_its_client(self, store):
        u = store.create_user("Ada", None, Role.owner, "1379")
        token = store.issue_enroll_token(u.id, "ip-1")
        assert store.resolve_enroll_token(token, "ip-2") is None

    def test_enroll_token_expires_after_ten_minutes(self, tmp_path):
        clock = FakeClock()
        s = AccessStore(path=tmp_path / "access.json", clock=clock, wall_clock=FakeWallClock())
        u = s.create_user("Ada", None, Role.owner, "1379")
        token = s.issue_enroll_token(u.id, "ip-1")
        clock.advance(601)
        assert s.resolve_enroll_token(token, "ip-1") is None

    def test_cleared_enroll_token_stops_resolving(self, store):
        u = store.create_user("Ada", None, Role.owner, "1379")
        token = store.issue_enroll_token(u.id, "ip-1")
        store.clear_enroll_token(token)
        assert store.resolve_enroll_token(token, "ip-1") is None
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_access.py -v -k "Sessions or Otps or EnrollTokens"`
Expected: FAIL — `AttributeError: 'AccessStore' object has no attribute 'create_session'`.

- [ ] **Step 3: Implement**

Add the constants and `Session` dataclass next to the others in `services/access.py`:

```python
SHARED_IDLE_SECONDS = 300.0
PERSONAL_IDLE_SECONDS = 28800.0
SESSION_MAX_SECONDS = 86400.0
OTP_TTL_SECONDS = 600.0
OTP_DIGITS = 6
ENROLL_TTL_SECONDS = 600.0


@dataclass
class Session:
    id: str
    user_id: str
    device_id: str
    created_at: float
    last_active_at: float
```

In `AccessStore.__init__`, after `self.devices`, add:

```python
        self._sessions: dict[str, Session] = {}
        # (user_id, device_id) -> (code, expires_at)
        self._pending_otps: dict[tuple[str, str], tuple[str, float]] = {}
        # enroll token -> (user_id, client, expires_at)
        self._enroll_tokens: dict[str, tuple[str, str, float]] = {}
```

Append these methods to `AccessStore`:

```python
    # --- sessions (memory only) ---

    def create_session(self, user_id: str, device_id: str) -> str:
        now = self._clock()
        session = Session(
            id=generate_token(),
            user_id=user_id,
            device_id=device_id,
            created_at=now,
            last_active_at=now,
        )
        self._sessions[session.id] = session
        return session.id

    def resolve_session(self, session_id: str | None) -> Session | None:
        """The live session for this cookie, refreshing its idle clock.

        Returns None — and forgets the session — when it has idled out, hit
        the absolute cap, or lost its user or device (spec §6).
        """
        if not session_id:
            return None
        session = self._sessions.get(session_id)
        if session is None:
            return None
        device = self.devices.get(session.device_id)
        user = self.users.get(session.user_id)
        if device is None or user is None or user.disabled:
            del self._sessions[session_id]
            return None
        now = self._clock()
        idle_limit = SHARED_IDLE_SECONDS if device.shared else PERSONAL_IDLE_SECONDS
        if (
            now - session.last_active_at > idle_limit
            or now - session.created_at > SESSION_MAX_SECONDS
        ):
            del self._sessions[session_id]
            return None
        session.last_active_at = now
        return session

    def end_session(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)

    def end_all_sessions(self) -> None:
        self._sessions.clear()

    def end_sessions_for_user(self, user_id: str) -> None:
        for sid, session in list(self._sessions.items()):
            if session.user_id == user_id:
                del self._sessions[sid]

    # --- one-time passwords (memory only) ---

    def issue_otp(self, user_id: str, device_id: str) -> str:
        """A fresh 6-digit OTP for this (user, device), replacing any pending one."""
        code = generate_code(OTP_DIGITS)
        self._pending_otps[(user_id, device_id)] = (
            code,
            self._clock() + OTP_TTL_SECONDS,
        )
        return code

    def verify_otp(self, user_id: str, device_id: str, code: str) -> bool:
        entry = self._pending_otps.get((user_id, device_id))
        if entry is None:
            return False
        stored, expires_at = entry
        if self._clock() > expires_at:
            del self._pending_otps[(user_id, device_id)]
            return False
        if not secrets.compare_digest(stored, code):
            return False
        del self._pending_otps[(user_id, device_id)]
        return True

    # --- enrollment tokens (memory only) ---

    def issue_enroll_token(self, user_id: str, client: str) -> str:
        """Proof that this client just verified *user_id*'s PIN, valid 10 minutes."""
        token = generate_token()
        self._enroll_tokens[token] = (
            user_id,
            client,
            self._clock() + ENROLL_TTL_SECONDS,
        )
        return token

    def resolve_enroll_token(self, token: str | None, client: str) -> str | None:
        if not token:
            return None
        entry = self._enroll_tokens.get(token)
        if entry is None:
            return None
        user_id, bound_client, expires_at = entry
        if self._clock() > expires_at:
            del self._enroll_tokens[token]
            return None
        if bound_client != client:
            return None
        return user_id

    def clear_enroll_token(self, token: str) -> None:
        self._enroll_tokens.pop(token, None)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_access.py -v`
Expected: PASS — all previous tests plus 18 new.

- [ ] **Step 5: Lint and commit**

```bash
ruff check --fix .
ruff format .
git add services/access.py tests/test_access.py
git commit -m "feat(access): AccessStore sessions, OTPs and enrollment tokens"
```

---

### Task 6: `AccessStore` emergency codes, setup code and pending transfer

**Spec:** §3.1, §3.2, §3.3, §3.4.

**Files:**
- Modify: `services/access.py`
- Test: `tests/test_access.py`

**Interfaces:**
- Consumes: everything from Tasks 3–5.
- Produces:
  - Constants `EMERGENCY_CODE_COUNT = 20`, `CODE_DIGITS = 8`, `TRANSFER_TTL_DAYS = 7`
  - `store.setup_mode -> bool` — True while no user has role `owner`
  - `store.begin_setup() -> str` — generate and store an 8-digit setup code, return the plaintext; idempotent while one is live (returns the same plaintext)
  - `store.pending_setup_code -> str | None` — the plaintext held in memory until `finalize_setup()`
  - `store.verify_setup_code(code: str) -> bool`
  - `store.setup_finalized -> bool`; `store.finalize_setup() -> None` (invalidates the setup code, sets `setup.finalized`)
  - `store.generate_emergency_codes(count: int = 20) -> list[str]` — replaces the whole pool, returns the plaintexts once
  - `store.consume_emergency_code(code: str, user_id: str, used_for: str) -> bool` — `used_for` is `"enroll"` or `"transfer"`
  - `store.unused_emergency_code_count() -> int`
  - `store.start_transfer(started_by_user_id: str) -> str` — 8-digit transfer code plaintext, 7-day expiry
  - `store.pending_transfer -> dict | None` — `None` once expired
  - `store.verify_transfer_code(code: str) -> bool`
  - `store.cancel_transfer() -> None`
  - `store.complete_transfer(name: str, email: str | None, pin: str) -> User` — atomically creates the new owner, deletes the old owner and removes them from every device, deletes every emergency code, ends every session, clears `pending_transfer`
  - `store.machine_report(config) -> str` — the plain-text report of spec §3.4

Persisted keys added to `access.json`: `emergency_codes` (list), `setup` (object), `pending_transfer` (object or null).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_access.py`:

```python
class TestEmergencyCodes:
    def test_pool_is_twenty_unique_eight_digit_codes(self, store):
        codes = store.generate_emergency_codes()
        assert len(codes) == 20
        assert len(set(codes)) == 20
        assert all(len(c) == 8 and c.isdigit() for c in codes)
        assert store.unused_emergency_code_count() == 20

    def test_code_is_single_use(self, store):
        u = store.create_user("Ada", None, Role.owner, "1379")
        codes = store.generate_emergency_codes()
        assert store.consume_emergency_code(codes[0], u.id, "enroll") is True
        assert store.consume_emergency_code(codes[0], u.id, "enroll") is False
        assert store.unused_emergency_code_count() == 19

    def test_unknown_code_is_refused(self, store):
        u = store.create_user("Ada", None, Role.owner, "1379")
        store.generate_emergency_codes()
        assert store.consume_emergency_code("00000000", u.id, "enroll") is False

    def test_regenerate_replaces_used_and_unused_alike(self, store):
        u = store.create_user("Ada", None, Role.owner, "1379")
        old = store.generate_emergency_codes()
        store.consume_emergency_code(old[0], u.id, "enroll")
        new = store.generate_emergency_codes()
        assert store.unused_emergency_code_count() == 20
        assert store.consume_emergency_code(old[1], u.id, "enroll") is False
        assert store.consume_emergency_code(new[1], u.id, "enroll") is True

    def test_codes_are_not_written_in_clear(self, tmp_path):
        path = tmp_path / "access.json"
        s = AccessStore(path=path, clock=FakeClock(), wall_clock=FakeWallClock())
        codes = s.generate_emergency_codes()
        text = path.read_text(encoding="utf-8")
        assert all(c not in text for c in codes)

    def test_pool_survives_a_reload(self, tmp_path):
        path = tmp_path / "access.json"
        s1 = AccessStore(path=path, clock=FakeClock(), wall_clock=FakeWallClock())
        u = s1.create_user("Ada", None, Role.owner, "1379")
        codes = s1.generate_emergency_codes()
        s2 = AccessStore(path=path, clock=FakeClock(), wall_clock=FakeWallClock())
        assert s2.consume_emergency_code(codes[0], u.id, "enroll") is True


class TestSetupCode:
    def test_setup_mode_until_an_owner_exists(self, store):
        assert store.setup_mode is True
        store.create_user("Ada", None, Role.owner, "1379")
        assert store.setup_mode is False

    def test_begin_setup_returns_a_stable_eight_digit_code(self, store):
        code = store.begin_setup()
        assert len(code) == 8 and code.isdigit()
        assert store.begin_setup() == code
        assert store.pending_setup_code == code

    def test_setup_code_verifies_and_is_not_stored_in_clear(self, tmp_path):
        path = tmp_path / "access.json"
        s = AccessStore(path=path, clock=FakeClock(), wall_clock=FakeWallClock())
        code = s.begin_setup()
        assert s.verify_setup_code(code) is True
        assert s.verify_setup_code("00000000") is False
        assert code not in path.read_text(encoding="utf-8")

    def test_setup_code_survives_a_reload_but_its_plaintext_does_not(self, tmp_path):
        path = tmp_path / "access.json"
        s1 = AccessStore(path=path, clock=FakeClock(), wall_clock=FakeWallClock())
        code = s1.begin_setup()
        s2 = AccessStore(path=path, clock=FakeClock(), wall_clock=FakeWallClock())
        assert s2.verify_setup_code(code) is True
        assert s2.pending_setup_code is None

    def test_finalize_invalidates_the_setup_code(self, store):
        code = store.begin_setup()
        store.create_user("Ada", None, Role.owner, "1379")
        assert store.verify_setup_code(code) is True
        store.finalize_setup()
        assert store.setup_finalized is True
        assert store.verify_setup_code(code) is False
        assert store.pending_setup_code is None


class TestTransfer:
    @pytest.fixture
    def seeded(self, tmp_path):
        wall = FakeWallClock()
        s = AccessStore(path=tmp_path / "access.json", clock=FakeClock(), wall_clock=wall)
        owner = s.create_user("Ada", "ada@example.com", Role.owner, "1379")
        tech = s.create_user("Tim", "tim@example.com", Role.tech, "2468")
        s.generate_emergency_codes()
        return s, owner, tech, wall

    def test_start_leaves_the_owner_in_control(self, seeded):
        s, owner, _, _ = seeded
        code = s.start_transfer(owner.id)
        assert len(code) == 8 and code.isdigit()
        assert s.owner().id == owner.id
        assert s.pending_transfer is not None
        assert s.pending_transfer["started_by_user_id"] == owner.id

    def test_transfer_code_verifies(self, seeded):
        s, owner, _, _ = seeded
        code = s.start_transfer(owner.id)
        assert s.verify_transfer_code(code) is True
        assert s.verify_transfer_code("00000000") is False

    def test_transfer_expires_after_seven_days(self, seeded):
        s, owner, _, wall = seeded
        code = s.start_transfer(owner.id)
        wall.advance(days=8)
        assert s.pending_transfer is None
        assert s.verify_transfer_code(code) is False
        assert s.owner().id == owner.id

    def test_cancel_restores_the_pending_state_to_null(self, seeded):
        s, owner, _, _ = seeded
        code = s.start_transfer(owner.id)
        s.cancel_transfer()
        assert s.pending_transfer is None
        assert s.verify_transfer_code(code) is False
        assert s.owner().id == owner.id

    def test_complete_swaps_the_owner_and_keeps_other_users(self, seeded):
        s, owner, tech, _ = seeded
        device, _ = s.create_device("Tablet", shared=True)
        s.trust_device(device.id, owner.id)
        session = s.create_session(owner.id, device.id)
        s.start_transfer(owner.id)
        new_owner = s.complete_transfer("Bea", "bea@example.com", "9042")
        assert s.owner().id == new_owner.id
        assert s.get_user(owner.id) is None
        assert s.get_user(tech.id) is not None
        assert owner.id not in s.devices[device.id].trusted_user_ids
        assert s.resolve_session(session) is None
        assert s.unused_emergency_code_count() == 0
        assert s.pending_transfer is None

    def test_complete_without_a_pending_transfer_is_refused(self, seeded):
        s, _, _, _ = seeded
        with pytest.raises(AccessError):
            s.complete_transfer("Bea", None, "9042")


class TestMachineReport:
    def test_report_names_users_devices_and_code_count(self, store):
        from config.config_model import ConfigModel

        owner = store.create_user("Ada", "ada@example.com", Role.owner, "1379")
        store.create_user("Lee", "lee@example.com", Role.loader, "2468")
        device, _ = store.create_device("Cabinet tablet", shared=True)
        store.trust_device(device.id, owner.id)
        store.generate_emergency_codes()
        report = store.machine_report(ConfigModel())
        assert "Ada" in report
        assert "Lee" in report
        assert "loader" in report
        assert "Cabinet tablet" in report
        assert "20" in report

    def test_report_never_contains_a_hash(self, store):
        from config.config_model import ConfigModel

        store.create_user("Ada", "ada@example.com", Role.owner, "1379")
        report = store.machine_report(ConfigModel())
        assert "scrypt" not in report
        assert "pin_hash" not in report
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_access.py -v -k "EmergencyCodes or SetupCode or Transfer or MachineReport"`
Expected: FAIL — `AttributeError: 'AccessStore' object has no attribute 'generate_emergency_codes'`.

- [ ] **Step 3: Implement**

Add the constants beside the others:

```python
EMERGENCY_CODE_COUNT = 20
CODE_DIGITS = 8
TRANSFER_TTL_DAYS = 7
```

In `AccessStore.__init__`, after the enroll-token dict, add `self._setup_plaintext: str | None = None`. In `load()`, after `self._raw_extra` is built, add:

```python
            self._emergency_codes = list(raw.get("emergency_codes", []))
            self._setup = dict(raw.get("setup", {}))
            self._pending_transfer = raw.get("pending_transfer") or None
            self._raw_extra = {
                k: v
                for k, v in raw.items()
                if k
                not in (
                    "users",
                    "devices",
                    "emergency_codes",
                    "setup",
                    "pending_transfer",
                )
            }
```

and initialise `self._emergency_codes: list[dict] = []`, `self._setup: dict = {}`, `self._pending_transfer: dict | None = None` at the top of `load()` (beside `self.users = {}`), so a missing or corrupt file leaves them empty. Add the three keys to `_document()`:

```python
        doc["emergency_codes"] = [dict(c) for c in self._emergency_codes]
        doc["setup"] = dict(self._setup)
        doc["pending_transfer"] = self._pending_transfer
```

Append these methods to `AccessStore`:

```python
    # --- emergency codes ---

    def generate_emergency_codes(self, count: int = EMERGENCY_CODE_COUNT) -> list[str]:
        """Replace the whole pool, used or not. Plaintexts are returned once."""
        codes: list[str] = []
        while len(codes) < count:
            code = generate_code(CODE_DIGITS)
            if code not in codes:
                codes.append(code)
        self._emergency_codes = [
            {
                "code_hash": hash_secret(c),
                "used_at": None,
                "used_by_user_id": None,
                "used_for": None,
            }
            for c in codes
        ]
        self.save()
        logger.info(f"AccessStore: generated {count} emergency codes")
        return codes

    def unused_emergency_code_count(self) -> int:
        return sum(1 for c in self._emergency_codes if c["used_at"] is None)

    def consume_emergency_code(self, code: str, user_id: str, used_for: str) -> bool:
        """Mark an unused code used. False when it is unknown or already spent."""
        for entry in self._emergency_codes:
            if entry["used_at"] is None and verify_secret(code, entry["code_hash"]):
                entry["used_at"] = self._stamp()
                entry["used_by_user_id"] = user_id
                entry["used_for"] = used_for
                self.save()
                return True
        return False

    # --- setup code ---

    @property
    def setup_mode(self) -> bool:
        return self.owner() is None

    @property
    def setup_finalized(self) -> bool:
        return bool(self._setup.get("finalized"))

    @property
    def pending_setup_code(self) -> str | None:
        """The plaintext, held in memory only while setup is unfinished."""
        return self._setup_plaintext

    def begin_setup(self) -> str:
        """Generate (or return) the setup code that unlocks the wizard.

        Only someone at the machine — reading the startup log or the customer
        display — can see it, so a remote stranger cannot claim the machine.
        """
        if self._setup_plaintext and self._setup.get("setup_code_hash"):
            return self._setup_plaintext
        code = generate_code(CODE_DIGITS)
        self._setup = {"setup_code_hash": hash_secret(code), "finalized": False}
        self._setup_plaintext = code
        self.save()
        return code

    def verify_setup_code(self, code: str) -> bool:
        stored = self._setup.get("setup_code_hash")
        if not stored or self.setup_finalized:
            return False
        return verify_secret(code, stored)

    def finalize_setup(self) -> None:
        self._setup = {"setup_code_hash": None, "finalized": True}
        self._setup_plaintext = None
        self.save()

    # --- ownership transfer ---

    @property
    def pending_transfer(self) -> dict | None:
        """The live transfer, or None once it has expired."""
        pending = self._pending_transfer
        if pending is None:
            return None
        try:
            expires = datetime.fromisoformat(pending["expires_at"])
        except (KeyError, ValueError):
            return None
        if self._wall() > expires:
            return None
        return pending

    def start_transfer(self, started_by_user_id: str) -> str:
        """Record a pending transfer and return its 8-digit code, shown once.

        Nothing else changes: the current owner stays in control until the
        incoming owner completes the wizard (spec §3.3).
        """
        self._require_user(started_by_user_id)
        code = generate_code(CODE_DIGITS)
        now = self._wall()
        self._pending_transfer = {
            "transfer_code_hash": hash_secret(code),
            "started_at": now.isoformat(),
            "expires_at": (now + timedelta(days=TRANSFER_TTL_DAYS)).isoformat(),
            "started_by_user_id": started_by_user_id,
        }
        self.save()
        logger.warning("AccessStore: ownership transfer started")
        return code

    def verify_transfer_code(self, code: str) -> bool:
        pending = self.pending_transfer
        if pending is None:
            return False
        return verify_secret(code, pending["transfer_code_hash"])

    def cancel_transfer(self) -> None:
        self._pending_transfer = None
        self.save()

    def complete_transfer(self, name: str, email: str | None, pin: str) -> User:
        """Swap the owner in one write.

        Creates the new owner, deletes the old one (and their device trust),
        deletes every emergency code, ends every session, and clears the
        pending transfer. Other users are retained for the review step.
        """
        if self.pending_transfer is None:
            raise AccessError("no pending ownership transfer")
        old_owner = self.owner()
        pin_hash, pin_salt = hash_pin(pin)
        new_owner = User(
            id=str(uuid.uuid4()),
            name=name,
            email=email or None,
            role=Role.owner,
            pin_hash=pin_hash,
            pin_salt=pin_salt,
            created_at=self._stamp(),
        )
        if old_owner is not None:
            del self.users[old_owner.id]
            for device in self.devices.values():
                if old_owner.id in device.trusted_user_ids:
                    device.trusted_user_ids.remove(old_owner.id)
        self.users[new_owner.id] = new_owner
        self._emergency_codes = []
        self._pending_transfer = None
        self.save()
        self.end_all_sessions()
        logger.warning(f"AccessStore: ownership transferred to {name}")
        return new_owner

    # --- machine report ---

    def machine_report(self, config) -> str:
        """Plain-text summary of who can reach this machine (spec §3.4)."""
        owner = self.owner()
        lines = [
            "Ice-Colder machine access report",
            "================================",
            "",
            f"Machine id:   {config.machine_id}",
            f"Machine name: {config.physical.common_name}",
            f"Owner:        {owner.name if owner else '(none)'}"
            f" <{owner.email if owner and owner.email else 'no email'}>",
            "",
            "Users",
            "-----",
        ]
        for user in sorted(self.users.values(), key=lambda u: u.name):
            devices = sum(
                1 for d in self.devices.values() if user.id in d.trusted_user_ids
            )
            lines.append(
                f"  {user.name} ({user.role.value})"
                f" email={user.email or '-'}"
                f" disabled={user.disabled}"
                f" last_login={user.last_login_at or 'never'}"
                f" devices={devices}"
            )
        lines += ["", "Devices", "-------"]
        for device in sorted(self.devices.values(), key=lambda d: d.label):
            names = ", ".join(
                self.users[uid].name
                for uid in device.trusted_user_ids
                if uid in self.users
            )
            lines.append(
                f"  {device.label} shared={device.shared}"
                f" trusted=[{names}] last_seen={device.last_seen_at}"
            )
        lines += [
            "",
            f"Unused emergency codes: {self.unused_emergency_code_count()}",
        ]
        return "\n".join(lines)
```

`PhysicalDetails.common_name` is the machine's friendly name (`config/config_model.py:134`); there is no `machine_name` field. Do not invent one.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_access.py -v`
Expected: PASS — every test in the file.

- [ ] **Step 5: Lint and commit**

```bash
ruff check --fix .
ruff format .
git add services/access.py tests/test_access.py
git commit -m "feat(access): emergency codes, setup code, transfer and machine report"
```

---

### Task 7: `services/mailer.py` and the notifier delegating to it

**Spec:** §2.3 ("a new `services/mailer.py` … extracted from the SMTP code in `services/notifier.py`, which then calls it"), §6 (SMTP failure, 15-second timeout).

**Files:**
- Create: `services/mailer.py`
- Modify: `services/notifier.py`
- Test (create): `tests/test_mailer.py`

**Interfaces:**
- Consumes: `config.config_model.EmailGatewayConfig`.
- Produces:
  - `SMTP_TIMEOUT_SECONDS = 15.0`
  - `services.mailer.smtp_send(email_config, msg: EmailMessage) -> None` — blocking; `smtplib.SMTP(host, port, timeout=SMTP_TIMEOUT_SECONDS)`, `starttls()`, `login()`, `send_message()`
  - `async services.mailer.send_email(email_config, to: str, subject: str, body: str) -> bool` — builds the `EmailMessage`, runs `smtp_send` in the default executor, logs and returns `False` on any failure, `True` on success. Never raises.

`Notifier._send_email` keeps its name, signature and log lines; only its transport moves. `Notifier._smtp_send` is deleted and `services.mailer.smtp_send` used instead. `tests/test_notifier.py` must keep passing unchanged.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_mailer.py`:

```python
"""Tests for services/mailer.py."""

from email.message import EmailMessage
from unittest.mock import MagicMock, patch

import pytest

from config.config_model import EmailGatewayConfig
from services.mailer import SMTP_TIMEOUT_SECONDS, send_email, smtp_send


@pytest.fixture
def gateway():
    return EmailGatewayConfig(
        smtp_server="smtp.test.local",
        smtp_port=587,
        username="vmc@test.local",
        default_from="vmc@test.local",
    )


def test_smtp_send_uses_starttls_login_and_the_timeout(gateway):
    msg = EmailMessage()
    with patch("services.mailer.smtplib.SMTP") as smtp:
        server = smtp.return_value.__enter__.return_value
        smtp_send(gateway, msg)
    smtp.assert_called_once_with(
        "smtp.test.local", 587, timeout=SMTP_TIMEOUT_SECONDS
    )
    server.starttls.assert_called_once()
    server.login.assert_called_once()
    server.send_message.assert_called_once_with(msg)


async def test_send_email_builds_the_message_and_reports_success(gateway):
    sent = {}

    def fake_send(cfg, msg):
        sent["to"] = msg["To"]
        sent["from"] = msg["From"]
        sent["subject"] = msg["Subject"]
        sent["body"] = msg.get_content()

    with patch("services.mailer.smtp_send", side_effect=fake_send):
        ok = await send_email(gateway, "ada@example.com", "Your code", "123456")

    assert ok is True
    assert sent["to"] == "ada@example.com"
    assert sent["from"] == "vmc@test.local"
    assert sent["subject"] == "Your code"
    assert "123456" in sent["body"]


async def test_send_email_swallows_failures_and_returns_false(gateway, caplog):
    with patch("services.mailer.smtp_send", side_effect=OSError("no route to host")):
        ok = await send_email(gateway, "ada@example.com", "Your code", "123456")
    assert ok is False
    assert "no route to host" in caplog.text


async def test_send_email_never_blocks_the_loop(gateway):
    """The blocking send must go through an executor, not run inline."""
    calls = []

    def fake_send(cfg, msg):
        calls.append(MagicMock())

    with patch("services.mailer.smtp_send", side_effect=fake_send):
        assert await send_email(gateway, "a@b.c", "s", "b") is True
    assert len(calls) == 1
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_mailer.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'services.mailer'`.

- [ ] **Step 3: Implement**

Create `services/mailer.py`:

```python
"""One place that sends email, so nothing in the app blocks on SMTP.

Extracted from services/notifier.py, which now calls it. OTP delivery and the
machine report use it too. A send never raises: the caller gets False and the
UI offers the offline path instead (spec §6).
"""

import asyncio
import smtplib
from email.message import EmailMessage

from loguru import logger

SMTP_TIMEOUT_SECONDS = 15.0


def smtp_send(email_config, msg: EmailMessage) -> None:
    """Blocking SMTP send. Call through send_email, or from an executor."""
    with smtplib.SMTP(
        email_config.smtp_server, email_config.smtp_port, timeout=SMTP_TIMEOUT_SECONDS
    ) as server:
        server.starttls()
        server.login(email_config.username, email_config.password.get_secret_value())
        server.send_message(msg)


async def send_email(email_config, to: str, subject: str, body: str) -> bool:
    """Send one plain-text email in a thread. True on success, False on any failure."""
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = email_config.default_from
    msg["To"] = to
    msg.set_content(body)

    loop = asyncio.get_running_loop()
    try:
        await loop.run_in_executor(None, smtp_send, email_config, msg)
    except Exception as e:
        logger.error(f"mailer: send to {to} failed: {e}")
        return False
    logger.info(f"mailer: email sent to {to}")
    return True
```

In `services/notifier.py`: delete `import smtplib`, delete the `_smtp_send` staticmethod, add `from services.mailer import smtp_send`, and change the executor call in `_send_email` from `self._smtp_send` to `smtp_send`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_mailer.py tests/test_notifier.py -v`
Expected: PASS — 4 new plus the 4 existing notifier tests, unchanged.

- [ ] **Step 5: Lint and commit**

```bash
ruff check --fix .
ruff format .
git add services/mailer.py services/notifier.py tests/test_mailer.py
git commit -m "feat(access): services/mailer.py; notifier delegates its SMTP send"
```

---

### Task 8: `web_interface/auth.py` — cookies, session resolution, `require()`

**Spec:** §2.1 (cookies), §4 (enforcement), §6 (corrupt file, dead session).

**Files:**
- Rewrite: `web_interface/auth.py`
- Test (create): `tests/test_web_auth.py`

**Interfaces:**
- Consumes: `services.access` (`AccessStore`, `Backoff`, `Permission`, `ROLE_PERMISSIONS`, `Role`, `Session`, `User`).
- Produces:
  - Cookie names `DEVICE_COOKIE = "vmc_device"`, `SESSION_COOKIE = "vmc_session"`, `ENROLL_COOKIE = "vmc_enroll"`; `DEVICE_COOKIE_MAX_AGE = 365 * 24 * 3600`, `ENROLL_COOKIE_MAX_AGE = 600`
  - `set_cookie(response, request, name, value, *, max_age: int | None, backoff: Backoff) -> None` — `HttpOnly`, `SameSite=Lax`, `Path=/`, `Secure` from `backoff.is_https(request)`
  - `clear_cookie(response, name) -> None`
  - `@dataclass Principal`: `user: User`, `session: Session`, `perms: frozenset[Permission]`
  - `current_principal(request) -> Principal | None` — resolves `vmc_session` through the module's store, `None` when absent or expired
  - `require(*permissions: Permission)` — returns a FastAPI dependency that yields the `Principal`. For a page request (no `HX-Request` header) an unauthenticated caller gets `HTTPException(303, headers={"Location": "/login"})`; for an HTMX request it gets `HTTPException(401, headers={"HX-Redirect": "/login"})`. A resolved principal lacking any listed permission gets `403`.
  - `template_context(request, **extra) -> dict` — always carries `request`, `current_user` (the `User` or `None`) and `perms` (a `frozenset[Permission]`, empty when anonymous), plus `extra`
  - `set_access_store(store: AccessStore) -> None`, `get_access_store() -> AccessStore | None`, and the module-level `backoff = Backoff()`
  - `client_key(request) -> str` — the stored device id when `vmc_device` resolves to a device record, else `backoff.client_ip(request)` (spec §2.4)
  - `is_trusted_client(request, user_id: str) -> bool` — True when `vmc_device` resolves to a device whose `trusted_user_ids` contains `user_id`

**Keep the existing `LoginLimiter` class at the bottom of the file, unchanged**, with a comment saying Task 11 deletes it. `web_interface/routes.py` still imports it and `main.py` still calls `routes.login_limiter.set_trusted_proxies(...)`; removing it now would break every import in the app and is not this task's deliverable. `tests/test_login_limiter.py` must keep passing after this task.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_web_auth.py`:

```python
"""Unit tests for web_interface/auth.py: cookies, client keying, require()."""

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from services.access import AccessStore, Permission, Role
from web_interface import auth


@pytest.fixture
def store(tmp_path):
    s = AccessStore(path=tmp_path / "access.json")
    auth.set_access_store(s)
    auth.backoff.set_trusted_proxies([])
    yield s
    auth.set_access_store(None)


@pytest.fixture
def app(store):
    api = FastAPI()

    @api.get("/open")
    def open_page(principal=Depends(auth.require(Permission.view_status))):
        return {"user": principal.user.name}

    @api.get("/secret")
    def secret_page(principal=Depends(auth.require(Permission.edit_secrets))):
        return {"ok": True}

    return api


def _login(store, client, role=Role.owner, shared=False):
    user = store.create_user("Ada", None, role, "1379")
    device, token = store.create_device("Test device", shared=shared)
    store.trust_device(device.id, user.id)
    session = store.create_session(user.id, device.id)
    client.cookies.set(auth.DEVICE_COOKIE, token)
    client.cookies.set(auth.SESSION_COOKIE, session)
    return user, device


class TestRequire:
    def test_page_request_without_a_session_redirects_to_login(self, app):
        with TestClient(app, follow_redirects=False) as c:
            resp = c.get("/open")
        assert resp.status_code == 303
        assert resp.headers["location"] == "/login"

    def test_htmx_request_without_a_session_gets_hx_redirect(self, app):
        with TestClient(app) as c:
            resp = c.get("/open", headers={"HX-Request": "true"})
        assert resp.status_code == 401
        assert resp.headers["hx-redirect"] == "/login"

    def test_permitted_role_passes(self, app, store):
        with TestClient(app) as c:
            _login(store, c, Role.owner)
            resp = c.get("/open")
        assert resp.status_code == 200
        assert resp.json()["user"] == "Ada"

    def test_missing_permission_is_403(self, app, store):
        with TestClient(app) as c:
            _login(store, c, Role.loader)
            resp = c.get("/secret")
        assert resp.status_code == 403

    def test_expired_session_redirects_not_403(self, app, store):
        with TestClient(app, follow_redirects=False) as c:
            _login(store, c, Role.owner)
            store.end_all_sessions()
            resp = c.get("/open")
        assert resp.status_code == 303


class TestClientKeying:
    def test_resolved_device_cookie_keys_on_the_device_id(self, store):
        device, token = store.create_device("Tablet", shared=True)
        request = _fake_request(cookies={auth.DEVICE_COOKIE: token})
        assert auth.client_key(request) == device.id

    def test_unresolvable_cookie_falls_back_to_the_ip(self, store):
        request = _fake_request(cookies={auth.DEVICE_COOKIE: "bogus"}, peer="10.1.2.3")
        assert auth.client_key(request) == "10.1.2.3"

    def test_no_cookie_falls_back_to_the_ip(self, store):
        assert auth.client_key(_fake_request(peer="10.1.2.3")) == "10.1.2.3"

    def test_is_trusted_client(self, store):
        user = store.create_user("Ada", None, Role.owner, "1379")
        device, token = store.create_device("Tablet", shared=True)
        request = _fake_request(cookies={auth.DEVICE_COOKIE: token})
        assert auth.is_trusted_client(request, user.id) is False
        store.trust_device(device.id, user.id)
        assert auth.is_trusted_client(request, user.id) is True


class _FakeUrl:
    def __init__(self, scheme):
        self.scheme = scheme


class _FakeClient:
    def __init__(self, host):
        self.host = host


class _FakeRequest:
    def __init__(self, cookies, peer, scheme):
        self.cookies = cookies
        self.client = _FakeClient(peer)
        self.headers = {}
        self.url = _FakeUrl(scheme)


def _fake_request(cookies=None, peer="127.0.0.1", scheme="http"):
    return _FakeRequest(cookies or {}, peer, scheme)


class TestCookieFlags:
    def test_http_request_gets_no_secure_flag(self, app, store):
        api = app

        @api.get("/setcookie")
        def setcookie(request: __import__("fastapi").Request):
            from fastapi.responses import JSONResponse

            resp = JSONResponse({})
            auth.set_cookie(
                resp, request, auth.SESSION_COOKIE, "abc", max_age=None,
                backoff=auth.backoff,
            )
            return resp

        with TestClient(api) as c:
            resp = c.get("/setcookie")
        header = resp.headers["set-cookie"]
        assert "HttpOnly" in header
        assert "SameSite=lax" in header.replace("SameSite=Lax", "SameSite=lax")
        assert "Secure" not in header
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_web_auth.py -v`
Expected: FAIL — `AttributeError: module 'web_interface.auth' has no attribute 'set_access_store'`.

- [ ] **Step 3: Implement**

Put this at the **top** of `web_interface/auth.py`, above the existing `LoginLimiter` class (which stays until Task 11). Keep the module's existing `ipaddress` / `time` / `deque` / `logger` imports — `LoginLimiter` still needs them.

```python
"""Session auth for the dashboard: cookies, session resolution, permissions.

The state lives in services/access.py's AccessStore; this module is the FastAPI
side of it. HTTP Basic auth goes away in Task 11, and with it the LoginLimiter
below — a Backoff instance here replaces the limiter and main.py hands it the
trusted proxies.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from fastapi import HTTPException, Request

from services.access import (
    ROLE_PERMISSIONS,
    AccessStore,
    Backoff,
    Permission,
    Session,
    User,
)

DEVICE_COOKIE = "vmc_device"
SESSION_COOKIE = "vmc_session"
ENROLL_COOKIE = "vmc_enroll"
DEVICE_COOKIE_MAX_AGE = 365 * 24 * 3600
ENROLL_COOKIE_MAX_AGE = 600

backoff = Backoff()

_store: Optional[AccessStore] = None


def set_access_store(store: Optional[AccessStore]) -> None:
    global _store
    _store = store


def get_access_store() -> Optional[AccessStore]:
    return _store


# --- cookies ---


def set_cookie(
    response,
    request: Request,
    name: str,
    value: str,
    *,
    max_age: int | None,
    backoff: Backoff = backoff,
) -> None:
    """HttpOnly, SameSite=Lax, Path=/; Secure only when the browser hop was TLS."""
    response.set_cookie(
        key=name,
        value=value,
        max_age=max_age,
        httponly=True,
        samesite="lax",
        path="/",
        secure=backoff.is_https(request),
    )


def clear_cookie(response, name: str) -> None:
    response.delete_cookie(key=name, path="/")


# --- identity ---


@dataclass
class Principal:
    user: User
    session: Session
    perms: frozenset[Permission]


def current_principal(request: Request) -> Principal | None:
    """Resolve vmc_session to a live user, or None."""
    store = _store
    if store is None or store.corrupt:
        return None
    session = store.resolve_session(request.cookies.get(SESSION_COOKIE))
    if session is None:
        return None
    user = store.get_user(session.user_id)
    if user is None or user.disabled:
        return None
    return Principal(user=user, session=session, perms=ROLE_PERMISSIONS[user.role])


def client_key(request: Request) -> str:
    """The back-off client key: the stored device id, else the client IP.

    A cookie that does not resolve to a device record is ignored, so an
    attacker minting random cookie values gets one counter per IP, not one
    per cookie (spec §2.4).
    """
    store = _store
    if store is not None and not store.corrupt:
        device = store.device_for_token(request.cookies.get(DEVICE_COOKIE))
        if device is not None:
            return device.id
    return backoff.client_ip(request)


def is_trusted_client(request: Request, user_id: str) -> bool:
    """True when this browser's device record already trusts *user_id*."""
    store = _store
    if store is None or store.corrupt:
        return False
    device = store.device_for_token(request.cookies.get(DEVICE_COOKIE))
    return device is not None and user_id in device.trusted_user_ids


def _unauthenticated(request: Request) -> HTTPException:
    """303 to /login for a page; 401 + HX-Redirect so a partial swap navigates."""
    if request.headers.get("HX-Request") == "true":
        return HTTPException(
            status_code=401, detail="Login required", headers={"HX-Redirect": "/login"}
        )
    return HTTPException(
        status_code=303, detail="Login required", headers={"Location": "/login"}
    )


def require(*permissions: Permission):
    """FastAPI dependency: a live session holding every listed permission."""

    def dependency(request: Request) -> Principal:
        principal = current_principal(request)
        if principal is None:
            raise _unauthenticated(request)
        if any(p not in principal.perms for p in permissions):
            raise HTTPException(status_code=403, detail="Not permitted")
        return principal

    return dependency


def template_context(request: Request, **extra) -> dict:
    """Every template gets current_user and perms, so it never renders a
    button the server would refuse. Server-side checks remain the authority."""
    principal = current_principal(request)
    ctx = {
        "request": request,
        "current_user": principal.user if principal else None,
        "perms": principal.perms if principal else frozenset(),
    }
    ctx.update(extra)
    return ctx
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_web_auth.py -v`
Expected: PASS — 10 tests.
Then run the full suite: `uv run pytest`. Everything that passed before this task must still pass — `LoginLimiter` is untouched and nothing else imports the new names yet.

- [ ] **Step 5: Lint and commit**

```bash
ruff check --fix .
ruff format .
git add web_interface/auth.py tests/test_web_auth.py
git commit -m "feat(access): session cookies, require() and template context"
```

---

### Task 9: Login page and `POST /login`

**Spec:** §2.2, §2.1 (cookies).

**Assumption recorded as a deviation:** the login, enrollment and setup forms post with HTMX (`hx-post`), because spec §4 keeps `require_htmx` on *every* POST and cookie auth makes that guard load-bearing. HTMX and Tailwind still come from a CDN in part 1; vendoring them for the offline tablet is explicitly part 2's work (v2 shell spec). Reversible.

**Files:**
- Modify: `web_interface/routes.py`
- Create: `web_interface/templates/login.html`, `web_interface/templates/partials/keypad.html`
- Test: `tests/test_web_routes.py`

**Interfaces:**
- Consumes: `web_interface.auth` (`require`, `template_context`, `set_access_store`, `client_key`, `is_trusted_client`, `backoff`, cookie names and helpers), `services.access.AccessStore`.
- Produces:
  - `routes.access_store: AccessStore | None` and `routes.set_access_store(store) -> None` (mirrors the other `set_*` setters; also calls `web_interface.auth.set_access_store`)
  - `GET /login` — full page, no auth. Redirects (303) to `/setup` while `store.setup_mode` is true.
  - `POST /login` — form fields `user_id`, `pin`; `require_htmx`; returns `partials/keypad.html`
  - On success: `HTMLResponse` with header `HX-Redirect: /`, `vmc_session` set, `vmc_device` re-set with a fresh 365-day max-age
  - On a wrong PIN: status 200, the keypad partial with a generic `error`
  - While backed off: status 429, header `Retry-After`, the keypad partial with `wait_seconds`
  - Template variables for `partials/keypad.html`: `users` (list of `User`), `selected_user_id: str | None`, `error: str | None`, `wait_seconds: int | None`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_web_routes.py`:

```python
class TestLogin:
    @pytest.fixture
    def public(self, tmp_path):
        """A client with a seeded AccessStore and no session cookies."""
        from services.access import AccessStore, Role
        from web_interface import auth as web_auth

        cfg = ConfigModel()
        store = AccessStore(path=tmp_path / "access.json")
        owner = store.create_user("Ada", "ada@example.com", Role.owner, "1379")
        routes.set_config_object(cfg)
        routes.set_access_store(store)
        with TestClient(app, follow_redirects=False) as c:
            c.headers["HX-Request"] = "true"
            yield c, store, owner
        routes.set_access_store(None)
        web_auth.backoff.set_trusted_proxies([])

    def test_login_page_lists_enabled_users_only(self, public):
        from services.access import Role

        c, store, owner = public
        hidden = store.create_user("Hidden", None, Role.tech, "2468")
        store.set_user_disabled(hidden.id, True)
        resp = c.get("/login", headers={})
        assert resp.status_code == 200
        assert "Ada" in resp.text
        assert "Hidden" not in resp.text

    def test_correct_pin_on_an_untrusted_browser_shows_enrollment(self, public):
        c, store, owner = public
        resp = c.post("/login", data={"user_id": owner.id, "pin": "1379"})
        assert resp.status_code == 200
        assert "code" in resp.text.lower()
        assert "hx-redirect" not in {k.lower() for k in resp.headers}
        assert c.cookies.get("vmc_enroll")

    def test_correct_pin_on_a_trusted_device_logs_in(self, public):
        from web_interface import auth as web_auth

        c, store, owner = public
        device, token = store.create_device("Tablet", shared=True)
        store.trust_device(device.id, owner.id)
        c.cookies.set(web_auth.DEVICE_COOKIE, token)
        resp = c.post("/login", data={"user_id": owner.id, "pin": "1379"})
        assert resp.status_code == 200
        assert resp.headers["hx-redirect"] == "/"
        assert c.cookies.get("vmc_session")
        assert store.get_user(owner.id).last_login_at is not None

    def test_wrong_pin_returns_a_generic_message_and_no_session(self, public):
        c, store, owner = public
        resp = c.post("/login", data={"user_id": owner.id, "pin": "9999"})
        assert resp.status_code == 200
        assert "wrong pin" in resp.text.lower()
        assert not c.cookies.get("vmc_session")

    def test_unknown_user_looks_identical_to_a_wrong_pin(self, public):
        c, store, owner = public
        wrong = c.post("/login", data={"user_id": owner.id, "pin": "9999"})
        unknown = c.post("/login", data={"user_id": "no-such-user", "pin": "9999"})
        assert unknown.status_code == wrong.status_code
        assert "wrong pin" in unknown.text.lower()

    def test_disabled_user_cannot_log_in(self, public):
        c, store, owner = public
        store.set_user_disabled(owner.id, True)
        resp = c.post("/login", data={"user_id": owner.id, "pin": "1379"})
        assert not c.cookies.get("vmc_session")
        assert "wrong pin" in resp.text.lower()

    def test_repeated_failures_back_off_with_429_and_retry_after(self, public):
        c, store, owner = public
        for _ in range(3):
            c.post("/login", data={"user_id": owner.id, "pin": "9999"})
        resp = c.post("/login", data={"user_id": owner.id, "pin": "9999"})
        assert resp.status_code == 429
        assert int(resp.headers["retry-after"]) >= 1

    def test_login_post_without_the_htmx_header_is_forbidden(self, public):
        c, store, owner = public
        resp = c.post(
            "/login", data={"user_id": owner.id, "pin": "1379"}, headers={"HX-Request": ""}
        )
        assert resp.status_code == 403

    def test_login_page_redirects_to_setup_when_no_owner_exists(self, tmp_path):
        from services.access import AccessStore

        routes.set_config_object(ConfigModel())
        routes.set_access_store(AccessStore(path=tmp_path / "access.json"))
        with TestClient(app, follow_redirects=False) as c:
            resp = c.get("/login")
        assert resp.status_code == 303
        assert resp.headers["location"] == "/setup"
        routes.set_access_store(None)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_web_routes.py -v -k TestLogin`
Expected: FAIL — `AttributeError: module 'web_interface.routes' has no attribute 'set_access_store'`.

- [ ] **Step 3: Implement**

In `web_interface/routes.py`, beside the other setters:

```python
from services.access import AccessStore
from web_interface import auth as web_auth

access_store: AccessStore | None = None


def set_access_store(store: AccessStore | None) -> None:
    global access_store
    access_store = store
    web_auth.set_access_store(store)
```

Inside `attach_routes`, before the gated `router` is built, add a public router and register it at the end alongside it (`app.include_router(public)`):

```python
    public = APIRouter()

    def _keypad(request: Request, *, selected_user_id=None, error=None,
                wait_seconds=None, status_code=200, headers=None):
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
            {"request": request, "users": access_store.enabled_users(),
             "selected_user_id": None, "error": None, "wait_seconds": None},
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

        remaining = web_auth.backoff.check("pin", user_id, client, trusted=trusted)
        if remaining is not None:
            return _keypad(
                request,
                selected_user_id=user_id,
                wait_seconds=int(remaining) + 1,
                status_code=429,
                headers={"Retry-After": str(int(remaining) + 1)},
            )

        # Identical response for a wrong PIN, an unknown user and a disabled
        # one: the picker already leaks names, nothing else should leak state.
        if not access_store.verify_user_pin(user_id, pin):
            web_auth.backoff.record_failure("pin", user_id, client, trusted=trusted)
            return _keypad(request, selected_user_id=user_id, error="Wrong PIN")

        web_auth.backoff.record_success("pin", user_id, client, trusted=trusted)
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
```

`_enrollment_response` lands in Task 10; for this task define it as a stub that renders `enroll.html` with `error=None` and sets the `vmc_enroll` cookie:

```python
    def _enrollment_response(request: Request, user_id: str, error: str | None = None):
        """Second factor: the PIN is proven, now prove the device (spec §2.3)."""
        token = access_store.issue_enroll_token(user_id, web_auth.client_key(request))
        user = access_store.get_user(user_id)
        gateway = config.communication.email_gateway if config else None
        resp = templates.TemplateResponse(
            "enroll.html",
            {
                "request": request,
                "user": user,
                "error": error,
                "can_email": bool(
                    user and user.email and gateway and gateway.is_configured
                ),
            },
        )
        web_auth.set_cookie(
            resp,
            request,
            web_auth.ENROLL_COOKIE,
            token,
            max_age=web_auth.ENROLL_COOKIE_MAX_AGE,
        )
        return resp
```

Add `RedirectResponse` to the `fastapi.responses` import.

Create `web_interface/templates/partials/keypad.html`:

```html
{# web_interface/templates/partials/keypad.html — PIN entry, swapped into #login-form #}
<form id="login-form"
      hx-post="/login"
      hx-target="#login-form"
      hx-swap="outerHTML"
      class="space-y-4">

  <label for="user_id" class="block text-sm font-medium text-gray-700">Who are you?</label>
  <select id="user_id" name="user_id" required
          class="w-full px-4 py-4 text-lg border border-gray-300 rounded-xl">
    {% for u in users %}
      <option value="{{ u.id }}" {{ "selected" if selected_user_id == u.id }}>{{ u.name }}</option>
    {% endfor %}
  </select>

  <label for="pin" class="block text-sm font-medium text-gray-700">PIN</label>
  <input type="password" id="pin" name="pin" inputmode="numeric" pattern="[0-9]*"
         autocomplete="off" required
         class="w-full px-4 py-4 text-2xl tracking-widest text-center border border-gray-300 rounded-xl">

  <div class="grid grid-cols-3 gap-2">
    {% for d in ["1","2","3","4","5","6","7","8","9"] %}
      <button type="button" data-digit="{{ d }}"
              class="py-5 text-2xl bg-gray-100 rounded-xl">{{ d }}</button>
    {% endfor %}
    <button type="button" data-clear="1" class="py-5 text-lg bg-gray-100 rounded-xl">Clear</button>
    <button type="button" data-digit="0" class="py-5 text-2xl bg-gray-100 rounded-xl">0</button>
    <button type="submit" class="py-5 text-lg bg-blue-600 text-white rounded-xl">Enter</button>
  </div>

  {% if error %}
    <p class="text-red-600 text-sm" role="alert">{{ error }}</p>
  {% endif %}
  {% if wait_seconds %}
    <p class="text-amber-700 text-sm" role="alert">
      Too many attempts. Try again in <span id="countdown">{{ wait_seconds }}</span> s.
    </p>
  {% endif %}

  <script>
    (function () {
      var form = document.getElementById('login-form');
      var pin = form.querySelector('#pin');
      form.querySelectorAll('[data-digit]').forEach(function (b) {
        b.addEventListener('click', function () { pin.value += b.dataset.digit; });
      });
      form.querySelectorAll('[data-clear]').forEach(function (b) {
        b.addEventListener('click', function () { pin.value = ''; });
      });
      var c = document.getElementById('countdown');
      if (c) {
        var left = parseInt(c.textContent, 10);
        var t = setInterval(function () {
          left -= 1;
          c.textContent = left;
          if (left <= 0) { clearInterval(t); }
        }, 1000);
      }
    })();
  </script>
</form>
```

Create `web_interface/templates/login.html`:

```html
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Sign in — Vending Machine</title>
  <script src="https://unpkg.com/htmx.org@1.9.10"></script>
  <script src="https://cdn.tailwindcss.com"></script>
</head>
<body class="bg-slate-50 text-gray-900 font-sans min-h-screen flex items-center justify-center">
  <main class="w-full max-w-sm p-6 space-y-6">
    <h1 class="text-xl font-semibold text-center">Sign in</h1>
    {% include "partials/keypad.html" %}
  </main>
</body>
</html>
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_web_routes.py -v -k TestLogin`
Expected: PASS — 9 tests.

- [ ] **Step 5: Lint and commit**

```bash
ruff check --fix .
ruff format .
git add web_interface/routes.py web_interface/templates/login.html web_interface/templates/partials/keypad.html tests/test_web_routes.py
git commit -m "feat(access): login page, PIN verification and back-off"
```

---

### Task 10: Enrollment page, `POST /login/enroll` and `POST /logout`

**Spec:** §2.3, §2.5, §6 (SMTP failure).

**Files:**
- Modify: `web_interface/routes.py`
- Create: `web_interface/templates/enroll.html`
- Test: `tests/test_web_routes.py`

**Interfaces:**
- Consumes: `_enrollment_response` (Task 9), `services.mailer.send_email` (Task 7), `AccessStore.issue_otp` / `verify_otp` / `consume_emergency_code` / `verify_setup_code` / `resolve_enroll_token` (Tasks 5–6).
- Produces:
  - `POST /login/enroll` — form field `code`; `require_htmx`. A 6-digit code is checked as an OTP; an 8-digit code is checked against the emergency pool, and — while setup is unfinished — against the setup code and the pending transfer code (spec §3.1 step 1, §3.3 step 4). On success: trust the device, create the session, clear `vmc_enroll`, respond `HX-Redirect: /`.
  - `POST /login/enroll/send` — issues and emails an OTP; `require_htmx`; re-renders `enroll.html` with a notice. Back-off kind `otp_send`, where **every send counts as a failure**.
  - `POST /logout` — `require_htmx`; ends the session, clears `vmc_session`, keeps `vmc_device`, responds `HX-Redirect: /login`.
  - When no `vmc_device` cookie exists as enrollment starts, `_enrollment_response` creates a device record (label `"New device"`, `shared=False`) and sets the cookie, so the pending OTP always has a device id to bind to.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_web_routes.py`:

```python
class TestEnrollment:
    @pytest.fixture
    def public(self, tmp_path):
        from services.access import AccessStore, Role
        from web_interface import auth as web_auth

        cfg = ConfigModel()
        store = AccessStore(path=tmp_path / "access.json")
        owner = store.create_user("Ada", "ada@example.com", Role.owner, "1379")
        store.generate_emergency_codes()
        routes.set_config_object(cfg)
        routes.set_access_store(store)
        with TestClient(app, follow_redirects=False) as c:
            c.headers["HX-Request"] = "true"
            yield c, store, owner, cfg
        routes.set_access_store(None)
        web_auth.backoff.set_trusted_proxies([])

    def test_enrollment_issues_a_device_cookie_immediately(self, public):
        c, store, owner, _ = public
        c.post("/login", data={"user_id": owner.id, "pin": "1379"})
        assert c.cookies.get("vmc_device")
        assert len(store.devices) == 1

    def test_emergency_code_enrolls_and_logs_in(self, public):
        c, store, owner, _ = public
        codes = store.generate_emergency_codes()
        c.post("/login", data={"user_id": owner.id, "pin": "1379"})
        resp = c.post("/login/enroll", data={"code": codes[0]})
        assert resp.headers["hx-redirect"] == "/"
        assert c.cookies.get("vmc_session")
        device = next(iter(store.devices.values()))
        assert owner.id in device.trusted_user_ids
        assert store.unused_emergency_code_count() == 19

    def test_emergency_code_works_with_no_smtp_configured(self, public):
        c, store, owner, cfg = public
        assert cfg.communication.email_gateway.is_configured is False
        codes = store.generate_emergency_codes()
        c.post("/login", data={"user_id": owner.id, "pin": "1379"})
        assert c.post("/login/enroll", data={"code": codes[0]}).headers["hx-redirect"] == "/"

    def test_a_used_emergency_code_is_refused(self, public):
        c, store, owner, _ = public
        codes = store.generate_emergency_codes()
        c.post("/login", data={"user_id": owner.id, "pin": "1379"})
        c.post("/login/enroll", data={"code": codes[0]})
        c.cookies.delete("vmc_session")
        c.post("/login", data={"user_id": owner.id, "pin": "1379"})
        resp = c.post("/login/enroll", data={"code": codes[0]})
        assert "hx-redirect" not in {k.lower() for k in resp.headers}

    def test_otp_path_with_a_stubbed_mailer(self, public, monkeypatch):
        c, store, owner, cfg = public
        cfg.communication.email_gateway.smtp_server = "smtp.real.local"
        sent = {}

        async def fake_send_email(gateway, to, subject, body):
            sent["to"] = to
            sent["body"] = body
            return True

        monkeypatch.setattr(routes, "send_email", fake_send_email)
        c.post("/login", data={"user_id": owner.id, "pin": "1379"})
        page = c.post("/login/enroll/send", data={})
        assert page.status_code == 200
        assert sent["to"] == "ada@example.com"
        code = "".join(ch for ch in sent["body"] if ch.isdigit())[-6:]
        resp = c.post("/login/enroll", data={"code": code})
        assert resp.headers["hx-redirect"] == "/"

    def test_smtp_failure_tells_the_user_to_use_an_emergency_code(
        self, public, monkeypatch
    ):
        c, store, owner, cfg = public
        cfg.communication.email_gateway.smtp_server = "smtp.real.local"

        async def fake_send_email(gateway, to, subject, body):
            return False

        monkeypatch.setattr(routes, "send_email", fake_send_email)
        c.post("/login", data={"user_id": owner.id, "pin": "1379"})
        page = c.post("/login/enroll/send", data={})
        assert "emergency code" in page.text.lower()

    def test_wrong_code_backs_off_with_429(self, public):
        c, store, owner, _ = public
        c.post("/login", data={"user_id": owner.id, "pin": "1379"})
        for _ in range(3):
            c.post("/login/enroll", data={"code": "00000000"})
        resp = c.post("/login/enroll", data={"code": "00000000"})
        assert resp.status_code == 429
        assert int(resp.headers["retry-after"]) >= 1

    def test_enroll_without_a_valid_enroll_cookie_is_refused(self, public):
        c, store, owner, _ = public
        codes = store.generate_emergency_codes()
        resp = c.post("/login/enroll", data={"code": codes[0]})
        assert resp.status_code in (401, 403)
        assert not c.cookies.get("vmc_session")

    def test_logout_ends_the_session_and_keeps_the_device(self, public):
        c, store, owner, _ = public
        codes = store.generate_emergency_codes()
        c.post("/login", data={"user_id": owner.id, "pin": "1379"})
        c.post("/login/enroll", data={"code": codes[0]})
        device_cookie = c.cookies.get("vmc_device")
        resp = c.post("/logout", data={})
        assert resp.headers["hx-redirect"] == "/login"
        assert not c.cookies.get("vmc_session")
        assert c.cookies.get("vmc_device") == device_cookie

    def test_locked_shared_session_resumes_with_pin_only(self, public):
        c, store, owner, _ = public
        codes = store.generate_emergency_codes()
        c.post("/login", data={"user_id": owner.id, "pin": "1379"})
        c.post("/login/enroll", data={"code": codes[0]})
        c.post("/logout", data={})
        resp = c.post("/login", data={"user_id": owner.id, "pin": "1379"})
        assert resp.headers["hx-redirect"] == "/"
        assert c.cookies.get("vmc_session")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_web_routes.py -v -k TestEnrollment`
Expected: FAIL — 404 on `/login/enroll`.

- [ ] **Step 3: Implement**

Add `from services.mailer import send_email` to `web_interface/routes.py` (imported by name so tests can monkeypatch `routes.send_email`).

Extend `_enrollment_response` from Task 9 so it creates a device when none exists — insert before `token = ...`:

```python
        device = access_store.device_for_token(
            request.cookies.get(web_auth.DEVICE_COOKIE)
        )
        new_device_token = None
        if device is None:
            device, new_device_token = access_store.create_device(
                "New device", shared=False
            )
```

and after the response is built, before `return resp`:

```python
        if new_device_token is not None:
            web_auth.set_cookie(
                resp,
                request,
                web_auth.DEVICE_COOKIE,
                new_device_token,
                max_age=web_auth.DEVICE_COOKIE_MAX_AGE,
            )
```

Add to the public router:

```python
    def _enroll_user_id(request: Request) -> str:
        """The user whose PIN this browser just proved, or 401."""
        user_id = access_store.resolve_enroll_token(
            request.cookies.get(web_auth.ENROLL_COOKIE), web_auth.client_key(request)
        )
        if user_id is None:
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
        if user is None or not user.email or not gateway.is_configured or device is None:
            return _enroll_page(
                request, user_id, error="Email is not available, use an emergency code"
            )
        code = access_store.issue_otp(user_id, device.id)
        ok = await send_email(
            gateway,
            user.email,
            "Vending machine sign-in code",
            f"Your one-time code is {code}. It expires in 10 minutes.",
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
            if not ok and not access_store.setup_finalized:
                # Until Done, the setup code and the transfer code also enroll
                # the owner, so a lost step-1 response cannot strand them
                # (spec §3.1 step 1, §3.3 step 4).
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
```

Add the shared renderer used above:

```python
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
```

Rewrite `_enrollment_response`'s body to build its page through `_enroll_page` so there is one template call. Import `OTP_DIGITS` from `services.access`.

Create `web_interface/templates/enroll.html`:

```html
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Trust this device — Vending Machine</title>
  <script src="https://unpkg.com/htmx.org@1.9.10"></script>
  <script src="https://cdn.tailwindcss.com"></script>
</head>
<body class="bg-slate-50 text-gray-900 font-sans min-h-screen flex items-center justify-center">
  <main class="w-full max-w-sm p-6 space-y-4">
    <h1 class="text-xl font-semibold">Trust this device</h1>
    <p class="text-sm text-gray-600">
      Hello {{ user.name }}. This browser has not been used here before.
      Enter a 6-digit emailed code or an 8-digit emergency code, once.
    </p>

    {% if can_email %}
      <button hx-post="/login/enroll/send" hx-target="body" hx-swap="outerHTML"
              class="w-full py-3 border border-gray-300 rounded-xl text-sm">
        Email me a code
      </button>
    {% endif %}

    <form hx-post="/login/enroll" hx-target="body" hx-swap="outerHTML" class="space-y-3">
      <label for="code" class="block text-sm font-medium text-gray-700">Code</label>
      <input type="text" id="code" name="code" inputmode="numeric" pattern="[0-9]*"
             autocomplete="one-time-code" required
             class="w-full px-4 py-4 text-2xl tracking-widest text-center border border-gray-300 rounded-xl">
      <button type="submit" class="w-full py-4 bg-blue-600 text-white rounded-xl">Continue</button>
    </form>

    {% if notice %}<p class="text-green-700 text-sm">{{ notice }}</p>{% endif %}
    {% if error %}<p class="text-red-600 text-sm" role="alert">{{ error }}</p>{% endif %}
    {% if wait_seconds %}
      <p class="text-amber-700 text-sm">Try again in {{ wait_seconds }} s.</p>
    {% endif %}
  </main>
</body>
</html>
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_web_routes.py -v -k "TestEnrollment or TestLogin"`
Expected: PASS — 19 tests.

- [ ] **Step 5: Lint and commit**

```bash
ruff check --fix .
ruff format .
git add web_interface/routes.py web_interface/templates/enroll.html tests/test_web_routes.py
git commit -m "feat(access): device enrollment by OTP or emergency code, and logout"
```

---

> **Style note for tasks 11–21.** These tasks were rewritten on 2026-09-25 to
> state interfaces and behavior rather than embedded implementations. Tasks
> 1–10 above still carry full code bodies; that style caused three transcribed
> defects (an unguarded `save()` in eleven methods, an enroll-token bound to
> the wrong client key, a test that did not exercise what it named). From here
> the implementer writes the code. The plan fixes *what* and *where*; the
> implementer and its reviewer own *how*, against the spec.
>
> A task below gives: the spec section, the files it may touch, the interface
> (exact names, signatures, routes, template variables), the behavior in
> prose, and the tests to write first. Code blocks appear only for wire
> formats and non-obvious algorithms. Everything named in an **Interfaces**
> block is a contract another task depends on — spell those names exactly.

### Task 11: Permissions on every existing route; HTTP Basic removed

**Spec:** §4 (enforcement), §7 (`login_as` helper, permission matrix test).

The largest task in the plan and the one the reviewer should read hardest. It touches four files and is not split further: the fixture rewrite and the dependency swap cannot land separately without leaving the suite red.

**Files:**
- Modify: `web_interface/routes.py`, `web_interface/auth.py`, `web_interface/templates/dashboard.html`, `web_interface/templates/partials/inventory_table.html`
- Modify: `main.py` (one line — see below)
- Test: `tests/test_web_routes.py`

**Route → permission map (authoritative):**

| Route | Permission |
|---|---|
| `GET /`, `/status`, `/kpi`, `/activity`, `/health`, `/inventory`, `/screen`, `/screen/body` | `view_status` |
| `GET /logs` | `view_logs` |
| `POST /faults/{key}/clear` | `clear_faults` |
| `POST /action/{command}` | `machine_controls` |
| `GET /inventory/new`, `POST /inventory/add`, `GET /inventory/copy/{sku}`, `POST /inventory/delete/{sku}` | `edit_catalog` |
| `GET /inventory/edit/{sku}`, `POST /inventory/update/{sku}` | `edit_catalog` (both split in Task 12) |
| `GET /config/machine`, `GET /config/contacts` | `edit_contacts` (spec §4: "`edit_contacts` — people and machine info") |
| `GET /config/payments`, `GET /config/comms` | `edit_secrets` (spec §4: "`edit_secrets` — payment, comms, MQTT settings") |

**Interfaces:**
- Consumes: `web_auth.require`, `web_auth.template_context`, `web_auth.Principal` (Task 8).
- Produces in `web_interface/routes.py`:
  - Every gated route carries `Depends(web_auth.require(Permission.x))` in its `dependencies` list, per the table. Where a route already depends on `require_htmx`, it lists both; `require_htmx` stays on every POST unchanged.
  - `require_auth`, `_basic_auth`, `login_limiter`, the `HTTPBasic` / `HTTPBasicCredentials` imports and the `LoginLimiter` import are **deleted**. The router is constructed with no default dependency.
  - Every gated route's template context comes from `web_auth.template_context(request, ...)` rather than a bare `{"request": request, ...}` dict. `_screen_context` starts from `template_context(request)` too.
- Produces in `web_interface/auth.py`: the `LoginLimiter` class is deleted, along with the imports only it used.
- Produces in `main.py`: the single `routes.login_limiter.set_trusted_proxies(...)` call becomes `web_auth.backoff.set_trusted_proxies(overrides.trusted_proxies)`, importing `from web_interface import auth as web_auth`. The rest of `main.py` is Task 20.
- Produces in `tests/test_web_routes.py` — these are contracts later tasks' tests use, so match the names exactly:
  - `sign_in(store, user, *, shared=False) -> TestClient` — mints a device, trusts *user* on it, opens a session, and returns a client carrying `vmc_device`, `vmc_session` and the `HX-Request: true` header.
  - `make_client(store, role=Role.owner, *, shared=False, name="Ada") -> tuple[TestClient, User]` — seeds a user of *role*, then `sign_in`.
  - Fixture `wired` — builds `ConfigModel`, `VMC`, `InventoryManager` and an `AccessStore` in `tmp_path`, **seeds the owner "Ada" (email `ada@example.com`, PIN `1379`) and calls `store.finalize_setup()`** so route tests are never in setup mode, wires all four into `routes` via the `set_*` functions, yields `(cfg, vmc, inv, store)`, and on teardown clears the access store and cancels `vmc._pending_tasks`.
  - Fixture `login_as` — `login_as(role=Role.owner, *, shared=False, name=None) -> TestClient`. `Role.owner` returns a client for the fixture's existing owner, because the store enforces one owner per machine; any other role seeds a fresh user. Closes every client it made on teardown.
  - Fixture `client` — `login_as(Role.owner)`. Every pre-existing test in the file runs through it unchanged.
  - Fixture `anonymous` — a `TestClient(app, follow_redirects=False)` with no cookies, against a `wired` store that does have an owner.

**Behavior to get right:**
- An unauthenticated page request (no `HX-Request` header) redirects 303 to `/login`; an unauthenticated HTMX request answers 401 with `HX-Redirect: /login`. Task 8 already implements this; this task only has to route through it.
- A resolved principal missing the route's permission gets 403, not a redirect.
- Templates must not render a control the server would refuse. Gate `dashboard.html`'s tab buttons and the three machine-control buttons on the permission their target route needs; gate `partials/inventory_table.html`'s Add/Copy/Delete controls on `edit_catalog`. Add a **Users** tab (`hx-get="/users"`, gated on `manage_users`; its routes arrive in Task 16) and a **Sign out** button (`hx-post="/logout"`). `perms` is a `frozenset[Permission]` and `Permission` is a `str` enum, so `{% if "view_logs" in perms %}` works.

**Tests to write first, in `tests/test_web_routes.py`:**
1. A `TestPermissionMatrix` class parametrised over `(method, path, permission)` × every `Role`, asserting 200 when `permission in ROLE_PERMISSIONS[role]` and 403 otherwise. Cover at least: `GET /`, `/status`, `/kpi`, `/activity`, `/health`, `/screen`, `/inventory`, `/logs`, `/inventory/new`, `/config/machine`, `/config/contacts`, and `POST /action/restart`. **Exclude `/config/payments` and `/config/comms`** and say why in a comment: their templates do not exist yet (their own tests are `@pytest.mark.skip`ped for that reason), so they 500 for a permitted role and would make the matrix lie.
2. No session on a page request → 303 to `/login`; on an HTMX request → 401 with `HX-Redirect: /login`; stale HTTP Basic credentials grant nothing (303 or 401, never 200). Use the `anonymous` fixture.

**Housekeeping:** delete the old `TestAuth` and `TestLoginLimiter` classes — `TestPermissionMatrix` replaces them. Re-aim `test_delete_requires_auth` and `test_screen_requires_auth` at the redirect behavior instead of a 401 with `WWW-Authenticate`. Fold the second `wired` fixture further down the file (used by `TestStillSellingBanner` and `TestAvailabilityOnDashboard`) into the new one — do not leave two fixtures of that name; adapt those classes' unpacking, not their assertions.

**Done when:** `uv run pytest` is green except `tests/test_login_limiter.py` (delete it here if it blocks, and say so in the report — Task 20 removes it otherwise) and the two skipped payments/comms tests. Commit: `feat(access): permission per route, cookie sessions replace HTTP Basic`.

---

### Task 12: Catalog / placement split on the product form

**Spec:** §4 — "The product edit form splits into `/inventory/edit/{sku}/catalog` and `/inventory/edit/{sku}/placement`, each with its own POST and permission. The current single form is removed."

**Files:**
- Modify: `web_interface/routes.py`, `web_interface/templates/partials/inventory_table.html`
- Create: `web_interface/templates/partials/inventory_catalog_form.html`, `web_interface/templates/partials/inventory_placement_form.html`
- Delete: `web_interface/templates/partials/inventory_edit_form.html`
- Test: `tests/test_web_routes.py`

**Interfaces:**
- `GET /inventory/edit/{sku}/catalog` → `partials/inventory_catalog_form.html`; permission `edit_catalog`. Context: `product`.
- `POST /inventory/update/{sku}/catalog`; form fields `name`, `price`, `kind`; permission `edit_catalog` + `require_htmx`; returns `partials/inventory_table.html`.
- `GET /inventory/edit/{sku}/placement` → `partials/inventory_placement_form.html`; permission `edit_placement`. Context: `product`, `inventory_count`, `tracked`.
- `POST /inventory/update/{sku}/placement`; form fields `slot`, `inventory_count`, `track_inventory` (checkbox, absent when unchecked); permission `edit_placement` + `require_htmx`; returns `partials/inventory_table.html`.
- `GET /inventory/edit/{sku}` and `POST /inventory/update/{sku}` are **removed** — they must 404.

**Behavior to get right:**
- A placement edit must not be able to move a catalog field. Pass the product's stored name and price through unchanged when persisting the slot.
- `services/config_store.py:update_product` treats `slot=None` as "unchanged" (`services/config_store.py:157`) — verify that before relying on it for the catalog POST.
- Counts and the tracked flag live in `InventoryManager` (`get_count`, `is_tracked`, `set_count`), not in `config.json`; read and write them there, falling back to the `Product` fields when no inventory manager is attached. `track_inventory` on the `Product` is set from the checkbox's presence.
- The two form templates take their fields from the deleted `inventory_edit_form.html`: catalog keeps SKU (disabled), Name, Price, Kind; placement keeps SKU (disabled), Dispenser Slot (`min="0" step="1" required`), Inventory Count and a `track_inventory` checkbox. Both target `#content-body` and carry a Cancel button that `hx-get`s `/inventory`.
- `partials/inventory_table.html`'s single Edit link becomes two — "Catalog" behind `edit_catalog`, "Placement" behind `edit_placement`.

**Tests to write first:** the old single-form routes 404; an owner reaches both forms; a catalog POST changes name, price and kind; a placement POST changes slot, count and the tracked flag; a **loader** gets 200 on the placement form and its POST and **403 on both catalog endpoints, with the price unchanged afterwards**; a tech behaves the same as the loader here. Re-aim the existing `test_edit_form`, `test_edit_form_shows_slot_input` and `test_update_product_changes_slot` at the new paths, keeping their assertions.

**Done when:** `uv run pytest` green. Commit: `feat(access): split the product form into catalog and placement`.

---

### Task 13: Setup code on the customer display

**Spec:** §3.1 — "publishes it to the customer display through `services/display_controller.py` (maintenance mode, 'Setup code: 1234 5678') for as long as setup mode lasts".

> **§3.5 NOTIFICATION — OWNER APPROVED 2026-09-25.** `DisplayCommand` carries only `mode`; there is nowhere to put the text the spec asks for. This task adds an **optional** `message: str | None = None` field. It is additive, nothing in-repo subscribes to `cmd/display` (only `display_controller.py` publishes it), and the model lives in `services/mqtt_messages.py`, not in `contracts/`, so no `CONTRACT_VERSION` bump is implied. Approved explicitly; proceed and record it in the pull-request deviations list. No further confirmation needed.

**Files:**
- Modify: `services/mqtt_messages.py`, `services/display_controller.py`
- Test: `tests/test_display_controller.py`

**Interfaces:**
- `DisplayCommand.message: str | None = None`, with a field description saying it is additive and optional and that firmware ignoring it behaves exactly as before.
- `DisplayController.setup_code -> str | None` (property)
- `DisplayController.show_setup_code(code: str) -> None`
- `DisplayController.clear_setup_code() -> None`
- `DisplayController._publish_mode(mode, message: str | None = None)` — the message reaches `DisplayCommand`.

**Behavior to get right:**
- `show_setup_code` sets maintenance mode and publishes `message="Setup code: 1234 5678"` — the eight digits split into two groups of four by a single space. It also logs the code at warning level.
- While a setup code is held, `update_for_state` records the state but publishes **nothing**, so an FSM transition cannot wipe the code off the screen.
- `clear_setup_code` forgets the code and republishes the mode for the last recorded state; calling it with no code held is a no-op that publishes nothing.
- Ordinary commands still carry `message=None`.

**Tests to write first:** show publishes maintenance with the grouped digits and sets `setup_code`; a state change while a code is held publishes nothing and leaves the mode at maintenance; clear returns to the state's mode with `message is None` and `setup_code is None`; clear with no code held publishes nothing; an ordinary state change carries `message is None`. Reuse whatever helpers `tests/test_display_controller.py` already uses to build a controller with a stub MQTT client and inspect published commands.

**Done when:** `uv run pytest tests/test_display_controller.py tests/test_mqtt.py` green, then the full suite. Commit: `feat(access): show the setup code on the customer display`.

---

### Task 14: Setup mode gate and the owner wizard

**Spec:** §3.1 (setup mode, `POST /setup` step 1), §6 (corrupt access file).

**Files:**
- Modify: `web_interface/routes.py`
- Create: `web_interface/templates/setup.html`
- Test: `tests/test_web_routes.py`

**Interfaces:**
- `routes.display_controller` module global and `routes.set_display_controller(display) -> None`.
- `routes.ensure_setup_mode() -> None` — while the store is in setup mode, calls `begin_setup()`, logs the plaintext at **warning** level (grouped as two blocks of four), and hands it to `display_controller.show_setup_code(...)` once. When an owner exists it calls `display_controller.clear_setup_code()` instead. Safe to call repeatedly; `begin_setup()` is idempotent within a process.
- An HTTP middleware registered on `app` inside `attach_routes`, running before every request except `/static/*`:
  - store corrupt → **503** on every path with a plain page saying the access file is corrupt and the machine keeps running (spec §6: a corrupt file must never silently become an open setup wizard)
  - store in setup mode → **303 to `/setup`** for every path except `/setup` and `/setup/codes`
  - otherwise → pass through
- `GET /setup` — public. Calls `ensure_setup_mode()`, renders the wizard, and redirects 303 to `/` when there is neither setup mode nor a pending transfer. Template context: `error`, `form` (a dict refilling `name` and `email`), `transfer` (bool).
- `POST /setup` — public, `require_htmx`. Fields `setup_code`, `name`, `email`, `pin`, `pin_confirm`, `shared_device` (checkbox). On success: creates the owner, creates a device with the `shared` flag, trusts the owner on it, records the login, opens a session, sets `vmc_session` and `vmc_device`, and answers `HX-Redirect: /setup/codes`.

**Behavior to get right:**
- Back-off kind `setup`, subject `"setup"` (kind `transfer` / subject `"transfer"` while a transfer is pending — Task 19 uses the same handler). Over the limit → 429 with `Retry-After` and the wizard re-rendered.
- Order of checks: back-off, then the code, then `pin != pin_confirm`, then `pin_problem(pin)`. A wrong code records a failure; a bad PIN does not.
- `OwnerExistsError` from two racing submissions is caught and re-rendered as an error, not a 500. The store, not the route, is what makes that safe.
- The setup code stays valid after step 1 — it is Task 15's Done that invalidates it — so a lost response is recoverable by logging in and enrolling with it (Task 10 already accepts it while `setup_finalized` is false).
- `setup.html` is a full page with viewport meta, posting `hx-post="/setup"` with `hx-target="body" hx-swap="outerHTML"`. Its copy tells the reader the code is in the machine's startup log and on the customer display. Heading "Set up this machine"; when `transfer` is true, "Take ownership" and the code field labelled "Transfer code". Render `error` in a `role="alert"` element.

**Tests to write first:** a fresh store redirects `/`, `/status`, `/inventory`, `/login` and `/health` to `/setup`, while `/setup` itself answers 200; the code is on the display and matches `store.pending_setup_code`; a wrong code creates no owner; repeated wrong codes reach 429; the right code creates the owner, sets both cookies, marks the device shared when the box is ticked and trusts the owner on it, and answers `HX-Redirect: /setup/codes`; a PIN failing `pin_problem` is rejected with the reason and no owner created; mismatched confirmation is rejected; a second browser recovers a lost step-1 response by signing in with the PIN and enrolling with the setup code; the POST without `HX-Request` is 403. Separately, a `TestCorruptAccessFile` class: `/`, `/setup`, `/login` and `/status` all answer 503 with "corrupt" in the body.

**Done when:** the full suite is green — every earlier test class seeds an owner, so the gate lets them through. Commit: `feat(access): setup-mode gate and the owner wizard`.

---

### Task 15: `/setup/codes` — the emergency-code page and Done

**Spec:** §3.1 step 2, §3.2.

**Files:**
- Modify: `web_interface/routes.py`
- Create: `web_interface/templates/setup_codes.html`
- Test: `tests/test_web_routes.py`

**Interfaces:**
- Module-level `routes._pending_codes: list[str]` — the plaintext codes, held **only** between generation and Done. After Done only hashes exist, so they can never be shown again.
- `GET /setup/codes` — permission `manage_ownership`. Generates the 20 codes on first view, reuses `_pending_codes` on a reload, and redirects 303 to `/` once setup is finalized and nothing is pending. Context: `codes`, `notice`, `error`, `can_email`.
- `POST /setup/codes/email` — `manage_ownership` + `require_htmx`. Emails the codes to the signed-in owner through `send_email`; re-renders with a notice, or an error when the gateway is unconfigured or the send fails.
- `POST /setup/codes/done` — `manage_ownership` + `require_htmx`. Calls `finalize_setup()`, clears `_pending_codes`, calls `display_controller.clear_setup_code()`, answers `HX-Redirect: /`.

These three sit on the public router (they are exempt from the setup-mode gate) but carry `require(Permission.manage_ownership)` themselves, so only a signed-in owner reaches them.

**Behavior to get right:**
- Reloading before Done shows the **same** codes and does not regenerate the pool — regenerating would invalidate codes the owner may have already written down.
- Done is what invalidates the setup code; `verify_setup_code` must return False afterwards.
- `setup_codes.html` is a full page: heading "Emergency codes", a warning that they are shown **once**, the codes in a monospace grid with **each code in its own element** so a `\b\d{8}\b` scan finds twenty distinct matches, an "Email these to me" button rendered only when `can_email`, a **Done** button, and `notice` / `error` paragraphs.
- The email body explains that each code works once, to trust a new device or authorise an ownership transfer, and that they should be kept somewhere other than the machine.

**Tests to write first:** twenty distinct 8-digit codes appear and `unused_emergency_code_count() == 20`; a reload shows the same set and the count is still 20; Done finalizes setup, clears the display and answers `HX-Redirect: /`; the setup code stops enrolling after Done; the page does not show codes again after Done; the email button routes through the mailer with all twenty codes in the body; a tech gets 403.

**Done when:** the full suite is green. Commit: `feat(access): emergency-code page and setup finalisation`.

---

### Task 16: User management routes and templates

**Spec:** §4.1 (user routes), §4 ("any write whose target user is the owner requires `manage_ownership`").

**Files:**
- Modify: `web_interface/routes.py`
- Create: `web_interface/templates/partials/users_list.html`, `web_interface/templates/partials/user_form.html`
- Test: `tests/test_web_routes.py`

**Interfaces** — all on the gated router, all rendering partials into `#content-body`, all POSTs carrying `require_htmx`:
- `GET /users` → `partials/users_list.html`; `manage_users`
- `GET /users/new` → `partials/user_form.html`; `manage_users`. Context: `roles`, `error`, `form`.
- `POST /users/new` — fields `name`, `email`, `role`, `pin`; `manage_users`
- `POST /users/{user_id}/disable`, `/enable`, `/reset-pin` (field `pin`), `/delete` — `manage_users`
- Helper `_guard_owner_target(principal, user_id)` — raises 403 when the target is the owner and the caller lacks `manage_ownership`.
- `partials/users_list.html` context: `users` (sorted by name), `owner_id`, `device_counts` (`dict[user_id, int]`), `unused_codes` (int), `pending_transfer` (dict or None), `error`, `notice`. Task 18 adds `transfer_code` and `new_codes`; write the renderer so adding them is a context change, not a restructure.

**Behavior to get right:**
- A secretary may manage everyone **except** the owner, and may not create an owner — `_guard_owner_target` on every write whose target is the owner, and a 403 when a caller without `manage_ownership` submits `role=owner`. `GET /users/new` offers the `owner` option only to a caller who holds `manage_ownership`.
- A new user's PIN runs through `pin_problem` first; a failure re-renders the list with the reason and creates nobody.
- Reset PIN rehashes **and** drops the user from every device so the next login re-enrolls — `AccessStore.set_user_pin` already does both.
- Disable and delete also end that user's live sessions (`end_sessions_for_user`), or a disabled user keeps their tab working until it idles out.
- Hide the owner's Disable / Delete / Reset buttons from a caller without `manage_ownership`; the server check remains the authority.
- The list shows, per user: name, role, email, disabled, last login, trusted-device count. Below it, "Unused emergency codes: N" and a `{% if pending_transfer %}` block Task 18 fills in.

**Tests to write first:** the owner sees the list containing "Ada"; the list shows the unused-code count; tech and loader get 403; the owner creates a loader; a bad PIN is refused with the reason and creates nobody; a secretary may not create an owner; a secretary gets 403 on disable, delete and reset-pin **targeting the owner**, and the owner is still enabled afterwards; a secretary may disable and re-enable a loader; reset-pin changes the hash and leaves the user trusted on no device; delete removes the user; a POST without `HX-Request` is 403.

**Done when:** the full suite is green. Commit: `feat(access): user management routes and Users tab partials`.

---

### Task 17: Device management routes

**Spec:** §4.1 — `GET /devices`, `POST /devices/{id}/forget`, `POST /devices/{id}/shared`.

**Files:**
- Modify: `services/access.py`, `web_interface/routes.py`, `web_interface/templates/dashboard.html`
- Create: `web_interface/templates/partials/devices_list.html`
- Test: `tests/test_web_routes.py`

**Interfaces:**
- `AccessStore.end_sessions_for_device(device_id: str) -> None` — new, mirrors `end_sessions_for_user`, memory-only.
- `GET /devices` → `partials/devices_list.html`; `manage_users`. Context: `devices` (sorted by label), `user_names` (`dict[user_id, str]`), `notice`.
- `POST /devices/{device_id}/forget` — `manage_users` + `require_htmx`; ends every session on that device **before** removing it.
- `POST /devices/{device_id}/shared` — `manage_users` + `require_htmx`; toggles the flag; 404 for an unknown device.

**Behavior to get right:** forgetting a device must actually log out whoever is using it — the session dies with the device either way (`resolve_session` checks), but ending them explicitly keeps the in-memory table from growing. The list resolves trusted user ids to names through `user_names` and never renders a raw id.

Add a **Devices** button beside **Users** in `dashboard.html`, gated on `manage_users`.

**Tests to write first:** the owner sees a device row naming its trusted user; a tech gets 403; forget removes the device and the forgotten device's client is refused on its next request; the shared toggle flips the flag; a POST without `HX-Request` is 403.

**Done when:** the full suite is green. Commit: `feat(access): device list, forget and shared toggle`.

---

### Task 18: Transfer start and cancel, code regeneration, machine report

**Spec:** §3.3 step 1, §3.2 (regenerate from the Users area), §3.4 (report on demand).

**Files:**
- Modify: `web_interface/routes.py`, `web_interface/templates/partials/users_list.html`
- Test: `tests/test_web_routes.py`

**Interfaces** — all `manage_ownership` + `require_htmx`, all returning `partials/users_list.html`:
- Helper `_check_owner_pin(request, principal, pin) -> str | None` — verifies the **caller's own** PIN under back-off kind `pin`, subject the caller's user id, `trusted` from `is_trusted_client`. Returns an error message or `None`.
- `POST /users/transfer` — fields `pin`, `emergency_code`. Renders the list with `transfer_code` shown once.
- `POST /users/transfer/cancel` — field `pin`.
- `POST /users/codes/regenerate` — field `pin`. Renders the list with `new_codes` shown once.
- `POST /users/report` — no fields; emails `store.machine_report(config)` to the signed-in owner.
- `_users_list(...)` gains `transfer_code=None` and `new_codes=None` parameters, passed straight into the template context.

**Behavior to get right:**
- `POST /users/transfer` checks the PIN first, then the emergency code under back-off kind `transfer`, subject `"pool"`. Only when **both** pass does it consume the code (`used_for="transfer"`) and record the pending transfer. A wrong PIN must leave the emergency-code pool untouched — check, then consume, never the other way round.
- Starting a transfer changes nothing else: the current owner keeps working, and the store still reports them as owner. That is spec §3.3's whole point and the reviewer should verify it explicitly.
- Regeneration replaces the entire pool, used codes included, and the old codes must stop working.
- `users_list.html` gains, all inside `{% if "manage_ownership" in perms %}`: a Transfer ownership form (`pin`, `emergency_code`); a `{% if pending_transfer %}` block showing `started_at` / `expires_at` with a Cancel form (`pin`); a Regenerate emergency codes form (`pin`); an Email machine report button; and `{% if transfer_code %}` / `{% if new_codes %}` blocks rendering those values in monospace with a "shown once" warning, **each 8-digit code in its own element**.

**Tests to write first:** start shows an 8-digit transfer code, records the pending transfer and consumes exactly one emergency code; the old owner still reaches `/status` and is still the owner while it is pending; a wrong PIN starts nothing **and consumes no code**; a wrong emergency code starts nothing; cancel clears the pending transfer and leaves the owner in place; a secretary gets 403 on start and on cancel; regenerate replaces the pool, shows twenty new codes and none of the old ones; regenerate with a wrong PIN changes nothing; a secretary gets 403 on regenerate; the report is emailed to the owner and names the users; a tech gets 403 on the report.

**Done when:** the full suite is green. Commit: `feat(access): transfer start and cancel, code regeneration, machine report`.

---

### Task 19: Completing a transfer and the user-review step

**Spec:** §3.3 steps 2–4.

**Files:**
- Modify: `services/access.py`, `web_interface/routes.py`, `web_interface/templates/setup.html`
- Create: `web_interface/templates/setup_review_user.html`
- Test: `tests/test_access.py`, `tests/test_web_routes.py`

**Interfaces:**
- `AccessStore.complete_transfer(...)` gains one step: before clearing `_pending_transfer`, it moves the transfer code's hash into `setup` as `{"setup_code_hash": <that hash>, "finalized": False}`. `verify_setup_code` already refuses once `finalized` is true, so Task 15's Done closes the window. This is how spec §3.3 step 4's "the transfer code remains valid as an enrollment code for the new owner until Done" is implemented — do **not** keep `pending_transfer` alive to achieve it.
- `GET /setup/review` — `manage_ownership`; renders `setup_review_user.html` for the next retained user, or redirects 303 to `/setup/codes` when none remain. Context: `user`, `device_count`.
- `POST /setup/review/{user_id}/keep` and `POST /setup/review/{user_id}/remove` — `manage_ownership` + `require_htmx`; apply the decision immediately and render the next user, or answer `HX-Redirect: /setup/codes` when that was the last one.
- `POST /setup` (Task 14) redirects to `/setup/review` when it completed a transfer, and to `/setup/codes` otherwise.

**Behavior to get right:**
- A pending transfer does **not** put the store in setup mode, so the gate keeps letting the old owner's dashboard through untouched while `/setup` is simultaneously reachable for the incoming owner. Task 14's `GET /setup` already returns the page when `pending_transfer` is not None; confirm the gate needs no change.
- Both decisions advance past the user just reviewed — "keep" is a no-op on the store but must not re-offer the same person. A reload of `GET /setup/review` restarting from the first remaining user is acceptable, because keeping is idempotent.
- Removing a user ends their sessions before deleting them.
- `setup_review_user.html` is a full page showing the user's name, role, email, last login and `device_count`, with **Keep** and **Remove** buttons (`hx-target="body" hx-swap="outerHTML"`) and a "Skip the rest" link to `/setup/codes`. Leaving mid-review simply keeps everyone remaining.

**Tests to write first.** In `tests/test_access.py`: the transfer code verifies as a setup code after `complete_transfer` and stops doing so after `finalize_setup`. In `tests/test_web_routes.py`: `/setup` is reachable while a transfer is pending; the rest of the dashboard is **not** redirected; the transfer code completes the swap — new owner installed, old owner gone, pending transfer cleared, emergency pool emptied, and the response redirects to `/setup/review`; the old owner's session is dead afterwards; a wrong transfer code changes nothing; review walks the retained users and removing the last one redirects on; keeping a user leaves them in place; the transfer code still enrolls the new owner on a second browser before Done.

**Done when:** `uv run pytest` is green across the suite. Commit: `feat(access): complete an ownership transfer and review retained users`.

---

### Task 20: Drop the admin credential and wire the store into `main.py`

**Spec:** §1 (`WebConfig` loses `admin_username` and `admin_password`; the startup password warning becomes a setup-mode warning), §5 (files table).

**Files:**
- Modify: `config/config_model.py`, `services/auth_policy.py`, `main.py`, `config.example.json`
- Modify: `tests/test_config_model.py`, `tests/test_first_run.py`, `tests/test_startup_policy.py`, `tests/test_auth_policy.py`
- Delete: `tests/test_login_limiter.py`

**Interfaces:**
- `WebConfig` keeps `host`, `port`, `trusted_proxies` only. Its docstring says authentication lives in `data/access.json`, not here; `trusted_proxies`' description says "login back-off keying", not "login limiter".
- `services/auth_policy.py` exports `pin_problem`, `MIN_PIN_LENGTH`, `MAX_PIN_LENGTH`, `is_loopback`, `LOOPBACK_HOSTS`. `password_problem`, `generate_admin_password`, `WEAK_PASSWORDS`, `MIN_PASSWORD_LENGTH` and the `secrets` import are deleted.
- `main.warn_if_setup_mode(store: AccessStore) -> None` replaces `main.enforce_password_policy`. It logs an error when the store is corrupt (saying the VMC and MQTT client keep running) and a warning when no owner exists ("Dashboard is in setup mode… Visit /setup at the machine"). It never exits.
- `main._create_default_config` no longer generates or logs a password.
- `ICE_COLDER_ALLOW_WEAK_PASSWORD` no longer exists anywhere.

**Wiring in `main()`:** construct `AccessStore()` after `load_config()`; `routes.set_access_store(store)`; `web_auth.backoff.set_trusted_proxies(overrides.trusted_proxies)` (replacing the line Task 11 already edited); `warn_if_setup_mode(store)`. After the display controller is built: `routes.set_display_controller(display)` then `routes.ensure_setup_mode()`. Delete the `enforce_password_policy(web_cfg)` call before the uvicorn config.

**Tests to update first:** drop the `admin_username` / `admin_password` assertions in `tests/test_config_model.py` and assert the attributes are **absent**; replace `tests/test_first_run.py`'s generated-password test with one asserting neither key appears in the saved config; delete the five password-policy tests in `tests/test_startup_policy.py`, keep its env-override tests, and add two covering `warn_if_setup_mode` (warns with no owner, silent once an owner exists); strip the password tests from `tests/test_auth_policy.py`, keeping `is_loopback` and Task 1's PIN tests; delete `tests/test_login_limiter.py`.

**Done when:** `uv run pytest` green and this matches nothing outside `docs/superpowers/plans/2026-09-12-*` and `2026-09-22-*`:

```bash
grep -rn "admin_password\|admin_username\|LoginLimiter\|ALLOW_WEAK_PASSWORD" --include="*.py" --include="*.json" --include="*.yml" .
```

Commit: `feat(access): remove the admin credential and wire AccessStore into startup`.

---

### Task 21: Documentation, compose and environment

**Spec:** §5 (files table: `.env.example`, `docker-compose*.yml`, `README.md`, `CLAUDE.md`), program plan §5 (backup guidance in `README.md`).

Documentation only — no code, no tests. **Haiku implementer.** Verify by grep and by `uv run pytest` / `ruff check .` still being green.

**Files:** `README.md`, `CLAUDE.md`, `.env.example`, `docker-compose.yml`, `docker/docker-compose.prod.yml`, `docker/docker-compose.yml`.

**Start by finding every reference:**

```bash
grep -rn "admin password\|admin_password\|ALLOW_WEAK_PASSWORD\|Basic auth\|HTTP Basic\|login limiter" README.md CLAUDE.md .env.example docker-compose.yml docker/
```

**`README.md`** — replace the weak-admin-password paragraph (around lines 72–79) with three things: (1) there is no default credential; first boot enters **setup mode**, the 8-digit setup code is printed in the startup log at warning level (`docker compose logs vmc`) and shown on the customer display, and the wizard creates the owner and then shows 20 emergency codes once; (2) a browser the machine has not seen needs a second factor once — a 6-digit emailed code when `communication.email_gateway` is configured, or an emergency code, which works with no network at all; (3) **back up `data/access.json`** — it holds every user, PIN hash, trusted device and emergency-code hash and is the single point of lockout, it sits inside the bind-mounted `./data`, the Users screen shows how many codes remain unused, and an owner who loses both their PIN and every emergency code has **no software recovery** (the controller is factory-restored by deleting `data/access.json` and rerunning the wizard). Keep the surrounding Traefik and `ICE_COLDER_TRUSTED_PROXIES` paragraphs; change only "the dashboard's login limiter" to "the dashboard's login back-off".

**`CLAUDE.md`** — three edits: the entry-point paragraph drops "HTTP Basic auth from `config.web.admin_username`/`admin_password`" in favour of sessions from `data/access.json`; the configuration paragraph notes `web` now holds host, port and trusted proxies only; the Web Dashboard section's HTTP Basic paragraph is replaced by a description of the `AccessStore` (path, 0600, four roles, PIN, per-device trust by OTP or offline emergency code), `require(...)` per route with `template_context` feeding templates `perms` and `current_user`, `Backoff` replacing `LoginLimiter` (exponential per `(kind, subject, client)` plus a per-user budget, no lockouts), setup mode redirecting everything to `/setup` behind a code that exists only at the machine, and `require_htmx` still guarding POSTs — now load-bearing because auth is cookie-based. Add `access.py` and `mailer.py` bullets to the Services list and correct the `auth_policy.py` bullet to "PIN policy (`pin_problem`)… `is_loopback` kept".

**Compose and `.env.example`** — remove any admin-credential or `ICE_COLDER_ALLOW_WEAK_PASSWORD` entries. Confirm `./data` is bind-mounted read-write for the `vmc` service (it already is) so `data/access.json` persists, and add a comment there naming the file. **Do not run `docker compose`** — CI's `compose-config` job validates it.

Commit: `docs: setup mode, roles and access file replace the admin password`.

---

## Definition of Done

1. `uv run pytest` is green and `ruff check .` is clean.
2. `grep -rn "admin_password\|admin_username\|LoginLimiter\|ALLOW_WEAK_PASSWORD" --include="*.py" --include="*.json" --include="*.yml" .` matches nothing outside `docs/superpowers/plans/2026-09-12-*` and `2026-09-22-*`.
3. No new entry in `pyproject.toml`'s dependency list.
4. `services/mqtt_messages.py`'s only change is the optional `DisplayCommand.message` field (Task 13), approved by the owner.
5. Every deviation and assumption recorded in the tasks above is listed in the pull-request description, alongside the part-1 acceptance results from program plan §4.

## Spec coverage check

| Spec section | Task(s) |
|---|---|
| §1 data model, invariants, 0600 file, clocks | 4, 4b, 5, 6 |
| §1 `pin_problem`, `WebConfig` change, startup warning | 1, 20 |
| §2.1 cookies, session validity | 8, 9 |
| §2.2 `GET`/`POST /login` | 9 |
| §2.3 enrollment, OTP, mailer, device creation | 7, 10 |
| §2.4 back-off, per-user budget, trusted proxies | 2, 8, 20 |
| §2.5 logout | 10 |
| §3.1 setup mode, setup code, two durable steps | 13, 14, 15 |
| §3.2 emergency-code page, regeneration | 15, 18 |
| §3.3 transfer start, complete, review, cancel, expiry | 6, 18, 19 |
| §3.4 machine report | 6, 18 |
| §4 permission table and enforcement | 3, 11 |
| §4 catalog/placement split | 12 |
| §4.1 user and device routes | 16, 17, 18 |
| §5 file list | 1–21 |
| §6 error handling | 4 (corrupt), 7 (SMTP), 8 (dead session), 14 (corrupt page), 16 (bad PIN) |
| §7 tests | every task |
| §8 out of scope | nothing built |
