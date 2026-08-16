"""
Is everything that has to be up, up?

There are four things in the path between the button on the page and a phone
ringing, and each fails in its own way:

    server    this process; up by definition if anything is reading this
    network   192.168.100.2 on an interface, which the SIP transport binds to
    pbx       Asterisk running, its transport bound, its manager answering
    gateway   the AP1100F reachable, its CLI answering, its FXS ports idle

Read separately, because a fault in one looks like a fault in another from the
outside. A call that does not go through with the PBX down, with the address
missing, and with a port stuck are three different problems with three
different fixes, and the point of this module is to say which one it is.

The checks are ordered cheapest first and cost very different amounts: reading
an interface list is free, telnetting to the gateway takes about a second. So
the gateway is not polled on the same interval as the rest — see the cache in
scripts/web.py.
"""

from __future__ import annotations

import re
import socket
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import gateway  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
PBX_IP = "192.168.100.2"
GATEWAY_IP = gateway.HOST
AMI_HOST, AMI_PORT = "127.0.0.1", 5038
ASTERISK = "/opt/homebrew/sbin/asterisk"
CONF = ROOT / "etc" / "asterisk.conf"

# ok      working
# warn    working, but something about it needs attention
# down    not working
OK, WARN, DOWN = "ok", "warn", "down"


def _check(name: str, label: str, state: str, detail: str, **extra) -> dict:
    return {"name": name, "label": label, "state": state, "detail": detail, **extra}


# ── the address the transport binds to ──────────────────────────────────

def check_network() -> dict:
    is_win = sys.platform == "win32"
    try:
        cmd = ["ipconfig"] if is_win else ["ifconfig"]
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        return _check("network", "Сеть", DOWN, f"не удалось прочитать интерфейсы: {exc}")

    # Support both Windows and Linux/macOS output formats
    if re.search(rf"(inet |IPv4 Address.*: ){re.escape(PBX_IP)}\b", out):
        return _check("network", "Сеть", OK, f"{PBX_IP} поднят")

    others = re.findall(r"(?:inet |IPv4 Address.*: )(192\.168\.100\.\d+)", out)
    if others:
        return _check("network", "Сеть", DOWN,
                      f"{PBX_IP} отсутствует, вместо него {', '.join(others)}. "
                      f"Поднять: sudo ifconfig en8 alias {PBX_IP} 255.255.255.255 (на Windows настройте адаптер)")
    return _check("network", "Сеть", DOWN,
                  f"{PBX_IP} не поднят ни на одном интерфейсе. "
                  f"Поднять: sudo ifconfig en8 alias {PBX_IP} 255.255.255.255 (на Windows настройте адаптер)")


# ── Asterisk ────────────────────────────────────────────────────────────

def _cli(command: str, timeout: float = 6.0) -> str | None:
    """Run one Asterisk CLI command. None if the PBX is not answering."""
    try:
        is_win = sys.platform == "win32"
        if is_win:
            cmd = ["docker", "exec", "backshot-pbx", "asterisk", "-rx", command]
        else:
            cmd = [ASTERISK, "-C", str(CONF), "-rx", command]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    if "Unable to connect" in result.stdout or "No such container" in result.stderr or result.returncode != 0:
        return None
    return result.stdout


def check_pbx() -> dict:
    version = _cli("core show version")
    if version is None:
        return _check("pbx", "АТС", DOWN,
                      "Asterisk не запущен. Запустить: ./scripts/pbx.sh start")

    # The full line carries the build host, architecture and date — far more
    # than a status card can show. Only the release number is kept.
    first = version.strip().splitlines()[0] if version.strip() else "Asterisk"
    match = re.match(r"(Asterisk [\d.]+)", first)
    release = match.group(1) if match else first[:40]

    # Up but with an unbound transport is the signature of a second Asterisk
    # holding UDP 5060, and it looks exactly like a dead gateway from a call's
    # point of view — so it is reported as a fault rather than as running.
    transports = _cli("pjsip show transports") or ""
    if "transport-udp" not in transports:
        return _check("pbx", "АТС", WARN,
                      f"{release}, но SIP-транспорт не привязан к 5060",
                      version=release)

    channels = _cli("core show channels") or ""
    active = 0
    match = re.search(r"(\d+) active channel", channels)
    if match:
        active = int(match.group(1))

    return _check("pbx", "АТС", OK,
                  f"{release}, транспорт на 5060", version=release, channels=active)


