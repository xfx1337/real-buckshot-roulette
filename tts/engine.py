"""Turning a line of Russian text into a file the handset can play.

The telephone path is a cable, not a codec: audio reaches the earpiece through
the Mac's headphone jack, played by afplay (voip/scripts/audio.py). So what a
voice has to produce here is simply a file on disk that afplay can open, named
in a way the rest of the telephony code already understands.

The voice is a clone. Someone's speech — a recording, a song — is turned into
an XTTS speaker sample once, and from then on the informant on the telephone
is that person. That is the whole point of the feature, and it is also what
makes this file look the way it does, because a cloned voice is expensive:
seconds per phrase even on a good GPU, tens of seconds on the machine under
the table.

So nothing is synthesised during a game. The vocabulary is finite (tts/corpus.py
enumerates all thousand-odd lines of it), it is generated ahead of time on a
machine with a GPU (tts/pregenerate.py), and the result is copied to the table
as a directory of wav files. At the table this module is a lookup: hash the
text, open the file. A game night never loads a model, never imports torch,
and never makes a player wait.

Which is also the failure mode to understand. A phrase that was not
pre-generated has no file, and speak() raises rather than stalling the request
that is holding a player at the telephone. The caller decides what happens
next; tts_bridge turns it into an audible fault rather than silence, because
silence down a telephone is indistinguishable from a dead line.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent

# Generated audio lives here, one directory per cloned voice. Kept inside the
# module rather than in voip/sounds/ because voip/sounds/ is the operator's own
# library — files someone dropped in by hand and picks from a list — and a
# thousand machine-generated phrases would bury it.
CACHE_DIR = ROOT / "cache"

# The speaker samples themselves: the cleaned few seconds of someone's voice
# that XTTS conditions on. Only needed on the machine that generates; the table
# has the wavs and does not need what produced them.
VOICES_DIR = ROOT / "voices"

# Which cloned voice speaks. A directory name under cache/. Overridable by
# environment so a second voice can be tried without editing anything, and
# settable at runtime by the dealer's panel through use_voice() below.
DEFAULT_VOICE = os.environ.get("TTS_VOICE", "default")

# The XTTS model every voice is generated with. Recorded in each cache
# directory's manifest and checked on load: a cache built with a different
# model is still playable, but the operator should know why it sounds unlike
# the one they auditioned.
MODEL = "tts_models/multilingual/multi-dataset/xtts_v2"

# What a phrase is stored at: the rate XTTS generates, kept whole.
#
# The earpiece only carries about 4 kHz, so storing at 8 kHz was tempting and
# wrong. Two reasons. The obvious one: every phrase is also auditioned in the
# dealer's panel, out of laptop speakers, where band-limited audio does not
# sound like a telephone — it sounds like a bad clone, and the operator judges
# the voice by what they hear there. The subtler one: resampling to 8 kHz
# throws away the sibilance and breath that make a cloned voice recognisable
# as a particular person, and once thrown away it cannot come back, even for
# a listener whose earpiece could have carried some of it.
#
# Storing wide costs about three times the disk for a voice — a few hundred
# megabytes — which is nothing next to the model that produced it.
SAMPLE_RATE = 24000

# What the handset gets, when the handset is the destination. The telephone
# path resamples on the way out (voip/scripts/audio.py plays through afplay,
# which handles it), so this is documentation of the constraint rather than
# something imposed on the stored file.
PHONE_SAMPLE_RATE = 8000

# Phone earpieces are quiet and a synthesised voice comes out quieter than a
# recording. Same normalisation voip/scripts/sounds.py applies to the
# operator's files, so a generated phrase and a dropped-in mp3 sit at the same
# level in the same earpiece.
LOUDNORM = "loudnorm=I=-16:TP=-1.5:LRA=11"

_lock = threading.Lock()
_voice = DEFAULT_VOICE


class TTSError(RuntimeError):
    """Speech could not be produced. The caller decides what the player hears."""


@dataclass
class Speech:
    """One spoken phrase, on disk and ready to play."""

    text: str
    path: Path
    voice: str
    cached: bool         # always True at the table; False only while generating

    @property
    def name(self) -> str:
        """What to call this in a log line or a call record."""
        return self.path.stem

    @property
    def engine(self) -> str:
        """Which voice spoke, under the name the rest of the code already uses."""
        return f"xtts:{self.voice}"


def key(text: str) -> str:
    """A filename for one phrase.

    The hash covers only the text: the voice is the directory the file sits in,
    so the same line under two voices is the same name in two places. That is
    what makes a cache directory portable — generated on a GPU machine under
    whatever path, dropped in here, and every lookup still finds it.
    """
    digest = hashlib.sha1(text.encode()).hexdigest()[:16]
    return f"tts_{digest}"


def normalise(text: str) -> str:
    """The exact form a phrase is hashed in.

    Generation and lookup have to agree on this down to the whitespace, and
    they are in different processes on different machines, so it lives in one
    function that both import rather than in two places that drift.
    """
    return " ".join(text.split())


# ── voices ──────────────────────────────────────────────────────────────

def voice_dir(voice: str | None = None) -> Path:
    return CACHE_DIR / (voice or _voice)


def manifest(voice: str | None = None) -> dict:
    """What a cache directory says about itself.

    Written by pregenerate.py: which model and speaker sample built it, when,
    and how many phrases it holds. Missing or unreadable is not an error — a
    directory of wavs still plays — so this answers with an empty dict and the
    caller shows what it can.
    """
    path = voice_dir(voice) / "manifest.json"
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def voices() -> list[dict]:
    """Every cloned voice this machine can speak in.

    A voice is a directory under cache/ holding at least one phrase. Read by
    the dealer's panel, so it carries enough to tell a complete voice from a
    half-copied one: how many phrases are present against how many the corpus
    expects.
    """
    if not CACHE_DIR.is_dir():
        return []
    found = []
    for path in sorted(CACHE_DIR.iterdir()):
        if not path.is_dir():
            continue
        phrases_on_disk = sum(1 for _ in path.glob("tts_*.wav"))
        if not phrases_on_disk:
            continue
        info = manifest(path.name)
        found.append({
            "name": path.name,
            "phrases": phrases_on_disk,
            "expected": info.get("phrases", 0),
            "model": info.get("model", ""),
            "source": info.get("source", ""),
            "generated": info.get("generated", ""),
            "active": path.name == _voice,
        })
    return found


def use_voice(name: str) -> None:
    """Switch which voice speaks from now on.

    Takes effect for the next phrase asked for; anything already generated and
    handed to the telephony side keeps playing in the voice it was made in.
    """
    global _voice
    if not (CACHE_DIR / name).is_dir():
        raise TTSError(f"голос {name!r} не найден в {CACHE_DIR}")
    with _lock:
        _voice = name


def current_voice() -> str:
    return _voice


# ── what the table actually does ────────────────────────────────────────

def available() -> dict:
    """Whether this machine can speak, and how completely.

    Read by the health page and by the setup script, so a half-copied voice is
    something the operator finds out about before a game rather than when a
    player lifts a receiver to silence.
    """
    from tts import corpus

    all_voices = voices()
    active = next((v for v in all_voices if v["active"]), None)
    expected = corpus.count()
    return {
        "voice": _voice,
        "voices": all_voices,
        "ready": bool(active and active["phrases"] >= expected),
        "phrases": active["phrases"] if active else 0,
        "expected": expected,
        "cache": str(CACHE_DIR),
    }


def speak(text: str, voice: str | None = None) -> Speech:
    """The file for one phrase, as generated ahead of time.

    Nothing is synthesised here. A phrase with no file on disk is a phrase the
    corpus did not enumerate or a voice that was copied incompletely, and both
    are faults the operator has to hear about — so this raises rather than
    quietly falling back to a voice the game was not set up with.
    """
    text = normalise(text)
    if not text:
        raise TTSError("нечего произносить: пустой текст")

    name = voice or _voice
    target = voice_dir(name) / f"{key(text)}.wav"
    if target.is_file() and target.stat().st_size > 0:
        return Speech(text=text, path=target, voice=name, cached=True)

    if not voice_dir(name).is_dir():
        raise TTSError(
            f"голос {name!r} не установлен: нет каталога {voice_dir(name)}. "
            f"См. tts/README.md — фразы генерируются заранее на машине с GPU")
    raise TTSError(
        f"фраза не сгенерирована для голоса {name!r}: {text!r}. "
        f"Добавьте её в tts/corpus.py и прогоните tts/pregenerate.py заново")


def missing(voice: str | None = None) -> list[str]:
    """Which corpus phrases this voice has no file for.

    The check the operator runs before a game. Empty means every line the game
    can utter will play.
    """
    from tts import corpus

    directory = voice_dir(voice)
    return [line.text for line in corpus.lines()
            if not (directory / f"{key(normalise(line.text))}.wav").is_file()]


# ── generation, for the machine with the GPU ────────────────────────────

def enable_cuda_libraries() -> int:
    """Load the CUDA libraries pip installed, so torchcodec can find them.

    torchaudio reads and writes audio through torchcodec, whose shared objects
    link against CUDA's NPP libraries. Pip puts those inside site-packages
    (nvidia/*/lib), which is on no loader search path, so unless the venv was
    activated in a shell that exported LD_LIBRARY_PATH they are invisible. The
    farm runs as a systemd unit calling venv/bin/python directly, and there
    every read of a speaker sample fails.

    The failure is worth describing because it does not look like a missing
    library. torch and torchaudio import fine, CUDA reports available, the
    model loads; it surfaces only deep inside get_conditioning_latents, as
    "libnppicc.so.12: cannot open shared object file". The library was present
    the whole time, a few directories away.

    Setting LD_LIBRARY_PATH here does not fix it — the loader reads that
    variable once, when the process starts, and by the time Python can edit it
    the value has already been captured. Opening each library explicitly with
    RTLD_GLOBAL does work: the symbols land in the process's global table, and
    torchcodec's own dlopen then resolves against what is already loaded.

    Returns how many were loaded. Failures are ignored on purpose — the set
    includes libraries for hardware this machine may not have, and one that
    will not open is only fatal if something later actually needs it.

    A no-op anywhere without those packages, which includes the table.
    """
    import ctypes
    import sys

    root = Path(sys.prefix) / "lib"
    loaded = 0
    for library in sorted(root.glob("python*/site-packages/nvidia/*/lib/*.so*")):
        try:
            ctypes.CDLL(str(library), mode=ctypes.RTLD_GLOBAL)
            loaded += 1
        except OSError:
            pass
    return loaded


def write_raw(samples, path: Path, rate: int = SAMPLE_RATE) -> None:
    """Write what XTTS returned to disk, before convert() levels it.

    torchaudio.save was the obvious way to do this and is the wrong one: recent
    torchaudio routes every write through torchcodec, whose shared library
    needs libnppicc from CUDA's NPP package. On a machine without it, importing
    works and saving raises, so the failure lands in the middle of a generation
    run rather than at startup. Writing a mono PCM wav needs no codec at all,
    so the standard library does it and the generation path stops depending on
    a decoder it never needed.

    Peak-normalising here is deliberate but modest: XTTS occasionally returns
    samples slightly outside [-1, 1], and clipping those before ffmpeg sees
    them would bake in distortion that loudnorm cannot undo.
    """
    import wave

    import numpy as np

    audio = np.asarray(samples, dtype=np.float32).reshape(-1)
    peak = float(np.max(np.abs(audio))) if audio.size else 0.0
    if peak > 1.0:
        audio = audio / peak
    pcm = (np.clip(audio, -1.0, 1.0) * 32767.0).astype("<i2")

    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(pcm.tobytes())


def convert(raw: Path, target: Path) -> None:
    """Level a generated phrase and store it at full width.

    Used by pregenerate.py and farm.py. Lives here so that the form of a
    cached file is decided in one place — the module that reads them back.

    Only two things happen: a gentle high-pass, because XTTS sometimes leaves
    a slow rumble under a phrase that no voice made, and loudness normalisation
    so that a generated phrase and one of the operator's own recordings sit at
    the same level in the same earpiece. The rate is left alone; see
    SAMPLE_RATE for why narrowing here would be a one-way loss.
    """
    if not shutil.which("ffmpeg"):
        raise TTSError("ffmpeg не установлен")
    result = subprocess.run(
        ["ffmpeg", "-y", "-i", str(raw),
         "-af", f"highpass=f=55,{LOUDNORM}",
         "-ac", "1", "-ar", str(SAMPLE_RATE), str(target)],
        capture_output=True, text=True, timeout=120,
    )
    if result.returncode != 0:
        target.unlink(missing_ok=True)
        tail = "\n".join(result.stderr.strip().splitlines()[-3:])
        raise TTSError(f"ffmpeg не смог обработать синтезированный звук:\n{tail}")


def clear_cache(voice: str | None = None) -> int:
    """Delete a voice's generated phrases. Returns how many files went.

    Nothing needs this during a game. It is for after a voice is regenerated,
    where the old files would otherwise sit alongside the new ones — harmless,
    since the hash is the same, but confusing to count.
    """
    directory = voice_dir(voice)
    if not directory.is_dir():
        return 0
    gone = 0
    with _lock:
        for path in directory.glob("tts_*.wav"):
            path.unlink(missing_ok=True)
            gone += 1
    return gone


if __name__ == "__main__":
    import sys

    state = available()
    print(json.dumps(state, ensure_ascii=False, indent=2))
    if not state["voices"]:
        print("\nНи одного голоса не установлено. См. tts/README.md")
        raise SystemExit(1)

    absent = missing()
    if absent:
        print(f"\nНе хватает {len(absent)} фраз из {state['expected']}. "
              f"Первая: {absent[0]!r}")
        raise SystemExit(1)
    print(f"\nГолос {state['voice']!r}: все {state['expected']} фраз на месте.")

    if len(sys.argv) > 1:
        result = speak(" ".join(sys.argv[1:]))
        print(f"{result.engine}: {result.path}")
