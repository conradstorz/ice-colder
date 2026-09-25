# Soft Faults Never Stop the Machine — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Only six physical-hazard fault codes may inhibit payment; every other fault — `PAY-104`, heartbeat loss, broker loss, empty bins — alerts the operator and blocks the individual sale, but never stops the machine taking money.

**Architecture:** `services/availability.py` currently answers one question ("may we sell?") and reuses the answer for two purposes. Split the permissive rows into three gates — `safety` (blocks payment and sales), `fulfillment` (blocks this sale only), `alert` (blocks nothing). `payment_enabled` reads only `safety` rows; `sale_available` reads `safety` + `fulfillment`. A new `PAYMENT_BLOCKING_FAULTS` frozenset in the contract replaces severity-based gating, so adding a fault code can never silently stop the machine again.

**Tech Stack:** Python 3.12, Pydantic v2, `transitions` FSM, FastAPI + Jinja2 + HTMX, pytest, loguru. Dependencies via `uv`.

## Global Constraints

- Run every command with `uv run`. Never `pip install`, never `python -m venv`, never activate a venv.
- Do not chain shell commands with `&&`. Run them as separate invocations.
- Lint and format before every commit: `ruff check --fix .` then `ruff format .`.
- Fault codes are never renumbered and never removed. Only severity and gating may change.
- `CONTRACT_VERSION` in `contracts/vending_machine.py` goes `0.3.0` -> `0.4.0` in Task 1. Every later task assumes `0.4.0`.
- Spec: `docs/superpowers/specs/2026-09-24-soft-faults-non-blocking-design.md`.
- The six payment-blocking codes, verbatim and in this order everywhere they are listed: `ICE-402`, `WTR-103`, `WTR-104`, `ENV-102`, `ENV-103`, `PWR-102`.
- Gate names, verbatim: `safety`, `fulfillment`, `alert`.

---

### Task 1: Contract — `PAY-104` becomes a warning, `PAYMENT_BLOCKING_FAULTS` becomes the gate

**Files:**
- Modify: `contracts/vending_machine.py` (docstring line 2, `CONTRACT_VERSION` line 22, `FaultCode.PAY_104` entry in `FAULT_TABLE`, new frozenset after `FAULT_TABLE`)
- Modify: `docs/contracts/vending-machine/CONTRACT.md` (title line 1, `## Semantics fixed in 0.3.0` heading and the `PAY-104` bullet)
- Modify: `ROADMAP.md` (line 179, the `PAY-104` registry row)
- Test: `tests/test_contracts_vending.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `contracts.vending_machine.PAYMENT_BLOCKING_FAULTS: frozenset[FaultCode]` — the only faults that may disable payment. `FAULT_TABLE[FaultCode.PAY_104].severity` is `Severity.warning`. `CONTRACT_VERSION == "0.4.0"`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_contracts_vending.py`:

```python
def test_payment_blocking_faults_is_exactly_the_six_hazards():
    from contracts.vending_machine import PAYMENT_BLOCKING_FAULTS

    assert PAYMENT_BLOCKING_FAULTS == frozenset(
        {
            FaultCode.ICE_402,
            FaultCode.WTR_103,
            FaultCode.WTR_104,
            FaultCode.ENV_102,
            FaultCode.ENV_103,
            FaultCode.PWR_102,
        }
    )


def test_every_payment_blocking_fault_is_a_machine_scope_critical():
    from contracts.vending_machine import PAYMENT_BLOCKING_FAULTS

    for code in PAYMENT_BLOCKING_FAULTS:
        spec = FAULT_TABLE[code]
        assert spec.scope is Scope.machine, code
        assert spec.severity is Severity.critical, code


def test_pay_104_is_a_warning_and_never_blocks_payment():
    from contracts.vending_machine import PAYMENT_BLOCKING_FAULTS

    spec = FAULT_TABLE[FaultCode.PAY_104]
    assert spec.severity is Severity.warning
    assert spec.scope is Scope.machine
    assert FaultCode.PAY_104 not in PAYMENT_BLOCKING_FAULTS
```

The existing imports at the top of `tests/test_contracts_vending.py` must include
`FAULT_TABLE`, `FaultCode`, `Scope` and `Severity`. Read the file's import block
first; add only the names that are missing.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_contracts_vending.py -v -k "payment_blocking or pay_104"`

Expected: FAIL — `ImportError: cannot import name 'PAYMENT_BLOCKING_FAULTS'` on the first two, and `assert <Severity.lockout> is <Severity.warning>` on the third.

- [ ] **Step 3: Change the `PAY-104` severity**

In `contracts/vending_machine.py`, find the `FaultCode.PAY_104` entry in `FAULT_TABLE` and change `severity`:

```python
    FaultCode.PAY_104: FaultSpec(
        severity=Severity.warning,
        scope=Scope.machine,
        description="Transaction uncertain after VMC restart; operator must reconcile",
    ),
```

Nothing else in that entry changes. The code, scope and description stay exactly as they are.

- [ ] **Step 4: Add the whitelist**

In `contracts/vending_machine.py`, immediately after the closing `}` of `FAULT_TABLE` and before the `# Which fault a terminal dispenser outcome raises.` comment, insert:

