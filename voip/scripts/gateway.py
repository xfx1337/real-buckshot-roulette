"""
Telnet control of the AddPac VoiceFinder AP1100F.

The one thing this module exists for is clearing a stuck FXS port.

Firmware 8.30U leaves a port in "Disconnecting" when a call ends and never
brings it back to "Idle" on its own — measured here at 45+ seconds with no
change, and the state survives for as long as anyone watches. The next call to
that port is answered "486 Busy Here", which is what makes only the first call
work.

Nothing in the port's configuration governs it: the timing knobs
(fxs-reorder-duration, fxs-linelock-duration, fxs-powerdown-duration) affect
tone lengths, not this, and "timeout tterm" does not release it either.
Cycling the port's admin state does, every time:

    voice-port 1/2
    shutdown
    no shutdown

That is the whole fix. Calling clear_port() before each call turns a
one-call-per-reboot gateway into one that can be called repeatedly.
"""

from __future__ import annotations

import re
import socket
import time
from dataclasses import dataclass

HOST = "192.168.100.3"
USER = "root"
PASSWORD = "router"

# Extension 101..108 map onto FXS ports 0/0..0/3 then 1/0..1/3, matching the
# dial-peer table in the gateway's own configuration.
PORTS = ["0/0", "0/1", "0/2", "0/3", "1/0", "1/1", "1/2", "1/3"]

# The CLI prompt, in both plain and configuration modes:
#   AP1100F#   AP1100F(config)#   AP1100F(config-voice-port-1/2)#
PROMPT = re.compile(rb"AP1100F[^\r\n]*#\s*$")

# Long output stops at a pager which only responds to a keypress.
MORE = re.compile(rb"--\s*more\s*--", re.I)


class GatewayError(RuntimeError):
    pass


# ── a very small telnet client ──────────────────────────────────────────
#
# telnetlib was removed from the standard library in Python 3.13, and this
# needs so little of it that a dependency would cost more than it saves. The
# gateway only ever negotiates a handful of options, and the answer to all of
# them is no: refusing everything leaves a plain byte stream, which is what the
# CLI wants anyway.

IAC = 255   # interpret as command
DONT, DO, WONT, WILL = 254, 253, 252, 251
SB, SE = 250, 240


class _Telnet:
    def __init__(self, host: str, port: int = 23, timeout: float = 10.0) -> None:
        self.timeout = timeout
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.buf = bytearray()

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass

    def write(self, data: bytes) -> None:
        # A literal 0xFF in outgoing data would be read as a command.
        self.sock.sendall(data.replace(bytes([IAC]), bytes([IAC, IAC])))

    def _negotiate(self, verb: int, option: int) -> None:
        # Say no to everything: WILL/DO -> refuse, WONT/DONT -> agree silently.
        if verb in (DO, DONT):
            self.sock.sendall(bytes([IAC, WONT, option]))
        elif verb in (WILL, WONT):
            self.sock.sendall(bytes([IAC, DONT, option]))

    def _fill(self) -> bool:
        """Read one chunk, strip telnet commands, append to the buffer."""
        try:
            chunk = self.sock.recv(4096)
        except socket.timeout:
            return False
        if not chunk:
            raise GatewayError("gateway closed the connection")

        out = bytearray()
        i = 0
        while i < len(chunk):
            byte = chunk[i]
            if byte != IAC:
                out.append(byte)
                i += 1
                continue
            if i + 1 >= len(chunk):
                break
            cmd = chunk[i + 1]
            if cmd == IAC:              # escaped 0xFF
                out.append(IAC)
                i += 2
            elif cmd in (DO, DONT, WILL, WONT):
                if i + 2 < len(chunk):
                    self._negotiate(cmd, chunk[i + 2])
                i += 3
            elif cmd == SB:             # subnegotiation, skip to SE
                end = chunk.find(bytes([IAC, SE]), i)
                i = len(chunk) if end == -1 else end + 2
            else:
                i += 2
        self.buf += out
        return True

    def read_until(self, pattern: re.Pattern[bytes] | bytes,
                   timeout: float | None = None) -> tuple[int, bytes]:
        """Read until `pattern` matches. Returns (matched?, data consumed).

        A compiled pattern is searched anywhere in the buffer; plain bytes are
        matched literally.
        """
        deadline = time.monotonic() + (timeout if timeout is not None else self.timeout)
        while True:
            if isinstance(pattern, bytes):
                found = self.buf.find(pattern)
                end = found + len(pattern) if found != -1 else -1
            else:
                m = pattern.search(self.buf)
                end = m.end() if m else -1
            if end != -1:
                data = bytes(self.buf[:end])
                del self.buf[:end]
                return 1, data
            if time.monotonic() >= deadline:
                data = bytes(self.buf)
                self.buf.clear()
                return 0, data
            self.sock.settimeout(max(0.1, deadline - time.monotonic()))
            self._fill()


