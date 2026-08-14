#!/usr/bin/env python3
"""
The web interface: watch the handsets, and ring one with a button.

    python3 scripts/web.py                 http://127.0.0.1:8080
    python3 scripts/web.py --port 9000

Two halves. The board shows every handset's current state and a live log,
driven by scripts/monitor.py over a server-sent event stream, so a keypress
appears in the browser as it happens rather than on the next refresh. The
button places a call through scripts/call.py, which clears the FXS port first —
the thing that makes a second call to the same handset work at all.

Calls run on a background thread. place() rings for up to 30 seconds and blocks
for the whole of it; doing that inside the request would hold the browser and,
worse, keep the single-threaded dev server from serving the very event stream
that shows the call progressing.

Bound to localhost. The AMI account it uses has originate rights, and the page
has no authentication of its own — anything that can load it can ring a phone.
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import re
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

ROOT = Path(__file__).resolve().parent.parent

from flask import Flask, Response, jsonify, request, send_from_directory  # noqa: E402

import admin    # noqa: E402
import audio    # noqa: E402
import call     # noqa: E402
import gateway  # noqa: E402
import health   # noqa: E402
import monitor  # noqa: E402
import sounds   # noqa: E402
import tones    # noqa: E402
import watchdog  # noqa: E402

HERE = Path(__file__).resolve().parent
STATIC = HERE / "web"

app = Flask(__name__, static_folder=None)
board = monitor.Monitor()

# Frees ports held by a fault rather than by a call. Off until asked for: it
# cycles ports on its own, and that should be a decision rather than a
# default. See scripts/watchdog.py for why the "busy with no channel" pair is
# safe to act on.
dog = watchdog.Watchdog(fix=False)

# One queue per connected browser. The monitor's callback runs on its reader
# thread and must not block there, so it drops into these and returns.
_clients: list[queue.Queue] = []
_clients_lock = threading.Lock()

# Calls in flight, keyed by extension, so the page can grey out a button that
# is already busy and report how the last attempt ended.
_calls: dict[str, dict] = {}
_calls_lock = threading.Lock()

# ── what is waiting to be heard ─────────────────────────────────────────
#
# The audio does not travel over SIP. The gateway's part in a call is to put
# ringing current on the line so the telephone's bells sound; it never
# answers, so no RTP stream is ever set up and there is nothing for Asterisk
# to play into — a call left to itself stays Ringing until the gateway gives
# up on it thirty seconds later.
#
# The sound reaches the earpiece through the mini jack instead, and the ESP
# is what says when to start it: it reads the hook switch directly, so the
# moment the receiver comes up it posts off-hook to /api/dialer. That event
# is the answer signal this system has.
#
# So a call is placed in two halves. Ringing the handset arms an entry here,
# naming the sound the operator picked; the off-hook that follows finds it
# and starts playback. An entry that is never claimed expires, because a
# handset nobody picked up must not play its sound into the next call.
_armed: dict[str, dict] = {}
_armed_lock = threading.Lock()

# How long an armed sound keeps waiting once the bells have stopped.
#
# Counted from the end of the ring rather than from the start of the call,
# which is the distinction that matters. The window used to be a flat 45
# seconds from the moment the call was placed, and a 30-second ring ate two
# thirds of it: the sound expired 15 seconds after the telephone went quiet.
# That is not long enough for the ordinary way a call is answered — someone
# hears the bells, walks to the telephone, and lifts the receiver after the
# ringing has stopped. Measured on 105: the receiver came up 47 seconds in,
# two seconds past a 45-second limit, and the sound had already been dropped.
#
# So this is the grace after the ring, and the whole window is the ring plus
# this. Generous on purpose: the cost of waiting too long is a sound armed
# for a handset nobody answered, which expires by itself and is harmless,
# while the cost of waiting too little is a telephone that is picked up and
# stays silent.
ARM_GRACE = 45.0


def _arm(extension: str, sound: sounds.Sound, loop: bool, ring: int) -> None:
    """Say which sound this handset should hear when it answers."""
    with _armed_lock:
        _armed[extension] = {"sound": sound.name, "path": str(sound.source),
                             "loop": loop, "at": time.time(),
                             "expires": time.time() + ring + ARM_GRACE}


def _disarm(extension: str, claiming: bool = False) -> dict | None:
    """Take the armed sound, if there is one and it is still good.

    claiming=True marks the one caller that is actually trying to play the
    sound — the off-hook. Only there does an expired entry mean something
    went wrong; the cleanup paths call this to throw a sound away on purpose
    and must not report that as a missed answer.
    """
    with _armed_lock:
        entry = _armed.pop(extension, None)
    if entry is None:
        return None
    if time.time() > entry["expires"]:
        # Reported rather than dropped in silence. An expired arm and a
        # handset that was never armed both come back as None here, and they
        # need different things done: this one means the sound was ready and
        # the receiver came up too late, which is the difference between a
        # broken audio path and a person who took a minute to answer.
        if claiming:
            waited = time.time() - entry["at"]
            # The limit as this particular call was armed with it, not the
            # constant: the window is the ring plus the grace, so a call that
            # rang for longer waited for longer, and quoting a fixed number
            # would name a deadline that was never the one applied.
            limit = entry["expires"] - entry["at"]
            _fan_out(monitor.Event(
                "warn", extension,
                f"трубку сняли через {waited:.0f} с — {entry['sound']} уже "
                f"не проигрывается (лимит {limit:.0f} с)",
                direction="inbound"))
        return None
    return entry

# The AP1100F allows very few telnet sessions and refuses new logins once they
# are used up — which looks exactly like the gateway having died. Every path
# that opens one takes this first, so a page open in three browsers cannot
# exhaust them by polling.
_gateway_lock = threading.Lock()

# The port summary costs a telnet login, about a second. Cached so the status
# panel can be asked for often without hammering the gateway; a call or a
# reset invalidates it, because those are the moments it changes.
_ports_cache: dict = {"at": 0.0, "value": None}
PORTS_TTL = 6.0

# Set while the gateway is rebooting or being reset, so the page can show it
# and every other gateway call can refuse rather than pile onto a device that
# is halfway through coming back.
_maintenance: dict = {"busy": False, "what": "", "started": 0.0, "detail": ""}

# ── progress of long operations ─────────────────────────────────────────
#
# A call takes half a minute, a reboot takes two, and both spend most of that
# time with nothing to show. Without a breakdown the interface can only say
# "working", which is indistinguishable from "hung". So each such operation
# declares its steps up front and marks them off as it goes; the page draws
# the list and the bar from that.

# done starts true: nothing has run yet, and "not done" on an empty step list
# reads to the page as an operation in progress that it can never finish.
_progress: dict = {"id": "", "title": "", "steps": [], "done": True,
                   "ok": True, "started": 0.0}
_progress_lock = threading.Lock()


def _progress_start(job_id: str, title: str, steps: list[str]) -> None:
    with _progress_lock:
        _progress.update(
            id=job_id, title=title, done=False, ok=True, started=time.time(),
            steps=[{"label": s, "state": "waiting", "detail": ""} for s in steps],
        )
    _push_progress()


def _progress_step(index: int, state: str, detail: str = "") -> None:
    """Mark one step. state: running | ok | fail | skip."""
    with _progress_lock:
        if 0 <= index < len(_progress["steps"]):
            _progress["steps"][index].update(state=state, detail=detail)
    _push_progress()


def _progress_finish(ok: bool, detail: str = "") -> None:
    with _progress_lock:
        _progress.update(done=True, ok=ok)
        for step in _progress["steps"]:
            # Anything still waiting when the job ends never ran; leaving it
            # as "waiting" would read as though it were still to come.
            if step["state"] in ("waiting", "running"):
                step["state"] = "skip" if ok else "fail"
        if detail:
            _progress["detail"] = detail
    _push_progress()


def _push_progress() -> None:
    """Send the current progress to every open page."""
    with _progress_lock:
        snapshot = {
            "id": _progress["id"], "title": _progress["title"],
            "done": _progress["done"], "ok": _progress["ok"],
            "steps": [dict(s) for s in _progress["steps"]],
        }
    payload = {"kind": "progress", "extension": "-", "detail": "",
               "at": time.time(), "clock": "", "direction": "",
               "progress": snapshot}
    with _clients_lock:
        listeners = list(_clients)
    for client in listeners:
        try:
            client.put_nowait(payload)
        except queue.Full:
            pass


def _ports(force: bool = False) -> dict:
    """The FXS port summary, from cache unless it is stale."""
    now = time.time()
    if not force and _ports_cache["value"] is not None \
            and now - _ports_cache["at"] < PORTS_TTL:
        return _ports_cache["value"]
    with _gateway_lock:
        result = health.check_ports()
    _ports_cache.update(at=now, value=result)
    return result


def _invalidate_ports() -> None:
    _ports_cache.update(at=0.0, value=None)


# What each kind is called in the console. The page has its own labels; this
# is the same information for someone watching the server rather than a
# browser, which is where a handset is usually watched from while the wiring
# is still being worked on.
_LOG_LABEL = {
    "off-hook": "трубка снята",
    "on-hook": "трубка положена",
    # The call attempt ending, which is the gateway giving up rather than
    # anyone hanging up — the hook is only ever reported by the ESP.
    "call-ended": "вызов завершён",
    "digit": "цифра",
    "number": "набран номер",
    "ringing": "звонит",
    "error": "ошибка",
    "warn": "внимание",
    "info": "",
}


def _log_line(event: monitor.Event) -> None:
    """One event, on the terminal the server was started from.

    Every event reaches the page through the stream below, but only if a
    browser is open. The console is the one place that shows a handset
    lifting its receiver whether or not anyone is looking at the interface —
    which is what makes it the thing to watch while a reader is being wired
    up.
    """
    label = _LOG_LABEL.get(event.kind, event.kind)
    who = event.extension if event.extension and event.extension != "-" else "—"
    detail = event.detail or ""
    # The detail often already says what the label would: a hook event's
    # detail is the words "трубка снята". Repeating it reads as a stutter, so
    # the label is only added when it tells you something the detail does not.
    if not label:
        what = detail
    elif not detail or detail == label:
        what = label
    else:
        what = f"{label} {detail}"
    print(f"{event.clock}  {who:>4}  {what}", flush=True)


def _fan_out(event: monitor.Event) -> None:
    payload = event.as_dict()
    _log_line(event)
    with _clients_lock:
        listeners = list(_clients)
    for client in listeners:
        try:
            client.put_nowait(payload)
        except queue.Full:
            # A browser that stopped reading. Dropping its events is better
            # than growing without bound; it resyncs from /api/state when it
            # reconnects.
            pass


board.subscribe(_fan_out)


# ── de-energising a line whose handset cannot signal on-hook ────────────
#
# The TX-220 on 106 has blown line switches: the loop stays closed after the
# receiver goes down, so the gateway sees no difference between a lifted
# handset and a resting one. No amount of polling the port can tell them
# apart — the two produce the same reading.
#
# Asterisk does know. The channel ends, and a Hangup event says so within a
# second. That is the signal the gateway cannot give, so it is the one used:
# on hang-up, cut power to the line for long enough that the short reads open
# and the port settles Idle by itself.
#
# Ports opted in by the operator, because it costs the line several seconds
# of being dead after every call and only a faulty handset needs it.
_auto_power: set[str] = set()
_auto_power_lock = threading.Lock()

# Long enough for a shorted loop to read open; measured on 106, where a
# one-second cycle was followed by the port going busy again within thirty
# seconds and a six-second one held for over forty.
AUTO_POWER_SECONDS = 6.0


def _auto_power_cycle(extension: str) -> None:
    """De-energise one line after its call ended."""
    # A moment for Asterisk to finish tearing the channel down. Cycling while
    # it is still closing leaves the port in Disconnecting rather than Idle.
    time.sleep(2.0)
    try:
        with _gateway_lock:
            status = gateway.power_cycle_extension(
                extension, down_seconds=AUTO_POWER_SECONDS)
        dog.touched(gateway.port_for(extension))
        _invalidate_ports()
        _fan_out(monitor.Event(
            "info", extension,
            f"линия обесточена после отбоя, порт {status}"))
    except gateway.GatewayError as exc:
        _fan_out(monitor.Event("error", extension,
                               f"не удалось обесточить линию: {exc}"))


def _on_handset_event(event: monitor.Event) -> None:
    """Cut power to a line the moment Asterisk says its call ended."""
    if event.kind != "on-hook":
        return
    with _auto_power_lock:
        wanted = event.extension in _auto_power
    if not wanted:
        return
    threading.Thread(target=_auto_power_cycle, args=(event.extension,),
                     name=f"autopower-{event.extension}", daemon=True).start()


board.subscribe(_on_handset_event)


# ── when a sound ends by itself ─────────────────────────────────────────

def _on_audio_finish(extension: str, sound: str, reason: str) -> None:
    """A file played to its end, or stopped.

    Nothing else notices: the audio runs through the jack rather than through
    the call, so the handset would stay marked busy in the interface with
    silence coming out of it.
    """
    # on-hook already recorded the ending and said so; repeating it here would
    # log the same moment twice.
    if reason == "on-hook":
        return

    # A call-progress tone ending is not a sound finishing. Dial tone gives
    # way to the first digit ("dialled") and the busy tone runs out
    # ("expired"); neither is a call, neither was ever marked busy, and
    # reporting them here would log "воспроизведение окончено" for a noise
    # nobody asked for — and, worse, clear the busy flag of whatever real
    # call happens to be running on that handset.
    if reason in ("dialled", "expired"):
        return
    with _calls_lock:
        _calls.setdefault(extension, {}).update(
            busy=False, ok=True, detail=f"{sound} проигран",
            finished=time.time())
    _fan_out(monitor.Event("info", extension, f"{sound}: воспроизведение окончено",
                           direction="outbound"))
    _progress_step(3, "ok", sound)
    _progress_finish(True, f"{sound} проигран")


audio.player.on_finish = _on_audio_finish


# ── pages ───────────────────────────────────────────────────────────────

def _no_store(response: Response) -> Response:
    """Serve the page and its assets without letting them be cached.

    Flask's default validators let a browser keep its copy and revalidate
    with a 304, which is right for a CDN and wrong here: this interface is
    edited while it is running, and a stale app.js reads as a bug in the
    interface rather than as an old file — a banner left on screen with the
    operation long finished, controls that do nothing. Correctness beats the
    few kilobytes.
    """
    response.headers["Cache-Control"] = "no-store, must-revalidate"
    response.headers.pop("ETag", None)
    response.headers.pop("Last-Modified", None)
    return response


@app.get("/")
def index() -> Response:
    return _no_store(send_from_directory(STATIC, "index.html"))


@app.get("/app.js")
def script() -> Response:
    return _no_store(send_from_directory(STATIC, "app.js"))


@app.get("/style.css")
def stylesheet() -> Response:
    return _no_store(send_from_directory(STATIC, "style.css"))


# ── state ───────────────────────────────────────────────────────────────

@app.get("/api/state")
def state() -> Response:
    """Everything the page needs to draw itself from cold."""
    snapshot = board.snapshot()
    with _calls_lock:
        snapshot["calls"] = {k: dict(v) for k, v in _calls.items()}
    # The audio is the half of a call that does not travel over SIP, so it has
    # to be reported separately or the page shows a call with no sound in it.
    snapshot["audio"] = audio.player.current()
    with _armed_lock:
        snapshot["armed"] = {k: dict(v) for k, v in _armed.items()}
    with _progress_lock:
        snapshot["progress"] = {
            "id": _progress["id"], "title": _progress["title"],
            "done": _progress["done"], "ok": _progress["ok"],
            "steps": [dict(s) for s in _progress["steps"]],
        }
    return jsonify(snapshot)


# ── the audio path, which is a cable rather than the call ───────────────

@app.get("/api/audio")
def audio_state() -> Response:
    """What is coming out of the jack, and where it is going.

    Worth its own endpoint because this is the part of the path that carries
    the sound, and nothing about it shows up in the SIP state the rest of the
    interface reports: a call can look perfect and be silent because the plug
    is out.
    """
    with _armed_lock:
        armed = {k: dict(v) for k, v in _armed.items()}
    return jsonify({
        "playing": audio.player.current(),
        "device": audio.output_device(),
        "armed": armed,
    })


@app.post("/api/audio/play")
def audio_play() -> Response:
    """Play a sound into a handset now, without ringing it.

    For a receiver already off the hook — testing the cable, or playing
    something to someone who is holding the telephone.
    """
    body = request.get_json(silent=True) or {}
    extension = str(body.get("extension", "")).strip()
    choice = str(body.get("sound", "")).strip()
    loop = bool(body.get("loop", False))

    try:
        gateway.port_for(extension)
    except gateway.GatewayError as exc:
        return jsonify({"error": str(exc)}), 400
    try:
        sound = sounds.resolve(choice)
    except sounds.SoundError as exc:
        return jsonify({"error": str(exc)}), 400

    try:
        playing = audio.player.start(extension, sound.name, sound.source,
                                     loop=loop)
    except audio.AudioError as exc:
        return jsonify({"error": str(exc)}), 500

    with _calls_lock:
        _calls.setdefault(extension, {}).update(
            busy=True, sound=sound.name, detail="играет в трубку",
            started=time.time())
    _fan_out(monitor.Event("info", extension,
                           f"играет {sound.name} (без вызова)",
                           direction="outbound"))
    return jsonify({"ok": True, "playing": playing.as_dict()})


@app.post("/api/audio/stop")
def audio_stop() -> Response:
    """Silence the earpiece."""
    body = request.get_json(silent=True) or {}
    extension = str(body.get("extension", "")).strip() or None
    if extension is not None:
        try:
            gateway.port_for(extension)
        except gateway.GatewayError as exc:
            return jsonify({"error": str(exc)}), 400

    stopped = audio.player.stop(extension, reason="stopped")
    if stopped and extension:
        with _calls_lock:
            _calls.setdefault(extension, {}).update(
                busy=False, detail="остановлено", finished=time.time())
        _fan_out(monitor.Event("info", extension,
                               "воспроизведение остановлено из интерфейса"))
    return jsonify({"ok": True, "stopped": stopped})


@app.get("/api/sounds")
def sound_list() -> Response:
    try:
        library = sounds.library()
    except sounds.SoundError as exc:
        return jsonify({"error": str(exc), "sounds": []}), 500
    return jsonify({"sounds": [
        {"name": s.name, "seconds": round(s.seconds, 1)} for s in library.values()
    ]})


@app.get("/api/health")
def system_health() -> Response:
    """Every part of the path, and the FXS ports.

    The cheap checks run on every request; the port summary comes from the
    cache, because it costs a telnet login and the panel is polled.
    """
    checks = health.fast()
    ports = _ports()
    checks.append(ports)
    return jsonify({
        "checks": checks,
        "overall": health.overall(checks),
        "ports": ports.get("ports", []),
        "maintenance": dict(_maintenance),
        "at": time.time(),
    })


@app.get("/api/ports")
def ports() -> Response:
    """The gateway's own view of the FXS ports.

    Distinct from the handset states on the board: those come from call
    events, this is what the hardware reports, and a port stuck in
    "Disconnecting" shows here while the board still calls the line idle.
    """
    result = _ports(force=request.args.get("fresh") == "1")
    if result["state"] == health.DOWN:
        return jsonify({"error": result["detail"], "ports": []}), 502
    return jsonify({"ports": result.get("ports", [])})


@app.get("/api/events")
def events() -> Response:
    """Server-sent events: one JSON object per handset event."""
    client: queue.Queue = queue.Queue(maxsize=200)
    with _clients_lock:
        _clients.append(client)

    def stream():
        try:
            yield "retry: 2000\n\n"
            while True:
                try:
                    payload = client.get(timeout=15)
                except queue.Empty:
                    # A comment frame. Without traffic, an idle connection is
                    # dropped by the browser or an intermediary and the page
                    # goes quiet without saying so.
                    yield ": keepalive\n\n"
                    continue
                yield f"data: {json.dumps(payload)}\n\n"
        finally:
            with _clients_lock:
                if client in _clients:
                    _clients.remove(client)

    return Response(stream(), mimetype="text/event-stream", headers={
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
        "Connection": "keep-alive",
    })


# ── placing a call ──────────────────────────────────────────────────────

def _run_call(extension: str, sound: sounds.Sound, loop: bool, ring: int) -> None:
    def record(**fields) -> None:
        with _calls_lock:
            _calls.setdefault(extension, {}).update(fields)

    _progress_start(f"call-{extension}", f"Вызов на {extension}", [
        "Освобождение порта FXS",
        "Звонок на аппарат",
        "Ожидание снятия трубки (по датчику ESP)",
        "Воспроизведение звука в трубку",
    ])
    try:
        # place() clears the port over telnet before it rings, so it needs the
        # gateway to itself for that stretch — the same session budget the
        # status panel draws from.
        _progress_step(0, "running")
        with _gateway_lock:
            # On a line a fault keeps shorted, the one-second clear that
            # place() does is not enough: measured on 106, the port goes busy
            # again about fifteen seconds after being freed, so a call placed
            # even ten seconds later finds it taken. De-energising here, in
            # the same breath as the originate, means the line is free at the
            # moment the INVITE arrives rather than some seconds earlier.
            with _auto_power_lock:
                shorted = extension in _auto_power
            if shorted:
                try:
                    gateway.power_cycle_extension(
                        extension, down_seconds=AUTO_POWER_SECONDS)
                    dog.touched(gateway.port_for(extension))
                except gateway.GatewayError:
                    # Not fatal on its own: place() clears the port too, and
                    # the call may still get through.
                    pass
            _progress_step(0, "ok")
            # Declared before the call goes out: the trunk channel appears
            # within milliseconds of the originate and carries nothing that
            # names the handset it is ringing.
            board.expect(extension)
            dog.touched(gateway.port_for(extension))
            _progress_step(1, "running")
            # Armed before the bells start, not after: the receiver can come
            # up within a second of the first ring, and an off-hook arriving
            # before the sound was named would find nothing to play.
            _arm(extension, sound, loop, ring)
            result = call.place(extension, sound, loop=loop, ring_seconds=ring,
                                verbose=False, prepared=shorted)

        # place() returns when the gateway stops ringing. It never answers —
        # it has no way to tell Asterisk the receiver came up — so a call that
        # rang correctly still comes back here as a failure, and the SIP
        # outcome says nothing about whether anyone picked up. What did happen
        # is that the bells rang, so that is what is reported.
        _progress_step(1, "ok", "аппарат звонил")
        _progress_step(2, "running", "ждём датчик трубки")

        # Whether the ESP has already reported the receiver up. If it has,
        # playback started while the gateway was still ringing and the call
        # is done; if not, the arming above is still waiting for it.
        playing = audio.player.is_playing(extension)
        if playing:
            _progress_step(2, "ok", "трубка снята")
            _progress_step(3, "ok", sound.name)
            _progress_finish(True, f"играет {sound.name}")
            detail = f"играет {sound.name}"
        else:
            still_armed = False
            with _armed_lock:
                still_armed = extension in _armed
            if still_armed:
                # The bells have stopped, but the sound stays armed. This is
                # the ordinary case rather than a failure: the gateway gives
                # up on the INVITE about thirty seconds in and place() returns
                # then, while the person is still walking to the telephone.
                # The ESP is what decides the call was answered, and it can
                # say so seconds after the ringing stopped — disarming here
                # would throw the sound away just before it was asked for.
                #
                # The entry expires on its own (see _arm), so nothing is left
                # behind for the next call to trip over.
                _progress_step(2, "running", "звонок закончился, ждём трубку")
                detail = "звонок прошёл, ждём снятия трубки"
            else:
                # Claimed and already finished: a short sound that played out
                # while the gateway was still ringing.
                _progress_step(2, "ok", "трубка снята")
                _progress_step(3, "ok", sound.name)
                _progress_finish(True, f"{sound.name} проигран")
                detail = f"{sound.name} проигран"

        _invalidate_ports()
        record(busy=playing, ok=playing or "проигран" in detail,
               detail=detail, finished=time.time())
        _fan_out(monitor.Event(
            "info", extension, f"вызов: {detail}", direction="outbound",
        ))
    except call.CallError as exc:
        # The armed sound goes with the failed call. Left behind, it would be
        # played to whoever next lifts that receiver.
        _disarm(extension)
        _progress_finish(False, str(exc))
        record(busy=False, ok=False, detail=str(exc), finished=time.time())
        _fan_out(monitor.Event("error", extension, f"вызов не удался: {exc}",
                               direction="outbound"))
    except Exception as exc:                                   # noqa: BLE001
        _disarm(extension)
        _progress_finish(False, str(exc))
        record(busy=False, ok=False, detail=str(exc), finished=time.time())
        _fan_out(monitor.Event("error", extension, f"вызов не удался: {exc}",
                               direction="outbound"))


@app.post("/api/call")
def place_call() -> Response:
    body = request.get_json(silent=True) or {}
    extension = str(body.get("extension", "")).strip()
    choice = str(body.get("sound", "")).strip()
    loop = bool(body.get("loop", False))
    ring = int(body.get("ring", call.RING_SECONDS))

    try:
        gateway.port_for(extension)             # rejects anything not 101-108
    except gateway.GatewayError as exc:
        return jsonify({"error": str(exc)}), 400

    try:
        sound = sounds.resolve(choice) if choice else None
    except sounds.SoundError as exc:
        return jsonify({"error": str(exc)}), 400
    if sound is None:
        return jsonify({"error": "выберите звук для воспроизведения"}), 400

    if _maintenance["busy"]:
        return jsonify({"error": "шлюз занят обслуживанием"}), 409

    with _calls_lock:
        if _calls.get(extension, {}).get("busy"):
            return jsonify({"error": f"на {extension} уже идёт вызов"}), 409
        _calls[extension] = {"busy": True, "sound": sound.name,
                             "started": time.time(), "detail": "освобождение порта"}

    _fan_out(monitor.Event("info", extension,
                           f"вызов, будет воспроизведено: {sound.name}",
                           direction="outbound"))

    threading.Thread(target=_run_call, args=(extension, sound, loop, ring),
                     name=f"call-{extension}", daemon=True).start()
    return jsonify({"ok": True, "extension": extension, "sound": sound.name})


# ── the rotary dial reader ─────────────────────────────────────────────
#
# An ESP32 wired to a TA-1132's hook switch and impulse contact posts here:
# the receiver lifted or put down, each digit as the disc returns, and the
# complete number once enough digits have been dialled. The firmware is in
# esp/.
#
# The telephone's own line stays on its FXS port, so its bell still rings
# for an outbound call placed from the page. What this adds is the other
# direction — the handset asking for a sound — on a set whose dial is not
# wired to the gateway at all.
#
# The ESP counts pulses and assembles digits; nothing here re-derives that.
# This decides what a finished number means.

# Shared with the firmware's DIALER_TOKEN. Set it in the environment to
# require it: an empty value accepts unauthenticated posts, which is only
# reasonable on a network where nothing else can reach this port.
DIALER_TOKEN = os.environ.get("DIALER_TOKEN", "")

# The kinds the firmware sends, and the ones monitor.Event already uses for
# the same things when they come from the gateway instead.
DIALER_KINDS = ("off-hook", "on-hook", "digit", "number")

# How long the ringback plays before the sound starts, for a number dialled
# from the handset.
#
# Nothing is being rung, so this is not a wait for anything — it is the part
# of the call that makes it sound like one. Two cadences of КПВ: the tone is
# one second on and four off, so this is heard as "ring ... ring ..." and
# then the answer, which is about how long a call to someone who is next to
# their telephone takes to be picked up. Longer would be more realistic and
# worse: the caller is standing with the receiver at their ear and has
# already dialled, so every second past the point where it reads as a
# connecting call is a second of nothing happening.
RINGBACK_SECONDS = 10.0

# How long the busy tone answers a number that cannot be played.
#
# Capped, unlike the dial tone: a receiver left off the hook after a wrong
# number would otherwise carry СИП until the process died, and unlike dial
# tone — which is a line waiting, and sounds like one — that is a noise. Long
# enough to be unmistakably the refusal signal and not a glitch, short enough
# that the handset goes quiet on its own if it is put down on the table.
BUSY_SECONDS = 8.0


def _stop_ringing(extension: str) -> list[str]:
    """End the call that is ringing this handset, and say which channels went.

    The receiver coming up is an answer, but the gateway cannot see it: the
    dial and the hook are read by the ESP, and the FXS loop the gateway
    watches is never closed by them. So Asterisk goes on believing nobody has
    picked up — it keeps the INVITE alive, the gateway keeps ringing the
    bells, and about thirty seconds later Asterisk gives up and CANCELs. That
    is the telephone that rings in the hand of the person already holding it.

    Hanging the channel up here is what the answer would have done. It stops
    the ringing current at once, which matters for more than the noise: the
    bells ringing into a lifted receiver is what disturbs the hook contact
    and produces the spurious on-hook readings that throw an armed sound
    away.

    Failure is not raised. This runs on the path that starts the sound, and a
    manager that cannot be reached is a reason to leave the bells ringing, not
    a reason to leave the handset silent.
    """
    targets = board.channels_of(extension)
    if not targets:
        return []

    try:
        with call.Manager() as ami:
            live = {c.get("channel", "") for c in ami.channels()}
            targets = [t for t in targets if t in live]
            for channel in targets:
                ami.hangup(channel)
    except call.CallError as exc:
        _fan_out(monitor.Event("warn", extension,
                               f"не удалось остановить звонок: {exc}",
                               direction="inbound"))
        return []

    if targets:
        _fan_out(monitor.Event("info", extension, "звонок остановлен по снятию трубки",
                               direction="inbound"))
    return targets


def _stop_tone(extension: str) -> bool:
    """Silence a call-progress tone on this handset, leaving a sound alone."""
    try:
        return audio.player.stop_tone(extension)
    except Exception:                                          # noqa: BLE001
        # This runs on the digit path, which must stay fast and must not fail
        # a report over the audio. A tone left playing is audible and wrong;
        # a digit lost is a number that never completes.
        return False


def _refuse(extension: str, number: str, message: str, status: int) -> Response:
    """Answer a dialled number that cannot be played, in the earpiece.

    The caller is holding the receiver and hears whatever this end does. An
    HTTP error reaches the ESP, which has nowhere to put it — the firmware
    posts and moves on — so a refusal that is only a status code is, from the
    handset, a number dialled into silence. That is the same thing a working
    number sounds like before the ringback starts, and the caller waits
    through it for a sound that is never coming.

    So the refusal is a tone, which is what an exchange answers an impossible
    number with: СИП, the busy pattern, capped so it does not run for as long
    as the receiver stays up.

    The status and the message still go back, for the log and for anything
    calling this endpoint that is not the ESP.
    """
    try:
        audio.player.start_tone(extension, tones.busy(), "занято",
                                seconds=BUSY_SECONDS)
    except (audio.AudioError, OSError) as exc:
        _fan_out(monitor.Event("warn", extension,
                               f"не удалось дать сигнал занято: {exc}",
                               direction="inbound"))

    _fan_out(monitor.Event("warn", extension, f"набран {number}: {message}",
                           direction="inbound"))
    return jsonify({"error": message, "tone": "busy"}), status


@app.post("/api/dialer")
def dialer() -> Response:
    """One event from a handset's dial reader."""
    if DIALER_TOKEN and request.headers.get("X-Dialer-Token", "") != DIALER_TOKEN:
        return jsonify({"error": "неверный токен"}), 403

    body = request.get_json(silent=True) or {}
    extension = str(body.get("extension", "")).strip()
    kind = str(body.get("kind", "")).strip()
    detail = str(body.get("detail", "")).strip()

    try:
        gateway.port_for(extension)             # rejects anything not 101-108
    except gateway.GatewayError as exc:
        return jsonify({"error": str(exc)}), 400

    if kind not in DIALER_KINDS:
        return jsonify({"error": f"неизвестное событие: {kind!r}"}), 400

    # The reader is the only thing that knows this handset's state, so every
    # event it sends goes to the page whether or not it starts a call.
    if kind == "off-hook":
        # The answer signal. Nothing else in the system produces one: the
        # gateway rings the line but never reports the receiver coming up, so
        # this is the moment a call becomes answered, and the moment the sound
        # has to start.
        _fan_out(monitor.Event("off-hook", extension, "трубка снята",
                               direction="inbound"))

        # First, before anything that can be slow or can fail: the bells stop
        # the moment the receiver moves. Done here rather than after the sound
        # starts because the ringing is what corrupts the hook reading, and a
        # sound that fails to start is still a call that should not go on
        # ringing at a telephone somebody is holding.
        stopped = _stop_ringing(extension)

        armed = _disarm(extension, claiming=True)
        if armed is None:
            # Lifted with nothing waiting — someone picked up a telephone that
            # was not ringing. There is nothing to play, but there is
            # something to say: a receiver lifted at a working exchange is
            # answered by dial tone, and the caller is holding this one to
            # dial with. Silence would be indistinguishable from a dead line,
            # which is the state this system spent its history actually
            # being in.
            #
            # Uncapped. The tone holds until the first digit replaces it or
            # the receiver goes down, which is what an exchange does.
            try:
                audio.player.start_tone(extension, tones.dial(), "гудок")
            except (audio.AudioError, OSError) as exc:
                # Not an error the caller can act on, and not worth failing
                # the request over: the hook was still reported, which is
                # what this endpoint is for.
                _fan_out(monitor.Event("warn", extension,
                                       f"не удалось дать гудок: {exc}",
                                       direction="inbound"))
                return jsonify({"ok": True, "playing": None, "stopped": stopped})

            _fan_out(monitor.Event("info", extension, "гудок — можно набирать",
                                   direction="inbound"))
            return jsonify({"ok": True, "playing": None, "stopped": stopped,
                            "tone": "dial"})

        try:
            playing = audio.player.start(extension, armed["sound"],
                                         Path(armed["path"]),
                                         loop=bool(armed["loop"]))
        except audio.AudioError as exc:
            with _calls_lock:
                _calls.setdefault(extension, {}).update(
                    busy=False, ok=False, detail=str(exc), finished=time.time())
            _fan_out(monitor.Event("error", extension,
                                   f"звук не пошёл: {exc}", direction="inbound"))
            return jsonify({"error": str(exc)}), 500

        with _calls_lock:
            _calls.setdefault(extension, {}).update(
                busy=True, sound=armed["sound"], detail="играет в трубку")
        _progress_step(2, "ok", "трубка снята")
        _progress_step(3, "running", armed["sound"])
        _fan_out(monitor.Event(
            "info", extension,
            f"трубка снята — играет {armed['sound']}", direction="inbound"))
        return jsonify({"ok": True, "playing": playing.sound, "stopped": stopped})

    if kind == "on-hook":
        _fan_out(monitor.Event("on-hook", extension, "трубка положена",
                               direction="inbound"))

        # The receiver is down, so the earpiece is against nobody's ear. The
        # sound has to stop here: it plays through the jack rather than down
        # the line, so nothing else in the path would ever end it, and a
        # looping file would play into an empty room until the process died.
        playing_stopped = audio.player.stop(extension, reason="on-hook")
        if playing_stopped:
            with _calls_lock:
                _calls.setdefault(extension, {}).update(
                    busy=False, detail="трубка положена", finished=time.time())
            _fan_out(monitor.Event("info", extension, "воспроизведение остановлено",
                                   direction="inbound"))

        # A sound armed but never claimed. Whether that is worth throwing away
        # depends on what the receiver was doing.
        #
        # If something was playing, this on-hook ended it: the receiver was up
        # and has come down, so an arm still sitting there is stale and goes.
        #
        # If nothing was playing, the receiver was never up — and an on-hook
        # from a receiver already resting is not a hang-up, it is a repeated
        # reading of a telephone that has not moved. The bells are ringing
        # while this arrives, and ringing current on the line is enough to
        # disturb the hook contact, so the ESP reports a transition the
        # handset never made. Dropping the sound on that reading is what left
        # a call armed at 18:30:32 silent when the receiver came up at
        # 18:31:16: the spurious on-hook twelve seconds earlier had already
        # thrown the sound away.
        #
        # So the arm is kept. It expires on its own if nobody answers, which
        # is the case this branch was guarding against anyway.
        if playing_stopped:
            _disarm(extension)

        # Ending the call from this end as well: the dial is not wired to the
        # gateway, so putting the receiver down opens no loop the gateway can
        # see, and a sound already playing would run to its end.
        #
        # Guarded by the same reading as the arm above. A call is "busy" from
        # the moment it is placed, so an on-hook arriving while the bells are
        # ringing would clear the port out from under a call that is still
        # trying to reach the handset — the spurious reading killing the very
        # call it was mistaken for the end of. Only a receiver that was
        # actually up can be putting one down.
        with _calls_lock:
            busy = _calls.get(extension, {}).get("busy", False)
        if busy and playing_stopped and not _maintenance["busy"]:
            try:
                with _gateway_lock:
                    gateway.clear_extension(extension)
                _invalidate_ports()
                dog.touched(gateway.port_for(extension))
            except gateway.GatewayError as exc:
                _fan_out(monitor.Event("warn", extension,
                                       f"не удалось освободить порт: {exc}"))
        return jsonify({"ok": True})

    if kind == "digit":
        # The dial tone goes at the first digit, as it does on a real
        # exchange: the tone means "waiting for a number" and a number is now
        # being given. Left running it would play under the whole of the
        # dialling and then under the ringback, since the two are different
        # files and nothing else would stop it.
        #
        # Only the tone. A sound already playing is left alone — dialling
        # into a handset that is playing something is the caller asking for
        # the next thing, and cutting the current one off before the new
        # number is even complete would silence a sound over a digit that
        # may never become a number.
        _stop_tone(extension)
        _fan_out(monitor.Event("digit", extension, detail, direction="inbound"))
        return jsonify({"ok": True})

    # kind == "number": a complete number, which is a request for a sound.
    #
    # Three ways it can fail, and the caller hears the same thing for all
    # three, because from the earpiece they are one thing: the number does
    # not play anything. Which of the three it was belongs in the log, where
    # the operator can act on it.
    if not _slot_allowed(detail):
        return _refuse(extension, detail,
                       f"номер {detail} не существует "
                       f"(доступны {SLOT_FIRST}–{SLOT_LAST})", 400)

    # Which sound the number plays is the same setting the page programmes,
    # read from the same place, so a number added or changed there takes
    # effect on the dial without anything being reloaded.
    assigned = _slots_assigned().get(detail, "")
    if not assigned:
        return _refuse(extension, detail,
                       f"на номер {detail} не назначен звук", 404)

    try:
        sound = sounds.resolve(assigned)
    except sounds.SoundError as exc:
        # Programmed, but the file behind it has gone or will not convert.
        # The caller gets the same refusal: the number is assigned, and from
        # the handset an assignment that cannot be played is a number that
        # does not work.
        return _refuse(extension, detail, str(exc), 400)

    # Dialled from the handset, so the receiver is already up and the sound
    # goes straight out of the jack. Nothing here touches the gateway.
    #
    # This is the opposite direction from the page's button, and it must not
    # take the path that one does. _run_call() rings the handset: it clears
    # the FXS port and originates, which is what a call *to* a resting
    # telephone needs. Sent to a receiver that is already lifted it does the
    # one thing that breaks the call — it de-energises the line the caller is
    # holding — and then waits for an off-hook that cannot arrive, because
    # the receiver never went down to come back up. So the gateway is left
    # alone entirely: nothing is dialled anywhere, the audio path is the
    # cable, and this end plays into it.
    #
    # What the caller hears is what they would hear from a real exchange:
    # ringing while the call is put through, then the thing they dialled.
    # There is no far end to ring, so the ringback is generated (scripts/
    # tones.py) and its length is the only part of this that is a decision
    # rather than a consequence — long enough to sound like a call being
    # connected, short enough not to be a wait.
    # A call already running on this handset. Dialling over one is the caller
    # asking for the next thing, which is allowed — the sound is replaced, as
    # it is anywhere else — but a call the *page* placed is not this caller's
    # to take over, and neither is one still ringing the bells.
    #
    # Checked outside the refusal so the lock is not held across it: _refuse
    # plays a tone and fans out an event, and both can take long enough that
    # holding the calls lock through them would stall every page reading it.
    with _calls_lock:
        current = dict(_calls.get(extension, {}))
    if current.get("busy") and not audio.player.is_playing(extension):
        # Busy with something that is not coming out of the earpiece: the
        # page placed a call and the gateway is ringing. Refusing keeps the
        # dial from cutting across it.
        return _refuse(extension, detail,
                       f"на {extension} уже идёт вызов", 409)

    with _calls_lock:
        _calls[extension] = {"busy": True, "sound": sound.name,
                             "started": time.time(), "detail": "идут гудки"}

    _fan_out(monitor.Event("info", extension,
                           f"набран {detail}: гудки, затем {sound.name}",
                           direction="inbound"))

    def answered(ext: str, name: str) -> None:
        """The ringing gave way to the sound."""
        with _calls_lock:
            _calls.setdefault(ext, {}).update(detail=f"играет {name}")
        _fan_out(monitor.Event("info", ext, f"соединено — играет {name}",
                               direction="inbound"))

    try:
        audio.player.start_sequence(extension, tones.ringback(), RINGBACK_SECONDS,
                                    sound.name, sound.source, loop=False,
                                    on_answer=answered)
    except (audio.AudioError, OSError) as exc:
        with _calls_lock:
            _calls.setdefault(extension, {}).update(
                busy=False, ok=False, detail=str(exc), finished=time.time())
        # The busy tone here as well: the caller dialled a number that is
        # programmed and correct, and this end could not play it. Nothing
        # about that is theirs to fix, but a refusal they can hear beats a
        # receiver that stays silent.
        return _refuse(extension, detail, f"звук не пошёл: {exc}", 500)

    return jsonify({"ok": True, "extension": extension, "number": detail,
                    "sound": sound.name, "ringback": RINGBACK_SECONDS})


