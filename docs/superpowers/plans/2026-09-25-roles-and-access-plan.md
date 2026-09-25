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

### Task 11: Permissions on every existing route; HTTP Basic removed

**Spec:** §4 (enforcement), §7 (`login_as` helper, permission matrix test).

This is the largest task in the plan and the one the reviewer should read hardest. It touches `routes.py`, `auth.py`, `dashboard.html` and `tests/test_web_routes.py` — four files. It is not split further because the fixture rewrite and the dependency swap cannot land separately without leaving the suite red.

**Files:**
- Modify: `web_interface/routes.py`, `web_interface/auth.py` (delete `LoginLimiter`), `web_interface/templates/dashboard.html`
- Test: `tests/test_web_routes.py`

**Route → permission map (authoritative for this task):**

| Route | Permission |
|---|---|
| `GET /` | `view_status` |
| `GET /status` | `view_status` |
| `GET /kpi` | `view_status` |
| `GET /activity` | `view_status` |
| `GET /health` | `view_status` |
| `GET /screen`, `GET /screen/body` | `view_status` |
| `GET /inventory` | `view_status` |
| `GET /logs` | `view_logs` |
| `POST /faults/{key}/clear` | `clear_faults` |
| `POST /action/{command}` | `machine_controls` |
| `GET /inventory/new`, `POST /inventory/add`, `GET /inventory/copy/{sku}`, `POST /inventory/delete/{sku}` | `edit_catalog` |
| `GET /inventory/edit/{sku}`, `POST /inventory/update/{sku}` | `edit_catalog` (split in Task 12) |
| `GET /config/machine`, `GET /config/contacts` | `edit_contacts` (spec §4: "`edit_contacts` — people and machine info") |
| `GET /config/payments`, `GET /config/comms` | `edit_secrets` (spec §4: "`edit_secrets` — payment, comms, MQTT settings") |

**Interfaces:**
- Consumes: `web_auth.require`, `web_auth.template_context`, `web_auth.Principal`.
- Produces:
  - Every gated route declares `dependencies=[Depends(web_auth.require(Permission.x))]`; the router-level `require_auth` dependency and `require_auth` itself are **deleted**, as are `HTTPBasic` / `HTTPBasicCredentials` / `_basic_auth` / `login_limiter` / the `LoginLimiter` import.
  - `require_htmx` is unchanged and stays on every POST.
  - Every `templates.TemplateResponse` in a gated route builds its context with `web_auth.template_context(request, ...)` instead of a bare `{"request": request, ...}`.
  - `tests/test_web_routes.py` gains a module-level `login_as(role, *, shared=False, store=None)` fixture factory returning a `TestClient` with `vmc_device` and `vmc_session` set.
  - `web_interface/auth.py` no longer defines `LoginLimiter`, and its `ipaddress` / `time` / `deque` imports go with it.

- [ ] **Step 1: Rewrite the test fixtures and add the matrix test**

Replace the `client` fixture at the top of `tests/test_web_routes.py` and add the new helpers. Every existing test keeps its body; only the fixture changes, so they now run as the owner.

```python
import pytest
from fastapi.testclient import TestClient

from config.config_model import ConfigModel
from contracts.vending_machine import FaultCode
from controller.vmc import VMC
from services.access import AccessStore, Permission, Role
from services.inventory_manager import InventoryManager
from web_interface import auth as web_auth
from web_interface import routes
from web_interface.server import app


def sign_in(store, user, *, shared=False):
    """A TestClient carrying a trusted device and a live session for *user*."""
    device, token = store.create_device(f"{user.name} device", shared=shared)
    store.trust_device(device.id, user.id)
    session_id = store.create_session(user.id, device.id)
    c = TestClient(app)
    c.cookies.set(web_auth.DEVICE_COOKIE, token)
    c.cookies.set(web_auth.SESSION_COOKIE, session_id)
    c.headers["HX-Request"] = "true"
    return c


def make_client(store, role=Role.owner, *, shared=False, name="Ada"):
    """Seed a user of *role* and return (client, user)."""
    user = store.create_user(name, f"{name.lower()}@example.com", role, "1379")
    return sign_in(store, user, shared=shared), user


@pytest.fixture
def wired(tmp_path):
    """Config, VMC, inventory and an AccessStore wired into the routes module.

    The store is seeded with the owner "Ada" (PIN 1379) so the app is never in
    setup mode for route tests — the setup gate has its own fixtures.
    """
    cfg = ConfigModel()
    vmc = VMC(config=cfg)
    inv = InventoryManager([], path=tmp_path / "inventory.json")
    store = AccessStore(path=tmp_path / "access.json")
    store.create_user("Ada", "ada@example.com", Role.owner, "1379")
    store.finalize_setup()
    routes.set_config_object(cfg)
    routes.set_vmc_instance(vmc)
    routes.set_inventory_manager(inv)
    routes.set_access_store(store)
    yield cfg, vmc, inv, store
    routes.set_access_store(None)
    for t in vmc._pending_tasks:
        t.cancel()


@pytest.fixture
def login_as(wired):
    """login_as(Role.tech) -> TestClient. Seeds the user and a trusted device.

    Role.owner returns a client for the fixture's existing owner, because the
    store enforces one owner per machine.
    """
    _, _, _, store = wired
    created = []

    def _login(role=Role.owner, *, shared=False, name=None):
        if role is Role.owner:
            user = store.owner()
        else:
            user = store.create_user(
                name or f"User{len(created)}",
                f"{(name or 'user').lower()}@example.com",
                role,
                "1379",
            )
        c = sign_in(store, user, shared=shared)
        created.append(c)
        return c

    yield _login
    for c in created:
        c.close()


@pytest.fixture
def client(login_as):
    """The owner's client — what every pre-existing test in this file uses."""
    yield login_as(Role.owner)


@pytest.fixture
def anonymous(wired):
    """A client with an owner seeded in the store but no cookies of its own."""
    with TestClient(app, follow_redirects=False) as c:
        yield c
```

Note for the implementer: the existing `wired` fixture further down the file (used by `TestStillSellingBanner` and `TestAvailabilityOnDashboard`) must be folded into this one — do not leave two fixtures named `wired`. Those classes currently unpack `wired` differently; adapt their bodies, not their assertions. `tests/test_web_routes.py` also needs `store.finalize_setup()` in any fixture that seeds an owner directly, so the `/setup/codes` route does not think setup is still running.

Add the matrix test:

```python
# (route, method, permission). /config/payments and /config/comms are left out:
# their templates do not exist yet (their own tests are skipped for that
# reason), so they 500 for a permitted role and would make this matrix lie.
PERMISSION_MATRIX = [
    ("GET", "/", Permission.view_status),
    ("GET", "/status", Permission.view_status),
    ("GET", "/kpi", Permission.view_status),
    ("GET", "/activity", Permission.view_status),
    ("GET", "/health", Permission.view_status),
    ("GET", "/screen", Permission.view_status),
    ("GET", "/inventory", Permission.view_status),
    ("GET", "/logs", Permission.view_logs),
    ("GET", "/inventory/new", Permission.edit_catalog),
    ("GET", "/config/machine", Permission.edit_contacts),
    ("GET", "/config/contacts", Permission.edit_contacts),
    ("POST", "/action/restart", Permission.machine_controls),
]


class TestPermissionMatrix:
    @pytest.mark.parametrize("method,path,permission", PERMISSION_MATRIX)
    @pytest.mark.parametrize("role", list(Role))
    def test_route_answers_exactly_as_the_table_predicts(
        self, login_as, method, path, permission, role
    ):
        from services.access import ROLE_PERMISSIONS

        c = login_as(role)
        resp = c.request(method, path)
        allowed = permission in ROLE_PERMISSIONS[role]
        if allowed:
            assert resp.status_code == 200, f"{role} {method} {path}"
        else:
            assert resp.status_code == 403, f"{role} {method} {path}"

    def test_no_session_redirects_a_page_request(self, anonymous):
        resp = anonymous.get("/")
        assert resp.status_code == 303
        assert resp.headers["location"] == "/login"

    def test_no_session_hx_redirects_a_partial_request(self, anonymous):
        resp = anonymous.get("/status", headers={"HX-Request": "true"})
        assert resp.status_code == 401
        assert resp.headers["hx-redirect"] == "/login"

    def test_basic_auth_credentials_no_longer_grant_access(self, anonymous):
        resp = anonymous.get("/status", auth=("admin", "changeme"))
        assert resp.status_code in (303, 401)
```

Delete the old `TestAuth` and `TestLoginLimiter` classes from `tests/test_web_routes.py`; `TestPermissionMatrix` replaces them. Change `test_delete_requires_auth` and `test_screen_requires_auth` to assert the new redirect behavior rather than 401-with-`WWW-Authenticate`.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_web_routes.py -v -k TestPermissionMatrix`
Expected: FAIL — every role reaches every route (200), because the router still uses HTTP Basic.

- [ ] **Step 3: Implement**

In `web_interface/routes.py`:

1. Delete the `HTTPBasic` / `HTTPBasicCredentials` imports, `_basic_auth`, `require_auth`, `login_limiter`, the `LoginLimiter` import, and the `login_limiter.set_trusted_proxies(...)` line inside `set_config_object`.
2. Change the router construction from `APIRouter(dependencies=[Depends(require_auth)])` to `APIRouter()`.
3. Add `dependencies=[Depends(web_auth.require(Permission.x))]` to every gated route per the table above; where a route already has `dependencies=[Depends(require_htmx)]`, list both:

```python
    @router.post(
        "/action/{command}",
        dependencies=[
            Depends(require_htmx),
            Depends(web_auth.require(Permission.machine_controls)),
        ],
    )
```

4. Replace every gated route's context dict with `web_auth.template_context(request, ...)`. For example:

```python
    @router.get(
        "/inventory",
        response_class=HTMLResponse,
        dependencies=[Depends(web_auth.require(Permission.view_status))],
    )
    async def inventory_view(request: Request):
        return templates.TemplateResponse(
            "partials/inventory_table.html",
            web_auth.template_context(
                request, products=config.products, locked=_locked_skus()
            ),
        )
