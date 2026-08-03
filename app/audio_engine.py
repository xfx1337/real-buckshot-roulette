"""
Серверный аудиодвижок — Buckshot Roulette IRL.

Играет озвучку прямо из Python на выбранное устройство вывода, минуя браузер.

Зачем не браузер: `HTMLMediaElement.setSinkId` живёт только в secure context,
требует разрешения на медиа ради названий устройств, блокирует автоплей до
жеста пользователя и стартует асинхронно (первые звуки успевают уйти на
устройство по умолчанию). Ни одно из этих ограничений к игре отношения не
имеет. PortAudio через `sounddevice` открывает конкретное устройство по имени
и играет сразу.

Зачем не pydub: он умеет резать и склеивать аудио, но проигрывает через
`ffplay`/simpleaudio на СИСТЕМНОЕ устройство по умолчанию — выбрать выход
нельзя, а ровно это здесь и нужно. Декодирование берёт на себя `soundfile`
(ogg/wav/flac напрямую), а редкие форматы (mp3/m4a) конвертирует ffmpeg.

Модель:
  * канал ('game' / 'video') = один открытый OutputStream на своё устройство;
  * внутри канала — микшер: несколько одновременных голосов складываются;
  * колбэк PortAudio крутится в его собственном потоке, поэтому весь доступ к
    списку голосов защищён локом, а сам колбэк не делает ни ввода-вывода, ни
    аллокаций сверх необходимого — иначе поток захлебнётся и звук затрещит.

Декодированные файлы кэшируются в памяти: звуки короткие, а читать ogg с диска
в момент выстрела — лишняя задержка.

Движок необязателен. Если `sounddevice` не установлен или устройство не
открылось, `available()` вернёт False, и сервер останется на браузерном звуке.
"""

from __future__ import annotations

import subprocess
import threading
from pathlib import Path

try:
    import numpy as np
    import sounddevice as sd
    import soundfile as sf
    _IMPORT_ERROR = None
except Exception as e:  # pragma: no cover - зависит от окружения
    np = None
    sd = None
    sf = None
    _IMPORT_ERROR = e

# Внутренний формат микшера. Всё приводится к нему при загрузке, поэтому в
# колбэке не остаётся ни ресемплинга, ни конверсии каналов.
SAMPLE_RATE = 44100
CHANNELS = 2
# Размер блока PortAudio. 512 кадров ≈ 11.6 мс — компромисс между задержкой
# (выстрел должен звучать сразу) и риском underrun на загруженной машине.
BLOCK_SIZE = 512

CHANNEL_NAMES = ("game", "video")


class _Voice:
    """Один играющий звук внутри канала."""

    __slots__ = ("data", "pos", "gain", "loop", "key", "stopping")

    def __init__(self, data, gain: float, loop: bool, key: str):
        self.data = data          # np.ndarray (frames, CHANNELS) float32
        self.pos = 0
        self.gain = gain
        self.loop = loop
        self.key = key
        self.stopping = False


class _Channel:
    """Устройство вывода + микшер голосов на нём."""

    def __init__(self, name: str):
        self.name = name
        self.device = None        # индекс/имя устройства PortAudio, None = дефолт
        self.stream = None
        self.voices: list[_Voice] = []
        self.lock = threading.Lock()
        self.error: str | None = None

    # ── Поток PortAudio ────────────────────────────────────────────────────
    def _callback(self, outdata, frames, time_info, status):
        # status сообщает об underrun; чинить его отсюда нечем, а печатать в
        # аудиопотоке нельзя — просто отдаём тишину поверх и идём дальше.
        outdata.fill(0.0)
        with self.lock:
            if not self.voices:
                return
            done = []
            for v in self.voices:
                if v.stopping:
                    done.append(v)
                    continue
                need = frames
                out_pos = 0
                while need > 0:
                    chunk = v.data[v.pos:v.pos + need]
                    n = len(chunk)
                    if n:
                        outdata[out_pos:out_pos + n] += chunk * v.gain
                        v.pos += n
                        out_pos += n
                        need -= n
                    if v.pos >= len(v.data):
                        if v.loop:
                            v.pos = 0
                            # Пустой файл в режиме loop крутил бы этот цикл
                            # вечно и подвесил аудиопоток — снимаем голос.
                            if len(v.data) == 0:
                                done.append(v)
                                break
                        else:
                            done.append(v)
                            break
            for v in done:
                if v in self.voices:
                    self.voices.remove(v)
        # Клиппинг: сумма голосов и усиление >1.0 легко выходят за [-1, 1],
        # а PortAudio за пределами диапазона даёт треск.
        np.clip(outdata, -1.0, 1.0, out=outdata)

    def open(self, device) -> bool:
        """Открыть (или переоткрыть) поток на устройстве. True если получилось."""
        self.close()
        self.device = device
        try:
            self.stream = sd.OutputStream(
                samplerate=SAMPLE_RATE,
                blocksize=BLOCK_SIZE,
                device=device,
                channels=CHANNELS,
                dtype="float32",
                callback=self._callback,
            )
            self.stream.start()
            self.error = None
            return True
        except Exception as e:
            self.stream = None
            self.error = f"{type(e).__name__}: {e}"
            return False

    def close(self):
        if self.stream is not None:
            try:
                self.stream.stop()
                self.stream.close()
            except Exception:
                pass
            self.stream = None
        with self.lock:
            self.voices.clear()

    # ── Голоса ─────────────────────────────────────────────────────────────
    def add(self, data, gain: float, loop: bool, key: str):
        if self.stream is None:
            return
        with self.lock:
            self.voices.append(_Voice(data, gain, loop, key))

    def stop_key(self, key: str):
        with self.lock:
            for v in self.voices:
                if v.key == key:
                    v.stopping = True

    def stop_all(self):
        with self.lock:
            for v in self.voices:
                v.stopping = True

    def set_gain(self, key: str, gain: float):
        with self.lock:
            for v in self.voices:
                if v.key == key:
                    v.gain = gain

    def has(self, key: str) -> bool:
        with self.lock:
            return any(v.key == key and not v.stopping for v in self.voices)


