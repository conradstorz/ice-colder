# Ice Maker Monitor Contract Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship contract v1.0.0 between ice-colder and the external brand-specific ice-maker monitor: Pydantic models + generated JSON Schemas + CONTRACT.md, the ice-maker simulator upgraded into the reference implementation (capabilities, telemetry, commands/acks, LWT), and minimal VMC consumption (capabilities store, generic channel health, ack logging, LWT offline handling).

**Architecture:** Per spec `docs/superpowers/specs/2026-09-13-ice-maker-monitor-contract-design.md` (Approach A — extend existing topics). New `contracts/` package is the machine-readable source of truth; schemas are generated artifacts with a drift test. Real topic prefix is `vmc/{machine_id}/` (simulators/base.py:79, services/mqtt_client.py:56) — CONTRACT.md documents that, superseding the spec's shorthand `{machine_id}/`.

**Tech Stack:** Python 3.12, Pydantic v2 (`model_json_schema`), aiomqtt (incl. `aiomqtt.Will`), pytest asyncio_mode=auto, uv.

## Global Constraints

- `uv run pytest` / `uv run python` only. Lint: `uv run ruff check --fix .` then `uv run ruff format .` (ALWAYS `uv run` — global ruff is older and formats differently). Separate tool calls; NEVER chain with `&&`.
- Commit after every task; messages end with `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`
- Do NOT touch config.json, colder-docker/, or data/.
- Baseline: full suite 357 passed, 9 skipped.
- Contract constants (verbatim everywhere): `CONTRACT_VERSION = "1.0.0"`; command names `power_cycle`, `force_report`, `set_interval`; ack statuses `ok`, `rejected`, `failed`, `unsupported`; dwell bounds [5, 300] s; interval bounds [1, 3600] s; power-cycle lockout 300 s; heartbeat 10 s; LWT payload `{"subsystem": "<name>", "uptime_seconds": -1}`.

---

### Task 1: `contracts` package — models + bounds tests

**Files:**
- Create: `contracts/__init__.py` (empty)
- Create: `contracts/ice_maker_monitor.py`
- Create: `tests/test_contracts.py`

**Interfaces:**
- Produces: `CONTRACT_VERSION`, `ChannelDescriptor`, `MonitorCapabilities`, `ChannelReading`, `MonitorCommand`, `CommandAck` — imported by Tasks 2, 4, 5.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_contracts.py`:

```python
"""Tests for the ice-maker monitor contract models."""

import pytest
from pydantic import ValidationError

from contracts.ice_maker_monitor import (
    CONTRACT_VERSION,
    ChannelDescriptor,
    ChannelReading,
    CommandAck,
    MonitorCapabilities,
    MonitorCommand,
)


class TestChannelDescriptor:
    def test_valid(self):
        d = ChannelDescriptor(
            channel_id="compressor_current",
            kind="current",
            unit="A",
            description="Compressor draw",
            interval_seconds=5.0,
        )
        assert d.channel_id == "compressor_current"

    def test_rejects_bad_channel_id(self):
        with pytest.raises(ValidationError):
            ChannelDescriptor(
                channel_id="Bad-Id!", kind="binary", interval_seconds=5.0
            )

    def test_rejects_unknown_kind(self):
        with pytest.raises(ValidationError):
            ChannelDescriptor(
                channel_id="x", kind="pressure", interval_seconds=5.0
            )

    def test_rejects_interval_out_of_range(self):
        with pytest.raises(ValidationError):
            ChannelDescriptor(channel_id="x", kind="binary", interval_seconds=0)
        with pytest.raises(ValidationError):
            ChannelDescriptor(channel_id="x", kind="binary", interval_seconds=3601)


class TestMonitorCapabilities:
    def test_valid_with_defaults(self):
        caps = MonitorCapabilities(
            contract_version=CONTRACT_VERSION,
            brand="BrandX",
            model="IM-500",
            firmware="0.1.0",
        )
        assert caps.subsystem == "ice_maker"
        assert caps.channels == []
        assert caps.commands == []

    def test_contract_version_constant(self):
        assert CONTRACT_VERSION == "1.0.0"


class TestChannelReading:
    def test_valid(self):
        r = ChannelReading(channel_id="bin_level", value=42.5)
        assert r.value == 42.5

    def test_rejects_bad_channel_id(self):
        with pytest.raises(ValidationError):
            ChannelReading(channel_id="Nope Space", value=1.0)


