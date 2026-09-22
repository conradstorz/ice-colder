# Unattended Operation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The VMC only accepts money when it can deliver, raises communication loss as registry faults, survives its own restart without losing a customer's credit, never drops a distinct owner alert, and keeps blocking I/O off the event loop.

**Architecture:** A new `services/availability.py` owns a permissive truth table and publishes `cmd/payment/enable` on change; VMC and HealthMonitor feed it. A new `services/session_store.py` persists the live sale so a restart raises `PAY-104` instead of forgetting escrow. Reliability fixes land in the notifier, MQTT client, event recorder, config store, and paths. A read-only `/screen` route and an arm64 image complete the owner-facing side.

**Tech Stack:** Python 3.12, asyncio, aiomqtt, pydantic v2, `transitions`, FastAPI + Jinja2 + HTMX, SQLite, pytest (asyncio_mode=auto), uv, ruff.

**Spec:** `docs/superpowers/specs/2026-09-21-unattended-operation-design.md`

## Global Constraints

- Run every command with `uv run ...`; never `pip`, never `python` bare.
- Do not chain shell commands with `&&`; one command per tool call.
- Lint before each commit: `uv run ruff check --fix .` then `uv run ruff format .`.
- Branch: `feat/unattended-operation` (already created). Commit after every task with the trailer `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`.
- Full suite must stay green: `uv run pytest -q` (598 passed, 10 skipped at start; e2e tests skip without a broker).
- Fault codes are never renumbered; `PAY-104` is the only new code. Adding it bumps `CONTRACT_VERSION` from `0.2.0` to `0.3.0`.
- Instrumented permissives start `UNKNOWN` and `UNKNOWN` is not a pass. Two exceptions, decided during planning because the vending ESP32 does not report them unprompted: `service_door_closed` and `no_critical_fault` and `transaction_certain` start `PASS`.
- Not-instrumented permissives are permanent `PASS` with `detail="not instrumented"`.
- Subsystem names are exactly `vending`, `mdb`, `ice_maker` (see `simulators/*.py` and `contracts/vending_machine.py:EXPECTED_SUBSYSTEMS`).
- `HardwareIO.state` is a bool; for `service_door` `True` means open; for `bin_half_full` `True` means ice present.
- Left as stubs, do not implement: admin restart/shutdown, broker hardening, SMS/Snapchat, revenue tagging, gateway reconciliation.

---

### Task 1: Shared paths and the logs-tab path fix

**Files:**
- Create: `services/paths.py`
- Modify: `main.py:31-84` (setup_logging), `web_interface/routes.py:74`
- Test: `tests/test_web_routes.py`

**Interfaces:**
- Produces: `services.paths.LOG_DIR: Path`, `LOG_FILE: Path`, `DATA_DIR: Path`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_web_routes.py`:

```python
class TestLogsContent:
    def test_logs_tab_shows_written_line(self, client, tmp_path, monkeypatch):
        from web_interface import routes as r

        log_file = tmp_path / "LOGS" / "vmc.log"
        log_file.parent.mkdir()
        log_file.write_text("first line\nunique-marker-42;INFO 2026-09-21\n", encoding="utf-8")
        monkeypatch.setattr(r, "LOG_PATH", log_file)

        resp = client.get("/logs")
        assert resp.status_code == 200
        assert "unique-marker-42" in resp.text

    def test_log_path_matches_logging_setup(self):
        from services.paths import LOG_FILE
        from web_interface import routes as r

        assert r.LOG_PATH == LOG_FILE
        assert LOG_FILE.parts[-2:] == ("LOGS", "vmc.log")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_web_routes.py::TestLogsContent -v`
Expected: `test_log_path_matches_logging_setup` FAILS (`ModuleNotFoundError: services.paths`).

- [ ] **Step 3: Create `services/paths.py`**

```python
# services/paths.py
"""Filesystem locations shared by main.py, the dashboard and the services.

Everything is relative to the working directory (``/app`` in Docker), matching
the bind mounts in docker-compose.yml (``./LOGS:/app/LOGS``, ``./data:/app/data``).
"""

from pathlib import Path