def check_ami() -> dict:
    """The manager port, which is how calls are placed and events are read."""
    try:
        sock = socket.create_connection((AMI_HOST, AMI_PORT), timeout=3)
    except OSError as exc:
        return _check("ami", "Интерфейс управления", DOWN,
                      f"порт {AMI_PORT} не отвечает ({exc.strerror or exc})")
    try:
        sock.settimeout(3)
        banner = sock.recv(200).decode("utf-8", "replace").strip()
    except OSError:
        banner = ""
    finally:
        sock.close()
    if not banner:
        return _check("ami", "Интерфейс управления", WARN,
                      f"порт {AMI_PORT} открыт, но молчит")
    return _check("ami", "Интерфейс управления", OK, banner)


# ── the gateway ─────────────────────────────────────────────────────────

def check_gateway_ping() -> dict:
    is_win = sys.platform == "win32"
    try:
        cmd = ["ping", "-n", "1", "-w", "1000", GATEWAY_IP] if is_win else ["ping", "-c", "1", "-W", "1000", GATEWAY_IP]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError) as exc:
        return _check("gateway", "Шлюз", DOWN, f"проверка не выполнена: {exc}")

    if result.returncode != 0:
        return _check("gateway", "Шлюз", DOWN, f"{GATEWAY_IP} не отвечает на ping")

    match = re.search(r"time[=<]([\d.]+)\s*ms", result.stdout, re.IGNORECASE)
    latency = f", {match.group(1)} мс" if match else ""
    return _check("gateway", "Шлюз", OK, f"{GATEWAY_IP} отвечает{latency}")


def check_ports() -> dict:
    """The FXS ports, read over telnet. The slow check."""
    try:
        states = gateway.status()
    except gateway.GatewayError as exc:
        return _check("ports", "Порты FXS", DOWN, str(exc), ports=[])

    ports = [
        {
            "extension": str(state.extension),
            "port": port,
            "status": state.status,
            "usable": state.usable,
        }
        for port, state in sorted(states.items())
    ]
    stuck = [p for p in ports if not p["usable"]]
    if stuck:
        names = ", ".join(f"{p['extension']} ({p['status']})" for p in stuck)
        return _check("ports", "Порты FXS", WARN,
                      f"занято или залипло: {names}", ports=ports)
    return _check("ports", "Порты FXS", OK,
                  f"все {len(ports)} свободны", ports=ports)


# ── everything at once ──────────────────────────────────────────────────

def fast() -> list[dict]:
    """The checks that cost nothing, safe to run on a short interval."""
    return [check_network(), check_pbx(), check_ami(), check_gateway_ping()]


def slow() -> list[dict]:
    """The telnet check, which takes about a second and holds a session."""
    return [check_ports()]


def overall(checks: list[dict]) -> str:
    """The worst state among the checks — what the header shows."""
    states = {c["state"] for c in checks}
    if DOWN in states:
        return DOWN
    if WARN in states:
        return WARN
    return OK


if __name__ == "__main__":
    marks = {OK: "  ok  ", WARN: " warn ", DOWN: " DOWN "}
    everything = fast() + slow()
    for check in everything:
        print(f"[{marks[check['state']]}] {check['label']:22} {check['detail']}")
    ports = next((c for c in everything if c["name"] == "ports"), None)
    if ports and ports.get("ports"):
        print()
        for port in ports["ports"]:
            flag = "" if port["usable"] else "   <- занят"
            print(f"    {port['extension']}  порт {port['port']}  "
                  f"{port['status']}{flag}")
    print(f"\nсостояние: {overall(everything)}")