class TestMonitorCommand:
    def test_power_cycle_valid(self):
        cmd = MonitorCommand(
            request_id="req-12345678",
            command="power_cycle",
            params={"dwell_seconds": 30},
        )
        assert cmd.params["dwell_seconds"] == 30

    def test_power_cycle_requires_dwell(self):
        with pytest.raises(ValidationError):
            MonitorCommand(request_id="req-12345678", command="power_cycle")

    def test_power_cycle_dwell_bounds(self):
        for dwell in (4, 301):
            with pytest.raises(ValidationError):
                MonitorCommand(
                    request_id="req-12345678",
                    command="power_cycle",
                    params={"dwell_seconds": dwell},
                )

    def test_set_interval_bounds(self):
        for iv in (0.5, 3601):
            with pytest.raises(ValidationError):
                MonitorCommand(
                    request_id="req-12345678",
                    command="set_interval",
                    params={"interval_seconds": iv},
                )

    def test_force_report_needs_no_params(self):
        cmd = MonitorCommand(request_id="req-12345678", command="force_report")
        assert cmd.params == {}

    def test_unknown_command_rejected(self):
        with pytest.raises(ValidationError):
            MonitorCommand(request_id="req-12345678", command="self_destruct")


class TestCommandAck:
    def test_valid(self):
        ack = CommandAck(
            request_id="req-12345678", command="power_cycle", status="rejected",
            detail="lockout",
        )
        assert ack.status == "rejected"

    def test_rejects_unknown_status(self):
        with pytest.raises(ValidationError):
            CommandAck(request_id="r-12345678", command="power_cycle", status="maybe")
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_contracts.py -q`
Expected: collection error — `ModuleNotFoundError: No module named 'contracts'`.

- [ ] **Step 3: Implement**

Create `contracts/__init__.py` (empty file) and `contracts/ice_maker_monitor.py`:

```python
# contracts/ice_maker_monitor.py
"""
Shared contract models for the ice-maker monitor interface (v1.0.0).

These models are the machine-readable source of truth for the interface
between ice-colder (the VMC) and the external brand-specific monitor
project. JSON Schemas are generated from them into
docs/contracts/ice-maker-monitor/schemas/ by contracts/generate.py.
Breaking changes require a major CONTRACT_VERSION bump.
"""

from datetime import datetime, timezone
from typing import Literal, Optional

from pydantic import BaseModel, Field, model_validator

CONTRACT_VERSION = "1.0.0"

_CHANNEL_ID_PATTERN = r"^[a-z0-9_]{1,64}$"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class ChannelDescriptor(BaseModel):
    """One telemetry channel the monitor declares in its capabilities."""

    channel_id: str = Field(
        ..., pattern=_CHANNEL_ID_PATTERN, description="Slug; also the topic segment"
    )
    kind: Literal["temperature", "current", "voltage", "level", "binary", "counter"]
    unit: str = Field("", description="Unit, e.g. 'C', 'A', '%'; empty for binary")
    description: str = Field("", description="Human-readable channel description")
    interval_seconds: float = Field(
        ..., gt=0, le=3600, description="Declared publish cadence"
    )


class MonitorCapabilities(BaseModel):
    """Retained self-description published on connect and on channel changes."""

    subsystem: Literal["ice_maker"] = "ice_maker"
    contract_version: str = Field(..., description="Contract semver, e.g. '1.0.0'")
    brand: str = Field(..., description="Ice maker brand the monitor targets")
    model: str = Field(..., description="Ice maker model")
    firmware: str = Field(..., description="Monitor software version")
    channels: list[ChannelDescriptor] = Field(default_factory=list)
    commands: list[str] = Field(
        default_factory=list, description="Contract commands this monitor supports"
    )
    timestamp: datetime = Field(default_factory=_utc_now)


class ChannelReading(BaseModel):
    """One reading on telemetry/ice_maker/<channel_id>."""

    channel_id: str = Field(..., pattern=_CHANNEL_ID_PATTERN)
    value: float = Field(..., description="Binary channels use 0.0/1.0")
    timestamp: datetime = Field(default_factory=_utc_now)


class MonitorCommand(BaseModel):
    """VMC -> monitor command on cmd/ice_maker."""

    request_id: str = Field(..., min_length=8, max_length=64)
    command: Literal["power_cycle", "force_report", "set_interval"]
    params: dict[str, float] = Field(default_factory=dict)
    timestamp: datetime = Field(default_factory=_utc_now)

    @model_validator(mode="after")
    def _check_params(self):
        if self.command == "power_cycle":
            dwell = self.params.get("dwell_seconds")
            if dwell is None or not (5 <= dwell <= 300):
                raise ValueError("power_cycle requires dwell_seconds in [5, 300]")
        elif self.command == "set_interval":
            interval = self.params.get("interval_seconds")
            if interval is None or not (1 <= interval <= 3600):
                raise ValueError(
                    "set_interval requires interval_seconds in [1, 3600]"
                )
        return self


class CommandAck(BaseModel):
    """Monitor -> VMC acknowledgement on cmd/ice_maker/ack."""

    request_id: str = Field(..., description="Echoed from the command")
    command: str
    status: Literal["ok", "rejected", "failed", "unsupported"]
    detail: Optional[str] = None
    timestamp: datetime = Field(default_factory=_utc_now)