```python
# The only faults that may inhibit payment. Everything else — bookkeeping
# doubt (PAY-104), heartbeat loss, broker loss, an empty bin — alerts the
# operator and blocks the individual sale, but never stops the machine taking
# money. Membership here, not severity, is the gate: adding a fault code can
# never silently stop the machine, because stopping it requires editing this
# frozenset on purpose.
PAYMENT_BLOCKING_FAULTS: frozenset[FaultCode] = frozenset(
    {
        FaultCode.ICE_402,  # trap door failed to close
        FaultCode.WTR_103,  # flow continues after valve close
        FaultCode.WTR_104,  # leak / overflow detected
        FaultCode.ENV_102,  # heater ineffective
        FaultCode.ENV_103,  # heater high-limit tripped
        FaultCode.PWR_102,  # 24 V control supply bad
    }
)
```

- [ ] **Step 5: Bump the contract version**

In `contracts/vending_machine.py`, line 2 of the module docstring:

```python
Shared contract models for the vending-machine ESP32 interface (v0.4.0).
```

and line 22:

```python
CONTRACT_VERSION = "0.4.0"
```

- [ ] **Step 6: Update the version assertions in the existing tests**

In `tests/test_contracts_vending.py`, replace every `"0.3.0"` literal with `"0.4.0"`. There are six occurrences, at approximately lines 22, 112, 122, 136, 140 and 159. Verify none remain:

Run: `grep -n "0\.3\.0" tests/test_contracts_vending.py`

Expected: no output.

- [ ] **Step 7: Run the contract tests**

Run: `uv run pytest tests/test_contracts_vending.py tests/test_contracts.py tests/test_contract_schemas.py -v`

Expected: PASS. `tests/test_contracts.py` covers the ice-maker contract at `1.1.0` and is unaffected. If `tests/test_contract_schemas.py` reports drift, regenerate and re-run:

Run: `uv run python -m contracts.generate`

`CONTRACT_VERSION` and `FAULT_TABLE` are module constants, not Pydantic model fields, so no schema should change. If `git status` shows modified files under `docs/contracts/vending-machine/schemas/`, include them in the commit.

- [ ] **Step 8: Run the simulator tests**

Run: `uv run pytest tests/test_simulator_base.py tests/test_simulator_vending.py tests/test_simulator_mdb.py tests/test_simulator_ice_maker.py -v`

Expected: PASS. `simulators/base.py:68` reads `CONTRACT_VERSION` from the contract module, so the simulators follow the bump automatically.

- [ ] **Step 9: Update `docs/contracts/vending-machine/CONTRACT.md`**

Line 1:

```markdown
# Vending Machine Contract — v0.4.0 (stub)
```

Change the `## Semantics fixed in 0.3.0` heading to `## Semantics fixed in 0.4.0`, then replace the `PAY-104` bullet:

```markdown
- Transaction uncertain after VMC restart (a persisted open sale found on
  boot) is `PAY-104` (warning, machine scope). It alerts the operator and
  holds the session snapshot as evidence until an operator clears it; it does
  **not** inhibit payment and does not block product selection.
- Only the codes in `PAYMENT_BLOCKING_FAULTS` inhibit payment: `ICE-402`,
  `WTR-103`, `WTR-104`, `ENV-102`, `ENV-103`, `PWR-102`. Every other fault
  alerts and may block an individual sale, but never stops the machine taking
  money.
```

- [ ] **Step 10: Update `ROADMAP.md`**

Replace the `PAY-104` row (line 179):

```markdown
| `PAY-104` | Transaction uncertain after VMC restart | warning | Alert; operator reconciles and clears. Payment stays enabled; snapshot in event history |
```

- [ ] **Step 11: Lint and format**

Run: `ruff check --fix .`

Run: `ruff format .`

- [ ] **Step 12: Commit**

```bash
git add contracts/vending_machine.py tests/test_contracts_vending.py docs/contracts/vending-machine ROADMAP.md
git commit -m "feat(contract): PAY-104 is a warning; PAYMENT_BLOCKING_FAULTS gates payment (0.4.0)"
```

---

### Task 2: Availability — three gates

**Files:**
- Modify: `services/availability.py` (whole file: new `Gate` enum, `Permissive.gate`, `_inst`/`_stub` signatures, row table, `set_active_faults`, `_rows_for`, `payment_enabled`, `blocking_reasons` -> `payment_blocking_reasons`, `table`, `_recompute`)
- Test: `tests/test_availability.py`

**Interfaces:**
- Consumes: `contracts.vending_machine.PAYMENT_BLOCKING_FAULTS` from Task 1.
- Produces:
  - `services.availability.Gate` — `str` `Enum` with members `safety`, `fulfillment`, `alert`.
  - `Permissive.gate: Gate` and the `"gate"` key in every `as_row()` / `table()` dict.
  - `Availability.payment_enabled -> bool` — true unless a `safety` row is failing. No longer depends on the product list.
  - `Availability.payment_blocking_reasons() -> list[str]` — names of failing `safety` rows. Replaces `blocking_reasons()`, which is deleted.
  - `Availability.sale_available(kind) -> tuple[bool, list[str]]` — unchanged signature; now ignores `alert` rows.
  - `Availability.product_sellable(product) -> tuple[bool, list[str]]` — unchanged signature and behaviour.

