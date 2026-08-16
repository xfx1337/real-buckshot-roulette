"""
Шутки в трубке: случайный звонок посреди партии.

Телефон за столом до сих пор говорил только по делу. Карточка выдавала номер,
игрок набирал, информатор называл патрон; лупа звонила сама, игрок снимал
трубку и слышал то же самое другими словами. Оба пути — часть игры, оба
предсказуемы, и оба случаются ровно тогда, когда кто-то потратил предмет.

Здесь — то, что случается само. В случайный момент, когда линия свободна,
аппарат либо звонит сам, либо на экране появляется номер с просьбой позвонить,
и в трубке играет мем из МЕМЫ/. К раскладу патронов это отношения не имеет и
иметь не должно: это шум за столом, а не подсказка.

Два направления, и разница между ними — в том, кто первый снял трубку:

    входящий    аппарат звонит, игрок берёт трубку, мем начинается сразу.
                Гудков нет и быть не может: тот, кто ответил на звонок, уже
                соединён — гудок в его ухе означал бы, что звонит он.

    исходящий   на экране появляется номер, игрок идёт и набирает. В трубке три
                гудка КПВ, затем мем. Это тот же путь, которым ходит номер от
                карточки, и звучать он обязан так же — иначе набор без гудков
                читается как номер, который не соединился.

Свободна ли линия, решает не этот модуль: он спрашивает у voip_service, и
спрашивает перед каждым звонком, а не один раз при планировании. Между
решением «пора» и самим звонком проходят секунды, и за них дилер вполне может
выдать карточку телефона — а мем, вклинившийся в подсказку про патрон, испортит
ровно то, ради чего аппарат за столом и стоит.
"""

from __future__ import annotations

import json
import random
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import tts.sessions

ROOT = Path(__file__).resolve().parent.parent

# Откуда берутся файлы. Папка лежит в корне проекта и наполняется вручную:
# уронить туда mp3 — это всё, что требуется, чтобы мем попал в ротацию.
MEMES_DIR = ROOT / "МЕМЫ"

# Текстовые мемы — то же самое, но на экран, а не в трубку. Один файл, потому
# что править список строк удобнее в одном месте, чем в тридцати.
TEXT_DIR = ROOT / "text_memes"
TEXT_FILE = TEXT_DIR / "memes.json"

# Что считается мемом. Тот же набор, что понимает voip/scripts/sounds.py, —
# играть их будет он же.
SUFFIXES = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".opus",
            ".flac", ".aiff", ".aif", ".wma", ".mp4"}


# ── настройки ───────────────────────────────────────────────────────────
#
# Всё, что решает, как часто и как громко это происходит, живёт в config.json,
# потому что решается оно за столом и по ходу репетиции, а не в коде. Значения
# ниже — то, с чем система работает, если в конфигурации ничего не сказано.

DEFAULTS = {
    # Включено ли вообще. Мемы — не часть сценария, и вечер, где они не нужны,
    # не должен требовать правки кода.
    "enabled": True,
    # Границы паузы между звонками, секунды. Разброс широкий намеренно: смысл
    # в том, что звонок застаёт врасплох, а предсказуемый интервал перестаёт
    # заставать врасплох к третьему разу.
    "min_seconds": 240,
    "max_seconds": 600,
    # Доля входящих среди всех звонков. Остальное — просьба позвонить самому.
    # Больше половины, потому что звонящий сам по себе аппарат — событие за
    # столом, а номер на экране требует, чтобы кто-то на экран смотрел.
    "incoming_share": 0.6,
    # Сколько живёт номер, выданный для исходящего мема, секунды. Дольше
    # игрового: за игровым идут, потому что он даёт подсказку, а за этим —
    # когда руки дойдут.
    "outgoing_seconds": 120,
    # Номера, из которых выдаются исходящие мемы. Вне диапазона слотов
    # оператора (510–529) и вне трёхзначных игровых: набранный мем-номер
    # должен опознаваться до всего остального, и пересечение с чужим
    # диапазоном означало бы мем, съевший проверочный номер.
    "numbers": ["600", "601", "602", "603", "604", "605",
                "606", "607", "608", "609"],
    # Не повторять мем, пока не сыграли столько других. Ноль — разрешить
    # повторы. По умолчанию треть библиотеки: этого хватает, чтобы за вечер
    # один и тот же голос не прозвучал дважды подряд, и не настолько много,
    # чтобы маленькая папка исчерпалась и застряла.
    "no_repeat": 0,

    # ── текстовые мемы ──────────────────────────────────────────────────
    # Вставка на телевизоре вместо звонка. Живёт в том же расписании и тем же
    # чередом: см. app/server.py, _meme_loop().
    "text_enabled": True,
    # Доля текстовых среди всех вставок. Текст дешевле звонка — он никого не
    # поднимает с места, — поэтому его больше.
    "text_share": 0.6,
    # Сколько текст висит на экране после того, как допечатался, секунды.
    "text_hold": 5.0,
    # Миллисекунд на знак при печати. То же, что у сообщений оператора.
    "text_speed": 45,
    # Показывать поверх видео и камер, а не на чёрном фоне. По умолчанию
    # поверх: мем — это вставка, а не объявление, и гасить ради него картинку
    # значит придавать ему вес, которого у него нет.
    "text_over_video": True,
}


