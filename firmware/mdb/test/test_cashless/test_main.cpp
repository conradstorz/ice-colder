#include <string.h>
#include <unity.h>

#include "../support/fake_reader.h"
#include "../support/fakes.h"
#include "cashless.h"

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
    FakeReader dev;
    Link link{bus, clock};
    Sink sink;
    FakeIds ids;
    Cashless c{link, clock, sink, ids};
    Rig() { bus.attach(0x10, &dev); }

    void steps(int k) {
        for (int i = 0; i < k; ++i) c.step();
    }
    // Run the whole init ladder; init events are discarded.
    void bring_up(bool enabled = true) {
        c.set_enabled(enabled);
        c.reset();
        steps(10);
        sink.n = 0;
    }
};

}  // namespace

static void test_setup_ladder_and_request_id(void) {
    Rig r;
    r.bring_up();
    const DeviceState& s = r.c.state();
    TEST_ASSERT_TRUE(s.present);
    TEST_ASSERT_EQUAL(Cashless::Phase::Polling, r.c.phase());
    TEST_ASSERT_EQUAL(Status::Ready, s.status);
    TEST_ASSERT_EQUAL_INT(1, s.level);
    TEST_ASSERT_EQUAL_STRING("FAKECASHLESS", s.model);
    TEST_ASSERT_EQUAL_STRING("RDR000000007", s.serial);
    TEST_ASSERT_EQUAL_INT(1, r.dev.n_cfg);
    TEST_ASSERT_EQUAL_UINT8(1, r.dev.vmc_levels[0]);
    TEST_ASSERT_EQUAL_INT(1, r.dev.n_prices);
    TEST_ASSERT_EQUAL_UINT16(0xFFFF, r.dev.max_price);
    TEST_ASSERT_EQUAL_UINT16(0x0000, r.dev.min_price);
    TEST_ASSERT_EQUAL_INT(30, r.dev.id_request_len);
    TEST_ASSERT_EQUAL_MEMORY("ICE", r.dev.id_request + 1, 3);
    TEST_ASSERT_EQUAL_MEMORY("mdb-s3", r.dev.id_request + 16, 6);
    TEST_ASSERT_TRUE(r.dev.reader_enabled);
    TEST_ASSERT_EQUAL_INT(1, r.dev.n_reader_cmds);
    TEST_ASSERT_EQUAL_UINT8(0x01, r.dev.reader_cmds[0]);
}

static void test_tap_with_funds(void) {
    Rig r;
    r.bring_up();
    r.dev.tap(200);  // 200 units * 5 = 1000 cents
    r.steps(1);
    TEST_ASSERT_EQUAL_INT(1, r.sink.n);
    const PaymentEvent& e = r.sink.ev[0];
    TEST_ASSERT_EQUAL(EventKind::SessionBegun, e.kind);
    TEST_ASSERT_EQUAL(Device::Cashless, e.device);
    TEST_ASSERT_TRUE(e.funds_known);
    TEST_ASSERT_EQUAL_UINT32(1000, e.funds_cents);
    TEST_ASSERT_EQUAL_INT(16, (int)strlen(e.session_id));
    char id[17];
    bool known = false;
    uint32_t funds = 0;
    TEST_ASSERT_TRUE(r.c.session_open(id, known, funds));
    TEST_ASSERT_EQUAL_STRING(e.session_id, id);
    TEST_ASSERT_TRUE(known);
    TEST_ASSERT_EQUAL_UINT32(1000, funds);
}

static void test_tap_with_undisclosed_funds(void) {
    Rig r;
    r.bring_up();
    r.dev.tap(0xFFFF);
    r.steps(1);
    TEST_ASSERT_EQUAL(EventKind::SessionBegun, r.sink.ev[0].kind);
    TEST_ASSERT_FALSE(r.sink.ev[0].funds_known);
    char id[17];
    bool known = true;
    uint32_t funds = 7;
    TEST_ASSERT_TRUE(r.c.session_open(id, known, funds));
    TEST_ASSERT_FALSE(known);
}

