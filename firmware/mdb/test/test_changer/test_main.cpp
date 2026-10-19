#include <string.h>
#include <unity.h>

#include "../support/fake_changer.h"
#include "../support/fakes.h"
#include "changer.h"

using namespace mdbcore;

void setUp(void) {}
void tearDown(void) {}

namespace {

struct Sink : EventSink {
    PaymentEvent ev[64];
    int n = 0;
    int capacity = 64;
    bool emit(const PaymentEvent& e) override {
        if (n >= capacity || n >= 64) return false;
        ev[n++] = e;
        return true;
    }
    int count(EventKind k) const {
        int c = 0;
        for (int i = 0; i < n; ++i) c += ev[i].kind == k;
        return c;
    }
    const PaymentEvent* first(EventKind k) const {
        for (int i = 0; i < n; ++i)
            if (ev[i].kind == k) return &ev[i];
        return nullptr;
    }
};

struct Rig {
    FakeClock clock;
    FakeBus bus{clock};
    FakeChanger dev;
    Link link{bus, clock};
    Sink sink;
    Changer ch{link, clock, sink};
    Rig() { bus.attach(0x08, &dev); }

    void steps(int k) {
        for (int i = 0; i < k; ++i) ch.step();
    }
    // Enable all four coins and run the whole init sequence; the init events are discarded.
    void bring_up(bool accept = true) {
        static const uint32_t all[4] = {5, 10, 25, 100};
        ch.set_enabled(all, 4, accept);
        ch.reset();
        steps(12);
        sink.n = 0;
    }
};

}  // namespace

static void test_setup_parse_usd_scale5(void) {
    Rig r;
    r.bring_up();
    const DeviceState& s = r.ch.state();
    TEST_ASSERT_TRUE(s.present);
    TEST_ASSERT_EQUAL_INT(3, s.level);
    TEST_ASSERT_EQUAL_UINT8(4, s.n_denominations);  // the 0xFF token is not a denomination
    TEST_ASSERT_EQUAL_UINT32(5, s.denominations[0]);
    TEST_ASSERT_EQUAL_UINT32(10, s.denominations[1]);
    TEST_ASSERT_EQUAL_UINT32(25, s.denominations[2]);
    TEST_ASSERT_EQUAL_UINT32(100, s.denominations[3]);
    TEST_ASSERT_EQUAL_STRING("FAKECHG-3", s.model);
    TEST_ASSERT_EQUAL_STRING("SER000000042", s.serial);
    TEST_ASSERT_TRUE(r.dev.features_enabled);
    TEST_ASSERT_EQUAL(Changer::Phase::Polling, r.ch.phase());
    TEST_ASSERT_EQUAL(Status::Ready, s.status);
}

static void test_level2_skips_identify(void) {
    Rig r;
    r.dev.level = 2;
    r.bring_up();
    TEST_ASSERT_EQUAL_INT(2, r.ch.state().level);
    TEST_ASSERT_EQUAL_INT(0, r.dev.calls[7]);
    TEST_ASSERT_FALSE(r.dev.features_enabled);
}

static void test_deposit_routes(void) {
    Rig r;
    r.bring_up();
    r.dev.deposit(2, 1);  // quarter to tubes
    r.dev.deposit(3, 0);  // dollar to cashbox
    r.steps(3);
    TEST_ASSERT_EQUAL_INT(2, r.sink.count(EventKind::CoinAccepted));
    TEST_ASSERT_EQUAL_UINT32(25, r.sink.ev[0].cents);
    TEST_ASSERT_EQUAL(CoinRoute::Tubes, r.sink.ev[0].route);
    TEST_ASSERT_EQUAL_UINT32(100, r.sink.ev[1].cents);
    TEST_ASSERT_EQUAL(CoinRoute::Cashbox, r.sink.ev[1].route);
    uint8_t n = 0;
    const TubeLevel* t = r.ch.tubes(n);
    TEST_ASSERT_EQUAL_UINT16(11, t[2].count);  // deposit count byte updates the tube
}