LOG_DIR = Path("LOGS")
LOG_FILE = LOG_DIR / "vmc.log"
DATA_DIR = Path("data")
```

- [ ] **Step 4: Use it in `main.py` and `routes.py`**

In `main.py`, add `from services.paths import LOG_DIR, LOG_FILE` to the imports and change `setup_logging`:

```python
    os.makedirs(LOG_DIR, exist_ok=True)
    ...
    logger.add(
        str(LOG_FILE),
        serialize=False,
        ...
    )
    # Transaction log ...
    logger.add(
        str(LOG_DIR / "transactions.log"),
        ...
    logger.add(
        str(LOG_DIR / "ice_maker.log"),
        ...
    logger.add(
        str(LOG_DIR / "vending.log"),
```

In `web_interface/routes.py` replace line 74:

```python
from services.paths import LOG_FILE

LOG_PATH = LOG_FILE
```

And make `view_logs` non-blocking:

```python
    @router.get("/logs", response_class=HTMLResponse)
    async def view_logs(request: Request):
        lines = await asyncio.to_thread(tail, LOG_PATH, 10)
        return templates.TemplateResponse(
            "partials/logs_fragment.html", {"request": request, "logs": lines}
        )
```

- [ ] **Step 5: Run tests**

Run: `uv run pytest tests/test_web_routes.py -q`
Expected: all pass.

- [ ] **Step 6: Lint and commit**

```bash
uv run ruff check --fix .
uv run ruff format .
git add services/paths.py main.py web_interface/routes.py tests/test_web_routes.py
git commit -m "fix(web): logs tab reads LOGS/vmc.log via shared services.paths; tail off the event loop"
```

---

### Task 2: fsync in `config_store.save_config`

**Files:**
- Modify: `services/config_store.py:52-60`
- Test: `tests/test_config_store.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_config_store.py`:

```python
def test_save_config_fsyncs_before_replace(tmp_path, monkeypatch):
    import os
    import services.config_store as cs

    calls: list[str] = []
    real_fsync = os.fsync
    real_replace = os.replace

    def fake_fsync(fd):
        calls.append("fsync")
        return real_fsync(fd)

    def fake_replace(src, dst):
        calls.append("replace")
        return real_replace(src, dst)

    monkeypatch.setattr(cs.os, "fsync", fake_fsync)
    monkeypatch.setattr(cs.os, "replace", fake_replace)

    save_config(ConfigModel(), tmp_path / "config.json")

    assert "fsync" in calls
    assert calls.index("fsync") < calls.index("replace")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_config_store.py::test_save_config_fsyncs_before_replace -v`
Expected: FAIL (`"fsync" in calls` is False).

- [ ] **Step 3: Implement**

Replace `save_config` in `services/config_store.py`:

```python
def save_config(config: ConfigModel, path: Path | None = None):
    """Atomically write the config, keeping a rolling ``<name>.bak``.

    The temp file is flushed and fsync'd before ``os.replace`` so a power loss
    right after the rename cannot leave an empty or truncated config.json.
    """
    if path is None:
        path = _config_path()
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(_config_json(config))
        f.flush()
        os.fsync(f.fileno())
    if path.exists():
        shutil.copy2(path, path.with_name(path.name + ".bak"))
    os.replace(tmp, path)
    _fsync_dir(path.parent)


def _fsync_dir(directory: Path) -> None:
    """Flush the directory entry after a rename (no-op on Windows)."""
    if os.name != "posix":
        return
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_config_store.py -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
uv run ruff check --fix .
uv run ruff format .
git add services/config_store.py tests/test_config_store.py
git commit -m "fix(config): fsync temp file and directory before atomic replace"
```

---

### Task 3: Notifier cooldown keyed per fault

**Files:**
- Modify: `services/notifier.py:33-51`
- Create: `tests/test_notifier.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_notifier.py
"""Notifier rate limiting: distinct faults from one source must all reach the owner."""

from unittest.mock import AsyncMock

from config.config_model import ConfigModel
from services.health_monitor import Alert
from services.notifier import Notifier


def _notifier() -> Notifier:
    n = Notifier(ConfigModel())
    n._send_email = AsyncMock()
    # Force the email branch regardless of placeholder config.
    n._deliver = AsyncMock()
    return n


async def test_two_codes_from_same_source_both_send():
    n = _notifier()
    await n.send(Alert(level="error", source="vmc", message="a", code="ICE-301"))
    await n.send(Alert(level="error", source="vmc", message="b", code="PAY-103"))
    assert n._deliver.await_count == 2


async def test_same_code_twice_is_suppressed():
    n = _notifier()
    await n.send(Alert(level="error", source="vmc", message="a", code="ICE-301"))
    await n.send(Alert(level="error", source="vmc", message="a", code="ICE-301"))
    assert n._deliver.await_count == 1


async def test_same_code_different_product_both_send():
    n = _notifier()
    await n.send(Alert(level="error", source="vmc", message="a", code="ICE-301", product_sku="ICE-1"))
    await n.send(Alert(level="error", source="vmc", message="a", code="ICE-301", product_sku="ICE-2"))
    assert n._deliver.await_count == 2


async def test_no_code_falls_back_to_message():
    n = _notifier()
    await n.send(Alert(level="warning", source="mdb", message="stale"))
    await n.send(Alert(level="warning", source="mdb", message="different"))
    assert n._deliver.await_count == 2
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_notifier.py -v`
Expected: FAIL (`_deliver` not called: the current code returns early on the second alert, and there is no `_deliver` method so the mock is never invoked by `send`).

- [ ] **Step 3: Implement**

In `services/notifier.py` replace the body of `send` from the rate-limit comment through the channel dispatch with:

```python
    @staticmethod
    def _cooldown_key(alert: Alert) -> str:
        """One cooldown bucket per distinct fault, not per source.

        Every registry fault is raised with source="vmc"; keying on source
        alone silently dropped any second fault inside the cooldown.
        """
        return f"{alert.source}|{alert.code or alert.message}|{alert.product_sku or ''}"

    async def send(self, alert: Alert):
        """
        Route an alert to the owner via their preferred channel.
        Runs blocking I/O (SMTP) in a thread executor to stay async.
        """
        now = asyncio.get_running_loop().time()
        key = self._cooldown_key(alert)
        last = self._last_sent.get(key)
        # `last is None` must mean "never sent": loop.time() is monotonic and can
        # be small on a freshly booted host, so a 0.0 sentinel would wrongly
        # suppress the first alert from every key for a whole cooldown.
        if last is not None and now - last < self._cooldown_seconds:
            logger.debug(f"Notifier: Suppressing alert {key} (cooldown)")
            return
        self._last_sent[key] = now

        logger.info(
            f"Notifier: [{alert.level}] {alert.source} -> {self._owner.name}: "
            f"{alert.message}"
        )
        await self._deliver(alert)

    async def _deliver(self, alert: Alert) -> None:
        """Pick the owner's channel and send. Split out so tests can stub delivery."""
        gateway_info = self._config.get_preferred_gateway_for(self._owner)
        if gateway_info is None:
            logger.warning("Notifier: No configured gateway for owner")
            return

        channel, gateway_config = gateway_info

        if channel == Channel.email:
            if not gateway_config.is_configured or _is_placeholder_host(
                self._owner.email
            ):
                if not self._warned_unconfigured:
                    logger.warning(
                        "Notifier: email gateway not configured (placeholder "
                        "smtp server or owner address); alerts are logged only"
                    )
                    self._warned_unconfigured = True
                return
            await self._send_email(alert, gateway_config)
        elif channel == Channel.sms:
            logger.info(f"Notifier: SMS alert would be sent to {self._owner.phone}")
            # SMS integration is a future task
        else:
            logger.info(f"Notifier: Channel {channel} not yet implemented")
```

Update the `__init__` comment: `# Rate limiting: last send time per (source, code|message, sku)`.

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_notifier.py tests/test_health_monitor.py -q`
Expected: all pass. If a test in `tests/test_health_monitor.py` asserted the old per-source suppression, update its assertion to the new key semantics (two different messages from one source now both send).

- [ ] **Step 5: Commit**

```bash
uv run ruff check --fix .
uv run ruff format .
git add services/notifier.py tests/test_notifier.py tests/test_health_monitor.py
git commit -m "fix(notifier): cooldown per fault code, not per source"
```

---

### Task 4: MQTT retain, LWT and online presence

**Files:**
- Modify: `services/mqtt_client.py:69-91, 133-165`, `services/mqtt_messages.py` (after `VMCStatus`), `controller/vmc.py:255-271`
- Test: `tests/test_mqtt.py`

**Interfaces:**
- Produces: `MQTTClient.publish(topic_suffix, payload, qos=1, retain=False)`; `VMCOnline(online: bool)` model; retained `vmc/{id}/online` and `vmc/{id}/status`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_mqtt.py`:

```python
class _RecordingAiomqttClient(_FakeAiomqttClient):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.published: list[tuple] = []

    async def publish(self, topic, payload, qos=0, retain=False):
        self.published.append((topic, payload, qos, retain))


class TestMQTTPresence:
    async def test_publish_forwards_retain(self):
        client = MQTTClient(config=MQTTConfig(), machine_id="vmc-0001")
        client._client = AsyncMock()
        client._connected = True
        await client.publish("status", {"a": 1}, retain=True)
        client._client.publish.assert_awaited_once_with(
            "vmc/vmc-0001/status", '{"a": 1}', qos=1, retain=True
        )

    async def test_connect_sets_last_will_and_publishes_online(self, monkeypatch):
        import services.mqtt_client as mc

        captured: dict = {}
        fake = _RecordingAiomqttClient()

        def factory(*args, **kwargs):
            captured.update(kwargs)
            return fake

        monkeypatch.setattr(mc.aiomqtt, "Client", factory)
        client = MQTTClient(config=MQTTConfig(), machine_id="vmc-0001")
        await client._connect_and_listen()

        will = captured["will"]
        assert will.topic == "vmc/vmc-0001/online"
        assert will.retain is True
        assert will.qos == 1
        assert json.loads(will.payload)["online"] is False

        topic, payload, qos, retain = fake.published[0]
        assert topic == "vmc/vmc-0001/online"
        assert json.loads(payload)["online"] is True
        assert (qos, retain) == (1, True)

    def test_vmc_online_model(self):
        from services.mqtt_messages import VMCOnline

        m = VMCOnline(online=True)
        assert m.online is True
        assert isinstance(m.timestamp, datetime)


class TestStatusRetained:
    async def test_publish_status_is_retained(self):
        vmc = _make_vmc()
        vmc.attach_to_loop(asyncio.get_running_loop())
        mqtt = MagicMock()
        mqtt.publish = AsyncMock()
        vmc._mqtt_client = mqtt
        vmc._publish_status()
        await asyncio.sleep(0)
        args, kwargs = mqtt.publish.await_args
        assert args[0] == "status"
        assert kwargs.get("retain") is True
        vmc.cancel_pending_tasks()
```

Add `import json` at the top of `tests/test_mqtt.py` if absent.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_mqtt.py -k "Presence or StatusRetained" -v`
Expected: FAIL (`retain` unexpected kwarg / `KeyError: 'will'` / `ImportError: VMCOnline`).

- [ ] **Step 3: Implement the model**

In `services/mqtt_messages.py` after `VMCStatus`:

```python
class VMCOnline(BaseModel):
    """Retained presence on vmc/{id}/online; the Last-Will publishes online=false."""

    online: bool
    timestamp: datetime = Field(default_factory=_utc_now)
```

- [ ] **Step 4: Implement the client changes**

In `services/mqtt_client.py`:

```python
from services.mqtt_messages import VMCOnline
```

```python
    @property
    def online_topic(self) -> str:
        return f"{self.topic_prefix}/online"

    async def publish(
        self,
        topic_suffix: str,
        payload: BaseModel | dict,
        qos: int = 1,
        retain: bool = False,
    ):
        """
        Publish a message to vmc/{machine_id}/{topic_suffix}.

        Accepts either a Pydantic model (serialized to JSON) or a plain dict.
        Defaults to QoS 1 (contract-mandated for commands/acks/events/heartbeats/
        refunds); pass qos=0 only for high-rate sensor readings. ``retain=True``
        is for state a late subscriber must see (status, online), never commands.
        """
        if self._client is None or not self._connected:
            logger.warning(f"MQTT: Cannot publish to {topic_suffix} — not connected")
            return

        full_topic = f"{self.topic_prefix}/{topic_suffix}"
        if isinstance(payload, BaseModel):
            data = payload.model_dump_json()
        else:
            data = json.dumps(payload)

        try:
            await self._client.publish(full_topic, data, qos=qos, retain=retain)
            logger.debug(f"MQTT: Published to {full_topic}")
        except Exception as e:
            logger.error(f"MQTT: Failed to publish to {full_topic}: {e}")
```

and in `_connect_and_listen`:

```python
        will = aiomqtt.Will(
            topic=self.online_topic,
            payload=VMCOnline(online=False).model_dump_json(),
            qos=1,
            retain=True,
        )
        async with aiomqtt.Client(
            hostname=self._config.broker_host,
            port=self._config.broker_port,
            username=self._config.username,
            password=password,
            identifier=self._config.client_id,
            keepalive=self._config.keepalive,
            will=will,
        ) as client:
            self._client = client
            self._connected = True
            await client.publish(
                self.online_topic,
                VMCOnline(online=True).model_dump_json(),
                qos=1,
                retain=True,
            )
            if self._connection_callback:
                self._connection_callback(True)
```

Keep the existing "Connected" log and subscribe loop after that.

- [ ] **Step 5: Retain the status publish in `controller/vmc.py`**

In `_publish_status` replace the `create_task` line with:

```python
        self._fire_and_forget(self._mqtt_client.publish("status", status, retain=True))
```

- [ ] **Step 6: Fix existing publish assertions**

In `tests/test_mqtt.py`, `test_publish_defaults_to_qos_1` and `test_publish_honors_explicit_qos_0` assert `assert_awaited_once_with(..., qos=N)`; add `retain=False` to each expected call. Search the rest of `tests/` for `publish.assert_awaited` / `assert_called` with `qos=` and add `retain=False` where the call goes through `MQTTClient.publish` to a mocked `_client`.

- [ ] **Step 7: Run tests**

Run: `uv run pytest tests/test_mqtt.py tests/test_vmc_fsm.py tests/test_vmc_flows.py -q`
Expected: all pass.

- [ ] **Step 8: Commit**

```bash
uv run ruff check --fix .
uv run ruff format .
git add services/mqtt_client.py services/mqtt_messages.py controller/vmc.py tests/test_mqtt.py
git commit -m "feat(mqtt): retained status, LWT on vmc/{id}/online, publish(retain=)"
```

---

### Task 5: Event recorder writer thread; inventory save off-loop

**Files:**
- Modify: `services/event_recorder.py:46-115`, `services/inventory_manager.py:60-90`, `controller/vmc.py:1163-1184`
- Test: `tests/test_event_recorder.py`, `tests/test_inventory_manager.py`

**Interfaces:**
- Produces: `EventRecorder.flush(timeout: float = 5.0) -> None`; `InventoryManager.decrement(sku, *, persist=True)`; `async InventoryManager.save_async()`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_event_recorder.py`:

```python
class TestWriterThread:
    def test_record_returns_before_row_is_visible_then_flush_makes_it_visible(self, tmp_path):
        db = str(tmp_path / "events.db")
        rec = EventRecorder(db_path=db)
        rec.record("payment", value=2.0)
        rec.flush()
        conn = sqlite3.connect(db)
        assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1

    def test_get_summary_flushes_pending_rows(self, recorder):
        recorder.record("payment", value=1.5)
        assert recorder.get_summary(24)["money_in"] == 1.5

    def test_writer_survives_bad_row(self, recorder, monkeypatch):
        recorder.record("payment", value=float("nan"))  # sqlite stores NULL; must not kill thread
        recorder.record("dispense", value=1.0)
        recorder.flush()
        assert recorder.get_summary(24)["products_out"] == 1
```

Append to `tests/test_inventory_manager.py`:

```python
async def test_decrement_without_persist_then_save_async(tmp_path):
    from config.config_model import Product
    from services.inventory_manager import InventoryManager

    path = tmp_path / "inventory.json"
    inv = InventoryManager([Product(sku="A", track_inventory=True, inventory_count=3)], path=path)
    inv.decrement("A", persist=False)
    assert json.loads(path.read_text())["A"] == 3  # not yet written
    await inv.save_async()
    assert json.loads(path.read_text())["A"] == 2
```

Add `import json` to that test file if missing.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_event_recorder.py::TestWriterThread tests/test_inventory_manager.py::test_decrement_without_persist_then_save_async -v`
Expected: FAIL (`AttributeError: flush` / unexpected keyword `persist`).

- [ ] **Step 3: Implement the writer thread**

In `services/event_recorder.py` add imports `import queue`, `import threading`, and change `__init__`/`record`/`prune`/`_compute_window`:

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
        self._historical_avg_cache: dict[int, tuple[float, dict]] = {}
        self._init_db()
        self.prune()
        # All inserts go through one daemon thread with one connection so MQTT
        # handlers never block the event loop on SD-card writes.
        self._queue: queue.Queue = queue.Queue()
        self._writer = threading.Thread(
            target=self._writer_loop, name="event-recorder", daemon=True
        )
        self._writer.start()

    def record(
        self, event_type: str, value: float = 1.0, metadata: Optional[dict] = None
    ):
        """Queue one event row; the writer thread inserts it."""
        meta_str = json.dumps(metadata) if metadata else None
        self._queue.put((event_type, time.time(), value, meta_str))
        logger.debug(f"EventRecorder: {event_type} value={value}")

    def flush(self, timeout: float = 5.0) -> None:
        """Block until every queued row is written (tests, shutdown, reads)."""
        deadline = time.monotonic() + timeout
        while self._queue.unfinished_tasks and time.monotonic() < deadline:
            time.sleep(0.005)

    def _writer_loop(self) -> None:
        conn = sqlite3.connect(self._db_path, check_same_thread=False)
        while True:
            row = self._queue.get()
            try:
                conn.execute(
                    "INSERT INTO events (event_type, timestamp, value, metadata) VALUES (?, ?, ?, ?)",
                    row,
                )
                conn.commit()
                if time.time() - self._last_prune > 86400:
                    self._prune_with(conn)
            except Exception:
                logger.exception(f"EventRecorder: failed to write {row[0]}")
            finally:
                self._queue.task_done()

    def prune(self):
        """Delete events older than the retention window (SD-card growth guard)."""
        with sqlite3.connect(self._db_path) as conn:
            self._prune_with(conn)

    def _prune_with(self, conn: sqlite3.Connection) -> None:
        cutoff = time.time() - self._retention_days * 86400
        cur = conn.execute("DELETE FROM events WHERE timestamp < ?", (cutoff,))
        conn.commit()
        self._last_prune = time.time()
        if cur.rowcount:
            logger.info(
                f"EventRecorder: pruned {cur.rowcount} events older than "
                f"{self._retention_days} days"
            )
```

At the top of `_compute_window` add `self.flush()` as the first statement so every read sees queued rows. `get_historical_average` goes through `_compute_window` already.

- [ ] **Step 4: Implement inventory save_async**

In `services/inventory_manager.py` add `import asyncio` and:

```python
    async def save_async(self) -> None:
        """Persist off the event loop (VMC hot path)."""
        await asyncio.to_thread(self._save)

    def decrement(self, sku: str, *, persist: bool = True):
        """Decrement inventory for a SKU; persist synchronously unless told not to."""
        if sku in self._counts:
            self._counts[sku] = max(0, self._counts[sku] - 1)
            logger.info(f"Inventory: {sku} decremented to {self._counts[sku]}")
            if persist:
                self._save()
```

In `controller/vmc.py` `_finish_dispensing`:

```python
            if self._inventory.is_tracked(sku):
                self._inventory.decrement(sku, persist=False)
                self._fire_and_forget(self._inventory.save_async())
```

`tests/test_vmc_flows.py::FakeSoldOutInventory.decrement(self, sku)` needs `**kwargs`; add `async def save_async(self): pass` to it and to any other fake inventory in `tests/`.

- [ ] **Step 5: Run tests**

Run: `uv run pytest tests/test_event_recorder.py tests/test_inventory_manager.py tests/test_vmc_flows.py tests/test_web_routes.py -q`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
uv run ruff check --fix .
uv run ruff format .
git add services/event_recorder.py services/inventory_manager.py controller/vmc.py tests/
git commit -m "perf: event recorder writes on a thread; inventory save off the loop"
```

---

### Task 6: Contract PAY-104 and `Product.kind`

**Files:**
- Modify: `contracts/vending_machine.py` (FaultCode, FAULT_TABLE, CONTRACT_VERSION), `config/config_model.py:101-123`, `docs/contracts/vending-machine/schemas/fault_code.schema.json` (generated), `docs/contracts/vending-machine/CONTRACT.md`, `ROADMAP.md` §5
- Test: `tests/test_contracts_vending.py`, `tests/test_config_model.py`

**Interfaces:**
- Produces: `FaultCode.PAY_104`; `Product.kind: Literal["ice","water","other"]`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_contracts_vending.py`:

```python
def test_pay_104_is_a_machine_lockout():
    from contracts.vending_machine import FAULT_TABLE, FaultCode, Scope, Severity

    spec = FAULT_TABLE[FaultCode.PAY_104]
    assert FaultCode.PAY_104.value == "PAY-104"
    assert spec.severity is Severity.lockout
    assert spec.scope is Scope.machine
    assert "restart" in spec.description.lower()


def test_contract_version_bumped_for_new_code():
    from contracts.vending_machine import CONTRACT_VERSION

    assert CONTRACT_VERSION == "0.3.0"
```

Append to `tests/test_config_model.py`:

```python
def test_product_kind_defaults_to_other_and_validates():
    from pydantic import ValidationError
    from config.config_model import Product

    assert Product().kind == "other"
    assert Product(kind="ice").kind == "ice"
    with pytest.raises(ValidationError):
        Product(kind="soda")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_contracts_vending.py tests/test_config_model.py -q`
Expected: the three new tests FAIL.

- [ ] **Step 3: Implement**

`contracts/vending_machine.py`: set `CONTRACT_VERSION = "0.3.0"`, add after `PAY_103`:

```python
    PAY_104 = "PAY-104"
```

and after the `PAY_103` table entry:

```python
    FaultCode.PAY_104: FaultSpec(
        severity=Severity.lockout,
        scope=Scope.machine,
        description="Transaction uncertain after VMC restart; operator must reconcile",
    ),
```

`config/config_model.py`: add `from typing import Literal` (extend the existing typing import) and in `Product` after `slot`:

```python
    kind: Literal["ice", "water", "other"] = Field(
        "other",
        description=(
            "Which availability permissives gate this product: ice, water, or "
            "other (gated by every permissive)"
        ),
    )
```

- [ ] **Step 4: Regenerate schemas and fix version references**

Run: `uv run python -m contracts.generate`
Then: `uv run ruff check --fix .` (nothing expected). Grep for the old version:

Run: `grep -rn "0.2.0" contracts docs/contracts tests`
Update every hit that refers to the vending contract version (`CONTRACT.md` header, any test literal) to `0.3.0`.

Add the row to `ROADMAP.md` §5 after `PAY-103`:

```
| `PAY-104` | Transaction uncertain after VMC restart | lockout | Payment inhibited until an operator clears the fault; snapshot in event history |
```

Add to `docs/contracts/vending-machine/CONTRACT.md` fault list (same wording) and mention `cmd/payment/enable` (`PaymentEnableCommand {accept: bool}`, QoS 1, not retained; the VMC republishes on connect and whenever the `mdb` subsystem returns).

- [ ] **Step 5: Run tests**

Run: `uv run pytest tests/test_contracts_vending.py tests/test_contract_schemas.py tests/test_config_model.py -q`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
uv run ruff format .
git add contracts config docs ROADMAP.md tests
git commit -m "feat(contract): PAY-104 transaction-uncertain fault; Product.kind; contract 0.3.0"
```

---

### Task 7: `services/availability.py`

**Files:**
- Create: `services/availability.py`
- Create: `tests/test_availability.py`

**Interfaces:**
- Produces:
  - `PermissiveState`, `Applies`, `Permissive` (dataclass), `LIVENESS_INPUTS`.
  - `Availability(products)` with setters `set_publisher(fn: Callable[[bool], None])`, `set_event_recorder(recorder)`, `set_mqtt_connected(bool)`, `set_subsystem_alive(name, alive)`, `set_payment_device(device, state)`, `set_fsm_state(state)`, `set_active_faults(list[dict])` (VMC `active_faults()` shape), `set_hardware_io(device, state: bool)`, `set_transaction_certain(bool)`, `set_products(products)`.
  - Outputs `sale_available(kind) -> tuple[bool, list[str]]`, `product_sellable(product) -> tuple[bool, list[str]]`, `payment_enabled: bool`, `blocking_reasons() -> list[str]`, `table() -> list[dict]`, `republish() -> None`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_availability.py
"""Permissive truth table and payment/enable publishing."""

from config.config_model import Product
from services.availability import Availability, PermissiveState


class Recorder:
    def __init__(self):
        self.events = []

    def record(self, event_type, value=1.0, metadata=None):
        self.events.append((event_type, value, metadata))


def _all_good(avail: Availability) -> None:
    avail.set_mqtt_connected(True)
    avail.set_subsystem_alive("vending", True)
    avail.set_subsystem_alive("mdb", True)
    avail.set_subsystem_alive("ice_maker", True)
    avail.set_payment_device("coin_acceptor", "ready")
    avail.set_fsm_state("idle")
    avail.set_hardware_io("bin_half_full", True)


def _avail(products=None):
    products = products or [Product(sku="ICE-1", name="Ice", kind="ice"), Product(sku="WTR-1", name="Water", kind="water")]
    published: list[bool] = []
    a = Availability(products)
    a.set_publisher(published.append)
    return a, published


def test_everything_unknown_at_start_blocks_payment_and_publishes_false():
    a, published = _avail()
    assert a.payment_enabled is False
    assert published == [False]
    ok, failing = a.sale_available("ice")
    assert ok is False
    assert "vending_alive" in failing


def test_all_known_inputs_pass_enables_and_publishes_once():
    a, published = _avail()
    _all_good(a)
    assert a.payment_enabled is True
    assert published == [False, True]
    # repeating a setter with the same value publishes nothing new
    a.set_fsm_state("idle")
    assert published == [False, True]


def test_not_instrumented_rows_pass_and_are_labelled():
    a, _ = _avail()
    rows = {r["name"]: r for r in a.table()}
    assert rows["bag_present"]["instrumented"] is False
    assert rows["bag_present"]["state"] == "pass"
    assert rows["bag_present"]["detail"] == "not instrumented"


def test_ice_only_failure_keeps_water_selling():
    a, published = _avail()
    _all_good(a)
    a.set_subsystem_alive("ice_maker", False)
    assert a.sale_available("ice")[0] is False
    assert a.sale_available("water")[0] is True
    assert a.payment_enabled is True
    assert published == [False, True]


def test_vending_loss_disables_everything():
    a, published = _avail()
    _all_good(a)
    a.set_subsystem_alive("vending", False)
    assert a.payment_enabled is False
    assert published == [False, True, False]
    assert a.blocking_reasons() == ["vending_alive"]


def test_lockout_on_every_product_disables_payment():
    a, _ = _avail()
    _all_good(a)
    a.set_active_faults(
        [
            {"key": "ICE-1", "sku": "ICE-1", "code": "ICE-301", "severity": "lockout", "scope": "product"},
            {"key": "WTR-1", "sku": "WTR-1", "code": "WTR-102", "severity": "lockout", "scope": "product"},
        ]
    )
    assert a.payment_enabled is False
    ok, failing = a.product_sellable(Product(sku="ICE-1", kind="ice"))
    assert ok is False and "lockout:ICE-301" in failing


def test_ice_101_fault_and_bin_empty_fail_ice_available():
    a, _ = _avail()
    _all_good(a)
    a.set_active_faults([{"key": "ICE-1", "sku": "ICE-1", "code": "ICE-101", "severity": "product_unavailable", "scope": "product"}])
    assert "ice_available" in a.sale_available("ice")[1]
    a.set_active_faults([])
    assert a.sale_available("ice")[0] is True
    a.set_hardware_io("bin_half_full", False)
    assert "ice_available" in a.sale_available("ice")[1]


def test_machine_critical_fault_blocks_all():
    a, _ = _avail()
    _all_good(a)
    a.set_active_faults([{"key": "WTR-104", "sku": None, "code": "WTR-104", "severity": "critical", "scope": "machine"}])
    assert a.payment_enabled is False
    assert "no_critical_fault" in a.blocking_reasons()


def test_service_door_open_blocks_and_closing_restores():
    a, _ = _avail()
    _all_good(a)
    a.set_hardware_io("service_door", True)
    assert a.payment_enabled is False
    a.set_hardware_io("service_door", False)
    assert a.payment_enabled is True


def test_payment_device_error_blocks():
    a, _ = _avail()
    _all_good(a)
    a.set_payment_device("card_reader", "error")
    assert "payment_devices_ready" in a.blocking_reasons()
    a.set_payment_device("card_reader", "ready")
    assert a.payment_enabled is True


def test_fsm_error_and_uncertain_transaction_block():
    a, _ = _avail()
    _all_good(a)
    a.set_fsm_state("error")
    assert a.payment_enabled is False
    a.set_fsm_state("idle")
    a.set_transaction_certain(False)
    assert "transaction_certain" in a.blocking_reasons()


def test_other_kind_needs_every_permissive():
    a, _ = _avail([Product(sku="X", kind="other")])
    _all_good(a)
    a.set_subsystem_alive("ice_maker", False)
    assert a.payment_enabled is False


def test_no_products_never_enables():
    a, published = _avail([])
    _all_good(a)
    assert a.payment_enabled is False
    assert a.blocking_reasons() == ["no products"]


def test_republish_sends_current_value_unconditionally():
    a, published = _avail()
    _all_good(a)
    a.republish()
    assert published == [False, True, True]


def test_change_is_recorded():
    a, _ = _avail()
    rec = Recorder()
    a.set_event_recorder(rec)
    _all_good(a)
    assert rec.events[-1][0] == "availability_changed"
    assert rec.events[-1][2]["enabled"] is True


def test_set_products_reevaluates():
    a, published = _avail([Product(sku="ICE-1", kind="ice")])
    _all_good(a)
    a.set_subsystem_alive("ice_maker", False)
    assert a.payment_enabled is False
    a.set_products([Product(sku="ICE-1", kind="ice"), Product(sku="WTR-1", kind="water")])
    assert a.payment_enabled is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_availability.py -q`
Expected: FAIL (`ModuleNotFoundError`).

- [ ] **Step 3: Implement**

```python
# services/availability.py
"""Availability permissives: decides when the machine may accept money.

ROADMAP.md §3. Inputs are pushed in by the VMC and the health monitor; this
module computes per-kind sale availability and publishes cmd/payment/enable
whenever the overall answer changes. Inputs the hardware cannot report yet are
present as "not instrumented" rows that always pass, so flipping one to
fail-closed later is a one-line change here.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Callable, Optional

from loguru import logger


class PermissiveState(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    UNKNOWN = "unknown"


class Applies(str, Enum):
    ice = "ice"
    water = "water"
    both = "both"


@dataclass
class Permissive:
    name: str
    applies_to: Applies
    instrumented: bool
    state: PermissiveState
    detail: str = ""

    def as_row(self) -> dict:
        return {
            "name": self.name,
            "applies_to": self.applies_to.value,
            "instrumented": self.instrumented,
            "state": self.state.value,
            "detail": self.detail,
        }


# Heartbeat subsystem name -> permissive it drives.
LIVENESS_INPUTS = {
    "vending": "vending_alive",
    "mdb": "payment_alive",
    "ice_maker": "ice_maker_alive",
}

_BLOCKING_SEVERITIES = {"critical", "lockout"}
_BAD_DEVICE_STATES = {"error", "offline"}


def _inst(
    name: str,
    applies: Applies,
    state: PermissiveState = PermissiveState.UNKNOWN,
    detail: str = "",
) -> Permissive:
    return Permissive(name, applies, True, state, detail)


def _stub(name: str, applies: Applies) -> Permissive:
    return Permissive(name, applies, False, PermissiveState.PASS, "not instrumented")


class Availability:
    """Truth table of permissives plus the payment/enable publisher.

    Usage:
        avail = Availability(config.products)
        avail.set_publisher(vmc.publish_payment_enable)   # sync callable(bool)
        avail.set_subsystem_alive("vending", True)         # ... from health monitor
        ok, failing = avail.product_sellable(product)
    """

    def __init__(self, products: list):
        self._products = list(products)
        self._lockouts: dict[str, str] = {}
        self._publish: Optional[Callable[[bool], None]] = None
        self._recorder = None
        self._payment_devices: dict[str, str] = {}
        self._bin_half_full: Optional[bool] = None
        self._ice_101_active = False
        self._last_published: Optional[bool] = None
        rows = [
            _inst("mqtt_connected", Applies.both),
            _inst("vending_alive", Applies.both),
            _inst("payment_alive", Applies.both),
            _inst("payment_devices_ready", Applies.both),
            _inst("ice_maker_alive", Applies.ice),
            _inst("ice_available", Applies.ice, detail="no bin report yet"),
            _inst("fsm_ok", Applies.both),
            _inst("no_critical_fault", Applies.both, PermissiveState.PASS),
            _inst(
                "service_door_closed",
                Applies.both,
                PermissiveState.PASS,
                "assumed closed; no report yet",
            ),
            _inst("transaction_certain", Applies.both, PermissiveState.PASS),
            _stub("bag_present", Applies.ice),
            _stub("trap_door_closed", Applies.ice),
            _stub("control_power_ok", Applies.both),
            _stub("water_pressure_ok", Applies.water),
            _stub("water_treatment_ok", Applies.water),
            _stub("no_leak", Applies.water),
            _stub("water_valve_closed", Applies.water),
        ]
        self._rows: dict[str, Permissive] = {r.name: r for r in rows}

    # --- wiring ---

    def set_publisher(self, publish: Callable[[bool], None]) -> None:
        self._publish = publish
        self._recompute()

    def set_event_recorder(self, recorder) -> None:
        self._recorder = recorder

    # --- inputs ---

    def _set(self, name: str, state: PermissiveState, detail: str = "") -> None:
        row = self._rows[name]
        row.state = state
        row.detail = detail
        self._recompute()

    def _set_bool(self, name: str, ok: bool, fail_detail: str = "") -> None:
        self._set(
            name,
            PermissiveState.PASS if ok else PermissiveState.FAIL,
            "" if ok else fail_detail,
        )

    def set_mqtt_connected(self, connected: bool) -> None:
        self._set_bool("mqtt_connected", connected, "broker disconnected")

    def set_subsystem_alive(self, subsystem: str, alive: bool) -> None:
        name = LIVENESS_INPUTS.get(subsystem)
        if name is None:
            return
        self._set_bool(name, alive, f"{subsystem} heartbeat lost")

    def set_payment_device(self, device: str, state: str) -> None:
        self._payment_devices[device] = state
        bad = sorted(d for d, s in self._payment_devices.items() if s in _BAD_DEVICE_STATES)
        self._set_bool("payment_devices_ready", not bad, ", ".join(bad))

    def set_fsm_state(self, state: str) -> None:
        self._set_bool("fsm_ok", state != "error", "VMC in error state")

    def set_active_faults(self, faults: list[dict]) -> None:
        """Consume VMC.active_faults(): lockouts, machine faults, ICE-101."""
        self._lockouts = {
            f["sku"]: f["code"] for f in faults if f.get("scope") == "product" and f.get("sku")
        }
        self._ice_101_active = any(f.get("code") == "ICE-101" for f in faults)
        blocking = sorted(
            f["code"]
            for f in faults
            if f.get("scope") == "machine" and f.get("severity") in _BLOCKING_SEVERITIES
        )
        self._rows["no_critical_fault"].state = (
            PermissiveState.FAIL if blocking else PermissiveState.PASS
        )
        self._rows["no_critical_fault"].detail = ", ".join(blocking)
        self._refresh_ice_available()
        self._recompute()

    def set_hardware_io(self, device: str, state: bool) -> None:
        if device == "bin_half_full":
            self._bin_half_full = state
            self._refresh_ice_available()
            self._recompute()
        elif device == "service_door":
            self._set_bool("service_door_closed", not state, "service door open")

    def set_transaction_certain(self, certain: bool) -> None:
        self._set_bool("transaction_certain", certain, "PAY-104 active")

    def set_products(self, products: list) -> None:
        self._products = list(products)
        self._recompute()

    def _refresh_ice_available(self) -> None:
        row = self._rows["ice_available"]
        if self._ice_101_active:
            row.state, row.detail = PermissiveState.FAIL, "ICE-101 active"
        elif self._bin_half_full is None:
            row.state, row.detail = PermissiveState.UNKNOWN, "no bin report yet"
        elif self._bin_half_full:
            row.state, row.detail = PermissiveState.PASS, ""
        else:
            row.state, row.detail = PermissiveState.FAIL, "bin empty"

    # --- outputs ---

    def _rows_for(self, kind: str) -> list[Permissive]:
        if kind in ("ice", "water"):
            return [
                r
                for r in self._rows.values()
                if r.applies_to in (Applies.both, Applies(kind))
            ]
        return list(self._rows.values())

    def sale_available(self, kind: str) -> tuple[bool, list[str]]:
        failing = [r.name for r in self._rows_for(kind) if r.state is not PermissiveState.PASS]
        return (not failing, failing)

    def product_sellable(self, product) -> tuple[bool, list[str]]:
        ok, failing = self.sale_available(getattr(product, "kind", "other"))
        code = self._lockouts.get(product.sku)
        if code:
            failing = failing + [f"lockout:{code}"]
            ok = False
        return ok, failing

    @property
    def payment_enabled(self) -> bool:
        return any(self.product_sellable(p)[0] for p in self._products)

    def blocking_reasons(self) -> list[str]:
        """Why payment is off: the shortest failing list across products."""
        if self.payment_enabled:
            return []
        if not self._products:
            return ["no products"]
        return min((self.product_sellable(p)[1] for p in self._products), key=len)

    def table(self) -> list[dict]:
        rows = sorted(self._rows.values(), key=lambda r: (not r.instrumented, r.name))
        return [r.as_row() for r in rows]

    def republish(self) -> None:
        """Send the current value even if unchanged (MQTT reconnect, gateway back)."""
        if self._publish is None:
            return
        enabled = self.payment_enabled
        self._last_published = enabled
        self._publish(enabled)

    def _recompute(self) -> None:
        enabled = self.payment_enabled
        if enabled == self._last_published:
            return
        self._last_published = enabled
        reasons = self.blocking_reasons()
        logger.info(
            f"Availability: payment {'ENABLED' if enabled else 'DISABLED'}"
            + (f" ({', '.join(reasons)})" if reasons else "")
        )
        if self._recorder:
            self._recorder.record(
                "availability_changed",
                value=1.0 if enabled else 0.0,
                metadata={"enabled": enabled, "failing": reasons},
            )
        if self._publish is not None:
            self._publish(enabled)
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_availability.py -v`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
uv run ruff check --fix .
uv run ruff format .
git add services/availability.py tests/test_availability.py
git commit -m "feat(availability): permissive truth table publishing payment/enable on change"
```

---

### Task 8: HealthMonitor liveness callback

**Files:**
- Modify: `services/health_monitor.py:22-45, 108-130, 176-186, 332-353`
- Test: `tests/test_health_monitor.py`

**Interfaces:**
- Produces: `HealthMonitor.set_liveness_callback(cb: Callable[[str, bool], None])`; fires `(subsystem, True)` on first heartbeat and on recovery, `(subsystem, False)` on LWT and on first staleness. Exactly once per transition.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_health_monitor.py`:

```python
class TestLivenessCallback:
    def test_first_heartbeat_reports_alive_once(self):
        m = HealthMonitor()
        seen = []
        m.set_liveness_callback(lambda name, alive: seen.append((name, alive)))
        m.record_heartbeat("vending", {"uptime_seconds": 1})
        m.record_heartbeat("vending", {"uptime_seconds": 2})
        assert seen == [("vending", True)]

    def test_lwt_reports_down_then_heartbeat_reports_up(self):
        m = HealthMonitor()
        seen = []
        m.set_liveness_callback(lambda name, alive: seen.append((name, alive)))
        m.record_heartbeat("mdb")
        m.mark_offline("mdb")
        m.mark_offline("mdb")
        m.record_heartbeat("mdb")
        assert seen == [("mdb", True), ("mdb", False), ("mdb", True)]

    async def test_stale_reports_down_once(self):
        m = HealthMonitor(subsystem_timeout=0.01)
        seen = []
        m.set_liveness_callback(lambda name, alive: seen.append((name, alive)))
        m.record_heartbeat("ice_maker")
        m._subsystems["ice_maker"].last_seen -= 1.0
        await m._check()
        await m._check()
        assert seen == [("ice_maker", True), ("ice_maker", False)]

    def test_callback_exception_is_swallowed(self):
        m = HealthMonitor()

        def boom(name, alive):
            raise RuntimeError("x")

        m.set_liveness_callback(boom)
        m.record_heartbeat("vending")  # must not raise
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_health_monitor.py::TestLivenessCallback -v`
Expected: FAIL (`AttributeError: set_liveness_callback`).

- [ ] **Step 3: Implement**

In `services/health_monitor.py`:

Add to `SubsystemStatus`:

```python
    liveness_reported: Optional[bool] = None  # last value sent to the liveness callback
```

Add type alias after `AlertCallback`:

```python
LivenessCallback = Callable[[str, bool], None]
```

In `HealthMonitor.__init__` add `self._liveness_callback: Optional[LivenessCallback] = None`, and:

```python
    def set_liveness_callback(self, callback: LivenessCallback):
        """Register a sync callback(subsystem, alive) fired once per transition:
        first heartbeat / recovery -> True, Last-Will / first staleness -> False."""
        self._liveness_callback = callback

    def _notify_liveness(self, sub: SubsystemStatus, alive: bool) -> None:
        if sub.liveness_reported == alive:
            return
        sub.liveness_reported = alive
        if self._liveness_callback is None:
            return
        try:
            self._liveness_callback(sub.name, alive)
        except Exception as e:
            logger.error(f"Health: liveness callback failed for {sub.name}: {e}")
```

In `record_heartbeat`, after `self._fired_alerts.discard(...)`:

```python
        self._notify_liveness(self._subsystems[subsystem], True)
```

In `mark_offline`, at the end:

```python
        self._notify_liveness(self._subsystems[subsystem], False)
```

In `_check`, inside the `if sub.is_stale(...)` block, before `_fire_alert`:

```python
                self._notify_liveness(sub, False)
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_health_monitor.py -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
uv run ruff check --fix .
uv run ruff format .
git add services/health_monitor.py tests/test_health_monitor.py
git commit -m "feat(health): liveness callback fires once per subsystem transition"
```

---

### Task 9: VMC wiring: availability, COM/PAY-101 faults, payment gate

**Files:**
- Modify: `controller/vmc.py` (imports, `__init__`, `set_mqtt_client`, `set_health_monitor`, new `set_availability`, `on_mqtt_connection`, `_publish_status`, `_push_active_faults`, `_handle_mqtt_hardware_io`, new `_handle_mqtt_payment_status`, `select_product`, `deposit_funds`), `main.py:158-205`
- Test: `tests/test_vmc_fsm.py` (or new class in `tests/test_vmc_flows.py`), `tests/test_mqtt.py`

**Interfaces:**
- Consumes: `Availability` (Task 7), `HealthMonitor.set_liveness_callback` (Task 8), `FaultCode.COM_101/COM_102/COM_103/PAY_101`.
- Produces: `VMC.set_availability(avail)`, `VMC.on_mqtt_connection(connected: bool)`, `VMC.publish_payment_enable(accept: bool)`, `VMC._on_subsystem_liveness(subsystem, alive)`, MQTT handler for `payment/status`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_vmc_flows.py`:

```python
from services.availability import Availability
from services.mqtt_messages import PaymentEnableCommand


def _wired_vmc(products=None):
    cfg = ConfigModel()
    cfg.physical.products = products or [
        Product(sku="ICE-1", name="Ice Bag", price=2.5, kind="ice"),
        Product(sku="WTR-1", name="Water", price=1.0, kind="water"),
    ]
    vmc = VMC(config=cfg)
    vmc.attach_to_loop(asyncio.get_running_loop())
    monitor = HealthMonitor()
    vmc.set_health_monitor(monitor)
    avail = Availability(cfg.products)
    vmc.set_availability(avail)
    published: list = []

    class FakeMQTT:
        def register(self, *a, **k):
            pass

        async def publish(self, topic, payload, qos=1, retain=False):
            published.append((topic, payload))

    vmc.set_mqtt_client(FakeMQTT())
    return vmc, monitor, avail, published


def _all_alive(monitor: HealthMonitor, vmc: VMC):
    for name in ("vending", "mdb", "ice_maker"):
        monitor.record_heartbeat(name, {"uptime_seconds": 1})
    vmc.on_mqtt_connection(True)


async def _enables(published) -> list[bool]:
    await asyncio.sleep(0)
    return [p.accept for t, p in published if t == "cmd/payment/enable" and isinstance(p, PaymentEnableCommand)]


async def test_vending_heartbeat_loss_raises_com_101_and_disables_payment():
    vmc, monitor, avail, published = _wired_vmc()
    _all_alive(monitor, vmc)
    avail.set_payment_device("coin_acceptor", "ready")
    await vmc._handle_mqtt_hardware_io("hardware/io/bin_half_full", {"device": "bin_half_full", "state": True})
    assert avail.payment_enabled is True

    monitor.mark_offline("vending")
    codes = {f["code"] for f in vmc.active_faults()}
    assert "COM-101" in codes
    assert avail.payment_enabled is False
    assert (await _enables(published))[-1] is False

    monitor.record_heartbeat("vending", {"uptime_seconds": 5})
    assert "COM-101" not in {f["code"] for f in vmc.active_faults()}
    assert avail.payment_enabled is True
    vmc.cancel_pending_tasks()


async def test_ice_maker_loss_is_com_102_and_only_ice_blocked():
    vmc, monitor, avail, published = _wired_vmc()
    _all_alive(monitor, vmc)
    avail.set_payment_device("coin_acceptor", "ready")
    await vmc._handle_mqtt_hardware_io("hardware/io/bin_half_full", {"device": "bin_half_full", "state": True})
    monitor.mark_offline("ice_maker")
    assert "COM-102" in {f["code"] for f in vmc.active_faults()}
    assert avail.sale_available("ice")[0] is False
    assert avail.payment_enabled is True
    vmc.cancel_pending_tasks()


async def test_mdb_loss_is_pay_101():
    vmc, monitor, avail, _ = _wired_vmc()
    _all_alive(monitor, vmc)
    monitor.mark_offline("mdb")
    assert "PAY-101" in {f["code"] for f in vmc.active_faults()}
    assert avail.payment_enabled is False
    vmc.cancel_pending_tasks()


async def test_mqtt_disconnect_is_com_103_and_reconnect_republishes():
    vmc, monitor, avail, published = _wired_vmc()
    _all_alive(monitor, vmc)
    vmc.on_mqtt_connection(False)
    assert "COM-103" in {f["code"] for f in vmc.active_faults()}
    before = len(await _enables(published))
    vmc.on_mqtt_connection(True)
    assert "COM-103" not in {f["code"] for f in vmc.active_faults()}
    assert len(await _enables(published)) == before + 1
    vmc.cancel_pending_tasks()


async def test_payment_status_error_feeds_availability():
    vmc, monitor, avail, _ = _wired_vmc()
    _all_alive(monitor, vmc)
    await vmc._handle_mqtt_payment_status("payment/status", {"device": "card_reader", "state": "error"})
    assert "payment_devices_ready" in avail.blocking_reasons()
    vmc.cancel_pending_tasks()


async def test_select_product_refused_when_kind_unavailable_names_reason():
    vmc, monitor, avail, _ = _wired_vmc()
    _all_alive(monitor, vmc)
    avail.set_payment_device("coin_acceptor", "ready")
    monitor.mark_offline("ice_maker")
    messages = []
    vmc.set_message_callback(messages.append)
    vmc.select_product(0)  # ICE-1
    assert vmc.state == "idle"
    assert vmc.selected_product is None
    assert "ice_maker_alive" in messages[-1]
    vmc.cancel_pending_tasks()


async def test_deposit_while_disabled_is_escrowed_and_logged():
    vmc, monitor, avail, _ = _wired_vmc()
    vmc.deposit_funds(1.0, payment_method="cash_coin")
    assert vmc.credit_escrow == 1.0
    vmc.cancel_pending_tasks()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_vmc_flows.py -k "com_10 or pay_101 or payment_status or kind_unavailable or deposit_while" -v`
Expected: FAIL (`AttributeError: set_availability`).

- [ ] **Step 3: Implement in `controller/vmc.py`**

Imports: add `PaymentEnableCommand`, `PaymentStatus` to the `services.mqtt_messages` import; add `from services.availability import Availability`.

Module-level map after `_SEVERITY_LEVEL`:

```python
# Heartbeat loss per subsystem -> registry fault (ROADMAP §5, §8).
_LIVENESS_FAULTS = {
    "vending": FaultCode.COM_101,
    "ice_maker": FaultCode.COM_102,
    "mdb": FaultCode.PAY_101,
}
```

In `__init__` next to the other service refs:

```python
        self._availability: Availability | None = None  # Set via set_availability()
```

Replace `set_health_monitor`:

```python
    def set_health_monitor(self, monitor: HealthMonitor):
        """Attach a HealthMonitor; its liveness transitions become COM/PAY faults."""
        self._health_monitor = monitor
        monitor.set_liveness_callback(self._on_subsystem_liveness)
        logger.debug("VMC attached health monitor.")

    def set_availability(self, availability: Availability):
        """Attach the permissive table; it publishes cmd/payment/enable through us."""
        self._availability = availability
        availability.set_fsm_state(self.state)
        availability.set_active_faults(self.active_faults())
        availability.set_publisher(self.publish_payment_enable)
        logger.debug("VMC attached availability.")

    def publish_payment_enable(self, accept: bool) -> None:
        """Sync publisher handed to Availability (fire-and-forget on the loop)."""
        if self._mqtt_client is None:
            logger.warning("No MQTT client; payment/enable not sent")
            return
        self._fire_and_forget(
            self._mqtt_client.publish(
                "cmd/payment/enable", PaymentEnableCommand(accept=accept)
            )
        )

    def _on_subsystem_liveness(self, subsystem: str, alive: bool) -> None:
        code = _LIVENESS_FAULTS.get(subsystem)
        if code is not None:
            if alive:
                self.clear_fault(code.value, by="auto")
            else:
                self._raise_fault(code, outcome="heartbeat_lost")
        if self._availability:
            self._availability.set_subsystem_alive(subsystem, alive)
            if subsystem == "mdb" and alive:
                self._availability.republish()

    def on_mqtt_connection(self, connected: bool) -> None:
        """Connection-state callback from MQTTClient (chained after the health monitor)."""
        if self._availability:
            self._availability.set_mqtt_connected(connected)
        if connected:
            self.clear_fault(FaultCode.COM_103.value, by="auto")
            if self._availability:
                self._availability.republish()
        else:
            self._raise_fault(FaultCode.COM_103, outcome="disconnected")
```

In `set_mqtt_client` add:

```python
        client.register("payment/status", self._handle_mqtt_payment_status)
```

Add the handler next to `_handle_mqtt_payment`:

```python
    async def _handle_mqtt_payment_status(self, topic: str, data: dict):
        """MDB device readiness; any device in error/offline blocks payment."""
        status = PaymentStatus.model_validate(data)
        logger.debug(f"MQTT payment status: {status.device}={status.state}")
        if self._availability:
            self._availability.set_payment_device(status.device, status.state)
```

In `_publish_status`, after the health-monitor update:

```python
        if self._availability:
            self._availability.set_fsm_state(self.state)
```

Move the `if self._availability` block above the early `return` guard so state changes reach Availability even without MQTT (restructure: compute `state` updates first, then publish if client present).

In `_push_active_faults`:

```python
    def _push_active_faults(self) -> None:
        faults = self.active_faults()
        if self._health_monitor:
            self._health_monitor.set_active_faults(faults)
        if self._availability:
            self._availability.set_active_faults(faults)
```

In `_handle_mqtt_hardware_io`, after validation and before the ICE-101 branch:

```python
        if self._availability:
            self._availability.set_hardware_io(hw.device, hw.state)
```

In `select_product`, after the lockout check and before `self.selected_product = candidate`:

```python
        if self._availability:
            sellable, failing = self._availability.product_sellable(candidate)
            if not sellable:
                reason = failing[0] if failing else "unavailable"
                txn_log.info(
                    f"UNAVAILABLE: '{candidate.name}' blocked by {reason}, customer rejected"
                )
                self.send_customer_message(
                    f"{candidate.name} is unavailable right now ({reason}). "
                    "Please try again later."
                )
                return
```

In `deposit_funds`, after the non-positive guard:

```python
        if self._availability and not self._availability.payment_enabled:
            logger.warning(
                f"Credit ${amount:.2f} arrived while payment is disabled "
                f"({', '.join(self._availability.blocking_reasons())}); escrowed"
            )
```

Note `clear_fault` returns False for a code that is not active; the auto-clear calls above rely on that being harmless.

- [ ] **Step 4: Wire `main.py`**

After `routes.set_health_monitor(health)`:

```python
    from services.availability import Availability  # move to top-level imports

    availability = Availability(live_config.products)
    vmc.set_availability(availability)
    routes.set_availability(availability)
```

(`routes.set_availability` is added in Task 13; add the setter now as a plain global setter in `web_interface/routes.py` next to `set_event_recorder`:)

```python
availability = None


def set_availability(avail):
    global availability
    availability = avail
```

Replace the connection callback line:

```python
    def _on_mqtt_connection(connected: bool) -> None:
        health.update_mqtt_status(connected)
        vmc.on_mqtt_connection(connected)

    mqtt.set_connection_callback(_on_mqtt_connection)
```

After the event recorder is created: `availability.set_event_recorder(recorder)`.

Catalog edits: in `web_interface/routes.py` the add/update/delete product routes mutate `config.products`; after each successful mutation add `if availability: availability.set_products(config.products)`.

- [ ] **Step 5: Run tests**

Run: `uv run pytest tests/test_vmc_flows.py tests/test_vmc_fsm.py tests/test_mqtt.py tests/test_main_supervise.py tests/test_web_routes.py -q`
Expected: all pass. `test_set_mqtt_client_registers_handlers` in `tests/test_mqtt.py` may count handlers; update the expected count/topics to include `payment/status`.

- [ ] **Step 6: Commit**

```bash
uv run ruff check --fix .
uv run ruff format .
git add controller/vmc.py main.py web_interface/routes.py tests/
git commit -m "feat(vmc): heartbeat loss raises COM-101/102/103 and PAY-101; payment/enable driven by availability"
```

---

### Task 10: `services/session_store.py`

**Files:**
- Create: `services/session_store.py`
- Create: `tests/test_session_store.py`

**Interfaces:**
- Produces: `SessionSnapshot` dataclass (`state, credit_escrow, selected_sku, dispense_slot, dispense_started_at, pending_refund_request_id, saved_at, error`), `SessionSnapshot.is_open() -> bool`, `SessionStore(path)` with `save(snap)`, `load() -> SessionSnapshot | None`, `clear()`, `async save_async(snap)`, `async clear_async()`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_session_store.py
import json
from dataclasses import asdict

from services.session_store import SessionSnapshot, SessionStore


def test_round_trip(tmp_path):
    store = SessionStore(tmp_path / "session.json")
    snap = SessionSnapshot(state="dispensing", credit_escrow=0.0, selected_sku="ICE-1", dispense_slot=2, dispense_started_at=123.0)
    store.save(snap)
    loaded = store.load()
    assert loaded == snap
    assert not (tmp_path / "session.json.tmp").exists()


def test_load_missing_returns_none(tmp_path):
    assert SessionStore(tmp_path / "session.json").load() is None


def test_clear_removes_file_and_is_idempotent(tmp_path):
    store = SessionStore(tmp_path / "session.json")
    store.save(SessionSnapshot(state="idle", credit_escrow=1.0))
    store.clear()
    store.clear()
    assert store.load() is None


def test_corrupt_file_is_an_open_session_with_error(tmp_path):
    p = tmp_path / "session.json"
    p.write_text("{not json", encoding="utf-8")
    snap = SessionStore(p).load()
    assert snap is not None
    assert snap.error
    assert snap.is_open() is True


def test_is_open_rules():
    assert SessionSnapshot(state="idle", credit_escrow=0.0).is_open() is False
    assert SessionSnapshot(state="interacting_with_user", credit_escrow=0.25).is_open() is True
    assert SessionSnapshot(state="dispensing", credit_escrow=0.0).is_open() is True
    assert SessionSnapshot(state="idle", credit_escrow=0.0, pending_refund_request_id="abc").is_open() is True


async def test_save_async_writes_in_order(tmp_path):
    store = SessionStore(tmp_path / "session.json")
    await store.save_async(SessionSnapshot(state="idle", credit_escrow=1.0))
    await store.save_async(SessionSnapshot(state="idle", credit_escrow=2.0))
    assert json.loads((tmp_path / "session.json").read_text())["credit_escrow"] == 2.0
    await store.clear_async()
    assert store.load() is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_session_store.py -q`
Expected: FAIL (`ModuleNotFoundError`).

- [ ] **Step 3: Implement**

```python
# services/session_store.py
"""Persist the live customer session so a VMC restart cannot silently lose credit.

The snapshot is evidence for the owner (PAY-104), not state to resume: the
FSM always boots idle. Writes are atomic (tmp + fsync + replace) and run on a
single worker thread so they land in submission order without blocking the
event loop.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

from loguru import logger

from services.paths import DATA_DIR

SESSION_PATH = DATA_DIR / "session.json"


@dataclass
class SessionSnapshot:
    state: str
    credit_escrow: float
    selected_sku: Optional[str] = None
    dispense_slot: Optional[int] = None
    dispense_started_at: Optional[float] = None
    pending_refund_request_id: Optional[str] = None
    saved_at: float = field(default_factory=time.time)
    error: Optional[str] = None  # set when the file could not be parsed

    def is_open(self) -> bool:
        """True when money or a vend was in flight (or we cannot tell)."""
        return (
            bool(self.error)
            or self.credit_escrow > 0
            or self.state == "dispensing"
            or self.pending_refund_request_id is not None
        )


class SessionStore:
    def __init__(self, path: Path = SESSION_PATH):
        self._path = Path(path)
        self._executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="session-store"
        )

    @property
    def path(self) -> Path:
        return self._path

    def save(self, snap: SessionSnapshot) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_name(self._path.name + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(asdict(snap), f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self._path)

    def load(self) -> Optional[SessionSnapshot]:
        if not self._path.exists():
            return None
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            return SessionSnapshot(**raw)
        except Exception as e:
            logger.error(f"SessionStore: unreadable {self._path}: {e}")
            return SessionSnapshot(state="unknown", credit_escrow=0.0, error=str(e))

    def clear(self) -> None:
        try:
            self._path.unlink(missing_ok=True)
        except OSError as e:
            logger.error(f"SessionStore: could not remove {self._path}: {e}")

    async def save_async(self, snap: SessionSnapshot) -> None:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(self._executor, self.save, snap)

    async def clear_async(self) -> None:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(self._executor, self.clear)
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_session_store.py -v`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
uv run ruff check --fix .
uv run ruff format .
git add services/session_store.py tests/test_session_store.py
git commit -m "feat(session): atomic session snapshot store"
```

---

### Task 11: VMC session persistence and PAY-104 on boot

**Files:**
- Modify: `controller/vmc.py` (`__init__`, new `set_session_store`, `_snapshot`, `_persist_session`, `_flag_uncertain_session`, `reconcile_session`, `clear_fault`, `_publish_status`, `_process_payment`, `_finish_dispensing`, `_refund_confirmed`, `on_dispense_product`), `main.py`
- Test: `tests/test_vmc_flows.py`

**Interfaces:**
- Consumes: `SessionStore`, `SessionSnapshot` (Task 10); `Availability.set_transaction_certain` (Task 7); `FaultCode.PAY_104` (Task 6).
- Produces: `VMC.set_session_store(store)` (call after `attach_to_loop`, `set_health_monitor`, `set_availability`), `VMC.reconcile_session() -> None` stub.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_vmc_flows.py`:

```python
from services.session_store import SessionSnapshot, SessionStore


def _boot_with(tmp_path, snap):
    store = SessionStore(tmp_path / "session.json")
    if snap is not None:
        store.save(snap)
    vmc, monitor, avail, published = _wired_vmc()
    vmc.set_session_store(store)
    return vmc, avail, store


async def test_clean_boot_raises_nothing(tmp_path):
    vmc, avail, _ = _boot_with(tmp_path, None)
    assert vmc.active_faults() == []
    vmc.cancel_pending_tasks()


async def test_boot_with_escrow_raises_pay_104_and_blocks(tmp_path):
    rec = FakeEventRecorder()
    vmc, avail, store = _boot_with(tmp_path, None)
    vmc.set_event_recorder(rec)
    store.save(SessionSnapshot(state="interacting_with_user", credit_escrow=1.25))
    vmc.set_session_store(store)
    assert "PAY-104" in {f["code"] for f in vmc.active_faults()}
    assert "transaction_certain" in avail.blocking_reasons() or avail.payment_enabled is False
    assert any(e[0] == "session_uncertain" and e[2]["credit_escrow"] == 1.25 for e in rec.events)
    assert store.load() is not None  # kept as evidence until cleared
    vmc.cancel_pending_tasks()


async def test_boot_mid_dispense_raises_pay_104(tmp_path):
    vmc, avail, _ = _boot_with(tmp_path, SessionSnapshot(state="dispensing", credit_escrow=0.0, selected_sku="ICE-1", dispense_slot=0))
    assert "PAY-104" in {f["code"] for f in vmc.active_faults()}
    vmc.cancel_pending_tasks()


async def test_boot_with_corrupt_file_raises_pay_104(tmp_path):
    (tmp_path / "session.json").write_text("garbage", encoding="utf-8")
    vmc, avail, _ = _boot_with(tmp_path, None)
    assert "PAY-104" in {f["code"] for f in vmc.active_faults()}
    vmc.cancel_pending_tasks()


async def test_clearing_pay_104_removes_file_and_reenables(tmp_path):
    vmc, avail, store = _boot_with(tmp_path, SessionSnapshot(state="interacting_with_user", credit_escrow=1.0))
    assert vmc.clear_fault("PAY-104", by="admin") is True
    await asyncio.sleep(0.05)
    assert store.load() is None
    assert "transaction_certain" not in avail.blocking_reasons()
    vmc.cancel_pending_tasks()


async def test_session_file_written_during_sale_and_cleared_after(tmp_path):
    store = SessionStore(tmp_path / "session.json")
    vmc, monitor, avail, published = _wired_vmc()
    vmc.set_session_store(store)
    _all_alive(monitor, vmc)
    avail.set_payment_device("coin_acceptor", "ready")
    await vmc._handle_mqtt_hardware_io("hardware/io/bin_half_full", {"device": "bin_half_full", "state": True})

    vmc.deposit_funds(2.5, payment_method="cash_bill")
    await asyncio.sleep(0.05)
    snap = store.load()
    assert snap is not None and snap.credit_escrow == 2.5

    vmc.select_product(0)
    await asyncio.sleep(1.2)  # _process_payment runs after 1s
    assert vmc.state == "dispensing"
    await asyncio.sleep(0.05)
    snap = store.load()
    assert snap.state == "dispensing" and snap.dispense_slot == 0

    await vmc._handle_mqtt_dispenser("hardware/dispenser", {"slot": 0, "state": "complete"})
    await asyncio.sleep(0.05)
    assert vmc.state == "idle"
    assert store.load() is None
    vmc.cancel_pending_tasks()


def test_reconcile_session_is_a_documented_stub():
    vmc = make_vmc()
    assert vmc.reconcile_session() is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_vmc_flows.py -k "boot or pay_104 or session_file or reconcile" -v`
Expected: FAIL (`AttributeError: set_session_store`).

- [ ] **Step 3: Implement in `controller/vmc.py`**

Imports: `from dataclasses import asdict, dataclass` and `from services.session_store import SessionSnapshot, SessionStore`.

In `__init__`: `self._session_store: SessionStore | None = None`.

Add methods after `set_event_recorder`:

```python
    def set_session_store(self, store: SessionStore):
        """Attach the session store and evaluate any snapshot left by a previous run.

        Call after attach_to_loop, set_health_monitor and set_availability so
        the PAY-104 alert and the availability gate both land.
        """
        self._session_store = store
        snap = store.load()
        if snap is not None and snap.is_open():
            self._flag_uncertain_session(snap)
        elif snap is not None:
            store.clear()
        logger.debug("VMC attached session store.")

    def _flag_uncertain_session(self, snap: SessionSnapshot) -> None:
        detail = snap.error or (
            f"state={snap.state} escrow=${snap.credit_escrow:.2f} "
            f"sku={snap.selected_sku} refund={snap.pending_refund_request_id}"
        )
        logger.error(f"Transaction uncertain after restart: {detail}")
        txn_log.error(f"RESTART WITH OPEN SESSION: {detail}")
        if self._event_recorder:
            self._event_recorder.record(
                "session_uncertain", value=snap.credit_escrow, metadata=asdict(snap)
            )
        if self._availability:
            self._availability.set_transaction_certain(False)
        self._raise_fault(FaultCode.PAY_104, outcome=detail)

    def reconcile_session(self) -> None:
        """Future hook: query the payment gateway for held credit and clear
        PAY-104 automatically. The contract has no credit query yet, so the
        operator clears the fault from the dashboard after checking the machine.
        """
        return None

    def _snapshot(self, state: str | None = None) -> SessionSnapshot:
        pending = next(iter(self._pending_refunds), None)
        product = self.selected_product
        return SessionSnapshot(
            state=state or self.state,
            credit_escrow=round(self.credit_escrow, 2),
            selected_sku=product.sku if product else None,
            dispense_slot=product.slot if product and (state or self.state) == "dispensing" else None,
            dispense_started_at=time.time() if (state or self.state) == "dispensing" else None,
            pending_refund_request_id=pending,
        )

    def _persist_session(self, state: str | None = None) -> None:
        """Save the live session, or remove the file once nothing is in flight."""
        if self._session_store is None:
            return
        if FaultCode.PAY_104 in self._machine_faults:
            return  # keep the evidence file untouched until the operator clears it
        snap = self._snapshot(state)
        if snap.is_open():
            self._fire_and_forget(self._session_store.save_async(snap))
        else:
            self._fire_and_forget(self._session_store.clear_async())
```

Hook it in:

- `_publish_status`: call `self._persist_session()` before the MQTT guard (so it runs without a client).
- `_process_payment`: after `self.dispense_product()` add `self._persist_session("dispensing")`.
- `_finish_dispensing`: after `self.complete_transaction()` add `self._persist_session()`.
- `_refund_confirmed`: after popping the pending refund add `self._persist_session()`.
- `_refund_attempt_failed` final branch: after popping add `self._persist_session()`.
- `clear_fault`, machine branch, after `del self._machine_faults[code]`:

```python
            if code is FaultCode.PAY_104:
                if self._session_store:
                    self._session_store.clear()
                if self._availability:
                    self._availability.set_transaction_certain(True)
```

- [ ] **Step 4: Wire `main.py`**

After `vmc.set_availability(availability)` and the MQTT/health wiring (must be after `set_health_monitor`):

```python
    from services.session_store import SessionStore  # top-level import

    vmc.set_session_store(SessionStore())
    logger.info("Session store attached; previous open session checked")
```

Place this after `vmc.set_event_recorder(recorder)` so the `session_uncertain` event is recorded.

- [ ] **Step 5: Run tests**

Run: `uv run pytest tests/test_vmc_flows.py tests/test_vmc_fsm.py tests/test_main_supervise.py -q`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
uv run ruff check --fix .
uv run ruff format .
git add controller/vmc.py main.py tests/test_vmc_flows.py
git commit -m "feat(vmc): persist the live session; PAY-104 holds payment after a restart mid-sale"
```

---

### Task 12: MDB simulator honours `cmd/payment/enable`

**Files:**
- Modify: `simulators/mdb_gateway.py` (`__init__`, `run_simulation`, new `_enable_loop`, `_do_cash_payment`, `_do_card_payment`, `_payment_loop`)
- Test: `tests/test_simulator_mdb.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_simulator_mdb.py`:

```python
class TestPaymentEnable:
    def test_starts_inhibited_until_enabled(self):
        sim = MDBGatewaySimulator()
        assert sim.accepting is False

    async def test_enable_command_toggles_accepting(self):
        sim = MDBGatewaySimulator()
        await sim._apply_enable({"accept": True})
        assert sim.accepting is True
        await sim._apply_enable({"accept": False})
        assert sim.accepting is False

    async def test_bad_enable_payload_ignored(self):
        sim = MDBGatewaySimulator()
        await sim._apply_enable({"nope": 1})
        assert sim.accepting is False

    async def test_no_credit_published_while_inhibited(self):
        sim = MDBGatewaySimulator()
        client = AsyncMock()
        sim.publish = AsyncMock()
        await sim._do_card_payment(client, "card", price=3.0)
        sim.publish.assert_not_awaited()
        await sim._apply_enable({"accept": True})
        await sim._do_card_payment(client, "card", price=3.0)
        sim.publish.assert_awaited()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_simulator_mdb.py::TestPaymentEnable -v`
Expected: FAIL (`AttributeError: accepting`).

- [ ] **Step 3: Implement**

In `simulators/mdb_gateway.py`:

```python
from services.mqtt_messages import PaymentEnableCommand, PaymentEvent, PaymentStatus
```

In `__init__` after `self.strategy = PaymentStrategy()`:

```python
        # Real MDB peripherals stay inhibited until the VMC enables them.
        self.accepting = False
```

Add methods:

```python
    async def _apply_enable(self, data: dict) -> None:
        try:
            cmd = PaymentEnableCommand.model_validate(data)
        except ValidationError as e:
            logger.error(f"[mdb] Bad payment/enable ignored: {e}")
            return
        if cmd.accept != self.accepting:
            logger.info(f"[mdb] Payment {'ENABLED' if cmd.accept else 'INHIBITED'} by VMC")
        self.accepting = cmd.accept

    async def _enable_loop(self, client: aiomqtt.Client):
        """Track cmd/payment/enable from the VMC."""
        topic = f"{self.topic_prefix}/cmd/payment/enable"
        queue = await self.subscribe(client, topic)
        logger.info(f"[mdb] Listening for payment enable on {topic}")
        while True:
            _topic, data = await queue.get()
            await self._apply_enable(data)
```

Guard the two credit publishers:

```python
    async def _do_card_payment(self, client, method, price=3.00):
        if not self.accepting:
            logger.info("[mdb] Payment inhibited; card not accepted")
            return
        ...
```

and at the top of each loop iteration in `_do_cash_payment`:

```python
            if not self.accepting:
                logger.info("[mdb] Payment inhibited; cash rejected")
                return
```

In `_payment_loop`, after `if state != "interacting_with_user": continue` add:

```python
            if not self.accepting:
                logger.debug("[mdb] Interaction seen but payment inhibited; waiting")
                continue
```

Add `tg.create_task(self._enable_loop(client))` wherever `run_simulation` creates its other tasks (`_refund_loop`, `_payment_loop`, `_watch_vmc_status`, `_publish_device_status`).

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_simulator_mdb.py tests/test_simulator_base.py -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
uv run ruff check --fix .
uv run ruff format .
git add simulators/mdb_gateway.py tests/test_simulator_mdb.py
git commit -m "feat(sim): MDB gateway honours cmd/payment/enable"
```

---

### Task 13: Dashboard permissive table, payment line, and `/screen`

**Files:**
- Modify: `web_interface/routes.py` (`_render_status`, `health_summary`, new `/screen`, `/screen/body`), `web_interface/templates/partials/status_fragment.html`, `web_interface/templates/partials/health_fragment.html`
- Create: `web_interface/templates/screen.html`, `web_interface/templates/partials/screen_body.html`
- Test: `tests/test_web_routes.py`

**Interfaces:**
- Consumes: `routes.availability` global (Task 9), `Availability.table()`, `payment_enabled`, `blocking_reasons()`, `sale_available(kind)`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_web_routes.py`:

```python
class TestAvailabilityOnDashboard:
    @pytest.fixture
    def wired(self, client):
        from services.availability import Availability
        from services.health_monitor import HealthMonitor
        from web_interface import routes as r

        avail = Availability(r.config.products)
        r.set_availability(avail)
        r.set_health_monitor(HealthMonitor())
        yield client, avail
        r.set_availability(None)

    def test_status_shows_payment_disabled_with_reason(self, wired):
        client, avail = wired
        resp = client.get("/status")
        assert "Payment" in resp.text
        assert "Disabled" in resp.text
        assert "no products" in resp.text or "vending_alive" in resp.text

    def test_health_lists_permissives_with_not_instrumented(self, wired):
        client, _ = wired
        resp = client.get("/health")
        assert "bag_present" in resp.text
        assert "not instrumented" in resp.text
        assert "vending_alive" in resp.text

    def test_screen_is_read_only_and_mobile(self, wired):
        client, _ = wired
        resp = client.get("/screen")
        assert resp.status_code == 200
        assert 'name="viewport"' in resp.text
        assert "hx-post" not in resp.text
        assert 'hx-get="/screen/body"' in resp.text
        body = client.get("/screen/body")
        assert body.status_code == 200
        assert "hx-post" not in body.text
        assert "Ice" in body.text and "Water" in body.text

    def test_screen_requires_auth(self, wired):
        client, _ = wired
        assert client.get("/screen", auth=("x", "y")).status_code == 401
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_web_routes.py::TestAvailabilityOnDashboard -v`
Expected: FAIL (404 on `/screen`, missing text on `/status`).

- [ ] **Step 3: Routes**

In `_render_status` add before the `TemplateResponse`:

```python
        payment_enabled = availability.payment_enabled if availability else None
        payment_reasons = availability.blocking_reasons() if availability else []
```

and pass `"payment_enabled": payment_enabled, "payment_reasons": payment_reasons` in the context.

In `health_summary` before the `TemplateResponse`:

```python
        health["availability"] = availability.table() if availability else []
        health["payment_enabled"] = availability.payment_enabled if availability else None
```

Add routes inside `attach_routes`:

```python
    def _screen_context(request: Request) -> dict:
        status = vmc_instance.get_status() if vmc_instance else {"state": "unknown", "credit_escrow": 0.0}
        faults = vmc_instance.active_faults() if vmc_instance else []
        health = health_monitor.get_summary() if health_monitor else {"subsystems": {}, "mqtt_connected": False}
        for name in EXPECTED_SUBSYSTEMS:
            health["subsystems"].setdefault(name, HealthMonitor.empty_subsystem_row())
        kinds = {}
        for kind in ("ice", "water"):
            ok, failing = availability.sale_available(kind) if availability else (None, [])
            kinds[kind] = {"ok": ok, "failing": failing}
        return {
            "request": request,
            "status": status,
            "faults": faults,
            "health": health,
            "kinds": kinds,
            "payment_enabled": availability.payment_enabled if availability else None,
            "payment_reasons": availability.blocking_reasons() if availability else [],
        }

    @router.get("/screen", response_class=HTMLResponse)
    async def screen(request: Request):
        return templates.TemplateResponse("screen.html", {"request": request})

    @router.get("/screen/body", response_class=HTMLResponse)
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
```

- [ ] **Step 4: Templates**

`web_interface/templates/screen.html`:

```html
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Machine Status</title>
    <script src="https://unpkg.com/htmx.org@1.9.10"></script>
    <script src="https://cdn.tailwindcss.com"></script>
</head>
<body class="bg-slate-900 text-slate-100 font-sans min-h-screen">
<div class="max-w-3xl mx-auto px-4 py-6">
    <div id="screen-body" hx-get="/screen/body" hx-trigger="load, every 5s" hx-swap="innerHTML">
        <p class="text-slate-400">Loading…</p>
    </div>
</div>
</body>
</html>
```

`web_interface/templates/partials/screen_body.html`:

```html
{# Read-only owner status screen; no controls here. #}
<div class="space-y-4">
  <div class="rounded-2xl p-6 {{ 'bg-emerald-700' if payment_enabled else 'bg-red-800' }}">
    <div class="text-sm uppercase tracking-wide opacity-80">Payment</div>
    <div class="text-4xl font-bold">{{ 'Enabled' if payment_enabled else 'Disabled' }}</div>
    {% if payment_reasons %}
    <div class="mt-2 text-lg">{{ payment_reasons|join(', ') }}</div>
    {% endif %}
  </div>

  <div class="grid grid-cols-1 sm:grid-cols-2 gap-4">
    {% for kind, info in kinds.items() %}
    <div class="rounded-2xl p-5 {{ 'bg-emerald-800' if info.ok else 'bg-slate-700' }}">
      <div class="text-sm uppercase tracking-wide opacity-80">{{ kind|capitalize }}</div>
      <div class="text-2xl font-semibold">{{ 'Available' if info.ok else 'Unavailable' }}</div>
      {% if info.failing %}<div class="text-sm opacity-90 mt-1">{{ info.failing|join(', ') }}</div>{% endif %}
    </div>
    {% endfor %}
  </div>

  <div class="grid grid-cols-2 sm:grid-cols-4 gap-4">
    <div class="rounded-2xl bg-slate-800 p-4"><div class="text-xs uppercase opacity-70">State</div><div class="text-xl">{{ status.state }}</div></div>
    <div class="rounded-2xl bg-slate-800 p-4"><div class="text-xs uppercase opacity-70">Escrow</div><div class="text-xl">${{ "%.2f"|format(status.credit_escrow) }}</div></div>
    <div class="rounded-2xl bg-slate-800 p-4"><div class="text-xs uppercase opacity-70">Money 24h</div><div class="text-xl">{% if money_24h is not none %}${{ "%.2f"|format(money_24h) }}{% else %}—{% endif %}</div></div>
    <div class="rounded-2xl bg-slate-800 p-4"><div class="text-xs uppercase opacity-70">Vends 24h</div><div class="text-xl">{{ vends_24h if vends_24h is not none else "—" }}</div></div>
  </div>

  <div class="rounded-2xl bg-slate-800 p-4">
    <div class="text-xs uppercase opacity-70 mb-2">Subsystems</div>
    <div class="flex flex-wrap gap-4">
      <span class="flex items-center gap-2"><span class="w-3 h-3 rounded-full {{ 'bg-emerald-400' if health.mqtt_connected else 'bg-red-500' }}"></span>broker</span>
      {% for name, sub in health.subsystems.items() %}
      <span class="flex items-center gap-2"><span class="w-3 h-3 rounded-full {{ 'bg-red-500' if sub.stale else ('bg-emerald-400' if sub.alive else 'bg-slate-500') }}"></span>{{ name }}</span>
      {% endfor %}
    </div>
  </div>

  <div class="rounded-2xl bg-slate-800 p-4">
    <div class="text-xs uppercase opacity-70 mb-2">Active faults</div>
    {% if faults %}
    <ul class="space-y-1">
      {% for f in faults %}<li><span class="font-mono">{{ f.code }}</span> {{ f.description }} <span class="opacity-70">· {{ f.product or "machine" }}</span></li>{% endfor %}
    </ul>
    {% else %}<p class="opacity-70">None</p>{% endif %}
  </div>
</div>
```

In `status_fragment.html`, in both branches' right-hand column add a tile before "State":

```html
        <div>
          <div class="text-xs text-gray-400 uppercase tracking-wide mb-0.5">Payment</div>
          {% if payment_enabled is none %}
          <div class="font-medium text-gray-400">—</div>
          {% elif payment_enabled %}
          <div class="font-medium text-green-700">Enabled</div>
          {% else %}
          <div class="font-medium text-red-700">Disabled</div>
          <div class="text-xs text-gray-500">{{ payment_reasons|join(', ') }}</div>
          {% endif %}
        </div>
```

In `health_fragment.html`, after the subsystems table add:

```html
  {% if health.availability %}
  <div>
    <h3 class="text-xs font-medium text-gray-400 uppercase tracking-wide mb-2">
      Sale permissives — payment {{ 'enabled' if health.payment_enabled else 'disabled' }}
    </h3>
    <table class="w-full text-sm">
      <thead>
        <tr class="border-b border-gray-200 text-xs text-gray-400 uppercase tracking-wide">
          <th class="pb-2 text-left font-medium">Input</th>
          <th class="pb-2 text-left font-medium">Applies</th>
          <th class="pb-2 text-left font-medium">State</th>
          <th class="pb-2 text-left font-medium">Detail</th>
        </tr>
      </thead>
      <tbody class="divide-y divide-gray-100">
        {% for row in health.availability %}
        <tr>
          <td class="py-2 font-mono text-xs text-gray-700">{{ row.name }}</td>
          <td class="py-2 text-gray-500">{{ row.applies_to }}</td>
          <td class="py-2">
            {% if not row.instrumented %}<span class="text-gray-400">not instrumented</span>
            {% elif row.state == 'pass' %}<span class="text-green-600 font-medium">pass</span>
            {% elif row.state == 'fail' %}<span class="text-red-600 font-medium">fail</span>
            {% else %}<span class="text-amber-600 font-medium">unknown</span>{% endif %}
          </td>
          <td class="py-2 text-gray-500">{{ row.detail }}</td>
        </tr>
        {% endfor %}
      </tbody>
    </table>
  </div>
  {% endif %}
```

- [ ] **Step 5: Run tests**

Run: `uv run pytest tests/test_web_routes.py -q`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
uv run ruff check --fix .
uv run ruff format .
git add web_interface tests/test_web_routes.py
git commit -m "feat(web): permissive table, payment status line, read-only /screen"
```

---

### Task 14: arm64 image, docs, and e2e enable test

**Files:**
- Modify: `.github/workflows/ci.yml:59-75`, `README.md`, `CLAUDE.md`, `ROADMAP.md` §3, §7, Phase C
- Test: `tests/test_integration_e2e.py`

- [ ] **Step 1: CI**

In `.github/workflows/ci.yml` image job, before `docker/setup-buildx-action@v3`:

```yaml
      - uses: docker/setup-qemu-action@v3
```

and change `platforms: linux/amd64` to `platforms: linux/amd64,linux/arm64`.

- [ ] **Step 2: Docs**

`README.md` after the Dashboard line:

```
Owner status screen (read-only, phone-friendly): http://localhost:26123/screen
```

`CLAUDE.md` Services section, add:

```
- `availability.py` - permissive truth table (ROADMAP §3); publishes `cmd/payment/enable` on change; feeds the health tab and `/screen`
- `session_store.py` - atomic snapshot of the live sale in `data/session.json`; an open snapshot at boot raises `PAY-104` until an admin clears it
- `paths.py` - `LOG_DIR`, `LOG_FILE`, `DATA_DIR` shared by main, routes and services
```

and in the FSM Core paragraph append: "Heartbeat loss raises `COM-101` (vending), `COM-102` (ice maker), `PAY-101` (MDB) and `COM-103` (broker) through the fault registry and auto-clears on recovery."

`ROADMAP.md`:
- §3: replace "Today the VMC enables payment whenever it is `idle`. Replacing that with these flags is Phase C work." with "Implemented in `services/availability.py`: known inputs are evaluated, inputs the firmware cannot report yet are listed as not instrumented and pass until Phase B/D."
- §7 restart bullet: append "(implemented as `PAY-104`; see `services/session_store.py`)".
- Phase C: strike "Availability permissives (§3) drive `payment/enable` instead of FSM state." and "Refund policy (§7) ... including the restart-mid-sale reconciliation." and "Per-product availability on the dashboard (ice vs water) with the failing permissive named." with `~~...~~` and a pointer to this spec.

- [ ] **Step 3: e2e test**

Append to `tests/test_integration_e2e.py`, reusing the module's existing broker-skip decorator/marker (read the file; every test there uses the same pattern):

```python
async def test_vending_heartbeat_loss_withdraws_payment_enable():
    """Live broker: vending LWT -> payment/enable false; heartbeat back -> true."""
    from services.availability import Availability

    cfg = ConfigModel()
    cfg.machine_id = "e2e-enable"
    cfg.physical.products = [Product(sku="ICE-1", name="Ice", price=1.0, kind="ice")]
    mqtt = MQTTClient(config=cfg.mqtt, machine_id=cfg.machine_id)
    vmc = VMC(config=cfg)
    vmc.attach_to_loop(asyncio.get_running_loop())
    monitor = HealthMonitor()
    vmc.set_health_monitor(monitor)
    avail = Availability(cfg.products)
    vmc.set_availability(avail)
    vmc.set_mqtt_client(mqtt)
    mqtt.set_connection_callback(lambda c: (monitor.update_mqtt_status(c), vmc.on_mqtt_connection(c)))
    run = asyncio.create_task(mqtt.run())

    seen: list[bool] = []
    async with aiomqtt.Client(hostname="localhost", port=1883) as probe:
        await probe.subscribe(f"vmc/{cfg.machine_id}/cmd/payment/enable")
        await asyncio.sleep(1.0)
        prefix = f"vmc/{cfg.machine_id}"
        for name in ("vending", "mdb", "ice_maker"):
            await probe.publish(f"{prefix}/heartbeat/{name}", json.dumps({"subsystem": name, "uptime_seconds": 1}))
        await probe.publish(f"{prefix}/payment/status", json.dumps({"device": "coin_acceptor", "state": "ready"}))
        await probe.publish(f"{prefix}/hardware/io/bin_half_full", json.dumps({"device": "bin_half_full", "state": True}))
        await asyncio.sleep(1.0)
        await probe.publish(f"{prefix}/heartbeat/vending", json.dumps({"subsystem": "vending", "uptime_seconds": -1}))
        await asyncio.sleep(1.0)
        await probe.publish(f"{prefix}/heartbeat/vending", json.dumps({"subsystem": "vending", "uptime_seconds": 2}))
        await asyncio.sleep(1.0)

        while True:
            try:
                msg = await asyncio.wait_for(probe.messages.__anext__(), timeout=0.5)
            except asyncio.TimeoutError:
                break
            seen.append(json.loads(msg.payload)["accept"])

    run.cancel()
    vmc.cancel_pending_tasks()
    assert seen[-3:] == [True, False, True]
```

Add `from config.config_model import Product` if missing.

- [ ] **Step 4: Full suite**

Run: `uv run pytest -q`
Expected: everything passes; e2e tests skip without a broker. If a broker is reachable on localhost:1883 the new e2e test must pass too.

Run: `uv run ruff check .` then `uv run ruff format --check .`
Expected: clean.

- [ ] **Step 5: Commit**

```bash
git add .github/workflows/ci.yml README.md CLAUDE.md ROADMAP.md tests/test_integration_e2e.py
git commit -m "ci: build arm64 image; docs for availability, session store, /screen; e2e enable test"
```

---

## Self-review

**Spec coverage:** §1 Availability → Tasks 7, 9, 13. §2 COM faults + MDB sim → Tasks 8, 9, 12. §3 Session persistence + PAY-104 + contract → Tasks 6, 10, 11. §4 Notifier → Task 3; LWT/retain → Task 4; event loop → Tasks 1, 5; fsync → Task 2; paths → Task 1. §5 arm64 + `/screen` → Tasks 13, 14. Error handling: Availability setters never raise (Task 7), corrupt session → PAY-104 (Tasks 10, 11), writer thread survives bad rows (Task 5), liveness callback exceptions swallowed (Task 8). Testing list in spec maps one-to-one onto the task tests.

**Deviations from the spec, decided here:** `Availability.set_publisher` takes a sync callable and `republish()` is sync (VMC fires the coroutine); `set_machine_faults` + `set_lockouts` are merged into `set_active_faults(list[dict])`; `service_door_closed`, `no_critical_fault`, `transaction_certain` start `PASS` (the vending ESP32 does not report the door unprompted). The spec's inventory-path move into `data/` is dropped (YAGNI; `inventory.json` stays where it is). `_persist_session()` also runs from every `_publish_status()` call (so from FSM `before` hooks), not only the five named call sites; transient snapshots written from a `before` hook are superseded by the explicit call that follows and fail safe (spurious PAY-104) on a crash in between.

**Type consistency:** `Availability.set_subsystem_alive(name, alive)`, `set_hardware_io(device, state)`, `set_active_faults(faults)`, `product_sellable(product) -> (bool, list[str])`, `blocking_reasons() -> list[str]` are used identically in Tasks 7, 9, 11, 13. `SessionSnapshot` field names match between Tasks 10 and 11. `MQTTClient.publish(..., retain=)` from Task 4 is what Task 9's fake accepts.