static void test_vend_approved(void) {
    Rig r;
    r.bring_up();
    r.dev.tap(200);
    r.steps(1);
    r.dev.approve(150);
    TEST_ASSERT_TRUE(r.c.vend_request(150, 3, "req-1"));
    r.steps(2);  // VEND REQUEST, then the poll that carries the verdict
    TEST_ASSERT_EQUAL_INT(2, r.sink.n);
    const PaymentEvent& e = r.sink.ev[1];
    TEST_ASSERT_EQUAL(EventKind::VendApproved, e.kind);
    TEST_ASSERT_EQUAL_STRING("req-1", e.request_id);
    TEST_ASSERT_EQUAL_STRING(r.sink.ev[0].session_id, e.session_id);
    TEST_ASSERT_EQUAL_UINT32(150, e.cents);
    TEST_ASSERT_EQUAL_INT(1, r.dev.n_vends);
    TEST_ASSERT_EQUAL_UINT8(0x00, r.dev.vends[0].sub);
    TEST_ASSERT_EQUAL_UINT16(30, r.dev.vends[0].price);  // 150 / 5 scaled units
    TEST_ASSERT_EQUAL_UINT16(3, r.dev.vends[0].item);
}

static void test_vend_denied(void) {
    Rig r;
    r.bring_up();
    r.dev.tap(200);
    r.steps(1);
    r.dev.deny();
    TEST_ASSERT_TRUE(r.c.vend_request(150, 3, "req-2"));
    r.steps(2);
    TEST_ASSERT_EQUAL(EventKind::VendDenied, r.sink.ev[1].kind);
    TEST_ASSERT_EQUAL_STRING("denied", r.sink.ev[1].detail);
    TEST_ASSERT_EQUAL_STRING("req-2", r.sink.ev[1].request_id);
    // The session survives a denial; a second request is allowed.
    char id[17];
    bool k;
    uint32_t f;
    TEST_ASSERT_TRUE(r.c.session_open(id, k, f));
    TEST_ASSERT_TRUE(r.c.vend_request(100, 4, "req-3"));
}

static void test_success_ladder(void) {
    Rig r;
    r.bring_up();
    r.dev.tap(200);
    r.steps(1);
    r.dev.approve(150);
    r.c.vend_request(150, 3, "req-1");
    r.steps(2);
    TEST_ASSERT_TRUE(r.c.vend_result(true, 3));
    r.steps(5);  // VEND SUCCESS, SESSION COMPLETE, poll 0x07
    TEST_ASSERT_EQUAL_INT(3, r.sink.n);
    TEST_ASSERT_EQUAL(EventKind::SessionBegun, r.sink.ev[0].kind);
    TEST_ASSERT_EQUAL(EventKind::VendApproved, r.sink.ev[1].kind);
    TEST_ASSERT_EQUAL(EventKind::SessionEnded, r.sink.ev[2].kind);
    TEST_ASSERT_EQUAL(SessionEndReason::Complete, r.sink.ev[2].end_reason);
    TEST_ASSERT_EQUAL_STRING(r.sink.ev[0].session_id, r.sink.ev[2].session_id);
    TEST_ASSERT_EQUAL_INT(3, r.dev.n_vends);
    TEST_ASSERT_EQUAL_UINT8(0x00, r.dev.vends[0].sub);
    TEST_ASSERT_EQUAL_UINT8(0x02, r.dev.vends[1].sub);
    TEST_ASSERT_EQUAL_UINT16(3, r.dev.vends[1].item);
    TEST_ASSERT_EQUAL_UINT8(0x04, r.dev.vends[2].sub);
    char id[17];
    bool k;
    uint32_t f;
    TEST_ASSERT_FALSE(r.c.session_open(id, k, f));
}

