#!/usr/bin/env python3
"""
Ring a handset and play a file into it.

    ./scripts/call.py                    pick a phone and a sound, then call
    ./scripts/call.py 107 alarm          call extension 107, play "alarm"
    ./scripts/call.py 107 alarm --loop   repeat the file until they hang up
    ./scripts/call.py --list             show the phones and the sounds

The part that matters is what happens before the call is placed. This gateway
leaves an FXS port in "Disconnecting" after a call and never brings it back on
its own, so the second call to a handset is refused with "486 Busy Here" — the
one-call-per-session behaviour. Every call here therefore starts by clearing
the port over telnet, which takes about two seconds and makes the failure
impossible rather than merely unlikely. See scripts/gateway.py.
"""

from __future__ import annotations

import argparse
import socket
import sys
import time
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import gateway  # noqa: E402
import sounds   # noqa: E402

AMI_HOST, AMI_PORT = "127.0.0.1", 5038
AMI_USER, AMI_SECRET = "caller", "voip-local"

# How long the handset is allowed to ring before giving up.
RING_SECONDS = 30


class CallError(RuntimeError):
    pass


# ── AMI ─────────────────────────────────────────────────────────────────

class Manager:
    """Just enough of the Asterisk Manager Interface to place a call."""

    def __init__(self, timeout: float = 10.0) -> None:
        self.timeout = timeout
        try:
            self.sock = socket.create_connection((AMI_HOST, AMI_PORT), timeout=timeout)
        except OSError as exc:
            raise CallError(
                f"cannot reach the PBX manager on {AMI_HOST}:{AMI_PORT} ({exc}).\n"
                "Is it running?  ./scripts/pbx.sh status"
            ) from exc
        self.buf = b""
        self._read_greeting()
        self._login()

    def close(self) -> None:
        try:
            self.sock.sendall(b"Action: Logoff\r\n\r\n")
            self.sock.close()
        except OSError:
            pass

    def __enter__(self) -> "Manager":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def _read_greeting(self) -> str:
        """Consume the banner.

        It is a single line — "Asterisk Call Manager/9.0.0\\r\\n" — and not a
        block: waiting for the usual blank-line terminator here hangs until the
        timeout and looks exactly like a PBX that is not answering.
        """
        deadline = time.monotonic() + self.timeout
        while b"\r\n" not in self.buf:
            if time.monotonic() >= deadline:
                raise CallError("the PBX manager sent no greeting")
            self.sock.settimeout(max(0.1, deadline - time.monotonic()))
            try:
                chunk = self.sock.recv(4096)
            except socket.timeout:
                continue
            if not chunk:
                raise CallError("the PBX manager closed the connection")
            self.buf += chunk
        line, self.buf = self.buf.split(b"\r\n", 1)
        return line.decode("utf-8", "replace")

    def _read_block(self, timeout: float | None = None) -> str:
        """Read one \\r\\n\\r\\n-terminated block."""
        deadline = time.monotonic() + (timeout or self.timeout)
        while b"\r\n\r\n" not in self.buf:
            if time.monotonic() >= deadline:
                raise CallError("the PBX manager stopped responding")
            self.sock.settimeout(max(0.1, deadline - time.monotonic()))
            try:
                chunk = self.sock.recv(4096)
            except socket.timeout:
                continue
            if not chunk:
                raise CallError("the PBX manager closed the connection")
            self.buf += chunk
        block, self.buf = self.buf.split(b"\r\n\r\n", 1)
        return block.decode("utf-8", "replace")

    def _send(self, **fields: str) -> None:
        packet = "".join(f"{k}: {v}\r\n" for k, v in fields.items()) + "\r\n"
        self.sock.sendall(packet.encode())

    def _login(self) -> None:
        # Events must be asked for explicitly. Without this the connection
        # carries command replies only, and an async Originate's
        # OriginateResponse — the thing that says whether the call worked —
        # never arrives.
        self._send(Action="Login", Username=AMI_USER, Secret=AMI_SECRET,
                   Events="on")
        reply = self._read_block()
        if "Success" not in reply:
            raise CallError(
                "the PBX manager rejected the login.\n"
                "Check the [caller] account in etc/manager.conf."
            )

    def channels(self) -> list[dict[str, str]]:
        """Every live channel, as the manager reports it."""
        self._send(Action="CoreShowChannels")
        found: list[dict[str, str]] = []
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            try:
                block = self._read_block(timeout=min(3.0, deadline - time.monotonic()))
            except CallError:
                break
            fields = {
                line.split(":", 1)[0].strip().lower(): line.split(":", 1)[1].strip()
                for line in block.splitlines() if ":" in line
            }
            event = fields.get("event", "")
            # The list ends with its own event rather than with a blank reply,
            # so the loop has to watch for it or it waits out the timeout.
            if event == "CoreShowChannelsComplete":
                break
            if event == "CoreShowChannel":
                found.append(fields)
        return found

    def hangup(self, channel: str) -> None:
        """End one channel.

        This is the right way to drop a call from the interface: Asterisk
        tears the session down and the gateway is told, so the FXS port is
        released the same way it would be by a handset going back on the
        hook. Cycling the port instead ends the call by pulling the line out
        from under it, which works but leaves Asterisk to notice on its own.
        """
        self._send(Action="Hangup", Channel=channel)
        try:
            reply = self._read_block(timeout=5.0)
        except CallError as exc:
            raise CallError(f"АТС не ответила на завершение вызова: {exc}") from exc
        if "Success" not in reply:
            message = next((l.split(":", 1)[1].strip()
                            for l in reply.splitlines()
                            if l.lower().startswith("message:")), "отказано")
            raise CallError(f"не удалось завершить вызов: {message}")

    def originate(self, channel: str, context: str, exten: str,
                  variables: dict[str, str], timeout_ms: int) -> str:
        """Place a call and wait for it to be answered or to fail.

        Async, so the reply comes back as an OriginateResponse event rather
        than blocking the connection for the length of the call.
        """
        action_id = f"call-{int(time.monotonic() * 1000)}"
        fields = {
            "Action": "Originate",
            "ActionID": action_id,
            "Channel": channel,
            "Context": context,
            "Exten": exten,
            "Priority": "1",
            "CallerID": "PBX <100>",
            "Timeout": str(timeout_ms),
            "Async": "true",
        }
        for index, (key, value) in enumerate(variables.items(), 1):
            fields[f"Variable{index}" if index > 1 else "Variable"] = f"{key}={value}"

        self._send(**fields)

        # Long enough to outlast the ring, plus room for the answer itself.
        deadline = time.monotonic() + (timeout_ms / 1000) + 15
        while time.monotonic() < deadline:
            # Read in short hops. A block that does not arrive is not an
            # error while the call is still ringing; only the outer deadline
            # decides that, so a quiet stretch is waited through rather than
            # mistaken for a dead connection.
            try:
                block = self._read_block(timeout=min(5.0, deadline - time.monotonic()))
            except CallError:
                continue
            if action_id not in block:
                continue                      # some other call's traffic
            fields = {
                line.split(":", 1)[0].strip().lower():
                    line.split(":", 1)[1].strip()
                for line in block.splitlines() if ":" in line
            }
            # An async Originate answers twice: immediately, to say the request
            # was accepted, and later with an OriginateResponse event carrying
            # what actually happened. Only the second one is an outcome —
            # treating the first as success reports every unanswered call as
            # having worked.
            if "originateresponse" not in block.lower():
                if fields.get("response") == "Error":
                    raise CallError(fields.get("message", "the PBX refused the call"))
                continue
            return fields.get("response", "Unknown")
        raise CallError("the PBX never reported how the call ended")