def port_for(extension: int | str) -> str:
    """FXS port carrying an extension, e.g. 107 -> '1/2'."""
    try:
        index = int(extension) - 101
    except (TypeError, ValueError):
        raise GatewayError(f"not an extension number: {extension!r}")
    if not 0 <= index < len(PORTS):
        raise GatewayError(f"extension out of range 101-108: {extension}")
    return PORTS[index]


def extension_for(port: str) -> int:
    """Extension carried by an FXS port, e.g. '1/2' -> 107."""
    try:
        return 101 + PORTS.index(port)
    except ValueError:
        raise GatewayError(f"not an FXS port: {port!r}")


@dataclass
class PortState:
    port: str
    status: str          # Idle, Disconnecting, Busy, Ringing, ...
    tie_type: str

    @property
    def extension(self) -> int:
        return extension_for(self.port)

    @property
    def usable(self) -> bool:
        """Whether a call placed to this port now would be answered.

        Only Idle is usable. "Disconnecting" is the stuck state described in
        the module docstring, and it does not clear by waiting.
        """
        return self.status == "Idle"


class Gateway:
    """A telnet session to the gateway.

    Used as a context manager so the connection is always closed; the AP1100F
    allows very few concurrent sessions and refuses new logins once they are
    used up, which looks like the gateway having died.
    """

    def __init__(self, host: str = HOST, timeout: float = 10.0) -> None:
        self.host = host
        self.timeout = timeout
        self._tn: _Telnet | None = None

    # ── connection ──────────────────────────────────────────────────────

    def __enter__(self) -> "Gateway":
        self.open()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def open(self) -> None:
        try:
            self._tn = _Telnet(self.host, 23, timeout=self.timeout)
        except (OSError, socket.timeout) as exc:
            raise GatewayError(f"cannot reach {self.host}: {exc}") from exc

        try:
            ok, _ = self._tn.read_until(b"login:", timeout=self.timeout)
            if not ok:
                raise GatewayError("no login prompt")
            self._tn.write(USER.encode() + b"\r")
            ok, _ = self._tn.read_until(b"assword:", timeout=self.timeout)
            if not ok:
                raise GatewayError("no password prompt")
            self._tn.write(PASSWORD.encode() + b"\r")
            self._expect_prompt()
        except (OSError, GatewayError) as exc:
            self.close()
            raise GatewayError(f"login to {self.host} failed: {exc}") from exc

    def close(self) -> None:
        if self._tn is not None:
            try:
                self._tn.write(b"exit\r")
            except OSError:
                pass
            self._tn.close()
            self._tn = None

    # ── command plumbing ────────────────────────────────────────────────

    def _expect_prompt(self) -> bytes:
        """Read until the prompt, answering the pager as it appears."""
        if self._tn is None:
            raise GatewayError("not connected")
        collected = bytearray()
        deadline = time.monotonic() + self.timeout
        # Matches whichever comes first, so a pager mid-output is handled
        # without mistaking it for the end of the command.
        either = re.compile(PROMPT.pattern + rb"|" + MORE.pattern, re.I)
        while time.monotonic() < deadline:
            ok, data = self._tn.read_until(either, timeout=deadline - time.monotonic())
            collected += data
            if not ok:
                break
            if MORE.search(data):
                self._tn.write(b" ")
                continue
            return bytes(collected)
        raise GatewayError("gateway did not return to its prompt")

    def send(self, command: str) -> str:
        """Run one CLI command and return its output."""
        if self._tn is None:
            raise GatewayError("not connected")
        self._tn.write(command.encode() + b"\r")
        raw = self._expect_prompt().decode("ascii", "replace")
        # Drop the echoed command and the trailing prompt.
        lines = [line.rstrip() for line in raw.replace("\r", "").split("\n")]
        if lines and command in lines[0]:
            lines = lines[1:]
        if lines and lines[-1].startswith("AP1100F"):
            lines = lines[:-1]
        text = "\n".join(lines)
        # The CLI reports a rejected command in its output rather than by any
        # other signal, so it has to be read back out.
        if "Invalid input command" in text or "Invalid command" in text:
            raise GatewayError(f"gateway rejected {command!r}: {text.strip()}")
        return text

    # ── state ───────────────────────────────────────────────────────────

    def port_states(self) -> dict[str, PortState]:
        """Current status of every FXS port, keyed by port ('0/0'...)."""
        out = self.send("show voice port summary")
        states: dict[str, PortState] = {}
        for line in out.splitlines():
            # " 1/ 2     FXS       Disconnecting   0  0   none ..."
            m = re.match(r"\s*(\d)/\s*(\d)\s+FXS\s+(\S+)\s+\S+\s+\S+\s+(\S+)", line)
            if not m:
                continue
            slot, port, status, tie = m.groups()
            key = f"{slot}/{port}"
            states[key] = PortState(port=key, status=status, tie_type=tie)
        if not states:
            raise GatewayError("could not read the port summary")
        return states

    def port_state(self, port: str) -> PortState:
        states = self.port_states()
        if port not in states:
            raise GatewayError(f"gateway did not report port {port}")
        return states[port]

    # ── the fix ─────────────────────────────────────────────────────────

    def _cycle(self, port: str, down_seconds: float) -> None:
        """Take the port administratively down and back up."""
        self.send("configure terminal")
        try:
            self.send(f"voice-port {port}")
            self.send("shutdown")
            # It has to stay down long enough for the firmware to drop the
            # call state it is holding; bringing it straight back up carries
            # that state across and the port returns still stuck.
            time.sleep(down_seconds)
            self.send("no shutdown")
            self.send("exit")
        finally:
            self.send("exit")

    def clear_port(self, port: str, attempts: int = 3) -> bool:
        """Force a port back to Idle. Returns True if it was not already.

        Cycling the admin state is the only thing that shifts a port out of
        the states it gets stuck in. It is safe on a healthy port — the line
        drops for under a second and comes back Idle — so callers need not
        check first, though this does, to keep the log honest.

        The retry is not defensive padding. A port that has just carried a
        call passes through Busy on its way to Disconnecting, and a cycle run
        during that transition returns it to Busy rather than Idle. Waiting
        longer between tries lets the firmware settle, and in practice the
        second attempt is the one that takes.
        """
        before = self.port_state(port)
        if before.usable:
            return False

        last = before.status
        for attempt in range(attempts):
            self._cycle(port, down_seconds=1.0 + attempt)
            # Coming back up is not instant, and checking too early reads the
            # port mid-transition.
            time.sleep(1.5 + attempt)
            state = self.port_state(port)
            if state.usable:
                return True
            last = state.status

        raise GatewayError(
            f"port {port} is still {last} after {attempts} shutdown cycles. "
            "If a handset on it is off the hook, put it down and try again."
        )

    def power_cycle(self, port: str, down_seconds: float = 6.0) -> str:
        """Hold a port de-energised for a while, then bring it back.

        For a line a fault keeps closed. clear_port() cycles for about a
        second, which is enough to drop a session the firmware is holding but
        not always enough for a short: with no current flowing the loop reads
        open, and the port needs to stay that way long enough to settle in
        Idle rather than seeing the short again on the way up.

        Longer than clear_port() and unconditional — it does not check first,
        because the operator asking for it has already seen the state on the
        handset in front of them.
        """
        self._cycle(port, down_seconds=down_seconds)
        # Coming back up is not instant; reading too early catches the port
        # mid-transition and reports a Busy that is not real.
        time.sleep(2.0)
        return self.port_state(port).status

    def clear_all_ports(self) -> list[str]:
        """Clear every stuck port. Returns the ports that needed it."""
        cleared = []
        for port, state in self.port_states().items():
            if not state.usable:
                self.clear_port(port)
                cleared.append(port)
        return cleared

    def prepare_for_call(self, extension: int | str) -> bool:
        """Make sure the port behind an extension can take a call."""
        return self.clear_port(port_for(extension))

    def cycle_all_ports(self) -> tuple[list[str], list[str]]:
        """Cycle every FXS port, whether or not it looks stuck.

        The soft repair. clear_all_ports() only touches ports that report a
        bad state, which misses the case where the summary says Idle and the
        port still refuses calls. This one does not ask.

        Returns (cycled, still_stuck). The second list is the point of the
        return value: a port that has just carried a call passes through Busy
        on its way down, and a single cycle catching it mid-transition brings
        it back Busy rather than Idle. So the sweep is checked afterwards and
        anything still bad gets the full retrying clear_port() treatment,
        which waits longer between attempts.
        """
        for port in PORTS:
            self._cycle(port, down_seconds=1.0)
        # One settle for the whole sweep rather than per port: they come back
        # in parallel, and waiting on each would multiply the time for no gain.
        time.sleep(2.0)

        still_stuck = []
        for port, state in self.port_states().items():
            if state.usable:
                continue
            try:
                self.clear_port(port)
            except GatewayError:
                # A handset physically off the hook cannot be cleared by
                # anything done from this end. Reported rather than raised:
                # one such port must not hide the fact that the rest worked.
                still_stuck.append(port)
        return list(PORTS), still_stuck

    def reboot(self) -> None:
        """Reboot the gateway.

        The hard repair, for when the firmware is wedged in a way that
        cycling ports does not reach. Every call in progress dies and the
        gateway is gone from the network for roughly a minute.

        Bare "reboot" only. The command also accepts an argument, and
        "reboot 0" leaves the device in BOOT mode — off the network and
        waiting for someone standing next to it — so the argument is never
        passed.
        """
        if self._tn is None:
            raise GatewayError("not connected")
        # No reply is read back. The device goes down while the command is
        # being acknowledged, so waiting for a prompt here always ends in the
        # timeout even though the reboot worked.
        self._tn.write(b"reboot\r")
        time.sleep(0.5)

    def alive(self) -> bool:
        """Whether the CLI is answering. Used to watch a reboot finish."""
        try:
            self.port_states()
            return True
        except (GatewayError, OSError):
            return False