```

Do the same for `dashboard`, `machine_info`, `contact_info`, `payment_config`, `comms_config`, `_render_status`, `view_logs`, `health_summary`, `activity_fragment`, `kpi_fragment`, `edit_inventory_item`, `update_inventory_item`, `delete_inventory_item`, `add_new_product`, `new_product_form`, `copy_product_form`, `screen`, `screen_body`. `_screen_context` returns a dict — have it start from `web_auth.template_context(request)` instead of `{"request": request}`.

5. Import `Permission` from `services.access`.

In `web_interface/auth.py`: delete the `LoginLimiter` class and the now-unused `ipaddress`, `time`, `deque` and `Callable` imports.

In `web_interface/templates/dashboard.html`: wrap each tab button in the permission that its target route needs, and add a Users tab (its routes arrive in Task 16 — the button may point at `/users` now):

```html
{% if "view_logs" in perms %}
<button hx-get="/logs" ...>Logs</button>
{% endif %}
{% if "edit_contacts" in perms %}
<button hx-get="/config/machine" ...>Machine Info</button>
{% endif %}
{% if "manage_users" in perms %}
<button hx-get="/users" hx-target="#content-body" hx-swap="innerHTML" ...>Users</button>
{% endif %}
```

`perms` is a `frozenset[Permission]`; `Permission` is a `str` enum, so `"view_logs" in perms` is true when the member is present. Gate the three control buttons (restart / reset / shutdown) on `"machine_controls" in perms`, the inventory Add/Copy/Delete controls in `partials/inventory_table.html` on `"edit_catalog" in perms`, and add a Sign out button posting to `/logout` with `hx-post`.

`main.py` still calls `routes.login_limiter.set_trusted_proxies(...)` — change that single line to `web_auth.backoff.set_trusted_proxies(overrides.trusted_proxies)` (importing `from web_interface import auth as web_auth`) so the app still starts. The rest of `main.py` is Task 20.

- [ ] **Step 4: Run the full suite**

Run: `uv run pytest`
Expected: PASS except `tests/test_login_limiter.py` (deleted in Task 20 — delete it now if it blocks, and say so in the report) and the two `@pytest.mark.skip`ped payments/comms tests. Every other pre-existing test in `tests/test_web_routes.py` must pass unchanged apart from the two auth assertions called out above.

- [ ] **Step 5: Lint and commit**

```bash
ruff check --fix .
ruff format .
git add web_interface tests/test_web_routes.py main.py
git commit -m "feat(access): permission per route, cookie sessions replace HTTP Basic"
```

---

### Task 12: Catalog / placement split on the product form

**Spec:** §4 ("The product edit form splits into `/inventory/edit/{sku}/catalog` and `/inventory/edit/{sku}/placement`, each with its own POST and permission. The current single form is removed.").

**Files:**
- Modify: `web_interface/routes.py`
- Create: `web_interface/templates/partials/inventory_catalog_form.html`, `web_interface/templates/partials/inventory_placement_form.html`
- Delete: `web_interface/templates/partials/inventory_edit_form.html`
- Test: `tests/test_web_routes.py`

**Interfaces:**
- Consumes: `services.config_store.update_product`, `services.inventory_manager.InventoryManager.set_count`, `web_auth.require`.
- Produces:
  - `GET /inventory/edit/{sku}/catalog` and `POST /inventory/update/{sku}/catalog` — `edit_catalog`; fields `name`, `price`, `kind`
  - `GET /inventory/edit/{sku}/placement` and `POST /inventory/update/{sku}/placement` — `edit_placement`; fields `slot`, `inventory_count`, `track_inventory`
  - `GET /inventory/edit/{sku}` and `POST /inventory/update/{sku}` are **removed**

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_web_routes.py`:

```python
class TestCatalogPlacementSplit:
    def _seed(self, client):
        client.post(
            "/inventory/add",
            data={"sku": "SPLIT-1", "name": "Bag of Ice", "price": "2.50", "slot": "4"},
        )

    def test_old_single_form_routes_are_gone(self, client):
        self._seed(client)
        assert client.get("/inventory/edit/SPLIT-1").status_code == 404
        assert (
            client.post(
                "/inventory/update/SPLIT-1",
                data={"name": "x", "price": "1", "slot": "1"},
            ).status_code
            == 404
        )

    def test_owner_sees_both_forms(self, client):
        self._seed(client)
        assert client.get("/inventory/edit/SPLIT-1/catalog").status_code == 200
        assert client.get("/inventory/edit/SPLIT-1/placement").status_code == 200

    def test_catalog_post_changes_price_and_name(self, client):
        self._seed(client)
        resp = client.post(
            "/inventory/update/SPLIT-1/catalog",
            data={"name": "Bigger Bag", "price": "3.25", "kind": "ice"},
        )
        assert resp.status_code == 200
        product = next(p for p in routes.config.products if p.sku == "SPLIT-1")
        assert product.name == "Bigger Bag"
        assert product.price == 3.25
        assert product.kind == "ice"

    def test_placement_post_changes_slot_and_count(self, client, wired):
        _, _, inv, _ = wired
        self._seed(client)
        inv.add_sku("SPLIT-1", 0, tracked=False)
        resp = client.post(
            "/inventory/update/SPLIT-1/placement",
            data={"slot": "9", "inventory_count": "12", "track_inventory": "on"},
        )
        assert resp.status_code == 200
        product = next(p for p in routes.config.products if p.sku == "SPLIT-1")
        assert product.slot == 9
        assert inv.get_count("SPLIT-1") == 12
        assert inv.is_tracked("SPLIT-1") is True

    def test_loader_may_change_slot_and_count(self, login_as, client):
        self._seed(client)
        loader = login_as(Role.loader)
        assert loader.get("/inventory/edit/SPLIT-1/placement").status_code == 200
        resp = loader.post(
            "/inventory/update/SPLIT-1/placement",
            data={"slot": "5", "inventory_count": "3"},
        )
        assert resp.status_code == 200

    def test_loader_gets_403_on_price(self, login_as, client):
        self._seed(client)
        loader = login_as(Role.loader)
        assert loader.get("/inventory/edit/SPLIT-1/catalog").status_code == 403
        assert (
            loader.post(
                "/inventory/update/SPLIT-1/catalog",
                data={"name": "Cheap", "price": "0.01", "kind": "ice"},
            ).status_code
            == 403
        )
        product = next(p for p in routes.config.products if p.sku == "SPLIT-1")
        assert product.price == 2.50

    def test_tech_may_place_but_not_price(self, login_as, client):
        self._seed(client)
        tech = login_as(Role.tech)
        assert tech.get("/inventory/edit/SPLIT-1/placement").status_code == 200
        assert tech.get("/inventory/edit/SPLIT-1/catalog").status_code == 403
```

Update the existing `test_edit_form`, `test_edit_form_shows_slot_input` and `test_update_product_changes_slot` to target the new paths (`/inventory/edit/{sku}/catalog` for name, `/inventory/edit/{sku}/placement` for slot). Keep their assertions.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_web_routes.py -v -k TestCatalogPlacementSplit`
Expected: FAIL — 404 on `/inventory/edit/SPLIT-1/catalog`.

- [ ] **Step 3: Implement**

Replace `edit_inventory_item` and `update_inventory_item` in `web_interface/routes.py` with:

```python
    @router.get(
        "/inventory/edit/{sku}/catalog",
        response_class=HTMLResponse,
        dependencies=[Depends(web_auth.require(Permission.edit_catalog))],
    )
    async def edit_catalog_form(request: Request, sku: str):
        product = next((p for p in config.products if p.sku == sku), None)
        return templates.TemplateResponse(
            "partials/inventory_catalog_form.html",
            web_auth.template_context(request, product=product),
        )

    @router.post(
        "/inventory/update/{sku}/catalog",
        response_class=HTMLResponse,
        dependencies=[
            Depends(require_htmx),
            Depends(web_auth.require(Permission.edit_catalog)),
        ],
    )
    async def update_catalog(
        request: Request,
        sku: str,
        name: str = Form(...),
        price: float = Form(...),
        kind: str = Form("other"),
    ):
        update_product(config, sku, name, price, kind=kind)
        return templates.TemplateResponse(
            "partials/inventory_table.html",
            web_auth.template_context(
                request, products=config.products, locked=_locked_skus()
            ),
        )

    @router.get(
        "/inventory/edit/{sku}/placement",
        response_class=HTMLResponse,
        dependencies=[Depends(web_auth.require(Permission.edit_placement))],
    )
    async def edit_placement_form(request: Request, sku: str):
        product = next((p for p in config.products if p.sku == sku), None)
        count = (
            inventory_manager.get_count(sku)
            if inventory_manager and product
            else (product.inventory_count if product else 0)
        )
        tracked = (
            inventory_manager.is_tracked(sku)
            if inventory_manager and product
            else bool(product and product.track_inventory)
        )
        return templates.TemplateResponse(
            "partials/inventory_placement_form.html",
            web_auth.template_context(
                request, product=product, inventory_count=count, tracked=tracked
            ),
        )

    @router.post(
        "/inventory/update/{sku}/placement",
        response_class=HTMLResponse,
        dependencies=[
            Depends(require_htmx),
            Depends(web_auth.require(Permission.edit_placement)),
        ],
    )
    async def update_placement(
        request: Request,
        sku: str,
        slot: int = Form(...),
        inventory_count: int = Form(0),
        track_inventory: str | None = Form(None),
    ):
        product = next((p for p in config.products if p.sku == sku), None)
        if product is not None:
            # Name, price and kind are catalog fields: pass the stored values
            # through unchanged so a placement edit can never move them.
            update_product(config, sku, product.name, product.price, slot=slot)
            product.track_inventory = track_inventory is not None
            if inventory_manager:
                inventory_manager.set_count(sku, inventory_count)
        return templates.TemplateResponse(
            "partials/inventory_table.html",
            web_auth.template_context(
                request, products=config.products, locked=_locked_skus()
            ),
        )
```

`update_product(config, sku, name, price, kind=kind)` leaves `slot` at `None`, which the existing implementation treats as "unchanged" (`services/config_store.py:157`) — verify that before relying on it.

Create `web_interface/templates/partials/inventory_catalog_form.html` — the SKU (disabled), Name, Price and Kind fields from the old `inventory_edit_form.html`, posting to `/inventory/update/{{ product.sku }}/catalog` with `hx-target="#content-body"`; heading "Edit Product — Catalog"; Cancel button `hx-get="/inventory"`.

Create `web_interface/templates/partials/inventory_placement_form.html` — the SKU (disabled), Dispenser Slot (`min="0" step="1" required`), Inventory Count and a `track_inventory` checkbox, posting to `/inventory/update/{{ product.sku }}/placement`, same target and Cancel button; heading "Edit Product — Placement". Use `{{ inventory_count }}` and `{% if tracked %}checked{% endif %}`.

Delete `web_interface/templates/partials/inventory_edit_form.html`. Update `partials/inventory_table.html`'s Edit link into two links — "Catalog" (shown when `"edit_catalog" in perms`) and "Placement" (shown when `"edit_placement" in perms`).

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_web_routes.py -v`
Expected: PASS.

- [ ] **Step 5: Lint and commit**

```bash
ruff check --fix .
ruff format .
git add web_interface tests/test_web_routes.py
git commit -m "feat(access): split the product form into catalog and placement"
```

---

### Task 13: Setup code on the customer display

**Spec:** §3.1 ("publishes it to the customer display through `services/display_controller.py` (maintenance mode, 'Setup code: 1234 5678') for as long as setup mode lasts").

> **§3.5 NOTIFICATION — OWNER APPROVED 2026-09-25.** `DisplayCommand` in `services/mqtt_messages.py` carries only `mode`; there is nowhere to put the text the spec asks for. This task adds an **optional** `message: str | None = None` field. It is additive, no in-repo consumer or simulator subscribes to `cmd/display` (grep: only `display_controller.py` publishes it), and the model lives in `services/mqtt_messages.py`, not in `contracts/`, so no `CONTRACT_VERSION` bump is implied. The owner has approved this explicitly; proceed as written and record it in the pull-request deviations list. No further confirmation is needed.

**Files:**
- Modify: `services/mqtt_messages.py`, `services/display_controller.py`
- Test: `tests/test_display_controller.py`

**Interfaces:**
- Consumes: `DisplayMode.maintenance`.
- Produces:
  - `DisplayCommand.message: str | None = None`
  - `DisplayController.show_setup_code(code: str) -> None` — sets maintenance mode and publishes `message=f"Setup code: {code[:4]} {code[4:]}"`; stores the code so `update_for_state` cannot overwrite it
  - `DisplayController.clear_setup_code() -> None` — forgets the code and republishes the mode for the current FSM state
  - `DisplayController.setup_code: str | None` property
  - While a setup code is held, `update_for_state` records the state but does **not** publish, so the code stays on screen until setup finishes

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_display_controller.py` (follow the file's existing pattern for a fake MQTT client and loop):

```python
class TestSetupCode:
    def test_show_setup_code_publishes_maintenance_with_grouped_digits(self):
        display, client, _ = _wired_display()   # existing helper in this file
        display.show_setup_code("12345678")
        topic, command = _last_publish(client)
        assert topic == "cmd/display"
        assert command.mode is DisplayMode.maintenance
        assert command.message == "Setup code: 1234 5678"
        assert display.setup_code == "12345678"

    def test_state_changes_do_not_wipe_the_setup_code(self):
        display, client, _ = _wired_display()
        display.show_setup_code("12345678")
        before = _publish_count(client)
        display.update_for_state("dispensing")
        assert _publish_count(client) == before
        assert display.current_mode is DisplayMode.maintenance

    def test_clear_setup_code_returns_to_the_state_mode(self):
        display, client, _ = _wired_display()
        display.show_setup_code("12345678")
        display.update_for_state("idle")
        display.clear_setup_code()
        _, command = _last_publish(client)
        assert command.mode is DisplayMode.advertising
        assert command.message is None
        assert display.setup_code is None

    def test_clear_without_a_code_is_a_no_op(self):
        display, client, _ = _wired_display()
        before = _publish_count(client)
        display.clear_setup_code()
        assert _publish_count(client) == before

    def test_message_defaults_to_none_on_ordinary_commands(self):
        display, client, _ = _wired_display()
        display.update_for_state("dispensing")
        _, command = _last_publish(client)
        assert command.message is None
