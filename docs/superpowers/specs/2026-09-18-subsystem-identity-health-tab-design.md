# Subsystem Identity on the Health Tab — Design

**Date:** 2026-09-18
**Status:** Approved (brainstormed with owner)

## Goal

The dashboard's System Health tab shows everything the VMC knows about each
subsystem, including what software each one is running, and shows the VMC's
own build identity. Subsystems that do not report a version today start
doing so through the same retained capabilities document the ice maker
already publishes.

## Why

- No component reports a version. The VMC has a static `0.1.0` in
  `pyproject.toml` nothing reads; heartbeats carry only name and uptime; only
  the ice-maker capabilities document carries `firmware`, and the VMC stores
  it without showing it.
- Watchtower now deploys `sha-<commit>` images unattended. Without the commit
  on the dashboard there is no way to tell which build a machine is running.
- An absent board is currently invisible (the table only lists subsystems
  that have spoken).

## Decisions

1. **VMC version = git commit + build time**, baked into the image by CI, with
   a `git describe` fallback when running from a checkout. No hand-bumped
   semver.
2. **Subsystem versions come from a retained `capabilities/<subsystem>`
   document**, generalized from the ice-maker contract. Heartbeats are
   unchanged.
3. **Information set per subsystem**: status, last seen, uptime, firmware,
   contract version, brand/model, hardware id, IP, channel count, commands.
   Plus a VMC row: commit, build time, uptime, Python version, machine id.
4. **One shared model** (`SubsystemCapabilities`); the ice-maker contract's
   `MonitorCapabilities` becomes that model with `subsystem` fixed. Minor bump
   to ice-maker contract **1.1.0** (additive optional fields) and vending
   contract **0.2.0**.

## Non-goals

- Semantic versioning or release tagging of the VMC.
- Adding version data to heartbeats.
- Real ESP32 firmware (this slice defines what it must publish; the
  simulators are the reference implementation).
- Any change to alerts. Version mismatch alerts may come later.

## 1. Contract models

### `contracts/common.py` (new)

`ChannelDescriptor` and `_utc_now` move here so both contracts can import
them without importing each other. `contracts/ice_maker_monitor.py` re-exports
`ChannelDescriptor` so existing imports keep working.

### `contracts/vending_machine.py` — `CONTRACT_VERSION = "0.2.0"`

```python
class SubsystemCapabilities(BaseModel):
    """Retained self-description on capabilities/<subsystem>; published on
    connect and whenever any declared property changes."""

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


EXPECTED_SUBSYSTEMS: tuple[str, ...] = ("vending", "mdb", "ice_maker")
```

`EXPECTED_SUBSYSTEMS` is what the dashboard lists even when silent.

### `contracts/ice_maker_monitor.py` — `CONTRACT_VERSION = "1.1.0"`

```python
class MonitorCapabilities(SubsystemCapabilities):
    subsystem: Literal["ice_maker"] = "ice_maker"
```

`brand`, `model`, `firmware`, `contract_version` keep their required-ness as
before for the ice maker (override `brand` and `model` as required fields in
the subclass so the 1.0.0 guarantees hold). `hardware_id` and `ip` are the
additive 1.1.0 fields.

### Docs and schemas

- `docs/contracts/ice-maker-monitor/CONTRACT.md`: version 1.1.0; the two new
  optional fields in the `MonitorCapabilities` table; conformance checklist
  line: "SHOULD publish `hardware_id` and `ip`".
- `docs/contracts/vending-machine/CONTRACT.md`: version 0.2.0; new topic row
  `capabilities/<subsystem>` (subsystem → VMC, retained, `SubsystemCapabilities`,
  MUST on connect; MUST re-publish on any change); `EXPECTED_SUBSYSTEMS`
  listed.
- `contracts/generate.py`: `subsystem_capabilities` added to `VENDING_MODELS`;
  ice-maker schemas regenerated (`monitor_capabilities` gains the two
  fields). Drift test covers both.

