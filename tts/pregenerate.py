"""Generate a cloned voice's whole vocabulary, on a machine with a GPU.

This does not run at the table. It runs wherever there is an NVIDIA card,
takes a recording of someone talking — or singing — and produces the directory
of wav files that tts/engine.py reads back all evening. Copy that directory to
the table and the informant on the telephone is that person.

Two steps, and the first is the one that decides whether the result sounds
like anybody:

    prepare   A song is a voice with a band playing over it, and XTTS clones
              what it hears, band included. So Demucs separates the vocal
              first, then the loudest continuous stretch of it is taken as the
              speaker sample. Plain speech skips the separation and only gets
              trimmed and levelled.

    generate  Every line tts/corpus.py enumerates, synthesised once and
              converted to the 8 kHz mono the earpiece carries. Resumable:
              phrases already on disk are skipped, so an interrupted run
              continues where it stopped.

Usage, on the GPU machine:

    pip install coqui-tts "transformers>=4.57,<5" torch torchaudio torchcodec demucs
    python -m tts.pregenerate prepare  ~/sample.mp3 --name kolya --song
    python -m tts.pregenerate generate --name kolya

The transformers pin is load-bearing: its fifth major version removed a helper
XTTS imports, and without the ceiling nothing runs at all.

Then copy tts/cache/kolya/ to the same path at the table.

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

# How much voice XTTS conditions on. More is not better past this: the model
# takes a speaker embedding from a few seconds, and a longer sample only makes
# it likelier to include a stretch where the person is not talking.
SAMPLE_SECONDS = 30

# What XTTS wants its speaker sample as. Not the telephone's 8 kHz — that is
# the output rate, and conditioning on band-limited audio makes every generated
# phrase sound like it went down a line twice.
SAMPLE_RATE = 22050

# Which Demucs stem holds the singing.
VOCAL_STEM = "vocals"


def _run(command: list[str], what: str, timeout: int = 3600) -> str:
    result = subprocess.run(command, capture_output=True, text=True,
                            timeout=timeout)
    if result.returncode != 0:
        tail = "\n".join(result.stderr.strip().splitlines()[-5:])
        raise SystemExit(f"{what} не удалось:\n{tail}")
    return result.stdout


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


def _window_rms(source: Path, start: float, seconds: int) -> float:
    """How loud one candidate window is, in dBFS.

    The measure that matters for a speaker sample is simply how much voice is
    in it. A window that lands on a quiet passage, a fade, or the gap between
    verses measures low and is a poor thing to clone from — XTTS conditions on
    what it is given, and near-silence gives it almost nothing to hold on to.
    """
    result = subprocess.run(
        ["ffmpeg", "-ss", str(start), "-t", str(seconds), "-i", str(source),
         "-af", "astats=metadata=1:reset=0", "-f", "null", "-"],
        capture_output=True, text=True, timeout=300,
    )
    for line in result.stderr.splitlines():
        if "RMS level dB" in line:
            try:
                return float(line.split(":")[-1].strip())
            except ValueError:
                break
    return -99.0


def _loudest_window(source: Path, seconds: int) -> float:
    """Where in the file to cut the speaker sample from.

    Every candidate start is measured and the fullest one wins, rather than
    taking the first stretch that is merely not silent. That earlier rule
    picked whatever came after the opening pause, which in a song is the
    quietest verse as often as not, and the clone was built from it.

    Coarse on purpose: a window is thirty seconds and the step is five, so a
    handful of probes covers a three-minute recording. Finer would measure
    differences no listener could hear in the result.
    """
    duration = _duration(source)
    if duration <= seconds:
        return 0.0

    step = max(5.0, seconds / 6)
    best_start, best_rms = 0.0, -99.0
    start = 0.0
    while start + seconds <= duration:
        rms = _window_rms(source, start, seconds)
        if rms > best_rms:
            best_start, best_rms = start, rms
        start += step

    print(f"лучший кусок: {best_start:.0f}–{best_start + seconds:.0f} с "
          f"({best_rms:.1f} dB)", flush=True)
    return best_start


def _duration(source: Path) -> float:
    out = _run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                "-of", "csv=p=0", str(source)], "ffprobe", timeout=120)
    try:
        return float(out.strip())
    except ValueError:
        return 0.0


def prepare(source: Path, name: str, *, song: bool,
            seconds: int = SAMPLE_SECONDS) -> Path:
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

        start = _loudest_window(voice_source, seconds)
        target = engine.VOICES_DIR / f"{name}.wav"
        print(f"вырезаю {seconds} с начиная с {start:.1f} с...", flush=True)

        # What the sample is cleaned of, and why each one is here.
        #
        # highpass  Demucs leaves bass bleeding into the vocal stem — a kick
        #           drum is broadband and does not separate cleanly. Below
        #           70 Hz there is nothing of a human voice anyway.
        # lowpass   above 11 kHz the stem is mostly separation artefacts:
        #           cymbal residue and the smeared hiss the model could not
        #           place. XTTS treats that as part of the timbre and
        #           reproduces it as a permanent sheen over every phrase.
        # afftdn    broadband denoise, gentle. Enough to take the hiss out
        #           from between words without gating the words themselves.
        # loudnorm  a quiet sample gives a vague speaker embedding, so it is
        #           brought up to a consistent level last, after cleaning.
        #
        # Speech only gets the rumble filter and the levelling: a clean
        # recording has none of these problems, and denoising it would only
        # remove the breath that makes the clone sound alive.
        chain = ("highpass=f=70,lowpass=f=11000,afftdn=nf=-25,"
                 "loudnorm=I=-16:TP=-1.5:LRA=9") if song else \
                "highpass=f=60,loudnorm=I=-16:TP=-1.5:LRA=9"

        _run(["ffmpeg", "-y", "-ss", str(start), "-t", str(seconds),
              "-i", str(voice_source), "-af", chain,
              "-ac", "1", "-ar", str(SAMPLE_RATE), str(target)],
             "ffmpeg (нарезка образца)")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    print(f"\nобразец готов: {target}")
    print(f"послушайте его — именно так будет звучать информатор.")
    print(f"дальше: python -m tts.pregenerate generate --name {name}")
    return target


# ── step two: the whole vocabulary ──────────────────────────────────────

def _load_model(device: str):
    """Bring XTTS up. Imported here so `prepare` needs no torch at all."""
    import os

    os.environ.setdefault("COQUI_TOS_AGREED", "1")
    engine.enable_cuda_libraries()
    print(f"загружаю модель на {device} (первый раз качает ~2 ГБ)...", flush=True)
    from TTS.api import TTS

    return TTS(engine.MODEL).to(device)


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


def generate(name: str, *, device: str = "auto", max_shells: int | None = None,
             force: bool = False) -> None:
    """Synthesise every corpus phrase in one voice.

    Resumable by design: a phrase whose file already exists is skipped, so a
    run killed at 600 of 1002 picks up at 600 rather than starting over.
    """
    sample = engine.VOICES_DIR / f"{name}.wav"
    if not sample.is_file():
        raise SystemExit(
            f"нет образца голоса: {sample}\n"
            f"сначала: python -m tts.pregenerate prepare <файл> --name {name}")

    out = engine.voice_dir(name)
    out.mkdir(parents=True, exist_ok=True)
    if force:
        gone = engine.clear_cache(name)
        print(f"удалено старых фраз: {gone}")

    lines = corpus.lines(max_shells) if max_shells else corpus.lines()
    todo = [line for line in lines
            if not (out / f"{engine.key(engine.normalise(line.text))}.wav").is_file()]
    print(f"фраз всего: {len(lines)}, надо сгенерировать: {len(todo)}")
    if not todo:
        print("всё уже на месте.")
        _write_manifest(name, sample, len(lines), device="—")
        return

    device = _pick_device(device)
    model = _load_model(device)

    # Same conditioning the farm uses, for the same reason: XTTS defaults to
    # six seconds of reference and the sample is thirty. See tts/farm.py.
    from tts import farm

    xtts = model.synthesizer.tts_model
    print(f"считаю латенты по {farm.COND_SECONDS} с образца...", flush=True)
    gpt_cond_latent, speaker_embedding = xtts.get_conditioning_latents(
        audio_path=[str(sample)],
        gpt_cond_len=farm.COND_SECONDS,
        gpt_cond_chunk_len=farm.COND_SECONDS,
        max_ref_length=farm.COND_SECONDS,
        sound_norm_refs=True,
    )

    started = time.time()
    raw = out / ".raw.wav"
    try:
        for index, line in enumerate(todo, 1):
            text = engine.normalise(line.text)
            target = out / f"{engine.key(text)}.wav"
            result = xtts.inference(
                text=text, language="ru",
                gpt_cond_latent=gpt_cond_latent,
                speaker_embedding=speaker_embedding,
                **farm.SAMPLING,
            )
            engine.write_raw(result["wav"], raw)
            engine.convert(raw, target)

            done = time.time() - started
            rate = done / index
            left = rate * (len(todo) - index)
            print(f"  [{index}/{len(todo)}] {line.kind} {line.detail} "
                  f"— осталось ~{left / 60:.0f} мин", flush=True)
    finally:
        raw.unlink(missing_ok=True)

    _write_manifest(name, sample, len(lines), device=device)
    absent = engine.missing(name)
    if absent:
        print(f"\nвнимание: {len(absent)} фраз всё ещё нет. "
              f"Запустите ещё раз — генерация продолжится с этого места.")
    else:
        print(f"\nготово: голос {name!r}, все {len(lines)} фраз.")
        print(f"скопируйте {out} на машину за столом, в тот же путь.")


def _write_manifest(name: str, sample: Path, phrases: int, device: str) -> None:
    """Leave a note in the cache directory about what built it."""
    (engine.voice_dir(name) / "manifest.json").write_text(json.dumps({
        "voice": name,
        "model": engine.MODEL,
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
        description="Клонировать голос и сгенерировать всю озвучку заранее.")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("prepare", help="запись → образец голоса")
    p.add_argument("source", type=Path, help="mp3/wav/m4a с голосом человека")
    p.add_argument("--name", required=True, help="как назвать голос")
    p.add_argument("--song", action="store_true",
                   help="это песня: отделить вокал от музыки")
    p.add_argument("--seconds", type=int, default=SAMPLE_SECONDS,
                   help=f"длина образца (по умолчанию {SAMPLE_SECONDS})")

    g = sub.add_parser("generate", help="образец → вся озвучка")
    g.add_argument("--name", required=True)
    g.add_argument("--device", default="auto", choices=["auto", "cuda", "mps", "cpu"])
    g.add_argument("--max-shells", type=int, default=None,
                   help=f"потолок магазина (по умолчанию {corpus.MAX_SHELLS})")
    g.add_argument("--force", action="store_true",
                   help="перегенерировать всё заново")

    c = sub.add_parser("check", help="что установлено и чего не хватает")
    c.add_argument("--name", default=None)

    args = parser.parse_args(argv)

    if args.command == "prepare":
        prepare(args.source, args.name, song=args.song, seconds=args.seconds)
    elif args.command == "generate":
        generate(args.name, device=args.device, max_shells=args.max_shells,
                 force=args.force)
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