```

- [ ] **Step 4: Run to verify pass, lint, commit**

Run: `uv run pytest tests/test_contracts.py -q` — all pass. Then full suite, lint.

```bash
git add contracts/ tests/test_contracts.py
git commit -m "feat: contract models for the ice-maker monitor interface (v1.0.0)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 2: Schema generator + drift test

**Files:**
- Create: `contracts/generate.py`
- Create: `docs/contracts/ice-maker-monitor/schemas/` (8 generated files)
- Create: `tests/test_contract_schemas.py`

**Interfaces:**
- Consumes: Task 1 models; `SensorReading`, `IceMakerEvent`, `SubsystemHeartbeat` from `services/mqtt_messages.py`.
- Produces: `contracts.generate.MODELS` dict and `generate(out_dir) -> list[Path]`; committed schema files Task 3 links to.

- [ ] **Step 1: Write the failing test**

Create `tests/test_contract_schemas.py`:

```python
"""Committed JSON Schemas must match the live Pydantic models (drift guard)."""

import json
from pathlib import Path

import pytest

from contracts.generate import MODELS, SCHEMA_DIR


def test_schema_dir_has_exactly_the_expected_files():
    expected = {f"{name}.schema.json" for name in MODELS}
    actual = {p.name for p in SCHEMA_DIR.glob("*.schema.json")}
    assert actual == expected


@pytest.mark.parametrize("name", sorted(MODELS))
def test_committed_schema_matches_model(name):
    committed = json.loads(
        (SCHEMA_DIR / f"{name}.schema.json").read_text(encoding="utf-8")
    )
    assert committed == MODELS[name].model_json_schema(), (
        f"{name} schema drifted — run: uv run python -m contracts.generate"
    )
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_contract_schemas.py -q`
Expected: ImportError (no `contracts.generate`).

- [ ] **Step 3: Implement the generator**

Create `contracts/generate.py`:

```python
# contracts/generate.py
"""Generate the contract's JSON Schema files.

Run after any model change: uv run python -m contracts.generate
tests/test_contract_schemas.py fails if the committed files drift.
"""

import json
from pathlib import Path

from services.mqtt_messages import IceMakerEvent, SensorReading, SubsystemHeartbeat

from contracts.ice_maker_monitor import (
    ChannelDescriptor,
    ChannelReading,
    CommandAck,
    MonitorCapabilities,
    MonitorCommand,
)

SCHEMA_DIR = Path("docs/contracts/ice-maker-monitor/schemas")

MODELS = {
    "sensor_reading": SensorReading,
    "ice_maker_event": IceMakerEvent,
    "subsystem_heartbeat": SubsystemHeartbeat,
    "channel_descriptor": ChannelDescriptor,
    "monitor_capabilities": MonitorCapabilities,
    "channel_reading": ChannelReading,
    "monitor_command": MonitorCommand,
    "command_ack": CommandAck,
}


def generate(out_dir: Path = SCHEMA_DIR) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for name, model in MODELS.items():
        path = out_dir / f"{name}.schema.json"
        schema = model.model_json_schema()
        path.write_text(json.dumps(schema, indent=2) + "\n", encoding="utf-8")
        written.append(path)
    return written


if __name__ == "__main__":
    for path in generate():
        print(f"wrote {path}")
```

- [ ] **Step 4: Generate, verify, commit**

Run: `uv run python -m contracts.generate` — prints 8 written files.
Run: `uv run pytest tests/test_contract_schemas.py -q` — all pass.
Run: `uv run pytest -q` — full suite green. Lint.

```bash
git add contracts/generate.py docs/contracts/ tests/test_contract_schemas.py
git commit -m "feat: JSON Schema generation for the monitor contract with drift test

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 3: CONTRACT.md

**Files:**
- Create: `docs/contracts/ice-maker-monitor/CONTRACT.md`

**Interfaces:**
- Consumes: the committed schemas (Task 2) and the design spec.

- [ ] **Step 1: Author the document**

Write `docs/contracts/ice-maker-monitor/CONTRACT.md` by transcribing the
normative content of `docs/superpowers/specs/2026-09-13-ice-maker-monitor-contract-design.md`
sections **Versioning**, **Transport rules**, **Topic map**, **Message
schemas**, **Cadence & liveness**, **Command semantics**, and **Conformance
checklist**, with these transformations:

1. Title: `# Ice Maker Monitor Contract — v1.0.0`, followed by a short
   preamble: this document plus the `schemas/*.schema.json` files define the
   interface between the ice-colder VMC and the brand-specific ice-maker
   monitor; the schemas are generated from ice-colder's Pydantic models and
   are the normative payload definitions.
2. Every topic uses the REAL prefix: `vmc/{machine_id}/...` (not
   `{machine_id}/...`).
