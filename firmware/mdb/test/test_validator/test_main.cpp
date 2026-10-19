#include <string.h>
#include <unity.h>

#include "../support/fake_validator.h"
#include "../support/fakes.h"
#include "validator.h"

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

struct Judge : EscrowJudge {
    bool stack = true;
    BillReturnReason reason = BillReturnReason::Policy;
    int calls = 0;
    uint32_t last_cents = 0;
    EscrowVerdict judge(uint32_t cents) override {
        ++calls;
        last_cents = cents;
        return EscrowVerdict{stack, reason};
    }
};

struct Rig {
    FakeClock clock;
    FakeBus bus{clock};
    FakeValidator dev;
    Link link{bus, clock};
    Sink sink;
    Judge judge;
    Validator v{link, clock, sink, judge};
    Rig() { bus.attach(0x30, &dev); }

    void steps(int k) {
        for (int i = 0; i < k; ++i) v.step();
    }
    // Enable $1 $5 $10 $20 and run the whole init sequence; the init events are discarded.
    void bring_up(bool accept = true) {
        static const uint32_t all[4] = {100, 500, 1000, 2000};
        v.set_enabled(all, 4, accept);
        v.reset();
        steps(12);
        sink.n = 0;
    }
};

}  // namespace

static void test_setup_parse(void) {
    Rig r;
    r.bring_up();
    const DeviceState& s = r.v.state();
    TEST_ASSERT_TRUE(s.present);
    TEST_ASSERT_EQUAL_INT(2, s.level);
    TEST_ASSERT_EQUAL_UINT8(4, s.n_denominations);
    TEST_ASSERT_EQUAL_UINT32(100, s.denominations[0]);
    TEST_ASSERT_EQUAL_UINT32(500, s.denominations[1]);
    TEST_ASSERT_EQUAL_UINT32(1000, s.denominations[2]);
    TEST_ASSERT_EQUAL_UINT32(2000, s.denominations[3]);
    TEST_ASSERT_EQUAL_STRING("FAKEVAL-2", s.model);
    TEST_ASSERT_EQUAL_STRING("SERV00000042", s.serial);
    TEST_ASSERT_EQUAL(Validator::Phase::Polling, r.v.phase());
    TEST_ASSERT_EQUAL(Status::Ready, s.status);
    uint16_t count = 99;
    bool full = true;
    TEST_ASSERT_TRUE(r.v.stacker(count, full));
    TEST_ASSERT_EQUAL_UINT16(0, count);
    TEST_ASSERT_FALSE(full);
}

static void test_level1_skips_identify(void) {
    Rig r;
    r.dev.level = 1;
    r.bring_up();
    TEST_ASSERT_EQUAL_INT(1, r.v.state().level);
    TEST_ASSERT_EQUAL_INT(0, r.dev.calls[7]);
    TEST_ASSERT_EQUAL(Validator::Phase::Polling, r.v.phase());
}

static void test_escrow_event_and_judge_stack(void) {
    Rig r;
    r.bring_up();
    r.dev.insert(1);  // $5
    r.steps(1);
    TEST_ASSERT_EQUAL_INT(1, r.sink.count(EventKind::BillEscrowed));
    TEST_ASSERT_EQUAL_UINT32(500, r.sink.ev[0].cents);
    TEST_ASSERT_EQUAL(Device::Bill, r.sink.ev[0].device);
    uint32_t held = 0;
    TEST_ASSERT_TRUE(r.v.bill_in_escrow(held));
    TEST_ASSERT_EQUAL_UINT32(500, held);
    r.steps(2);  // ESCROW, then the follow-up poll
    TEST_ASSERT_EQUAL_INT(1, r.judge.calls);
    TEST_ASSERT_EQUAL_UINT32(500, r.judge.last_cents);
    TEST_ASSERT_EQUAL_UINT8(0x01, r.dev.escrow_cmds[0]);
    TEST_ASSERT_EQUAL_INT(1, r.sink.count(EventKind::BillStacked));
    TEST_ASSERT_EQUAL_UINT32(500, r.sink.first(EventKind::BillStacked)->cents);
    TEST_ASSERT_FALSE(r.v.bill_in_escrow(held));
    r.steps(3);  // stacker re-read after a stacked bill
    uint16_t count = 0;
    bool full = false;
    r.v.stacker(count, full);
    TEST_ASSERT_EQUAL_UINT16(1, count);
}