```

The helpers `_wired_display`, `_last_publish` and `_publish_count` may not exist under those names — reuse whatever `tests/test_display_controller.py` already does to build a controller with a stub MQTT client and inspect published commands, and name the new helpers consistently with the file.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_display_controller.py -v -k TestSetupCode`
Expected: FAIL — `AttributeError: 'DisplayController' object has no attribute 'show_setup_code'`.

- [ ] **Step 3: Implement**

In `services/mqtt_messages.py`:

```python
class DisplayCommand(BaseModel):
    """Command to change the customer-facing display mode."""

    mode: DisplayMode = Field(..., description="Display mode to switch to")
    message: str | None = Field(
        None,
        description=(
            "Optional line for the display to render under the mode, e.g. the "
            "setup code while the dashboard is in setup mode. Additive and "
            "optional: firmware that ignores it behaves exactly as before."
        ),
    )
```

In `services/display_controller.py`:

```python
    def __init__(self):
        self._current_mode: DisplayMode = DisplayMode.advertising
        self._mqtt_client = None
        self._loop = None
        self._setup_code: str | None = None
        self._last_state: str = "idle"

    @property
    def setup_code(self) -> str | None:
        return self._setup_code

    def show_setup_code(self, code: str) -> None:
        """Hold the setup code on the customer display for as long as setup lasts.

        Someone standing at the machine can read it; a remote stranger cannot.
        """
        self._setup_code = code
        self._current_mode = DisplayMode.maintenance
        logger.warning(f"Display: showing setup code {code[:4]} {code[4:]}")
        self._publish_mode(
            DisplayMode.maintenance, message=f"Setup code: {code[:4]} {code[4:]}"
        )

    def clear_setup_code(self) -> None:
        if self._setup_code is None:
            return
        self._setup_code = None
        mode = _STATE_TO_MODE.get(self._last_state, DisplayMode.advertising)
        self._current_mode = mode
        logger.info("Display: setup code cleared")
        self._publish_mode(mode)
```

