# Dispenser Profiles — Plan 2: Contract Runtime, VMC Wiring, Simulator

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Spec:** `docs/superpowers/specs/2026-10-06-dispenser-profiles-design.md` §6 (runtime), §9 (migration), §10 item 2. Plan 1 (`docs/superpowers/plans/2026-10-07-dispenser-profiles-schema.md`, merged as `85854ca`) delivered the schema, validation, `DispenserProfiles` service, example and CLI. Plan 3 (dashboard editor) follows.

**Goal:** The machine sells only slots with a valid profile, sends the whole validated profile to the board with every dispense, maps board outcomes to faults per mechanism, and the simulator executes the received profile so every fault row is reachable end to end.

**Architecture:** `VMC` gets a `DispenserProfiles` reference and reconciles `CFG-101`/`CFG-102` against it (lockouts via the existing `_raise_fault`/`clear_fault` path). A sale's dispense moves from a bare publish on `cmd/dispense` to `CommandDispatcher.send("vending", "dispense", params)` on the command channel, awaiting only the *accepted* ack; completion stays with the existing `hardware/dispenser` handler and timeout. The simulator stops guessing mechanisms from product names and executes the profile it is sent.

**Tech Stack:** Python 3.12, Pydantic v2, `transitions`, aiomqtt, pytest (+ `pytest-asyncio`). No new dependencies.

## Global Constraints

- **Contract version stays `0.8.0`.** It was bumped in plan 1; this plan fills in the reserved bullet of "Semantics fixed in 0.8.0" in `docs/contracts/vending-machine/CONTRACT.md` (lines ~119-121). Spec §6.4's "0.7.0 → 0.8.0" is stale.
- `DispenseCommand` is `slot: int (ge=0)`, `mechanism: Literal["bagged_ice", "water_fill"]`, `profile: SlotProfile` — all **required**, no defaults. The board is stateless about configuration; a save during a vend cannot affect that vend.
- `DispenserStatus.state` **stays `str`** (deviation from spec §6.4's union type, recorded): a 0.7.0 board's `motor_active`/`fill_complete`/`solenoid_open` must still be treated as intermediate steps (spec §9). `DispenseStep` is the enum the simulator emits; the VMC never switches on it. `DispenserStatus.detail: str | None = None` is added.
- `DispenserOutcome` gains `door_open`, `no_flow`, `over_dispense`. `OUTCOME_FAULTS` becomes keyed by `(mechanism, outcome)` exactly per spec §6.4's table; `bin_empty` → `ICE-101` for both mechanisms; a `fault_for_outcome(mechanism, outcome) -> FaultCode` helper is the only reader. `ICE-302`'s description becomes "Dispense actuator fault reported by the board (motor stall, valve driver, over-current)".
- `door_open` is a **successful vend for the customer** (sale recorded, FSM completes) **and** raises critical `ICE-402`. At the dispatcher layer (test runs from `/tests`), `door_open` reports `status="failed"` with `detail` — a tech running a test must see that the door did not close. Recorded decision.
- The production sale uses `CommandDispatcher.send` (accept phase only, one retry with the same `request_id`, `CommandTimeout` → `PAY-102` immediately). `send_and_await_completion` is **never** used for a sale. The VMC stops publishing `cmd/dispense`.
- The simulator's legacy `cmd/dispense` subscription is **removed in this plan** (deviation from spec §9's "keep one version", recorded): without a profile in the payload it cannot execute anything meaningful, no physical board exists, and the VMC stops publishing to it in the same change.
- The simulator **never reads `dispensers.toml`**; it executes `params["profile"]`. `_classify_product`/`_slot_types`/`slot_type` are deleted.
- `CFG-101` is raised per product (`kind` `ice`/`water`/`other`) whose slot has no valid profile, via `_raise_fault(FaultCode.CFG_101, sku=sku)` (its `product_unavailable` severity locks the product); it is cleared only when `_lockouts.get(sku) is FaultCode.CFG_101` and a valid profile now exists. `CFG-102` is a machine fault raised while `report.file_error` is true and cleared otherwise. Reconciliation runs on `set_dispenser_profiles`, after `set_capabilities`, and on an explicit public `reconcile_dispenser_profiles()` plan 3's save route will call.
- Every mutating `/tests` POST still requires `require_htmx`; `TESTABLE_COMMANDS` is unchanged.
- Run tests with `uv run pytest`; lint touched files only with `uv run ruff check --fix <files>` then `uv run ruff format <files>` (never a bare `ruff format .`); no `&&` chaining; commit after every task with messages ending `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`.
- Work happens in the worktree `.claude/worktrees/feat-dispenser-profiles-runtime` on branch `feat/dispenser-profiles-runtime`.

---

## File map

