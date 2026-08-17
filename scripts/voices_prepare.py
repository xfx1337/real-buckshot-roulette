"""Turn the voices/ folder into one flat set of uploadable recordings.

voices/ is what people actually sent: a folder per person, Russian filenames,
several Telegram voice notes for some and one long podcast rip for others, plus
a handful of folders that are empty because the recording never arrived. None of
that survives the trip to the farm — the name becomes an scp argument, a remote
path and a directory of a thousand phrases, so it has to be latin, and the farm
takes exactly one recording per voice.

So this collapses each folder to a single file under a transliterated name:

    voices/common/andrey_nechaev/*.ogg   ->  build/voices_flat/andrey_nechaev.ogg
    voices/uncommon/Убермаргинал.mp3     ->  build/voices_flat/ubermarginal.mp3

Three rules decide what a folder collapses to.

A folder that already carries a `<name>_total.*` is taken as-is: that file is
the person's own material already joined, and re-joining the pieces beside it
would duplicate every second of audio in the sample.

A folder of several separate recordings is concatenated, longest first. F5 reads
the beginning of a reference before it reads the end, and the longest take is
the one most likely to be clean speech rather than a two-second "ну привет".

A folder holding one file is copied.

Cleaning is deliberately NOT done here. tts/pregenerate.prepare runs a denoise,
gate and loudnorm chain on the farm when it cuts the sample, and doing a second
pass on this side would mean two gates and two loudness passes over the same
audio — the artefacts stack and the clone loses breath. This step only decides
*which audio* and *under what name*; the farm decides what it sounds like.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VOICES = ROOT / "voices"
OUT = ROOT / "build" / "voices_flat"

# What counts as a recording. Everything else in these folders (.DS_Store and
# whatever else a Mac leaves behind) is ignored rather than reported, because
# reporting it once per folder buries the findings that matter.
AUDIO = {".ogg", ".mp3", ".wav", ".m4a", ".mp4", ".opus", ".flac", ".aac"}

# Names that are already latin keep their spelling; the rest are transliterated.
# Folder names in voices/ are mostly latin already — this table exists for the
# few that are not, and for filenames, which are Russian far more often.
TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
    "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "h", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "sch",
    "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}

# The farm refuses anything else, and refusing here costs one line instead of a
# round trip. Mirrors app/voice_farm._valid_name.
NAME_MAX = 40


def translit(text: str) -> str:
    """A Russian name as the farm will accept it: latin, digits, underscore."""
    out = []
    for char in text.lower():
        if char in TRANSLIT:
            out.append(TRANSLIT[char])
        elif char.isalnum() and char.isascii():
            out.append(char)
        else:
            out.append("_")
    name = "".join(out).strip("_")
    while "__" in name:
        name = name.replace("__", "_")
    return name[:NAME_MAX]


def duration(path: Path) -> float:
    """Seconds of audio, or 0.0 for anything ffprobe cannot read."""
    try:
        done = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, timeout=60)
        return float((done.stdout or "0").strip() or 0)
    except (subprocess.SubprocessError, ValueError):
        return 0.0


def digest(path: Path) -> str:
    """Content hash, used to catch the same recording filed under two people."""
    h = hashlib.md5()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def recordings(folder: Path) -> list[Path]:
    return sorted(p for p in folder.iterdir()
                  if p.is_file() and p.suffix.lower() in AUDIO)


def join(pieces: list[Path], target: Path) -> None:
    """Concatenate several takes into one recording, longest take first.

    Re-encoded rather than stream-copied: the pieces are a mix of ogg/opus and
    mp3 at different rates, and concat of mismatched streams produces a file
    whose later takes play at the wrong speed or not at all. One encode to
    48 kHz mono ogg costs a few seconds and removes the whole class of problem.
    """
    ordered = sorted(pieces, key=duration, reverse=True)
    listing = target.with_suffix(".txt")
    listing.write_text(
        "".join(f"file '{p.resolve()}'\n" for p in ordered), encoding="utf-8")
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
             "-f", "concat", "-safe", "0", "-i", str(listing),
             "-ac", "1", "-ar", "48000", "-c:a", "libopus", "-b:a", "96k",
             str(target)],
            check=True, capture_output=True)
    finally:
        listing.unlink(missing_ok=True)


def collapse(folder: Path, name: str, out: Path) -> tuple[Path | None, str]:
    """The one recording this folder contributes, and how it was chosen."""
    files = recordings(folder)
    if not files:
        return None, "пусто"

    total = [p for p in files if "_total" in p.stem.lower()]
    if total:
        pick = max(total, key=duration)
        target = out / f"{name}{pick.suffix.lower()}"
        shutil.copy2(pick, target)
        return target, f"готовая склейка {pick.name}"

    if len(files) == 1:
        pick = files[0]
        target = out / f"{name}{pick.suffix.lower()}"
        shutil.copy2(pick, target)
        return target, f"один файл {pick.name}"

    target = out / f"{name}.ogg"
    join(files, target)
    return target, f"склеено {len(files)} файлов"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Собрать voices/ в плоский набор для фермы.")
    parser.add_argument("--out", type=Path, default=OUT,
                        help="куда складывать (по умолчанию build/voices_flat)")
    args = parser.parse_args(argv)

    out = args.out
    out.mkdir(parents=True, exist_ok=True)

    manifest: list[dict] = []
    skipped: list[dict] = []
    seen: dict[str, str] = {}

    # Folders whose recording matches their own name go first, so that when the
    # same file sits in two people's folders the one it actually belongs to is
    # the one that keeps it. voices/uncommon/nagiev/ holds Папич.mp3 — a
    # misfile, and in plain alphabetical order nagiev would claim Папич's voice
    # and papich would be dropped as the duplicate.
    def owns_its_audio(folder: Path) -> int:
        name = translit(folder.name)
        return 0 if any(translit(p.stem) == name for p in recordings(folder)) else 1

    for group in ("common", "uncommon"):
        base = VOICES / group
        if not base.is_dir():
            continue
        folders = sorted((p for p in base.iterdir() if p.is_dir()),
                         key=lambda p: (owns_its_audio(p), p.name))
        for folder in folders:
            name = translit(folder.name)
            target, why = collapse(folder, name, out)
            if target is None:
                skipped.append({"voice": name, "group": group, "why": why})
                print(f"  — {group}/{name}: {why}, пропущен")
                continue

            # The same recording under two people's folders makes two voices
            # that sound identical, and the duplicate is found here rather than
            # after an hour of GPU time has gone into it.
            fingerprint = digest(target)
            if fingerprint in seen:
                target.unlink()
                skipped.append({"voice": name, "group": group,
                                "why": f"дубль записи {seen[fingerprint]}"})
                print(f"  — {group}/{name}: та же запись, что у "
                      f"{seen[fingerprint]}, пропущен")
                continue
            seen[fingerprint] = name

            seconds = duration(target)
            manifest.append({
                "voice": name, "group": group, "source": folder.name,
                "file": target.name, "seconds": round(seconds, 1),
                "chosen": why,
            })
            print(f"  ✓ {group}/{name}: {why}, {seconds:.0f}s")

    (out / "manifest.json").write_text(
        json.dumps({"voices": manifest, "skipped": skipped},
                   ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\nготово: {len(manifest)} голосов в {out}")
    if skipped:
        print(f"пропущено: {len(skipped)} "
              f"({', '.join(s['voice'] for s in skipped)})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
