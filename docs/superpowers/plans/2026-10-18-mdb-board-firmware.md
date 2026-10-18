# MDB Payment Board Firmware Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship the ESP32-S3 MDB payment board firmware, the shared `firmware/common` platform it is built on, contract 1.1.0 (`cash_sale`), the fault-tolerant `scripts/flash_board.py`, the interface document, and the host, golden and conformance test suites, per `docs/superpowers/specs/2026-10-18-mdb-board-firmware-design.md`.

**Architecture:** Three FreeRTOS tasks (bus, ledger, comms) over a pure-C++ `lib/mdbcore` whose bus, clock, ids and log store are ports, so the whole MDB master runs on the host against byte-level fake peripherals. The chip-agnostic platform is lifted out of the vending firmware into `firmware/common/` (`boardcore` PlatformIO library, `boardidf` ESP-IDF component) and the vending project is re-pointed at it with its tests and golden files as the regression net.

**Tech Stack:** C++17 on ESP-IDF via PlatformIO (`espressif32@^6.9`), Unity host tests (`platform = native`), ArduinoJson 7.4.3 for decoding, hand-built JSON for encoding; Python 3.12 / pydantic / pytest for the contract, the tool and the conformance suite.

**Plan style:** each task states its files, interfaces and tests. Implementers write the bodies. Exact values below are binding; everything else is the implementer's call within the spec.

## Global Constraints

- **Contract:** `ice_contracts.board.payment` **1.1.0**; `capabilities/mdb.contract_version = "1.1.0"`; payload rules of `docs/contracts/payment/CONTRACT.md` verbatim. Every amount is an integer number of cents; no float ever touches money, in Python or C++.
- **Topics** under `vmc/<machine_id>/`: `payment/event` (QoS 1), `payment/event/ack` (subscribed), `payment/state` (QoS 1, retained), `cmd/mdb` (subscribed), `cmd/mdb/ack`, `heartbeat/mdb` (every 10 s, LWT `uptime_seconds: -1`), `capabilities/mdb` (retained).
- **Capabilities commands** (exact list): `ping, self_test, force_report, set_policy, payout, vend_request, vend_result, end_session, return_escrow, device_reset, cash_sale`; `channels: []`.
- **Ids:** `boot_id` random 8–32 chars (use 16 lowercase hex); `seq` starts at 1 per boot; `event_id = "<boot_id>-<seq>"`.
- **Bus timing:** 9600 baud, 8 data + mode bit + 1 stop; wait at most **5 ms** for the first response byte; a block ends on the mode-bit checksum byte **or** a **1 ms** idle gap; checksum is the 8-bit sum of address and data bytes; ACK `0x00`, RET `0xAA`, NAK `0xFF`; one retry per command; **10** consecutive misses = `offline`.
- **Replay log:** NVS partition `evlog`, 128 KB, capacity **256** envelopes; append before publish; erase only entries with the acked `boot_id` and `seq <=` acked; replay oldest boot first.
- **Ack phases:** `payout` and `vend_request` ack `accepted` only after the peripheral ACKed the MDB command; `set_policy` acks `ok` after enables are applied on the bus; `ping`/`self_test`/`force_report` ack `completed` from comms.
- **Edge silence:** measured from the last `cmd/mdb` message only; sequence per contract §3.2; `edge_timeout_s` default 30.
- **No heap after boot** in firmware; fixed-size queues; a full `evt_q` defers polling, never drops a money event.
- **Shared platform rule:** platform code lives in `firmware/common/`, never copied into a project. The vending firmware's behaviour, host tests and `firmware/vending/test/golden/*` stay byte-identical through Tasks 2–3 (`git diff --exit-code -- firmware/vending/test/golden`).
- **Toolchain (this Windows machine):** `pio` is at `C:\Users\Conrad\.local\bin\pio` (on PATH). Host g++ is NOT on PATH: before `pio test -e native` run (Git Bash) `export PATH="$PATH:/c/Users/Conrad/AppData/Local/Microsoft/WinGet/Packages/BrechtSanders.WinLibs.POSIX.UCRT_Microsoft.Winget.Source_8wekyb3d8bbwe/mingw64/bin"`. The first `pio run -e esp32-s3-devkitc-1` downloads the S3 toolchain: allow 600000 ms. Always `uv run ruff`, never the global ruff.
- **Repo rules:** work from the worktree root `D:\Users\Conrad\Documents\programming\ice-colder\.claude\worktrees\mdb-firmware`; never `cd` out of it; no `&&`-chained shell commands (separate calls); no bare `git stash`. Commit each task; every commit message ends with the trailer line `Assisted-by: <model> (Claude Code)` naming the model that wrote it, no `Co-Authored-By`. Python: `uv run pytest`, `uv run ruff check --fix .`, `uv run ruff format .`, `uv run lint-imports`, `uv run python scripts/check_package_isolation.py`. The generator (`uv run python -m ice_contracts.generate`) writes CRLF into unrelated contract dirs on Windows: restore them with `git checkout -- docs/contracts/vending-machine docs/contracts/ice-maker-monitor` before committing.

## MDB reference (ICP 4.3; the fakes and the firmware must agree, the bench validates against real devices)

Command byte = device address | command. Master's checksum byte has mode bit 0; a peripheral's final checksum byte has mode bit 1; a lone ACK/NAK from a peripheral has mode bit 1.

