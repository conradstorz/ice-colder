# Review Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix the critical defects found in the 2026-09-12 project review: secret-destroying config saves, no error-state recovery, process-killing health monitor, unauthenticated dashboard, unbounded MQTT payment amounts, test suite clobbering real config, unbounded event DB growth, and ~600 lines of dead code.

**Architecture:** All fixes stay within the existing single-asyncio-loop design. No new dependencies. Config gains a `web` section for dashboard auth/binding. `save_config` becomes atomic with secret revelation. `main.py` gains a supervisor wrapper around long-running tasks.

**Tech Stack:** Python 3.12, FastAPI, Pydantic v2, transitions, aiomqtt, loguru, pytest (asyncio_mode=auto), uv.

## Global Constraints

- Use `uv run pytest` / `uv run python` for everything; never bare `pip`/`python`.
- NEVER chain shell commands with `&&` — the permission system blocks them. Run each command as a separate tool call.
- Windows host: prefer the Bash tool for git/pytest commands; paths in code use `pathlib`.
- After code changes in a task, run `ruff check --fix .` then `ruff format .` before committing (separate calls).
- Commit after every task. Commit messages end with:
  `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`
- `colder-docker/` is an untracked scaffold directory with its own `.git` — NEVER add, modify, or delete anything under it.
- Do not modify `config.json` (gitignored, machine-specific) except where a task explicitly says so.

---

### Task 1: Repo hygiene + checkpoint commit

**Files:**
- Modify: `.gitignore`
- Modify: `pyproject.toml`
- Delete (git rm): `config.json.bak_20250607_133228`, `config/config.json.old`

**Interfaces:**
- Produces: pytest collects only `tests/` from repo root; `data/` and `colder-docker/` ignored; all in-progress work committed.

- [ ] **Step 1: Commit the existing in-progress work as a checkpoint**

There are ~35 modified tracked files (uncommitted feature work). Commit them as-is first so later tasks have clean diffs:

```bash
git add -u
git commit -m "chore: checkpoint in-progress work before review fixes

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

(`git add -u` stages only tracked modifications — it cannot pick up the untracked `Dockerfile`, `colder-docker/`, or `data/`.) Then add the new Dockerfile separately:

```bash
git add Dockerfile
git commit -m "feat: add production Dockerfile (uv-based, port 26123)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

- [ ] **Step 2: Extend .gitignore**

Append to `.gitignore` (after the `# Git worktrees` section at the end):

```
# Runtime data (SQLite event store)
data/

# Config backups produced by save_config / manual copies
config.json.bak*
config.json.tmp
*.json.old

# Vendored scaffold project (separate repo, never commit)
colder-docker/
```

