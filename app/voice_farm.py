"""Talking to the machine that has the GPU, one ssh command at a time.

The dealer's panel is on the laptop under the table; cloning a voice happens
somewhere with a graphics card. This is the only file that knows the second one
exists, and it exists so that the panel can be written as if it did not.

There used to be an HTTP server on the far side and an SSH tunnel holding a
local port open to reach it. The tunnel was the problem this file was rewritten
to remove. ssh survives a broken network while still listening on the forwarded
port, so a dead link looks identical to a working one until every request hangs
to its timeout — the panel sat on "Загрузка…" and the tool looked broken rather
than disconnected. A watchdog re-raised it every few seconds and that was a
permanent chore rather than a fix.

Nothing here keeps a connection. Every operation opens its own `ssh gpufarm`,
runs one command, reads one JSON answer, and closes. Files move with `scp`. A
link that dies takes exactly one operation with it, the error says so, and the
next attempt is a fresh connection — there is no half-dead state to detect.

Nothing here synthesises anything either. It sends a recording, asks for a
phrase or for the whole vocabulary, and fetches what came out.

Where the farm is: TTS_FARM_HOST, an ssh destination — a Host from ~/.ssh/config
(`gpufarm`, which is the default) or user@address. Where the code lives on it:
TTS_FARM_DIR.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import zipfile
from pathlib import Path

from tts import engine

# Which machine, as ssh names one. A Host block in ~/.ssh/config is the
# expected form: it carries the key, the user and the address, so nothing here
# has to know any of them.
HOST = os.environ.get("TTS_FARM_HOST", "gpufarm")

# Where the project is checked out on that machine, and which interpreter can
# import it. Both are configuration rather than discovery — a farm that keeps
# them elsewhere says so once, here.
REMOTE_DIR = os.environ.get("TTS_FARM_DIR", "~/backshot-tts")
REMOTE_PYTHON = os.environ.get("TTS_FARM_PYTHON", "venv/bin/python")

# The farm's own working directory, where jobs and finished archives live.
#
# Inside the package rather than beside it: tts/jobs.py builds these paths from
# tts/engine.ROOT, which is the tts/ directory itself. Getting this wrong is
# invisible until a voice finishes — upload and generate both work, and only
# fetch fails, looking for an archive one directory above the one that holds it.
REMOTE_WORK = f"{REMOTE_DIR}/tts/work"

# Where uploads land on the far side before they are cut into a sample.
REMOTE_INBOX = f"{REMOTE_WORK}/.inbox"

# Marks the one line of the far side's stdout that is the answer. Must match
# tts/remote.py; everything else on that stream is ffmpeg and torch talking.
REPLY_MARKER = "__TTS_REPLY__ "

# ssh options shared by every call. BatchMode so a missing key fails instead of
# waiting for a password nobody will type; the keepalives so a command whose
# network died returns an error in about half a minute rather than hanging on a
# TCP timeout measured in tens of minutes.
SSH_OPTIONS = [
    "-o", "BatchMode=yes",
    "-o", "ConnectTimeout=10",
    "-o", "ServerAliveInterval=10",
    "-o", "ServerAliveCountMax=3",
]

# How long a command may take. Three tiers, because the work behind them
# differs by three orders of magnitude and one number would either cut off a
# real job or leave a dead one hanging.
QUICK_TIMEOUT = 30        # health, status, list: reading a json file
PREPARE_TIMEOUT = 1800    # demucs on a song is genuinely slow
SPEAK_TIMEOUT = 1800      # one phrase, plus a cold model load the first time
TRANSFER_TIMEOUT = 3600   # a recording up, or a finished vocabulary down


class FarmError(RuntimeError):
    """The farm could not do it, in words worth showing the operator."""


def configured() -> bool:
    return bool(HOST)


# ── running one command over there ──────────────────────────────────────

def _how_long(seconds: int) -> str:
    """A timeout as a person would say it, since these span 30 s to an hour."""
    return f"{seconds} с" if seconds < 120 else f"{seconds // 60} мин"


def _run(argv: list[str], timeout: int, what: str) -> subprocess.CompletedProcess:
    """One ssh or scp, with its failure turned into something readable."""
    try:
        return subprocess.run(argv, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise FarmError(f"{what}: ферма не ответила за {_how_long(timeout)}")
    except OSError as exc:
        raise FarmError(f"{what}: не запустить {argv[0]} ({exc})")


def _decode(raw: bytes) -> str:
    """Whatever the far side printed, as text that cannot raise.

    errors="replace" rather than strict: ffmpeg echoes the source file's own
    metadata, and an mp3 tagged in cp1251 is not UTF-8. Failing to decode the
    noise around an answer must not destroy the answer.
    """
    return raw.decode("utf-8", errors="replace")


def _remote(command: str, payload: dict | None = None, *,
            timeout: int = QUICK_TIMEOUT) -> dict:
    """Run one tts.remote subcommand on the farm and return what it said.

    The arguments travel on stdin as JSON rather than in argv. They contain
    Russian text and arbitrary phrases, and argv here would be quoted by a
    local shell, by ssh, and by the login shell on the far side — three chances
    to mangle it. stdin passes through all three untouched.
    """
    if not configured():
        raise FarmError(
            "Машина с видеокартой не настроена: TTS_FARM_HOST пуст. "
            "Укажите ssh-хост фермы (обычно gpufarm).")

    remote_command = (f"cd {REMOTE_DIR} && "
                      f"{REMOTE_PYTHON} -m tts.remote {command}")
    what = f"команда {command!r}"
    try:
        result = subprocess.run(
            ["ssh", *SSH_OPTIONS, HOST, remote_command],
            input=json.dumps(payload or {}, ensure_ascii=False).encode(),
            capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise FarmError(f"{what}: ферма не ответила за {_how_long(timeout)}")
    except OSError as exc:
        raise FarmError(f"{what}: не запустить ssh ({exc})")

    stdout = _decode(result.stdout)
    answer = None
    for line in stdout.splitlines():
        if line.startswith(REPLY_MARKER):
            try:
                answer = json.loads(line[len(REPLY_MARKER):])
            except ValueError:
                answer = None

    if answer is None:
        # No marked line at all: the far side died before it could answer, or
        # ssh itself refused. Its complaint is the useful part.
        tail = "\n".join(
            (_decode(result.stderr) or stdout).strip().splitlines()[-4:])
        raise FarmError(tail or f"ферма молча вышла с кодом {result.returncode}")

    if isinstance(answer, dict) and answer.get("error"):
        raise FarmError(str(answer["error"]))
    return answer


# How many times a file transfer is attempted before giving up.
#
# Transfers get retries and commands do not, because they fail differently. A
# command either ran or did not, and repeating one that did would start a
# second job; a transfer that dies mid-file has changed nothing that matters
# and the second attempt is free of consequences.
#
# They also fail more. The link to this farm drops connections that were open
# and idle — "Timeout, server not responding" arrives after minutes of a
# working transfer — and that is the ordinary condition of the link rather than
# an outage. Retrying is what makes a phrase arrive rather than a dealer
# pressing the same button again.
TRANSFER_ATTEMPTS = 3


def _transfer(argv: list[str], what: str, complaint: str) -> None:
    """One scp, attempted more than once, since this link drops mid-file."""
    last = ""
    for attempt in range(1, TRANSFER_ATTEMPTS + 1):
        result = _run(argv, TRANSFER_TIMEOUT, what)
        if result.returncode == 0:
            return
        last = "\n".join(_decode(result.stderr).strip().splitlines()[-3:])
        print(f"[ферма] {what}: попытка {attempt} из {TRANSFER_ATTEMPTS} "
              f"не удалась: {last}", flush=True)
    raise FarmError(f"{complaint}: {last}")


def _push(local: Path, remote: str) -> None:
    """Copy a file to the farm, making sure its directory exists first."""
    _run(["ssh", *SSH_OPTIONS, HOST, f"mkdir -p {remote}"],
         QUICK_TIMEOUT, "подготовка каталога")
    _transfer(["scp", *SSH_OPTIONS, str(local), f"{HOST}:{remote}/"],
              "отправка файла", "не отправить файл на ферму")


def _pull(remote: str, local: Path) -> None:
    """Copy a file back from the farm, resuming where the link dropped.

    Not scp, and not for elegance. The link from this farm drops downloads
    partway through — measured repeatedly at a few tens of kilobytes, in a
    different place every time, while uploads of eighteen megabytes go through
    untouched. scp and `ssh cat` fail identically, so it is the path and not
    the tool. A voice archive is a hundred megabytes; restarting it from zero
    on every drop never finishes.

    So the file is fetched in pieces with `dd`, each piece appended to what is
    already here, and a piece that dies is simply asked for again from the byte
    after the last one that arrived. Progress is never lost, and a link that
    manages thirty kilobytes at a time still delivers the file — slowly, which
    is the difference between slow and impossible.

    The size is asked for first, so the loop knows when it is done rather than
    guessing from a short read: a short read is the normal case here.
    """
    local.parent.mkdir(parents=True, exist_ok=True)

    size = _remote_size(remote)

    partial = local.with_suffix(local.suffix + ".part")
    partial.unlink(missing_ok=True)

    # How much to ask for at once, and how far to back off when the link
    # refuses that much.
    #
    # Asking high and letting short reads bank their progress works only while
    # the link answers at all. This one does something worse: past a certain
    # request size it drops the whole connection and returns nothing, so a
    # fixed large chunk retries the same doomed size until it gives up, having
    # moved zero bytes. Measured on this farm — a megabyte closed the
    # connection every time, sixty-four kilobytes went through untouched.
    #
    # So the size is not fixed. It starts where the link is usually happy and
    # halves on every empty answer, down to a floor; anything that arrives
    # resets it. A link having a bad minute costs a smaller chunk rather than
    # the transfer.
    chunk = 1 << 18                      # 256 KB
    MIN_CHUNK = 1 << 15                  # 32 KB, below which it is not worth it
    stalled = 0
    while True:
        have = partial.stat().st_size if partial.is_file() else 0
        if have >= size:
            break
        # dd rather than `tail -c +N`: byte offsets, no line semantics.
        #
        # skip_bytes/count_bytes rather than bs=1, which is the obvious way to
        # write a byte offset and is unusably slow — one read and one write
        # syscall per byte, on a file that runs to a hundred megabytes. With
        # the flags, dd reads in large blocks and the offset is still exact.
        command = (f"dd if={remote} bs=1M skip={have} count={chunk} "
                   f"iflag=skip_bytes,count_bytes status=none 2>/dev/null")
        result = _run(["ssh", *SSH_OPTIONS, HOST, command],
                      TRANSFER_TIMEOUT, "получение файла")
        got = result.stdout
        if got:
            with partial.open("ab") as handle:
                handle.write(got)
            stalled = 0
            continue

        # Nothing at all came back. Ask for less before deciding the farm is
        # gone: on this link an empty answer usually means the request was too
        # large rather than that the machine has stopped answering, and only a
        # chunk already at the floor is evidence of the latter.
        stalled += 1
        if chunk > MIN_CHUNK:
            chunk = max(MIN_CHUNK, chunk // 2)
            print(f"[ферма] связь оборвалась на {have} из {size} байт — "
                  f"беру кусками по {chunk // 1024} КБ", flush=True)
            stalled = 0
            continue

        print(f"[ферма] получение файла встало на {have} из {size} байт "
              f"(попытка {stalled} из {TRANSFER_ATTEMPTS})", flush=True)
        if stalled >= TRANSFER_ATTEMPTS:
            partial.unlink(missing_ok=True)
            raise FarmError(
                f"не забрать файл с фермы: связь рвётся, получено "
                f"{have} из {size} байт")

    partial.replace(local)


def _remote_size(remote: str) -> int:
    """How many bytes the file on the farm has, so a resumed pull can end.

    The path is unquoted deliberately. REMOTE_DIR starts with `~`, and a shell
    only expands a tilde that is bare — inside quotes it is a directory called
    "~", which does not exist. Quoting here made every pull report a missing
    file while the archive sat on the farm at the path in the error message.

    Upload never hit this: there the tilde travels inside JSON and tts/remote.py
    calls expanduser() on it. Only the paths that go into a command line need
    the shell to do the expanding, so only they must stay unquoted.

    Safe because these paths are ours — REMOTE_DIR from configuration and a
    voice name that _valid_name() has already restricted to letters, digits,
    dash and underscore.
    """
    result = _run(["ssh", *SSH_OPTIONS, HOST,
                   f"stat -c %s {remote} 2>/dev/null || echo missing"],
                  QUICK_TIMEOUT, "размер файла на ферме")
    answer = _decode(result.stdout).strip().splitlines()
    try:
        return int(answer[-1])
    except (IndexError, ValueError):
        raise FarmError(f"на ферме нет файла {remote}")


# ── what the panel asks for ─────────────────────────────────────────────

def health() -> dict:
    """Whether the far machine is there, and what it can do."""
    if not configured():
        return {"ok": False, "configured": False,
                "error": "TTS_FARM_HOST не задан"}
    state = _remote("health")
    state["configured"] = True
    state["url"] = HOST
    return state


def voices() -> dict:
    """Every voice being worked on, and every one already generated there."""
    if not configured():
        return {"jobs": [], "installed": [], "configured": False}
    state = _remote("list")
    state["configured"] = True
    return state


def upload(name: str, filename: str, content: bytes, *, song: bool,
           seconds: int | None = None) -> dict:
    """Send a recording and have a speaker sample cut from it.

    `seconds` is accepted and ignored. How long a sample to cut is decided on
    the farm from the recording's own duration — see tts/remote.sample_seconds.
    The parameter stays in the signature because the panel and this file are
    deployed separately and an older panel still posts it.

    The uploaded name is thrown away and only its extension kept. A browser
    sends whatever the file is called on disk, and here that is regularly
    Russian — "Мобилизация силами частников.mp3" — which then has to survive
    scp, a remote shell and a path. Nothing downstream wants it: the sample is
    stored under the voice's own name, and only the extension matters, so
    ffmpeg can tell what it has been handed.
    """
    name = _valid_name(name)

    suffix = Path(filename).suffix.lower()
    if not suffix or len(suffix) > 8 or not suffix[1:].isalnum():
        suffix = ".bin"

    staging = Path(engine.ROOT) / "work" / ".outgoing"
    staging.mkdir(parents=True, exist_ok=True)
    local = staging / f"{name}{suffix}"
    try:
        local.write_bytes(content)
        _push(local, REMOTE_INBOX)
    finally:
        local.unlink(missing_ok=True)

    # The `~` in this path is left for the far side to expand. It travels
    # inside JSON, where no shell touches it, so tts/remote.py calls
    # expanduser() on what it receives — one round trip cheaper than asking
    # this machine what the farm's home directory is, and one fewer command
    # that can time out before any work has started.
    return _remote("prepare", {
        "name": name,
        "source": f"{REMOTE_INBOX}/{name}{suffix}",
        "song": song,
    }, timeout=PREPARE_TIMEOUT)


def _valid_name(name: str) -> str:
    """The same rule the farm enforces, applied before anything travels.

    Duplicated deliberately rather than imported from tts.jobs: this name ends
    up in an scp argument and a remote path, so it has to be checked on this
    side too — refusing here costs one error message, refusing there costs a
    round trip with an unchecked string already in a command line.
    """
    name = (name or "").strip()
    if not name:
        raise FarmError("пустое имя голоса")
    if not all(c.isalnum() or c in "-_" for c in name):
        raise FarmError(
            f"имя {name!r}: только латиница, цифры, дефис и подчёркивание")
    if len(name) > 40:
        raise FarmError(f"имя длиннее 40 символов: {len(name)}")
    return name


def speak(name: str, text: str) -> dict:
    """Say one line in this voice and bring the wav back.

    The single-phrase mode, and the short path through all of this: a sample
    exists, a sentence is typed, a file comes back. It never touches the corpus
    or the job stages — those are for building a whole vocabulary, which is a
    different and much longer errand.

    The result lands in tts/cache/.spoken/ rather than in a voice directory: it
    is one file someone asked to hear, not part of what the game can say.
    """
    name = _valid_name(name)
    text = (text or "").strip()
    if not text:
        raise FarmError("пустой текст")

    # Named from the text's hash so asking for the same line twice reuses one
    # filename instead of accumulating copies, and so the name is safe by
    # construction — the text is Russian and arbitrary.
    out_name = f"{engine.key(text)}.wav"
    answer = _remote("speak", {"name": name, "text": text,
                                    "out": out_name},
                          timeout=SPEAK_TIMEOUT)

    local = spoken_dir() / f"{name}_{out_name}"
    _pull(answer["path"], local)
    answer["local"] = str(local)
    answer["file"] = local.name
    return answer


def spoken_dir() -> Path:
    """Where single phrases fetched from the farm are kept."""
    directory = engine.CACHE_DIR / ".spoken"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def generate(name: str) -> dict:
    """Say yes: generate the whole vocabulary in this voice."""
    return _remote("generate", {"name": _valid_name(name)})


def status(name: str) -> dict:
    return _remote("status", {"name": _valid_name(name)})


def forget(name: str) -> dict:
    return _remote("forget", {"name": _valid_name(name)})


# ── bringing a finished voice home ──────────────────────────────────────

def fetch(name: str) -> dict:
    """Download a finished voice and install it into tts/cache/.

    Unpacked through a temporary directory and moved into place at the end: a
    download that dies half way must not leave a voice that looks installed but
    is missing the phrases after the break, because nothing at the table would
    notice until a player was already holding the receiver.
    """
    name = _valid_name(name)

    staging = engine.CACHE_DIR / f".incoming_{name}"
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True, exist_ok=True)
    local_zip = staging / f"{name}.zip"
    try:
        _pull(f"{REMOTE_WORK}/{name}/{name}.zip", local_zip)

        with zipfile.ZipFile(local_zip) as zf:
            for entry in zf.namelist():
                # The archive is written by tts/remote.py with flat names, so
                # anything with a path in it did not come from there.
                if "/" in entry or "\\" in entry or entry.startswith(".."):
                    raise FarmError(f"подозрительное имя в архиве: {entry!r}")
            zf.extractall(staging)
        local_zip.unlink(missing_ok=True)

        got = sum(len(list(staging.glob(f"tts_*.{suffix}")))
                  for suffix in engine.FORMATS)
        from tts import corpus
        expected = corpus.count()
        if got < expected:
            raise FarmError(f"в архиве {got} фраз из {expected} — "
                            f"голос на ферме не достроен")

        final = engine.voice_dir(name)
        shutil.rmtree(final, ignore_errors=True)
        staging.rename(final)
    except zipfile.BadZipFile:
        raise FarmError(f"архив голоса {name!r} повреждён — скачайте заново")
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    return {"ok": True, "voice": name, "phrases": got}
