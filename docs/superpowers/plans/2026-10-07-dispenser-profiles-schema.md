# Dispenser Profiles — Plan 1: Schema, Validation, Generator, CLI

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Spec:** `docs/superpowers/specs/2026-10-06-dispenser-profiles-design.md` — this plan is phase 1 of §10. Phases 2 (contract 0.8.0 runtime, simulator, VMC) and 3 (dashboard editor) are separate plans.

**Goal:** A standalone `dispensers.toml` with a Pydantic schema, per-slot validation with cross-checks against the catalog, a schema-generated self-documenting example file, a `--check` CLI, and startup loading that only logs. No runtime behaviour changes yet.

**Architecture:** Three focused modules under `services/`: `dispenser_schema.py` (models only), `dispensers.py` (validation pipeline, `DispenserProfiles` service, CLI entry), `dispensers_doc.py` (example generator). Validation is per slot so one bad slot never invalidates a good one. The example file is generated from the models so documentation cannot drift; a test enforces byte-identity.

**Tech Stack:** Python 3.12 (`tomllib` stdlib), Pydantic v2, pytest. No new dependencies.

## Global Constraints

- Every physical field is **required**; the only permitted default is `accessories = {}`.
- `extra="forbid"` on every model.
- Sensor absence is spelled `"unmonitored"`, never omitted.
- A slot key must match `^(0|[1-9][0-9]*)$`.
- Channel ids use `contracts.common.CHANNEL_ID_PATTERN` (`^[a-z0-9_]{1,64}$`) — the contract's own rule, in place of the stricter pattern the spec sketched, so an id valid here is valid in a capabilities document.
- Fault codes and step order live in code, never in the file.
- `ICE_COLDER_DISPENSERS` is read at call time (like `ICE_COLDER_CONFIG`), default `dispensers.toml` in cwd.
- `CONTRACT_VERSION` in `contracts/vending_machine.py` becomes `0.8.0` in this plan (the CFG codes are additive); plan 2 extends the same `0.8.0` section rather than bumping again — nothing ships between plans.
- Run tests with `uv run pytest`, lint with `ruff check --fix .` then `ruff format .`. No `&&` chaining in commands.
- Commit after every task, messages end with `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`.

---

## File map

| File | Responsibility |
|---|---|
| `services/dispenser_schema.py` (new) | Pydantic models for the file: `DispenserFile`, `SlotProfile` union, step models, `Accessory`, `CurrentSense`. Nothing else. |
| `services/dispensers.py` (new) | `Finding`, `ValidationReport`, `validate_document()`, `DispenserProfiles` service, `dispensers_path()`, CLI `main()` under `__main__`. |
| `services/dispensers_doc.py` (new) | `render_example() -> str` walking the models to produce the commented TOML. |
| `scripts/gen_dispensers_example.py` (new) | Writes `dispensers.example.toml` from `render_example()`. |
| `dispensers.example.toml` (new, committed) | Generated reference and first-run starting point. |
| `contracts/vending_machine.py` | `CFG_101`, `CFG_102` fault codes and table rows; version `0.8.0`. |
| `docs/contracts/vending-machine/CONTRACT.md`, `schemas/fault_code.schema.json` | Document and regenerate. |
| `config.example.json` | `kind` on the three sample products. |
| `main.py` | Load profiles after config, log the report, exit 1 if the path is a directory. |
| `docker-compose.yml`, `.gitignore`, `CLAUDE.md` | Env var, ignore rule, documentation. |
| `tests/conftest.py` | Shared `dispenser_profiles` fixture. |
| `tests/test_dispenser_schema.py`, `tests/test_dispensers_validation.py`, `tests/test_dispensers_service.py`, `tests/test_dispensers_example.py`, `tests/test_dispensers_cli.py` | One test module per unit. |

---

### Task 1: Fault codes `CFG-101` / `CFG-102` and contract 0.8.0

**Files:**
- Modify: `contracts/vending_machine.py` (`FaultCode` enum after `DATA_102`, `FAULT_TABLE` after the `DATA_102` row, `CONTRACT_VERSION`, module docstring history comment)
- Modify: `docs/contracts/vending-machine/CONTRACT.md` (new "Semantics fixed in 0.8.0" section above the 0.7.0 one; fault list near line 154)
- Regenerate: `docs/contracts/vending-machine/schemas/fault_code.schema.json`
- Test: `tests/test_contracts_vending.py`

