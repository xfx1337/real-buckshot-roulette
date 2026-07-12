#!/usr/bin/env python3
"""
Запекает стандартные звуки из reference/Buckshot Roulette/ в
app/static/audio/defaults/<key><ext>, чтобы они попали в Docker-образ
(reference/ в образ не копируется — см. .dockerignore).

Запускать на ХОСТЕ, где есть reference/:
    python scripts/bake_sounds.py

Идемпотентно: перезаписывает целевые файлы. После прогона пересобери образ.
"""
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import sound_config as sc  # noqa: E402


def main() -> int:
    dst_dir = sc._DEFAULTS_DIR
    dst_dir.mkdir(parents=True, exist_ok=True)
    ok, missing = 0, []
    for ev in sc.SOUND_EVENTS:
        srrc = sc._REFERENCE_DIR / ev["default"]
        if not srrc.exists():
            missing.append((ev["key"], ev["default"]))
            continue
        dst = sc._baked_path(ev)
        shutil.copy2(srrc, dst)
        ok += 1
    print(f"Запечено: {ok}/{len(sc.SOUND_EVENTS)} в {dst_dir}")
    if missing:
        print("НЕ НАЙДЕНЫ в reference/:")
        for k, p in missing:
            print(f"  {k}: {p}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
