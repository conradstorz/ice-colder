// The vending board's one declarative channel table (mirrors
// ice_sims/vending/channels.py). No other file spells a channel id.
#pragma once

#include <stdint.h>
#include <string.h>

namespace vendcore {

enum class Kind : uint8_t { Binary, Counter, Temperature };
enum class Dir : uint8_t { Input, Output };

struct Channel {
    const char* id;
    Kind kind;
    Dir dir;
    const char* driven_by;  // nullptr when no command inhibits it
    const char* unit;
    const char* description;
    float interval_s;
};

constexpr int kChannelCount = 18;
constexpr int kOutputCount = 7;
constexpr int kBinaryInputCount = 9;

// 9 binary inputs, 7 binary outputs, fill_pulses, ambient.
constexpr Channel kChannels[kChannelCount] = {
    {"bag_full_sensor", Kind::Binary, Dir::Input, nullptr, "", "Ice bag full sensor", 1.0f},
    {"button_ice", Kind::Binary, Dir::Input, nullptr, "", "Bagged ice customer button", 1.0f},
    {"button_water_1gal", Kind::Binary, Dir::Input, nullptr, "", "1 gallon water customer button", 1.0f},
    {"button_water_5gal", Kind::Binary, Dir::Input, nullptr, "", "5 gallon water customer button", 1.0f},
    {"ro_tank_full", Kind::Binary, Dir::Input, nullptr, "", "RO storage tank full switch", 1.0f},
    {"ro_tank_empty", Kind::Binary, Dir::Input, nullptr, "", "RO storage tank empty switch", 1.0f},
    {"water_pressure_ok", Kind::Binary, Dir::Input, nullptr, "",
     "Water supply pressure OK (false = low pressure)", 1.0f},
    {"sump_high", Kind::Binary, Dir::Input, nullptr, "", "Sump pit high-level switch", 1.0f},
    {"service_door", Kind::Binary, Dir::Input, nullptr, "", "Service door open switch", 1.0f},
    {"agitator_motor", Kind::Binary, Dir::Output, "dispense", "", "Ice agitator motor", 1.0f},
    {"auger_motor", Kind::Binary, Dir::Output, "dispense", "", "Ice auger motor", 1.0f},
    {"bag_fan", Kind::Binary, Dir::Output, "dispense", "", "Bag-opening fan", 1.0f},
    {"trap_door_catch", Kind::Binary, Dir::Output, "dispense", "",
     "Trap-door magnetic catch (energized holds the door shut)", 1.0f},
    {"water_valve_solenoid", Kind::Binary, Dir::Output, "water_valve", "", "Water valve solenoid", 1.0f},
    {"ro_production", Kind::Binary, Dir::Output, nullptr, "",
     "RO production (inlet solenoid and booster pump; autonomous)", 1.0f},
    {"heater_power", Kind::Binary, Dir::Output, nullptr, "", "Cabinet heater power (autonomous)", 1.0f},
    {"fill_pulses", Kind::Counter, Dir::Input, nullptr, "pulses",
     "Flow-meter pulses during the current fill; divide by the slot profile's pulses_per_liter for litres",
     1.0f},
    {"ambient", Kind::Temperature, Dir::Input, nullptr, "C", "Ambient (machine room) temperature", 10.0f},
};

constexpr const char* kCommands[5] = {"ping", "self_test", "force_report", "dispense", "water_valve"};
constexpr const char* kSubsystem = "vending";

// Output ordinals are channel index - kBinaryInputCount (outputs follow the
// nine binary inputs in the table).
enum class Out : uint8_t { Agitator, Auger, BagFan, TrapDoorCatch, WaterValve, RoProduction, Heater };
enum class In : uint8_t {
    BagFull, ButtonIce, ButtonWater1, ButtonWater5, RoTankFull, RoTankEmpty, WaterPressureOk, SumpHigh,
    ServiceDoor
};

// -1 when undeclared.
inline int channel_index(const char* id) {
    if (id == nullptr) return -1;
    for (int i = 0; i < kChannelCount; ++i) {
        if (strcmp(kChannels[i].id, id) == 0) return i;
    }
    return -1;
}
inline bool is_output(int idx) {
    return idx >= 0 && idx < kChannelCount && kChannels[idx].dir == Dir::Output;
}
inline bool is_binary_input(int idx) { return idx >= 0 && idx < kBinaryInputCount; }
inline Out out_from_index(int idx) { return static_cast<Out>(idx - kBinaryInputCount); }
inline In in_from_index(int idx) { return static_cast<In>(idx); }

}  // namespace vendcore