static void test_judge_return_reports_policy(void) {
    Rig r;
    r.judge.stack = false;
    r.judge.reason = BillReturnReason::Policy;
    r.bring_up();
    r.dev.insert(2);
    r.steps(3);
    TEST_ASSERT_EQUAL_UINT8(0x00, r.dev.escrow_cmds[0]);
    TEST_ASSERT_EQUAL_INT(0, r.sink.count(EventKind::BillStacked));
    const PaymentEvent* e = r.sink.first(EventKind::BillReturned);
    TEST_ASSERT_NOT_NULL(e);
    TEST_ASSERT_EQUAL(BillReturnReason::Policy, e->bill_reason);
    TEST_ASSERT_EQUAL_UINT32(1000, e->cents);
}

static void test_return_escrow_customer(void) {
    Rig r;
    r.bring_up();
    uint32_t c = 0;
    TEST_ASSERT_FALSE(r.v.return_escrow(BillReturnReason::Customer));  // nothing held
    r.dev.insert(0);
    r.steps(1);  // BillEscrowed; the judge has not been consulted yet
    TEST_ASSERT_TRUE(r.v.bill_in_escrow(c));
    TEST_ASSERT_TRUE(r.v.return_escrow(BillReturnReason::Customer));
    r.steps(2);
    TEST_ASSERT_EQUAL_INT(0, r.judge.calls);  // the customer's press overrides the judge
    const PaymentEvent* e = r.sink.first(EventKind::BillReturned);
    TEST_ASSERT_NOT_NULL(e);
    TEST_ASSERT_EQUAL(BillReturnReason::Customer, e->bill_reason);
    TEST_ASSERT_EQUAL_UINT32(100, e->cents);
    TEST_ASSERT_FALSE(r.v.return_escrow(BillReturnReason::Customer));
}

static void test_unrequested_return_is_timeout(void) {
    Rig r;
    r.bring_up();
    r.dev.add(0xA1);  // returned, $5, nobody asked
    r.steps(1);
    const PaymentEvent* e = r.sink.first(EventKind::BillReturned);
    TEST_ASSERT_NOT_NULL(e);
    TEST_ASSERT_EQUAL(BillReturnReason::Timeout, e->bill_reason);
    TEST_ASSERT_EQUAL_UINT32(500, e->cents);
}

static void test_cashbox_out_and_in(void) {
    Rig r;
    r.bring_up();
    r.dev.cashbox_out();
    r.steps(2);
    TEST_ASSERT_EQUAL(Status::Offline, r.v.state().status);
    TEST_ASSERT_EQUAL_INT(1, r.sink.count(EventKind::DeviceStatus));
    TEST_ASSERT_EQUAL(Status::Offline, r.sink.ev[0].status);
    TEST_ASSERT_EQUAL_INT16(0x08, r.sink.ev[0].mdb_status);
    TEST_ASSERT_EQUAL_INT(0, r.sink.count(EventKind::CashCollected));
    r.dev.cashbox_in();
    r.steps(1);
    TEST_ASSERT_EQUAL_INT(1, r.sink.count(EventKind::CashCollected));
    TEST_ASSERT_EQUAL(Device::Bill, r.sink.first(EventKind::CashCollected)->device);
    TEST_ASSERT_EQUAL(Status::Ready, r.v.state().status);
    TEST_ASSERT_EQUAL_INT(3, r.sink.n);  // Offline, CashCollected, Ready
    TEST_ASSERT_EQUAL(EventKind::DeviceStatus, r.sink.ev[0].kind);
    TEST_ASSERT_EQUAL(EventKind::CashCollected, r.sink.ev[1].kind);
    TEST_ASSERT_EQUAL(EventKind::DeviceStatus, r.sink.ev[2].kind);
}

static void test_stacker_full(void) {
    Rig r;
    r.dev.stacker_capacity = 2;
    r.bring_up();
    r.dev.insert(0);
    r.steps(3);
    r.dev.insert(0);
    r.steps(3);
    r.steps(3);
    TEST_ASSERT_EQUAL(Status::Full, r.v.state().status);
    uint16_t count = 0;
    bool full = false;
    TEST_ASSERT_TRUE(r.v.stacker(count, full));
    TEST_ASSERT_TRUE(full);
    TEST_ASSERT_EQUAL_UINT16(2, count);
}