**Interfaces:**
- Produces: `FaultCode.CFG_101 == "CFG-101"` (`Severity.product_unavailable`, `Scope.product`, description "No valid dispenser profile for this slot (see dispensers.toml)"); `FaultCode.CFG_102 == "CFG-102"` (`Severity.warning`, `Scope.machine`, description "dispensers.toml could not be read (missing or syntax error)"); `CONTRACT_VERSION == "0.8.0"`. Neither code is in `PAYMENT_BLOCKING_FAULTS`.

- [ ] **Step 1: Write the failing tests** in `tests/test_contracts_vending.py`:

```python
def test_cfg_faults_are_registered_and_never_block_payment():
    assert FAULT_TABLE[FaultCode.CFG_101].severity is Severity.product_unavailable
    assert FAULT_TABLE[FaultCode.CFG_101].scope is Scope.product
    assert FAULT_TABLE[FaultCode.CFG_102].severity is Severity.warning
    assert FAULT_TABLE[FaultCode.CFG_102].scope is Scope.machine
    assert FaultCode.CFG_101 not in PAYMENT_BLOCKING_FAULTS
    assert FaultCode.CFG_102 not in PAYMENT_BLOCKING_FAULTS
```
Update `test_contract_version` to expect `"0.8.0"` with a one-line comment: `# 0.7.0 -> 0.8.0: CFG-101/CFG-102 (dispenser profiles); plan 2 adds DispenseCommand/DispenseStep under the same version.`

- [ ] **Step 2: Run** `uv run pytest tests/test_contracts_vending.py -v` — expect the two new/changed tests to FAIL (`AttributeError: CFG_101`, version mismatch).
- [ ] **Step 3: Implement** the enum members, table rows, version bump, and the CONTRACT.md section (three bullets: what CFG-101 means, what CFG-102 means, that neither blocks payment; add both to the fault list paragraph that currently names `DATA-101`/`DATA-102` if present, otherwise add a sentence).
- [ ] **Step 4: Regenerate schemas:** `uv run python -m contracts.generate`. Confirm only `fault_code.schema.json` changed (`git status`).
- [ ] **Step 5: Run** `uv run pytest tests/test_contracts_vending.py tests/test_contract_schemas.py -v` — all PASS.
- [ ] **Step 6: Commit** `feat(contracts): CFG-101/CFG-102 dispenser-profile faults, contract 0.8.0`.

---

### Task 2: Schema models (`services/dispenser_schema.py`)

**Files:**
- Create: `services/dispenser_schema.py`
- Test: `tests/test_dispenser_schema.py`