static void test_multi_byte_block_parsed_whole(void) {
    Rig r;
    r.bring_up();
    const int polls = r.dev.calls[3];
    r.dev.deposit(0, 1);
    r.dev.deposit(1, 0, true);
    r.dev.lever(true);
    r.dev.slug(true);
    r.steps(1);
    TEST_ASSERT_EQUAL_INT(2, r.sink.count(EventKind::CoinAccepted));
    TEST_ASSERT_TRUE(r.ch.escrow_lever_pending());
    TEST_ASSERT_EQUAL_INT(polls + 1, r.dev.calls[3]);  // one POLL for the whole block
}

static void test_slug_and_token_emit_nothing(void) {
    Rig r;
    r.bring_up();
    r.dev.slug();
    r.dev.deposit(4, 0);   // token
    r.dev.deposit(1, 3);   // rejected
    r.steps(3);
    TEST_ASSERT_EQUAL_INT(0, r.sink.count(EventKind::CoinAccepted));
    TEST_ASSERT_EQUAL_INT(0, r.sink.n);
}

static void test_lever_flag_consumed_on_read(void) {
    Rig r;
    r.bring_up();
    r.dev.lever();
    r.steps(1);
    TEST_ASSERT_TRUE(r.ch.escrow_lever_pending());
    TEST_ASSERT_FALSE(r.ch.escrow_lever_pending());
    TEST_ASSERT_EQUAL_INT(0, r.sink.n);
}

static void test_every_status_byte_maps(void) {
    struct Row {
        uint8_t byte;
        Status want;
    };
    static const Row rows[] = {
        {0x02, Status::Busy},    {0x03, Status::Error},   {0x04, Status::Error},  {0x05, Status::Error},
        {0x06, Status::Offline}, {0x07, Status::Jammed},  {0x08, Status::Error},  {0x09, Status::Error},
        {0x0A, Status::Busy},    {0x0B, Status::Busy},    {0x0C, Status::Jammed}, {0x0D, Status::Error},
    };
    for (const Row& row : rows) {
        Rig r;
        r.bring_up();
        r.dev.status(row.byte);
        r.steps(1);
        TEST_ASSERT_EQUAL_INT_MESSAGE(1, r.sink.count(EventKind::DeviceStatus), "one status event");
        const PaymentEvent* e = r.sink.first(EventKind::DeviceStatus);
        TEST_ASSERT_EQUAL_MESSAGE(static_cast<int>(row.want), static_cast<int>(e->status), "status");
        TEST_ASSERT_EQUAL_INT(row.byte, e->mdb_status);
        TEST_ASSERT_EQUAL(Device::Coin, e->device);
        TEST_ASSERT_EQUAL(static_cast<int>(row.want), static_cast<int>(r.ch.state().status));
    }
}

static void test_status_clears_back_to_ready(void) {
    Rig r;
    r.bring_up();
    r.dev.status(0x07);
    r.steps(2);  // jam seen, then a clean poll
    TEST_ASSERT_EQUAL_INT(2, r.sink.count(EventKind::DeviceStatus));
    TEST_ASSERT_EQUAL(Status::Jammed, r.sink.ev[0].status);
    TEST_ASSERT_EQUAL(Status::Ready, r.sink.ev[1].status);
    TEST_ASSERT_EQUAL_INT(-1, r.sink.ev[1].mdb_status);
}

static void test_changer_reset_goes_back_to_setup(void) {
    Rig r;
    r.bring_up();
    const int setups = r.dev.calls[1];
    r.dev.just_reset();
    r.steps(1);
    TEST_ASSERT_EQUAL_INT(1, r.sink.count(EventKind::DeviceStatus));
    TEST_ASSERT_EQUAL_INT(0x0B, r.sink.ev[0].mdb_status);
    TEST_ASSERT_EQUAL(Changer::Phase::Setup, r.ch.phase());
    r.steps(8);
    TEST_ASSERT_EQUAL_INT(setups + 1, r.dev.calls[1]);
    TEST_ASSERT_EQUAL(Changer::Phase::Polling, r.ch.phase());
}

