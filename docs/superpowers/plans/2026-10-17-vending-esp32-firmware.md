# Vending ESP32 Firmware Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Working firmware for the IM600XL vending board (contract 2.0.0), a hardware interface document for the relay module, and a conformance suite that checks the real board and the simulator with the same tests.

**Architecture:** A pure C++ library, `vendcore` (no ESP-IDF dependency; `Clock`, `IoPort`, `Publisher` are ports), holds every decision: profile parsing, the dispense state machine, interlocks, the RO and freeze loops, the ack cache, and the JSON wire serializers. It is built up test-first on the host (`pio test -e native`) in Tasks 2–9. Two thin ESP-IDF tasks then wrap it (Tasks 10–11): a 10 ms sequencer task owning the I²C expanders, the pulse counter, the 1-Wire sensor and the hardware watchdog kick, and a comms task owning the network, MQTT and the queues. The conformance suite (Task 12) drives any board over MQTT; the interface doc and repo docs close it out (Task 13).

**Tech Stack:** C++17, PlatformIO (`platform = espressif32`, `framework = espidf`; `platform = native` for host tests), Unity test framework, ArduinoJson 7 (header-only, works on both envs), ESP-IDF 5.x (`esp-mqtt`, `driver/i2c_master.h`, `driver/pulse_cnt.h`, `onewire_bus` + `ds18b20` managed components, NVS), Python 3.12 + pytest + aiomqtt for the conformance suite.

**Spec:** [`docs/superpowers/specs/2026-10-17-vending-esp32-firmware-design.md`](../specs/2026-10-17-vending-esp32-firmware-design.md). Section references (§) are to the spec. Hardware tags (IC4, W7…) are from [`docs/hardware/im600xl-io-list.md`](../../hardware/im600xl-io-list.md). The 2.0.0 contract shapes (`ButtonPress.button: str`, `ReleaseByCatch`, the 17-channel list) are defined in the [IM600XL simulator design](../specs/2026-10-17-im600xl-simulator-design.md) §3 and §5.1; that plan is **not yet implemented**, and this plan does not depend on it except where Task 12 says so.

**Plan style:** each task gives files, interfaces and the tests to write. Implementers write the code bodies, matching surrounding style. C++ signatures below are the contract between tasks; bodies are the implementer's.

## Global Constraints

- The board implements vending-machine contract **2.0.0**: `capabilities/vending` carries `contract_version: "2.0.0"`, `ButtonPress.button` is a channel-id **string**, `ReleaseByCatch` (`proof: "catch"`, `catch_channel`, `drop_seconds` 0.5–30) is a valid release step, and `sensors/water_flow` is never published.
- The 17 channels, exactly (spec §4.2 / IM600XL §5.1): binary inputs `bag_full_sensor`, `button_ice`, `button_water_1gal`, `button_water_5gal`, `ro_tank_full`, `ro_tank_empty`, `water_pressure_ok`, `sump_high`, `service_door`; binary outputs `agitator_motor`, `auger_motor`, `bag_fan`, `trap_door_catch`, `water_valve_solenoid`, `ro_production`, `heater_power`; counter `fill_pulses`; temperature `ambient`. `driven_by`: the four ice outputs → `"dispense"`, `water_valve_solenoid` → `"water_valve"`, others `null`. Commands: `["ping", "self_test", "force_report", "dispense", "water_valve"]`.
- Every channel id and command name in C++ comes from one table, `vendcore/channels.h` (Task 2); no string literal for them anywhere else in `firmware/`.
- `vendcore` has **no** ESP-IDF, FreeRTOS or Arduino include, and no dynamic allocation after construction (fixed-capacity containers; ArduinoJson documents sized at compile time). A native-env test must compile and run every vendcore header.
- All timing in `vendcore` is in milliseconds as `uint32_t` from the injected `Clock`; nothing calls a system clock or sleeps.
- Safety invariants that every task preserves: an output is written only through `OutputGuard` (Task 7); any dispense exit other than `complete` de-energizes every output including the catch; the watchdog kick is asserted only when `Health::ok()` (Task 7) is true.
- `.gitignore` already ignores `lib/`; Task 1 adds the negation so `firmware/vending/lib/` is tracked. `.pio/` is ignored.
- Firmware acks exactly as the contract says: no `request_id` → no ack; unknown command → `unsupported`; bad params → `rejected` with a detail naming the field; busy → `rejected` + `"busy"`; VMC silent → `rejected` + `"vmc_offline"`.
- Run per task: `pio test -e native` (from `firmware/vending/`) green; `pio run -e kc868-a8` builds from Task 1 on; `uv run pytest` green for any task touching `tests/`; `ruff check --fix .` and `ruff format .` for Python.
- Commit trailer on every commit: `Assisted-by: <model> (Claude Code)`.

## File map

| File | Change |
|---|---|
| `.gitignore` | `!firmware/vending/lib/`, `.pio/` |
| `.github/workflows/ci.yml` | new `firmware` job (Task 1); `ICE_COLDER_BOARD_TESTS: "1"` on the test job (Task 12) |
| `firmware/vending/platformio.ini` | envs `kc868-a8`, `native` |
| `firmware/vending/sdkconfig.defaults` | IDF options (task WDT, MQTT, NVS) |
| `firmware/vending/lib/vendcore/` | the pure library: `channels.h`, `ports.h`, `profile.h/.cpp`, `command.h/.cpp`, `ack_cache.h/.cpp`, `inputs.h/.cpp`, `dispenser.h/.cpp`, `interlocks.h/.cpp`, `ro_loop.h/.cpp`, `freeze_protect.h/.cpp`, `core.h/.cpp`, `wire.h/.cpp` |
| `firmware/vending/test/test_<module>/test_main.cpp` | one Unity test dir per vendcore module |
| `firmware/vending/test/fixtures/*.json` | hand-written 2.0.0 profiles and commands |
| `firmware/vending/test/golden/*.json` | JSON the serializers emit, validated by `tests/firmware/test_golden_wire.py` |
| `firmware/vending/boards/kc868-a8.h` | pin map, I²C addresses, polarity table |
| `firmware/vending/src/` | `main.cpp`, `kc868_io.h/.cpp`, `esp_clock.h`, `settings.h/.cpp`, `console.h/.cpp`, `sequencer_task.h/.cpp`, `comms_task.h/.cpp`, `mqtt_publisher.h/.cpp`, `net.h/.cpp` |
| `firmware/vending/idf_component.yml` | `espressif/onewire_bus`, `espressif/ds18b20` |
| `firmware/README.md` | project, build, flash, provisioning, links |
| `firmware/docs/vending-board-interface.md` | relay module spec and bench checklist |
| `tests/firmware/` | `conftest.py`, `board_client.py`, `test_capabilities.py`, `test_liveness.py`, `test_standard_commands.py`, `test_idempotency.py`, `test_water_valve.py`, `test_dispense.py`, `test_rejections.py`, `test_golden_wire.py` |
| `tests/skip_policy.py` | docstring: the board suite is opt-in like the browser tests and must run in CI |
| `CLAUDE.md`, `ROADMAP.md`, `docs/hardware/im600xl-io-list.md` | docs |

