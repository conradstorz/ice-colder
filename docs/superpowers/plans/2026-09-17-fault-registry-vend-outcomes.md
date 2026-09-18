# Fault Registry and Honest Vend Outcomes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A failed vend is recorded as a failed vend with a stable fault code, locks out only the failed product, and pays the customer back through the payment gateway with an acked refund command.

**Architecture:** A new shared contract module `contracts/vending_machine.py` defines the terminal dispenser outcomes, the fault-code registry, and the refund command/ack models; the VMC, simulators, dashboard and event recorder all import it. The VMC gains a `vend_failed` transition (`dispensing → interacting_with_user`), a per-product lockout map, a fault-raising path that feeds the existing health-monitor alert pipeline, and a real `request_refund` that publishes `cmd/payment/refund` and tracks acks. The MDB simulator answers refunds; the dashboard shows active faults with a Clear button.

**Tech Stack:** Python 3.12, uv, Pydantic v2, `transitions` FSM, aiomqtt, FastAPI + Jinja2 + HTMX, sqlite3, loguru, pytest (asyncio mode auto).

**Spec:** `docs/superpowers/specs/2026-09-17-fault-registry-vend-outcomes-design.md`

## Global Constraints

- Run everything with `uv`: `uv run pytest`, `uv run python -m contracts.generate`. Never bare `pytest`/`pip`.
- Do NOT chain shell commands with `&&`; run separate commands.
- Lint/format before every commit: `ruff check --fix .` then `ruff format .`. Five pre-existing E402 errors in `tests/test_integration_e2e.py` are known and out of scope.
- Baseline at plan time: `uv run pytest -q` → 470 passed, 9 skipped. Keep it green after every task.
- Fault codes are stable strings exactly as in `ROADMAP.md` §5 (`ICE-101`, …) plus new `PAY-103`.
- Refund ack deadline 10 s; exactly one retry with the same `request_id`; then `PAY-103`.
- Dispense timeout default `120.0` s, config-driven (`physical.dispense_timeout_seconds`, `ge=10`).
- MDB refund result cache bounded to 256 entries.
- The VMC treats only `DispenserOutcome` members as terminal dispenser states; every other `state` string is an intermediate step.
- Fault key convention (used by the VMC, health monitor and `/faults/{key}/clear`): product-scope faults are keyed by SKU; machine-scope faults are keyed by their code string (e.g. `PAY-103`). This replaces the spec's `__machine__` placeholder.
- Commit message trailer on every commit: `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`.

---

## File map

| File | Responsibility |
|---|---|
| `contracts/vending_machine.py` (new) | `DispenserOutcome`, `FaultCode`, `Severity`, `Scope`, `FaultSpec`, `FAULT_TABLE`, `OUTCOME_FAULTS`, `RefundStatus`, `PaymentRefundCommand`, `PaymentRefundResult` |
| `contracts/generate.py` | Generates schemas for both contracts |
| `docs/contracts/vending-machine/CONTRACT.md` (new), `schemas/*.schema.json` (new) | Stub contract + generated schemas |
| `services/mqtt_messages.py` | `VMCAlert` gains `code`, `product_sku` |
| `config/config_model.py`, `config.example.json` | `dispense_timeout_seconds` |
| `services/event_recorder.py` | `vends_failed`, `refunds` summary keys |
| `services/health_monitor.py` | `Alert.code/product_sku`, `raise_alert`, `clear_alert`, `set_active_faults`, summary `active_faults` |
| `controller/vmc.py` | lockouts, `_raise_fault`, `clear_fault`, `active_faults`, `vend_failed` transition, terminal outcome handling, config-driven dispense timeout, real refunds |
| `simulators/mdb_gateway.py` | refund loop, result cache, `changer_empty` fault |
| `simulators/vending_machine.py` | publishes `DispenserOutcome` values |
| `web_interface/routes.py`, `templates/partials/status_fragment.html`, `inventory_table.html`, `kpi_fragment.html`, `activity_fragment.html` | active faults UI, `/faults/{key}/clear`, counters |
| `ROADMAP.md`, `CLAUDE.md` | doc updates |
| `tests/test_contracts_vending.py` (new), `tests/test_contract_schemas.py`, `tests/test_config_model.py`, `tests/test_event_recorder.py`, `tests/test_health_monitor.py`, `tests/test_vmc_flows.py`, `tests/test_simulator_mdb.py`, `tests/test_simulator_vending.py`, `tests/test_web_routes.py`, `tests/test_integration_e2e.py` | tests |

---

### Task 1: Contract models

**Files:**
- Create: `contracts/vending_machine.py`
- Modify: `services/mqtt_messages.py` (class `VMCAlert`, ~line 163)
- Test: `tests/test_contracts_vending.py` (new)

**Interfaces:**
- Produces: everything listed in the file map row for `contracts/vending_machine.py`, with the exact names and signatures below. Later tasks import from `contracts.vending_machine`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_contracts_vending.py
"""Contract models for the vending-machine interface (v0.1.0)."""

import pytest
from pydantic import ValidationError

from contracts.vending_machine import (
    CONTRACT_VERSION,
    FAULT_TABLE,
    OUTCOME_FAULTS,
    DispenserOutcome,
    FaultCode,
    PaymentRefundCommand,
    PaymentRefundResult,
    RefundStatus,
    Scope,
    Severity,
)


def test_contract_version():
    assert CONTRACT_VERSION == "0.1.0"


def test_every_fault_code_has_a_table_entry():
    missing = [c for c in FaultCode if c not in FAULT_TABLE]
    assert missing == []


def test_every_failure_outcome_maps_to_a_fault_code():
    for outcome in DispenserOutcome:
        if outcome is DispenserOutcome.complete:
            assert outcome not in OUTCOME_FAULTS
        else:
            assert isinstance(OUTCOME_FAULTS[outcome], FaultCode)


def test_outcome_mapping_matches_spec():
    assert OUTCOME_FAULTS[DispenserOutcome.bin_empty] is FaultCode.ICE_101
    assert OUTCOME_FAULTS[DispenserOutcome.timeout] is FaultCode.ICE_301
    assert OUTCOME_FAULTS[DispenserOutcome.jam] is FaultCode.ICE_401
    assert OUTCOME_FAULTS[DispenserOutcome.error] is FaultCode.ICE_302


def test_fault_code_values_are_stable_strings():
    assert FaultCode.ICE_101.value == "ICE-101"
    assert FaultCode.PAY_103.value == "PAY-103"
    assert FaultCode("ICE-301") is FaultCode.ICE_301


def test_severities_follow_roadmap():
    assert FAULT_TABLE[FaultCode.ICE_101].severity is Severity.product_unavailable
    assert FAULT_TABLE[FaultCode.ICE_301].severity is Severity.lockout
    assert FAULT_TABLE[FaultCode.ICE_401].severity is Severity.lockout
    assert FAULT_TABLE[FaultCode.ICE_302].severity is Severity.lockout
    assert FAULT_TABLE[FaultCode.PAY_102].severity is Severity.vend_failed
    assert FAULT_TABLE[FaultCode.PAY_103].severity is Severity.warning
    assert FAULT_TABLE[FaultCode.PAY_103].scope is Scope.machine
    assert FAULT_TABLE[FaultCode.ICE_301].scope is Scope.product


class TestPaymentRefundCommand:
    def test_valid(self):
        cmd = PaymentRefundCommand(
            request_id="a" * 32, amount=2.5, reason="session_timeout"
        )
        assert cmd.amount == 2.5
        assert cmd.timestamp is not None

    def test_rejects_non_positive_amount(self):
        with pytest.raises(ValidationError):
            PaymentRefundCommand(request_id="a" * 32, amount=0, reason="cancel")

    def test_rejects_short_request_id(self):
        with pytest.raises(ValidationError):
            PaymentRefundCommand(request_id="short", amount=1.0, reason="cancel")


class TestPaymentRefundResult:
    def test_defaults(self):
        res = PaymentRefundResult(request_id="a" * 32, status=RefundStatus.ok)
        assert res.amount_returned == 0.0
        assert res.detail is None

    def test_round_trip_json(self):
        res = PaymentRefundResult(
            request_id="a" * 32,
            status="failed",
            amount_returned=0.0,
            detail="changer_empty",
        )
        again = PaymentRefundResult.model_validate_json(res.model_dump_json())
        assert again.status is RefundStatus.failed
        assert again.detail == "changer_empty"


def test_vmc_alert_carries_code_and_sku():
    from services.mqtt_messages import VMCAlert

    alert = VMCAlert(level="error", message="x", code=FaultCode.ICE_301, product_sku="ICE-1")
    data = alert.model_dump(mode="json")
    assert data["code"] == "ICE-301"
    assert data["product_sku"] == "ICE-1"
    assert VMCAlert(level="info", message="y").code is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_contracts_vending.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'contracts.vending_machine'`

- [ ] **Step 3: Create the contract module**

```python
# contracts/vending_machine.py
"""
Shared contract models for the vending-machine ESP32 interface (v0.1.0).

Terminal dispenser outcomes, the fault-code registry, and the refund
command/ack exchanged between ice-colder (the VMC) and the vending
ESP32 / MDB payment gateway. JSON Schemas are generated from these models
into docs/contracts/vending-machine/schemas/ by contracts/generate.py.
The VMC and the simulators both import from here so the two sides cannot
drift apart silently. Breaking changes require a major CONTRACT_VERSION
bump; adding a FaultCode or an enum member is a minor bump.
"""

from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field

CONTRACT_VERSION = "0.1.0"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class DispenserOutcome(str, Enum):
    """Terminal states a dispense can end in, published as DispenserStatus.state.

    Any other DispenserStatus.state string is an intermediate step
    (motor_active, fill_complete, solenoid_open, ...) and never ends a sale.
    """

    complete = "complete"
    bin_empty = "bin_empty"
    timeout = "timeout"
    jam = "jam"
    error = "error"


class FaultCode(str, Enum):
    """Stable fault codes (ROADMAP.md §5). Never renumber; only add."""

    ICE_101 = "ICE-101"
    ICE_201 = "ICE-201"
    ICE_202 = "ICE-202"
    ICE_301 = "ICE-301"
    ICE_302 = "ICE-302"
    ICE_401 = "ICE-401"
    ICE_402 = "ICE-402"
    WTR_101 = "WTR-101"
    WTR_102 = "WTR-102"
    WTR_103 = "WTR-103"
    WTR_104 = "WTR-104"
    WTR_105 = "WTR-105"
    ENV_101 = "ENV-101"
    ENV_102 = "ENV-102"
    ENV_103 = "ENV-103"
    PAY_101 = "PAY-101"
    PAY_102 = "PAY-102"
    PAY_103 = "PAY-103"
    PWR_101 = "PWR-101"
    PWR_102 = "PWR-102"
    COM_101 = "COM-101"
    COM_102 = "COM-102"
    COM_103 = "COM-103"
    SVC_101 = "SVC-101"


class Severity(str, Enum):
    info = "info"  # logged only
    warning = "warning"  # alerts the owner
    product_unavailable = "product_unavailable"  # locks a product; clears itself
    vend_failed = "vend_failed"  # ends the sale; no lockout
    lockout = "lockout"  # locks a product until an admin clears it
    critical = "critical"  # machine-scope; never auto-clears


class Scope(str, Enum):
    product = "product"
    machine = "machine"


class FaultSpec(BaseModel):
    severity: Severity
    scope: Scope
    description: str


