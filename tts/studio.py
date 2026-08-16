"""A voice studio anyone on the local network can open in a browser.

The farm already knew how to clone a voice; what it lacked was a way in that
did not involve a terminal. tts/remote.py is driven over ssh by the laptop
under the table, which is right for the game — the table wants one command,
one answer, no service to keep alive — and wrong for a person who simply has a
recording and wants to hear what it sounds like cloned. That person had to own
the laptop, know the ssh alias, and type JSON on stdin.

So this is the same work behind an address. It runs on the GPU machine itself
rather than proxying from the laptop, because every byte it handles is already
there: the recording is uploaded straight to the disk that will read it, the
wav is served from where it was written, and nothing crosses the link that
tts/voice_farm.py has to retry three times per transfer. The laptop can be off.

The shape of a visit, and the reason it is in this order:

    1. upload a recording          → a speaker sample is cut from it
    2. hear one phrase in it       → the preview, before anything expensive
    3. approve, and generate       → five game phrases and/or your own text

The preview exists because cloning is not reliable enough to skip it. A sample
cut from a bad stretch of audio — music under the voice, two people talking,
six seconds of breathing — produces a clone that is confidently wrong, and the
only way to know is to listen. One phrase costs about eleven seconds; finding
out after a batch costs the batch. So nothing generates in bulk until a person
has heard the voice and said yes.

Bound to 0.0.0.0 and asks for no password. It lives on a WSL machine behind a
Windows host on a home network, which is the only reason that is acceptable —
see the deployment notes in tts/README.md. Do not put it on a public address.
"""

from __future__ import annotations

import asyncio
import random
import threading
import time
import uuid
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from tts import corpus, engine, jobs, pregenerate, remote

# Where uploads land before a sample is cut from them. Under the work
# directory rather than in /tmp so a recording survives a restart mid-job and
# an operator can find what was actually uploaded when a clone sounds wrong.
INBOX = jobs.WORK_DIR / ".inbox"

# Where generated wavs are written and served from. One directory per batch,
# named by a token the browser is handed, so two people using the studio at
# once never read each other's files.
OUTBOX = jobs.WORK_DIR / ".studio"

# How many game phrases a batch draws. The number the operator asked for: a
# handful is enough to hear the voice across different sentence shapes without
# waiting through the full corpus, which is a thousand phrases and three hours.
GAME_PHRASES = 5

# The largest recording accepted, in bytes. Generous — an hour-long interview
# is a legitimate source and demucs will find the vocal in it — but not
# unbounded, because this reads the whole upload into memory before writing it.
MAX_UPLOAD = 512 * 1024 * 1024

# How long a finished batch stays on disk before it is swept. Long enough to
# download what you came for, short enough that a machine running for months
# does not fill its disk with auditions nobody kept.
BATCH_TTL = 24 * 3600

app = FastAPI(title="Студия голосов", docs_url=None, redoc_url=None)

# Every batch this process has started, by token. In memory rather than on
# disk, unlike tts/jobs.py: a batch is one browser tab's worth of work, and if
# the service restarts mid-batch the wavs are gone anyway because the thread
# generating them died with it. Voices are the durable thing here, not batches.
_batches: dict[str, dict] = {}
_lock = threading.Lock()

# Synthesis is serialised. The GPU fits one XTTS decode at a time and two
# concurrent requests do not run twice as fast — they run at half speed each
# and double the chance of an out-of-memory kill halfway through somebody's
# batch. A queue is slower to start and finishes sooner.
_gpu = threading.Lock()


# ── batches ─────────────────────────────────────────────────────────────

def _new_batch(voice: str, texts: list[str]) -> str:
    """Register a batch of phrases and return the token that names it."""
    token = uuid.uuid4().hex[:16]
    with _lock:
        _batches[token] = {
            "token": token,
            "voice": voice,
            "total": len(texts),
            "done": 0,
            "stage": "queued",
            "error": "",
            "started": time.time(),
            "items": [{"text": t, "file": "", "error": ""} for t in texts],
        }
    return token


def _batch_dir(token: str) -> Path:
    return OUTBOX / token


