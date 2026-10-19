#include <string.h>
#include <unity.h>

#include "../support/fake_changer.h"
#include "../support/fake_reader.h"
#include "../support/fake_validator.h"
#include "../support/fakes.h"
#include "master.h"

using namespace mdbcore;
using boardcore::Ack;
using boardcore::AckStatus;
using boardcore::Phase;

void setUp(void) {}
void tearDown(void) {}

namespace {

struct Sink : EventSink {
    PaymentEvent ev[256];
    int n = 0;
    int capacity = 256;
    bool emit(const PaymentEvent& e) override {
        if (n >= capacity || n >= 256) return false;
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
    const PaymentEvent* last(EventKind k) const {
        for (int i = n - 1; i >= 0; --i)
            if (ev[i].kind == k) return &ev[i];
        return nullptr;
    }
};

const PaymentEvent* need(const PaymentEvent* e) {
    TEST_ASSERT_NOT_NULL(e);
    return e;
}

struct Rig {
    FakeClock clock;
    FakeBus bus{clock};
    FakeChanger coin;
    FakeValidator bill;
    FakeReader rdr;
    Link link{bus, clock};
    Sink sink;
    FakeIds ids;
    MasterTap tap;
    Changer ch{link, clock, tap};
    Validator va{link, clock, tap, tap};
    Cashless cl{link, clock, tap, ids};
    Accounts acc;
    EdgeWatch edge{clock};
    Master m{link, clock, ids, sink, ch, va, cl, acc, edge};
    int seq = 0;

    Rig() {
        bus.attach(0x08, &coin);
        bus.attach(0x30, &bill);
        bus.attach(0x10, &rdr);
        tap.bind(m);
    }

    void steps(int k) {
        for (int i = 0; i < k; ++i) m.step();
    }
    // Run all three init ladders; init events and the dirty flag are discarded.
    void bring_up() {
        steps(40);
        sink.n = 0;
        m.take_state_dirty();
    }

    Command cmd(CmdKind kind, const char* name) {
        Command c;
        memset(&c, 0, sizeof(c));
        c.kind = kind;
        c.timestamp_unix = -1;
        snprintf(c.request_id, sizeof(c.request_id), "req-%08d", ++seq);
        snprintf(c.name, sizeof(c.name), "%s", name);
        return c;
    }
    Command policy(bool accept = true, bool cashless = true) {
        Command c = cmd(CmdKind::SetPolicy, "set_policy");
        Policy& p = c.policy;
        snprintf(p.policy_id, sizeof(p.policy_id), "p-00000001");
        p.accept = accept;
        p.max_escrow_cents = 5000;
        const uint32_t coins[4] = {5, 10, 25, 100};
        const uint32_t bills[4] = {100, 500, 1000, 2000};
        memcpy(p.coin_enabled, coins, sizeof(coins));
        p.n_coin = 4;
        memcpy(p.bill_enabled, bills, sizeof(bills));
        p.n_bill = 4;
        p.cashless_enabled = cashless;
        p.change_threshold_cents = 0;
        p.edge_timeout_s = 30;
        p.return_bill_on_lever = false;
        return c;
    }
    Ack set_policy(bool accept = true, bool cashless = true) { return m.submit(policy(accept, cashless)); }
    Ack payout(uint32_t cents, const char* id = nullptr) {
        Command c = cmd(CmdKind::Payout, "payout");
        c.cents = cents;
        if (id) snprintf(c.request_id, sizeof(c.request_id), "%s", id);
        return m.submit(c);
    }
    Ack cash_sale(uint32_t cents) {
        Command c = cmd(CmdKind::CashSale, "cash_sale");
        c.cents = cents;
        return m.submit(c);
    }
    Ack vend(uint32_t cents, const char* session = nullptr, const char* id = nullptr) {
        Command c = cmd(CmdKind::VendRequest, "vend_request");
        c.cents = cents;
        snprintf(c.item, sizeof(c.item), "3");
        if (session) {
            c.has_session_id = true;
            snprintf(c.session_id, sizeof(c.session_id), "%s", session);
        }
        if (id) snprintf(c.request_id, sizeof(c.request_id), "%s", id);
        return m.submit(c);
    }
    Ack vend_result(const char* session, const char* req, bool success) {
        Command c = cmd(CmdKind::VendResult, "vend_result");
        c.has_session_id = true;
        snprintf(c.session_id, sizeof(c.session_id), "%s", session);
        snprintf(c.vend_request_id, sizeof(c.vend_request_id), "%s", req);
        c.success = success;
        return m.submit(c);
    }
    Ack simple(CmdKind k, const char* name) { return m.submit(cmd(k, name)); }