**Interfaces:**
- Produces (all Pydantic v2, `ConfigDict(extra="forbid")`):
  - `UNMONITORED: Literal["unmonitored"]` type alias and the string constant `UNMONITORED_VALUE = "unmonitored"`.
  - `ChannelId = Annotated[str, StringConstraints(pattern=CHANNEL_ID_PATTERN)]` (import the pattern from `contracts.common`).
  - `class CurrentSense(BaseModel)`: `stall_current_amps: float (ge=0.1, le=50) | UNMONITORED`, `current_channel: ChannelId | UNMONITORED`; `@model_validator(mode="after")` requiring both numeric/string or both `"unmonitored"`, error text `stall_current_amps and current_channel must both be set or both be "unmonitored"`.
  - `class AgitateStep(CurrentSense)`: `motor_channel: ChannelId`, `run_seconds: float (ge=0.5, le=60)`.
  - `class IceFillBySensor(CurrentSense)`: `proof: Literal["bag_full_sensor"]`, `motor_channel`, `sensor_channel: ChannelId`, `max_run_seconds: float (ge=1, le=120)`.
  - `class IceFillTimed(CurrentSense)`: `proof: Literal["timed"]`, `motor_channel`, `max_run_seconds (1–120)`.
  - `IceFillStep = Annotated[IceFillBySensor | IceFillTimed, Field(discriminator="proof")]`.
  - `class ReleaseBySensor(BaseModel)`: `proof: Literal["door_sensor"]`, `solenoid_channel`, `sensor_channel`, `pulse_seconds (0.1–10)`, `open_timeout_seconds (0.5–30)`, `close_timeout_seconds (0.5–60)`.
  - `class ReleaseTimed(BaseModel)`: `proof: Literal["timed"]`, `solenoid_channel`, `pulse_seconds (0.1–10)`.
  - `ReleaseStep = Annotated[ReleaseBySensor | ReleaseTimed, Field(discriminator="proof")]`.
  - `class WaterFillByVolume(BaseModel)`: `proof: Literal["flow_volume"]`, `valve_channel`, `flow_sensor_channel`, `target_volume_ml: float (50–50000)`, `pulses_per_liter: float (gt=0)`, `min_flow_ml_per_second: float (gt=0)`, `no_flow_grace_seconds (0.5–30)`, `over_dispense_percent (0–50)`, `max_fill_seconds (1–600)`.
  - `class WaterFillTimed(BaseModel)`: `proof: Literal["timed"]`, `valve_channel`, `max_fill_seconds (1–600)`.
  - `WaterFillStep = Annotated[WaterFillByVolume | WaterFillTimed, Field(discriminator="proof")]`.
  - `class Accessory(BaseModel)`: `channel: ChannelId`, `on_during: list[str] (min_length=1)`, `lead_seconds (0–30)`, `lag_seconds (0–30)`; field validator: entries unique, and either exactly `["all"]` or every entry in the owning mechanism's step names — the mechanism check happens in the profile's model validator (below), since `Accessory` alone does not know its mechanism.
  - `class BaggedIceProfile(BaseModel)`: `mechanism: Literal["bagged_ice"]`, `product_sku: str (min_length=1)`, `agitate: AgitateStep`, `fill: IceFillStep`, `release: ReleaseStep`, `accessories: dict[str, Accessory] = {}`; `STEP_NAMES: ClassVar[tuple[str, ...]] = ("agitate", "fill", "release")`; model validator rejecting any accessory `on_during` entry not in `STEP_NAMES` unless the list is `["all"]`, error text `accessory "<name>": on_during contains "<x>"; valid steps for bagged_ice are agitate, fill, release (or ["all"])`.
  - `class WaterFillProfile(BaseModel)`: `mechanism: Literal["water_fill"]`, `product_sku`, `fill: WaterFillStep`, `accessories`; `STEP_NAMES = ("fill",)`; same accessory validator.
  - `SlotProfile = Annotated[BaggedIceProfile | WaterFillProfile, Field(discriminator="mechanism")]`.
  - `MECHANISM_FOR_KIND: dict[str, str] = {"ice": "bagged_ice", "water": "water_fill"}`.
  - `def worst_case_seconds(profile: SlotProfile) -> float` — the §5.2 time-budget formula (bagged ice: agitate.run + fill.max_run + release.pulse + open_timeout + close_timeout when `door_sensor`, + max accessory lead + max accessory lag; water: no_flow_grace (if by volume) + max_fill + leads + lags).
  - `def drive_channels(profile) -> set[str]` and `def sense_channels(profile) -> set[str]` — motor/solenoid/valve/accessory ids vs sensor/flow/current ids (excluding `"unmonitored"`).
  - Every `Field` has `description=`; numeric fields carry `json_schema_extra={"unit": "s" | "A" | "ml" | "pulses/L" | "ml/s" | "%"}`. The generator (Task 5) reads these.
  - **No `DispenserFile` model**: the top level is validated by hand in Task 3 so slots stay isolated.

- [ ] **Step 1: Write the failing tests** in `tests/test_dispenser_schema.py` — build dicts and call `TypeAdapter(SlotProfile).validate_python(...)`:
  - `test_bagged_ice_minimal_valid` (sensor proofs, `"unmonitored"` current, no accessories) → object with `mechanism == "bagged_ice"`, `accessories == {}`.
  - `test_unknown_field_rejected` (`fill.pulses_per_litre`) → `ValidationError` whose errors include `type == "extra_forbidden"` at loc `("fill", ..., "pulses_per_litre")`.
  - `test_timed_fill_rejects_sensor_channel` → extra_forbidden on `sensor_channel`.
  - `test_current_sense_requires_both` (numeric amps, `"unmonitored"` channel) → error containing `both be set or both be "unmonitored"`.
  - `test_accessory_on_during_must_name_mechanism_steps` (water profile, accessory `on_during=["agitate"]`) → error containing `valid steps for water_fill are fill`.
  - `test_accessory_all_alone` (`["all", "fill"]`) → error; `["all"]` → ok.
  - `test_ranges` parametrized over (`agitate.run_seconds`, 0.4), (`fill.max_run_seconds`, 121), (`release.pulse_seconds`, 0), (`fill.target_volume_ml`, 49 on water) → `ValidationError`.
  - `test_worst_case_seconds_bagged_ice`: agitate 4 + fill 25 + pulse 1.5 + open 3 + close 5 + lead 2 + lag 0.5 → `41.0`; `test_worst_case_seconds_water_timed`: max_fill 90, no accessories → `90.0`.
  - `test_drive_and_sense_channels` on a bagged-ice profile with a fan accessory → drive `{"agitator_motor","auger_motor","bag_drop_solenoid","bag_fan"}`, sense `{"bag_full_sensor","door_sensor"}`.