| Device | Address | Commands |
|---|---|---|
| Coin changer | `0x08` | RESET `0x08`, SETUP `0x09`, TUBE STATUS `0x0A`, POLL `0x0B`, COIN TYPE `0x0C` (2 bytes enable mask, 2 bytes manual-dispense mask), DISPENSE `0x0D` (1 byte: count<<4 \| coin type), EXPANSION `0x0F` + sub: `0x00` IDENTIFICATION (manufacturer 3, serial 12, model 12, software 2, optional features 4 bytes), `0x01` FEATURE ENABLE (4 bytes; bit 0 alternative payout), `0x02` PAYOUT (1 byte value in scaled units), `0x03` PAYOUT STATUS (16 bytes coins paid per type, or ACK while busy), `0x04` PAYOUT VALUE POLL (1 byte paid so far) |
| Changer SETUP response | | level (1), country code (2, `0x18 0x40` = USD), scaling factor (1), decimal places (1), coin type routing (2: bit set = coin routed to tubes), coin type credit (16: value per type in scaled units, `0x00` = unused, `0xFF` = token) |
| Changer TUBE STATUS response | | tube full status (2), tube status (16: coin count per type) |
| Changer POLL bytes | | coin deposited `01rrtttt` + count byte (rr: 00 cashbox, 01 tubes, 10 unused, 11 reject); slug `0010xxxx`; status `0x01` escrow request, `0x02` payout busy, `0x03` no credit, `0x04` defective tube sensor, `0x05` double arrival, `0x06` acceptor unplugged, `0x07` tube jam, `0x08` ROM checksum error, `0x09` coin routing error, `0x0A` changer busy, `0x0B` changer was reset, `0x0C` coin jam, `0x0D` possible credited coin removal |
| Bill validator | `0x30` | RESET `0x30`, SETUP `0x31`, SECURITY `0x32`, POLL `0x33`, BILL TYPE `0x34` (2 bytes enable, 2 bytes escrow enable), ESCROW `0x35` (1 byte: `0x01` stack, `0x00` return), STACKER `0x36`, EXPANSION `0x37` + `0x00` IDENTIFICATION (level 1 form, 29 bytes) |
| Validator SETUP response | | level (1), country code (2), scaling factor (2), decimal places (1), stacker capacity (2), security levels (2), escrow (1: `0xFF` capable), bill type credit (16) |
| Validator STACKER response | | 2 bytes: bit 15 full, bits 0–14 count |
| Validator POLL bytes | | bill `1yyyxxxx` (yyy: 000 stacked, 001 escrow position, 010 returned, 011 to recycler, 100 disabled rejected, 101 to recycler manual fill, 110 manual dispense, 111 transferred to stacker; xxxx bill type); status `0x01` defective motor, `0x02` sensor problem, `0x03` validator busy, `0x04` ROM checksum error, `0x05` validator jammed, `0x06` just reset, `0x07` bill removed, `0x08` cash box out of position, `0x09` unit disabled, `0x0A` invalid escrow request, `0x0B` bill rejected, `0x0C` possible credited bill removal; `0x2x` = x attempts to insert while disabled |
| Cashless reader | `0x10` | RESET `0x10`, SETUP `0x11` (sub `0x00` config: VMC level, columns, rows, display info; sub `0x01` max/min prices), POLL `0x12`, VEND `0x13` (sub `0x00` request: price 2, item 2; `0x01` cancel; `0x02` success: item 2; `0x03` failure; `0x04` session complete; `0x05` cash sale: price 2, item 2), READER `0x14` (`0x00` disable, `0x01` enable, `0x02` cancel), REVALUE `0x15` (unused), EXPANSION `0x17` (`0x00` request id: manufacturer 3, serial 12, model 12, software 2; `0x04` feature enable, bit 1 always idle, level 3 readers only) |
| Reader POLL bytes | | `0x00` just reset, `0x01` reader config data (level, country 2, scale, decimal, max response time s, misc options), `0x02` display request, `0x03` begin session (funds 2: `0xFFFF` = undisclosed), `0x04` session cancel request, `0x05` vend approved (amount 2), `0x06` vend denied, `0x07` end session, `0x08` cancelled, `0x09` peripheral id, `0x0A` malfunction/error (code), `0x0B` command out of sequence, `0x0D`–`0x0F` revalue (unused) |

Status byte → `DeviceStatus` mapping (binding): coin jam / tube jam / validator jammed → `jammed`; defective sensor, ROM error, routing error, defective motor, sensor problem, malfunction, possible credited removal → `error`; acceptor unplugged / cash box out / no response → `offline` (cash box out additionally arms `cash_collected{bill}` when it comes back); stacker full → `full`; tubes empty for every enabled coin → `empty`; busy codes → `busy`; disabled by policy → `disabled`; otherwise `ready`. `tilt` is reserved (no MDB peripheral here reports it).

---

### Task 1: Contract 1.1.0 — `cash_sale`

**Model:** Sonnet. **Files:**
- Modify: `packages/contracts/src/ice_contracts/board/payment.py`, `packages/contracts/src/ice_contracts/board/common.py`, `packages/contracts/src/ice_contracts/generate.py`
- Modify: `docs/contracts/payment/CONTRACT.md`; Create (generated): `docs/contracts/payment/schemas/cash_sale_params.schema.json`
- Test: `tests/contracts/test_payment_contract.py`

**Interfaces:**
- `payment.CONTRACT_VERSION = "1.1.0"`.
- `class CashSaleParams(BaseModel): cents: PositiveCents; sale_id: Optional[str] = None`.
- `"cash_sale"` appended to `PAYMENT_COMMANDS` and `PARAM_MODELS["cash_sale"] = CashSaleParams`; `MDB_TESTABLE_COMMANDS` unchanged.
- `common.COMMAND_PARAM_VALIDATORS["cash_sale"] = _payment_params_validator("cash_sale")`; no completion timeout (immediate command).
- `generate.PAYMENT_MODELS["cash_sale_params"] = CashSaleParams`.
- `CONTRACT.md`: title and identity section say 1.1.0 with a "1.1.0 (additive)" changelog paragraph; a `cash_sale` row in the Commands table (ack only; board lowers `escrow_cents` by `cents` clamped at zero, ack `ok` with `detail: "clamped"` when it clamps; sends MDB CASH SALE to the reader when present); the completion table lists `cash_sale` as immediate; flow 1 gains "edge sends `cash_sale{price, sale_id}` once the dispense completes"; the topic-map intro notes the board stays source-agnostic (the edge may also hold virtual-gateway credit).

**Tests** (extend the existing classes): version pin `"1.1.0"`; `CashSaleParams` accepts `{"cents": 250}` with `sale_id is None`, rejects `cents: 0`, `cents: 2.5`, `cents: "250"`; `PAYMENT_COMMANDS` contains `cash_sale` and every entry has a validator in `COMMAND_PARAM_VALIDATORS`; the validator message for a float reads `cash_sale params invalid: ...`; `test_generate_registers_payment_contract` covers `cash_sale_params`; the schema drift test (`tests/contracts/test_contract_schemas.py`) passes after regeneration. Grep every test for the literal `"1.0.0"` pin of the payment contract and update it.

- [ ] Write failing tests, run them, implement, regenerate schemas (restore CRLF noise per Global Constraints), run `uv run pytest tests/contracts`, ruff, commit `feat(contracts): payment contract 1.1.0 adds cash_sale`.

---

### Task 2: `firmware/common/boardcore` lifted from vendcore

**Model:** Sonnet. **Files:**
- Create: `firmware/common/boardcore/library.json` (`{"name": "boardcore", "version": "0.1.0"}`), `firmware/common/boardcore/src/ack_cache.{h,cpp}`, `outbound_ring.{h,cpp}`, `json_writer.h` (the `W` appender from `wire.cpp`, made public as `boardcore::JsonWriter` with the same methods: `raw`, `ch`, `str`, `key`, `field`, `num`, `flt`, `boolean`, `done`), `board_wire.{h,cpp}`
- Modify: `firmware/vending/platformio.ini` (`[env] lib_extra_dirs = ../common`), `firmware/vending/lib/vendcore/{ack_cache,outbound_ring}.{h,cpp}` (deleted), `wire.{h,cpp}` (use boardcore), `library.json` (depends on boardcore)
- Tests: `firmware/vending/test/test_ack_cache`, `test_outbound_ring` unchanged in content, now compiling against boardcore

**Interfaces (namespace `boardcore`):**
- `AckStatus`, `Phase`, `Ack`, `AckCache` exactly as today (moved); `Prio`, `RingEntry`, `OutboundRing` exactly as today (moved).
- `struct WireCtx { machine_id, hardware_id, ip, firmware, iso_time }` (moved).
- `size_t topic_join(const WireCtx&, const char* tail, char* out, size_t cap)`; `size_t heartbeat_json(const char* subsystem, uint32_t uptime_s, char*, size_t)`; `size_t lwt_json(const char* subsystem, char*, size_t)`; `size_t ack_json(const Ack&, char*, size_t)`.
- `vendcore` keeps its public names via `using boardcore::Ack; using boardcore::AckCache; ...` in `ack_cache.h`/`outbound_ring.h` shims (one-line headers) so `src/` and the tests do not change; `vendcore::heartbeat_json(uptime)` / `lwt_json()` become one-line wrappers passing `"vending"`.

