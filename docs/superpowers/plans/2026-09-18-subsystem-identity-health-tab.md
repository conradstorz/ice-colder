# Subsystem Identity on the Health Tab Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The System Health tab shows each subsystem's firmware, contract version, hardware identity, uptime and status, plus the VMC's own commit/build identity, and every simulator publishes the retained capabilities document that carries it.

**Architecture:** A shared `SubsystemCapabilities` model (generalized from the ice-maker contract) is published retained on `capabilities/<subsystem>` by every simulator; the VMC forwards it to the `HealthMonitor`, whose summary now carries identity fields per subsystem and a `vmc` block sourced from a new `services/build_info.py` (commit/build time baked into the image by CI, `git` fallback locally). The health fragment renders it all and lists expected-but-silent subsystems.

**Tech Stack:** Python 3.12, uv, Pydantic v2, aiomqtt, FastAPI + Jinja2, pytest asyncio auto mode, GitHub Actions + Docker buildx.

**Spec:** `docs/superpowers/specs/2026-09-18-subsystem-identity-health-tab-design.md`

## Global Constraints

- Run everything with `uv`: `uv run pytest`, `uv run python -m contracts.generate`. Never bare `pytest`/`pip`.
- Do NOT chain shell commands with `&&`; run separate commands.
- Lint/format before every commit: `ruff check --fix .` then `ruff format .`. If `ruff format` touches `tests/test_contract_schemas.py` or `tests/test_simulator_ice_maker.py` (known flip-flop), `git checkout --` those two before committing unless the task edits them.
- Baseline at plan time (branch `feat/subsystem-identity` off `main` 2873f1d): `uv run pytest -q` → 545 passed, 10 skipped (7 e2e without broker, 3 template stubs), 50 pre-existing warnings. Keep it green.
- Contract versions after this plan: ice-maker monitor **1.1.0**, vending machine **0.2.0**.
- `SubsystemCapabilities.subsystem` pattern `^[a-z0-9_]{1,32}$`; `EXPECTED_SUBSYSTEMS = ("vending", "mdb", "ice_maker")`.
- Build identity env vars: `ICE_COLDER_COMMIT`, `ICE_COLDER_BUILD_TIME`; Docker build args `VCS_REF`, `BUILD_TIME`; unknown values are the literal string `"unknown"`; `source` is one of `"image"`, `"git"`, `"unknown"`.
- Retained capabilities must never mark a subsystem alive; only heartbeats move `last_seen`.
- Unknown values on the health tab render as `—` (U+2014), never blank.
- Commit message trailer on every commit: `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`.

---

## File map

| File | Responsibility |
|---|---|
| `contracts/common.py` (new) | `ChannelDescriptor`, `_utc_now`, `CHANNEL_ID_PATTERN` shared by both contracts |
| `contracts/vending_machine.py` | `SubsystemCapabilities`, `EXPECTED_SUBSYSTEMS`, version 0.2.0 |
| `contracts/ice_maker_monitor.py` | `MonitorCapabilities(SubsystemCapabilities)`, version 1.1.0, re-exports `ChannelDescriptor` |
| `contracts/generate.py`, `docs/contracts/**` | schemas + contract docs |
| `services/build_info.py` (new) | `BuildInfo`, `resolve_build_info`, `BUILD_INFO` |
| `Dockerfile`, `.github/workflows/ci.yml` | bake commit/build time into the image |
| `services/mqtt_messages.py` | `VMCStatus.version` |
| `main.py` | log build identity; `HealthMonitor(machine_id=...)` |
| `simulators/base.py`, `simulators/vending_machine.py`, `simulators/mdb_gateway.py`, `simulators/ice_maker.py` | publish retained capabilities on connect |
| `services/health_monitor.py` | `record_capabilities`, identity fields in summary, `vmc` block, `empty_subsystem_row` |
| `controller/vmc.py` | forward capabilities to the health monitor |
| `web_interface/filters.py` (new), `web_interface/server.py`, `web_interface/routes.py`, `templates/partials/health_fragment.html` | health tab |
| `CLAUDE.md`, `README.md` | docs |

---

### Task 1: Shared capabilities model and contract bumps

**Files:**
- Create: `contracts/common.py`
- Modify: `contracts/vending_machine.py`, `contracts/ice_maker_monitor.py`, `contracts/generate.py`
- Modify: `docs/contracts/ice-maker-monitor/CONTRACT.md`, `docs/contracts/vending-machine/CONTRACT.md`, regenerated `docs/contracts/**/schemas/*.json`
- Test: `tests/test_contracts_vending.py`, `tests/test_contracts.py`, `tests/test_contract_schemas.py`

**Interfaces:**
- Produces: `contracts.common.ChannelDescriptor`, `contracts.common._utc_now`; `contracts.vending_machine.SubsystemCapabilities` (fields exactly as in Step 3), `contracts.vending_machine.EXPECTED_SUBSYSTEMS`, `contracts.vending_machine.CONTRACT_VERSION == "0.2.0"`; `contracts.ice_maker_monitor.MonitorCapabilities` (subclass), `contracts.ice_maker_monitor.CONTRACT_VERSION == "1.1.0"`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_contracts_vending.py`:

```python
from contracts.vending_machine import EXPECTED_SUBSYSTEMS, SubsystemCapabilities


class TestSubsystemCapabilities:
    def test_minimal(self):
        caps = SubsystemCapabilities(
            subsystem="vending", firmware="abc1234", contract_version="0.2.0"
        )
        assert caps.brand == "" and caps.model == ""
        assert caps.hardware_id is None and caps.ip is None
        assert caps.channels == [] and caps.commands == []

    def test_full(self):
        caps = SubsystemCapabilities(
            subsystem="mdb",
            firmware="abc1234",
            contract_version="0.2.0",
            brand="Acme",
            model="X1",
            hardware_id="02:11:22:33:44:55",
            ip="192.168.86.40",
            commands=["payment/enable", "refund"],
        )
        data = caps.model_dump(mode="json")
        assert data["hardware_id"] == "02:11:22:33:44:55"
        assert data["commands"] == ["payment/enable", "refund"]

    def test_subsystem_pattern(self):
        with pytest.raises(ValidationError):
            SubsystemCapabilities(
                subsystem="Bad Name", firmware="x", contract_version="0.2.0"
            )

    def test_contract_version_bumped(self):
        assert CONTRACT_VERSION == "0.2.0"

    def test_expected_subsystems(self):
        assert EXPECTED_SUBSYSTEMS == ("vending", "mdb", "ice_maker")