- [ ] **Step 2: Run** `uv run pytest tests/test_dispenser_schema.py -v` — FAIL (`ModuleNotFoundError`).
- [ ] **Step 3: Implement** `services/dispenser_schema.py` per the interface list.
- [ ] **Step 4: Run** the file again — all PASS. Then `ruff check --fix .` and `ruff format .`.
- [ ] **Step 5: Commit** `feat(dispensers): Pydantic schema for dispensers.toml slot profiles`.

---

### Task 3: Validation pipeline (`services/dispensers.py`, part 1)

**Files:**
- Create: `services/dispensers.py`
- Test: `tests/test_dispensers_validation.py`

**Interfaces:**
- Consumes: Task 2 models and helpers; `config.config_model.Product` (`sku`, `slot`, `kind`); `contracts.vending_machine.SubsystemCapabilities` (`channels[].channel_id`, `channels[].direction`).
- Produces:
  - `@dataclass(frozen=True) class Finding`: `slot: int | None`, `path: str` (dotted, e.g. `"fill.max_run_seconds"`, `""` for file-level), `line: int | None`, `severity: Literal["error", "warning"]`, `message: str`.
  - `@dataclass class ValidationReport`: `findings: list[Finding]`, `profiles: dict[int, SlotProfile]` (valid slots only), `file_error: bool` (True when syntax/top-level failed and no slot was examined); properties `errors`, `warnings`, `ok` (no errors), `slots_examined: set[int]`; methods `for_slot(slot) -> list[Finding]`, `render_text() -> str` (one line per finding `Slot 1 › fill.max_run_seconds (line 14): must be ...`, file-level lines prefixed `File:`, then a verdict line per examined slot `Slot 1 (ICE-10LB, bagged ice): OK` / `INVALID (2 errors)`, final line `OK` or `N error(s), M warning(s)`).
  - `def validate_document(text: str, products: Sequence[Product], capabilities: SubsystemCapabilities | None = None, dispense_timeout_seconds: float = 120.0) -> ValidationReport` — the full §5.2 pipeline.
  - `def humanize(err: dict, slot: int) -> Finding` — turns one Pydantic error dict into a Finding: `extra_forbidden` → `unknown field "<name>"` plus `(did you mean <close>?)` via `difflib.get_close_matches` against the model's field names; `missing` → `missing required field "<name>"`; `greater_than_equal`/`less_than_equal` pairs → `must be between <lo> and <hi> <unit>, got <v>` (read `ge`/`le`/unit from the field's metadata; fall back to Pydantic's message); `literal_error` → `must be one of <values>, got "<v>"`; `union_tag_invalid` → `proof/mechanism must be one of ...`; everything else → Pydantic's `msg` verbatim. Path is the loc joined with `.` with discriminator tags (`bag_full_sensor`, `bagged_ice`) removed.
  - `def find_line(text: str, slot: int, path: str) -> int | None` — best-effort: locate the `[slot.N` or `[slot.N.<table>` header for the deepest table in `path`, then the first `key =` line after it; `None` when not found.