# ── gateway administration ──────────────────────────────────────────────
#
# Every one of these opens a telnet session, so they all take _gateway_lock
# and all refuse while the gateway is mid-reboot.

def _guard() -> Response | None:
    """The check every administrative endpoint starts with."""
    if _maintenance["busy"]:
        return jsonify({"error": "шлюз занят обслуживанием"}), 409
    return None


@app.get("/api/admin/ports")
def admin_ports() -> Response:
    """Every port with its live state and its key settings."""
    blocked = _guard()
    if blocked:
        return blocked
    try:
        with _gateway_lock:
            with gateway.Gateway() as gw:
                ports = admin.all_ports(gw)
                config = gw.send("show running-config")
                peers = [p for p in admin.dial_peers(config) if p["kind"] == "pots"]
    except (gateway.GatewayError, admin.AdminError) as exc:
        return jsonify({"error": str(exc)}), 502
    return jsonify({"ports": ports, "peers": peers})


@app.get("/api/admin/port/<path:port>")
def admin_port(port: str) -> Response:
    """One port's full settings, as the gateway reports them."""
    blocked = _guard()
    if blocked:
        return blocked
    try:
        with _gateway_lock:
            with gateway.Gateway() as gw:
                detail = admin.port_detail(gw, port)
    except admin.AdminError as exc:
        return jsonify({"error": str(exc)}), 400
    except gateway.GatewayError as exc:
        return jsonify({"error": str(exc)}), 502
    return jsonify({
        "detail": detail,
        "parameters": [
            {
                "key": p.key, "label": p.label, "kind": p.kind,
                "min": p.minimum, "max": p.maximum,
                "choices": list(p.choices), "unit": p.unit, "help": p.help,
            }
            for p in admin.PARAMETERS.values()
        ],
    })