---

### Task 1: Toolchain, project skeleton, CI job

Nothing in `firmware/` builds today and the machine has neither PlatformIO nor a host C++ compiler. This task makes `pio run` and `pio test -e native` work locally and in CI with a trivial library and test, so every later task has a green baseline to extend.

**Files:**
- Create: `firmware/vending/platformio.ini`
  ```ini
  [platformio]
  default_envs = kc868-a8

  [env]
  lib_deps = bblanchon/ArduinoJson@^7.2
  build_flags = -std=gnu++17 -Wall -Wextra -Werror
  test_framework = unity

  [env:kc868-a8]
  platform = espressif32@^6.9
  board = esp32dev
  framework = espidf
  monitor_speed = 115200
  build_flags = ${env.build_flags} -DVENDCORE_TARGET_ESP32

  [env:native]
  platform = native
  build_flags = ${env.build_flags} -DVENDCORE_TARGET_NATIVE
  test_build_src = false
  ```
- Create: `firmware/vending/sdkconfig.defaults` — `CONFIG_ESP_TASK_WDT_EN=y`, `CONFIG_ESP_TASK_WDT_TIMEOUT_S=5`, `CONFIG_MQTT_PROTOCOL_311=y`, `CONFIG_FREERTOS_HZ=1000`.
- Create: `firmware/vending/lib/vendcore/version.h` — `namespace vendcore { constexpr const char* kContractVersion = "2.0.0"; constexpr const char* kFirmwareVersion = "0.1.0"; }` plus `firmware/vending/lib/vendcore/library.json` (`{"name": "vendcore", "version": "0.1.0"}`).
- Create: `firmware/vending/src/main.cpp` — ESP-IDF `app_main` that logs `kFirmwareVersion` and idles. Replaced in Task 10.
- Create: `firmware/vending/test/test_version/test_main.cpp` — Unity test asserting `kContractVersion` is `"2.0.0"`.
- Modify: `.gitignore` — append `!firmware/vending/lib/` directly after the existing `lib/` line, and `.pio/` under a `# PlatformIO` heading.
- Modify: `.github/workflows/ci.yml` — new job `firmware` (ubuntu-latest): checkout, `astral-sh/setup-uv@v5` with `python-version: "3.12"`, `uv tool install platformio`, `actions/cache@v4` on `~/.platformio` keyed by `hashFiles('firmware/vending/platformio.ini')`, then `pio run -e kc868-a8` and `pio test -e native`, both with `working-directory: firmware/vending`.

**Interfaces:**
- Produces: a `vendcore` library every later task adds headers to; the `native` env where every later task's tests run; `kContractVersion`/`kFirmwareVersion` consumed by Task 9's capabilities.

- [ ] **Step 1: Install the toolchain locally.** `uv tool install platformio`; a host g++ on PATH (e.g. `winget install -e --id BrechtSanders.WinLibs.POSIX.UCRT`, then a new shell). Verify `pio --version` and `g++ --version`.
- [ ] **Step 2: Write the version test**, run `pio test -e native` from `firmware/vending/`: fails (no header).
- [ ] **Step 3: Create the files above.** `pio test -e native` passes; `pio run -e kc868-a8` builds (first run downloads the IDF toolchain, several minutes).
- [ ] **Step 4: `git status` shows `firmware/vending/lib/vendcore/` as tracked** (the `.gitignore` negation works) and nothing under `.pio/`.
- [ ] **Step 5: Commit** — `build(firmware): PlatformIO skeleton for the vending board with a native test env and CI job`

---

### Task 2: Channel table, ports and profile parsing

**Files:**
- Create: `firmware/vending/lib/vendcore/channels.h` — the one declarative table:
  ```cpp
  namespace vendcore {
  enum class Kind : uint8_t { Binary, Counter, Temperature };
  enum class Dir : uint8_t { Input, Output };
  struct Channel { const char* id; Kind kind; Dir dir; const char* driven_by; const char* unit; const char* description; float interval_s; };
  constexpr Channel kChannels[17] = { /* spec §4.2 order: 9 inputs, 7 outputs, fill_pulses, ambient */ };
  constexpr const char* kCommands[5] = {"ping", "self_test", "force_report", "dispense", "water_valve"};
  constexpr const char* kSubsystem = "vending";
  int channel_index(const char* id);             // -1 when undeclared
  bool is_output(int idx); bool is_binary_input(int idx);
  constexpr int kOutputCount = 7; constexpr int kBinaryInputCount = 9;
  enum class Out : uint8_t { Agitator, Auger, BagFan, TrapDoorCatch, WaterValve, RoProduction, Heater };
  enum class In  : uint8_t { BagFull, ButtonIce, ButtonWater1, ButtonWater5, RoTankFull, RoTankEmpty, WaterPressureOk, SumpHigh, ServiceDoor };
  Out out_from_index(int idx); In in_from_index(int idx);
  }
  ```
- Create: `firmware/vending/lib/vendcore/ports.h`:
  ```cpp
  struct Clock { virtual uint32_t now_ms() const = 0; };
  struct InputSnapshot { bool binary[kBinaryInputCount]; uint32_t pulses; float ambient_c; bool ambient_valid; bool master_enable; };
  struct IoPort { virtual bool read(InputSnapshot& out) = 0;            // false on I²C failure
                  virtual bool write_outputs(const bool (&on)[kOutputCount]) = 0;
                  virtual void kick_watchdog() = 0; };
  enum class EventType : uint8_t { InputChanged, ButtonPressed, Step, Outcome, Telemetry, Temperature, OutputChanged, Health };
  struct Event { EventType type; uint8_t channel; bool state; uint32_t value; float fvalue; char text[48]; char request_id[65]; uint8_t slot; };
  struct Publisher { virtual bool publish(const Event& e) = 0; };  // false = queue full
  ```
