"""
Sound engine — конфигурация озвучки.

Каждое игровое событие имеет ключ (`key`) и стандартный звук из папки
референсов оригинального Buckshot Roulette (`reference/Buckshot Roulette/`).
Оператор-дилер может через веб-интерфейс:
  * отключить/включить звук события;
  * загрузить свой файл вместо стандартного (upload);
  * сбросить обратно на стандартный.

Отклонения от дефолта хранятся в `sound_overrides.json` в корне проекта
(структура: {key: {"enabled": bool, "custom": "<имя файла>"|null}}). Стандартные
пути НЕ хранятся в оверрайдах — они заданы кодом ниже (SOUND_EVENTS), поэтому
обновление дефолтов не ломает сохранённые пользовательские настройки.

Кастомные загрузки лежат в `app/static/audio/custom/`.

Список событий согласован с docs/SOUND_EVENTS.md; отмеченные пользователем как
не реализуемые (НЕ ШАРИМ / большая задержка / ХЗ) сюда НЕ включены.
"""

import json
from pathlib import Path

_ROOT_DIR = Path(__file__).resolve().parent.parent
_REFERENCE_DIR = _ROOT_DIR / "reference" / "Buckshot Roulette"
_OVERRIDES_PATH = _ROOT_DIR / "sound_overrides.json"
_STATIC_AUDIO = Path(__file__).resolve().parent / "static" / "audio"
_CUSTOM_DIR = _STATIC_AUDIO / "custom"
# Запечённые стандартные звуки: копии нужных файлов из reference/, лежат ВНУТРИ
# app/ (значит попадают в Docker-образ через `COPY app/`). Имя файла — <key><ext>.
# Заполняется скриптом scripts/bake_sounds.py. reference/ в образ не копируется,
# поэтому в контейнере резолвер берёт звук отсюда (см. resolve_file).
_DEFAULTS_DIR = _STATIC_AUDIO / "defaults"

# Разрешённые расширения для загрузки/стандартных звуков.
ALLOWED_EXT = {".ogg", ".wav", ".mp3"}

MIME_BY_EXT = {
    ".ogg": "audio/ogg",
    ".wav": "audio/wav",
    ".mp3": "audio/mpeg",
}


