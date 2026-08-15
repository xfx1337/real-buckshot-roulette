"""The machine with the GPU, listening for voices to clone.

Runs on the GPU host, not at the table. It holds the XTTS model in memory —
loading it costs half a minute, and an operator adding four voices in an
evening should pay that once — and exposes the few things the dealer's panel
needs to ask for:

    POST /voice          a recording arrives; cut a speaker sample from it
    POST /voice/audition make a few phrases so a human can hear the clone
    POST /voice/approve  the human said yes; generate the whole vocabulary
    GET  /voices         what every voice is doing right now
    GET  /file/...       fetch an audition phrase, or a finished voice archive

Everything long-running answers immediately and works in a thread, because the
panel asking "how is it going" must not be the same request that is doing it.
Progress goes to disk through tts/jobs.py, so a restart here loses nothing
except the model in memory.

Deliberately not authenticated and not encrypted. This speaks to one laptop
over a private link, and adding a login here would be security theatre that
still leaves the audio in the clear. Bind it to a private interface or an SSH
tunnel — never to a public one.

    python -m tts.farm --port 8770

The panel is pointed at it with TTS_FARM=http://host:8770 on the game server.
"""

from __future__ import annotations

import argparse
import json
import shutil
import threading
import time
import zipfile
from pathlib import Path

from tts import corpus, engine, jobs, pregenerate

# The model, once. Loaded on the first request that needs it rather than at
# startup, so the process comes up instantly and a panel pointed at it can
# already list voices while the weights are still arriving.
_model = None
_model_lock = threading.Lock()
_device = "auto"


def model():
    """The XTTS model, loading it the first time it is asked for."""
    global _model
    with _model_lock:
        if _model is None:
            device = pregenerate._pick_device(_device)
            _model = pregenerate._load_model(device)
        return _model


# How much of the speaker sample XTTS conditions on, in seconds.
#
# The library defaults to six, which is what a demo needs and far less than a
# voice deserves: a six-second window catches one intonation and one register,
# and every phrase afterwards is built from that alone. Feeding the whole
# thirty gives the model the person's range instead of one moment of it, and
# the difference in how recognisable the clone is dwarfs anything the sampling
# parameters below do.
#
# It costs GPU time once per voice rather than per phrase — the latents are
# computed when a voice is first spoken and reused for its whole vocabulary.
COND_SECONDS = 30

# How the model samples while generating. Left near the library's defaults
# except where they hurt a voice reading the same sentence shape a thousand
# times:
#
# temperature  0.75 → 0.7. Slightly tighter. These phrases are all built from
#              the same handful of templates, and the higher setting wanders
#              into readings that sound like a different take of the same line.
# repetition_penalty  10 → 5. The default is tuned for free text; our phrases
#              deliberately say the important part twice ("третий — боевой,
#              повторяю: третий — боевой") and a heavy penalty makes the model
#              rush or swallow the repeat, which is exactly the part a player
#              is straining to hear.
# top_k / top_p  left alone. They control variety rather than fidelity.
#
# enable_text_splitting is off, and that matters for intonation. With it on the
# model cuts long text and generates each piece from nothing, so the pitch
# resets at every seam — heard as intonation lurching between words rather than
# as one person speaking. Our longest line is about 130 characters, well inside
# the 250 the tokenizer allows, so there is nothing to gain by splitting and a
# continuous contour to lose.
#
# The same reasoning shaped the punctuation in phrases.py: a full stop ends the
# contour, so the lines were rewritten to have three rather than six.
SAMPLING = {
    "temperature": 0.7,
    "repetition_penalty": 5.0,
    "top_k": 50,
    "top_p": 0.85,
    "length_penalty": 1.0,
    "enable_text_splitting": False,
}

# Conditioning latents, per voice and per chunk length. Computing them reads
# and encodes the whole sample, so doing it per phrase would add seconds to
# every one of a thousand.
#
# Keyed by chunk length as well as by voice because engines disagree about it:
# the same sample divided into four-second pieces and into one thirty-second
# piece produces different latents, and an audition comparing two engines has
# to actually hear that difference rather than whichever was computed first.
_latents: dict[tuple[str, int], tuple] = {}
_latents_lock = threading.Lock()


