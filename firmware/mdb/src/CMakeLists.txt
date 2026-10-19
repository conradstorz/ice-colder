# Registers every file under src/ (glob), plus the IDF components the port uses.
FILE(GLOB_RECURSE app_sources ${CMAKE_SOURCE_DIR}/src/*.*)

idf_component_register(SRCS ${app_sources}
                       REQUIRES boardidf nvs_flash console esp_timer esp_system esp_rom esp_hw_support freertos
                                esp_wifi esp_netif esp_event mqtt driver esp_driver_gpio esp_driver_uart
                                esp_driver_rmt
                       INCLUDE_DIRS "." "../boards")