- Create: `firmware/vending/lib/vendcore/profile.h/.cpp` — C++ mirror of `dispenser_schema.py` plus `ReleaseByCatch`:
  ```cpp
  enum class Mechanism : uint8_t { BaggedIce, WaterFill };
  struct Agitate { int motor; float run_s; };                                  // channel indexes, -1 = absent
  struct IceFill { enum { BySensor, Timed } proof; int motor; int sensor; float max_run_s; };
  struct Release { enum { ByCatch, Timed, BySensor } proof; int channel; int sensor; float drop_s; float pulse_s; float open_timeout_s; float close_timeout_s; };
  struct WaterFill { enum { ByVolume, Timed } proof; int valve; int flow; float target_ml; float pulses_per_l; float min_flow_ml_s; float no_flow_grace_s; float over_pct; float max_fill_s; };
  struct Accessory { int channel; uint8_t on_during_mask; /* bit per step: agitate=1, fill=2, release=4; all=7 */ float lead_s; float lag_s; };
  struct SlotProfile { Mechanism mechanism; char sku[33]; Agitate agitate; IceFill ice; Release release; WaterFill water; Accessory acc[4]; uint8_t acc_count; };
  struct ParseResult { bool ok; char detail[96]; };
  ParseResult parse_profile(JsonVariantConst v, Mechanism expected, SlotProfile& out);
  float worst_case_seconds(const SlotProfile& p);
  ```
  `parse_profile` enforces: `profile.mechanism == expected` (detail `"mechanism mismatch"`), every named channel declared (detail `"undeclared channel <id>"`) and of the right direction, every bound in `dispenser_schema.py` (detail `"<path> out of range"`), unknown keys rejected (the schema is strict), `on_during` names only that mechanism's steps.
- Create: `firmware/vending/test/fixtures/` — `profile_ice_catch.json` (IM600XL slot 0: `IceFillBySensor` on `auger_motor`/`bag_full_sensor`, agitate `agitator_motor` 2 s, `ReleaseByCatch` `trap_door_catch` 3 s, accessory `bag_fan` on `["agitate","fill"]` lead 1 lag 2), `profile_water_1gal.json` (`WaterFillByVolume` valve `water_valve_solenoid`, flow `fill_pulses`, 3785 ml, 450 pulses/l, min 20 ml/s, grace 3 s, over 5 %, max 120 s), `profile_water_timed.json`, `profile_ice_timed.json`, and bad variants: `bad_mechanism.json`, `bad_channel.json`, `bad_range.json`, `bad_extra_key.json`.
- Test: `firmware/vending/test/test_channels/test_main.cpp`, `test_profile/test_main.cpp`. Fixtures are embedded via a tiny generated header (`test/fixtures/fixtures.h`, produced by `firmware/vending/tools/embed_fixtures.py`, run by hand and committed) so the native test needs no filesystem.

**Interfaces:**
- Produces: `kChannels`, `Out`/`In`, `channel_index`, `InputSnapshot`, `IoPort`, `Clock`, `Publisher`, `Event`, `SlotProfile`, `parse_profile`, `worst_case_seconds` — used by every later task.

- [ ] **Step 1: Write the tests** — table has 17 entries with the exact ids and `driven_by`; `channel_index` round-trips every id and returns -1 for `"water_flow"`; each good fixture parses with the expected indexes and numbers; each bad fixture fails with the expected detail prefix; `worst_case_seconds` of the catch profile is lead + agitate + max fill + drop + lag.
- [ ] **Step 2: Run** `pio test -e native -f test_channels -f test_profile`: fail to compile.
- [ ] **Step 3: Implement** `channels.h`, `ports.h`, `profile.*`, the embed tool and header.
- [ ] **Step 4: Run** the two test dirs: pass. `pio run -e kc868-a8` still builds.
- [ ] **Step 5: Commit** — `feat(firmware): channel table, ports and 2.0.0 profile parsing in vendcore`

---

### Task 3: Command parsing and the ack cache

**Files:**
- Create: `firmware/vending/lib/vendcore/command.h/.cpp`:
  ```cpp
  enum class CmdKind : uint8_t { Ping, SelfTest, ForceReport, Dispense, WaterValve, Unknown };
  struct Command { CmdKind kind; char request_id[65]; char name[33]; uint8_t slot; Mechanism mechanism; SlotProfile profile; uint8_t valve_seconds; };
  enum class Decode : uint8_t { Ok, NoRequestId, Rejected, Unsupported };
  struct Decoded { Decode result; Command cmd; char detail[96]; };
  Decoded decode_command(const char* json, size_t len);   // ArduinoJson StaticJsonDocument<4096>
  ```
  Rules: `request_id` must be a string of 8–64 chars or `NoRequestId`; `command` not in `kCommands` → `Unsupported` (with `request_id` kept so an ack can be sent); `water_valve.seconds` integer 1–10 else `Rejected` `"seconds out of range [1, 10]"`; `dispense` requires `slot` (int ≥ 0), `mechanism` (`"bagged_ice"`/`"water_fill"`), `profile` (via `parse_profile`), detail `"dispense requires: slot, mechanism, profile (missing: …)"` when any is absent.
- Create: `firmware/vending/lib/vendcore/ack_cache.h/.cpp`:
  ```cpp
  enum class AckStatus : uint8_t { Ok, Rejected, Failed, Unsupported };
  enum class Phase : uint8_t { Accepted, Completed };
  struct Ack { char request_id[65]; char command[33]; AckStatus status; Phase phase; char detail[96]; char result_json[512]; };
  class AckCache { public: static constexpr int kCapacity = 32;
    const Ack* find(const char* request_id) const; void put(const Ack& a); /* same id overwrites in place; else evicts oldest */ };
  ```
- Test: `test_command/test_main.cpp`, `test_ack_cache/test_main.cpp`.

**Interfaces:**
- Consumes: `parse_profile`, `kCommands` (Task 2).
- Produces: `decode_command`, `Decoded`, `Command`, `Ack`, `AckCache`, `AckStatus`, `Phase` — used by Task 9 (core) and Task 11 (comms).

- [ ] **Step 1: Write the tests** — missing/empty/non-string/7-char/65-char `request_id` → `NoRequestId`; `"frobnicate"` → `Unsupported` with id kept; `water_valve` 0 and 11 → `Rejected` with the range detail, 1 and 10 → `Ok`; `dispense` fixtures from Task 2 decode with the right mechanism and slot; missing `profile` → the "requires" detail; 33rd `put` evicts the first; `put` with an existing id overwrites (`Accepted` → `Completed`) without growing; `find` of unknown returns null.
- [ ] **Step 2: Run** → compile failure. **Step 3: Implement.** **Step 4: Run** → pass.
- [ ] **Step 5: Commit** — `feat(firmware): command decoding and the 32-entry ack cache`

---

### Task 4: Input scanning, debounce, polarity and buttons

