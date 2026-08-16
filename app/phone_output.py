"""
Отправить звук трубки на выбранное устройство, а не на системное по умолчанию.

Трубка подключена к ноутбуку своим кабелем и почти никогда не является тем
устройством, куда система играет по умолчанию: по умолчанию играют колонки, за
которыми сидит стол. Поэтому у капсюля должен быть собственный выход, и
выбирать его должен оператор, как он уже выбирает выходы для игры и видео.

Мешает этому afplay, которым voip/scripts/audio.py играет всё: устройство он
выбирать не умеет вовсе — только системное по умолчанию. Флага для этого у него
нет, так что обойти это, оставаясь на afplay, невозможно.

Здесь его подменяет PortAudio, который уже стоит в проекте ради серверного
звука (app/audio_engine.py) и устройства по имени открывать умеет. Подмена
делается на объекте плеера, а не в voip/: ни одна строка в voip/ этим кодом не
правится, и подсистема телефонии, поднятая без игры, продолжает играть через
afplay, как и раньше.

Форма подмены — процессоподобный объект. voip/scripts/audio.py запускает
afplay через Popen и потом только ждёт его (`wait()`), завершает
(`terminate()`, `kill()`) и смотрит на этом же объекте. Поэтому здесь
реализовано ровно то, что он вызывает: воспроизведение, которое выглядит как
процесс. Так работает весь его учёт — поток-наблюдатель, повтор зацикленного
файла, событие о конце звука — не зная, что играет уже не afplay.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Optional

from app import audio_engine

# Канал, на котором живёт капсюль. Тот же ключ, что в sound_config и в панели
# дилера — выбор устройства и воспроизведение должны говорить об одном канале.
CHANNEL = "phone"

# Ключ голоса внутри канала. Один: в трубке один капсюль, и второй звук в ней
# не смешивается с первым, а заменяет его — так же, как в voip/scripts/audio.py.
VOICE_KEY = "__phone__"


class _Playback:
    """Воспроизведение, которое ведёт себя как процесс.

    voip/scripts/audio.py обращается к результату Popen ровно четырьмя
    способами: wait(), terminate(), kill() и хранение ссылки. Здесь они и
    реализованы — остальное ему не нужно и никогда не вызывается.
    """

    def __init__(self, path: Path, seconds: float) -> None:
        self.path = path
        self._done = threading.Event()
        self._stopped = False
        # Конец звука определяется по его длительности: PortAudio не сообщает,
        # что голос доиграл, а канал микширует их без обратной связи. Разница с
        # процессом здесь только в том, что таймер приходится завести самому.
        self._timer = threading.Timer(seconds, self._finish)
        self._timer.daemon = True
        self._timer.start()

    def _finish(self) -> None:
        self._done.set()

    # ── то, что вызывает voip/scripts/audio.py ──────────────────────────

    def wait(self, timeout: Optional[float] = None) -> int:
        """Дождаться конца. Возвращает код возврата, как процесс.

        Исключение при истёкшем сроке — то же самое, которое кидает
        Popen.wait(), а не встроенное TimeoutError: плеер ловит именно его,
        когда после terminate() решает, добивать ли процесс. Другое пролетело
        бы мимо except и уронило поток, который останавливает звук.
        """
        import subprocess

        if not self._done.wait(timeout):
            raise subprocess.TimeoutExpired(str(self.path), timeout or 0)
        return 0

    def terminate(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        self._timer.cancel()
        audio_engine.stop_key(CHANNEL, VOICE_KEY)
        self._done.set()

    # Убить нечего — процесса нет. Метод есть, потому что его вызывают, когда
    # terminate() не уложился в срок.
    kill = terminate


def _seconds(path: Path) -> float:
    """Длительность файла. Ноль, если прочитать не удалось."""
    try:
        import soundfile as sf

        info = sf.info(str(path))
        return float(info.frames) / float(info.samplerate or 1)
    except Exception:                                           # noqa: BLE001
        # Не фатально: без длительности звук всё равно играет, просто конец
        # его заметят по on-hook или по следующему звуку, а не по таймеру.
        return 0.0


def _spawn(path: Path) -> Optional[_Playback]:
    """Начать воспроизведение файла в канал трубки.

    None означает «не смог» — вызывающий тогда возвращается к afplay, а не
    остаётся с молчащей трубкой.
    """
    if not audio_engine.available():
        return None
    status = audio_engine.status().get("channels", {}).get(CHANNEL, {})
    if not status.get("open"):
        return None
    if not audio_engine.play(path, CHANNEL, 1.0, VOICE_KEY):
        return None
    seconds = _seconds(path)
    # Файл без известной длительности не должен обрывать сам себя таймером на
    # нуле секунд: пусть играет, а закончит его on-hook или следующий звук.
    return _Playback(path, seconds if seconds > 0 else 3600.0)


def install(audio_module) -> None:
    """Заставить плеер трубки играть в выбранное оператором устройство.

    Принимает модуль voip/scripts/audio.py. Подменяется в нём одно имя —
    subprocess.Popen, — потому что именно им плеер запускает afplay, и это
    единственная точка, где решается, куда пойдёт звук. Всё остальное
    (поток-наблюдатель, повтор зацикленного файла, событие о конце звука)
    продолжает работать как есть: ему возвращают то, что ведёт себя как
    процесс.

    Идемпотентно: повторный вызов ничего не делает, поэтому подъём телефонии
    можно вызывать сколько угодно раз.

    К afplay возвращаемся, когда канал трубки закрыт или PortAudio нет — то
    есть ровно там, где своего устройства у трубки всё равно не выбрано.
    """
    if getattr(audio_module, "_phone_output_installed", False):
        return

    import subprocess

    real_popen = subprocess.Popen

    def popen(args, **kwargs):
        # Плеер запускает ровно одно: [PLAYER, путь]. Всё, что на это не
        # похоже, уходит настоящему Popen нетронутым.
        path = Path(args[1]) if len(args) == 2 else None
        if path is not None and path.is_file():
            playback = _spawn(path)
            if playback is not None:
                return playback
        return real_popen(args, **kwargs)

    # Подменяется имя subprocess в модуле плеера, а не сам модуль subprocess:
    # правка настоящего модуля задела бы каждый Popen в процессе — а их здесь
    # много, от ffmpeg до telnet до шлюза.
    audio_module.subprocess = _Subprocess(popen)
    audio_module._phone_output_installed = True


class _Subprocess:
    """То, что модуль плеера видит вместо subprocess: подменён только Popen."""

    def __init__(self, popen) -> None:
        import subprocess as real

        self._real = real
        self.Popen = popen

    def __getattr__(self, name):
        # DEVNULL, TimeoutExpired и всё прочее — настоящие.
        return getattr(self._real, name)


def status() -> dict:
    """Куда сейчас идёт звук трубки — для панели дилера."""
    channels = audio_engine.status().get("channels", {})
    channel = channels.get(CHANNEL, {})
    return {
        "available": audio_engine.available(),
        "open": bool(channel.get("open")),
        "device": channel.get("device"),
        "error": channel.get("error"),
    }