```

Update the existing `test_contract_version` in that file to expect `"0.2.0"`.

Append to `tests/test_contracts.py`:

```python
class TestMonitorCapabilitiesIdentity:
    def test_is_a_subsystem_capabilities(self):
        from contracts.vending_machine import SubsystemCapabilities

        assert issubclass(MonitorCapabilities, SubsystemCapabilities)

    def test_subsystem_fixed_to_ice_maker(self):
        caps = MonitorCapabilities(
            contract_version="1.1.0", brand="B", model="M", firmware="f"
        )
        assert caps.subsystem == "ice_maker"
        with pytest.raises(ValidationError):
            MonitorCapabilities(
                subsystem="vending",
                contract_version="1.1.0",
                brand="B",
                model="M",
                firmware="f",
            )

    def test_brand_model_still_required(self):
        with pytest.raises(ValidationError):
            MonitorCapabilities(contract_version="1.1.0", firmware="f")

    def test_identity_fields_optional_and_accepted(self):
        caps = MonitorCapabilities(
            contract_version="1.1.0",
            brand="B",
            model="M",
            firmware="f",
            hardware_id="02:aa:bb:cc:dd:ee",
            ip="10.0.0.5",
        )
        assert caps.hardware_id == "02:aa:bb:cc:dd:ee"

    def test_contract_version_is_1_1_0(self):
        from contracts.ice_maker_monitor import CONTRACT_VERSION

        assert CONTRACT_VERSION == "1.1.0"
```

(`MonitorCapabilities`, `pytest`, `ValidationError` are already imported at the top of that file.)

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_contracts_vending.py tests/test_contracts.py -q`
Expected: failures (`ImportError: SubsystemCapabilities`, version assertions).

- [ ] **Step 3: Create `contracts/common.py`**

```python
# contracts/common.py
"""Pieces shared by every contract module. Contracts import from here, never
from each other."""

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field

CHANNEL_ID_PATTERN = r"^[a-z0-9_]{1,64}$"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class ChannelDescriptor(BaseModel):
    """One telemetry channel a subsystem declares in its capabilities."""

    channel_id: str = Field(
        ..., pattern=CHANNEL_ID_PATTERN, description="Slug; also the topic segment"
    )
    kind: Literal["temperature", "current", "voltage", "level", "binary", "counter"]
    unit: str = Field("", description="Unit, e.g. 'C', 'A', '%'; empty for binary")
    description: str = Field("", description="Human-readable channel description")
    interval_seconds: float = Field(
        ..., gt=0, le=3600, description="Declared publish cadence"
    )
```

- [ ] **Step 4: Add `SubsystemCapabilities` to `contracts/vending_machine.py`**

Set `CONTRACT_VERSION = "0.2.0"`. Add to the imports `from contracts.common import ChannelDescriptor, _utc_now` and delete the module's own `_utc_now` definition (keep `datetime` import for field types). Append:

```python
class SubsystemCapabilities(BaseModel):
    """Retained self-description on capabilities/<subsystem>.

    Published on connect and whenever any declared property changes. A
    retained document never means the subsystem is alive — only heartbeats
    do — it means "this is what that board is, if and when it is up".
    """

    subsystem: str = Field(..., pattern=r"^[a-z0-9_]{1,32}$")
    firmware: str = Field(..., description="Software/firmware version string")
    contract_version: str = Field(..., description="Contract semver implemented")
    brand: str = Field("", description="Hardware brand, if meaningful")
    model: str = Field("", description="Hardware model, if meaningful")
    hardware_id: Optional[str] = Field(None, description="MAC or serial number")
    ip: Optional[str] = Field(None, description="IPv4/IPv6 address on the LAN")
    channels: list[ChannelDescriptor] = Field(default_factory=list)
    commands: list[str] = Field(default_factory=list)
    timestamp: datetime = Field(default_factory=_utc_now)


# Subsystems the dashboard always lists, even before they have ever spoken.
EXPECTED_SUBSYSTEMS: tuple[str, ...] = ("vending", "mdb", "ice_maker")
```

- [ ] **Step 5: Rebase `MonitorCapabilities` on it in `contracts/ice_maker_monitor.py`**

Set `CONTRACT_VERSION = "1.1.0"` and update the module docstring's version. Replace the local `_CHANNEL_ID_PATTERN`, `_utc_now`, and `ChannelDescriptor` definitions with:

```python
from contracts.common import CHANNEL_ID_PATTERN, ChannelDescriptor, _utc_now
from contracts.vending_machine import SubsystemCapabilities

_CHANNEL_ID_PATTERN = CHANNEL_ID_PATTERN  # kept for ChannelReading

__all__ = [
    "CONTRACT_VERSION",
    "ChannelDescriptor",
    "ChannelReading",
    "CommandAck",
    "MonitorCapabilities",
    "MonitorCommand",
]
```

Replace the `MonitorCapabilities` class with:

```python
class MonitorCapabilities(SubsystemCapabilities):
    """Retained self-description published on connect and on channel changes.

    The ice-maker contract fixes `subsystem` and requires brand/model; the
    optional `hardware_id`/`ip` are the 1.1.0 additions.
    """

    subsystem: Literal["ice_maker"] = "ice_maker"
    brand: str = Field(..., description="Ice maker brand the monitor targets")
    model: str = Field(..., description="Ice maker model")
```

Keep `ChannelReading`, `MonitorCommand`, `CommandAck` unchanged (they use `_CHANNEL_ID_PATTERN` and `_utc_now`, both still bound).

- [ ] **Step 6: Generator and docs**

In `contracts/generate.py` add `SubsystemCapabilities` to the `contracts.vending_machine` import and `"subsystem_capabilities": SubsystemCapabilities,` to `VENDING_MODELS`. Run:

```
uv run python -m contracts.generate
```

Expected: 13 `wrote` lines; `git status` shows `monitor_capabilities.schema.json` changed (two new properties) and `subsystem_capabilities.schema.json` new; `channel_descriptor.schema.json` unchanged.

`docs/contracts/ice-maker-monitor/CONTRACT.md`:
- Title line `# Ice Maker Monitor Contract — v1.0.0` → `v1.1.0`.
- After the `firmware` row of the `MonitorCapabilities` table add:

```markdown
| `hardware_id` | string \| null | default `null`; added in 1.1.0 | MAC address or serial number of the monitor board |
| `ip` | string \| null | default `null`; added in 1.1.0 | The monitor's LAN address |
```