# ── Каноничный список событий ──────────────────────────────────────────────
# Поля: key, category, label, default (путь относительно _REFERENCE_DIR),
# loop (True для ambient/BGM — зацикленные).
# Порядок важен: в таком порядке рендерится вкладка настроек дилера.
SOUND_EVENTS = [
    # A. Фазы игры
    {"key": "player_join",       "category": "Фазы",     "label": "Игрок присоединился",      "default": "audio/kick door enter lobby.ogg", "loop": False},
    {"key": "game_start",        "category": "Фазы",     "label": "Старт игры",               "default": "audio/button_start main.ogg", "loop": False},
    {"key": "round_start",       "category": "Фазы",     "label": "Начало раунда",            "default": "audio/round blinker wave.ogg", "loop": False},
    {"key": "dealer_loading",    "category": "Фазы",     "label": "Дробовик заряжен",         "default": "audio/load single shell.ogg", "loop": False},
    {"key": "dealer_reloading",  "category": "Фазы",     "label": "Требуется дозарядка",       "default": "audio/rack shotgun.ogg", "loop": False},
    {"key": "dealer_items",      "category": "Фазы",     "label": "Раздача предметов",         "default": "audio/open briefcase.ogg", "loop": False},
    {"key": "turn_start",        "category": "Фазы",     "label": "Начало хода игрока",        "default": "multiplayer/audio/main audio/mp_display_turn order bootup1.ogg", "loop": False},
    {"key": "round_win",         "category": "Фазы",     "label": "Раунд выигран",            "default": "audio/winner.ogg", "loop": False},
    {"key": "heaven",            "category": "Фазы",     "label": "Завершение раунда (звук «из рая»)", "default": "audio/god.ogg", "loop": False, "default_volume": 1.4},
    {"key": "round_draw",        "category": "Фазы",     "label": "Ничья в раунде",           "default": "audio/round indicator shut down.ogg", "loop": False},
    {"key": "game_over",         "category": "Фазы",     "label": "Победитель / конец игры",   "default": "audio/playtest win.ogg", "loop": False},

    # B. Выстрелы
    {"key": "trigger_pull",      "category": "Выстрелы", "label": "Курок нажат (ждём цель)",   "default": "audio/gun foley1.ogg", "loop": False},
    {"key": "shot_live",         "category": "Выстрелы", "label": "БОЕВОЙ −1 HP",             "default": "audio/temp gunshot_live.wav", "loop": False, "default_enabled": False},
    {"key": "shot_live_saw",     "category": "Выстрелы", "label": "БОЕВОЙ x2 (пила) −2 HP",   "default": "multiplayer/audio/main audio/mp_gun fire5.ogg", "loop": False},
    {"key": "shot_blank",        "category": "Выстрелы", "label": "ХОЛОСТОЙ",                 "default": "audio/temp gunshot_blank.wav", "loop": False, "default_enabled": False},
    {"key": "blank_self_extra",  "category": "Выстрелы", "label": "Холостой в себя (доп. ход)", "default": "multiplayer/audio/main audio/mp_dry fire1.wav", "loop": False},
    {"key": "player_dead",       "category": "Выстрелы", "label": "Игрок выбыл",              "default": "audio/splatter1.ogg", "loop": False},
    {"key": "new_magazine",      "category": "Выстрелы", "label": "Новый магазин",            "default": "audio/shell latch1.ogg", "loop": False},

    # C. Предметы (только согласованные)
    {"key": "item_medicine_death", "category": "Предметы", "label": "Смерть от лекарства",     "default": "audio/player death medicine.ogg", "loop": False},
    {"key": "handcuff_skip",       "category": "Предметы", "label": "Пропуск хода (наручники)", "default": "audio/player check handcuffs.ogg", "loop": False},

    # D. Действия дилера / системные
    {"key": "hp_adjust",         "category": "Дилер",    "label": "Дилер изменил HP",         "default": "audio/health counter beep2.wav", "loop": False},
    {"key": "hp_adjust_death",   "category": "Дилер",    "label": "Выбыл от правки HP",        "default": "audio/splatter1.ogg", "loop": False},
    {"key": "toggle_shells",     "category": "Дилер",    "label": "Переключил показ патронов", "default": "audio/crt_part click.ogg", "loop": False},
    {"key": "force_end_game",    "category": "Дилер",    "label": "Force-end игры",           "default": "audio/crt_turn off display2.ogg", "loop": False},
    {"key": "force_round_over",  "category": "Дилер",    "label": "Force-end раунда",          "default": "audio/round indicator shut down.ogg", "loop": False},
    {"key": "next_round",        "category": "Дилер",    "label": "Следующий раунд",          "default": "audio/round blinker wave.ogg", "loop": False},
    {"key": "undo",              "category": "Дилер",    "label": "Отмена действия",           "default": "audio/button press2.ogg", "loop": False},
    {"key": "player_leave",      "category": "Дилер",    "label": "Игрок покинул игру",        "default": "multiplayer/audio/misc audio/mp_beep exit.ogg", "loop": False},

    # E. UI / веб
    {"key": "ui_click",          "category": "UI",       "label": "Клик кнопки",              "default": "audio/button_press.ogg", "loop": False},
    {"key": "ui_settings_open",  "category": "UI",       "label": "Открытие настроек",         "default": "audio/crt_show icons.ogg", "loop": False},
    {"key": "ui_settings_close", "category": "UI",       "label": "Закрытие настроек",         "default": "audio/crt_turn off display2.ogg", "loop": False},
    {"key": "ui_join",           "category": "UI",       "label": "Join / create игры",        "default": "audio/check item_pickup.ogg", "loop": False},
    {"key": "ui_tab_switch",     "category": "UI",       "label": "Переключение вкладок",      "default": "audio/button_hover.ogg", "loop": False},

    # F. Ambient (loop)
    {"key": "ambient_lobby",         "category": "Ambient", "label": "Лобби (фон)",            "default": "audio/club ambience1.ogg", "loop": True},
    {"key": "ambient_idle_turn",     "category": "Ambient", "label": "Ход, простой (фон)",     "default": "multiplayer/audio/music/mp_music desolate loop2.ogg", "loop": True},
    {"key": "ambient_pending",       "category": "Ambient", "label": "Ожидание выбора цели",   "default": "audio/heartbeat effect.ogg", "loop": True},
    {"key": "ambient_between_rounds", "category": "Ambient", "label": "Между раундами",        "default": "multiplayer/audio/music/mp_music resolve.ogg", "loop": True},
    {"key": "ambient_loading",       "category": "Ambient", "label": "Зарядка/дозарядка (фон)", "default": "audio/ambience_fluorescent light.ogg", "loop": True},

    # G. Фоновая музыка (loop)
    {"key": "bgm_menu",      "category": "Музыка", "label": "Меню / вход в комнату (зарядка)", "default": "audio/music/music main_room.ogg", "loop": True},
    {"key": "bgm_main",      "category": "Музыка", "label": "Главная тема (фон)",         "default": "audio/music/music main_techno techno.ogg", "loop": True, "default_volume": 0.3},
    {"key": "bgm_main_loop", "category": "Музыка", "label": "Главная тема — продолжение", "default": "audio/music/music main second loop_techno techno.ogg", "loop": True},
    {"key": "bgm_death",     "category": "Музыка", "label": "Тема смерти/поражения",       "default": "audio/music_true death vol1.ogg", "loop": True},
]

_EVENTS_BY_KEY = {e["key"]: e for e in SOUND_EVENTS}