`update_for_state` records `self._last_state = vmc_state` first and returns early while `self._setup_code is not None`. `_publish_mode` gains a `message: str | None = None` parameter and passes it into `DisplayCommand`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_display_controller.py tests/test_mqtt.py -v`
Expected: PASS — the 5 new tests and every existing display and MQTT test.

- [ ] **Step 5: Lint and commit**

```bash
ruff check --fix .
ruff format .
git add services/mqtt_messages.py services/display_controller.py tests/test_display_controller.py
git commit -m "feat(access): show the setup code on the customer display"
```

---

### Task 14: Setup mode gate and the owner wizard

**Spec:** §3.1 (setup mode, `POST /setup` step 1), §6 (corrupt access file).

**Files:**
- Modify: `web_interface/routes.py`
- Create: `web_interface/templates/setup.html`
- Test: `tests/test_web_routes.py`

**Interfaces:**
- Consumes: `AccessStore.setup_mode` / `begin_setup` / `verify_setup_code` / `create_user` (Tasks 4, 6), `DisplayController.show_setup_code` (Task 13).
- Produces:
  - `routes.set_display_controller(display) -> None` and module global `display_controller`
  - `routes.ensure_setup_mode() -> None` — called from `main.py` and at the first request; while `store.setup_mode` and not `store.setup_finalized`, calls `store.begin_setup()`, logs the plaintext at **warning** level, and calls `display_controller.show_setup_code(code)` once
  - An HTTP middleware on `app`: while the store is corrupt, every path except `/static/*` answers **503** with a plain "access file is corrupt" page; while `store.setup_mode`, every path except `/setup`, `/setup/codes` and `/static/*` redirects 303 to `/setup`
  - `GET /setup` — the wizard page. Renders the owner form; while a transfer is pending (Task 19) it asks for the transfer code instead.
  - `POST /setup` — `require_htmx`; fields `setup_code`, `name`, `email`, `pin`, `pin_confirm`, `shared_device` (checkbox). Back-off kind `setup`, subject `"setup"`. On success creates the owner, creates the device with the `shared` flag, trusts the owner, creates the session, sets both cookies, responds `HX-Redirect: /setup/codes`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_web_routes.py`:

```python
class TestSetupWizard:
    @pytest.fixture
    def fresh(self, tmp_path):
        from services.access import AccessStore
        from services.display_controller import DisplayController

        cfg = ConfigModel()
        store = AccessStore(path=tmp_path / "access.json")
        display = DisplayController()
        routes.set_config_object(cfg)
        routes.set_access_store(store)
        routes.set_display_controller(display)
        routes.ensure_setup_mode()
        with TestClient(app, follow_redirects=False) as c:
            c.headers["HX-Request"] = "true"
            yield c, store, display
        routes.set_access_store(None)
        routes.set_display_controller(None)

    def test_setup_mode_redirects_every_route(self, fresh):
        c, store, _ = fresh
        for path in ("/", "/status", "/inventory", "/login", "/health"):
            resp = c.get(path)
            assert resp.status_code == 303, path
            assert resp.headers["location"] == "/setup"

    def test_setup_page_itself_is_reachable(self, fresh):
        c, _, _ = fresh
        assert c.get("/setup").status_code == 200

    def test_the_code_is_logged_and_shown_on_the_display(self, fresh, caplog):
        c, store, display = fresh
        assert display.setup_code == store.pending_setup_code
        assert store.pending_setup_code is not None

    def test_a_wrong_setup_code_creates_no_owner(self, fresh):
        c, store, _ = fresh
        resp = c.post(
            "/setup",
            data={
                "setup_code": "00000000",
                "name": "Ada",
                "email": "ada@example.com",
                "pin": "1379",
                "pin_confirm": "1379",
            },
        )
        assert resp.status_code == 200
        assert store.owner() is None
        assert "code" in resp.text.lower()

    def test_repeated_wrong_setup_codes_back_off(self, fresh):
        c, store, _ = fresh
        body = {
            "setup_code": "00000000",
            "name": "Ada",
            "email": "",
            "pin": "1379",
            "pin_confirm": "1379",
        }
        for _ in range(3):
            c.post("/setup", data=body)
        resp = c.post("/setup", data=body)
        assert resp.status_code == 429

    def test_the_right_code_creates_the_owner_and_signs_them_in(self, fresh):
        c, store, _ = fresh
        code = store.pending_setup_code
        resp = c.post(
            "/setup",
            data={
                "setup_code": code,
                "name": "Ada",
                "email": "ada@example.com",
                "pin": "1379",
                "pin_confirm": "1379",
                "shared_device": "on",
            },
        )
        assert resp.headers["hx-redirect"] == "/setup/codes"
        owner = store.owner()
        assert owner.name == "Ada"
        assert c.cookies.get("vmc_session")
        assert c.cookies.get("vmc_device")
        device = next(iter(store.devices.values()))
        assert device.shared is True
        assert owner.id in device.trusted_user_ids

    def test_a_bad_pin_is_rejected_with_the_reason(self, fresh):
        c, store, _ = fresh
        resp = c.post(
            "/setup",
            data={
                "setup_code": store.pending_setup_code,
                "name": "Ada",
                "email": "",
                "pin": "1234",
                "pin_confirm": "1234",
            },
        )
        assert resp.status_code == 200
        assert "run" in resp.text.lower()
        assert store.owner() is None

    def test_mismatched_pin_confirmation_is_rejected(self, fresh):
        c, store, _ = fresh
        resp = c.post(
            "/setup",
            data={
                "setup_code": store.pending_setup_code,
                "name": "Ada",
                "email": "",
                "pin": "1379",
                "pin_confirm": "9042",
            },
        )
        assert store.owner() is None
        assert "match" in resp.text.lower()

    def test_a_lost_step_one_response_is_recovered_by_enrolling_with_the_code(
        self, fresh
    ):
        c, store, _ = fresh
        code = store.pending_setup_code
        c.post(
            "/setup",
            data={
                "setup_code": code,
                "name": "Ada",
                "email": "",
                "pin": "1379",
                "pin_confirm": "1379",
            },
        )
        # A second browser: PIN, then the setup code as the enrollment code.
        with TestClient(app, follow_redirects=False) as other:
            other.headers["HX-Request"] = "true"
            owner = store.owner()
            other.post("/login", data={"user_id": owner.id, "pin": "1379"})
            resp = other.post("/login/enroll", data={"code": code})
            assert resp.headers["hx-redirect"] == "/"

    def test_setup_post_without_the_htmx_header_is_forbidden(self, fresh):
        c, store, _ = fresh
        resp = c.post(
            "/setup",
            data={"setup_code": store.pending_setup_code, "name": "A", "email": "",
                  "pin": "1379", "pin_confirm": "1379"},
            headers={"HX-Request": ""},
        )
        assert resp.status_code == 403


class TestCorruptAccessFile:
    def test_every_route_serves_an_error_page_not_the_wizard(self, tmp_path):
        from services.access import AccessStore

        path = tmp_path / "access.json"
        path.write_text("{not json", encoding="utf-8")
        routes.set_config_object(ConfigModel())
        routes.set_access_store(AccessStore(path=path))
        with TestClient(app, follow_redirects=False) as c:
            for p in ("/", "/setup", "/login", "/status"):
                resp = c.get(p)
                assert resp.status_code == 503, p
                assert "corrupt" in resp.text.lower()
        routes.set_access_store(None)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_web_routes.py -v -k "TestSetupWizard or TestCorruptAccessFile"`
Expected: FAIL — `AttributeError: module 'web_interface.routes' has no attribute 'set_display_controller'`.

- [ ] **Step 3: Implement**

Add to `web_interface/routes.py`, beside the other setters:

```python
display_controller = None


def set_display_controller(display) -> None:
    global display_controller
    display_controller = display


def ensure_setup_mode() -> None:
    """Generate, log and display the setup code while no owner exists.

    The code exists only at the machine — the startup log and the customer
    display — so the wizard cannot be reached and claimed remotely (spec §3.1).
    """
    if access_store is None or access_store.corrupt:
        return
    if not access_store.setup_mode:
        if display_controller is not None:
            display_controller.clear_setup_code()
        return
    code = access_store.begin_setup()
    logger.warning(
        f"Dashboard is in SETUP MODE. Setup code: {code[:4]} {code[4:]} "
        "— enter it at /setup to create the owner account."
    )
    if display_controller is not None and display_controller.setup_code != code:
        display_controller.show_setup_code(code)
```

Add the gate as a middleware in `attach_routes` (registered on `app`, so it also covers `/static`):

```python
    SETUP_EXEMPT = ("/setup", "/setup/codes")

    @app.middleware("http")
    async def access_gate(request: Request, call_next):
        path = request.url.path
        if path.startswith("/static"):
            return await call_next(request)
        if access_store is None:
            return await call_next(request)
        if access_store.corrupt:
            # A corrupt access file must not silently become an open setup
            # wizard (spec §6). The VMC and MQTT client are unaffected.
            return HTMLResponse(
                "<h1>Access file is corrupt</h1><p>data/access.json could not be "
                "read. The machine keeps running; the dashboard is unavailable "
                "until an administrator restores or removes that file.</p>",
                status_code=503,
            )
        if access_store.setup_mode and path not in SETUP_EXEMPT:
            return RedirectResponse("/setup", status_code=303)
        return await call_next(request)
```

Add the wizard routes to the public router:

```python
    def _setup_page(request: Request, *, error=None, form=None, status_code=200,
                    headers=None):
        pending = access_store.pending_transfer if access_store else None
        return templates.TemplateResponse(
            "setup.html",
            {
                "request": request,
                "error": error,
                "form": form or {},
                "transfer": pending is not None,
            },
            status_code=status_code,
            headers=headers or {},
        )

    @public.get("/setup", response_class=HTMLResponse)
    async def setup_page(request: Request):
        if access_store is None:
            raise HTTPException(status_code=503, detail="Access store not loaded")
        ensure_setup_mode()
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
        form = {"name": name, "email": email}
        client = web_auth.client_key(request)
        transferring = access_store.pending_transfer is not None

        kind = "transfer" if transferring else "setup"
        subject = "transfer" if transferring else "setup"
        remaining = web_auth.backoff.check(kind, subject, client)
        if remaining is not None:
            return _setup_page(
                request,
                error=f"Too many attempts. Try again in {int(remaining) + 1} s.",
                form=form,
                status_code=429,
                headers={"Retry-After": str(int(remaining) + 1)},
            )

        code_ok = (
            access_store.verify_transfer_code(setup_code.strip())
            if transferring
            else access_store.verify_setup_code(setup_code.strip())
        )
        if not code_ok:
            web_auth.backoff.record_failure(kind, subject, client)
            return _setup_page(request, error="That code was not accepted", form=form)

        if pin != pin_confirm:
            return _setup_page(request, error="The two PINs do not match", form=form)
        problem = pin_problem(pin)
        if problem:
            return _setup_page(request, error=problem, form=form)

        web_auth.backoff.record_success(kind, subject, client)
        try:
            if transferring:
                owner = access_store.complete_transfer(name, email or None, pin)
            else:
                owner = access_store.create_user(name, email or None, Role.owner, pin)
        except OwnerExistsError:
            # Two racing submissions: the store rejects the second one.
            return _setup_page(
                request, error="This machine already has an owner", form=form
            )

        device, token = access_store.create_device(
            "Machine tablet" if shared_device else "Owner device",
            shared=shared_device is not None,
        )
        access_store.trust_device(device.id, owner.id)
        access_store.record_login(owner.id)
        session_id = access_store.create_session(owner.id, device.id)

        resp = HTMLResponse("", headers={"HX-Redirect": "/setup/codes"})
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
```

Import `pin_problem` from `services.auth_policy`, `OwnerExistsError` and `Role` from `services.access`, and `logger` from `loguru` in `routes.py`.

Create `web_interface/templates/setup.html`: the same page chrome as `login.html` (viewport meta, HTMX, Tailwind), an `<h1>` of "Set up this machine" (or "Take ownership" when `transfer`), a paragraph telling the user the code is in the machine's startup log and on the customer display, and a form `hx-post="/setup" hx-target="body" hx-swap="outerHTML"` with fields `setup_code`, `name`, `email`, `pin`, `pin_confirm` and a `shared_device` checkbox labelled "This browser is the machine's own tablet". Render `{{ error }}` in a `role="alert"` paragraph and refill `name`/`email` from `form`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_web_routes.py -v`
Expected: PASS. Every earlier test class must still pass — they seed an owner, so the gate lets them through.

- [ ] **Step 5: Lint and commit**

```bash
ruff check --fix .
ruff format .
git add web_interface tests/test_web_routes.py
git commit -m "feat(access): setup-mode gate and the owner wizard"
```

---

### Task 15: `/setup/codes` — the emergency-code page and Done

**Spec:** §3.1 step 2, §3.2.

**Files:**
- Modify: `web_interface/routes.py`
- Create: `web_interface/templates/setup_codes.html`
- Test: `tests/test_web_routes.py`

**Interfaces:**
- Consumes: `AccessStore.generate_emergency_codes` / `finalize_setup` (Task 6), `send_email` (Task 7), `require(Permission.manage_ownership)`.
- Produces:
  - `GET /setup/codes` — owner session required. Generates the 20 codes on first view and holds the plaintexts in a module-level `_pending_codes: list[str]` until Done; a reload before Done shows the same codes.
  - `POST /setup/codes/email` — `require_htmx`, `manage_ownership`; emails the codes; re-renders with a notice.
  - `POST /setup/codes/done` — `require_htmx`, `manage_ownership`; calls `finalize_setup()`, clears `_pending_codes`, calls `display_controller.clear_setup_code()`, responds `HX-Redirect: /`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_web_routes.py`, inside `TestSetupWizard` or a new `TestSetupCodes` class using the same `fresh` fixture:

```python
class TestSetupCodes:
    @pytest.fixture
    def after_step_one(self, tmp_path):
        from services.access import AccessStore
        from services.display_controller import DisplayController

        cfg = ConfigModel()
        store = AccessStore(path=tmp_path / "access.json")
        display = DisplayController()
        routes.set_config_object(cfg)
        routes.set_access_store(store)
        routes.set_display_controller(display)
        routes.ensure_setup_mode()
        with TestClient(app, follow_redirects=False) as c:
            c.headers["HX-Request"] = "true"
            c.post(
                "/setup",
                data={
                    "setup_code": store.pending_setup_code,
                    "name": "Ada",
                    "email": "ada@example.com",
                    "pin": "1379",
                    "pin_confirm": "1379",
                    "shared_device": "on",
                },
            )
            yield c, store, display
        routes.set_access_store(None)
        routes.set_display_controller(None)

    def test_twenty_codes_are_shown(self, after_step_one):
        c, store, _ = after_step_one
        resp = c.get("/setup/codes")
        assert resp.status_code == 200
        assert store.unused_emergency_code_count() == 20
        import re

        assert len(set(re.findall(r"\b\d{8}\b", resp.text))) >= 20

    def test_reloading_before_done_shows_the_same_codes(self, after_step_one):
        import re

        c, store, _ = after_step_one
        first = set(re.findall(r"\b\d{8}\b", c.get("/setup/codes").text))
        second = set(re.findall(r"\b\d{8}\b", c.get("/setup/codes").text))
        assert first == second
        assert store.unused_emergency_code_count() == 20

    def test_done_finalizes_setup_and_clears_the_display(self, after_step_one):
        c, store, display = after_step_one
        resp = c.post("/setup/codes/done", data={})
        assert resp.headers["hx-redirect"] == "/"
        assert store.setup_finalized is True
        assert display.setup_code is None

    def test_the_setup_code_stops_enrolling_after_done(self, after_step_one):
        c, store, _ = after_step_one
        code = store.pending_setup_code
        c.post("/setup/codes/done", data={})
        assert store.verify_setup_code(code or "") is False

    def test_codes_are_not_shown_again_after_done(self, after_step_one):
        import re

        c, store, _ = after_step_one
        c.get("/setup/codes")
        c.post("/setup/codes/done", data={})
        resp = c.get("/setup/codes", follow_redirects=False)
        assert resp.status_code in (303, 403) or not re.findall(r"\b\d{8}\b", resp.text)

    def test_email_button_uses_the_mailer(self, after_step_one, monkeypatch):
        c, store, _ = after_step_one
        routes.config.communication.email_gateway.smtp_server = "smtp.real.local"
        sent = {}

        async def fake_send_email(gateway, to, subject, body):
            sent["to"] = to
            sent["body"] = body
            return True

        monkeypatch.setattr(routes, "send_email", fake_send_email)
        c.get("/setup/codes")
        resp = c.post("/setup/codes/email", data={})
        assert resp.status_code == 200
        assert sent["to"] == "ada@example.com"
        assert len([w for w in sent["body"].split() if w.isdigit()]) >= 20

    def test_a_non_owner_cannot_reach_the_codes_page(self, after_step_one, tmp_path):
        from services.access import Role

        c, store, _ = after_step_one
        c.post("/setup/codes/done", data={})
        other, _ = make_client(store, Role.tech, name="Tim")
        assert other.get("/setup/codes").status_code == 403
        other.close()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_web_routes.py -v -k TestSetupCodes`
Expected: FAIL — 404 on `/setup/codes`.

- [ ] **Step 3: Implement**

Add near the other module globals in `web_interface/routes.py`:

```python
# Plaintext emergency codes, held only between generation and Done. After
# Done only their hashes exist, so they can never be shown again.
_pending_codes: list[str] = []
```

Add to the public router (they are exempt from the setup gate but still require an owner session, so they carry `require(Permission.manage_ownership)` themselves):

```python
    def _codes_page(request: Request, *, notice=None, error=None):
        return templates.TemplateResponse(
            "setup_codes.html",
            web_auth.template_context(
                request,
                codes=_pending_codes,
                notice=notice,
                error=error,
                can_email=config.communication.email_gateway.is_configured,
            ),
        )

    @public.get(
        "/setup/codes",
        response_class=HTMLResponse,
        dependencies=[Depends(web_auth.require(Permission.manage_ownership))],
    )
    async def setup_codes(request: Request):
        global _pending_codes
        if access_store.setup_finalized and not _pending_codes:
            return RedirectResponse("/", status_code=303)
        if not _pending_codes:
            _pending_codes = access_store.generate_emergency_codes()
        return _codes_page(request)

    @public.post(
        "/setup/codes/email",
        response_class=HTMLResponse,
        dependencies=[
            Depends(require_htmx),
            Depends(web_auth.require(Permission.manage_ownership)),
        ],
    )
    async def email_setup_codes(request: Request):
        principal = web_auth.current_principal(request)
        gateway = config.communication.email_gateway
        if not principal.user.email or not gateway.is_configured:
            return _codes_page(request, error="Email is not configured")
        body = (
            "Emergency codes for your ice-colder machine. Each works once, to "
            "trust a new device or to authorise an ownership transfer. Keep "
            "them somewhere other than the machine.\n\n"
            + "\n".join(_pending_codes)
        )
        ok = await send_email(
            gateway, principal.user.email, "Ice-colder emergency codes", body
        )
        if not ok:
            return _codes_page(request, error="Email could not be sent")
        return _codes_page(request, notice="Sent.")

    @public.post(
        "/setup/codes/done",
        dependencies=[
            Depends(require_htmx),
            Depends(web_auth.require(Permission.manage_ownership)),
        ],
    )
    async def finish_setup(request: Request):
        global _pending_codes
        access_store.finalize_setup()
        _pending_codes = []
        if display_controller is not None:
            display_controller.clear_setup_code()
        return HTMLResponse("", headers={"HX-Redirect": "/"})
```

Create `web_interface/templates/setup_codes.html`: page chrome as in `setup.html`; a heading "Emergency codes"; a warning that they are shown **once**; the codes in a monospace grid, each in its own element so `\b\d{8}\b` matches; an "Email these to me" button (`hx-post="/setup/codes/email"`, rendered only when `can_email`) and a **Done** button (`hx-post="/setup/codes/done"`); `notice` and `error` paragraphs.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_web_routes.py -v`
Expected: PASS.

- [ ] **Step 5: Lint and commit**

```bash
ruff check --fix .
ruff format .
git add web_interface tests/test_web_routes.py
git commit -m "feat(access): emergency-code page and setup finalisation"
```

---

### Task 16: User management routes and templates

**Spec:** §4.1 (user routes), §4 ("any write whose target user is the owner requires `manage_ownership`").

**Files:**
- Modify: `web_interface/routes.py`
- Create: `web_interface/templates/partials/users_list.html`, `web_interface/templates/partials/user_form.html`
- Test: `tests/test_web_routes.py`

**Interfaces:**
- Consumes: `AccessStore` user methods (Task 4), `pin_problem` (Task 1), `require(Permission.manage_users)`.
- Produces (all render partials into the dashboard's `#content-body`, all POSTs carry `require_htmx`):
  - `GET /users` → `partials/users_list.html` — `manage_users`
  - `GET /users/new`, `POST /users/new` (`name`, `email`, `role`, `pin`) — `manage_users`; a secretary choosing `owner` gets 403
  - `POST /users/{id}/disable`, `POST /users/{id}/enable` — `manage_users`
  - `POST /users/{id}/reset-pin` (`pin`) — `manage_users`
  - `POST /users/{id}/delete` — `manage_users`
  - Helper `_guard_owner_target(principal, user_id)` — raises 403 when the target is the owner and the caller lacks `manage_ownership`
  - `partials/users_list.html` context: `users` (all users), `owner_id`, `unused_codes` (int), `pending_transfer` (dict or None), `devices_count` per user

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_web_routes.py`:

```python
class TestUserManagement:
    def test_owner_sees_the_user_list(self, client, wired):
        _, _, _, store = wired
        resp = client.get("/users")
        assert resp.status_code == 200
        assert "Ada" in resp.text

    def test_list_shows_the_unused_emergency_code_count(self, client, wired):
        _, _, _, store = wired
        store.generate_emergency_codes()
        assert "20" in client.get("/users").text

    def test_tech_and_loader_get_403(self, login_as):
        for role in (Role.tech, Role.loader):
            assert login_as(role).get("/users").status_code == 403

    def test_owner_creates_a_loader(self, client, wired):
        _, _, _, store = wired
        resp = client.post(
            "/users/new",
            data={"name": "Lee", "email": "lee@example.com", "role": "loader",
                  "pin": "9042"},
        )
        assert resp.status_code == 200
        assert any(u.name == "Lee" for u in store.users.values())

    def test_a_bad_pin_is_refused_with_the_reason(self, client, wired):
        _, _, _, store = wired
        before = len(store.users)
        resp = client.post(
            "/users/new",
            data={"name": "Lee", "email": "", "role": "loader", "pin": "1111"},
        )
        assert "same digit" in resp.text.lower()
        assert len(store.users) == before

    def test_secretary_may_not_create_an_owner(self, login_as, wired):
        _, _, _, store = wired
        sec = login_as(Role.secretary, name="Sue")
        resp = sec.post(
            "/users/new",
            data={"name": "Bea", "email": "", "role": "owner", "pin": "9042"},
        )
        assert resp.status_code == 403

    def test_secretary_may_not_disable_delete_or_reset_the_owner(
        self, login_as, client, wired
    ):
        _, _, _, store = wired
        owner = store.owner()
        sec = login_as(Role.secretary, name="Sue")
        assert sec.post(f"/users/{owner.id}/disable", data={}).status_code == 403
        assert sec.post(f"/users/{owner.id}/delete", data={}).status_code == 403
        assert (
            sec.post(f"/users/{owner.id}/reset-pin", data={"pin": "9042"}).status_code
            == 403
        )
        assert store.owner().disabled is False

    def test_secretary_may_manage_a_loader(self, login_as, client, wired):
        _, _, _, store = wired
        sec = login_as(Role.secretary, name="Sue")
        lee = login_as(Role.loader, name="Lee")
        lee_user = next(u for u in store.users.values() if u.name == "Lee")
        assert sec.post(f"/users/{lee_user.id}/disable", data={}).status_code == 200
        assert store.get_user(lee_user.id).disabled is True
        assert sec.post(f"/users/{lee_user.id}/enable", data={}).status_code == 200
        assert store.get_user(lee_user.id).disabled is False

    def test_reset_pin_untrusts_every_device(self, client, login_as, wired):
        _, _, _, store = wired
        login_as(Role.loader, name="Lee")
        lee = next(u for u in store.users.values() if u.name == "Lee")
        assert any(lee.id in d.trusted_user_ids for d in store.devices.values())
        resp = client.post(f"/users/{lee.id}/reset-pin", data={"pin": "9042"})
        assert resp.status_code == 200
        assert store.verify_user_pin(lee.id, "9042")
        assert not any(lee.id in d.trusted_user_ids for d in store.devices.values())

    def test_delete_removes_the_user(self, client, login_as, wired):
        _, _, _, store = wired
        login_as(Role.loader, name="Lee")
        lee = next(u for u in store.users.values() if u.name == "Lee")
        assert client.post(f"/users/{lee.id}/delete", data={}).status_code == 200
        assert store.get_user(lee.id) is None

    def test_user_writes_need_the_htmx_header(self, client, wired):
        _, _, _, store = wired
        resp = client.post(
            "/users/new",
            data={"name": "Lee", "email": "", "role": "loader", "pin": "9042"},
            headers={"HX-Request": ""},
        )
        assert resp.status_code == 403
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_web_routes.py -v -k TestUserManagement`
Expected: FAIL — 404 on `/users`.

- [ ] **Step 3: Implement**

Add to the gated `router` in `web_interface/routes.py`:

```python
    def _users_list(request: Request, *, error=None, notice=None):
        pending = access_store.pending_transfer
        owner = access_store.owner()
        return templates.TemplateResponse(
            "partials/users_list.html",
            web_auth.template_context(
                request,
                users=sorted(access_store.users.values(), key=lambda u: u.name),
                owner_id=owner.id if owner else None,
                devices=sorted(access_store.devices.values(), key=lambda d: d.label),
                device_counts={
                    u.id: sum(
                        1
                        for d in access_store.devices.values()
                        if u.id in d.trusted_user_ids
                    )
                    for u in access_store.users.values()
                },
                unused_codes=access_store.unused_emergency_code_count(),
                pending_transfer=pending,
                error=error,
                notice=notice,
            ),
        )

    def _guard_owner_target(principal, user_id: str) -> None:
        """A secretary may manage everyone except the owner (spec §4)."""
        owner = access_store.owner()
        if (
            owner is not None
            and owner.id == user_id
            and Permission.manage_ownership not in principal.perms
        ):
            raise HTTPException(status_code=403, detail="Not permitted")

    @router.get(
        "/users",
        response_class=HTMLResponse,
        dependencies=[Depends(web_auth.require(Permission.manage_users))],
    )
    async def users_list(request: Request):
        return _users_list(request)

    @router.get(
        "/users/new",
        response_class=HTMLResponse,
        dependencies=[Depends(web_auth.require(Permission.manage_users))],
    )
    async def new_user_form(request: Request):
        principal = web_auth.current_principal(request)
        return templates.TemplateResponse(
            "partials/user_form.html",
            web_auth.template_context(
                request,
                roles=[r for r in Role if r is not Role.owner
                       or Permission.manage_ownership in principal.perms],
                error=None,
                form={},
            ),
        )

    @router.post(
        "/users/new",
        response_class=HTMLResponse,
        dependencies=[
            Depends(require_htmx),
            Depends(web_auth.require(Permission.manage_users)),
        ],
    )
    async def create_user_route(
        request: Request,
        principal=Depends(web_auth.require(Permission.manage_users)),
        name: str = Form(...),
        email: str = Form(""),
        role: str = Form(...),
        pin: str = Form(...),
    ):
        try:
            wanted = Role(role)
        except ValueError:
            raise HTTPException(status_code=400, detail="Unknown role")
        if wanted is Role.owner and Permission.manage_ownership not in principal.perms:
            raise HTTPException(status_code=403, detail="Not permitted")
        problem = pin_problem(pin)
        if problem:
            return _users_list(request, error=problem)
        try:
            access_store.create_user(name, email or None, wanted, pin)
        except OwnerExistsError as e:
            return _users_list(request, error=str(e))
        return _users_list(request, notice=f"Added {name}")

    @router.post(
        "/users/{user_id}/disable",
        response_class=HTMLResponse,
        dependencies=[
            Depends(require_htmx),
            Depends(web_auth.require(Permission.manage_users)),
        ],
    )
    async def disable_user(
        request: Request,
        user_id: str,
        principal=Depends(web_auth.require(Permission.manage_users)),
    ):
        _guard_owner_target(principal, user_id)
        access_store.set_user_disabled(user_id, True)
        access_store.end_sessions_for_user(user_id)
        return _users_list(request)
```

Write `enable_user`, `reset_user_pin` and `delete_user_route` the same way: `_guard_owner_target` first, then `set_user_disabled(user_id, False)` / `set_user_pin(user_id, pin)` (validating with `pin_problem` and returning `_users_list(request, error=problem)` on failure) / `delete_user(user_id)` plus `end_sessions_for_user(user_id)`. Each returns `_users_list(request, notice=...)`.

Create `web_interface/templates/partials/users_list.html`: a table of users (name, role, email, disabled, last login, device count) with Disable/Enable, Reset PIN and Delete buttons — each `hx-post` targeting `#content-body` — an "Add user" button (`hx-get="/users/new"`), a line reading "Unused emergency codes: {{ unused_codes }}", a `{% if pending_transfer %}` block (Task 18 fills in its Cancel button), and `error` / `notice` paragraphs. Hide the owner's Disable/Delete/Reset buttons unless `"manage_ownership" in perms`.

Create `web_interface/templates/partials/user_form.html`: name, email, a `role` select built from `roles`, and a PIN field, posting to `/users/new` with `hx-target="#content-body"`, plus a Cancel button `hx-get="/users"`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_web_routes.py -v`
Expected: PASS.

- [ ] **Step 5: Lint and commit**

```bash
ruff check --fix .
ruff format .
git add web_interface tests/test_web_routes.py
git commit -m "feat(access): user management routes and Users tab partials"
```

---

### Task 17: Device management routes

**Spec:** §4.1 (`GET /devices`, `POST /devices/{id}/forget`, `POST /devices/{id}/shared`).

**Files:**
- Modify: `web_interface/routes.py`
- Create: `web_interface/templates/partials/devices_list.html`
- Test: `tests/test_web_routes.py`

**Interfaces:**
- Consumes: `AccessStore.devices` / `forget_device` / `set_device_shared` (Task 4).
- Produces:
  - `GET /devices` → `partials/devices_list.html` — `manage_users`
  - `POST /devices/{id}/forget` — `manage_users`, `require_htmx`; also ends every session on that device
  - `POST /devices/{id}/shared` — `manage_users`, `require_htmx`; toggles the flag
  - `partials/devices_list.html` context: `devices`, `user_names` (`dict[str, str]`)
  - `AccessStore.end_sessions_for_device(device_id: str) -> None` (new, mirrors `end_sessions_for_user`)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_web_routes.py`:

```python
class TestDeviceManagement:
    def test_owner_sees_devices_with_trusted_names(self, client, wired):
        _, _, _, store = wired
        assert "Ada" in client.get("/devices").text

    def test_tech_gets_403(self, login_as):
        assert login_as(Role.tech).get("/devices").status_code == 403

    def test_forget_removes_the_device_and_its_sessions(self, client, login_as, wired):
        _, _, _, store = wired
        lee_client = login_as(Role.loader, name="Lee")
        lee = next(u for u in store.users.values() if u.name == "Lee")
        device = next(d for d in store.devices.values() if lee.id in d.trusted_user_ids)
        assert client.post(f"/devices/{device.id}/forget", data={}).status_code == 200
        assert device.id not in store.devices
        assert lee_client.get("/status").status_code == 401

    def test_shared_toggle_flips_the_flag(self, client, wired):
        _, _, _, store = wired
        device = next(iter(store.devices.values()))
        before = device.shared
        assert client.post(f"/devices/{device.id}/shared", data={}).status_code == 200
        assert store.devices[device.id].shared is not before

    def test_device_writes_need_the_htmx_header(self, client, wired):
        _, _, _, store = wired
        device = next(iter(store.devices.values()))
        resp = client.post(
            f"/devices/{device.id}/shared", data={}, headers={"HX-Request": ""}
        )
        assert resp.status_code == 403
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_web_routes.py -v -k TestDeviceManagement`
Expected: FAIL — 404 on `/devices`.

- [ ] **Step 3: Implement**

Add to `services/access.py`:

```python
    def end_sessions_for_device(self, device_id: str) -> None:
        for sid, session in list(self._sessions.items()):
            if session.device_id == device_id:
                del self._sessions[sid]
```

Add to the gated router in `web_interface/routes.py`:

```python
    def _devices_list(request: Request, *, notice=None):
        return templates.TemplateResponse(
            "partials/devices_list.html",
            web_auth.template_context(
                request,
                devices=sorted(access_store.devices.values(), key=lambda d: d.label),
                user_names={u.id: u.name for u in access_store.users.values()},
                notice=notice,
            ),
        )

    @router.get(
        "/devices",
        response_class=HTMLResponse,
        dependencies=[Depends(web_auth.require(Permission.manage_users))],
    )
    async def devices_list(request: Request):
        return _devices_list(request)

    @router.post(
        "/devices/{device_id}/forget",
        response_class=HTMLResponse,
        dependencies=[
            Depends(require_htmx),
            Depends(web_auth.require(Permission.manage_users)),
        ],
    )
    async def forget_device_route(request: Request, device_id: str):
        access_store.end_sessions_for_device(device_id)
        access_store.forget_device(device_id)
        return _devices_list(request, notice="Device forgotten")

    @router.post(
        "/devices/{device_id}/shared",
        response_class=HTMLResponse,
        dependencies=[
            Depends(require_htmx),
            Depends(web_auth.require(Permission.manage_users)),
        ],
    )
    async def toggle_device_shared(request: Request, device_id: str):
        device = access_store.devices.get(device_id)
        if device is None:
            raise HTTPException(status_code=404, detail="No such device")
        access_store.set_device_shared(device_id, not device.shared)
        return _devices_list(request)
```

Create `web_interface/templates/partials/devices_list.html`: a table with label, shared flag, the trusted users' names resolved through `user_names`, and last seen; a Forget button and a "Shared / Personal" toggle button per row, each `hx-post` targeting `#content-body`; and a `notice` paragraph. Add a "Devices" button beside "Users" in `dashboard.html`, gated on `"manage_users" in perms`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_web_routes.py -v`
Expected: PASS.

- [ ] **Step 5: Lint and commit**

```bash
ruff check --fix .
ruff format .
git add services/access.py web_interface tests/test_web_routes.py
git commit -m "feat(access): device list, forget and shared toggle"
```

---

### Task 18: Transfer start and cancel, code regeneration, machine report

**Spec:** §3.3 step 1, §3.2 (regenerate from the Users area), §3.4 (report on demand).

**Files:**
- Modify: `web_interface/routes.py`, `web_interface/templates/partials/users_list.html`
- Test: `tests/test_web_routes.py`

**Interfaces:**
- Consumes: `AccessStore.start_transfer` / `cancel_transfer` / `consume_emergency_code` / `generate_emergency_codes` / `machine_report` / `verify_user_pin` (Tasks 4, 6), `send_email` (Task 7).
- Produces (all `manage_ownership` + `require_htmx`, all returning `partials/users_list.html`):
  - `POST /users/transfer` (`pin`, `emergency_code`) — back-off kinds `pin` (subject: the owner's id) and `transfer` (subject: `"pool"`). On success consumes the emergency code with `used_for="transfer"`, records the pending transfer, and renders the list with `transfer_code` shown **once**.
  - `POST /users/transfer/cancel` (`pin`) — back-off kind `pin`.
  - `POST /users/codes/regenerate` (`pin`) — replaces the pool and renders the list with `new_codes` shown once.
  - `POST /users/report` — emails `store.machine_report(config)` to the owner.
  - `partials/users_list.html` gains optional `transfer_code`, `new_codes` and the `pending_transfer` Cancel form.

- [ ] **Step 1: Write the failing tests**

```python
class TestTransferStart:
    @pytest.fixture
    def owned(self, client, wired):
        _, _, _, store = wired
        codes = store.generate_emergency_codes()
        return client, store, store.owner(), codes

    def test_start_shows_a_transfer_code_and_consumes_an_emergency_code(self, owned):
        import re

        c, store, owner, codes = owned
        resp = c.post(
            "/users/transfer", data={"pin": "1379", "emergency_code": codes[0]}
        )
        assert resp.status_code == 200
        assert re.search(r"\b\d{8}\b", resp.text)
        assert store.pending_transfer is not None
        assert store.unused_emergency_code_count() == 19

    def test_the_old_owner_still_works_while_a_transfer_is_pending(self, owned):
        c, store, owner, codes = owned
        c.post("/users/transfer", data={"pin": "1379", "emergency_code": codes[0]})
        assert c.get("/status").status_code == 200
        assert store.owner().id == owner.id

    def test_a_wrong_pin_starts_nothing(self, owned):
        c, store, owner, codes = owned
        resp = c.post(
            "/users/transfer", data={"pin": "9999", "emergency_code": codes[0]}
        )
        assert store.pending_transfer is None
        assert store.unused_emergency_code_count() == 20
        assert resp.status_code in (200, 429)

    def test_a_wrong_emergency_code_starts_nothing(self, owned):
        c, store, owner, codes = owned
        c.post("/users/transfer", data={"pin": "1379", "emergency_code": "00000000"})
        assert store.pending_transfer is None

    def test_cancel_clears_the_pending_transfer(self, owned):
        c, store, owner, codes = owned
        c.post("/users/transfer", data={"pin": "1379", "emergency_code": codes[0]})
        assert c.post("/users/transfer/cancel", data={"pin": "1379"}).status_code == 200
        assert store.pending_transfer is None
        assert store.owner().id == owner.id

    def test_a_secretary_cannot_start_or_cancel(self, login_as, owned):
        c, store, owner, codes = owned
        sec = login_as(Role.secretary, name="Sue")
        assert (
            sec.post(
                "/users/transfer", data={"pin": "1379", "emergency_code": codes[1]}
            ).status_code
            == 403
        )
        assert sec.post("/users/transfer/cancel", data={"pin": "1379"}).status_code == 403


class TestCodeRegenerationAndReport:
    def test_regenerate_replaces_the_pool_and_shows_the_new_codes(self, client, wired):
        import re

        _, _, _, store = wired
        old = store.generate_emergency_codes()
        store.consume_emergency_code(old[0], store.owner().id, "enroll")
        resp = client.post("/users/codes/regenerate", data={"pin": "1379"})
        assert resp.status_code == 200
        assert store.unused_emergency_code_count() == 20
        shown = set(re.findall(r"\b\d{8}\b", resp.text))
        assert len(shown) >= 20
        assert not shown & set(old)

    def test_regenerate_needs_the_owner_pin(self, client, wired):
        _, _, _, store = wired
        store.generate_emergency_codes()
        client.post("/users/codes/regenerate", data={"pin": "9999"})
        assert store.unused_emergency_code_count() == 20

    def test_a_secretary_cannot_regenerate(self, login_as, wired):
        sec = login_as(Role.secretary, name="Sue")
        assert sec.post("/users/codes/regenerate", data={"pin": "1379"}).status_code == 403

    def test_report_is_emailed_to_the_owner(self, client, wired, monkeypatch):
        cfg, _, _, store = wired
        cfg.communication.email_gateway.smtp_server = "smtp.real.local"
        sent = {}

        async def fake_send_email(gateway, to, subject, body):
            sent["to"] = to
            sent["body"] = body
            return True

        monkeypatch.setattr(routes, "send_email", fake_send_email)
        resp = client.post("/users/report", data={})
        assert resp.status_code == 200
        assert sent["to"] == store.owner().email
        assert "Ada" in sent["body"]

    def test_a_tech_cannot_request_the_report(self, login_as):
        assert login_as(Role.tech).post("/users/report", data={}).status_code == 403
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_web_routes.py -v -k "TestTransferStart or TestCodeRegenerationAndReport"`
Expected: FAIL — 404 on `/users/transfer`.

- [ ] **Step 3: Implement**

Extend `_users_list` with `transfer_code=None, new_codes=None` parameters, passed straight into the template context. Add to the gated router:

```python
    def _check_owner_pin(request: Request, principal, pin: str) -> str | None:
        """Verify the caller's own PIN under back-off. Returns an error message."""
        client = web_auth.client_key(request)
        trusted = web_auth.is_trusted_client(request, principal.user.id)
        remaining = web_auth.backoff.check(
            "pin", principal.user.id, client, trusted=trusted
        )
        if remaining is not None:
            return f"Too many attempts. Try again in {int(remaining) + 1} s."
        if not access_store.verify_user_pin(principal.user.id, pin):
            web_auth.backoff.record_failure(
                "pin", principal.user.id, client, trusted=trusted
            )
            return "Wrong PIN"
        web_auth.backoff.record_success(
            "pin", principal.user.id, client, trusted=trusted
        )
        return None

    @router.post(
        "/users/transfer",
        response_class=HTMLResponse,
        dependencies=[
            Depends(require_htmx),
            Depends(web_auth.require(Permission.manage_ownership)),
        ],
    )
    async def start_transfer_route(
        request: Request,
        principal=Depends(web_auth.require(Permission.manage_ownership)),
        pin: str = Form(...),
        emergency_code: str = Form(...),
    ):
        error = _check_owner_pin(request, principal, pin)
        if error:
            return _users_list(request, error=error)
        client = web_auth.client_key(request)
        remaining = web_auth.backoff.check("transfer", "pool", client)
        if remaining is not None:
            return _users_list(
                request, error=f"Too many attempts. Wait {int(remaining) + 1} s."
            )
        if not access_store.consume_emergency_code(
            emergency_code.strip(), principal.user.id, "transfer"
        ):
            web_auth.backoff.record_failure("transfer", "pool", client)
            return _users_list(request, error="That emergency code was not accepted")
        web_auth.backoff.record_success("transfer", "pool", client)
        code = access_store.start_transfer(principal.user.id)
        return _users_list(request, transfer_code=code)

    @router.post(
        "/users/transfer/cancel",
        response_class=HTMLResponse,
        dependencies=[
            Depends(require_htmx),
            Depends(web_auth.require(Permission.manage_ownership)),
        ],
    )
    async def cancel_transfer_route(
        request: Request,
        principal=Depends(web_auth.require(Permission.manage_ownership)),
        pin: str = Form(...),
    ):
        error = _check_owner_pin(request, principal, pin)
        if error:
            return _users_list(request, error=error)
        access_store.cancel_transfer()
        return _users_list(request, notice="Transfer cancelled")

    @router.post(
        "/users/codes/regenerate",
        response_class=HTMLResponse,
        dependencies=[
            Depends(require_htmx),
            Depends(web_auth.require(Permission.manage_ownership)),
        ],
    )
    async def regenerate_codes_route(
        request: Request,
        principal=Depends(web_auth.require(Permission.manage_ownership)),
        pin: str = Form(...),
    ):
        error = _check_owner_pin(request, principal, pin)
        if error:
            return _users_list(request, error=error)
        codes = access_store.generate_emergency_codes()
        return _users_list(request, new_codes=codes)

    @router.post(
        "/users/report",
        response_class=HTMLResponse,
        dependencies=[
            Depends(require_htmx),
            Depends(web_auth.require(Permission.manage_ownership)),
        ],
    )
    async def machine_report_route(
        request: Request,
        principal=Depends(web_auth.require(Permission.manage_ownership)),
    ):
        gateway = config.communication.email_gateway
        if not principal.user.email or not gateway.is_configured:
            return _users_list(request, error="Email is not configured")
        ok = await send_email(
            gateway,
            principal.user.email,
            "Ice-colder machine report",
            access_store.machine_report(config),
        )
        return _users_list(
            request,
            notice="Report sent." if ok else None,
            error=None if ok else "Report could not be sent",
        )
```

In `partials/users_list.html` add, all inside `{% if "manage_ownership" in perms %}`: a **Transfer ownership** form (`pin`, `emergency_code`) posting to `/users/transfer`; a `{% if pending_transfer %}` block showing `started_at` / `expires_at` with a Cancel form (`pin`) posting to `/users/transfer/cancel`; a **Regenerate emergency codes** form (`pin`) posting to `/users/codes/regenerate`; an **Email machine report** button posting to `/users/report`; and `{% if transfer_code %}` / `{% if new_codes %}` blocks rendering those values in monospace with a "shown once" warning, each 8-digit code in its own element.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_web_routes.py -v`
Expected: PASS.

- [ ] **Step 5: Lint and commit**

```bash
ruff check --fix .
ruff format .
git add web_interface tests/test_web_routes.py
git commit -m "feat(access): transfer start and cancel, code regeneration, machine report"
```

---

### Task 19: Completing a transfer and the user-review step

**Spec:** §3.3 steps 2–4.

**Files:**
- Modify: `web_interface/routes.py`, `web_interface/templates/setup.html`
- Create: `web_interface/templates/setup_review_user.html`
- Test: `tests/test_web_routes.py`

**Interfaces:**
- Consumes: `AccessStore.complete_transfer` (Task 6), the `/setup` POST already branching on `pending_transfer` (Task 14).
- Produces:
  - The access gate (Task 14) also lets `/setup` through while `store.pending_transfer is not None`, **without** redirecting anything else — the old owner's dashboard keeps working.
  - `GET /setup/review` — `manage_ownership`; renders `setup_review_user.html` for the next retained user, or redirects to `/setup/codes` when none are left
  - `POST /setup/review/{user_id}/keep` and `POST /setup/review/{user_id}/remove` — `manage_ownership`, `require_htmx`; apply immediately and render the next user
  - After `POST /setup` completes a transfer, the response redirects to `/setup/review` instead of `/setup/codes`

- [ ] **Step 1: Write the failing tests**

```python
class TestTransferCompletion:
    @pytest.fixture
    def pending(self, client, wired):
        _, _, _, store = wired
        store.create_user("Tim", "tim@example.com", Role.tech, "2468")
        codes = store.generate_emergency_codes()
        resp = client.post(
            "/users/transfer", data={"pin": "1379", "emergency_code": codes[0]}
        )
        import re

        code = re.search(r"\b\d{8}\b", resp.text).group(0)
        return client, store, code

    def test_setup_is_reachable_while_a_transfer_is_pending(self, pending):
        c, store, code = pending
        with TestClient(app, follow_redirects=False) as fresh:
            assert fresh.get("/setup").status_code == 200

    def test_the_rest_of_the_dashboard_is_not_redirected(self, pending):
        c, store, code = pending
        assert c.get("/status").status_code == 200

    def test_the_transfer_code_completes_the_swap(self, pending):
        c, store, code = pending
        old_owner_id = store.owner().id
        with TestClient(app, follow_redirects=False) as fresh:
            fresh.headers["HX-Request"] = "true"
            resp = fresh.post(
                "/setup",
                data={
                    "setup_code": code,
                    "name": "Bea",
                    "email": "bea@example.com",
                    "pin": "9042",
                    "pin_confirm": "9042",
                },
            )
            assert resp.headers["hx-redirect"] == "/setup/review"
            assert fresh.cookies.get("vmc_session")
        assert store.owner().name == "Bea"
        assert store.get_user(old_owner_id) is None
        assert store.pending_transfer is None
        assert store.unused_emergency_code_count() == 0

    def test_the_old_owners_session_is_dead_afterwards(self, pending):
        c, store, code = pending
        with TestClient(app, follow_redirects=False) as fresh:
            fresh.headers["HX-Request"] = "true"
            fresh.post(
                "/setup",
                data={"setup_code": code, "name": "Bea", "email": "", "pin": "9042",
                      "pin_confirm": "9042"},
            )
        assert c.get("/status").status_code == 401

    def test_a_wrong_transfer_code_changes_nothing(self, pending):
        c, store, code = pending
        owner_id = store.owner().id
        with TestClient(app, follow_redirects=False) as fresh:
            fresh.headers["HX-Request"] = "true"
            fresh.post(
                "/setup",
                data={"setup_code": "00000000", "name": "Bea", "email": "",
                      "pin": "9042", "pin_confirm": "9042"},
            )
        assert store.owner().id == owner_id

    def test_review_walks_the_retained_users(self, pending):
        c, store, code = pending
        with TestClient(app, follow_redirects=False) as fresh:
            fresh.headers["HX-Request"] = "true"
            fresh.post(
                "/setup",
                data={"setup_code": code, "name": "Bea", "email": "", "pin": "9042",
                      "pin_confirm": "9042"},
            )
            page = fresh.get("/setup/review")
            assert page.status_code == 200
            assert "Tim" in page.text
            tim = next(u for u in store.users.values() if u.name == "Tim")
            resp = fresh.post(f"/setup/review/{tim.id}/remove", data={})
            assert resp.status_code in (200, 303)
            assert store.get_user(tim.id) is None
            assert fresh.get("/setup/review", follow_redirects=False).status_code == 303

    def test_keeping_a_user_leaves_them_alone(self, pending):
        c, store, code = pending
        with TestClient(app, follow_redirects=False) as fresh:
            fresh.headers["HX-Request"] = "true"
            fresh.post(
                "/setup",
                data={"setup_code": code, "name": "Bea", "email": "", "pin": "9042",
                      "pin_confirm": "9042"},
            )
            tim = next(u for u in store.users.values() if u.name == "Tim")
            fresh.post(f"/setup/review/{tim.id}/keep", data={})
        assert store.get_user(tim.id) is not None

    def test_the_transfer_code_still_enrolls_the_new_owner_before_done(self, pending):
        c, store, code = pending
        with TestClient(app, follow_redirects=False) as fresh:
            fresh.headers["HX-Request"] = "true"
            fresh.post(
                "/setup",
                data={"setup_code": code, "name": "Bea", "email": "", "pin": "9042",
                      "pin_confirm": "9042"},
            )
        # Simulate the lost response: a second browser signs in with the PIN.
        with TestClient(app, follow_redirects=False) as other:
            other.headers["HX-Request"] = "true"
            bea = store.owner()
            other.post("/login", data={"user_id": bea.id, "pin": "9042"})
            resp = other.post("/login/enroll", data={"code": code})
            assert resp.headers["hx-redirect"] == "/"
```

`test_the_transfer_code_still_enrolls_the_new_owner_before_done` depends on `complete_transfer` leaving the transfer-code hash usable until Done. Implement it by having `complete_transfer` move the hash into `setup` as the enrollment code (see below) rather than by keeping `pending_transfer` alive.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_web_routes.py -v -k TestTransferCompletion`
Expected: FAIL — `/setup` is not reachable while an owner exists.

- [ ] **Step 3: Implement**

In `services/access.py`, `complete_transfer` gains a final step before `save()` so the incoming owner can still enroll with the transfer code until Done (spec §3.3 step 4):

```python
        self._setup = {
            "setup_code_hash": self._pending_transfer["transfer_code_hash"],
            "finalized": False,
        }
```

placed **before** `self._pending_transfer = None`. `verify_setup_code` already refuses once `finalized` is true, so Done closes it. Add a test for this in `tests/test_access.py`:

```python
    def test_the_transfer_code_enrolls_the_new_owner_until_done(self, seeded):
        s, owner, _, _ = seeded
        code = s.start_transfer(owner.id)
        s.complete_transfer("Bea", None, "9042")
        assert s.verify_setup_code(code) is True
        s.finalize_setup()
        assert s.verify_setup_code(code) is False
```

In `web_interface/routes.py`:

1. The access gate already only redirects when `access_store.setup_mode`; a pending transfer does not set setup mode, so nothing else needs changing there. `GET /setup` (Task 14) already returns the page when `access_store.pending_transfer is not None`.
2. `setup_submit` redirects to `/setup/review` when `transferring` and `/setup/codes` otherwise.
3. Add the review routes to the public router:

```python
    def _next_review_user(owner_id: str):
        return next(
            (
                u
                for u in sorted(access_store.users.values(), key=lambda u: u.name)
                if u.id != owner_id
            ),
            None,
        )

    @public.get(
        "/setup/review",
        response_class=HTMLResponse,
        dependencies=[Depends(web_auth.require(Permission.manage_ownership))],
    )
    async def setup_review(request: Request):
        principal = web_auth.current_principal(request)
        user = _next_review_user(principal.user.id)
        if user is None:
            return RedirectResponse("/setup/codes", status_code=303)
        return _review_page(request, user)

    def _review_page(request: Request, user):
        return templates.TemplateResponse(
            "setup_review_user.html",
            web_auth.template_context(
                request,
                user=user,
                device_count=sum(
                    1
                    for d in access_store.devices.values()
                    if user.id in d.trusted_user_ids
                ),
            ),
        )

    @public.post(
        "/setup/review/{user_id}/keep",
        response_class=HTMLResponse,
        dependencies=[
            Depends(require_htmx),
            Depends(web_auth.require(Permission.manage_ownership)),
        ],
    )
    async def review_keep(request: Request, user_id: str):
        return await _advance_review(request, user_id)

    @public.post(
        "/setup/review/{user_id}/remove",
        response_class=HTMLResponse,
        dependencies=[
            Depends(require_htmx),
            Depends(web_auth.require(Permission.manage_ownership)),
        ],
    )
    async def review_remove(request: Request, user_id: str):
        access_store.end_sessions_for_user(user_id)
        access_store.delete_user(user_id)
        return await _advance_review(request, user_id)

    async def _advance_review(request: Request, decided_user_id: str):
        """Each decision applies immediately; leaving mid-review keeps the rest."""
        principal = web_auth.current_principal(request)
        remaining = [
            u
            for u in sorted(access_store.users.values(), key=lambda u: u.name)
            if u.id not in (principal.user.id, decided_user_id)
        ]
        if not remaining:
            return HTMLResponse("", headers={"HX-Redirect": "/setup/codes"})
        return _review_page(request, remaining[0])
```

A "keep" decision must still advance past the user just reviewed, hence `decided_user_id` is excluded either way; on a reload `GET /setup/review` starts from the first remaining user, which is acceptable because keeping is a no-op.

Create `web_interface/templates/setup_review_user.html`: page chrome as in `setup.html`; heading "Review users"; the user's name, role, email, last login and `device_count`; a **Keep** button (`hx-post="/setup/review/{{ user.id }}/keep"`) and a **Remove** button (`hx-post="/setup/review/{{ user.id }}/remove"`), both `hx-target="body" hx-swap="outerHTML"`; and a "Skip the rest" link to `/setup/codes`.

In `setup.html`, when `transfer` is true, change the heading to "Take ownership" and the code field's label to "Transfer code".

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest`
Expected: PASS across the suite.

- [ ] **Step 5: Lint and commit**

```bash
ruff check --fix .
ruff format .
git add services/access.py web_interface tests
git commit -m "feat(access): complete an ownership transfer and review retained users"
```

---

### Task 20: Drop the admin credential and wire the store into `main.py`

**Spec:** §1 (`WebConfig` loses `admin_username` and `admin_password`; the startup password warning becomes a setup-mode warning), §5 (files table).

**Files:**
- Modify: `config/config_model.py`, `services/auth_policy.py`, `main.py`, `config.example.json`
- Modify: `tests/test_config_model.py`, `tests/test_first_run.py`, `tests/test_startup_policy.py`, `tests/test_auth_policy.py`
- Delete: `tests/test_login_limiter.py`

**Interfaces:**
- Consumes: `AccessStore` (Task 4), `routes.set_access_store` / `set_display_controller` / `ensure_setup_mode` (Tasks 9, 14), `web_auth.backoff` (Task 8).
- Produces:
  - `WebConfig` has `host`, `port`, `trusted_proxies` only
  - `services/auth_policy.py` exports `pin_problem`, `MIN_PIN_LENGTH`, `MAX_PIN_LENGTH`, `is_loopback`, `LOOPBACK_HOSTS` — `password_problem`, `generate_admin_password`, `WEAK_PASSWORDS`, `MIN_PASSWORD_LENGTH` are gone
  - `main.enforce_password_policy` is replaced by `main.warn_if_setup_mode(store) -> None`
  - `main()` constructs `AccessStore()` right after `load_config()` and calls `routes.set_access_store(store)`, `routes.set_display_controller(display)` (after the display controller is built) and `routes.ensure_setup_mode()`
  - `ICE_COLDER_ALLOW_WEAK_PASSWORD` no longer exists anywhere

- [ ] **Step 1: Update the tests first**

- `tests/test_config_model.py`: delete the two `admin_username` / `admin_password` assertions (lines 103–104) and add:

```python
def test_web_config_has_no_admin_credentials():
    cfg = ConfigModel()
    assert not hasattr(cfg.web, "admin_username")
    assert not hasattr(cfg.web, "admin_password")
```

- `tests/test_first_run.py`: delete `test_first_run_generates_strong_password_and_logs_it_once` and add:

```python
def test_first_run_config_has_no_admin_password(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = main.load_config()
    saved = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    assert "admin_password" not in saved["web"]
    assert "admin_username" not in saved["web"]
```

- `tests/test_startup_policy.py`: delete the five password-policy tests and the `_web` helper's `admin_password` argument; keep the env-override tests. Add:

```python
def test_setup_mode_is_warned_about(tmp_path, caplog):
    from services.access import AccessStore

    store = AccessStore(path=tmp_path / "access.json")
    main.warn_if_setup_mode(store)
    assert "setup mode" in caplog.text.lower()


def test_no_warning_once_an_owner_exists(tmp_path, caplog):
    from services.access import AccessStore, Role

    store = AccessStore(path=tmp_path / "access.json")
    store.create_user("Ada", None, Role.owner, "1379")
    main.warn_if_setup_mode(store)
    assert "setup mode" not in caplog.text.lower()
```

- `tests/test_auth_policy.py`: delete every `password_problem` / `generate_admin_password` test and their imports; keep `is_loopback` and the Task 1 PIN tests.
- Delete `tests/test_login_limiter.py` — `LoginLimiter` no longer exists.

Run: `uv run pytest tests/test_config_model.py tests/test_first_run.py tests/test_startup_policy.py -v`
Expected: FAIL — `AttributeError: module 'main' has no attribute 'warn_if_setup_mode'` and the `hasattr` assertions failing.

- [ ] **Step 2: Implement**

`config/config_model.py` — `WebConfig` becomes:

```python
class WebConfig(BaseModel):
    """Web dashboard binding. Authentication lives in data/access.json, not here."""

    host: str = Field("0.0.0.0", description="Interface to bind the dashboard to")
    port: int = Field(26123, description="Dashboard port")
    trusted_proxies: List[str] = Field(
        default_factory=list,
        description=(
            "CIDRs of reverse proxies whose X-Forwarded-For is trusted for "
            "login back-off keying (e.g. the Docker network Traefik reaches "
            "the VMC from)"
        ),
    )
```

`services/auth_policy.py` — delete `WEAK_PASSWORDS`, `MIN_PASSWORD_LENGTH`, `password_problem`, `generate_admin_password` and the now-unused `import secrets`.

`config.example.json` — remove the `admin_username` and `admin_password` lines from the `web` object.

`main.py`:

- imports: drop `generate_admin_password`, `password_problem`; keep `is_loopback` only if still used (it is not — drop it too and remove the import line entirely if nothing remains). Add `from services.access import AccessStore` and `from web_interface import auth as web_auth`. Drop `SecretStr` if it becomes unused.
- `_create_default_config` loses the password lines:

```python
def _create_default_config(path: str) -> ConfigModel:
    """First run: blank defaults, persisted, then continue.

    There is no admin credential any more: the dashboard boots into setup
    mode and the setup code is logged by routes.ensure_setup_mode().
    """
    defaults = ConfigModel()
    save_config(defaults, Path(path))
    logger.info(f"First run: created '{path}' with blank defaults")
    return defaults
```

- replace `enforce_password_policy` with:

```python
def warn_if_setup_mode(store: AccessStore) -> None:
    """Say loudly that nobody owns this machine yet.

    Replaces the old weak-admin-password check: there is no default
    credential to be weak. The setup code itself is logged by
    routes.ensure_setup_mode().
    """
    if store.corrupt:
        logger.error(
            "data/access.json is corrupt: the dashboard will serve an error "
            "page. The VMC and MQTT client keep running."
        )
        return
    if store.setup_mode:
        logger.warning(
            "Dashboard is in setup mode: no owner account exists yet. "
            "Visit /setup at the machine to create one."
        )
```

- in `main()`, after `routes.set_config_object(live_config)`:

```python
    access_store = AccessStore()
    routes.set_access_store(access_store)
    web_auth.backoff.set_trusted_proxies(overrides.trusted_proxies)
    warn_if_setup_mode(access_store)
```

(replacing the `routes.login_limiter.set_trusted_proxies(...)` line Task 11 already edited), and after the display controller is created:

```python
    routes.set_display_controller(display)
    routes.ensure_setup_mode()
```

- delete the `enforce_password_policy(web_cfg)` call before the uvicorn config.

- [ ] **Step 3: Run the full suite**

Run: `uv run pytest`
Expected: PASS. Grep to confirm nothing is left behind:

```bash
grep -rn "admin_password\|admin_username\|LoginLimiter\|ALLOW_WEAK_PASSWORD" --include="*.py" --include="*.json" --include="*.yml" .
```

Only `docs/superpowers/plans/2026-09-*` (historical plans) may match.

- [ ] **Step 4: Lint and commit**

```bash
ruff check --fix .
ruff format .
git add config main.py services/auth_policy.py config.example.json tests
git rm tests/test_login_limiter.py
git commit -m "feat(access): remove the admin credential and wire AccessStore into startup"
```

---

### Task 21: Documentation, compose and environment

**Spec:** §5 (files table: `.env.example`, `docker-compose*.yml`, `README.md`, `CLAUDE.md`), program plan §5 (backup guidance in `README.md`).

**Files:**
- Modify: `README.md`, `CLAUDE.md`, `.env.example`, `docker-compose.yml`, `docker/docker-compose.prod.yml`, `docker/docker-compose.yml`
- Test: none (documentation task). Verify by grep and by `uv run pytest`.

**Interfaces:**
- Consumes: everything above.
- Produces: no code.

- [ ] **Step 1: Find every reference**

```bash
grep -rn "admin password\|admin_password\|ALLOW_WEAK_PASSWORD\|Basic auth\|HTTP Basic" README.md CLAUDE.md .env.example docker-compose.yml docker/
```

- [ ] **Step 2: Rewrite `README.md`**

Replace the paragraph at `README.md:72–79` (the weak-admin-password refusal and the first-run generated password) with:

```markdown
The dashboard has no default credential. On first boot it enters **setup
mode**: the VMC generates an 8-digit setup code, prints it in the startup log
at warning level (`docker compose logs vmc`) and shows it on the customer
display. Visit the dashboard, enter that code, and create the owner account —
a name and a 4–8 digit PIN. The wizard then shows 20 emergency codes once;
save them somewhere that is not the machine.

Signing in from a browser the machine has not seen before needs a second
factor once: a 6-digit code emailed to the user (when
`communication.email_gateway` is configured) or one of the 20 emergency
codes, which works with no network at all.

**Back up `data/access.json`.** It holds every user, PIN hash, trusted device
and emergency-code hash, and it is the single point of lockout. It is inside
the bind-mounted `./data` directory, so a copy of that directory is enough.
The Users screen shows how many emergency codes remain unused — regenerate
the pool before it runs dry. An owner who loses both their PIN and every
emergency code has **no software recovery**: the controller must be
factory-restored, which means deleting `data/access.json` and running the
wizard again.
```

Keep the surrounding Traefik and `ICE_COLDER_TRUSTED_PROXIES` paragraphs; only the trusted-proxy sentence changes from "the dashboard's login limiter" to "the dashboard's login back-off".

- [ ] **Step 3: Rewrite the `CLAUDE.md` sections**

- Entry point paragraph: replace "HTTP Basic auth from `config.web.admin_username`/`admin_password`" with "sessions from `data/access.json`; no credential lives in `config.json`".
- Configuration paragraph: note that `web` now holds host, port and trusted proxies only.
- Web Dashboard section: replace the HTTP Basic paragraph with:

```markdown
The dashboard is session auth backed by `services/access.py`'s `AccessStore`
(`data/access.json`, mode 0600): named users with one of four roles
(`owner`, `secretary`, `tech`, `loader`), a 4–8 digit PIN, and per-device
trust proved once by an emailed 6-digit OTP or an offline 8-digit emergency
code. Every route declares a `Permission` through
`web_interface/auth.py`'s `require(...)`; templates receive `perms` and
`current_user` via `template_context` so they never render a control the
server would refuse. `Backoff` (also in `services/access.py`) replaces the
old `LoginLimiter`: exponential per `(kind, subject, client)` with a
per-user budget for untrusted clients, no lockouts. While no owner exists
the app is in setup mode — every route redirects to `/setup`, which is
gated by a code printed in the startup log and shown on the customer
display. POST routes still require the `HX-Request` header as a CSRF guard,
which cookie auth makes load-bearing.
```

- Services section: add bullets for `access.py` and `mailer.py`, and correct the `auth_policy.py` bullet to "PIN policy (`pin_problem`) shared by the setup wizard and user management; `is_loopback` kept".

- [ ] **Step 4: Compose and `.env.example`**

Remove any admin-credential or `ICE_COLDER_ALLOW_WEAK_PASSWORD` entries from `.env.example` and all three compose files. Confirm `./data` is bind-mounted read-write for the `vmc` service (it already is) so `data/access.json` persists, and add a comment there naming the file. **Do not run `docker compose`** — CI's `compose-config` job validates it.

- [ ] **Step 5: Verify and commit**

```bash
uv run pytest
ruff check .
git add README.md CLAUDE.md .env.example docker-compose.yml docker
git commit -m "docs: setup mode, roles and access file replace the admin password"
```

---

## Definition of Done

The part is complete when all of the following hold:

1. `uv run pytest` is green and `ruff check .` is clean.
2. `grep -rn "admin_password\|admin_username\|LoginLimiter\|ALLOW_WEAK_PASSWORD" --include="*.py" --include="*.json" --include="*.yml" .` matches nothing outside `docs/superpowers/plans/2026-09-12-*` and `2026-09-22-*`.
3. No new entry in `pyproject.toml`'s dependency list.
4. `services/mqtt_messages.py`'s only change is the optional `DisplayCommand.message` field (Task 13), and the owner approved it.
5. Every deviation and assumption recorded in the tasks above is listed in the pull-request description, alongside the part-1 acceptance results from program plan §4.

## Spec coverage check

| Spec section | Task(s) |
|---|---|
| §1 data model, invariants, 0600 file, clocks | 4, 5, 6 |
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

