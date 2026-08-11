#!/usr/bin/env python3
"""Report every digit dialled on a handset attached to the AddPac gateway.

Digits reach this script two ways, and both are handled:

  * as the number a handset dialled before the call was set up, which the
    gateway puts in its INVITE — one event per call;
  * as keys pressed once the call is up, which arrive as RFC 2833 DTMF —
    one event per key, and the only way a rotary dial behind a pulse-to-DTMF
    converter can be read, since the converter needs an open line to signal
    into.

For the second kind, point the port's PLAR at extension 700:

    python3 scripts/addpac.py "configure terminal" "voice-port 1/2" \
        "connection plar 700" "exit" "exit" "write" "y"

Lifting the handset then opens a channel that stays up, and every digit
turns into a line here.

Usage:

    python3 scripts/phone_digits.py                     # print digits
    python3 scripts/phone_digits.py --post URL          # and POST each one
    python3 scripts/phone_digits.py --group             # buffer into numbers

The PBX must be running (`docker compose up -d`).
"""
import argparse
import json
import socket
import sys
import time
import urllib.error
import urllib.request

HOST = "127.0.0.1"
PORT = 5038
USERNAME = "digits"
SECRET = "backshot-ami"

# The gateway's FXS ports, as numbered in its own CLI.
PORT_NAMES = {
    "101": "0/0",
    "102": "0/1",
    "103": "0/2",
    "104": "0/3",
    "105": "1/0",
    "106": "1/1",
    "107": "1/2",
    "108": "1/3",
}


class AMI:
    """A minimal Asterisk Manager Interface client — login, then read events."""

    def __init__(self, host=HOST, port=PORT):
        self.sock = socket.create_connection((host, port), timeout=10)
        self.sock.settimeout(1.0)
        self.buffer = b""

    def login(self, username=USERNAME, secret=SECRET):
        self.sock.sendall(
            f"Action: Login\r\nUsername: {username}\r\n"
            f"Secret: {secret}\r\nEvents: on\r\n\r\n".encode()
        )
        for packet in self.packets(limit=5.0):
            if packet.get("response") == "Success":
                return True
            if packet.get("response") == "Error":
                raise RuntimeError(packet.get("message", "AMI login refused"))
        raise RuntimeError("no reply to AMI login")

    def packets(self, limit=None):
        """Yield AMI packets as dicts with lower-case keys."""
        end = time.time() + limit if limit else None
        while end is None or time.time() < end:
            while b"\r\n\r\n" not in self.buffer:
                try:
                    chunk = self.sock.recv(4096)
                except socket.timeout:
                    if end is not None and time.time() >= end:
                        return
                    continue
                if not chunk:
                    return
                self.buffer += chunk
            raw, self.buffer = self.buffer.split(b"\r\n\r\n", 1)
            packet = {}
            for line in raw.decode("utf-8", "replace").split("\r\n"):
                if ": " in line:
                    key, value = line.split(": ", 1)
                    packet[key.lower()] = value
            if packet:
                yield packet

    def close(self):
        self.sock.close()


def describe_port(caller_id, channel=""):
    """Name the handset a digit came from, as 'number (slot/port)'.

    A port dialling through PLAR does not always put its number in the INVITE,
    so fall back to the channel name — which is unique per call even when the
    port behind it is anonymous.
    """
    slot = PORT_NAMES.get(caller_id)
    if slot:
        return f"{caller_id} ({slot})"
    if caller_id:
        return caller_id
    return channel or "unknown"


def post(url, payload):
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        urllib.request.urlopen(request, timeout=3).close()
    except (urllib.error.URLError, OSError) as exc:
        print(f"  ! POST failed: {exc}", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--post", metavar="URL", help="POST each digit as JSON")
    parser.add_argument(
        "--group",
        action="store_true",
        help="collect digits into a number, flushed on # or after a pause",
    )
    parser.add_argument(
        "--gap",
        type=float,
        default=3.0,
        help="seconds of silence that ends a number with --group (default 3)",
    )
    args = parser.parse_args()

    try:
        ami = AMI()
        ami.login()
    except (OSError, RuntimeError) as exc:
        print(f"cannot reach the PBX on {HOST}:{PORT} — {exc}", file=sys.stderr)
        print("is it running?  docker compose up -d", file=sys.stderr)
        return 1

    print(f"listening on {HOST}:{PORT} — press Ctrl-C to stop")

    # Per-channel buffer of digits, used only with --group.
    pending = {}
    # Calls already reported, so a call is announced once.
    announced = set()

    def flush(channel, caller=""):
        digits = pending.pop(channel, None)
        if digits and digits["digits"]:
            number = "".join(digits["digits"])
            stamp = time.strftime("%H:%M:%S")
            # A flush on a pause has no event to read the caller from, so the
            # one stored when the first digit arrived is used instead.
            caller = caller or digits["caller"]
            print(f"[{stamp}] {describe_port(caller, channel)} dialled {number}")
            if args.post:
                post(args.post, {"port": caller, "channel": channel,
                                 "number": number})

    try:
        while True:
            for event in ami.packets(limit=1.0):
                name = event.get("event", "")
                channel = event.get("channel", "")
                caller = event.get("calleridnum", "")
                stamp = time.strftime("%H:%M:%S")

                # A number the gateway collected before the call was set up.
                if name == "Newchannel":
                    exten = event.get("exten", "")
                    # Local channels come in halves named "...;1" and "...;2",
                    # which would otherwise report the same call twice.
                    base = channel.rsplit(";", 1)[0]
                    if base in announced:
                        continue
                    if exten and exten not in ("s", ""):
                        announced.add(base)
                        print(f"[{stamp}] {describe_port(caller, channel)} "
                              f"called {exten}")
                        if args.post:
                            post(args.post, {"port": caller, "channel": channel,
                                             "number": exten})

                # A key pressed during an established call.
                elif name == "DTMFEnd":
                    digit = event.get("digit", "")
                    if not digit:
                        continue
                    # Asterisk reports the key on both legs of a bridge, so the
                    # same press arrives twice. Only the receiving leg counts.
                    if event.get("direction", "Received") != "Received":
                        continue
                    if not args.group:
                        print(f"[{stamp}] {describe_port(caller, channel)} "
                              f"pressed {digit}")
                        if args.post:
                            post(args.post, {"port": caller, "channel": channel,
                                             "digit": digit})
                        continue
                    if digit == "#":
                        flush(channel, caller)
                    else:
                        entry = pending.setdefault(
                            channel, {"digits": [], "at": 0.0, "caller": caller}
                        )
                        entry["digits"].append(digit)
                        entry["at"] = time.time()

                elif name == "Hangup":
                    announced.discard(channel.rsplit(";", 1)[0])
                    # flush() pops the buffer either way, so a hung-up channel
                    # never leaves one behind.
                    flush(channel, caller)

            # With --group, a pause also ends a number.
            if args.group:
                now = time.time()
                for channel in list(pending):
                    if now - pending[channel]["at"] > args.gap:
                        flush(channel)
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        ami.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
