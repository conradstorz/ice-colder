# Shared ESP-IDF platform for every board firmware: NVS settings, USB console, Wi-Fi/Ethernet/SNTP, MQTT.
# Consumed through EXTRA_COMPONENT_DIRS; board-specific values arrive via BoardIdentity at runtime.
idf_component_register(SRCS "src/settings.cpp" "src/console.cpp" "src/net.cpp" "src/mqtt_publisher.cpp"
                       INCLUDE_DIRS "include"
                       PRIV_INCLUDE_DIRS "../boardcore/src"
                       REQUIRES nvs_flash console esp_timer esp_system esp_rom esp_wifi esp_netif
                                esp_event esp_eth mqtt driver esp_driver_gpio)