**Acceptance:** `pio run -e kc868-a8` builds; `pio test -e native` passes with the same test count; `git diff --exit-code -- firmware/vending/test/golden` is clean (run `pio test -e native -f test_wire` first, then the diff).

- [ ] Move, shim, build, test, golden diff, commit `refactor(firmware): lift AckCache, OutboundRing and wire helpers into firmware/common/boardcore`.

---

### Task 3: `firmware/common/boardidf` ESP-IDF component

**Model:** Sonnet. **Files:**
- Create: `firmware/common/boardidf/CMakeLists.txt`, `include/boardidf/board_identity.h`, `include/boardidf/settings.h`, `include/boardidf/console.h`, `include/boardidf/net.h`, `include/boardidf/mqtt_publisher.h`, `src/settings.cpp`, `src/console.cpp`, `src/net.cpp`, `src/mqtt_publisher.cpp` (lifted from `firmware/vending/src/`)
- Modify: `firmware/vending/CMakeLists.txt` (`set(EXTRA_COMPONENT_DIRS "${CMAKE_CURRENT_LIST_DIR}/../common/boardidf")` before `project(...)`), `firmware/vending/src/CMakeLists.txt` (REQUIRES boardidf), `firmware/vending/src/{main,comms_task,sequencer_task}.cpp` (new include paths, identity), `firmware/vending/platformio.ini` (comment: `BOARD_DEFAULT_*` flags), delete the four lifted `src/` pairs
- Modify docs that name `VEND_DEFAULT_*`: `firmware/README.md`, `firmware/docs/vending-board-interface.md` (rename to `BOARD_DEFAULT_*`)

**Interfaces:**
- `struct BoardIdentity { const char* subsystem; const char* console_prompt; const char* nvs_namespace; bool ethernet_supported; }`; vending passes `{"vending", "vend> ", "vend", true}` so existing boards keep their stored settings.
- `struct Settings { wifi_ssid[33], wifi_pass[65], mqtt_host[65], mqtt_port, mqtt_user[33], mqtt_pass[65], machine_id[33], use_eth }` (the generic keys); compile-time defaults from `BOARD_DEFAULT_WIFI_SSID` etc.
- `struct ExtraSetting { const char* key; const char* help; bool (*set)(const char* value); void (*show)(); }`; `void settings_register_extra(const ExtraSetting* table, int n)`: vending registers `polarity` and keeps its 9-byte table in its own `src/polarity_setting.{h,cpp}` reading/writing NVS under the same namespace and key as today.
- `settings_defaults(Settings&)`, `settings_load(Settings&)`, `settings_set(key, value)`, `settings_erase()`, `settings_show(const Settings&)` as today, namespace from `BoardIdentity`.
- `console_start(const BoardIdentity&, const Settings&)`; `net_start(const BoardIdentity&, const Settings&)`, `net_up()`, `net_ip()`, `net_hardware_id()`, `net_iso_time()`; the Ethernet path compiles only under `#if CONFIG_ETH_USE_ESP32_EMAC` and runs only when `ethernet_supported && use_eth`; `mqtt_start(const BoardIdentity&, const Settings&, MqttMessageCb)` builds the LWT topic `vmc/<id>/heartbeat/<subsystem>`; the rest of the mqtt API unchanged.

**Acceptance:** vending builds, `show` prints the same keys, prompt `vend> `, host tests and golden unchanged. No host test covers boardidf (it includes IDF headers); the build is the test.

- [ ] Lift, parameterise, re-point vending, build, host tests, golden diff, commit `refactor(firmware): lift settings, console, net and mqtt into the firmware/common/boardidf component`.

---

### Task 4: `firmware/mdb` project skeleton, platform boot, capabilities

**Model:** Sonnet. **Files:**
- Create: `firmware/mdb/platformio.ini`, `partitions.csv`, `sdkconfig.defaults`, `CMakeLists.txt`, `boards/mdb-s3.h`, `src/CMakeLists.txt`, `src/idf_component.yml` (empty dependencies), `src/main.cpp`, `src/comms_task.{h,cpp}` (minimal: connect sequence, heartbeat, retained capabilities, LWT), `lib/mdbcore/library.json`, `lib/mdbcore/version.h`, `lib/mdbcore/ports.h`, `lib/mdbcore/wire.{h,cpp}` (capabilities only for now), `test/test_version/test_main.cpp`, `test/test_wire/test_main.cpp` (golden writer as in vending), `test/golden/{capabilities,heartbeat,lwt}.json`

**Interfaces:**
- `platformio.ini`: `[env] lib_deps = bblanchon/ArduinoJson@7.4.3`, `lib_extra_dirs = ../common`, `build_flags = -std=gnu++17 -Wall -Wextra -Werror -DARDUINOJSON_POOL_CAPACITY=32`, `test_framework = unity`; `[env:esp32-s3-devkitc-1]` `platform = espressif32@^6.9`, `board = esp32-s3-devkitc-1`, `framework = espidf`, `board_build.partitions = partitions.csv`, `monitor_speed = 115200`, `-DMDBCORE_TARGET_ESP32`; `[env:native]` `platform = native`, `-DMDBCORE_TARGET_NATIVE`, `test_build_src = false`.
- `partitions.csv`: `nvs` 24K, `phy_init` 4K, `evlog` (type data, subtype nvs) 128K, `factory` app 3M; `sdkconfig.defaults`: custom partition table pointing at it, 8 MB flash, `CONFIG_ESP_TASK_WDT_EN=y`, `CONFIG_ESP_TASK_WDT_TIMEOUT_S=5`, `CONFIG_ESP_TASK_WDT_PANIC=y`, `CONFIG_FREERTOS_HZ=1000`, the vending MQTT settings.
- `boards/mdb-s3.h`: `kMdbUart = UART_NUM_1`, `kMdbTxGpio = 17`, `kMdbRxGpio = 18`, `kStatusLedGpio = 48`.
- `mdbcore/version.h`: `kContractVersion = "1.1.0"`, `kFirmwareVersion = "0.1.0"`.
- `mdbcore/ports.h`: `struct Clock { virtual uint32_t now_ms() const = 0; virtual uint64_t now_us() const = 0; }`; `struct RxByte { uint8_t data; bool mode; uint64_t t_us; }`; `struct BusPort { virtual void write(const uint8_t* bytes, const bool* modes, uint8_t n) = 0; virtual bool read(RxByte& out, uint32_t timeout_us) = 0; virtual void flush_rx() = 0; }`; `struct IdSource { virtual void fill_hex(char* out, size_t n_chars) = 0; }`; `struct LogStore { static constexpr size_t kMaxJson = 512; virtual bool append(const char* json, size_t len) = 0; virtual int count() const = 0; virtual bool get(int index, char* out, size_t cap) const = 0; virtual void erase_first(int n) = 0; }` (entries are kept in append order; index 0 is the oldest).
- `mdbcore/wire.h`: `size_t capabilities_json(const boardcore::WireCtx&, char*, size_t)` emitting `subsystem "mdb"`, `firmware`, `contract_version`, `brand "ice-colder"`, `model "mdb-s3"`, `hardware_id`, `ip`, `channels: []`, `commands` (exact list from Global Constraints); `extern const char* const kCommands[11]`.
- `src/main.cpp`: NVS init, `BoardIdentity{"mdb", "mdb> ", "mdb", false}`, settings, console, `start_comms(...)`; comms publishes heartbeat every 10 s, retained capabilities on each connect, LWT per boardidf.

