"""
Репетиция без стола.

Партию приходится прогонять раньше, чем собран стол: проверить порядок
раундов, тексты карточек, что показывает телевизор игроку и что видит дилер.
Железа при этом рядом может не быть вовсе — ни АТС, ни аппарата, ни ESP на
дробовике, — и тогда каждая карточка с телефоном упирается в «ЛИНИЯ МОЛЧИТ»,
а выстрел ждёт курка, которого не существует. Ошибка честная: линии
действительно нет. Но она рассказывает про кабель, а проверяется в этот
момент сценарий.

Отсюда — переключатель, который оператор ставит сам, в настройках, и три его
положения:

    off         обычная игра. Ничего не подменяется, отказ железа виден как
                отказ железа. Это состояние по умолчанию и то, в котором
                проходит настоящая партия.

    hardware    тестируем со стойкой на столе. Железо работает и должно
                работать: подмен нет никаких, режим включён только чтобы
                панель показывала «идёт тест» и запись партии не путали с
                настоящей. Разница с off не в поведении, а в том, что
                оператор объявил происходящее репетицией.

    mock        тестируем без железа. Телефония, набор диска и курок
                изображаются в процессе сервера: вызов «проходит», номер
                «набирается», выстрел засчитывается — сценарий доходит до
                конца, ни одна карточка не обрывается в ошибку.

Что именно подменяется в mock, решают не флаги, а места вызова: этот модуль
только держит положение переключателя и выдаёт заглушки, а игровая логика
нигде не разветвляется на «если тест». Она вызывает телефонию как обычно —
через telephony() ниже, — и в mock получает объект, который ведёт себя как
телефония, у которой всё получилось.

Положение живёт в config.json (ключ "test_mode"), а не в памяти: репетиция
переживает перезапуск сервера, который во время неё случается чаще, чем в
партии, и оператор не обнаруживает посреди прогона, что тест молча выключился.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

from app import config as app_config

# Три положения переключателя, ровно как в шапке файла.
OFF = "off"
HARDWARE = "hardware"
MOCK = "mock"
MODES = (OFF, HARDWARE, MOCK)

# Русские названия для панели: оператор читает их, а не значения ключей.
LABELS = {
    OFF: "Боевой режим",
    HARDWARE: "Тест со стойкой",
    MOCK: "Тест без железа",
}

_lock = threading.RLock()
_mode: str = OFF

# Что происходило в mock, по порядку и словами. Дилеру этого достаточно, чтобы
# убедиться: карточка сработала и телефон «зазвонил» бы. Список короткий и с
# потолком — это журнал репетиции, а не история.
_JOURNAL_LIMIT = 200
_journal: list[dict] = []

# Кого будить, когда положение сменилось или в журнал легла строка. Ставит
# сервер: панель дилера должна увидеть перемену, не опрашивая.
_listener: Optional[Callable[[], None]] = None


def _load() -> None:
    """Прочитать положение из config.json при импорте.

    Незнакомое значение — это отредактированный руками конфиг, и лучшее, что
    с ним можно сделать, — считать его боевым режимом: тест, включившийся сам
    по себе из-за опечатки, опаснее теста, который не включился.
    """
    global _mode
    try:
        cfg = app_config.load_config()
    except Exception:                                          # noqa: BLE001
        return
    value = str(cfg.get("test_mode", OFF)).strip().lower()
    if value in MODES:
        _mode = value


_load()


def _persist(value: str) -> None:
    """Записать положение в config.json, не роняя переключение об ошибку диска.

    Незаписанный режим всё равно уже установлен в памяти: репетиция начнётся,
    просто не переживёт перезапуск. Это лучше, чем отказать оператору в
    переключении из-за конфига, доступного только на чтение.
    """
    try:
        cfg = app_config.load_config()
        cfg["test_mode"] = value
        app_config.save_config(cfg)
    except Exception as exc:                                   # noqa: BLE001
        print(f"[test] режим не сохранён в config.json: {exc}")


def _notify() -> None:
    if _listener is None:
        return
    try:
        _listener()
    except Exception as exc:                                   # noqa: BLE001
        print(f"[test] слушатель режима отказал: {exc}")


def set_listener(listener: Optional[Callable[[], None]]) -> None:
    """Кого звать, когда режим сменился или в журнал добавилась строка."""
    global _listener
    _listener = listener


# ── положение переключателя ─────────────────────────────────────────────

def mode() -> str:
    with _lock:
        return _mode


def active() -> bool:
    """Идёт ли репетиция — в любом из двух её видов."""
    return mode() != OFF


def mocking() -> bool:
    """Надо ли изображать железо, которого нет."""
    return mode() == MOCK


def set_mode(value: str) -> str:
    """Переключить режим. Возвращает установленное положение."""
    global _mode
    value = str(value).strip().lower()
    if value not in MODES:
        raise ValueError(f"неизвестный режим: {value!r}")
    with _lock:
        if value == _mode:
            return _mode
        _mode = value
        # Журнал принадлежит прогону, а не сессии: новый режим — новый прогон,
        # и строки предыдущего в нём только мешают.
        _journal.clear()
    _persist(value)
    if value != OFF:
        note("режим", f"включён: {LABELS[value]}")
    _notify()
    return value


# ── журнал репетиции ────────────────────────────────────────────────────

def note(kind: str, text: str) -> None:
    """Записать, что заглушка изобразила. В боевом режиме — ничего не делает."""
    if not active():
        return
    with _lock:
        _journal.append({"time": time.time(), "kind": kind, "text": text})
        if len(_journal) > _JOURNAL_LIMIT:
            del _journal[:len(_journal) - _JOURNAL_LIMIT]
    print(f"[test] {kind}: {text}")
    _notify()


def journal() -> list[dict]:
    with _lock:
        return list(_journal)


def clear_journal() -> None:
    with _lock:
        _journal.clear()
    _notify()


def state() -> dict:
    """Всё, что панели нужно, чтобы нарисовать переключатель и журнал."""
    current = mode()
    return {
        "mode": current,
        "label": LABELS[current],
        "active": current != OFF,
        "mocking": current == MOCK,
        "modes": [{"value": m, "label": LABELS[m]} for m in MODES],
        "journal": journal(),
    }


# ── звук репетиции ──────────────────────────────────────────────────────
# Через сколько после «звонка» трубку снимают, и через сколько после конца
# реплики её кладут. Обе паузы существуют только чтобы прогон читался глазами:
# мгновенный снял-проиграл-положил в журнале выглядит как одна строка, и понять
# по нему, какой из трёх шагов сломался, невозможно.
_ANSWER_DELAY = 2.0
_HANGUP_DELAY = 1.0

# Канал, в который играет трубка. Тот же, что у настоящей телефонии
# (app/phone_output.py): оператор выбрал устройство один раз, и репетиция
# звучит оттуда же, откуда партия.
_PHONE_CHANNEL = "phone"


def _play(path: Path, name: str) -> float:
    """Проиграть файл репетиции. Возвращает длительность в секундах.

    Заглушка подменяет линию, а не звук: файл настоящий, тот же самый, который
    ушёл бы в капсюль. Играется он в канал трубки, если оператор его открыл, а
    иначе — системным плеером, потому что репетиция без железа чаще всего идёт
    и без выбранного устройства, и молчащая проверка голоса бессмысленна.

    Ноль означает «не зазвучало» — файла нет или играть нечем. Это записывается
    в журнал: беззвучный билет (_SILENT_AUDIO в tts_bridge) — штатный случай на
    прогоне без голосов, и отличать его от поломки оператор должен по журналу.
    """
    if not path or not Path(path).is_file():
        note("звук", f"файла нет, играть нечего: {name}")
        return 0.0
    path = Path(path)
    seconds = _duration(path)
    if _play_engine(path, name) or _play_system(path):
        return seconds
    note("звук", f"проиграть {name} нечем: нет ни устройства, ни плеера")
    return 0.0


def _play_engine(path: Path, name: str) -> bool:
    """Отдать файл в канал трубки. False — канала нет, играть будет система."""
    try:
        from app import audio_engine, phone_output

        if not audio_engine.available():
            return False
        channel = audio_engine.status().get("channels", {}).get(_PHONE_CHANNEL, {})
        if not channel.get("open"):
            return False
        return bool(audio_engine.play(path, _PHONE_CHANNEL, 1.0,
                                      phone_output.VOICE_KEY))
    except Exception as exc:                                   # noqa: BLE001
        note("звук", f"канал трубки отказал на {name}: {exc}")
        return False


def _play_system(path: Path) -> bool:
    """Системный плеер — последний рубеж, чтобы репетиция всё же звучала.

    Запускается и не ждётся: длительность уже известна из файла, а вызывающий
    здесь — игровой поток, который останавливать на реплику нельзя.
    """
    import shutil
    import subprocess

    for player in ("afplay", "ffplay", "aplay"):
        binary = shutil.which(player)
        if binary is None:
            continue
        args = [binary]
        if player == "ffplay":
            args += ["-nodisp", "-autoexit", "-loglevel", "quiet"]
        args.append(str(path))
        try:
            subprocess.Popen(args, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
            return True
        except OSError:
            continue
    return False


def _duration(path: Path) -> float:
    """Длительность файла. Ноль, если прочитать нечем.

    На ней держится пауза до отбоя в _FakeTelephony._answer: трубку надо класть
    после реплики, а не посреди неё.
    """
    try:
        import soundfile as sf

        info = sf.info(str(path))
        return float(info.frames) / float(info.samplerate or 1)
    except Exception:                                          # noqa: BLE001
        return 0.0


# ── телефония, которой нет ──────────────────────────────────────────────

class _FakeTelephony:
    """Телефония, у которой всё получилось.

    Повторяет ровно те функции voip_service, которые зовёт игра, и с теми же
    именами и подписями: игровой код не должен знать, с какой из двух он
    говорит. Каждый вызов ложится в журнал репетиции, и каждый — звучит:
    заглушкой подменяется линия, а не голос. Реплика та же самая и из того же
    файла, просто идёт в динамик ноутбука, а не в капсюль через джек.

    Чего заглушка не изображает — это ожидания человека. АТС ждёт, пока трубку
    снимут, потому что ESP сообщит ей об этом рычагом; здесь рычага нет, и
    ждать нечего. Поэтому вызов сам «отвечает» через _ANSWER_DELAY и играет
    реплику, а по её концу сам сообщает отбой — жизненный цикл раунда доходит
    до конца без единого физического действия.
    """

    # Отказ игрового кода ловится по типу; заглушка не отказывает никогда, но
    # обработчики ошибок ссылаются на этот атрибут, и он должен быть тем же.
    VoipError = None            # проставляется ниже, после импорта voip_service

    def play_generated(self, extension: str, name: str, path: Path,
                       detail: str = "", ringback: bool = True) -> dict:
        note("трубка", detail or f"в трубку {extension} пошло бы: {name}")
        _play(path, name)
        return {"ok": True, "extension": extension, "sound": name,
                "ringback": 0, "mocked": True}

    def call_generated(self, extension: str, name: str, path: Path,
                       ring: Optional[int] = None) -> dict:
        note("вызов", f"аппарат {extension} зазвонил бы ({name})")
        threading.Thread(target=self._answer, args=(extension, name, path),
                         name=f"mock-call-{extension}", daemon=True).start()
        return {"ok": True, "extension": extension, "sound": name,
                "mocked": True}

    def _answer(self, extension: str, name: str, path: Path) -> None:
        """Снять трубку за игрока, проиграть реплику и положить её обратно.

        Отбой в конце — не украшение: на нём висит «сними трубку» на экранах,
        и без него надпись досидела бы до своего таймера. Слушателя зовёт тот
        же, кого зовёт рычаг настоящей АТС, так что экран гаснет тем же кодом.
        """
        time.sleep(_ANSWER_DELAY)
        note("рычаг", f"трубку сняли на {extension} (без ESP)")
        seconds = _play(path, name)
        time.sleep(seconds + _HANGUP_DELAY)
        note("рычаг", f"трубку положили на {extension} (без ESP)")
        from app import voip_service
        voip_service._announce_on_hook(extension)

    def check_extension(self, extension: str) -> None:
        return None

    def __getattr__(self, name: str) -> Any:
        # Всё остальное — настоящему voip_service. Панель телефонов, состояние
        # портов и здоровье тракта в тесте показывают правду: оператору важно
        # видеть, что железа нет, там, где он смотрит именно на железо.
        from app import voip_service
        return getattr(voip_service, name)


_fake = _FakeTelephony()


def telephony():
    """Кому отдавать телефонные вызовы: настоящей АТС или заглушке."""
    from app import voip_service
    if not mocking():
        return voip_service
    _FakeTelephony.VoipError = voip_service.VoipError
    return _fake
