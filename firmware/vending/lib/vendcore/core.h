// The whole firmware core: one tick(), one submit(). The ESP-IDF tasks only wrap this.
#pragma once

#include <stdint.h>

#include "ack_cache.h"
#include "channels.h"
#include "command.h"
#include "dispenser.h"
#include "freeze_protect.h"
#include "inputs.h"
#include "interlocks.h"
#include "ports.h"
#include "ro_loop.h"

namespace vendcore {

struct CoreConfig {
    Polarity polarity;
    // VMC silence timer. 0 (default) = disabled: liveness is the retained `online` topic (the
    // VMC's LWT flips it false) plus any inbound command marking the VMC seen. The edge publishes
    // `online` only on connect and sends commands only while selling, so a pure silence timer would
    // trip during every idle spell and every dispense longer than the timeout. A non-zero value
    // re-enables the timer and requires the edge to publish `online` periodically.
    uint32_t silence_ms = 0;
    uint32_t ro_max_run_ms = 5400000;
    float heater_on_c = 3.0f, heater_off_c = 6.0f;
    uint32_t io_republish_ms = 60000;
    uint32_t temp_publish_ms = 10000;
};

class Core {
public:
    Core(const CoreConfig& cfg, Clock& clock, IoPort& io, Publisher& pub);

    void tick();                       // one 10 ms step: read, scan, interlocks, dispenser, loops, guard, write, kick
    Ack submit(const Decoded& d);      // first ack for any command; never blocks
    bool poll_completion(Ack& out);    // water_valve completed acks (and cancellation failures), one per call
    void vmc_seen() { vmc_.seen(clock_.now_ms()); }
    void vmc_online(bool online) { vmc_.set_online(online, clock_.now_ms()); }
    void force_report() { force_report_ = true; }
    void self_test(Ack& out) const;    // result_json {"checks":[{name,pass,detail}...]}
    const Health& health() const { return health_; }
    uint32_t dropped_events() const { return pub_.dropped; }

private:
    struct CountingPub : Publisher {
        explicit CountingPub(Publisher& inner) : inner_(inner) {}
        bool publish(const Event& e) override {
            const bool ok = inner_.publish(e);
            if (!ok) ++dropped;
            return ok;
        }
        Publisher& inner_;
        uint32_t dropped = 0;
    };

    static constexpr int kRing = 4;

    void step(uint32_t now);
    void on_trip(uint32_t now, const char* detail);
    bool dispense_output_written() const;
    void valve_tick(uint32_t now);
    void cancel_valve(const char* detail);
    void push_completion(const Ack& a);
    void publish_temperature();
    Ack make_ack(const Command& c, AckStatus s, const char* detail) const;

    CoreConfig cfg_;
    Clock& clock_;
    IoPort& io_;
    CountingPub pub_;
    InputScanner scanner_;
    OutputGuard guard_;
    VmcLiveness vmc_;
    Interlocks interlocks_;
    Health health_;
    Dispenser dispenser_;
    RoLoop ro_;
    FreezeProtect freeze_;

    InputSnapshot raw_ = {};
    bool io_ok_ = false;
    bool out_[kOutputCount] = {};
    bool ticked_ = false;   // tick() has run at least once (deadline baseline)
    bool started_ = false;  // step() has run at least once (republish baseline)
    uint32_t last_tick_ms_ = 0;
    uint32_t last_io_republish_ms_ = 0;
    uint32_t last_temp_ms_ = 0;
    bool temp_sent_ = false;
    bool force_report_ = false;
    bool suppress_completion_ = false;  // submit() returns the valve outcome directly: no ring push

    bool trip_active_ = false;
    char trip_detail_[24] = {};
    const char* step_trip_ = nullptr;  // trip detail seen by the most recent step(), else nullptr

    bool valve_active_ = false;
    char valve_id_[65] = {};
    uint32_t valve_start_ms_ = 0;
    uint32_t valve_ms_ = 0;

    Ack ring_[kRing] = {};
    int ring_head_ = 0, ring_count_ = 0;
};

}  // namespace vendcore
