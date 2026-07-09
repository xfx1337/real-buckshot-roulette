#!/usr/bin/env python3
"""
Генерирует esp/config.h из корневого config.json, чтобы прошивка ESP32 брала
настройки из того же единого источника, что и Python-сервер.

Запуск (из корня проекта или из папки esp):
    python esp/gen_config.py

config.h попадает в .gitignore (содержит пароль Wi-Fi) — генерируй его локально
перед прошивкой.
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.json"
OUTPUT_PATH = ROOT / "esp" / "config.h"

# Тот же стриппер JSONC-комментариев, что использует сервер (app/config.py) —
# один источник истины, чтобы config.json с //-подписями читался одинаково.
sys.path.insert(0, str(ROOT))
from app.config import strip_jsonc  # noqa: E402


def esc(s: str) -> str:
    """Экранирование строки для C-литерала."""
    return s.replace("\\", "\\\\").replace('"', '\\"')


def main() -> None:
    if not CONFIG_PATH.exists():
        raise SystemExit(
            f"Не найден {CONFIG_PATH}. Скопируй config.example.json в config.json "
            f"и укажи свои значения."
        )

    with open(CONFIG_PATH, encoding="utf-8") as f:
        cfg = json.loads(strip_jsonc(f.read()))

    esp = cfg["esp"]
    pins = esp["pins"]
    remote = esp["trigger_remote"]

    # Прошивка сама добавляет "/api/..." к базовому URL, поэтому завершающий
    # слэш здесь приведёт к "//api/..." и ответу 404 от сервера. Срезаем его.
    esp["server_base_url"] = esp["server_base_url"].rstrip("/")

    # Тайминги/пороги. Значения по умолчанию — на случай старого config.json без
    # блока "timings", чтобы генерация не падала. Единица измерения (мс/шт) —
    # см. комментарии в esp.ino рядом с #define.
    t = esp.get("timings", {})
    defaults = {
        "poll_interval_ms": 1000,
        "http_timeout_ms": 1500,
        "solenoid_pulse_ms": 250,
        "solenoid_max_on_ms": 500,
        "trigger_debounce_ms": 250,
        "rf_confirm_count": 2,
        "rf_confirm_window_ms": 300,
        "rf_lockout_ms": 800,
        "wifi_retry_interval_ms": 10000,
        "live_led_blink_ms": 300,
        "wdt_timeout_ms": 5000,
    }
    tv = {k: t.get(k, d) for k, d in defaults.items()}

    content = f"""// АВТОГЕНЕРАЦИЯ — не редактируй вручную.
// Сгенерировано из config.json скриптом esp/gen_config.py.
#pragma once

#define WIFI_SSID "{esc(esp['wifi_ssid'])}"
#define WIFI_PASSWORD "{esc(esp['wifi_password'])}"
#define SERVER_BASE_URL "{esc(esp['server_base_url'])}"

#define TRIGGER_PIN {pins['trigger']}
#define SOLENOID_PIN {pins['solenoid']}
#define LIVE_LED_PIN {pins['live_led']}

#define KNOWN_TRIGGER_CODE {remote['code']}UL
#define KNOWN_TRIGGER_PROTOCOL {remote['protocol']}
#define KNOWN_TRIGGER_BITLENGTH {remote['bitlength']}
// Длительность юнита пульта в мкс (печатается в режиме обучения).
// 0 = автоопределение из sync-паузы (срабатывание со 2-го кадра пачки);
// точное значение = срабатывание с ПЕРВОГО кадра.
#define CFG_TRIGGER_PULSE_US {remote.get('pulse_us', 0)}UL

// ── Тайминги и пороги (из config.json → esp.timings) ──
#define CFG_POLL_INTERVAL_MS {tv['poll_interval_ms']}UL
#define CFG_HTTP_TIMEOUT_MS {tv['http_timeout_ms']}UL
#define CFG_SOLENOID_PULSE_MS {tv['solenoid_pulse_ms']}UL
#define CFG_SOLENOID_MAX_ON_MS {tv['solenoid_max_on_ms']}UL
#define CFG_TRIGGER_DEBOUNCE_MS {tv['trigger_debounce_ms']}UL
#define CFG_RF_CONFIRM_COUNT {tv['rf_confirm_count']}U
#define CFG_RF_CONFIRM_WINDOW_MS {tv['rf_confirm_window_ms']}UL
#define CFG_RF_LOCKOUT_MS {tv['rf_lockout_ms']}UL
#define CFG_WIFI_RETRY_INTERVAL_MS {tv['wifi_retry_interval_ms']}UL
#define CFG_LIVE_LED_BLINK_MS {tv['live_led_blink_ms']}UL
#define CFG_WDT_TIMEOUT_MS {tv['wdt_timeout_ms']}UL
"""

    OUTPUT_PATH.write_text(content, encoding="utf-8")
    print(f"Сгенерирован {OUTPUT_PATH.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