## 2. Build identity — `services/build_info.py` (new)

```python
@dataclass(frozen=True)
class BuildInfo:
    commit: str          # full sha or "unknown"
    commit_short: str    # 7 chars or "unknown"
    build_time: str      # ISO-8601 UTC or "unknown"
    source: str          # "image" | "git" | "unknown"

def resolve_build_info() -> BuildInfo: ...
BUILD_INFO = resolve_build_info()
```

Resolution, in order, never raising:

1. `ICE_COLDER_COMMIT` env set → `source="image"`, `build_time` from
   `ICE_COLDER_BUILD_TIME` (or `"unknown"`).
2. `git rev-parse HEAD` succeeds in the working directory (2 s timeout) →
   `source="git"`, `commit` from it (suffix `-dirty` on `commit_short` when
   `git status --porcelain` is non-empty), `build_time` from
   `git log -1 --format=%cI`.
3. Otherwise all `"unknown"`.

### Dockerfile and CI

```dockerfile
ARG VCS_REF=unknown
ARG BUILD_TIME=unknown
ENV ICE_COLDER_COMMIT=$VCS_REF ICE_COLDER_BUILD_TIME=$BUILD_TIME
```

`ci.yml` image job passes `build-args: VCS_REF=${{ github.sha }}` and
`BUILD_TIME=<run timestamp>` (a step computes `date -u +%Y-%m-%dT%H:%M:%SZ`).
A local `docker compose up --build` yields `unknown` unless the args are
supplied; that is honest and documented in the README Docker section.

### Consumers

- Simulators use `BUILD_INFO.commit_short` as `firmware` (same image).
- `VMCStatus` gains `version: str = BUILD_INFO.commit_short`.
- `main.py` logs `Build: <commit_short> (<source>, <build_time>)` at startup.

## 3. Simulators publish capabilities

`ESP32Simulator` (base):

- `build_capabilities(self) -> SubsystemCapabilities` default implementation:
  `subsystem=self.subsystem_name`, `firmware=BUILD_INFO.commit_short`,
  `contract_version` from a class attribute `CONTRACT_VERSION` (vending/MDB:
  vending contract `0.2.0`; ice maker overrides with its own),
  `hardware_id=self.fake_hardware_id()` (a stable `02:xx:xx:xx:xx:xx` derived
  from `sha1(f"{machine_id}/{subsystem_name}")`), `ip=self.container_ip()`
  (`socket.gethostbyname(socket.gethostname())`, `None` on failure),
  `commands=self.SUPPORTED_COMMANDS` (class attribute; vending:
  `["dispense", "payment/enable"]`, MDB: `["payment/enable", "refund"]`).
- `run()` publishes it retained on `capabilities/{subsystem_name}` right after
  HA discovery, before the TaskGroup starts.
- Ice-maker simulator keeps its `build_capabilities` override (channels,
  commands, brand, model) but now returns the subclass populated with
  `hardware_id`/`ip` from the base helpers; its existing publish sites stay.

## 4. Health monitor and VMC

### `HealthMonitor`

- Constructor gains optional `machine_id: str | None = None` and
  `started_at: float | None = None` (monotonic; default `time.monotonic()`).
- `record_capabilities(subsystem, caps: dict)`: creates the `SubsystemStatus`
  entry if missing (never-seen, `last_seen=0.0`), stores `caps` and a
  `capabilities_at` monotonic timestamp. Does **not** touch `last_seen`;
  retained documents must not make a dead board look alive.
- `SubsystemStatus` gains `capabilities: dict`, `capabilities_at: float`.
- `get_summary()["subsystems"][name]` gains:
  `uptime_seconds` (from `last_payload.get("uptime_seconds")`, `None` if never
  seen or `-1`), `firmware`, `contract_version`, `brand`, `model`,
  `hardware_id`, `ip`, `channel_count`, `commands`,
  `capabilities_age_seconds` (all `None` / `[]` / `0` when absent).