**Files:**
- Create: `firmware/vending/lib/vendcore/inputs.h/.cpp`:
  ```cpp
  struct Polarity { bool active_low[kBinaryInputCount]; };         // true: raw 0 means asserted
  class InputScanner { public:
    InputScanner(const Polarity& p, uint32_t debounce_ms = 30);
    void scan(const InputSnapshot& raw, uint32_t now_ms, Publisher& pub);   // emits InputChanged / ButtonPressed
    bool state(In which) const;                                              // debounced, polarity-applied
    void republish_all(Publisher& pub) const;                                // every input's current state
    uint32_t pulses() const;                                                 // last raw counter
  };
  ```
  A `ButtonPressed` event is emitted once per asserted edge of the three button inputs (after debounce), in addition to their `InputChanged`. Before the first stable reading no event is emitted and `state()` returns the polarity-applied raw value.
- Test: `test_inputs/test_main.cpp` with a `VecPublisher` (fixed array recorder) — add `firmware/vending/test/support/fakes.h` holding `FakeClock { uint32_t t; }`, `FakeIo` (scripted snapshots, recorded `write_outputs` and kick count), `VecPublisher` (array of up to 256 events). Every later test dir includes this header.

**Interfaces:**
- Consumes: `InputSnapshot`, `In`, `Publisher` (Task 2).
- Produces: `InputScanner`, `Polarity`, `test/support/fakes.h` — used by Tasks 5–9.

- [ ] **Step 1: Write the tests** — a 20 ms blip at 10 ms ticks emits nothing; a 40 ms hold emits one `InputChanged`; `active_low` inverts; `button_ice` asserted emits `ButtonPressed` with channel index of `button_ice` exactly once until released and re-pressed; `republish_all` emits 9 events; `state(In::ServiceDoor)` follows the debounced value.
- [ ] **Step 2–4:** fail, implement, pass.
- [ ] **Step 5: Commit** — `feat(firmware): debounced input scanner with polarity table and button events`

---

### Task 5: Dispenser state machine, bagged ice

**Files:**
- Create: `firmware/vending/lib/vendcore/dispenser.h/.cpp`:
  ```cpp
  enum class Step : uint8_t { Agitate, Fill, Release };
  enum class Outcome : uint8_t { Complete, BinEmpty, Timeout, Jam, Error, DoorOpen, NoFlow, OverDispense };
  const char* step_name(Step); const char* outcome_name(Outcome);           // contract strings
  class Dispenser { public:
    enum class State : uint8_t { Idle, Running, Done };
    bool start(const Command& cmd, uint32_t now_ms);                          // false if Running
    void tick(uint32_t now_ms, const InputScanner& in, bool (&outputs)[kOutputCount], Publisher& pub);
    void abort(uint32_t now_ms, const char* detail, Publisher& pub);         // Outcome::Error, all off
    bool first_actuator_on() const;                                           // for the accepted ack
    State state() const; const char* request_id() const; uint8_t slot() const;
    void finish();                                                            // Done -> Idle
  };
  ```
  Bagged-ice order (§5.1): `trap_door_catch` on → accessories whose mask includes the first step turn on `lead_s` before it → `Step::Agitate` (motor on `run_s`) → `Step::Fill` (motor on until `bag_full_sensor` asserted, or `max_run_s` → `BinEmpty` for `BySensor`, `Complete` for `Timed`) → motors off → `Step::Release` (`ByCatch`: catch off, wait `drop_s`; `Timed`: solenoid on `pulse_s`; `BySensor`: pulse, then wait for the sensor open then closed within the timeouts, `DoorOpen` if it never closes) → accessories off after `lag_s` → `Outcome::Complete`. Each step entry emits `Event{Step}`; the end emits exactly one `Event{Outcome}` with `request_id`, `slot` and a `text` detail (`"bag_full after 8.2 s"`, `"max_run_s reached"`). Every non-`Complete` exit sets all seven outputs false in the same tick.
- Test: `test_dispenser_ice/test_main.cpp`.

**Interfaces:**
- Consumes: `Command`, `SlotProfile`, `InputScanner`, `Publisher`, `Out` (Tasks 2–4).
- Produces: `Dispenser`, `Step`, `Outcome`, `step_name`, `outcome_name` — used by Tasks 6, 7, 9.

- [ ] **Step 1: Write the tests** (drive `tick` at 10 ms with `FakeClock`, assert on the `outputs` array and `VecPublisher`): catch is on before any motor; `bag_fan` turns on 1000 ms before agitate and stays 2000 ms after fill; agitate runs exactly `run_s`; fill ends on the sensor with `Complete`, detail begins `"bag_full after"`; fill hitting `max_run_s` → `BinEmpty`, every output false, catch included; `Timed` fill hitting `max_run_s` → `Complete`; `ByCatch` release: catch off then `drop_s` later `Complete`; `abort` mid-fill → `Error` with the given detail and all off; `first_actuator_on()` false until the catch energizes, then true; `start` while `Running` returns false; exactly one `Outcome` event per run.
- [ ] **Step 2–4:** fail, implement, pass.
- [ ] **Step 5: Commit** — `feat(firmware): bagged-ice dispense sequence with catch-first release`

---

### Task 6: Dispenser, water fill

**Files:**
- Modify: `firmware/vending/lib/vendcore/dispenser.h/.cpp` — the `WaterFill` branch. `ByVolume`: valve on; target pulses = `target_ml × pulses_per_l / 1000` (rounded); `Event{Telemetry}` for `fill_pulses` every 1000 ms while filling (cumulative counter value); `NoFlow` when the measured rate over the last 1000 ms is below `min_flow_ml_s` for longer than `no_flow_grace_s`; `OverDispense` when pulses exceed target × (1 + `over_pct`/100) after the valve was commanded off (the valve is turned off at target; pulses that keep arriving past the margin are the fault); `Timeout` at `max_fill_s`; `Complete` detail `"<n> pulses"`. `Timed`: valve on `max_fill_s`, `Complete`. Only `Step::Fill` is emitted for water.
- Test: `test_dispenser_water/test_main.cpp`.

**Interfaces:**
- Consumes/produces: as Task 5; `FakeIo` snapshots now script `pulses`.

- [ ] **Step 1: Write the tests** — valve opens on start and `first_actuator_on()` is true; 1703 pulses (3785 ml × 450/l) → valve off and `Complete` `"1703 pulses"`; no pulses for grace + 1 tick → `NoFlow`, valve off; pulses continuing to 1800 after valve-off → `OverDispense`; stuck below the target at `max_fill_s` → `Timeout`; a `Telemetry` event each 1000 ms during the fill and none after; accessory with `on_during ["fill"]` follows the valve with lead/lag.
- [ ] **Step 2–4:** fail, implement, pass.
- [ ] **Step 5: Commit** — `feat(firmware): water fill by pulse volume with no-flow, over-dispense and timeout outcomes`