def config() -> dict:
    """Настройки мемов с подставленными умолчаниями."""
    data = dict(DEFAULTS)
    try:
        raw = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return data
    section = raw.get("memes")
    if isinstance(section, dict):
        data.update(section)
    return data


# ── библиотека ──────────────────────────────────────────────────────────

@dataclass
class Meme:
    """Файл и то, как он называется в журнале."""
    path: Path

    @property
    def title(self) -> str:
        """Имя файла без расширения — оператору по нему и искать."""
        return self.path.stem


def library() -> list[Meme]:
    """Все мемы на диске, в порядке имён.

    Читается на каждый звонок, а не кэшируется: файлы кладут в папку руками, в
    том числе посреди вечера, и мем, добавленный между партиями, должен играть
    без перезапуска сервера.
    """
    if not MEMES_DIR.is_dir():
        return []
    return [Meme(path=path) for path in sorted(MEMES_DIR.iterdir())
            if path.is_file() and path.suffix.lower() in SUFFIXES]


# Что уже играло, в порядке от старого к новому. Хранится здесь, а не в
# истории телефонии, потому что вопрос узкий: не повторяться. Длина
# ограничена настройкой no_repeat при каждом выборе.
_recent: list[str] = []


def pick() -> Optional[Meme]:
    """Случайный мем, по возможности не из недавних. None, если папка пуста."""
    memes = library()
    if not memes:
        return None

    settings = config()
    window = int(settings.get("no_repeat") or 0)
    if window <= 0:
        # Треть библиотеки: см. DEFAULTS["no_repeat"]. Считается от того, что
        # на диске сейчас, а не от числа при старте, — папку пополняют по ходу.
        window = max(1, len(memes) // 3)
    # Окно не может съесть библиотеку целиком: иначе выбирать станет не из
    # чего и звонок замолчит совсем.
    window = min(window, max(0, len(memes) - 1))

    recent = set(_recent[-window:]) if window else set()
    fresh = [meme for meme in memes if meme.title not in recent]
    chosen = random.choice(fresh or memes)

    _recent.append(chosen.title)
    # Обрезается щедро: список нужен ровно на длину окна, а окно может
    # вырасти вместе с папкой.
    del _recent[:-64]
    return chosen


def forget() -> None:
    """Забыть, что играло. Для новой партии — история прошлой ей не нужна."""
    _recent.clear()
    _recent_text.clear()


# ── текстовые мемы ──────────────────────────────────────────────────────
#
# То же самое, что выше, но на экран. Отдельная библиотека, отдельная история
# показанного, общее с телефоном расписание: см. _meme_loop() в app/server.py,
# где решается, чем будет очередная вставка.

@dataclass
class TextMeme:
    """Строка на экран и то, как её показать."""
    text: str
    hold: Optional[float] = None
    speed: Optional[int] = None
    over_video: Optional[bool] = None

    @property
    def title(self) -> str:
        """Как мем называется в журнале — первая строка, коротко."""
        first = self.text.strip().splitlines()[0] if self.text.strip() else ""
        return first[:40] + ("…" if len(first) > 40 else "")

    def command(self, settings: dict) -> dict:
        """Готовая команда телевизору.

        Умолчания берутся из настроек, а не подставляются здесь: мем задаёт
        только то, что у него отличается, и строка без единого поля — это
        строка, показанная так, как показываются все.
        """
        hold = self.hold if self.hold is not None else settings.get(
            "text_hold", DEFAULTS["text_hold"])
        speed = self.speed if self.speed is not None else settings.get(
            "text_speed", DEFAULTS["text_speed"])
        over = self.over_video if self.over_video is not None else settings.get(
            "text_over_video", DEFAULTS["text_over_video"])
        return {
            "action": "message",
            "text": self.text,
            # Та же граница, что у сообщения оператора: длиннее на кинескопе
            # не помещается читаемым кеглем.
            "speed": max(5, min(400, int(speed))),
            "hold": max(0.0, min(600.0, float(hold))),
            "over_video": bool(over),
            "beep": True,
        }


def text_library() -> list[TextMeme]:
    """Все текстовые мемы из text_memes/memes.json.

    Читается на каждый показ, как и папка со звуками: список правят руками, в
    том числе между партиями, и добавленная строка должна появиться без
    перезапуска сервера.

    Битый или отсутствующий файл — это пустой список, а не исключение. Мемы —
    украшение вечера, и опечатка в JSON не повод ронять расписание вместе с
    телефонными звонками, которые лежат в том же цикле.
    """
    try:
        raw = json.loads(TEXT_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []

    # Допускаются обе формы: голый список и объект с ключом memes. Первая
    # короче для того, кто правит файл руками; вторая нужна, чтобы рядом с
    # мемами лежали комментарии, которых в JSON иначе не бывает.
    items = raw if isinstance(raw, list) else raw.get("memes", [])
    if not isinstance(items, list):
        return []

    out: list[TextMeme] = []
    for item in items:
        if isinstance(item, str):
            text = item.strip()
            if text:
                out.append(TextMeme(text=text))
            continue
        if not isinstance(item, dict):
            continue
        text = str(item.get("text", "")).strip()
        if not text:
            continue
        out.append(TextMeme(
            text=text,
            hold=_optional_float(item.get("hold")),
            speed=_optional_int(item.get("speed")),
            over_video=(bool(item["over_video"])
                        if "over_video" in item else None),
        ))
    return out


def _optional_float(value) -> Optional[float]:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def _optional_int(value) -> Optional[int]:
    try:
        return None if value is None else int(value)
    except (TypeError, ValueError):
        return None


_recent_text: list[str] = []


def pick_text() -> Optional[TextMeme]:
    """Случайный текстовый мем, по возможности не из недавних."""
    memes = text_library()
    if not memes:
        return None

    settings = config()
    window = int(settings.get("no_repeat") or 0)
    if window <= 0:
        window = max(1, len(memes) // 3)
    window = min(window, max(0, len(memes) - 1))

    recent = set(_recent_text[-window:]) if window else set()
    fresh = [meme for meme in memes if meme.text not in recent]
    chosen = random.choice(fresh or memes)

    _recent_text.append(chosen.text)
    del _recent_text[:-64]
    return chosen


def text_enabled() -> bool:
    return bool(config().get("text_enabled", DEFAULTS["text_enabled"]))


def text_next() -> bool:
    """Быть ли очередной вставке текстовой, а не телефонной.

    Решается здесь, а не в расписании, потому что это настройка вечера:
    доля текста в общей ротации.
    """
    share = float(config().get("text_share", DEFAULTS["text_share"]))
    return random.random() < share


# ── номера для исходящих ────────────────────────────────────────────────
#
# Исходящий мем — это номер на экране, который игрок набирает на диске. Номер
# должен существовать ровно столько, сколько показан, и опознаваться игрой
# раньше, чем телефония пойдёт искать его в слотах оператора.
#
# Реестр tts.registry для этого не годится: он ведёт билеты с озвученным
# текстом, привязкой к игроку и раунду, и мем — ни то, ни другое, ни третье.
# Поэтому свой, маленький: номер, файл и срок.

@dataclass
class Pending:
    """Выданный номер, который ещё ждёт, чтобы его набрали."""
    number: str
    meme: Meme
    expires: float

    @property
    def alive(self) -> bool:
        return time.time() < self.expires


_pending: dict[str, Pending] = {}


def issue(meme: Meme, seconds: Optional[int] = None) -> Optional[Pending]:
    """Занять номер под этот мем и вернуть его. None, если все номера заняты."""
    settings = config()
    if seconds is None:
        seconds = int(settings.get("outgoing_seconds",
                                   DEFAULTS["outgoing_seconds"]))

    # Истёкшие — не заняты. Чистится здесь, а не по таймеру: единственное
    # место, где занятость номера вообще имеет значение.
    for number in [n for n, p in _pending.items() if not p.alive]:
        _pending.pop(number, None)

    numbers = [str(n) for n in settings.get("numbers", DEFAULTS["numbers"])]
    free = [number for number in numbers if number not in _pending]
    if not free:
        return None

    entry = Pending(number=random.choice(free), meme=meme,
                    expires=time.time() + seconds)
    _pending[entry.number] = entry
    return entry


def redeem(number: str) -> Optional[Pending]:
    """Забрать мем по набранному номеру. Один раз: набранный номер гаснет.

    None и для чужого номера, и для истёкшего. Разница между ними есть, но не
    здесь: обе стороны этого вызова умеют только играть мем или не играть его.
    """
    entry = _pending.pop(str(number), None)
    if entry is None or not entry.alive:
        return None
    return entry


def known(number: str) -> bool:
    """Наш ли это номер вообще — хоть выданный, хоть уже истёкший.

    Нужно, чтобы отличить «мем-номер, который опоздали набрать» от номера, к
    мемам отношения не имеющего: первому положен отказ в трубку, второй идёт
    прежним путём, к слотам оператора.
    """
    settings = config()
    numbers = [str(n) for n in settings.get("numbers", DEFAULTS["numbers"])]
    return str(number) in numbers


def reserve_numbers() -> None:
    """Забрать мем-номера у реестра игровых билетов.

    Игровые номера выдаются случайно из 100–999, и этот диапазон накрывает
    мем-номера целиком. Совпадение не выглядит как ошибка ни с одной стороны:
    набранный номер разбирается по очереди, мемы отвечают первыми — и игрок,
    набравший номер со своей карточки, услышал бы мем вместо подсказки про
    патрон, за которую отдал предмет.

    Поэтому блок изымается до того, как выдан первый билет. Реестру не нужно
    знать, чей он и зачем: ему сказано, что эти номера заняты.
    """
    tts.sessions.reserve(config().get("numbers", DEFAULTS["numbers"]))


# Изымается при импорте, а не при install(): телефония поднимается лениво, при
# первом обращении к ней, а билеты выдаются с первой же карточки телефона — и
# между этими двумя моментами реестр раздавал бы номера, ничего не зная о
# мемах. Импорт же случается раньше всего, что способно выдать билет.
reserve_numbers()


def clear() -> None:
    """Погасить все выданные номера. Конец партии — конец их срока."""
    _pending.clear()


def pending() -> list[Pending]:
    """Живые выданные номера. Для панели и для журнала."""
    return [entry for entry in _pending.values() if entry.alive]


# ── расписание ──────────────────────────────────────────────────────────

def next_delay() -> float:
    """Сколько ждать до следующего звонка, секунды."""
    settings = config()
    low = float(settings.get("min_seconds", DEFAULTS["min_seconds"]))
    high = float(settings.get("max_seconds", DEFAULTS["max_seconds"]))
    if high < low:
        low, high = high, low
    return random.uniform(low, high)


def incoming_next() -> bool:
    """Звонить самим или просить позвонить."""
    share = float(config().get("incoming_share", DEFAULTS["incoming_share"]))
    return random.random() < share


def enabled() -> bool:
    return bool(config().get("enabled", DEFAULTS["enabled"]))