**Tests:** `test_version` pins both strings; `test_wire` writes the three golden files and asserts non-zero lengths and that `capabilities_json` contains `"contract_version":"1.1.0"` and `"channels":[]`.

**Acceptance:** `pio run -e esp32-s3-devkitc-1` builds (600000 ms timeout the first time); `pio test -e native` passes; the golden files are committed.

- [ ] Build the skeleton, host tests, target build, commit `feat(firmware): mdb board project skeleton on the shared platform`.

---

### Task 5: `mdbcore::Link` — 9-bit block layer

**Model:** Sonnet. **Files:**
- Create: `firmware/mdb/lib/mdbcore/link.{h,cpp}`, `firmware/mdb/test/support/fakes.h` (`FakeClock`, `FakeBus`, `FakePeripheral`, `FakeIds`, `FakeLogStore`), `firmware/mdb/test/test_link/test_main.cpp`

**Interfaces (namespace `mdbcore`):**
- `constexpr uint32_t kResponseTimeoutUs = 5000; constexpr uint32_t kInterByteGapUs = 1000; constexpr uint8_t kMaxBlock = 36; constexpr uint8_t kAck = 0x00, kRet = 0xAA, kNak = 0xFF; constexpr int kMissThreshold = 10;`
- `bool parity_even_for(uint8_t data, bool mode)`: pure helper returning which parity setting (true = even) makes the transmitted parity bit equal `mode`; used by the ESP-IDF port in Task 12 and tested here.
- `uint8_t checksum(const uint8_t* bytes, uint8_t n)`.
- `enum class Reply : uint8_t { Ack, Nak, Data, Timeout, Corrupt }`; `struct Response { Reply kind; uint8_t data[kMaxBlock]; uint8_t len; }`.
- `class Link { Link(BusPort&, Clock&); Response transact(uint8_t address, uint8_t cmd, const uint8_t* data, uint8_t n); void acknowledge(); int misses(uint8_t address) const; void clear_misses(uint8_t address); uint32_t mode_disagreements() const; uint32_t corrupt_blocks() const; }`.
- `transact` sends the block (address|cmd with mode bit, data, checksum), reads the response per the Global Constraints, replies RET once on a bad checksum and NAK on the second, retries the whole command once on timeout, increments the address's miss counter on a final `Timeout`, resets it on any valid reply; `Data` responses are checksum-stripped; the caller sends `acknowledge()` after consuming a `Data` block.
- `FakeBus : BusPort` routes a written block by its address byte to one of up to three `FakePeripheral` (`virtual Response respond(uint8_t cmd, const uint8_t* data, uint8_t n) = 0; virtual void on_master_byte(uint8_t) {}` for ACK/RET/NAK observation), records every written byte with its mode bit, and feeds the reply bytes back through `read()` with configurable per-byte timestamps (`gap_us`), a `drop_checksum_mode_bit` flag, a `corrupt_next` flag and a `silent_next` count.

**Tests:** framing of a RESET (one byte with mode bit, checksum without); `parity_even_for` truth table; ACK and NAK replies; a data block terminated by the mode bit; the same block with `drop_checksum_mode_bit` terminated by the idle gap, counted in `mode_disagreements`; corrupt block → RET, second corrupt → NAK and `Corrupt`; `silent_next = 1` → one retry succeeds and misses stay 0; `silent_next = 2` → `Timeout` and misses 1; 10 timeouts reach `kMissThreshold`; a valid reply clears misses; a block longer than 36 bytes is `Corrupt`.

- [ ] TDD, `pio test -e native -f test_link`, commit `feat(mdbcore): Link block layer with mode-bit and idle-gap terminators`.

---

### Task 6: `mdbcore::Changer`

**Model:** Sonnet. **Files:**
- Create: `firmware/mdb/lib/mdbcore/events.h` (shared by Tasks 6–11), `device_state.h`, `changer.{h,cpp}`, `firmware/mdb/test/support/fake_changer.h`, `firmware/mdb/test/test_changer/test_main.cpp`

**Interfaces:**
- `events.h`: `enum class EventKind : uint8_t { CoinAccepted, BillEscrowed, BillStacked, BillReturned, EscrowReturnPressed, PayoutResult, SessionBegun, VendApproved, VendDenied, SessionEnded, DeviceStatus, CashCollected }`; `enum class Device : uint8_t { Coin, Bill, Cashless }`; `enum class Status : uint8_t { Ready, Disabled, Busy, Jammed, Full, Empty, Tilt, Offline, Error }`; `enum class CoinRoute { Tubes, Cashbox }`; `enum class BillReturnReason { Policy, EdgeSilent, Customer, Edge, Timeout }`; `enum class PayoutStatus { Ok, Partial, Failed }`; `enum class SessionEndReason { Complete, Cancelled, Reader, Edge, Timeout }`; `struct PaymentEvent { EventKind kind; uint32_t cents, requested_cents, paid_cents; CoinRoute route; BillReturnReason bill_reason; PayoutStatus payout_status; SessionEndReason end_reason; Device device; Status status; int16_t mdb_status /* -1 = null */; bool funds_known; uint32_t funds_cents; char session_id[65]; char request_id[65]; char detail[64]; }`; `struct EventSink { virtual bool emit(const PaymentEvent&) = 0; }` (false = queue full; the device must not advance past a money event it could not emit).
- `device_state.h`: `struct TubeLevel { uint32_t cents; uint16_t count; bool full; }`; `struct DeviceState { bool present; Status status; int8_t level /* 0 = null */; char model[13]; char serial[13]; uint32_t denominations[16]; uint8_t n_denominations; uint32_t enabled[16]; uint8_t n_enabled; }`.
- `class Changer { Changer(Link&, Clock&, EventSink&); void step(); /* one bus transaction */ void set_enabled(const uint32_t* cents, uint8_t n, bool accept); void reset(); bool request_payout(uint32_t cents, const char* request_id); /* false if busy or offline */ bool payout_in_progress() const; const DeviceState& state() const; const TubeLevel* tubes(uint8_t& n) const; uint32_t payable_total_cents() const; bool exact_change(uint32_t threshold_cents) const; bool escrow_lever_pending(); /* consumed on read */ }`.
- Internal phases: `Inactive → Reset → Setup → Identify (level 3 only; enable alternative payout) → TubeStatus → CoinType → Polling`; TUBE STATUS re-read every 10 s while polling and after every payout; `offline` after `kMissThreshold` misses and back to `Reset`; payout: level 3 PAYOUT then PAYOUT STATUS/VALUE POLL until the changer stops reporting busy, level 2 greedy DISPENSE per coin type; terminal `PayoutResult{ok|partial|failed, paid_cents}` with `request_id`.
- `FakeChanger : FakePeripheral`: configurable level, scaling factor, coin values and routing, tube counts; scripted POLL responses (`deposit(type, route)`, `lever()`, `status(byte)`, `just_reset()`); records COIN TYPE masks and DISPENSE/PAYOUT requests; pays from tubes honestly (reports partial when short).