**Behaviour change to expect and accept:** a freshly constructed `Availability` now reports `payment_enabled is True`, because no `safety` row starts failing. The machine boots selling. Selection is still refused until `vending_alive` and friends report in. This is the intended consequence of the design and several existing tests assert the opposite — Step 7 migrates them.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_availability.py`. The file already defines `Recorder`, `_all_good` and `_avail`; reuse them.

```python
def test_gate_is_exported_on_every_row():
    a, _ = _avail()
    rows = {r["name"]: r for r in a.table()}
    assert rows["no_critical_fault"]["gate"] == "safety"
    assert rows["service_door_closed"]["gate"] == "safety"
    assert rows["no_leak"]["gate"] == "safety"
    assert rows["water_valve_closed"]["gate"] == "safety"
    assert rows["trap_door_closed"]["gate"] == "safety"
    assert rows["control_power_ok"]["gate"] == "safety"
    assert rows["transaction_certain"]["gate"] == "alert"
    assert rows["vending_alive"]["gate"] == "fulfillment"
    assert rows["mqtt_connected"]["gate"] == "fulfillment"
    assert rows["payment_alive"]["gate"] == "fulfillment"
    assert rows["payment_devices_ready"]["gate"] == "fulfillment"
    assert rows["ice_maker_alive"]["gate"] == "fulfillment"
    assert rows["ice_available"]["gate"] == "fulfillment"
    assert rows["fsm_ok"]["gate"] == "fulfillment"
    assert rows["bag_present"]["gate"] == "fulfillment"
    assert rows["water_pressure_ok"]["gate"] == "fulfillment"
    assert rows["water_treatment_ok"]["gate"] == "fulfillment"
    assert all("gate" in r for r in a.table())


def _machine_fault(code: str, severity: str = "critical") -> dict:
    return {
        "key": code,
        "sku": None,
        "code": code,
        "severity": severity,
        "scope": "machine",
    }


def test_each_payment_blocking_fault_disables_payment():
    from contracts.vending_machine import PAYMENT_BLOCKING_FAULTS

    for fault in PAYMENT_BLOCKING_FAULTS:
        a, _ = _avail()
        _all_good(a)
        assert a.payment_enabled is True
        a.set_active_faults([_machine_fault(fault.value)])
        assert a.payment_enabled is False, fault
        assert a.payment_blocking_reasons() == ["no_critical_fault"]
        a.set_active_faults([])
        assert a.payment_enabled is True, fault


def test_pay_104_does_not_disable_payment():
    a, _ = _avail()
    _all_good(a)
    a.set_active_faults([_machine_fault("PAY-104", severity="warning")])
    a.set_transaction_certain(False)
    assert a.payment_enabled is True
    assert a.payment_blocking_reasons() == []
    assert a.sale_available("ice")[0] is True


def test_machine_fault_outside_the_whitelist_does_not_disable_payment():
    a, _ = _avail()
    _all_good(a)
    a.set_active_faults([_machine_fault("COM-103", severity="critical")])
    assert a.payment_enabled is True
    assert a.payment_blocking_reasons() == []


def test_every_fulfillment_row_blocks_the_sale_but_not_payment():
    setters = [
        ("mqtt_connected", lambda a: a.set_mqtt_connected(False)),
        ("vending_alive", lambda a: a.set_subsystem_alive("vending", False)),
        ("payment_alive", lambda a: a.set_subsystem_alive("mdb", False)),
        (
            "payment_devices_ready",
            lambda a: a.set_payment_device("card_reader", "error"),
        ),
        ("fsm_ok", lambda a: a.set_fsm_state("error")),
    ]
    for name, apply in setters:
        a, _ = _avail()
        _all_good(a)
        apply(a)
        assert a.payment_enabled is True, name
        assert a.payment_blocking_reasons() == [], name
        ok, failing = a.sale_available("ice")
        assert ok is False, name
        assert name in failing, name


def test_ice_maker_loss_blocks_only_ice_and_never_payment():
    a, _ = _avail()
    _all_good(a)
    a.set_subsystem_alive("ice_maker", False)
    assert a.payment_enabled is True
    assert a.sale_available("ice")[0] is False
    assert a.sale_available("water")[0] is True
```

- [ ] **Step 2: Run the new tests to verify they fail**

Run: `uv run pytest tests/test_availability.py -v -k "gate_is_exported or payment_blocking_fault or pay_104 or outside_the_whitelist or fulfillment_row or ice_maker_loss"`

Expected: FAIL — `KeyError: 'gate'`, and `AttributeError: 'Availability' object has no attribute 'payment_blocking_reasons'`.

- [ ] **Step 3: Add the `Gate` enum and the `gate` field**

In `services/availability.py`, replace the import block and the `Applies` / `Permissive` definitions. Add the contract import beside the existing imports:

```python
from contracts.vending_machine import PAYMENT_BLOCKING_FAULTS
```

Add `Gate` immediately after the `Applies` enum:

```python
class Gate(str, Enum):
    """How far a failing permissive reaches.

    safety      — a physical hazard. Blocks payment and every sale.
    fulfillment — the machine cannot complete this sale right now. Blocks the
                  sale; payment stays enabled so a transient heartbeat gap or
                  broker reconnect never costs a night of revenue.
    alert       — the operator should know, but nothing is blocked.
    """

    safety = "safety"
    fulfillment = "fulfillment"
    alert = "alert"
```

Add the field to `Permissive` and export it from `as_row`:

```python
@dataclass
class Permissive:
    name: str
    applies_to: Applies
    instrumented: bool
    state: PermissiveState
    detail: str = ""
    gate: Gate = Gate.fulfillment

    def as_row(self) -> dict:
        return {
            "name": self.name,
            "applies_to": self.applies_to.value,
            "instrumented": self.instrumented,
            "state": self.state.value,
            "detail": self.detail,
            "gate": self.gate.value,
        }