def _own_token(token: str) -> bool:
    """Whether this string is a token this service issues, rather than a path.

    Tokens are hex from uuid4, so anything else — a separator, a dot, an empty
    string — is refused before it is ever joined onto a directory.
    """
    return bool(token) and len(token) <= 32 and all(
        c in "0123456789abcdef" for c in token)


def _recover(token: str) -> dict | None:
    """Rebuild a finished batch from the wavs it left on disk.

    Batches live in memory, which is right while one is running — the thread
    doing the work is in this process and dies with it. But the audio outlives
    the process, and a restart used to turn every link on an open page into a
    404 while the files sat there intact. That looked exactly like broken
    playback and was the second thing to chase when it happened.

    What cannot be recovered is the text of each phrase: the filename holds a
    hash of it, and a hash does not read back. So a recovered batch plays and
    downloads, and shows its lines as unknown rather than pretending.
    """
    directory = _batch_dir(token)
    if not _own_token(token) or not directory.is_dir():
        return None

    files = sorted(p.name for p in directory.glob("*.wav"))
    if not files:
        return None

    return {
        "token": token,
        "voice": "",
        "total": len(files),
        "done": len(files),
        "stage": "ready",
        "error": "",
        "started": directory.stat().st_mtime,
        "items": [{"text": "(фраза из прошлой сессии)", "file": name,
                   "error": ""} for name in files],
        "recovered": True,
    }


def _get_batch(token: str) -> dict | None:
    """A batch by token, from memory if it is live and from disk if it is not."""
    batch = _batches.get(token)
    if batch is not None:
        return batch
    return _recover(token)


def _run_batch(token: str) -> None:
    """Generate every phrase in a batch, one at a time, recording each result.

    A phrase that fails does not stop the batch. Cloning fails per phrase more
    often than per voice — a line with an unusual character, a decode that ran
    long — and losing four good phrases because the fifth broke would mean
    starting over for no reason. Each item carries its own error instead.
    """
    batch = _batches[token]
    directory = _batch_dir(token)
    directory.mkdir(parents=True, exist_ok=True)
    batch["stage"] = "generating"

    for index, item in enumerate(batch["items"]):
        target = directory / f"{index:02d}_{engine.key(item['text'])}.wav"
        try:
            with _gpu:
                remote.synthesise(batch["voice"], item["text"], target)
            item["file"] = target.name
        except BaseException as exc:                            # noqa: BLE001
            item["error"] = f"{type(exc).__name__}: {exc}"
        batch["done"] = index + 1

    failed = sum(1 for item in batch["items"] if item["error"])
    batch["stage"] = "failed" if failed == batch["total"] else "ready"
    if failed and batch["stage"] == "ready":
        batch["error"] = f"не получилось фраз: {failed}"
    elif failed == batch["total"]:
        batch["error"] = batch["items"][0]["error"]


def _sweep() -> None:
    """Drop batches whose audio has outlived its usefulness."""
    cutoff = time.time() - BATCH_TTL
    import shutil

    with _lock:
        stale = [t for t, b in _batches.items()
                 if b["started"] < cutoff and b["stage"] in ("ready", "failed")]
        for token in stale:
            _batches.pop(token, None)
    for token in stale:
        shutil.rmtree(_batch_dir(token), ignore_errors=True)


# ── what the page asks for ──────────────────────────────────────────────

@app.get("/api/health")
async def api_health() -> dict:
    """Whether this machine can synthesise, and on what."""
    return await asyncio.to_thread(remote.cmd_health, {})


@app.get("/api/voices")
async def api_voices() -> dict:
    """Every voice with a speaker sample, ready to be spoken in.

    Read from the samples directory rather than from job files: a voice whose
    sample exists can speak, whatever its job.json last recorded. Jobs track a
    full-corpus build, which most visits here never start.
    """
    def collect() -> dict:
        found = []
        if engine.VOICES_DIR.is_dir():
            for path in sorted(engine.VOICES_DIR.glob("*.wav")):
                found.append({
                    "name": path.stem,
                    "seconds": round(pregenerate._duration(path), 1),
                })
        return {"voices": found}

    return await asyncio.to_thread(collect)


