"""
Video configuration for TV mode.
Persistent config stored in video_config.json.
Videos served from app/videos/ directory.
"""

import json
import os
from pathlib import Path

CONFIG_PATH = Path(__file__).parent / "video_config.json"
VIDEOS_DIR = Path(__file__).parent / "videos"

# Video event slots — each maps to a filename in the videos/ directory.
# "static_noise" is the default placeholder (always loops).
DEFAULT_CONFIG = {
    "videos": {
        "static_noise": "static.mp4",
        "intro": "intro.mp4",
        "player_win": "win.mp4",
        "player_lose": "lose.mp4",
    },
    "auto_play": {
        "static_on_create": True,
        "intro_after_static": False,
        "result_on_game_over": True,
        "mute_game_sound": False,
    },
    "loop": {
        "static_noise": True,
        "intro": False,
        "player_win": False,
        "player_lose": False,
    },
    "settings": {
        "volume": 100
    },
    # Multiplayer TV layout. `slots` — сколько секций игроков телевизор рисует
    # в мультиплеере. 0 = авто: столько секций, сколько игроков в партии.
    # Ненулевое значение фиксирует сетку, лишние секции остаются пустыми
    # ("СВОБОДНО") — оператор готовит экран до того, как игроки подключились.
    "multiplayer": {
        "slots": 0
    },
    # CCTV camera-mode settings. The TV (teleplayer) runs its own random
    # auto-cycle timer using these values — no cross-device time sync.
    "cctv": {
        "auto_enabled": True,    # TV auto-flips into camera mode on a random timer
        "min_time": 30,          # min seconds between shows
        "max_time": 120,         # max seconds between shows
        "min_show": 2,           # min seconds a show stays on screen
        "max_show": 10,          # max seconds a show stays on screen
        "mode": "random",        # "random" = coin-flip: one random cam or all; "grid" = all enabled; "single" = one random fullscreen
        "cameras": ["cam1", "cam2", "cam3", "cam4"],  # enabled camera pool (MediaMTX path names)
        # Видимость каждой камеры для игрока. Ключ — имя камеры, значение:
        #   "normal"  — обычный показ в составе пула (по умолчанию);
        #   "rare"    — камера выкинута из пула, но с шансом rare_chance
        #               прорывается на экран одна, как случайный сбой связи;
        #   "blocked" — игрок не видит её никогда, ни авто, ни вручную.
        # Панель дилера /cams показывает все камеры независимо от статуса.
        "visibility": {},
        "rare_chance": 10,       # проценты: вероятность прорыва «редкой» камеры за один показ
        # Fake "signal lost" glitch — TV-only cosmetic. Dealer /cams page never fakes.
        "fake_error": {
            "enabled": False,
            "chance": 0.25,      # per-camera probability each tick
            "interval": 15,      # seconds between fake-error ticks
            "duration": 4        # seconds a faked camera stays "unavailable"
        },
        # Реактивные эффекты ЭЛТ: телевизор реагирует на ход партии, а не шумит
        # ровно. Каждый эффект — независимый тумблер + своя степень (0..100),
        # чтобы оператор гасил лишнее прямо во время игры. Степень 0 = эффект
        # включён, но не виден; тумблер off = слой вообще не рисуется.
        "reactive": {
            # Всплеск помех в момент боевого выстрела.
            "shot_enabled": True,
            "shot_level": 70,
            # Постоянный уровень помех тем выше, чем меньше HP у игрока.
            "hp_enabled": True,
            "hp_level": 60,
            # Срыв кадра (потеря синхронизации) в момент смерти игрока.
            "death_enabled": True,
            "death_level": 80,
            # Дрожание картинки, пока дилер выбирает цель после выстрела.
            "pending_enabled": True,
            "pending_level": 45,
            # Послесвечение люминофора — след за исчезающими элементами.
            "afterglow_enabled": False,
            "afterglow_level": 50,
        },
        # Artificial picture degradation shown to the player on the TV — blur,
        # grain, scanlines. Dealer /cams viewer is never degraded.
        "degrade": {
            "enabled": False,
            "level": 50,         # 0..100 "badness" strength (base / static level)
            # Dynamic drift: each camera walks its own degradation level between
            # min and max, re-rolling a new target every `interval` seconds.
            # Cameras drift independently, so the picture never looks synced.
            "dynamic": False,
            "min_level": 10,     # 0..100 lower bound of the walk
            "max_level": 85,     # 0..100 upper bound of the walk
            "interval": 6        # seconds between per-camera re-rolls
        }
    }
}

SLOT_LABELS = {
    "static_noise": "Помехи / белый шум (placeholder)",
    "intro": "Вступительный ролик",
    "player_win": "Победа игрока",
    "player_lose": "Поражение игрока",
}


def _merge_defaults(cfg: dict, defaults: dict) -> None:
    """Fill in missing keys from `defaults` at any nesting depth, in place.
    Existing values are never overwritten — only gaps are filled, so newly
    added settings (e.g. cctv.degrade.dynamic) reach configs saved earlier."""
    for key, val in defaults.items():
        if key not in cfg:
            cfg[key] = dict(val) if isinstance(val, dict) else val
        elif isinstance(val, dict) and isinstance(cfg[key], dict):
            _merge_defaults(cfg[key], val)


def load_config() -> dict:
    """Load video config from disk, or return defaults."""
    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            _merge_defaults(cfg, DEFAULT_CONFIG)
            return cfg
        except Exception:
            pass
    return {k: (dict(v) if isinstance(v, dict) else v) for k, v in DEFAULT_CONFIG.items()}


def save_config(cfg: dict) -> None:
    """Save video config to disk."""
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def list_videos() -> list[str]:
    """List video files in the videos/ directory."""
    os.makedirs(VIDEOS_DIR, exist_ok=True)
    exts = {".mp4", ".webm", ".ogg", ".mov", ".avi", ".mkv"}
    return sorted(
        f.name for f in VIDEOS_DIR.iterdir()
        if f.is_file() and f.suffix.lower() in exts
    )