```

- [ ] **Step 4: Replace the severity gate with the whitelist, and give every row a gate**

In `services/availability.py`, delete this line:

```python
_BLOCKING_SEVERITIES = {"critical", "lockout"}
```

and put in its place:

```python
# active_faults() reports codes as strings; compare against the contract set.
_PAYMENT_BLOCKING_CODES = {code.value for code in PAYMENT_BLOCKING_FAULTS}
```

Update the two row helpers to take a gate:

```python
def _inst(
    name: str,
    applies: Applies,
    state: PermissiveState = PermissiveState.UNKNOWN,
    detail: str = "",
    gate: Gate = Gate.fulfillment,
) -> Permissive:
    return Permissive(name, applies, True, state, detail, gate)


def _stub(
    name: str, applies: Applies, gate: Gate = Gate.fulfillment
) -> Permissive:
    return Permissive(
        name, applies, False, PermissiveState.PASS, "not instrumented", gate
    )
```

Replace the `rows = [...]` list in `__init__` with:

```python
        rows = [
            _inst("mqtt_connected", Applies.both),
            _inst("vending_alive", Applies.both),
            _inst("payment_alive", Applies.both),
            _inst("payment_devices_ready", Applies.both),
            _inst("ice_maker_alive", Applies.ice),
            _inst("ice_available", Applies.ice, detail="no bin report yet"),
            _inst("fsm_ok", Applies.both),
            _inst(
                "no_critical_fault",
                Applies.both,
                PermissiveState.PASS,
                gate=Gate.safety,
            ),
            _inst(
                "service_door_closed",
                Applies.both,
                PermissiveState.PASS,
                "assumed closed; no report yet",
                gate=Gate.safety,
            ),
            _inst(
                "transaction_certain",
                Applies.both,
                PermissiveState.PASS,
                gate=Gate.alert,
            ),
            _stub("bag_present", Applies.ice),
            _stub("trap_door_closed", Applies.ice, gate=Gate.safety),
            _stub("control_power_ok", Applies.both, gate=Gate.safety),
            _stub("water_pressure_ok", Applies.water),
            _stub("water_treatment_ok", Applies.water),
            _stub("no_leak", Applies.water, gate=Gate.safety),
            _stub("water_valve_closed", Applies.water, gate=Gate.safety),
        ]
```

- [ ] **Step 5: Gate `no_critical_fault` on the whitelist**

In `set_active_faults`, replace the `blocking = sorted(...)` expression:

```python
        blocking = sorted(
            f["code"]
            for f in faults
            if f.get("scope") == "machine"
            and f.get("code") in _PAYMENT_BLOCKING_CODES
        )
```

The rest of `set_active_faults` — `_lockouts`, `_ice_101_active`, the
`no_critical_fault` state/detail assignment, `_refresh_ice_available()`,
`_recompute()` — is unchanged.

- [ ] **Step 6: Narrow `payment_enabled` and replace `blocking_reasons`**

In `services/availability.py`, replace `_rows_for`, `payment_enabled` and
`blocking_reasons` with:

```python
    def _rows_for(self, kind: str) -> list[Permissive]:
        """Rows that can block a sale of *kind*. Alert rows never block."""
        rows = [r for r in self._rows.values() if r.gate is not Gate.alert]
        if kind in ("ice", "water"):
            return [
                r for r in rows if r.applies_to in (Applies.both, Applies(kind))
            ]
        return rows

    def payment_blocking_reasons(self) -> list[str]:
        """Failing safety rows — the only reasons payment may be inhibited.

        Safety is machine-wide: a leak or a bad 24 V supply stops the whole
        machine, regardless of which product kind the row nominally applies to.
        """
        return sorted(
            r.name
            for r in self._rows.values()
            if r.gate is Gate.safety and r.state is not PermissiveState.PASS
        )

    @property
    def payment_enabled(self) -> bool:
        return not self.payment_blocking_reasons()
```

`sale_available` and `product_sellable` keep their existing bodies. Delete the
old `blocking_reasons` method entirely — Tasks 3 and 4 update its callers.

- [ ] **Step 7: Point `_recompute` at the new reasons**

In `_recompute`, change the one line:

```python
        reasons = self.payment_blocking_reasons()
```

Everything else in `_recompute` — the early return on no change, the log line,
the event record, the publish — stays exactly as it is.

- [ ] **Step 8: Migrate the existing tests in `tests/test_availability.py`**

Nine existing tests assert the old coupling. Replace each one in place.

Replace `test_everything_unknown_at_start_blocks_payment_and_publishes_false`:

```python
def test_unknown_inputs_at_start_leave_payment_on_but_block_sales():
    a, published = _avail()
    assert a.payment_enabled is True
    assert published == [True]
    ok, failing = a.sale_available("ice")
    assert ok is False
    assert "vending_alive" in failing
```

Replace `test_all_known_inputs_pass_enables_and_publishes_once`:

```python
def test_all_known_inputs_pass_and_publish_only_once():
    a, published = _avail()
    _all_good(a)
    assert a.payment_enabled is True
    assert a.sale_available("ice")[0] is True
    # payment was already on; no safety row changed, so nothing new is published
    assert published == [True]
    a.set_fsm_state("idle")
    assert published == [True]
```

Replace `test_ice_only_failure_keeps_water_selling`'s final assertion — the
published list is now `[True]`, not `[False, True]`:

```python
def test_ice_only_failure_keeps_water_selling():
    a, published = _avail()
    _all_good(a)
    a.set_subsystem_alive("ice_maker", False)
    assert a.sale_available("ice")[0] is False
    assert a.sale_available("water")[0] is True
    assert a.payment_enabled is True
    assert published == [True]
```

Replace `test_vending_loss_disables_everything`:

```python
def test_vending_loss_stops_sales_but_keeps_payment_on():
    a, published = _avail()
    _all_good(a)
    a.set_subsystem_alive("vending", False)
    assert a.payment_enabled is True
    assert published == [True]
    assert a.payment_blocking_reasons() == []
    assert "vending_alive" in a.sale_available("ice")[1]
    assert "vending_alive" in a.sale_available("water")[1]