# ── placing a call ──────────────────────────────────────────────────────

@dataclass
class Result:
    ok: bool
    detail: str
    port_was_stuck: bool


def place(extension: str, sound: sounds.Sound, loop: bool = False,
          ring_seconds: int = RING_SECONDS, verbose: bool = True,
          prepared: bool = False) -> Result:
    """Clear the port, ring the handset, play the file.

    prepared=True skips the clear, for a caller that has just de-energised
    the line itself. On a handset whose switches keep the loop closed, the
    port is only free for a few seconds after that, and spending them on a
    second cycle here is what lets the short take it back before the INVITE
    lands.
    """
    def say(message: str) -> None:
        if verbose:
            print(message, flush=True)

    port = gateway.port_for(extension)

    # The port has to be cleared first, not afterwards. Clearing after a call
    # would leave the gateway stuck for as long as the process is not running,
    # and would do nothing about a port already stuck when we arrive.
    was_stuck = False
    if not prepared:
        say(f"port {port}: checking")
        try:
            was_stuck = gateway.clear_extension(extension)
        except gateway.GatewayError as exc:
            raise CallError(f"could not prepare port {port}: {exc}") from exc
        say(f"port {port}: {'cleared, it was stuck' if was_stuck else 'idle'}")

    say(f"calling {extension}, playing {sound.name} ({sound.seconds:.0f}s)")
    with Manager() as ami:
        response = ami.originate(
            channel=f"PJSIP/{extension}@addpac",
            context="play-file",
            exten="loop" if loop else "play",
            variables={"SOUNDFILE": sound.name},
            timeout_ms=ring_seconds * 1000,
        )

    if response.lower() == "success":
        return Result(True, "answered, audio playing", was_stuck)

    # A failure here is the handset not picking up, or the port going busy
    # again — worth separating, because they need different things done.
    try:
        state = gateway.status()[port].status
    except gateway.GatewayError:
        state = "unknown"
    if state == "Idle":
        detail = "nobody answered"
    else:
        detail = f"the gateway refused the call, port {port} is {state}"
    return Result(False, detail, was_stuck)