- In "Identity & versioning" add a bullet: `- **1.1.0** (2026-09-18): \`MonitorCapabilities\` gains optional \`hardware_id\` and \`ip\`; the model is now the shared \`SubsystemCapabilities\` with \`subsystem\` fixed to \`ice_maker\`.`
- Conformance checklist: add `- [ ] SHOULD include \`hardware_id\` and \`ip\` in \`MonitorCapabilities\` so the dashboard can match a board to a row.`

`docs/contracts/vending-machine/CONTRACT.md`:
- Title `v0.1.0 (stub)` → `v0.2.0 (stub)`.
- Topic map row: `| \`capabilities/<subsystem>\` | subsystem → VMC | [\`SubsystemCapabilities\`](schemas/subsystem_capabilities.schema.json) | retained; MUST be published on connect and re-published on any change; \`firmware\`, \`hardware_id\`, \`ip\` identify the board |`
- Under "Semantics fixed": `- The VMC expects \`vending\`, \`mdb\` and \`ice_maker\` (\`EXPECTED_SUBSYSTEMS\`); a subsystem that has never published a heartbeat is shown as never seen.`

- [ ] **Step 7: Run tests**

Run: `uv run pytest tests/test_contracts_vending.py tests/test_contracts.py tests/test_contract_schemas.py tests/test_simulator_ice_maker.py tests/test_mqtt.py -q`
Expected: pass (existing importers of `ChannelDescriptor` from `contracts.ice_maker_monitor` still work through the re-export).

- [ ] **Step 8: Lint and commit**

```bash
ruff check --fix .
ruff format .
git add contracts docs/contracts tests/test_contracts_vending.py tests/test_contracts.py
git commit -m "feat(contracts): shared SubsystemCapabilities with hardware_id/ip; ice-maker 1.1.0, vending 0.2.0

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 2: Build identity

**Files:**
- Create: `services/build_info.py`, `tests/test_build_info.py`
- Modify: `Dockerfile`, `.github/workflows/ci.yml`, `main.py`, `services/mqtt_messages.py` (`VMCStatus`)
- Test: `tests/test_mqtt_messages_validation.py` (one assertion)

**Interfaces:**
- Produces: `services.build_info.BuildInfo(commit, commit_short, build_time, source)` frozen dataclass; `resolve_build_info(env: Mapping[str, str] | None = None, cwd: Path | None = None) -> BuildInfo`; module-level `BUILD_INFO`. `VMCStatus.version: str`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_build_info.py
"""Build identity: image env vars win, git checkout is the fallback, else unknown."""

import subprocess
from pathlib import Path

from services.build_info import BuildInfo, resolve_build_info


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def test_env_wins():
    info = resolve_build_info(
        env={"ICE_COLDER_COMMIT": "abcdef1234567890", "ICE_COLDER_BUILD_TIME": "2026-09-18T00:00:00Z"},
        cwd=Path("/nonexistent"),
    )
    assert info == BuildInfo(
        commit="abcdef1234567890",
        commit_short="abcdef1",
        build_time="2026-09-18T00:00:00Z",
        source="image",
    )


def test_env_commit_without_time():
    info = resolve_build_info(env={"ICE_COLDER_COMMIT": "abcdef1"}, cwd=Path("/nonexistent"))
    assert info.source == "image" and info.build_time == "unknown"


def test_git_fallback(tmp_path):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "x")
    sha = _git(tmp_path, "rev-parse", "HEAD")
    info = resolve_build_info(env={}, cwd=tmp_path)
    assert info.source == "git"
    assert info.commit == sha
    assert info.commit_short == sha[:7]
    assert info.build_time != "unknown"


def test_git_dirty_suffix(tmp_path):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "x")
    (tmp_path / "scratch.txt").write_text("x", encoding="utf-8")
    info = resolve_build_info(env={}, cwd=tmp_path)
    assert info.commit_short.endswith("-dirty")


def test_unknown_when_nothing(tmp_path):
    info = resolve_build_info(env={}, cwd=tmp_path)  # not a git repo
    assert info == BuildInfo("unknown", "unknown", "unknown", "unknown")


def test_vmc_status_carries_version():
    from services.build_info import BUILD_INFO
    from services.mqtt_messages import VMCStatus

    assert VMCStatus(state="idle").version == BUILD_INFO.commit_short
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_build_info.py -q`
Expected: `ModuleNotFoundError: services.build_info`.

- [ ] **Step 3: Implement `services/build_info.py`**

```python
# services/build_info.py
"""Which build is this? Resolved once at import, never raises.

1. ICE_COLDER_COMMIT / ICE_COLDER_BUILD_TIME env (set by the Dockerfile from
   CI build args)                                   -> source="image"
2. a git checkout in the repo root                   -> source="git"
3. otherwise                                         -> "unknown" everywhere
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

UNKNOWN = "unknown"
_REPO_ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class BuildInfo:
    commit: str
    commit_short: str
    build_time: str
    source: str  # "image" | "git" | "unknown"


def _run_git(cwd: Path, *args: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def resolve_build_info(
    env: Mapping[str, str] | None = None, cwd: Path | None = None
) -> BuildInfo:
    env = os.environ if env is None else env
    cwd = _REPO_ROOT if cwd is None else cwd

    commit = env.get("ICE_COLDER_COMMIT", "").strip()
    if commit:
        return BuildInfo(
            commit=commit,
            commit_short=commit[:7],
            build_time=env.get("ICE_COLDER_BUILD_TIME", "").strip() or UNKNOWN,
            source="image",
        )

    sha = _run_git(cwd, "rev-parse", "HEAD") if cwd.is_dir() else None
    if sha:
        dirty = bool(_run_git(cwd, "status", "--porcelain"))
        commit_time = _run_git(cwd, "log", "-1", "--format=%cI") or UNKNOWN
        return BuildInfo(
            commit=sha,
            commit_short=sha[:7] + ("-dirty" if dirty else ""),
            build_time=commit_time,
            source="git",
        )

    return BuildInfo(UNKNOWN, UNKNOWN, UNKNOWN, UNKNOWN)


BUILD_INFO = resolve_build_info()
```

- [ ] **Step 4: Wire it in**

`services/mqtt_messages.py`: add `from services.build_info import BUILD_INFO` and give `VMCStatus` the field `version: str = Field(default_factory=lambda: BUILD_INFO.commit_short, description="VMC build (short commit)")` after `uptime_seconds`.

`main.py`: add `from services.build_info import BUILD_INFO` and, right after `logger.info("Starting Vending Machine Controller")`, add:

```python
    logger.info(
        f"Build: {BUILD_INFO.commit_short} ({BUILD_INFO.source}, {BUILD_INFO.build_time})"
    )
```

`Dockerfile`: after `WORKDIR /app` add:

```dockerfile
# Build identity, passed by CI (see .github/workflows/ci.yml); "unknown" for
# an ad-hoc local build. Read by services/build_info.py.
ARG VCS_REF=unknown
ARG BUILD_TIME=unknown
ENV ICE_COLDER_COMMIT=$VCS_REF \
    ICE_COLDER_BUILD_TIME=$BUILD_TIME
```

`.github/workflows/ci.yml`, in the `image` job before the `docker/build-push-action@v6` step add:

```yaml
      - id: stamp
        run: echo "time=$(date -u +%Y-%m-%dT%H:%M:%SZ)" >> "$GITHUB_OUTPUT"
```

and inside the build-push step's `with:` add:

```yaml
          build-args: |
            VCS_REF=${{ github.sha }}
            BUILD_TIME=${{ steps.stamp.outputs.time }}
```

- [ ] **Step 5: Run tests**

Run: `uv run pytest tests/test_build_info.py tests/test_mqtt_messages_validation.py tests/test_mqtt.py tests/test_main_supervise.py -q`
Expected: pass. If a test compares a serialized `VMCStatus` to an exact dict, add `"version"`.

- [ ] **Step 6: Lint and commit**

```bash
ruff check --fix .
ruff format .
git add services/build_info.py tests/test_build_info.py Dockerfile .github/workflows/ci.yml main.py services/mqtt_messages.py tests/test_mqtt_messages_validation.py
git commit -m "feat: build identity (commit/build time) from image env or git; VMCStatus.version

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 3: Simulators publish retained capabilities

**Files:**
- Modify: `simulators/base.py`, `simulators/vending_machine.py`, `simulators/mdb_gateway.py`, `simulators/ice_maker.py`
- Test: `tests/test_simulator_base.py`, `tests/test_simulator_vending.py`, `tests/test_simulator_mdb.py`, `tests/test_simulator_ice_maker.py`

**Interfaces:**
- Consumes: Task 1 `SubsystemCapabilities`, `contracts.vending_machine.CONTRACT_VERSION`; Task 2 `BUILD_INFO`.
- Produces on `ESP32Simulator`: class attrs `CONTRACT_VERSION: str` (vending contract by default), `SUPPORTED_COMMANDS: list[str] = []`, `BRAND = ""`, `MODEL = ""`; `fake_hardware_id() -> str`; `container_ip() -> str | None`; `build_capabilities() -> SubsystemCapabilities`; `async _publish_capabilities(client)` called in `run()` right after HA discovery.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_simulator_base.py` (it defines `ConcreteSimulator(ESP32Simulator)` near the top; reuse it):

```python
from contracts.vending_machine import SubsystemCapabilities
from services.build_info import BUILD_INFO


class TestCapabilities:
    def test_default_capabilities_validate(self):
        sim = ConcreteSimulator(subsystem_name="test", machine_id="vmc-t")
        caps = sim.build_capabilities()
        assert isinstance(caps, SubsystemCapabilities)
        assert caps.subsystem == "test"
        assert caps.firmware == BUILD_INFO.commit_short
        assert caps.contract_version == "0.2.0"
        assert caps.hardware_id is not None

    def test_hardware_id_is_stable_and_distinct(self):
        a = ConcreteSimulator(subsystem_name="test", machine_id="vmc-t")
        b = ConcreteSimulator(subsystem_name="test", machine_id="vmc-t")
        c = ConcreteSimulator(subsystem_name="other", machine_id="vmc-t")
        assert a.fake_hardware_id() == b.fake_hardware_id()
        assert a.fake_hardware_id() != c.fake_hardware_id()
        assert a.fake_hardware_id().startswith("02:")
        assert len(a.fake_hardware_id()) == 17

    async def test_publish_capabilities_is_retained(self):
        sim = ConcreteSimulator(subsystem_name="test", machine_id="vmc-t")
        sim.publish = AsyncMock()
        await sim._publish_capabilities(None)
        sim.publish.assert_awaited_once()
        args, kwargs = sim.publish.await_args
        assert args[1] == "capabilities/test"
        assert isinstance(args[2], SubsystemCapabilities)
        assert kwargs.get("retain") is True
```

Append to `tests/test_simulator_vending.py`:

```python
class TestVendingCapabilities:
    def test_commands_and_contract(self):
        caps = _make_sim().build_capabilities()
        assert caps.subsystem == "vending"
        assert caps.commands == ["dispense", "payment/enable"]
        assert caps.contract_version == "0.2.0"
```

Append to `tests/test_simulator_mdb.py`:

```python
class TestMDBCapabilities:
    def test_commands_and_contract(self):
        caps = MDBGatewaySimulator().build_capabilities()
        assert caps.subsystem == "mdb"
        assert caps.commands == ["payment/enable", "refund"]
        assert caps.contract_version == "0.2.0"
```

Append to `tests/test_simulator_ice_maker.py` (there is a `_sim()` helper in the file; if it is a method on a class, add this class at module level with its own helper mirroring it):

```python
class TestIceMakerCapabilitiesIdentity:
    def test_identity_fields_present(self):
        from contracts.ice_maker_monitor import CONTRACT_VERSION, MonitorCapabilities
        from services.build_info import BUILD_INFO
        from simulators.ice_maker import IceMakerSimulator

        caps = IceMakerSimulator(machine_id="vmc-t").build_capabilities()
        assert isinstance(caps, MonitorCapabilities)
        assert caps.contract_version == CONTRACT_VERSION == "1.1.0"
        assert caps.firmware == BUILD_INFO.commit_short
        assert caps.hardware_id is not None
        assert caps.commands == ["power_cycle", "force_report", "set_interval"]
```

(Check the actual simulator class name in `simulators/ice_maker.py` and use it.)

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_simulator_base.py::TestCapabilities tests/test_simulator_vending.py::TestVendingCapabilities tests/test_simulator_mdb.py::TestMDBCapabilities tests/test_simulator_ice_maker.py::TestIceMakerCapabilitiesIdentity -q`
Expected: `AttributeError: build_capabilities` etc. (the ice-maker test fails on `hardware_id`/`firmware`).

- [ ] **Step 3: Base class**

In `simulators/base.py` add imports `import hashlib`, `import socket`, `from contracts.vending_machine import CONTRACT_VERSION as VENDING_CONTRACT_VERSION, SubsystemCapabilities`, `from services.build_info import BUILD_INFO`. On `ESP32Simulator` add class attributes after `HEARTBEAT_INTERVAL`:

```python
    CONTRACT_VERSION = VENDING_CONTRACT_VERSION  # ice maker overrides
    SUPPORTED_COMMANDS: list[str] = []
    BRAND = ""
    MODEL = ""
