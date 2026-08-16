"""Состояние телефонии: то, что раньше держал voip/scripts/web.py.

Подсистема voip была отдельным Flask-приложением на порту 8080 со своим
состоянием процесса: монитор AMI, сторож портов, реестр текущих вызовов,
взведённые звуки, кэш портов шлюза, прогресс долгих операций. Всё это живёт
между запросами и не может быть выведено из БД или из железа — часть данных
существует только здесь (какой звук ждёт снятия трубки, чей канал принадлежит
какой трубке).

Этот модуль переносит то состояние в процесс игры без изменений в логике: те
же структуры, те же блокировки, те же измеренные константы. Отличий два.

Первое — рассылка событий. Flask раздавал их через SSE-очереди; здесь события
уходят в общий вещатель сервера игры, поэтому подписчиком может быть и панель
дилера, и отдельная страница /voip.

Второе — блокирующие операции. Telnet до шлюза и AMI до Asterisk работают на
обычных сокетах, а сервер игры асинхронный. Каждый вызов, который может ждать
секунду и дольше, обёрнут в asyncio.to_thread на стороне роутов, а фоновые
работы (вызов, сброс, перезагрузка) как и раньше уходят в отдельный поток.

Модули voip/scripts не импортируются напрямую — только через app.voip_bridge,
который ставит sys.path. Ни одна строка в voip/ этим кодом не правится.
"""

import os
import re
import threading
import time
from pathlib import Path
from typing import Callable, Optional

from app import phone_output
from app.busy_tracker import get_tracker
from app.voip_bridge import (
    VOIP_ROOT,
    admin,
    audio,
    call,
    gateway,
    health,
    monitor,
    sounds,
    tones,
    watchdog,
)

# ── общее состояние подсистемы ──────────────────────────────────────────

board = monitor.Monitor()

# Освобождает порты, которые держит неисправность, а не разговор. Выключен,
# пока не попросят: он сам циклирует порты, и это должно быть решением, а не
# умолчанием. Почему пара «занят на шлюзе, но канала нет» безопасна для
# вмешательства — см. voip/scripts/watchdog.py.
dog = watchdog.Watchdog(fix=False)

# Вызовы в работе, по номеру трубки: панель гасит кнопку занятой линии и
# показывает, чем кончилась последняя попытка.
_calls: dict[str, dict] = {}
_calls_lock = threading.Lock()

# ── что ждёт, чтобы его услышали ────────────────────────────────────────
#
# Звук не идёт по SIP. Дело шлюза в вызове — подать на линию вызывное
# напряжение, чтобы звонили звонки аппарата; он никогда не отвечает, поэтому
# RTP-поток не устанавливается и Asterisk нечего проигрывать — вызов сам по
# себе висит в Ringing, пока шлюз через тридцать секунд не сдастся.
#
# Звук доходит до капсюля через мини-джек, а момент начала называет ESP: он
# читает рычажный переключатель напрямую, поэтому в секунду поднятия трубки
# шлёт off-hook на /api/dialer. Это и есть сигнал ответа, который есть у
# системы.
#
# Поэтому вызов ставится в два приёма. Звонок на трубку взводит запись здесь
# с именем выбранного звука; пришедший следом off-hook находит её и запускает
# воспроизведение. Невостребованная запись истекает: трубка, которую никто не
# поднял, не должна проиграть свой звук в следующий вызов.
_armed: dict[str, dict] = {}
_armed_lock = threading.Lock()

# Сколько взведённый звук ждёт после того, как звонки смолкли.
#
# Отсчёт от конца вызывного сигнала, а не от начала вызова — в этом и разница.
# Раньше окно было 45 секунд от момента постановки вызова, и тридцатисекундный
# звонок съедал две трети: звук истекал через 15 секунд после того, как аппарат
# затих. Для обычного способа ответить этого мало — человек слышит звонки,
# идёт к аппарату и поднимает трубку уже после того, как звонить перестало.
# Замерено на 105: трубку сняли на 47-й секунде, на две секунды позже лимита в
# 45, и звук был уже выброшен.
#
# Так что это отсрочка после звонка, а всё окно — звонок плюс она. Щедро
# намеренно: цена слишком долгого ожидания — звук, взведённый для трубки,
# которую никто не взял, он истечёт сам и вреда не принесёт; цена слишком
# короткого — поднятая трубка, которая молчит.
ARM_GRACE = 45.0

# Шлюз AP1100F допускает считанные telnet-сессии и отказывает в новых, когда
# они исчерпаны — со стороны это выглядит ровно как умерший шлюз. Каждый путь,
# который открывает сессию, берёт эту блокировку, чтобы панель, открытая в трёх
# браузерах, не исчерпала их опросом.
_gateway_lock = threading.Lock()

# Сводка по портам стоит одного telnet-логина, около секунды. Кэшируется, чтобы
# статусную панель можно было опрашивать часто; вызов или сброс сбрасывают кэш,
# потому что это моменты, когда сводка меняется.
_ports_cache: dict = {"at": 0.0, "value": None}
PORTS_TTL = 6.0

# Взводится, пока шлюз перезагружается или сбрасывается: панель это показывает,
# а остальные обращения к шлюзу отказывают, вместо того чтобы наваливаться на
# устройство, которое на середине возвращения к жизни.
_maintenance: dict = {"busy": False, "what": "", "started": 0.0, "detail": ""}

# ── прогресс долгих операций ────────────────────────────────────────────
#
# Вызов занимает полминуты, перезагрузка две, и обе большую часть времени
# ничего не показывают. Без разбивки интерфейс может сказать только «работаю»,
# что неотличимо от «завис». Поэтому каждая такая операция объявляет свои шаги
# заранее и отмечает их по ходу; панель рисует список и полосу по этим данным.

# done изначально true: ещё ничего не запускалось, а «не готово» при пустом
# списке шагов читается панелью как незавершимая операция.
_progress: dict = {"id": "", "title": "", "steps": [], "done": True,
                   "ok": True, "started": 0.0, "detail": ""}
_progress_lock = threading.Lock()

# Линия, обесточиваемая автоматически после отбоя.
#
# У TX-220 на 106 пробиты ключи линии: шлейф остаётся замкнутым после того, как
# трубку положили, и шлюз не видит разницы между поднятой и лежащей трубкой.
# Никакой опрос порта их не различит — показания одинаковые.
#
# Asterisk же знает. Канал завершается, и Hangup говорит об этом в течение
# секунды. Это тот сигнал, которого шлюз дать не может, поэтому используется
# он: по отбою снять питание с линии на столько, чтобы замыкание прочиталось
# как разрыв и порт сам сел в Idle.
#
# Порты включает оператор: каждая такая операция стоит линии нескольких секунд
# мёртвого состояния после каждого вызова, и нужна она только неисправному
# аппарату.
_auto_power: set[str] = set()
_auto_power_lock = threading.Lock()

# Достаточно, чтобы замкнутый шлейф прочитался разомкнутым; замерено на 106,
# где после односекундного цикла порт снова занимался в пределах тридцати
# секунд, а после шестисекундного держался больше сорока.
AUTO_POWER_SECONDS = 6.0

# Куда уходят события. Сервер игры подставляет сюда свой вещатель при старте:
# так одно и то же событие видит и вкладка «Телефоны» в панели дилера, и
# отдельная страница /voip.
_sink: Optional[Callable[[dict], None]] = None
_sink_lock = threading.Lock()

# Последние события — чтобы страница, открытая посреди игры, нарисовалась не с
# чистого листа. Flask этого не хранил: там панель всегда начинала с /api/state
# и пустого лога.
_history: list[dict] = []
HISTORY_MAX = 200
_history_lock = threading.Lock()

_started = False
_start_lock = threading.Lock()

# Кто разбирает набранный номер до того, как это сделает АТС.
#
# Номера, которые набирают игроки, выдаёт игра и знает о них только она: они
# не заведены ни в плане набора, ни в базе Asterisk. Так что телефония
# спрашивает — «этот твой?» — и играет то, что ей вернут; None означает «не
# мой», и номер идёт прежним путём, к слотам оператора.
#
# Ставится сервером игры (app/tts_bridge.py). Пока не поставлен, эта сторона
# работает ровно как раньше, поэтому подсистему voip можно поднять и без игры.
_game_number_handler: Optional[Callable[[str, str], Optional[dict]]] = None

# Кому сказать, что трубку положили на рычаг. Игре: пока аппарат звонит, у
# игрока на экране висит «сними трубку», и снять её должно то же движение, что
# кладёт трубку обратно, — иначе надпись переживает разговор и висит до
# истечения таймера над человеком, который уже всё услышал.
#
# Ставится сервером игры (app/tts_bridge.py). Вызывается из потока считывателя
# рычага, поэтому то, что сюда поставлено, обязано быть с ним безопасно.
_on_hook_listener: Optional[Callable[[str], None]] = None

# Трубки, которые сейчас подняты, по показаниям рычага. Ведётся здесь, потому
# что больше это состояние никто не хранит: считыватель сообщает переходы, а не
# положение, и вопрос «клали ли трубку, которую снимали» иначе не задать.
#
# Нужно оно ровно для того, чтобы отличить настоящий отбой от ложного. Пока
# аппарат звонит, вызывного напряжения на линии довольно, чтобы дёрнуть
# рычажный контакт, и ESP присылает on-hook от трубки, которая лежала не
# шелохнувшись. Отбой засчитывается только той, что до этого снималась.
_lifted: set[str] = set()
_lifted_lock = threading.Lock()


# ── рассылка событий ────────────────────────────────────────────────────

def set_sink(sink: Optional[Callable[[dict], None]]) -> None:
    """Назначить получателя событий телефонии."""
    global _sink
    with _sink_lock:
        _sink = sink


def set_game_number_handler(
        handler: Optional[Callable[[str, str], Optional[dict]]]) -> None:
    """Назначить разбор набранных номеров игрой.

    Вызывается с (трубка, номер) и возвращает результат, если номер игровой,
    либо None, если нет.
    """
    global _game_number_handler
    _game_number_handler = handler