static void test_failure_ladder(void) {
    Rig r;
    r.bring_up();
    r.dev.tap(200);
    r.steps(1);
    r.dev.approve(150);
    r.c.vend_request(150, 3, "req-1");
    r.steps(2);
    TEST_ASSERT_TRUE(r.c.vend_result(false, 3));
    r.steps(5);
    TEST_ASSERT_EQUAL_INT(3, r.sink.n);
    TEST_ASSERT_EQUAL(EventKind::SessionEnded, r.sink.ev[2].kind);
    TEST_ASSERT_EQUAL(SessionEndReason::Complete, r.sink.ev[2].end_reason);
    TEST_ASSERT_EQUAL_INT(3, r.dev.n_vends);
    TEST_ASSERT_EQUAL_UINT8(0x03, r.dev.vends[1].sub);
    TEST_ASSERT_EQUAL_UINT8(0x04, r.dev.vends[2].sub);
}

static void test_vend_result_needs_approved_vend(void) {
    Rig r;
    r.bring_up();
    TEST_ASSERT_FALSE(r.c.vend_result(true, 1));
    r.dev.tap(200);
    r.steps(1);
    TEST_ASSERT_FALSE(r.c.vend_result(true, 1));
}

static void test_end_session_by_edge(void) {
    Rig r;
    r.bring_up();
    r.dev.tap(200);
    r.steps(1);
    TEST_ASSERT_TRUE(r.c.end_session());
    TEST_ASSERT_FALSE(r.c.end_session());  // already closing
    r.steps(4);
    TEST_ASSERT_EQUAL_INT(2, r.sink.n);
    TEST_ASSERT_EQUAL(EventKind::SessionEnded, r.sink.ev[1].kind);
    TEST_ASSERT_EQUAL(SessionEndReason::Edge, r.sink.ev[1].end_reason);
    TEST_ASSERT_EQUAL_INT(1, r.dev.n_vends);
    TEST_ASSERT_EQUAL_UINT8(0x04, r.dev.vends[0].sub);
}

static void test_reader_cancel_with_pending_request(void) {
    Rig r;
    r.bring_up();
    r.dev.tap(200);
    r.steps(1);
    TEST_ASSERT_TRUE(r.c.vend_request(150, 3, "req-1"));  // reader holds its answer
    r.steps(1);                                           // VEND REQUEST sent, no verdict
    r.dev.cancel_session();
    r.steps(5);
    TEST_ASSERT_EQUAL_INT(3, r.sink.n);
    TEST_ASSERT_EQUAL(EventKind::VendDenied, r.sink.ev[1].kind);
    TEST_ASSERT_EQUAL_STRING("session_ended", r.sink.ev[1].detail);
    TEST_ASSERT_EQUAL_STRING("req-1", r.sink.ev[1].request_id);
    TEST_ASSERT_EQUAL(EventKind::SessionEnded, r.sink.ev[2].kind);
    TEST_ASSERT_EQUAL(SessionEndReason::Reader, r.sink.ev[2].end_reason);
    TEST_ASSERT_EQUAL_INT(3, r.dev.n_vends);
    TEST_ASSERT_EQUAL_UINT8(0x00, r.dev.vends[0].sub);
    TEST_ASSERT_EQUAL_UINT8(0x01, r.dev.vends[1].sub);  // VEND CANCEL
    TEST_ASSERT_EQUAL_UINT8(0x04, r.dev.vends[2].sub);  // SESSION COMPLETE
}

static void test_reader_cancel_without_vend(void) {
    Rig r;
    r.bring_up();
    r.dev.tap(200);
    r.steps(1);
    r.dev.cancel_session();
    r.steps(4);
    TEST_ASSERT_EQUAL_INT(2, r.sink.n);
    TEST_ASSERT_EQUAL(EventKind::SessionEnded, r.sink.ev[1].kind);
    TEST_ASSERT_EQUAL(SessionEndReason::Reader, r.sink.ev[1].end_reason);
    TEST_ASSERT_EQUAL_INT(1, r.dev.n_vends);
    TEST_ASSERT_EQUAL_UINT8(0x04, r.dev.vends[0].sub);
}