| File | Responsibility in this plan |
|---|---|
| `contracts/vending_machine.py` | `DispenseStep`, three new outcomes, `OUTCOME_FAULTS` by `(mechanism, outcome)`, `fault_for_outcome`, `ICE-302` wording. |
| `services/mqtt_messages.py` | `DispenseCommand(slot, mechanism, profile)`, `DispenserStatus.detail`, `request_id` docstring. |
| `contracts/common.py` | `COMMAND_PARAM_VALIDATORS["dispense"]` parses params as `DispenseCommand`. |
| `contracts/generate.py`, `docs/contracts/vending-machine/schemas/*.json`, `CONTRACT.md` | New schemas (`dispense_command`, `dispense_step`, `slot_profile`), prose. |
| `services/command_dispatcher.py` | Pass `detail` through; `door_open` → `failed`. |
| `controller/vmc.py` | `set_dispenser_profiles`, `reconcile_dispenser_profiles`, capabilities hook, dispatcher-based dispense, `request_id` echo check, mechanism-aware fault mapping, `door_open` branch, `run_test_sale` pre-check. |
| `services/session_store.py` | `SessionSnapshot.dispense_mechanism: str | None` (so a crash mid-vend records how it was running). |
| `main.py` | Hand `dispenser_profiles` to the VMC and routes after the health monitor is wired. |
| `web_interface/context.py`, `routes/__init__.py`, `routes/tests_level.py` | `dispenser_profiles` global + setter; `/tests/vending/dispense` builds full params or refuses. |
| `simulators/vending_machine.py` | Execute the profile; new channels/devices; new fault injections; delete heuristic and legacy topic. |
| `tests/dispenser_fixtures.py` | `profiles_for(products, directory)` and `FakeDispatcher`. |
| `tests/test_vmc_flows.py`, `tests/test_simulator_vending.py`, `tests/test_mqtt.py`, `tests/test_mqtt_messages_validation.py`, `tests/test_contracts_vending.py`, `tests/test_integration_e2e.py`, `tests/test_routes_tests.py`, `tests/test_session_store.py` | Fixture sweep and new behaviour tests. |
| `docker-compose.yml`, `CLAUDE.md` | Drop the vestigial `ICE_COLDER_DISPENSERS` from the three simulator services; document the runtime. |

---

### Task 1: Contract runtime types

**Files:**
- Modify: `contracts/vending_machine.py` (`DispenserOutcome` ~55-67, `OUTCOME_FAULTS` ~307-313, `FAULT_TABLE[ICE_302]` ~145-149, version banner comment ~47-51), `services/mqtt_messages.py` (`DispenseCommand` ~131-135, `DispenserStatus` ~74-90), `contracts/common.py` (`COMMAND_PARAM_VALIDATORS` ~84-88), `contracts/generate.py` (`VENDING_MODELS` ~49-57), `docs/contracts/vending-machine/CONTRACT.md` (lines ~19-20, 76, 94, 111-121, 156)
- Regenerate: `docs/contracts/vending-machine/schemas/` (`uv run python -m contracts.generate`)
- Test: `tests/test_contracts_vending.py`, `tests/test_mqtt_messages_validation.py`, `tests/test_mqtt.py` (line ~71), `tests/test_contract_schemas.py` (drift)

**Interfaces:**
- Produces, in `contracts/vending_machine.py`:
  - `class DispenseStep(str, Enum)`: `agitate`, `fill`, `release`.
  - `DispenserOutcome` += `door_open = "door_open"`, `no_flow = "no_flow"`, `over_dispense = "over_dispense"`; docstring updated to say any non-outcome string is an intermediate step and `DispenseStep` lists the ones this contract's boards emit.
  - `Mechanism = Literal["bagged_ice", "water_fill"]`; `MECHANISMS: tuple[str, ...] = ("bagged_ice", "water_fill")`.
  - `OUTCOME_FAULTS: dict[tuple[str, DispenserOutcome], FaultCode]` with exactly: `("bagged_ice", timeout) → ICE_301`, `("bagged_ice", error) → ICE_302`, `("bagged_ice", jam) → ICE_401`, `("bagged_ice", door_open) → ICE_402`, `("water_fill", no_flow) → WTR_101`, `("water_fill", over_dispense) → WTR_102`, `("water_fill", timeout) → WTR_101`, `("water_fill", error) → ICE_302`, and `(m, bin_empty) → ICE_101` for both.
  - `def fault_for_outcome(mechanism: str, outcome: DispenserOutcome) -> FaultCode` — raises `KeyError` with a message naming both when unmapped (`complete` is never mapped; callers must not ask).
  - `FAULT_TABLE[ICE_302].description` = the Global Constraints wording.
