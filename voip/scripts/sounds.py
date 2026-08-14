"""
The library of files that can be played down the phone.

Anything ffmpeg can read is accepted — mp3, m4a, wav, ogg, flac. It is
converted once into the form the telephone path actually carries and cached in
sounds/converted/, so picking a file to call with never waits on a transcode.

The target is 8 kHz mono signed 16-bit little-endian, saved as .sln. That is
Asterisk's own internal format for narrowband audio: the gateway's G.711
codecs are 8 kHz mono, so the sample rate has to come down whatever happens,
and .sln lets Asterisk do the final G.711 encoding itself instead of decoding
someone else's. Handing it an untouched 44.1 kHz stereo mp3 works, but the
resampling then happens per call, in the middle of the audio path.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCE_DIR = ROOT / "sounds"
CONVERTED_DIR = SOURCE_DIR / "converted"

# Where Asterisk looks. Playback(foo) with no path reads from here.
ASTERISK_SOUNDS = Path("/opt/homebrew/var/lib/asterisk/sounds/en")

SOURCE_SUFFIXES = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".opus",
                   ".flac", ".aiff", ".aif", ".wma", ".mp4"}

# Asterisk names a sound by a bare word, without a path or an extension, so
# only what survives that is allowed through.
SAFE_NAME = re.compile(r"[^a-z0-9_]+")


class SoundError(RuntimeError):
    pass


@dataclass
class Sound:
    name: str          # what the dialplan is given, e.g. "alarm"
    source: Path       # the original file the operator dropped in
    converted: Path    # the 8 kHz .sln actually played
    seconds: float

    @property
    def label(self) -> str:
        return f"{self.name} ({self.seconds:.0f}s)"


# Asterisk names a sound by a bare word, so a Cyrillic filename has to become
# one. Dropping the characters it cannot use would leave nothing at all —
# "зоопарк" reduces to the empty string — so they are transliterated instead,
# which keeps the name recognisable to whoever dropped the file in.
CYRILLIC = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
    "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "h", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "sch",
    "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}


def _safe_name(stem: str) -> str:
    lowered = "".join(CYRILLIC.get(character, character)
                      for character in stem.lower())
    name = SAFE_NAME.sub("_", lowered).strip("_")
    if not name:
        raise SoundError(f"nothing usable as a name in {stem!r}")
    return name


def _ffprobe_seconds(path: Path) -> float:
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", str(path)],
            capture_output=True, text=True, timeout=30,
        )
        return float(out.stdout.strip())
    except (OSError, ValueError, subprocess.SubprocessError):
        return 0.0


def convert(source: Path, force: bool = False) -> Sound:
    """Convert one file into the played form, reusing the cache if it is fresh."""
    if not source.is_file():
        raise SoundError(f"no such file: {source}")

    name = _safe_name(source.stem)
    CONVERTED_DIR.mkdir(parents=True, exist_ok=True)
    target = CONVERTED_DIR / f"{name}.sln"

    stale = (not target.exists()
             or target.stat().st_mtime < source.stat().st_mtime)
    if force or stale:
        # -ac 1        the phone line is mono
        # -ar 8000     what G.711 carries; anything above it is discarded
        # -f s16le     raw signed 16-bit LE, which is what .sln holds
        # loudnorm     phone earpieces are quiet and source levels vary wildly;
        #              without it one file is inaudible and the next is clipped
        result = subprocess.run(
            ["ffmpeg", "-y", "-i", str(source),
             "-af", "loudnorm=I=-16:TP=-1.5:LRA=11",
             "-ac", "1", "-ar", "8000", "-f", "s16le", str(target)],
            capture_output=True, text=True, timeout=300,
        )
        if result.returncode != 0:
            target.unlink(missing_ok=True)
            tail = result.stderr.strip().splitlines()[-3:]
            raise SoundError(f"ffmpeg could not convert {source.name}:\n"
                             + "\n".join(tail))

    return Sound(name=name, source=source, converted=target,
                 seconds=_ffprobe_seconds(source))


def publish(sound: Sound) -> None:
    """Put the converted file where Asterisk will find it by name.

    Copied rather than symlinked: Asterisk opens sound files through its own
    path handling, and a link pointing outside its sounds directory is not
    always followed.
    """
    ASTERISK_SOUNDS.mkdir(parents=True, exist_ok=True)
    destination = ASTERISK_SOUNDS / f"{sound.name}.sln"
    data = sound.converted.read_bytes()
    if not destination.exists() or destination.read_bytes() != data:
        destination.write_bytes(data)


def library(force: bool = False) -> dict[str, Sound]:
    """Every playable sound, converted and published, keyed by name."""
    SOURCE_DIR.mkdir(parents=True, exist_ok=True)
    sounds: dict[str, Sound] = {}
    for path in sorted(SOURCE_DIR.iterdir()):
        if path.is_dir() or path.suffix.lower() not in SOURCE_SUFFIXES:
            continue
        try:
            sound = convert(path, force=force)
        except SoundError:
            # One unusable file must not empty the whole library: the caller
            # is usually a picker listing what can be played, and losing
            # every other sound to one bad name is worse than skipping it.
            continue
        publish(sound)
        sounds[sound.name] = sound
    return sounds


def resolve(choice: str, sounds: dict[str, Sound] | None = None) -> Sound:
    """Turn what someone typed into a sound: a name, a number, or a path."""
    sounds = library() if sounds is None else sounds

    if choice in sounds:
        return sounds[choice]

    # A number, as printed by the picker.
    if choice.isdigit():
        ordered = list(sounds.values())
        index = int(choice) - 1
        if 0 <= index < len(ordered):
            return ordered[index]
        raise SoundError(f"there is no sound {choice}; there are {len(ordered)}")

    # A path to a file outside the library: convert it in place and adopt it.
    path = Path(choice).expanduser()
    if path.is_file():
        sound = convert(path)
        publish(sound)
        return sound

    known = ", ".join(sounds) or "none yet"
    raise SoundError(f"unknown sound {choice!r}. Available: {known}")


if __name__ == "__main__":
    import sys

    force = "--force" in sys.argv
    found = library(force=force)
    if not found:
        print(f"No audio files in {SOURCE_DIR}")
        print("Drop an mp3 or wav in there and run this again.")
    else:
        print(f"{len(found)} sound(s), converted and ready:\n")
        for index, sound in enumerate(found.values(), 1):
            print(f"  {index}. {sound.name:<24} {sound.seconds:6.1f}s  "
                  f"({sound.source.name})")
