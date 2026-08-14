"""
Watching the handsets: who lifted a receiver, who put one down, what they typed.

Asterisk reports all three over the manager interface, but not as three tidy
events. What arrives is the channel lifecycle — a channel appearing, changing
state, and going away — plus a DTMF event per keypress. This module turns that
stream into the vocabulary the phones actually have:

    off-hook    someone lifted a receiver, or a handset we rang answered
    digit       a key was pressed
    call-ended  the call attempt finished

The hook itself is not among them, and cannot be. This gateway never reports
a receiver lifted or replaced: its part in a call is to put ringing current
on the line, and it gives up on the INVITE about thirty seconds later whether
or not anyone answered. What reads the hook is the ESP wired to the switch,
which posts to /api/dialer — see scripts/web.py. So a Hangup here means the
call attempt ended, not that anybody hung up, and the two are hours apart in
meaning: the receiver is often still coming up when it arrives.

The distinction that matters is *why* a channel appeared. A handset lifted by
the person holding it produces an inbound channel from the gateway; a handset
ringing because scripts/call.py originated to it produces an outbound one. Both
end up Up, and only the direction separates "they called us" from "they picked
up what we sent them". Both are reported, tagged, because an operator watching
this wants to know which happened.

There is no polling here and no gateway telnet. The FXS port summary in
scripts/gateway.py says what a port is doing but not when it changed, and
asking it on a timer gives an event log with the timestamps smeared across the
poll interval. AMI events carry their own moment.

    python3 scripts/monitor.py            print events as they happen
"""

from __future__ import annotations

import re
import socket
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Iterable

AMI_HOST, AMI_PORT = "127.0.0.1", 5038
AMI_USER, AMI_SECRET = "caller", "voip-local"

# Extensions the gateway carries; see gateway.PORTS.
EXTENSIONS = [str(n) for n in range(101, 109)]

# "PJSIP/101-00000003" -> "101". Anything else (a local channel, the PBX's own
# side of a call) has no handset behind it and is not reported.
CHANNEL_EXTEN = re.compile(r"^PJSIP/(\d{3})-")


class MonitorError(RuntimeError):
    pass


# ── what the rest of the program sees ───────────────────────────────────

@dataclass
class Event:
    """One thing that happened on one handset."""

    kind: str            # off-hook | on-hook | call-ended | digit | ringing
                         # | error | info
    extension: str       # "101".."108", or "-" when it belongs to no handset
    detail: str          # human-readable; for a digit, the digit itself
    at: float = field(default_factory=time.time)
    # Which side started the call this event belongs to:
    #   inbound   the handset was lifted and dialled
    #   outbound  the PBX rang the handset
    direction: str = ""

    @property
    def clock(self) -> str:
        return datetime.fromtimestamp(self.at).strftime("%H:%M:%S")

    def as_dict(self) -> dict:
        return {
            "kind": self.kind,
            "extension": self.extension,
            "detail": self.detail,
            "at": self.at,
            "clock": self.clock,
            "direction": self.direction,
        }

    def __str__(self) -> str:
        where = self.extension if self.extension != "-" else "  -"
        return f"{self.clock}  {where}  {self.kind:9} {self.detail}"


@dataclass
class Line:
    """The current state of one handset, as far as the event stream shows."""

    extension: str
    state: str = "idle"       # idle | ringing | off-hook
    direction: str = ""
    digits: str = ""          # what has been typed during the current call
    since: float = field(default_factory=time.time)

    def as_dict(self) -> dict:
        return {
            "extension": self.extension,
            "state": self.state,
            "direction": self.direction,
            "digits": self.digits,
            "since": self.since,
        }


# ── the AMI event stream ────────────────────────────────────────────────