@app.post("/api/upload")
async def api_upload(name: str = Form(...), song: str = Form(""),
                     file: UploadFile = File(...)) -> dict:
    """Take a recording and cut a speaker sample out of it.

    `song` asks for the vocal to be separated from the backing track first,
    which is slow (demucs, minutes) and only correct for music — run on plain
    speech it removes parts of the voice it mistakes for accompaniment.
    """
    try:
        name = jobs.valid_name(name)
    except ValueError as exc:
        raise HTTPException(400, str(exc))

    content = await file.read()
    if not content:
        raise HTTPException(400, "пустой файл")
    if len(content) > MAX_UPLOAD:
        raise HTTPException(413, f"файл больше {MAX_UPLOAD // (1024 * 1024)} МБ")

    # Only the extension of the uploaded name is kept. Browsers send whatever
    # the file is called on disk, and here that is regularly Russian with
    # spaces; ffmpeg needs the suffix to know what it was handed and nothing
    # needs the rest.
    suffix = Path(file.filename or "").suffix.lower()
    if not suffix or len(suffix) > 8 or not suffix[1:].isalnum():
        suffix = ".bin"

    INBOX.mkdir(parents=True, exist_ok=True)
    source = INBOX / f"{name}{suffix}"
    source.write_bytes(content)

    try:
        return await asyncio.to_thread(remote.cmd_prepare, {
            "name": name,
            "source": str(source),
            "song": song.lower() in ("1", "true", "on", "yes"),
        })
    except SystemExit as exc:
        raise HTTPException(400, str(exc))


@app.post("/api/preview")
async def api_preview(name: str = Form(...), text: str = Form("")) -> dict:
    """One phrase in this voice, to listen to before committing to a batch.

    Deliberately a single synchronous call rather than a batch of one: the
    caller waits for it, hears it, and decides. Roughly eleven seconds with the
    model resident, longer on the first phrase after a restart.
    """
    text = (text or "").strip()
    if not text:
        # A default worth hearing: long enough to judge a clone by, and a
        # sentence the game itself would say, so a voice that sounds right here
        # sounds right at the table.
        text = random.choice([line.text for line in corpus.lines()])

    try:
        answer = await asyncio.to_thread(_preview, name, text)
    except SystemExit as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:                                    # noqa: BLE001
        raise HTTPException(500, f"{type(exc).__name__}: {exc}")
    return answer


def _preview(name: str, text: str) -> dict:
    """Synthesise one phrase into its own batch directory, so it is servable."""
    name = jobs.valid_name(name)
    if not (engine.VOICES_DIR / f"{name}.wav").is_file():
        raise SystemExit(f"нет образца для {name!r} — сначала загрузите запись")

    token = _new_batch(name, [text])
    directory = _batch_dir(token)
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"00_{engine.key(text)}.wav"

    batch = _batches[token]
    batch["stage"] = "generating"
    try:
        with _gpu:
            remote.synthesise(name, text, target)
    except BaseException as exc:                                # noqa: BLE001
        batch["stage"] = "failed"
        batch["error"] = str(exc)
        raise
    batch["items"][0]["file"] = target.name
    batch["done"] = 1
    batch["stage"] = "ready"

    return {"ok": True, "token": token, "text": text,
            "url": f"/audio/{token}/{target.name}",
            "seconds": round(pregenerate._duration(target), 1)}