```

Replace `test_lockout_on_every_product_disables_payment` — product lockouts are
a selection concern now, never a payment concern:

```python
def test_lockout_on_every_product_blocks_selection_not_payment():
    a, _ = _avail()
    _all_good(a)
    a.set_active_faults(
        [
            {
                "key": "ICE-1",
                "sku": "ICE-1",
                "code": "ICE-301",
                "severity": "lockout",
                "scope": "product",
            },
            {
                "key": "WTR-1",
                "sku": "WTR-1",
                "code": "WTR-102",
                "severity": "lockout",
                "scope": "product",
            },
        ]
    )
    assert a.payment_enabled is True
    ok, failing = a.product_sellable(Product(sku="ICE-1", kind="ice"))
    assert ok is False and "lockout:ICE-301" in failing
```

In `test_machine_critical_fault_blocks_all`, change the final assertion only:

```python
    assert "no_critical_fault" in a.payment_blocking_reasons()
```

`test_service_door_open_blocks_and_closing_restores` needs no change —
`service_door_closed` is a `safety` row and still disables payment.

Replace `test_payment_device_error_blocks`:

```python
def test_payment_device_error_blocks_the_sale_not_payment():
    a, _ = _avail()
    _all_good(a)
    a.set_payment_device("card_reader", "error")
    assert "payment_devices_ready" in a.sale_available("ice")[1]
    assert a.payment_enabled is True
    a.set_payment_device("card_reader", "ready")
    assert a.sale_available("ice")[0] is True
```

Replace `test_fsm_error_and_uncertain_transaction_block`:

```python
def test_fsm_error_blocks_the_sale_and_uncertain_transaction_blocks_nothing():
    a, _ = _avail()
    _all_good(a)
    a.set_fsm_state("error")
    assert a.payment_enabled is True
    assert "fsm_ok" in a.sale_available("ice")[1]
    a.set_fsm_state("idle")
    a.set_transaction_certain(False)
    assert a.payment_enabled is True
    assert a.sale_available("ice")[0] is True
    rows = {r["name"]: r for r in a.table()}
    assert rows["transaction_certain"]["state"] == "fail"
    assert rows["transaction_certain"]["detail"] == "PAY-104 active"
```

Replace `test_other_kind_needs_every_permissive`:

```python
def test_other_kind_needs_every_permissive_to_sell():
    a, _ = _avail([Product(sku="X", kind="other")])
    _all_good(a)
    a.set_subsystem_alive("ice_maker", False)
    assert a.payment_enabled is True
    assert a.sale_available("other")[0] is False
    assert a.product_sellable(Product(sku="X", kind="other"))[0] is False
```

Replace `test_no_products_never_enables` — payment is a machine property now, so
an empty catalog is not a reason to refuse money:

```python
def test_no_products_still_allows_payment():
    a, published = _avail([])
    _all_good(a)
    assert a.payment_enabled is True
    assert a.payment_blocking_reasons() == []
    assert published == [True]
```

Replace `test_republish_sends_current_value_unconditionally`:

```python
def test_republish_sends_current_value_unconditionally():
    a, published = _avail()
    _all_good(a)
    a.republish()
    assert published == [True, True]
```

- [ ] **Step 9: Run the availability tests**

Run: `uv run pytest tests/test_availability.py -v`

Expected: PASS, every test.

- [ ] **Step 10: Lint and format**

Run: `ruff check --fix .`

Run: `ruff format .`

- [ ] **Step 11: Commit**

```bash
git add services/availability.py tests/test_availability.py
git commit -m "feat(availability): split permissives into safety/fulfillment/alert gates"
```

---

### Task 3: VMC — payment survives every soft fault

**Files:**
- Modify: `controller/vmc.py:1048` (the `deposit_funds` warning, the last `blocking_reasons()` caller outside the web layer)
- Test: `tests/test_vmc_flows.py`

**Interfaces:**
- Consumes: `Availability.payment_blocking_reasons()` and the gate split from Task 2; `PAYMENT_BLOCKING_FAULTS` and the `PAY-104` warning severity from Task 1.
- Produces: no new API. `VMC._flag_uncertain_session`, `VMC.clear_fault` and `VMC._persist_session` keep their current behaviour; this task proves the new gating reaches them correctly and migrates the flow tests.

Note the two deliberate non-changes. `VMC._persist_session` keeps its
`if FaultCode.PAY_104 in self._machine_faults: return` guard
(`controller/vmc.py:359`), so the evidence file survives until an admin clears
the fault. `clear_fault` keeps its fail-closed branch: if the evidence file
cannot be removed, the fault stays. Neither is edited here.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_vmc_flows.py`, beside the existing `_boot_with` session
tests. `_boot_with`, `_wired_vmc`, `_all_alive`, `FakeEventRecorder`,
`SessionSnapshot` and `SessionStore` are already defined in that file.