def _conditioning(name: str, chunk: int = COND_SECONDS) -> tuple:
    """The speaker latents for one voice, computed once per chunk length."""
    with _latents_lock:
        cache_key = (name, chunk)
        if cache_key not in _latents:
            sample = engine.VOICES_DIR / f"{name}.wav"
            if not sample.is_file():
                raise FileNotFoundError(f"нет образца голоса: {sample}")
            print(f"[{name}] считаю латенты по {COND_SECONDS} с образца, "
                  f"кусками по {chunk} с...", flush=True)
            xtts = model().synthesizer.tts_model
            _latents[cache_key] = xtts.get_conditioning_latents(
                audio_path=[str(sample)],
                gpt_cond_len=COND_SECONDS,
                gpt_cond_chunk_len=chunk,
                max_ref_length=COND_SECONDS,
                sound_norm_refs=True,
            )
        return _latents[cache_key]


def forget_voice(name: str) -> None:
    """Drop cached latents, so a re-cut sample is actually heard.

    Without this, cutting a new sample and auditioning again would keep
    speaking in the voice built from the old one — the confusing kind of bug
    where the operator's change appears to do nothing.

    Every chunk length for this voice goes, not just the one last used: they
    were all computed from the sample that has just been replaced.
    """
    with _latents_lock:
        for cache_key in [k for k in _latents if k[0] == name]:
            _latents.pop(cache_key, None)


def _synth(name: str, text: str, target: Path,
           spoken_by: str = engines.DEFAULT) -> None:
    """One phrase, in one voice, spoken by one engine, ready to play."""
    chosen = engines.get(spoken_by)
    if chosen.model != "xtts":
        raise RuntimeError(
            f"движок {chosen.label!r} пока не подключён к ферме: "
            f"здесь умеют только XTTS")

    gpt_cond_latent, speaker_embedding = _conditioning(name, chosen.cond_chunk)
    xtts = model().synthesizer.tts_model

    result = xtts.inference(
        text=engine.normalise(text),
        language="ru",
        gpt_cond_latent=gpt_cond_latent,
        speaker_embedding=speaker_embedding,
        **chosen.sampling,
    )

    raw = target.with_suffix(".raw.wav")
    try:
        engine.write_raw(result["wav"], raw)
        engine.convert(raw, target)
    finally:
        raw.unlink(missing_ok=True)


# ── the three long jobs ─────────────────────────────────────────────────

def _do_prepare(name: str) -> None:
    """Turn the uploaded recording into a speaker sample."""
    job = jobs.load(name)
    if job is None:
        return
    try:
        jobs.update(name, stage="preparing", error="")
        pregenerate.prepare(jobs.dir_for(name) / job.source, name,
                            song=job.song, seconds=job.seconds)
        # The sample on disk just changed, so anything computed from the old
        # one is now wrong.
        forget_voice(name)
        jobs.update(name, stage="sample")
    except Exception as exc:                                    # noqa: BLE001
        jobs.fail(name, exc)


def _do_audition(name: str, count: int) -> None:
    """Generate a handful of phrases for a human to judge the clone by.

    The phrases are real game lines rather than a fixed test sentence: what is
    being judged is whether this voice can carry the thing it will actually
    have to say, and a clone that sounds fine reading "проверка связи" can
    still fall apart on a shell number.

    Different lines each time it is asked. Auditioning twice and hearing the
    same three phrases tells the operator nothing new, and the usual reason to
    press the button again is that the first three were not enough to decide.
    """
    import random

    job = jobs.load(name)
    if job is None:
        return
    try:
        jobs.update(name, stage="auditioning", error="", progress=0,
                    total=count)
        directory = jobs.dir_for(name)
        # Clear the previous round: an audition is a snapshot of the current
        # sample, and mixing it with phrases cut from an older one is how an
        # operator ends up approving something they did not hear.
        for old in directory.glob("audition_*.wav"):
            old.unlink(missing_ok=True)

        lines = random.sample(corpus.lines(), min(count, corpus.count()))
        made, texts = [], []
        for index, line in enumerate(lines, 1):
            target = directory / f"audition_{index}.wav"
            _synth(name, line.text, target)
            made.append(target.name)
            texts.append(line.text)
            jobs.update(name, progress=index, auditions=made,
                        audition_texts=texts)
        jobs.update(name, stage="review", auditions=made, audition_texts=texts)
    except Exception as exc:                                    # noqa: BLE001
        jobs.fail(name, exc)