def set_on_hook_listener(listener: Optional[Callable[[str], None]]) -> None:
    """Назначить, кому сообщать о положенной трубке. Вызывается с номером."""
    global _on_hook_listener
    _on_hook_listener = listener


def _announce_on_hook(extension: str) -> None:
    """Сказать слушателю, что трубку положили, не за счёт вызывающего.

    Этот путь обслуживает рычаг: он гасит звук, разбирает вызов и освобождает
    порт. Экран, который не обновился, — не повод оставить всё перечисленное
    несделанным, поэтому отказ слушателя тут и остаётся.
    """
    listener = _on_hook_listener
    if listener is None:
        return
    try:
        listener(extension)
    except Exception as exc:                                    # noqa: BLE001
        print(f"[voip] слушатель отбоя отказал на {extension}: {exc}")


def _log_label(kind: str) -> str:
    return _LOG_LABEL.get(kind, kind)


# Как каждый вид называется в консоли. У панели свои подписи; это та же
# информация для того, кто смотрит на сервер, а не в браузер — а именно оттуда
# за трубками и наблюдают, пока проводка ещё в работе.
_LOG_LABEL = {
    "off-hook": "трубка снята",
    "on-hook": "трубка положена",
    # Завершение попытки вызова — это шлюз сдался, а не кто-то положил трубку:
    # рычаг сообщает только ESP.
    "call-ended": "вызов завершён",
    "digit": "цифра",
    "number": "набран номер",
    "ringing": "звонит",
    "error": "ошибка",
    "warn": "внимание",
    "info": "",
}


def _log_line(event: monitor.Event) -> None:
    """Одно событие в терминал, из которого запущен сервер.

    Каждое событие доходит до панели через вещатель, но только если браузер
    открыт. Консоль — единственное место, где поднятие трубки видно независимо
    от того, смотрит ли кто-то в интерфейс, и потому именно за ней следят, пока
    считыватель ещё паяется.
    """
    label = _log_label(event.kind)
    who = event.extension if event.extension and event.extension != "-" else "—"
    detail = event.detail or ""
    # Подробность часто уже говорит то же, что сказала бы подпись: у события
    # рычага подробность — слова «трубка снята». Повтор читается как заикание,
    # поэтому подпись добавляется, только когда сообщает что-то сверх.
    if not label:
        what = detail
    elif not detail or detail == label:
        what = label
    else:
        what = f"{label} {detail}"
    print(f"[voip] {event.clock}  {who:>4}  {what}", flush=True)


def _fan_out(event: monitor.Event) -> None:
    payload = event.as_dict()
    _log_line(event)
    with _history_lock:
        _history.append(payload)
        del _history[:-HISTORY_MAX]
    with _sink_lock:
        sink = _sink
    if sink is not None:
        # Один сломанный получатель — закрывшийся браузер — не должен уронить
        # поток монитора, а с ним и всех остальных.
        try:
            sink(payload)
        except Exception:  # noqa: BLE001
            pass


def history() -> list[dict]:
    with _history_lock:
        return list(_history)


def _push_progress() -> None:
    """Отправить текущий прогресс всем открытым панелям."""
    snapshot = progress_snapshot()
    payload = {"kind": "progress", "extension": "-", "detail": "",
               "at": time.time(), "clock": "", "direction": "",
               "progress": snapshot}
    with _sink_lock:
        sink = _sink
    if sink is not None:
        try:
            sink(payload)
        except Exception:  # noqa: BLE001
            pass


def progress_snapshot() -> dict:
    with _progress_lock:
        return {
            "id": _progress["id"], "title": _progress["title"],
            "done": _progress["done"], "ok": _progress["ok"],
            "detail": _progress.get("detail", ""),
            "steps": [dict(s) for s in _progress["steps"]],
        }


def _progress_start(job_id: str, title: str, steps: list[str]) -> None:
    with _progress_lock:
        _progress.update(
            id=job_id, title=title, done=False, ok=True, started=time.time(),
            detail="",
            steps=[{"label": s, "state": "waiting", "detail": ""} for s in steps],
        )
    _push_progress()


def _progress_step(index: int, state: str, detail: str = "") -> None:
    """Отметить один шаг. state: running | ok | fail | skip."""
    with _progress_lock:
        if 0 <= index < len(_progress["steps"]):
            _progress["steps"][index].update(state=state, detail=detail)
    _push_progress()


def _progress_finish(ok: bool, detail: str = "") -> None:
    with _progress_lock:
        _progress.update(done=True, ok=ok)
        for step in _progress["steps"]:
            # Всё, что осталось в ожидании к концу работы, уже не запустится;
            # оставить «waiting» — значит показать это как ещё предстоящее.
            if step["state"] in ("waiting", "running"):
                step["state"] = "skip" if ok else "fail"
        if detail:
            _progress["detail"] = detail
    _push_progress()


# ── взведённые звуки ────────────────────────────────────────────────────

def _arm(extension: str, sound: "sounds.Sound", loop: bool, ring: int) -> None:
    """Назначить звук, который эта трубка услышит при ответе."""
    with _armed_lock:
        _armed[extension] = {"sound": sound.name, "path": str(sound.source),
                             "loop": loop, "at": time.time(),
                             "expires": time.time() + ring + ARM_GRACE}


def _disarm(extension: str, claiming: bool = False) -> Optional[dict]:
    """Забрать взведённый звук, если он есть и ещё годен.

    claiming=True помечает единственного вызывающего, который действительно
    пытается проиграть звук, — снятие трубки. Только там истёкшая запись
    означает, что что-то пошло не так; пути очистки вызывают это, чтобы
    выбросить звук намеренно, и не должны сообщать о пропущенном ответе.
    """
    with _armed_lock:
        entry = _armed.pop(extension, None)
    if entry is None:
        return None
    if time.time() > entry["expires"]:
        # Сообщается, а не выбрасывается молча. Истёкшая запись и трубка,
        # которую никогда не взводили, возвращают отсюда одинаковый None, а
        # требуют разного: здесь звук был готов, и трубку сняли слишком
        # поздно — это разница между сломанным звуковым трактом и человеком,
        # которому понадобилась минута, чтобы дойти.
        if claiming:
            waited = time.time() - entry["at"]
            # Лимит в том виде, в каком был взведён именно этот вызов, а не
            # константа: окно — это звонок плюс отсрочка, поэтому вызов с
            # длинным звонком ждал дольше, и фиксированное число назвало бы
            # срок, которого не было.
            limit = entry["expires"] - entry["at"]
            _fan_out(monitor.Event(
                "warn", extension,
                f"трубку сняли через {waited:.0f} с — {entry['sound']} уже "
                f"не проигрывается (лимит {limit:.0f} с)",
                direction="inbound"))
        return None
    return entry


# ── порты шлюза ─────────────────────────────────────────────────────────

def ports_summary(force: bool = False) -> dict:
    """Сводка по портам FXS, из кэша, если он не устарел."""
    now = time.time()
    if not force and _ports_cache["value"] is not None \
            and now - _ports_cache["at"] < PORTS_TTL:
        return _ports_cache["value"]
    with _gateway_lock:
        result = health.check_ports()
    _ports_cache.update(at=now, value=result)
    return result


def invalidate_ports() -> None:
    _ports_cache.update(at=0.0, value=None)


# ── автообесточивание линии, которая не умеет сообщить об отбое ─────────

def _auto_power_cycle(extension: str) -> None:
    """Снять питание с линии после того, как её вызов закончился."""
    # Немного времени, чтобы Asterisk дорвал канал. Цикл во время закрытия
    # оставляет порт в Disconnecting, а не в Idle.
    time.sleep(2.0)
    try:
        with _gateway_lock:
            status = gateway.power_cycle_extension(
                extension, down_seconds=AUTO_POWER_SECONDS)
        dog.touched(gateway.port_for(extension))
        invalidate_ports()
        _fan_out(monitor.Event(
            "info", extension,
            f"линия обесточена после отбоя, порт {status}"))
    except gateway.GatewayError as exc:
        _fan_out(monitor.Event("error", extension,
                               f"не удалось обесточить линию: {exc}"))


def _on_handset_event(event: monitor.Event) -> None:
    """Снять питание с линии, как только Asterisk скажет, что вызов кончился."""
    if event.kind != "on-hook":
        return
    with _auto_power_lock:
        wanted = event.extension in _auto_power
    if not wanted:
        return
    threading.Thread(target=_auto_power_cycle, args=(event.extension,),
                     name=f"autopower-{event.extension}", daemon=True).start()


# ── история вызовов ─────────────────────────────────────────────────────

def _on_tracked_event(event: monitor.Event) -> None:
    """Записать начало и конец разговора в app/busy_tracker.py.

    Трекер ведёт свою историю в SQLite, чтобы после игры можно было посмотреть,
    какая трубка сколько была занята. Его словарь описывает канал, а монитор
    сообщает событиями трубки, поэтому связка идёт по имени канала: у трубки
    он в этот момент один, и монитор его знает.

    Ошибки глушатся: журнал статистики не должен ронять поток монитора, на
    котором держится вся телефония.
    """
    if event.extension == "-":
        return
    try:
        tracker = get_tracker()
        channels = board.channels_of(event.extension)
        channel = channels[0] if channels else f"handset/{event.extension}"
        record = {
            "channel": channel,
            "exten": event.extension,
            "slot": gateway.port_for(event.extension),
            "state": event.kind,
            "caller": event.extension,
            "connected": event.direction,
        }
        if event.kind == "off-hook":
            tracker.on_channel_new(record)
        elif event.kind in ("on-hook", "call-ended"):
            tracker.on_channel_hangup(record, cause=event.detail or event.kind)
    except Exception:  # noqa: BLE001
        pass


# ── когда звук кончается сам ────────────────────────────────────────────