- `get_summary()["vmc"]`: `{commit, commit_short, build_time, source,
  uptime_seconds, python_version, machine_id}`.

### `VMC._handle_mqtt_capabilities`

Validate against `SubsystemCapabilities` (log a warning and keep the raw
dict on `ValidationError`, as today), store in `subsystem_capabilities`, and
call `health_monitor.record_capabilities(subsystem, data)`.

### `main.py`

`HealthMonitor(machine_id=live_config.machine_id)`; the rest unchanged.

## 5. Health tab — `partials/health_fragment.html` and `/health`

- **VMC row** above the subsystem table: `commit_short` (full sha in
  `title`), source tag (`image` / `git` / `unknown`, muted), build time,
  uptime, Python version, machine id.
- **Subsystems table** columns: Name, Status, Last Seen, Uptime, Firmware,
  Contract, Hardware. Hardware cell: `brand model` on the first line,
  `hardware_id · ip` muted on the second. Any unknown value renders `—`.
  Channel count and the command list go in the row's `title`.
- **Expected but silent**: `/health` merges `EXPECTED_SUBSYSTEMS` into the
  summary so each appears as a "Never seen" row with `—` everywhere.
- Uptime and ages formatted by a tiny Jinja filter `humanize_seconds`
  (`45s`, `12m`, `3h 04m`, `2d 5h`) registered in `server.py`.

## 6. Tests

- `tests/test_build_info.py`: env wins over git; git fallback in a temp repo
  (`git init`, one commit) yields `source="git"` and the sha; neither → all
  `"unknown"`; dirty suffix.
- `tests/test_contracts.py` / `test_contracts_vending.py`:
  `SubsystemCapabilities` validation (pattern on `subsystem`, defaults);
  `MonitorCapabilities` still requires brand/model/firmware and accepts the
  two new fields; `EXPECTED_SUBSYSTEMS` content.
- `tests/test_contract_schemas.py`: regenerated schemas match (both dirs).
- `tests/test_health_monitor.py`: capabilities before heartbeat creates a
  never-seen row without changing `last_seen`; uptime from heartbeat; `-1`
  uptime → `None`; ages; `vmc` block fields.
- `tests/test_vmc_flows.py` or `test_mqtt.py`: capabilities handler forwards
  to the health monitor and tolerates unknown fields / invalid payloads.
- `tests/test_simulator_base.py` (+ vending/mdb/ice_maker): each simulator's
  `build_capabilities()` validates, `firmware == BUILD_INFO.commit_short`,
  `hardware_id` stable across instances with the same machine id, published
  retained on connect (patch `publish`, assert `retain=True` and topic).
- `tests/test_web_routes.py`: `/health` shows the VMC row (commit short and
  source), a heartbeat-only subsystem renders `—` in the new columns, a
  subsystem with capabilities renders firmware/contract/hardware, and the
  expected-but-silent rows appear.

## 7. Docs

- Both `CONTRACT.md` files as in §1.
- `CLAUDE.md`: one sentence in the Web Dashboard section on the health tab's
  sources (heartbeat + retained capabilities + `services/build_info.py`).
- `README.md` Docker section: build identity args for local builds.

## Files touched

| Area | Files |
|---|---|
| Contracts | `contracts/common.py` (new), `contracts/vending_machine.py`, `contracts/ice_maker_monitor.py`, `contracts/generate.py`, `docs/contracts/**` |
| Build identity | `services/build_info.py` (new), `Dockerfile`, `.github/workflows/ci.yml`, `main.py`, `services/mqtt_messages.py` (`VMCStatus.version`) |
| Simulators | `simulators/base.py`, `simulators/vending_machine.py`, `simulators/mdb_gateway.py`, `simulators/ice_maker.py` |
| Health | `services/health_monitor.py`, `controller/vmc.py` |
| Web | `web_interface/routes.py`, `web_interface/server.py`, `templates/partials/health_fragment.html` |
| Docs | `CLAUDE.md`, `README.md` |
| Tests | as listed in §6 |