# ── Загрузка и декодирование ───────────────────────────────────────────────

_cache: dict[tuple[str, float], object] = {}
_cache_lock = threading.Lock()


def _to_engine_format(data, sr: int):
    """Привести к стерео float32 SAMPLE_RATE."""
    if data.ndim == 1:
        data = data.reshape(-1, 1)
    if data.shape[1] == 1:
        data = np.repeat(data, CHANNELS, axis=1)
    elif data.shape[1] > CHANNELS:
        data = data[:, :CHANNELS]

    if sr != SAMPLE_RATE:
        # Линейная интерполяция. Для эффектов и фоновой музыки этого хватает;
        # тащить scipy ради ресемплинга редких файлов не стоит — почти все
        # звуки проекта и так 44100.
        duration = data.shape[0] / float(sr)
        n_out = int(round(duration * SAMPLE_RATE))
        if n_out <= 0:
            return np.zeros((0, CHANNELS), dtype="float32")
        src_idx = np.linspace(0, data.shape[0] - 1, n_out)
        out = np.empty((n_out, CHANNELS), dtype="float32")
        for ch in range(CHANNELS):
            out[:, ch] = np.interp(src_idx, np.arange(data.shape[0]), data[:, ch])
        data = out

    return np.ascontiguousarray(data, dtype="float32")


def _decode_via_ffmpeg(path: Path):
    """Фолбэк для форматов, которые не читает libsndfile (mp3, m4a)."""
    proc = subprocess.run(
        [
            "ffmpeg", "-v", "quiet", "-i", str(path),
            "-f", "f32le", "-acodec", "pcm_f32le",
            "-ac", str(CHANNELS), "-ar", str(SAMPLE_RATE), "-",
        ],
        capture_output=True,
    )
    if proc.returncode != 0 or not proc.stdout:
        raise RuntimeError(f"ffmpeg не смог декодировать {path.name}")
    return np.frombuffer(proc.stdout, dtype="float32").reshape(-1, CHANNELS).copy()


def load(path: Path):
    """Декодировать файл в формат микшера. Результат кэшируется по пути+mtime."""
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None
    ck = (str(path), mtime)
    with _cache_lock:
        hit = _cache.get(ck)
    if hit is not None:
        return hit

    try:
        data, sr = sf.read(str(path), dtype="float32", always_2d=True)
        data = _to_engine_format(data, sr)
    except Exception:
        try:
            data = _decode_via_ffmpeg(path)
        except Exception:
            return None

    with _cache_lock:
        # Кэш живёт всю сессию: звуков десятки, счёт идёт на мегабайты.
        # Записи для устаревших mtime вычищаем, чтобы правка файла не копилась.
        for k in [k for k in _cache if k[0] == str(path)]:
            del _cache[k]
        _cache[ck] = data
    return data


# ── Публичный интерфейс ────────────────────────────────────────────────────

_channels: dict[str, _Channel] = {}
_started = False
_start_lock = threading.Lock()


def available() -> bool:
    """Готов ли движок играть (библиотеки на месте, хотя бы один канал открыт)."""
    if sd is None:
        return False
    return any(c.stream is not None for c in _channels.values())


def import_error() -> str | None:
    return None if _IMPORT_ERROR is None else f"{type(_IMPORT_ERROR).__name__}: {_IMPORT_ERROR}"