@app.post("/api/admin/port/<path:port>")
def admin_set_port(port: str) -> Response:
    """Change one whitelisted parameter on one port."""
    blocked = _guard()
    if blocked:
        return blocked
    body = request.get_json(silent=True) or {}
    key = str(body.get("key", ""))
    value = body.get("value")
    try:
        with _gateway_lock:
            with gateway.Gateway() as gw:
                command = admin.set_parameter(gw, port, key, value)
    except admin.AdminError as exc:
        return jsonify({"error": str(exc)}), 400
    except gateway.GatewayError as exc:
        return jsonify({"error": str(exc)}), 502
    _invalidate_ports()
    _fan_out(monitor.Event("info", str(gateway.extension_for(port)),
                           f"порт {port}: {command}"))
    return jsonify({"ok": True, "command": command})


@app.post("/api/admin/port/<path:port>/state")
def admin_port_state(port: str) -> Response:
    """Take a port in or out of service."""
    blocked = _guard()
    if blocked:
        return blocked
    up = bool((request.get_json(silent=True) or {}).get("up", True))
    try:
        with _gateway_lock:
            with gateway.Gateway() as gw:
                admin.set_admin_state(gw, port, up)
    except admin.AdminError as exc:
        return jsonify({"error": str(exc)}), 400
    except gateway.GatewayError as exc:
        return jsonify({"error": str(exc)}), 502
    _invalidate_ports()
    _fan_out(monitor.Event("info", str(gateway.extension_for(port)),
                           f"порт {port}: {'включён' if up else 'выключен (shutdown)'}"))
    return jsonify({"ok": True})


