// Payment events shared by the MDB device drivers; maps 1:1 onto ice_contracts.board.payment.
#pragma once

#include <stdint.h>
#include <string.h>

namespace mdbcore {

enum class EventKind : uint8_t {
    CoinAccepted, BillEscrowed, BillStacked, BillReturned, EscrowReturnPressed, PayoutResult,
    SessionBegun, VendApproved, VendDenied, SessionEnded, DeviceStatus, CashCollected
};
enum class Device : uint8_t { Coin, Bill, Cashless };
enum class Status : uint8_t { Ready, Disabled, Busy, Jammed, Full, Empty, Tilt, Offline, Error };
enum class CoinRoute : uint8_t { Tubes, Cashbox };
enum class BillReturnReason : uint8_t { Policy, EdgeSilent, Customer, Edge, Timeout };
enum class PayoutStatus : uint8_t { Ok, Partial, Failed };
enum class SessionEndReason : uint8_t { Complete, Cancelled, Reader, Edge, Timeout };

struct PaymentEvent {
    EventKind kind;
    uint32_t cents, requested_cents, paid_cents;
    CoinRoute route;
    BillReturnReason bill_reason;
    PayoutStatus payout_status;
    SessionEndReason end_reason;
    Device device;
    Status status;
    int16_t mdb_status;  // -1 = null
    bool funds_known;
    uint32_t funds_cents;
    char session_id[65];
    char request_id[65];
    char detail[64];
};

inline PaymentEvent make_event(EventKind kind, Device device) {
    PaymentEvent e;
    memset(&e, 0, sizeof(e));
    e.kind = kind;
    e.device = device;
    e.mdb_status = -1;
    return e;
}

// emit() returning false means the queue is full: the device must not advance past a
// money event it could not emit, and must emit it again on a later step.
struct EventSink {
    virtual bool emit(const PaymentEvent&) = 0;
};

}  // namespace mdbcore