def list_devices() -> list[dict]:
    """Устройства вывода PortAudio: [{index, name, channels, default}]."""
    if sd is None:
        return []
    try:
        devices = sd.query_devices()
        default_out = sd.default.device[1] if isinstance(sd.default.device, (list, tuple)) else None
    except Exception:
        return []
    out = []
    for i, d in enumerate(devices):
        if d.get("max_output_channels", 0) > 0:
            out.append({
                "index": i,
                "name": d.get("name", f"Устройство {i}"),
                "channels": d.get("max_output_channels", 0),
                "default": i == default_out,
            })
    return out


def _resolve_device(name_or_index):
    """Найти устройство по сохранённому имени. Имена стабильнее индексов:
    индекс уезжает, стоит подключить или отключить любое другое устройство."""
    if name_or_index in (None, ""):
        return None
    for d in list_devices():
        if d["name"] == name_or_index:
            return d["index"]
    # Индекс, сохранённый прошлой версией, тоже принимаем.
    try:
        idx = int(name_or_index)
    except (TypeError, ValueError):
        return None
    return idx if any(d["index"] == idx for d in list_devices()) else None


def start(devices: dict[str, str] | None = None) -> dict:
    """Поднять каналы. devices: {'game': '<имя устройства>', ...}, пусто = дефолт.

    Возвращает статус по каналам — сервер отдаёт его панели дилера, чтобы
    оператор видел, что реально открылось, а что нет."""
    global _started
    if sd is None:
        return {"available": False, "error": import_error(), "channels": {}}
    devices = devices or {}
    with _start_lock:
        for name in CHANNEL_NAMES:
            ch = _channels.get(name)
            if ch is None:
                ch = _Channel(name)
                _channels[name] = ch
            ch.open(_resolve_device(devices.get(name)))
        _started = True
    return status()


def stop():
    """Погасить все каналы (выход из приложения)."""
    global _started
    with _start_lock:
        for ch in _channels.values():
            ch.close()
        _started = False


def set_device(channel: str, device_name: str) -> dict:
    """Переключить канал на другое устройство. Играющие звуки при этом
    обрываются — переоткрыть поток PortAudio, не уронив голоса, нельзя."""
    if channel not in CHANNEL_NAMES:
        raise KeyError(channel)
    if sd is None:
        return {"ok": False, "error": import_error()}
    # Имя задано, но такого устройства нет — молча открыть системное по
    # умолчанию нельзя: звук уйдёт не туда, а оператор будет думать, что попал
    # в наушники. Лучше честная ошибка.
    resolved = _resolve_device(device_name)
    if device_name and resolved is None:
        return {"ok": False, "error": f"Устройство не найдено: {device_name}"}
    with _start_lock:
        ch = _channels.get(channel)
        if ch is None:
            ch = _Channel(channel)
            _channels[channel] = ch
        ok = ch.open(resolved)
    return {"ok": ok, "error": ch.error}


def status() -> dict:
    chans = {}
    for name in CHANNEL_NAMES:
        ch = _channels.get(name)
        if ch is None:
            chans[name] = {"open": False, "device": None, "error": None}
            continue
        dev_name = None
        if ch.device is not None:
            for d in list_devices():
                if d["index"] == ch.device:
                    dev_name = d["name"]
                    break
        chans[name] = {"open": ch.stream is not None, "device": dev_name, "error": ch.error}
    return {"available": available(), "started": _started,
            "error": import_error(), "channels": chans}


def play(path: Path, channel: str = "game", gain: float = 1.0, key: str = "") -> bool:
    """Проиграть файл разово. Возвращает False, если канал закрыт или файл битый."""
    ch = _channels.get(channel)
    if ch is None or ch.stream is None:
        return False
    data = load(path)
    if data is None or len(data) == 0:
        return False
    ch.add(data, gain, loop=False, key=key or path.stem)
    return True


def play_loop(path: Path, channel: str = "game", gain: float = 1.0, key: str = "") -> bool:
    """Зациклить файл. Повторный вызов с тем же key только правит громкость,
    чтобы фоновая музыка не начиналась заново на каждом обновлении состояния."""
    ch = _channels.get(channel)
    if ch is None or ch.stream is None:
        return False
    k = key or path.stem
    if ch.has(k):
        ch.set_gain(k, gain)
        return True
    data = load(path)
    if data is None or len(data) == 0:
        return False
    ch.add(data, gain, loop=True, key=k)
    return True


def stop_key(channel: str, key: str):
    ch = _channels.get(channel)
    if ch is not None:
        ch.stop_key(key)


def stop_channel(channel: str):
    ch = _channels.get(channel)
    if ch is not None:
        ch.stop_all()


def set_gain(channel: str, key: str, gain: float):
    ch = _channels.get(channel)
    if ch is not None:
        ch.set_gain(key, gain)
