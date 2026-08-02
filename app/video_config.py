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
    },
    "loop": {
        "static_noise": True,
        "intro": False,
        "player_win": False,
        "player_lose": False,
    },
    "settings": {
        "volume": 100
    }
}

SLOT_LABELS = {
    "static_noise": "Помехи / белый шум (placeholder)",
    "intro": "Вступительный ролик",
    "player_win": "Победа игрока",
    "player_lose": "Поражение игрока",
}


def load_config() -> dict:
    """Load video config from disk, or return defaults."""
    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            # Merge defaults for any missing keys
            for key, val in DEFAULT_CONFIG.items():
                if key not in cfg:
                    cfg[key] = val
                elif isinstance(val, dict):
                    for k2, v2 in val.items():
                        cfg[key].setdefault(k2, v2)
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