```

and these methods after `_build_will`:

```python
    def fake_hardware_id(self) -> str:
        """Stable locally-administered MAC derived from machine id + subsystem."""
        digest = hashlib.sha1(
            f"{self.machine_id}/{self.subsystem_name}".encode()
        ).digest()
        return "02:" + ":".join(f"{b:02x}" for b in digest[:5])

    @staticmethod
    def container_ip() -> str | None:
        try:
            return socket.gethostbyname(socket.gethostname())
        except OSError:
            return None

    def build_capabilities(self) -> SubsystemCapabilities:
        """Retained self-description; subclasses override to add channels etc."""
        return SubsystemCapabilities(
            subsystem=self.subsystem_name,
            firmware=BUILD_INFO.commit_short,
            contract_version=self.CONTRACT_VERSION,
            brand=self.BRAND,
            model=self.MODEL,
            hardware_id=self.fake_hardware_id(),
            ip=self.container_ip(),
            commands=list(self.SUPPORTED_COMMANDS),
        )

    async def _publish_capabilities(self, client: aiomqtt.Client) -> None:
        caps = self.build_capabilities()
        await self.publish(
            client, f"capabilities/{self.subsystem_name}", caps, retain=True
        )
        logger.info(
            f"[{self.subsystem_name}] Capabilities published "
            f"(firmware {caps.firmware}, contract {caps.contract_version})"
        )
```

In `run()`, after `await self._publish_ha_discovery(client)` add `await self._publish_capabilities(client)`.

- [ ] **Step 4: Subclasses**

`simulators/vending_machine.py`, on `VendingMachineSimulator` next to `IDLE_MIN`: `SUPPORTED_COMMANDS = ["dispense", "payment/enable"]`, `BRAND = "ice-colder"`, `MODEL = "vending-sim"`.

`simulators/mdb_gateway.py`, on `MDBGatewaySimulator` next to `DEVICE_STATUS_INTERVAL`: `SUPPORTED_COMMANDS = ["payment/enable", "refund"]`, `BRAND = "ice-colder"`, `MODEL = "mdb-sim"`.

`simulators/ice_maker.py`: add `from services.build_info import BUILD_INFO`; on the class add `CONTRACT_VERSION = CONTRACT_VERSION` is unnecessary (it already imports the ice-maker `CONTRACT_VERSION`); change `build_capabilities` to:

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
            firmware=BUILD_INFO.commit_short,
            hardware_id=self.fake_hardware_id(),
            ip=self.container_ip(),
            channels=temp_channels + TELEMETRY_CHANNELS,
            commands=["power_cycle", "force_report", "set_interval"],
        )
```

In its `run_simulation`, remove the connect-time `await self.publish(client, "capabilities/ice_maker", self.build_capabilities(), retain=True)` (the base now does it); keep the re-publish after `set_interval`.

- [ ] **Step 5: Run tests**

Run: `uv run pytest tests/test_simulator_base.py tests/test_simulator_vending.py tests/test_simulator_mdb.py tests/test_simulator_ice_maker.py -q`
Expected: pass. If an ice-maker test counted capabilities publishes on connect, adjust it to the base-class publish (topic and retain unchanged).

- [ ] **Step 6: Lint and commit**

```bash
ruff check --fix .
ruff format .
git add simulators tests/test_simulator_base.py tests/test_simulator_vending.py tests/test_simulator_mdb.py tests/test_simulator_ice_maker.py
git commit -m "feat(simulators): every subsystem publishes retained capabilities with firmware, hardware_id, ip

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 4: Health monitor identity

**Files:**
- Modify: `services/health_monitor.py`
- Test: `tests/test_health_monitor.py`

**Interfaces:**
- Consumes: Task 2 `BUILD_INFO`.
- Produces: `HealthMonitor(..., machine_id: str | None = None, started_at: float | None = None)`; `record_capabilities(subsystem: str, caps: dict) -> None`; `SubsystemStatus.capabilities: dict`, `.capabilities_at: float`; `HealthMonitor.empty_subsystem_row() -> dict` (static); `get_summary()["subsystems"][name]` keys `alive, seconds_since_seen, stale, uptime_seconds, firmware, contract_version, brand, model, hardware_id, ip, channel_count, commands, capabilities_age_seconds`; `get_summary()["vmc"]` keys `commit, commit_short, build_time, source, uptime_seconds, python_version, machine_id`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_health_monitor.py`:

```python
class TestSubsystemIdentity:
    def _caps(self, **over):
        base = {
            "subsystem": "vending",
            "firmware": "abc1234",
            "contract_version": "0.2.0",
            "brand": "ice-colder",
            "model": "vending-sim",
            "hardware_id": "02:11:22:33:44:55",
            "ip": "172.18.0.5",
            "channels": [],
            "commands": ["dispense"],
        }
        base.update(over)
        return base

    def test_capabilities_before_heartbeat_is_never_seen(self):
        hm = HealthMonitor()
        hm.record_capabilities("vending", self._caps())
        row = hm.get_summary()["subsystems"]["vending"]
        assert row["alive"] is False
        assert row["seconds_since_seen"] == float("inf")
        assert row["firmware"] == "abc1234"
        assert row["hardware_id"] == "02:11:22:33:44:55"
        assert row["uptime_seconds"] is None

    def test_heartbeat_gives_uptime_and_alive(self):
        hm = HealthMonitor()
        hm.record_heartbeat("vending", {"subsystem": "vending", "uptime_seconds": 321})
        row = hm.get_summary()["subsystems"]["vending"]
        assert row["alive"] is True
        assert row["uptime_seconds"] == 321
        assert row["firmware"] is None
        assert row["commands"] == []
        assert row["channel_count"] == 0

    def test_lwt_uptime_is_none(self):
        hm = HealthMonitor()
        hm.record_heartbeat("mdb", {"subsystem": "mdb", "uptime_seconds": -1})
        assert hm.get_summary()["subsystems"]["mdb"]["uptime_seconds"] is None

    def test_capabilities_age_and_channel_count(self, monkeypatch):
        import time as _time

        hm = HealthMonitor()
        t = [500.0]
        monkeypatch.setattr(_time, "monotonic", lambda: t[0])
        hm.record_capabilities(
            "ice_maker",
            self._caps(subsystem="ice_maker", channels=[{"channel_id": "a"}, {"channel_id": "b"}]),
        )
        t[0] = 545.0
        row = hm.get_summary()["subsystems"]["ice_maker"]
        assert row["channel_count"] == 2
        assert row["capabilities_age_seconds"] == 45.0

    def test_empty_row_shape(self):
        row = HealthMonitor.empty_subsystem_row()
        assert row["alive"] is False and row["stale"] is False
        assert row["firmware"] is None and row["commands"] == []

    def test_vmc_block(self, monkeypatch):
        import time as _time

        from services.build_info import BUILD_INFO

        t = [1000.0]
        monkeypatch.setattr(_time, "monotonic", lambda: t[0])
        hm = HealthMonitor(machine_id="vmc-0000")
        t[0] = 1060.0
        vmc = hm.get_summary()["vmc"]
        assert vmc["commit_short"] == BUILD_INFO.commit_short
        assert vmc["source"] == BUILD_INFO.source
        assert vmc["uptime_seconds"] == 60
        assert vmc["machine_id"] == "vmc-0000"
        assert vmc["python_version"].count(".") == 2
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_health_monitor.py::TestSubsystemIdentity -q`
Expected: `AttributeError: record_capabilities` etc.

