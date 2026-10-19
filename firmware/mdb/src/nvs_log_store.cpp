#include "nvs_log_store.h"

#include <stdio.h>
#include <string.h>

#include "esp_log.h"
#include "nvs_flash.h"

namespace {
const char* TAG = "nvs_log";
constexpr const char* kPartition = "evlog";
constexpr const char* kNamespace = "log";
constexpr uint32_t kOrphanSweep = 256;  // matches EventLog::kCapacity: the most one erase_first can strand
}  // namespace

void NvsLogStore::key_for(uint32_t index, char* out) { snprintf(out, 16, "e%08x", static_cast<unsigned>(index)); }

bool NvsLogStore::begin() {
    esp_err_t err = nvs_flash_init_partition(kPartition);
    if (err == ESP_ERR_NVS_NO_FREE_PAGES || err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_LOGE(TAG, "evlog partition unusable (%s); erasing it, the replay log is lost", esp_err_to_name(err));
        nvs_flash_erase_partition(kPartition);
        err = nvs_flash_init_partition(kPartition);
    }
    if (err == ESP_OK) err = nvs_open_from_partition(kPartition, kNamespace, NVS_READWRITE, &h_);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "evlog open failed: %s", esp_err_to_name(err));
        ok_ = false;
        return false;
    }
    uint32_t head = 0, tail = 0;
    if (nvs_get_u32(h_, "head", &head) != ESP_OK) head = 0;
    if (nvs_get_u32(h_, "tail", &tail) != ESP_OK) tail = head;
    if (tail < head || tail - head > 4096) {
        ESP_LOGE(TAG, "log counters inconsistent (head %u tail %u); treating the log as empty",
                 static_cast<unsigned>(head), static_cast<unsigned>(tail));
        head = tail;
        nvs_set_u32(h_, "head", head);
        nvs_commit(h_);
    }
    head_.store(head);
    tail_.store(tail);
    char key[16];
    for (uint32_t i = 1; i <= kOrphanSweep && i <= head; ++i) {  // blobs stranded by a cut erase
        key_for(head - i, key);
        nvs_erase_key(h_, key);
    }
    nvs_commit(h_);
    ok_ = true;
    ESP_LOGI(TAG, "replay log: %d entries pending", count());
    return true;
}

bool NvsLogStore::append(const char* json, size_t len) {
    if (!ok_ || len == 0 || len >= kMaxJson) return false;
    const uint32_t tail = tail_.load();
    char key[16];
    key_for(tail, key);
    esp_err_t err = nvs_set_blob(h_, key, json, len);
    if (err == ESP_OK) err = nvs_commit(h_);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "append: blob write failed: %s", esp_err_to_name(err));
        return false;
    }
    err = nvs_set_u32(h_, "tail", tail + 1);
    if (err == ESP_OK) err = nvs_commit(h_);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "append: tail update failed: %s", esp_err_to_name(err));
        return false;  // the blob is unreferenced; the retry overwrites it
    }
    tail_.store(tail + 1);
    return true;
}

bool NvsLogStore::get(int index, char* out, size_t cap) const {
    if (!ok_ || index < 0 || index >= count() || cap == 0) return false;
    char key[16];
    key_for(head_.load() + static_cast<uint32_t>(index), key);
    size_t size = 0;
    if (nvs_get_blob(h_, key, nullptr, &size) != ESP_OK || size + 1 > cap) return false;
    if (nvs_get_blob(h_, key, out, &size) != ESP_OK) return false;
    out[size] = '\0';
    return true;
}

void NvsLogStore::erase_first(int n) {
    if (!ok_ || n <= 0) return;
    if (n > count()) n = count();
    const uint32_t head = head_.load();
    const uint32_t new_head = head + static_cast<uint32_t>(n);
    if (nvs_set_u32(h_, "head", new_head) != ESP_OK || nvs_commit(h_) != ESP_OK) {
        ESP_LOGE(TAG, "erase: head update failed; entries stay");
        return;
    }
    head_.store(new_head);
    char key[16];
    for (uint32_t i = head; i < new_head; ++i) {
        key_for(i, key);
        nvs_erase_key(h_, key);
    }
    nvs_commit(h_);
}

bool NvsLogStore::load_payouts(char* out, size_t cap, size_t& len) const {
    len = 0;
    if (!ok_) return false;
    size_t size = 0;
    if (nvs_get_blob(h_, "payouts", nullptr, &size) != ESP_OK || size == 0 || size + 1 > cap) return false;
    if (nvs_get_blob(h_, "payouts", out, &size) != ESP_OK) return false;
    out[size] = '\0';
    len = size;
    return true;
}

bool NvsLogStore::save_payouts(const char* json, size_t len) {
    if (!ok_) return false;
    esp_err_t err = nvs_set_blob(h_, "payouts", json, len);
    if (err == ESP_OK) err = nvs_commit(h_);
    if (err != ESP_OK) ESP_LOGE(TAG, "payouts save failed: %s", esp_err_to_name(err));
    return err == ESP_OK;
}