```python
async def test_pay_104_on_boot_leaves_payment_enabled(tmp_path):
    rec = FakeEventRecorder()
    vmc, avail, store = _boot_with(tmp_path, None)
    vmc.set_event_recorder(rec)
    store.save(SessionSnapshot(state="interacting_with_user", credit_escrow=1.25))
    vmc.set_session_store(store)

    assert "PAY-104" in {f["code"] for f in vmc.active_faults()}
    assert avail.payment_enabled is True
    assert avail.payment_blocking_reasons() == []
    assert store.load() is not None  # evidence kept until an admin clears it
    vmc.cancel_pending_tasks()


async def test_pay_104_is_reported_as_a_warning():
    vmc, monitor, avail, _ = _wired_vmc()
    _all_alive(monitor, vmc)
    vmc._raise_fault(FaultCode.PAY_104, outcome="test")
    fault = next(f for f in vmc.active_faults() if f["code"] == "PAY-104")
    assert fault["severity"] == "warning"
    assert fault["scope"] == "machine"
    assert avail.payment_enabled is True
    vmc.cancel_pending_tasks()


async def test_select_product_refused_while_vending_offline_but_payment_stays_on():
    vmc, monitor, avail, _ = _wired_vmc()
    _all_alive(monitor, vmc)
    avail.set_payment_device("coin_acceptor", "ready")
    await vmc._handle_mqtt_hardware_io(
        "hardware/io/bin_half_full", {"device": "bin_half_full", "state": True}
    )
    monitor.mark_offline("vending")

    assert avail.payment_enabled is True
    vmc.deposit_funds(2.00)
    vmc.select_product(0)
    assert vmc.selected_product is None
    assert vmc.credit_escrow == 2.00
    vmc.cancel_pending_tasks()


async def test_hazard_fault_still_disables_payment():
    vmc, monitor, avail, published = _wired_vmc()
    _all_alive(monitor, vmc)
    avail.set_payment_device("coin_acceptor", "ready")
    vmc._raise_fault(FaultCode.WTR_104, outcome="leak")
    assert avail.payment_enabled is False
    assert avail.payment_blocking_reasons() == ["no_critical_fault"]
    assert (await _enables(published))[-1] is False
    vmc.cancel_pending_tasks()
```

`FaultCode` must be importable in `tests/test_vmc_flows.py`. Read the import
block; if `FaultCode` is not already imported from
`contracts.vending_machine`, add it.

`test_select_product_refused_while_vending_offline_but_payment_stays_on`
assumes `_wired_vmc()` gives the VMC at least one product at index 0 and that
the FSM reaches `interacting_with_user` on deposit. Read `_wired_vmc` before
writing the test; if it configures no products, add one the same way the
neighbouring selection tests in the file do.

- [ ] **Step 2: Run the new tests to verify they fail**

Run: `uv run pytest tests/test_vmc_flows.py -v -k "pay_104_on_boot or reported_as_a_warning or vending_offline_but_payment or hazard_fault_still"`

Expected: FAIL — `assert False is True` on `payment_enabled` in the first three (`transaction_certain` and the liveness rows still gate payment before Task 2 lands; if Task 2 is already merged, the first three pass and only the severity assertion fails), and `assert 'lockout' == 'warning'` in the second.

- [ ] **Step 3: Point `deposit_funds` at the new reasons**

In `controller/vmc.py`, in `deposit_funds`, replace the warning block:

```python
        if self._availability and not self._availability.payment_enabled:
            logger.warning(
                f"Credit ${amount:.2f} arrived while payment is disabled "
                f"({', '.join(self._availability.payment_blocking_reasons())}); "
                "escrowed"
            )
```

- [ ] **Step 4: Migrate the existing flow tests**

Six existing tests in `tests/test_vmc_flows.py` assert the old coupling.

Replace `test_vending_heartbeat_loss_raises_com_101_and_disables_payment`:

```python
async def test_vending_heartbeat_loss_raises_com_101_without_disabling_payment():
    vmc, monitor, avail, published = _wired_vmc()
    _all_alive(monitor, vmc)
    avail.set_payment_device("coin_acceptor", "ready")
    await vmc._handle_mqtt_hardware_io(
        "hardware/io/bin_half_full", {"device": "bin_half_full", "state": True}
    )
    assert avail.payment_enabled is True

    monitor.mark_offline("vending")
    codes = {f["code"] for f in vmc.active_faults()}
    assert "COM-101" in codes
    assert avail.payment_enabled is True
    assert avail.sale_available("ice")[0] is False
    assert "vending_alive" in avail.sale_available("ice")[1]

    monitor.record_heartbeat("vending", {"uptime_seconds": 5})
    assert "COM-101" not in {f["code"] for f in vmc.active_faults()}
    assert avail.sale_available("ice")[0] is True
    vmc.cancel_pending_tasks()
```

In `test_ice_maker_loss_is_com_102_and_only_ice_blocked`, no change is needed —
it already asserts `sale_available("ice")` is false and `payment_enabled` is
true.

Replace `test_mdb_loss_is_pay_101`:

```python
async def test_mdb_loss_is_pay_101_and_blocks_sales_not_payment():
    vmc, monitor, avail, _ = _wired_vmc()
    _all_alive(monitor, vmc)
    monitor.mark_offline("mdb")
    assert "PAY-101" in {f["code"] for f in vmc.active_faults()}
    assert avail.payment_enabled is True
    assert "payment_alive" in avail.sale_available("ice")[1]
    vmc.cancel_pending_tasks()
```

In `test_payment_status_error_feeds_availability` (around line 968), replace the
one assertion:

```python
    assert "payment_devices_ready" in avail.sale_available("ice")[1]
```

In `test_boot_with_escrow_raises_pay_104_and_blocks` (around line 1016), rename
it and replace the two-branch availability assertion:

```python
async def test_boot_with_escrow_raises_pay_104_without_blocking(tmp_path):
```

```python
    assert avail.payment_enabled is True
    rows = {r["name"]: r for r in avail.table()}
    assert rows["transaction_certain"]["state"] == "fail"
```

In `test_clearing_pay_104_removes_file_and_reenables` (around line 1052),
replace the final assertion:

```python
    rows = {r["name"]: r for r in avail.table()}
    assert rows["transaction_certain"]["state"] == "pass"
```

In `test_clear_pay_104_fails_closed_when_evidence_file_persists` (around line
1063), replace the availability assertion — the fault stays, but payment was
never off:

```python
    assert avail.payment_enabled is True
    rows = {r["name"]: r for r in avail.table()}
    assert rows["transaction_certain"]["state"] == "fail"
```

Replace `test_error_occurred_and_reset_publish_destination_state_to_availability`
(around line 1107). It must still prove that the *destination* state reaches
`Availability`, which is now observable through `sale_available` rather than
`payment_enabled`:

```python
async def test_error_occurred_and_reset_publish_destination_state_to_availability():
    """error_occurred() must flip fsm_ok immediately, and reset_state() must
    restore it — both require the destination state, not the source state, to
    be published to Availability."""
    vmc, monitor, avail, published = _wired_vmc()
    _all_alive(monitor, vmc)
    avail.set_payment_device("coin_acceptor", "ready")
    assert avail.sale_available("water")[0] is True

    vmc.error_occurred()
    assert avail.sale_available("water")[0] is False
    assert "fsm_ok" in avail.sale_available("water")[1]
    assert avail.payment_enabled is True

    vmc.reset_state()
    assert avail.sale_available("water")[0] is True
    vmc.cancel_pending_tasks()
```

- [ ] **Step 5: Confirm no `blocking_reasons` callers remain outside the web layer**

Run: `grep -rn "blocking_reasons" controller/ services/ tests/ simulators/`

Expected: only `payment_blocking_reasons` matches. `web_interface/routes.py`
still has two callers; Task 4 fixes them.

- [ ] **Step 6: Run the VMC test suites**

Run: `uv run pytest tests/test_vmc_flows.py tests/test_vmc_fsm.py tests/test_session_store.py -v`

Expected: PASS.

- [ ] **Step 7: Lint and format**

Run: `ruff check --fix .`

Run: `ruff format .`

- [ ] **Step 8: Commit**

```bash
git add controller/vmc.py tests/test_vmc_flows.py
git commit -m "feat(vmc): soft faults alert without inhibiting payment"
```

---

### Task 4: Dashboard — say "still selling" when the machine is still selling

**Files:**
- Modify: `web_interface/routes.py:249` (status context), `web_interface/routes.py:456` (screen context)
- Modify: `web_interface/templates/partials/status_fragment.html` (the `{% else %}` unhealthy banner, around line 67-77)
- Modify: `web_interface/templates/partials/health_fragment.html` (permissives table, around line 80-110)
- Test: `tests/test_web_routes.py`

**Interfaces:**
- Consumes: `Availability.payment_blocking_reasons()` and the `gate` key in `Availability.table()` rows, both from Task 2.
- Produces: the status-fragment template context gains `machine_stopped: bool | None` — `True` when `payment_enabled` is `False`, `False` when it is `True`, `None` when there is no `Availability` attached.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_web_routes.py`. Read the file's existing fixtures first and
use whatever client/VMC fixture the neighbouring status tests use; the
assertions below only depend on the rendered HTML.

```python
def test_status_shows_still_selling_for_a_soft_fault(client, vmc_instance):
    from contracts.vending_machine import FaultCode

    vmc_instance._raise_fault(FaultCode.PAY_104, outcome="restart")
    body = client.get("/status", headers={"HX-Request": "true"}).text
    assert "still selling" in body
    assert "Machine Stopped" not in body
    assert "PAY-104" in body


def test_status_shows_machine_stopped_for_a_hazard_fault(client, vmc_instance):
    from contracts.vending_machine import FaultCode

    vmc_instance._raise_fault(FaultCode.WTR_104, outcome="leak")
    body = client.get("/status", headers={"HX-Request": "true"}).text
    assert "Machine Stopped" in body
    assert "still selling" not in body


def test_health_permissives_table_shows_the_gate(client):
    body = client.get("/health", headers={"HX-Request": "true"}).text
    assert "Gate" in body
    assert "fulfillment" in body
```

- [ ] **Step 2: Run the new tests to verify they fail**

Run: `uv run pytest tests/test_web_routes.py -v -k "still_selling or machine_stopped or permissives_table"`

Expected: FAIL — `assert 'still selling' in body` (the template says "Issues Detected"), and `assert 'Gate' in body`.

- [ ] **Step 3: Update the status context in `routes.py`**

In `web_interface/routes.py`, in `_render_status`, replace the two lines that
build the payment context:

```python
        payment_enabled = availability.payment_enabled if availability else None
        payment_reasons = (
            availability.payment_blocking_reasons() if availability else []
        )
```

and add `machine_stopped` to the returned context dict, immediately after
`"payment_reasons": payment_reasons,`:

```python
                "machine_stopped": (
                    None if payment_enabled is None else not payment_enabled
                ),
```

- [ ] **Step 4: Update the screen context in `routes.py`**

In `_screen_context`, replace the `payment_reasons` entry:

```python
            "payment_reasons": (
                availability.payment_blocking_reasons() if availability else []
            ),
