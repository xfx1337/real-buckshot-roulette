"""
Releasing ports that a faulty handset holds down.

The TX-220 on extension 106 has blown line switches (Q6/Q7) that keep the
loop closed after the receiver goes back down — and, as measured, at rest as
well. The gateway reads that closed loop exactly as it would read a lifted
receiver, so the port goes Busy on its own, then Disconnecting, and the next
call to it is refused "486 Busy Here".

No gateway setting fixes this. Every mechanism APOS v8 has hangs off an
event: fxs-powerdown-duration acts at hang-up, timeout tterm bounds a
session, clear-down-tone-detect listens to audio. None of them fires for "the
port has been busy for no reason", and the firmware has no loop-current
threshold to raise. So the repair has to come from this side.

What makes it safe is the second signal. A port that is busy because someone
is talking has a channel on the PBX; a port held by a short does not. The
pair — busy on the gateway, no channel on Asterisk — is what a real call can
never look like, so acting on it cannot cut anyone off. A brief grace period
covers the moments around setup and teardown when the two legitimately
disagree.

Cycling the port de-energises the line, which is the same thing the operator
does by hand with "Освободить" — this only does it without being asked.

    python3 scripts/watchdog.py            watch every port, report only
    python3 scripts/watchdog.py --fix 1/1  release that port when it sticks
"""

from __future__ import annotations

import argparse
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import call     # noqa: E402
import gateway  # noqa: E402

# How long a port may look busy-without-a-channel before it is released.
# Long enough to outlast the gap between an originate and the channel
# appearing, short enough that the next call is not kept waiting.
GRACE_SECONDS = 20.0

# Between sweeps. Each one costs a telnet login, and the gateway has few
# sessions, so this is deliberately slower than the web interface's polling.
INTERVAL_SECONDS = 15.0

# Releasing the same port over and over means the short is permanent rather
# than a leftover session. It is still worth doing — the port is usable
# between attempts — but the log should say so rather than repeating one line
# forever.
NOISY_AFTER = 5

# How long to leave a port alone after someone else has cycled it. Covers the
# whole of a manual release — six seconds down, plus the settle — with room
# to spare, so the two never overlap.
SETTLE_AFTER_TOUCH = 30.0


@dataclass
class PortWatch:
    port: str
    stuck_since: float = 0.0
    releases: int = 0
    last_release: float = 0.0
    reported: bool = False


