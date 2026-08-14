"""Импорт модулей voip/scripts в адресное пространство игры.

Папка voip/ — самостоятельная подсистема со своим Asterisk, шлюзом AddPac и
прошивкой ESP32. Её код не подлежит изменению: он написан против конкретного
железа, и каждая константа в нём — измеренная характеристика этого железа, а
не предпочтение автора. Поэтому здесь нет ни одной правки в voip/scripts —
только импорт.

Проблема одна: модули voip/scripts импортируют друг друга по короткому имени
(`import gateway`), потому что запускались как скрипты из своей директории.
Внутри пакета app такой импорт не разрешается. Решение — добавить
voip/scripts в sys.path до первого импорта, ровно как это делает
voip/scripts/web.py у себя в шапке.

Пути внутри модулей отсчитываются от voip/ (ROOT = parent.parent), так что
etc/, sounds/ и var/ читаются из папки voip независимо от того, откуда
запущен сервер игры.
"""

import sys
from pathlib import Path

# Корень репозитория: app/voip_bridge.py -> app -> корень.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
VOIP_ROOT = PROJECT_ROOT / "voip"
VOIP_SCRIPTS = VOIP_ROOT / "scripts"

if not VOIP_SCRIPTS.is_dir():
    raise ImportError(
        f"не найдена папка {VOIP_SCRIPTS}. Подсистема телефонии живёт в voip/ "
        "и должна лежать рядом с app/."
    )

# Перед импортом, а не после: модули ниже разрешают друг друга по этому пути.
# Вставляется в начало, чтобы одноимённый модуль из site-packages не победил.
if str(VOIP_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(VOIP_SCRIPTS))

import admin  # noqa: E402
import audio  # noqa: E402
import call  # noqa: E402
import gateway  # noqa: E402
import health  # noqa: E402
import monitor  # noqa: E402
import sounds  # noqa: E402
import tones  # noqa: E402
import watchdog  # noqa: E402

__all__ = [
    "PROJECT_ROOT", "VOIP_ROOT", "VOIP_SCRIPTS",
    "admin", "audio", "call", "gateway", "health", "monitor", "sounds",
    "tones", "watchdog",
]
