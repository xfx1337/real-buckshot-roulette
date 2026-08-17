"""Generate the whole vocabulary in every queued voice, in one long process.

Runs on the farm, not on the laptop. It is what the detached batch actually
executes, and it exists because `tts.remote generate` cannot be driven from a
shell loop: that command marks the job as generating, starts a background
thread and returns, so the thread dies with the process the moment the command
exits. A queue built out of `remote generate` calls leaves 25 job files reading
"generating" with nothing behind any of them — which is exactly what the first
attempt did.

So the work is done in the foreground here. One process, one voice at a time,
each voice generated to completion before the next begins. The process lives as
long as the batch does; when it ends, the batch is genuinely finished rather
than merely started.

Sequential across voices on purpose. Concurrency lives inside a voice, in the
worker pool, where several workers share one card and one queue of phrases.
Running two voices at once would put two pools on the same GPU competing for
the same memory, which buys no throughput and risks the out-of-memory that
would cost both.

    venv/bin/python scripts/farm_batch.py tts/work/.batch/queue.txt

Resumable at every level. A voice with a collected zip is skipped whole; a
voice with a sample keeps it; phrases already on disk are never re-said.
"""

from __future__ import annotations

import os
import shutil
import sys
import time
from pathlib import Path

# Run from the project root whatever the working directory was: the batch is
# launched by setsid from a shell whose cwd is not guaranteed, and every path
# below is relative to the checkout.
ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

from tts import corpus, engine, jobs, pregenerate, pool          # noqa: E402
from tts.remote import ENGINE, archive                           # noqa: E402

BATCH = Path("tts/work/.batch")
INBOX = Path("tts/work/.inbox")
COLLECTED = Path("tts/work/.collected")

def phrase_file(name: str, text: str) -> Path:
    """Where a new phrase is written: the voice's directory, in engine.FORMAT.

    New phrases are always written compressed. Reading is a different question —
    engine.phrase_path answers that one, and it accepts the uncompressed wav an
    older engine left behind, so a voice generated before the change to AAC is
    finished rather than started over.
    """
    return (engine.voice_dir(name)
            / f"{engine.key(engine.normalise(text))}.{engine.FORMAT}")


def note(message: str) -> None:
    """One line of progress, on disk, readable from another machine.

    Flushed and fsync'd rather than buffered: this file is how the batch is
    watched over a link that is not reliable, and a progress line still sitting
    in a buffer when the connection is up is a progress line nobody can read.
    """
    line = f"{time.strftime('%F %T')} {message}\n"
    with (BATCH / "progress.txt").open("a", encoding="utf-8") as handle:
        handle.write(line)
        handle.flush()
        os.fsync(handle.fileno())
    print(line, end="", flush=True)


def prepare_sample(name: str, source: Path) -> bool:
    """Cut the speaker sample, unless this voice already has one.

    Cut once and never re-cut. A voice resumed after an interruption still has
    its wav, and cutting a fresh one would pick a different window of the same
    recording — the phrases already on disk would then be in a subtly different
    voice from the ones still to come, which is audible and unfixable short of
    regenerating the lot.
    """
    sample = engine.VOICES_DIR / f"{name}.wav"
    if sample.is_file():
        return True
    if not source.is_file():
        note(f"{name}: нет записи {source}")
        return False

    note(f"{name}: образец из {source.name}")
    try:
        # The window is chosen from the recording's own length, the same way
        # tts.remote does it, rather than from a fixed default.
        from tts.remote import sample_seconds
        seconds = sample_seconds(source)
        jobs.save(jobs.Job(name=name, stage="preparing", source=source.name,
                           song=False, seconds=seconds))
        pregenerate.prepare(source, name, song=False, seconds=seconds)
    except BaseException as exc:                                 # noqa: BLE001
        jobs.fail(name, exc)
        note(f"{name}: образец не вышел: {exc}")
        return False

    jobs.update(name, stage="sample")
    return sample.is_file()