3. In the Message schemas section, replace the Pydantic field lists with a
   table per message: field, type, constraints, description — matching the
   generated schema files exactly — and link each to its schema file
   (relative link `schemas/<name>.schema.json`).
4. Add a "Reference implementation" section: `simulators/ice_maker.py` in
   the ice-colder repo speaks the full contract and serves as a live
   conformance fixture (run it against a broker and observe/drive it).
5. Keep the conformance checklist as a checkbox list.

No TBDs, no placeholders — every rule in the spec sections named above must
appear with its concrete value (10 s heartbeat, 120 s stale, LWT payload,
QoS rules, retained capabilities, 10 s ack deadline, 5–300 s dwell, 300 s
lockout, semver rules, ISO-8601 UTC timestamps).

- [ ] **Step 2: Verify links and commit**

Verify every `schemas/...` link target exists (`ls docs/contracts/ice-maker-monitor/schemas`).

```bash
git add docs/contracts/ice-maker-monitor/CONTRACT.md
git commit -m "docs: CONTRACT.md v1.0.0 for the ice-maker monitor interface

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 4: Simulator becomes the reference implementation

**Files:**
- Modify: `simulators/base.py` (`publish` gains `retain`; LWT via `_build_will()` used in `run()`)
- Modify: `simulators/ice_maker.py` (capabilities, telemetry, command handling)
- Modify: `tests/test_simulator_ice_maker.py` (append tests), `tests/test_simulator_base.py` (append LWT/retain tests)

**Interfaces:**
- Consumes: Task 1 models.
- Produces: retained `capabilities/ice_maker`, `telemetry/ice_maker/{compressor_current,bin_level}`, command handling on `cmd/ice_maker` with acks on `cmd/ice_maker/ack`, LWT on `heartbeat/<subsystem>` for ALL simulators.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_simulator_base.py` (match its existing imports; it imports `ESP32Simulator`; add `import json` if absent — use a concrete subclass the file already defines, or `IceMakerSimulator` if none):

```python
class TestContractTransport:
    def test_build_will_targets_heartbeat_with_offline_marker(self):
        from simulators.ice_maker import IceMakerSimulator

        sim = IceMakerSimulator(machine_id="vmc-test")
        will = sim._build_will()
        assert will.topic == "vmc/vmc-test/heartbeat/ice_maker"
        assert json.loads(will.payload) == {
            "subsystem": "ice_maker",
            "uptime_seconds": -1,
        }
        assert will.qos == 1

    @pytest.mark.asyncio
    async def test_publish_passes_retain_flag(self):
        from unittest.mock import AsyncMock

        from simulators.ice_maker import IceMakerSimulator

        sim = IceMakerSimulator(machine_id="vmc-test")
        client = AsyncMock()
        await sim.publish(client, "capabilities/ice_maker", {"x": 1}, retain=True)
        assert client.publish.call_args.kwargs.get("retain") is True
```

Append to `tests/test_simulator_ice_maker.py`:

```python
class TestMonitorContract:
    def _sim(self):
        return IceMakerSimulator(machine_id="vmc-test")

    def test_capabilities_lists_all_channels_and_commands(self):
        from contracts.ice_maker_monitor import CONTRACT_VERSION

        caps = self._sim().build_capabilities()
        assert caps.contract_version == CONTRACT_VERSION
        ids = [c.channel_id for c in caps.channels]
        assert len(ids) == 12  # 10 temps + compressor_current + bin_level
        assert "hot_gas_valve_1" in ids
        assert "compressor_current" in ids
        assert "bin_level" in ids
        assert caps.commands == ["power_cycle", "force_report", "set_interval"]

    @pytest.mark.asyncio
    async def test_power_cycle_ok_then_lockout(self):
        from contracts.ice_maker_monitor import MonitorCommand

        sim = self._sim()
        published = []

        async def capture(client, suffix, payload, retain=False):
            published.append((suffix, payload))

        sim.publish = capture
        client = AsyncMock()
        cmd = MonitorCommand(
            request_id="req-00000001",
            command="power_cycle",
            params={"dwell_seconds": 5},
        )
        await sim._handle_command(client, cmd)
        acks = [p for s, p in published if s == "cmd/ice_maker/ack"]
        assert acks[-1].status == "ok"
        assert sim.compressor_on is False

        cmd2 = MonitorCommand(
            request_id="req-00000002",
            command="power_cycle",
            params={"dwell_seconds": 5},
        )
        await sim._handle_command(client, cmd2)
        acks = [p for s, p in published if s == "cmd/ice_maker/ack"]
        assert acks[-1].status == "rejected"
        assert acks[-1].detail == "lockout"

    @pytest.mark.asyncio
    async def test_duplicate_request_id_reacks_without_reexecuting(self):
        from contracts.ice_maker_monitor import MonitorCommand

        sim = self._sim()
        published = []

        async def capture(client, suffix, payload, retain=False):
            published.append((suffix, payload))

        sim.publish = capture
        client = AsyncMock()
        cmd = MonitorCommand(
            request_id="req-00000003",
            command="set_interval",
            params={"interval_seconds": 7},
        )
        await sim._handle_command(client, cmd)
        first_ack_count = len(published)
        sim._publish_interval = 99.0  # would change again if re-executed
        await sim._handle_command(client, cmd)
        assert len(published) == first_ack_count + 1  # re-acked
        assert sim._publish_interval == 99.0  # NOT re-executed

    @pytest.mark.asyncio
    async def test_set_interval_changes_publish_interval(self):
        from contracts.ice_maker_monitor import MonitorCommand

        sim = self._sim()
        sim.publish = AsyncMock()
        client = AsyncMock()
        await sim._handle_command(
            client,
            MonitorCommand(
                request_id="req-00000004",
                command="set_interval",
                params={"interval_seconds": 30},
            ),
        )
        assert sim._publish_interval == 30.0

    @pytest.mark.asyncio
    async def test_force_report_publishes_snapshot_and_acks(self):
        from contracts.ice_maker_monitor import MonitorCommand

        sim = self._sim()
        published = []

        async def capture(client, suffix, payload, retain=False):
            published.append((suffix, payload))

        sim.publish = capture
        client = AsyncMock()
        await sim._handle_command(
            client,
            MonitorCommand(request_id="req-00000005", command="force_report"),
        )
        suffixes = [s for s, _ in published]
        assert sum(s.startswith("sensors/temp/") for s in suffixes) == 10
        assert "telemetry/ice_maker/compressor_current" in suffixes
        assert "telemetry/ice_maker/bin_level" in suffixes
        assert suffixes[-1] == "cmd/ice_maker/ack"
```

- [ ] **Step 2: Run to verify failures**

Run: `uv run pytest tests/test_simulator_base.py tests/test_simulator_ice_maker.py -q`
Expected: new tests FAIL (`_build_will`/`build_capabilities`/`_handle_command` missing; `publish` lacks `retain`).

- [ ] **Step 3: Implement base.py transport pieces**

In `simulators/base.py`:

1. `publish` gains retain:

```python
    async def publish(
        self,
        client: aiomqtt.Client,
        topic_suffix: str,
        payload: BaseModel | dict,
        retain: bool = False,
    ):
        """Publish a message to vmc/{machine_id}/{topic_suffix}."""
        full_topic = f"{self.topic_prefix}/{topic_suffix}"
        if isinstance(payload, BaseModel):
            data = payload.model_dump_json()
        else:
            data = json.dumps(payload)
        await client.publish(full_topic, data, retain=retain)
        logger.debug(f"[{self.subsystem_name}] published to {full_topic}")
```

2. Add below `_build_heartbeat`:

```python
    def _build_will(self) -> aiomqtt.Will:
        """LWT: mark this subsystem offline instantly on unclean disconnect."""
        return aiomqtt.Will(
            topic=f"{self.topic_prefix}/heartbeat/{self.subsystem_name}",
            payload=json.dumps(
                {"subsystem": self.subsystem_name, "uptime_seconds": -1}
            ),
            qos=1,
        )
```

3. In `run()`, pass it: `aiomqtt.Client(hostname=self.broker, port=self.port, identifier=f"sim-{self.subsystem_name}", will=self._build_will())`.

- [ ] **Step 4: Implement the ice-maker contract surface**

In `simulators/ice_maker.py`:

1. Imports: add `import time` and

```python
from contracts.ice_maker_monitor import (
    CONTRACT_VERSION,
    ChannelDescriptor,
    ChannelReading,
    CommandAck,
    MonitorCapabilities,
    MonitorCommand,
)
from pydantic import ValidationError
```

2. Module constants after `SENSOR_DEFS`:

```python
TELEMETRY_CHANNELS = [
    ChannelDescriptor(
        channel_id="compressor_current",
        kind="current",
        unit="A",
        description="Compressor current draw",
        interval_seconds=5.0,
    ),
    ChannelDescriptor(
        channel_id="bin_level",
        kind="level",
        unit="%",
        description="Ice bin fill level",
        interval_seconds=5.0,
    ),
]

POWER_CYCLE_LOCKOUT_SECONDS = 300.0
```

3. `__init__` additions (after `self._pending_events = []`):

```python
        self._publish_interval = float(self.PUBLISH_INTERVAL)
        self._last_power_cycle = -1e9
        self._acked: dict[str, CommandAck] = {}
        self._bin_level = 20.0
```

4. In `tick()`, where `ice_dropped` is appended, also add (directly after the append):

```python
                self._bin_level = min(100.0, self._bin_level + 2.0)
```