@dataclass
class Watchdog:
    """Watches ports and frees the ones a fault is holding."""

    ports: tuple[str, ...] = field(default_factory=lambda: tuple(gateway.PORTS))
    grace: float = GRACE_SECONDS
    interval: float = INTERVAL_SECONDS
    fix: bool = False
    on_event: object = None          # callable(kind, port, message) or None
    # Held by whatever else is talking to the gateway — a call being placed,
    # the operator pressing a button. Two cycles landing on one port at the
    # same moment leave it in Disconnecting, which is worse than the state
    # either was trying to repair.
    gateway_lock: object = None
    # Ports someone else has just touched, and when. A release moments after
    # a manual one is not a second fault, it is the first one still settling.
    _recent: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        self._watch = {p: PortWatch(p) for p in self.ports}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    # ── reporting ───────────────────────────────────────────────────────

    def touched(self, port: str) -> None:
        """Note that something else has just worked on this port.

        Called by the interface when the operator releases a port or places a
        call. The sweep then leaves it alone for a while: a port cycling back
        through Busy on its way to Idle looks exactly like a stuck one, and
        cycling it again mid-transition is what leaves it Disconnecting.
        """
        with self._lock:
            self._recent[port] = time.time()

    def _say(self, kind: str, port: str, message: str) -> None:
        if callable(self.on_event):
            try:
                self.on_event(kind, port, message)
            except Exception:                                  # noqa: BLE001
                pass

    def status(self) -> list[dict]:
        with self._lock:
            return [
                {
                    "port": w.port,
                    "extension": str(gateway.extension_for(w.port)),
                    "stuck_since": w.stuck_since,
                    "releases": w.releases,
                    "last_release": w.last_release,
                }
                for w in self._watch.values()
            ]

    # ── one sweep ───────────────────────────────────────────────────────

    def _channels(self) -> set[str] | None:
        """Extensions with a live channel. None if the PBX cannot be asked.

        None matters: with no answer from Asterisk there is no way to tell a
        real call from a short, and releasing on a guess would cut a call.
        """
        try:
            with call.Manager(timeout=5.0) as ami:
                names = {c.get("channel", "") for c in ami.channels()}
        except call.CallError:
            return None

        busy: set[str] = set()
        for name in names:
            # A handset that dialled in is named after its own extension.
            for extension in (str(n) for n in range(101, 109)):
                if name.startswith(f"PJSIP/{extension}-"):
                    busy.add(extension)
            # A call the PBX placed runs on the trunk endpoint's channel and
            # names no handset, so any such channel means some port is
            # legitimately in use. Which one cannot be told from here, so the
            # sweep stands down entirely rather than risk the wrong port.
            if name.startswith("PJSIP/addpac-"):
                return None
        return busy

    def sweep(self) -> list[str]:
        """One pass. Returns the ports released."""
        with_channel = self._channels()
        if with_channel is None:
            return []                       # cannot judge safely; do nothing

        try:
            if self.gateway_lock is not None:
                with self.gateway_lock:
                    states = gateway.status()
            else:
                states = gateway.status()
        except gateway.GatewayError as exc:
            # Not worth reporting on its own: the gateway is briefly
            # unreachable during a reboot, and saying so every sweep buries
            # the log in noise. The status panel already shows it.
            return []

        now = time.time()
        released: list[str] = []

        for port in self.ports:
            state = states.get(port)
            if state is None:
                continue
            watch = self._watch[port]
            extension = str(gateway.extension_for(port))

            # Free, or carrying a real call — nothing to do, and the clock
            # resets so a later fault is timed from its own start.
            if state.usable or extension in with_channel:
                if watch.stuck_since:
                    watch.stuck_since = 0.0
                    watch.reported = False
                continue

            # Someone else has just cycled this port. What follows is that
            # cycle settling, not a new fault — the two firing together is
            # what leaves a port in Disconnecting.
            with self._lock:
                touched = self._recent.get(port, 0.0)
            if now - touched < SETTLE_AFTER_TOUCH:
                watch.stuck_since = 0.0
                continue

            if not watch.stuck_since:
                watch.stuck_since = now
                continue

            held = now - watch.stuck_since
            if held < self.grace:
                continue

            if not self.fix:
                if not watch.reported:
                    self._say("warn", port,
                              f"порт {port} занят {held:.0f} с без вызова")
                    watch.reported = True
                continue

            try:
                # The same lock every other gateway path takes. Without it a
                # sweep can open a telnet session in the middle of a call
                # being set up, and the AP1100F has very few to spare.
                if self.gateway_lock is not None:
                    with self.gateway_lock:
                        gateway.clear_extension(extension)
                else:
                    gateway.clear_extension(extension)
                self.touched(port)
                watch.releases += 1
                watch.last_release = now
                watch.stuck_since = 0.0
                watch.reported = False
                released.append(port)
                if watch.releases <= NOISY_AFTER:
                    self._say("info", port,
                              f"порт {port} освобождён автоматически "
                              f"(был занят {held:.0f} с без вызова)")
                elif watch.releases % 10 == 0:
                    self._say("warn", port,
                              f"порт {port} освобождён {watch.releases}-й раз — "
                              "замыкание в линии не устранено")
            except gateway.GatewayError as exc:
                self._say("error", port, f"не удалось освободить {port}: {exc}")
                # Restart the clock so the next attempt waits out the grace
                # period again instead of retrying every sweep.
                watch.stuck_since = now

        return released

    # ── the loop ────────────────────────────────────────────────────────

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.sweep()
            except Exception as exc:                           # noqa: BLE001
                self._say("error", "-", f"сторож: {exc}")
            self._stop.wait(self.interval)

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="watchdog",
                                        daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Освобождает порты, которые держит неисправная линия.")
    parser.add_argument("--fix", nargs="*", metavar="ПОРТ",
                        help="освобождать эти порты (без значения — все)")
    parser.add_argument("--grace", type=float, default=GRACE_SECONDS,
                        metavar="СЕК",
                        help=f"сколько ждать перед сбросом (по умолчанию {GRACE_SECONDS:.0f})")
    parser.add_argument("--interval", type=float, default=INTERVAL_SECONDS,
                        metavar="СЕК", help="пауза между проверками")
    args = parser.parse_args()

    ports = tuple(args.fix) if args.fix else tuple(gateway.PORTS)
    for port in ports:
        if port not in gateway.PORTS:
            print(f"error: неизвестный порт {port}", file=sys.stderr)
            return 1

    dog = Watchdog(ports=ports, grace=args.grace, interval=args.interval,
                   fix=args.fix is not None,
                   on_event=lambda kind, port, message: print(
                       f"{time.strftime('%H:%M:%S')}  {message}", flush=True))

    mode = "освобождение" if args.fix is not None else "только наблюдение"
    print(f"сторож: {', '.join(ports)} · {mode} · "
          f"порог {args.grace:.0f} с · проверка каждые {args.interval:.0f} с\n")
    dog.start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        dog.stop()
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
