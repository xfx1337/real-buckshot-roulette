// АВТОГЕНЕРАЦИЯ — не редактируй вручную.
// Сгенерировано из config.json скриптом esp/gen_config.py.
#pragma once

#define WIFI_SSID "Prototype2G_EXT"
#define WIFI_PASSWORD "1303proto"
#define SERVER_BASE_URL "http://192.168.0.184:8000"

#define TRIGGER_PIN 5
//#define SOLENOID_PIN 5
#define LIVE_LED_PIN 2

#define KNOWN_TRIGGER_CODE 0UL
#define KNOWN_TRIGGER_PROTOCOL 1
#define KNOWN_TRIGGER_BITLENGTH 24
// Длительность юнита пульта в мкс (печатается в режиме обучения).
// 0 = автоопределение из sync-паузы (срабатывание со 2-го кадра пачки);
// точное значение = срабатывание с ПЕРВОГО кадра.
#define CFG_TRIGGER_PULSE_US 0UL

// ── Тайминги и пороги (из config.json → esp.timings) ──
#define CFG_POLL_INTERVAL_MS 1000UL
#define CFG_HTTP_TIMEOUT_MS 1500UL
#define CFG_SOLENOID_PULSE_MS 250UL
#define CFG_SOLENOID_MAX_ON_MS 500UL
#define CFG_TRIGGER_DEBOUNCE_MS 250UL
#define CFG_RF_CONFIRM_COUNT 2U
#define CFG_RF_CONFIRM_WINDOW_MS 300UL
#define CFG_RF_LOCKOUT_MS 800UL
#define CFG_WIFI_RETRY_INTERVAL_MS 10000UL
#define CFG_LIVE_LED_BLINK_MS 300UL
#define CFG_WDT_TIMEOUT_MS 5000UL
