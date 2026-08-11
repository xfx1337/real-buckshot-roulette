#!/usr/bin/env python3
"""Ring a handset and play something to it when it answers.

The CLI's "originate" gives up after 30 seconds and offers no way to change
that, which is not long enough to walk to a phone. AMI's Originate takes a
Timeout, so this uses it.

    python3 scripts/ring.py 107                 # ring for two minutes
    python3 scripts/ring.py 107 --seconds 300
    python3 scripts/ring.py 107 --to 601@from-gateway

The PBX must be running (./scripts/run-asterisk.sh -d).
"""
import argparse
import socket
import sys
import time

HOST, PORT = "127.0.0.1", 5038
USERNAME, SECRET = "digits", "backshot-ami"


def ami(action_lines, read_for=3.0):
    """Send one AMI action after logging in, and return what comes back."""
    sock = socket.create_connection((HOST, PORT), timeout=10)
    sock.settimeout(1.0)
    sock.sendall(
        f"Action: Login\r\nUsername: {USERNAME}\r\n"
        f"Secret: {SECRET}\r\nEvents: off\r\n\r\n".encode()
    )
    time.sleep(0.4)
    try:
        sock.recv(8192)
    except socket.timeout:
        pass

    sock.sendall(("\r\n".join(action_lines) + "\r\n\r\n").encode())

    out = b""
    end = time.time() + read_for
    while time.time() < end:
        try:
            chunk = sock.recv(4096)
        except socket.timeout:
            continue
        if not chunk:
            break
        out += chunk
    sock.close()
    return out.decode("utf-8", "replace")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("number", help="handset extension, 101-108")
    parser.add_argument(
        "--seconds", type=int, default=120,
        help="how long to ring before giving up (default 120)",
    )
    parser.add_argument(
        "--to", default="lobby@to-handset",
        help="dialplan target the handset reaches on answering",
    )
    args = parser.parse_args()

    extension, _, context = args.to.partition("@")
    context = context or "to-handset"

    reply = ami([
        "Action: Originate",
        f"Channel: PJSIP/{args.number}@addpac",
        f"Context: {context}",
        f"Exten: {extension}",
        "Priority: 1",
        # AMI counts this in milliseconds, unlike almost everything else.
        f"Timeout: {args.seconds * 1000}",
        f"CallerID: PBX <{args.number}>",
        # Without this the action blocks until the call is answered or times
        # out, holding the connection open for the full ring.
        "Async: true",
    ])

    if "Response: Error" in reply:
        message = "unknown error"
        for line in reply.split("\r\n"):
            if line.startswith("Message: "):
                message = line[9:]
        print(f"не вышло: {message}", file=sys.stderr)
        return 1

    print(f"звоню на {args.number} — {args.seconds} с, снимай трубку")
    return 0


if __name__ == "__main__":
    sys.exit(main())