@app.get("/api/admin/probe/<path:port>")
def admin_probe(port: str) -> Response:
    """Can this port take a call right now, and if not, why not."""
    blocked = _guard()
    if blocked:
        return blocked
    try:
        with _gateway_lock:
            with gateway.Gateway() as gw:
                result = admin.probe(gw, port)
    except admin.AdminError as exc:
        return jsonify({"error": str(exc)}), 400
    except gateway.GatewayError as exc:
        return jsonify({"error": str(exc)}), 502
    return jsonify(result)


@app.post("/api/admin/dial-peer")
def admin_dial_peer() -> Response:
    """Point an extension at a different FXS port."""
    blocked = _guard()
    if blocked:
        return blocked
    body = request.get_json(silent=True) or {}
    try:
        tag = int(body.get("tag", 0))
    except (TypeError, ValueError):
        return jsonify({"error": "неверный номер dial-peer"}), 400
    port = str(body.get("port", ""))
    try:
        with _gateway_lock:
            with gateway.Gateway() as gw:
                admin.set_dial_peer_port(gw, tag, port)
    except admin.AdminError as exc:
        return jsonify({"error": str(exc)}), 400
    except gateway.GatewayError as exc:
        return jsonify({"error": str(exc)}), 502
    _invalidate_ports()
    _fan_out(monitor.Event("info", "-", f"dial-peer {tag} переведён на порт {port}"))
    return jsonify({"ok": True})