- [ ] **Step 3: Implement**

`services/health_monitor.py`: add `import platform` and `from services.build_info import BUILD_INFO`.

`SubsystemStatus` gains two fields after `last_payload`:

```python
    capabilities: dict = field(default_factory=dict)
    capabilities_at: float = 0.0  # monotonic; 0.0 = never received
```

Constructor: add parameters `machine_id: str | None = None, started_at: float | None = None` and store `self._machine_id = machine_id`, `self._started_at = time.monotonic() if started_at is None else started_at`.

Add after `record_heartbeat`:

```python
    def record_capabilities(self, subsystem: str, caps: dict):
        """Store a subsystem's retained self-description. Never touches
        last_seen: a retained document says what the board is, not that it
        is up."""
        if subsystem not in self._subsystems:
            self._subsystems[subsystem] = SubsystemStatus(name=subsystem)
            logger.info(f"Health: New subsystem registered (capabilities): {subsystem}")
        status = self._subsystems[subsystem]
        status.capabilities = dict(caps)
        status.capabilities_at = time.monotonic()

    @staticmethod
    def empty_subsystem_row() -> dict:
        """The dashboard row for a subsystem that has never been heard from."""
        return {
            "alive": False,
            "seconds_since_seen": float("inf"),
            "stale": False,
            "uptime_seconds": None,
            "firmware": None,
            "contract_version": None,
            "brand": None,
            "model": None,
            "hardware_id": None,
            "ip": None,
            "channel_count": 0,
            "commands": [],
            "capabilities_age_seconds": None,
        }
```

In `get_summary`, replace the subsystems loop with:

```python
        now = time.monotonic()
        subsystems = {}
        for name, sub in self._subsystems.items():
            row = self.empty_subsystem_row()
            uptime = sub.last_payload.get("uptime_seconds")
            caps = sub.capabilities
            row.update(
                {
                    "alive": sub.alive,
                    "seconds_since_seen": round(sub.seconds_since_seen, 1),
                    "stale": sub.seconds_since_seen > self._subsystem_timeout,
                    "uptime_seconds": (
                        int(uptime)
                        if sub.alive and isinstance(uptime, (int, float)) and uptime >= 0
                        else None
                    ),
                    "firmware": caps.get("firmware"),
                    "contract_version": caps.get("contract_version"),
                    "brand": caps.get("brand") or None,
                    "model": caps.get("model") or None,
                    "hardware_id": caps.get("hardware_id"),
                    "ip": caps.get("ip"),
                    "channel_count": len(caps.get("channels") or []),
                    "commands": list(caps.get("commands") or []),
                    "capabilities_age_seconds": (
                        round(now - sub.capabilities_at, 1)
                        if sub.capabilities_at
                        else None
                    ),
                }
            )
            subsystems[name] = row
```

(`round(float("inf"), 1)` is `inf`, which the existing tests already accept.) Reuse that `now` for the `active_faults` block below it (delete the second `now = time.monotonic()`), and add to the returned dict:

```python
            "vmc": {
                "commit": BUILD_INFO.commit,
                "commit_short": BUILD_INFO.commit_short,
                "build_time": BUILD_INFO.build_time,
                "source": BUILD_INFO.source,
                "uptime_seconds": int(now - self._started_at),
                "python_version": platform.python_version(),
                "machine_id": self._machine_id,
            },
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_health_monitor.py tests/test_web_routes.py -q`
Expected: pass. `test_empty_summary`, if it compares the whole dict, needs the `vmc` block added with `BUILD_INFO` values and `uptime_seconds` ignored or patched.

- [ ] **Step 5: Lint and commit**

```bash
ruff check --fix .
ruff format .
git add services/health_monitor.py tests/test_health_monitor.py
git commit -m "feat(health): per-subsystem identity from capabilities, uptime from heartbeat, VMC build block

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 5: VMC forwards capabilities; main wiring

**Files:**
- Modify: `controller/vmc.py` (`_handle_mqtt_capabilities` ~line 528, imports ~line 19), `main.py` (~line 211)
- Test: `tests/test_mqtt.py`

**Interfaces:**
- Consumes: Task 1 `SubsystemCapabilities`; Task 4 `record_capabilities`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_mqtt.py` inside the class that holds `test_capabilities_stored` (look at its `vmc` construction and reuse it):

```python
    async def test_capabilities_forwarded_to_health_monitor(self):
        from services.health_monitor import HealthMonitor

        vmc = VMC(config=ConfigModel())
        hm = HealthMonitor()
        vmc.set_health_monitor(hm)
        await vmc._handle_mqtt_capabilities(
            "capabilities/vending",
            {
                "subsystem": "vending",
                "firmware": "abc1234",
                "contract_version": "0.2.0",
                "hardware_id": "02:11:22:33:44:55",
                "future_field": "ignored",
            },
        )
        row = hm.get_summary()["subsystems"]["vending"]
        assert row["firmware"] == "abc1234"
        assert row["hardware_id"] == "02:11:22:33:44:55"
        assert row["alive"] is False

    async def test_malformed_capabilities_still_forwarded_raw(self):
        from services.health_monitor import HealthMonitor

        vmc = VMC(config=ConfigModel())
        hm = HealthMonitor()
        vmc.set_health_monitor(hm)
        await vmc._handle_mqtt_capabilities(
            "capabilities/mdb", {"subsystem": "mdb", "whatever": 1}
        )
        assert hm.get_summary()["subsystems"]["mdb"]["firmware"] is None
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_mqtt.py -q -k capabilities`
Expected: the two new tests fail (`KeyError: 'vending'`).

- [ ] **Step 3: Implement**

