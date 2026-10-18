#include "net.h"

#include <stdio.h>
#include <string.h>
#include <time.h>

#include "driver/gpio.h"
#include "esp_eth.h"
#include "esp_event.h"
#include "esp_log.h"
#include "esp_mac.h"
#include "esp_netif.h"
#include "esp_netif_sntp.h"
#include "esp_timer.h"
#include "esp_wifi.h"

namespace {
const char* TAG = "net";
constexpr int64_t kReconnectUs = 3000000;
constexpr time_t kMinSaneEpoch = 1700000000;  // Nov 2023: earlier means SNTP has not set the clock

char g_ip[16] = "";
char g_hwid[20] = "";
volatile bool g_up = false;
esp_timer_handle_t g_retry = nullptr;

void on_got_ip(void*, esp_event_base_t, int32_t, void* data) {
    const auto* ev = static_cast<ip_event_got_ip_t*>(data);
    snprintf(g_ip, sizeof(g_ip), IPSTR, IP2STR(&ev->ip_info.ip));
    g_up = true;
    ESP_LOGI(TAG, "got ip %s", g_ip);
}

void on_lost_ip(void*, esp_event_base_t, int32_t, void*) {
    g_up = false;
    g_ip[0] = '\0';
}

void retry_wifi(void*) { esp_wifi_connect(); }

void on_wifi(void*, esp_event_base_t, int32_t id, void*) {
    if (id == WIFI_EVENT_STA_START) {
        esp_wifi_connect();
    } else if (id == WIFI_EVENT_STA_DISCONNECTED) {
        g_up = false;
        g_ip[0] = '\0';
        esp_timer_start_once(g_retry, kReconnectUs);  // reconnect loop
    }
}

// Truncating copy into a fixed byte field (zero-filled by the caller).
void copy_text(uint8_t* dst, size_t cap, const char* src) {
    const size_t n = strlen(src) < cap - 1 ? strlen(src) : cap - 1;
    memcpy(dst, src, n);
}

void start_wifi(const Settings& s) {
    if (s.wifi_ssid[0] == '\0') {
        ESP_LOGW(TAG, "no wifi_ssid configured; network stays down");
        return;
    }
    esp_netif_create_default_wifi_sta();
    wifi_init_config_t init = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&init));
    ESP_ERROR_CHECK(esp_event_handler_register(WIFI_EVENT, ESP_EVENT_ANY_ID, on_wifi, nullptr));
    wifi_config_t cfg = {};
    copy_text(cfg.sta.ssid, sizeof(cfg.sta.ssid), s.wifi_ssid);
    copy_text(cfg.sta.password, sizeof(cfg.sta.password), s.wifi_pass);
    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &cfg));
    ESP_ERROR_CHECK(esp_wifi_start());
}

void on_eth(void*, esp_event_base_t, int32_t id, void*) {
    if (id == ETHERNET_EVENT_DISCONNECTED) {
        g_up = false;
        g_ip[0] = '\0';
    }
}

// KC868-A8 LAN8720 wiring (Kincony documentation); every pin: verify against schematic.
bool eth_ok(esp_err_t e, const char* what) {
    if (e == ESP_OK) return true;
    ESP_LOGE(TAG, "ethernet %s failed: %s; board runs without network", what, esp_err_to_name(e));
    return false;
}

void start_eth() {
    constexpr gpio_num_t kPhyPower = GPIO_NUM_12;  // verify against schematic
    constexpr int kMdc = 23;                        // verify against schematic
    constexpr int kMdio = 18;                       // verify against schematic
    constexpr int kPhyAddr = 0;                     // verify against schematic
    gpio_set_direction(kPhyPower, GPIO_MODE_OUTPUT);
    gpio_set_level(kPhyPower, 1);  // oscillator/PHY power enable
    esp_netif_config_t ncfg = ESP_NETIF_DEFAULT_ETH();
    esp_netif_t* netif = esp_netif_new(&ncfg);
    eth_esp32_emac_config_t emac = ETH_ESP32_EMAC_DEFAULT_CONFIG();
    emac.smi_gpio.mdc_num = kMdc;
    emac.smi_gpio.mdio_num = kMdio;
    emac.clock_config.rmii.clock_mode = EMAC_CLK_OUT;           // verify against schematic
    emac.clock_config.rmii.clock_gpio = EMAC_CLK_OUT_180_GPIO;  // GPIO17; verify against schematic
    eth_mac_config_t mac_cfg = ETH_MAC_DEFAULT_CONFIG();
    eth_phy_config_t phy_cfg = ETH_PHY_DEFAULT_CONFIG();
    phy_cfg.phy_addr = kPhyAddr;
    phy_cfg.reset_gpio_num = -1;
    esp_eth_mac_t* mac = esp_eth_mac_new_esp32(&emac, &mac_cfg);
    esp_eth_phy_t* phy = esp_eth_phy_new_lan87xx(&phy_cfg);
    esp_eth_config_t ecfg = ETH_DEFAULT_CONFIG(mac, phy);
    esp_eth_handle_t eth = nullptr;
    if (!eth_ok(esp_eth_driver_install(&ecfg, &eth), "driver install")) return;
    if (!eth_ok(esp_netif_attach(netif, esp_eth_new_netif_glue(eth)), "netif attach")) return;
    if (!eth_ok(esp_event_handler_register(ETH_EVENT, ESP_EVENT_ANY_ID, on_eth, nullptr), "event register")) return;
    eth_ok(esp_eth_start(eth), "start");
}
}  // namespace

void net_start(const Settings& settings) {
    uint8_t mac[6] = {};
    esp_read_mac(mac, ESP_MAC_WIFI_STA);
    snprintf(g_hwid, sizeof(g_hwid), "esp32-%02x%02x%02x%02x%02x%02x", mac[0], mac[1], mac[2], mac[3], mac[4], mac[5]);

    esp_timer_create_args_t targs = {};
    targs.callback = retry_wifi;
    targs.dispatch_method = ESP_TIMER_TASK;
    targs.name = "wifi_retry";
    ESP_ERROR_CHECK(esp_timer_create(&targs, &g_retry));
    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    ESP_ERROR_CHECK(esp_event_handler_register(IP_EVENT, IP_EVENT_STA_GOT_IP, on_got_ip, nullptr));
    ESP_ERROR_CHECK(esp_event_handler_register(IP_EVENT, IP_EVENT_ETH_GOT_IP, on_got_ip, nullptr));
    ESP_ERROR_CHECK(esp_event_handler_register(IP_EVENT, IP_EVENT_STA_LOST_IP, on_lost_ip, nullptr));
    ESP_ERROR_CHECK(esp_event_handler_register(IP_EVENT, IP_EVENT_ETH_LOST_IP, on_lost_ip, nullptr));
    if (settings.use_eth) start_eth(); else start_wifi(settings);

    esp_sntp_config_t sntp = ESP_NETIF_SNTP_DEFAULT_CONFIG("pool.ntp.org");
    ESP_ERROR_CHECK(esp_netif_sntp_init(&sntp));
}

bool net_up() { return g_up; }
const char* net_ip() { return g_ip; }
const char* net_hardware_id() { return g_hwid; }

bool net_iso_time(char* out, size_t cap) {
    const time_t now = time(nullptr);
    if (now < kMinSaneEpoch) return false;
    struct tm t;
    gmtime_r(&now, &t);
    return strftime(out, cap, "%Y-%m-%dT%H:%M:%SZ", &t) > 0;
}