def _load_overrides() -> dict:
    if not _OVERRIDES_PATH.exists():
        return {}
    try:
        with open(_OVERRIDES_PATH, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def _save_overrides(data: dict) -> None:
    with open(_OVERRIDES_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _custom_path(filename: str) -> Path:
    return _CUSTOM_DIR / filename


def _baked_path(ev: dict) -> Path:
    """Путь к запечённой копии стандартного звука (app/static/audio/defaults)."""
    return _DEFAULTS_DIR / (ev["key"] + Path(ev["default"]).suffix.lower())


def resolve_file(key: str) -> Path | None:
    """
    Абсолютный путь к ЭФФЕКТИВНОМУ звуковому файлу события. Порядок:
      1) кастомный файл оператора (если задан и существует);
      2) стандартный из reference/ (локальная разработка);
      3) запечённая копия в app/static/audio/defaults/ (Docker — reference/ в
         образ не копируется).
    None, если события нет или ни один файл не найден.
    """
    ev = _EVENTS_BY_KEY.get(key)
    if not ev:
        return None
    ov = _load_overrides().get(key, {})
    custom = ov.get("custom")
    if custom:
        p = _custom_path(custom)
        if p.exists():
            return p
    p = _REFERENCE_DIR / ev["default"]
    if p.exists():
        return p
    p = _baked_path(ev)
    return p if p.exists() else None


def _default_enabled(key: str) -> bool:
    return _EVENTS_BY_KEY.get(key, {}).get("default_enabled", True)


def _default_volume(key: str) -> float:
    return _EVENTS_BY_KEY.get(key, {}).get("default_volume", 1.0)


def is_enabled(key: str) -> bool:
    ov = _load_overrides().get(key, {})
    return ov.get("enabled", _default_enabled(key))


def get_config() -> list[dict]:
    """
    Полный список событий с эффективными настройками для веб-интерфейса.
    """
    overrides = _load_overrides()
    out = []
    for ev in SOUND_EVENTS:
        ov = overrides.get(ev["key"], {})
        custom = ov.get("custom")
        eff = resolve_file(ev["key"])
        out.append({
            "key": ev["key"],
            "category": ev["category"],
            "label": ev["label"],
            "loop": ev["loop"],
            "enabled": ov.get("enabled", _default_enabled(ev["key"])),
            "volume": ov.get("volume", _default_volume(ev["key"])),
            "source": "custom" if custom else "default",
            "filename": custom if custom else Path(ev["default"]).name,
            "default_name": Path(ev["default"]).name,
            "available": eff is not None,
        })
    return out


def get_volume(key: str) -> float:
    ov = _load_overrides().get(key, {})
    return ov.get("volume", _default_volume(key))


def set_enabled(key: str, enabled: bool) -> None:
    if key not in _EVENTS_BY_KEY:
        raise KeyError(key)
    data = _load_overrides()
    entry = data.get(key, {})
    entry["enabled"] = bool(enabled)
    data[key] = entry
    _save_overrides(data)


def set_volume(key: str, volume: float) -> None:
    if key not in _EVENTS_BY_KEY:
        raise KeyError(key)
    data = _load_overrides()
    entry = data.get(key, {})
    # Потолок 2.0 (а не 1.0): звук «из рая» и др. могут быть громче базовой
    # единицы — усиление >1 обрабатывается через WebAudio gain на клиенте.
    entry["volume"] = max(0.0, min(2.0, float(volume)))
    data[key] = entry
    _save_overrides(data)


def set_custom(key: str, filename: str) -> None:
    """Назначить событию кастомный файл (уже сохранённый в custom-папке)."""
    if key not in _EVENTS_BY_KEY:
        raise KeyError(key)
    data = _load_overrides()
    entry = data.get(key, {})
    entry["custom"] = filename
    data[key] = entry
    _save_overrides(data)


def reset_custom(key: str) -> None:
    """Убрать кастомный файл — вернуть стандартный звук из референсов."""
    if key not in _EVENTS_BY_KEY:
        raise KeyError(key)
    data = _load_overrides()
    entry = data.get(key, {})
    old = entry.pop("custom", None)
    data[key] = entry
    _save_overrides(data)
    # Удаляем осиротевший файл, если на него больше никто не ссылается.
    if old:
        still_used = any(v.get("custom") == old for v in data.values())
        if not still_used:
            p = _custom_path(old)
            try:
                p.unlink(missing_ok=True)
            except OSError:
                pass


def save_upload(key: str, orig_filename: str, content: bytes) -> str:
    """
    Сохранить загруженный файл в custom-папку и привязать к событию.
    Имя нормализуется в `<key>__<safe-orig>`. Возвращает сохранённое имя.
    """
    if key not in _EVENTS_BY_KEY:
        raise KeyError(key)
    ext = Path(orig_filename).suffix.lower()
    if ext not in ALLOWED_EXT:
        raise ValueError(f"Недопустимый формат {ext}. Разрешено: {', '.join(sorted(ALLOWED_EXT))}")
    _CUSTOM_DIR.mkdir(parents=True, exist_ok=True)
    safe = "".join(c for c in Path(orig_filename).stem if c.isalnum() or c in "-_") or "sound"
    filename = f"{key}__{safe}{ext}"
    with open(_custom_path(filename), "wb") as f:
        f.write(content)
    set_custom(key, filename)
    return filename


def mime_for(path: Path) -> str:
    return MIME_BY_EXT.get(path.suffix.lower(), "application/octet-stream")