static void test_every_status_byte_maps(void) {
    struct Case {
        uint8_t byte;
        Status want;
    };
    static const Case cases[] = {
        {0x01, Status::Error}, {0x02, Status::Error},   {0x03, Status::Busy},     {0x04, Status::Error},
        {0x05, Status::Jammed}, {0x08, Status::Offline}, {0x09, Status::Disabled}, {0x0A, Status::Error},
        {0x0C, Status::Error},
    };
    for (const Case& c : cases) {
        Rig r;
        r.bring_up();
        r.dev.status(c.byte);
        r.steps(1);
        TEST_ASSERT_EQUAL_MESSAGE(c.want, r.v.state().status, "status byte mapping");
        const PaymentEvent* e = r.sink.first(EventKind::DeviceStatus);
        TEST_ASSERT_NOT_NULL(e);
        TEST_ASSERT_EQUAL_INT16(c.byte, e->mdb_status);
    }
    {  // 0x0B rejected, 0x07 with nothing held, 0x2x attempts: no status change
        Rig r;
        r.bring_up();
        r.dev.status(0x0B);
        r.dev.status(0x07);
        r.dev.status(0x23);
        r.steps(3);
        TEST_ASSERT_EQUAL(Status::Ready, r.v.state().status);
        TEST_ASSERT_EQUAL_INT(0, r.sink.n);
    }
    {  // disabled-bill rejection credits nothing
        Rig r;
        r.bring_up();
        r.dev.add(0xC1);
        r.steps(1);
        TEST_ASSERT_EQUAL_INT(0, r.sink.n);
    }
    {  // 111 transferred to stacker counts as stacked
        Rig r;
        r.bring_up();
        r.dev.add(0xF1);
        r.steps(1);
        TEST_ASSERT_EQUAL_INT(1, r.sink.count(EventKind::BillStacked));
        TEST_ASSERT_EQUAL_UINT32(500, r.sink.ev[0].cents);
    }
    {  // worst status in a block wins
        Rig r;
        r.bring_up();
        r.dev.status(0x03);
        r.dev.status(0x05, true);
        r.steps(1);
        TEST_ASSERT_EQUAL(Status::Jammed, r.v.state().status);
    }
}

static void test_possible_credited_removal_is_error(void) {
    Rig r;
    r.bring_up();
    r.dev.status(0x0C);
    r.steps(1);
    TEST_ASSERT_EQUAL(Status::Error, r.v.state().status);
}

static void test_validator_reset_during_escrow_returns_timeout(void) {
    Rig r;
    r.bring_up();
    r.dev.insert(1);
    r.steps(1);
    r.dev.clear_script();
    r.dev.just_reset();
    r.v.reset();  // forced re-init while a bill is held
    const PaymentEvent* e = nullptr;
    r.steps(12);
    e = r.sink.first(EventKind::BillReturned);
    TEST_ASSERT_NOT_NULL(e);
    TEST_ASSERT_EQUAL(BillReturnReason::Timeout, e->bill_reason);
    uint32_t c;
    TEST_ASSERT_FALSE(r.v.bill_in_escrow(c));
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
    TEST_ASSERT_FALSE(r.v.state().present);
    TEST_ASSERT_EQUAL(Validator::Phase::Reset, r.v.phase());

    const int before = r.bus.tx_len;
    r.steps(5);
    TEST_ASSERT_EQUAL_INT(before, r.bus.tx_len);

    r.dev.silent = false;
    r.clock.advance_ms(1500);
    r.steps(12);
    TEST_ASSERT_TRUE(r.v.state().present);
    TEST_ASSERT_EQUAL(Validator::Phase::Polling, r.v.phase());
    TEST_ASSERT_EQUAL(Status::Ready, r.v.state().status);
    TEST_ASSERT_EQUAL_INT(2, r.sink.count(EventKind::DeviceStatus));  // Offline, then Ready
}

static void test_bill_type_mask_and_escrow_bits(void) {
    Rig r;
    static const uint32_t some[2] = {500, 2000};
    r.v.set_enabled(some, 2, true);
    r.v.reset();
    r.steps(12);
    TEST_ASSERT_TRUE(r.dev.n_masks >= 1);
    const int last = r.dev.n_masks - 1;
    TEST_ASSERT_EQUAL_HEX16(0x000A, r.dev.bill_masks[last]);  // types 1 and 3
    TEST_ASSERT_EQUAL_HEX16(0x000A, r.dev.escrow_masks[last]);
    // a denomination the validator does not have is simply absent
    static const uint32_t odd[2] = {500, 7};
    r.v.set_enabled(odd, 2, true);
    r.steps(2);
    TEST_ASSERT_EQUAL_HEX16(0x0002, r.dev.bill_masks[r.dev.n_masks - 1]);
}

static void test_accept_false_disables_all(void) {
    Rig r;
    r.bring_up();
    static const uint32_t all[4] = {100, 500, 1000, 2000};
    r.v.set_enabled(all, 4, false);
    r.steps(2);
    TEST_ASSERT_EQUAL_HEX16(0, r.dev.bill_masks[r.dev.n_masks - 1]);
    TEST_ASSERT_EQUAL_HEX16(0, r.dev.escrow_masks[r.dev.n_masks - 1]);
    TEST_ASSERT_EQUAL(Status::Disabled, r.v.state().status);
    r.v.set_enabled(all, 4, true);
    r.steps(2);
    TEST_ASSERT_EQUAL_HEX16(0x000F, r.dev.bill_masks[r.dev.n_masks - 1]);
    TEST_ASSERT_EQUAL(Status::Ready, r.v.state().status);
}

