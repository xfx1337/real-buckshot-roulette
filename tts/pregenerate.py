"""Turning a recording of a person into the speaker sample a clone is built on.

This does not run at the table. It runs on the machine with the GPU, where
tts/remote.py calls it as the first step of adding a voice: a recording arrives,
and what has to come out is one clean wav holding nothing but that person
talking. Everything after it — the synthesis of a phrase or of the whole
vocabulary — reads that file and nothing else, so this step decides whether the
result sounds like anybody.

    prepare   A song is a voice with a band playing over it, and the model
              clones what it hears, band included. So Demucs separates the
              vocal first, then the stretches with the most actual speech in
              them are taken as the sample. Plain speech skips the separation
              and only gets cleaned and levelled.

Usable by hand as well as through the panel:

    python -m tts.pregenerate prepare ~/sample.mp3 --name kolya --song
    python -m tts.pregenerate check --name kolya

How long a sample to cut is not asked for here any more. tts/remote.py reads
the recording's own duration and decides, because a number typed into a panel
is wrong in both directions — a twelve-second clip cut to "thirty" silently
yields twelve, and a nine-minute interview still gives up one half-minute.

Why a fixed corpus rather than synthesising on demand: see tts/engine.py. The
short version is that a player is standing at the handset with the receiver
already at their ear, and a cloned voice takes seconds per phrase.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

from tts import corpus, engine

# How long one window of the sample is.
#
# Thirty because that is XTTS's own ceiling, and it is a hard one: xtts.py
# does `audio[:, : load_sr * max_ref_length]` before computing anything, so a
# longer file is silently truncated — from the *start*, discarding whatever
# the window search picked. Raising this past 30 does not give the model more
# to work with, it only makes the truncation less predictable.
SAMPLE_SECONDS = 30

# How many such windows are taken from the recording.
#
# This is the answer to "use as much of the recording as possible", and the
# reason it is a count of windows rather than a longer window. XTTS takes a
# *list* of references: it embeds each one separately, averages the speaker
# embeddings, and concatenates the audio for the GPT latents. So four windows
# from across an interview give it four different moments of the person —
# raised voice, quiet aside, different sentences — which is genuinely more
# information than any single stretch of the same total length, because a
# single stretch is one mood recorded once.
#
# Four rather than more: past this the embedding average stops changing much,
# and every extra window is another cut that has to actually contain clean
# speech. Recordings shorter than the windows need simply yield fewer.
SAMPLE_WINDOWS = 4

# What XTTS wants its speaker sample as. Not the telephone's 8 kHz — that is
# the output rate, and conditioning on band-limited audio makes every generated
# phrase sound like it went down a line twice.
SAMPLE_RATE = 22050

# Which Demucs stem holds the singing.
VOCAL_STEM = "vocals"


def _run(command: list[str], what: str, timeout: int = 3600) -> str:
    """Run a tool and return its stdout, surviving whatever it prints.

    Decoded with errors="replace" rather than with text=True, because ffmpeg
    and ffprobe echo the source file's metadata — an mp3's title and artist
    tags — and those are frequently not UTF-8. A Russian tag written in
    cp1251 makes text=True raise UnicodeDecodeError while the tool itself
    succeeded, and the upload fails with "'utf-8' codec can't decode byte
    0xd1" naming a byte the operator has no way to connect to a song title.

    What is in those tags does not matter here; only the exit code and, on
    failure, a readable tail of the error do. So undecodable bytes become
    replacement characters and the work continues.
    """
    result = subprocess.run(command, capture_output=True, timeout=timeout)
    stdout = result.stdout.decode("utf-8", errors="replace")
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", errors="replace")
        tail = "\n".join(stderr.strip().splitlines()[-5:])
        raise SystemExit(f"{what} не удалось:\n{tail}")
    return stdout


# ── step one: a recording becomes a speaker sample ──────────────────────

def demucs_binary() -> str | None:
    """Where demucs is, if this machine has it.

    Looked for beside the running interpreter before anything else: the farm
    runs as `venv/bin/python -m tts.farm` without the venv activated, so PATH
    is the login shell's and does not contain venv/bin at all. Without this,
    a demucs installed exactly where it should be reports as missing.
    """
    local = Path(sys.executable).with_name("demucs")
    if local.is_file():
        return str(local)
    return shutil.which("demucs")


def _separate_vocal(source: Path, workdir: Path) -> Path:
    """Pull the singing out of a song, leaving the band behind."""
    binary = demucs_binary()
    if not binary:
        raise SystemExit(
            "demucs не установлен, а --song требует отделения вокала.\n"
            "  pip install demucs\n"
            "Либо уберите --song, если в записи только голос.")
    print("отделяю вокал (demucs, это небыстро)...", flush=True)
    _run([binary, "--two-stems", VOCAL_STEM, "-o", str(workdir), str(source)],
         "demucs")
    found = list(workdir.rglob(f"{VOCAL_STEM}.wav"))
    if not found:
        raise SystemExit(f"demucs отработал, но {VOCAL_STEM}.wav не найден")
    return found[0]


def _window_stats(source: Path, start: float, seconds: int) -> tuple[float, float]:
    """How loud one candidate window is, and how much of it is not noise.

    Two numbers, because loudness alone picks the wrong window. A stretch of
    traffic rumble under a fan measures loud and holds no voice at all; the
    thing that separates speech from a steady noise floor is the *gap* between
    the loud parts and the quiet parts. Speech is bursty — words, then pauses —
    so a window full of it has a wide peak-to-floor spread. A window full of
    machinery is flat.

    Returns (rms, spread) in dB. astats reports both the overall RMS and the
    minimum RMS across its analysis windows, and the difference between them
    is the measure that matters.
    """
    # Without -hide_banner and with a raised log level there is no astats
    # output at all: the filter reports through the info log, and -v error
    # silences exactly the lines being parsed.
    # Decoded by hand, for the reason _run explains: the file's own metadata
    # comes back in this stream and need not be UTF-8.
    result = subprocess.run(
        ["ffmpeg", "-hide_banner", "-ss", str(start), "-t", str(seconds),
         "-i", str(source), "-af", "astats=metadata=1:reset=0",
         "-f", "null", "-"],
        capture_output=True, timeout=300,
    )
    overall, floor = -99.0, -99.0
    for line in result.stderr.decode("utf-8", errors="replace").splitlines():
        # astats prints its summary once per channel and once overall, so the
        # first of each is taken and the repeats ignored.
        if "RMS level dB" in line and overall <= -99.0:
            try:
                overall = float(line.split(":")[-1].strip())
            except ValueError:
                pass
        elif "Noise floor dB" in line and floor <= -99.0:
            try:
                floor = float(line.split(":")[-1].strip())
            except ValueError:
                pass
    if floor <= -99.0 or overall <= -99.0:
        return overall, 0.0
    return overall, overall - floor


def _best_windows(source: Path, seconds: int, count: int) -> list[float]:
    """Where to cut speaker samples from, best first, without overlaps.

    More than one, because a single window is a single moment of someone's
    voice. XTTS accepts a list of references, averages their speaker
    embeddings and concatenates them for the GPT latents, so several windows
    from across a recording give it the person's range — a raised voice, a
    quiet aside, the way they start a sentence — instead of one half-minute of
    whatever they happened to be doing.

    Ranked by how much voice is in them rather than by how loud they are: see
    _window_stats. Overlapping candidates are dropped, since two windows
    sharing twenty seconds of the same audio contribute one window's worth of
    information and crowd out somewhere else in the recording.
    """
    duration = _duration(source)
    if duration <= seconds:
        return [0.0]

    step = max(5.0, seconds / 3)
    scored: list[tuple[float, float, float]] = []
    start = 0.0
    while start + seconds <= duration:
        rms, spread = _window_stats(source, start, seconds)
        # Loudness still matters — a window has to have voice in it at all —
        # but the spread is what says the loudness is speech. Weighted so a
        # flat, loud stretch of noise loses to a quieter one with real speech.
        scored.append((rms + 2.0 * spread, start, rms))
        start += step

    scored.sort(reverse=True)
    chosen: list[float] = []
    for _, begin, rms in scored:
        if len(chosen) >= count:
            break
        if any(abs(begin - taken) < seconds for taken in chosen):
            continue
        chosen.append(begin)
        print(f"кусок {len(chosen)}: {begin:.0f}–{begin + seconds:.0f} с "
              f"({rms:.1f} dB)", flush=True)
    return sorted(chosen) or [0.0]


def _duration(source: Path) -> float:
    out = _run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                "-of", "csv=p=0", str(source)], "ffprobe", timeout=120)
    try:
        return float(out.strip())
    except ValueError:
        return 0.0


def _clean_chain(song: bool) -> str:
    """What the sample is cleaned of, and why each filter is here.

    Everything the recording contains that is not the person's voice, XTTS
    treats as part of their voice. A fan, a fridge, traffic through a window,
    the room's own reverberation — all of it gets folded into the speaker
    embedding and comes back as a permanent layer under every generated
    phrase. It cannot be removed afterwards, because by then it is not noise
    on top of a voice, it *is* the voice as the model understands it.

    So the cleaning is done here, once, and it is done properly:

    highpass    below 80 Hz there is nothing of a human voice. There is a
                great deal of everything else — traffic rumble, fan bearings,
                mains hum, the thump of a table being knocked.
    lowpass     above 11 kHz a speech recording holds mostly hiss. Kept high
                enough to leave sibilance and breath, which is what makes a
                clone recognisable as a particular person.
    afftdn      spectral denoise, tracking the noise profile as it goes
                (nt=w, noise tracking on white-ish noise). This is what takes
                out steady machinery: it learns the floor between words and
                subtracts it from the words too.
    anlmdn      non-local means, a second pass with a different principle —
                it finds repeated patterns and averages them, which catches
                the periodic noise afftdn leaves (hum harmonics, the regular
                whirr of a fan) without touching speech, since speech does
                not repeat that way.
    agate       everything under the threshold is not quiet, it is silent.
                Between words the noise floor is what remains after the two
                denoisers, and leaving it in tells the model the person is
                accompanied by a faint hiss whenever they stop talking.
    loudnorm    a quiet sample gives a vague speaker embedding, so it is
                brought to a consistent level last, after cleaning.

    A song gets the same treatment plus a stricter low end: Demucs leaves bass
    bleeding into the vocal stem, because a kick drum is broadband and does
    not separate cleanly, and the separation artefacts above 11 kHz are
    cymbal residue the model could not place.

    The denoisers are deliberately moderate. Pushed harder they start eating
    breath and the tails of words, and a clone built from over-processed audio
    sounds synthetic in a way no amount of sampling tuning repairs — the usual
    trade of noise for artefacts, where the artefacts are worse.
    """
    low = 90 if song else 80
    return (
        f"highpass=f={low},"
        "lowpass=f=11000,"
        "afftdn=nf=-28:nt=w,"
        "anlmdn=s=0.0004:p=0.008:r=0.006,"
        "agate=threshold=0.008:ratio=2:attack=10:release=250,"
        "loudnorm=I=-16:TP=-1.5:LRA=9"
    )


def _join(pieces: list[Path], target: Path) -> None:
    """Put the chosen windows together into one speaker sample.

    Concatenated rather than handed to XTTS as separate files, even though it
    accepts a list. Two reasons, and both are about everything else that
    touches this file: the panel plays the sample back so an operator can hear
    what was cut, and F5-TTS takes exactly one reference. One file keeps every
    consumer working and loses nothing — XTTS concatenates its references
    internally anyway.

    A short silence between pieces, because two unrelated stretches of speech
    butted together make a click and an impossible transition, and the model
    hears both as things this person's voice does.
    """
    if len(pieces) == 1:
        shutil.move(str(pieces[0]), str(target))
        return

    listing = pieces[0].parent / "pieces.txt"
    gap = pieces[0].parent / "gap.wav"
    _run(["ffmpeg", "-y", "-hide_banner", "-f", "lavfi",
          "-i", f"anullsrc=r={SAMPLE_RATE}:cl=mono", "-t", "0.25", str(gap)],
         "ffmpeg (пауза между кусками)")

    lines = []
    for index, piece in enumerate(pieces):
        if index:
            lines.append(f"file '{gap.name}'")
        lines.append(f"file '{piece.name}'")
    listing.write_text("\n".join(lines))

    _run(["ffmpeg", "-y", "-hide_banner", "-f", "concat", "-safe", "0",
          "-i", str(listing), "-c", "copy", str(target)],
         "ffmpeg (склейка образца)")


def prepare(source: Path, name: str, *, song: bool,
            seconds: int = SAMPLE_SECONDS,
            windows: int = SAMPLE_WINDOWS) -> Path:
    """Turn a recording into the speaker sample XTTS conditions on."""
    if not source.is_file():
        raise SystemExit(f"нет такого файла: {source}")

    engine.VOICES_DIR.mkdir(parents=True, exist_ok=True)
    workdir = engine.VOICES_DIR / f".work_{name}"
    if workdir.exists():
        shutil.rmtree(workdir)
    workdir.mkdir(parents=True)

    try:
        voice_source = _separate_vocal(source, workdir) if song else source

        target = engine.VOICES_DIR / f"{name}.wav"
        starts = _best_windows(voice_source, seconds, windows)
        pieces = []
        for index, start in enumerate(starts, 1):
            piece = workdir / f"piece_{index}.wav"
            _run(["ffmpeg", "-y", "-hide_banner", "-ss", str(start),
                  "-t", str(seconds), "-i", str(voice_source),
                  "-af", _clean_chain(song),
                  "-ac", "1", "-ar", str(SAMPLE_RATE), str(piece)],
                 "ffmpeg (нарезка образца)")
            pieces.append(piece)

        _join(pieces, target)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    print(f"\nобразец готов: {target}")
    print(f"послушайте его — именно так будет звучать информатор.")
    print(f"дальше: озвучить фразу или весь словарь через панель дилера, "
          f"либо `python -m tts.remote speak` на этой машине.")
    return target


# ── what the far side needs to know about this machine ───────────────

def _pick_device(requested: str) -> str:
    if requested != "auto":
        return requested
    # Before torch, for the reason enable_cuda_libraries explains: the CUDA
    # libraries have to be in the process before anything dlopens against them.
    engine.enable_cuda_libraries()
    try:
        import torch
    except ImportError:
        raise SystemExit("torch не установлен: pip install torch torchcodec")
    if torch.cuda.is_available():
        return "cuda"
    if torch.backends.mps.is_available():
        return "mps"
    return "cpu"


def _write_manifest(name: str, sample: Path, phrases: int, device: str,
                    spoken_by: str = "") -> None:
    """Leave a note in the cache directory about what built it.

    The engine is recorded because a cache directory is otherwise silent about
    how it was spoken — the filenames hash the text, not the settings — and
    tts/remote.py reads it back to notice that a voice is being regenerated by
    a different one, so the old phrases go rather than mixing with the new.
    """
    from tts import engines

    (engine.voice_dir(name) / "manifest.json").write_text(json.dumps({
        "voice": name,
        "model": engine.MODEL,
        "engine": spoken_by or engines.DEFAULT,
        "source": sample.name,
        "phrases": phrases,
        "max_shells": corpus.MAX_SHELLS,
        "sample_rate": engine.SAMPLE_RATE,
        "device": device,
        "generated": time.strftime("%Y-%m-%d %H:%M:%S"),
    }, ensure_ascii=False, indent=2))


# ── command line ────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="python -m tts.pregenerate",
        description="Запись человека → образец, на котором строится клон.")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("prepare", help="запись → образец голоса")
    p.add_argument("source", type=Path, help="mp3/wav/m4a с голосом человека")
    p.add_argument("--name", required=True, help="как назвать голос")
    p.add_argument("--song", action="store_true",
                   help="это песня: отделить вокал от музыки")
    p.add_argument("--seconds", type=int, default=SAMPLE_SECONDS,
                   help=f"длина одного окна (по умолчанию {SAMPLE_SECONDS}); "
                        f"через панель считается по длине записи")

    c = sub.add_parser("check", help="что установлено и чего не хватает")
    c.add_argument("--name", default=None)

    args = parser.parse_args(argv)

    if args.command == "prepare":
        prepare(args.source, args.name, song=args.song, seconds=args.seconds)
    else:
        state = engine.available()
        print(json.dumps(state, ensure_ascii=False, indent=2))
        if args.name or state["voices"]:
            absent = engine.missing(args.name)
            print(f"\nне хватает фраз: {len(absent)}")
            for text in absent[:5]:
                print(f"  {text}")


if __name__ == "__main__":
    main(sys.argv[1:])
