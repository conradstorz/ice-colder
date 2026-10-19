#include "nvs_log_store.h"

#include <stdio.h>
#include <string.h>

#include "esp_log.h"
#include "nvs_flash.h"

namespace {
const char* TAG = "nvs_log";
constexpr const char* kPartition = "evlog";
constexpr const char* kNamespace = "log";

// Index of an "e<8 hex>" key, or false for any other key.
bool parse_key(const char* key, uint32_t& index) {
    if (key[0] != 'e' || strlen(key) != 9) return false;
    char* end = nullptr;
    const unsigned long v = strtoul(key + 1, &end, 16);
    if (end != key + 9) return false;
    index = static_cast<uint32_t>(v);
    return true;
}
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
    // One read-only walk of the namespace (no erases, so nothing here can run long): find the lowest
    // entry blob below head, whatever cut stranded it. sweep_step() then erases swept_..head-1
    // incrementally inside quiet windows once the bus is running.
    uint32_t lowest = head;
    nvs_iterator_t it = nullptr;
    esp_err_t e = nvs_entry_find_in_handle(h_, NVS_TYPE_BLOB, &it);
    while (e == ESP_OK && it != nullptr) {
        nvs_entry_info_t info;
        uint32_t idx = 0;
        if (nvs_entry_info(it, &info) == ESP_OK && parse_key(info.key, idx) && idx < lowest) lowest = idx;
        e = nvs_entry_next(&it);
    }
    nvs_release_iterator(it);
    swept_ = lowest;
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
    const uint32_t new_head = head_.load() + static_cast<uint32_t>(n);
    if (nvs_set_u32(h_, "head", new_head) != ESP_OK || nvs_commit(h_) != ESP_OK) {
        ESP_LOGE(TAG, "erase: head update failed; entries stay");
        return;
    }
    head_.store(new_head);  // the blobs below it are erased by sweep_step()
}

int NvsLogStore::sweep_step(int max_keys) {
    if (!ok_) return 0;
    int done = 0;
    char key[16];
    const uint32_t head = head_.load();
    while (swept_ < head && done < max_keys) {
        key_for(swept_, key);
        nvs_erase_key(h_, key);
        ++swept_;
        ++done;
    }
    if (done > 0) nvs_commit(h_);
    return done;
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