- Pipeline order inside `validate_document`:
  1. `tomllib.loads`; on `TOMLDecodeError` return a report with one file-level error carrying the line parsed from the exception message (`at line N, column M`), `file_error=True`.
  2. `schema_version` missing/≠1 → file-level error; `slot` missing or not a dict → file-level error `no [slot.N] tables found`; `file_error=True` only when `slot` is unusable. Each key not matching `^(0|[1-9][0-9]*)$` → file-level error naming the key; skip it.
  3. For each slot table: `TypeAdapter(SlotProfile).validate_python(table)`; on failure map every error via `humanize`, attach `find_line`; on success store in `profiles`.
  4. Cross-checks over `profiles` (valid ones only) and `products`:
     - product with `kind in ("ice","water")` and no valid profile whose `product_sku == sku` → error at `slot=product.slot`, path `""`, message `product "<sku>" (kind <kind>, slot <n>) has no valid [slot.<n>] table`.
     - profile `product_sku` not in catalog → error `product_sku "<sku>" is not in the catalog`; product's `slot != N` → error `product "<sku>" is slot <p.slot> in the catalog, not slot <N>`; mechanism ≠ `MECHANISM_FOR_KIND[kind]` → error `mechanism "<m>" needs a product of kind "<k>", but "<sku>" is kind "<kind>"`; same sku in two slots → error on both.
     - channel roles: any id in the union of all drive sets that is also in the union of all sense sets → error on every slot using it, path `""`, message `channel "<id>" is used as both a drive and a sensor`.
     - `worst_case_seconds(p) > dispense_timeout_seconds - 5` → error `worst-case dispense of <x> s exceeds dispense_timeout_seconds <t> s minus the 5 s margin`.
     - capabilities: when given, build `{channel_id: direction}`; each drive id must be present with `"output"`, each sense id with `"input"`; missing → error `channel "<id>" is not declared by the vending board`; wrong direction → error `channel "<id>" is declared as <dir>, but this profile uses it as <role>`. When `capabilities is None` → one warning per valid slot `board capabilities unknown; channel names not verified`.
     - A slot that gains a cross-check error is **removed from `profiles`** (it is invalid).
- [ ] **Step 1: Write the failing tests** in `tests/test_dispensers_validation.py`. Define module-level helpers `ICE = Product(sku="ICE-10LB", slot=1, kind="ice")`, `WATER = Product(sku="WATER-1GAL", slot=2, kind="water")`, and a `GOOD` TOML string matching the spec §4.2 listing exactly. Tests:
  - `test_good_file_is_ok`: `validate_document(GOOD, [ICE, WATER])` → `ok`, `profiles.keys() == {1, 2}`, two capability warnings only.
  - `test_syntax_error_reports_line`: `GOOD` with `= =` injected at line 6 → `file_error`, one error, `line == 6`.
  - `test_bad_slot_key`: `[slot.01]` → file-level error mentioning `"01"`, other slots still examined.
  - `test_schema_error_names_slot_path_and_value`: `max_run_seconds = 500` → finding `slot == 1`, `path == "fill.max_run_seconds"`, message contains `between 1 and 120`, `500`; `line` equals the line of that key.
  - `test_unknown_field_suggests`: `pulses_per_litre` → message contains `did you mean pulses_per_liter`.
  - `test_one_bad_slot_does_not_sink_the_other`: slot 1 broken → `profiles.keys() == {2}`.
  - `test_missing_profile_for_catalog_product`: drop `[slot.2]` → error for slot 2 mentioning `WATER-1GAL`.
  - `test_sku_slot_mismatch`, `test_kind_mechanism_mismatch`, `test_duplicate_sku`, `test_unknown_sku`: one each, assert message substrings.
  - `test_channel_used_as_drive_and_sense`: fan accessory channel `bag_full_sensor` → error on slot 1 containing `both a drive and a sensor`, slot 1 not in `profiles`.
  - `test_time_budget`: `dispense_timeout_seconds=40` → slot 1 error containing `exceeds dispense_timeout_seconds`.
  - `test_capabilities_checked`: a `SubsystemCapabilities` declaring every channel with the right directions → no warnings; the same with `bag_fan` missing → error `not declared`; with `bag_full_sensor` declared `output` → error `declared as output`.
  - `test_render_text_format`: snapshot the exact lines for the good file (two warning lines, two OK verdicts, final `OK` line… note: `ok` is True with warnings, so the final line reads `0 error(s), 2 warning(s)`; make `render_text` print `OK` only when there are no findings at all, else the counts).
- [ ] **Step 2: Run** `uv run pytest tests/test_dispensers_validation.py -v` — FAIL.
- [ ] **Step 3: Implement** `Finding`, `ValidationReport`, `humanize`, `find_line`, `validate_document` in `services/dispensers.py`.
- [ ] **Step 4: Run** the file — PASS. Lint/format.
- [ ] **Step 5: Commit** `feat(dispensers): per-slot validation with catalog and channel cross-checks`.