- Produces, in `services/mqtt_messages.py`:
  - `class DispenseCommand(BaseModel)`: `slot: int = Field(..., ge=0)`, `mechanism: Mechanism`, `profile: SlotProfile` (import `SlotProfile` from `services.dispenser_schema`); `model_validator(mode="after")` asserting `profile.mechanism == mechanism` with message `profile mechanism "<p>" does not match command mechanism "<m>"`.
  - `DispenserStatus` += `detail: Optional[str] = Field(None, description="Board-supplied text, e.g. 'stall 4.2 A' or '412 pulses'")`; the `request_id` docstring now says: echoed from the command-channel `dispense` request for both test runs and production sales; the VMC logs a mismatch and keys completion on `slot` and FSM state.
- Produces, in `contracts/common.py`: `COMMAND_PARAM_VALIDATORS["dispense"] = _validate_dispense_params` which calls `DispenseCommand.model_validate(params)` (lazy import inside the function to avoid a `services` → `contracts` cycle) and re-raises `ValueError` so a bad payload is acked `rejected`, never run.
- `contracts/generate.py`: `VENDING_MODELS` += `"dispense_command": DispenseCommand`, `"dispense_step": DispenseStep`, `"slot_profile": SlotProfile` (schema via `TypeAdapter(SlotProfile)`; `schema_for` already handles non-`BaseModel` via `TypeAdapter`).
- CONTRACT.md: replace the reserved bullet of "Semantics fixed in 0.8.0" with bullets for `DispenseCommand` params, `DispenseStep`, the three outcomes and the `(mechanism, outcome)` fault table (copy the table), `DispenserStatus.detail`/`request_id` semantics, and the sentence "A production sale is sent on the command channel (`cmd/vending`, command `dispense`) exactly like a test run; `cmd/dispense` is no longer published." Correct lines ~19-20, 76, 94, 156 to match.