    // Starts a tap and returns its session id.
    const char* tap_session(uint16_t funds_units = 0xFFFF) {
        rdr.tap(funds_units);
        steps(3);
        const PaymentEvent* e = sink.last(EventKind::SessionBegun);
        return e ? e->session_id : nullptr;
    }
    bool reader_saw(uint8_t sub) const {
        for (int i = 0; i < rdr.n_vends; ++i)
            if (rdr.vends[i].sub == sub) return true;
        return false;
    }
};

}  // namespace

// ---------------------------------------------------------------- boot and policy

static void test_boot_without_policy_disables_everything(void) {
    Rig r;
    r.bring_up();
    TEST_ASSERT_EQUAL(Inhibit::NoPolicy, r.m.inhibit());
    TEST_ASSERT_TRUE(r.coin.n_masks > 0);
    TEST_ASSERT_EQUAL_UINT16(0, r.coin.coin_masks[r.coin.n_masks - 1]);
    TEST_ASSERT_EQUAL_UINT16(0, r.bill.bill_mask);
    TEST_ASSERT_FALSE(r.rdr.reader_enabled);
    PaymentState s;
    r.m.snapshot(s);
    TEST_ASSERT_NULL(s.policy_id);
    TEST_ASSERT_FALSE(s.accepting);
    TEST_ASSERT_EQUAL(Inhibit::NoPolicy, s.inhibited);
}

static void test_set_policy_acks_ok_after_enables_on_the_bus(void) {
    Rig r;
    r.bring_up();
    const Ack a = r.set_policy();
    TEST_ASSERT_EQUAL(AckStatus::Ok, a.status);
    TEST_ASSERT_EQUAL(Phase::Completed, a.phase);
    TEST_ASSERT_EQUAL_STRING("set_policy", a.command);
    // By the time the ack exists, every device was told, on the bus.
    TEST_ASSERT_EQUAL_UINT16(0x000F, r.coin.coin_masks[r.coin.n_masks - 1]);
    TEST_ASSERT_EQUAL_UINT16(0x000F, r.bill.bill_mask);
    TEST_ASSERT_TRUE(r.rdr.reader_enabled);
    TEST_ASSERT_EQUAL(Inhibit::None, r.m.inhibit());
    PaymentState s;
    r.m.snapshot(s);
    TEST_ASSERT_EQUAL_STRING("p-00000001", s.policy_id);
    TEST_ASSERT_TRUE(s.accepting);
    TEST_ASSERT_TRUE(r.m.take_state_dirty());
    Ack none;
    TEST_ASSERT_FALSE(r.m.poll_completion(none));
}

static void test_set_policy_accept_false_is_policy_off(void) {
    Rig r;
    r.bring_up();
    r.set_policy();
    const Ack a = r.set_policy(false);
    TEST_ASSERT_EQUAL(AckStatus::Ok, a.status);
    TEST_ASSERT_EQUAL(Inhibit::PolicyOff, r.m.inhibit());
    TEST_ASSERT_EQUAL_UINT16(0, r.coin.coin_masks[r.coin.n_masks - 1]);
    TEST_ASSERT_EQUAL_UINT16(0, r.bill.bill_mask);
    TEST_ASSERT_FALSE(r.rdr.reader_enabled);
}

static void test_set_policy_validates_and_keeps_state(void) {
    Rig r;
    r.bring_up();
    Command c = r.policy();
    c.policy.edge_timeout_s = 3;
    Ack a = r.m.submit(c);
    TEST_ASSERT_EQUAL(AckStatus::Rejected, a.status);
    TEST_ASSERT_EQUAL_STRING("edge_timeout_s", a.detail);
    TEST_ASSERT_EQUAL(Inhibit::NoPolicy, r.m.inhibit());
    c = r.policy();
    c.policy.policy_id[0] = '\0';
    TEST_ASSERT_EQUAL(AckStatus::Rejected, r.m.submit(c).status);
    TEST_ASSERT_EQUAL(Inhibit::NoPolicy, r.m.inhibit());
}

static void test_set_policy_does_not_wait_for_an_absent_reader(void) {
    Rig r;
    r.rdr.silent = true;
    r.steps(80);  // the reader never answers: offline, not present
    const Ack a = r.set_policy();
    TEST_ASSERT_EQUAL(AckStatus::Ok, a.status);
    TEST_ASSERT_EQUAL(Inhibit::None, r.m.inhibit());
    TEST_ASSERT_EQUAL_UINT16(0x000F, r.coin.coin_masks[r.coin.n_masks - 1]);
}

static void test_basic_commands(void) {
    Rig r;
    r.bring_up();
    TEST_ASSERT_EQUAL(AckStatus::Ok, r.simple(CmdKind::Ping, "ping").status);
    TEST_ASSERT_EQUAL(AckStatus::Unsupported, r.simple(CmdKind::Unknown, "bogus").status);
    r.m.take_state_dirty();
    TEST_ASSERT_EQUAL(AckStatus::Ok, r.simple(CmdKind::ForceReport, "force_report").status);
    TEST_ASSERT_TRUE(r.m.take_state_dirty());
}

// ---------------------------------------------------------------- reference flows

static void test_flow1_coins_then_cash_sale_then_change(void) {
    Rig r;
    r.bring_up();
    r.set_policy();
    r.coin.deposit(2, 1);
    r.coin.deposit(2, 1);
    r.steps(6);
    TEST_ASSERT_EQUAL_INT(2, r.sink.count(EventKind::CoinAccepted));
    TEST_ASSERT_EQUAL_UINT32(50, r.acc.escrow_cents());

    const Ack s = r.cash_sale(35);
    TEST_ASSERT_EQUAL(AckStatus::Ok, s.status);
    TEST_ASSERT_EQUAL_STRING("", s.detail);
    TEST_ASSERT_EQUAL_UINT32(15, r.acc.escrow_cents());
    r.steps(4);
    TEST_ASSERT_TRUE(r.reader_saw(0x05));  // MDB CASH SALE to the reader
    TEST_ASSERT_EQUAL_UINT16(7, r.rdr.vends[r.rdr.n_vends - 1].price);

    const Ack p = r.payout(15);
    TEST_ASSERT_EQUAL(AckStatus::Ok, p.status);
    TEST_ASSERT_EQUAL(Phase::Accepted, p.phase);
    r.steps(8);
    const PaymentEvent* res = r.sink.first(EventKind::PayoutResult);
    TEST_ASSERT_NOT_NULL(res);
    TEST_ASSERT_EQUAL(PayoutStatus::Ok, res->payout_status);
    TEST_ASSERT_EQUAL_UINT32(15, res->paid_cents);
    TEST_ASSERT_EQUAL_UINT32(0, r.acc.escrow_cents());
}

static void test_flow2_bill_price_first(void) {
    Rig r;
    r.bring_up();
    r.set_policy();
    r.bill.insert(1);  // $5
    r.steps(8);
    TEST_ASSERT_EQUAL_INT(1, r.sink.count(EventKind::BillEscrowed));
    TEST_ASSERT_EQUAL_INT(1, r.sink.count(EventKind::BillStacked));
    TEST_ASSERT_EQUAL_UINT32(500, r.acc.escrow_cents());
    uint32_t c = 0;
    TEST_ASSERT_FALSE(r.acc.bill_in_escrow(c));
    PaymentState s;
    r.m.snapshot(s);
    TEST_ASSERT_EQUAL_UINT32(500, s.escrow_cents);
    TEST_ASSERT_FALSE(s.bill_in_escrow);
}

static void test_flow3_cashless_tap_first_to_session_complete(void) {
    Rig r;
    r.bring_up();
    r.set_policy();
    const char* sid = r.tap_session(80);
    TEST_ASSERT_NOT_NULL(sid);
    char session[65];
    snprintf(session, sizeof(session), "%s", sid);
    PaymentState s;
    r.m.snapshot(s);
    TEST_ASSERT_TRUE(s.session_open);
    TEST_ASSERT_EQUAL_STRING(session, s.session_id);
    TEST_ASSERT_TRUE(s.funds_known);
    TEST_ASSERT_EQUAL_UINT32(400, s.funds_cents);
    TEST_ASSERT_NOT_NULL(s.session_started_ts);

    r.rdr.approve(25);
    const Ack a = r.vend(25, session, "vend-0000001");
    TEST_ASSERT_EQUAL(AckStatus::Ok, a.status);
    TEST_ASSERT_EQUAL(Phase::Accepted, a.phase);
    TEST_ASSERT_TRUE(r.reader_saw(0x00));  // accepted only because the reader ACKed VEND REQUEST
    TEST_ASSERT_EQUAL_UINT16(5, r.rdr.vends[0].price);
    TEST_ASSERT_EQUAL_UINT16(3, r.rdr.vends[0].item);
    r.steps(3);
    const PaymentEvent* ap = r.sink.first(EventKind::VendApproved);
    TEST_ASSERT_NOT_NULL(ap);
    TEST_ASSERT_EQUAL_UINT32(25, ap->cents);
    TEST_ASSERT_EQUAL_STRING("vend-0000001", ap->request_id);

    const Ack vr = r.vend_result(session, "vend-0000001", true);
    TEST_ASSERT_EQUAL(AckStatus::Ok, vr.status);
    r.steps(5);
    TEST_ASSERT_TRUE(r.reader_saw(0x02));
    TEST_ASSERT_TRUE(r.reader_saw(0x04));
    const PaymentEvent* end = r.sink.first(EventKind::SessionEnded);
    TEST_ASSERT_NOT_NULL(end);
    TEST_ASSERT_EQUAL(SessionEndReason::Complete, end->end_reason);
    r.m.snapshot(s);
    TEST_ASSERT_FALSE(s.session_open);
    TEST_ASSERT_NULL(s.session_started_ts);
}

static void test_flow4_price_first_without_always_idle_is_no_session(void) {
    Rig r;
    r.bring_up();
    r.set_policy();
    const Ack a = r.vend(25);
    TEST_ASSERT_EQUAL(AckStatus::Rejected, a.status);
    TEST_ASSERT_EQUAL_STRING("no_session", a.detail);
    TEST_ASSERT_EQUAL_INT(0, r.rdr.n_vends);
}

static void test_flow4_price_first_with_always_idle_continues_as_flow3(void) {
    Rig r;
    r.rdr.level = 3;
    r.rdr.offer_always_idle = true;
    r.bring_up();
    r.set_policy();  // first wants always-idle: the reader's ladder reruns
    r.steps(40);
    TEST_ASSERT_TRUE(r.cl.always_idle());
    r.rdr.approve(25);
    const Ack a = r.vend(25);
    TEST_ASSERT_EQUAL(AckStatus::Ok, a.status);
    TEST_ASSERT_EQUAL(Phase::Accepted, a.phase);
    r.steps(4);
    TEST_ASSERT_EQUAL_INT(1, r.sink.count(EventKind::VendApproved));
}

static void test_flow5_cashless_failure_charges_nothing(void) {
    Rig r;
    r.bring_up();
    r.set_policy();
    char session[65];
    snprintf(session, sizeof(session), "%s", r.tap_session());
    r.rdr.approve(25);
    r.vend(25, session, "vend-0000002");
    r.steps(3);
    const Ack vr = r.vend_result(session, "vend-0000002", false);
    TEST_ASSERT_EQUAL(AckStatus::Ok, vr.status);
    r.steps(5);
    TEST_ASSERT_TRUE(r.reader_saw(0x03));  // VEND FAILURE
    TEST_ASSERT_FALSE(r.reader_saw(0x02));
    TEST_ASSERT_EQUAL(SessionEndReason::Complete, need(r.sink.first(EventKind::SessionEnded))->end_reason);
    TEST_ASSERT_EQUAL_UINT32(0, r.acc.escrow_cents());
}

static void test_flow6_cash_vend_failed_pays_back_everything(void) {
    Rig r;
    r.bring_up();
    r.set_policy();
    r.coin.deposit(2, 1);
    r.coin.deposit(2, 1);
    r.coin.deposit(2, 1);
    r.steps(8);
    TEST_ASSERT_EQUAL_UINT32(75, r.acc.escrow_cents());
    const Ack p = r.payout(75);
    TEST_ASSERT_EQUAL(Phase::Accepted, p.phase);
    r.steps(8);
    TEST_ASSERT_EQUAL_UINT32(75, need(r.sink.first(EventKind::PayoutResult))->paid_cents);
    TEST_ASSERT_EQUAL_UINT32(0, r.acc.escrow_cents());
}

static void test_flow7_edge_silence_sequence(void) {
    Rig r;
    r.bring_up();
    Command pc = r.policy();
    pc.policy.edge_timeout_s = 5;
    TEST_ASSERT_EQUAL(AckStatus::Ok, r.m.submit(pc).status);
    r.coin.deposit(2, 1);
    r.steps(4);
    TEST_ASSERT_EQUAL_UINT32(25, r.acc.escrow_cents());
    char session[65];
    snprintf(session, sizeof(session), "%s", r.tap_session());
    r.bill.insert(1);
    r.steps(1);  // the bill is now held, its verdict still pending
    uint32_t held = 0;
    TEST_ASSERT_TRUE(r.va.bill_in_escrow(held));
    r.sink.n = 0;

    r.clock.advance_ms(6000);
    r.steps(8);
    TEST_ASSERT_EQUAL(Inhibit::EdgeSilent, r.m.inhibit());
    const PaymentEvent* ret = r.sink.first(EventKind::BillReturned);
    TEST_ASSERT_NOT_NULL(ret);
    TEST_ASSERT_EQUAL(BillReturnReason::EdgeSilent, ret->bill_reason);
    TEST_ASSERT_EQUAL_INT(0, r.sink.count(EventKind::BillStacked));
    const PaymentEvent* end = r.sink.first(EventKind::SessionEnded);
    TEST_ASSERT_NOT_NULL(end);
    TEST_ASSERT_EQUAL(SessionEndReason::Edge, end->end_reason);
    TEST_ASSERT_EQUAL_UINT16(0, r.coin.coin_masks[r.coin.n_masks - 1]);
    TEST_ASSERT_EQUAL_UINT16(0, r.bill.bill_mask);
    TEST_ASSERT_FALSE(r.rdr.reader_enabled);
    TEST_ASSERT_EQUAL_UINT32(25, r.acc.escrow_cents());  // coins already taken are still owed
    PaymentState s;
    r.m.snapshot(s);
    TEST_ASSERT_FALSE(s.accepting);
    TEST_ASSERT_EQUAL(Inhibit::EdgeSilent, s.inhibited);

    // Traffic alone does not lift it; and the edge can still pay the owed coins out.
    r.m.edge_seen();
    r.simple(CmdKind::Ping, "ping");
    r.steps(3);
    TEST_ASSERT_EQUAL(Inhibit::EdgeSilent, r.m.inhibit());
    const Ack p = r.payout(25);
    TEST_ASSERT_EQUAL(AckStatus::Ok, p.status);
    TEST_ASSERT_EQUAL(Phase::Accepted, p.phase);
    // Only an acked set_policy resumes acceptance.
    TEST_ASSERT_EQUAL(AckStatus::Ok, r.set_policy().status);
    TEST_ASSERT_EQUAL(Inhibit::None, r.m.inhibit());
    TEST_ASSERT_EQUAL_UINT16(0x000F, r.bill.bill_mask);
}

static void test_silence_waits_out_an_outstanding_vend_then_driver_timeout_closes(void) {
    Rig r;
    r.bring_up();
    Command pc = r.policy();
    pc.policy.edge_timeout_s = 5;
    r.m.submit(pc);
    char session[65];
    snprintf(session, sizeof(session), "%s", r.tap_session());
    r.rdr.approve(25);
    r.vend(25, session, "vend-0000003");
    r.steps(3);
    r.sink.n = 0;
    r.clock.advance_ms(6000);
    r.steps(10);  // end_session() is refused while a vend is approved, the driver's own timeout answers
    const PaymentEvent* end = r.sink.first(EventKind::SessionEnded);
    TEST_ASSERT_NOT_NULL(end);
    TEST_ASSERT_EQUAL(SessionEndReason::Timeout, end->end_reason);
    TEST_ASSERT_TRUE(r.reader_saw(0x03));
}

static void test_rule6_missing_vend_result_fails_the_vend_and_times_out(void) {
    Rig r;
    r.bring_up();
    Command pc = r.policy();
    pc.policy.edge_timeout_s = 5;
    r.m.submit(pc);
    char session[65];
    snprintf(session, sizeof(session), "%s", r.tap_session());
    r.rdr.approve(25);
    r.vend(25, session, "vend-0000004");
    r.steps(3);
    TEST_ASSERT_EQUAL_INT(1, r.sink.count(EventKind::VendApproved));
    for (int i = 0; i < 8; ++i) {
        r.clock.advance_ms(1000);
        r.m.edge_seen();  // the edge is alive, only its vend_result is missing
        r.steps(2);
    }
    TEST_ASSERT_TRUE(r.reader_saw(0x03));
    TEST_ASSERT_EQUAL(SessionEndReason::Timeout, need(r.sink.first(EventKind::SessionEnded))->end_reason);
    TEST_ASSERT_EQUAL(Inhibit::None, r.m.inhibit());
    // The late vend_result finds nothing to settle.
    TEST_ASSERT_EQUAL(AckStatus::Rejected, r.vend_result(session, "vend-0000004", true).status);
}

// ---------------------------------------------------------------- escrow judge

static void test_judge_without_policy_and_when_off(void) {
    Rig r;
    r.bring_up();
    EscrowVerdict v = r.m.judge(500);
    TEST_ASSERT_FALSE(v.stack);
    TEST_ASSERT_EQUAL(BillReturnReason::Policy, v.reason);
    r.set_policy();
    TEST_ASSERT_TRUE(r.m.judge(500).stack);
    r.set_policy(false);
    v = r.m.judge(500);
    TEST_ASSERT_FALSE(v.stack);
    TEST_ASSERT_EQUAL(BillReturnReason::Policy, v.reason);
}

static void test_judge_returns_bill_that_would_exceed_escrow_cap(void) {
    Rig r;
    r.bring_up();
    Command pc = r.policy();
    pc.policy.max_escrow_cents = 1000;
    r.m.submit(pc);
    r.acc.coin(600);
    r.bill.insert(1);  // $5: 600 + 500 > 1000
    r.steps(8);
    const PaymentEvent* ret = r.sink.first(EventKind::BillReturned);
    TEST_ASSERT_NOT_NULL(ret);
    TEST_ASSERT_EQUAL(BillReturnReason::Policy, ret->bill_reason);
    TEST_ASSERT_EQUAL_UINT32(600, r.acc.escrow_cents());
    // Coins are never refused for the cap: they cannot be given back.
    r.coin.deposit(3, 0);
    r.steps(4);
    TEST_ASSERT_EQUAL_UINT32(700, r.acc.escrow_cents());
}

static void test_judge_exact_change_returns_bills_above_threshold(void) {
    Rig r;
    r.coin.tubes[0] = r.coin.tubes[1] = r.coin.tubes[2] = 1;  // 40 cents payable
    r.bring_up();
    Command pc = r.policy();
    pc.policy.change_threshold_cents = 1000;
    r.m.submit(pc);
    PaymentState s;
    r.m.snapshot(s);
    TEST_ASSERT_TRUE(s.exact_change);
    r.bill.insert(3);  // $20 > threshold
    r.steps(8);
    TEST_ASSERT_EQUAL(BillReturnReason::Policy, need(r.sink.first(EventKind::BillReturned))->bill_reason);
    TEST_ASSERT_EQUAL_INT(0, r.sink.count(EventKind::BillStacked));
    r.bill.insert(1);  // $5 <= threshold is fine
    r.steps(8);
    TEST_ASSERT_EQUAL_INT(1, r.sink.count(EventKind::BillStacked));
    TEST_ASSERT_EQUAL_UINT32(500, r.acc.escrow_cents());
}

static void test_judge_reports_edge_silent_once_silent(void) {
    Rig r;
    r.bring_up();
    Command pc = r.policy();
    pc.policy.edge_timeout_s = 5;
    r.m.submit(pc);
    r.clock.advance_ms(6000);
    r.steps(2);
    const EscrowVerdict v = r.m.judge(500);
    TEST_ASSERT_FALSE(v.stack);
    TEST_ASSERT_EQUAL(BillReturnReason::EdgeSilent, v.reason);
}

static void test_unbound_tap_refuses_events_and_bills(void) {
    MasterTap t;
    const PaymentEvent e = make_event(EventKind::CoinAccepted, Device::Coin);
    TEST_ASSERT_FALSE(t.emit(e));
    TEST_ASSERT_FALSE(t.judge(100).stack);
}

// ---------------------------------------------------------------- lever

static void test_lever_returns_escrowed_bill_when_policy_asks(void) {
    Rig r;
    r.bring_up();
    Command pc = r.policy();
    pc.policy.return_bill_on_lever = true;
    r.m.submit(pc);
    r.bill.insert(1);
    r.steps(1);  // held
    r.coin.lever();
    r.steps(6);
    TEST_ASSERT_EQUAL_INT(1, r.sink.count(EventKind::EscrowReturnPressed));
    const PaymentEvent* ret = r.sink.first(EventKind::BillReturned);
    TEST_ASSERT_NOT_NULL(ret);
    TEST_ASSERT_EQUAL(BillReturnReason::Customer, ret->bill_reason);
    TEST_ASSERT_EQUAL_INT(0, r.sink.count(EventKind::BillStacked));
    TEST_ASSERT_EQUAL_UINT32(0, r.acc.escrow_cents());
}

static void test_lever_without_flag_only_reports(void) {
    Rig r;
    r.bring_up();
    r.set_policy();
    r.bill.insert(1);
    r.steps(1);
    r.coin.lever();
    r.steps(8);
    TEST_ASSERT_EQUAL_INT(1, r.sink.count(EventKind::EscrowReturnPressed));
    TEST_ASSERT_EQUAL_INT(0, r.sink.count(EventKind::BillReturned));
    TEST_ASSERT_EQUAL_INT(1, r.sink.count(EventKind::BillStacked));
}

// ---------------------------------------------------------------- payout

static void test_payout_acked_accepted_only_after_changer_acked_the_bus_command(void) {
    Rig r;
    r.bring_up();
    r.set_policy();
    TEST_ASSERT_EQUAL_INT(0, r.coin.n_payouts);
    const Ack a = r.payout(15);
    TEST_ASSERT_EQUAL(AckStatus::Ok, a.status);
    TEST_ASSERT_EQUAL(Phase::Accepted, a.phase);
    TEST_ASSERT_EQUAL_INT(1, r.coin.n_payouts);  // the PAYOUT reached the changer before the ack existed
    TEST_ASSERT_EQUAL_UINT8(3, r.coin.payouts[0]);
}

static void test_payout_changer_refusal_is_a_failed_ack(void) {
    Rig r;
    r.bring_up();
    r.set_policy();
    r.coin.fail_payout = true;  // the changer NAKs the PAYOUT command
    const Ack a = r.payout(15);
    TEST_ASSERT_EQUAL(AckStatus::Failed, a.status);
    TEST_ASSERT_EQUAL(Phase::Completed, a.phase);
    TEST_ASSERT_EQUAL_STRING("no coins paid", a.detail);
    TEST_ASSERT_EQUAL_UINT8(PayoutStatus::Failed, need(r.sink.first(EventKind::PayoutResult))->payout_status);
}

// A command the driver still holds is never answered `failed` (rule 5: failed is final).
static void test_payout_still_queued_when_changer_is_silent_is_accepted(void) {
    Rig r;
    r.bring_up();
    r.set_policy();
    r.coin.silent = true;
    const Ack a = r.payout(15, "pay-00000030");
    TEST_ASSERT_EQUAL(AckStatus::Ok, a.status);
    TEST_ASSERT_EQUAL(Phase::Accepted, a.phase);
    TEST_ASSERT_EQUAL_INT(0, r.coin.n_payouts);
    // The same-id retry while the first is still queued: accepted, not failed, not busy.
    const Ack again = r.payout(15, "pay-00000030");
    TEST_ASSERT_EQUAL(AckStatus::Ok, again.status);
    TEST_ASSERT_EQUAL(Phase::Accepted, again.phase);
    // The changer comes back: the one queued payout runs exactly once.
    r.coin.silent = false;
    r.clock.advance_ms(1500);
    r.steps(20);
    TEST_ASSERT_EQUAL_INT(1, r.coin.n_payouts);
    TEST_ASSERT_EQUAL_INT(1, r.sink.count(EventKind::PayoutResult));
}

static void test_payout_retry_while_in_flight_is_accepted_not_busy(void) {
    Rig r;
    r.bring_up();
    r.set_policy();
    r.coin.busy_polls = 50;
    TEST_ASSERT_EQUAL(Phase::Accepted, r.payout(15, "pay-00000031").phase);
    const Ack again = r.payout(15, "pay-00000031");
    TEST_ASSERT_EQUAL(AckStatus::Ok, again.status);
    TEST_ASSERT_EQUAL(Phase::Accepted, again.phase);
    TEST_ASSERT_EQUAL_INT(1, r.coin.n_payouts);
}

static void test_payout_result_held_by_full_sink_retry_is_accepted_then_replays(void) {
    Rig r;
    r.bring_up();
    r.set_policy();
    r.acc.coin(100);
    r.sink.capacity = r.sink.n;  // nothing more gets out
    TEST_ASSERT_EQUAL(Phase::Accepted, r.payout(15, "pay-00000032").phase);
    r.steps(8);  // the payout ends; its PayoutResult is held inside the changer
    TEST_ASSERT_EQUAL_INT(0, r.sink.count(EventKind::PayoutResult));
    Ack again = r.payout(15, "pay-00000032");
    TEST_ASSERT_EQUAL(AckStatus::Ok, again.status);
    TEST_ASSERT_EQUAL(Phase::Accepted, again.phase);
    TEST_ASSERT_EQUAL_INT(1, r.coin.n_payouts);
    r.sink.capacity = 256;
    r.steps(3);
    TEST_ASSERT_EQUAL_INT(1, r.sink.count(EventKind::PayoutResult));
    TEST_ASSERT_EQUAL_UINT32(85, r.acc.escrow_cents());
    again = r.payout(15, "pay-00000032");  // now the stored result is replayed
    TEST_ASSERT_EQUAL(AckStatus::Ok, again.status);
    TEST_ASSERT_EQUAL_STRING("replay", again.detail);
    TEST_ASSERT_EQUAL_INT(2, r.sink.count(EventKind::PayoutResult));
    TEST_ASSERT_EQUAL_INT(1, r.coin.n_payouts);
    TEST_ASSERT_EQUAL_UINT32(85, r.acc.escrow_cents());
}

static void test_payout_the_changer_cannot_express_is_chunked_or_unpayable(void) {
    Rig r;
    r.coin.tubes[2] = 200;
    r.bring_up();
    r.set_policy();
    // Above 255 scaled units: accepted, paid in two chunks.
    Ack a = r.payout(1500, "pay-00000033");
    TEST_ASSERT_EQUAL(AckStatus::Ok, a.status);
    TEST_ASSERT_EQUAL(Phase::Accepted, a.phase);
    r.steps(40);
    TEST_ASSERT_EQUAL_INT(2, r.coin.n_payouts);
    TEST_ASSERT_EQUAL_UINT32(1500, need(r.sink.last(EventKind::PayoutResult))->paid_cents);
    // Not a multiple of the scaling: floor paid, partial, never refused.
    a = r.payout(1502, "pay-00000034");
    TEST_ASSERT_EQUAL(AckStatus::Ok, a.status);
    r.steps(40);
    const PaymentEvent* e = need(r.sink.last(EventKind::PayoutResult));
    TEST_ASSERT_EQUAL(PayoutStatus::Partial, e->payout_status);
    TEST_ASSERT_EQUAL_UINT32(1500, e->paid_cents);
    TEST_ASSERT_EQUAL_STRING("scaling", e->detail);
    // Less than one unit: genuinely impossible, and not reported as busy.
    a = r.payout(3, "pay-00000035");
    TEST_ASSERT_EQUAL(AckStatus::Rejected, a.status);
    TEST_ASSERT_EQUAL_STRING("unpayable", a.detail);
}

static void test_payout_rejections(void) {
    Rig r;
    r.bring_up();
    r.set_policy();
    Ack a = r.payout(0);
    TEST_ASSERT_EQUAL(AckStatus::Rejected, a.status);
    r.coin.busy_polls = 100;
    TEST_ASSERT_EQUAL(Phase::Accepted, r.payout(15, "pay-00000001").phase);
    a = r.payout(15, "pay-00000002");
    TEST_ASSERT_EQUAL(AckStatus::Rejected, a.status);
    TEST_ASSERT_EQUAL_STRING("busy", a.detail);
    TEST_ASSERT_EQUAL_INT(1, r.coin.n_payouts);
}

static void test_payout_retry_of_running_payout_does_not_pay_twice(void) {
    Rig r;
    r.bring_up();
    r.set_policy();
    r.coin.busy_polls = 3;
    TEST_ASSERT_EQUAL(Phase::Accepted, r.payout(15, "pay-00000009").phase);
    const Ack again = r.payout(15, "pay-00000009");
    TEST_ASSERT_EQUAL(AckStatus::Ok, again.status);
    TEST_ASSERT_EQUAL(Phase::Accepted, again.phase);
    TEST_ASSERT_EQUAL_STRING("in_progress", again.detail);
    r.steps(12);
    TEST_ASSERT_EQUAL_INT(1, r.coin.n_payouts);
    TEST_ASSERT_EQUAL_INT(1, r.sink.count(EventKind::PayoutResult));
}

static void test_payout_idempotent_replay_after_completion(void) {
    Rig r;
    r.bring_up();
    r.set_policy();
    r.acc.coin(100);
    r.payout(15, "pay-00000010");
    r.steps(8);
    TEST_ASSERT_EQUAL_UINT32(85, r.acc.escrow_cents());
    TEST_ASSERT_EQUAL_INT(1, r.sink.count(EventKind::PayoutResult));
    const Ack again = r.payout(15, "pay-00000010");
    TEST_ASSERT_EQUAL(AckStatus::Ok, again.status);
    TEST_ASSERT_EQUAL(Phase::Accepted, again.phase);
    TEST_ASSERT_EQUAL_STRING("replay", again.detail);
    TEST_ASSERT_EQUAL_INT(2, r.sink.count(EventKind::PayoutResult));
    const PaymentEvent* e = r.sink.last(EventKind::PayoutResult);
    TEST_ASSERT_EQUAL_STRING("pay-00000010", e->request_id);
    TEST_ASSERT_EQUAL_UINT32(15, e->paid_cents);
    TEST_ASSERT_EQUAL_INT(1, r.coin.n_payouts);          // no second payout on the bus
    TEST_ASSERT_EQUAL_UINT32(85, r.acc.escrow_cents());  // and no second deduction
}

static void test_payout_replay_survives_a_full_sink(void) {
    Rig r;
    r.bring_up();
    r.set_policy();
    r.payout(15, "pay-00000011");
    r.steps(8);
    const int before = r.sink.n;
    r.sink.capacity = before;
    TEST_ASSERT_EQUAL(Phase::Accepted, r.payout(15, "pay-00000011").phase);
    TEST_ASSERT_EQUAL_INT(before, r.sink.n);
    r.sink.capacity = 256;
    r.steps(2);
    TEST_ASSERT_EQUAL_INT(before + 1, r.sink.n);
}

static void test_payout_expiry_uses_wall_time(void) {
    Rig r;
    r.bring_up();
    r.set_policy();
    // No wall time known: the rule is skipped.
    Command c = r.cmd(CmdKind::Payout, "payout");
    c.cents = 5;
    c.timestamp_unix = 5;
    TEST_ASSERT_EQUAL(AckStatus::Ok, r.m.submit(c).status);
    r.steps(8);

    r.m.set_wall_time(1000000);
    c = r.cmd(CmdKind::Payout, "payout");
    c.cents = 5;
    c.timestamp_unix = 1000000 - 31;  // edge_timeout_s is 30
    Ack a = r.m.submit(c);
    TEST_ASSERT_EQUAL(AckStatus::Rejected, a.status);
    TEST_ASSERT_EQUAL_STRING("expired", a.detail);
    TEST_ASSERT_EQUAL_INT(1, r.coin.n_payouts);
    c = r.cmd(CmdKind::Payout, "payout");
    c.cents = 5;
    c.timestamp_unix = 1000000 - 29;
    TEST_ASSERT_EQUAL(AckStatus::Ok, r.m.submit(c).status);
    // vend_request obeys the same rule (checked before anything else).
    c = r.cmd(CmdKind::VendRequest, "vend_request");
    c.cents = 25;
    c.timestamp_unix = 1000000 - 100;
    snprintf(c.item, sizeof(c.item), "1");
    a = r.m.submit(c);
    TEST_ASSERT_EQUAL(AckStatus::Rejected, a.status);
    TEST_ASSERT_EQUAL_STRING("expired", a.detail);
}

// ---------------------------------------------------------------- vend and sessions

static void test_vend_request_rejections(void) {
    Rig r;
    r.bring_up();
    r.set_policy();
    Ack a = r.vend(25, "nosuchsession");
    TEST_ASSERT_EQUAL_STRING("no_session", a.detail);
    char session[65];
    snprintf(session, sizeof(session), "%s", r.tap_session());
    a = r.vend(25, "wrong-session-id");
    TEST_ASSERT_EQUAL(AckStatus::Rejected, a.status);
    TEST_ASSERT_EQUAL_STRING("session_mismatch", a.detail);
    a = r.vend(33, session, "vend-0000005");  // not a multiple of the reader's 5 cent scale
    TEST_ASSERT_EQUAL(AckStatus::Rejected, a.status);
    TEST_ASSERT_EQUAL_STRING("price_scale", a.detail);
    a = r.vend(0, session);
    TEST_ASSERT_EQUAL(AckStatus::Rejected, a.status);
}

static void test_vend_denied_by_reader_arrives_as_event_after_accepted_ack(void) {
    Rig r;
    r.bring_up();
    r.set_policy();
    char session[65];
    snprintf(session, sizeof(session), "%s", r.tap_session());
    r.rdr.deny();
    const Ack a = r.vend(25, session, "vend-0000006");
    TEST_ASSERT_EQUAL(Phase::Accepted, a.phase);
    r.steps(3);
    const PaymentEvent* d = r.sink.first(EventKind::VendDenied);
    TEST_ASSERT_NOT_NULL(d);
    TEST_ASSERT_EQUAL_STRING("denied", d->detail);
    TEST_ASSERT_EQUAL_STRING("vend-0000006", d->request_id);
    // The session stays open for the edge to decide.
    PaymentState s;
    r.m.snapshot(s);
    TEST_ASSERT_TRUE(s.session_open);
}

static void test_vend_request_still_queued_when_reader_does_not_ack_is_accepted(void) {
    Rig r;
    r.bring_up();
    r.set_policy();
    char session[65];
    snprintf(session, sizeof(session), "%s", r.tap_session());
    r.rdr.silent = true;
    const Ack a = r.vend(25, session, "vend-0000007");
    TEST_ASSERT_EQUAL(AckStatus::Ok, a.status);
    TEST_ASSERT_EQUAL(Phase::Accepted, a.phase);
    TEST_ASSERT_EQUAL_INT(0, r.rdr.n_vends);
}

static void test_vend_result_rejections(void) {
    Rig r;
    r.bring_up();
    r.set_policy();
    char session[65];
    snprintf(session, sizeof(session), "%s", r.tap_session());
    Ack a = r.vend_result(session, "vend-0000008", true);
    TEST_ASSERT_EQUAL_STRING("no_vend", a.detail);
    r.rdr.approve(25);
    r.vend(25, session, "vend-0000008");
    r.steps(3);
    a = r.vend_result("wrong-session-id", "vend-0000008", true);
    TEST_ASSERT_EQUAL_STRING("session_mismatch", a.detail);
    a = r.vend_result(session, "vend-9999999", true);
    TEST_ASSERT_EQUAL_STRING("request_mismatch", a.detail);
    TEST_ASSERT_EQUAL(AckStatus::Ok, r.vend_result(session, "vend-0000008", true).status);
    // Exactly one result per approval.
    TEST_ASSERT_EQUAL(AckStatus::Rejected, r.vend_result(session, "vend-0000008", true).status);
}

static void test_end_session(void) {
    Rig r;
    r.bring_up();
    r.set_policy();
    Command c = r.cmd(CmdKind::EndSession, "end_session");
    snprintf(c.session_id, sizeof(c.session_id), "x");
    TEST_ASSERT_EQUAL_STRING("no_session", r.m.submit(c).detail);
    char session[65];
    snprintf(session, sizeof(session), "%s", r.tap_session());
    TEST_ASSERT_EQUAL_STRING("session_mismatch", r.m.submit(c).detail);
    snprintf(c.session_id, sizeof(c.session_id), "%s", session);
    TEST_ASSERT_EQUAL(AckStatus::Ok, r.m.submit(c).status);
    r.steps(5);
    TEST_ASSERT_EQUAL(SessionEndReason::Edge, need(r.sink.first(EventKind::SessionEnded))->end_reason);
}

// ---------------------------------------------------------------- other commands

static void test_return_escrow(void) {
    Rig r;
    r.bring_up();
    r.set_policy();
    Ack a = r.simple(CmdKind::ReturnEscrow, "return_escrow");
    TEST_ASSERT_EQUAL(AckStatus::Ok, a.status);
    TEST_ASSERT_EQUAL_STRING("no_bill", a.detail);
    r.bill.insert(1);
    r.steps(1);
    a = r.simple(CmdKind::ReturnEscrow, "return_escrow");
    TEST_ASSERT_EQUAL(AckStatus::Ok, a.status);
    TEST_ASSERT_EQUAL_STRING("", a.detail);
    r.steps(5);
    const PaymentEvent* ret = r.sink.first(EventKind::BillReturned);
    TEST_ASSERT_NOT_NULL(ret);
    TEST_ASSERT_EQUAL(BillReturnReason::Edge, ret->bill_reason);
    TEST_ASSERT_EQUAL_UINT32(0, r.acc.escrow_cents());
}

static void test_device_reset_resets_that_device_only(void) {
    Rig r;
    r.bring_up();
    r.set_policy();
    const int c0 = r.coin.calls[0], b0 = r.bill.calls[0], r0 = r.rdr.calls[0];
    Command c = r.cmd(CmdKind::DeviceReset, "device_reset");
    c.device = Device::Bill;
    TEST_ASSERT_EQUAL(AckStatus::Ok, r.m.submit(c).status);
    r.steps(30);
    TEST_ASSERT_EQUAL_INT(c0, r.coin.calls[0]);
    TEST_ASSERT_EQUAL_INT(b0 + 1, r.bill.calls[0]);
    TEST_ASSERT_EQUAL_INT(r0, r.rdr.calls[0]);
    c.device = Device::Coin;
    r.m.submit(c);
    c.device = Device::Cashless;
    r.m.submit(c);
    r.steps(30);
    TEST_ASSERT_EQUAL_INT(c0 + 1, r.coin.calls[0]);
    TEST_ASSERT_EQUAL_INT(r0 + 1, r.rdr.calls[0]);
    // After a reset the policy's enables come back on their own.
    TEST_ASSERT_EQUAL_UINT16(0x000F, r.bill.bill_mask);
    TEST_ASSERT_EQUAL_UINT16(0x000F, r.coin.coin_masks[r.coin.n_masks - 1]);
}

static void test_cash_sale_clamps(void) {
    Rig r;
    r.bring_up();
    r.set_policy();
    r.acc.coin(10);
    const Ack a = r.cash_sale(25);
    TEST_ASSERT_EQUAL(AckStatus::Ok, a.status);
    TEST_ASSERT_EQUAL_STRING("clamped", a.detail);
    TEST_ASSERT_EQUAL_UINT32(0, r.acc.escrow_cents());
    r.steps(4);
    TEST_ASSERT_TRUE(r.reader_saw(0x05));  // the reader is told even so
    TEST_ASSERT_EQUAL(AckStatus::Rejected, r.cash_sale(0).status);
}

static void test_device_fault_does_not_inhibit_the_others(void) {
    Rig r;
    r.bring_up();
    r.set_policy();
    r.bill.silent = true;
    r.steps(40);
    PaymentState s;
    r.m.snapshot(s);
    TEST_ASSERT_EQUAL(Status::Offline, s.devices[1].status);
    TEST_ASSERT_EQUAL(Inhibit::None, r.m.inhibit());
    TEST_ASSERT_TRUE(s.accepting);
    r.coin.deposit(2, 1);
    r.steps(4);
    TEST_ASSERT_EQUAL_UINT32(25, r.acc.escrow_cents());
    TEST_ASSERT_TRUE(r.rdr.reader_enabled);
}

// ---------------------------------------------------------------- accounting, state, self test

static void test_accounts_book_an_event_once_even_if_the_sink_refuses_first(void) {
    Rig r;
    r.bring_up();
    r.set_policy();
    r.sink.capacity = r.sink.n;  // the outbound queue is full
    r.coin.deposit(2, 1);
    r.steps(4);
    TEST_ASSERT_EQUAL_UINT32(0, r.acc.escrow_cents());
    TEST_ASSERT_EQUAL_INT(0, r.sink.count(EventKind::CoinAccepted));
    r.sink.capacity = 256;
    r.steps(4);
    TEST_ASSERT_EQUAL_INT(1, r.sink.count(EventKind::CoinAccepted));
    TEST_ASSERT_EQUAL_UINT32(25, r.acc.escrow_cents());
    r.steps(4);
    TEST_ASSERT_EQUAL_UINT32(25, r.acc.escrow_cents());
}

static void test_all_three_devices_share_one_clean_bus(void) {
    Rig r;
    r.bring_up();
    r.set_policy();
    r.coin.deposit(1, 1);
    r.bill.insert(0);
    r.rdr.tap(0xFFFF);
    r.steps(40);
    TEST_ASSERT_EQUAL_UINT32(110, r.acc.escrow_cents());
    TEST_ASSERT_EQUAL_INT(1, r.sink.count(EventKind::SessionBegun));
    TEST_ASSERT_EQUAL_INT(0, r.link.misses(0x08));
    TEST_ASSERT_EQUAL_INT(0, r.link.misses(0x30));
    TEST_ASSERT_EQUAL_INT(0, r.link.misses(0x10));
    TEST_ASSERT_EQUAL_UINT32(0, r.link.corrupt_blocks());
    TEST_ASSERT_EQUAL_UINT32(0, r.link.mode_disagreements());
}

static void test_state_dirty_flag(void) {
    Rig r;
    TEST_ASSERT_TRUE(r.m.take_state_dirty());
    TEST_ASSERT_FALSE(r.m.take_state_dirty());
    r.bring_up();
    r.steps(5);
    TEST_ASSERT_FALSE(r.m.take_state_dirty());  // nothing changed
    r.set_policy();
    TEST_ASSERT_TRUE(r.m.take_state_dirty());
    TEST_ASSERT_FALSE(r.m.take_state_dirty());
    r.coin.deposit(2, 1);
    r.steps(3);
    TEST_ASSERT_TRUE(r.m.take_state_dirty());
    TEST_ASSERT_FALSE(r.m.take_state_dirty());
    r.rdr.tap(0xFFFF);
    r.steps(3);
    TEST_ASSERT_TRUE(r.m.take_state_dirty());
    r.m.force_report();
    TEST_ASSERT_TRUE(r.m.take_state_dirty());
}

static void test_snapshot_contents(void) {
    Rig r;
    r.m.set_wall_time(1700000000);  // 2023-11-14T22:13:20Z
    r.bring_up();
    r.set_policy();
    r.coin.deposit(2, 1);
    r.steps(4);
    r.tap_session(100);
    PaymentState s;
    r.m.snapshot(s);
    TEST_ASSERT_TRUE(s.devices[0].present);
    TEST_ASSERT_TRUE(s.devices[1].present);
    TEST_ASSERT_TRUE(s.devices[2].present);
    TEST_ASSERT_TRUE(s.n_tubes >= 3);
    TEST_ASSERT_EQUAL_UINT32(25, s.escrow_cents);
    TEST_ASSERT_TRUE(s.stacker_known);
    TEST_ASSERT_TRUE(s.session_open);
    TEST_ASSERT_EQUAL_UINT32(500, s.funds_cents);
    TEST_ASSERT_NOT_NULL(s.session_started_ts);
    TEST_ASSERT_EQUAL_INT(0, strncmp(s.session_started_ts, "2023-11-14T22:13:", 17));
    TEST_ASSERT_EQUAL_INT(20, static_cast<int>(strlen(s.session_started_ts)));
}

// ---------------------------------------------------------------- ack model, policy commit, enables

static void test_failed_set_policy_rolls_back_and_never_resumes_acceptance(void) {
    Rig r;
    r.bring_up();
    Command pc = r.policy();
    pc.policy.edge_timeout_s = 5;
    TEST_ASSERT_EQUAL(AckStatus::Ok, r.m.submit(pc).status);
    r.clock.advance_ms(6000);
    r.steps(8);
    TEST_ASSERT_EQUAL(Inhibit::EdgeSilent, r.m.inhibit());
    const int masks = r.coin.n_masks;
    r.coin.fail_coin_type = true;  // the changer NAKs the enable update
    const Ack a = r.set_policy();
    TEST_ASSERT_EQUAL(AckStatus::Failed, a.status);
    TEST_ASSERT_EQUAL_STRING("enable_pending:coin", a.detail);
    TEST_ASSERT_EQUAL(Inhibit::EdgeSilent, r.m.inhibit());
    PaymentState s;
    r.m.snapshot(s);
    TEST_ASSERT_FALSE(s.accepting);
    TEST_ASSERT_EQUAL_STRING("p-00000001", s.policy_id);
    r.steps(5);
    TEST_ASSERT_EQUAL_INT(masks, r.coin.n_masks);
    TEST_ASSERT_EQUAL_UINT16(0, r.bill.bill_mask);  // still disabled on the bus
    TEST_ASSERT_FALSE(r.rdr.reader_enabled);
    // A later set_policy that the bus accepts does resume acceptance.
    r.coin.fail_coin_type = false;
    TEST_ASSERT_EQUAL(AckStatus::Ok, r.set_policy().status);
    TEST_ASSERT_EQUAL(Inhibit::None, r.m.inhibit());
}

static void test_failed_first_set_policy_stays_without_a_policy(void) {
    Rig r;
    r.bring_up();
    r.coin.fail_coin_type = true;
    TEST_ASSERT_EQUAL(AckStatus::Failed, r.set_policy().status);
    TEST_ASSERT_EQUAL(Inhibit::NoPolicy, r.m.inhibit());
    PaymentState s;
    r.m.snapshot(s);
    TEST_ASSERT_NULL(s.policy_id);
    TEST_ASSERT_FALSE(s.accepting);
}

static void test_enabled_is_within_the_denominations_the_devices_reported(void) {
    Rig r;
    Command c = r.policy();
    const uint32_t coins[2] = {5, 200};  // the fake changer has no 200 cent coin
    memcpy(c.policy.coin_enabled, coins, sizeof(coins));
    c.policy.n_coin = 2;
    r.m.submit(c);  // before SETUP: nothing is known yet
    PaymentState s;
    r.m.snapshot(s);
    TEST_ASSERT_EQUAL_UINT8(0, s.devices[0].n_enabled);
    TEST_ASSERT_EQUAL_UINT8(0, s.devices[1].n_enabled);
    r.steps(40);
    r.m.snapshot(s);
    TEST_ASSERT_EQUAL_UINT8(1, s.devices[0].n_enabled);
    TEST_ASSERT_EQUAL_UINT32(5, s.devices[0].enabled[0]);
    TEST_ASSERT_EQUAL_UINT8(4, s.devices[1].n_enabled);
    TEST_ASSERT_EQUAL_UINT16(0x0001, r.coin.coin_masks[r.coin.n_masks - 1]);  // no bit for the unknown type
    TEST_ASSERT_EQUAL_UINT16(0x000F, r.bill.bill_mask);
}

static void test_vend_result_end_session_return_escrow_ack_after_the_bus(void) {
    Rig r;
    r.bring_up();
    r.set_policy();
    char session[65];
    snprintf(session, sizeof(session), "%s", r.tap_session());
    r.rdr.approve(25);
    r.vend(25, session, "vend-0000020");
    r.steps(3);
    // The result reaches the reader before the ack exists.
    const int before = r.rdr.n_vends;
    Ack a = r.vend_result(session, "vend-0000020", true);
    TEST_ASSERT_EQUAL(AckStatus::Ok, a.status);
    TEST_ASSERT_EQUAL_STRING("", a.detail);
    TEST_ASSERT_TRUE(r.rdr.n_vends >= before + 2);  // VEND SUCCESS and SESSION COMPLETE

    // A silent reader: the action is still queued, so the ack says so (the events are authoritative).
    char s2[65];
    snprintf(s2, sizeof(s2), "%s", r.tap_session());
    TEST_ASSERT_TRUE(s2[0] != '\0');
    r.rdr.silent = true;
    Command c = r.cmd(CmdKind::EndSession, "end_session");
    snprintf(c.session_id, sizeof(c.session_id), "%s", s2);
    a = r.m.submit(c);
    TEST_ASSERT_EQUAL(AckStatus::Ok, a.status);
    TEST_ASSERT_EQUAL_STRING("queued", a.detail);
    TEST_ASSERT_EQUAL(Phase::Completed, a.phase);
    r.rdr.silent = false;
    r.clock.advance_ms(1500);
    r.steps(10);
    TEST_ASSERT_EQUAL(SessionEndReason::Edge, need(r.sink.last(EventKind::SessionEnded))->end_reason);

    // return_escrow: a NAK from the validator is a failed ack.
    r.bill.insert(1);
    r.steps(1);
    r.bill.fail_escrow = true;
    a = r.simple(CmdKind::ReturnEscrow, "return_escrow");
    TEST_ASSERT_EQUAL(AckStatus::Failed, a.status);
    TEST_ASSERT_EQUAL_STRING("nak", a.detail);
}

static void test_self_test_reports_driver_event_drops(void) {
    Rig r;
    r.bring_up();
    Ack a;
    r.m.self_test(a);
    TEST_ASSERT_NOT_NULL(strstr(a.result_json, "\"name\":\"event_queue\",\"pass\":true,\"detail\":\"0 dropped\""));
}

static int count_vends(const Rig& r, uint8_t sub) {
    int c = 0;
    for (int i = 0; i < r.rdr.n_vends; ++i) c += r.rdr.vends[i].sub == sub;
    return c;
}

// A refused or repeated vend_request must never erase the id of the vend the reader holds.
static void test_same_id_vend_request_retry_keeps_the_in_flight_vend(void) {
    Rig r;
    r.bring_up();
    r.set_policy();
    char session[65];
    snprintf(session, sizeof(session), "%s", r.tap_session());
    r.rdr.approve(25);
    r.rdr.silent = true;
    Ack a = r.vend(25, session, "vend-0000040");
    TEST_ASSERT_EQUAL(AckStatus::Ok, a.status);
    TEST_ASSERT_EQUAL(Phase::Accepted, a.phase);
    a = r.vend(25, session, "vend-0000040");  // the edge's retry; the first is still queued
    TEST_ASSERT_EQUAL(AckStatus::Ok, a.status);
    TEST_ASSERT_EQUAL(Phase::Accepted, a.phase);
    TEST_ASSERT_EQUAL_STRING("in_progress", a.detail);
    r.rdr.silent = false;
    r.clock.advance_ms(1500);
    r.steps(20);
    TEST_ASSERT_EQUAL_INT(1, count_vends(r, 0x00));  // exactly one VEND REQUEST on the wire
    const PaymentEvent* ap = need(r.sink.first(EventKind::VendApproved));
    TEST_ASSERT_EQUAL_STRING("vend-0000040", ap->request_id);
    a = r.vend_result(session, "vend-0000040", true);
    TEST_ASSERT_EQUAL(AckStatus::Ok, a.status);
    r.steps(10);
    TEST_ASSERT_EQUAL(SessionEndReason::Complete, need(r.sink.last(EventKind::SessionEnded))->end_reason);
}

static void test_different_id_vend_request_while_one_is_in_flight_is_busy(void) {
    Rig r;
    r.bring_up();
    r.set_policy();
    char session[65];
    snprintf(session, sizeof(session), "%s", r.tap_session());
    r.rdr.approve(25);
    Ack a = r.vend(25, session, "vend-0000041");
    TEST_ASSERT_EQUAL(Phase::Accepted, a.phase);
    a = r.vend(25, session, "vend-0000042");
    TEST_ASSERT_EQUAL(AckStatus::Rejected, a.status);
    TEST_ASSERT_EQUAL_STRING("busy", a.detail);
    r.steps(3);
    TEST_ASSERT_EQUAL_STRING("vend-0000041", need(r.sink.first(EventKind::VendApproved))->request_id);
    a = r.vend_result(session, "vend-0000041", true);
    TEST_ASSERT_EQUAL(AckStatus::Ok, a.status);
    r.steps(10);
    TEST_ASSERT_EQUAL(SessionEndReason::Complete, need(r.sink.last(EventKind::SessionEnded))->end_reason);
}

static void test_refused_price_scale_request_keeps_the_in_flight_id(void) {
    Rig r;
    r.bring_up();
    r.set_policy();
    char session[65];
    snprintf(session, sizeof(session), "%s", r.tap_session());
    r.rdr.approve(25);
    r.vend(25, session, "vend-0000043");
    const Ack bad = r.vend(33, session, "vend-0000044");  // not a multiple of the scale
    TEST_ASSERT_EQUAL(AckStatus::Rejected, bad.status);
    r.steps(3);
    const Ack a = r.vend_result(session, "vend-0000043", true);
    TEST_ASSERT_EQUAL(AckStatus::Ok, a.status);
}

static void test_reader_nak_of_vend_success_is_a_failed_ack_and_the_chain_continues(void) {
    Rig r;
    r.bring_up();
    r.set_policy();
    char session[65];
    snprintf(session, sizeof(session), "%s", r.tap_session());
    r.rdr.approve(25);
    r.vend(25, session, "vend-0000045");
    r.steps(3);
    r.rdr.fail_next_action = true;  // the reader NAKs VEND SUCCESS
    const Ack a = r.vend_result(session, "vend-0000045", true);
    TEST_ASSERT_EQUAL(AckStatus::Failed, a.status);
    TEST_ASSERT_EQUAL_STRING("nak", a.detail);
    r.steps(10);
    TEST_ASSERT_TRUE(r.reader_saw(0x04));  // SESSION COMPLETE still went out
}

static void test_return_escrow_with_a_silent_validator_is_queued(void) {
    Rig r;
    r.bring_up();
    r.set_policy();
    r.bill.insert(1);
    r.steps(1);
    r.bill.silent = true;
    const Ack a = r.simple(CmdKind::ReturnEscrow, "return_escrow");
    TEST_ASSERT_EQUAL(AckStatus::Ok, a.status);
    TEST_ASSERT_EQUAL_STRING("queued", a.detail);
}

static void test_payout_b_while_a_result_is_held_is_busy_and_a_retry_still_accepted(void) {
    Rig r;
    r.bring_up();
    r.set_policy();
    r.acc.coin(100);
    r.sink.capacity = r.sink.n;  // A's PayoutResult will be held back
    TEST_ASSERT_EQUAL(Phase::Accepted, r.payout(15, "pay-00000050").phase);
    r.steps(8);
    TEST_ASSERT_EQUAL_INT(0, r.sink.count(EventKind::PayoutResult));
    const Ack b = r.payout(10, "pay-00000051");
    TEST_ASSERT_EQUAL(AckStatus::Rejected, b.status);
    TEST_ASSERT_EQUAL_STRING("busy", b.detail);
    const Ack again = r.payout(15, "pay-00000050");
    TEST_ASSERT_EQUAL(AckStatus::Ok, again.status);
    TEST_ASSERT_EQUAL(Phase::Accepted, again.phase);
    TEST_ASSERT_EQUAL_INT(1, r.coin.n_payouts);
    r.sink.capacity = 256;
    r.steps(3);
    TEST_ASSERT_EQUAL_INT(1, r.sink.count(EventKind::PayoutResult));
    TEST_ASSERT_EQUAL_STRING("pay-00000050", need(r.sink.first(EventKind::PayoutResult))->request_id);
    TEST_ASSERT_EQUAL_UINT32(85, r.acc.escrow_cents());
}

static void test_self_test_event_queue_fails_after_a_driver_dropped_events(void) {
    Rig r;
    r.bring_up();
    r.set_policy();
    char session[65];
    snprintf(session, sizeof(session), "%s", r.tap_session());
    r.sink.capacity = r.sink.n;  // a refusing sink: each price_scale denial stays queued in the reader driver
    for (int i = 0; i < 20; ++i) r.vend(33, session, "vend-0000046");
    Ack a;
    r.m.self_test(a);
    TEST_ASSERT_NOT_NULL(strstr(a.result_json, "\"name\":\"event_queue\",\"pass\":false"));
    TEST_ASSERT_NULL(strstr(a.result_json, "\"detail\":\"0 dropped\""));
}

static void test_self_test_shape(void) {
    Rig r;
    r.bring_up();
    r.set_policy();
    r.steps(4);
    Ack a;
    r.m.self_test(a);
    TEST_ASSERT_EQUAL(AckStatus::Ok, a.status);
    const char* j = a.result_json;
    TEST_ASSERT_EQUAL_INT(0, strncmp(j, "{\"checks\":[{\"name\":\"changer\",\"pass\":true,\"detail\":\"level 3, ready\"}", 67));
    const char* names[7] = {"changer", "validator", "reader", "clock_synced", "event_log_full", "rx_mode_bit", "policy"};
    const char* at = j;
    for (int i = 0; i < 7; ++i) {
        char key[48];
        snprintf(key, sizeof(key), "\"name\":\"%s\"", names[i]);
        const char* p = strstr(at, key);
        TEST_ASSERT_NOT_NULL_MESSAGE(p, names[i]);
        at = p;
    }
    TEST_ASSERT_NOT_NULL(strstr(j, "\"name\":\"clock_synced\",\"pass\":false"));
    TEST_ASSERT_NOT_NULL(strstr(j, "\"name\":\"rx_mode_bit\",\"pass\":true,\"detail\":\"0 disagreements\""));
    TEST_ASSERT_NOT_NULL(strstr(j, "\"name\":\"policy\",\"pass\":true,\"detail\":\"p-00000001\""));
    TEST_ASSERT_EQUAL_STRING("]}", j + strlen(j) - 2);

    FakeLogStore store;
    EventLog log(store, r.ids);
    log.begin_boot();
    r.m.set_event_log(&log);
    r.m.set_wall_time(1700000000);
    r.m.self_test(a);
    TEST_ASSERT_NOT_NULL(strstr(a.result_json, "\"name\":\"event_log_full\",\"pass\":true,\"detail\":\"depth 0\""));
    TEST_ASSERT_NOT_NULL(strstr(a.result_json, "\"name\":\"clock_synced\",\"pass\":true"));
    // Via submit, the result rides in the ack.
    const Ack viacmd = r.simple(CmdKind::SelfTest, "self_test");
    TEST_ASSERT_EQUAL_STRING("self_test", viacmd.command);
    TEST_ASSERT_EQUAL_STRING(a.result_json, viacmd.result_json);
}

static void test_log_pressure_inhibits_with_device_fault_and_zeroes_the_bus(void) {
    Rig r;
    r.bring_up();
    TEST_ASSERT_EQUAL(AckStatus::Ok, r.set_policy().status);
    r.steps(4);
    TEST_ASSERT_EQUAL_UINT16(0x000F, r.bill.bill_mask);
    TEST_ASSERT_TRUE(r.rdr.reader_enabled);
    r.m.set_log_pressure(true);
    r.steps(6);
    TEST_ASSERT_EQUAL(Inhibit::DeviceFault, r.m.inhibit());
    PaymentState s;
    r.m.snapshot(s);
    TEST_ASSERT_FALSE(s.accepting);
    TEST_ASSERT_EQUAL(Inhibit::DeviceFault, s.inhibited);
    TEST_ASSERT_EQUAL_UINT16(0, r.coin.coin_masks[r.coin.n_masks - 1]);
    TEST_ASSERT_EQUAL_UINT16(0, r.bill.bill_mask);
    TEST_ASSERT_FALSE(r.rdr.reader_enabled);
    Ack a;
    r.m.self_test(a);
    TEST_ASSERT_NOT_NULL(strstr(a.result_json, "\"name\":\"event_log_full\",\"pass\":false"));
    // Releasing the pressure with a valid policy resumes acceptance.
    r.m.set_log_pressure(false);
    r.steps(6);
    TEST_ASSERT_EQUAL(Inhibit::None, r.m.inhibit());
    TEST_ASSERT_EQUAL_UINT16(0x000F, r.bill.bill_mask);
    TEST_ASSERT_TRUE(r.rdr.reader_enabled);
    r.m.self_test(a);
    TEST_ASSERT_NOT_NULL(strstr(a.result_json, "\"name\":\"event_log_full\",\"pass\":true"));
}

static void test_log_pressure_never_lifts_a_higher_inhibit(void) {
    Rig r;
    r.bring_up();
    r.m.set_log_pressure(true);  // no policy yet: no_policy outranks it
    TEST_ASSERT_EQUAL(Inhibit::NoPolicy, r.m.inhibit());
    r.m.set_log_pressure(false);
    TEST_ASSERT_EQUAL(Inhibit::NoPolicy, r.m.inhibit());

    Command pc = r.policy();
    pc.policy.edge_timeout_s = 5;
    TEST_ASSERT_EQUAL(AckStatus::Ok, r.m.submit(pc).status);
    r.m.set_log_pressure(true);
    TEST_ASSERT_EQUAL(Inhibit::DeviceFault, r.m.inhibit());
    r.clock.advance_ms(6000);
    r.steps(8);
    TEST_ASSERT_EQUAL(Inhibit::EdgeSilent, r.m.inhibit());  // silence outranks it
    r.m.set_log_pressure(false);
    r.steps(4);
    TEST_ASSERT_EQUAL(Inhibit::EdgeSilent, r.m.inhibit());
    TEST_ASSERT_EQUAL_UINT16(0, r.bill.bill_mask);
    TEST_ASSERT_FALSE(r.rdr.reader_enabled);

    // policy off outranks it too
    TEST_ASSERT_EQUAL(AckStatus::Ok, r.set_policy(false).status);
    r.m.set_log_pressure(true);
    TEST_ASSERT_EQUAL(Inhibit::PolicyOff, r.m.inhibit());
    r.m.set_log_pressure(false);
    TEST_ASSERT_EQUAL(Inhibit::PolicyOff, r.m.inhibit());
}

static void test_log_pressure_does_not_return_a_held_bill_or_end_a_session(void) {
    Rig r;
    r.bring_up();
    TEST_ASSERT_EQUAL(AckStatus::Ok, r.set_policy().status);
    r.steps(4);
    const char* sid = r.tap_session();
    TEST_ASSERT_NOT_NULL(sid);
    r.sink.n = 0;
    r.m.set_log_pressure(true);
    r.steps(6);
    TEST_ASSERT_EQUAL_INT(0, r.sink.count(EventKind::BillReturned));
    TEST_ASSERT_EQUAL_INT(0, r.sink.count(EventKind::SessionEnded));
}

int main(int, char**) {
    UNITY_BEGIN();
    RUN_TEST(test_log_pressure_inhibits_with_device_fault_and_zeroes_the_bus);
    RUN_TEST(test_log_pressure_never_lifts_a_higher_inhibit);
    RUN_TEST(test_log_pressure_does_not_return_a_held_bill_or_end_a_session);
    RUN_TEST(test_boot_without_policy_disables_everything);
    RUN_TEST(test_set_policy_acks_ok_after_enables_on_the_bus);
    RUN_TEST(test_set_policy_accept_false_is_policy_off);
    RUN_TEST(test_set_policy_validates_and_keeps_state);
    RUN_TEST(test_set_policy_does_not_wait_for_an_absent_reader);
    RUN_TEST(test_basic_commands);
    RUN_TEST(test_flow1_coins_then_cash_sale_then_change);
    RUN_TEST(test_flow2_bill_price_first);
    RUN_TEST(test_flow3_cashless_tap_first_to_session_complete);
    RUN_TEST(test_flow4_price_first_without_always_idle_is_no_session);
    RUN_TEST(test_flow4_price_first_with_always_idle_continues_as_flow3);
    RUN_TEST(test_flow5_cashless_failure_charges_nothing);
    RUN_TEST(test_flow6_cash_vend_failed_pays_back_everything);
    RUN_TEST(test_flow7_edge_silence_sequence);
    RUN_TEST(test_silence_waits_out_an_outstanding_vend_then_driver_timeout_closes);
    RUN_TEST(test_rule6_missing_vend_result_fails_the_vend_and_times_out);
    RUN_TEST(test_judge_without_policy_and_when_off);
    RUN_TEST(test_judge_returns_bill_that_would_exceed_escrow_cap);
    RUN_TEST(test_judge_exact_change_returns_bills_above_threshold);
    RUN_TEST(test_judge_reports_edge_silent_once_silent);
    RUN_TEST(test_unbound_tap_refuses_events_and_bills);
    RUN_TEST(test_lever_returns_escrowed_bill_when_policy_asks);
    RUN_TEST(test_lever_without_flag_only_reports);
    RUN_TEST(test_payout_acked_accepted_only_after_changer_acked_the_bus_command);
    RUN_TEST(test_payout_changer_refusal_is_a_failed_ack);
    RUN_TEST(test_payout_still_queued_when_changer_is_silent_is_accepted);
    RUN_TEST(test_payout_retry_while_in_flight_is_accepted_not_busy);
    RUN_TEST(test_payout_result_held_by_full_sink_retry_is_accepted_then_replays);
    RUN_TEST(test_payout_the_changer_cannot_express_is_chunked_or_unpayable);
    RUN_TEST(test_payout_rejections);
    RUN_TEST(test_payout_retry_of_running_payout_does_not_pay_twice);
    RUN_TEST(test_payout_idempotent_replay_after_completion);
    RUN_TEST(test_payout_replay_survives_a_full_sink);
    RUN_TEST(test_payout_expiry_uses_wall_time);
    RUN_TEST(test_vend_request_rejections);
    RUN_TEST(test_vend_denied_by_reader_arrives_as_event_after_accepted_ack);
    RUN_TEST(test_vend_request_still_queued_when_reader_does_not_ack_is_accepted);
    RUN_TEST(test_vend_result_rejections);
    RUN_TEST(test_end_session);
    RUN_TEST(test_return_escrow);
    RUN_TEST(test_device_reset_resets_that_device_only);
    RUN_TEST(test_cash_sale_clamps);
    RUN_TEST(test_device_fault_does_not_inhibit_the_others);
    RUN_TEST(test_accounts_book_an_event_once_even_if_the_sink_refuses_first);
    RUN_TEST(test_all_three_devices_share_one_clean_bus);
    RUN_TEST(test_state_dirty_flag);
    RUN_TEST(test_snapshot_contents);
    RUN_TEST(test_failed_set_policy_rolls_back_and_never_resumes_acceptance);
    RUN_TEST(test_failed_first_set_policy_stays_without_a_policy);
    RUN_TEST(test_enabled_is_within_the_denominations_the_devices_reported);
    RUN_TEST(test_vend_result_end_session_return_escrow_ack_after_the_bus);
    RUN_TEST(test_self_test_reports_driver_event_drops);
    RUN_TEST(test_same_id_vend_request_retry_keeps_the_in_flight_vend);
    RUN_TEST(test_different_id_vend_request_while_one_is_in_flight_is_busy);
    RUN_TEST(test_refused_price_scale_request_keeps_the_in_flight_id);
    RUN_TEST(test_reader_nak_of_vend_success_is_a_failed_ack_and_the_chain_continues);
    RUN_TEST(test_return_escrow_with_a_silent_validator_is_queued);
    RUN_TEST(test_payout_b_while_a_result_is_held_is_busy_and_a_retry_still_accepted);
    RUN_TEST(test_self_test_event_queue_fails_after_a_driver_dropped_events);
    RUN_TEST(test_self_test_shape);
    return UNITY_END();
}