def _do_generate(name: str) -> None:
    """The full vocabulary, after a human has approved the clone.

    Resumable in the same way pregenerate is: phrases already on disk are
    skipped, so this picks up where a killed run stopped rather than starting
    the hour over.
    """
    try:
        jobs.update(name, stage="generating", error="", progress=0)
        out = engine.voice_dir(name)
        out.mkdir(parents=True, exist_ok=True)

        lines = corpus.lines()
        todo = [l for l in lines
                if not (out / f"{engine.key(engine.normalise(l.text))}.wav").is_file()]
        jobs.update(name, total=len(lines), progress=len(lines) - len(todo))

        for index, line in enumerate(todo, 1):
            text = engine.normalise(line.text)
            _synth(name, text, out / f"{engine.key(text)}.wav")
            jobs.update(name, progress=len(lines) - len(todo) + index)

        pregenerate._write_manifest(
            name, engine.VOICES_DIR / f"{name}.wav", len(lines),
            device=pregenerate._pick_device(_device))
        absent = engine.missing(name)
        if absent:
            jobs.fail(name, f"не хватает {len(absent)} фраз — запустите ещё раз")
        else:
            _archive(name)
            jobs.update(name, stage="ready", progress=len(lines))
    except Exception as exc:                                    # noqa: BLE001
        jobs.fail(name, exc)


def _archive(name: str) -> Path:
    """Zip a finished voice so the table can fetch it in one request.

    A thousand small files over a link that may not be fast is a thousand
    chances to end up with a voice that is missing one phrase, which is
    exactly the failure the table cannot detect until a player is holding the
    receiver. One archive either arrives or does not.
    """
    target = jobs.dir_for(name) / f"{name}.zip"
    source = engine.voice_dir(name)
    with zipfile.ZipFile(target, "w", zipfile.ZIP_STORED) as zf:
        for path in sorted(source.iterdir()):
            if path.is_file():
                zf.write(path, arcname=path.name)
    return target


# ── the http face ───────────────────────────────────────────────────────

def build_app():
    from flask import Flask, jsonify, request, send_file

    app = Flask(__name__)
    # A recording is the input here, and a minute of lossless audio is
    # comfortably tens of megabytes.
    app.config["MAX_CONTENT_LENGTH"] = 200 * 1024 * 1024

    def state(name: str):
        job = jobs.load(name)
        return jsonify(job.as_dict() if job else {"error": "нет такого голоса"})

    @app.get("/health")
    def health():
        """Whether this machine can clone, and on what."""
        try:
            import torch
            device = pregenerate._pick_device(_device)
            gpu = (torch.cuda.get_device_name(0)
                   if torch.cuda.is_available() else "")
        except Exception:                                       # noqa: BLE001
            device, gpu = "", ""
        return jsonify({
            "ok": True,
            "device": device,
            "gpu": gpu,
            "model_loaded": _model is not None,
            "phrases": corpus.count(),
            "demucs": bool(pregenerate.demucs_binary()),
            "ffmpeg": bool(shutil.which("ffmpeg")),
        })

    @app.get("/voices")
    def voices():
        return jsonify({"jobs": jobs.all_jobs(), "installed": engine.voices()})

    @app.post("/voice")
    def upload():
        """A recording arrives and a sample is cut from it."""
        name = request.form.get("name", "")
        try:
            name = jobs.valid_name(name)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400

        file = request.files.get("file")
        if file is None or not file.filename:
            return jsonify({"error": "нет файла"}), 400

        existing = jobs.load(name)
        if existing and existing.as_dict()["busy"]:
            return jsonify({"error": f"голос {name!r} уже в работе"}), 409

        directory = jobs.dir_for(name)
        directory.mkdir(parents=True, exist_ok=True)
        source = Path(file.filename).name
        file.save(directory / source)

        job = jobs.save(jobs.Job(
            name=name, stage="preparing", source=source,
            song=request.form.get("song") in ("1", "true", "on"),
            seconds=int(request.form.get("seconds", 30)),
        ))
        threading.Thread(target=_do_prepare, args=(name,), daemon=True).start()
        return jsonify(job.as_dict())

    @app.post("/voice/audition")
    def audition():
        """Make a few phrases so the operator can hear what was cloned."""
        data = request.get_json(silent=True) or {}
        name = data.get("name", "")
        count = max(1, min(int(data.get("count", jobs.AUDITION_PHRASES)), 10))

        job = jobs.load(name)
        if job is None:
            return jsonify({"error": "нет такого голоса"}), 404
        if job.as_dict()["busy"]:
            return jsonify({"error": "голос сейчас занят"}), 409
        if not (engine.VOICES_DIR / f"{name}.wav").is_file():
            return jsonify({"error": "нет образца — сначала загрузите запись"}), 409

        # Помечаем занятость здесь, а не в потоке: иначе ответ на этот же
        # запрос успевает уйти со старой стадией, и панель на один цикл
        # показывает «готов к прослушиванию» у голоса, который уже считается.
        jobs.update(name, stage="auditioning", error="", progress=0, total=count)
        threading.Thread(target=_do_audition, args=(name, count),
                         daemon=True).start()
        return state(name)

    @app.post("/voice/approve")
    def approve():
        """The operator said yes. Generate everything."""
        data = request.get_json(silent=True) or {}
        name = data.get("name", "")
        job = jobs.load(name)
        if job is None:
            return jsonify({"error": "нет такого голоса"}), 404
        if job.as_dict()["busy"]:
            return jsonify({"error": "голос сейчас занят"}), 409

        # total выставляем сразу: иначе первые секунды панель считает проценты
        # от числа пробных фраз и показывает «2 из 2, 100%» у голоса, которому
        # осталась ещё тысяча.
        jobs.update(name, stage="generating", error="", progress=0,
                    total=corpus.count(), auditions=[], audition_texts=[])
        threading.Thread(target=_do_generate, args=(name,), daemon=True).start()
        return state(name)

    @app.post("/voice/reject")
    def reject():
        """Not that one. Cut a different stretch of the same recording.

        Back to `sample` rather than to an error: this is the ordinary way a
        voice gets dialled in, not a failure. An offset moves the window when
        the interesting part of a recording is not at its start.
        """
        data = request.get_json(silent=True) or {}
        name = data.get("name", "")
        job = jobs.load(name)
        if job is None:
            return jsonify({"error": "нет такого голоса"}), 404

        seconds = int(data.get("seconds", job.seconds))
        jobs.update(name, stage="preparing", seconds=seconds, auditions=[],
                    audition_texts=[], error="")
        threading.Thread(target=_do_prepare, args=(name,), daemon=True).start()
        return state(name)

    @app.get("/voice/<name>")
    def one(name):
        return state(name)

    @app.delete("/voice/<name>")
    def drop(name):
        return jsonify({"ok": jobs.remove(name)})

    @app.get("/file/<name>/<filename>")
    def file(name, filename):
        """An audition phrase, or the finished archive."""
        path = jobs.dir_for(name) / Path(filename).name
        if not path.is_file():
            return jsonify({"error": "нет такого файла"}), 404
        return send_file(path)

    return app