- [ ] **Step 1: Failing tests.** `tests/test_contracts_vending.py`: rewrite `test_every_failure_outcome_maps_to_a_fault_code` to assert every `(mechanism, outcome)` pair for `outcome != complete` is in `OUTCOME_FAULTS` and `fault_for_outcome` returns it; `test_outcome_mapping_matches_spec` asserts the nine rows above verbatim; `test_dispense_step_values`; `test_ice_302_description_is_mechanism_agnostic`; `test_fault_for_outcome_unmapped_raises_keyerror` (`("bagged_ice", DispenserOutcome.complete)`). `tests/test_mqtt_messages_validation.py`: replace the `DispenseCommand(slot=-1)` case with `test_dispense_command_requires_mechanism_and_profile` (missing either → `ValidationError`), `test_dispense_command_rejects_mechanism_profile_mismatch`, `test_dispense_command_round_trips_profile` (build from `tests.dispenser_fixtures` — load `GOOD` via `validate_document` and take `profiles[1]`; `DispenseCommand(...).model_dump(mode="json")` → `DispenseCommand.model_validate(...)` equal). `tests/test_mqtt.py:71`: update the constructor the same way. `tests/test_contracts_common.py` (or wherever `COMMAND_PARAM_VALIDATORS` is tested): `test_dispense_params_validator_rejects_bare_slot` (`SubsystemCommand(request_id=..., command="dispense", params={"slot": 1})` raises) and accepts a full payload.
- [ ] **Step 2: Run** the four test files — FAIL for the right reasons.
- [ ] **Step 3: Implement**, regenerate schemas, edit CONTRACT.md.
- [ ] **Step 4: Run** `uv run pytest tests/test_contracts_vending.py tests/test_mqtt_messages_validation.py tests/test_mqtt.py tests/test_contract_schemas.py tests/test_contracts.py tests/test_contracts_common.py -q` — PASS. (`tests/test_vmc_flows.py::…` that index `OUTCOME_FAULTS[outcome]` at ~950 will now fail; that is Task 3's sweep — note it, do not fix here.) Lint touched files.
- [ ] **Step 5: Commit** `feat(contracts): DispenseCommand carries the slot profile; DispenseStep; outcomes by mechanism`.

---

### Task 2: VMC owns profiles — `CFG-101`/`CFG-102` reconciliation, startup wiring

**Files:**
- Modify: `controller/vmc.py` (constructor ~237-340 for `self._dispenser_profiles = None`; setters ~499-507; `_handle_mqtt_capabilities` ~1180-1197; `run_test_sale` guard ~2282-2290), `main.py` (~471-523), `web_interface/context.py` (after ~70-75), `web_interface/routes/__init__.py` (~21-32)
- Create: `profiles_for` and `write_profiles_toml` in `tests/dispenser_fixtures.py`
- Test: `tests/test_vmc_dispense_profiles.py` (new), `tests/test_main_startup_dispensers.py` (extend)

**Interfaces:**
- Consumes: `services.dispensers.DispenserProfiles` (`.report.profiles`, `.report.file_error`, `.profile_for_slot(slot)`, `.set_capabilities(doc) -> ValidationReport`, `.load()`); `VMC._raise_fault(code, sku=None, outcome=None)`, `VMC.clear_fault(key, by)`, `VMC._lockouts`, `VMC._machine_faults`, `VMC.subsystem_capabilities`.
- Produces, `tests/dispenser_fixtures.py`:
  - `def render_profiles_toml(products: Sequence[Product]) -> str` — for each product with `kind in ("ice", "water")` emits a minimal valid `[slot.N]` (bagged ice: sensor proofs, `"unmonitored"` current, no accessories; water: `flow_volume` with the `GOOD` numbers), `schema_version = 1` header. Products with `kind == "other"` get no table.
  - `def profiles_for(products: Sequence[Product], directory: Path) -> DispenserProfiles` — writes `directory / "dispensers.toml"` from `render_profiles_toml`, builds `ConfigModel(physical=PhysicalDetails(products=list(products)))`, returns a **loaded** `DispenserProfiles(config, path=...)`. Asserts `report.ok` so a fixture bug fails loudly.
- Produces, `controller/vmc.py`:
  - `def set_dispenser_profiles(self, profiles: DispenserProfiles) -> None` — stores and calls `reconcile_dispenser_profiles()`.
  - `def reconcile_dispenser_profiles(self) -> None` — for every product in `self.config.products`: valid = `kind in MECHANISM_FOR_KIND and profiles.profile_for_slot(slot) is not None and profile.product_sku == sku`; if not valid and `self._lockouts.get(sku) is not FaultCode.CFG_101` and the sku is not already locked by another code → `_raise_fault(FaultCode.CFG_101, sku=sku)`; if valid and `self._lockouts.get(sku) is FaultCode.CFG_101` → `clear_fault(sku, by="auto")`. Then `CFG_102`: raise (`_raise_fault(FaultCode.CFG_102)`) when `profiles.report.file_error` and not active; `clear_fault("CFG-102", by="auto")` when active and not `file_error`. No-op when no profiles object is set. Idempotent.
  - `def dispenser_profile_for(self, product: Product) -> SlotProfile | None` — the lookup every later task uses (returns `None` unless valid as above).
  - `_handle_mqtt_capabilities`: when `subsystem == "vending"` and profiles are set, call `self._dispenser_profiles.set_capabilities(caps)` then `reconcile_dispenser_profiles()`.
  - `run_test_sale`: before depositing, if `dispenser_profile_for(product) is None` raise `RuntimeError(f'{product.sku} has no valid dispenser profile (CFG-101); fix dispensers.toml')` — the route already renders `RuntimeError` as a refusal.
- Produces, `web_interface/context.py`: `dispenser_profiles: DispenserProfiles | None = None` and `set_dispenser_profiles(p)`; `routes/__init__.py` re-exports it.
- `main.py`: after `vmc.set_health_monitor(health)` (~509): `vmc.set_dispenser_profiles(dispenser_profiles)` and `routes.set_dispenser_profiles(dispenser_profiles)`; `dispenser_profiles` is the module global set by `load_dispenser_profiles` — assert it is not `None` there (it never is after that call).

- [ ] **Step 1: Failing tests** in `tests/test_vmc_dispense_profiles.py` (build a `VMC` the way `tests/test_vmc_flows.py::make_vmc` does, with products `Product(sku="ICE-1", slot=0, kind="ice")`, `Product(sku="W-1", slot=1, kind="water")`, `Product(sku="X", slot=2, kind="other")`; profiles via `profiles_for` in `tmp_path`):
  - `test_set_profiles_locks_products_without_profile` — remove slot 1's table (write a TOML with only slot 0), `set_dispenser_profiles` → `_lockouts == {"W-1": CFG_101, "X": CFG_101}`, `ICE-1` unlocked.
  - `test_reconcile_clears_cfg101_when_profile_appears` — then write the full file, `profiles.load()`, `reconcile_dispenser_profiles()` → `"W-1"` cleared, `"X"` still locked.
  - `test_reconcile_never_clears_other_lockouts` — lock `ICE-1` with `ICE_301` via `_raise_fault`, reconcile → still `ICE_301`.
  - `test_cfg102_follows_file_error` — missing file → `CFG-102` in `active_faults()`; after a valid load + reconcile → gone.
  - `test_select_product_refuses_cfg101_locked` — selecting `X` returns the existing "unavailable (CFG-101)" refusal.
  - `test_capabilities_hook_reruns_cross_checks` — feed a `capabilities/vending` payload through `_handle_mqtt_capabilities` that lacks `bag_full_sensor` → `profiles.report` now has an error for slot 0 and `ICE-1` becomes `CFG-101`-locked; feed a complete one → cleared.
  - `test_run_test_sale_refuses_without_profile` — `await vmc.run_test_sale(...)` for `X` raises `RuntimeError` containing `CFG-101` before any deposit (assert `credit_escrow == 0`).
  - In `tests/test_main_startup_dispensers.py`: `test_main_wires_profiles_into_vmc_and_routes` — monkeypatch `VMC.set_dispenser_profiles` and `routes.set_dispenser_profiles` with recorders and run the part of `main()` that constructs the VMC (if `main()` cannot be run partially, extract the construction into a helper `build_vmc(config, profiles, ...)` and test that; state which you did).
- [ ] **Step 2: Run** — FAIL. **Step 3: Implement.** **Step 4: Run** the two files plus `tests/test_vmc_fsm.py tests/test_maintenance_standby.py tests/test_routes_tests.py -q` — PASS; lint.
- [ ] **Step 5: Commit** `feat(vmc): own dispenser profiles; raise and clear CFG-101/CFG-102`.

---

### Task 3: The sale dispenses through the dispatcher with the profile

**Files:**
- Modify: `controller/vmc.py` (`on_dispense_product` ~1340-1363, `_persist_then_dispense` ~1365-1375, `_handle_mqtt_dispenser` ~1052-1121, `_dispenser_event_slot_mismatch` ~948-961, `_dispense_timed_out` ~1551, `_snapshot` ~531-550), `services/session_store.py` (`SessionSnapshot` ~53-78), `services/command_dispatcher.py` (~200-210: pass `detail`; `door_open` → `failed`)
- Modify: `tests/dispenser_fixtures.py` (`FakeDispatcher`), `tests/test_vmc_flows.py` (fixtures + `cmd/dispense` assertions), `tests/test_session_store.py`, `tests/test_command_dispatcher.py`
- Test: `tests/test_vmc_dispense_profiles.py` (extend)

**Interfaces:**
- Consumes: Task 1's `DispenseCommand`, `fault_for_outcome`, `DispenserOutcome.door_open`; Task 2's `dispenser_profile_for`; `CommandDispatcher.send(subsystem, command, params) -> CommandAck` (raises `CommandTimeout`).
- Produces, `tests/dispenser_fixtures.py`:
  - `class FakeDispatcher`: `sent: list[tuple[str, str, dict]]`, `fail_with: Exception | None = None`, `async def send(self, subsystem, command, params=None) -> CommandAck` (records, raises `fail_with` if set, else returns `CommandAck(request_id=<fresh hex>, command=command, status="ok", phase="accepted")`), `last_request_id`. `send_and_await_completion` raises `AssertionError("a sale must not await completion")`.
- Produces, `controller/vmc.py`:
  - `on_dispense_product` builds `DispenseCommand(slot=product.slot, mechanism=profile.mechanism, profile=profile)` from `dispenser_profile_for(self.selected_product)`; if `None` (cannot happen after Task 2's lockout, but defend) → `_fail_vend(FaultCode.CFG_101, outcome="no_profile")` instead of dispatching. Stores `self._sale_mechanism = profile.mechanism`.
  - `_persist_then_dispense(snap, cmd)`: after the snapshot save, `ack = await self._command_dispatcher.send("vending", "dispense", cmd.model_dump(mode="json"))`, store `self._dispense_request_id = ack.request_id`. On `CommandTimeout` (or `self._command_dispatcher is None`, logged as a wiring error) while still `dispensing`: `_cancel_dispense_timeout()`, `_raise_fault(FaultCode.PAY_102, sku=..., outcome="no_ack")`, `_fail_vend(FaultCode.PAY_102, outcome="no_ack")`. An ack with `status != "ok"` (`rejected`/`unsupported`/`failed`) is treated the same as no ack. No publish to `cmd/dispense` remains anywhere in `vmc.py`.
  - `_handle_mqtt_dispenser`: after the slot guard, if `data.get("request_id")` and `self._dispense_request_id` are both set and differ → `logger.warning` (still processed). Branches: `complete` → existing success path; **`door_open`** → the success path **plus** `_raise_fault(FaultCode.ICE_402, sku=sku, outcome="door_open")` before `_finish_dispensing`; every other outcome → `code = fault_for_outcome(self._sale_mechanism, outcome)` then the existing fail path. `self._sale_mechanism`/`_dispense_request_id` reset in `_finish_dispensing` and `_fail_vend`.
  - `_snapshot` adds `dispense_mechanism=self._sale_mechanism` when dispensing; `SessionSnapshot.dispense_mechanism: Optional[str] = None` (additive, old files still load).
- Produces, `services/command_dispatcher.py` `_on_dispenser_report`: `status = "ok"` only for `complete`; `detail = data.get("detail") or f"outcome {state}"`; for `door_open` detail is `"bag released but door did not close"` when the board gave none.
- `tests/test_vmc_flows.py` sweep: `make_vmc`/`make_vmc2` give every product `kind="ice"` (water where a test needs it), attach `profiles_for(products, tmp_path)` via `set_dispenser_profiles` and a `FakeDispatcher` via `set_command_dispatcher` (turn the helpers into pytest fixtures or give them a `tmp_path` parameter — state which); every `assert topic == "cmd/dispense"` becomes an assertion on `dispatcher.sent[-1] == ("vending", "dispense", <params containing slot/mechanism/profile>)`; the `OUTCOME_FAULTS[outcome]` use at ~950 becomes `fault_for_outcome("bagged_ice", outcome)`; fault-outcome parametrisations gain `door_open`.

- [ ] **Step 1: Failing tests** in `tests/test_vmc_dispense_profiles.py`:
  - `test_sale_dispatches_full_profile_on_command_channel` — run a sale to `dispensing`; `dispatcher.sent[-1]` is `("vending", "dispense", params)` with `params["slot"]`, `params["mechanism"] == "bagged_ice"`, `params["profile"]["agitate"]["motor_channel"] == "agitator_motor"`; nothing published to `cmd/dispense`.
  - `test_no_ack_fails_vend_immediately_with_pay102` — `dispatcher.fail_with = CommandTimeout("vending", "dispense")` → `vend_failed`, escrow restored, `PAY-102` active, timeout task cancelled.
  - `test_rejected_ack_fails_vend` — ack `status="rejected"`.
  - `test_request_id_mismatch_is_logged_not_fatal` — report with a different `request_id` still completes the sale; `caplog` has the warning.
  - `test_door_open_completes_sale_and_raises_ice402` — sale recorded (`_record_sale` called / `sales` row), FSM back to idle, `ICE-402` in `active_faults()`, payment disabled by `Availability`.
  - `test_water_outcomes_map_by_mechanism` — parametrised `no_flow → WTR-101`, `over_dispense → WTR-102`, `timeout → WTR-101`, `error → ICE-302` for a water product; `bagged_ice` `timeout → ICE-301`.
  - `test_snapshot_records_mechanism` — `SessionSnapshot` built mid-dispense has `dispense_mechanism == "bagged_ice"`; `tests/test_session_store.py`: an old JSON without the field still loads.
  - `test_mid_vend_profile_save_does_not_change_inflight_command` — reload profiles with different `run_seconds` after dispatch; `dispatcher.sent[-1]` unchanged.
  - `tests/test_command_dispatcher.py`: `test_door_open_report_is_failed_with_detail`; `test_detail_passthrough`.
- [ ] **Step 2: Run** — FAIL. **Step 3: Implement** VMC, snapshot, dispatcher. **Step 4: Sweep `tests/test_vmc_flows.py`** as above; run `uv run pytest tests/test_vmc_dispense_profiles.py tests/test_vmc_flows.py tests/test_session_store.py tests/test_command_dispatcher.py tests/test_vmc_fsm.py tests/test_maintenance_standby.py -q` — PASS; lint.
- [ ] **Step 5: Commit** in two commits: `feat(vmc): dispense through the command dispatcher with the slot profile; door_open; outcomes by mechanism` and `test(vmc): fixtures carry kinds, profiles and a fake dispatcher`.

---

### Task 4: `/tests/vending/dispense` sends the profile

**Files:**
- Modify: `web_interface/routes/tests_level.py` (`_parse_command_params` ~249-278, `post_test_command` ~899-968, `_run_command` ~344-436)
- Test: `tests/test_routes_tests.py`

**Interfaces:**
- Consumes: `context.dispenser_profiles` (Task 2), `context.vmc_instance.dispenser_profile_for(product)`.
- Produces: `_parse_command_params` for `"dispense"` returns `{"slot": n, "mechanism": m, "profile": <profile.model_dump(mode="json")>}` when `context.vmc_instance.dispenser_profile_for(product_for_slot)` exists; otherwise raises the existing refusal type with `f'slot {n} ({sku}) has no valid dispenser profile (CFG-101)'`, rendered through `partials/test_refusal.html` like other refusals (never a 500). `_run_command` unchanged otherwise; the `test_run` event's metadata gains `mechanism`.

- [ ] **Step 1: Failing tests** in `tests/test_routes_tests.py` (reuse its dispatcher recorder pattern around lines ~167-173 and ~1118): `test_dispense_test_sends_full_profile` (POST with a valid slot → dispatcher received `params` with `profile`), `test_dispense_test_refused_without_profile` (slot of a `kind="other"` product → 200 with the refusal partial containing `CFG-101`, dispatcher untouched, lease released), `test_dispense_test_refused_unknown_slot_unchanged` (existing behaviour).
- [ ] **Step 2: Run** — FAIL. **Step 3: Implement.** **Step 4: Run** `uv run pytest tests/test_routes_tests.py tests/test_routes_tests_level.py -q` — PASS; lint.
- [ ] **Step 5: Commit** `feat(tests-level): dispense test carries the slot profile; refuse slots without one`.

---

### Task 5: Simulator executes the profile

**Files:**
- Modify: `simulators/vending_machine.py` (`HARDWARE_DEVICES` ~45-62, `_VENDING_CHANNELS` ~67-142, fault registrations ~173-216 and handlers ~353-386, `_apply_products`/`_slot_types`/`slot_type`/`_classify_product` ~38-41 & ~292-302 (delete), `_run_ice_dispense` ~413-529, `_run_water_dispense` ~531-587, `_dispense_slot` ~589-604, `_handle_dispense` ~606-633, `_listen_for_commands` ~684-694 and the `_customer_loop` queue consumer)
- Test: `tests/test_simulator_vending.py`, `tests/test_integration_e2e.py` (subscriptions ~130, 474; state literals ~183, 240, 308, 359)

**Interfaces:**
- Consumes: `DispenseCommand` (parse `cmd.params` with `DispenseCommand.model_validate` — the `COMMAND_PARAM_VALIDATORS` entry already guarantees it), `DispenseStep`, `DispenserOutcome`, `SlotProfile` and its step models, `Accessory`.
- Produces:
  - `HARDWARE_DEVICES` += `bag_fan: False`, `vending_now_light: False`, `door_sensor: False` (True = door open); `_VENDING_CHANNELS` += `bag_fan` (binary, output, `driven_by="dispense"`), `vending_now_light` (binary, output, `driven_by="dispense"`), `door_sensor` (binary, input), `agitator_current` (kind `current`, unit `A`, input), `auger_current` (kind `current`, unit `A`, input). The shipped `dispensers.example.toml` must validate with **zero warnings** against this capabilities doc (add a test that builds `SubsystemCapabilities` from `CHANNELS` and runs `validate_document(example, example_config.products, capabilities=...)`).
  - `async def _execute_profile(self, client, cmd: DispenseCommand) -> None` replaces `_dispense_slot`/`_run_ice_dispense`/`_run_water_dispense`:
    - accessories: for each, on at `lead_seconds` before its first step in `on_during` (or `["all"]` → whole run) and off `lag_seconds` after its last; implemented with per-accessory tasks and the existing simulator sleep helper (respect any time-scaling the base simulator already applies — grep `simulators/base.py` for a sleep/scale helper and use it).
    - `bagged_ice`: publish `DispenserStatus(state="agitate")`, agitator on for `run_seconds`, current channel reports a plausible value if the profile monitors it; `motor_stall` injected → `error` with `detail="stall <amps> A"`. `fill`: auger on; `bag_full_sensor` trips after `min(6 s, max_run_seconds/2)` unless `auger_jam` is active, in which case run to `max_run_seconds` then `timeout`; `timed` proof runs exactly `max_run_seconds`. `release`: solenoid on for `pulse_seconds`; `door_sensor` → True then False after 1 s; `bag_drop_solenoid_stuck` → `jam`; `door_stuck_open` → door stays True past `close_timeout_seconds` → `door_open`; `timed` proof skips the door sensor. Then `complete`.
    - `water_fill`: valve on; `flow_volume` proof emits `water_flow` counter pulses at a rate reaching `target_volume_ml` in ~`min(8 s, max_fill_seconds/2)`; `no_water_flow` → no pulses → after `no_flow_grace_seconds` → `no_flow`; `flow_runaway` → pulses continue past target by more than `over_dispense_percent` → `over_dispense`; `timed` runs `max_fill_seconds`; then `complete`. `water_valve_stuck_open` keeps its existing effect.
    - Every output toggle goes through `_set_hw`; every step publishes `DispenserStatus(slot, state=<DispenseStep value>, request_id=cmd.request_id)`; the terminal publish carries `detail`.
  - Fault registry += `motor_stall`, `no_water_flow`, `door_stuck_open`, `flow_runaway` (`FaultDef` with sensible categories; `on_activate`/`on_recover` just log). Existing `auger_jam`, `bag_drop_solenoid_stuck`, `water_valve_stuck_open`, `ice_bin_empty` kept.
  - `_handle_dispense` validates `DispenseCommand.model_validate(cmd.params)` (a `ValidationError` → `CommandOutcome(status="rejected", detail=<first line>)`), acks `accepted` with `result={"slot": slot, "mechanism": mechanism}`, spawns `_execute_profile`.
  - Delete `_classify_product`, `_WATER_KEYWORDS`, `_slot_types`, `slot_type`, the `cmd/dispense` subscription in `_listen_for_commands`, and the `_dispense_command` queue consumption in `_customer_loop` (if the customer loop needs a trigger to simulate a purchase, it now publishes the same `hardware/dispenser` flow only via a command it received — read the loop and keep whatever does not depend on the legacy topic; describe what you removed in the report).

- [ ] **Step 1: Failing tests** in `tests/test_simulator_vending.py` (use the file's existing fake-client pattern; build commands with `SubsystemCommand(request_id=..., command="dispense", params=DispenseCommand(...).model_dump(mode="json"))` from `tests.dispenser_fixtures` profiles): `test_bagged_ice_profile_step_sequence_and_io` (status states `agitate, fill, release, complete` in order; `hardware/io` toggles for agitator, auger, solenoid; fan on before fill and off `lag_seconds` after; light on for the whole run), `test_timed_fill_runs_exactly_max_run_seconds`, parametrised `test_injected_fault_yields_outcome` over `(motor_stall, error)`, `(auger_jam, timeout)`, `(bag_drop_solenoid_stuck, jam)`, `(door_stuck_open, door_open)`, `(no_water_flow, no_flow)`, `(flow_runaway, over_dispense)`, `(ice_bin_empty, bin_empty)`; `test_water_fill_by_volume_stops_within_over_dispense_percent`; `test_bare_slot_params_are_rejected` (ack `rejected`, nothing runs); `test_legacy_cmd_dispense_topic_is_not_subscribed`; `test_example_toml_validates_with_zero_warnings_against_simulator_capabilities`. Delete the `_classify_product`/`slot_type` tests (~17-97, 113-114, 538-554) and update the fault-outcome table (~605-608). `tests/test_integration_e2e.py`: update the two subscriptions and the four state literals; these tests skip without a broker — keep them consistent, do not try to run them.
- [ ] **Step 2: Run** — FAIL. **Step 3: Implement.** **Step 4: Run** `uv run pytest tests/test_simulator_vending.py tests/test_simulator_base.py tests/test_dispensers_example.py -q` — PASS; lint.
- [ ] **Step 5: Commit** `feat(sim): vending simulator executes the received slot profile; new channels and fault injections`.

---

### Task 6: Docs, compose cleanup, full-suite sweep

**Files:**
- Modify: `docker-compose.yml` (remove `ICE_COLDER_DISPENSERS` from the three simulator services only; keep it on `vmc`), `CLAUDE.md` (the "Dispenser profiles" subsection: replace "not yet raised anywhere … is a later increment" with the Task 2/3 behaviour; add the FSM Core paragraph on dispatcher-based dispense, `door_open`, `fault_for_outcome`, and the recorded deviations: version not bumped, `state` stays `str`, legacy topic removed now, dispatcher `door_open` = failed), `docs/superpowers/specs/2026-10-06-dispenser-profiles-design.md` (append a short "Implementation notes (plan 2)" section listing the same deviations — the spec stays the design of record, the notes say what shipped differently and why)
- Test: `tests/test_main_startup_dispensers.py::test_compose_sets_dispensers_env` (now asserts exactly one occurrence, on the `vmc` service)

- [ ] **Step 1:** Update the compose test to the new expectation; run it — FAIL.
- [ ] **Step 2:** Edit compose, CLAUDE.md, the spec notes.
- [ ] **Step 3:** `docker compose -f docker-compose.yml config --quiet` (with `--env-file .env.example` if `.env` is absent) — exit 0. Full `uv run pytest -q` — all pass (browser tests skip). `uv run ruff check .` clean.
- [ ] **Step 4: Commit** `docs(dispensers): runtime wiring documented; drop vestigial simulator env`.

---

## Self-review

- **Spec coverage:** §6.1 service object was plan 1; its consumers (`set_capabilities` hook, `profile_for_slot`) → Task 2. §6.2 fault reconciliation → Task 2. §6.3 `select_product` refusal (via lockout) → Task 2; `DispenseCommand` with profile, dispatcher accept-phase, `CommandTimeout` → `PAY-102`, `request_id` echo, test-sale lookup → Tasks 3 and 4. §6.4 enum, outcomes, `(mechanism, outcome)` table, `ICE-302` wording, schemas → Task 1; `door_open` semantics → Task 3 (VMC) and Task 3 (dispatcher). §6.5 simulator → Task 5. §9 fixture inventory → Tasks 1, 3, 5 (`test_availability.py`, `test_config_model.py`, `test_routes_inventory.py`, `test_config_store.py` need no change: they never run a sale through the VMC; `test_routes_tests_level.py` has no dispense references — the dispense route tests are in `test_routes_tests.py`, Task 4).
- **Decisions recorded as deviations (Task 6 writes them down):** no version bump; `DispenserStatus.state: str`; legacy `cmd/dispense` subscription removed now; dispatcher reports `door_open` as `failed`; `SessionSnapshot.dispense_mechanism` added (additive, spec did not ask).
- **Type consistency:** `fault_for_outcome(mechanism: str, outcome: DispenserOutcome)` used in Tasks 1, 3; `dispenser_profile_for(product) -> SlotProfile | None` in Tasks 2, 3, 4; `profiles_for(products, directory)` in Tasks 2, 3, 5; `FakeDispatcher.sent` tuples `(subsystem, command, params)` in Tasks 3, 4; `DispenseCommand(slot, mechanism, profile)` everywhere; `reconcile_dispenser_profiles()` in Tasks 2 and (future) plan 3.
- **Placeholders:** none; every test is named with its assertion and every interface has its signature. Line numbers are from HEAD `85854ca` and are hints, not anchors.