def generate(name: str) -> bool:
    """Say every phrase this voice is missing, and zip the result.

    The body of tts.remote._generate, called in the foreground instead of in a
    daemon thread. Progress is written to the job file as each phrase lands, so
    the dealer's panel and `--status` see the same numbers they always did.
    """
    out = engine.voice_dir(name)
    out.mkdir(parents=True, exist_ok=True)

    # A phrase's filename is a hash of its text and says nothing about how it
    # was spoken, so a directory built by an older engine would be skipped
    # phrase by phrase and leave a vocabulary in two voices.
    built_with = engine.manifest(name).get("engine", "")
    if built_with and built_with != ENGINE:
        gone = engine.clear_cache(name)
        note(f"{name}: движок сменился ({built_with} → {ENGINE}), "
             f"удалено {gone} фраз")

    lines = corpus.lines()
    todo = [line for line in lines
            if engine.phrase_path(engine.normalise(line.text), name) is None]
    done = len(lines) - len(todo)

    jobs.update(name, stage="generating", error="", progress=done,
                total=len(lines), engine=ENGINE)

    if not todo:
        note(f"{name}: все {len(lines)} фраз уже на диске")
    else:
        note(f"{name}: синтез {len(todo)} фраз "
             f"(готово {done}, воркеров {pool.WORKERS})")

        state = {"done": done, "failed": 0, "mark": time.time()}
        started = time.time()

        def landed(result: pool.Result) -> None:
            # Called from a worker thread as each phrase finishes, under the
            # pool's lock. The count is written to disk here because the run is
            # watched from another machine, and a number that only exists in
            # memory is invisible to it. Every fiftieth phrase also lands in
            # the progress log, which is what makes a stall distinguishable
            # from slow work without opening the job file.
            if result.ok:
                state["done"] += 1
                jobs.update(name, progress=state["done"])
                if state["done"] % 50 == 0:
                    rate = (time.time() - started) / max(1, state["done"] - done)
                    left = (len(lines) - state["done"]) * rate
                    note(f"{name}: {state['done']}/{len(lines)}, "
                         f"{rate:.1f}с/фраза, осталось ~{left / 60:.0f} мин")
            else:
                state["failed"] += 1
                note(f"{name}: не сказалась: {result.error}")

        results = pool.speak_all(
            name,
            [(engine.normalise(line.text), phrase_file(name, line.text))
             for line in todo],
            accent=ENGINE.endswith("accent"),
            on_done=landed,
        )

        spoken = sum(1 for r in results if r.ok)
        if spoken:
            average = sum(r.seconds for r in results if r.ok) / spoken
            note(f"{name}: сказано {spoken}, в среднем {average:.1f}с/фраза")

    pregenerate._write_manifest(
        name, engine.VOICES_DIR / f"{name}.wav", len(lines),
        device=pregenerate._pick_device("auto"), spoken_by=ENGINE)

    absent = engine.missing(name)
    if absent:
        jobs.fail(name, f"не хватает {len(absent)} фраз")
        note(f"{name}: не хватает {len(absent)} фраз")
        return False

    jobs.update(name, stage="ready", progress=len(lines))
    zipped = archive(name)
    COLLECTED.mkdir(parents=True, exist_ok=True)
    shutil.copy2(zipped, COLLECTED / f"{name}.zip")
    note(f"{name}: готов, архив собран")
    return True


def main(argv: list[str]) -> int:
    queue_file = Path(argv[1]) if len(argv) > 1 else BATCH / "queue.txt"
    entries = []
    for line in queue_file.read_text(encoding="utf-8").splitlines():
        parts = line.split(None, 1)
        if len(parts) == 2:
            entries.append((parts[0].strip(), parts[1].strip()))

    BATCH.mkdir(parents=True, exist_ok=True)
    COLLECTED.mkdir(parents=True, exist_ok=True)

    note(f"батч начался: {len(entries)} голосов, "
         f"{corpus.count()} фраз каждый, воркеров {pool.WORKERS}")

    ok = 0
    for index, (name, filename) in enumerate(entries, 1):
        if (COLLECTED / f"{name}.zip").is_file():
            note(f"[{index}/{len(entries)}] {name}: уже собран, пропуск")
            ok += 1
            continue

        note(f"[{index}/{len(entries)}] {name}")
        try:
            if not prepare_sample(name, INBOX / filename):
                continue
            if generate(name):
                ok += 1
        except BaseException as exc:                             # noqa: BLE001
            # One voice that dies must not take the queue with it: the next
            # voice is independent work and there is no reason it should wait
            # for a person to notice.
            jobs.fail(name, exc)
            note(f"{name}: сорвался: {exc}")

    note(f"батч закончился: {ok} из {len(entries)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
