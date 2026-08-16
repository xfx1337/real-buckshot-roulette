"""Speaking a whole vocabulary through workers that stay loaded.

tts/worker.py is one F5 process that keeps its model and answers phrases off a
pipe. This is the side that starts them, hands out the work, and puts what
comes back where the game expects to find it.

Why more than one. A single worker leaves the card idle between phrases: F5
spends part of each phrase in Python — accenting the text, writing the wav,
levelling it through ffmpeg — and the GPU waits through all of it. Two or three
workers interleave, so one is on the card while another is in numpy. Past that
they contend for the same card and for its memory, and each holds its own copy
of the checkpoint, so the number is small and bounded rather than "one per
core".

What a worker costs to start is the reason it is started once: a model load and
a Whisper pass over the speaker sample, tens of seconds, paid per worker per
run instead of per phrase.

The failure this is built around is a worker dying mid-run — out of memory,
CUDA fault, killed. Its phrase is put back, the worker is replaced, and the run
continues. A phrase that fails repeatedly is recorded and skipped, because a
thousand-phrase run should not end on one line the model cannot say.
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from tts import backends, engine

# How many workers a run uses unless told otherwise.
#
# Two is the useful default on one card. It covers the gap where a worker is in
# Python rather than on the GPU, which is most of what a single worker wastes,
# without the memory of a third copy of the checkpoint. Raise it on a card with
# room to spare; TTS_WORKERS exists so that does not need an edit.
WORKERS = int(os.environ.get("TTS_WORKERS", "2"))

# How long to wait for a worker to load its model and transcribe the sample.
# Generous: the first run on a cold machine may be downloading weights, and a
# worker wrongly declared dead here restarts and pays the same cost again.
STARTUP_TIMEOUT = 900

# How long one phrase may take before its worker is presumed hung. Ordinary
# phrases are seconds; this is the ceiling that tells a slow phrase from a
# process that will never answer.
PHRASE_TIMEOUT = 300

# How many times a phrase is handed to a fresh worker before it is given up on.
# Two, because the failure worth retrying is "the worker died", and a phrase
# that kills two workers in a row is the phrase's fault rather than bad luck.
MAX_ATTEMPTS = 2


@dataclass
class Result:
    """What happened to one phrase."""

    text: str
    target: Path
    ok: bool
    seconds: float = 0.0
    error: str = ""


class Worker:
    """One loaded F5 process, addressed over its stdin and stdout."""

    def __init__(self, name: str, index: int, *, accent: bool = True) -> None:
        self.name = name
        self.index = index
        self.accent = accent
        self.process: subprocess.Popen | None = None
        self.spoken = 0

    def start(self) -> None:
        """Launch the process and wait until it says it is loaded.

        The worker script is sent as a file rather than as source on the
        command line: it is long enough that a shell argument is the wrong
        shape for it, and having it on disk means a crash traceback names real
        line numbers in a real file.
        """
        python = backends.interpreter("f5")
        if python is None:
            raise RuntimeError("нет окружения f5: ~/f5venv")

        script = Path(__file__).resolve().parent / "worker.py"
        if not script.is_file():
            raise RuntimeError(f"нет tts/worker.py рядом с {__file__}")

        config = json.dumps({
            "sample": str(engine.VOICES_DIR / f"{self.name}.wav"),
            "arch": backends.F5_ARCH,
            "checkpoint": str(backends.F5_CHECKPOINT),
            "vocab": str(backends.F5_VOCAB),
            "accent": self.accent,
        })

        # PYTHONPATH so the worker can be run by an interpreter that has never
        # heard of this package: it lives in tts/ but is executed by ~/f5venv.
        environment = dict(os.environ)
        root = str(Path(__file__).resolve().parent.parent)
        environment["PYTHONPATH"] = root + os.pathsep + environment.get("PYTHONPATH", "")

        self.process = subprocess.Popen(
            [str(python), str(script), config],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=None,                 # straight to the run's own log
            text=True, bufsize=1, env=environment,
        )

        # The worker's first line is its readiness. Waiting for it here means a
        # model that cannot load fails at startup, with the traceback already
        # on stderr, rather than on the first phrase handed to it.
        deadline = time.time() + STARTUP_TIMEOUT
        while time.time() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError(
                    f"worker {self.index} умер при загрузке "
                    f"(код {self.process.returncode}) — см. лог")
            line = self.process.stdout.readline()
            if not line:
                raise RuntimeError(f"worker {self.index}: канал закрылся при загрузке")
            try:
                if json.loads(line).get("ready"):
                    return
            except ValueError:
                continue                 # not protocol; the worker prints freely
        raise RuntimeError(
            f"worker {self.index}: не загрузился за {STARTUP_TIMEOUT // 60} минут")

    def speak(self, text: str, target: Path) -> Result:
        """One phrase. Raises only if the worker itself is gone."""
        if self.process is None or self.process.poll() is not None:
            raise RuntimeError(f"worker {self.index} не жив")

        raw = target.with_suffix(".raw.wav")
        request = json.dumps({"text": text, "target": str(raw)},
                             ensure_ascii=False)
        try:
            self.process.stdin.write(request + "\n")
            self.process.stdin.flush()
        except (BrokenPipeError, ValueError) as exc:
            raise RuntimeError(f"worker {self.index}: канал закрыт ({exc})")

        deadline = time.time() + PHRASE_TIMEOUT
        while time.time() < deadline:
            line = self.process.stdout.readline()
            if not line:
                raise RuntimeError(f"worker {self.index}: замолчал на фразе")
            try:
                answer = json.loads(line)
            except ValueError:
                continue
            if "ok" not in answer:
                continue

            if not answer["ok"]:
                raw.unlink(missing_ok=True)
                return Result(text=text, target=target, ok=False,
                              error=answer.get("error", ""))

            # Levelling stays here rather than in the worker. It is ffmpeg
            # rather than the model, engine.convert owns what a cached file
            # looks like, and doing it on this side keeps the worker to one
            # job: turning text into samples.
            try:
                engine.convert(raw, target)
            except Exception as exc:                              # noqa: BLE001
                return Result(text=text, target=target, ok=False,
                              error=f"ffmpeg: {exc}")
            finally:
                raw.unlink(missing_ok=True)

            self.spoken += 1
            return Result(text=text, target=target, ok=True,
                          seconds=float(answer.get("seconds", 0.0)))

        raise RuntimeError(f"worker {self.index}: фраза не уложилась в "
                           f"{PHRASE_TIMEOUT}s")

    def stop(self) -> None:
        if self.process is None:
            return
        try:
            self.process.stdin.write(json.dumps({"stop": True}) + "\n")
            self.process.stdin.flush()
            self.process.wait(timeout=20)
        except Exception:                                         # noqa: BLE001
            self.process.kill()
        finally:
            self.process = None


def speak_all(name: str, work: list[tuple[str, Path]], *,
              workers: int = WORKERS, accent: bool = True,
              on_done=None) -> list[Result]:
    """Say every phrase in `work`, through workers that stay loaded.

    `work` is (text, target) pairs, already filtered to what is missing — this
    does not decide what needs saying, only says it. `on_done` is called with
    each Result as it lands, from a worker's own thread, and is how a caller
    reports progress; it is called under a lock, so it may touch shared state.

    Results come back in completion order rather than in the order given. The
    caller has the target path in each Result and nothing downstream depends on
    ordering — the files are addressed by hash.
    """
    if not work:
        return []

    workers = max(1, min(workers, len(work)))
    pending: queue.Queue = queue.Queue()
    for item in work:
        pending.put((item[0], item[1], 0))    # text, target, attempts

    results: list[Result] = []
    lock = threading.Lock()

    def record(result: Result) -> None:
        with lock:
            results.append(result)
            if on_done is not None:
                on_done(result)

    def run(index: int) -> None:
        worker: Worker | None = None
        while True:
            try:
                text, target, attempts = pending.get_nowait()
            except queue.Empty:
                break

            # Started lazily, so a run whose queue drains before this worker
            # ever gets an item never pays to load a model.
            if worker is None:
                try:
                    worker = Worker(name, index, accent=accent)
                    worker.start()
                except Exception as exc:                          # noqa: BLE001
                    # This worker cannot exist. Put the phrase back for
                    # somebody else and stop, rather than spinning on a
                    # failure that will repeat.
                    pending.put((text, target, attempts))
                    print(f"[{name}] worker {index} не запустился: {exc}",
                          file=sys.stderr, flush=True)
                    return

            try:
                record(worker.speak(text, target))
            except Exception as exc:                              # noqa: BLE001
                # The worker died rather than the phrase failing. Replace it
                # and give the phrase another go on the fresh one.
                print(f"[{name}] worker {index} упал: {exc}",
                      file=sys.stderr, flush=True)
                try:
                    worker.stop()
                except Exception:                                 # noqa: BLE001
                    pass
                worker = None

                if attempts + 1 < MAX_ATTEMPTS:
                    pending.put((text, target, attempts + 1))
                else:
                    record(Result(text=text, target=target, ok=False,
                                  error=f"фраза не далась за {MAX_ATTEMPTS} "
                                        f"попытки: {exc}"))
            finally:
                pending.task_done()

        if worker is not None:
            worker.stop()

    threads = [threading.Thread(target=run, args=(i,), daemon=True)
               for i in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    return results
