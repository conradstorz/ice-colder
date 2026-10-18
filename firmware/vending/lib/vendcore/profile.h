// C++ mirror of ice_contracts/board/dispenser_schema.py (contract 2.0.0).
#pragma once

#include <ArduinoJson.h>
#include <stdint.h>

namespace vendcore {

enum class Mechanism : uint8_t { BaggedIce, WaterFill };

// Channel fields are indexes into kChannels; -1 = absent.
struct Agitate { int motor; float run_s; };
struct IceFill { enum { BySensor, Timed } proof; int motor; int sensor; float max_run_s; };
// `channel` is the release actuator (catch for ByCatch, solenoid otherwise);
// `sensor` is the door sensor for BySensor (-1 otherwise).
struct Release {
    enum { ByCatch, Timed, BySensor } proof;
    int channel; int sensor; float drop_s; float pulse_s; float open_timeout_s; float close_timeout_s;
};
struct WaterFill {
    enum { ByVolume, Timed } proof;
    int valve; int flow; float target_ml; float pulses_per_l; float min_flow_ml_s; float no_flow_grace_s;
    float over_pct; float max_fill_s;
};
// on_during_mask bits: agitate=1, fill=2, release=4; "all" = 7.
struct Accessory { int channel; uint8_t on_during_mask; float lead_s; float lag_s; };

struct SlotProfile {
    Mechanism mechanism;
    char sku[33];
    Agitate agitate;
    IceFill ice;
    Release release;
    WaterFill water;
    Accessory acc[4];
    uint8_t acc_count;
};

struct ParseResult { bool ok; char detail[96]; };

// Does not allocate. `out` is fully reset first.
ParseResult parse_profile(JsonVariantConst v, Mechanism expected, SlotProfile& out);
float worst_case_seconds(const SlotProfile& p);

}  // namespace vendcore