`controller/vmc.py`: change the import line `from contracts.ice_maker_monitor import ChannelReading, CommandAck, MonitorCapabilities` to `from contracts.ice_maker_monitor import ChannelReading, CommandAck` and add `SubsystemCapabilities` to the existing `from contracts.vending_machine import (...)` block. Replace `_handle_mqtt_capabilities`:

```python
    async def _handle_mqtt_capabilities(self, topic: str, data: dict):
        """Store a subsystem's retained self-description and hand it to health."""
        subsystem = data.get("subsystem") or topic.split("/")[-1]
        try:
            caps = SubsystemCapabilities.model_validate(data)
            logger.info(
                f"Capabilities registered for '{subsystem}' "
                f"(firmware {caps.firmware}, contract {caps.contract_version}, "
                f"{len(caps.channels)} channels)"
            )
        except ValidationError:
            logger.warning(
                f"Capabilities for '{subsystem}' don't match the known schema; "
                "storing raw payload"
            )
        self.subsystem_capabilities[subsystem] = data
        if self._health_monitor:
            self._health_monitor.record_capabilities(subsystem, data)
```

`main.py`: `health = HealthMonitor()` → `health = HealthMonitor(machine_id=live_config.machine_id)`.

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_mqtt.py tests/test_main_supervise.py tests/test_vmc_flows.py -q`
Expected: pass.

- [ ] **Step 5: Lint and commit**

```bash
ruff check --fix .
ruff format .
git add controller/vmc.py main.py tests/test_mqtt.py
git commit -m "feat(vmc): forward subsystem capabilities to the health monitor

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 6: Health tab

**Files:**
- Create: `web_interface/filters.py`
- Modify: `web_interface/server.py`, `web_interface/routes.py` (`/health` ~line 231), `web_interface/templates/partials/health_fragment.html`
- Test: `tests/test_web_routes.py`, `tests/test_filters.py` (new)

**Interfaces:**
- Consumes: Task 4 summary shape and `HealthMonitor.empty_subsystem_row()`; Task 1 `EXPECTED_SUBSYSTEMS`.
- Produces: `web_interface.filters.humanize_seconds(value) -> str`; Jinja filter `humanize_seconds`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_filters.py
import pytest

from web_interface.filters import humanize_seconds


@pytest.mark.parametrize(
    "value,expected",
    [
        (None, "—"),
        (float("inf"), "—"),
        (0, "0s"),
        (45, "45s"),
        (59.9, "59s"),
        (60, "1m"),
        (754, "12m"),
        (3600, "1h 00m"),
        (10_980, "3h 03m"),
        (190_800, "2d 5h"),
    ],
)
def test_humanize_seconds(value, expected):
    assert humanize_seconds(value) == expected
```

Append to `tests/test_web_routes.py`:

```python
class TestHealthTabIdentity:
    def _hm(self):
        from services.health_monitor import HealthMonitor

        hm = HealthMonitor(machine_id="vmc-test")
        routes.set_health_monitor(hm)
        return hm

    def test_vmc_row(self, client):
        from services.build_info import BUILD_INFO

        self._hm()
        try:
            r = client.get("/health", auth=client.auth)
            assert r.status_code == 200
            assert BUILD_INFO.commit_short in r.text
            assert BUILD_INFO.source in r.text
            assert "vmc-test" in r.text
        finally:
            routes.set_health_monitor(None)

    def test_expected_subsystems_listed_when_silent(self, client):
        self._hm()
        try:
            r = client.get("/health", auth=client.auth)
            for name in ("vending", "mdb", "ice_maker"):
                assert name in r.text
            assert r.text.count("Never seen") >= 3
        finally:
            routes.set_health_monitor(None)

    def test_heartbeat_only_row_shows_dashes(self, client):
        hm = self._hm()
        try:
            hm.record_heartbeat("vending", {"subsystem": "vending", "uptime_seconds": 90})
            r = client.get("/health", auth=client.auth)
            assert "1m" in r.text  # uptime humanized
            assert "—" in r.text  # firmware/contract/hardware unknown
        finally:
            routes.set_health_monitor(None)

    def test_capabilities_render(self, client):
        hm = self._hm()
        try:
            hm.record_heartbeat("mdb", {"subsystem": "mdb", "uptime_seconds": 5})
            hm.record_capabilities(
                "mdb",
                {
                    "subsystem": "mdb",
                    "firmware": "abc1234",
                    "contract_version": "0.2.0",
                    "brand": "ice-colder",
                    "model": "mdb-sim",
                    "hardware_id": "02:11:22:33:44:55",
                    "ip": "172.18.0.7",
                    "commands": ["refund"],
                },
            )
            r = client.get("/health", auth=client.auth)
            assert "abc1234" in r.text
            assert "0.2.0" in r.text
            assert "ice-colder mdb-sim" in r.text
            assert "02:11:22:33:44:55" in r.text
            assert "172.18.0.7" in r.text
            assert "refund" in r.text  # in the row title
        finally:
            routes.set_health_monitor(None)
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_filters.py tests/test_web_routes.py::TestHealthTabIdentity -q`
Expected: `ModuleNotFoundError: web_interface.filters`; route tests fail on missing text.

- [ ] **Step 3: Filter and registration**

```python
# web_interface/filters.py
"""Jinja filters for the dashboard."""

import math


def humanize_seconds(value) -> str:
    """45s, 12m, 3h 03m, 2d 5h; '—' for None/inf/negative."""
    if value is None:
        return "—"
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return "—"
    if math.isinf(seconds) or math.isnan(seconds) or seconds < 0:
        return "—"
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    minutes, _ = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h {minutes:02d}m"
    days, hours = divmod(hours, 24)
    return f"{days}d {hours}h"
```

`web_interface/server.py`: after `templates = Jinja2Templates(...)` add:

```python
from .filters import humanize_seconds  # noqa: E402  (needs `templates` above)

templates.env.filters["humanize_seconds"] = humanize_seconds
```

(If ruff complains about E402 even with the noqa, move the import to the top with the other imports; it has no dependency on `templates`.)

- [ ] **Step 4: Route**

In `web_interface/routes.py` add `from contracts.vending_machine import EXPECTED_SUBSYSTEMS` and `from services.health_monitor import HealthMonitor` to the imports, and replace the `/health` handler body:

```python
    @router.get("/health", response_class=HTMLResponse)
    async def health_summary(request: Request):
        if not health_monitor:
            return HTMLResponse("<div>Health monitor not initialized</div>")
        health = health_monitor.get_summary()
        for name in EXPECTED_SUBSYSTEMS:
            health["subsystems"].setdefault(name, HealthMonitor.empty_subsystem_row())
        return templates.TemplateResponse(
            "partials/health_fragment.html",
            {"request": request, "health": health},
        )