5. New methods:

```python
    def build_capabilities(self) -> MonitorCapabilities:
        temp_channels = [
            ChannelDescriptor(
                channel_id=s.name,
                kind="temperature",
                unit="C",
                description=f"{s.name.replace('_', ' ')} temperature",
                interval_seconds=self._publish_interval,
            )
            for s in self.sensors
        ]
        return MonitorCapabilities(
            contract_version=CONTRACT_VERSION,
            brand="ice-colder",
            model="simulator",
            firmware="sim",
            channels=temp_channels + TELEMETRY_CHANNELS,
            commands=["power_cycle", "force_report", "set_interval"],
        )

    def _compressor_current(self) -> float:
        base = 8.5 if self.compressor_on else 0.4
        return round(base + random.gauss(0, 0.15), 2)

    async def _publish_snapshot(self, client: aiomqtt.Client):
        """Publish one full round of sensor + telemetry readings."""
        for sensor in self.sensors:
            reading = SensorReading(location=sensor.name, value=round(sensor.value, 2))
            await self.publish(client, f"sensors/temp/{sensor.name}", reading)
        await self.publish(
            client,
            "telemetry/ice_maker/compressor_current",
            ChannelReading(
                channel_id="compressor_current", value=self._compressor_current()
            ),
        )
        await self.publish(
            client,
            "telemetry/ice_maker/bin_level",
            ChannelReading(channel_id="bin_level", value=round(self._bin_level, 1)),
        )

    async def _handle_command(self, client: aiomqtt.Client, cmd: MonitorCommand):
        if cmd.request_id in self._acked:
            await self.publish(client, "cmd/ice_maker/ack", self._acked[cmd.request_id])
            return

        if cmd.command == "power_cycle":
            now = time.monotonic()
            if now - self._last_power_cycle < POWER_CYCLE_LOCKOUT_SECONDS:
                ack = CommandAck(
                    request_id=cmd.request_id,
                    command=cmd.command,
                    status="rejected",
                    detail="lockout",
                )
            else:
                self._last_power_cycle = now
                dwell = cmd.params["dwell_seconds"]
                self.compressor_on = False
                self._cycle_elapsed = 0.0
                self._pending_events.append(
                    IceMakerEvent(event="power_off", detail="commanded power_cycle")
                )
                asyncio.get_running_loop().create_task(self._finish_power_cycle(dwell))
                ack = CommandAck(
                    request_id=cmd.request_id,
                    command=cmd.command,
                    status="ok",
                    detail=f"dwell {dwell:.0f}s",
                )
        elif cmd.command == "set_interval":
            self._publish_interval = float(cmd.params["interval_seconds"])
            ack = CommandAck(
                request_id=cmd.request_id, command=cmd.command, status="ok"
            )
        else:  # force_report — validated Literal, only three commands exist
            await self._publish_snapshot(client)
            ack = CommandAck(
                request_id=cmd.request_id, command=cmd.command, status="ok"
            )

        self._acked[cmd.request_id] = ack
        await self.publish(client, "cmd/ice_maker/ack", ack)
        logger.info(
            f"[ice_maker] Command {cmd.command} ({cmd.request_id}): {ack.status}"
        )

    async def _finish_power_cycle(self, dwell: float):
        await asyncio.sleep(dwell)
        self._pending_events.append(
            IceMakerEvent(event="power_cycled", detail="power restored")
        )
        logger.info("[ice_maker] Power cycle complete")

    async def _command_loop(self, client: aiomqtt.Client):
        queue = await self.subscribe(client, f"{self.topic_prefix}/cmd/ice_maker")
        while True:
            _, data = await queue.get()
            try:
                cmd = MonitorCommand.model_validate(data)
            except ValidationError as e:
                logger.warning(f"[ice_maker] Invalid command dropped: {e}")
                continue
            await self._handle_command(client, cmd)
```

6. Replace `run_simulation` with:

```python
    async def run_simulation(self, client: aiomqtt.Client):
        """Publish capabilities, then readings/events; handle contract commands."""
        logger.info("[ice_maker] Starting temperature monitoring simulation")
        await self.publish(
            client, "capabilities/ice_maker", self.build_capabilities(), retain=True
        )
        await self.publish(
            client,
            "ice_maker/event",
            IceMakerEvent(event="power_on", detail="simulator started"),
        )
        async with asyncio.TaskGroup() as tg:
            tg.create_task(self._command_loop(client))
            tg.create_task(self._publish_loop(client))

    async def _publish_loop(self, client: aiomqtt.Client):
        while True:
            self.tick(self._publish_interval)
            await self._publish_snapshot(client)
            for event in self._pending_events:
                await self.publish(client, "ice_maker/event", event)
            self._pending_events.clear()
            await asyncio.sleep(self._publish_interval)
```