# ── choosing, when nothing was given on the command line ────────────────

def _pick_phone() -> str:
    print("Phones:\n")
    states = gateway.status()
    for port, state in sorted(states.items()):
        note = "" if state.usable else f"   [{state.status}, will be cleared]"
        print(f"  {state.extension}   port {port}{note}")
    print()
    while True:
        choice = input("Extension (101-108): ").strip()
        try:
            gateway.port_for(choice)
            return choice
        except gateway.GatewayError as exc:
            print(f"  {exc}")


def _pick_sound(library: dict[str, sounds.Sound]) -> sounds.Sound:
    print("\nSounds:\n")
    for index, sound in enumerate(library.values(), 1):
        print(f"  {index}. {sound.name:<24} {sound.seconds:6.1f}s")
    print()
    while True:
        choice = input("Sound (number or name): ").strip()
        try:
            return sounds.resolve(choice, library)
        except sounds.SoundError as exc:
            print(f"  {exc}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Ring a handset and play a sound file into it.",
        epilog="With no arguments it asks which phone and which sound.",
    )
    parser.add_argument("extension", nargs="?", help="101-108")
    parser.add_argument("sound", nargs="?",
                        help="a name from sounds/, its number, or a path")
    parser.add_argument("--loop", action="store_true",
                        help="repeat the file until the handset hangs up")
    parser.add_argument("--ring", type=int, default=RING_SECONDS, metavar="SECONDS",
                        help=f"how long to ring before giving up (default {RING_SECONDS})")
    parser.add_argument("--list", action="store_true",
                        help="show the phones and the sounds, and place no call")
    args = parser.parse_args()

    try:
        library = sounds.library()
    except sounds.SoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.list:
        print("Phones:\n")
        for port, state in sorted(gateway.status().items()):
            flag = "" if state.usable else f"   [{state.status}]"
            print(f"  {state.extension}   port {port}{flag}")
        print(f"\nSounds in {sounds.SOURCE_DIR}:\n")
        if not library:
            print("  none — drop an mp3 or a wav in there")
        for index, sound in enumerate(library.values(), 1):
            print(f"  {index}. {sound.name:<24} {sound.seconds:6.1f}s")
        return 0

    if not library:
        print(f"error: no audio files in {sounds.SOURCE_DIR}", file=sys.stderr)
        print("Drop an mp3 or a wav in there first.", file=sys.stderr)
        return 1

    try:
        extension = args.extension or _pick_phone()
        gateway.port_for(extension)             # validated before anything else
        sound = sounds.resolve(args.sound, library) if args.sound else _pick_sound(library)
    except (gateway.GatewayError, sounds.SoundError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except (KeyboardInterrupt, EOFError):
        print()
        return 130

    try:
        result = place(extension, sound, loop=args.loop, ring_seconds=args.ring)
    except CallError as exc:
        print(f"\nerror: {exc}", file=sys.stderr)
        return 1

    print(f"\n{'ok' if result.ok else 'failed'}: {result.detail}")
    return 0 if result.ok else 1


if __name__ == "__main__":
    sys.exit(main())