# ── one-shot helpers, for callers that do not want a session ────────────

def clear_extension(extension: int | str) -> bool:
    with Gateway() as gw:
        return gw.prepare_for_call(extension)


def clear_everything() -> list[str]:
    with Gateway() as gw:
        return gw.clear_all_ports()


def status() -> dict[str, PortState]:
    with Gateway() as gw:
        return gw.port_states()


def power_cycle_extension(extension: int | str, down_seconds: float = 6.0) -> str:
    with Gateway() as gw:
        return gw.power_cycle(port_for(extension), down_seconds=down_seconds)


def cycle_everything() -> tuple[list[str], list[str]]:
    with Gateway() as gw:
        return gw.cycle_all_ports()


def reboot_gateway() -> None:
    with Gateway() as gw:
        gw.reboot()


def wait_until_alive(timeout: float = 180.0) -> float:
    """Block until the gateway answers again. Returns how long it took.

    Called after a reboot. The first attempts are expected to fail — the
    device is not merely slow to answer, it is not on the network at all —
    so a refused connection is not treated as an error until the deadline.
    """
    started = time.monotonic()
    deadline = started + timeout
    while time.monotonic() < deadline:
        try:
            with Gateway(timeout=4.0) as gw:
                if gw.alive():
                    return time.monotonic() - started
        except (GatewayError, OSError):
            pass
        time.sleep(5.0)
    raise GatewayError(f"the gateway did not come back within {timeout:.0f}s")


if __name__ == "__main__":
    import sys

    args = sys.argv[1:]
    if args and args[0] == "clear":
        if len(args) > 1:
            port = args[1] if "/" in args[1] else port_for(args[1])
            with Gateway() as gw:
                changed = gw.clear_port(port)
            print(f"{port}: {'cleared' if changed else 'was already idle'}")
        else:
            cleared = clear_everything()
            print(f"cleared: {', '.join(cleared) if cleared else 'nothing was stuck'}")
    else:
        print(f"{'ext':>4}  {'port':5} {'status':14} tie")
        for port, st in sorted(status().items()):
            mark = "" if st.usable else "  <- stuck"
            print(f"{st.extension:>4}  {port:5} {st.status:14} {st.tie_type}{mark}")