- [ ] **Step 5: Run tests, full suite, lint, commit**

Run: `uv run pytest tests/test_simulator_base.py tests/test_simulator_ice_maker.py -q` — all pass.
Run: `uv run pytest -q` — full suite green (existing snapshot-publishing tests may need no change; if one asserts exact publish counts per loop iteration, update it for the two added telemetry publishes and note it in the report).

```bash
git add simulators/base.py simulators/ice_maker.py tests/test_simulator_base.py tests/test_simulator_ice_maker.py
git commit -m "feat: ice-maker simulator speaks the full monitor contract (reference implementation)

Retained capabilities, telemetry channels, power_cycle/force_report/set_interval
with acks + lockout + request_id idempotency, and MQTT LWT for all simulators.

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 5: VMC consumption — capabilities, telemetry, acks, LWT offline

**Files:**
- Modify: `services/health_monitor.py` (`record_channel`, `mark_offline`, summary)
- Modify: `controller/vmc.py` (three new handlers + registrations; LWT handling in heartbeat handler)
- Modify: `tests/test_health_monitor.py`, `tests/test_mqtt.py` (append tests)

**Interfaces:**
- Consumes: Task 1 models; `MQTTClient.register`.
- Produces: `HealthMonitor.record_channel(channel_id, value)`, `HealthMonitor.mark_offline(subsystem)`, `get_summary()["channels"]`; VMC handlers `_handle_mqtt_capabilities`, `_handle_mqtt_telemetry`, `_handle_mqtt_command_ack`; heartbeat with `uptime_seconds == -1` marks the subsystem offline.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_health_monitor.py`:

```python
class TestChannelsAndOffline:
    def test_record_channel_appears_in_summary(self):
        monitor = HealthMonitor()
        monitor.record_channel("compressor_current", 8.4)
        channels = monitor.get_summary()["channels"]
        assert channels["compressor_current"]["value"] == 8.4
        assert channels["compressor_current"]["age_seconds"] >= 0

    def test_mark_offline_makes_subsystem_stale(self):
        monitor = HealthMonitor()
        monitor.record_heartbeat("ice_maker")
        monitor.mark_offline("ice_maker")
        summary = monitor.get_summary()["subsystems"]["ice_maker"]
        assert summary["alive"] is False
        assert summary["stale"] is True

    def test_mark_offline_unknown_subsystem_is_harmless(self):
        HealthMonitor().mark_offline("nope")  # must not raise
```

Append to `tests/test_mqtt.py` (it already has direct VMC-handler tests — match its fixture style for constructing a VMC; add imports as needed):

```python
class TestMonitorContractHandlers:
    def _vmc_with_monitor(self):
        from services.health_monitor import HealthMonitor

        vmc = VMC(config=ConfigModel())
        monitor = HealthMonitor()
        vmc.set_health_monitor(monitor)
        return vmc, monitor

    async def test_capabilities_stored(self):
        from contracts.ice_maker_monitor import CONTRACT_VERSION

        vmc, _ = self._vmc_with_monitor()
        await vmc._handle_mqtt_capabilities(
            "capabilities/ice_maker",
            {
                "subsystem": "ice_maker",
                "contract_version": CONTRACT_VERSION,
                "brand": "BrandX",
                "model": "IM-500",
                "firmware": "0.1.0",
                "channels": [],
                "commands": ["power_cycle"],
            },
        )
        assert "ice_maker" in vmc.subsystem_capabilities
        assert vmc.subsystem_capabilities["ice_maker"]["brand"] == "BrandX"

    async def test_malformed_capabilities_stored_raw_with_warning(self):
        vmc, _ = self._vmc_with_monitor()
        await vmc._handle_mqtt_capabilities(
            "capabilities/vending", {"subsystem": "vending", "whatever": 1}
        )
        assert vmc.subsystem_capabilities["vending"] == {
            "subsystem": "vending",
            "whatever": 1,
        }

    async def test_telemetry_routed_to_health_monitor(self):
        vmc, monitor = self._vmc_with_monitor()
        await vmc._handle_mqtt_telemetry(
            "telemetry/ice_maker/bin_level",
            {"channel_id": "bin_level", "value": 42.0},
        )
        assert monitor.get_summary()["channels"]["bin_level"]["value"] == 42.0

    async def test_lwt_heartbeat_marks_offline(self):
        vmc, monitor = self._vmc_with_monitor()
        await vmc._handle_mqtt_heartbeat(
            "heartbeat/ice_maker", {"subsystem": "ice_maker", "uptime_seconds": 10}
        )
        assert monitor.get_summary()["subsystems"]["ice_maker"]["alive"] is True
        await vmc._handle_mqtt_heartbeat(
            "heartbeat/ice_maker", {"subsystem": "ice_maker", "uptime_seconds": -1}
        )
        assert monitor.get_summary()["subsystems"]["ice_maker"]["alive"] is False

    async def test_command_ack_logged_without_error(self):
        vmc, _ = self._vmc_with_monitor()
        await vmc._handle_mqtt_command_ack(
            "cmd/ice_maker/ack",
            {
                "request_id": "req-00000001",
                "command": "power_cycle",
                "status": "ok",
            },
        )  # must not raise
```

