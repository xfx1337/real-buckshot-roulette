"""Talking to the machine that has the GPU.

The dealer's panel is on the laptop under the table; cloning a voice happens
somewhere with a graphics card. This is the only file that knows the second one
exists, and it exists so that the panel can be written as if it did not: every
route here takes what a browser sent and hands back what the farm said.

Nothing here synthesises anything. It uploads a recording, asks for a few
phrases to listen to, passes on a yes or a no, and — when a voice is finished —
fetches the archive and unpacks it into tts/cache/, which is the moment the
voice becomes something the game can speak in.

Where the farm is: TTS_FARM, e.g. http://10.0.0.5:8770. Unset means no farm is
configured, and everything here answers with that rather than a connection
error, because "no GPU machine set up" and "the GPU machine is down" are
different problems for whoever is reading the panel.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from typing import Optional

from tts import engine

# Where the farm is. Nothing is assumed about it beyond speaking the protocol
# in tts/farm.py; it may be a machine on the bench or one across a tunnel.
FARM = os.environ.get("TTS_FARM", "").rstrip("/")

# Long enough to cover a model still loading on the far side, short enough that
# a farm which is simply not there fails while the operator is still looking at
# the panel.
TIMEOUT = 60

# Fetching a finished voice is a thousand phrases in one archive over a link
# that may not be fast, so it gets its own, far more generous, limit.
DOWNLOAD_TIMEOUT = 1800


class FarmError(RuntimeError):
    """The farm could not do it, in words worth showing the operator."""


def configured() -> bool:
    return bool(FARM)


def _url(path: str) -> str:
    if not FARM:
        raise FarmError(
            "Машина с видеокартой не настроена: переменная TTS_FARM пуста. "
            "Запустите на ней `python -m tts.farm` и укажите её адрес.")
    return f"{FARM}{path}"


def _request(path: str, *, data: bytes | None = None,
             headers: Optional[dict] = None, method: str = "GET",
             timeout: int = TIMEOUT) -> dict:
    request = urllib.request.Request(_url(path), data=data, method=method,
                                     headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read()
    except urllib.error.HTTPError as exc:
        # The farm reports its own refusals as JSON, and those are the useful
        # ones — "голос уже в работе" beats "HTTP 409" in front of an operator.
        try:
            detail = json.loads(exc.read()).get("error", "")
        except Exception:                                       # noqa: BLE001
            detail = ""
        raise FarmError(detail or f"ферма ответила {exc.code}")
    except urllib.error.URLError as exc:
        raise FarmError(f"ферма недоступна ({FARM}): {exc.reason}")
    except OSError as exc:
        raise FarmError(f"ферма недоступна ({FARM}): {exc}")

    try:
        return json.loads(body)
    except ValueError:
        raise FarmError("ферма ответила не JSON")


def _post_json(path: str, payload: dict, timeout: int = TIMEOUT) -> dict:
    return _request(path, data=json.dumps(payload).encode(),
                    headers={"Content-Type": "application/json"},
                    method="POST", timeout=timeout)


# ── what the panel asks for ─────────────────────────────────────────────

def health() -> dict:
    """Whether the far machine is there, and what it can do."""
    if not configured():
        return {"ok": False, "configured": False,
                "error": "TTS_FARM не задан"}
    state = _request("/health")
    state["configured"] = True
    state["url"] = FARM
    return state


def voices() -> dict:
    """Every voice being worked on, and every one already generated there."""
    if not configured():
        return {"jobs": [], "installed": [], "configured": False}
    state = _request("/voices")
    state["configured"] = True
    return state


def upload(name: str, filename: str, content: bytes, *, song: bool,
           seconds: int = 30) -> dict:
    """Send a recording and have a speaker sample cut from it.

    multipart is assembled by hand rather than with a library: this is the one
    place in the project that posts a file, and the alternative is a dependency
    on the game server for the sake of thirty lines.
    """
    boundary = "----backshot-voice-upload"
    parts: list[bytes] = []

    def field(key: str, value: str) -> None:
        parts.append(
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"{key}\"\r\n\r\n"
            f"{value}\r\n".encode())

    field("name", name)
    field("song", "1" if song else "0")
    field("seconds", str(seconds))
    parts.append(
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; "
        f"filename=\"{filename}\"\r\nContent-Type: application/octet-stream\r\n\r\n"
        .encode())
    parts.append(content)
    parts.append(f"\r\n--{boundary}--\r\n".encode())

    body = b"".join(parts)
    return _request("/voice", data=body, method="POST", timeout=DOWNLOAD_TIMEOUT,
                    headers={
                        "Content-Type": f"multipart/form-data; boundary={boundary}",
                        "Content-Length": str(len(body)),
                    })


def audition(name: str, count: int) -> dict:
    """Ask for a few phrases in this voice, to listen to before committing."""
    return _post_json("/voice/audition", {"name": name, "count": count})


def approve(name: str) -> dict:
    """Say yes: generate the whole vocabulary in this voice."""
    return _post_json("/voice/approve", {"name": name})


def reject(name: str, seconds: Optional[int] = None) -> dict:
    """Say no: cut a different stretch of the same recording and try again."""
    payload: dict = {"name": name}
    if seconds:
        payload["seconds"] = seconds
    return _post_json("/voice/reject", payload)


def status(name: str) -> dict:
    return _request(f"/voice/{name}")


def forget(name: str) -> dict:
    return _request(f"/voice/{name}", method="DELETE")


def audition_url(name: str, filename: str) -> str:
    """Where the panel's <audio> tag should point for one audition phrase."""
    return _url(f"/file/{name}/{filename}")


# ── bringing a finished voice home ──────────────────────────────────────

def fetch(name: str) -> dict:
    """Download a finished voice and install it into tts/cache/.

    Unpacked through a temporary directory and moved into place at the end:
    a download that dies half way must not leave a voice that looks installed
    but is missing the phrases after the break, because nothing at the table
    would notice until a player was already holding the receiver.
    """
    if not configured():
        raise FarmError("TTS_FARM не задан")

    url = _url(f"/file/{name}/{name}.zip")
    try:
        with urllib.request.urlopen(url, timeout=DOWNLOAD_TIMEOUT) as response:
            archive = response.read()
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise FarmError(f"на ферме нет готового архива для {name!r} — "
                            f"голос ещё не сгенерирован")
        raise FarmError(f"не скачать голос: ферма ответила {exc.code}")
    except (urllib.error.URLError, OSError) as exc:
        raise FarmError(f"не скачать голос: {exc}")

    staging = engine.CACHE_DIR / f".incoming_{name}"
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True, exist_ok=True)
    try:
        with zipfile.ZipFile(io.BytesIO(archive)) as zf:
            for entry in zf.namelist():
                # The archive is written by tts/farm.py with flat names, so
                # anything with a path in it did not come from there.
                if "/" in entry or "\\" in entry or entry.startswith(".."):
                    raise FarmError(f"подозрительное имя в архиве: {entry!r}")
            zf.extractall(staging)

        got = sum(1 for _ in staging.glob("tts_*.wav"))
        from tts import corpus
        expected = corpus.count()
        if got < expected:
            raise FarmError(f"в архиве {got} фраз из {expected} — "
                            f"голос на ферме не достроен")

        final = engine.voice_dir(name)
        shutil.rmtree(final, ignore_errors=True)
        staging.rename(final)
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    return {"ok": True, "voice": name, "phrases": got}
