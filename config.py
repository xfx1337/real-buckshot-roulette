"""
Единый конфиг проекта. Все настраиваемые данные (адрес/порт сервера, Wi-Fi и
пины для ESP32) лежат в config.json в корне проекта. Python-часть читает их
отсюда, а прошивка ESP32 получает те же значения через сгенерированный
esp/config.h (см. esp/gen_config.py).

config.json не хранится в git (содержит пароль Wi-Fi) — скопируй его из
config.example.json и подставь свои значения.
"""

import json
from pathlib import Path

_CONFIG_PATH = Path(__file__).resolve().parent / "config.json"
_EXAMPLE_PATH = Path(__file__).resolve().parent / "config.example.json"


def load_config() -> dict:
    if not _CONFIG_PATH.exists():
        raise FileNotFoundError(
            f"Не найден {_CONFIG_PATH.name}. Скопируй {_EXAMPLE_PATH.name} в "
            f"{_CONFIG_PATH.name} и укажи свои значения:\n"
            f"    cp {_EXAMPLE_PATH.name} {_CONFIG_PATH.name}"
        )
    with open(_CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)


CONFIG = load_config()

# Удобные шорткаты для серверной части.
SERVER_HOST = CONFIG["server"]["host"]
SERVER_PORT = CONFIG["server"]["port"]