```

`partials/screen_body.html` needs no change — it already renders
`payment_enabled` and `payment_reasons` and will simply show red far less often.

- [ ] **Step 5: Split the banner in `status_fragment.html`**

In `web_interface/templates/partials/status_fragment.html`, the `{% else %}`
branch opens with a hard-coded red card. Replace the opening three elements and
the heading — from the `<div class="bg-red-50 ...">` line through the
`</div>` that closes the `flex items-center gap-2` heading row — with:

```html
<div class="{{ 'bg-red-50 border-red-200' if machine_stopped else 'bg-amber-50 border-amber-200' }} rounded-xl border shadow-sm overflow-hidden">
  <div class="flex">
    <div class="w-1 {{ 'bg-red-500' if machine_stopped else 'bg-amber-500' }} flex-shrink-0"></div>
    <div class="flex items-start justify-between flex-1 p-5">
      <div>
        <div class="flex items-center gap-2">
          <span class="w-2.5 h-2.5 rounded-full {{ 'bg-red-500' if machine_stopped else 'bg-amber-500' }} flex-shrink-0"></span>
          {% if machine_stopped %}
          <span class="text-base font-semibold text-red-700">Machine Stopped</span>
          {% else %}
          <span class="text-base font-semibold text-amber-700">Issues Detected — still selling</span>
          {% endif %}
        </div>
```

Then change the issues list immediately below it so the text colour follows the
same condition:

```html
        <ul class="mt-2 space-y-0.5">
          {% for issue in issues %}
          <li class="text-sm {{ 'text-red-600' if machine_stopped else 'text-amber-700' }}">{{ issue }}</li>
          {% endfor %}
        </ul>
```

Leave the Active faults list, the Payment/State/Escrow/Last Payment column and
the closing tags exactly as they are.

- [ ] **Step 6: Add the gate column in `health_fragment.html`**

In `web_interface/templates/partials/health_fragment.html`, in the permissives
table, add a header cell between `Applies` and `State`:

```html
          <th class="pb-2 text-left font-medium">Gate</th>
```

and the matching body cell between the `applies_to` cell and the state cell:

```html
          <td class="py-2">
            {% if row.gate == 'safety' %}<span class="text-xs font-medium text-red-600">safety</span>
            {% elif row.gate == 'fulfillment' %}<span class="text-xs text-gray-500">fulfillment</span>
            {% else %}<span class="text-xs text-gray-400">alert</span>{% endif %}
          </td>
```

Change the section heading above the table so it names what actually stops the
machine:

```html
    <h3 class="text-xs font-medium text-gray-400 uppercase tracking-wide mb-2">
      Sale permissives — payment {{ 'enabled' if health.payment_enabled else 'disabled' }};
      only <span class="text-red-600">safety</span> rows stop the machine
    </h3>
```

- [ ] **Step 7: Run the web tests**

Run: `uv run pytest tests/test_web_routes.py -v`

Expected: PASS. If an existing test asserts the literal string
`"Issues Detected"` and now fails, update it to match whichever variant that
test's fault produces — `PAY-104` and other soft faults render
`Issues Detected — still selling`, hazards render `Machine Stopped`.

- [ ] **Step 8: Run the full suite**

Run: `uv run pytest`

Expected: PASS. Report the exact counts.

- [ ] **Step 9: Lint and format**

Run: `ruff check --fix .`

Run: `ruff format .`

- [ ] **Step 10: Update `CLAUDE.md` and `ROADMAP.md`**

In `CLAUDE.md`, replace the `availability.py` Services bullet:

```markdown
- `availability.py` - permissive truth table (ROADMAP §3) split into three
  gates: `safety` rows block payment and sales, `fulfillment` rows block only
  the individual sale, `alert` rows block nothing. Publishes
  `cmd/payment/enable` on change; feeds the health tab and `/screen`. Only the
  six codes in `contracts.vending_machine.PAYMENT_BLOCKING_FAULTS` can inhibit
  payment.
```

and the `session_store.py` bullet:

```markdown
- `session_store.py` - atomic snapshot of the live sale in `data/session.json`;
  an open snapshot at boot raises `PAY-104`, which alerts the operator and
  holds the evidence file until an admin clears it, but never inhibits payment
```

In `ROADMAP.md` §3, append after the paragraph beginning "Implemented in
`services/availability.py`":

```markdown
Each permissive carries a gate. Only `safety` rows — an active hazard fault
(`PAYMENT_BLOCKING_FAULTS`), an open service door, a leak, a proven-open trap
door, bad 24 V control power — inhibit `cmd/payment/enable`. Subsystem
liveness, broker connectivity, bin level and FSM state are `fulfillment` rows:
they refuse the individual sale at selection time and the customer is refunded
on session timeout, but the machine keeps accepting money through a transient
heartbeat gap or broker reconnect. `transaction_certain` (`PAY-104`) is an
`alert` row and blocks nothing.
```

- [ ] **Step 11: Commit**

```bash
git add web_interface/routes.py web_interface/templates/partials/status_fragment.html web_interface/templates/partials/health_fragment.html tests/test_web_routes.py CLAUDE.md ROADMAP.md
git commit -m "feat(web): distinguish a stopped machine from one that is still selling"
```

---

## Verification

After Task 4, confirm the whole behaviour end to end.

- [ ] Run the full suite: `uv run pytest`. Report exact pass/fail counts.
- [ ] Run `ruff check .` and `ruff format --check .`. Both must be clean.
- [ ] Confirm the gate is the only thing that stops payment:
      `grep -rn "PAYMENT_BLOCKING_FAULTS" contracts/ services/ tests/` should
      show the definition, the `_PAYMENT_BLOCKING_CODES` derivation in
      `services/availability.py`, and test references — and nothing else
      computing a payment gate from severity.
- [ ] Confirm `blocking_reasons` is fully gone:
      `grep -rn "\.blocking_reasons()" .` (excluding `.venv` and
      `__pycache__`) returns only `payment_blocking_reasons` matches.
