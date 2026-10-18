# Registers every file under src/ (glob), plus the IDF components the port uses.
FILE(GLOB_RECURSE app_sources ${CMAKE_SOURCE_DIR}/src/*.*)

idf_component_register(SRCS ${app_sources}
                       REQUIRES esp_driver_i2c esp_driver_gpio esp_driver_pcnt esp_adc
                                nvs_flash console esp_timer esp_system esp_rom freertos
                                esp_wifi esp_netif esp_event esp_eth mqtt driver)