# ── programmable numbers ────────────────────────────────────────────────
#
# Numbers a handset can dial, each playing whichever sound is assigned to it.
# Kept in the Asterisk database rather than in extensions.conf: the dialplan
# matches the whole range with one pattern and reads DB(playslot/510) at call
# time, so both which numbers exist and what each plays are decided here,
# taking effect on the next call with no reload and no configuration editing
# from the web server.
#
# This is the direction that works on a line whose handset cannot take
# incoming calls — the person lifts the receiver and dials, which needs
# nothing from the gateway that a shorted loop can refuse.

# What the dialplan's pattern covers. A number outside it would be accepted
# here and then go nowhere when dialled, so it is refused instead.
#
# 500-509 are excluded deliberately: 500, 501 and 502 are the fixed test
# numbers in extensions.conf, and the pattern there starts at 510 to leave
# them alone.
SLOT_RANGE = range(510, 530)
SLOT_FIRST = str(SLOT_RANGE.start)
SLOT_LAST = str(SLOT_RANGE.stop - 1)


def _slot_allowed(number: str) -> bool:
    return number.isdigit() and int(number) in SLOT_RANGE


def _slots_assigned() -> dict[str, str]:
    """Every programmed number and the sound it plays, read from the PBX.

    One CLI call for the whole family rather than one per number: this runs
    on every page load, and twenty round trips to the Asterisk console take
    long enough to be felt.
    """
    listing = health._cli("database show playslot") or ""
    found = {}
    for line in listing.splitlines():
        # "/playslot/510                    : zoopark"
        match = re.match(r"\s*/playslot/(\d+)\s*:\s*(\S+)", line)
        if match:
            found[match.group(1)] = match.group(2)
    return found