---

### Task 7: Interlocks, output guard, VMC liveness and health

**Files:**
- Create: `firmware/vending/lib/vendcore/interlocks.h/.cpp`:
  ```cpp
  class OutputGuard { public:                       // the ONLY writer of the output array
    void request(Out o, bool on, uint32_t max_on_ms, uint32_t now_ms);   // per-output software max-on-time
    void all_off();
    void apply(uint32_t now_ms, bool (&out)[kOutputCount], Publisher& pub);  // enforces max-on, emits OutputChanged
    bool any_motion() const;                         // agitator, auger, catch, valve
  };
  class VmcLiveness { public: explicit VmcLiveness(uint32_t silence_ms = 30000);
    void seen(uint32_t now_ms); void set_online(bool online, uint32_t now_ms);
    bool silent(uint32_t now_ms) const; };
  struct TripReason { const char* detail; };         // "service_door", "sump_high", "vmc_offline", "master_enable_dropped", "io_failure"
  class Interlocks { public:
    bool trip(const InputScanner& in, const InputSnapshot& raw, bool io_ok, const VmcLiveness& vmc, bool motion, uint32_t now_ms, TripReason& why) const;
  };
  class Health { public: void set_io_ok(bool); void set_deadline_met(bool); void set_latched(bool); bool ok() const; };
  ```
  Trip rules (§5.1, §7): `service_door` asserted while `motion`; `sump_high` asserted (always); `vmc.silent()`; `raw.master_enable == false` while any output is requested on; `io_ok == false`. A trip is reported every tick it holds; the core (Task 9) acts on the first one.
- Test: `test_interlocks/test_main.cpp`.