---

### Task 4: `DispenserProfiles` service (`services/dispensers.py`, part 2)

**Files:**
- Modify: `services/dispensers.py`
- Test: `tests/test_dispensers_service.py`

**Interfaces:**
- Produces:
  - `def dispensers_path() -> Path` — `Path(os.environ.get("ICE_COLDER_DISPENSERS", "dispensers.toml"))`, read at call time.
  - `class DispenserProfiles`:
    - `__init__(self, config: ConfigModel, path: Path | None = None)` — `path or dispensers_path()`; stores `config` (reads `config.products` and `config.physical.dispense_timeout_seconds` live on every validation, so a catalog edit is seen on the next `load()`).
    - `report: ValidationReport` (starts as an empty OK report with no profiles), `capabilities: SubsystemCapabilities | None`, `digest: str | None` (SHA-256 hex of the file bytes at last load, `None` when missing).
    - `load() -> ValidationReport` — missing file → one file-level **warning** `dispensers.toml not found at <path>; no products have a dispenser profile` with `profiles == {}`, `file_error=True`; directory → raise `IsADirectoryError` (main.py turns this into exit 1); otherwise `validate_document(text, ...)`. Stores `report`, `digest`.
    - `validate_text(text: str) -> ValidationReport` — pure; writes nothing, does not touch `report`.
    - `save_text(text: str, expected_digest: str | None) -> ValidationReport` — if `expected_digest != self.digest` return a report with the single error `file changed on disk since you opened it; reload the page` (file untouched); else validate; if `not report.ok` return it (file untouched); else write `path.with_suffix(".toml.tmp")`, `os.replace` the existing file to `dispensers.toml.bak` (when it exists), `os.replace` tmp → path, then `load()` and return the new `report`.
    - `profile_for_slot(slot: int) -> SlotProfile | None`.
    - `set_capabilities(doc: SubsystemCapabilities | None) -> ValidationReport` — store and re-run `validate_document` on the last-loaded text (kept as `self._text`) so capability warnings resolve without a save; returns the new report.
