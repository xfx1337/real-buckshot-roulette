"""The farm's face, driven over ssh instead of over a listening socket.

The GPU machine used to run an HTTP server and the table reached it through an
SSH tunnel held open by a watchdog. The tunnel was the weak part: ssh survives
a broken network while still holding the local port, so requests hung until
they timed out and the panel looked broken rather than disconnected. Restarting
it was a permanent chore.

There is nothing the tunnel bought that a plain `ssh gpufarm ...` does not.
Every operation here is short-lived and self-contained: it runs, it prints one
JSON object, it exits. A connection that dies takes one command with it and the
next one opens its own. Nothing to keep alive, nothing to supervise, nothing
listening on the GPU machine between jobs.

Each subcommand reads its arguments as one JSON object on stdin and writes one
JSON object to stdout. That keeps Russian text, long phrases and file paths out
of argv quoting entirely, which matters because the string travels through a
local shell, ssh, and a remote shell before it arrives.

    echo '{"name":"kate","text":"..."}' | python -m tts.remote speak

Progress that outlives a command goes to disk through tts/jobs.py, exactly as
before — the caller polls `status` rather than holding a connection open, so a
generation running for an hour does not depend on any link staying up.

Only F5-TTS with stress marks synthesises here. The engine comparison this
project once carried is over; see tts/engines.py.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import threading
import zipfile
from pathlib import Path

from tts import corpus, engine, engines, jobs, pregenerate

# The one way anything is spoken now. Named rather than passed in: with a
# single engine, letting the caller choose one is a way for the table and the
# farm to disagree about what a voice was built with.
ENGINE = "f5_accent"

# Prefixes the one line of stdout that is an answer rather than noise.
#
# The marker matters. Everything on the far side prints — ffmpeg, torch's
# import banner, huggingface's download bars, a login shell's own warnings —
# and all of it lands on the same stream the caller parses. Rather than fight
# that, the answer is fenced: the caller looks for this line and ignores the
# rest, which also means a crash inside a library still leaves readable output
# to diagnose from.
#
# Plain printable characters only. The first version of this began with \x1e,
# which reads as a tidy record separator and is one: str.splitlines() treats it
# as a line break, so the caller split the marker away from its own JSON and
# never recognised an answer that had arrived intact.
REPLY_MARKER = "__TTS_REPLY__ "


# ── how long a speaker sample should be ─────────────────────────────────

# The most conditioning audio worth cutting. F5 takes one reference and
# transcribes it to steer the clone; past roughly half a minute the extra adds
# transcription errors rather than voice.
MAX_SAMPLE_SECONDS = 30

# The least that is still a voice rather than a syllable. Below this the clone
# has nothing to hold on to and the result is closer to the model's own default
# speaker than to the person in the recording.
MIN_SAMPLE_SECONDS = 6


def sample_seconds(source: Path) -> int:
    """How long a window to cut from this recording, read from the recording.

    The old panel asked the operator for a number and defaulted to thirty. That
    is wrong in both directions: a twelve-second clip cut to "thirty seconds"
    silently yields twelve, and nothing says so, while a nine-minute interview
    still contributes one half-minute window because that is what the field
    said. What the window should be is a property of the file, so it is taken
    from the file.

    Shorter than the cap: use all of it, there is nothing to choose between.
    Longer: take the cap's worth from the best-sounding stretch, which is what
    pregenerate._best_windows is for.
    """
    duration = pregenerate._duration(source)
    if duration <= 0:
        # ffprobe could not say. The cap is the safe guess: too long a request
        # is truncated to whatever exists, too short throws audio away.
        return MAX_SAMPLE_SECONDS
    return max(MIN_SAMPLE_SECONDS, min(int(duration), MAX_SAMPLE_SECONDS))


# ── synthesis ───────────────────────────────────────────────────────────

def synthesise(name: str, text: str, target: Path) -> None:
    """One phrase, in one voice, ready to play.

    F5 runs in its own virtualenv as a subprocess — it wants transformers>=5
    and cannot share a process with anything pinned below that. convert() then
    levels the result and puts it at the rate the game stores phrases at, so
    what comes out of here is the same kind of file whatever produced it.
    """
    from tts import backends

    raw = target.with_suffix(".raw.wav")
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        backends.synthesise("f5", text=engine.normalise(text),
                            sample=engine.VOICES_DIR / f"{name}.wav",
                            target=raw, accent=True)
        engine.convert(raw, target)
    finally:
        raw.unlink(missing_ok=True)


# ── the commands ────────────────────────────────────────────────────────

def cmd_prepare(request: dict) -> dict:
    """A recording has arrived; cut a speaker sample from it.

    Synchronous, unlike the old farm's threaded version. Cutting a sample is
    ffmpeg work measured in seconds, and the caller is one ssh command that can
    simply wait for it — the asynchrony existed to keep an HTTP request short,
    and there is no HTTP request any more.
    """
    name = jobs.valid_name(request["name"])
    # expanduser because the caller writes the inbox path with a `~` and sends
    # it inside JSON, where no shell is involved to expand it.
    source = Path(request["source"]).expanduser()
    if not source.is_file():
        raise SystemExit(f"нет такого файла: {source}")

    song = bool(request.get("song"))
    seconds = sample_seconds(source)

    jobs.save(jobs.Job(name=name, stage="preparing", source=source.name,
                       song=song, seconds=seconds))
    try:
        pregenerate.prepare(source, name, song=song, seconds=seconds)
    except BaseException as exc:                                # noqa: BLE001
        # SystemExit included: pregenerate raises it for tool failures, and
        # letting it through would exit with an empty stdout and leave the
        # caller guessing.
        jobs.fail(name, exc)
        raise SystemExit(str(exc))

    sample = engine.VOICES_DIR / f"{name}.wav"
    jobs.update(name, stage="sample")
    return {"ok": True, "name": name, "sample": str(sample),
            "seconds": seconds,
            "duration": round(pregenerate._duration(source), 1)}


def cmd_speak(request: dict) -> dict:
    """Say one line in a prepared voice and leave the wav where it can be taken.

    The single-phrase mode: a recording and a sentence in, one file out. It
    does not touch the corpus, the job stages, or anything else — a voice that
    has a sample can speak, and that is the whole requirement.
    """
    name = jobs.valid_name(request["name"])
    text = str(request.get("text", "")).strip()
    if not text:
        raise SystemExit("пустой текст")

    sample = engine.VOICES_DIR / f"{name}.wav"
    if not sample.is_file():
        raise SystemExit(f"нет образца для {name!r} — сначала подготовьте запись")

    # Named by the caller so it can fetch a specific one back, and sanitised
    # here because it becomes a path on this machine.
    out_name = Path(str(request.get("out", "phrase.wav"))).name
    if not out_name.endswith(".wav"):
        out_name += ".wav"

    target = jobs.dir_for(name) / out_name
    synthesise(name, text, target)
    return {"ok": True, "name": name, "file": target.name,
            "path": str(target), "text": text,
            "seconds": round(pregenerate._duration(target), 1)}


def cmd_generate(request: dict) -> dict:
    """The whole vocabulary, in the background, reporting progress to disk.

    This is the one thing that genuinely outlives its command: a thousand
    phrases is an hour or more. So it starts a thread, answers immediately, and
    the caller watches `status` — which reads the same json file whether the
    generating process is still alive or was killed and restarted.

    Resumable: phrases already on disk are skipped, so a run that died halfway
    picks up rather than starting over.
    """
    name = jobs.valid_name(request["name"])
    if jobs.load(name) is None:
        raise SystemExit(f"нет такого голоса: {name}")
    if not (engine.VOICES_DIR / f"{name}.wav").is_file():
        raise SystemExit(f"нет образца для {name!r}")

    # Marked busy here rather than inside the thread: otherwise this command
    # can answer with the previous stage and the panel shows a voice as idle
    # one poll after it was told to start.
    jobs.update(name, stage="generating", error="", progress=0,
                total=corpus.count(), engine=ENGINE)
    threading.Thread(target=_generate, args=(name,), daemon=True).start()
    return jobs.load(name).as_dict()


def _generate(name: str) -> None:
    try:
        out = engine.voice_dir(name)
        out.mkdir(parents=True, exist_ok=True)

        # A phrase's filename is a hash of its text and says nothing about how
        # it was spoken, so a directory built by an older engine would be
        # skipped phrase by phrase and leave a vocabulary in two voices.
        built_with = engine.manifest(name).get("engine", "")
        if built_with and built_with != ENGINE:
            gone = engine.clear_cache(name)
            print(f"[{name}] движок сменился ({built_with} → {ENGINE}): "
                  f"удалено {gone} старых фраз", flush=True)

        lines = corpus.lines()
        todo = [line for line in lines
                if engine.phrase_path(engine.normalise(line.text), name) is None]
        done = len(lines) - len(todo)
        jobs.update(name, total=len(lines), progress=done)

        # Spoken through workers that keep their model rather than one
        # subprocess per phrase. The old way paid an interpreter start, a
        # checkpoint load and a Whisper pass over the speaker sample for every
        # line — about fifteen seconds each, most of it setup. See tts/pool.py.
        from tts import pool

        state = {"done": done, "failed": 0}

        def landed(result: pool.Result) -> None:
            # Called from a worker thread as each phrase finishes, under the
            # pool's lock. Progress is written to disk here because a run is
            # watched from another machine entirely, and a count that only
            # exists in memory is invisible to it.
            if result.ok:
                state["done"] += 1
                jobs.update(name, progress=state["done"])
            else:
                state["failed"] += 1
                print(f"[{name}] не сказалась: {result.error}", flush=True)

        results = pool.speak_all(
            name,
            [(engine.normalise(line.text),
              out / f"{engine.key(engine.normalise(line.text))}.{engine.FORMAT}")
             for line in todo],
            accent=ENGINE.endswith("accent"),
            on_done=landed,
        )

        spoken = sum(1 for r in results if r.ok)
        if spoken:
            average = sum(r.seconds for r in results if r.ok) / spoken
            print(f"[{name}] сказано {spoken} фраз, "
                  f"в среднем {average:.1f}s на фразу", flush=True)

        pregenerate._write_manifest(
            name, engine.VOICES_DIR / f"{name}.wav", len(lines),
            device=pregenerate._pick_device("auto"), spoken_by=ENGINE)

        absent = engine.missing(name)
        if absent:
            jobs.fail(name, f"не хватает {len(absent)} фраз — запустите ещё раз")
            return
        archive(name)
        jobs.update(name, stage="ready", progress=len(lines))
    except BaseException as exc:                                # noqa: BLE001
        jobs.fail(name, exc)


def archive(name: str) -> Path:
    """Zip a finished voice so the table fetches it in one scp.

    A thousand small files over one link is a thousand chances to end up with a
    voice missing a phrase, which nothing at the table notices until a player
    is already holding the receiver. One archive either arrives or does not.
    """
    target = jobs.dir_for(name) / f"{name}.zip"
    source = engine.voice_dir(name)
    with zipfile.ZipFile(target, "w", zipfile.ZIP_STORED) as zf:
        for path in sorted(source.iterdir()):
            if path.is_file():
                zf.write(path, arcname=path.name)
    return target


def cmd_status(request: dict) -> dict:
    """One voice, as far as it has got."""
    name = jobs.valid_name(request["name"])
    job = jobs.load(name)
    if job is None:
        return {"error": "нет такого голоса", "name": name}
    return job.as_dict()


def cmd_list(request: dict) -> dict:
    """Every voice in progress here, and every one finished."""
    return {"jobs": jobs.all_jobs(), "installed": engine.voices()}


def cmd_health(request: dict) -> dict:
    """Whether this machine can actually synthesise, and on what."""
    from tts import backends

    try:
        import torch
        device = pregenerate._pick_device("auto")
        gpu = torch.cuda.get_device_name(0) if torch.cuda.is_available() else ""
    except Exception:                                           # noqa: BLE001
        device, gpu = "", ""

    ready = backends.available("f5")
    return {
        "ok": ready,
        "device": device,
        "gpu": gpu,
        "engine": ENGINE,
        "phrases": corpus.count(),
        "demucs": bool(pregenerate.demucs_binary()),
        "ffmpeg": bool(shutil.which("ffmpeg")),
        "error": "" if ready else backends.why_unavailable("f5"),
    }


def cmd_forget(request: dict) -> dict:
    """Drop a voice in progress and everything it was working from."""
    name = jobs.valid_name(request["name"])
    return {"ok": jobs.remove(name)}


COMMANDS = {
    "health": cmd_health,
    "prepare": cmd_prepare,
    "speak": cmd_speak,
    "generate": cmd_generate,
    "status": cmd_status,
    "list": cmd_list,
    "forget": cmd_forget,
}


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="python -m tts.remote",
        description="Клонирование голосов по одной команде через ssh.")
    parser.add_argument("command", choices=sorted(COMMANDS))
    args = parser.parse_args(argv)

    jobs.WORK_DIR.mkdir(parents=True, exist_ok=True)
    engine.VOICES_DIR.mkdir(parents=True, exist_ok=True)

    raw = sys.stdin.read().strip()
    try:
        request = json.loads(raw) if raw else {}
    except ValueError:
        _reply({"error": "аргументы не JSON"}, code=2)
        return

    try:
        answer = COMMANDS[args.command](request)
    except SystemExit as exc:
        _reply({"error": str(exc)}, code=1)
        return
    except KeyError as exc:
        _reply({"error": f"не хватает поля {exc}"}, code=1)
        return
    except Exception as exc:                                    # noqa: BLE001
        _reply({"error": f"{type(exc).__name__}: {exc}"}, code=1)
        return

    _reply(answer)


def _reply(payload: dict, code: int = 0) -> None:
    """One JSON object on its own marked line, and nothing else that matters."""
    sys.stdout.flush()
    print(REPLY_MARKER + json.dumps(payload, ensure_ascii=False), flush=True)
    if code:
        raise SystemExit(code)


if __name__ == "__main__":
    main()
