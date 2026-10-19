#include "boardidf/console.h"

#include <stdio.h>

#include "esp_console.h"
#include "sdkconfig.h"
#include "esp_system.h"

namespace {
const Settings* g_settings = nullptr;

int cmd_set(int argc, char** argv) {
    if (argc != 3) {
        printf("usage: set <key> <value>\n");
        return 1;
    }
    if (!settings_set(argv[1], argv[2])) {
        printf("rejected: unknown key or bad value\n");
        return 1;
    }
    printf("saved; reboot to apply\n");
    return 0;
}

int cmd_show(int, char**) {
    settings_show(*g_settings);
    return 0;
}

int cmd_erase(int, char**) {
    settings_erase();
    printf("erased; reboot to apply\n");
    return 0;
}

int cmd_reboot(int, char**) {
    esp_restart();
    return 0;
}

void add(const char* name, const char* help, esp_console_cmd_func_t fn) {
    esp_console_cmd_t c = {};
    c.command = name;
    c.help = help;
    c.func = fn;
    esp_console_cmd_register(&c);
}
}  // namespace

void console_start(const BoardIdentity& id, const Settings& settings) {
    g_settings = &settings;
    esp_console_repl_t* repl = nullptr;
    esp_console_repl_config_t rc = ESP_CONSOLE_REPL_CONFIG_DEFAULT();
    rc.prompt = id.console_prompt;
    // The transport follows the sdkconfig console: the MDB board (ESP32-S3 DevKitC) uses its native USB
    // port (USB Serial/JTAG), the vending board keeps UART0.
#if CONFIG_ESP_CONSOLE_USB_SERIAL_JTAG
    esp_console_dev_usb_serial_jtag_config_t uc = ESP_CONSOLE_DEV_USB_SERIAL_JTAG_CONFIG_DEFAULT();
    if (esp_console_new_repl_usb_serial_jtag(&uc, &rc, &repl) != ESP_OK) return;
#else
    esp_console_dev_uart_config_t uc = ESP_CONSOLE_DEV_UART_CONFIG_DEFAULT();
    if (esp_console_new_repl_uart(&uc, &rc, &repl) != ESP_OK) return;
#endif
    static char set_help[160];
    int n = snprintf(set_help, sizeof(set_help), "set <key> <value>");
    for (int i = 0; i < settings_extra_count() && n > 0 && n < static_cast<int>(sizeof(set_help)); ++i) {
        n += snprintf(set_help + n, sizeof(set_help) - n, "%s%s%s", i == 0 ? " (" : "; ", settings_extra(i)->help,
                      i == settings_extra_count() - 1 ? ")" : "");
    }
    add("set", set_help, cmd_set);
    add("show", "print settings, secrets masked", cmd_show);
    add("erase", "erase all stored settings", cmd_erase);
    add("reboot", "restart", cmd_reboot);
    esp_console_start_repl(repl);
}