- [ ] **Step 1: Write the failing tests** in `tests/test_dispensers_service.py` (use `tmp_path`, `monkeypatch.setenv("ICE_COLDER_DISPENSERS", ...)`, a `ConfigModel` with the two products from Task 3's helpers):
  - `test_path_from_env_read_at_call_time`.
  - `test_load_missing_file_is_a_warning_with_no_profiles`.
  - `test_load_directory_raises`.
  - `test_load_good_file_populates_profiles_and_digest`.
  - `test_validate_text_writes_nothing` (mtime and bytes unchanged).
  - `test_save_refuses_errors_and_leaves_file_and_bak_untouched`.
  - `test_save_refuses_stale_digest`.
  - `test_save_writes_atomically_and_rotates_bak` (after save: new content at path, old content at `.bak`, no `.tmp` left, `digest` updated, `profile_for_slot(1)` is the new profile).
  - `test_set_capabilities_clears_warnings`.
- [ ] **Step 2: Run** — FAIL. **Step 3: Implement.** **Step 4: Run** — PASS; lint/format.
- [ ] **Step 5: Commit** `feat(dispensers): DispenserProfiles service with atomic save and digest guard`.

---

### Task 5: Self-documenting example generator

**Files:**
- Create: `services/dispensers_doc.py`, `scripts/gen_dispensers_example.py`, `dispensers.example.toml`
- Modify: `config.example.json` (add `"kind": "ice"` to `SAMPLE-ICE`, `"kind": "water"` to both `SAMPLE-WATER-*`; slots stay 0, 1, 2)
- Test: `tests/test_dispensers_example.py`

**Interfaces:**
- Consumes: Task 2 models (field `description`, `json_schema_extra["unit"]`, `ge`/`le`/`gt` metadata, `Literal` choices); `validate_document` from Task 3.
- Produces: `def render_example() -> str` in `services/dispensers_doc.py`. Deterministic output:
  1. A header block: what the file is, where it lives, that `product_sku`/`[slot.N]` must match the catalog, how to validate (`uv run python -m services.dispensers --check`), and that this file is generated by `scripts/gen_dispensers_example.py` — do not hand-edit the example, edit the schema.
  2. `schema_version = 1`.
  3. Three slots matching `config.example.json`: `[slot.0]` bagged ice for `SAMPLE-ICE` (sensor proofs, `"unmonitored"` current, `bag_fan` and `vending_now_light` accessories), `[slot.1]` water fill by volume for `SAMPLE-WATER-SM` (1 gallon, 3785 ml), `[slot.2]` water fill **timed** for `SAMPLE-WATER-LG` (so both water variants appear live).
  4. Every field preceded by a comment line: `# <description>. Unit: <unit>. Range: <lo>–<hi>.` or `# <description>. One of: a, b.`; for a union step the live variant is written and the other variant follows as a commented block headed `# If proof = "timed" instead, the fields are:`.
  5. Sample values chosen so the whole file validates with zero errors against the example catalog at the default 120 s timeout.
- `scripts/gen_dispensers_example.py`: inserts the repo root (`Path(__file__).resolve().parents[1]`) into `sys.path`, writes `render_example()` to `dispensers.example.toml` with `\n` newlines, prints the path.
- [ ] **Step 1: Write the failing tests** in `tests/test_dispensers_example.py`:
  - `test_example_is_byte_identical_to_generator`: `Path("dispensers.example.toml").read_text(encoding="utf-8") == render_example()` with the assertion message `run: uv run python scripts/gen_dispensers_example.py`.
  - `test_example_validates_clean_against_example_config`: load `config.example.json` via `ConfigModel.model_validate`, `validate_document(example_text, config.products)` → `errors == []`, `profiles.keys() == {0, 1, 2}`.
  - `test_every_schema_field_is_documented`: for each model in Task 2, every field name appears in the example text preceded (within the previous 3 lines) by a `#` comment — guards against a new field being added without a doc line.
  - `test_example_config_products_have_kinds`: the three sample products have kinds `ice`, `water`, `water`.
- [ ] **Step 2: Run** — FAIL. **Step 3: Implement** `render_example`, the script, edit `config.example.json`, run the script to create the example. **Step 4: Run** `uv run pytest tests/test_dispensers_example.py tests/test_config_model.py tests/test_first_run.py -v` — PASS (the last two guard that the example config still loads). Lint/format.
- [ ] **Step 5: Commit** `feat(dispensers): schema-generated dispensers.example.toml`.

---

### Task 6: CLI (`python -m services.dispensers`)

**Files:**
- Modify: `services/dispensers.py` (add `main(argv: list[str] | None = None) -> int` and `if __name__ == "__main__": raise SystemExit(main())`)
- Test: `tests/test_dispensers_cli.py`

**Interfaces:**
- `main(argv)` parses with `argparse`: positional `path` (default `dispensers_path()`), `--config PATH` (default `main._config_path()` — import lazily to avoid a cycle, or duplicate the one-line env lookup), `--capabilities FILE` (JSON `SubsystemCapabilities`), `--check` (default action), `--example` (print `render_example()` and return 0). `--check` prints `report.render_text()` to stdout and returns `0` when `report.ok` else `1`. A missing dispensers file prints the warning line and returns 0 (it is a warning); a directory or unreadable config prints one line to stderr and returns 2.
- [ ] **Step 1: Write the failing tests** (call `main([...])` with `capsys`, never subprocess):
  - `test_check_ok_exit_0` (temp good file + temp config JSON written from a `ConfigModel` with the two products) → return 0, stdout ends with the counts line.
  - `test_check_errors_exit_1` → return 1, stdout contains `Slot 1 › fill.max_run_seconds`.
  - `test_check_with_capabilities_file_clears_warnings`.
  - `test_example_prints_generator_output` → stdout == `render_example()`.
  - `test_missing_file_is_warning_exit_0`; `test_directory_exit_2`.
- [ ] **Step 2: Run** — FAIL. **Step 3: Implement.** **Step 4: Run** — PASS; also `uv run python -m services.dispensers --example` prints the example (manual check). Lint/format.
- [ ] **Step 5: Commit** `feat(dispensers): --check / --example CLI`.

---

### Task 7: Startup wiring, compose, docs, shared fixture

**Files:**
- Modify: `main.py` (after `load_config()` in `main()`: construct `DispenserProfiles(config)`, call `load()`, log each finding at `warning` for warnings and `error` for errors via loguru, log the verdict line; catch `IsADirectoryError` → `logger.error(...)` mirroring the config wording and `sys.exit(1)`; keep the instance in a module-level `dispenser_profiles` variable so plan 2 can hand it to the VMC and routes)
- Modify: `docker-compose.yml` (add `- ICE_COLDER_DISPENSERS=/app/data/dispensers.toml` beside every `ICE_COLDER_CONFIG` line, four services), `docker/docker-compose.prod.yml` (same, if it carries `ICE_COLDER_CONFIG`), `.gitignore` (`dispensers.toml` under the config comment), `.env.example` (no change unless it documents `ICE_COLDER_CONFIG`; if it does, add the sibling line)
- Modify: `tests/conftest.py` (fixture `dispenser_profiles(tmp_path, monkeypatch)` that writes the Task 3 `GOOD` text to `tmp_path / "dispensers.toml"`, sets `ICE_COLDER_DISPENSERS`, builds a `ConfigModel` with the two products, returns a loaded `DispenserProfiles`; the `GOOD` text and two `Product`s move to `tests/dispenser_fixtures.py` so Task 3's tests and conftest share them)
- Modify: `CLAUDE.md` (new "Dispenser profiles (`dispensers.toml`)" subsection under Configuration: location, env var, generated example, `--check`, the three modules, CFG-101/CFG-102, and that plan 1 only loads and logs)
- Test: `tests/test_main_startup_dispensers.py`

- [ ] **Step 1: Write the failing tests** in `tests/test_main_startup_dispensers.py`:
  - `test_startup_loads_and_logs_report` — call the new `main.load_dispenser_profiles(config) -> DispenserProfiles` helper (extract the construction + logging into this function so it is testable without running the event loop) with `ICE_COLDER_DISPENSERS` pointing at a bad file; assert it returns the instance and `caplog` contains the finding line.
  - `test_startup_exits_when_path_is_directory` — `pytest.raises(SystemExit)` with code 1.
  - `test_fixture_provides_two_profiles` — uses the conftest fixture, asserts `profile_for_slot(1).mechanism == "bagged_ice"` and `profile_for_slot(2).mechanism == "water_fill"`.
  - `test_compose_sets_dispensers_env` — read `docker-compose.yml` as text, assert `ICE_COLDER_DISPENSERS=/app/data/dispensers.toml` appears once per `ICE_COLDER_CONFIG` occurrence.
- [ ] **Step 2: Run** — FAIL. **Step 3: Implement** all file edits. **Step 4: Run the full suite** `uv run pytest` — all PASS (browser tests skip). Lint/format.
- [ ] **Step 5: Commit** `feat(dispensers): load dispensers.toml at startup, compose env, docs, shared fixture`.

---

## Self-review

- **Spec coverage (plan 1 scope, §10 item 1):** §4.1 location/env/gitignore/example/directory-exit → Tasks 5, 7. §4.2 shape and rules → Task 2 (schema) and Task 3 (slot keys, isolation). §5.1 models → Task 2. §5.2 layers 1–5 → Task 3. §5.3 generator and drift test → Task 5. §5.4 CLI → Task 6. §6.1 service object (`load`, `validate_text`, `save_text`, `profile_for_slot`, `set_capabilities`, `digest`) → Task 4. §6.2 fault rows (registration only; reconciliation is plan 2) → Task 1. `config.example.json` kinds and shared fixture (§9) → Tasks 5, 7. Startup "loads and only logs" → Task 7.
- **Deliberate deviations from the spec, recorded in CLAUDE.md by Task 7:** channel-id pattern follows the contract's `CHANNEL_ID_PATTERN`; the contract version bump lands in plan 1 (0.8.0) and plan 2 extends the same section; there is no `DispenserFile` model because top-level validation is by hand to keep slots isolated.
- **Type consistency:** `validate_document(text, products, capabilities=None, dispense_timeout_seconds=120.0)` is used identically in Tasks 3, 4, 5, 6; `ValidationReport.ok/errors/warnings/profiles/file_error/render_text()` named the same throughout; `DispenserProfiles(config, path=None)` in Tasks 4, 6, 7; `render_example()` in Tasks 5, 6.
- **Placeholders:** none; every test is named with its assertion and every interface has its signature.