@app.post("/api/generate")
async def api_generate(name: str = Form(...), texts: str = Form(""),
                       game: str = Form("")) -> dict:
    """Start a batch: some game phrases, some typed lines, or both.

    Answers as soon as the batch is registered rather than when it is done. A
    batch of five is a minute of GPU time and a browser holding a request open
    that long is a browser that times out on someone's network; the page polls
    /api/batch instead.
    """
    try:
        name = jobs.valid_name(name)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    if not (engine.VOICES_DIR / f"{name}.wav").is_file():
        raise HTTPException(400, f"нет образца для {name!r}")

    wanted: list[str] = []
    if game.lower() in ("1", "true", "on", "yes"):
        # Sampled rather than taken from the front: the first phrases of the
        # corpus are all the same shape (position 1 of a 1-shell magazine), and
        # five of those say nothing about how the voice handles anything else.
        lines = [line.text for line in corpus.lines()]
        wanted += random.sample(lines, min(GAME_PHRASES, len(lines)))

    for line in (texts or "").splitlines():
        line = engine.normalise(line)
        if line:
            wanted.append(line)

    if not wanted:
        raise HTTPException(400, "нечего генерировать: "
                                 "введите текст или отметьте игровые фразы")

    _sweep()
    token = _new_batch(name, wanted)
    threading.Thread(target=_run_batch, args=(token,), daemon=True).start()
    return {"ok": True, "token": token, "total": len(wanted)}


@app.get("/api/batch/{token}")
async def api_batch(token: str) -> dict:
    """How far a batch has got, and what can be played already."""
    batch = _get_batch(token)
    if batch is None:
        raise HTTPException(404, "нет такой партии")

    items = [{
        "text": item["text"],
        "error": item["error"],
        "url": f"/audio/{token}/{item['file']}" if item["file"] else "",
    } for item in batch["items"]]

    return {"token": token, "voice": batch["voice"], "stage": batch["stage"],
            "done": batch["done"], "total": batch["total"],
            "error": batch["error"], "items": items}


@app.get("/audio/{token}/{filename}")
async def audio(token: str, filename: str) -> FileResponse:
    """Serve one generated wav, playable in a browser's audio element.

    No `filename=` argument, deliberately. Passing one makes FileResponse send
    `Content-Disposition: attachment`, which tells the browser to download the
    file instead of playing it — an <audio> element handed that response stays
    silent with no error anywhere. Downloading is what the "скачать wav" link's
    own `download` attribute is for, and that works without the header.

    Both parts of the path are validated rather than sanitised: the token must
    name a batch directory this process created, and the filename must be one
    of the wavs inside it. That is a shorter rule than trying to enumerate the
    ways `..` can be spelled.
    """
    directory = _batch_dir(token)
    if not _own_token(token) or not directory.is_dir():
        raise HTTPException(404, "нет такой партии")

    path = directory / filename
    # resolve() then compare parents: a filename containing a path separator or
    # `..` resolves outside the batch directory and is refused here regardless
    # of how it was spelled.
    if path.resolve().parent != directory.resolve() or not path.is_file():
        raise HTTPException(404, "нет такого файла")
    return FileResponse(path, media_type="audio/wav")


@app.get("/sample/{name}")
async def sample(name: str) -> FileResponse:
    """The speaker sample cut from an upload, so it can be checked by ear.

    Worth hearing on its own: when a clone sounds wrong, the sample is where
    it usually went wrong — a window with music under it, or the wrong person
    talking — and that is audible here in three seconds rather than inferred
    from a bad phrase.
    """
    try:
        name = jobs.valid_name(name)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    path = engine.VOICES_DIR / f"{name}.wav"
    if not path.is_file():
        raise HTTPException(404, "нет образца")
    return FileResponse(path, media_type="audio/wav", filename=f"{name}.wav")


@app.exception_handler(HTTPException)
async def http_error(request, exc: HTTPException) -> JSONResponse:
    """Errors as JSON with a message, since the page shows exactly that."""
    return JSONResponse({"error": exc.detail}, status_code=exc.status_code)


# ── the page ────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    return PAGE