class _Connection:
    """A read-only manager connection that stays open and yields events.

    Separate from call.Manager, which logs in, does one thing and logs out.
    This one has to survive for as long as the web interface is up, so it
    reconnects on its own rather than raising into the caller's lap.
    """

    def __init__(self, timeout: float = 5.0) -> None:
        self.timeout = timeout
        self.sock: socket.socket | None = None
        self.buf = b""

    def connect(self) -> None:
        try:
            self.sock = socket.create_connection((AMI_HOST, AMI_PORT), timeout=self.timeout)
        except OSError as exc:
            raise MonitorError(
                f"cannot reach the PBX manager on {AMI_HOST}:{AMI_PORT} ({exc}). "
                "Is it running?  ./scripts/pbx.sh status"
            ) from exc
        self.buf = b""
        self._read_line()                       # the banner
        self.sock.sendall(
            f"Action: Login\r\nUsername: {AMI_USER}\r\nSecret: {AMI_SECRET}\r\n"
            # Only the classes this needs. Asking for everything would also
            # carry the per-frame reporting traffic, which is noise here.
            "Events: call,dtmf\r\n\r\n".encode()
        )
        reply = self._read_block()
        if "Success" not in reply:
            raise MonitorError(
                "the PBX manager rejected the login.\n"
                "Check the [caller] account in etc/manager.conf."
            )

    def close(self) -> None:
        if self.sock is not None:
            try:
                self.sock.sendall(b"Action: Logoff\r\n\r\n")
            except OSError:
                pass
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None

    def _recv(self, deadline: float) -> None:
        if self.sock is None:
            raise MonitorError("not connected")
        self.sock.settimeout(max(0.1, deadline - time.monotonic()))
        try:
            chunk = self.sock.recv(8192)
        except socket.timeout:
            return
        if not chunk:
            raise MonitorError("the PBX manager closed the connection")
        self.buf += chunk

    def _read_line(self, timeout: float | None = None) -> str:
        deadline = time.monotonic() + (timeout or self.timeout)
        while b"\r\n" not in self.buf:
            if time.monotonic() >= deadline:
                raise MonitorError("the PBX manager sent no greeting")
            self._recv(deadline)
        line, self.buf = self.buf.split(b"\r\n", 1)
        return line.decode("utf-8", "replace")

    def _read_block(self, timeout: float | None = None) -> str:
        deadline = time.monotonic() + (timeout or self.timeout)
        while b"\r\n\r\n" not in self.buf:
            if time.monotonic() >= deadline:
                raise MonitorError("timed out waiting for the PBX manager")
            self._recv(deadline)
        block, self.buf = self.buf.split(b"\r\n\r\n", 1)
        return block.decode("utf-8", "replace")

    def blocks(self) -> Iterable[dict[str, str]]:
        """Yield every event as a dict of lowercased keys.

        Yields None-free forever, blocking between events; a quiet line is not
        an error, so the read timeout is swallowed rather than raised. Only a
        closed or broken socket ends the loop.
        """
        while True:
            try:
                raw = self._read_block(timeout=2.0)
            except MonitorError as exc:
                if "closed the connection" in str(exc):
                    raise
                continue                        # nothing happened, keep waiting
            fields = {}
            for line in raw.splitlines():
                if ":" in line:
                    key, value = line.split(":", 1)
                    fields[key.strip().lower()] = value.strip()
            if fields:
                yield fields


# ── turning the stream into handset events ──────────────────────────────