- [ ] **Step 2: Run to verify failures**

Run: `uv run pytest tests/test_health_monitor.py tests/test_mqtt.py -q`
Expected: new tests FAIL (missing methods/attributes).

- [ ] **Step 3: Implement HealthMonitor additions**

In `services/health_monitor.py`:

1. `__init__`: add `self._channels: dict[str, TemperatureReading] = {}` (reuse the `TemperatureReading` dataclass — it's just location/value/timestamp; rename semantics are fine, `location` holds the channel_id).
2. After `record_temperature`:

```python
    def record_channel(self, channel_id: str, value: float):
        """Record a generic telemetry channel reading (analog or binary)."""
        self._channels[channel_id] = TemperatureReading(
            location=channel_id, value=value, timestamp=time.monotonic()
        )

    def mark_offline(self, subsystem: str):
        """Force a subsystem to stale/offline (e.g., MQTT Last-Will received)."""
        if subsystem in self._subsystems:
            self._subsystems[subsystem].last_seen = 0.0
```

3. In `get_summary()`, before the return, build and include channels:

```python
        channels = {}
        for channel_id, reading in self._channels.items():
            channels[channel_id] = {
                "value": reading.value,
                "age_seconds": round(time.monotonic() - reading.timestamp, 1),
            }
```

and add `"channels": channels,` to the returned dict.

- [ ] **Step 4: Implement VMC handlers**

In `controller/vmc.py`:

1. Imports: add

```python
from contracts.ice_maker_monitor import ChannelReading, CommandAck, MonitorCapabilities
from pydantic import ValidationError
```

2. `__init__`: add `self.subsystem_capabilities: dict[str, dict] = {}` (near the other state fields).
3. In `set_mqtt_client`, add registrations after the existing ones:

```python
        client.register("capabilities/+", self._handle_mqtt_capabilities)
        client.register("telemetry/ice_maker/+", self._handle_mqtt_telemetry)
        client.register("cmd/ice_maker/ack", self._handle_mqtt_command_ack)
```

4. New handlers (place after `_handle_mqtt_ice_maker_event`):

```python
    async def _handle_mqtt_capabilities(self, topic: str, data: dict):
        """Store a subsystem's self-declared capabilities for the dashboard."""
        subsystem = data.get("subsystem") or topic.split("/")[-1]
        try:
            MonitorCapabilities.model_validate(data)
            logger.info(
                f"Capabilities registered for '{subsystem}' "
                f"(contract {data.get('contract_version')}, "
                f"{len(data.get('channels', []))} channels)"
            )
        except ValidationError:
            logger.warning(
                f"Capabilities for '{subsystem}' don't match the known schema; "
                "storing raw payload"
            )
        self.subsystem_capabilities[subsystem] = data

    async def _handle_mqtt_telemetry(self, topic: str, data: dict):
        """Route a generic telemetry channel reading into health tracking."""
        reading = ChannelReading.model_validate(data)
        if self._health_monitor:
            self._health_monitor.record_channel(reading.channel_id, reading.value)

    async def _handle_mqtt_command_ack(self, topic: str, data: dict):
        """Log command acknowledgements from the monitor."""
        ack = CommandAck.model_validate(data)
        detail = f" — {ack.detail}" if ack.detail else ""
        logger.info(
            f"Monitor ack: {ack.command} -> {ack.status}{detail} ({ack.request_id})"
        )
```

5. LWT handling — in `_handle_mqtt_heartbeat`, after computing `subsystem`, before `record_heartbeat`:

```python
            if data.get("uptime_seconds") == -1:
                logger.warning(
                    f"Subsystem '{subsystem}' reported OFFLINE (MQTT last will)"
                )
                self._health_monitor.mark_offline(subsystem)
                return
```

- [ ] **Step 5: Run tests, full suite, lint, commit**

Run: `uv run pytest tests/test_health_monitor.py tests/test_mqtt.py -q` — all pass.
Run: `uv run pytest -q` — full suite green.

```bash
git add services/health_monitor.py controller/vmc.py tests/test_health_monitor.py tests/test_mqtt.py
git commit -m "feat: VMC consumes monitor contract — capabilities, telemetry channels, acks, LWT offline

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Out of scope (per spec)

Broker TLS/credentials provisioning; HA discovery requirements for the monitor; dashboard UI beyond what `get_summary()` already renders; the brand-specific monitor implementation itself (other project — it pins CONTRACT.md + schemas).
