#include "event_log.h"

#include <stdio.h>
#include <string.h>

#include "wire.h"

namespace mdbcore {

void EventLog::begin_boot() {
    ids_.fill_hex(boot_id_, 16);
    boot_id_[16] = '\0';
    seq_ = 0;
}

bool EventLog::append(const PaymentEvent& e, const char* iso_ts, char* json_out, size_t cap) {
    if (full() || boot_id_[0] == '\0') return false;
    const uint32_t next = seq_ + 1;
    char event_id[40];
    snprintf(event_id, sizeof(event_id), "%s-%lu", boot_id_, static_cast<unsigned long>(next));
    const size_t n = event_json(e, event_id, boot_id_, next, iso_ts, json_out, cap);
    if (n == 0 || n >= LogStore::kMaxJson) return false;
    if (!store_.append(json_out, n)) return false;
    seq_ = next;
    return true;
}

void EventLog::acked(const char* boot_id, uint32_t seq) {
    char entry[LogStore::kMaxJson];
    char bid[33];
    for (;;) {
        int n = 0;
        bool bad = false;
        const int total = store_.count();
        while (n < total) {
            uint32_t s = 0;
            if (!store_.get(n, entry, sizeof(entry)) || !peek_boot_seq(entry, bid, s)) {
                bad = true;  // would wedge the front of the log for good
                break;
            }
            if (strcmp(bid, boot_id) != 0 || s > seq) break;
            ++n;
        }
        // The store erases from the front only: take the acked prefix, then the bad entry behind it.
        if (bad) {
            ++n;
            ++corrupt_;
        }
        if (n > 0) store_.erase_first(n);
        if (!bad || store_.count() >= total) return;  // a store that cannot erase must not spin us
    }
}

}  // namespace mdbcore