@app.get("/api/slots")
def slots_state() -> Response:
    """Which numbers are programmed, and what each plays."""
    assigned = _slots_assigned()

    try:
        library = [{"name": s.name, "seconds": round(s.seconds, 1)}
                   for s in sounds.library().values()]
    except sounds.SoundError:
        library = []

    slots = [{"number": n, "sound": assigned[n]} for n in sorted(assigned)]
    # Which numbers are still free, so the interface can offer them rather
    # than making the operator guess and be refused.
    free = [str(n) for n in SLOT_RANGE if str(n) not in assigned]

    return jsonify({"slots": slots, "sounds": library, "free": free,
                    "range": {"first": SLOT_FIRST, "last": SLOT_LAST}})


@app.post("/api/slots")
def slots_set() -> Response:
    """Add a number, change what it plays, or remove it.

    An empty sound removes the number: with nothing in the database the
    dialplan's pattern still matches, but the call hits the branch that says
    the number is unassigned rather than playing anything.
    """
    body = request.get_json(silent=True) or {}
    number = str(body.get("number", "")).strip()
    name = str(body.get("sound", "")).strip()

    if not _slot_allowed(number):
        return jsonify({"error": f"номер {number} не программируется "
                                 f"(доступны {SLOT_FIRST}–{SLOT_LAST})"}), 400

    if not name:
        health._cli(f"database del playslot {number}")
        _fan_out(monitor.Event("info", "-", f"номер {number} удалён"))
        return jsonify({"ok": True, "number": number, "sound": ""})

    # Checked against the library rather than trusted: the value goes into a
    # Playback() argument, and an unknown name would make the caller hear the
    # line die with no explanation.
    try:
        sound = sounds.resolve(name)
    except sounds.SoundError as exc:
        return jsonify({"error": str(exc)}), 400

    result = health._cli(f"database put playslot {number} {sound.name}")
    if result is None or "Updated" not in result:
        return jsonify({"error": "АТС не приняла настройку"}), 502

    _fan_out(monitor.Event("info", "-",
                           f"номер {number} → {sound.name} ({sound.seconds:.0f} с)"))
    return jsonify({"ok": True, "number": number, "sound": sound.name})


@app.get("/api/auto-power")
def auto_power_state() -> Response:
    with _auto_power_lock:
        return jsonify({"extensions": sorted(_auto_power),
                        "seconds": AUTO_POWER_SECONDS})