- [ ] **Step 3: Scope pytest collection to tests/**

In `pyproject.toml`, change:

```toml
[tool.pytest.ini_options]
asyncio_mode = "auto"
```

to:

```toml
[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
```

- [ ] **Step 4: Remove stray tracked backup files**

```bash
git rm config.json.bak_20250607_133228
```
```bash
git rm config/config.json.old
```

- [ ] **Step 5: Verify collection works from repo root**

Run: `uv run pytest --collect-only -q`
Expected: only `tests/` items collected, no `colder-docker` errors, exit 0.

- [ ] **Step 6: Commit**

```bash
git add .gitignore pyproject.toml
git commit -m "chore: gitignore data/ and scaffold dirs; scope pytest to tests/; drop stray config backups

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 2: Secrets-safe atomic save_config + test isolation

**Files:**
- Modify: `services/config_store.py`
- Create: `tests/conftest.py`
- Create: `tests/test_config_store.py`

**Interfaces:**
- Consumes: `ConfigModel` from `config/config_model.py` (Pydantic v2, `SecretStr` fields under `payment.*` and `communication.*`).
- Produces: `save_config(config: ConfigModel, path: Path | None = None)` — writes real secret values, atomically (tmp + `os.replace`), keeping a rolling `<name>.bak`. Reads module-level `CONFIG_PATH` at call time (so tests can monkeypatch it). `add_product`/`update_product` signatures unchanged.

**Background:** Current `save_config` (`services/config_store.py:16-17`) does `path.write_text(config.model_dump_json(...))`. Pydantic serializes `SecretStr` as literal `**********`, so every product add/update destroys stored credentials. It is also non-atomic and the default arg binds `CONFIG_PATH` at import time. Additionally `tests/test_web_routes.py::test_add_product` writes to the real `config.json` because nothing isolates the path.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_config_store.py`:

```python
"""Tests for services/config_store.py — secret-preserving, atomic saves."""

import json

from pydantic import SecretStr

from config.config_model import ConfigModel
from services.config_store import add_product, save_config


def _config_with_secret() -> ConfigModel:
    cfg = ConfigModel()
    cfg.payment.stripe.api_key = SecretStr("sk_live_REALKEY123")
    return cfg


def test_save_config_writes_real_secret_values(tmp_path):
    cfg = _config_with_secret()
    target = tmp_path / "config.json"
    save_config(cfg, target)
    text = target.read_text(encoding="utf-8")
    assert "sk_live_REALKEY123" in text
    assert "**********" not in text


def test_save_config_round_trips_through_model_validate(tmp_path):
    cfg = _config_with_secret()
    target = tmp_path / "config.json"
    save_config(cfg, target)
    reloaded = ConfigModel.model_validate(json.loads(target.read_text(encoding="utf-8")))
    assert reloaded.payment.stripe.api_key.get_secret_value() == "sk_live_REALKEY123"


def test_save_config_keeps_rolling_backup(tmp_path):
    cfg = ConfigModel()
    target = tmp_path / "config.json"
    save_config(cfg, target)          # first save: no backup yet
    assert not (tmp_path / "config.json.bak").exists()
    save_config(cfg, target)          # second save: previous file backed up
    assert (tmp_path / "config.json.bak").exists()


def test_save_config_leaves_no_tmp_file(tmp_path):
    cfg = ConfigModel()
    target = tmp_path / "config.json"
    save_config(cfg, target)
    assert not (tmp_path / "config.json.tmp").exists()


def test_add_product_uses_module_config_path(tmp_path, monkeypatch):
    import services.config_store as cs

    monkeypatch.setattr(cs, "CONFIG_PATH", tmp_path / "config.json")
    cfg = ConfigModel()
    assert add_product(cfg, "NEW-1", "New Thing", 3.25) is True
    assert (tmp_path / "config.json").exists()
```

Create `tests/conftest.py`:

```python
"""Shared fixtures. Isolates every test from the developer's real config.json."""

import pytest

import services.config_store as config_store


@pytest.fixture(autouse=True)
def isolated_config_path(tmp_path, monkeypatch):
    """Redirect config saves to a temp dir so no test can clobber config.json."""
    monkeypatch.setattr(config_store, "CONFIG_PATH", tmp_path / "config.json")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_config_store.py -v`
Expected: FAIL — `test_save_config_writes_real_secret_values` (masked secrets), `test_save_config_keeps_rolling_backup` (no .bak), `test_add_product_uses_module_config_path` (default arg bound at import).

- [ ] **Step 3: Rewrite save_config**

Replace the top of `services/config_store.py` (imports through `save_config`) with:

```python
# services/config_store.py
"""
Persists product catalog changes (add/update) back to config.json.

Saves are atomic (write-to-tmp + os.replace) and keep one rolling
``config.json.bak`` of the previous version. SecretStr fields are written
with their real values so a save never destroys stored credentials.

Note: inventory counts are managed by InventoryManager (inventory.json),
not stored in config.json.
"""

import json
import os
import shutil
from pathlib import Path

from loguru import logger
from pydantic import SecretStr

from config.config_model import ConfigModel, Product

CONFIG_PATH = Path("config.json")


def _config_json(config: ConfigModel) -> str:
    """Serialize the config with real secret values (not masked)."""
    data = config.model_dump(mode="python")

    def _encode(obj):
        if isinstance(obj, SecretStr):
            return obj.get_secret_value()
        raise TypeError(f"Not JSON serializable: {type(obj)!r}")

    return json.dumps(data, indent=2, default=_encode)


def save_config(config: ConfigModel, path: Path | None = None):
    """Atomically write the config, keeping a rolling ``<name>.bak``."""
    if path is None:
        path = CONFIG_PATH
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(_config_json(config), encoding="utf-8")
    if path.exists():
        shutil.copy2(path, path.with_name(path.name + ".bak"))
    os.replace(tmp, path)
```

Keep `add_product` and `update_product` exactly as they are (they call `save_config(config)`, which now resolves `CONFIG_PATH` at call time).

Caveat for the implementer: `model_dump(mode="python")` keeps `SecretStr` instances (handled by `_encode`) and str-subclass enums like `Channel` (handled natively by `json.dumps`). If a future config field type breaks `json.dumps`, extend `_encode` — do not switch back to `model_dump_json`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_config_store.py -v`
Expected: all PASS.

- [ ] **Step 5: Run the full suite (verifies conftest isolation broke nothing)**

Run: `uv run pytest -q`
Expected: 302+ passed (existing count plus new), 9 skipped, 0 failures. Also verify `config.json` in the repo root was NOT modified by the run: `git status --short config.json` shows nothing new and the file's mtime should predate the test run (check with `ls -l config.json` before and after if unsure).

- [ ] **Step 6: Commit**

```bash
git add services/config_store.py tests/conftest.py tests/test_config_store.py
git commit -m "fix: atomic secret-preserving config saves; isolate tests from real config.json

save_config previously wrote SecretStr fields as literal ********** (destroying
stored credentials on every product edit) and overwrote config.json non-atomically.
Now: real secret values, tmp+os.replace, rolling .bak, call-time CONFIG_PATH.

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 3: Health monitor resilience + supervised long-running tasks

**Files:**
- Modify: `services/health_monitor.py:165-173` (`run()`)
- Modify: `main.py:185-193` (gather block; add `_supervise` helper above `main()`)
- Test: `tests/test_health_monitor.py` (append one test)

**Interfaces:**
- Consumes: `HealthMonitor.run()`, `MQTTClient.run()`, `uvicorn.Server.serve()` — all awaited in `main.py`.
- Produces: `_supervise(name: str, coro_factory) -> Coroutine` in `main.py` — restarts a crashed component after 5s instead of letting `asyncio.gather` tear down the process.

**Background:** `HealthMonitor.run()` has a bare `while True: await self._check()` — one exception propagates into `asyncio.gather(...)` in `main.py:190`, cancels the web server and MQTT client, and the process exits (`@logger.catch` on `main()` just logs it).

- [ ] **Step 1: Write the failing test**

Append to `tests/test_health_monitor.py`:

```python
async def test_run_survives_check_exception(monkeypatch):
    """A failing health check must not kill the run() loop."""
    import asyncio

    monitor = HealthMonitor(check_interval=0.01)
    calls = {"n": 0}

    async def exploding_check():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")

    monkeypatch.setattr(monitor, "_check", exploding_check)
    task = asyncio.create_task(monitor.run())
    await asyncio.sleep(0.1)
    assert not task.done()          # loop survived the exception
    assert calls["n"] >= 2          # and kept checking afterwards
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
```

(Match the existing import style in that file — it already imports `HealthMonitor`; add `import asyncio` at module top if not present instead of inside the test.)

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_health_monitor.py::test_run_survives_check_exception -v`
Expected: FAIL — task is done (crashed with RuntimeError).

- [ ] **Step 3: Guard the health-check loop**

In `services/health_monitor.py`, replace `run()`:

```python
    async def run(self):
        """Run periodic health checks forever. A failing check is logged, never fatal."""
        logger.info(
            f"Health monitor started: interval={self._check_interval}s, "
            f"timeout={self._subsystem_timeout}s"
        )
        while True:
            try:
                await self._check()
            except Exception:
                logger.exception("Health check round failed")
            await asyncio.sleep(self._check_interval)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_health_monitor.py -v`
Expected: all PASS.

- [ ] **Step 5: Add the supervisor in main.py**

Insert above `async def main():` in `main.py`:

```python
async def _supervise(name: str, coro_factory):
    """Keep a long-running component alive: log a crash and restart it after 5s.

    Prevents one component's unhandled exception from unwinding asyncio.gather
    and taking down the whole VMC process.
    """
    while True:
        try:
            await coro_factory()
            logger.warning(f"{name} exited unexpectedly; restarting in 5s")
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(f"{name} crashed; restarting in 5s")
        await asyncio.sleep(5)
```

Replace the gather line (`main.py:190`):

```python
        await asyncio.gather(
            server.serve(),
            _supervise("MQTT client", mqtt.run),
            _supervise("health monitor", health.run),
        )
```

(`server.serve()` stays unsupervised on purpose: if uvicorn exits, the process should exit and let Docker's `restart: unless-stopped` handle it.)

- [ ] **Step 6: Full suite + lint**

Run: `uv run pytest -q` — expected: all pass.
Run: `ruff check --fix .` then `ruff format .` (separate calls).

- [ ] **Step 7: Commit**

```bash
git add services/health_monitor.py main.py tests/test_health_monitor.py
git commit -m "fix: health-check exceptions no longer kill the process; supervise mqtt/health tasks

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 4: Working error-state recovery via admin reset

**Files:**
- Modify: `services/fsm_control.py`
- Modify: `web_interface/routes.py:155-158` (`control_action`)
- Test: `tests/test_web_routes.py` (append), plus new unit tests in `tests/test_fsm_control.py`

**Interfaces:**
- Consumes: `VMC.reset_state()` (transitions trigger, source=`error`, dest=`idle`), `routes.vmc_instance` global.
- Produces: `perform_command(command: str, vmc=None) -> str` — `reset` now actually recovers the FSM from `error`.

**Background:** The only exit from the `error` state is `reset_state`, and nothing in production calls it — `perform_command("reset")` is a logging stub. A jammed dispenser wedges the machine until process restart.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_fsm_control.py`:

```python
"""Tests for services/fsm_control.py admin commands."""

from config.config_model import ConfigModel
from controller.vmc import VMC
from services.fsm_control import perform_command


def test_reset_recovers_vmc_from_error():
    vmc = VMC(config=ConfigModel())
    vmc.error_occurred()
    assert vmc.state == "error"
    result = perform_command("reset", vmc)
    assert vmc.state == "idle"
    assert "Reset complete" in result


def test_reset_ignored_when_not_in_error():
    vmc = VMC(config=ConfigModel())
    result = perform_command("reset", vmc)
    assert vmc.state == "idle"
    assert "ignored" in result.lower()


def test_reset_without_vmc_reports_failure():
    assert "not available" in perform_command("reset", None).lower()


def test_unknown_command_still_reported():
    assert "Unknown" in perform_command("frobnicate", None)
```

Append to `tests/test_web_routes.py` inside `TestActionEndpoint`:

```python
    def test_reset_action_recovers_from_error(self, client):
        from web_interface import routes as r

        r.vmc_instance.error_occurred()
        assert r.vmc_instance.state == "error"
        resp = client.post("/action/reset")
        assert resp.status_code == 200
        assert "Reset complete" in resp.text
        assert r.vmc_instance.state == "idle"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_fsm_control.py tests/test_web_routes.py -v`
Expected: new tests FAIL (`perform_command` takes 1 arg / returns "Reset command sent" without resetting).

- [ ] **Step 3: Implement**

Replace `services/fsm_control.py` entirely:

```python
# fsm_control.py
"""Translates admin dashboard commands into VMC actions."""

from loguru import logger


def perform_command(command: str, vmc=None) -> str:
    logger.info(f"[Admin] Received command: {command}")

    match command:
        case "restart":
            logger.info("Restarting machine...")
            # TODO: Actual restart logic (process-level; handled by Docker/systemd)
            return "Restart command sent"

        case "reset":
            if vmc is None:
                logger.error("Reset requested but no VMC instance is available")
                return "Reset failed: VMC not available"
            if vmc.state != "error":
                logger.info(f"Reset ignored: VMC state is '{vmc.state}', not 'error'")
                return f"Reset ignored: machine is in '{vmc.state}', not 'error'"
            vmc.reset_state()
            logger.info("VMC reset from error to idle by admin command")
            return "Reset complete: machine returned to idle"

        case "shutdown":
            logger.info("Shutting down machine...")
            # TODO: Actual shutdown logic
            return "Shutdown command sent"

        case _:
            logger.warning(f"Unknown command: {command}")
            return f"Unknown command: {command}"
```

In `web_interface/routes.py`, change the action route:

```python
    @router.post("/action/{command}")
    async def control_action(command: str):
        result = perform_command(command, vmc_instance)
        return HTMLResponse(f"<p>{result}</p>")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_fsm_control.py tests/test_web_routes.py -v`
Expected: all PASS (existing `test_restart_action`/`test_unknown_action` still pass — return strings unchanged).

- [ ] **Step 5: Commit**

```bash
git add services/fsm_control.py web_interface/routes.py tests/test_fsm_control.py tests/test_web_routes.py
git commit -m "fix: admin reset now actually recovers the FSM from the error state

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 5: Dashboard authentication + drop wildcard CORS

**Files:**
- Modify: `config/config_model.py` (add `WebConfig`, wire into `ConfigModel`)
- Modify: `web_interface/routes.py` (HTTP Basic dependency on the router)
- Modify: `web_interface/server.py` (remove CORS middleware)
- Modify: `main.py:181-183` (host/port from config, default-password warning)
- Test: `tests/test_web_routes.py` (fixture + auth tests), `tests/test_config_model.py` (append)

**Interfaces:**
- Consumes: `routes.config` global (`ConfigModel`), set via `set_config_object`.
- Produces: `ConfigModel.web: WebConfig` with fields `host: str = "0.0.0.0"`, `port: int = 26123`, `admin_username: str = "admin"`, `admin_password: SecretStr = SecretStr("changeme")`. All dashboard routes require HTTP Basic auth.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_config_model.py`:

```python
def test_web_config_defaults():
    cfg = ConfigModel()
    assert cfg.web.host == "0.0.0.0"
    assert cfg.web.port == 26123
    assert cfg.web.admin_username == "admin"
    assert cfg.web.admin_password.get_secret_value() == "changeme"
```

In `tests/test_web_routes.py`, update the `client` fixture to authenticate:

```python
@pytest.fixture
def client():
    """Create a TestClient with a real ConfigModel and VMC."""
    cfg = ConfigModel()
    vmc = VMC(config=cfg)
    routes.set_config_object(cfg)
    routes.set_vmc_instance(vmc)

    with TestClient(app) as c:
        c.auth = ("admin", "changeme")
        yield c

        for t in vmc._pending_tasks:
            t.cancel()
```

Append a new test class:

```python
class TestAuth:
    def test_unauthenticated_request_rejected(self, client):
        resp = client.get("/", auth=None)
        assert resp.status_code == 401

    def test_wrong_password_rejected(self, client):
        resp = client.get("/", auth=("admin", "wrong"))
        assert resp.status_code == 401

    def test_mutating_endpoint_requires_auth(self, client):
        resp = client.post("/action/reset", auth=None)
        assert resp.status_code == 401
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_config_model.py tests/test_web_routes.py -v`
Expected: `test_web_config_defaults` FAILS (no `web` attr); `TestAuth` tests FAIL (200 instead of 401).

- [ ] **Step 3: Add WebConfig to the config model**

In `config/config_model.py`, insert after `class MQTTConfig` (before `class ConfigModel`):

```python
class WebConfig(BaseModel):
    """Web dashboard binding and admin authentication."""

    host: str = Field("0.0.0.0", description="Interface to bind the dashboard to")
    port: int = Field(26123, description="Dashboard port")
    admin_username: str = Field("admin", description="Dashboard admin username")
    admin_password: SecretStr = Field(
        default=SecretStr("changeme"),
        description="Dashboard admin password — CHANGE THIS before deployment",
    )
```

In `ConfigModel`, add after the `mqtt` field:

```python
    web: WebConfig = Field(
        default_factory=WebConfig, description="Web dashboard configuration"
    )
```

- [ ] **Step 4: Require HTTP Basic on all routes**

In `web_interface/routes.py`, add imports:

```python
import secrets as _secrets

from fastapi import APIRouter, Depends, FastAPI, Form, HTTPException, Query, Request
from fastapi.security import HTTPBasic, HTTPBasicCredentials
```

Add below the module globals (after `set_event_recorder`):

```python
_basic_auth = HTTPBasic()


def require_auth(credentials: HTTPBasicCredentials = Depends(_basic_auth)):
    """HTTP Basic auth for every dashboard route, checked against config.web."""
    if config is None:
        raise HTTPException(status_code=503, detail="Configuration not loaded")
    user_ok = _secrets.compare_digest(
        credentials.username, config.web.admin_username
    )
    pass_ok = _secrets.compare_digest(
        credentials.password, config.web.admin_password.get_secret_value()
    )
    if not (user_ok and pass_ok):
        raise HTTPException(
            status_code=401,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Basic"},
        )
```

Change the router creation inside `attach_routes`:

```python
    router = APIRouter(dependencies=[Depends(require_auth)])
```

- [ ] **Step 5: Remove wildcard CORS**

Replace `web_interface/server.py` contents:

```python
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import routes

# No CORS middleware: the dashboard is same-origin (HTMX partials from this app).
app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

app.mount("/static", StaticFiles(directory="web_interface/static"), name="static")
templates = Jinja2Templates(directory="web_interface/templates")

routes.attach_routes(app, templates)
```

(Disabling `/docs`/`/openapi.json` also stops advertising the route map to the LAN.)

- [ ] **Step 6: Use config for bind host/port + warn on default password**

In `main.py`, replace the uvicorn block (`main.py:180-183`):

```python
    # Start uvicorn as an asyncio task (non-blocking)
    web_cfg = live_config.web
    if web_cfg.admin_password.get_secret_value() == "changeme":
        logger.warning(
            "Web dashboard is using the DEFAULT admin password — "
            "set web.admin_password in config.json before exposing this machine"
        )
    uvicorn_config = uvicorn.Config(
        app, host=web_cfg.host, port=web_cfg.port, log_level="info"
    )
    server = uvicorn.Server(uvicorn_config)
    logger.info(f"Starting web interface on http://{web_cfg.host}:{web_cfg.port}")
```

- [ ] **Step 7: Run tests to verify they pass**

Run: `uv run pytest tests/test_config_model.py tests/test_web_routes.py -v`
Expected: all PASS. If any pre-existing web test fails with 401, it is missing the fixture's `c.auth` — fix the test, not the auth.

- [ ] **Step 8: Full suite + lint + commit**

Run: `uv run pytest -q` — all pass.
Run: `ruff check --fix .` then `ruff format .`.

```bash
git add config/config_model.py web_interface/routes.py web_interface/server.py main.py tests/test_config_model.py tests/test_web_routes.py
git commit -m "feat: HTTP Basic auth on the dashboard; drop wildcard CORS and public /docs

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 6: Bound MQTT payment amounts + deposit guard

**Files:**
- Modify: `services/mqtt_messages.py:37-42` (`PaymentEvent`), `:57-62` (`ButtonPress`), `:111-114` (`DispenseCommand`)
- Modify: `controller/vmc.py:442-456` (`deposit_funds`)
- Test: `tests/test_mqtt_messages_validation.py` (new)

**Interfaces:**
- Consumes: `VMC._handle_mqtt_payment` validates inbound payloads with `PaymentEvent.model_validate` — a `ValidationError` is caught by `MQTTClient._dispatch`'s per-handler try/except, so invalid messages are dropped and logged, not fatal.
- Produces: `PaymentEvent.amount` constrained `gt=0, le=500`; `ButtonPress.button` and `DispenseCommand.slot` constrained `ge=0`; `deposit_funds` ignores non-positive amounts.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_mqtt_messages_validation.py`:

```python
"""Bounds validation on inbound MQTT payloads (forged-message hardening)."""

import pytest
from pydantic import ValidationError

from config.config_model import ConfigModel
from controller.vmc import VMC
from services.mqtt_messages import ButtonPress, DispenseCommand, PaymentEvent


class TestPaymentEventBounds:
    def test_rejects_negative_amount(self):
        with pytest.raises(ValidationError):
            PaymentEvent(amount=-5.0, method="cash")

    def test_rejects_zero_amount(self):
        with pytest.raises(ValidationError):
            PaymentEvent(amount=0.0, method="cash")

    def test_rejects_huge_amount(self):
        with pytest.raises(ValidationError):
            PaymentEvent(amount=10_000.0, method="cash")

    def test_accepts_normal_amount(self):
        assert PaymentEvent(amount=2.50, method="cash").amount == 2.50


class TestOtherMessageBounds:
    def test_button_press_rejects_negative_index(self):
        with pytest.raises(ValidationError):
            ButtonPress(button=-1)

    def test_dispense_command_rejects_negative_slot(self):
        with pytest.raises(ValidationError):
            DispenseCommand(slot=-1)


class TestVMCDepositGuard:
    def test_deposit_ignores_negative(self):
        vmc = VMC(config=ConfigModel())
        vmc.deposit_funds(-1.0)
        assert vmc.credit_escrow == 0.0

    def test_deposit_ignores_zero(self):
        vmc = VMC(config=ConfigModel())
        vmc.deposit_funds(0.0)
        assert vmc.credit_escrow == 0.0

    async def test_mqtt_handler_drops_invalid_payment(self):
        import asyncio

        vmc = VMC(config=ConfigModel())
        vmc.attach_to_loop(asyncio.get_running_loop())
        with pytest.raises(ValidationError):
            await vmc._handle_mqtt_payment(
                "payment/credit", {"amount": -5.0, "method": "cash"}
            )
        assert vmc.credit_escrow == 0.0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_mqtt_messages_validation.py -v`
Expected: bounds tests FAIL (no constraints yet); deposit-guard tests FAIL (escrow goes negative).

- [ ] **Step 3: Add constraints**

In `services/mqtt_messages.py`:

```python
class PaymentEvent(BaseModel):
    """Credit inserted or payment status change from MDB ESP32."""

    amount: float = Field(
        ...,
        gt=0,
        le=500,
        description="Amount in dollars (bounded to reject forged/corrupt messages)",
    )
    method: str = Field(..., description="Payment method (e.g., 'cash', 'card')")
    timestamp: datetime = Field(default_factory=_utc_now)
```

`ButtonPress`: change `button: int = Field(..., description="Button index")` to `button: int = Field(..., ge=0, description="Button index")`.
`DispenseCommand`: change `slot: int = Field(..., description="Slot to dispense from")` to `slot: int = Field(..., ge=0, description="Slot to dispense from")`.

In `controller/vmc.py`, at the top of `deposit_funds` (after the debug log line):

```python
        if amount <= 0:
            logger.warning(f"Ignoring non-positive deposit: {amount}")
            return
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_mqtt_messages_validation.py -v` — all PASS.
Run: `uv run pytest -q` — full suite; if any existing test used out-of-bounds amounts, examine it: fix the test only if it was testing an amount that should now be rejected.

- [ ] **Step 5: Commit**

```bash
git add services/mqtt_messages.py controller/vmc.py tests/test_mqtt_messages_validation.py
git commit -m "fix: bound MQTT payment amounts and reject non-positive deposits

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 7: Unit tests for the money-critical VMC flows

**Files:**
- Create: `tests/test_vmc_flows.py`

**Interfaces:**
- Consumes: `VMC` (`select_product`, `_process_payment`, `_handle_mqtt_dispenser`, `_expire_session`, `_finish_dispensing`, `start_interaction`, `machine.set_state`), `ConfigModel`, `Product`.
- Produces: coverage for refund-on-jam, session-timeout refund, insufficient-funds retry, sold-out rejection, happy dispense path, dispense-timeout fallback — all currently covered only by the broker-gated (always-skipped) e2e suite.

- [ ] **Step 1: Write the tests** (these should PASS immediately — they document existing behavior; any failure is a real bug to raise, not to paper over)

Create `tests/test_vmc_flows.py`:

```python
"""Unit tests for money-critical VMC flows (no broker required).

These paths were previously only covered by tests/test_integration_e2e.py,
which skips without a live MQTT broker.
"""

import asyncio

from config.config_model import ConfigModel, Product
from controller.vmc import VMC


def make_vmc(price: float = 2.50) -> VMC:
    cfg = ConfigModel()
    cfg.physical.products = [Product(sku="ICE-1", name="Ice Bag", price=price)]
    return VMC(config=cfg)


class FakeSoldOutInventory:
    def is_available(self, sku):
        return False

    def is_tracked(self, sku):
        return True

    def decrement(self, sku):
        pass

    def get_count(self, sku):
        return 0


async def test_dispenser_jam_refunds_and_enters_error():
    vmc = make_vmc()
    vmc.attach_to_loop(asyncio.get_running_loop())
    messages: list[str] = []
    vmc.set_message_callback(messages.append)
    vmc.selected_product = vmc.products[0]
    vmc.machine.set_state("dispensing")
    vmc.credit_escrow = 0.0  # price already deducted before dispensing

    await vmc._handle_mqtt_dispenser(
        "hardware/dispenser", {"slot": 0, "state": "jammed"}
    )

    assert vmc.state == "error"
    # Jam refunds the price into escrow; on_error then refunds escrow to customer.
    assert vmc.credit_escrow == 0.0
    assert any("refunded" in m.lower() for m in messages)


async def test_session_timeout_refunds_and_returns_to_idle():
    vmc = make_vmc()
    vmc.attach_to_loop(asyncio.get_running_loop())
    vmc._session_timeout_seconds = 0.05
    messages: list[str] = []
    vmc.set_message_callback(messages.append)
    vmc.credit_escrow = 3.00
    vmc.start_interaction()

    await asyncio.sleep(0.3)

    assert vmc.state == "idle"
    assert vmc.credit_escrow == 0.0
    assert any("Refund" in m for m in messages)


async def test_insufficient_funds_waits_without_charging():
    vmc = make_vmc(price=2.50)
    vmc.attach_to_loop(asyncio.get_running_loop())
    messages: list[str] = []
    vmc.set_message_callback(messages.append)
    vmc.machine.set_state("interacting_with_user")
    vmc.selected_product = vmc.products[0]
    vmc.credit_escrow = 1.00

    vmc._process_payment()

    assert vmc.state == "interacting_with_user"
    assert vmc.credit_escrow == 1.00
    assert any("Insufficient funds" in m for m in messages)
    vmc.cancel_pending_tasks()  # cancel the scheduled 5s retry


async def test_sold_out_rejects_selection():
    vmc = make_vmc()
    vmc.attach_to_loop(asyncio.get_running_loop())
    vmc.set_inventory_manager(FakeSoldOutInventory())
    messages: list[str] = []
    vmc.set_message_callback(messages.append)

    vmc.select_product(0)

    assert vmc.state == "idle"
    assert any("sold out" in m.lower() for m in messages)


async def test_sufficient_funds_charges_and_dispenses():
    vmc = make_vmc(price=2.50)
    vmc.attach_to_loop(asyncio.get_running_loop())
    vmc.machine.set_state("interacting_with_user")
    vmc.selected_product = vmc.products[0]
    vmc.credit_escrow = 5.00

    vmc._process_payment()
    assert vmc.state == "dispensing"
    assert vmc.credit_escrow == 2.50

    await vmc._handle_mqtt_dispenser(
        "hardware/dispenser", {"slot": 0, "state": "complete"}
    )
    assert vmc.state == "interacting_with_user"  # credit remains
    vmc.cancel_pending_tasks()


async def test_dispense_timeout_fallback_completes_transaction():
    vmc = make_vmc(price=2.50)
    vmc.attach_to_loop(asyncio.get_running_loop())
    vmc.machine.set_state("interacting_with_user")
    vmc.selected_product = vmc.products[0]
    vmc.credit_escrow = 2.50

    vmc._process_payment()
    assert vmc.state == "dispensing"

    # Simulate the 60s hardware-silence fallback firing
    vmc._finish_dispensing()
    assert vmc.state == "idle"  # no credit left
    vmc.cancel_pending_tasks()
```

- [ ] **Step 2: Run the tests**

Run: `uv run pytest tests/test_vmc_flows.py -v`
Expected: all PASS. If one fails, STOP — that is a live defect in `controller/vmc.py`; report it rather than adjusting the test to match broken behavior.

- [ ] **Step 3: Full suite + commit**

Run: `uv run pytest -q` — all pass.

```bash
git add tests/test_vmc_flows.py
git commit -m "test: unit-cover refund, timeout, sold-out, and dispense flows without a broker

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 8: Event DB retention

**Files:**
- Modify: `services/event_recorder.py` (`__init__`, `record`; new `prune()`)
- Test: `tests/test_event_recorder.py` (append)

**Interfaces:**
- Consumes: SQLite `events` table (`timestamp REAL` = `time.time()` seconds).
- Produces: `EventRecorder(db_path, temp_min, temp_max, retention_days: int = 90)`; `prune()` deletes rows older than the window; called on init and at most daily from `record()`.

**Background:** `data/events.db` reached 57 MB in dev with no retention. Heartbeats arrive every 10s forever — on a Pi SD card this grows unbounded.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_event_recorder.py` (match its existing import style; it already imports `EventRecorder`; add `import sqlite3` and `import time` at module top if absent):

```python
class TestRetention:
    def test_prune_removes_events_older_than_retention(self, tmp_path):
        rec = EventRecorder(db_path=str(tmp_path / "e.db"), retention_days=1)
        old_ts = time.time() - 2 * 86400
        with sqlite3.connect(str(tmp_path / "e.db")) as conn:
            conn.execute(
                "INSERT INTO events (event_type, timestamp, value) VALUES (?, ?, ?)",
                ("payment", old_ts, 1.0),
            )
        rec.record("payment", 1.0)
        rec.prune()
        with sqlite3.connect(str(tmp_path / "e.db")) as conn:
            count = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        assert count == 1  # only the fresh event survives

    def test_init_prunes_existing_old_events(self, tmp_path):
        db = str(tmp_path / "e.db")
        rec = EventRecorder(db_path=db, retention_days=1)
        old_ts = time.time() - 2 * 86400
        with sqlite3.connect(db) as conn:
            conn.execute(
                "INSERT INTO events (event_type, timestamp, value) VALUES (?, ?, ?)",
                ("payment", old_ts, 1.0),
            )
        rec2 = EventRecorder(db_path=db, retention_days=1)
        with sqlite3.connect(db) as conn:
            count = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        assert count == 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_event_recorder.py -v`
Expected: FAIL — `retention_days` unexpected kwarg / no `prune` attribute.

- [ ] **Step 3: Implement retention**

In `services/event_recorder.py`, change `__init__`:

```python
    def __init__(
        self,
        db_path: str = "data/events.db",
        temp_min: float = -20.0,
        temp_max: float = 80.0,
        retention_days: int = 90,
    ):
        self._db_path = db_path
        self._temp_min = temp_min
        self._temp_max = temp_max
        self._retention_days = retention_days
        self._last_prune = 0.0
        self._init_db()
        self.prune()
```

Add after `record()`:

```python
    def prune(self):
        """Delete events older than the retention window (SD-card growth guard)."""
        cutoff = time.time() - self._retention_days * 86400
        with sqlite3.connect(self._db_path) as conn:
            cur = conn.execute("DELETE FROM events WHERE timestamp < ?", (cutoff,))
        self._last_prune = time.time()
        if cur.rowcount:
            logger.info(
                f"EventRecorder: pruned {cur.rowcount} events older than "
                f"{self._retention_days} days"
            )
```

At the end of `record()` (after the debug log line):

```python
        if time.time() - self._last_prune > 86400:
            self.prune()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_event_recorder.py -v` — all PASS.

- [ ] **Step 5: Full suite + lint + commit**

Run: `uv run pytest -q`; then `ruff check --fix .`; then `ruff format .`.

```bash
git add services/event_recorder.py tests/test_event_recorder.py
git commit -m "feat: 90-day retention pruning for the SQLite event store

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 9: Dead-code purge + documentation fixes

**Files:**
- Delete: `controller/event_store.py`, `config/state_model.py`, `controller/message_manager.py`, `controller/payment_device_baseclass_fsm.py`, `services/async_payment_fsm.py`, `services/virtual_payment_fsm.py`, `hardware/mdb_payment_fsm.py`, `hardware/dispensing_fsm.py`, `requirements.txt`
- Modify: `CLAUDE.md`

**Interfaces:**
- Consumes: nothing — these modules are imported by no live code (verify in Step 1).
- Produces: a codebase where the only FSMs are `controller/vmc.py` and the only persistence is `services/event_recorder.py` + `services/inventory_manager.py` + `services/config_store.py`.

- [ ] **Step 1: Verify the modules are truly dead**

Run (Grep tool, not shell): search pattern `event_store|state_model|message_manager|payment_device_baseclass|async_payment_fsm|virtual_payment_fsm|mdb_payment_fsm|dispensing_fsm` across `*.py` files, excluding `colder-docker/`.
Expected: matches only inside the files being deleted themselves (self-references/comments). If ANY live module or test imports one of them, STOP and report — do not delete that file.

- [ ] **Step 2: Delete**

```bash
git rm controller/event_store.py config/state_model.py controller/message_manager.py controller/payment_device_baseclass_fsm.py services/async_payment_fsm.py services/virtual_payment_fsm.py hardware/mdb_payment_fsm.py hardware/dispensing_fsm.py requirements.txt
```

- [ ] **Step 3: Full suite still green**

Run: `uv run pytest -q` — same pass count as Task 8.
Run: `uv run python -c "import main"` — imports cleanly (catches any missed import).

- [ ] **Step 4: Fix CLAUDE.md**

In `CLAUDE.md`:

1. Replace the **Entry Point & Startup** paragraph with:

```markdown
### Entry Point & Startup (`main.py`)

`main()` loads `config.json` into a Pydantic `ConfigModel`, then runs three
concurrent asyncio tasks on a single event loop: a uvicorn web server (host/port
from `config.web`, default `0.0.0.0:26123`, HTTP Basic auth from
`config.web.admin_username`/`admin_password`), the MQTT client, and the health
monitor. The MQTT client and health monitor are wrapped in a supervisor that
restarts them on crash; if uvicorn exits, the process exits (Docker's
`restart: unless-stopped` handles process-level restarts).
```

2. In the **Configuration** paragraph, replace the sentence `Missing keys are deep-merged with defaults and the original file is backed up before overwriting.` with:

```markdown
Missing keys are filled from Pydantic defaults at load time. Saves via
`services/config_store.py` are atomic (tmp + rename), write real secret values,
and keep a rolling `config.json.bak`.
```

3. Replace the **Docker** section with:

```markdown
### Docker

`Dockerfile` builds from `python:3.12-slim`, installs dependencies with
`uv sync --frozen --no-dev` from `pyproject.toml`/`uv.lock`, and runs
`uv run python main.py` (port 26123). `docker-compose.yml` orchestrates the VMC,
the three ESP32 simulators, and a mosquitto broker, all with
`restart: unless-stopped`. There is no `requirements.txt` — `pyproject.toml` is
the single dependency source of truth.
```

4. In the **Hardware** section, remove the lines mentioning `dispensing_fsm.py` (deleted) and note that `mdb_interface.py` is a reference stub — real MDB communication happens on the ESP32 and arrives over MQTT.

- [ ] **Step 5: Lint + commit**

Run: `ruff check --fix .` then `ruff format .`.

```bash
git add -A
git commit -m "chore: remove abandoned FSM/persistence modules and stale requirements.txt; fix CLAUDE.md

Deleted dead code (never imported by the live app): event_store, state_model,
message_manager (Tkinter), the parallel payment-FSM hierarchy, dispensing_fsm.
CLAUDE.md now matches the real startup model, Dockerfile, and config save behavior.

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Out of scope (deliberately)

- MQTT broker TLS + per-device credentials (infrastructure/ESP32 firmware work, not this repo's Python code).
- Moving blocking SQLite/file I/O to executors (review finding #7) — real but lower urgency; separate plan.
- Auto-reset from `error` on a timer — product decision (auto-retrying into a jammed dispenser may be worse); admin reset now works.
- Deleting `colder-docker/` from disk — user's call; it is now gitignored.
- `config.json` in the repo root currently contains test-fixture data from a pre-fix pytest run (products list shows "TEST-001 Test Ice"); its secrets were already masked before this. The owner must re-enter real products/credentials by hand — code cannot recover them.