static void test_no_escrow_stacks_straight_away(void) {
    Rig r;
    r.dev.escrow_capable = false;
    static const uint32_t all[4] = {100, 500, 1000, 2000};
    r.v.set_enabled(all, 4, true);
    r.v.reset();
    r.steps(12);
    TEST_ASSERT_EQUAL_INT(1, r.sink.count(EventKind::DeviceStatus));
    TEST_ASSERT_EQUAL_STRING("no_escrow", r.sink.ev[0].detail);
    TEST_ASSERT_EQUAL_HEX16(0, r.dev.escrow_masks[r.dev.n_masks - 1]);
    r.sink.n = 0;
    r.dev.insert(3);
    r.steps(1);
    TEST_ASSERT_EQUAL_INT(0, r.sink.count(EventKind::BillEscrowed));
    TEST_ASSERT_EQUAL_INT(1, r.sink.count(EventKind::BillStacked));
    TEST_ASSERT_EQUAL_UINT32(2000, r.sink.first(EventKind::BillStacked)->cents);
    TEST_ASSERT_EQUAL_INT(0, r.judge.calls);
}

static void test_money_event_survives_full_sink(void) {
    Rig r;
    r.bring_up();
    r.sink.capacity = 0;
    r.dev.insert(1);
    r.steps(1);
    const int polls = r.dev.calls[3];
    r.steps(3);
    TEST_ASSERT_EQUAL_INT(polls, r.dev.calls[3]);  // no bus traffic past an undelivered event
    TEST_ASSERT_EQUAL_INT(0, r.dev.n_escrow_cmds);
    r.sink.capacity = 64;
    r.steps(1);
    TEST_ASSERT_EQUAL_INT(1, r.sink.count(EventKind::BillEscrowed));
    r.steps(2);
    TEST_ASSERT_EQUAL_INT(1, r.sink.count(EventKind::BillStacked));
}

static void test_stacked_event_survives_full_sink(void) {
    Rig r;
    r.bring_up();
    r.dev.insert(1);
    r.steps(2);  // escrowed, ESCROW sent
    r.sink.n = 0;
    r.sink.capacity = 0;
    r.steps(2);  // poll sees stacked; sink refuses
    r.sink.capacity = 64;
    r.steps(1);
    TEST_ASSERT_EQUAL_INT(1, r.sink.count(EventKind::BillStacked));
    r.steps(5);
    TEST_ASSERT_EQUAL_INT(1, r.sink.count(EventKind::BillStacked));
}

static void test_stacker_reread_every_10s(void) {
    Rig r;
    r.bring_up();
    const int before = r.dev.calls[6];
    r.steps(3);
    TEST_ASSERT_EQUAL_INT(before, r.dev.calls[6]);
    r.clock.advance_ms(10000);
    r.steps(1);
    TEST_ASSERT_EQUAL_INT(before + 1, r.dev.calls[6]);
}

static void test_event_queue_drops_are_counted(void) {
    Rig r;
    r.bring_up();
    TEST_ASSERT_EQUAL_UINT32(0, r.v.dropped_events());
}

int main(int, char**) {
    UNITY_BEGIN();
    RUN_TEST(test_setup_parse);
    RUN_TEST(test_level1_skips_identify);
    RUN_TEST(test_escrow_event_and_judge_stack);
    RUN_TEST(test_judge_return_reports_policy);
    RUN_TEST(test_return_escrow_customer);
    RUN_TEST(test_unrequested_return_is_timeout);
    RUN_TEST(test_cashbox_out_and_in);
    RUN_TEST(test_stacker_full);
    RUN_TEST(test_every_status_byte_maps);
    RUN_TEST(test_possible_credited_removal_is_error);
    RUN_TEST(test_validator_reset_during_escrow_returns_timeout);
    RUN_TEST(test_miss_threshold_goes_offline_then_recovers);
    RUN_TEST(test_bill_type_mask_and_escrow_bits);
    RUN_TEST(test_accept_false_disables_all);
    RUN_TEST(test_no_escrow_stacks_straight_away);
    RUN_TEST(test_money_event_survives_full_sink);
    RUN_TEST(test_stacked_event_survives_full_sink);
    RUN_TEST(test_stacker_reread_every_10s);
    RUN_TEST(test_event_queue_drops_are_counted);
    return UNITY_END();
}