```

- [ ] **Step 5: Template**

In `health_fragment.html`, after the `VMC State` block and before `{% if health.subsystems %}`, insert:

```html
  {% if health.vmc %}
  <div class="rounded-lg border border-gray-200 bg-gray-50 p-3 text-sm">
    <div class="flex flex-wrap items-baseline gap-x-6 gap-y-1">
      <div>
        <span class="text-xs text-gray-400 uppercase tracking-wide">VMC build</span>
        <span class="ml-2 font-mono text-gray-900" title="{{ health.vmc.commit }}">{{ health.vmc.commit_short }}</span>
        <span class="ml-1 text-xs px-1.5 py-0.5 rounded bg-gray-200 text-gray-600">{{ health.vmc.source }}</span>
      </div>
      <div><span class="text-xs text-gray-400 uppercase tracking-wide">Built</span> <span class="ml-2 text-gray-700">{{ health.vmc.build_time }}</span></div>
      <div><span class="text-xs text-gray-400 uppercase tracking-wide">Uptime</span> <span class="ml-2 text-gray-700">{{ health.vmc.uptime_seconds|humanize_seconds }}</span></div>
      <div><span class="text-xs text-gray-400 uppercase tracking-wide">Python</span> <span class="ml-2 text-gray-700">{{ health.vmc.python_version }}</span></div>
      <div><span class="text-xs text-gray-400 uppercase tracking-wide">Machine</span> <span class="ml-2 font-mono text-gray-700">{{ health.vmc.machine_id or "—" }}</span></div>
    </div>
  </div>
  {% endif %}
```

Replace the subsystems table header and row with:

```html
      <thead>
        <tr class="border-b border-gray-200 text-xs text-gray-400 uppercase tracking-wide">
          <th class="pb-2 text-left font-medium">Name</th>
          <th class="pb-2 text-left font-medium">Status</th>
          <th class="pb-2 text-left font-medium">Last Seen</th>
          <th class="pb-2 text-left font-medium">Uptime</th>
          <th class="pb-2 text-left font-medium">Firmware</th>
          <th class="pb-2 text-left font-medium">Contract</th>
          <th class="pb-2 text-left font-medium">Hardware</th>
        </tr>
      </thead>
      <tbody class="divide-y divide-gray-100">
        {% for name, sub in health.subsystems.items() %}
        <tr title="channels: {{ sub.channel_count }}; commands: {{ sub.commands|join(', ') if sub.commands else '—' }}">
          <td class="py-2.5 text-gray-700 font-mono text-xs">{{ name }}</td>
          <td class="py-2.5">
            {% if sub.stale %}
              <span class="text-red-600 font-medium">Stale</span>
            {% elif sub.alive %}
              <span class="text-green-600 font-medium">OK</span>
            {% else %}
              <span class="text-gray-400">Never seen</span>
            {% endif %}
          </td>
          <td class="py-2.5 text-gray-500">{% if sub.alive %}{{ sub.seconds_since_seen|humanize_seconds }} ago{% else %}—{% endif %}</td>
          <td class="py-2.5 text-gray-700">{{ sub.uptime_seconds|humanize_seconds }}</td>
          <td class="py-2.5 font-mono text-xs text-gray-700">{{ sub.firmware or "—" }}</td>
          <td class="py-2.5 font-mono text-xs text-gray-700">{{ sub.contract_version or "—" }}</td>
          <td class="py-2.5 text-gray-700">
            {% if sub.brand or sub.model %}{{ sub.brand or "" }} {{ sub.model or "" }}{% else %}—{% endif %}
            <div class="text-xs text-gray-400 font-mono">{{ sub.hardware_id or "—" }} · {{ sub.ip or "—" }}</div>
          </td>
        </tr>
        {% endfor %}
      </tbody>
```

Remove the `{% if not health.subsystems and not health.temperatures %}` empty-state block's dependence on subsystems (with expected rows always present it would never show): change its condition to `{% if not health.temperatures %}` and its text to "No temperature readings yet."

- [ ] **Step 6: Run tests**

Run: `uv run pytest tests/test_filters.py tests/test_web_routes.py -q`
Expected: pass. Existing `/health` tests that asserted "No subsystems reporting yet" need the new text.

- [ ] **Step 7: Lint and commit**

```bash
ruff check --fix .
ruff format .
git add web_interface tests/test_filters.py tests/test_web_routes.py
git commit -m "feat(dashboard): health tab shows VMC build and per-subsystem firmware, contract, hardware, uptime

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 7: Docs

**Files:**
- Modify: `CLAUDE.md` (Web Dashboard section), `README.md` (Docker section)

- [ ] **Step 1: CLAUDE.md**

After the Web Dashboard paragraph add:

```markdown
The System Health tab (`/health`) merges three sources: heartbeats (liveness,
uptime), each subsystem's retained `capabilities/<subsystem>` document
(`SubsystemCapabilities`: firmware, contract version, brand/model,
hardware_id, ip), and the VMC's own build identity from
`services/build_info.py` (image env vars set by CI, or `git` when run from a
checkout). Subsystems in `EXPECTED_SUBSYSTEMS` are listed even before they speak.
```

- [ ] **Step 2: README.md**

In the Docker section, after the `--build` note add:

```markdown
A local `--build` shows `unknown` as the VMC build on the health tab unless you
pass the identity args CI uses:
```
docker compose build --build-arg VCS_REF=$(git rev-parse HEAD) --build-arg BUILD_TIME=$(date -u +%Y-%m-%dT%H:%M:%SZ)
```
```

- [ ] **Step 3: Full suite, lint, commit**

Run: `uv run pytest -q`
Expected: all pass (10 skipped as before).

```bash
ruff check --fix .
ruff format .
git add CLAUDE.md README.md
git commit -m "docs: health tab sources and local build identity args

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

## Self-review notes

- Spec §1 → Task 1; §2 → Task 2; §3 → Task 3; §4 → Tasks 4–5; §5 → Task 6; §6 tests spread across tasks; §7 → Task 7.
- `EXPECTED_SUBSYSTEMS`, `SubsystemCapabilities`, `record_capabilities`, `empty_subsystem_row`, `humanize_seconds`, `BUILD_INFO` are named identically everywhere they appear.
- Task 3 removes the ice-maker simulator's own connect-time capabilities publish because the base class now does it; the `set_interval` re-publish stays, so contract behavior is unchanged.
