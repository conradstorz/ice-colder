# Remote Exposure Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Nobody outside the LAN can publish MQTT commands, guess the dashboard login, or replay a logged-in browser's credentials against the control endpoints.

**Architecture:** Broker authentication comes from a gitignored `.env` through a one-shot `mosquitto-init` container; VMC and simulators read the same env vars. The dashboard keeps HTTP Basic auth and gains three guards: a startup password policy (`services/auth_policy.py`), an `HX-Request` requirement on every POST route, and a per-IP failed-login limiter (`web_interface/auth.py`) that trusts `X-Forwarded-For` only from configured proxy CIDRs.

**Tech Stack:** Python 3.12, FastAPI, pydantic v2, aiomqtt, docker compose, mosquitto 2, pytest (asyncio_mode=auto), uv, ruff.

**Spec:** `docs/superpowers/specs/2026-09-22-remote-exposure-hardening-design.md`

## Global Constraints

- Run every command with `uv run ...`; never `pip`, never bare `python`. Do not chain shell commands with `&&`; one command per tool call.
- Lint before each commit: `uv run ruff check --fix .` then `uv run ruff format .`. If ruff reformats `tests/test_contract_schemas.py` or `tests/test_simulator_ice_maker.py` (pre-existing drift), discard those with `git checkout -- <file>`.
- Branch: `feat/remote-exposure-hardening` (checked out, PR #13 draft). Commit after every task with the trailer `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`.
- Full suite must stay green: `uv run pytest -q` (683 passed, 11 skipped at start).
- Exact values: `WEAK_PASSWORDS = {"changeme", "admin", "password", "ice-colder"}`, `MIN_PASSWORD_LENGTH = 12`, generated password `secrets.token_urlsafe(15)` (20 chars). Loopback hosts: `127.0.0.1`, `localhost`, `::1`. Bypass env var `ICE_COLDER_ALLOW_WEAK_PASSWORD` equal to `"1"`. Limiter: `max_failures=10`, `window_seconds=900.0`, `lockout_seconds=900.0`; lockout answers 429 with `Retry-After`. CSRF header: `HX-Request: true`, failure 403 `"HTMX request required"`. Env vars: `MQTT_USERNAME`, `MQTT_PASSWORD`, `MQTT_BIND_ADDR` (default `0.0.0.0`), `ICE_COLDER_TRUSTED_PROXIES` (comma-separated CIDRs).
- Never log or print a password except the single first-run WARNING line.
- Neither compose file sets `ICE_COLDER_ALLOW_WEAK_PASSWORD`.

---

### Task 1: `services/auth_policy.py`

**Files:**
- Create: `services/auth_policy.py`
- Create: `tests/test_auth_policy.py`

**Interfaces:**
- Produces: `WEAK_PASSWORDS`, `MIN_PASSWORD_LENGTH`, `LOOPBACK_HOSTS`, `password_problem(password: str) -> str | None`, `generate_admin_password() -> str`, `is_loopback(host: str) -> bool`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_auth_policy.py
import re

import pytest

from services.auth_policy import (
    MIN_PASSWORD_LENGTH,
    generate_admin_password,
    is_loopback,
    password_problem,
)


@pytest.mark.parametrize("weak", ["changeme", "admin", "password", "ice-colder", "CHANGEME"])
def test_known_weak_passwords_rejected(weak):
    assert password_problem(weak) is not None


def test_short_password_rejected():
    assert password_problem("abcdefghijk") is not None  # 11 chars


def test_strong_password_accepted():
    assert password_problem("correct-horse-battery") is None


def test_empty_password_rejected():
    assert "empty" in password_problem("")


def test_generated_password_is_long_and_urlsafe():
    pw = generate_admin_password()
    assert len(pw) >= MIN_PASSWORD_LENGTH
    assert re.fullmatch(r"[A-Za-z0-9_-]+", pw)
    assert password_problem(pw) is None
    assert generate_admin_password() != pw


@pytest.mark.parametrize("host,expected", [("127.0.0.1", True), ("localhost", True), ("::1", True), ("0.0.0.0", False), ("192.168.1.5", False)])
def test_is_loopback(host, expected):
    assert is_loopback(host) is expected
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_auth_policy.py -q`
Expected: `ModuleNotFoundError: services.auth_policy`.

- [ ] **Step 3: Implement**

```python
# services/auth_policy.py
"""Dashboard admin-password policy shared by first-run setup and startup checks."""

import secrets

WEAK_PASSWORDS = {"changeme", "admin", "password", "ice-colder"}
MIN_PASSWORD_LENGTH = 12
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


def password_problem(password: str) -> str | None:
    """Why this admin password must not face a network, or None if acceptable."""
    if not password:
        return "admin password is empty"
    if password.lower() in WEAK_PASSWORDS:
        return f"admin password {password!r} is a well-known default"
    if len(password) < MIN_PASSWORD_LENGTH:
        return f"admin password is shorter than {MIN_PASSWORD_LENGTH} characters"
    return None


def generate_admin_password() -> str:
    """A random URL-safe password for first-run configs (20 characters)."""
    return secrets.token_urlsafe(15)


def is_loopback(host: str) -> bool:
    return host in LOOPBACK_HOSTS
```

- [ ] **Step 4: Run tests** — `uv run pytest tests/test_auth_policy.py -q` → all pass.

- [ ] **Step 5: Commit**

```bash
git add services/auth_policy.py tests/test_auth_policy.py
git commit -m "feat(auth): admin password policy helpers"
```

---

### Task 2: Startup enforcement, first-run password, env overrides, `trusted_proxies`

**Files:**
- Modify: `main.py` (`_create_default_config`, new `apply_env_overrides`, new `enforce_password_policy`, `main()` around the `MQTT_BROKER_HOST` override and the `changeme` warning), `config/config_model.py::WebConfig`, `config.example.json` (web section)
- Test: `tests/test_first_run.py`, new `tests/test_startup_policy.py`

**Interfaces:**
- Consumes: Task 1.
- Produces: `WebConfig.trusted_proxies: list[str]`; `main.apply_env_overrides(config: ConfigModel) -> None`; `main.enforce_password_policy(web: WebConfig) -> None` (raises `SystemExit(1)`).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_first_run.py`:

```python
def test_first_run_generates_strong_password_and_logs_it_once(tmp_path, monkeypatch, caplog):
    from services.auth_policy import password_problem

    monkeypatch.chdir(tmp_path)
    caplog.set_level("WARNING")
    cfg = main_mod.load_config()
    pw = cfg.web.admin_password.get_secret_value()
    assert password_problem(pw) is None
    saved = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    assert saved["web"]["admin_password"] == pw
    lines = [r.message for r in caplog.records if "First run: dashboard login" in r.message]
    assert len(lines) == 1 and pw in lines[0]
```

`caplog` does not see loguru by default; if the repo has no loguru→caplog bridge, add this fixture to `tests/conftest.py`:

```python
import logging

from loguru import logger as _loguru


@pytest.fixture
def caplog(caplog):
    handler_id = _loguru.add(
        lambda msg: logging.getLogger("loguru").handle(
            logging.LogRecord("loguru", msg.record["level"].no, "", 0, msg.record["message"], None, None)
        ),
        level="DEBUG",
    )
    yield caplog
    _loguru.remove(handler_id)
```

New `tests/test_startup_policy.py`:

```python
import pytest
from pydantic import SecretStr

import main as main_mod
from config.config_model import ConfigModel, WebConfig


def _web(host="0.0.0.0", password="changeme"):
    return WebConfig(host=host, admin_password=SecretStr(password))


def test_weak_password_on_public_host_exits(monkeypatch):
    monkeypatch.delenv("ICE_COLDER_ALLOW_WEAK_PASSWORD", raising=False)
    with pytest.raises(SystemExit) as e:
        main_mod.enforce_password_policy(_web())
    assert e.value.code == 1


def test_short_password_on_public_host_exits(monkeypatch):
    monkeypatch.delenv("ICE_COLDER_ALLOW_WEAK_PASSWORD", raising=False)
    with pytest.raises(SystemExit):
        main_mod.enforce_password_policy(_web(password="short-pw"))


def test_weak_password_on_loopback_is_allowed(monkeypatch):
    monkeypatch.delenv("ICE_COLDER_ALLOW_WEAK_PASSWORD", raising=False)
    main_mod.enforce_password_policy(_web(host="127.0.0.1"))


def test_bypass_flag_downgrades_to_warning(monkeypatch):
    monkeypatch.setenv("ICE_COLDER_ALLOW_WEAK_PASSWORD", "1")
    main_mod.enforce_password_policy(_web())


def test_strong_password_passes(monkeypatch):
    monkeypatch.delenv("ICE_COLDER_ALLOW_WEAK_PASSWORD", raising=False)
    main_mod.enforce_password_policy(_web(password="correct-horse-battery"))


def test_env_overrides_mqtt_credentials_and_trusted_proxies(monkeypatch):
    monkeypatch.setenv("MQTT_BROKER_HOST", "mosquitto")
    monkeypatch.setenv("MQTT_USERNAME", "vmc")
    monkeypatch.setenv("MQTT_PASSWORD", "s3cret-value")
    monkeypatch.setenv("ICE_COLDER_TRUSTED_PROXIES", "172.25.0.0/16, 10.0.0.0/8")
    cfg = ConfigModel()
    main_mod.apply_env_overrides(cfg)
    assert cfg.mqtt.broker_host == "mosquitto"
    assert cfg.mqtt.username == "vmc"
    assert cfg.mqtt.password.get_secret_value() == "s3cret-value"
    assert cfg.web.trusted_proxies == ["172.25.0.0/16", "10.0.0.0/8"]


def test_env_overrides_absent_leave_config_alone(monkeypatch):
    for k in ("MQTT_BROKER_HOST", "MQTT_USERNAME", "MQTT_PASSWORD", "ICE_COLDER_TRUSTED_PROXIES"):
        monkeypatch.delenv(k, raising=False)
    cfg = ConfigModel()
    main_mod.apply_env_overrides(cfg)
    assert cfg.mqtt.username is None and cfg.mqtt.password is None
    assert cfg.web.trusted_proxies == []


def test_web_config_trusted_proxies_default_empty():
    assert WebConfig().trusted_proxies == []
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_startup_policy.py tests/test_first_run.py -q`
Expected: `AttributeError: enforce_password_policy` and the first-run test fails on `password_problem`.

- [ ] **Step 3: Implement**

`config/config_model.py::WebConfig` add after `admin_password`:

```python
    trusted_proxies: List[str] = Field(
        default_factory=list,
        description=(
            "CIDRs of reverse proxies whose X-Forwarded-For is trusted for the "
            "login limiter (e.g. the Docker network Traefik reaches the VMC from)"
        ),
    )
```

`config.example.json`: add `"trusted_proxies": []` to the `web` object (create the section if the example lacks one, mirroring the model defaults).

`main.py`:

```python
from pydantic import SecretStr, ValidationError
from services.auth_policy import generate_admin_password, is_loopback, password_problem
```

```python
def _create_default_config(path: str) -> ConfigModel:
    """First run: blank defaults plus a random admin password, persisted, then continue."""
    defaults = ConfigModel()
    password = generate_admin_password()
    defaults.web.admin_password = SecretStr(password)
    save_config(defaults, Path(path))
    logger.info(f"First run: created '{path}' with blank defaults")
    logger.warning(
        f"First run: dashboard login is {defaults.web.admin_username} / {password} "
        "— change it in config.json"
    )
    return defaults


def apply_env_overrides(config: ConfigModel) -> None:
    """Docker-friendly overrides: broker host/credentials and trusted proxies.

    Read at call time so tests can monkeypatch the environment.
    """
    host = os.environ.get("MQTT_BROKER_HOST")
    if host:
        config.mqtt.broker_host = host
        logger.info(f"MQTT broker host overridden by env: {host}")
    username = os.environ.get("MQTT_USERNAME")
    if username:
        config.mqtt.username = username
        logger.info(f"MQTT username overridden by env: {username}")
    password = os.environ.get("MQTT_PASSWORD")
    if password:
        config.mqtt.password = SecretStr(password)
    proxies = os.environ.get("ICE_COLDER_TRUSTED_PROXIES")
    if proxies:
        config.web.trusted_proxies = [p.strip() for p in proxies.split(",") if p.strip()]
        logger.info(f"Trusted proxies overridden by env: {config.web.trusted_proxies}")


def enforce_password_policy(web) -> None:
    """Refuse to serve a weak admin password on a non-loopback interface.

    ICE_COLDER_ALLOW_WEAK_PASSWORD=1 downgrades the refusal to a warning; it is
    for a local shell or an uncommitted compose override, never the committed
    stack.
    """
    problem = password_problem(web.admin_password.get_secret_value())
    if problem is None:
        return
    if is_loopback(web.host):
        logger.warning(f"Dashboard on loopback with a weak password ({problem})")
        return
    if os.environ.get("ICE_COLDER_ALLOW_WEAK_PASSWORD") == "1":
        logger.warning(
            f"ICE_COLDER_ALLOW_WEAK_PASSWORD=1: serving on {web.host} although {problem}"
        )
        return
    logger.error(
        f"Refusing to serve the dashboard on {web.host}: {problem}. "
        "Set web.admin_password in config.json to at least 12 characters, "
        "or bind web.host to 127.0.0.1, or set ICE_COLDER_ALLOW_WEAK_PASSWORD=1 "
        "for a private test host."
    )
    sys.exit(1)
```

In `main()`: replace the `MQTT_BROKER_HOST` block with `apply_env_overrides(live_config)` placed right after `load_config()` (before the VMC is created, so `routes` and the MQTT client see the overrides), and replace the `changeme` warning block with `enforce_password_policy(live_config.web)`.

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_startup_policy.py tests/test_first_run.py tests/test_main_supervise.py tests/test_web_routes.py -q`
Expected: all pass. The web-routes fixture uses `ConfigModel()` (password `changeme`) but never calls `enforce_password_policy`, so it is unaffected.

- [ ] **Step 5: Commit**

```bash
git add main.py config/config_model.py config.example.json tests/
git commit -m "feat(startup): generated first-run password, fail-closed password policy, MQTT credential and trusted-proxy env overrides"
```

---

### Task 3: `web_interface/auth.py` login limiter

**Files:**
- Create: `web_interface/auth.py`
- Create: `tests/test_login_limiter.py`

**Interfaces:**
- Produces: `LoginLimiter(max_failures=10, window_seconds=900.0, lockout_seconds=900.0, clock=time.monotonic, trusted_proxies: list[str] | None = None)` with `client_ip(request) -> str`, `check(ip) -> float | None`, `record_failure(ip)`, `record_success(ip)`, `set_trusted_proxies(cidrs: list[str])`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_login_limiter.py
from types import SimpleNamespace

from web_interface.auth import LoginLimiter


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def _req(peer, xff=None):
    headers = {"x-forwarded-for": xff} if xff else {}
    return SimpleNamespace(client=SimpleNamespace(host=peer), headers=headers)


def test_locks_after_max_failures_and_expires():
    clock = Clock()
    lim = LoginLimiter(max_failures=3, window_seconds=100, lockout_seconds=50, clock=clock)
    for _ in range(2):
        lim.record_failure("1.2.3.4")
    assert lim.check("1.2.3.4") is None
    lim.record_failure("1.2.3.4")
    remaining = lim.check("1.2.3.4")
    assert remaining is not None and 0 < remaining <= 50
    clock.now += 51
    assert lim.check("1.2.3.4") is None


def test_failures_outside_window_do_not_count():
    clock = Clock()
    lim = LoginLimiter(max_failures=3, window_seconds=100, lockout_seconds=50, clock=clock)
    lim.record_failure("a")
    lim.record_failure("a")
    clock.now += 101
    lim.record_failure("a")
    assert lim.check("a") is None


def test_success_clears_failures():
    lim = LoginLimiter(max_failures=2)
    lim.record_failure("a")
    lim.record_success("a")
    lim.record_failure("a")
    assert lim.check("a") is None


def test_ips_are_independent():
    lim = LoginLimiter(max_failures=1)
    lim.record_failure("a")
    assert lim.check("a") is not None
    assert lim.check("b") is None


def test_client_ip_ignores_forwarded_header_from_untrusted_peer():
    lim = LoginLimiter()
    assert lim.client_ip(_req("203.0.113.9", xff="10.0.0.1")) == "203.0.113.9"


def test_client_ip_uses_rightmost_forwarded_hop_from_trusted_proxy():
    lim = LoginLimiter(trusted_proxies=["172.25.0.0/16"])
    assert lim.client_ip(_req("172.25.0.7", xff="10.0.0.1, 198.51.100.4")) == "198.51.100.4"


def test_client_ip_trusted_proxy_without_header_falls_back_to_peer():
    lim = LoginLimiter(trusted_proxies=["172.25.0.0/16"])
    assert lim.client_ip(_req("172.25.0.7")) == "172.25.0.7"


def test_invalid_cidr_is_ignored_not_fatal():
    lim = LoginLimiter(trusted_proxies=["not-a-cidr", "172.25.0.0/16"])
    assert lim.client_ip(_req("172.25.0.7", xff="198.51.100.4")) == "198.51.100.4"


def test_pruning_bounds_memory():
    clock = Clock()
    lim = LoginLimiter(max_failures=5, window_seconds=10, clock=clock)
    for i in range(50):
        lim.record_failure(f"ip{i}")
    clock.now += 11
    lim.check("ip0")
    assert len(lim._failures) == 0
```

- [ ] **Step 2: Run to verify failure** — `uv run pytest tests/test_login_limiter.py -q` → `ModuleNotFoundError`.

- [ ] **Step 3: Implement**

```python
# web_interface/auth.py
"""Failed-login limiter for the dashboard's HTTP Basic auth.

Per client IP, a sliding window of failed attempts; too many inside the
window locks that IP out for a while. State is in-process memory: a restart
clears it, which is fine for a single machine.
"""

from __future__ import annotations

import ipaddress
import time
from collections import deque
from typing import Callable, Optional

from loguru import logger


class LoginLimiter:
    def __init__(
        self,
        max_failures: int = 10,
        window_seconds: float = 900.0,
        lockout_seconds: float = 900.0,
        clock: Callable[[], float] = time.monotonic,
        trusted_proxies: Optional[list[str]] = None,
    ):
        self._max = max_failures
        self._window = window_seconds
        self._lockout = lockout_seconds
        self._clock = clock
        self._failures: dict[str, deque[float]] = {}
        self._locked_until: dict[str, float] = {}
        self._networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
        self.set_trusted_proxies(trusted_proxies or [])

    # --- configuration ---

    def set_trusted_proxies(self, cidrs: list[str]) -> None:
        self._networks = []
        for cidr in cidrs:
            try:
                self._networks.append(ipaddress.ip_network(cidr.strip(), strict=False))
            except ValueError:
                logger.warning(f"LoginLimiter: ignoring invalid trusted proxy CIDR {cidr!r}")

    def _is_trusted_proxy(self, peer: str) -> bool:
        try:
            addr = ipaddress.ip_address(peer)
        except ValueError:
            return False
        return any(addr in net for net in self._networks)

    def client_ip(self, request) -> str:
        """The address to rate-limit on.

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

    # --- accounting ---

    def _prune(self, now: float) -> None:
        cutoff = now - self._window
        for ip in list(self._failures):
            dq = self._failures[ip]
            while dq and dq[0] < cutoff:
                dq.popleft()
            if not dq:
                del self._failures[ip]
        for ip in list(self._locked_until):
            if self._locked_until[ip] <= now:
                del self._locked_until[ip]

    def check(self, ip: str) -> float | None:
        """Seconds remaining in a lockout for ip, or None if allowed."""
        now = self._clock()
        self._prune(now)
        until = self._locked_until.get(ip)
        if until is None:
            return None
        return until - now

    def record_failure(self, ip: str) -> None:
        now = self._clock()
        self._prune(now)
        dq = self._failures.setdefault(ip, deque())
        dq.append(now)
        if len(dq) >= self._max:
            self._locked_until[ip] = now + self._lockout
            del self._failures[ip]
            logger.warning(
                f"Dashboard: {ip} locked out for {self._lockout:.0f}s after "
                f"{self._max} failed logins"
            )

    def record_success(self, ip: str) -> None:
        self._failures.pop(ip, None)
```

- [ ] **Step 4: Run tests** — `uv run pytest tests/test_login_limiter.py -q` → all pass.

- [ ] **Step 5: Commit**

```bash
git add web_interface/auth.py tests/test_login_limiter.py
git commit -m "feat(web): per-IP failed-login limiter with trusted-proxy forwarding"
```

---

### Task 4: Wire the limiter and the HTMX guard into the routes

**Files:**
- Modify: `web_interface/routes.py` (`require_auth`, new `require_htmx`, every `@router.post`, `set_config_object`)
- Test: `tests/test_web_routes.py`

**Interfaces:**
- Consumes: `LoginLimiter` (Task 3), `WebConfig.trusted_proxies` (Task 2).
- Produces: `routes.login_limiter: LoginLimiter`, `routes.require_htmx`.

- [ ] **Step 1: Write the failing tests**

In `tests/test_web_routes.py` `client` fixture, add `c.headers["HX-Request"] = "true"` right after `c.auth = ...`. Then append:

```python
class TestCsrfGuard:
    @pytest.mark.parametrize(
        "path",
        ["/inventory/add", "/faults/PAY-104/clear", "/action/reset", "/inventory/update/X", "/inventory/delete/X"],
    )
    def test_post_without_htmx_header_is_forbidden(self, client, path):
        resp = client.post(path, headers={"HX-Request": ""}, data={"sku": "X", "name": "n", "price": "1", "slot": "0", "kind": "other"})
        assert resp.status_code == 403
        assert "HTMX" in resp.text

    def test_get_routes_do_not_need_header(self, client):
        resp = client.get("/status", headers={"HX-Request": ""})
        assert resp.status_code == 200


class TestLoginLimiter:
    def test_lockout_after_ten_failures(self, client):
        from web_interface import routes as r

        r.login_limiter._failures.clear()
        r.login_limiter._locked_until.clear()
        for _ in range(10):
            assert client.get("/status", auth=("admin", "wrong")).status_code == 401
        resp = client.get("/status", auth=("admin", "wrong"))
        assert resp.status_code == 429
        assert "Retry-After" in resp.headers
        # even the right password is refused while locked
        assert client.get("/status").status_code == 429
        r.login_limiter._locked_until.clear()

    def test_success_resets_counter(self, client):
        from web_interface import routes as r

        r.login_limiter._failures.clear()
        r.login_limiter._locked_until.clear()
        for _ in range(9):
            client.get("/status", auth=("admin", "wrong"))
        assert client.get("/status").status_code == 200
        for _ in range(9):
            client.get("/status", auth=("admin", "wrong"))
        assert client.get("/status").status_code == 200

    def test_trusted_proxies_applied_from_config(self, tmp_path):
        from config.config_model import ConfigModel
        from web_interface import routes as r

        cfg = ConfigModel()
        cfg.web.trusted_proxies = ["172.25.0.0/16"]
        r.set_config_object(cfg)
        assert r.login_limiter._networks and str(r.login_limiter._networks[0]) == "172.25.0.0/16"
        r.set_config_object(ConfigModel())
        assert r.login_limiter._networks == []
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_web_routes.py -k "Csrf or LoginLimiter" -q`
Expected: 403 tests fail (routes return 200/404/422), limiter tests fail (never 429).

- [ ] **Step 3: Implement**

In `web_interface/routes.py`:

```python
from web_interface.auth import LoginLimiter

login_limiter = LoginLimiter()


def set_config_object(cfg: ConfigModel):
    global config
    config = cfg
    login_limiter.set_trusted_proxies(list(cfg.web.trusted_proxies))
```

Replace `require_auth`:

```python
def require_auth(request: Request, credentials: HTTPBasicCredentials = Depends(_basic_auth)):
    """HTTP Basic auth for every dashboard route, checked against config.web,
    with a per-IP failed-login lockout."""
    if config is None:
        raise HTTPException(status_code=503, detail="Configuration not loaded")
    ip = login_limiter.client_ip(request)
    remaining = login_limiter.check(ip)
    if remaining is not None:
        raise HTTPException(
            status_code=429,
            detail="Too many failed logins; try again later",
            headers={"Retry-After": str(int(remaining) + 1)},
        )
    user_ok = _secrets.compare_digest(credentials.username, config.web.admin_username)
    pass_ok = _secrets.compare_digest(
        credentials.password, config.web.admin_password.get_secret_value()
    )
    if not (user_ok and pass_ok):
        login_limiter.record_failure(ip)
        raise HTTPException(
            status_code=401,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Basic"},
        )
    login_limiter.record_success(ip)


def require_htmx(request: Request):
    """CSRF guard for mutating routes.

    Browsers replay cached Basic-auth credentials on cross-site requests, so
    a hostile page could POST to /action/* or /inventory/delete/*. HTMX sends
    HX-Request: true on every request it makes; a cross-site form cannot add
    it, and a cross-origin fetch with a custom header needs a CORS preflight
    this app never answers.
    """
    if request.headers.get("HX-Request") != "true":
        raise HTTPException(status_code=403, detail="HTMX request required")
```

Add `dependencies=[Depends(require_htmx)]` to each of the five `@router.post(...)` decorators (`/inventory/add`, `/faults/{key}/clear`, `/action/{command}`, `/inventory/update/{sku}`, `/inventory/delete/{sku}`).

- [ ] **Step 4: Run tests** — `uv run pytest tests/test_web_routes.py -q` → all pass (existing POST tests now carry the header from the fixture).

- [ ] **Step 5: Commit**

```bash
git add web_interface/routes.py tests/test_web_routes.py
git commit -m "feat(web): HX-Request guard on POST routes; login limiter wired into Basic auth"
```

---

### Task 5: Simulator and e2e credentials

**Files:**
- Modify: `simulators/base.py` (`__init__`, `run`, `entry_point`), `tests/test_integration_e2e.py`
- Test: `tests/test_simulator_base.py`

**Interfaces:**
- Produces: `ESP32Simulator(..., username: str | None = None, password: str | None = None)`; `ESP32Simulator.credentials_from(config) -> tuple[str | None, str | None]` (config values overridden by `MQTT_USERNAME`/`MQTT_PASSWORD`).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_simulator_base.py`:

```python
class TestCredentials:
    def test_credentials_from_config_unwrap_secret(self, monkeypatch):
        from pydantic import SecretStr

        for k in ("MQTT_USERNAME", "MQTT_PASSWORD"):
            monkeypatch.delenv(k, raising=False)
        cfg = ConfigModel()
        cfg.mqtt.username = "vmc"
        cfg.mqtt.password = SecretStr("pw-from-config")
        assert ESP32Simulator.credentials_from(cfg) == ("vmc", "pw-from-config")

    def test_env_overrides_config(self, monkeypatch):
        monkeypatch.setenv("MQTT_USERNAME", "envuser")
        monkeypatch.setenv("MQTT_PASSWORD", "envpw")
        cfg = ConfigModel()
        assert ESP32Simulator.credentials_from(cfg) == ("envuser", "envpw")

    def test_no_credentials_is_none_pair(self, monkeypatch):
        for k in ("MQTT_USERNAME", "MQTT_PASSWORD"):
            monkeypatch.delenv(k, raising=False)
        assert ESP32Simulator.credentials_from(ConfigModel()) == (None, None)

    async def test_run_passes_credentials_to_client(self, monkeypatch):
        import simulators.base as base

        captured = {}

        class FakeClient:
            def __init__(self, *a, **kw):
                captured.update(kw)

            async def __aenter__(self):
                raise base.aiomqtt.MqttError("stop")

            async def __aexit__(self, *exc):
                return False

        monkeypatch.setattr(base.aiomqtt, "Client", FakeClient)
        sim = ConcreteSimulator(username="u", password="p")
        task = asyncio.create_task(sim.run())
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
        assert captured["username"] == "u" and captured["password"] == "p"
```

- [ ] **Step 2: Run to verify failure** — `uv run pytest tests/test_simulator_base.py::TestCredentials -q` → `AttributeError: credentials_from` / unexpected kwarg.

- [ ] **Step 3: Implement**

`simulators/base.py` `__init__` signature gains `username: str | None = None, password: str | None = None`; store `self.username = username`, `self.password = password`. In `run()` pass `username=self.username, password=self.password` to `aiomqtt.Client(...)`. Add:

```python
    @staticmethod
    def credentials_from(config: ConfigModel) -> tuple[str | None, str | None]:
        """Broker credentials: env vars win over config; SecretStr is unwrapped."""
        username = os.environ.get("MQTT_USERNAME") or config.mqtt.username
        password = os.environ.get("MQTT_PASSWORD")
        if not password and config.mqtt.password is not None:
            password = config.mqtt.password.get_secret_value()
        return (username or None, password or None)
```

In `entry_point`, after `config = ...`: `username, password = ESP32Simulator.credentials_from(config)` and pass `username=username, password=password` to `simulator_class(...)`.

`tests/test_integration_e2e.py`: define at module level

```python
_MQTT_AUTH = {
    "username": os.environ.get("MQTT_USERNAME") or None,
    "password": os.environ.get("MQTT_PASSWORD") or None,
}
```

and pass `**_MQTT_AUTH` to every `aiomqtt.Client(...)` in the file (including `_check_broker`); for `MQTTClient` instances set `cfg.mqtt.username`/`cfg.mqtt.password = SecretStr(...)` from the same values when present. Add `import os` if missing.

- [ ] **Step 4: Run tests** — `uv run pytest tests/test_simulator_base.py tests/test_simulator_mdb.py tests/test_simulator_ice_maker.py tests/test_simulator_vending.py tests/test_integration_e2e.py -q` → pass (e2e skipped).

- [ ] **Step 5: Commit**

```bash
git add simulators/base.py tests/test_simulator_base.py tests/test_integration_e2e.py
git commit -m "feat(sim): simulators and e2e tests authenticate to the broker"
```

---

### Task 6: Compose, env, mosquitto init, Traefik labels, CI lint, docs

**Files:**
- Create: `.env.example`, `docker/mosquitto/config/.gitignore`
- Modify: `.gitignore`, `docker-compose.yml`, `docker/docker-compose.prod.yml`, `docker/docker-compose.yml` (comment), `docker/mosquitto/config/mosquitto.conf` (comment), `.github/workflows/ci.yml`, `README.md`, `CLAUDE.md`, `ROADMAP.md`

- [ ] **Step 1: `.env.example` and gitignores**

`.env.example`:

```
# Copy to .env (gitignored). Shared by the broker init, the VMC, the
# simulators and Home Assistant.
MQTT_USERNAME=vmc
MQTT_PASSWORD=change-me
# Host address the broker listens on. Use the LAN interface address so
# 1883 is never reachable through a stray port-forward or a second NIC.
MQTT_BIND_ADDR=0.0.0.0
# Docker network(s) Traefik reaches the VMC from; X-Forwarded-For is
# trusted only from these (comma-separated CIDRs).
ICE_COLDER_TRUSTED_PROXIES=
```

`.gitignore`: append `.env`. `docker/mosquitto/config/.gitignore`: `passwd`.

- [ ] **Step 2: `docker-compose.yml`**

Add before `mosquitto`:

```yaml
  mosquitto-init:
    # One-shot: writes the broker password file from .env before mosquitto starts.
    image: eclipse-mosquitto:2
    container_name: ice-colder-mqtt-init
    environment:
      - MQTT_USERNAME=${MQTT_USERNAME:?set MQTT_USERNAME in .env}
      - MQTT_PASSWORD=${MQTT_PASSWORD:?set MQTT_PASSWORD in .env}
    volumes:
      - ./docker/mosquitto/config:/mosquitto/config
    entrypoint: ["sh", "-c"]
    command:
      - mosquitto_passwd -c -b /mosquitto/config/passwd "$$MQTT_USERNAME" "$$MQTT_PASSWORD" && chmod 600 /mosquitto/config/passwd && chown mosquitto:mosquitto /mosquitto/config/passwd
    restart: "no"
```

`mosquitto` service: `ports: - "${MQTT_BIND_ADDR:-0.0.0.0}:1883:1883"`; volumes `./docker/mosquitto/config/mosquitto-prod.conf:/mosquitto/config/mosquitto.conf:ro` and `./docker/mosquitto/config/passwd:/mosquitto/config/passwd:ro`; `environment` with `MQTT_USERNAME`/`MQTT_PASSWORD`; healthcheck `mosquitto_sub -u "$$MQTT_USERNAME" -P "$$MQTT_PASSWORD" -t '$$SYS/broker/uptime' -C 1 -W 3`; add

```yaml
    depends_on:
      mosquitto-init:
        condition: service_completed_successfully
```

`vmc` and each `sim-*`: append to `environment`:

```yaml
      - MQTT_USERNAME=${MQTT_USERNAME}
      - MQTT_PASSWORD=${MQTT_PASSWORD}
```

`vmc` additionally `- ICE_COLDER_TRUSTED_PROXIES=${ICE_COLDER_TRUSTED_PROXIES:-}` and labels

```yaml
      - traefik.http.routers.ice.entrypoints=websecure
      - traefik.http.routers.ice.tls=true
```

Update the header comment: `.env` is required (`cp .env.example .env`).

- [ ] **Step 3: `docker/docker-compose.prod.yml`**

Same `mosquitto-init` service (paths relative to `docker/`: `./mosquitto/config`), `ports` with `MQTT_BIND_ADDR`, `depends_on`, env, healthcheck. Header comment: `docker compose --env-file ../.env -f docker-compose.prod.yml up -d`.

`docker/docker-compose.yml` and `docker/mosquitto/config/mosquitto.conf`: add a top comment "Anonymous broker for LOCAL DEVELOPMENT ONLY; the root compose and prod compose require credentials."

- [ ] **Step 4: CI lint**

In `.github/workflows/ci.yml` add a job:

```yaml
  compose-config:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: docker network create harbor
      - run: docker compose --env-file .env.example -f docker-compose.yml config -q
      - run: docker compose --env-file .env.example -f docker/docker-compose.prod.yml config -q
```

and add `compose-config` to the `image` job's `needs`. Verify locally: `docker --context default compose --env-file .env.example -f docker-compose.yml config -q` is NOT allowed on this machine (never use the default context); instead run `docker compose --env-file .env.example -f docker-compose.yml config -q` with the `hpz440` context, which only parses the file locally, or trust CI.

- [ ] **Step 5: Docs**

`README.md`, after the Docker section: a "Credentials" subsection: copy `.env.example` to `.env`, set `MQTT_PASSWORD`, set `MQTT_BIND_ADDR` to the LAN address, `ICE_COLDER_TRUSTED_PROXIES` to the proxy network; the dashboard refuses to start on a public interface with a password under 12 characters or a known default (`ICE_COLDER_ALLOW_WEAK_PASSWORD=1` only for private test hosts); first run prints a generated password once; Home Assistant uses the same broker user.

`CLAUDE.md`: Services list gains `auth_policy.py`; Web Dashboard paragraph mentions Basic auth + `web_interface/auth.py` limiter + `HX-Request` requirement on POST routes; Docker section mentions `.env`, `mosquitto-init`, and the env overrides `MQTT_USERNAME`, `MQTT_PASSWORD`, `ICE_COLDER_TRUSTED_PROXIES`.

`ROADMAP.md` §10: replace the two bullets (site connectivity / who needs remote access) with the answer: Traefik with TLS on the site router's 80/443, Basic auth with a login limiter and CSRF guard, authenticated broker on the LAN only, a VPN not required for the owner; technicians share the owner login until roles exist. Point to the spec.

- [ ] **Step 6: Full suite, lint, commit**

Run: `uv run pytest -q` → 683+ passed; `uv run ruff check .` clean.

```bash
git add .env.example .gitignore docker docker-compose.yml .github/workflows/ci.yml README.md CLAUDE.md ROADMAP.md
git commit -m "build: authenticated broker via .env and mosquitto-init; Traefik TLS labels; compose lint in CI; docs"
```

---

## Self-review

**Spec coverage:** §1 broker → Task 6; §2 clients → Tasks 2, 5; §3 labels → Task 6; §4 policy → Tasks 1, 2; §5 CSRF → Task 4; §6 limiter → Tasks 3, 4; §7 docs → Task 6. Testing list maps onto Tasks 1–6.

**Deviations from the spec:** `WebConfig.trusted_proxies` is read into the limiter via `routes.set_config_object` rather than a separate setter; `credentials_from` is a static method on `ESP32Simulator` so the e2e test could reuse the same rule. The pre-merge hpz440 steps (create `.env`, add a `web` section) are operator actions, not code.

**Type consistency:** `enforce_password_policy(web: WebConfig)` and `apply_env_overrides(config)` names match between Tasks 2 and 6 docs; `LoginLimiter` constructor and method names match Tasks 3 and 4; `credentials_from` matches Task 5 tests and implementation.