FAULT_TABLE: dict[FaultCode, FaultSpec] = {
    FaultCode.ICE_101: FaultSpec(
        severity=Severity.product_unavailable,
        scope=Scope.product,
        description="Ice unavailable (hopper low / maker bin empty)",
    ),
    FaultCode.ICE_201: FaultSpec(
        severity=Severity.product_unavailable,
        scope=Scope.product,
        description="Bag not detected",
    ),
    FaultCode.ICE_202: FaultSpec(
        severity=Severity.vend_failed,
        scope=Scope.product,
        description="Bag lost during fill",
    ),
    FaultCode.ICE_301: FaultSpec(
        severity=Severity.lockout,
        scope=Scope.product,
        description="Fill timeout (full-bag sensor never tripped)",
    ),
    FaultCode.ICE_302: FaultSpec(
        severity=Severity.lockout,
        scope=Scope.product,
        description="Dispense/agitator motor fault",
    ),
    FaultCode.ICE_401: FaultSpec(
        severity=Severity.lockout,
        scope=Scope.product,
        description="Trap door / bag release failed to open",
    ),
    FaultCode.ICE_402: FaultSpec(
        severity=Severity.critical,
        scope=Scope.machine,
        description="Trap door failed to close",
    ),
    FaultCode.WTR_101: FaultSpec(
        severity=Severity.vend_failed,
        scope=Scope.product,
        description="No flow after valve open",
    ),
    FaultCode.WTR_102: FaultSpec(
        severity=Severity.lockout,
        scope=Scope.product,
        description="Over-dispense (flow pulses exceeded)",
    ),
    FaultCode.WTR_103: FaultSpec(
        severity=Severity.critical,
        scope=Scope.machine,
        description="Flow continues after valve close",
    ),
    FaultCode.WTR_104: FaultSpec(
        severity=Severity.critical,
        scope=Scope.machine,
        description="Leak / overflow detected",
    ),
    FaultCode.WTR_105: FaultSpec(
        severity=Severity.product_unavailable,
        scope=Scope.product,
        description="Water pressure or treatment status failed",
    ),
    FaultCode.ENV_101: FaultSpec(
        severity=Severity.warning,
        scope=Scope.machine,
        description="Cabinet below freeze threshold",
    ),
    FaultCode.ENV_102: FaultSpec(
        severity=Severity.critical,
        scope=Scope.machine,
        description="Heater ineffective (low temperature persists)",
    ),
    FaultCode.ENV_103: FaultSpec(
        severity=Severity.critical,
        scope=Scope.machine,
        description="Heater high-limit tripped",
    ),
    FaultCode.PAY_101: FaultSpec(
        severity=Severity.product_unavailable,
        scope=Scope.machine,
        description="Payment device offline",
    ),
    FaultCode.PAY_102: FaultSpec(
        severity=Severity.vend_failed,
        scope=Scope.product,
        description="No dispense report within the timeout after credit taken",
    ),
    FaultCode.PAY_103: FaultSpec(
        severity=Severity.warning,
        scope=Scope.machine,
        description="Refund not confirmed by payment gateway; needs reconciliation",
    ),
    FaultCode.PWR_101: FaultSpec(
        severity=Severity.info,
        scope=Scope.machine,
        description="Power restored after loss",
    ),
    FaultCode.PWR_102: FaultSpec(
        severity=Severity.critical,
        scope=Scope.machine,
        description="24 V control supply bad",
    ),
    FaultCode.COM_101: FaultSpec(
        severity=Severity.product_unavailable,
        scope=Scope.machine,
        description="Vending ESP32 heartbeat lost",
    ),
    FaultCode.COM_102: FaultSpec(
        severity=Severity.warning,
        scope=Scope.machine,
        description="Ice-maker monitor heartbeat lost",
    ),
    FaultCode.COM_103: FaultSpec(
        severity=Severity.warning,
        scope=Scope.machine,
        description="MQTT broker unreachable",
    ),
    FaultCode.SVC_101: FaultSpec(
        severity=Severity.info,
        scope=Scope.machine,
        description="Service door open / service mode",
    ),
}

# Which fault a terminal dispenser outcome raises. `complete` is not a fault.
OUTCOME_FAULTS: dict[DispenserOutcome, FaultCode] = {
    DispenserOutcome.bin_empty: FaultCode.ICE_101,
    DispenserOutcome.timeout: FaultCode.ICE_301,
    DispenserOutcome.jam: FaultCode.ICE_401,
    DispenserOutcome.error: FaultCode.ICE_302,
}


class RefundStatus(str, Enum):
    ok = "ok"
    failed = "failed"
    unsupported = "unsupported"


class PaymentRefundCommand(BaseModel):
    """VMC -> payment gateway, published on cmd/payment/refund (QoS 1)."""

    request_id: str = Field(
        ...,
        min_length=8,
        max_length=64,
        description="Opaque correlation key, unique per refund; UUID4 hex by the VMC",
    )
    amount: float = Field(..., gt=0, description="Amount to pay out, USD")
    reason: str = Field(
        ...,
        description="A FaultCode value, or 'session_timeout', 'cancel', 'error', 'admin'",
    )
    timestamp: datetime = Field(default_factory=_utc_now)


class PaymentRefundResult(BaseModel):
    """Payment gateway -> VMC, published on cmd/payment/refund/ack (QoS 1).

    Exactly one per command; a repeated request_id is answered with the
    previously computed result and never paid twice.
    """

    request_id: str = Field(..., description="Echoed from the command")
    status: RefundStatus
    amount_returned: float = Field(0.0, ge=0, description="Amount actually paid out")
    detail: Optional[str] = Field(None, description="e.g. 'changer_empty'")
    timestamp: datetime = Field(default_factory=_utc_now)
```

- [ ] **Step 4: Extend `VMCAlert`**

In `services/mqtt_messages.py`, add the import near the other imports:

```python
from contracts.vending_machine import FaultCode
```

and replace the `VMCAlert` class with:

```python
class VMCAlert(BaseModel):
    """Alert published when something needs owner attention."""

    level: AlertLevel
    message: str
    source: str = Field("vmc", description="Subsystem that generated the alert")
    code: Optional[FaultCode] = Field(None, description="Fault code, if any")
    product_sku: Optional[str] = Field(None, description="Affected product, if any")
    timestamp: datetime = Field(default_factory=_utc_now)
```

Check `contracts/vending_machine.py` does not import `services.mqtt_messages` (it doesn't) so there is no import cycle.

- [ ] **Step 5: Run tests**

Run: `uv run pytest tests/test_contracts_vending.py -q`
Expected: all pass.

Run: `uv run pytest -q`
Expected: 470 + 11 new passed, 9 skipped.

- [ ] **Step 6: Lint and commit**

```bash
ruff check --fix .
ruff format .
git add contracts/vending_machine.py services/mqtt_messages.py tests/test_contracts_vending.py
git commit -m "feat: vending-machine contract models — outcomes, fault registry, refund messages

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 2: Schema generation for the vending contract

**Files:**
- Modify: `contracts/generate.py`
- Create: `docs/contracts/vending-machine/CONTRACT.md`, `docs/contracts/vending-machine/schemas/*.schema.json` (generated)
- Modify: `tests/test_contract_schemas.py`

**Interfaces:**
- Consumes: Task 1 models.
- Produces: `contracts.generate.CONTRACTS: dict[str, tuple[Path, dict[str, type]]]`, `contracts.generate.schema_for(model) -> dict`, `VENDING_SCHEMA_DIR`, `VENDING_MODELS`. `SCHEMA_DIR` and `MODELS` keep their ice-maker meaning.

- [ ] **Step 1: Rewrite the drift test to cover both contracts**

Replace `tests/test_contract_schemas.py` with:

```python
"""Committed JSON Schemas must match the live Pydantic models (drift guard)."""

import json

import pytest

from contracts.generate import CONTRACTS, schema_for

_CASES = [
    (contract, name, schema_dir, models[name])
    for contract, (schema_dir, models) in CONTRACTS.items()
    for name in sorted(models)
]


@pytest.mark.parametrize("contract", sorted(CONTRACTS))
def test_schema_dir_has_exactly_the_expected_files(contract):
    schema_dir, models = CONTRACTS[contract]
    expected = {f"{name}.schema.json" for name in models}
    actual = {p.name for p in schema_dir.glob("*.schema.json")}
    assert actual == expected


@pytest.mark.parametrize(
    "contract,name,schema_dir,model", _CASES, ids=[f"{c}/{n}" for c, n, _, _ in _CASES]
)
def test_committed_schema_matches_model(contract, name, schema_dir, model):
    committed = json.loads(
        (schema_dir / f"{name}.schema.json").read_text(encoding="utf-8")
    )
    assert committed == schema_for(model), (
        f"{contract}/{name} schema drifted — run: uv run python -m contracts.generate"
    )
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_contract_schemas.py -q`
Expected: FAIL with `ImportError: cannot import name 'CONTRACTS'`

- [ ] **Step 3: Extend the generator**

Replace `contracts/generate.py` with:

```python
# contracts/generate.py
"""Generate the contracts' JSON Schema files.

Run after any model change: uv run python -m contracts.generate
tests/test_contract_schemas.py fails if the committed files drift.
"""

import json
from pathlib import Path

from pydantic import BaseModel, TypeAdapter

from services.mqtt_messages import IceMakerEvent, SensorReading, SubsystemHeartbeat

from contracts.ice_maker_monitor import (
    ChannelDescriptor,
    ChannelReading,
    CommandAck,
    MonitorCapabilities,
    MonitorCommand,
)
from contracts.vending_machine import (
    DispenserOutcome,
    FaultCode,
    PaymentRefundCommand,
    PaymentRefundResult,
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

VENDING_SCHEMA_DIR = Path("docs/contracts/vending-machine/schemas")

VENDING_MODELS = {
    "dispenser_outcome": DispenserOutcome,
    "fault_code": FaultCode,
    "payment_refund_command": PaymentRefundCommand,
    "payment_refund_result": PaymentRefundResult,
}

CONTRACTS: dict[str, tuple[Path, dict]] = {
    "ice-maker-monitor": (SCHEMA_DIR, MODELS),
    "vending-machine": (VENDING_SCHEMA_DIR, VENDING_MODELS),
}


def schema_for(model) -> dict:
    """JSON Schema for a Pydantic model or a plain Enum."""
    if isinstance(model, type) and issubclass(model, BaseModel):
        return model.model_json_schema()
    return TypeAdapter(model).json_schema()


def generate(contracts: dict | None = None) -> list[Path]:
    written = []
    for schema_dir, models in (contracts or CONTRACTS).values():
        schema_dir.mkdir(parents=True, exist_ok=True)
        for name, model in models.items():
            path = schema_dir / f"{name}.schema.json"
            path.write_text(
                json.dumps(schema_for(model), indent=2) + "\n", encoding="utf-8"
            )
            written.append(path)
    return written


if __name__ == "__main__":
    for path in generate():
        print(f"wrote {path}")
```

- [ ] **Step 4: Generate the schemas and confirm the ice-maker ones did not change**

Run: `uv run python -m contracts.generate`
Expected: 12 `wrote ...` lines.

Run: `git status --short docs/contracts/ice-maker-monitor/`
Expected: no output (ice-maker schemas byte-identical). If a file shows as modified with only CRLF differences, run `git checkout -- docs/contracts/ice-maker-monitor/` and re-check that the drift test still passes.

- [ ] **Step 5: Write the contract stub**