**Tests:** setup parse with scaling factor 5 and USD; deposit routed to tubes and to cashbox → `CoinAccepted{cents, route}`; a slug emits nothing; lever → `EscrowReturnPressed`; every status byte → the binding `Status`; `0x0B` changer was reset → back to Setup and a `DeviceStatus`; tubes to `TubeLevel`s and `payable_total_cents`; `exact_change(threshold)` both ways; level 3 payout exact, partial, failed; level 2 dispense fallback with greedy coin choice; `request_payout` refused while one runs; miss threshold → `Offline` status event then re-Reset; `set_enabled` sends the right COIN TYPE mask and `accept=false` sends zero.

- [ ] TDD, commit `feat(mdbcore): coin changer driver (level 2/3) with tube math and payout`.

---

### Task 7: `mdbcore::Validator`

**Model:** Sonnet. **Files:** Create `firmware/mdb/lib/mdbcore/validator.{h,cpp}`, `test/support/fake_validator.h`, `test/test_validator/test_main.cpp`.

**Interfaces:**
- `struct EscrowVerdict { bool stack; BillReturnReason reason; }`; `struct EscrowJudge { virtual EscrowVerdict judge(uint32_t bill_cents) = 0; }` (the Master implements it from the policy and accounts; the validator never knows the policy).
- `class Validator { Validator(Link&, Clock&, EventSink&, EscrowJudge&); void step(); void set_enabled(const uint32_t* cents, uint8_t n, bool accept); void reset(); bool return_escrow(BillReturnReason); /* false when no bill is held */ bool bill_in_escrow(uint32_t& cents) const; const DeviceState& state() const; bool stacker(uint16_t& count, bool& full) const; }`.
- Phases: `Inactive → Reset → Setup → Identify (level 2) → Stacker → BillType → Polling`; STACKER re-read every 10 s; escrow position poll → `BillEscrowed` then `judge()` → ESCROW stack or return → `BillStacked` or `BillReturned{reason}` when the follow-up poll confirms; `0x08` cash box out → `Status::Offline` with the raw byte, and the next poll without it → `CashCollected{Bill}` and `Ready`; stacker full → `Full`; `0x0C` possible credited bill removal → `Error`; `bill_returned` seen without our request → `BillReturned{Timeout}`.
- `FakeValidator : FakePeripheral`: level, scaling, bill values, stacker capacity and count; scripted `insert(type)` (goes to escrow), `status(byte)`, `cashbox_out()/in()`, honours ESCROW stack/return and reports the matching poll byte next.

**Tests:** setup parse (scaling 100, USD, escrow capable); insert → `BillEscrowed{cents}`; judge stack → `BillStacked`, stacker count +1; judge return → `BillReturned{Policy}`; `return_escrow(Customer)` → `BillReturned{Customer}`, false when none; cash box out and in → `Offline` then `CashCollected`; stacker full → `Full`; every status byte mapping; miss threshold → `Offline` and re-Reset; BILL TYPE mask from `set_enabled` with escrow bits set for every enabled bill; `accept=false` disables all.

- [ ] TDD, commit `feat(mdbcore): bill validator driver with escrow verdicts`.

---

### Task 8: `mdbcore::Cashless`

**Model:** Sonnet. **Files:** Create `firmware/mdb/lib/mdbcore/cashless.{h,cpp}`, `test/support/fake_reader.h`, `test/test_cashless/test_main.cpp`.

**Interfaces:**
- `class Cashless { Cashless(Link&, Clock&, EventSink&, IdSource&); void step(); void set_enabled(bool); void reset(); bool session_open(char* session_id_out, bool& funds_known, uint32_t& funds_cents) const; bool vend_request(uint32_t cents, uint16_t item, const char* request_id); /* false unless a session is open or always-idle is active */ bool vend_result(bool success, uint16_t item); bool end_session(); bool cash_sale(uint32_t cents, uint16_t item); void set_edge_timeout_ms(uint32_t); bool always_idle() const; const DeviceState& state() const; }`.
- Phases: `Inactive → Reset → SetupConfig → SetupPrices → RequestId → (FeatureEnable always-idle when offered and enabled) → ReaderEnable → Polling`; `session_id` = 16 hex from `IdSource` minted at BEGIN SESSION; `SessionBegun{session_id, funds}` (`0xFFFF` → `funds_known=false`); `vend_request` → VEND REQUEST and, from the poll, `VendApproved{session_id, request_id, cents}` or `VendDenied{..., reason "denied"}`; after approval, no `vend_result` within the edge timeout → VEND FAILURE and `SessionEnded{Timeout}`; `vend_result(true)` → VEND SUCCESS → SESSION COMPLETE → END SESSION poll → `SessionEnded{Complete}`; `vend_result(false)` → VEND FAILURE → SESSION COMPLETE → `SessionEnded{Complete}`; `end_session()` → SESSION COMPLETE → `SessionEnded{Edge}`; SESSION CANCEL REQUEST → VEND cancel if pending (`VendDenied{reason "session_ended"}`) → `SessionEnded{Reader}`; `0x0B` command out of sequence → `DeviceStatus{Error}` and re-Reset; `set_enabled(false)` → READER DISABLE and `Disabled`.
- `FakeReader : FakePeripheral`: level 1 or 3, optional always-idle feature, scripted `tap(funds)`, `approve(cents)`/`deny()` for the next VEND REQUEST, `cancel_session()`, `malfunction(code)`; records every VEND sub-command.

**Tests:** setup ladder and REQUEST ID into `DeviceState.model/serial`; tap → `SessionBegun` with funds and with undisclosed funds; approve and deny; the complete success ladder with the exact VEND sub-commands recorded; failure ladder; `end_session`; reader cancel with a pending request → `VendDenied{session_ended}` then `SessionEnded{Reader}`; vend_result timeout → VEND FAILURE and `SessionEnded{Timeout}`; `vend_request` without a session → false unless always-idle; always-idle negotiated only when offered and enabled; `cash_sale` sends VEND `0x05` with price and item; disable → READER `0x00` and `Disabled`; out-of-sequence → `Error` and re-Reset; miss threshold → `Offline`.

- [ ] TDD, commit `feat(mdbcore): cashless reader driver with explicit sessions`.

---

### Task 9: `mdbcore` Policy, Accounts, EdgeWatch, EventLog

**Model:** Sonnet. **Files:** Create `firmware/mdb/lib/mdbcore/policy.h`, `accounts.{h,cpp}`, `edge_watch.{h,cpp}`, `event_log.{h,cpp}`, `test/test_accounts/test_main.cpp`, `test/test_edge_watch/test_main.cpp`, `test/test_event_log/test_main.cpp`.