def main(argv: list[str] | None = None) -> None:
    global _device

    parser = argparse.ArgumentParser(
        prog="python -m tts.farm",
        description="Клонирование голосов на машине с видеокартой.")
    # Петля по умолчанию, и менять это почти никогда не надо. Ферма не
    # спрашивает пароля и не шифрует, так что «слушать на всех интерфейсах» —
    # это отдать чужому в интернете и загрузку файлов, и видеокарту. Игровой
    # ноутбук дотягивается сюда туннелем (см. README), которому внешний адрес
    # не нужен вовсе.
    parser.add_argument("--host", default="127.0.0.1",
                        help="слушать на этом адресе; менять только если "
                             "точно понимаете, зачем открываете ферму наружу")
    parser.add_argument("--port", type=int, default=8770)
    parser.add_argument("--device", default="auto",
                        choices=["auto", "cuda", "mps", "cpu"])
    parser.add_argument("--preload", action="store_true",
                        help="загрузить модель сразу, а не при первом запросе")
    args = parser.parse_args(argv)

    _device = args.device
    jobs.WORK_DIR.mkdir(parents=True, exist_ok=True)
    engine.VOICES_DIR.mkdir(parents=True, exist_ok=True)

    if args.preload:
        model()

    print(f"ферма голосов на {args.host}:{args.port}, "
          f"устройство {pregenerate._pick_device(args.device)}")
    print(f"фраз в словаре: {corpus.count()}")
    if args.host not in ("127.0.0.1", "localhost", "::1"):
        print(f"ВНИМАНИЕ: ферма слушает {args.host} — она без пароля и без "
              f"шифрования.\n           Кто до неё дотянется, тот загружает "
              f"записи и занимает видеокарту.\n           Обычный способ "
              f"подключить игровой ноутбук — туннель, см. tts/README.md")
    build_app().run(host=args.host, port=args.port, threaded=True)


if __name__ == "__main__":
    main()