static void test_vend_result_timeout(void) {
    Rig r;
    r.bring_up();
    r.c.set_edge_timeout_ms(5000);
    r.dev.tap(200);
    r.steps(1);
    r.dev.approve(150);
    r.c.vend_request(150, 3, "req-1");
    r.steps(2);
    r.steps(3);
    TEST_ASSERT_EQUAL_INT(2, r.sink.n);  // still waiting for the Master
    r.clock.advance_ms(5001);
    r.steps(5);
    TEST_ASSERT_EQUAL_INT(3, r.sink.n);
    TEST_ASSERT_EQUAL(EventKind::SessionEnded, r.sink.ev[2].kind);
    TEST_ASSERT_EQUAL(SessionEndReason::Timeout, r.sink.ev[2].end_reason);
    TEST_ASSERT_EQUAL_INT(3, r.dev.n_vends);
    TEST_ASSERT_EQUAL_UINT8(0x03, r.dev.vends[1].sub);  // VEND FAILURE
    TEST_ASSERT_EQUAL_UINT8(0x04, r.dev.vends[2].sub);
    TEST_ASSERT_FALSE(r.c.vend_result(true, 3));
}

static void test_vend_request_without_session(void) {
    Rig r;
    r.bring_up();
    TEST_ASSERT_FALSE(r.c.vend_request(150, 3, "req-1"));
    r.steps(2);
    TEST_ASSERT_EQUAL_INT(0, r.dev.n_vends);
    TEST_ASSERT_EQUAL_INT(0, r.sink.n);
}

static void test_always_idle_negotiated(void) {
    Rig r;
    r.dev.level = 3;
    r.dev.offer_always_idle = true;
    r.c.set_always_idle_wanted(true);
    r.bring_up();
    TEST_ASSERT_TRUE(r.c.always_idle());
    TEST_ASSERT_EQUAL_INT(1, r.dev.n_features);
    TEST_ASSERT_EQUAL_UINT8(0x02, r.dev.features[3]);
    TEST_ASSERT_EQUAL_INT(2, r.dev.n_cfg);  // level 1 first, then level 3 once the reader said 3
    TEST_ASSERT_EQUAL_UINT8(1, r.dev.vmc_levels[0]);
    TEST_ASSERT_EQUAL_UINT8(3, r.dev.vmc_levels[1]);
    TEST_ASSERT_EQUAL_INT(3, r.c.state().level);
    // A vend works with no reader session.
    r.dev.approve(150);
    TEST_ASSERT_TRUE(r.c.vend_request(150, 3, "idle-1"));
    r.steps(2);
    TEST_ASSERT_EQUAL(EventKind::VendApproved, r.sink.ev[0].kind);
    TEST_ASSERT_EQUAL_STRING("idle-1", r.sink.ev[0].request_id);
    TEST_ASSERT_EQUAL_INT(16, (int)strlen(r.sink.ev[0].session_id));
    TEST_ASSERT_TRUE(r.c.vend_result(true, 3));
    r.steps(5);
    TEST_ASSERT_EQUAL(EventKind::SessionEnded, r.sink.ev[1].kind);
    TEST_ASSERT_EQUAL(SessionEndReason::Complete, r.sink.ev[1].end_reason);
}

static void test_always_idle_not_wanted(void) {
    Rig r;
    r.dev.level = 3;
    r.dev.offer_always_idle = true;
    r.bring_up();
    TEST_ASSERT_FALSE(r.c.always_idle());
    TEST_ASSERT_EQUAL_INT(0, r.dev.n_features);
    TEST_ASSERT_EQUAL_INT(1, r.dev.n_cfg);
    TEST_ASSERT_FALSE(r.c.vend_request(150, 3, "x"));
}

static void test_always_idle_not_offered(void) {
    Rig r;
    r.dev.level = 3;
    r.dev.offer_always_idle = false;
    r.c.set_always_idle_wanted(true);
    r.bring_up();
    TEST_ASSERT_FALSE(r.c.always_idle());
    TEST_ASSERT_EQUAL_INT(0, r.dev.n_features);
    TEST_ASSERT_EQUAL(Cashless::Phase::Polling, r.c.phase());
    TEST_ASSERT_FALSE(r.c.vend_request(150, 3, "x"));
}