@app.post("/api/auto-power")
def auto_power_set() -> Response:
    """Turn automatic de-energising on or off for one handset."""
    body = request.get_json(silent=True) or {}
    extension = str(body.get("extension", "")).strip()
    enabled = bool(body.get("enabled", False))
    try:
        gateway.port_for(extension)
    except gateway.GatewayError as exc:
        return jsonify({"error": str(exc)}), 400

    with _auto_power_lock:
        if enabled:
            _auto_power.add(extension)
        else:
            _auto_power.discard(extension)
        current = sorted(_auto_power)

    _fan_out(monitor.Event(
        "info", extension,
        f"автообесточивание после отбоя {'включено' if enabled else 'выключено'}"))
    return jsonify({"ok": True, "extensions": current})


@app.get("/api/watchdog")
def watchdog_state() -> Response:
    return jsonify({
        "enabled": dog.fix,
        "ports": list(dog.ports),
        "grace": dog.grace,
        "interval": dog.interval,
        "watch": dog.status(),
    })


@app.post("/api/watchdog")
def watchdog_set() -> Response:
    """Turn automatic release on or off, and choose which ports it covers."""
    body = request.get_json(silent=True) or {}
    if "ports" in body:
        wanted = [str(p) for p in body.get("ports") or []]
        for port in wanted:
            if port not in gateway.PORTS:
                return jsonify({"error": f"неизвестный порт: {port}"}), 400
        dog.ports = tuple(wanted) if wanted else tuple(gateway.PORTS)
        # The per-port bookkeeping is keyed by port, so it has to be rebuilt
        # when the set changes or a removed port keeps a stale timer.
        dog.__post_init__()
    if "grace" in body:
        try:
            dog.grace = max(5.0, float(body["grace"]))
        except (TypeError, ValueError):
            return jsonify({"error": "неверный порог"}), 400
    if "enabled" in body:
        dog.fix = bool(body["enabled"])

    _fan_out(monitor.Event(
        "info", "-",
        f"автосброс {'включён' if dog.fix else 'выключен'}"
        + (f" для {', '.join(dog.ports)}" if dog.fix else "")))
    return jsonify({"ok": True, "enabled": dog.fix, "ports": list(dog.ports)})


@app.get("/api/panel")
def panel() -> Response:
    """The indicator panel: LEDs, FXS ports, and how to reach the gateway."""
    blocked = _guard()
    if blocked:
        return blocked
    try:
        with _gateway_lock:
            with gateway.Gateway() as gw:
                data = admin.panel(gw)
    except (gateway.GatewayError, admin.AdminError) as exc:
        return jsonify({"error": str(exc)}), 502
    return jsonify(data)


@app.post("/api/hangup-call")
def hangup_call() -> Response:
    """Drop the live call on one handset, through Asterisk.

    Distinct from /api/hangup, which cycles the FXS port: that ends a call by
    pulling the line out from under it and is the repair for a port that will
    not release. This is the ordinary way to end a call in progress.
    """
    extension = str((request.get_json(silent=True) or {}).get("extension", "")).strip()
    try:
        gateway.port_for(extension)
    except gateway.GatewayError as exc:
        return jsonify({"error": str(exc)}), 400

    # Which channel belongs to this handset is knowledge the manager does not
    # have — an outgoing call runs on the trunk endpoint's channel, whose
    # exten is "s" and whose connectedlinenum is "<unknown>". The monitor
    # tracks the association as calls start, so it is asked instead.
    targets = board.channels_of(extension)
    if not targets:
        return jsonify({"error": f"на {extension} нет активного вызова"}), 404

    try:
        with call.Manager() as ami:
            live = {c.get("channel", "") for c in ami.channels()}
            targets = [t for t in targets if t in live]
            if not targets:
                return jsonify({"error": f"на {extension} нет активного вызова"}), 404
            for channel in targets:
                ami.hangup(channel)
    except call.CallError as exc:
        return jsonify({"error": str(exc)}), 502

    _fan_out(monitor.Event("info", extension, "вызов завершён из интерфейса"))
    return jsonify({"ok": True, "channels": targets})


@app.get("/api/pbx")
def pbx_state() -> Response:
    """What Asterisk is doing: channels, endpoint, and what handsets can dial.

    The dialplan half is read from the file rather than from the CLI: it is
    what an operator needs in order to know which numbers do anything, and
    the CLI's own rendering of it is far longer than a panel can show.
    """
    channels = health._cli("core show channels concise") or ""
    active = [c for c in channels.splitlines() if c.strip()]

    contacts = health._cli("pjsip show contacts") or ""
    trunk = "неизвестно"
    for line in contacts.splitlines():
        if "addpac" in line:
            # "NonQual" is the wanted state here: the contact is usable and
            # deliberately not probed, because this gateway ignores OPTIONS.
            trunk = "доступен" if "NonQual" in line or "Avail" in line else line.strip()
            break

    # What each pattern does, in words. Taken from the dialplan's own
    # comments would be ideal, but they sit above the line rather than in it,
    # and NoOp() text is full of unexpanded variables — so the well-known
    # ones are named here and anything new falls back to its pattern.
    KNOWN = {
        "_10[1-8]": "Вызов на другой аппарат (101–108)",
        "500": "Проверка звука — АТС проигрывает файл в трубку",
        "501": "Эхо-тест — проверка звука в обе стороны",
        "_X.": "Любой другой номер — сообщение «неверный номер»",
    }

    extensions = []
    dialplan = ROOT / "etc" / "extensions.conf"
    if dialplan.is_file():
        context = ""
        for line in dialplan.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped.startswith("[") and stripped.endswith("]"):
                context = stripped[1:-1]
            match = re.match(r"exten\s*=>\s*([^,]+),1,", stripped)
            if match and context == "from-gateway":
                pattern = match.group(1).strip()
                extensions.append({
                    "pattern": pattern,
                    "note": KNOWN.get(pattern, "—"),
                })

    return jsonify({
        "channels": active,
        "channel_count": len(active),
        "trunk": trunk,
        "dialplan": extensions,
    })


@app.get("/api/admin/diagnostics")
def admin_diagnostics_list() -> Response:
    return jsonify({"available": [
        {"key": k, "label": v[0], "command": v[1]}
        for k, v in admin.DIAGNOSTICS.items()
    ]})


@app.get("/api/admin/diagnostics/<name>")
def admin_diagnostic(name: str) -> Response:
    """Run one read-only CLI command from the fixed list."""
    blocked = _guard()
    if blocked:
        return blocked
    try:
        with _gateway_lock:
            with gateway.Gateway() as gw:
                text = admin.diagnostic(gw, name)
    except admin.AdminError as exc:
        return jsonify({"error": str(exc)}), 400
    except gateway.GatewayError as exc:
        return jsonify({"error": str(exc)}), 502
    return jsonify({"name": name, "text": text})


@app.post("/api/pbx/reload")
def pbx_reload() -> Response:
    """Re-read the dialplan without dropping calls.

    A reload, not a restart: restarting Asterisk would tear down every
    channel, and nothing here needs that to pick up an edited
    etc/extensions.conf.
    """
    output = health._cli("dialplan reload")
    if output is None:
        return jsonify({"error": "АТС не отвечает"}), 502
    _fan_out(monitor.Event("info", "-", "план набора перезагружен"))
    return jsonify({"ok": True, "text": output.strip()})


@app.post("/api/admin/save")
def admin_save() -> Response:
    """Persist the running configuration to flash.

    Confirmed on the page. Until this runs, every change made here is undone
    by power-cycling the gateway — the only rollback the device has.
    """
    blocked = _guard()
    if blocked:
        return blocked
    try:
        with _gateway_lock:
            with gateway.Gateway() as gw:
                text = admin.save_to_flash(gw)
    except gateway.GatewayError as exc:
        return jsonify({"error": str(exc)}), 502
    _fan_out(monitor.Event("info", "-", "конфигурация сохранена во flash"))
    return jsonify({"ok": True, "text": text})


# ── maintenance: resetting and rebooting the gateway ────────────────────

def _begin_maintenance(what: str, detail: str) -> bool:
    """Claim the gateway. False if something else already has it."""
    with _calls_lock:
        if _maintenance["busy"]:
            return False
        _maintenance.update(busy=True, what=what, started=time.time(),
                            detail=detail)
    return True


def _end_maintenance(detail: str) -> None:
    with _calls_lock:
        _maintenance.update(busy=False, what="", detail=detail)
    _invalidate_ports()