static void test_tubes_and_payable_total(void) {
    Rig r;
    r.dev.tubes[0] = 4;   // 5c
    r.dev.tubes[1] = 3;   // 10c
    r.dev.tubes[2] = 2;   // 25c
    r.dev.full_mask = 0x0004;  // quarter tube full
    r.bring_up();
    uint8_t n = 0;
    const TubeLevel* t = r.ch.tubes(n);
    TEST_ASSERT_EQUAL_UINT8(3, n);  // only types routed to tubes
    TEST_ASSERT_EQUAL_UINT32(5, t[0].cents);
    TEST_ASSERT_EQUAL_UINT16(4, t[0].count);
    TEST_ASSERT_FALSE(t[0].full);
    TEST_ASSERT_EQUAL_UINT32(25, t[2].cents);
    TEST_ASSERT_TRUE(t[2].full);
    TEST_ASSERT_EQUAL_UINT32(4 * 5 + 3 * 10 + 2 * 25, r.ch.payable_total_cents());
}

static void test_exact_change_both_ways(void) {
    Rig r;
    r.dev.tubes[0] = 4;
    r.dev.tubes[1] = 3;
    r.dev.tubes[2] = 2;
    r.bring_up();  // payable = 100
    TEST_ASSERT_TRUE(r.ch.exact_change(101));
    TEST_ASSERT_FALSE(r.ch.exact_change(100));
    TEST_ASSERT_FALSE(r.ch.exact_change(50));
}

static void test_empty_tubes_status(void) {
    Rig r;
    memset(r.dev.tubes, 0, sizeof(r.dev.tubes));
    static const uint32_t some[2] = {5, 25};
    r.ch.set_enabled(some, 2, true);
    r.ch.reset();
    r.steps(12);
    TEST_ASSERT_EQUAL(Status::Empty, r.ch.state().status);
}

static void test_l3_payout_exact(void) {
    Rig r;
    r.dev.busy_polls = 2;
    r.bring_up();
    TEST_ASSERT_TRUE(r.ch.request_payout(35, "req-00000001"));
    TEST_ASSERT_TRUE(r.ch.payout_in_progress());
    r.steps(20);
    TEST_ASSERT_FALSE(r.ch.payout_in_progress());
    TEST_ASSERT_EQUAL_INT(1, r.dev.n_payouts);
    TEST_ASSERT_EQUAL_UINT8(7, r.dev.payouts[0]);  // 35 cents / scaling 5
    const PaymentEvent* e = r.sink.first(EventKind::PayoutResult);
    TEST_ASSERT_NOT_NULL(e);
    TEST_ASSERT_EQUAL(PayoutStatus::Ok, e->payout_status);
    TEST_ASSERT_EQUAL_UINT32(35, e->requested_cents);
    TEST_ASSERT_EQUAL_UINT32(35, e->paid_cents);
    TEST_ASSERT_EQUAL_STRING("req-00000001", e->request_id);
    TEST_ASSERT_EQUAL_UINT32(10 * 5 + 9 * 10 + 9 * 25, r.ch.payable_total_cents());  // tubes re-read
}

static void test_l3_payout_partial(void) {
    Rig r;
    memset(r.dev.tubes, 0, sizeof(r.dev.tubes));
    r.dev.tubes[2] = 1;  // a single quarter
    r.bring_up();
    TEST_ASSERT_TRUE(r.ch.request_payout(50, "req-00000002"));
    r.steps(12);
    const PaymentEvent* e = r.sink.first(EventKind::PayoutResult);
    TEST_ASSERT_NOT_NULL(e);
    TEST_ASSERT_EQUAL(PayoutStatus::Partial, e->payout_status);
    TEST_ASSERT_EQUAL_UINT32(25, e->paid_cents);
    TEST_ASSERT_EQUAL_UINT32(50, e->requested_cents);
}

static void test_l3_payout_failed_when_empty(void) {
    Rig r;
    memset(r.dev.tubes, 0, sizeof(r.dev.tubes));
    r.bring_up();
    TEST_ASSERT_TRUE(r.ch.request_payout(50, "req-00000003"));
    r.steps(12);
    const PaymentEvent* e = r.sink.first(EventKind::PayoutResult);
    TEST_ASSERT_NOT_NULL(e);
    TEST_ASSERT_EQUAL(PayoutStatus::Failed, e->payout_status);
    TEST_ASSERT_EQUAL_UINT32(0, e->paid_cents);
}