**Interfaces:**
- `policy.h`: `struct Policy { char policy_id[65]; bool accept; uint32_t max_escrow_cents; uint32_t coin_enabled[16]; uint8_t n_coin; uint32_t bill_enabled[16]; uint8_t n_bill; bool cashless_enabled; uint32_t change_threshold_cents; uint16_t edge_timeout_s; bool return_bill_on_lever; }`; `enum class Inhibit : uint8_t { None, NoPolicy, EdgeSilent, PolicyOff, DeviceFault }`.
- `class Accounts { void coin(uint32_t cents); void bill_stacked(uint32_t cents); void bill_escrowed(uint32_t cents); void bill_escrow_cleared(); uint32_t escrow_cents() const; bool bill_in_escrow(uint32_t&) const; bool cash_sale(uint32_t cents) /* false when it clamped */; bool would_exceed(uint32_t bill_cents, uint32_t max_escrow) const; bool remember_payout(const char* request_id, PayoutStatus, uint32_t requested, uint32_t paid); bool find_payout(const char* request_id, PaymentEvent& replay) const; int payout_count() const; void restore_payouts(const char* json, size_t len); size_t payouts_json(char*, size_t) const; }` (32 entries, oldest evicted; the JSON form is what the ledger task persists).
- `class EdgeWatch { EdgeWatch(Clock&); void seen(); void set_timeout_ms(uint32_t); bool expired() const; /* true once per silence onset */ bool take_silence_edge(); }`.
- `class EventLog { EventLog(LogStore&, IdSource&); void begin_boot(); /* mints boot_id, seq = 1 */ const char* boot_id() const; uint32_t seq_last() const; bool append(const PaymentEvent&, const char* iso_ts, char* json_out, size_t cap); /* writes the envelope json, appends to the store, advances seq; false when the store refused */ int pending() const; bool pending_at(int index, char* out, size_t cap) const; void acked(const char* boot_id, uint32_t seq); bool full() const; }`; capacity 256; `acked` erases, from the oldest, every entry whose embedded `boot_id` equals the acked id and `seq <=` the acked value, stopping at the first entry of a different boot that is newer than those (entries from earlier boots are only erased by acks carrying their own `boot_id`). The envelope JSON serializer lives in `wire.h` (Task 11); Task 9 defines its signature `size_t event_json(const PaymentEvent&, const char* event_id, const char* boot_id, uint32_t seq, const char* iso_ts, char*, size_t)` and implements it here minimally (every field, exact key names from the schemas) so the log tests can parse `boot_id`/`seq` back out with a tiny `peek_boot_seq(json, boot_id_out, seq_out)` helper.

**Tests:** accounts arithmetic and clamp; `would_exceed`; payout remember/find/evict and JSON round trip; edge watch expiry and single silence edge; event log: two boots in one store, acks for the second boot leave the first untouched, acks for the first erase only its entries, replay order, `full()` at 256, store refusal returns false and does not advance `seq`.

- [ ] TDD, commit `feat(mdbcore): policy, accounts, edge watch and the flash replay log`.

---

### Task 10: `mdbcore::Master` composition

**Model:** Sonnet. **Files:** Create `firmware/mdb/lib/mdbcore/master.{h,cpp}`, `state.h`, `test/test_master/test_main.cpp`.

**Interfaces:**
- `state.h`: `struct PaymentState { const char* boot_id; uint32_t seq_last; const char* policy_id /* nullptr = null */; bool accepting; Inhibit inhibited; uint32_t escrow_cents; bool bill_in_escrow; uint32_t bill_in_escrow_cents; DeviceState devices[3]; TubeLevel tubes[16]; uint8_t n_tubes; bool exact_change; bool stacker_known; uint16_t stacker_count; bool stacker_full; bool session_open; char session_id[65]; bool funds_known; uint32_t funds_cents; const char* session_started_ts; }`.
- Commands into the master (decoded by Task 11): `enum class CmdKind { Ping, SelfTest, ForceReport, SetPolicy, Payout, VendRequest, VendResult, EndSession, ReturnEscrow, DeviceReset, CashSale, Unknown }`; `struct Command { CmdKind kind; char request_id[65]; char name[33]; int64_t timestamp_unix /* -1 unknown */; Policy policy; uint32_t cents; char reason[20]; char sale_id[65]; bool has_session_id; char session_id[65]; char item[65]; char vend_request_id[65]; bool success; Device device; }`.
- `class Master { Master(Link&, Clock&, IdSource&, EventSink&, Changer&, Validator&, Cashless&, Accounts&, EdgeWatch&); void step(); /* one bus transaction, round-robin, plus timers */ boardcore::Ack submit(const Command&); bool poll_completion(boardcore::Ack&); void edge_seen(); void force_report(); bool take_state_dirty(); void snapshot(PaymentState&) const; void self_test(boardcore::Ack&) const; }` implementing `EscrowJudge` for the validator.
- Behaviour fixed here: no policy → `Inhibit::NoPolicy`, every device disabled; `set_policy` applies enables to all three devices, acks `ok` once each enable command has been ACKed on the bus (one `poll_completion` later, `accepted` first); `accept=false` → `PolicyOff`; `payout` → `accepted` after the changer accepts, replay of a remembered result for a repeated `request_id`, `rejected expired` when `timestamp` is older than `edge_timeout_s`; `vend_request` → `rejected no_session` when no session and no always-idle; `return_escrow` → `ok no_bill` or the validator return with `BillReturned{Edge}`; `device_reset` → that device's `reset()`; `cash_sale` → accounts and reader CASH SALE, `ok` or `ok clamped`; the escrow judge: `!accept` → return `Policy`; `would_exceed(max_escrow)` → `Policy`; `exact_change && bill > change_threshold` → `Policy`; else stack; the lever with `return_bill_on_lever` → `return_escrow(Customer)`; edge silence → disable all, return the escrowed bill (`EdgeSilent`), `end_session`, `Inhibit::EdgeSilent`, stays until the next acked `set_policy`; a device status of `Jammed|Error|Offline` while the policy accepts does not inhibit the others; `self_test` result JSON `{"checks":[{name,pass,detail}...]}` with checks `changer`, `validator`, `reader`, `clock_synced`, `event_log` (detail depth), `rx_mode_bit` (disagreement count), `policy`.
- `step()` round-robins `changer.step()`, `validator.step()`, `cashless.step()` (one transaction each per call), then runs the vend-result timeout and the edge watch; a device whose last money event could not be emitted is skipped until `EventSink` accepts again.