def _run_reset() -> None:
    """Cycle every FXS port. The soft repair."""
    _progress_start("reset", "Сброс портов FXS", [
        "Подключение к шлюзу",
        "Цикл shutdown/no shutdown по 8 портам",
        "Ожидание инициализации",
        "Проверка результата",
    ])
    try:
        _progress_step(0, "running")
        with _gateway_lock:
            _progress_step(0, "ok")
            _progress_step(1, "running", "около 10 секунд")
            cycled, stuck = gateway.cycle_everything()
            _progress_step(1, "ok")
            _progress_step(2, "ok")
            _progress_step(3, "ok" if not stuck else "fail")
        if stuck:
            # Almost always a handset left off the hook, which nothing done
            # from this end can fix — so it is named rather than buried.
            names = ", ".join(f"{gateway.extension_for(p)}" for p in stuck)
            _fan_out(monitor.Event(
                "error", "-",
                f"порты сброшены ({len(cycled)}), но не освободились: {names}. "
                "Проверьте, не снята ли трубка."))
            _progress_finish(False, f"не освободились: {names}")
            _end_maintenance(f"не освободились: {names}")
        else:
            _fan_out(monitor.Event("info", "-",
                                   f"сброшены все порты FXS ({len(cycled)})"))
            _progress_finish(True, "все порты свободны")
            _end_maintenance("порты сброшены")
    except gateway.GatewayError as exc:
        _fan_out(monitor.Event("error", "-", f"сброс портов не удался: {exc}"))
        _progress_finish(False, str(exc))
        _end_maintenance(str(exc))
    except Exception as exc:                                   # noqa: BLE001
        _fan_out(monitor.Event("error", "-", f"сброс портов не удался: {exc}"))
        _progress_finish(False, str(exc))
        _end_maintenance(str(exc))


def _run_reboot() -> None:
    """Reboot the gateway and wait for it to answer again."""
    _progress_start("reboot", "Перезагрузка шлюза", [
        "Отправка команды reboot",
        "Шлюз выключается",
        "Ожидание загрузки (до 3 минут)",
        "Проверка связи",
    ])
    try:
        _progress_step(0, "running")
        with _gateway_lock:
            gateway.reboot_gateway()
        _progress_step(0, "ok")
        _progress_step(1, "ok")
        _progress_step(2, "running", "несохранённые настройки откатятся")
        _fan_out(monitor.Event("info", "-",
                               "шлюз перезагружается, это займёт около минуты"))
        # Not inside the lock: waiting holds it for the whole minute, and the
        # wait opens its own short-lived sessions anyway.
        seconds = gateway.wait_until_alive(timeout=180)
        _progress_step(2, "ok", f"{seconds:.0f} с")
        _progress_step(3, "ok")
        _progress_finish(True, f"шлюз вернулся за {seconds:.0f} с")
        _fan_out(monitor.Event("info", "-",
                               f"шлюз снова на связи, {seconds:.0f} с"))
        _end_maintenance(f"перезагружен за {seconds:.0f} с")
    except gateway.GatewayError as exc:
        _progress_finish(False, str(exc))
        _fan_out(monitor.Event("error", "-", f"шлюз не вернулся: {exc}"))
        _end_maintenance(str(exc))
    except Exception as exc:                                   # noqa: BLE001
        _progress_finish(False, str(exc))
        _fan_out(monitor.Event("error", "-", f"перезагрузка не удалась: {exc}"))
        _end_maintenance(str(exc))


@app.post("/api/reset-ports")
def reset_ports() -> Response:
    """Cycle every FXS port without touching the gateway's power."""
    if not _begin_maintenance("reset", "сброс портов"):
        return jsonify({"error": "шлюз уже занят обслуживанием"}), 409
    _invalidate_ports()
    _fan_out(monitor.Event("info", "-", "сброс всех портов FXS"))
    threading.Thread(target=_run_reset, name="reset-ports", daemon=True).start()
    return jsonify({"ok": True})


@app.post("/api/reboot")
def reboot() -> Response:
    """Reboot the gateway.

    Confirmed on the page before it gets here: it drops every call in
    progress and takes the gateway off the network for about a minute.
    """
    if not _begin_maintenance("reboot", "перезагрузка шлюза"):
        return jsonify({"error": "шлюз уже занят обслуживанием"}), 409
    _invalidate_ports()
    _fan_out(monitor.Event("info", "-", "перезагрузка шлюза"))
    threading.Thread(target=_run_reboot, name="reboot", daemon=True).start()
    return jsonify({"ok": True})


@app.post("/api/on-hook")
def on_hook() -> Response:
    """"Я положил трубку" — de-energise the line long enough to let go.

    For a handset whose line switches keep the loop closed after the receiver
    is down. /api/hangup cycles the port for about a second, which clears a
    session the firmware is holding; this holds the line dead for six, which
    also lets a shorted loop settle. Measured on the TX-220 on 106: after a
    one-second cycle the port went busy again within 30 seconds, after six it
    stayed idle for a full minute.
    """
    body = request.get_json(silent=True) or {}
    extension = str(body.get("extension", "")).strip()
    try:
        seconds = float(body.get("seconds", 6.0))
    except (TypeError, ValueError):
        seconds = 6.0
    # Bounded: the line is unusable while it is down, and a long hold would
    # look like a dead extension rather than a repair.
    seconds = min(max(seconds, 1.0), 20.0)

    try:
        gateway.port_for(extension)
    except gateway.GatewayError as exc:
        return jsonify({"error": str(exc)}), 400
    if _maintenance["busy"]:
        return jsonify({"error": "шлюз занят обслуживанием"}), 409

    _progress_start(f"onhook-{extension}", f"Обесточивание линии {extension}", [
        "Снятие питания с линии",
        f"Ожидание {seconds:.0f} с",
        "Подача питания",
        "Проверка состояния",
    ])
    try:
        _progress_step(0, "running")
        with _gateway_lock:
            _progress_step(0, "ok")
            _progress_step(1, "running", f"{seconds:.0f} с без питания")
            status = gateway.power_cycle_extension(extension, down_seconds=seconds)
        _progress_step(1, "ok")
        _progress_step(2, "ok")
        free = status == "Idle"
        _progress_step(3, "ok" if free else "fail", status)
        _progress_finish(free, "линия свободна" if free
                         else f"порт остался {status}")
        # Keeps the watchdog off this port while the cycle settles; two
        # cycles landing together leave it Disconnecting.
        dog.touched(gateway.port_for(extension))
    except gateway.GatewayError as exc:
        _progress_finish(False, str(exc))
        return jsonify({"error": str(exc)}), 502

    _invalidate_ports()
    _fan_out(monitor.Event(
        "info", extension,
        f"линия обесточена на {seconds:.0f} с, порт {status}"))
    return jsonify({"ok": free, "status": status})


@app.post("/api/hangup")
def hangup() -> Response:
    """End whatever call a handset has, from this end.

    Uses the gateway's port cycle rather than an AMI Hangup: it ends the call
    and leaves the port Idle in one step, where hanging up the channel alone
    can leave the port in the stuck state that blocks the next call.
    """
    extension = str((request.get_json(silent=True) or {}).get("extension", "")).strip()
    try:
        gateway.port_for(extension)
    except gateway.GatewayError as exc:
        return jsonify({"error": str(exc)}), 400
    if _maintenance["busy"]:
        return jsonify({"error": "шлюз занят обслуживанием"}), 409
    try:
        with _gateway_lock:
            gateway.clear_extension(extension)
    except gateway.GatewayError as exc:
        return jsonify({"error": str(exc)}), 502
    _invalidate_ports()
    dog.touched(gateway.port_for(extension))
    _fan_out(monitor.Event("info", extension, "порт освобождён из интерфейса"))
    return jsonify({"ok": True})


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()

    board.start()
    # Its findings go to the same event stream the page already follows, so a
    # release shows up in the log beside the calls it affects.
    # Shares the lock so a sweep cannot open a telnet session while a call is
    # being placed or a port released by hand.
    dog.gateway_lock = _gateway_lock
    dog.on_event = lambda kind, port, message: _fan_out(
        monitor.Event(kind if kind in ("error", "warn") else "info",
                      str(gateway.extension_for(port)) if port != "-" else "-",
                      message))
    dog.start()

    # Generated now rather than on the first dialled call. It takes a moment,
    # and that moment would otherwise land between the last digit and the
    # ringing — the one place in the call where a pause is audible.
    try:
        tones.build_all()
    except OSError as exc:
        print(f"не удалось подготовить гудки: {exc}", file=sys.stderr)

    print(f"open http://{args.host}:{args.port}")
    # threaded, because the event stream holds a worker for as long as a
    # browser is open; single-threaded, the first page to connect would be
    # the only one the server could ever answer.
    app.run(host=args.host, port=args.port, threaded=True, debug=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