static void test_l3_payout_nak_is_failed(void) {
    Rig r;
    r.dev.fail_payout = true;
    r.bring_up();
    TEST_ASSERT_TRUE(r.ch.request_payout(50, "req-00000004"));
    r.steps(4);
    const PaymentEvent* e = r.sink.first(EventKind::PayoutResult);
    TEST_ASSERT_NOT_NULL(e);
    TEST_ASSERT_EQUAL(PayoutStatus::Failed, e->payout_status);
    TEST_ASSERT_FALSE(r.ch.payout_in_progress());
}

static void test_l3_without_alt_payout_uses_dispense(void) {
    Rig r;
    r.dev.alt_payout = false;
    r.bring_up();
    TEST_ASSERT_TRUE(r.ch.request_payout(35, "req-00000005"));
    r.steps(8);
    TEST_ASSERT_EQUAL_INT(0, r.dev.n_payouts);
    TEST_ASSERT_EQUAL_INT(2, r.dev.n_disp);  // 1 quarter + 1 dime
    TEST_ASSERT_EQUAL(PayoutStatus::Ok, r.sink.first(EventKind::PayoutResult)->payout_status);
}

static void test_l2_dispense_greedy_choice(void) {
    Rig r;
    r.dev.level = 2;
    r.bring_up();
    TEST_ASSERT_TRUE(r.ch.request_payout(65, "req-00000006"));
    r.steps(10);
    TEST_ASSERT_EQUAL_INT(3, r.dev.n_disp);
    TEST_ASSERT_EQUAL_HEX8(0x22, r.dev.dispenses[0]);  // 2 quarters
    TEST_ASSERT_EQUAL_HEX8(0x11, r.dev.dispenses[1]);  // 1 dime
    TEST_ASSERT_EQUAL_HEX8(0x10, r.dev.dispenses[2]);  // 1 nickel
    const PaymentEvent* e = r.sink.first(EventKind::PayoutResult);
    TEST_ASSERT_NOT_NULL(e);
    TEST_ASSERT_EQUAL(PayoutStatus::Ok, e->payout_status);
    TEST_ASSERT_EQUAL_UINT32(65, e->paid_cents);
}

static void test_l2_dispense_caps_at_15_per_command(void) {
    Rig r;
    r.dev.level = 2;
    r.dev.tubes[2] = 20;
    r.bring_up();
    TEST_ASSERT_TRUE(r.ch.request_payout(400, "req-00000007"));  // 16 quarters
    r.steps(10);
    TEST_ASSERT_EQUAL_INT(2, r.dev.n_disp);
    TEST_ASSERT_EQUAL_HEX8(0xF2, r.dev.dispenses[0]);
    TEST_ASSERT_EQUAL_HEX8(0x12, r.dev.dispenses[1]);
    TEST_ASSERT_EQUAL_UINT32(400, r.sink.first(EventKind::PayoutResult)->paid_cents);
}

static void test_l2_partial_when_short(void) {
    Rig r;
    r.dev.level = 2;
    memset(r.dev.tubes, 0, sizeof(r.dev.tubes));
    r.dev.tubes[1] = 1;  // one dime
    r.bring_up();
    TEST_ASSERT_TRUE(r.ch.request_payout(30, "req-00000008"));
    r.steps(8);
    const PaymentEvent* e = r.sink.first(EventKind::PayoutResult);
    TEST_ASSERT_EQUAL(PayoutStatus::Partial, e->payout_status);
    TEST_ASSERT_EQUAL_UINT32(10, e->paid_cents);
}

static void test_l2_failed_when_no_coin_fits(void) {
    Rig r;
    r.dev.level = 2;
    memset(r.dev.tubes, 0, sizeof(r.dev.tubes));
    r.bring_up();
    TEST_ASSERT_TRUE(r.ch.request_payout(30, "req-00000009"));
    r.steps(4);
    TEST_ASSERT_EQUAL_INT(0, r.dev.n_disp);
    TEST_ASSERT_EQUAL(PayoutStatus::Failed, r.sink.first(EventKind::PayoutResult)->payout_status);
}

