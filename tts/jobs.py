"""What a voice is doing right now, written down where both machines can see it.

Adding a voice is not one action, it is a conversation that outlives any single
request: a file is uploaded, a sample is cut, a few phrases are auditioned, a
person says yes or no, and only then does an hour of generation start. The
panel that starts all this is on a laptop; the work happens on a machine with a
GPU somewhere else. Neither can hold that state in memory, because either can
be restarted in the middle and the other must still know where things stand.

So the state lives on disk, one JSON file per voice, beside the audio it
describes. That is enough: there is one operator, the transitions are slow and
few, and a file that can be read with `cat` when something goes wrong is worth
more here than anything cleverer.

The stages a voice moves through:

    uploaded    a recording arrived, nothing has been done to it yet
    preparing   the vocal is being separated and the sample cut
    sample      there is a speaker sample; audition phrases can be made
    auditioning a few phrases are being generated for a human to listen to
    review      phrases are ready and waiting on a yes or a no
    generating  the answer was yes; the full vocabulary is being produced
    ready       every phrase exists; the voice can speak at the table
    failed      something broke, and `error` says what

Rejection is not a stage. Saying no about an audition puts a voice back to
`sample` so another few seconds of the recording can be tried, which is the
normal way this goes rather than an exception.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

from tts import engine

# Where a voice's own working files live: the upload, the cut sample, the
# audition phrases, and the status file tying them together. Separate from
# cache/ because cache/ is only finished phrases — the thing that gets copied
# to the table — and a half-finished voice must not look like a usable one.
WORK_DIR = engine.ROOT / "work"

# How many phrases an audition makes by default. One is often not enough to
# tell a good clone from a lucky one, and more than a handful is a wait for
# no more information.
AUDITION_PHRASES = 3

STAGES = ("uploaded", "preparing", "sample", "auditioning", "review",
          "generating", "ready", "failed")

_lock = threading.Lock()


@dataclass
class Job:
    """One voice, and how far along it is."""

    name: str
    stage: str = "uploaded"
    source: str = ""             # the uploaded recording, as a filename
    song: bool = False           # whether the vocal had to be separated out
    seconds: int = 30            # how much of it the sample takes
    error: str = ""
    progress: int = 0            # phrases generated so far
    total: int = 0               # phrases the full run will make
    auditions: list[str] = field(default_factory=list)   # wav filenames
    audition_texts: list[str] = field(default_factory=list)
    started: float = field(default_factory=time.time)
    updated: float = field(default_factory=time.time)

    def as_dict(self) -> dict:
        data = asdict(self)
        data["percent"] = (round(100 * self.progress / self.total)
                           if self.total else 0)
        data["busy"] = self.stage in ("preparing", "auditioning", "generating")
        return data


def _path(name: str) -> Path:
    return WORK_DIR / name / "job.json"


def dir_for(name: str) -> Path:
    """Where this voice's working files go."""
    return WORK_DIR / name


def valid_name(name: str) -> str:
    """A voice name that is safe to use as a directory.

    Names arrive from a text field in a browser, and they become paths on two
    machines and an rsync between them. Anything outside this set is refused
    rather than sanitised: a voice silently renamed is a voice the operator
    cannot find again.
    """
    name = name.strip()
    if not name:
        raise ValueError("пустое имя голоса")
    if not all(c.isalnum() or c in "-_" for c in name):
        raise ValueError(
            f"имя {name!r}: только латиница, цифры, дефис и подчёркивание")
    if len(name) > 40:
        raise ValueError(f"имя длиннее 40 символов: {len(name)}")
    return name


def load(name: str) -> Optional[Job]:
    """This voice's state, or None if there is no such voice in progress."""
    try:
        data = json.loads(_path(name).read_text())
    except (OSError, ValueError):
        return None
    known = {f for f in Job.__dataclass_fields__}
    return Job(**{k: v for k, v in data.items() if k in known})


def save(job: Job) -> Job:
    """Write a voice's state down. Every transition goes through here."""
    job.updated = time.time()
    with _lock:
        dir_for(job.name).mkdir(parents=True, exist_ok=True)
        _path(job.name).write_text(
            json.dumps(job.as_dict(), ensure_ascii=False, indent=2))
    return job


def update(name: str, **fields) -> Optional[Job]:
    """Change some of a voice's state, leaving the rest alone."""
    job = load(name)
    if job is None:
        return None
    for key, value in fields.items():
        if key in Job.__dataclass_fields__:
            setattr(job, key, value)
    return save(job)


def fail(name: str, error: str) -> Optional[Job]:
    """Record that something broke, in words the operator can act on."""
    return update(name, stage="failed", error=str(error))


def all_jobs() -> list[dict]:
    """Every voice being worked on, newest first.

    Includes finished ones: a voice that generated successfully is still worth
    showing, because "ready here" and "copied to the table" are different
    facts and the operator is the one who bridges them.
    """
    if not WORK_DIR.is_dir():
        return []
    found = []
    for path in WORK_DIR.iterdir():
        if not path.is_dir():
            continue
        job = load(path.name)
        if job is not None:
            found.append(job.as_dict())
    return sorted(found, key=lambda j: j["started"], reverse=True)


def remove(name: str) -> bool:
    """Forget a voice in progress, and everything it was working from.

    Does not touch cache/: a voice that finished generating has already left
    this directory behind, and deleting an audition should never delete the
    hour of work that came after it.
    """
    import shutil

    directory = dir_for(name)
    if not directory.is_dir():
        return False
    with _lock:
        shutil.rmtree(directory, ignore_errors=True)
    return True