def _on_audio_finish(extension: str, sound: str, reason: str) -> None:
    """Файл доиграл до конца или был остановлен.

    Больше этого никто не заметит: звук идёт через джек, а не через вызов, так
    что трубка осталась бы в интерфейсе занятой, а из неё шла бы тишина.
    """
    # on-hook уже записал окончание и сообщил о нём; повтор здесь занёс бы один
    # и тот же момент в журнал дважды.
    if reason == "on-hook":
        return

    # Конец сигнала о состоянии вызова — не конец звука. Гудок сменяется первой
    # цифрой («dialled»), сигнал занято истекает («expired»); ни то, ни другое
    # не вызов, ни то, ни другое не помечалось занятым, и запись об этом дала
    # бы «воспроизведение окончено» для шума, которого никто не просил, — а
    # хуже того, сняла бы флаг занятости с настоящего вызова, если он идёт на
    # этой трубке.
    if reason in ("dialled", "expired"):
        return
    with _calls_lock:
        _calls.setdefault(extension, {}).update(
            busy=False, ok=True, detail=f"{sound} проигран",
            finished=time.time())
    _fan_out(monitor.Event("info", extension, f"{sound}: воспроизведение окончено",
                           direction="outbound"))
    _progress_step(3, "ok", sound)
    _progress_finish(True, f"{sound} проигран")


# ── запуск и остановка подсистемы ───────────────────────────────────────

def start() -> None:
    """Поднять монитор AMI, сторож портов и подготовить гудки.

    Идемпотентно: сервер игры зовёт это при старте, а панель — при первом
    открытии, и второй вызов не должен поднимать второй монитор.
    """
    global _started
    with _start_lock:
        if _started:
            return
        _started = True

    board.subscribe(_fan_out)
    board.subscribe(_on_handset_event)
    board.subscribe(_on_tracked_event)
    audio.player.on_finish = _on_audio_finish

    # Звук трубки — на своё устройство, выбранное оператором. Без этого он идёт
    # туда же, куда всё остальное: afplay играет только в системное устройство
    # по умолчанию, а капсюль воткнут в ноутбук отдельным кабелем и по
    # умолчанию почти никогда не является.
    phone_output.install(audio)

    board.start()

    # Находки сторожа уходят в тот же поток событий, за которым уже следит
    # панель, — освобождение порта видно рядом с вызовами, которых оно
    # касается. Общая блокировка, чтобы обход не открыл telnet-сессию, пока
    # ставится вызов или порт освобождают вручную.
    dog.gateway_lock = _gateway_lock
    dog.on_event = lambda kind, port, message: _fan_out(
        monitor.Event(kind if kind in ("error", "warn") else "info",
                      str(gateway.extension_for(port)) if port != "-" else "-",
                      message))
    dog.start()

    # Готовятся сейчас, а не при первом набранном номере. Это занимает
    # мгновение, и оно иначе пришлось бы ровно между последней цифрой и
    # гудками — единственное место в вызове, где пауза слышна.
    try:
        tones.build_all()
    except OSError as exc:
        print(f"[voip] не удалось подготовить гудки: {exc}", flush=True)


def stop() -> None:
    """Остановить фоновые потоки. Для корректного завершения сервера."""
    global _started
    with _start_lock:
        if not _started:
            return
        _started = False
    try:
        dog.stop()
    except Exception:  # noqa: BLE001
        pass
    try:
        board.stop()
    except Exception:  # noqa: BLE001
        pass
    try:
        audio.player.stop()
    except Exception:  # noqa: BLE001
        pass


# ── снимок для панели ───────────────────────────────────────────────────

def snapshot() -> dict:
    """Всё, что нужно панели, чтобы нарисоваться с нуля."""
    data = board.snapshot()
    # Монитор отдаёт линии списком в порядке номеров — так их удобно рисовать
    # подряд. Панели же нужен доступ по номеру: карточка аппарата 105 ищет своё
    # состояние, а не своё место в списке. Индекс строится здесь, а список
    # остаётся рядом, чтобы обе формы были доступны.
    data["lines_list"] = data.get("lines", [])
    data["lines"] = {line["extension"]: line for line in data["lines_list"]}
    with _calls_lock:
        data["calls"] = {k: dict(v) for k, v in _calls.items()}
    # Звук — та половина вызова, которая не идёт по SIP, поэтому сообщается
    # отдельно: иначе панель покажет вызов, в котором не слышно звука.
    data["audio"] = audio.player.current()
    with _armed_lock:
        data["armed"] = {k: dict(v) for k, v in _armed.items()}
    data["progress"] = progress_snapshot()
    data["maintenance"] = dict(_maintenance)
    with _auto_power_lock:
        data["auto_power"] = sorted(_auto_power)
    data["watchdog"] = {"enabled": dog.fix, "ports": list(dog.ports)}
    return data


def audio_state() -> dict:
    """Что идёт из джека и куда.

    Заслуживает отдельной точки, потому что это часть тракта, которая несёт
    звук, и ничего о ней не видно в состоянии SIP, о котором сообщает
    остальной интерфейс: вызов может выглядеть безупречно и быть беззвучным,
    потому что штекер вынут.
    """
    with _armed_lock:
        armed = {k: dict(v) for k, v in _armed.items()}
    return {
        "playing": audio.player.current(),
        "device": audio.output_device(),
        "armed": armed,
    }


# ── звонок на трубку ────────────────────────────────────────────────────

def _run_call(extension: str, sound: "sounds.Sound", loop: bool, ring: int) -> None:
    def record(**fields) -> None:
        with _calls_lock:
            _calls.setdefault(extension, {}).update(fields)

    _progress_start(f"call-{extension}", f"Вызов на {extension}", [
        "Освобождение порта FXS",
        "Звонок на аппарат",
        "Ожидание снятия трубки (по датчику ESP)",
        "Воспроизведение звука в трубку",
    ])
    try:
        # place() освобождает порт по telnet до звонка, поэтому на это время
        # шлюз нужен ему одному — тот же лимит сессий, из которого черпает
        # статусная панель.
        _progress_step(0, "running")
        with _gateway_lock:
            # На линии, которую неисправность держит замкнутой, односекундного
            # освобождения внутри place() мало: замерено на 106 — порт снова
            # занимается примерно через пятнадцать секунд после освобождения,
            # так что вызов даже десятью секундами позже находит его занятым.
            # Обесточивание здесь, в один приём с постановкой вызова, означает,
            # что линия свободна в момент прихода INVITE, а не несколькими
            # секундами раньше.
            with _auto_power_lock:
                shorted = extension in _auto_power
            if shorted:
                try:
                    gateway.power_cycle_extension(
                        extension, down_seconds=AUTO_POWER_SECONDS)
                    dog.touched(gateway.port_for(extension))
                except gateway.GatewayError:
                    # Само по себе не смертельно: place() тоже освобождает
                    # порт, и вызов ещё может пройти.
                    pass
            _progress_step(0, "ok")
            # Объявляется до того, как вызов ушёл: магистральный канал
            # появляется через миллисекунды после originate и не несёт ничего,
            # что называло бы вызываемую трубку.
            board.expect(extension)
            dog.touched(gateway.port_for(extension))
            _progress_step(1, "running")
            # Взводится до того, как зазвонят звонки, а не после: трубку могут
            # снять через секунду после первого звонка, и off-hook, пришедший
            # раньше, чем назван звук, не нашёл бы что играть.
            _arm(extension, sound, loop, ring)
            result = call.place(extension, sound, loop=loop, ring_seconds=ring,
                                verbose=False, prepared=shorted)

        # place() возвращается, когда шлюз перестаёт звонить. Он никогда не
        # отвечает — ему нечем сообщить Asterisk, что трубку подняли, — поэтому
        # вызов, который прозвонил правильно, всё равно приходит сюда как
        # неудача, и исход SIP ничего не говорит о том, взял ли кто-то трубку.
        # Что действительно произошло — звонки звонили, и сообщается именно это.
        _progress_step(1, "ok", "аппарат звонил")
        _progress_step(2, "running", "ждём датчик трубки")

        # Успел ли ESP сообщить, что трубка поднята. Если успел, воспроизведение
        # началось, пока шлюз ещё звонил, и вызов состоялся; если нет —
        # взведённая выше запись всё ещё ждёт его.
        playing = audio.player.is_playing(extension)
        if playing:
            _progress_step(2, "ok", "трубка снята")
            _progress_step(3, "ok", sound.name)
            _progress_finish(True, f"играет {sound.name}")
            detail = f"играет {sound.name}"
        else:
            with _armed_lock:
                still_armed = extension in _armed
            if still_armed:
                # Звонки смолкли, но звук остаётся взведённым. Это обычный
                # случай, а не отказ: шлюз сдаётся на INVITE примерно через
                # тридцать секунд, place() тогда и возвращается, а человек в
                # это время ещё идёт к аппарату. Решает, что вызов отвечен,
                # именно ESP, и сказать он может через секунды после того, как
                # звонить перестало, — снять взведение здесь значило бы
                # выбросить звук ровно перед тем, как его попросят.
                #
                # Запись истекает сама (см. _arm), поэтому ничего не остаётся
                # лежать под ногами следующего вызова.
                _progress_step(2, "running", "звонок закончился, ждём трубку")
                detail = "звонок прошёл, ждём снятия трубки"
            else:
                # Востребован и уже закончился: короткий звук, который доиграл,
                # пока шлюз ещё звонил.
                _progress_step(2, "ok", "трубка снята")
                _progress_step(3, "ok", sound.name)
                _progress_finish(True, f"{sound.name} проигран")
                detail = f"{sound.name} проигран"

        invalidate_ports()
        record(busy=playing, ok=playing or "проигран" in detail,
               detail=detail, finished=time.time())
        _fan_out(monitor.Event(
            "info", extension, f"вызов: {detail}", direction="outbound",
        ))
    except call.CallError as exc:
        # Взведённый звук уходит вместе с неудавшимся вызовом. Оставленный, он
        # проигрался бы тому, кто следующим поднимет эту трубку.
        _disarm(extension)
        _progress_finish(False, str(exc))
        record(busy=False, ok=False, detail=str(exc), finished=time.time())
        _fan_out(monitor.Event("error", extension, f"вызов не удался: {exc}",
                               direction="outbound"))
    except Exception as exc:  # noqa: BLE001
        _disarm(extension)
        _progress_finish(False, str(exc))
        record(busy=False, ok=False, detail=str(exc), finished=time.time())
        _fan_out(monitor.Event("error", extension, f"вызов не удался: {exc}",
                               direction="outbound"))