static void test_request_payout_refused_while_running_or_offline(void) {
    Rig r;
    r.bring_up();
    TEST_ASSERT_TRUE(r.ch.request_payout(35, "req-0000000a"));
    TEST_ASSERT_FALSE(r.ch.request_payout(10, "req-0000000b"));
    r.steps(12);
    TEST_ASSERT_TRUE(r.ch.request_payout(10, "req-0000000b"));  // free again after the result
    r.steps(12);

    Rig o;
    TEST_ASSERT_FALSE(o.ch.request_payout(10, "req-0000000c"));  // never initialised
    o.bring_up();
    o.dev.silent = true;
    o.steps(kMissThreshold);
    TEST_ASSERT_FALSE(o.ch.request_payout(10, "req-0000000d"));
}

static void test_miss_threshold_goes_offline_then_recovers(void) {
    Rig r;
    r.bring_up();
    r.dev.silent = true;
    r.steps(kMissThreshold - 1);
    TEST_ASSERT_EQUAL_INT(0, r.sink.count(EventKind::DeviceStatus));
    r.steps(1);
    TEST_ASSERT_EQUAL_INT(1, r.sink.count(EventKind::DeviceStatus));
    TEST_ASSERT_EQUAL(Status::Offline, r.sink.ev[0].status);
    TEST_ASSERT_FALSE(r.ch.state().present);
    TEST_ASSERT_EQUAL(Changer::Phase::Reset, r.ch.phase());

    const int before = r.bus.tx_len;
    r.steps(5);  // rate limited: no hammering while offline
    TEST_ASSERT_EQUAL_INT(before, r.bus.tx_len);

    r.dev.silent = false;
    r.clock.advance_ms(1500);
    r.steps(12);
    TEST_ASSERT_TRUE(r.ch.state().present);
    TEST_ASSERT_EQUAL(Changer::Phase::Polling, r.ch.phase());
    TEST_ASSERT_EQUAL(Status::Ready, r.ch.state().status);
    TEST_ASSERT_EQUAL_INT(1, r.sink.count(EventKind::DeviceStatus) - 1);  // Offline, then Ready
}

static void test_set_enabled_sends_mask(void) {
    Rig r;
    r.bring_up();
    const int base = r.dev.n_masks;
    static const uint32_t two[2] = {5, 25};
    r.ch.set_enabled(two, 2, true);
    r.steps(1);
    TEST_ASSERT_EQUAL_INT(base + 1, r.dev.n_masks);
    TEST_ASSERT_EQUAL_HEX16(0x0005, r.dev.coin_masks[r.dev.n_masks - 1]);  // types 0 and 2
    TEST_ASSERT_EQUAL_UINT8(0, r.dev.manual_mask_bytes);
    r.ch.set_enabled(two, 2, false);
    r.steps(1);
    TEST_ASSERT_EQUAL_HEX16(0x0000, r.dev.coin_masks[r.dev.n_masks - 1]);
    TEST_ASSERT_EQUAL_UINT8(0, r.ch.state().n_enabled);
    r.steps(1);
    TEST_ASSERT_EQUAL(Status::Disabled, r.ch.state().status);
}

static void test_money_event_survives_full_sink(void) {
    Rig r;
    r.bring_up();
    r.sink.capacity = 0;
    r.dev.deposit(2, 1);
    r.steps(1);  // consumes the deposit, emit refused
    const int calls = r.dev.calls_total;
    r.dev.deposit(1, 1);
    r.steps(5);  // no new bus transaction while an event is undelivered
    TEST_ASSERT_EQUAL_INT(calls, r.dev.calls_total);
    TEST_ASSERT_EQUAL_INT(0, r.sink.n);
    r.sink.capacity = 64;
    r.steps(3);
    TEST_ASSERT_EQUAL_INT(2, r.sink.count(EventKind::CoinAccepted));
    TEST_ASSERT_EQUAL_UINT32(25, r.sink.ev[0].cents);  // order preserved, none lost or duplicated
    TEST_ASSERT_EQUAL_UINT32(10, r.sink.ev[1].cents);
}

static void test_tubes_reread_every_10s(void) {
    Rig r;
    r.bring_up();
    const int tube_calls = r.dev.calls[2];
    r.steps(3);
    TEST_ASSERT_EQUAL_INT(tube_calls, r.dev.calls[2]);
    r.clock.advance_ms(10000);
    r.steps(1);
    TEST_ASSERT_EQUAL_INT(tube_calls + 1, r.dev.calls[2]);
}