PAGE = """<!doctype html>
<html lang="ru">
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Студия голосов</title>
<style>
  :root {
    --bg: #14161a; --panel: #1c1f26; --line: #2b3039;
    --text: #e8eaed; --dim: #9aa2ad; --accent: #d8b26a; --bad: #d97070;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; padding: 24px 16px 64px; background: var(--bg); color: var(--text);
    font: 15px/1.55 system-ui, -apple-system, "Segoe UI", sans-serif;
  }
  main { max-width: 720px; margin: 0 auto; }
  h1 { font-size: 22px; margin: 0 0 4px; font-weight: 600; }
  .sub { color: var(--dim); font-size: 13px; margin-bottom: 24px; }
  section {
    background: var(--panel); border: 1px solid var(--line); border-radius: 10px;
    padding: 18px; margin-bottom: 16px;
  }
  section.off { opacity: .45; pointer-events: none; }
  h2 { font-size: 13px; text-transform: uppercase; letter-spacing: .08em;
       color: var(--dim); margin: 0 0 14px; font-weight: 600; }
  h2 .n { color: var(--accent); }
  label { display: block; font-size: 13px; color: var(--dim); margin: 12px 0 5px; }
  input[type=text], input[type=file], textarea, select {
    width: 100%; padding: 9px 11px; background: #12141a; color: var(--text);
    border: 1px solid var(--line); border-radius: 6px; font: inherit;
  }
  textarea { min-height: 88px; resize: vertical; }
  button {
    padding: 9px 18px; background: var(--accent); color: #1a1206; border: 0;
    border-radius: 6px; font: inherit; font-weight: 600; cursor: pointer;
    margin-top: 14px;
  }
  button.ghost { background: transparent; color: var(--dim);
                 border: 1px solid var(--line); }
  button:disabled { opacity: .5; cursor: default; }
  .row { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }
  .check { display: flex; align-items: center; gap: 8px; margin-top: 14px;
           color: var(--text); font-size: 14px; }
  .check input { width: 16px; height: 16px; accent-color: var(--accent); }
  audio { width: 100%; margin-top: 12px; }
  .msg { margin-top: 12px; font-size: 13px; color: var(--dim); }
  .msg.bad { color: var(--bad); }
  .item { border-top: 1px solid var(--line); padding: 12px 0; }
  .item:first-of-type { border-top: 0; }
  .item p { margin: 0 0 6px; font-size: 14px; }
  .item .err { color: var(--bad); font-size: 13px; }
  .bar { height: 3px; background: var(--line); border-radius: 2px;
         overflow: hidden; margin-top: 12px; }
  .bar i { display: block; height: 100%; background: var(--accent); width: 0;
           transition: width .3s; }
  .hint { font-size: 12px; color: var(--dim); margin-top: 6px; }
  .health { font-size: 12px; color: var(--dim); margin-bottom: 20px; }
  .health b { color: var(--text); font-weight: 500; }
</style>
<main>
  <h1>Студия голосов</h1>
  <div class="sub">Запись — образец — прослушивание — генерация.</div>
  <div class="health" id="health">Проверяю ферму…</div>

  <section>
    <h2><span class="n">1.</span> Исходная запись</h2>
    <div class="row">
      <div style="flex:1 1 200px">
        <label for="voice">Готовый голос</label>
        <select id="voice"><option value="">— загрузить новый —</option></select>
      </div>
    </div>
    <div id="upload">
      <label for="name">Имя нового голоса (латиница, цифры, дефис)</label>
      <input type="text" id="name" placeholder="katz" autocomplete="off">
      <label for="file">Файл записи (mp3, wav, m4a…)</label>
      <input type="file" id="file" accept="audio/*,video/*">
      <div class="check">
        <input type="checkbox" id="song">
        <label for="song" style="margin:0">Это песня — отделить вокал от музыки (долго)</label>
      </div>
      <div class="hint">Нужно 6–30 секунд чистой речи. Длинную запись ферма
        обрежет сама, выбрав лучший участок.</div>
      <button id="send">Загрузить и вырезать образец</button>
    </div>
    <div id="sampleBox" style="display:none">
      <div class="msg">Образец, по которому клонируется голос:</div>
      <audio id="sampleAudio" controls preload="none"></audio>
    </div>
    <div class="msg" id="uploadMsg"></div>
  </section>

  <section id="step2" class="off">
    <h2><span class="n">2.</span> Прослушать</h2>
    <label for="previewText">Фраза для проверки (пусто — случайная игровая)</label>
    <input type="text" id="previewText" placeholder="Здравствуйте. Второй патрон боевой." autocomplete="off">
    <button id="preview">Синтезировать одну фразу</button>
    <div class="msg" id="previewMsg"></div>
    <div id="previewBox" style="display:none">
      <p class="msg" id="previewSaid"></p>
      <audio id="previewAudio" controls autoplay></audio>
      <div class="hint">Похоже на оригинал — подтверждайте. Нет — загрузите
        другой отрывок записи.</div>
    </div>
  </section>

  <section id="step3" class="off">
    <h2><span class="n">3.</span> Сгенерировать фразы</h2>
    <div class="check">
      <input type="checkbox" id="game" checked>
      <label for="game" style="margin:0">5 случайных игровых фраз</label>
    </div>
    <label for="texts">Свои фразы — по одной в строке</label>
    <textarea id="texts" placeholder="Здравствуйте. Третий патрон холостой.
Абонент временно недоступен."></textarea>
    <button id="run">Сгенерировать</button>
    <div class="bar" id="bar" style="display:none"><i></i></div>
    <div class="msg" id="runMsg"></div>
    <div id="results"></div>
  </section>
</main>
<script>
const $ = id => document.getElementById(id);
let voice = "";

const say = (el, text, bad) => {
  el.textContent = text || "";
  el.className = bad ? "msg bad" : "msg";
};

async function call(url, options) {
  const res = await fetch(url, options);
  let data = {};
  try { data = await res.json(); } catch (e) {}
  if (!res.ok) throw new Error(data.error || ("ошибка " + res.status));
  return data;
}

// ── ферма ─────────────────────────────────────────────────────────────
async function health() {
  try {
    const h = await call("/api/health");
    $("health").innerHTML = h.ok
      ? `Ферма готова: <b>${h.gpu || h.device}</b> · движок <b>${h.engine}</b>
         · в словаре игры <b>${h.phrases}</b> фраз`
      : `<span style="color:var(--bad)">Ферма не может синтезировать: ${h.error}</span>`;
  } catch (e) {
    $("health").innerHTML = `<span style="color:var(--bad)">Ферма недоступна: ${e.message}</span>`;
  }
}

async function loadVoices() {
  const data = await call("/api/voices");
  const select = $("voice");
  const chosen = select.value;
  select.innerHTML = '<option value="">— загрузить новый —</option>';
  for (const v of data.voices) {
    const option = document.createElement("option");
    option.value = v.name;
    option.textContent = `${v.name} (образец ${v.seconds} с)`;
    select.appendChild(option);
  }
  select.value = chosen;
}

// Choosing a ready voice skips the upload entirely — the sample it needs
// already exists, which is the only thing step 2 requires.
$("voice").onchange = () => {
  const name = $("voice").value;
  $("upload").style.display = name ? "none" : "";
  if (name) { useVoice(name); }
  else { voice = ""; $("step2").classList.add("off"); $("step3").classList.add("off");
         $("sampleBox").style.display = "none"; }
};

function useVoice(name) {
  voice = name;
  $("step2").classList.remove("off");
  $("sampleAudio").src = "/sample/" + encodeURIComponent(name);
  $("sampleBox").style.display = "";
}

// ── 1. загрузка ───────────────────────────────────────────────────────
$("send").onclick = async () => {
  const name = $("name").value.trim();
  const file = $("file").files[0];
  if (!name)  return say($("uploadMsg"), "введите имя голоса", true);
  if (!file)  return say($("uploadMsg"), "выберите файл записи", true);

  const body = new FormData();
  body.append("name", name);
  body.append("song", $("song").checked ? "1" : "");
  body.append("file", file);

  $("send").disabled = true;
  say($("uploadMsg"), $("song").checked
    ? "Отделяю вокал и режу образец — это может занять минуты…"
    : "Загружаю и режу образец…");
  try {
    const data = await call("/api/upload", {method: "POST", body});
    say($("uploadMsg"),
        `Образец готов: ${data.seconds} с из записи в ${data.duration} с.`);
    await loadVoices();
    $("voice").value = data.name;
    useVoice(data.name);
  } catch (e) {
    say($("uploadMsg"), e.message, true);
  } finally {
    $("send").disabled = false;
  }
};

// ── 2. прослушивание ──────────────────────────────────────────────────
$("preview").onclick = async () => {
  const body = new FormData();
  body.append("name", voice);
  body.append("text", $("previewText").value);

  $("preview").disabled = true;
  $("previewBox").style.display = "none";
  say($("previewMsg"), "Синтезирую — обычно около 10–15 секунд…");
  try {
    const data = await call("/api/preview", {method: "POST", body});
    $("previewSaid").textContent = "«" + data.text + "»";
    $("previewAudio").src = data.url;
    $("previewBox").style.display = "";
    say($("previewMsg"), "");
    // Approval is the act of listening and moving on. A separate "yes" button
    // would only record a click; the batch below is the confirmation.
    $("step3").classList.remove("off");
  } catch (e) {
    say($("previewMsg"), e.message, true);
  } finally {
    $("preview").disabled = false;
  }
};

// ── 3. партия фраз ────────────────────────────────────────────────────
$("run").onclick = async () => {
  const body = new FormData();
  body.append("name", voice);
  body.append("game", $("game").checked ? "1" : "");
  body.append("texts", $("texts").value);

  $("run").disabled = true;
  $("results").innerHTML = "";
  $("bar").style.display = "";
  $("bar").firstElementChild.style.width = "0";
  say($("runMsg"), "Ставлю в очередь…");
  try {
    const started = await call("/api/generate", {method: "POST", body});
    await poll(started.token);
  } catch (e) {
    say($("runMsg"), e.message, true);
    $("bar").style.display = "none";
  } finally {
    $("run").disabled = false;
  }
};

async function poll(token) {
  for (;;) {
    const batch = await call("/api/batch/" + token);
    const percent = batch.total ? Math.round(100 * batch.done / batch.total) : 0;
    $("bar").firstElementChild.style.width = percent + "%";
    render(batch);

    if (batch.stage === "ready" || batch.stage === "failed") {
      say($("runMsg"), batch.error || `Готово: ${batch.total} фраз.`,
          batch.stage === "failed");
      return;
    }
    say($("runMsg"), `Сгенерировано ${batch.done} из ${batch.total}…`);
    await new Promise(r => setTimeout(r, 1500));
  }
}

// Rebuilt from scratch on each poll rather than patched. The list is at most
// a couple of dozen rows and replacing it is simpler than tracking which ones
// changed — except for the audio elements, which would restart mid-playback,
// so a row that already has its wav is left alone.
function render(batch) {
  const box = $("results");
  batch.items.forEach((item, index) => {
    let row = box.children[index];
    if (row && row.dataset.done === "1") return;
    if (!row) {
      row = document.createElement("div");
      row.className = "item";
      box.appendChild(row);
    }
    const text = item.text.replace(/[<>&]/g, c =>
      ({"<": "&lt;", ">": "&gt;", "&": "&amp;"}[c]));
    if (item.error) {
      row.innerHTML = `<p>«${text}»</p><div class="err">${item.error}</div>`;
      row.dataset.done = "1";
    } else if (item.url) {
      row.innerHTML = `<p>«${text}»</p>
        <audio controls preload="none" src="${item.url}"></audio>
        <div class="hint"><a href="${item.url}" download
           style="color:var(--accent)">скачать wav</a></div>`;
      row.dataset.done = "1";
    } else {
      row.innerHTML = `<p style="color:var(--dim)">«${text}»</p>`;
    }
  });
}

health();
loadVoices();
setInterval(health, 30000);
</script>
</html>
"""


def main() -> None:
    import argparse
    import uvicorn

    parser = argparse.ArgumentParser(
        prog="python -m tts.studio",
        description="Веб-студия клонирования голосов на машине с видеокартой.")
    parser.add_argument("--host", default="0.0.0.0",
                        help="какой адрес слушать (по умолчанию все)")
    parser.add_argument("--port", type=int, default=8970)
    args = parser.parse_args()

    INBOX.mkdir(parents=True, exist_ok=True)
    OUTBOX.mkdir(parents=True, exist_ok=True)
    engine.VOICES_DIR.mkdir(parents=True, exist_ok=True)

    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
