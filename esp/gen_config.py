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
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.json"
OUTPUT_PATH = ROOT / "esp" / "config.h"


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
        cfg = json.load(f)

    esp = cfg["esp"]
    pins = esp["pins"]
    remote = esp["trigger_remote"]

    # Прошивка сама добавляет "/api/..." к базовому URL, поэтому завершающий
    # слэш здесь приведёт к "//api/..." и ответу 404 от сервера. Срезаем его.
    esp["server_base_url"] = esp["server_base_url"].rstrip("/")

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
"""

    OUTPUT_PATH.write_text(content, encoding="utf-8")
    print(f"Сгенерирован {OUTPUT_PATH.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