static const PaymentEvent* only_result(Rig& r) {
    TEST_ASSERT_EQUAL_INT(1, r.sink.count(EventKind::PayoutResult));
    return r.sink.first(EventKind::PayoutResult);
}

static void test_l3_interrupted_offline_reports_progress(void) {
    Rig r;
    r.dev.busy_polls = 1000;  // never finishes on its own
    r.bring_up();
    TEST_ASSERT_TRUE(r.ch.request_payout(35, "req-00000010"));
    r.steps(3);  // PAYOUT, VALUE POLL (35c so far), STATUS (busy)
    r.dev.silent = true;
    r.steps(kMissThreshold + 1);
    const PaymentEvent* e = only_result(r);
    TEST_ASSERT_EQUAL(PayoutStatus::Ok, e->payout_status);  // vouched == requested
    TEST_ASSERT_EQUAL_UINT32(35, e->paid_cents);
    TEST_ASSERT_EQUAL_STRING("interrupted: offline", e->detail);
    TEST_ASSERT_FALSE(r.ch.payout_in_progress());
}

static void test_l3_interrupted_offline_partial(void) {
    Rig r;
    r.dev.busy_polls = 1000;
    r.dev.value_poll_cap_units = 5;  // changer reports 25c of the 35c
    r.bring_up();
    TEST_ASSERT_TRUE(r.ch.request_payout(35, "req-00000011"));
    r.steps(3);
    r.dev.silent = true;
    r.steps(kMissThreshold + 1);
    const PaymentEvent* e = only_result(r);
    TEST_ASSERT_EQUAL(PayoutStatus::Partial, e->payout_status);
    TEST_ASSERT_EQUAL_UINT32(25, e->paid_cents);
    TEST_ASSERT_EQUAL_UINT32(35, e->requested_cents);
    TEST_ASSERT_EQUAL_STRING("interrupted: offline", e->detail);
}

static void test_l3_interrupted_before_progress_is_failed(void) {
    Rig r;
    r.dev.busy_polls = 1000;
    r.bring_up();
    TEST_ASSERT_TRUE(r.ch.request_payout(35, "req-00000012"));
    r.steps(1);  // PAYOUT sent, no VALUE POLL yet
    r.dev.silent = true;
    r.steps(kMissThreshold + 1);
    const PaymentEvent* e = only_result(r);
    TEST_ASSERT_EQUAL(PayoutStatus::Failed, e->payout_status);
    TEST_ASSERT_EQUAL_UINT32(0, e->paid_cents);
    TEST_ASSERT_EQUAL_STRING("interrupted: offline", e->detail);
}

static void test_l3_interrupted_by_reset_reports_progress(void) {
    Rig r;
    r.dev.busy_polls = 1000;
    r.dev.value_poll_cap_units = 5;
    r.bring_up();
    TEST_ASSERT_TRUE(r.ch.request_payout(35, "req-00000013"));
    r.steps(3);
    r.ch.reset();
    r.steps(1);
    const PaymentEvent* e = only_result(r);
    TEST_ASSERT_EQUAL(PayoutStatus::Partial, e->payout_status);
    TEST_ASSERT_EQUAL_UINT32(25, e->paid_cents);
    TEST_ASSERT_EQUAL_STRING("interrupted: reset", e->detail);
}

static void test_l3_timeout_reports_progress(void) {
    Rig r;
    r.dev.busy_polls = 1000;
    r.dev.value_poll_cap_units = 5;
    r.bring_up();
    TEST_ASSERT_TRUE(r.ch.request_payout(35, "req-00000014"));
    r.steps(3);
    r.clock.advance_ms(31000);
    r.steps(1);
    const PaymentEvent* e = only_result(r);
    TEST_ASSERT_EQUAL(PayoutStatus::Partial, e->payout_status);
    TEST_ASSERT_EQUAL_UINT32(25, e->paid_cents);
    TEST_ASSERT_EQUAL_STRING("interrupted: timeout", e->detail);
}