**Interfaces:**
- Consumes: `InputScanner`, `InputSnapshot`, `Out` (Tasks 2, 4).
- Produces: `OutputGuard`, `VmcLiveness`, `Interlocks`, `Health`, `TripReason` — used by Tasks 8–10. From here on `Dispenser::tick` writes through an `OutputGuard&` instead of the raw array; update Task 5/6 tests accordingly in this task (the signature change is this task's work).

- [ ] **Step 1: Write the tests** — an output requested with `max_on_ms` 500 is forced off at 510 and emits `OutputChanged`; `all_off` clears everything; `any_motion` true for the valve, false for the heater; `VmcLiveness` silent at 30001 ms after the last `seen`, not at 29999, immediately on `set_online(false)`; each trip rule fires with its detail and no other rule fires in a clean state; the door trips only with motion; `Health::ok()` false if any flag is false.
- [ ] **Step 2–4:** fail, implement, pass (including the migrated dispenser tests).
- [ ] **Step 5: Commit** — `feat(firmware): output guard, interlocks, VMC liveness and health gate`

---

### Task 8: RO fill loop and freeze protection

**Files:**
- Create: `firmware/vending/lib/vendcore/ro_loop.h/.cpp`:
  ```cpp
  class RoLoop { public: explicit RoLoop(uint32_t max_run_ms = 5400000);
    void tick(const InputScanner& in, OutputGuard& out, uint32_t now_ms, Publisher& pub);
    bool latched() const; };
  ```
  `ro_production` requested on while `ro_tank_full` is false; off when full; after `max_run_ms` continuous running, off and `latched` (emits `Health` event text `"ro_overrun_latched"`) until `ro_tank_full` becomes true.
- Create: `firmware/vending/lib/vendcore/freeze_protect.h/.cpp`:
  ```cpp
  class FreezeProtect { public: FreezeProtect(float on_below_c = 3.0f, float off_above_c = 6.0f);
    void tick(const InputSnapshot& raw, OutputGuard& out, uint32_t now_ms); };
  ```
  Heater on below `on_below_c`, off above `off_above_c`, unchanged between; off whenever `ambient_valid` is false.
- Test: `test_ro_loop/test_main.cpp`, `test_freeze_protect/test_main.cpp`.

**Interfaces:**
- Consumes: `InputScanner`, `OutputGuard`, `InputSnapshot` (Tasks 4, 7).
- Produces: `RoLoop`, `FreezeProtect` — used by Task 9.

- [ ] **Step 1: Write the tests** — pump on when not full, off at full, on again when full drops; running 5400001 ms → off, `latched`, stays off while not full, releases when full; heater on at 2.9 °C, still on at 4.5 °C, off at 6.1 °C, off when the sensor is invalid.
- [ ] **Step 2–4:** fail, implement, pass.
- [ ] **Step 5: Commit** — `feat(firmware): RO fill loop with overrun latch and heater freeze protection`

---

### Task 9: The core: one `tick`, one `submit`, self-test and capabilities data

**Files:**
- Create: `firmware/vending/lib/vendcore/core.h/.cpp`:
  ```cpp
  struct CoreConfig { Polarity polarity; uint32_t silence_ms = 30000; uint32_t ro_max_run_ms = 5400000; float heater_on_c = 3.0f, heater_off_c = 6.0f; uint32_t io_republish_ms = 60000; uint32_t temp_publish_ms = 10000; };
  class Core { public: Core(const CoreConfig&, Clock&, IoPort&, Publisher&);
    void tick();                                      // one 10 ms step: read, scan, interlocks, dispenser, loops, guard, write, kick
    Ack submit(const Decoded& d);                     // first ack for any command; never blocks
    bool poll_completion(Ack& out);                   // water_valve "completed" acks, one per call
    void vmc_seen(); void vmc_online(bool);
    void force_report();                              // republish every input, output and the temperature next tick
    void self_test(Ack& out) const;                   // result_json {"checks":[{name,pass,detail}...]}
    const Health& health() const; uint32_t dropped_events() const; };
  ```
  `submit` semantics: `Ping` → `Ok/Completed`; `SelfTest` → `Ok/Completed` with checks; `ForceReport` → `Ok/Completed` and schedules the republish; `Dispense`/`WaterValve` while the dispenser is `Running` or a valve test is open → `Rejected "busy"`; while `VmcLiveness::silent()` → `Rejected "vmc_offline"`; otherwise the command is **staged** and `submit` returns an `Accepted` ack only when `first_actuator_on()` became true (the core runs `tick` once internally after staging to try; if the actuator could not energize this tick, the caller re-polls via `poll_completion`, which returns the `Accepted` ack once it does, or a `Failed` ack with the trip detail). `water_valve` is a fixed-duration valve run handled by the core itself, **not** the `Dispenser`: `OutputGuard::request(Out::WaterValve, true, seconds × 1000 + 500)`, `Accepted` once the valve output is on, `Completed` through `poll_completion` when the core turns it off at `seconds`; it emits `OutputChanged` only, never a `Step` or `Outcome` (no `hardware/dispenser` traffic for a valve test). Self-test checks: `i2c_expanders` (last `IoPort::read` ok), `ambient_sensor` (`ambient_valid`), `watchdog_kick` (`Health::ok()`), `vmc_seen` (not silent), `master_enable` (readback true), `dropped_events` (0).
- Test: `test_core/test_main.cpp` — the only test dir that exercises every module together.

**Interfaces:**
- Consumes: everything from Tasks 2–8.
- Produces: `Core`, `CoreConfig` — the whole surface Tasks 10–11 wrap.

- [ ] **Step 1: Write the tests** — a decoded ice `dispense` yields `Accepted` only after the catch output is on (check `FakeIo`'s recorded write); the full ice run emits `Step` ×3 then one `Outcome Complete` with the `request_id`; a second dispense during the run is `Rejected "busy"`; with no `vmc_seen` for 30 s every actuator command is `Rejected "vmc_offline"` and a running dispense ends `Error "vmc_offline"` with all outputs off; `water_valve 3` → `Accepted`, then after 3000 ms `poll_completion` yields `Completed`; `sump_high` mid-run → `Error "sump_high"`; `IoPort::read` returning false → no kick that tick and `Health::ok()` false; `force_report` republishes 9 inputs + 7 outputs + temperature on the next tick; `io_republish_ms` elapsing republishes all inputs; `self_test` result lists the six checks; the kick count equals the number of healthy ticks.
- [ ] **Step 2–4:** fail, implement, pass. `pio run -e kc868-a8` still builds (vendcore compiles under the Xtensa toolchain too).
- [ ] **Step 5: Commit** — `feat(firmware): vendcore Core — tick, submit, completions, self-test`

---

### Task 10: ESP-IDF sequencer task and the KC868-A8 I/O port

No host tests: this task is the thin hardware port. Verification is `pio run -e kc868-a8` building with `-Werror`, plus the bench checklist in Task 13.

**Files:**
- Create: `firmware/vending/boards/kc868-a8.h` — `constexpr` pin map: I²C SDA 4 / SCL 5, relay expander `0x24`, input expander `0x22`, `kGpioServiceDoor = 13`, `kGpioOneWire = 14`, `kGpioFlowPulse = 32`, `kGpioWatchdogKick = 33`, `kAdcMasterEnable = 34` (digital threshold 1.5 V), relay bit for each `Out`, input bit for each `In` 0–7, default `Polarity` (buttons active-high NO; `bag_full_sensor` active-high; W6/W7/W9/C6 per the I/O list §8 once surveyed — default `active_low = true` so an open circuit reads asserted/faulted). A comment per pin names the I/O list tag and says "verify against KC868-A8-schematic.pdf".
- Create: `firmware/vending/src/kc868_io.h/.cpp` — `class Kc868Io : public vendcore::IoPort` using `driver/i2c_master.h` (two PCF8574 devices, 100 kHz), `driver/pulse_cnt.h` on GPIO 32 (glitch filter 1 µs, 32-bit accumulation across overflow), `onewire_bus` + `ds18b20` on GPIO 14 read every 10 s from a low-priority helper (never inside the 10 ms tick; the tick reads the last value), GPIO 13 input with pull-up, GPIO 33 kick as a 50 µs pulse, ADC1 ch6 (GPIO 34) readback. `read()` returns false when either expander transaction fails.
- Create: `firmware/vending/src/esp_clock.h` — `esp_timer_get_time() / 1000`.
- Create: `firmware/vending/src/settings.h/.cpp` — NVS namespace `vend`: `wifi_ssid`, `wifi_pass`, `mqtt_host`, `mqtt_port`, `mqtt_user`, `mqtt_pass`, `machine_id`, `use_eth` (bool), `polarity` (9-byte blob). Compile-time defaults from `-D` flags in `platformio.ini` (`VEND_DEFAULT_MQTT_HOST` etc., documented, empty by default).
- Create: `firmware/vending/src/console.h/.cpp` — a USB-serial line console (`esp_console`) with `set <key> <value>`, `show`, `erase`, `reboot`; secrets print masked.
- Create: `firmware/vending/src/sequencer_task.h/.cpp` — FreeRTOS task on core 1, priority 20, `vTaskDelayUntil` 10 ms, registered with the task WDT; constructs `Core` with `Kc868Io`, `EspClock`, and a `Publisher` that pushes into `evt_q` (Task 11 supplies the queue type; this task defines `struct QueuePublisher : Publisher` over a `QueueHandle_t` passed in). Drains `cmd_q` each tick: `Decoded` items → `Core::submit`, the resulting `Ack` → `ack_q`; `VmcSeen`/`VmcOnline`/`ForceReport` control items → the matching `Core` calls; `poll_completion` results → `ack_q`.
- Create: `firmware/vending/idf_component.yml` — `espressif/onewire_bus: "^1.0"`, `espressif/ds18b20: "^0.1"`.
- Modify: `firmware/vending/src/main.cpp` — NVS init, settings load, console start, sequencer start; comms start is added in Task 11.

**Interfaces:**
- Consumes: `Core`, `IoPort`, `Clock`, `Publisher`, `Decoded`, `Ack` (Tasks 2–9).
- Produces: `cmd_q` item type `struct CmdItem { enum { Decoded, VmcSeen, VmcOnline, ForceReport } kind; vendcore::Decoded decoded; bool online; }`, `evt_q` of `vendcore::Event`, `ack_q` of `vendcore::Ack`; `Settings` struct; `start_sequencer(QueueHandle_t cmd_q, QueueHandle_t evt_q, QueueHandle_t ack_q, const Settings&)`.

- [ ] **Step 1: Write the files.** Keep each under ~200 lines; the port does no decision-making.
- [ ] **Step 2: `pio run -e kc868-a8`** builds clean with `-Werror`; `pio test -e native` still passes (nothing in `lib/` changed).
- [ ] **Step 3: Flash if a board is on hand** (`pio run -e kc868-a8 -t upload -t monitor`): the console answers `show`; with no network configured the sequencer logs one `vmc_offline` line and the relays stay off. Record the result (or "no board available") in the commit body.
- [ ] **Step 4: Commit** — `feat(firmware): ESP-IDF sequencer task and KC868-A8 I/O port`

---

### Task 11: Wire serializers and the comms task

The JSON the board emits is host-testable and must validate against the Python contract models; the network plumbing is not and stays thin.

**Files:**
- Create: `firmware/vending/lib/vendcore/wire.h/.cpp`:
  ```cpp
  struct WireCtx { const char* machine_id; const char* hardware_id; const char* ip; const char* firmware; };
  size_t topic_for(const Event& e, const WireCtx&, char* out, size_t cap);      // "vmc/<id>/hardware/io/<ch>" etc.
  size_t payload_for(const Event& e, const WireCtx&, uint32_t uptime_s, char* out, size_t cap);
  size_t capabilities_json(const WireCtx&, char* out, size_t cap);              // SubsystemCapabilities, 17 channels, 5 commands
  size_t heartbeat_json(uint32_t uptime_s, char* out, size_t cap);              // {"subsystem":"vending","uptime_seconds":N}
  size_t lwt_json(char* out, size_t cap);                                       // uptime_seconds -1
  size_t ack_json(const Ack&, char* out, size_t cap);
  int ha_discovery_count(); size_t ha_discovery(int i, const WireCtx&, char* topic, size_t tcap, char* payload, size_t pcap);  // one per channel, same shape as ice_sims base._publish_ha_discovery
  ```
  Event → topic/payload: `InputChanged`/`OutputChanged` → `hardware/io/<channel>` `HardwareIO{device,state}`; `ButtonPressed` → `hardware/buttons` `ButtonPress{button:"<id>",action:"pressed"}`; `Step`/`Outcome` → `hardware/dispenser` `DispenserStatus{slot,state,request_id,detail}`; `Telemetry` → `telemetry/vending/fill_pulses` `{"channel_id":"fill_pulses","value":N}`; `Temperature` → `sensors/temp/ambient` `SensorReading{location:"ambient",value,unit:"C"}`. Every payload carries a `timestamp` only when the board has NTP time; otherwise the key is omitted (the Pydantic default fills it).
- Create: `firmware/vending/test/test_wire/test_main.cpp` — also **writes** `firmware/vending/test/golden/<name>.json` for: capabilities, heartbeat, lwt, each ack status/phase, one of each event payload, one HA discovery payload. Golden files are committed; Task 12's `tests/firmware/test_golden_wire.py` validates them with the Pydantic models.
- Create: `firmware/vending/src/net.h/.cpp` — Wi-Fi STA or LAN8720 Ethernet per `Settings.use_eth`, reconnect loop, SNTP.
- Create: `firmware/vending/src/mqtt_publisher.h/.cpp` — `esp-mqtt` client: LWT on `vmc/<id>/heartbeat/vending` with `lwt_json`, QoS 1 everywhere, subscriptions `vmc/<id>/cmd/vending` and `vmc/<id>/online`; on connect publishes capabilities (retained), heartbeat, every HA discovery (retained), then pushes `ForceReport` into `cmd_q`.
- Create: `firmware/vending/src/comms_task.h/.cpp` — FreeRTOS task on core 0: 10 s heartbeat timer; drains `evt_q` → `topic_for`/`payload_for` → publish; drains `ack_q` → `AckCache::put` + publish on `cmd/vending/ack`; inbound `cmd/vending` → `decode_command` → on `NoRequestId` drop and count; cache hit → republish cached ack; else push `CmdItem{Decoded}` to `cmd_q` **and** push `VmcSeen`; inbound `online` → parse `{"online":bool}` → `VmcOnline`. Outbound publishes while disconnected are held in a 64-entry ring that drops `Telemetry` first and never an `Outcome`.
- Modify: `firmware/vending/src/main.cpp` — start comms after the sequencer.

**Interfaces:**
- Consumes: `Event`, `Ack`, `AckCache`, `decode_command`, `kChannels`, `kContractVersion`, queues and `CmdItem` (Task 10).
- Produces: the golden JSON files; a complete, flashable firmware.

- [ ] **Step 1: Write the wire tests** — `topic_for` for each event type is the exact contract topic; `capabilities_json` has 17 channels in table order with the right `direction`/`driven_by`/`kind`/`unit`/`interval_seconds`, `contract_version` `"2.0.0"`, `subsystem` `"vending"`, the 5 commands; `heartbeat_json`/`lwt_json` exact; `ack_json` emits `phase` always and omits `detail`/`result` when empty; `ButtonPressed` payload's `button` is the id string; golden files are written.
- [ ] **Step 2–4:** fail, implement, pass; `pio run -e kc868-a8` builds.
- [ ] **Step 5: Flash if a board is on hand** against the LAN broker: capabilities retained, heartbeat every 10 s, `ping` acked. Record the result in the commit body.
- [ ] **Step 6: Commit** — `feat(firmware): contract wire serializers, MQTT comms task and golden JSON`

---

### Task 12: Conformance suite `tests/firmware/`

Opt-in like the browser tests: skipped unless `ICE_COLDER_BOARD_TESTS=1`, and the skip reason is deliberately **not** added to `tests/skip_policy.py`'s legitimate list, so CI (which sets the flag) fails if the suite skips.

**Files:**
- Create: `tests/firmware/conftest.py`:
  - Skip the whole directory with reason `"board conformance tests are opt-in (set ICE_COLDER_BOARD_TESTS=1)"` unless the flag is `"1"`.
  - Target selection: `ICE_COLDER_BOARD_BROKER` set → hardware target (`host[:port]`, `ICE_COLDER_BOARD_MACHINE_ID` required); unset → **simulator target**: start `ice_sims.vending.simulator.VendingMachineSimulator` if that module exists, else `ice_sims.vending_machine.VendingMachineSimulator`, in-process against `localhost:1883` (the broker the e2e tests already use; `MQTT_USERNAME`/`MQTT_PASSWORD` honoured as in `tests/integration/test_integration_e2e.py`) with a fresh `machine_id`, stopped at session end.
  - Fixtures: `board` (a `BoardClient`, below), `machine_id`, `is_sim: bool`, marker `requires_sim` (skips on hardware with reason `"needs the simulator to drive sensors"` — the one additional legitimate skip; add it to `LEGITIMATE_SKIP_REASONS` as `board_sensor_drive_requires_sim`, and accept it in CI since CI runs the simulator target where it never fires anyway).
- Create: `tests/firmware/board_client.py`:
  ```python
  class BoardClient:
      async def retained(self, suffix: str, timeout: float = 5) -> dict
      async def wait_for(self, suffix: str, predicate, timeout: float) -> dict
      async def send(self, command: str, params: dict | None = None, request_id: str | None = None) -> str
      async def acks(self, request_id: str, count: int, timeout: float) -> list[CommandAck]
      async def collect(self, suffix: str, seconds: float) -> list[dict]
  ```
- Create the test modules (each a handful of tests):
  - `test_golden_wire.py` — **always runs, no flag** (pure file validation): every `firmware/vending/test/golden/*.json` validates against its Pydantic model (`SubsystemCapabilities`, `SubsystemHeartbeat`, `CommandAck`, `HardwareIO`, `ButtonPress`, `DispenserStatus`, `SensorReading`); the capabilities golden lists exactly the 17 ids; `ButtonPress` golden is marked `xfail(strict=True, reason="2.0.0 ButtonPress.button is int until IM600XL Task 3")`.
  - `test_capabilities.py` — retained doc validates; `subsystem == "vending"`; commands ⊇ the five; channel ids == the 17 (`xfail(strict=True)` against the simulator until IM600XL Task 10; a `BOARD_IS_2_0_0 = not is_sim or "ro_tank_empty" in ids` helper decides at runtime so the mark lifts itself when the simulator catches up).
  - `test_liveness.py` — two heartbeats ≤ 12 s apart; `uptime_seconds` increases; on the simulator target, stopping its client makes the LWT `-1` arrive (`requires_sim`).
  - `test_standard_commands.py` — `ping` one `ok/completed`; `self_test` result has a non-empty `checks` list each with `name`, `pass`, `detail`; `force_report` is acked and a `capabilities/vending` and ≥ 1 `hardware/io/+` message follow within 5 s.
  - `test_idempotency.py` — same `request_id` twice for `ping` → two identical acks and (`requires_sim`) no second handler run; a duplicate `water_valve` after completion replays `completed`, not `accepted`.
  - `test_water_valve.py` — `seconds=2` → `accepted` then `completed` within 7 s; `seconds=0` and `11` → `rejected` with a detail mentioning `seconds`.
  - `test_dispense.py` — the catch profile fixture from `firmware/vending/test/fixtures/profile_ice_catch.json`: `accepted`, then `hardware/dispenser` steps, then one terminal report carrying the `request_id` within 120 s (`requires_sim` for the sensor-driven `complete`; on hardware only the `accepted` ack and the first `agitate` step are asserted); a second dispense during the first → `rejected` with `"busy"`; `ReleaseByCatch` is `xfail(strict=True)` against the simulator until IM600XL Task 2.
  - `test_rejections.py` — unknown command → `unsupported`; missing `request_id` → no ack within 3 s and no error; `dispense` missing `profile` → `rejected`.
- Modify: `tests/skip_policy.py` — docstring paragraph for the board suite mirroring the browser-test paragraph; the one new entry above.
- Modify: `.github/workflows/ci.yml` — `ICE_COLDER_BOARD_TESTS: "1"` in the test job's env (the broker step already runs before it).
- Modify: `tests/private_access_budget.py` only if a new file needs a budget (it should not; target 0).

**Interfaces:**
- Consumes: the golden files (Task 11), the simulator, the contract models.

- [ ] **Step 1: Write the suite.** Run `uv run pytest tests/firmware -q` without the flag: everything but `test_golden_wire.py` skips with the opt-in reason. Run with `ICE_COLDER_BOARD_TESTS=1` and a local mosquitto (`docker compose -f docker/docker-compose.yml up -d mosquitto` through the `hpz440` context, or any broker on `localhost:1883`): the simulator target passes, the 2.0.0-only checks xfail.
- [ ] **Step 2: Run the skip guard** the way CI does: `ICE_COLDER_REQUIRE_BROKER=1 uv run python tests/run_skip_guard.py skip-report.json` after a flagged run — green.
- [ ] **Step 3: `uv run pytest`** whole suite green; `ruff check --fix .`; `ruff format .`.
- [ ] **Step 4: Commit** — `test(firmware): opt-in board conformance suite and golden wire validation`

---

### Task 13: Interface document, bench checklist and repo docs

**Files:**
- Create: `firmware/docs/vending-board-interface.md` — sections: reference board and substitution requirements (§4.1); the channel map table with board point, I/O list tag, wire colour column left blank for the Phase A survey (§4.2); the master-enable circuit (§4.3) with a one-paragraph description and a parts list (watchdog timer IC or off-delay timer, interposing relay, 24 V coil wiring through its NO contact), the 2 s window and 500 ms kick; isolation and power (§4.4); input polarity table and how to set it from the console; provisioning (console commands from Task 10); the **bench checklist** (§8): a switch per input, lamp per relay, pulse generator on GPIO 32, the reset-hold watchdog test with the ≤ 2 s expectation, one `dispense` with the catch fixture, one `water_valve 3`, LWT on power-off. Every "assumed" item links to the I/O list §8 open item.
- Modify: `firmware/README.md` — replace "ESPHome configs will live here. None exist yet." with the layout, `pio run`/`pio test`/upload commands, the console, and links to the interface doc and spec.
- Modify: `CLAUDE.md` — a `firmware/` paragraph under Layout (project, `vendcore` rule, CI job, conformance suite env vars and the skip rule), and two Commands rows (`pio run -e kc868-a8`, `pio test -e native` from `firmware/vending/`).
- Modify: `ROADMAP.md` §9 Phase D — "in progress: `docs/superpowers/specs/2026-10-17-vending-esp32-firmware-design.md`"; Phase E bullet points at the bench checklist.
- Modify: `docs/hardware/im600xl-io-list.md` §2 intro — one sentence pointing to the interface doc for board points.

- [ ] **Step 1: Write the docs.** `uv run pytest tests/ui tests/meta -q` (docs touch nothing tested, but CLAUDE.md edits sometimes trip the CSS extractor if a `class="..."` appears in a comment — confirm green).
- [ ] **Step 2: Commit** — `docs(firmware): vending board interface spec, bench checklist, README and repo docs`

---

## Self-review

- **Spec coverage:** §3 layout → Tasks 1, 13; §4.1–4.4 → Tasks 10, 13; §5.1 Dispenser → 5–6, Interlocks/OutputGuard → 7, RoLoop/FreezeProtect → 8; §5.2 comms → 11; §5.3 queues → 10–11; §5.4 ack phases → 9, 11; §6 topics, fail-safe, NVS → 9–11; §7 error table → 3 (no-id drop, unsupported, rejected), 9 (busy, vmc_offline, trips), 11 (ring buffer on broker loss); §8 host tests → 2–9, conformance → 12, bench → 13; §9 docs → 13; §10 open items → pin-map comments and interface doc.
- **Known deferral:** the `xfail(strict=True)` marks in Task 12 come off in the IM600XL plan's Tasks 2, 3 and 10; each mark's `reason` names that task so the removal is found by grep.
- **Type consistency:** `Decoded`/`Ack`/`Event` are defined once (Tasks 2–3) and used by name in 9–11; `Dispenser::tick` takes `OutputGuard&` from Task 7 on (Task 5–6 tests migrate in Task 7, stated there).
