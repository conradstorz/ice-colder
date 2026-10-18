#pragma once

// What differs between boards in the shared platform. Static strings; stored by pointer.
struct BoardIdentity {
    const char* subsystem;       // heartbeat/<subsystem> topic leaf, MQTT client-id prefix
    const char* console_prompt;  // e.g. "vend> "
    const char* nvs_namespace;   // NVS namespace for settings (keep stable across firmware updates)
    bool ethernet_supported;     // false: net_start always uses Wi-Fi
};