static void test_cash_sale(void) {
    Rig r;
    r.bring_up();
    TEST_ASSERT_TRUE(r.c.cash_sale(250, 9));
    r.steps(1);
    TEST_ASSERT_EQUAL_INT(1, r.dev.n_vends);
    TEST_ASSERT_EQUAL_UINT8(0x05, r.dev.vends[0].sub);
    TEST_ASSERT_EQUAL_UINT16(50, r.dev.vends[0].price);
    TEST_ASSERT_EQUAL_UINT16(9, r.dev.vends[0].item);
    TEST_ASSERT_FALSE(r.c.cash_sale(252, 9));  // not a multiple of the scale factor
    r.steps(2);
    TEST_ASSERT_EQUAL_INT(1, r.dev.n_vends);
}

static void test_price_scale_denied_locally(void) {
    Rig r;
    r.bring_up();
    r.dev.tap(200);
    r.steps(1);
    TEST_ASSERT_TRUE(r.c.vend_request(152, 3, "bad-1"));
    TEST_ASSERT_EQUAL(EventKind::VendDenied, r.sink.ev[1].kind);
    TEST_ASSERT_EQUAL_STRING("price_scale", r.sink.ev[1].detail);
    TEST_ASSERT_EQUAL_STRING("bad-1", r.sink.ev[1].request_id);
    r.steps(2);
    TEST_ASSERT_EQUAL_INT(0, r.dev.n_vends);  // never reached the bus
}

static void test_disable_and_enable(void) {
    Rig r;
    r.bring_up();
    r.c.set_enabled(false);
    r.steps(1);
    TEST_ASSERT_EQUAL_INT(2, r.dev.n_reader_cmds);
    TEST_ASSERT_EQUAL_UINT8(0x00, r.dev.reader_cmds[1]);
    TEST_ASSERT_EQUAL(Status::Disabled, r.c.state().status);
    TEST_ASSERT_EQUAL(EventKind::DeviceStatus, r.sink.ev[0].kind);
    TEST_ASSERT_EQUAL(Status::Disabled, r.sink.ev[0].status);
    r.c.set_enabled(true);
    r.steps(1);
    TEST_ASSERT_EQUAL_UINT8(0x01, r.dev.reader_cmds[2]);
    TEST_ASSERT_EQUAL(Status::Ready, r.c.state().status);
}

static void test_out_of_sequence_re_resets(void) {
    Rig r;
    r.bring_up();
    r.dev.tap(200);
    r.steps(1);
    const int resets = r.dev.calls[0];
    r.dev.out_of_sequence();
    r.steps(1);
    const PaymentEvent* e = r.sink.first(EventKind::DeviceStatus);
    TEST_ASSERT_NOT_NULL(e);
    TEST_ASSERT_EQUAL(Status::Error, e->status);
    TEST_ASSERT_EQUAL_INT16(0x0B, e->mdb_status);
    TEST_ASSERT_EQUAL(EventKind::SessionEnded, r.sink.ev[r.sink.n - 1].kind);
    r.steps(10);
    TEST_ASSERT_EQUAL_INT(resets + 1, r.dev.calls[0]);
    TEST_ASSERT_EQUAL(Cashless::Phase::Polling, r.c.phase());
    TEST_ASSERT_EQUAL(Status::Ready, r.c.state().status);
}

static void test_malfunction_reports_code(void) {
    Rig r;
    r.bring_up();
    r.dev.malfunction(0x42);
    r.steps(1);
    TEST_ASSERT_EQUAL_INT(1, r.sink.n);
    TEST_ASSERT_EQUAL(Status::Error, r.sink.ev[0].status);
    TEST_ASSERT_EQUAL_INT16(0x42, r.sink.ev[0].mdb_status);
}