class Monitor:
    """Follows the handsets and keeps a log of what they did.

    Runs its reader on a background thread so the web app can serve pages
    while it waits. Every observer added with subscribe() is called with each
    Event as it is produced.
    """

    def __init__(self, history: int = 400) -> None:
        self.lines: dict[str, Line] = {e: Line(extension=e) for e in EXTENSIONS}
        self.log: deque[Event] = deque(maxlen=history)
        self.connected = False
        self.last_error = ""
        self._lock = threading.Lock()
        self._observers: list[Callable[[Event], None]] = []
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        # Channel name -> the extension and direction it belongs to. Needed
        # because a Hangup event names only the channel, and by then the
        # channel's own fields may no longer say which handset it was.
        self._channels: dict[str, tuple[str, str]] = {}
        # Handsets we have just been asked to ring, oldest first. See the
        # trunk-channel branch in _handle().
        self._expecting: list[str] = []

    # ── observers ───────────────────────────────────────────────────────

    def subscribe(self, callback: Callable[[Event], None]) -> None:
        with self._lock:
            self._observers.append(callback)

    def unsubscribe(self, callback: Callable[[Event], None]) -> None:
        with self._lock:
            if callback in self._observers:
                self._observers.remove(callback)

    def _emit(self, event: Event) -> None:
        with self._lock:
            self.log.append(event)
            observers = list(self._observers)
        for callback in observers:
            # One broken subscriber — a browser that closed mid-stream — must
            # not take down the reader thread and with it every other client.
            try:
                callback(event)
            except Exception:
                pass

    # ── state ───────────────────────────────────────────────────────────

    def expect(self, extension: str) -> None:
        """Say that a call is about to be placed to this handset.

        The trunk channel that carries it appears moments later with nothing
        in it naming the destination, so it is matched to the most recent
        unclaimed request. Queued rather than stored singly because two
        handsets can be rung at once.
        """
        with self._lock:
            self._expecting.append(extension)

    def claim(self, channel: str, extension: str, direction: str = "outbound") -> None:
        """Record that a channel belongs to a handset.

        Called by whoever placed the call, because nothing on the wire says
        so. A call the PBX originates runs on a channel named after the trunk
        endpoint — "PJSIP/addpac-0000000b" — and neither Newchannel nor
        CoreShowChannels carries the number that was dialled: exten is "s",
        connectedlinenum is "<unknown>". The only place the destination is
        known is the code that asked for the call.
        """
        with self._lock:
            self._channels[channel] = (extension, direction)

    def channels_of(self, extension: str) -> list[str]:
        """Live channel names belonging to one handset."""
        with self._lock:
            return [name for name, (ext, _) in self._channels.items()
                    if ext == extension]

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "connected": self.connected,
                "error": self.last_error,
                "lines": [self.lines[e].as_dict() for e in EXTENSIONS],
                "log": [e.as_dict() for e in self.log],
            }

    def _set(self, extension: str, state: str, direction: str = "") -> None:
        with self._lock:
            line = self.lines[extension]
            line.state = state
            line.since = time.time()
            if direction:
                line.direction = direction
            if state == "idle":
                line.digits = ""
                line.direction = ""

    # ── event handling ──────────────────────────────────────────────────

    @staticmethod
    def _extension_of(channel: str) -> str | None:
        match = CHANNEL_EXTEN.match(channel or "")
        if not match:
            return None
        extension = match.group(1)
        return extension if extension in EXTENSIONS else None

    def _handle(self, fields: dict[str, str]) -> None:
        event = fields.get("event", "")

        if event == "Newchannel":
            channel = fields.get("channel", "")
            extension = self._extension_of(channel)
            if not extension:
                # A call the PBX placed runs on the trunk endpoint's own
                # channel, "PJSIP/addpac-...", which names no handset: exten
                # is "s" and connectedlinenum is "<unknown>". Only the code
                # that asked for the call knows where it is going, and it
                # says so through expect(); this attaches the next such
                # channel to that handset.
                if channel.startswith("PJSIP/addpac-") and self._expecting:
                    wanted = self._expecting.pop(0)
                    self._channels[channel] = (wanted, "outbound")
                return
            # A channel Asterisk created towards the gateway is an outbound
            # call: we are ringing that handset. One arriving from the gateway
            # means the receiver was lifted at the handset's end. The
            # difference shows in the dialplan context the channel starts in —
            # inbound channels land in [from-gateway] (see etc/extensions.conf),
            # originated ones start with none.
            inbound = fields.get("context", "") == "from-gateway"
            direction = "inbound" if inbound else "outbound"
            self._channels[channel] = (extension, direction)
            if inbound:
                self._set(extension, "off-hook", direction)
                self._emit(Event("off-hook", extension,
                                 "снята трубка", direction=direction))
            else:
                self._set(extension, "ringing", direction)
                self._emit(Event("ringing", extension,
                                 "АТС звонит на аппарат",
                                 direction=direction))

        elif event == "Newstate":
            channel = fields.get("channel", "")
            known = self._channels.get(channel)
            if not known:
                return
            extension, direction = known
            # Up means the media path is open. On an outbound call that is the
            # moment the receiver was lifted; on an inbound one the handset was
            # already off-hook and this only confirms it.
            if fields.get("channelstatedesc") == "Up" and direction == "outbound":
                self._set(extension, "off-hook", direction)
                self._emit(Event("off-hook", extension,
                                 "ответили на вызов", direction=direction))

        elif event in ("DTMFEnd", "DTMFBegin"):
            # Both are sent for every keypress: Begin when the tone starts,
            # End when it stops. Counting both would double every digit, so
            # only End is taken — it is the one that carries a settled digit.
            if event == "DTMFBegin":
                return
            channel = fields.get("channel", "")
            known = self._channels.get(channel)
            extension = known[0] if known else self._extension_of(channel)
            if not extension:
                return
            digit = fields.get("digit", "?")
            direction = known[1] if known else ""
            with self._lock:
                self.lines[extension].digits += digit
            self._emit(Event("digit", extension, digit, direction=direction))

        elif event == "Hangup":
            channel = fields.get("channel", "")
            known = self._channels.pop(channel, None)
            if not known:
                return
            extension, direction = known
            cause = fields.get("cause-txt") or fields.get("cause") or ""
            self._set(extension, "idle")
            # A SIP channel ending is not a receiver going down, and must not
            # be reported as one. The gateway never sees the hook at all: it
            # puts ringing current on the line and gives up about thirty
            # seconds later whether or not anybody answered, which is the
            # hangup arriving here. The receiver may well be up at that
            # moment — someone walking to the telephone answers it after the
            # bells stop — and an on-hook here would end a call that is only
            # just beginning.
            #
            # The hook is the ESP's to report; see /api/dialer in
            # scripts/web.py. This says only that the call attempt is over.
            self._emit(Event("call-ended", extension,
                             f"вызов завершён ({cause})" if cause else "вызов завершён",
                             direction=direction))

    # ── the reader thread ───────────────────────────────────────────────

    def _run(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            connection = _Connection()
            try:
                connection.connect()
                with self._lock:
                    self.connected = True
                    self.last_error = ""
                self._emit(Event("info", "-", "подключено к АТС"))
                backoff = 1.0
                for fields in connection.blocks():
                    if self._stop.is_set():
                        break
                    self._handle(fields)
            except MonitorError as exc:
                with self._lock:
                    self.connected = False
                    self.last_error = str(exc)
                self._emit(Event("error", "-", str(exc)))
            except Exception as exc:                       # noqa: BLE001
                with self._lock:
                    self.connected = False
                    self.last_error = str(exc)
                self._emit(Event("error", "-", f"монитор остановлен: {exc}"))
            finally:
                connection.close()
                with self._lock:
                    self.connected = False
                # Channels seen through a connection that has gone are stale;
                # keeping them would attach the next call's events to the wrong
                # handset if a channel name were ever reused.
                self._channels.clear()

            # Reconnect, backing off so a PBX that is down does not turn into
            # a busy loop against a refused port.
            if self._stop.wait(backoff):
                break
            backoff = min(backoff * 2, 15.0)

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="ami-monitor",
                                        daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)


if __name__ == "__main__":
    monitor = Monitor()
    monitor.subscribe(lambda event: print(event, flush=True))
    monitor.start()
    print("watching handsets 101-108, ctrl-c to stop\n")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        monitor.stop()
        print()