static void test_l2_interrupted_offline_reports_acked_dispense(void) {
    Rig r;
    r.dev.level = 2;
    r.bring_up();
    TEST_ASSERT_TRUE(r.ch.request_payout(65, "req-00000015"));
    r.steps(1);  // DISPENSE 2 quarters ACKed
    TEST_ASSERT_EQUAL_INT(1, r.dev.n_disp);
    r.dev.silent = true;  // the next DISPENSE (dime) times out, so is not vouched for
    r.steps(kMissThreshold + 1);
    const PaymentEvent* e = only_result(r);
    TEST_ASSERT_EQUAL(PayoutStatus::Partial, e->payout_status);
    TEST_ASSERT_EQUAL_UINT32(50, e->paid_cents);
    TEST_ASSERT_EQUAL_STRING("interrupted: offline", e->detail);
}

static void test_l2_interrupted_before_any_dispense_is_failed(void) {
    Rig r;
    r.dev.level = 2;
    r.bring_up();
    TEST_ASSERT_TRUE(r.ch.request_payout(65, "req-00000016"));
    r.dev.silent = true;
    r.steps(kMissThreshold + 1);
    const PaymentEvent* e = only_result(r);
    TEST_ASSERT_EQUAL(PayoutStatus::Failed, e->payout_status);
    TEST_ASSERT_EQUAL_UINT32(0, e->paid_cents);
    TEST_ASSERT_EQUAL_STRING("interrupted: offline", e->detail);
}

static void test_event_queue_drops_are_counted(void) {
    Rig r;
    r.bring_up();
    TEST_ASSERT_EQUAL_UINT32(0, r.ch.dropped_events());
    r.sink.capacity = 0;  // nothing can be delivered, so pending_ fills
    for (int i = 0; i < 3; ++i) r.dev.deposit(2, 0, i > 0);
    r.dev.deposit(1, 0, true);
    r.steps(1);
    TEST_ASSERT_EQUAL_UINT32(0, r.ch.dropped_events());  // a POLL block never exceeds the queue
}

int main(int, char**) {
    UNITY_BEGIN();
    RUN_TEST(test_setup_parse_usd_scale5);
    RUN_TEST(test_level2_skips_identify);
    RUN_TEST(test_deposit_routes);
    RUN_TEST(test_multi_byte_block_parsed_whole);
    RUN_TEST(test_slug_and_token_emit_nothing);
    RUN_TEST(test_lever_flag_consumed_on_read);
    RUN_TEST(test_every_status_byte_maps);
    RUN_TEST(test_status_clears_back_to_ready);
    RUN_TEST(test_changer_reset_goes_back_to_setup);
    RUN_TEST(test_tubes_and_payable_total);
    RUN_TEST(test_exact_change_both_ways);
    RUN_TEST(test_empty_tubes_status);
    RUN_TEST(test_l3_payout_exact);
    RUN_TEST(test_l3_payout_partial);
    RUN_TEST(test_l3_payout_failed_when_empty);
    RUN_TEST(test_l3_payout_nak_is_failed);
    RUN_TEST(test_l3_without_alt_payout_uses_dispense);
    RUN_TEST(test_l2_dispense_greedy_choice);
    RUN_TEST(test_l2_dispense_caps_at_15_per_command);
    RUN_TEST(test_l2_partial_when_short);
    RUN_TEST(test_l2_failed_when_no_coin_fits);
    RUN_TEST(test_request_payout_refused_while_running_or_offline);
    RUN_TEST(test_miss_threshold_goes_offline_then_recovers);
    RUN_TEST(test_set_enabled_sends_mask);
    RUN_TEST(test_money_event_survives_full_sink);
    RUN_TEST(test_tubes_reread_every_10s);
    RUN_TEST(test_l3_interrupted_offline_reports_progress);
    RUN_TEST(test_l3_interrupted_offline_partial);
    RUN_TEST(test_l3_interrupted_before_progress_is_failed);
    RUN_TEST(test_l3_interrupted_by_reset_reports_progress);
    RUN_TEST(test_l3_timeout_reports_progress);
    RUN_TEST(test_l2_interrupted_offline_reports_acked_dispense);
    RUN_TEST(test_l2_interrupted_before_any_dispense_is_failed);
    RUN_TEST(test_event_queue_drops_are_counted);
    return UNITY_END();
}