static void test_reader_reset_mid_session(void) {
    Rig r;
    r.bring_up();
    r.dev.tap(200);
    r.steps(1);
    r.dev.just_reset();
    r.steps(1);
    TEST_ASSERT_EQUAL(EventKind::DeviceStatus, r.sink.ev[1].kind);
    TEST_ASSERT_EQUAL(Status::Busy, r.sink.ev[1].status);
    TEST_ASSERT_EQUAL(EventKind::SessionEnded, r.sink.ev[2].kind);
    r.steps(10);
    TEST_ASSERT_EQUAL(Cashless::Phase::Polling, r.c.phase());
}

static void test_miss_threshold_goes_offline_then_recovers(void) {
    Rig r;
    r.bring_up();
    r.dev.silent = true;
    r.steps(12);
    TEST_ASSERT_EQUAL(Status::Offline, r.c.state().status);
    TEST_ASSERT_FALSE(r.c.state().present);
    TEST_ASSERT_EQUAL_INT(1, r.sink.count(EventKind::DeviceStatus));
    r.dev.silent = false;
    r.clock.advance_ms(1100);
    r.steps(10);
    TEST_ASSERT_EQUAL(Status::Ready, r.c.state().status);
    TEST_ASSERT_TRUE(r.c.state().present);
}

static void test_offline_mid_session_ends_it(void) {
    Rig r;
    r.bring_up();
    r.dev.tap(200);
    r.steps(1);
    r.dev.silent = true;
    r.steps(12);
    TEST_ASSERT_EQUAL_INT(1, r.sink.count(EventKind::SessionEnded));
    TEST_ASSERT_EQUAL(SessionEndReason::Reader, r.sink.first(EventKind::SessionEnded)->end_reason);
}

static void test_money_event_survives_full_sink(void) {
    Rig r;
    r.bring_up();
    const int h0 = r.dev.head;
    r.sink.capacity = 0;
    r.dev.tap(200);
    r.steps(3);
    TEST_ASSERT_EQUAL_INT(0, r.sink.n);
    TEST_ASSERT_EQUAL_INT(h0 + 1, r.dev.head);  // one POLL consumed, none more while undelivered
    r.sink.capacity = 64;
    r.steps(1);
    TEST_ASSERT_EQUAL_INT(1, r.sink.n);
    TEST_ASSERT_EQUAL(EventKind::SessionBegun, r.sink.ev[0].kind);
    TEST_ASSERT_EQUAL_UINT32(0, r.c.dropped_events());
}

int main(int, char**) {
    UNITY_BEGIN();
    RUN_TEST(test_setup_ladder_and_request_id);
    RUN_TEST(test_tap_with_funds);
    RUN_TEST(test_tap_with_undisclosed_funds);
    RUN_TEST(test_vend_approved);
    RUN_TEST(test_vend_denied);
    RUN_TEST(test_success_ladder);
    RUN_TEST(test_failure_ladder);
    RUN_TEST(test_vend_result_needs_approved_vend);
    RUN_TEST(test_end_session_by_edge);
    RUN_TEST(test_reader_cancel_with_pending_request);
    RUN_TEST(test_reader_cancel_without_vend);
    RUN_TEST(test_vend_result_timeout);
    RUN_TEST(test_vend_request_without_session);
    RUN_TEST(test_always_idle_negotiated);
    RUN_TEST(test_always_idle_not_wanted);
    RUN_TEST(test_always_idle_not_offered);
    RUN_TEST(test_cash_sale);
    RUN_TEST(test_price_scale_denied_locally);
    RUN_TEST(test_disable_and_enable);
    RUN_TEST(test_out_of_sequence_re_resets);
    RUN_TEST(test_malfunction_reports_code);
    RUN_TEST(test_reader_reset_mid_session);
    RUN_TEST(test_miss_threshold_goes_offline_then_recovers);
    RUN_TEST(test_offline_mid_session_ends_it);
    RUN_TEST(test_money_event_survives_full_sink);
    return UNITY_END();
}
