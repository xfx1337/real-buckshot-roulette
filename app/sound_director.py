"""
Серверная озвучка событий — Buckshot Roulette IRL.

Порт логики `app/static/sound.js` на сервер. Браузерный движок смотрел на
снапшот состояния, приходящий по WebSocket, и решал, какой звук сыграть.
Здесь то же самое, только состояние доступно напрямую — гонять его через сеть,
чтобы клиент отправил решение обратно, незачем.

Правила детекта намеренно повторяют клиентские дословно (те же подстроки в
`message`, тот же выбор loop по фазе): пока браузерный тракт жив как fallback,
два движка должны звучать одинаково, иначе переключение между ними будет
слышно.

Играет через `app/audio_engine.py` в канал 'game'. Канал 'video' остаётся за
браузером: там звук идёт вместе с картинкой из `<video>`, и разводить их
означало бы синхронизировать вручную.

Движок держит своё состояние (прошлая фаза, прошлый лог), поэтому экземпляр
один на процесс — см. `director` внизу файла.
"""

from __future__ import annotations

import threading
import time

from . import audio_engine, sound_config

# Фоновая музыка по фазам. Совпадает с LOOP_BY_PHASE в static/sound.js.
LOOP_BY_PHASE = {
    "lobby": "ambient_lobby",
    "round_start": "bgm_menu",
    "dealer_loading": "bgm_menu",
    "dealer_reloading": "bgm_menu",
    "dealer_items": "bgm_menu",
    "round_over": "ambient_between_rounds",
    "game_over": "bgm_death",
    # player_turn выбирается динамически (pending → ambient_pending, иначе bgm_main)
}

# Ключ голоса фоновой музыки в микшере. Один на канал: смена фазы подменяет
# трек, а не накладывает второй поверх.
_LOOP_VOICE = "__loop__"

# Выстрелы приглушают фоновую музыку (ducking), как в браузерном движке.
_DUCK_KEYS = {"shot_live", "shot_live_saw", "shot_blank"}
_DUCK_SECONDS = 2.0
_DUCK_FACTOR = 0.1
# Фон всегда тише эффектов — то же соотношение, что в updateLoopVolume().
_LOOP_BASE = 0.55


def classify(entry: dict, log_list: list, entry_idx: int, state: dict) -> str | None:
    """Какой звук соответствует записи лога. Порт classify() из sound.js."""
    m = entry.get("message") or ""
    t = entry.get("type") or ""

    if t == "shot":
        if m.startswith("[КУРОК]"):
            # Физический выстрел: звук играем сразу по нажатию курка.
            is_saw = bool(state.get("saw_active"))
            if "(БОЕВОЙ)" in m or "(СЕРЕБРЯНЫЙ)" in m:
                return "shot_live_saw" if is_saw else "shot_live"
            if "(ХОЛОСТОЙ)" in m:
                return "shot_blank"
            return "trigger_pull"

        if m.startswith("[БОЕВОЙ]") or m.startswith("[СЕРЕБРЯНЫЙ]") or m.startswith("[ХОЛОСТОЙ]"):
            # Исход выстрела мог быть уже озвучен на этапе [КУРОК] — тогда
            # молчим, иначе один выстрел прозвучит дважды.
            was_physical = False
            if log_list and entry_idx > 0:
                for i in range(entry_idx - 1, -1, -1):
                    prev = log_list[i]
                    prev_msg = prev.get("message") or ""
                    if (prev.get("type") or "") == "shot":
                        if prev_msg.startswith("[КУРОК]"):
                            was_physical = True
                            break
                        if (prev_msg.startswith("[БОЕВОЙ]")
                                or prev_msg.startswith("[СЕРЕБРЯНЫЙ]")
                                or prev_msg.startswith("[ХОЛОСТОЙ]")):
                            # Предыдущий исход встретился раньше [КУРОК] —
                            # значит этот выстрел ручной/дилерский.
                            break
            if was_physical:
                return None

            if m.startswith("[БОЕВОЙ]"):
                return "shot_live_saw" if "(-2 HP)" in m else "shot_live"
            if m.startswith("[СЕРЕБРЯНЫЙ]"):
                return "shot_live"
            if m.startswith("[ХОЛОСТОЙ]"):
                return "shot_blank"

        if "испорченного лекарства" in m:
            return "item_medicine_death"
        if "выбыл" in m:
            return "player_dead"
        return None

    if t == "item":
        if "наручники" in m and "пропуска" in m:
            return "handcuff_skip"
        if "испорченного лекарства" in m:
            return "item_medicine_death"
        return None

    if t == "round":
        if "Новый магазин" in m or "расстреляны" in m:
            return "new_magazine"
        if "ВЫИГРАЛ" in m:
            return "round_win"
        if "Ничья" in m:
            return "round_draw"
        if "ПОБЕДИТЕЛЬ" in m:
            return "game_over"
        return None

    if t == "system":
        if "присоединился" in m:
            return "player_join"
        if "покинул" in m:
            return "player_leave"
        if "изменил HP" in m:
            return "hp_adjust"
        if "Игра завершена дилером" in m:
            return "force_end_game"
        if "Раунд завершен дилером" in m:
            return "force_round_over"
        return None

    if t == "info":
        if "дополнительный ход" in m:
            return "blank_self_extra"
        if "пропускает ход" in m:
            return "handcuff_skip"
        return None

    return None


