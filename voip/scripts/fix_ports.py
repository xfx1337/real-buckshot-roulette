#!/usr/bin/env python3
"""
Bring FXS ports to the working configuration: polling, no polarity inversion.

    python3 scripts/fix_ports.py --check          show what would change
    python3 scripts/fix_ports.py 1/2              fix one port
    python3 scripts/fix_ports.py 107              same, by extension
    python3 scripts/fix_ports.py --all            fix every misconfigured port

On an FXS port `polarity-inverse` means *generate*: at the end of a session the
gateway flips the line polarity as a hangup signal to the analogue set. Nothing
on the SIP side can see that signal, let alone confirm it, so when the set does
not respond by opening the loop the port stays energised, reads as off-hook,
and answers the next INVITE with "486 Busy Here". `connection polling` decides
the port's state from the session instead of from the line, which is what the
seven working ports do.

Nothing is written to flash. The change lives in RAM until `write` is run on
the gateway, so a bad outcome is undone by rebooting it — which is the point:
verify first, persist after. See --help for the exact follow-up.
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import gateway  # noqa: E402

# How long the port stays administratively down. Below about a second the
# firmware carries the held call state across the cycle and the port comes
# back still busy, which is the failure this whole script exists to undo.
DOWN_SECONDS = 2.0

# A port needs time after coming up before its state means anything; reading
# earlier catches it mid-transition and reports a false Busy.
SETTLE_SECONDS = 30.0


def audit(gw: gateway.Gateway) -> dict[str, dict]:
    """What every port's configuration and state currently is."""
    config = gw.send("show running-config")
    states = gw.port_states()

    found = {}
    # Each "voice-port X/Y" starts a block that runs to the next one.
    for block in re.split(r"(?=^\s*voice-port )", config, flags=re.M):
        match = re.match(r"\s*voice-port (\S+)", block)
        if not match:
            continue
        port = match.group(1)
        if port not in gateway.PORTS:
            continue
        found[port] = {
            "polling": "connection polling" in block,
            "polarity": "polarity-inverse" in block,
            "status": states[port].status if port in states else "?",
        }
    return found


def needs_fixing(entry: dict) -> bool:
    return entry["polarity"] or not entry["polling"]


def fix(gw: gateway.Gateway, port: str, down_seconds: float = DOWN_SECONDS) -> None:
    """Apply the working configuration to one port and cycle it."""
    gw.send("configure terminal")
    try:
        gw.send(f"voice-port {port}")
        # Order matters: remove the inversion before the cycle, so the port
        # comes back up already under the new line handling rather than
        # generating one more inversion on the way down.
        gw.send("no polarity-inverse")
        gw.send("connection polling")
        gw.send("shutdown")
        time.sleep(down_seconds)
        gw.send("no shutdown")
        gw.send("exit")
    finally:
        # Left in configuration mode, the next command in this session is
        # parsed in the wrong context and rejected.
        gw.send("exit")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Fix FXS ports stuck by polarity-inverse.",
        epilog="Changes are not saved to flash. Verify, then run 'write' on "
               "the gateway to persist them.",
    )
    parser.add_argument("ports", nargs="*",
                        help="ports (1/2) or extensions (107)")
    parser.add_argument("--all", action="store_true",
                        help="every port whose configuration differs")
    parser.add_argument("--check", action="store_true",
                        help="report only, change nothing")
    parser.add_argument("--wait", type=float, default=SETTLE_SECONDS,
                        metavar="SECONDS",
                        help=f"settle before re-reading (default {SETTLE_SECONDS:.0f})")
    args = parser.parse_args()

    if not args.ports and not args.all and not args.check:
        parser.error("name a port, or pass --all, or --check")

    try:
        with gateway.Gateway() as gw:
            before = audit(gw)

            print(f"{'порт':6} {'polling':9} {'polarity':9} состояние")
            for port in gateway.PORTS:
                entry = before.get(port)
                if not entry:
                    print(f"{port:6} {'?':9} {'?':9} нет в конфигурации")
                    continue
                flag = "  <- требует правки" if needs_fixing(entry) else ""
                print(f"{port:6} {'да' if entry['polling'] else 'НЕТ':9} "
                      f"{'ДА' if entry['polarity'] else 'нет':9} "
                      f"{entry['status']}{flag}")

            broken = [p for p, e in before.items() if needs_fixing(e)]

            if args.check:
                print(f"\nтребуют правки: {', '.join(broken) if broken else 'нет'}")
                return 0

            if args.all:
                targets = broken
            else:
                targets = []
                for raw in args.ports:
                    # Accept either form; an extension is what the operator
                    # reads off the handset, a port is what the CLI prints.
                    # port_for() raises on anything outside 101-108, and that
                    # is a bad argument rather than a gateway fault — caught
                    # here so it reports as one and changes nothing.
                    try:
                        port = raw if "/" in raw else gateway.port_for(raw)
                    except gateway.GatewayError as exc:
                        print(f"error: {exc}", file=sys.stderr)
                        return 1
                    if port not in gateway.PORTS:
                        print(f"error: неизвестный порт {raw}", file=sys.stderr)
                        return 1
                    targets.append(port)

            if not targets:
                print("\nничего менять не нужно")
                return 0

            print(f"\nправка: {', '.join(targets)}")
            for port in targets:
                print(f"  {port}: no polarity-inverse, connection polling, "
                      f"shutdown/no shutdown …", flush=True)
                fix(gw, port)

            print(f"\nожидание инициализации {args.wait:.0f} с", flush=True)
            time.sleep(args.wait)

            after = audit(gw)
            print(f"\n{'порт':6} {'polling':9} {'polarity':9} состояние")
            failed = []
            for port in targets:
                entry = after.get(port, {})
                usable = entry.get("status") == "Idle"
                if not usable or needs_fixing(entry):
                    failed.append(port)
                print(f"{port:6} {'да' if entry.get('polling') else 'НЕТ':9} "
                      f"{'ДА' if entry.get('polarity') else 'нет':9} "
                      f"{entry.get('status','?')}{'' if usable else '  <- всё ещё занят'}")

            if failed:
                print(f"\nне освободились: {', '.join(failed)}")
                print("Проверьте, не снята ли трубка на этих линиях.")
                print("Конфигурация НЕ сохранена — перезагрузка шлюза откатит правку.")
                return 1

            print("\nвсе порты Idle и приведены к рабочему виду.")
            print("Проверьте двумя звонками подряд на один аппарат:")
            print("    python3 scripts/call.py 107 beep      # и повторить")
            print("Диагностическую ценность имеет ВТОРОЙ звонок — первый")
            print("работал и до правки.")
            print("\nЗатем зафиксировать во flash, на самом шлюзе:")
            print("    telnet 192.168.100.3   (root/router)")
            print("    write")
            return 0

    except gateway.GatewayError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nпрервано; конфигурация не сохранена во flash", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