class VoipError(RuntimeError):
    """Отказ, который нужно показать оператору. status — код HTTP."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def check_extension(extension: str) -> None:
    """Отвергнуть всё, что не 101–108."""
    try:
        gateway.port_for(extension)
    except gateway.GatewayError as exc:
        raise VoipError(str(exc), 400) from exc


def place_call(extension: str, choice: str, loop: bool = False,
               ring: Optional[int] = None) -> dict:
    """Позвонить на трубку и проиграть в неё звук."""
    check_extension(extension)
    ring = int(ring if ring is not None else call.RING_SECONDS)

    if not choice:
        raise VoipError("выберите звук для воспроизведения", 400)
    try:
        sound = sounds.resolve(choice)
    except sounds.SoundError as exc:
        raise VoipError(str(exc), 400) from exc

    if _maintenance["busy"]:
        raise VoipError("шлюз занят обслуживанием", 409)

    with _calls_lock:
        if _calls.get(extension, {}).get("busy"):
            raise VoipError(f"на {extension} уже идёт вызов", 409)
        _calls[extension] = {"busy": True, "sound": sound.name,
                             "started": time.time(),
                             "detail": "освобождение порта"}

    _fan_out(monitor.Event("info", extension,
                           f"вызов, будет воспроизведено: {sound.name}",
                           direction="outbound"))

    threading.Thread(target=_run_call, args=(extension, sound, loop, ring),
                     name=f"call-{extension}", daemon=True).start()
    return {"ok": True, "extension": extension, "sound": sound.name}


def play_audio(extension: str, choice: str, loop: bool = False) -> dict:
    """Проиграть звук в трубку сейчас, не звоня на неё.

    Для трубки, которая уже снята, — проверить кабель или проиграть что-то
    тому, кто держит аппарат в руке.
    """
    check_extension(extension)
    try:
        sound = sounds.resolve(choice)
    except sounds.SoundError as exc:
        raise VoipError(str(exc), 400) from exc

    try:
        playing = audio.player.start(extension, sound.name, sound.source,
                                     loop=loop)
    except audio.AudioError as exc:
        raise VoipError(str(exc), 500) from exc

    with _calls_lock:
        _calls.setdefault(extension, {}).update(
            busy=True, sound=sound.name, detail="играет в трубку",
            started=time.time())
    _fan_out(monitor.Event("info", extension,
                           f"играет {sound.name} (без вызова)",
                           direction="outbound"))
    return {"ok": True, "playing": playing.as_dict()}


# ── звуки, сгенерированные игрой ────────────────────────────────────────
#
# Синтезированная реплика — не файл из библиотеки оператора. Её никто не
# бросал в voip/sounds/, её незачем там показывать, и живёт она ровно один
# вызов: подсказка, названная вслух, после этого вызова не значит ничего.
#
# Поэтому она обходит sounds.resolve() и приходит сюда готовым путём. Всё
# остальное — тот же тракт: тот же плеер, тот же капсюль, те же события на
# панели, потому что для трубки разницы между синтезом и записью нет.

def _generated(name: str, path: Path) -> "sounds.Sound":
    """Обернуть готовый файл так, как остальной тракт ожидает звук.

    sounds.Sound — это описание, а не владение файлом: путь, имя и
    длительность. Синтезированная реплика уже имеет всё три, так что
    конвертировать и публиковать её незачем — она и так в нужном формате
    (tts/engine.py пишет 8 кГц моно), а публикация нужна только Asterisk,
    который в этой ветке не участвует.
    """
    if not path.is_file():
        raise VoipError(f"нет синтезированного файла: {path}", 500)
    return sounds.Sound(name=name, source=path, converted=path,
                        seconds=sounds._ffprobe_seconds(path))


def play_generated(extension: str, name: str, path: Path, detail: str = "",
                   ringback: bool = True) -> dict:
    """Проиграть готовый файл в уже поднятую трубку.

    Путь набора: человек держит трубку и только что набрал номер. Шлюз здесь
    не нужен вовсе — звук идёт из джека, — а КПВ перед репликой нужен, потому
    что набор без гудков звучит как номер, который не соединился.
    """
    check_extension(extension)
    sound = _generated(name, path)

    with _calls_lock:
        _calls[extension] = {"busy": True, "sound": sound.name,
                             "started": time.time(),
                             "detail": detail or "идут гудки"}

    _fan_out(monitor.Event("info", extension, detail or f"играет {sound.name}",
                           direction="inbound"))

    def answered(ext: str, played: str) -> None:
        with _calls_lock:
            _calls.setdefault(ext, {}).update(detail="говорит")
        _fan_out(monitor.Event("info", ext, "соединено — говорит",
                               direction="inbound"))

    try:
        if ringback:
            audio.player.start_sequence(extension, tones.ringback(),
                                        RINGBACK_SECONDS, sound.name,
                                        sound.source, loop=False,
                                        on_answer=answered)
        else:
            audio.player.start(extension, sound.name, sound.source, loop=False)
    except (audio.AudioError, OSError) as exc:
        with _calls_lock:
            _calls.setdefault(extension, {}).update(
                busy=False, ok=False, detail=str(exc), finished=time.time())
        raise VoipError(f"звук не пошёл: {exc}", 500) from exc

    return {"ok": True, "extension": extension, "sound": sound.name,
            "ringback": RINGBACK_SECONDS if ringback else 0}


def call_generated(extension: str, name: str, path: Path,
                   ring: Optional[int] = None) -> dict:
    """Позвонить на трубку и проиграть в неё готовый файл при ответе.

    Единственное, ради чего шлюз здесь остаётся: вызывное напряжение на линии.
    Звонки аппарата больше нечем зазвонить — считыватель на ESP только читает
    рычаг и диск, выходов у него нет, — так что входящий вызов идёт прежним
    путём, через _run_call.
    """
    check_extension(extension)
    sound = _generated(name, path)
    ring = int(ring if ring is not None else call.RING_SECONDS)

    if _maintenance["busy"]:
        raise VoipError("шлюз занят обслуживанием", 409)

    with _calls_lock:
        if _calls.get(extension, {}).get("busy"):
            raise VoipError(f"на {extension} уже идёт вызов", 409)
        _calls[extension] = {"busy": True, "sound": sound.name,
                             "started": time.time(),
                             "detail": "освобождение порта"}

    _fan_out(monitor.Event("info", extension, "входящий вызов от игры",
                           direction="outbound"))

    threading.Thread(target=_run_call, args=(extension, sound, False, ring),
                     name=f"call-{extension}", daemon=True).start()
    return {"ok": True, "extension": extension, "sound": sound.name}


def stop_audio(extension: Optional[str] = None) -> dict:
    """Заглушить капсюль."""
    if extension:
        check_extension(extension)
    else:
        extension = None

    stopped = audio.player.stop(extension, reason="stopped")
    if stopped and extension:
        with _calls_lock:
            _calls.setdefault(extension, {}).update(
                busy=False, detail="остановлено", finished=time.time())
        _fan_out(monitor.Event("info", extension,
                               "воспроизведение остановлено из интерфейса"))
    return {"ok": True, "stopped": stopped}


def sound_library() -> dict:
    try:
        library = sounds.library()
    except sounds.SoundError as exc:
        raise VoipError(str(exc), 500) from exc
    return {"sounds": [{"name": s.name, "seconds": round(s.seconds, 1)}
                       for s in library.values()]}


def system_health() -> dict:
    """Каждая часть тракта и порты FXS.

    Дешёвые проверки идут на каждый запрос; сводка по портам берётся из кэша,
    потому что стоит telnet-логина, а панель опрашивают часто.
    """
    checks = health.fast()
    ports = ports_summary()
    checks.append(ports)
    return {
        "checks": checks,
        "overall": health.overall(checks),
        "ports": ports.get("ports", []),
        "maintenance": dict(_maintenance),
        "at": time.time(),
    }


def ports_state(fresh: bool = False) -> dict:
    """Как порты FXS видит сам шлюз.

    Отличается от состояний трубок на панели: те берутся из событий вызовов, а
    это то, что сообщает железо, — порт, застрявший в «Disconnecting», виден
    здесь, пока панель ещё считает линию свободной.
    """
    result = ports_summary(force=fresh)
    if result["state"] == health.DOWN:
        raise VoipError(result["detail"], 502)
    return {"ports": result.get("ports", [])}


# ── считыватель дискового номеронабирателя ─────────────────────────────
#
# ESP32, подключённый к рычагу и импульсному контакту ТА-1132, шлёт сюда:
# трубку подняли или положили, каждую цифру по возврату диска и готовый номер,
# когда цифр набрано достаточно. Прошивка в voip/esp/.
#
# Собственная линия аппарата остаётся на своём порту FXS, поэтому его звонок
# по-прежнему звонит на исходящий вызов, поставленный из панели. Здесь
# добавляется обратное направление — трубка просит звук — на аппарате, чей диск
# к шлюзу не подключён вовсе.
#
# ESP считает импульсы и собирает цифры; здесь это не переделывается. Здесь
# решается, что означает готовый номер.

# Общий с DIALER_TOKEN прошивки. Задайте в окружении, чтобы требовать его:
# пустое значение принимает запросы без аутентификации, что разумно только в
# сети, где до этого порта больше некому дотянуться.
DIALER_TOKEN = os.environ.get("DIALER_TOKEN", "")

# Виды, которые шлёт прошивка, и те же, которые monitor.Event уже использует
# для того же самого, когда оно приходит от шлюза.
DIALER_KINDS = ("off-hook", "on-hook", "digit", "number")

# Сколько играет КПВ, прежде чем начнётся звук, для номера, набранного с трубки.
#
# Ничего не вызывается, поэтому это не ожидание чего-либо — это та часть
# вызова, которая делает его похожим на вызов. Три посылки КПВ: сигнал — секунда
# звучания и четыре паузы, так что слышится «гудок ... гудок ... гудок ...», а
# затем ответ, что примерно и есть время дозвона до того, кто стоит рядом со
# своим аппаратом.
#
# Три, а не сколько-нибудь: за столом это отсчёт, который слышно всей комнате, и
# на третьем гудке снимают трубку. Больше было бы правдоподобнее и хуже: человек
# стоит с трубкой у уха и уже набрал, поэтому каждая секунда сверх той, где это
# читается как устанавливающееся соединение, — секунда, в которую ничего не
# происходит.
#
# Кратно кадансу (RINGBACK_ON + RINGBACK_OFF = 5 c в voip/scripts/tones.py):
# файл зациклен, и длительность не по границе цикла обрубила бы последний гудок
# на середине.
RINGBACK_SECONDS = 15.0

# Сколько сигнал занято отвечает номеру, который нельзя проиграть.
#
# С ограничением, в отличие от гудка: трубка, оставленная снятой после
# неправильного номера, иначе несла бы СИП, пока не умрёт процесс, а это, в
# отличие от гудка — линии, которая ждёт, и звучит соответственно, — шум.
# Достаточно долго, чтобы это было безошибочно сигналом отказа, а не сбоем, и
# достаточно коротко, чтобы трубка стихла сама, если её положили на стол.
BUSY_SECONDS = 8.0


def _stop_ringing(extension: str) -> list[str]:
    """Прекратить вызов, звонящий на эту трубку, и назвать ушедшие каналы.

    Поднятая трубка — это ответ, но шлюз его не видит: диск и рычаг читает ESP,
    а шлейф FXS, за которым следит шлюз, ими не замыкается. Поэтому Asterisk
    продолжает считать, что никто не взял трубку, — держит INVITE живым, шлюз
    продолжает звонить звонками, и секунд через тридцать Asterisk сдаётся и
    шлёт CANCEL. Это и есть аппарат, звонящий в руке у того, кто его уже держит.

    Положить канал здесь — то, что сделал бы ответ. Вызывное напряжение
    снимается сразу, и это важно не только из-за шума: именно звонки, звонящие
    в поднятую трубку, дёргают рычажный контакт и порождают ложные показания
    on-hook, из-за которых выбрасывается взведённый звук.

    Отказ не поднимается наверх. Это путь, который запускает звук, и
    недоступный менеджер — повод оставить звонки звонить, а не повод оставить
    трубку немой.
    """
    targets = board.channels_of(extension)
    if not targets:
        return []

    try:
        with call.Manager() as ami:
            live = {c.get("channel", "") for c in ami.channels()}
            targets = [t for t in targets if t in live]
            for channel in targets:
                ami.hangup(channel)
    except call.CallError as exc:
        _fan_out(monitor.Event("warn", extension,
                               f"не удалось остановить звонок: {exc}",
                               direction="inbound"))
        return []

    if targets:
        _fan_out(monitor.Event("info", extension,
                               "звонок остановлен по снятию трубки",
                               direction="inbound"))
    return targets


def _stop_tone(extension: str) -> bool:
    """Заглушить сигнал о состоянии вызова, не трогая звук."""
    try:
        return audio.player.stop_tone(extension)
    except Exception:  # noqa: BLE001
        # Это путь цифры, он должен оставаться быстрым и не имеет права
        # провалить сообщение из-за звука. Оставленный сигнал слышен и
        # неуместен; потерянная цифра — это номер, который никогда не
        # соберётся.
        return False


def _refuse(extension: str, number: str, message: str, status: int) -> dict:
    """Ответить в капсюль на набранный номер, который нельзя проиграть.

    Звонящий держит трубку и слышит то, что делает эта сторона. Ошибка HTTP
    доходит до ESP, которому её некуда деть — прошивка отправляет и идёт
    дальше, — так что отказ, состоящий только из кода состояния, со стороны
    трубки есть номер, набранный в тишину. А тишина звучит ровно так же, как
    рабочий номер до начала КПВ, и звонящий пережидает её, ожидая звука,
    которого не будет.

    Поэтому отказ — это сигнал, которым станция отвечает на невозможный номер:
    СИП, схема «занято», с ограничением, чтобы он не длился столько же, сколько
    поднята трубка.

    Код и сообщение всё равно уходят назад — для журнала и для всего, что
    обращается к этой точке и не является ESP.
    """
    try:
        audio.player.start_tone(extension, tones.busy(), "занято",
                                seconds=BUSY_SECONDS)
    except (audio.AudioError, OSError) as exc:
        _fan_out(monitor.Event("warn", extension,
                               f"не удалось дать сигнал занято: {exc}",
                               direction="inbound"))

    _fan_out(monitor.Event("warn", extension, f"набран {number}: {message}",
                           direction="inbound"))
    raise VoipError(message, status)


def dialer_event(extension: str, kind: str, detail: str = "") -> dict:
    """Одно событие от считывателя трубки."""
    check_extension(extension)

    if kind not in DIALER_KINDS:
        raise VoipError(f"неизвестное событие: {kind!r}", 400)

    # Считыватель — единственное, что знает состояние этой трубки, поэтому
    # каждое его событие уходит на панель независимо от того, начинает ли оно
    # вызов.
    if kind == "off-hook":
        # Сигнал ответа. Больше его в системе никто не производит: шлюз звонит
        # на линию, но не сообщает о поднятой трубке, поэтому вызов становится
        # отвеченным именно здесь, и именно здесь должен начаться звук.
        _fan_out(monitor.Event("off-hook", extension, "трубка снята",
                               direction="inbound"))
        with _lifted_lock:
            _lifted.add(extension)

        # Первым делом, до всего, что может быть медленным или отказать:
        # звонки смолкают в момент движения рычага. Здесь, а не после запуска
        # звука, потому что именно звонки портят показания рычага, а звук,
        # который не пошёл, — всё равно вызов, который не должен продолжать
        # звонить в аппарат, который держат в руках.
        stopped = _stop_ringing(extension)

        armed = _disarm(extension, claiming=True)
        if armed is None:
            # Подняли, а ничего не ждёт — кто-то взял трубку аппарата, который
            # не звонил. Играть нечего, но сказать есть что: поднятой трубке на
            # рабочей станции отвечают гудком, а звонящий держит эту, чтобы
            # набрать. Тишина была бы неотличима от мёртвой линии — того
            # состояния, в котором эта система провела всю свою историю.
            #
            # Без ограничения. Гудок держится, пока его не сменит первая цифра
            # или пока трубку не положат, — как это делает станция.
            try:
                audio.player.start_tone(extension, tones.dial(), "гудок")
            except (audio.AudioError, OSError) as exc:
                # Не та ошибка, с которой звонящий может что-то сделать, и не
                # повод провалить запрос: о рычаге всё равно сообщили, а это и
                # есть назначение точки.
                _fan_out(monitor.Event("warn", extension,
                                       f"не удалось дать гудок: {exc}",
                                       direction="inbound"))
                return {"ok": True, "playing": None, "stopped": stopped}

            _fan_out(monitor.Event("info", extension, "гудок — можно набирать",
                                   direction="inbound"))
            return {"ok": True, "playing": None, "stopped": stopped,
                    "tone": "dial"}

        try:
            playing = audio.player.start(extension, armed["sound"],
                                         Path(armed["path"]),
                                         loop=bool(armed["loop"]))
        except audio.AudioError as exc:
            with _calls_lock:
                _calls.setdefault(extension, {}).update(
                    busy=False, ok=False, detail=str(exc), finished=time.time())
            _fan_out(monitor.Event("error", extension,
                                   f"звук не пошёл: {exc}", direction="inbound"))
            raise VoipError(str(exc), 500) from exc

        with _calls_lock:
            _calls.setdefault(extension, {}).update(
                busy=True, sound=armed["sound"], detail="играет в трубку")
        _progress_step(2, "ok", "трубка снята")
        _progress_step(3, "running", armed["sound"])
        _fan_out(monitor.Event(
            "info", extension,
            f"трубка снята — играет {armed['sound']}", direction="inbound"))
        return {"ok": True, "playing": playing.sound, "stopped": stopped}

    if kind == "on-hook":
        _fan_out(monitor.Event("on-hook", extension, "трубка положена",
                               direction="inbound"))

        # Настоящий это отбой или дрожь рычага под звонками — решается по тому,
        # снимали ли эту трубку. Сказать игре можно только про настоящий: на
        # экране висит «сними трубку», и убрать его по ложному показанию значит
        # погасить надпись за секунду до того, как игрок к аппарату подойдёт.
        with _lifted_lock:
            was_lifted = extension in _lifted
            _lifted.discard(extension)
        if was_lifted:
            _announce_on_hook(extension)

        # Трубка лежит, значит капсюль ни у чьего уха. Звук обязан кончиться
        # здесь: он идёт через джек, а не по линии, поэтому больше в тракте его
        # никто не завершит, и зацикленный файл играл бы в пустую комнату, пока
        # не умрёт процесс.
        playing_stopped = audio.player.stop(extension, reason="on-hook")
        if playing_stopped:
            with _calls_lock:
                _calls.setdefault(extension, {}).update(
                    busy=False, detail="трубка положена", finished=time.time())
            _fan_out(monitor.Event("info", extension,
                                   "воспроизведение остановлено",
                                   direction="inbound"))

        # Звук взведён, но не востребован. Стоит ли его выбрасывать, зависит от
        # того, что делала трубка.
        #
        # Если что-то играло, этот on-hook его и закончил: трубка была поднята
        # и опущена, так что оставшееся взведение устарело и уходит.
        #
        # Если не играло ничего, трубку и не поднимали — а on-hook от лежащей
        # трубки не отбой, а повторное показание аппарата, который не двигался.
        # В этот момент звонят звонки, и вызывного напряжения на линии довольно,
        # чтобы дёрнуть рычажный контакт, поэтому ESP сообщает о переходе,
        # которого аппарат не совершал. Именно выброс звука по такому показанию
        # оставил вызов, взведённый в 18:30:32, немым, когда трубку сняли в
        # 18:31:16: ложный on-hook двенадцатью секундами раньше звук уже
        # выбросил.
        #
        # Поэтому взведение сохраняется. Оно истечёт само, если никто не
        # ответит, — а это и есть случай, от которого ветка защищала.
        if playing_stopped:
            _disarm(extension)

        # Завершение вызова и с этой стороны: диск к шлюзу не подключён, так
        # что положенная трубка не размыкает никакого шлейфа, видимого шлюзу, а
        # уже играющий звук доиграл бы до конца.
        #
        # Под той же защитой, что и взведение выше. Вызов считается «занятым» с
        # момента постановки, поэтому on-hook, пришедший во время звонков,
        # освободил бы порт из-под вызова, который ещё пытается дозвониться до
        # трубки, — ложное показание убило бы тот самый вызов, за конец
        # которого его приняли. Класть трубку может только та, что была
        # действительно поднята.
        with _calls_lock:
            busy = _calls.get(extension, {}).get("busy", False)
        if busy and playing_stopped and not _maintenance["busy"]:
            try:
                with _gateway_lock:
                    gateway.clear_extension(extension)
                invalidate_ports()
                dog.touched(gateway.port_for(extension))
            except gateway.GatewayError as exc:
                _fan_out(monitor.Event("warn", extension,
                                       f"не удалось освободить порт: {exc}"))
        return {"ok": True}

    if kind == "digit":
        # Гудок уходит на первой цифре, как на настоящей станции: он означает
        # «жду номер», а номер уже дают. Оставленный, он играл бы под всем
        # набором, а затем под КПВ, потому что это разные файлы и больше его
        # ничто не остановит.
        #
        # Только сигнал. Уже играющий звук не трогаем — набор на трубке, из
        # которой что-то играет, это просьба звонящего о следующем, и обрывать
        # текущее до того, как новый номер вообще собран, значило бы заглушить
        # звук из-за цифры, которая может не стать номером.
        _stop_tone(extension)
        _fan_out(monitor.Event("digit", extension, detail, direction="inbound"))
        return {"ok": True}

    # kind == "number": готовый номер, то есть просьба о звуке.
    #
    # Сначала — номера, выданные игрой. Их не существует ни в плане набора, ни
    # в базе АТС: игрок получил три цифры с экрана, и что они значат, решает
    # игровой процесс. Поэтому проверка идёт до всего, что смотрит в Asterisk,
    # — иначе выданный игрой номер отвергался бы как «не программируется»
    # раньше, чем кто-нибудь спросил бы, чей он.
    #
    # Не игровой номер падает ниже, в прежний путь со слотами: оператору его
    # оставили как есть — это то, чем проверяют тракт, когда игры нет.
    if _game_number_handler is not None:
        try:
            handled = _game_number_handler(extension, detail)
        except Exception as exc:                                # noqa: BLE001
            # Игровая сторона не должна ронять телефонию: звонящий держит
            # трубку, и отказ, который он слышит, лучше исключения, которое
            # видит только журнал.
            _fan_out(monitor.Event("error", extension,
                                   f"игровой номер {detail}: {exc}",
                                   direction="inbound"))
            handled = None
        if handled is not None:
            return handled

    # Отказать можно тремя способами, и звонящий во всех трёх слышит одно и то
    # же, потому что из капсюля они одно и то же: номер ничего не играет. Какой
    # именно из трёх — принадлежит журналу, где оператор может на это
    # среагировать.
    if not slot_allowed(detail):
        _refuse(extension, detail,
                f"номер {detail} не существует "
                f"(доступны {SLOT_FIRST}–{SLOT_LAST})", 400)

    # Какой звук играет номер — та же настройка, которую программирует панель,
    # и читается она оттуда же, так что добавленный или изменённый там номер
    # действует на диске без всякой перезагрузки.
    assigned = slots_assigned().get(detail, "")
    if not assigned:
        _refuse(extension, detail, f"на номер {detail} не назначен звук", 404)

    try:
        sound = sounds.resolve(assigned)
    except sounds.SoundError as exc:
        # Запрограммирован, но файла за ним нет или он не конвертируется.
        # Звонящий получает тот же отказ: номер назначен, а со стороны трубки
        # назначение, которое нельзя проиграть, — это неработающий номер.
        _refuse(extension, detail, str(exc), 400)

    # Набрано с трубки, значит она уже поднята и звук идёт прямо из джека.
    # Ничего здесь не трогает шлюз.
    #
    # Это направление, обратное кнопке панели, и оно не должно идти её путём.
    # _run_call() звонит на трубку: он освобождает порт FXS и делает originate,
    # что нужно вызову *на* лежащий аппарат. Посланное на уже поднятую трубку,
    # оно делает единственное, что ломает вызов, — обесточивает линию, которую
    # держит звонящий, — и затем ждёт off-hook, который прийти не может, потому
    # что трубка не опускалась, чтобы подняться снова. Поэтому шлюз не трогается
    # вовсе: никуда ничего не набирается, звуковой тракт — это кабель, и эта
    # сторона играет в него.
    #
    # Звонящий слышит то же, что услышал бы от настоящей станции: КПВ, пока
    # соединение устанавливается, затем то, что он набрал. Дальнего конца, чтобы
    # звонить, нет, поэтому КПВ генерируется (voip/scripts/tones.py), и его
    # длина — единственная часть всего этого, которая является решением, а не
    # следствием.
    #
    # Вызов, уже идущий на этой трубке. Набор поверх него — просьба звонящего о
    # следующем, и это разрешено: звук заменяется, как и везде. Но вызов,
    # поставленный *панелью*, не этому звонящему перехватывать, как и тот, что
    # ещё звонит звонками.
    #
    # Проверяется вне отказа, чтобы не держать блокировку через него: _refuse
    # играет сигнал и рассылает событие, и то и другое может занять достаточно,
    # чтобы удержание блокировки застопорило каждую читающую её панель.
    with _calls_lock:
        current = dict(_calls.get(extension, {}))
    if current.get("busy") and not audio.player.is_playing(extension):
        # Занят чем-то, что не идёт из капсюля: панель поставила вызов, и шлюз
        # звонит. Отказ не даёт диску перебить его.
        _refuse(extension, detail, f"на {extension} уже идёт вызов", 409)

    with _calls_lock:
        _calls[extension] = {"busy": True, "sound": sound.name,
                             "started": time.time(), "detail": "идут гудки"}

    _fan_out(monitor.Event("info", extension,
                           f"набран {detail}: гудки, затем {sound.name}",
                           direction="inbound"))

    def answered(ext: str, name: str) -> None:
        """КПВ сменился звуком."""
        with _calls_lock:
            _calls.setdefault(ext, {}).update(detail=f"играет {name}")
        _fan_out(monitor.Event("info", ext, f"соединено — играет {name}",
                               direction="inbound"))

    try:
        audio.player.start_sequence(extension, tones.ringback(), RINGBACK_SECONDS,
                                    sound.name, sound.source, loop=False,
                                    on_answer=answered)
    except (audio.AudioError, OSError) as exc:
        with _calls_lock:
            _calls.setdefault(extension, {}).update(
                busy=False, ok=False, detail=str(exc), finished=time.time())
        # Сигнал занято и здесь: звонящий набрал номер, который запрограммирован
        # и верен, а эта сторона не смогла его проиграть. Исправить это не в его
        # силах, но слышимый отказ лучше молчащей трубки.
        _refuse(extension, detail, f"звук не пошёл: {exc}", 500)

    return {"ok": True, "extension": extension, "number": detail,
            "sound": sound.name, "ringback": RINGBACK_SECONDS}


# ── программируемые номера ──────────────────────────────────────────────
#
# Номера, которые можно набрать с трубки, каждый играет назначенный ему звук.
# Хранятся в базе Asterisk, а не в extensions.conf: план набора покрывает весь
# диапазон одним шаблоном и читает DB(playslot/510) в момент вызова, поэтому и
# какие номера существуют, и что каждый играет, решается здесь и действует со
# следующего вызова — без перезагрузки и без правки конфигурации веб-сервером.
#
# Это то направление, которое работает на линии, чей аппарат не может принимать
# входящие: человек поднимает трубку и набирает, а для этого от шлюза не нужно
# ничего, в чём замкнутый шлейф мог бы отказать.

# Что покрывает шаблон плана набора. Номер вне его был бы принят здесь и никуда
# не привёл при наборе, поэтому отвергается.
#
# 500–509 исключены намеренно: 500, 501 и 502 — постоянные проверочные номера
# в extensions.conf, и шаблон там начинается с 510, чтобы их не трогать.
SLOT_RANGE = range(510, 530)
SLOT_FIRST = str(SLOT_RANGE.start)
SLOT_LAST = str(SLOT_RANGE.stop - 1)


def slot_allowed(number: str) -> bool:
    return number.isdigit() and int(number) in SLOT_RANGE


def slots_assigned() -> dict[str, str]:
    """Все запрограммированные номера и звук каждого, из АТС.

    Один вызов CLI на всё семейство, а не по одному на номер: это выполняется
    при каждой загрузке панели, а двадцать обращений к консоли Asterisk длятся
    достаточно, чтобы это чувствовалось.
    """
    listing = health._cli("database show playslot") or ""
    found = {}
    for line in listing.splitlines():
        # "/playslot/510                    : zoopark"
        match = re.match(r"\s*/playslot/(\d+)\s*:\s*(\S+)", line)
        if match:
            found[match.group(1)] = match.group(2)
    return found


def slots_state() -> dict:
    """Какие номера запрограммированы и что каждый играет."""
    assigned = slots_assigned()

    try:
        library = [{"name": s.name, "seconds": round(s.seconds, 1)}
                   for s in sounds.library().values()]
    except sounds.SoundError:
        library = []

    slots = [{"number": n, "sound": assigned[n]} for n in sorted(assigned)]
    # Какие номера ещё свободны, чтобы интерфейс мог их предложить, а не
    # заставлять оператора угадывать и получать отказ.
    free = [str(n) for n in SLOT_RANGE if str(n) not in assigned]

    return {"slots": slots, "sounds": library, "free": free,
            "range": {"first": SLOT_FIRST, "last": SLOT_LAST}}


def slots_set(number: str, name: str) -> dict:
    """Добавить номер, сменить его звук или убрать его.

    Пустой звук убирает номер: без записи в базе шаблон плана набора всё ещё
    совпадает, но вызов попадает в ветку, которая говорит, что номер не
    назначен, вместо того чтобы что-то играть.
    """
    if not slot_allowed(number):
        raise VoipError(f"номер {number} не программируется "
                        f"(доступны {SLOT_FIRST}–{SLOT_LAST})", 400)

    if not name:
        health._cli(f"database del playslot {number}")
        _fan_out(monitor.Event("info", "-", f"номер {number} удалён"))
        return {"ok": True, "number": number, "sound": ""}

    # Проверяется по библиотеке, а не принимается на веру: значение уходит в
    # аргумент Playback(), и от незнакомого имени звонящий услышал бы, как
    # линия умирает без объяснений.
    try:
        sound = sounds.resolve(name)
    except sounds.SoundError as exc:
        raise VoipError(str(exc), 400) from exc

    result = health._cli(f"database put playslot {number} {sound.name}")
    if result is None or "Updated" not in result:
        raise VoipError("АТС не приняла настройку", 502)

    _fan_out(monitor.Event("info", "-",
                           f"номер {number} → {sound.name} ({sound.seconds:.0f} с)"))
    return {"ok": True, "number": number, "sound": sound.name}


# ── автообесточивание и сторож ──────────────────────────────────────────

def auto_power_state() -> dict:
    with _auto_power_lock:
        return {"extensions": sorted(_auto_power), "seconds": AUTO_POWER_SECONDS}


def auto_power_set(extension: str, enabled: bool) -> dict:
    """Включить или выключить автообесточивание для одной трубки."""
    check_extension(extension)
    with _auto_power_lock:
        if enabled:
            _auto_power.add(extension)
        else:
            _auto_power.discard(extension)
        current = sorted(_auto_power)

    _fan_out(monitor.Event(
        "info", extension,
        f"автообесточивание после отбоя {'включено' if enabled else 'выключено'}"))
    return {"ok": True, "extensions": current}


def watchdog_state() -> dict:
    return {
        "enabled": dog.fix,
        "ports": list(dog.ports),
        "grace": dog.grace,
        "interval": dog.interval,
        "watch": dog.status(),
    }


def watchdog_set(ports: Optional[list] = None, grace=None, enabled=None) -> dict:
    """Включить или выключить автосброс и выбрать, какие порты он покрывает."""
    if ports is not None:
        wanted = [str(p) for p in ports]
        for port in wanted:
            if port not in gateway.PORTS:
                raise VoipError(f"неизвестный порт: {port}", 400)
        dog.ports = tuple(wanted) if wanted else tuple(gateway.PORTS)
        # Учёт по портам ведётся по ключу-порту, поэтому при смене набора его
        # надо перестроить, иначе снятый порт сохранит устаревший таймер.
        dog.__post_init__()
    if grace is not None:
        try:
            dog.grace = max(5.0, float(grace))
        except (TypeError, ValueError) as exc:
            raise VoipError("неверный порог", 400) from exc
    if enabled is not None:
        dog.fix = bool(enabled)

    _fan_out(monitor.Event(
        "info", "-",
        f"автосброс {'включён' if dog.fix else 'выключен'}"
        + (f" для {', '.join(dog.ports)}" if dog.fix else "")))
    return {"ok": True, "enabled": dog.fix, "ports": list(dog.ports)}


# ── администрирование шлюза ─────────────────────────────────────────────
#
# Каждая из этих операций открывает telnet-сессию, поэтому все берут
# _gateway_lock и все отказывают, пока шлюз перезагружается.

def _guard() -> None:
    """Проверка, с которой начинается каждая административная операция."""
    if _maintenance["busy"]:
        raise VoipError("шлюз занят обслуживанием", 409)


def admin_ports() -> dict:
    """Каждый порт с его текущим состоянием и ключевыми настройками."""
    _guard()
    try:
        with _gateway_lock:
            with gateway.Gateway() as gw:
                ports = admin.all_ports(gw)
                config = gw.send("show running-config")
                peers = [p for p in admin.dial_peers(config) if p["kind"] == "pots"]
    except (gateway.GatewayError, admin.AdminError) as exc:
        raise VoipError(str(exc), 502) from exc
    return {"ports": ports, "peers": peers}


def admin_port(port: str) -> dict:
    """Полные настройки одного порта, как их сообщает шлюз."""
    _guard()
    try:
        with _gateway_lock:
            with gateway.Gateway() as gw:
                detail = admin.port_detail(gw, port)
    except admin.AdminError as exc:
        raise VoipError(str(exc), 400) from exc
    except gateway.GatewayError as exc:
        raise VoipError(str(exc), 502) from exc
    return {
        "detail": detail,
        "parameters": [
            {
                "key": p.key, "label": p.label, "kind": p.kind,
                "min": p.minimum, "max": p.maximum,
                "choices": list(p.choices), "unit": p.unit, "help": p.help,
            }
            for p in admin.PARAMETERS.values()
        ],
    }


def admin_set_port(port: str, key: str, value) -> dict:
    """Изменить один параметр из белого списка на одном порту."""
    _guard()
    try:
        with _gateway_lock:
            with gateway.Gateway() as gw:
                command = admin.set_parameter(gw, port, key, value)
    except admin.AdminError as exc:
        raise VoipError(str(exc), 400) from exc
    except gateway.GatewayError as exc:
        raise VoipError(str(exc), 502) from exc
    invalidate_ports()
    _fan_out(monitor.Event("info", str(gateway.extension_for(port)),
                           f"порт {port}: {command}"))
    return {"ok": True, "command": command}


def admin_port_state(port: str, up: bool) -> dict:
    """Ввести порт в работу или вывести из неё."""
    _guard()
    try:
        with _gateway_lock:
            with gateway.Gateway() as gw:
                admin.set_admin_state(gw, port, up)
    except admin.AdminError as exc:
        raise VoipError(str(exc), 400) from exc
    except gateway.GatewayError as exc:
        raise VoipError(str(exc), 502) from exc
    invalidate_ports()
    _fan_out(monitor.Event("info", str(gateway.extension_for(port)),
                           f"порт {port}: {'включён' if up else 'выключен (shutdown)'}"))
    return {"ok": True}


def admin_probe(port: str) -> dict:
    """Может ли этот порт принять вызов прямо сейчас, и если нет, то почему."""
    _guard()
    try:
        with _gateway_lock:
            with gateway.Gateway() as gw:
                return admin.probe(gw, port)
    except admin.AdminError as exc:
        raise VoipError(str(exc), 400) from exc
    except gateway.GatewayError as exc:
        raise VoipError(str(exc), 502) from exc


def admin_dial_peer(tag, port: str) -> dict:
    """Направить внутренний номер на другой порт FXS."""
    _guard()
    try:
        tag = int(tag)
    except (TypeError, ValueError) as exc:
        raise VoipError("неверный номер dial-peer", 400) from exc
    try:
        with _gateway_lock:
            with gateway.Gateway() as gw:
                admin.set_dial_peer_port(gw, tag, port)
    except admin.AdminError as exc:
        raise VoipError(str(exc), 400) from exc
    except gateway.GatewayError as exc:
        raise VoipError(str(exc), 502) from exc
    invalidate_ports()
    _fan_out(monitor.Event("info", "-", f"dial-peer {tag} переведён на порт {port}"))
    return {"ok": True}


def admin_diagnostics_list() -> dict:
    return {"available": [{"key": k, "label": v[0], "command": v[1]}
                          for k, v in admin.DIAGNOSTICS.items()]}


def admin_diagnostic(name: str) -> dict:
    """Выполнить одну команду только на чтение из фиксированного списка."""
    _guard()
    try:
        with _gateway_lock:
            with gateway.Gateway() as gw:
                text = admin.diagnostic(gw, name)
    except admin.AdminError as exc:
        raise VoipError(str(exc), 400) from exc
    except gateway.GatewayError as exc:
        raise VoipError(str(exc), 502) from exc
    return {"name": name, "text": text}


def admin_save() -> dict:
    """Записать текущую конфигурацию во flash.

    Подтверждается на панели. Пока это не выполнено, каждое сделанное здесь
    изменение откатывается выключением питания шлюза — единственный откат,
    который у устройства есть.
    """
    _guard()
    try:
        with _gateway_lock:
            with gateway.Gateway() as gw:
                text = admin.save_to_flash(gw)
    except gateway.GatewayError as exc:
        raise VoipError(str(exc), 502) from exc
    _fan_out(monitor.Event("info", "-", "конфигурация сохранена во flash"))
    return {"ok": True, "text": text}


def panel() -> dict:
    """Панель индикации: светодиоды, порты FXS и как достучаться до шлюза."""
    _guard()
    try:
        with _gateway_lock:
            with gateway.Gateway() as gw:
                return admin.panel(gw)
    except (gateway.GatewayError, admin.AdminError) as exc:
        raise VoipError(str(exc), 502) from exc


def pbx_state() -> dict:
    """Чем занят Asterisk: каналы, точка регистрации и что можно набрать.

    План набора читается из файла, а не из CLI: оператору нужно знать, какие
    номера что-то делают, а собственный вывод CLI куда длиннее, чем панель
    может показать.
    """
    channels = health._cli("core show channels concise") or ""
    active = [c for c in channels.splitlines() if c.strip()]

    contacts = health._cli("pjsip show contacts") or ""
    trunk = "неизвестно"
    for line in contacts.splitlines():
        if "addpac" in line:
            # «NonQual» здесь и есть нужное состояние: контакт пригоден и
            # намеренно не опрашивается, потому что этот шлюз игнорирует
            # OPTIONS.
            trunk = "доступен" if "NonQual" in line or "Avail" in line else line.strip()
            break

    # Что делает каждый шаблон, словами. Идеально было бы взять из комментариев
    # самого плана набора, но они стоят над строкой, а не в ней, а текст NoOp()
    # полон нераскрытых переменных — поэтому известные названы здесь, а всё
    # новое откатывается к своему шаблону.
    KNOWN = {
        "_10[1-8]": "Вызов на другой аппарат (101–108)",
        "500": "Проверка звука — АТС проигрывает файл в трубку",
        "501": "Эхо-тест — проверка звука в обе стороны",
        "_X.": "Любой другой номер — сообщение «неверный номер»",
    }

    extensions = []
    dialplan = VOIP_ROOT / "etc" / "extensions.conf"
    if dialplan.is_file():
        context = ""
        for line in dialplan.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped.startswith("[") and stripped.endswith("]"):
                context = stripped[1:-1]
            match = re.match(r"exten\s*=>\s*([^,]+),1,", stripped)
            if match and context == "from-gateway":
                pattern = match.group(1).strip()
                extensions.append({"pattern": pattern,
                                   "note": KNOWN.get(pattern, "—")})

    return {
        "channels": active,
        "channel_count": len(active),
        "trunk": trunk,
        "dialplan": extensions,
    }


def pbx_reload() -> dict:
    """Перечитать план набора, не роняя вызовы.

    Именно reload, не restart: перезапуск Asterisk снёс бы каждый канал, а
    ничему здесь это не нужно, чтобы подхватить отредактированный
    voip/etc/extensions.conf.
    """
    output = health._cli("dialplan reload")
    if output is None:
        raise VoipError("АТС не отвечает", 502)
    _fan_out(monitor.Event("info", "-", "план набора перезагружен"))
    return {"ok": True, "text": output.strip()}


# ── обслуживание: сброс и перезагрузка шлюза ────────────────────────────

def _begin_maintenance(what: str, detail: str) -> bool:
    """Занять шлюз. False, если его уже занял кто-то другой."""
    with _calls_lock:
        if _maintenance["busy"]:
            return False
        _maintenance.update(busy=True, what=what, started=time.time(),
                            detail=detail)
    return True


def _end_maintenance(detail: str) -> None:
    with _calls_lock:
        _maintenance.update(busy=False, what="", detail=detail)
    invalidate_ports()


def _run_reset() -> None:
    """Прогнать циклом каждый порт FXS. Мягкий ремонт."""
    _progress_start("reset", "Сброс портов FXS", [
        "Подключение к шлюзу",
        "Цикл shutdown/no shutdown по 8 портам",
        "Ожидание инициализации",
        "Проверка результата",
    ])
    try:
        _progress_step(0, "running")
        with _gateway_lock:
            _progress_step(0, "ok")
            _progress_step(1, "running", "около 10 секунд")
            cycled, stuck = gateway.cycle_everything()
            _progress_step(1, "ok")
            _progress_step(2, "ok")
            _progress_step(3, "ok" if not stuck else "fail")
        if stuck:
            # Почти всегда это оставленная снятой трубка, чего с этой стороны
            # не исправить ничем, — поэтому она названа, а не закопана.
            names = ", ".join(f"{gateway.extension_for(p)}" for p in stuck)
            _fan_out(monitor.Event(
                "error", "-",
                f"порты сброшены ({len(cycled)}), но не освободились: {names}. "
                "Проверьте, не снята ли трубка."))
            _progress_finish(False, f"не освободились: {names}")
            _end_maintenance(f"не освободились: {names}")
        else:
            _fan_out(monitor.Event("info", "-",
                                   f"сброшены все порты FXS ({len(cycled)})"))
            _progress_finish(True, "все порты свободны")
            _end_maintenance("порты сброшены")
    except Exception as exc:  # noqa: BLE001
        _fan_out(monitor.Event("error", "-", f"сброс портов не удался: {exc}"))
        _progress_finish(False, str(exc))
        _end_maintenance(str(exc))


def _run_reboot() -> None:
    """Перезагрузить шлюз и дождаться, пока он снова ответит."""
    _progress_start("reboot", "Перезагрузка шлюза", [
        "Отправка команды reboot",
        "Шлюз выключается",
        "Ожидание загрузки (до 3 минут)",
        "Проверка связи",
    ])
    try:
        _progress_step(0, "running")
        with _gateway_lock:
            gateway.reboot_gateway()
        _progress_step(0, "ok")
        _progress_step(1, "ok")
        _progress_step(2, "running", "несохранённые настройки откатятся")
        _fan_out(monitor.Event("info", "-",
                               "шлюз перезагружается, это займёт около минуты"))
        # Не внутри блокировки: ожидание держало бы её всю минуту, а само оно и
        # так открывает собственные короткие сессии.
        seconds = gateway.wait_until_alive(timeout=180)
        _progress_step(2, "ok", f"{seconds:.0f} с")
        _progress_step(3, "ok")
        _progress_finish(True, f"шлюз вернулся за {seconds:.0f} с")
        _fan_out(monitor.Event("info", "-",
                               f"шлюз снова на связи, {seconds:.0f} с"))
        _end_maintenance(f"перезагружен за {seconds:.0f} с")
    except Exception as exc:  # noqa: BLE001
        _progress_finish(False, str(exc))
        _fan_out(monitor.Event("error", "-", f"перезагрузка не удалась: {exc}"))
        _end_maintenance(str(exc))


def reset_ports() -> dict:
    """Прогнать циклом каждый порт FXS, не трогая питание шлюза."""
    if not _begin_maintenance("reset", "сброс портов"):
        raise VoipError("шлюз уже занят обслуживанием", 409)
    invalidate_ports()
    _fan_out(monitor.Event("info", "-", "сброс всех портов FXS"))
    threading.Thread(target=_run_reset, name="reset-ports", daemon=True).start()
    return {"ok": True}


def reboot_gateway() -> dict:
    """Перезагрузить шлюз.

    Подтверждается на панели до того, как дойдёт сюда: это роняет каждый
    идущий вызов и уводит шлюз из сети примерно на минуту.
    """
    if not _begin_maintenance("reboot", "перезагрузка шлюза"):
        raise VoipError("шлюз уже занят обслуживанием", 409)
    invalidate_ports()
    _fan_out(monitor.Event("info", "-", "перезагрузка шлюза"))
    threading.Thread(target=_run_reboot, name="reboot", daemon=True).start()
    return {"ok": True}


def on_hook(extension: str, seconds: float = 6.0) -> dict:
    """«Я положил трубку» — обесточить линию настолько, чтобы она отпустила.

    Для аппарата, чьи ключи линии держат шлейф замкнутым после того, как трубку
    положили. hangup_port() циклирует порт около секунды, чего хватает, чтобы
    снять сессию, которую держит прошивка; это держит линию мёртвой шесть, что
    вдобавок даёт замкнутому шлейфу успокоиться. Замерено на TX-220 на 106:
    после односекундного цикла порт снова занимался в пределах 30 секунд, после
    шестисекундного оставался свободным целую минуту.
    """
    check_extension(extension)
    try:
        seconds = float(seconds)
    except (TypeError, ValueError):
        seconds = 6.0
    # С ограничением: пока линия обесточена, она непригодна, и долгое удержание
    # выглядело бы как мёртвый номер, а не как ремонт.
    seconds = min(max(seconds, 1.0), 20.0)

    if _maintenance["busy"]:
        raise VoipError("шлюз занят обслуживанием", 409)

    _progress_start(f"onhook-{extension}", f"Обесточивание линии {extension}", [
        "Снятие питания с линии",
        f"Ожидание {seconds:.0f} с",
        "Подача питания",
        "Проверка состояния",
    ])
    try:
        _progress_step(0, "running")
        with _gateway_lock:
            _progress_step(0, "ok")
            _progress_step(1, "running", f"{seconds:.0f} с без питания")
            status = gateway.power_cycle_extension(extension, down_seconds=seconds)
        _progress_step(1, "ok")
        _progress_step(2, "ok")
        free = status == "Idle"
        _progress_step(3, "ok" if free else "fail", status)
        _progress_finish(free, "линия свободна" if free
                         else f"порт остался {status}")
        # Держит сторожа в стороне от этого порта, пока цикл устаканивается: два
        # цикла, попавшие вместе, оставляют его в Disconnecting.
        dog.touched(gateway.port_for(extension))
    except gateway.GatewayError as exc:
        _progress_finish(False, str(exc))
        raise VoipError(str(exc), 502) from exc

    invalidate_ports()
    _fan_out(monitor.Event(
        "info", extension,
        f"линия обесточена на {seconds:.0f} с, порт {status}"))
    return {"ok": free, "status": status}


def hangup_port(extension: str) -> dict:
    """Завершить вызов трубки с этой стороны.

    Использует цикл порта на шлюзе, а не AMI Hangup: это и завершает вызов, и
    оставляет порт свободным за один приём, тогда как одно только снятие канала
    может оставить порт в том застрявшем состоянии, которое блокирует следующий
    вызов.
    """
    check_extension(extension)
    if _maintenance["busy"]:
        raise VoipError("шлюз занят обслуживанием", 409)
    try:
        with _gateway_lock:
            gateway.clear_extension(extension)
    except gateway.GatewayError as exc:
        raise VoipError(str(exc), 502) from exc
    invalidate_ports()
    dog.touched(gateway.port_for(extension))
    _fan_out(monitor.Event("info", extension, "порт освобождён из интерфейса"))
    return {"ok": True}


def hangup_call(extension: str) -> dict:
    """Сбросить живой вызов на одной трубке, через Asterisk.

    Отличается от hangup_port(), который циклирует порт FXS: тот завершает
    вызов, выдёргивая из-под него линию, и является ремонтом для порта, который
    не отпускает. Это же — обычный способ завершить идущий разговор.
    """
    check_extension(extension)

    # Какой канал принадлежит этой трубке — знание, которого у менеджера нет:
    # исходящий вызов идёт по каналу магистральной точки, у которого exten равен
    # «s», а connectedlinenum — «<unknown>». Монитор ведёт эту связь по мере
    # постановки вызовов, поэтому спрашивают его.
    targets = board.channels_of(extension)
    if not targets:
        raise VoipError(f"на {extension} нет активного вызова", 404)

    try:
        with call.Manager() as ami:
            live = {c.get("channel", "") for c in ami.channels()}
            targets = [t for t in targets if t in live]
            if not targets:
                raise VoipError(f"на {extension} нет активного вызова", 404)
            for channel in targets:
                ami.hangup(channel)
    except call.CallError as exc:
        raise VoipError(str(exc), 502) from exc

    _fan_out(monitor.Event("info", extension, "вызов завершён из интерфейса"))
    return {"ok": True, "channels": targets}