```markdown
# Vending Machine Contract — v0.1.0 (stub)

This document and the JSON Schema files in `schemas/` define the interface
between the ice-colder VMC and the vending ESP32 firmware plus the MDB
payment gateway. Version 0.1.0 covers only what the VMC needs for honest
vend outcomes and acked refunds. The full sequence vocabulary, capabilities
document, heartbeat/LWT rules and interlock MUSTs follow in Phase B
(`ROADMAP.md` §4–§5, §9).

The `schemas/*.schema.json` files are generated from
`contracts/vending_machine.py` by `uv run python -m contracts.generate` and
are the normative payload definitions.

## Topic map

All topics are relative to `vmc/{machine_id}/`.

| Topic | Direction | Payload | Notes |
|---|---|---|---|
| `hardware/dispenser` | ESP32 → VMC | `DispenserStatus` (`services/mqtt_messages.py`) | `state` is a [`DispenserOutcome`](schemas/dispenser_outcome.schema.json) for terminal states; any other string is an intermediate step |
| `cmd/payment/refund` | VMC → gateway | [`PaymentRefundCommand`](schemas/payment_refund_command.schema.json) | QoS 1; `request_id` unique per refund |
| `cmd/payment/refund/ack` | gateway → VMC | [`PaymentRefundResult`](schemas/payment_refund_result.schema.json) | QoS 1; exactly one per command; repeated `request_id` re-sends the stored result, never pays twice |
| `alerts` | VMC → world | `VMCAlert` (`services/mqtt_messages.py`) | carries a [`FaultCode`](schemas/fault_code.schema.json) when one applies |

## Semantics fixed in 0.1.0

- The VMC finishes a sale only on `DispenserOutcome.complete` for the slot
  it commanded. `bin_empty`, `timeout`, `jam`, `error` end the sale as a
  failed vend and raise the fault in `OUTCOME_FAULTS`.
- No terminal report within the VMC's configured dispense timeout is a
  failed vend (`PAY-102`).
- Refund ack deadline is 10 s; the VMC retries once with the same
  `request_id`, then raises `PAY-103`.
- Fault codes are stable; see `ROADMAP.md` §5 for the registry.

## Reference implementation

`simulators/vending_machine.py` publishes the outcomes; `simulators/mdb_gateway.py`
answers refunds (including the `changer_empty` failure path).
```

Save as `docs/contracts/vending-machine/CONTRACT.md`.

- [ ] **Step 6: Run tests**

Run: `uv run pytest tests/test_contract_schemas.py tests/test_contracts.py -q`
Expected: all pass (ice-maker cases plus 4 vending cases plus 2 dir checks).

- [ ] **Step 7: Lint and commit**

```bash
ruff check --fix .
ruff format .
git add contracts/generate.py docs/contracts/vending-machine tests/test_contract_schemas.py
git commit -m "feat: generate and drift-test vending-machine contract schemas

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 3: Config-driven dispense timeout

**Files:**
- Modify: `config/config_model.py` (class `PhysicalDetails`, ~line 126)
- Modify: `config.example.json`
- Test: `tests/test_config_model.py`

**Interfaces:**
- Produces: `ConfigModel().physical.dispense_timeout_seconds: float` (default `120.0`, `ge=10`).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_config_model.py`:

```python
class TestDispenseTimeout:
    def test_default_is_120_seconds(self):
        from config.config_model import ConfigModel

        assert ConfigModel().physical.dispense_timeout_seconds == 120.0

    def test_rejects_below_10_seconds(self):
        import pytest
        from pydantic import ValidationError

        from config.config_model import ConfigModel

        with pytest.raises(ValidationError):
            ConfigModel.model_validate({"physical": {"dispense_timeout_seconds": 5}})

    def test_example_config_declares_it(self):
        import json
        from pathlib import Path

        raw = json.loads(Path("config.example.json").read_text(encoding="utf-8"))
        assert raw["physical"]["dispense_timeout_seconds"] == 120
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_config_model.py::TestDispenseTimeout -q`
Expected: 3 failures (`AttributeError` / no `ValidationError` / `KeyError`).

- [ ] **Step 3: Add the field**

In `PhysicalDetails`, after `serial_number`:

```python
    dispense_timeout_seconds: float = Field(
        120.0,
        ge=10,
        description=(
            "Seconds the VMC waits for a terminal dispenser report after "
            "commanding a dispense; expiry is a failed vend (PAY-102)"
        ),
    )
```

In `config.example.json`, add after the `"serial_number"` line inside `"physical"`:

```json
    "dispense_timeout_seconds": 120,
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_config_model.py tests/test_first_run.py -q`
Expected: pass.

- [ ] **Step 5: Lint and commit**

```bash
ruff check --fix .
ruff format .
git add config/config_model.py config.example.json tests/test_config_model.py
git commit -m "feat: physical.dispense_timeout_seconds (default 120 s)

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 4: Event recorder summary keys

**Files:**
- Modify: `services/event_recorder.py` (`SUMMARY_KEYS` ~line 33, `_compute_window` ~line 128)
- Test: `tests/test_event_recorder.py`

**Interfaces:**
- Produces: `get_summary()` / `get_historical_average()` dicts include `vends_failed` (count of `vend_failed` events) and `refunds` (sum of `refund` event values).

- [ ] **Step 1: Write the failing test**

Add to `tests/test_event_recorder.py` inside `class TestGetSummary`:

```python
    def test_vends_failed_and_refunds(self, recorder):
        recorder.record("vend_failed", value=2.5, metadata={"code": "ICE-301"})
        recorder.record("vend_failed", value=3.0, metadata={"code": "ICE-401"})
        recorder.record("refund", value=2.5, metadata={"request_id": "r1"})
        recorder.record("refund_failed", value=3.0, metadata={"request_id": "r2"})
        s = recorder.get_summary(24)
        assert s["vends_failed"] == 2
        assert s["refunds"] == 2.5
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_event_recorder.py::TestGetSummary::test_vends_failed_and_refunds -q`
Expected: FAIL with `KeyError: 'vends_failed'`

- [ ] **Step 3: Implement**

In `SUMMARY_KEYS` add two entries so it reads:

```python
SUMMARY_KEYS = (
    "money_in",
    "products_out",
    "ice_cycles",
    "errors",
    "service_door_opens",
    "temp_exceedances",
    "uptime_pct",
    "vends_failed",
    "refunds",
)
```

In `_compute_window`'s returned dict add:

```python
                "vends_failed": count("vend_failed"),
                "refunds": round(total("refund"), 2),
```

- [ ] **Step 4: Run the recorder tests; fix any that assert the exact key set**

Run: `uv run pytest tests/test_event_recorder.py -q`
Expected: pass. If a test compares `set(summary) == {...}` or checks `len(SUMMARY_KEYS)`, update it to include the two new keys.

- [ ] **Step 5: Lint and commit**

```bash
ruff check --fix .
ruff format .
git add services/event_recorder.py tests/test_event_recorder.py
git commit -m "feat: event summaries count failed vends and refunds paid

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 5: Health monitor fault plumbing

**Files:**
- Modify: `services/health_monitor.py`
- Test: `tests/test_health_monitor.py`

**Interfaces:**
- Produces:
  - `Alert` dataclass gains `code: str | None = None`, `product_sku: str | None = None`.
  - `async HealthMonitor.raise_alert(key: str, level: str, source: str, message: str, code: str | None = None, product_sku: str | None = None)` — dedups on `key`, calls the alert callback.
  - `HealthMonitor.clear_alert(key: str)` — forgets the dedup key.
  - `HealthMonitor.set_active_faults(faults: list[dict])` — each dict has `key, sku, product, code, severity, scope, description`; the monitor keeps a `since` monotonic timestamp per key across calls.
  - `get_summary()["active_faults"]`: list of those dicts plus `since_seconds: float`, ordered as given.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_health_monitor.py`:

```python
class TestFaultPlumbing:
    async def test_raise_alert_carries_code_and_dedups(self):
        hm = HealthMonitor()
        received = []

        async def cb(alert):
            received.append(alert)

        hm.set_alert_callback(cb)
        await hm.raise_alert("ICE-301:ICE-1", "error", "vmc", "fill timeout",
                             code="ICE-301", product_sku="ICE-1")
        await hm.raise_alert("ICE-301:ICE-1", "error", "vmc", "fill timeout",
                             code="ICE-301", product_sku="ICE-1")
        assert len(received) == 1
        assert received[0].code == "ICE-301"
        assert received[0].product_sku == "ICE-1"

    async def test_clear_alert_rearms(self):
        hm = HealthMonitor()
        received = []

        async def cb(alert):
            received.append(alert)

        hm.set_alert_callback(cb)
        await hm.raise_alert("k", "warning", "vmc", "m")
        hm.clear_alert("k")
        await hm.raise_alert("k", "warning", "vmc", "m")
        assert len(received) == 2

    def test_set_active_faults_preserves_since(self, monkeypatch):
        import time as _time

        hm = HealthMonitor()
        fault = {"key": "ICE-1", "sku": "ICE-1", "product": "Ice", "code": "ICE-301",
                 "severity": "lockout", "scope": "product", "description": "d"}
        t = [1000.0]
        monkeypatch.setattr(_time, "monotonic", lambda: t[0])
        hm.set_active_faults([fault])
        t[0] = 1030.0
        hm.set_active_faults([fault])
        summary = hm.get_summary()
        assert len(summary["active_faults"]) == 1
        assert summary["active_faults"][0]["code"] == "ICE-301"
        assert summary["active_faults"][0]["since_seconds"] == 30.0

    def test_set_active_faults_drops_cleared(self):
        hm = HealthMonitor()
        hm.set_active_faults([{"key": "a", "sku": "a", "product": "A", "code": "ICE-301",
                               "severity": "lockout", "scope": "product", "description": "d"}])
        hm.set_active_faults([])
        assert hm.get_summary()["active_faults"] == []
```

Ensure the file's `HealthMonitor` import is present (it is at the top of the existing tests).

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_health_monitor.py::TestFaultPlumbing -q`
Expected: 4 failures (`AttributeError: raise_alert` etc.).

- [ ] **Step 3: Implement**

In `services/health_monitor.py`:

Replace the `Alert` dataclass:

```python
@dataclass
class Alert:
    """A health alert ready to be sent to the owner."""

    level: str  # "info", "warning", "error", "critical"
    source: str
    message: str
    timestamp: float = field(default_factory=time.monotonic)
    code: Optional[str] = None  # FaultCode value, when the alert is a fault
    product_sku: Optional[str] = None
```

In `__init__`, after `self._fired_alerts: set[str] = set()` add:

```python
        # Active faults pushed by the VMC: key -> fault dict (+ "since" monotonic)
        self._active_faults: dict[str, dict] = {}
```

Add these methods after `mark_offline`:

```python
    def set_active_faults(self, faults: list[dict]):
        """Replace the active-fault snapshot; `since` survives for keys already present."""
        now = time.monotonic()
        new: dict[str, dict] = {}
        for f in faults:
            key = f["key"]
            since = self._active_faults.get(key, {}).get("since", now)
            new[key] = {**f, "since": since}
        self._active_faults = new

    async def raise_alert(
        self,
        key: str,
        level: str,
        source: str,
        message: str,
        code: str | None = None,
        product_sku: str | None = None,
    ):
        """Public entry for VMC-raised faults; dedups on `key` like periodic checks."""
        await self._fire_alert(
            key, level, source, message, code=code, product_sku=product_sku
        )

    def clear_alert(self, key: str):
        """Forget a dedup key so the next raise_alert with it fires again."""
        self._fired_alerts.discard(key)
```

Change `_fire_alert` signature and the `Alert(...)` construction:

```python
    async def _fire_alert(
        self,
        key: str,
        level: str,
        source: str,
        message: str,
        code: str | None = None,
        product_sku: str | None = None,
    ):
        """Fire an alert if it hasn't already been fired (deduplication)."""
        if key in self._fired_alerts:
            return
        self._fired_alerts.add(key)

        alert = Alert(
            level=level,
            source=source,
            message=message,
            code=code,
            product_sku=product_sku,
        )
```

(the rest of `_fire_alert` unchanged).

In `get_summary`, before the `return`, add:

```python
        now = time.monotonic()
        active_faults = [
            {**{k: v for k, v in f.items() if k != "since"},
             "since_seconds": round(now - f["since"], 1)}
            for f in self._active_faults.values()
        ]
```

and add `"active_faults": active_faults,` to the returned dict.

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_health_monitor.py -q`
Expected: pass. If `TestGetSummary.test_empty_summary` compares the exact dict, add `"active_faults": []` to its expectation.

- [ ] **Step 5: Lint and commit**

```bash
ruff check --fix .
ruff format .
git add services/health_monitor.py tests/test_health_monitor.py
git commit -m "feat: health monitor carries fault codes and an active-fault snapshot

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 6: VMC fault registry and lockouts

**Files:**
- Modify: `controller/vmc.py`
- Test: `tests/test_vmc_flows.py`

**Interfaces:**
- Consumes: Task 1 (`FaultCode`, `FAULT_TABLE`, `Severity`, `Scope`), Task 5 (`raise_alert`, `clear_alert`, `set_active_faults`).
- Produces on `VMC`:
  - `_lockouts: dict[str, FaultCode]` (sku → code), `_machine_faults: dict[FaultCode, float]` (code → monotonic since).
  - `_raise_fault(code: FaultCode, sku: str | None = None, outcome: str | None = None) -> None`
  - `clear_fault(key: str, by: str = "admin") -> bool`
  - `active_faults() -> list[dict]` (dicts with `key, sku, product, code, severity, scope, description`)
  - `_sellable_products() -> list[Product]`
  - `_handle_mqtt_hardware_io(topic, data)` registered on `hardware/io/+`
  - `select_product` refuses locked SKUs.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_vmc_flows.py`:

```python
from contracts.vending_machine import FaultCode
from services.health_monitor import HealthMonitor


def make_vmc2() -> VMC:
    cfg = ConfigModel()
    cfg.physical.products = [
        Product(sku="ICE-1", name="Ice Bag", price=2.50, slot=0),
        Product(sku="WATER-1", name="Water", price=1.00, slot=1),
    ]
    return VMC(config=cfg)


class TestFaultRegistry:
    async def test_lockout_fault_locks_product_and_alerts(self):
        vmc = make_vmc2()
        vmc.attach_to_loop(asyncio.get_running_loop())
        rec = FakeEventRecorder()
        vmc.set_event_recorder(rec)
        hm = HealthMonitor()
        vmc.set_health_monitor(hm)

        vmc._raise_fault(FaultCode.ICE_301, sku="ICE-1", outcome="timeout")
        await asyncio.sleep(0)

        assert vmc._lockouts == {"ICE-1": FaultCode.ICE_301}
        assert ("lockout_set", 1.0, {"code": "ICE-301", "sku": "ICE-1"}) in rec.events
        assert "ICE-301:ICE-1" in hm._fired_alerts
        faults = hm.get_summary()["active_faults"]
        assert faults[0]["code"] == "ICE-301" and faults[0]["product"] == "Ice Bag"

    async def test_vend_failed_severity_does_not_lock(self):
        vmc = make_vmc2()
        vmc.attach_to_loop(asyncio.get_running_loop())
        vmc._raise_fault(FaultCode.PAY_102, sku="ICE-1")
        assert vmc._lockouts == {}
        assert vmc.active_faults() == []

    async def test_machine_scope_fault_keyed_by_code(self):
        vmc = make_vmc2()
        vmc.attach_to_loop(asyncio.get_running_loop())
        vmc._raise_fault(FaultCode.PAY_103)
        faults = vmc.active_faults()
        assert faults == [
            {
                "key": "PAY-103",
                "sku": None,
                "product": None,
                "code": "PAY-103",
                "severity": "warning",
                "scope": "machine",
                "description": "Refund not confirmed by payment gateway; needs reconciliation",
            }
        ]
        assert vmc.clear_fault("PAY-103") is True
        assert vmc.active_faults() == []

    async def test_select_locked_product_is_refused(self):
        vmc = make_vmc2()
        vmc.attach_to_loop(asyncio.get_running_loop())
        messages: list[str] = []
        vmc.set_message_callback(messages.append)
        vmc._raise_fault(FaultCode.ICE_301, sku="ICE-1")

        vmc.select_product(0)

        assert vmc.state == "idle"
        assert vmc.selected_product is None
        assert any("ICE-301" in m for m in messages)

    async def test_sellable_products_excludes_locked(self):
        vmc = make_vmc2()
        vmc._raise_fault(FaultCode.ICE_401, sku="ICE-1")
        assert [p.sku for p in vmc._sellable_products()] == ["WATER-1"]

    async def test_clear_fault_records_and_rearms_alert(self):
        vmc = make_vmc2()
        vmc.attach_to_loop(asyncio.get_running_loop())
        rec = FakeEventRecorder()
        vmc.set_event_recorder(rec)
        hm = HealthMonitor()
        vmc.set_health_monitor(hm)
        vmc._raise_fault(FaultCode.ICE_301, sku="ICE-1")
        await asyncio.sleep(0)

        assert vmc.clear_fault("ICE-1") is True
        assert vmc._lockouts == {}
        assert "ICE-301:ICE-1" not in hm._fired_alerts
        assert (
            "lockout_cleared",
            1.0,
            {"code": "ICE-301", "sku": "ICE-1", "by": "admin"},
        ) in rec.events
        assert hm.get_summary()["active_faults"] == []
        assert vmc.clear_fault("ICE-1") is False

    async def test_bin_half_full_auto_clears_ice_101(self):
        vmc = make_vmc2()
        vmc.attach_to_loop(asyncio.get_running_loop())
        rec = FakeEventRecorder()
        vmc.set_event_recorder(rec)
        vmc._raise_fault(FaultCode.ICE_101, sku="ICE-1")
        vmc._raise_fault(FaultCode.ICE_301, sku="WATER-1")

        await vmc._handle_mqtt_hardware_io(
            "hardware/io/bin_half_full", {"device": "bin_half_full", "state": True}
        )

        assert vmc._lockouts == {"WATER-1": FaultCode.ICE_301}
        assert (
            "lockout_cleared",
            1.0,
            {"code": "ICE-101", "sku": "ICE-1", "by": "auto"},
        ) in rec.events

    def test_hardware_io_handler_is_registered(self):
        vmc = make_vmc2()

        class FakeClient:
            def __init__(self):
                self.topics = []

            def register(self, topic, handler):
                self.topics.append(topic)

        client = FakeClient()
        vmc.set_mqtt_client(client)
        assert "hardware/io/+" in client.topics
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_vmc_flows.py::TestFaultRegistry -q`
Expected: 8 failures (`AttributeError: '_raise_fault'` etc.).

- [ ] **Step 3: Implement in `controller/vmc.py`**

Imports (add near the other imports at the top):

```python
from contracts.vending_machine import (
    FAULT_TABLE,
    FaultCode,
    Scope,
    Severity,
)
from services.mqtt_messages import HardwareIO, VMCAlert
```

(`HardwareIO` and `VMCAlert` join the existing `from services.mqtt_messages import ...` line if one exists; do not duplicate the import statement.)

Module-level, after `TRANSITIONS`:

```python
# Alert level sent to the owner for each fault severity.
_SEVERITY_LEVEL = {
    Severity.info: "info",
    Severity.warning: "warning",
    Severity.product_unavailable: "warning",
    Severity.vend_failed: "warning",
    Severity.lockout: "error",
    Severity.critical: "critical",
}
```

In `__init__`, after `self.subsystem_capabilities: dict[str, dict] = {}`:

```python
        # Fault registry: product-scope faults by SKU, machine-scope faults by code.
        self._lockouts: dict[str, FaultCode] = {}
        self._machine_faults: dict[FaultCode, float] = {}
```

In `set_mqtt_client`, add a registration line:

```python
        client.register("hardware/io/+", self._handle_mqtt_hardware_io)
```

Add a fire-and-forget helper next to `_schedule`:

```python
    def _fire_and_forget(self, coro) -> None:
        """Run a coroutine on the attached loop without awaiting it."""
        if self._loop is None or self._loop.is_closed():
            coro.close()
            return
        self._loop.create_task(coro)
```

Add the fault-registry methods (place them after `_publish_status`):

```python
    # --- Fault registry ---

    def _product_name(self, sku: str | None) -> str | None:
        if sku is None:
            return None
        return next((p.name for p in self.products if p.sku == sku), sku)

    def _sellable_products(self) -> list:
        return [p for p in self.products if p.sku not in self._lockouts]

    def active_faults(self) -> list[dict]:
        """Snapshot for the dashboard/health monitor. Product faults first."""
        out = []
        for sku, code in self._lockouts.items():
            spec = FAULT_TABLE[code]
            out.append(
                {
                    "key": sku,
                    "sku": sku,
                    "product": self._product_name(sku),
                    "code": code.value,
                    "severity": spec.severity.value,
                    "scope": spec.scope.value,
                    "description": spec.description,
                }
            )
        for code in self._machine_faults:
            spec = FAULT_TABLE[code]
            out.append(
                {
                    "key": code.value,
                    "sku": None,
                    "product": None,
                    "code": code.value,
                    "severity": spec.severity.value,
                    "scope": spec.scope.value,
                    "description": spec.description,
                }
            )
        return out

    def _push_active_faults(self) -> None:
        if self._health_monitor:
            self._health_monitor.set_active_faults(self.active_faults())

    def _raise_fault(
        self,
        code: FaultCode,
        sku: str | None = None,
        outcome: str | None = None,
    ) -> None:
        """Record a fault: lock the product if its severity says so, alert the owner."""
        spec = FAULT_TABLE[code]
        locks = spec.severity in (Severity.lockout, Severity.product_unavailable)
        if spec.scope is Scope.product and sku is not None and locks:
            if self._lockouts.get(sku) != code:
                self._lockouts[sku] = code
                if self._event_recorder:
                    self._event_recorder.record(
                        "lockout_set", metadata={"code": code.value, "sku": sku}
                    )
        elif spec.scope is Scope.machine:
            self._machine_faults.setdefault(code, time.monotonic())

        name = self._product_name(sku)
        message = f"{code.value} {spec.description}"
        if name:
            message += f" — product '{name}'"
        if outcome:
            message += f" (reported: {outcome})"
        logger.error(f"FAULT {message}")

        key = f"{code.value}:{sku or 'machine'}"
        level = _SEVERITY_LEVEL[spec.severity]
        if self._health_monitor:
            self._fire_and_forget(
                self._health_monitor.raise_alert(
                    key, level, "vmc", message, code=code.value, product_sku=sku
                )
            )
        if self._mqtt_client:
            self._fire_and_forget(
                self._mqtt_client.publish(
                    "alerts",
                    VMCAlert(level=level, message=message, code=code, product_sku=sku),
                )
            )
        self._push_active_faults()

    def clear_fault(self, key: str, by: str = "admin") -> bool:
        """Clear a fault by key (SKU for product faults, code string for machine faults)."""
        code = self._lockouts.pop(key, None)
        if code is not None:
            sku = key
            if self._event_recorder:
                self._event_recorder.record(
                    "lockout_cleared",
                    metadata={"code": code.value, "sku": sku, "by": by},
                )
            if self._health_monitor:
                self._health_monitor.clear_alert(f"{code.value}:{sku}")
            logger.info(f"Fault {code.value} cleared for product {sku} ({by})")
        else:
            try:
                code = FaultCode(key)
            except ValueError:
                return False
            if code not in self._machine_faults:
                return False
            del self._machine_faults[code]
            if self._health_monitor:
                self._health_monitor.clear_alert(f"{code.value}:machine")
            logger.info(f"Machine fault {code.value} cleared ({by})")
        self._push_active_faults()
        self._publish_status()
        return True

    async def _handle_mqtt_hardware_io(self, topic: str, data: dict):
        """Binary hardware IO from the vending ESP32; ice returning clears ICE-101."""
        hw = HardwareIO.model_validate(data)
        if hw.device == "bin_half_full" and hw.state:
            for sku, code in list(self._lockouts.items()):
                if code is FaultCode.ICE_101:
                    self.clear_fault(sku, by="auto")
        else:
            logger.debug(f"MQTT hardware IO: {hw.device}={hw.state}")
```

In `select_product`, replace the line `self.selected_product = self.products[product_index]` and the comment above it with:

```python
        # `product_index` here is the physical button index (ButtonPress.button),
        # not the product's dispense `slot` — buttons stay positional for now.
        candidate = self.products[product_index]
        locked_code = self._lockouts.get(candidate.sku)
        if locked_code is not None:
            txn_log.info(
                f"LOCKED OUT: '{candidate.name}' ({locked_code.value}), customer rejected"
            )
            self.send_customer_message(
                f"{candidate.name} is unavailable ({locked_code.value}). "
                "Please choose another product."
            )
            return
        self.selected_product = candidate
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_vmc_flows.py tests/test_vmc_fsm.py tests/test_mqtt.py -q`
Expected: pass. If `tests/test_mqtt.py` asserts the exact count of registered VMC handlers, update it (+1 for `hardware/io/+`).

- [ ] **Step 5: Lint and commit**

```bash
ruff check --fix .
ruff format .
git add controller/vmc.py tests/test_vmc_flows.py tests/test_mqtt.py
git commit -m "feat: VMC fault registry — per-product lockouts, coded alerts, admin/auto clear

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 7: Honest vend outcomes — `vend_failed` transition and dispense timeout

**Files:**
- Modify: `controller/vmc.py` (`TRANSITIONS`, `__init__`, `_handle_mqtt_dispenser`, `_process_payment`, new `on_vend_failed`, `_fail_vend`, `_dispense_timed_out`)
- Test: `tests/test_vmc_flows.py`

**Interfaces:**
- Consumes: Task 1 (`DispenserOutcome`, `OUTCOME_FAULTS`), Task 3 (`dispense_timeout_seconds`), Task 6 (`_raise_fault`, `_sellable_products`).
- Produces: trigger `vend_failed(code=FaultCode, outcome=str)`; `VMC._fail_vend(code, outcome)`; `VMC._dispense_timed_out()`. `request_refund(reason: str = "admin")` is called by `_fail_vend` — Task 8 gives it the real signature; in this task add the `reason` parameter to the existing method with no other change.

- [ ] **Step 1: Replace and add tests**

In `tests/test_vmc_flows.py`, delete `test_dispenser_jam_refunds_and_enters_error` and, in `test_late_dispenser_fault_after_completed_sale_is_ignored`, change every `"jammed"` to `"jam"`. Then append:

```python
from contracts.vending_machine import DispenserOutcome, OUTCOME_FAULTS


def _start_dispensing(vmc: VMC, index: int = 0):
    vmc.machine.set_state("interacting_with_user")
    vmc.selected_product = vmc.products[index]
    vmc.credit_escrow = vmc.products[index].price
    vmc._process_payment()
    assert vmc.state == "dispensing"


class TestVendOutcomes:
    @pytest.mark.parametrize("outcome", ["timeout", "jam", "bin_empty", "error"])
    async def test_failure_outcome_restores_credit_and_records(self, outcome):
        vmc = make_vmc2()
        vmc.attach_to_loop(asyncio.get_running_loop())
        rec = FakeEventRecorder()
        vmc.set_event_recorder(rec)
        published: list[tuple[str, object]] = []

        class FakeClient:
            def register(self, *_):
                pass

            async def publish(self, topic, payload):
                published.append((topic, payload))

        vmc.set_mqtt_client(FakeClient())
        _start_dispensing(vmc, 0)
        price = vmc.products[0].price

        await vmc._handle_mqtt_dispenser(
            "hardware/dispenser", {"slot": 0, "state": outcome}
        )
        await asyncio.sleep(0)

        code = OUTCOME_FAULTS[DispenserOutcome(outcome)]
        assert vmc.state == "interacting_with_user"
        assert vmc.credit_escrow == price
        assert vmc.selected_product is None
        assert ("vend_failed", price, {"code": code.value, "sku": "ICE-1", "outcome": outcome}) in rec.events
        assert not any(t == "cmd/payment/refund" for t, _ in published)
        assert vmc._lockouts == {"ICE-1": code}
        assert vmc._dispense_timeout_task is None

    async def test_intermediate_state_does_not_end_sale(self):
        vmc = make_vmc2()
        vmc.attach_to_loop(asyncio.get_running_loop())
        _start_dispensing(vmc, 0)
        await vmc._handle_mqtt_dispenser(
            "hardware/dispenser", {"slot": 0, "state": "fill_complete"}
        )
        assert vmc.state == "dispensing"

    async def test_dispense_timeout_is_a_failed_vend(self):
        vmc = make_vmc2()
        vmc.attach_to_loop(asyncio.get_running_loop())
        vmc._dispense_timeout_seconds = 0.01
        rec = FakeEventRecorder()
        vmc.set_event_recorder(rec)
        _start_dispensing(vmc, 0)

        await asyncio.sleep(0.05)

        assert vmc.state == "interacting_with_user"
        assert vmc.credit_escrow == 2.50
        assert ("vend_failed", 2.50, {"code": "PAY-102", "sku": "ICE-1", "outcome": "no_report"}) in rec.events
        assert vmc._lockouts == {}  # PAY-102 is vend_failed severity: no lockout
        assert not any(e[0] == "dispense" for e in rec.events)

    async def test_timeout_seconds_come_from_config(self):
        cfg = ConfigModel()
        cfg.physical.dispense_timeout_seconds = 45.0
        cfg.physical.products = [Product(sku="ICE-1", name="Ice", price=1.0)]
        vmc = VMC(config=cfg)
        assert vmc._dispense_timeout_seconds == 45.0

    async def test_complete_after_failure_is_ignored(self):
        vmc = make_vmc2()
        vmc.attach_to_loop(asyncio.get_running_loop())
        rec = FakeEventRecorder()
        vmc.set_event_recorder(rec)
        _start_dispensing(vmc, 0)
        await vmc._handle_mqtt_dispenser(
            "hardware/dispenser", {"slot": 0, "state": "timeout"}
        )
        await vmc._handle_mqtt_dispenser(
            "hardware/dispenser", {"slot": 0, "state": "complete"}
        )
        assert vmc.state == "interacting_with_user"
        assert not any(e[0] == "dispense" for e in rec.events)
```

Add `import pytest` at the top of the file if it is not already there.

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_vmc_flows.py::TestVendOutcomes -q`
Expected: failures (state stays `error`/`dispensing`, missing attributes).

- [ ] **Step 3: Implement**

Imports: extend the `contracts.vending_machine` import in `controller/vmc.py` to include `DispenserOutcome` and `OUTCOME_FAULTS`.

`TRANSITIONS`: add after the `cancel_sale` entry:

```python
    {
        "trigger": "vend_failed",
        "source": "dispensing",
        "dest": "interacting_with_user",
        "before": "on_vend_failed",
    },
```

`__init__`: after `self._session_timeout_seconds = 180.0  # 3 minutes` add:

```python
        self._dispense_timeout_seconds = (
            self.config_model.physical.dispense_timeout_seconds
        )
```

`_process_payment`: replace

```python
            self._dispense_timeout_task = self._schedule(60.0, self._finish_dispensing)
```

with

```python
            self._dispense_timeout_task = self._schedule(
                self._dispense_timeout_seconds, self._dispense_timed_out
            )
```

and update the comment above it to: `# Dispenser hardware reports a terminal DispenserOutcome via MQTT; no report within the timeout is a failed vend (PAY-102).`

Replace the whole body of `_handle_mqtt_dispenser` with:

```python
    async def _handle_mqtt_dispenser(self, topic: str, data: dict):
        """Handle dispenser status from ESP32.

        Only DispenserOutcome members end a sale; every other `state` string
        is an intermediate hardware step and is logged.
        """
        logger.info(f"MQTT dispenser event: {data}")
        state = data.get("state", "")
        slot = data.get("slot", "?")
        try:
            outcome = DispenserOutcome(state)
        except ValueError:
            vend_log.info(f"DISPENSER: slot {slot}, state: {state}")
            return

        if self.state != "dispensing":
            logger.warning(
                f"Ignoring dispenser outcome '{outcome.value}' outside dispensing "
                f"state (current state: {self.state}, slot {slot})"
            )
            return
        if self._dispenser_event_slot_mismatch(data):
            logger.warning(
                f"Ignoring dispenser outcome '{outcome.value}' for mismatched slot "
                f"{slot} (active sale is slot {self.selected_product.slot})"
            )
            return

        product_name = self.selected_product.name if self.selected_product else "Unknown"
        if outcome is DispenserOutcome.complete:
            txn_log.info(f"DISPENSE SUCCESS: slot {slot}, product '{product_name}'")
            vend_log.info(f"DISPENSE COMPLETE: slot {slot}, product '{product_name}'")
            if self._event_recorder and self.selected_product:
                self._event_recorder.record(
                    "dispense", value=float(self.selected_product.slot)
                )
            self._finish_dispensing()
            return

        self._cancel_dispense_timeout()
        code = OUTCOME_FAULTS[outcome]
        sku = self.selected_product.sku if self.selected_product else None
        txn_log.error(
            f"DISPENSE FAILED: slot {slot}, product '{product_name}', "
            f"outcome: {outcome.value}, fault: {code.value}"
        )
        vend_log.error(
            f"DISPENSE FAILED: slot {slot}, product '{product_name}', "
            f"outcome: {outcome.value}, fault: {code.value}"
        )
        self._raise_fault(code, sku=sku, outcome=outcome.value)
        self._fail_vend(code, outcome=outcome.value)
```

Add these methods after `on_cancel_sale`:

```python
    @logger.catch()
    def on_vend_failed(self, code: FaultCode, outcome: str):
        """`before` hook for dispensing -> interacting_with_user on a failed vend.

        Restores the price to escrow (it was deducted in _process_payment),
        records the failure, and clears the selection. Whether the customer
        stays to choose again or is paid out is decided in _fail_vend.
        """
        product = self.selected_product
        price = product.price if product else 0.0
        name = product.name if product else "Unknown"
        sku = product.sku if product else None
        self._cancel_dispense_timeout()
        self.credit_escrow += price
        logger.error(
            f"{STATE_CHANGE_PREFIX} Vend failed for '{name}' ({code.value}, {outcome}); "
            f"${price:.2f} returned to escrow"
        )
        txn_log.error(
            f"VEND FAILED: '{name}' {code.value} ({outcome}); ${price:.2f} returned to escrow"
        )
        if self._event_recorder:
            self._event_recorder.record(
                "vend_failed",
                value=price,
                metadata={"code": code.value, "sku": sku, "outcome": outcome},
            )
        self.selected_product = None
        self.last_insufficient_message = ""
        self.send_customer_message(
            f"Sorry, {name} could not be dispensed ({code.value}). "
            f"Your ${price:.2f} credit has been kept."
        )

    def _fail_vend(self, code: FaultCode, outcome: str) -> None:
        """Run the vend_failed transition, then decide: choose again, or pay out."""
        self.vend_failed(code=code, outcome=outcome)
        if not self._sellable_products():
            txn_log.info("No sellable products remain; refunding and returning to idle")
            self.request_refund(reason=code.value)
            self._cancel_session_timeout()
            self.machine.set_state("idle")
            self._publish_status()
            self._update_display("idle")
        else:
            self.send_customer_message("Please choose another product.")
            self._reset_session_timeout()
            self._publish_status()
            self._update_display("interacting_with_user")
        self._refresh_ui()

    @logger.catch()
    def _dispense_timed_out(self):
        """No terminal dispenser report arrived within the configured timeout."""
        self._dispense_timeout_task = None
        if self.state != "dispensing":
            return
        sku = self.selected_product.sku if self.selected_product else None
        logger.error(
            f"Dispense timed out after {self._dispense_timeout_seconds:.0f}s with no "
            f"terminal report (slot {self.selected_product.slot if self.selected_product else '?'})"
        )
        self._raise_fault(FaultCode.PAY_102, sku=sku, outcome="no_report")
        self._fail_vend(FaultCode.PAY_102, outcome="no_report")
```

`request_refund`: change the signature to `def request_refund(self, reason: str = "admin"):` and leave the body as is (Task 8 rewrites it).

Check `_finish_dispensing`: it still calls `self._cancel_dispense_timeout()` first; keep that.

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_vmc_flows.py tests/test_vmc_fsm.py tests/test_fsm_control.py tests/test_web_routes.py -q`
Expected: pass. `test_web_routes.py::TestActionEndpoint::test_reset_action_recovers_from_error` uses `error_occurred` directly and is unaffected.

- [ ] **Step 5: Lint and commit**

```bash
ruff check --fix .
ruff format .
git add controller/vmc.py tests/test_vmc_flows.py
git commit -m "feat: failed vends restore credit and lock the product; timeout is PAY-102, never a sale

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 8: Real refunds with acks and retry

**Files:**
- Modify: `controller/vmc.py` (`request_refund`, `on_error`, `set_mqtt_client`, new refund plumbing)
- Test: `tests/test_vmc_flows.py`

**Interfaces:**
- Consumes: Task 1 (`PaymentRefundCommand`, `PaymentRefundResult`, `RefundStatus`), Task 6 (`_raise_fault`).
- Produces:
  - class attrs `VMC.REFUND_ACK_TIMEOUT = 10.0`, `VMC.REFUND_MAX_ATTEMPTS = 2`
  - `VMC._pending_refunds: dict[str, PendingRefund]`
  - `request_refund(reason: str = "admin")` publishes `cmd/payment/refund`
  - `_handle_mqtt_refund_ack(topic, data)` registered on `cmd/payment/refund/ack`
  - events: `refund` (value = amount_returned), `refund_failed` (value = amount)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_vmc_flows.py`:

```python
from contracts.vending_machine import PaymentRefundCommand


class RecordingClient:
    def __init__(self):
        self.published: list[tuple[str, object]] = []

    def register(self, *_):
        pass

    async def publish(self, topic, payload):
        self.published.append((topic, payload))

    def refund_commands(self) -> list[PaymentRefundCommand]:
        return [p for t, p in self.published if t == "cmd/payment/refund"]


class TestRefunds:
    async def test_request_refund_publishes_command_and_zeroes_escrow(self):
        vmc = make_vmc2()
        vmc.attach_to_loop(asyncio.get_running_loop())
        client = RecordingClient()
        vmc.set_mqtt_client(client)
        vmc.credit_escrow = 1.75

        vmc.request_refund(reason="session_timeout")
        await asyncio.sleep(0)

        cmds = client.refund_commands()
        assert len(cmds) == 1
        assert cmds[0].amount == 1.75
        assert cmds[0].reason == "session_timeout"
        assert vmc.credit_escrow == 0.0
        assert cmds[0].request_id in vmc._pending_refunds

    async def test_ack_ok_records_refund(self):
        vmc = make_vmc2()
        vmc.attach_to_loop(asyncio.get_running_loop())
        client = RecordingClient()
        vmc.set_mqtt_client(client)
        rec = FakeEventRecorder()
        vmc.set_event_recorder(rec)
        vmc.credit_escrow = 2.0
        vmc.request_refund(reason="cancel")
        await asyncio.sleep(0)
        rid = client.refund_commands()[0].request_id

        await vmc._handle_mqtt_refund_ack(
            "cmd/payment/refund/ack",
            {"request_id": rid, "status": "ok", "amount_returned": 2.0},
        )

        assert rid not in vmc._pending_refunds
        assert ("refund", 2.0, {"request_id": rid, "reason": "cancel"}) in rec.events

    async def test_ack_failed_retries_once_then_pay_103(self):
        vmc = make_vmc2()
        vmc.attach_to_loop(asyncio.get_running_loop())
        client = RecordingClient()
        vmc.set_mqtt_client(client)
        rec = FakeEventRecorder()
        vmc.set_event_recorder(rec)
        vmc.credit_escrow = 2.0
        vmc.request_refund(reason="cancel")
        await asyncio.sleep(0)
        rid = client.refund_commands()[0].request_id

        await vmc._handle_mqtt_refund_ack(
            "cmd/payment/refund/ack",
            {"request_id": rid, "status": "failed", "detail": "changer_empty"},
        )
        await asyncio.sleep(0)
        assert [c.request_id for c in client.refund_commands()] == [rid, rid]
        assert rid in vmc._pending_refunds

        await vmc._handle_mqtt_refund_ack(
            "cmd/payment/refund/ack",
            {"request_id": rid, "status": "failed", "detail": "changer_empty"},
        )
        await asyncio.sleep(0)

        assert rid not in vmc._pending_refunds
        assert len(client.refund_commands()) == 2
        assert (
            "refund_failed",
            2.0,
            {"request_id": rid, "reason": "cancel", "detail": "changer_empty"},
        ) in rec.events
        assert "PAY-103" in [f["code"] for f in vmc.active_faults()]

    async def test_no_ack_deadline_retries_then_pay_103(self):
        vmc = make_vmc2()
        vmc.attach_to_loop(asyncio.get_running_loop())
        vmc.REFUND_ACK_TIMEOUT = 0.01
        client = RecordingClient()
        vmc.set_mqtt_client(client)
        rec = FakeEventRecorder()
        vmc.set_event_recorder(rec)
        vmc.credit_escrow = 3.0
        vmc.request_refund(reason="error")

        await asyncio.sleep(0.1)

        assert len(client.refund_commands()) == 2
        assert vmc._pending_refunds == {}
        assert any(
            e[0] == "refund_failed" and e[2]["detail"] == "ack_timeout" for e in rec.events
        )
        assert "PAY-103" in [f["code"] for f in vmc.active_faults()]

    async def test_unknown_request_id_ack_is_ignored(self):
        vmc = make_vmc2()
        vmc.attach_to_loop(asyncio.get_running_loop())
        rec = FakeEventRecorder()
        vmc.set_event_recorder(rec)
        await vmc._handle_mqtt_refund_ack(
            "cmd/payment/refund/ack",
            {"request_id": "x" * 32, "status": "ok", "amount_returned": 1.0},
        )
        assert rec.events == []

    async def test_on_error_pays_out_via_refund_command(self):
        vmc = make_vmc2()
        vmc.attach_to_loop(asyncio.get_running_loop())
        client = RecordingClient()
        vmc.set_mqtt_client(client)
        vmc.machine.set_state("interacting_with_user")
        vmc.credit_escrow = 1.25

        vmc.error_occurred()
        await asyncio.sleep(0)

        assert vmc.state == "error"
        assert vmc.credit_escrow == 0.0
        cmds = client.refund_commands()
        assert len(cmds) == 1 and cmds[0].amount == 1.25 and cmds[0].reason == "error"

    async def test_all_products_locked_refunds_and_idles(self):
        vmc = make_vmc()  # single product
        vmc.attach_to_loop(asyncio.get_running_loop())
        client = RecordingClient()
        vmc.set_mqtt_client(client)
        _start_dispensing(vmc, 0)

        await vmc._handle_mqtt_dispenser(
            "hardware/dispenser", {"slot": 0, "state": "jam"}
        )
        await asyncio.sleep(0)

        assert vmc.state == "idle"
        assert vmc.credit_escrow == 0.0
        refunds = client.refund_commands()
        assert len(refunds) == 1
        assert refunds[0].amount == 2.50
        assert refunds[0].reason == "ICE-401"

    def test_refund_ack_handler_is_registered(self):
        vmc = make_vmc2()
        client = RecordingClient()
        topics = []
        client.register = lambda topic, handler: topics.append(topic)
        vmc.set_mqtt_client(client)
        assert "cmd/payment/refund/ack" in topics
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_vmc_flows.py::TestRefunds -q`
Expected: 8 failures.

- [ ] **Step 3: Implement**

Imports in `controller/vmc.py`: extend the `contracts.vending_machine` import with `PaymentRefundCommand, PaymentRefundResult, RefundStatus`; add `from dataclasses import dataclass` and `from uuid import uuid4` if not present.

Module level, after `_SEVERITY_LEVEL`:

```python
@dataclass
class PendingRefund:
    request_id: str
    amount: float
    reason: str
    attempts: int = 1
    deadline_task: asyncio.Task | None = None
```

Class attributes on `VMC` (right under `states = [...]`):

```python
    REFUND_ACK_TIMEOUT = 10.0  # seconds to wait for cmd/payment/refund/ack
    REFUND_MAX_ATTEMPTS = 2  # one retry with the same request_id, then PAY-103
```

`__init__`: after the `_machine_faults` line add `self._pending_refunds: dict[str, PendingRefund] = {}`.

`set_mqtt_client`: add `client.register("cmd/payment/refund/ack", self._handle_mqtt_refund_ack)`.

`cancel_pending_tasks`: after `self._cancel_session_timeout()` add:

```python
        for pending in self._pending_refunds.values():
            if pending.deadline_task and not pending.deadline_task.done():
                pending.deadline_task.cancel()
```

Replace `request_refund` entirely:

```python
    @logger.catch()
    def request_refund(self, reason: str = "admin"):
        """Pay the customer back: publish a refund command and await its ack.

        This is the ONLY path that sends money out. Restoring a price to
        escrow after a failed vend is not a refund and does not come here.
        """
        logger.debug(f"Requesting refund with current credit: {self.credit_escrow:.2f}")
        if self.credit_escrow <= 0:
            self.send_customer_message("No funds to refund.")
            return
        amount = round(self.credit_escrow, 2)
        self.credit_escrow = 0.0
        pending = PendingRefund(request_id=uuid4().hex, amount=amount, reason=reason)
        self._pending_refunds[pending.request_id] = pending
        self._send_refund_command(pending)
        logger.info(
            f"Refund of ${amount:.2f} requested via {self.last_payment_method} "
            f"(reason={reason}, request_id={pending.request_id})"
        )
        txn_log.info(
            f"REFUND REQUESTED: ${amount:.2f} via {self.last_payment_method} "
            f"reason={reason} request_id={pending.request_id}"
        )
        self.send_customer_message(
            f"Refund of ${amount:.2f} issued via {self.last_payment_method}."
        )
        self._refresh_ui()

    def _send_refund_command(self, pending: PendingRefund) -> None:
        cmd = PaymentRefundCommand(
            request_id=pending.request_id, amount=pending.amount, reason=pending.reason
        )
        if self._mqtt_client is not None:
            self._fire_and_forget(self._mqtt_client.publish("cmd/payment/refund", cmd))
        else:
            logger.warning("No MQTT client; refund command not sent")
        pending.deadline_task = self._schedule(
            self.REFUND_ACK_TIMEOUT, lambda: self._refund_deadline(pending.request_id)
        )

    async def _handle_mqtt_refund_ack(self, topic: str, data: dict):
        """Payment gateway acknowledged (or refused) a refund command."""
        result = PaymentRefundResult.model_validate(data)
        pending = self._pending_refunds.get(result.request_id)
        if pending is None:
            logger.warning(f"Refund ack for unknown request_id {result.request_id}")
            return
        if result.status is RefundStatus.ok:
            self._refund_confirmed(pending, result.amount_returned)
        else:
            self._refund_attempt_failed(pending, detail=result.detail or result.status.value)

    def _cancel_refund_deadline(self, pending: PendingRefund) -> None:
        if pending.deadline_task and not pending.deadline_task.done():
            pending.deadline_task.cancel()
        pending.deadline_task = None

    def _refund_confirmed(self, pending: PendingRefund, amount_returned: float) -> None:
        self._cancel_refund_deadline(pending)
        self._pending_refunds.pop(pending.request_id, None)
        txn_log.info(
            f"REFUND CONFIRMED: ${amount_returned:.2f} request_id={pending.request_id}"
        )
        if self._event_recorder:
            self._event_recorder.record(
                "refund",
                value=amount_returned,
                metadata={"request_id": pending.request_id, "reason": pending.reason},
            )

    def _refund_deadline(self, request_id: str) -> None:
        pending = self._pending_refunds.get(request_id)
        if pending is None:
            return
        pending.deadline_task = None
        self._refund_attempt_failed(pending, detail="ack_timeout")

    def _refund_attempt_failed(self, pending: PendingRefund, detail: str) -> None:
        self._cancel_refund_deadline(pending)
        if pending.attempts < self.REFUND_MAX_ATTEMPTS:
            pending.attempts += 1
            logger.warning(
                f"Refund {pending.request_id} not confirmed ({detail}); "
                f"retry {pending.attempts}/{self.REFUND_MAX_ATTEMPTS}"
            )
            self._send_refund_command(pending)
            return
        self._pending_refunds.pop(pending.request_id, None)
        txn_log.error(
            f"REFUND FAILED: ${pending.amount:.2f} request_id={pending.request_id} "
            f"reason={pending.reason} detail={detail}"
        )
        if self._event_recorder:
            self._event_recorder.record(
                "refund_failed",
                value=pending.amount,
                metadata={
                    "request_id": pending.request_id,
                    "reason": pending.reason,
                    "detail": detail,
                },
            )
        self._raise_fault(FaultCode.PAY_103, outcome=detail)
```

`on_error`: replace the inline escrow block

```python
        # Refund any remaining credit in escrow
        if self.credit_escrow > 0:
            refund = self.credit_escrow
            self.credit_escrow = 0.0
            txn_log.info(
                f"REFUND (error state): ${refund:.2f} via {self.last_payment_method}"
            )
            logger.info(f"Refunded ${refund:.2f} due to error state transition.")
```

with

```python
        # Pay out any remaining credit through the gateway
        if self.credit_escrow > 0:
            self.request_refund(reason="error")
```

`_expire_session`: change `self.request_refund()` to `self.request_refund(reason="session_timeout")`.
`on_cancel_sale`: change `self.request_refund()` to `self.request_refund(reason="cancel")`.

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_vmc_flows.py tests/test_vmc_fsm.py tests/test_web_routes.py tests/test_fsm_control.py -q`
Expected: pass. If an existing test asserted the old `"REFUND ISSUED"` log text or that `on_error` sends "refunded" in the message, update it to the new wording (`"Refund of $x issued via ..."` is still sent by `request_refund`).

- [ ] **Step 5: Lint and commit**

```bash
ruff check --fix .
ruff format .
git add controller/vmc.py tests/test_vmc_flows.py
git commit -m "feat: refunds are real — cmd/payment/refund with ack, one retry, PAY-103 on failure

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 9: MDB simulator answers refunds

**Files:**
- Modify: `simulators/mdb_gateway.py`
- Test: `tests/test_simulator_mdb.py`

**Interfaces:**
- Consumes: Task 1 models.
- Produces: `MDBGatewaySimulator.REFUND_DELAY_RANGE = (0.5, 2.0)`, `REFUND_RESULTS_MAX = 256`, `_refund_results: OrderedDict[str, PaymentRefundResult]`, `async _handle_refund(client, cmd: PaymentRefundCommand)`, `async _refund_loop(client)`, fault `changer_empty`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_simulator_mdb.py`:

```python
from contracts.vending_machine import PaymentRefundCommand, RefundStatus


class TestRefunds:
    def _sim(self):
        sim = MDBGatewaySimulator()
        sim.REFUND_DELAY_RANGE = (0.0, 0.0)
        sim.publish = AsyncMock()
        return sim

    def _acks(self, sim):
        return [
            call.args[2]
            for call in sim.publish.await_args_list
            if call.args[1] == "cmd/payment/refund/ack"
        ]

    async def test_refund_acked_ok_with_amount(self):
        sim = self._sim()
        cmd = PaymentRefundCommand(request_id="r" * 32, amount=2.5, reason="cancel")
        await sim._handle_refund(None, cmd)
        acks = self._acks(sim)
        assert len(acks) == 1
        assert acks[0].status is RefundStatus.ok
        assert acks[0].amount_returned == 2.5
        assert acks[0].request_id == "r" * 32

    async def test_repeated_request_id_resends_stored_result(self):
        sim = self._sim()
        cmd = PaymentRefundCommand(request_id="r" * 32, amount=2.5, reason="cancel")
        await sim._handle_refund(None, cmd)
        await sim._handle_refund(None, cmd)
        acks = self._acks(sim)
        assert len(acks) == 2
        assert acks[0] is acks[1]  # same stored object, no second pay-out
        assert len(sim._refund_results) == 1

    async def test_changer_empty_fault_answers_failed(self):
        sim = self._sim()
        sim._fault_state["changer_empty"]["active"] = True
        cmd = PaymentRefundCommand(request_id="r" * 32, amount=2.5, reason="cancel")
        await sim._handle_refund(None, cmd)
        ack = self._acks(sim)[0]
        assert ack.status is RefundStatus.failed
        assert ack.amount_returned == 0.0
        assert ack.detail == "changer_empty"

    async def test_result_cache_is_bounded(self):
        sim = self._sim()
        sim.REFUND_RESULTS_MAX = 3
        for i in range(5):
            cmd = PaymentRefundCommand(
                request_id=f"{i:032d}", amount=1.0, reason="cancel"
            )
            await sim._handle_refund(None, cmd)
        assert list(sim._refund_results) == ["2".zfill(32), "3".zfill(32), "4".zfill(32)]

    def test_changer_empty_fault_registered(self):
        sim = MDBGatewaySimulator()
        assert "changer_empty" in sim._fault_state
```

Update `TestMDBFaultRegistration.test_four_faults_registered` to expect five faults and rename it `test_five_faults_registered`; add `"changer_empty"` to the expected names in `test_fault_names`.

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_simulator_mdb.py -q`
Expected: new tests fail with `AttributeError`; the two updated registration tests fail on count/names.

- [ ] **Step 3: Implement**

In `simulators/mdb_gateway.py`:

Imports:

```python
from collections import OrderedDict

from pydantic import ValidationError

from contracts.vending_machine import (
    PaymentRefundCommand,
    PaymentRefundResult,
    RefundStatus,
)
```

Class attributes (next to `DEVICE_STATUS_INTERVAL`):

```python
    REFUND_DELAY_RANGE = (0.5, 2.0)  # seconds the changer takes to pay out
    REFUND_RESULTS_MAX = 256  # idempotency cache bound, oldest evicted first
```

`__init__`: after `self._vmc_status: asyncio.Queue = asyncio.Queue()` add:

```python
        # request_id -> result, so a repeated refund command is never paid twice
        self._refund_results: OrderedDict[str, PaymentRefundResult] = OrderedDict()
```

Register the fault after `mdb_bus_reset`:

```python
        self.register_fault(
            FaultDef(
                name="changer_empty",
                category="medium",
                probability=0.0003,
                on_activate=self._on_changer_empty_activate,
                on_recover=self._on_changer_empty_recover,
                message="Coin changer empty — refunds cannot be paid out",
                severity="warning",
            )
        )
```

Add methods (after `_on_mdb_bus_reset_recover`):

```python
    async def _on_changer_empty_activate(self, client: aiomqtt.Client) -> None:
        logger.warning("[mdb] FAULT: changer empty — refunds will fail")

    async def _on_changer_empty_recover(self, client: aiomqtt.Client) -> None:
        logger.info("[mdb] Fault cleared: changer_empty")

    async def _refund_loop(self, client: aiomqtt.Client):
        """Answer VMC refund commands from the subscription queue."""
        topic = f"{self.topic_prefix}/cmd/payment/refund"
        queue = await self.subscribe(client, topic)
        logger.info(f"[mdb] Listening for refund commands on {topic}")
        while True:
            _topic, data = await queue.get()
            try:
                cmd = PaymentRefundCommand.model_validate(data)
            except ValidationError as e:
                logger.error(f"[mdb] Bad refund command ignored: {e}")
                continue
            await self._handle_refund(client, cmd)

    async def _handle_refund(
        self, client: aiomqtt.Client, cmd: PaymentRefundCommand
    ) -> None:
        cached = self._refund_results.get(cmd.request_id)
        if cached is not None:
            logger.info(f"[mdb] Refund {cmd.request_id}: repeat request, re-sending result")
            await self.publish(client, "cmd/payment/refund/ack", cached)
            return

        await asyncio.sleep(random.uniform(*self.REFUND_DELAY_RANGE))
        if "changer_empty" in self._active_fault_names:
            result = PaymentRefundResult(
                request_id=cmd.request_id,
                status=RefundStatus.failed,
                amount_returned=0.0,
                detail="changer_empty",
            )
            logger.warning(
                f"[mdb] Refund {cmd.request_id}: FAILED, changer empty (${cmd.amount:.2f})"
            )
        else:
            result = PaymentRefundResult(
                request_id=cmd.request_id,
                status=RefundStatus.ok,
                amount_returned=cmd.amount,
            )
            logger.info(
                f"[mdb] Refund {cmd.request_id}: paid out ${cmd.amount:.2f} ({cmd.reason})"
            )
        self._refund_results[cmd.request_id] = result
        while len(self._refund_results) > self.REFUND_RESULTS_MAX:
            self._refund_results.popitem(last=False)
        await self.publish(client, "cmd/payment/refund/ack", result)
```

`run_simulation`: add `tg.create_task(self._refund_loop(client))`.

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_simulator_mdb.py tests/test_simulator_base.py -q`
Expected: pass.

- [ ] **Step 5: Lint and commit**

```bash
ruff check --fix .
ruff format .
git add simulators/mdb_gateway.py tests/test_simulator_mdb.py
git commit -m "feat: MDB simulator answers refund commands (idempotent, changer_empty fault)

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 10: Vending simulator publishes contract outcomes

**Files:**
- Modify: `simulators/vending_machine.py`
- Test: `tests/test_simulator_vending.py`

**Interfaces:**
- Consumes: Task 1 `DispenserOutcome`.
- Produces: every terminal `DispenserStatus` the simulator publishes has `state` equal to a `DispenserOutcome` value.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_simulator_vending.py`:

```python
from contracts.vending_machine import DispenserOutcome
from services.mqtt_messages import DispenserStatus


class TestTerminalOutcomesFollowContract:
    def _run(self, sim, coro):
        with patch("simulators.vending_machine.asyncio.sleep", new=AsyncMock()):
            asyncio.run(coro)
        return [
            call.args[2]
            for call in sim.publish.await_args_list
            if call.args[1] == "hardware/dispenser"
        ]

    @pytest.mark.parametrize(
        "fault,expected",
        [
            (None, DispenserOutcome.complete),
            ("ice_bin_empty", DispenserOutcome.bin_empty),
            ("auger_jam", DispenserOutcome.timeout),
            ("bag_drop_solenoid_stuck", DispenserOutcome.jam),
        ],
    )
    def test_ice_dispense_ends_with_a_contract_outcome(self, fault, expected):
        sim = _make_sim()
        sim.publish = AsyncMock()
        if fault:
            sim._fault_state[fault]["active"] = True
        statuses = self._run(sim, sim._run_ice_dispense(None, 0))
        last = statuses[-1]
        assert isinstance(last, DispenserStatus)
        assert DispenserOutcome(last.state) is expected

    def test_water_dispense_ends_complete(self):
        sim = _make_sim()
        sim.publish = AsyncMock()
        statuses = self._run(sim, sim._run_water_dispense(None, 1))
        assert DispenserOutcome(statuses[-1].state) is DispenserOutcome.complete
```

If `simulators/vending_machine.py` registers a fault by a different name than `ice_bin_empty`, use the registered name (check `sim._fault_state.keys()`).

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_simulator_vending.py::TestTerminalOutcomesFollowContract -q`
Expected: the tests may already pass because the strings coincide. That is fine: the purpose is the guard. If they pass, continue; the implementation step still makes the simulator use the enum so a future rename breaks here.

- [ ] **Step 3: Use the enum in the simulator**

In `simulators/vending_machine.py` add `from contracts.vending_machine import DispenserOutcome` and replace each terminal publish's literal:

- `state="bin_empty"` → `state=DispenserOutcome.bin_empty.value`
- `state="timeout"` → `state=DispenserOutcome.timeout.value`
- `state="jam"` → `state=DispenserOutcome.jam.value`
- every `state="complete"` in `_run_ice_dispense` and `_run_water_dispense` → `state=DispenserOutcome.complete.value`

Leave `motor_active`, `fill_complete`, `solenoid_open` as plain strings (intermediate steps).

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_simulator_vending.py -q`
Expected: pass.

- [ ] **Step 5: Lint and commit**

```bash
ruff check --fix .
ruff format .
git add simulators/vending_machine.py tests/test_simulator_vending.py
git commit -m "test: vending simulator terminal outcomes are pinned to the contract enum

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 11: Dashboard — active faults, clear, counters

**Files:**
- Modify: `web_interface/routes.py`
- Modify: `web_interface/templates/partials/status_fragment.html`, `inventory_table.html`, `kpi_fragment.html`, `activity_fragment.html`
- Test: `tests/test_web_routes.py`

**Interfaces:**
- Consumes: Task 6 (`vmc.active_faults()`, `vmc.clear_fault(key)`), Task 4 (`vends_failed`, `refunds`).
- Produces: `POST /faults/{key}/clear` → re-rendered status fragment; status fragment context gains `active_faults`; inventory renders get `locked: dict[str, str]`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_web_routes.py`:

```python
from contracts.vending_machine import FaultCode


class TestFaultsUI:
    def _lock(self, client):
        vmc = routes.vmc_instance
        vmc._raise_fault(FaultCode.ICE_301, sku=routes.config.products[0].sku)

    def _add_product(self, client):
        client.post(
            "/inventory/add",
            data={"sku": "ICE-1", "name": "Ice", "price": "2.5"},
            auth=client.auth,
        )

    def test_status_lists_active_fault_with_clear_button(self, client):
        self._add_product(client)
        self._lock(client)
        r = client.get("/status", auth=client.auth)
        assert r.status_code == 200
        assert "ICE-301" in r.text
        assert "Ice" in r.text
        assert 'hx-post="/faults/ICE-1/clear"' in r.text
        assert "Issues Detected" in r.text

    def test_status_without_faults_says_none(self, client):
        r = client.get("/status", auth=client.auth)
        assert "No active faults" in r.text

    def test_clear_endpoint_clears_and_rerenders(self, client):
        self._add_product(client)
        self._lock(client)
        r = client.post("/faults/ICE-1/clear", auth=client.auth)
        assert r.status_code == 200
        assert "ICE-301" not in r.text
        assert routes.vmc_instance.active_faults() == []

    def test_clear_unknown_key_returns_404(self, client):
        r = client.post("/faults/NOPE/clear", auth=client.auth)
        assert r.status_code == 404

    def test_inventory_table_shows_locked_badge(self, client):
        self._add_product(client)
        self._lock(client)
        r = client.get("/inventory", auth=client.auth)
        assert "locked" in r.text.lower()
        assert "ICE-301" in r.text

    def test_kpi_shows_failed_vends(self, client, tmp_path):
        from services.event_recorder import EventRecorder

        rec = EventRecorder(db_path=str(tmp_path / "events.db"))
        rec.record("vend_failed", value=2.5, metadata={"code": "ICE-301"})
        routes.set_event_recorder(rec)
        try:
            r = client.get("/kpi", auth=client.auth)
            assert "1 failed" in r.text
        finally:
            routes.set_event_recorder(None)

    def test_activity_shows_failed_vends_and_refunds(self, client, tmp_path):
        from services.event_recorder import EventRecorder

        rec = EventRecorder(db_path=str(tmp_path / "events.db"))
        rec.record("vend_failed", value=2.5)
        rec.record("refund", value=2.5)
        routes.set_event_recorder(rec)
        try:
            r = client.get("/activity", auth=client.auth)
            assert "Failed Vends" in r.text
            assert "Refunds Paid" in r.text
            assert "$2.50" in r.text
        finally:
            routes.set_event_recorder(None)
```

Check the inventory list route path used by the existing `test_inventory_list` and use the same path in `test_inventory_table_shows_locked_badge`.

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_web_routes.py::TestFaultsUI -q`
Expected: 7 failures.

- [ ] **Step 3: Routes**

In `web_interface/routes.py`, inside `attach_routes`, add a helper before the `/status` route and refactor the status route to use it:

```python
    def _locked_skus() -> dict[str, str]:
        if not vmc_instance:
            return {}
        return {
            f["sku"]: f["code"]
            for f in vmc_instance.active_faults()
            if f["scope"] == "product"
        }

    async def _render_status(request: Request):
        if not vmc_instance:
            return HTMLResponse(
                '<div class="bg-red-50 rounded-xl border border-red-200 shadow-sm p-5">'
                '<p class="text-red-600 font-semibold">VMC not initialized</p></div>'
            )

        status = vmc_instance.get_status()
        issues: list[str] = []
        active_faults = vmc_instance.active_faults()
        for f in active_faults:
            target = f["product"] or "machine"
            issues.append(f"{f['code']} {f['description']} ({target})")

        if event_recorder:
            summary_24h = await asyncio.to_thread(event_recorder.get_summary, 24)
            errors_24h = summary_24h["errors"]
            if errors_24h > 0:
                issues.append(
                    f"{errors_24h} error{'s' if errors_24h != 1 else ''} in last 24h"
                )

        if health_monitor:
            health = health_monitor.get_summary()
            stale = [name for name, sub in health["subsystems"].items() if sub["stale"]]
            if stale:
                issues.append(f"Stale subsystems: {', '.join(stale)}")
            out_of_range = [
                loc
                for loc, temp in health["temperatures"].items()
                if not temp["in_range"]
            ]
            if out_of_range:
                issues.append(f"Temp issues: {', '.join(out_of_range)}")

        return templates.TemplateResponse(
            "partials/status_fragment.html",
            {
                "request": request,
                "status": status,
                "is_healthy": len(issues) == 0,
                "issues": issues,
                "active_faults": active_faults,
            },
        )

    @router.get("/status", response_class=HTMLResponse)
    async def status_fragment(request: Request):
        return await _render_status(request)

    @router.post("/faults/{key}/clear", response_class=HTMLResponse)
    async def clear_fault(request: Request, key: str):
        if not vmc_instance or not vmc_instance.clear_fault(key, by="admin"):
            raise HTTPException(status_code=404, detail=f"No active fault with key {key}")
        return await _render_status(request)
```

Every place that renders `partials/inventory_table.html` (the list route, `/inventory/add`, `/inventory/update/{sku}`, `/inventory/delete/{sku}`) gets `"locked": _locked_skus()` added to its context dict.

- [ ] **Step 4: Templates**

`status_fragment.html`: inside both branches, after the `<p class="text-sm text-gray-400 mt-1">…</p>` / `</ul>` block and before the closing `</div>` of the left column, insert:

```html
        <div class="mt-3">
          <div class="text-xs text-gray-400 uppercase tracking-wide mb-1">Active faults</div>
          {% if active_faults %}
          <ul class="space-y-1">
            {% for f in active_faults %}
            <li class="flex items-center gap-2 text-sm">
              <span class="font-mono text-xs px-1.5 py-0.5 rounded
                {% if f.severity == 'critical' %}bg-red-100 text-red-700
                {% elif f.severity == 'lockout' %}bg-orange-100 text-orange-700
                {% else %}bg-amber-100 text-amber-700{% endif %}">{{ f.code }}</span>
              <span class="text-gray-700">{{ f.description }}</span>
              <span class="text-gray-400">· {{ f.product or "machine" }}</span>
              <button hx-post="/faults/{{ f.key }}/clear" hx-target="#status-panel" hx-swap="innerHTML"
                      class="ml-auto text-xs text-blue-600 hover:underline">Clear</button>
            </li>
            {% endfor %}
          </ul>
          {% else %}
          <p class="text-sm text-gray-400">No active faults</p>
          {% endif %}
        </div>
```

Check `dashboard.html` for the element that wraps the status fragment; if its id is not `status-panel`, use the real id in `hx-target`.

`inventory_table.html`: in the product row, change the name cell to:

```html
        <td class="py-3 text-gray-900">
          {{ product.name }}
          {% if locked and product.sku in locked %}
          <span class="ml-2 text-xs font-mono px-1.5 py-0.5 rounded bg-orange-100 text-orange-700"
                title="Locked out">locked · {{ locked[product.sku] }}</span>
          {% endif %}
        </td>
```

`kpi_fragment.html`: in the Products Out card, replace the `<div class="text-xs text-gray-400 mt-1">` block with:

```html
  <div class="text-xs text-gray-400 mt-1">
    {% if average and average.products_out is not none %}
      avg {{ average.products_out }}
    {% else %}
      <span class="text-gray-300">no history yet</span>
    {% endif %}
    {% if summary.vends_failed %}
      <span class="ml-2 text-red-500">· {{ summary.vends_failed }} failed</span>
    {% endif %}
  </div>
```

`activity_fragment.html`: after the Products Out row add two rows:

```html
      {# Failed Vends #}
      <tr>
        <td class="px-5 py-3 text-gray-500">Failed Vends</td>
        <td class="px-5 py-3 text-right">
          <span class="font-medium {{ 'text-red-600' if s.vends_failed else 'text-gray-900' }}">{{ s.vends_failed }}</span>
          {% if a.vends_failed is not none %}
            <span class="ml-2 text-xs text-gray-400">avg {{ a.vends_failed }}</span>
          {% else %}
            <span class="ml-2 text-xs text-gray-300">avg —</span>
          {% endif %}
        </td>
      </tr>

      {# Refunds Paid #}
      <tr>
        <td class="px-5 py-3 text-gray-500">Refunds Paid</td>
        <td class="px-5 py-3 text-right">
          <span class="font-medium text-gray-900">${{ "%.2f"|format(s.refunds) }}</span>
          {% if a.refunds is not none %}
            <span class="ml-2 text-xs text-gray-400">avg ${{ "%.2f"|format(a.refunds) }}</span>
          {% else %}
            <span class="ml-2 text-xs text-gray-300">avg —</span>
          {% endif %}
        </td>
      </tr>
```

- [ ] **Step 5: Run tests**

Run: `uv run pytest tests/test_web_routes.py -q`
Expected: pass.

- [ ] **Step 6: Lint and commit**

```bash
ruff check --fix .
ruff format .
git add web_interface tests/test_web_routes.py
git commit -m "feat: dashboard shows active faults with Clear, locked badge, failed-vend and refund counters

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 12: Docs and end-to-end scenario

**Files:**
- Modify: `ROADMAP.md`, `CLAUDE.md`
- Modify: `tests/test_integration_e2e.py`

- [ ] **Step 1: ROADMAP.md**

In §2, replace the bullet that starts `- The VMC never declares a vend successful because it sent the command.` with:

```markdown
- The VMC never declares a vend successful because it sent the command. It
  waits for the ESP32's `complete` report for the slot it commanded. No
  terminal report within `physical.dispense_timeout_seconds` is a failed vend
  (`PAY-102`), never a sale.
```

In §5 table: change the `ICE-401` row's severity to `lockout ice` and response to `Stop, lock out ice until service`; add after `PAY-102`:

```markdown
| `PAY-103` | Refund not confirmed by payment gateway | warning | Alert; operator reconciles against the event history |
```

In §7 replace the second bullet (`The price is moved from escrow…`) with:

```markdown
- The price is moved from escrow at the start of `dispensing`. A terminal
  failure report (`bin_empty`, `timeout`, `jam`, `error`) or the dispense
  timeout returns the price to escrow, locks out the product per its fault
  severity, and keeps the customer in the session to choose again. If nothing
  sellable remains the VMC pays out immediately. Late or duplicate reports
  after a completed sale, and reports for another slot, are ignored.
- A refund is a `cmd/payment/refund` command acked by the gateway within
  10 s; one retry with the same `request_id`, then `PAY-103` for
  reconciliation. Escrow bookkeeping alone is never called a refund.
```

In §9 Phase C, remove the bullet `Dispense timeout becomes a failed vend…` and add at the top of the Phase C list:

```markdown
- ~~Fault-code registry, honest vend outcomes, per-product lockouts, acked
  refunds~~ — done (spec `docs/superpowers/specs/2026-09-17-fault-registry-vend-outcomes-design.md`).
```

- [ ] **Step 2: CLAUDE.md**

Replace the FSM sentence `States: \`idle\` -> \`interacting_with_user\` -> \`dispensing\` -> back to \`idle\` (or \`error\` from any state).` with:

```markdown
States: `idle` -> `interacting_with_user` -> `dispensing` -> back to `idle` (or `error` from any state). Extra transitions: `cancel_sale` (interacting → idle, catalog edit removed the selection) and `vend_failed` (dispensing → interacting, price restored to escrow, product locked out per `contracts/vending_machine.py` `FAULT_TABLE`). Refunds are real: `request_refund` publishes `cmd/payment/refund` and tracks the ack.
```

- [ ] **Step 3: End-to-end scenario**

Append to `tests/test_integration_e2e.py` (inside the module, using the existing `_make_config`, `_wait_for_state`, `MQTTClient`, `VMC`, `HealthMonitor` imports and the same skip marker):

```python
class TestFailedVendLoop:
    """jam → vend_failed → lockout → clear → sell again, through a real broker."""

    async def test_jam_locks_product_then_clear_sells_again(self):
        config = _make_config()
        prefix = f"vmc/{config.machine_id}"
        mqtt_client = MQTTClient(config=config.mqtt, machine_id=config.machine_id)
        vmc = VMC(config=config)
        health = HealthMonitor()

        async with aiomqtt.Client(
            hostname="localhost", port=1883, identifier="e2e-fault-sim"
        ) as sim_client:
            await sim_client.subscribe(f"{prefix}/cmd/dispense")
            await sim_client.subscribe(f"{prefix}/cmd/payment/refund")
            loop = asyncio.get_running_loop()
            vmc.attach_to_loop(loop)
            vmc.set_mqtt_client(mqtt_client)
            vmc.set_health_monitor(health)
            mqtt_task = asyncio.create_task(mqtt_client.run())
            try:
                for _ in range(50):
                    if mqtt_client._connected:
                        break
                    await asyncio.sleep(0.1)

                async def sale(outcome: str):
                    await sim_client.publish(
                        f"{prefix}/hardware/buttons", json.dumps({"button": 0})
                    )
                    await _wait_for_state(vmc, "interacting_with_user")
                    await sim_client.publish(
                        f"{prefix}/payment/credit",
                        json.dumps({"amount": 2.00, "method": "cash_bill"}),
                    )
                    await _wait_for_state(vmc, "dispensing")
                    await sim_client.publish(
                        f"{prefix}/hardware/dispenser",
                        json.dumps({"slot": 0, "state": outcome}),
                    )

                await sale("jam")
                await _wait_for_state(vmc, "interacting_with_user")
                assert vmc.credit_escrow == 2.00
                assert vmc._lockouts["ICE-SM"].value == "ICE-401"
                assert health.get_summary()["active_faults"][0]["code"] == "ICE-401"

                # Customer walks away: session expiry pays out via the gateway.
                vmc._expire_session()
                msg = await asyncio.wait_for(anext(aiter(sim_client.messages)), 5)
                assert str(msg.topic).endswith("/cmd/payment/refund")
                assert json.loads(msg.payload)["amount"] == 2.00
                await _wait_for_state(vmc, "idle")

                assert vmc.clear_fault("ICE-SM") is True
                await sale("complete")
                await _wait_for_state(vmc, "idle")
                assert vmc._lockouts == {}
            finally:
                vmc.cancel_pending_tasks()
                mqtt_task.cancel()
                try:
                    await mqtt_task
                except asyncio.CancelledError:
                    pass
```

If the existing file iterates `sim_client.messages` differently (e.g. `async for`), match that style. If `aiter/anext` on `sim_client.messages` does not work with the installed aiomqtt, replace the two `msg` lines with:

```python
                async for msg in sim_client.messages:
                    if str(msg.topic).endswith("/cmd/payment/refund"):
                        assert json.loads(msg.payload)["amount"] == 2.00
                        break
```

- [ ] **Step 4: Run the whole suite**

Run: `uv run pytest -q`
Expected: all pass; e2e still skipped without a broker (9 skipped). If mosquitto on localhost:1883 is reachable, the e2e test runs; run `uv run pytest tests/test_integration_e2e.py -q` and confirm it passes.

- [ ] **Step 5: Lint and commit**

```bash
ruff check --fix .
ruff format .
git add ROADMAP.md CLAUDE.md tests/test_integration_e2e.py
git commit -m "docs: roadmap and CLAUDE.md reflect fault registry and real refunds; e2e failed-vend loop

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

## Self-review notes

- Spec §1 → Tasks 1–2. §2 → Tasks 3, 6, 7. §3 → Tasks 8, 9. §4 → Tasks 4, 5, 11. §5 → tests in every task plus Task 12.
- Spec's `__machine__` clear key is replaced by the code-string key (Global Constraints); the dashboard passes `f.key` so the template does not care.
- `request_refund(reason=...)` gets its parameter in Task 7 (no other change) and its real body in Task 8. The all-products-locked test, which asserts a `cmd/payment/refund` publish, lives in Task 8 for that reason.