def loop_for_state(state: dict) -> str | None:
    phase = state.get("phase")
    if not phase:
        return None
    if phase == "player_turn":
        return "ambient_pending" if state.get("pending_shot") else "bgm_main"
    return LOOP_BY_PHASE.get(phase)


class SoundDirector:
    """Решает, что играть, по потоку снапшотов состояния."""

    def __init__(self):
        self.enabled = False          # серверный звук выключен по умолчанию
        self.master_volume = 0.8
        self.ducking_enabled = True
        self._ducked_until = 0.0
        self._lock = threading.Lock()
        self.reset()

    def reset(self):
        """Забыть историю. Нужно при включении движка, иначе первый же снапшот
        прозвучит как пачка «новых» событий из уже прошедшей игры."""
        with self._lock:
            self.initialized = False
            self.prev_phase = None
            self.prev_player_id = None
            self.prev_log: list = []
            self.prev_show_shells = None
            self.loop_key = None

    # ── Проигрывание ───────────────────────────────────────────────────────
    def _gain_for(self, key: str) -> float:
        return self.master_volume * sound_config.get_volume(key)

    def play(self, key: str, force: bool = False) -> bool:
        """Проиграть событие. force=1 — проверка из панели, играет даже если
        событие выключено (как preview в браузерном тракте)."""
        if not force and not self.enabled:
            return False
        if not force and not sound_config.is_enabled(key):
            return False
        path = sound_config.resolve_file(key)
        if path is None:
            return False
        if key in _DUCK_KEYS:
            self._duck()
        return audio_engine.play(path, "game", self._gain_for(key), key)

    def _loop_gain(self, key: str) -> float:
        base = self.master_volume * _LOOP_BASE * sound_config.get_volume(key)
        if self.ducking_enabled and time.monotonic() < self._ducked_until:
            base *= _DUCK_FACTOR
        return base

    def _duck(self):
        if not self.ducking_enabled or self.loop_key is None:
            return
        self._ducked_until = time.monotonic() + _DUCK_SECONDS
        audio_engine.set_gain("game", _LOOP_VOICE, self._loop_gain(self.loop_key))
        # Через _DUCK_SECONDS вернуть громкость. Таймер-демон, чтобы не держать
        # процесс при выходе.
        t = threading.Timer(_DUCK_SECONDS, self._unduck)
        t.daemon = True
        t.start()

    def _unduck(self):
        if self.loop_key:
            audio_engine.set_gain("game", _LOOP_VOICE, self._loop_gain(self.loop_key))

    def set_loop(self, key: str | None):
        """Сменить фоновую музыку. Тот же ключ — только правим громкость,
        чтобы трек не начинался заново на каждом снапшоте."""
        if key and key == self.loop_key and sound_config.is_enabled(key):
            audio_engine.set_gain("game", _LOOP_VOICE, self._loop_gain(key))
            return

        # Любой другой случай начинается со снятия текущего трека: он либо
        # больше не нужен, либо его выключили в настройках, либо сменилась фаза.
        audio_engine.stop_key("game", _LOOP_VOICE)
        self.loop_key = None

        if not key or not sound_config.is_enabled(key):
            return
        path = sound_config.resolve_file(key)
        if path is None:
            return
        if audio_engine.play_loop(path, "game", self._loop_gain(key), _LOOP_VOICE):
            self.loop_key = key

    # ── Детект новых записей лога ──────────────────────────────────────────
    def _new_entries(self, cur: list) -> list:
        """Хвост лога после последней записи, которую уже видели."""
        cur = cur or []
        if not cur:
            return []
        prev = self.prev_log
        if not prev:
            # Первый снапшот озвучивать нельзя — там вся история игры.
            return cur[-4:] if self.initialized else []
        last = prev[-1]
        for i in range(len(cur) - 1, -1, -1):
            if (cur[i].get("message") == last.get("message")
                    and cur[i].get("type") == last.get("type")):
                return cur[i + 1:]
        # Совпадения нет — лог обрезали или заменили; капим, чтобы не выстрелить
        # десятком звуков разом.
        return cur[-4:]

    # ── Главная точка входа ────────────────────────────────────────────────
    def on_state(self, state: dict | None):
        """Обработать снапшот состояния игры. Зеркало onState() из sound.js."""
        if not state or not self.enabled or not audio_engine.available():
            return
        with self._lock:
            phase = state.get("phase") or "no_game"
            cur_player = state.get("current_player") or {}
            cur_player_id = cur_player.get("id") if cur_player else None
            log = state.get("log") or []

            for entry in self._new_entries(log):
                try:
                    idx = log.index(entry)
                except ValueError:
                    idx = -1
                key = classify(entry, log, idx, state)
                if key:
                    self.play(key)

            if phase != self.prev_phase:
                if self.prev_phase == "lobby" and phase not in ("lobby", "no_game"):
                    self.play("game_start")
                elif phase == "round_start":
                    self.play("next_round" if self.prev_phase == "round_over" else "round_start")
                if phase == "dealer_loading":
                    self.play("dealer_loading")
                if phase == "dealer_reloading":
                    self.play("dealer_reloading")
                if phase == "dealer_items":
                    self.play("dealer_items")
                if phase == "round_over":
                    self.play("heaven")
                if phase == "player_turn":
                    self.play("turn_start")
            elif phase == "player_turn" and cur_player_id and cur_player_id != self.prev_player_id:
                self.play("turn_start")

            show_shells = state.get("show_shells_to_players")
            if (self.prev_show_shells is not None and show_shells is not None
                    and show_shells != self.prev_show_shells):
                self.play("toggle_shells")

            self.set_loop(loop_for_state(state))

            self.initialized = True
            self.prev_phase = phase
            self.prev_player_id = cur_player_id
            self.prev_log = list(log)
            if show_shells is not None:
                self.prev_show_shells = show_shells

    # ── Управление из панели дилера ────────────────────────────────────────
    def set_enabled(self, on: bool) -> dict:
        """Включить/выключить серверный звук. При выключении гасим канал —
        иначе фоновая музыка продолжит играть без движка, который ей управляет."""
        on = bool(on)
        if on == self.enabled:
            return self.status()
        self.enabled = on
        if on:
            # История прошлой сессии не должна прозвучать пачкой при включении.
            self.reset()
        else:
            audio_engine.stop_channel("game")
            self.loop_key = None
        return self.status()

    def set_volume(self, v: float):
        self.master_volume = max(0.0, min(1.0, float(v)))
        if self.loop_key:
            audio_engine.set_gain("game", _LOOP_VOICE, self._loop_gain(self.loop_key))

    def set_ducking(self, on: bool):
        self.ducking_enabled = bool(on)
        if self.loop_key:
            audio_engine.set_gain("game", _LOOP_VOICE, self._loop_gain(self.loop_key))

    def status(self) -> dict:
        return {
            "enabled": self.enabled,
            "volume": self.master_volume,
            "ducking": self.ducking_enabled,
            "loop": self.loop_key,
            "engine": audio_engine.status(),
        }


director = SoundDirector()
