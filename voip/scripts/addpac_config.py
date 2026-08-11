#!/usr/bin/env python3
"""Push configuration to the AddPac AP1100F gateway over telnet.

`addpac.py` is deliberately read-only. This script is the write side: it enters
the gateway's configure mode, applies the settings the PBX needs, and saves them
to flash. Without the save, everything here is lost on the next power cycle.

    ./addpac_config.py            # show what would be sent, change nothing
    ./addpac_config.py --apply    # apply, then save to flash

The problem it fixes: a handset takes one call and then refuses every call after
it with 503. Three separate settings kept a port out of Idle — two timers that
ran for a minute after each hangup, and PLAR, which re-seized the port for as
long as the handset stayed off the cradle.

What it sets, and why:

  * `no forced-clear-down` on all eight FXS ports. This is the latch. The
    factory setting, `forced-clear-down -55 60`, tears a call down only after
    sixty continuous seconds below -55 dB. Between the far end hanging up and
    that timer expiring the port sits in "Disconnecting" and answers new calls
    with 503 — which, with a converter or a handset holding the loop closed,
    means it never clears at all. Asterisk signals the end of a call over SIP;
    the gateway does not need to infer it from silence.

  * `timing fxs-powerdown-duration 1` on all eight. After a call the port drops
    its line feed for this many seconds before offering dial tone again. Two
    seconds is longer than the panel waits before placing the next call, so the
    second call arrives while the port is still down.

  * `timing fxs-reorder-duration 1` and `timing fxs-linelock-duration 1`, both
    down from thirty. When the far end hangs up on an off-hook handset the port
    cannot go on-hook — there is nobody to hang up — so it plays reorder tone,
    then line-lock tone, and stays out of Idle for the whole of both. A minute
    of that is a minute of 503s. At one second each the port reports Idle
    immediately after a hangup.

  * `no connection` on all eight, removing `connection plar 700`. PLAR sends a
    port to extension 700 the moment its handset goes off-hook, and 700 answers
    and holds the line open — so an off-hook handset pinned its port to Busy
    indefinitely and every call to it was refused with 503. Clearing the port
    did not help: PLAR re-seized it about a second later, because nothing about
    the off-hook handset had changed. Without PLAR the port stays Idle with the
    handset off the cradle, and the PBX can ring it whenever it likes.

    The cost is that lifting a handset no longer opens a channel by itself,
    which is what `scripts/phone_digits.py` relied on to read a rotary dial
    through a pulse-to-DTMF converter: the converter signals into an
    established line, so with no PLAR there is nothing for it to signal into
    until a call exists. Reading digits that way needs PLAR back on the port in
    question — at the price of that port refusing incoming calls whenever its
    handset is off-hook.

  * `no register e164`. The gateway carries one set of provider credentials
    (partner4@permngn.usi.ru) for all eight numbers, left over from a dead
    provider. Asterisk has no matching account, so all eight registrations fail
    and retry every twenty seconds. Calls run over the static IP trunk in both
    directions and need no registration at all.

  * `no ip-share enable`. NAT between the WAN side (ether0.0) and the LAN side
    (ether1.0) serves nothing on a direct cable to the Mac, and rewriting
    addresses on the voice path is a way to lose RTP.

The gateway echoes each line back, so anything it rejects shows up in the
output rather than failing silently.
"""
import argparse
import re
import sys

from addpac import Gateway, PROMPT

PORTS = ["0/0", "0/1", "0/2", "0/3", "1/0", "1/1", "1/2", "1/3"]

# Asterisk's address on the direct segment.
SIP_SERVER = "192.168.100.2"

# Seconds the port keeps its line feed down between calls. The panel can place a
# second call about three seconds after a hangup, so this has to be well under
# that.
POWERDOWN_SECONDS = 1

# Seconds of reorder and line-lock tone after the far end hangs up on a handset
# that is still off-hook. The port is not Idle while either plays, so the
# firmware minimum is what we want; the factory setting is thirty of each.
TONE_SECONDS = 1


def config_lines():
    """The configure-mode commands, in the order they must be sent."""
    lines = ["configure terminal"]

    # No NAT on a direct cable; it only rewrites the voice path.
    lines.append("no ip-share enable")

    for port in PORTS:
        lines += [
            f"voice-port {port}",
            # The 503-after-first-call fix: stop inferring hangup from silence.
            "no forced-clear-down",
            f"timing fxs-powerdown-duration {POWERDOWN_SECONDS}",
            f"timing fxs-reorder-duration {TONE_SECONDS}",
            f"timing fxs-linelock-duration {TONE_SECONDS}",
            # No PLAR: an off-hook handset must not hold its port occupied.
            "no connection",
            "exit",
        ]

    lines += [
        "sip-ua",
        # Stop the failing registration loop. The trunk is static-IP in both
        # directions, so the PBX still reaches every handset. The negation takes
        # no argument — `no register e164` is rejected.
        "no register",
        f"sip-server {SIP_SERVER}",
        "exit",
        "exit",
    ]
    return lines


def apply(gateway, lines):
    """Send each line, printing anything the gateway says back about it."""
    rejected = []
    for line in lines:
        reply = gateway.run(line, limit=4.0)
        print(f"> {line}")
        body = "\n".join(
            l for l in reply.splitlines()[1:]
            if l.strip() and not PROMPT.search(l.encode())
        )
        if body.strip():
            print(body)
        if "Invalid input" in reply or "% Unknown" in reply:
            rejected.append(line)
    return rejected


def save(gateway):
    """Write the running config to flash. `write` asks for confirmation."""
    print("> write")
    gateway.sock.sendall(b"write\r\n")
    reply = gateway.read_until(re.compile(rb"\[y/n\]|\(y/n\)|\?"), limit=6.0)
    print(reply.decode("latin-1", "replace").replace("\r", ""))
    gateway.sock.sendall(b"y\r\n")
    print(gateway.read_until(PROMPT, limit=20.0)
          .decode("latin-1", "replace").replace("\r", ""))


def main():
    parser = argparse.ArgumentParser(
        description="Configure the AddPac AP1100F gateway.")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="send the commands and save to flash; without it, only print them",
    )
    args = parser.parse_args()

    lines = config_lines()

    if not args.apply:
        print("Would send (re-run with --apply):\n")
        for line in lines:
            print(f"  {line}")
        print("  write / y")
        return 0

    gateway = Gateway()
    gateway.login()
    rejected = apply(gateway, lines)
    save(gateway)
    print("\n--- show voice port summary")
    print(gateway.run("show voice port summary", limit=10.0))
    gateway.close()

    if rejected:
        print("\nThe gateway rejected these lines — this firmware may not "
              "carry them:")
        for line in rejected:
            print(f"  {line}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