**Tests:** the eight reference flows of the contract at the master level (coins then `cash_sale`; price-first; cashless tap-first through `session_ended{complete}`; price-first `no_session`; cashless failure; cash vend failed payout; edge silence sequence; reboot replay is Task 9's); every command's ack phases and rejections; escrow judge rules; lever behaviour both ways; payout idempotency; `self_test` JSON shape; `take_state_dirty` true after every state-changing event and false after.

- [ ] TDD, commit `feat(mdbcore): Master composes the three devices under the edge policy`.

---

### Task 11: Wire — event, state and command JSON; golden files; Python golden check

**Model:** Sonnet. **Files:** Modify `firmware/mdb/lib/mdbcore/wire.{h,cpp}`; Create `command.{h,cpp}`, `test/test_command/test_main.cpp`, extend `test/test_wire/test_main.cpp`, golden files, `tests/firmware/mdb/__init__.py`, `tests/firmware/mdb/test_golden_wire.py`.

**Interfaces:**
- `wire.h`: `event_json(...)` (Task 9 signature, now complete: every kind with exactly the schema's keys, nullable fields emitted as `null`; `ts` is required by the schema, so the caller always supplies an ISO string, SNTP time when synced and otherwise the best-effort boot-clock time); `size_t state_json(const PaymentState&, const char* iso_ts, char*, size_t)` with `currency: "USD"`, `inhibited_reason` strings `null|no_policy|edge_silent|policy|device_fault`, `accepting == (inhibited == None)`; `size_t topic_event/state/ack(...)` via `boardcore::topic_join`.
- `command.h`: `enum class Decode { Ok, NoRequestId, Rejected, Unsupported }`; `struct Decoded { Decode result; Command cmd; char detail[96]; }`; `Decoded decode_command(const char* json, size_t len)` validating every bound in the schemas (`policy_id` 1–64, `edge_timeout_s` 5–120, `cents > 0`, `item` 1–64, `request_id` 8–64, enum members) and rejecting a float or string where an integer is required (`detail` names the field); `bool decode_event_ack(const char* json, size_t len, char* boot_id, uint32_t& seq)`.
- Golden files: `event_<kind>.json` for all twelve kinds, `state.json`, `state_inhibited.json`, `ack_ok_accepted.json`, `ack_ok_completed.json`, `ack_rejected.json`, `ack_failed.json`, `ack_unsupported.json` (plus Task 4's three).
- `tests/firmware/mdb/test_golden_wire.py`: every `event_*.json` validates as `PaymentEventEnvelope` with `event.kind` equal to the file's kind and the exact key set of that body model; `state*.json` as `PaymentState`; `capabilities.json` as `SubsystemCapabilities` with `commands == set(PAYMENT_COMMANDS) | STANDARD_COMMANDS` and `contract_version == payment.CONTRACT_VERSION`; heartbeat/LWT/acks as in the vending check. Always runs, no flag (mirror the vending conftest's exemption for `test_golden_wire.py` in Task 14's conftest; for this task, place the test so the existing `tests/firmware/conftest.py` exempts it: its rule exempts by file name, so `test_golden_wire.py` under `tests/firmware/mdb/` is already exempt).

**Tests (C++):** decode every command from a valid JSON; each bound violation → `Rejected` with the field in `detail`; integer-only enforcement for `cents`; missing `request_id` → `NoRequestId`; unknown command → `Unsupported`; `decode_event_ack` happy and malformed; `event_json` output of each kind parses back (use ArduinoJson in the test) with the expected keys.

- [ ] TDD, write goldens, `uv run pytest tests/firmware/mdb/test_golden_wire.py`, commit `feat(mdbcore): payment wire format, command decoder and golden files`.

---

### Task 12: ESP-IDF integration — UART 9-bit port, NVS log store, bus/ledger/comms tasks

**Model:** Sonnet. **Files:** Create `firmware/mdb/src/uart_bus.{h,cpp}`, `nvs_log_store.{h,cpp}`, `esp_ids.h`, `esp_clock.h`, `queues.h`, `bus_task.{h,cpp}`, `ledger_task.{h,cpp}`, `status_led.{h,cpp}`; Modify `src/comms_task.cpp`, `src/main.cpp`, `src/CMakeLists.txt` (REQUIRES `esp_driver_uart` or `driver`, `nvs_flash`, `esp_driver_rmt` for the LED), `firmware/mdb/sdkconfig.defaults` as needed.

**Interfaces and rules:**
- `UartBus : mdbcore::BusPort` on `kMdbUart`, 9600 8N1 base config: `write()` sets the parity per byte with `parity_even_for()` via `uart_set_parity` after `uart_wait_tx_done` for the previous byte, then writes one byte; `read()` returns bytes with their mode bit decoded from the driver's per-byte parity-error events (install the driver with an event queue, `uart_intr_config` with `rxfifo_full_thresh = 1` and a 2-symbol `rx_timeout_thresh`, parity configured EVEN so a parity error means `mode != parity_even(data)`); if the driver cannot correlate an error to its byte reliably, fall back to `mode = false` for data bytes and let the idle gap terminate (the Link already tolerates this and counts the disagreement); document which path shipped in the interface doc's bench checklist.
- `NvsLogStore : mdbcore::LogStore` over NVS partition `evlog`, namespace `log`: entries keyed `e<8 hex>` by a monotonically increasing index pair `(head, tail)` persisted in the same namespace; `append` writes the blob then advances `tail` (so a power cut between the two leaves at most a duplicate, never a hole); `erase_first(n)` erases blobs then advances `head`; also `payouts` blob for `Accounts::payouts_json`.
- `queues.h`: `cmd_q` (`CmdItem { enum { Decoded, EdgeSeen, ForceReport } kind; mdbcore::Decoded decoded; }`, 8 deep), `evt_q` (`PaymentEvent`, 64), `pub_q` (`PubItem { char topic[96]; char payload[512]; uint16_t len; bool retain; boardcore::Prio prio; }`, 32), `ack_q` (`boardcore::Ack`, 16), `ack_in_q` (`EventAckItem { char boot_id[33]; uint32_t seq; }`, 8).
- `bus_task` (core 1, priority 20): builds `Link`, `Changer`, `Validator`, `Cashless`, `Accounts`, `EdgeWatch`, `Master`; loop: drain `cmd_q` into `Master::submit` → `ack_q`; `master.step()`; `poll_completion` → `ack_q`; events emitted by the master go to `evt_q` via an `EventSink` that returns false when the queue is full; every state change (`take_state_dirty`) and every 30 s pushes a snapshot to the ledger via a 1-deep "state mailbox" (overwrite, never block); TWDT subscribed.
- `ledger_task` (core 0, priority 10): owns `EventLog` and `NvsLogStore`; drains `evt_q` → `EventLog::append` → `pub_q` (prio Outcome); drains `ack_in_q` → `EventLog::acked`; on each connect edge (a flag from comms) replays `pending_at(i)` for all entries into `pub_q` before new ones; persists `payouts_json` when it changes; turns the state mailbox into `state_json` → `pub_q` (prio Normal, retain); exposes `event_log_full()` for `self_test`.
- `comms_task`: extends Task 4: subscribes `cmd/mdb` and `payment/event/ack`; decodes commands (`decode_command`) → `cmd_q` + `EdgeSeen` on every `cmd/mdb` message; `ping/self_test/force_report` handled as in vending (`self_test` asks the bus task through `cmd_q` and awaits the ack); `payment/event/ack` → `ack_in_q`; drains `ack_q` and `pub_q` into the outbound ring and MQTT; HA discovery for `escrow_cents`, `accepting`, `exact_change`, and three device statuses; sets the connect-edge flag for the ledger.
- `status_led`: solid green accepting, amber inhibited, red any device offline, blue blink no broker, white blink log full.
- `main.cpp` creates the queues, starts the three tasks, and keeps the vending structure.

**Acceptance:** `pio run -e esp32-s3-devkitc-1` builds with `-Werror`; `pio test -e native` still passes (no native coverage of `src/`). No board is attached in this session: do not attempt to flash.

- [ ] Implement, build, commit `feat(firmware): mdb board tasks, 9-bit UART port and NVS replay store`.

---

### Task 13: `scripts/flash_board.py`

**Model:** Sonnet. **Files:** Create `scripts/flash_board.py`, `tests/meta/test_flash_board.py`; Delete `scripts/flash_vending.py`, `tests/meta/test_flash_vending.py`; Modify the three docs that name `flash_vending.py` (`CLAUDE.md` commands table row, `firmware/README.md`, `firmware/docs/vending-board-interface.md` §7) to name `flash_board.py vending`.

**Interfaces (pure, tested):**
- `@dataclass(frozen=True) class Target: name, project_dir: Path, env: str, prompt: str`; `TARGETS = {"vending": Target(..., Path("firmware/vending"), "kc868-a8", "vend> "), "mdb": Target(..., Path("firmware/mdb"), "esp32-s3-devkitc-1", "mdb> ")}`.
- `SerialPort(port, description, hwid)` with `.vid` and `.serial_number` (parsed from `SER=` in hwid); `parse_device_list(text)`, `candidate_boards(ports)` (vendor ids `1A86, 10C4, 0403, 303A`); `find_port_by_serial(ports, serial)`.
- `parse_banner(text) -> str | None` returns the version from a `firmware <version>` line; `expected_version(project_dir) -> str` reads `kFirmwareVersion` from `lib/*/version.h`.
- `load_provisioning(path) -> dict[str, str]` accepting only the seven keys and rejecting anything else or a non-string value; `mask_secret(key, value)`.
- `EXIT_VERIFIED = 0, EXIT_UNVERIFIED = 1, EXIT_FAILED = 2, EXIT_USAGE = 3`; `plan_steps(opts) -> list[str]` returning the ordered step names for the given flags (so the retry, skip-build and verify-only logic is unit-tested without a board).
- Side-effect layer (`run`, `list_ports`, `upload_with_retry(target, port, attempts=2, pause_s=2.0)`, `verify(port, target, timeout_s=15)`, `provision(port, target, values)`) is thin. Console access uses `pyserial` (`import serial`), which PlatformIO already installs in its own environment but the repo venv may lack: import it lazily and, when absent, report `EXIT_UNVERIFIED` with a message naming `pyserial` rather than failing. Do not add a dependency to `pyproject.toml`.

**Tests:** the vending tests ported; target table; serial number parsing; banner parsing; provisioning file validation (unknown key, non-string, missing file); exit code mapping; `plan_steps` for default, `--no-build`, `--verify-only`, `--provision`.

- [ ] TDD, `uv run pytest tests/meta/test_flash_board.py`, ruff, docs, commit `tooling: scripts/flash_board.py replaces flash_vending.py with retry, verify and provisioning`.

---

### Task 14: MDB conformance suite (opt-in)

**Model:** Sonnet. **Files:** Create `tests/firmware/mdb/conftest.py`, `tests/firmware/mdb/test_capabilities.py`, `test_standard_commands.py`, `test_policy.py`, `test_payout.py`, `test_cashless.py`, `test_replay.py`; Modify `tests/firmware/board_client.py` (subsystem parameter), `tests/skip_policy.py`.

**Interfaces:**
- `BoardClient(host, port, machine_id, subsystem="vending")`; the existing callers keep the default.
- `tests/firmware/mdb/conftest.py`: skips the whole package with reason `"MDB board conformance needs hardware until the MDB simulator ships (set ICE_COLDER_BOARD_MACHINE_ID)"` unless `ICE_COLDER_BOARD_TESTS=1` and `ICE_COLDER_BOARD_MACHINE_ID` are both set; `test_golden_wire.py` exempt; `board` fixture yields a `BoardClient` with `subsystem="mdb"`.
- `tests/skip_policy.py`: a fourth legitimate category `mdb_board_needs_hardware` matching that exact reason; docstring updated to say why (no MDB simulator yet; spec 2).
- Tests (all async, bounded waits via the client): retained capabilities validate and `contract_version == "1.1.0"` with the exact commands set; heartbeat within 12 s; `ping`/`self_test`/`force_report` ack `completed` and `force_report` republishes state; duplicate `request_id` replays the cached ack; `set_policy` is acked and echoed in `state.policy_id` with `accepting` matching `accept`; a malformed `set_policy` is `rejected`; `payout` with a timestamp 200 s old is `rejected expired`; `return_escrow` with no bill acks `ok no_bill`; `vend_request` without a session is `rejected no_session` (unless state shows a session); `cash_sale` larger than escrow acks `ok clamped`; after a `set_policy` the state's `boot_id` matches every event's; an ack for `state.seq_last` is accepted and a subsequent `force_report` shows no change in `seq_last`.
- `tests/meta` guards: `tests/meta/test_test_isolation.py` must still pass (no `ice_edge`/`ice_sims` imports here).

- [ ] Write the suite, run `uv run pytest tests/firmware tests/meta` (the mdb package skips with the policy reason; `tests/run_skip_guard.py` must accept it: run `uv run pytest -p no:cacheprovider tests/firmware` then `uv run python tests/run_skip_guard.py skip-report.json`), commit `test(firmware): opt-in MDB board conformance suite`.

---

### Task 15: Documentation and CI

**Model:** Sonnet. **Files:** Create `firmware/docs/mdb-board-interface.md`; Modify `firmware/README.md`, `CLAUDE.md`, `ROADMAP.md`, `docs/hardware/im600xl-io-list.md`, `.github/workflows/ci.yml`, `docs/superpowers/specs/2026-10-18-mdb-board-firmware-design.md` (§11 "As built" deviations collected from the task reports in `.superpowers/sdd/`).

**Content:**
- Interface doc sections: 1 purpose and contract; 2 carrier board and power (bus power open item); 3 MDB connector pinout; 4 transceiver and pin map; 5 the 9-bit path as shipped (which RX mechanism, the disagreement counter); 6 provisioning (console keys, `BOARD_DEFAULT_*`, the flash tool with every flag and exit code); 7 replay log and what a full log means for the operator; 8 LED patterns; 9 bench checklist (from spec §8); 10 open items (spec §10); 11 as-built deviations.
- `firmware/README.md`: layout with `common/` (the sharing rule), `mdb/`; build/test/flash table for both boards; the flash tool.
- `CLAUDE.md`: layout tree (`firmware/common`, `firmware/mdb`, `scripts/flash_board.py`, `tests/firmware/mdb`); commands table rows for the mdb build, host tests and the tool; the `firmware/` paragraph gains the platform rule and the mdb suite's skip rule; the Fault Codes paragraph's last sentence notes payment contract 1.1.0.
- `ROADMAP.md` Phase D: the MDB board as a second field device with a link to the spec; the open question about the payment equipment make now points at the interface doc.
- I/O list §8: the MDB open item points at the interface doc.
- `ci.yml` `firmware` job: cache key over both `platformio.ini` files; `pio run -e esp32-s3-devkitc-1` and `pio test -e native` in `firmware/mdb`; golden drift check covering `firmware/mdb/test/golden`.

- [ ] Write, run `uv run pytest tests/meta tests/contracts` (doc tests such as `test_static_css`/CLAUDE guards if any), commit `docs(firmware): MDB board interface, README, CLAUDE.md, roadmap and CI`.
