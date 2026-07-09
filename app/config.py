"""
Единый конфиг проекта. Все настраиваемые данные (адрес/порт сервера, Wi-Fi и
пины для ESP32) лежат в config.json в корне проекта. Python-часть читает их
отсюда, а прошивка ESP32 получает те же значения через сгенерированный
esp/config.h (см. esp/gen_config.py).

config.json не хранится в git (содержит пароль Wi-Fi) — скопируй его из
config.example.json и подставь свои значения.
"""

import json
import re
from pathlib import Path

# config.json живёт в КОРНЕ проекта (на уровень выше app/) — его же читают
# start.sh и esp/gen_config.py, а docker-compose монтирует туда же.
_ROOT_DIR = Path(__file__).resolve().parent.parent
_CONFIG_PATH = _ROOT_DIR / "config.json"
_EXAMPLE_PATH = _ROOT_DIR / "config.example.json"


def strip_jsonc(text: str) -> str:
    """
    Убирает //-комментарии из JSONC, чтобы можно было подписывать параметры
    прямо в config.json. Работает построчно и НЕ трогает `//` внутри строковых
    значений (например, в "http://..."), отслеживая, находимся ли мы внутри
    строки и не экранирована ли кавычка. Блочные /* */ не поддерживаются —
    в этом конфиге они не нужны.
    """
    out = []
    for line in text.splitlines():
        in_str = False
        esc = False
        cut = None
        for i, ch in enumerate(line):
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
            else:
                if ch == '"':
                    in_str = True
                elif ch == "/" and i + 1 < len(line) and line[i + 1] == "/":
                    cut = i
                    break
        out.append(line if cut is None else line[:cut])
    return "\n".join(out)


def load_config() -> dict:
    if not _CONFIG_PATH.exists():
        raise FileNotFoundError(
            f"Не найден {_CONFIG_PATH.name}. Скопируй {_EXAMPLE_PATH.name} в "
            f"{_CONFIG_PATH.name} и укажи свои значения:\n"
            f"    cp {_EXAMPLE_PATH.name} {_CONFIG_PATH.name}"
        )
    with open(_CONFIG_PATH, encoding="utf-8") as f:
        return json.loads(strip_jsonc(f.read()))


CONFIG = load_config()

# Удобные шорткаты для серверной части.
SERVER_HOST = CONFIG["server"]["host"]
SERVER_PORT = CONFIG["server"]["port"]


# ── Мастер первичной настройки (веб-онбординг) ────────────────────────────
# Значения-заглушки из config.example.json. Пока в config.json стоит любое из
# них — считаем, что развёртывание ещё не настроено, и веб-мастер (/setup)
# должен провести пользователя через настройку.
_PLACEHOLDERS = {
    "wifi_ssid": {"", "YOUR_WIFI_SSID"},
    "wifi_password": {"", "YOUR_WIFI_PASSWORD"},
    "server_base_url": {"", "http://192.168.1.100:8000"},
}


def is_configured(cfg: dict | None = None) -> bool:
    """
    True, если config.json заполнен реальными значениями (не заглушками из
    примера). Используется, чтобы решить, показывать ли мастер настройки:
    непустой SSID + пароль + server_base_url, ни одно не равно шаблонному.
    """
    cfg = cfg if cfg is not None else load_config()
    esp = cfg.get("esp", {})
    for key, placeholders in _PLACEHOLDERS.items():
        val = str(esp.get(key, "")).strip()
        if val in placeholders:
            return False
    return True


def save_config(new_cfg: dict) -> None:
    """
    Записывает config.json целиком (pretty-print, UTF-8). Комментарии JSONC при
    этом теряются — мастер сохраняет чистый JSON. Вызывается из /api/setup/save.
    После записи обновляет модульный кэш CONFIG, чтобы уже поднятый сервер видел
    свежие server.host/port без перезапуска.
    """
    global CONFIG, SERVER_HOST, SERVER_PORT
    with open(_CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(new_cfg, f, ensure_ascii=False, indent=2)
    CONFIG = new_cfg
    SERVER_HOST = new_cfg["server"]["host"]
    SERVER_PORT = new_cfg["server"]["port"]
