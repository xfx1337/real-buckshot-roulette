"""
Reading and changing the gateway's own configuration.

scripts/gateway.py holds the telnet client and the one repair that matters
(cycling a stuck port). This module is the rest of the CLI surface: reading a
port's full settings, changing them, reading the dial-peer table that maps
extensions to ports, and pulling the diagnostics the firmware exposes.

Two rules run through all of it.

Whitelists, not free text. Nothing here passes an operator's string to the
CLI. A parameter is looked up in PARAMETERS, its value is checked against the
range the firmware itself reports, and the command is assembled from parts
this file controls. The gateway has no notion of a restricted account — a
telnet session can erase the configuration or reboot the device — so the web
interface must never become a way to type arbitrary commands into it.

Nothing is saved to flash unless asked. Every change lands in the running
configuration only, which means a mistake is undone by power-cycling the
gateway. save_to_flash() is the separate, deliberate step that gives that up.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import gateway  # noqa: E402


class AdminError(RuntimeError):
    pass


# ── the parameters the interface is allowed to change ───────────────────
#
# Ranges are the firmware's own, read out of its contextual help. Anything
# not in this table cannot be set through the web interface at all.

@dataclass
class Parameter:
    key: str            # what the API calls it
    label: str          # what the page shows
    command: str        # the CLI command, without its value
    kind: str           # "int" | "flag" | "choice"
    minimum: int = 0
    maximum: int = 0
    choices: tuple[str, ...] = ()
    unit: str = ""
    help: str = ""


PARAMETERS: dict[str, Parameter] = {
    "connection": Parameter(
        key="connection", label="Режим соединения", command="connection",
        kind="choice",
        choices=("polling", "plar", "plar-with-polling",
                 "trunk-initiate", "trunk-answer"),
        help="polling — состояние порта определяется сессией, а не линией. "
             "Рабочий режим для связки с Asterisk.",
    ),
    "polarity_inverse": Parameter(
        key="polarity_inverse", label="Переполюсовка (polarity-inverse)",
        command="polarity-inverse", kind="flag",
        help="На FXS порт ГЕНЕРИРУЕТ переполюсовку как сигнал отбоя. "
             "Asterisk этот аналоговый сигнал не видит — порт может залипнуть.",
    ),
    "caller_id": Parameter(
        key="caller_id", label="Определитель номера", command="caller-id enable",
        kind="flag", help="Передача Caller ID на аналоговый аппарат.",
    ),
    "echo_cancel": Parameter(
        key="echo_cancel", label="Эхоподавление", command="echo-cancel enable",
        kind="flag",
    ),
    "clear_down_tone_detect": Parameter(
        key="clear_down_tone_detect", label="Детект тона отбоя",
        command="clear-down-tone-detect", kind="flag",
        help="Страховка от мёртвых сессий. Включать только после того, как "
             "устранена основная причина залипания.",
    ),
    "timeout_tterm": Parameter(
        key="timeout_tterm", label="Таймаут разговора", command="timeout tterm",
        kind="int", minimum=0, maximum=64000, unit="с",
        help="Максимальная длительность вызова.",
    ),
    "clear_down_delay": Parameter(
        key="clear_down_delay", label="Задержка отбоя",
        command="timing clear-down-delay", kind="int",
        minimum=0, maximum=600, unit="с",
    ),
    "fxs_reorder": Parameter(
        key="fxs_reorder", label="Длительность тона reorder",
        command="timing fxs-reorder-duration", kind="int",
        minimum=0, maximum=3600, unit="с",
    ),
    "fxs_linelock": Parameter(
        key="fxs_linelock", label="Длительность тона line-lock",
        command="timing fxs-linelock-duration", kind="int",
        minimum=0, maximum=3600, unit="с",
    ),
    "fxs_powerdown": Parameter(
        key="fxs_powerdown", label="Разрыв питания линии (CPC)",
        command="timing fxs-powerdown-duration", kind="int",
        minimum=0, maximum=3600, unit="с",
        help="Обесточивание линии как сигнал отбоя. 1 с — рабочее значение.",
    ),
    "input_gain": Parameter(
        key="input_gain", label="Усиление входа", command="input gain",
        kind="int", minimum=-14, maximum=14, unit="дБ",
    ),
    "output_gain": Parameter(
        key="output_gain", label="Усиление выхода", command="output gain",
        kind="int", minimum=-14, maximum=14, unit="дБ",
    ),
}

# How "show voice port X/Y" names each setting. The command used to change a
# value and the label used to report it are different strings in this
# firmware, so the mapping has to be written out.
SHOW_FIELDS = {
    "tie connection": "connection",
    "polarity inverse": "polarity_inverse",
    "clear down tone detect": "clear_down_tone_detect",
    "clear down delay": "clear_down_delay",
    "reorder tone duration": "fxs_reorder",
    "line lock tone duration": "fxs_linelock",
    "power down duration": "fxs_powerdown",
    "input gain": "input_gain",
    "output gain": "output_gain",
    "echo cancellation": "echo_cancel",
    "status": "status",
    "line type": "line_type",
    "description": "description",
    "ring frequency": "ring_frequency",
    "ring cadence": "ring_cadence",
}


def _clean(value: str) -> str:
    """Strip the units the CLI prints so a value can be compared and edited."""
    value = value.strip()
    value = re.sub(r"\s*(db|dB|sec|Hz|msec)\b.*$", "", value).strip()
    return value


def port_detail(gw: gateway.Gateway, port: str) -> dict:
    """Everything "show voice port X/Y" reports, as a dict."""
    if port not in gateway.PORTS:
        raise AdminError(f"неизвестный порт: {port}")
    out = gw.send(f"show voice port {port}")

    detail: dict[str, object] = {"port": port,
                                 "extension": str(gateway.extension_for(port))}
    for line in out.splitlines():
        if "=" not in line:
            continue
        name, _, value = line.partition("=")
        key = SHOW_FIELDS.get(name.strip().lower())
        if not key:
            continue
        raw = _clean(value)
        if key in ("polarity_inverse", "clear_down_tone_detect", "echo_cancel"):
            detail[key] = raw.lower() == "enabled"
        elif key in ("clear_down_delay", "fxs_reorder", "fxs_linelock",
                     "fxs_powerdown", "input_gain", "output_gain"):
            try:
                detail[key] = int(raw)
            except ValueError:
                detail[key] = raw
        else:
            detail[key] = raw

    # timeout tterm is not in "show voice port"; it only appears in the
    # running configuration, so it is read from there.
    detail.setdefault("status", "?")
    return detail


def port_config_block(config: str, port: str) -> str:
    """The voice-port block for one port, out of a running-config dump."""
    for block in re.split(r"(?=^\s*voice-port )", config, flags=re.M):
        match = re.match(r"\s*voice-port (\S+)", block)
        if match and match.group(1) == port:
            return block
    return ""


def all_ports(gw: gateway.Gateway) -> list[dict]:
    """Every port: live state, key settings, and the extension mapped to it."""
    config = gw.send("show running-config")
    states = gw.port_states()
    peers = dial_peers(config)
    by_port = {p["port"]: p for p in peers}

    ports = []
    for port in gateway.PORTS:
        block = port_config_block(config, port)
        state = states.get(port)
        tterm = re.search(r"timeout tterm (\d+)", block)
        ports.append({
            "port": port,
            "extension": by_port.get(port, {}).get("pattern",
                                                   str(gateway.extension_for(port))),
            "assigned": port in by_port,
            "status": state.status if state else "?",
            "usable": state.usable if state else False,
            "connection": ("polling" if "connection polling" in block
                           else "plar" if "connection plar" in block else "—"),
            "polarity_inverse": "polarity-inverse" in block
                                and "no polarity-inverse" not in block,
            "caller_id": "caller-id enable" in block,
            "shutdown": re.search(r"^\s*shutdown\s*$", block, re.M) is not None,
            "timeout_tterm": int(tterm.group(1)) if tterm else None,
        })
    return ports


# ── dial-peers: which extension rings which port ────────────────────────

def dial_peers(config: str) -> list[dict]:
    """The POTS dial-peer table, as a list."""
    peers = []
    for block in re.split(r"(?=^\s*dial-peer )", config, flags=re.M):
        match = re.match(r"\s*dial-peer voice (\d+) (\S+)", block)
        if not match:
            continue
        tag, kind = match.groups()
        pattern = re.search(r"destination-pattern (\S+)", block)
        port = re.search(r"^\s*port (\S+)", block, re.M)
        target = re.search(r"session target (\S+)", block)
        peers.append({
            "tag": int(tag),
            "kind": kind,
            "pattern": pattern.group(1) if pattern else "",
            "port": port.group(1) if port else "",
            "target": target.group(1) if target else "",
        })
    return peers


def set_dial_peer_port(gw: gateway.Gateway, tag: int, port: str) -> None:
    """Point one POTS dial-peer at a different FXS port.

    Refuses to create a second peer on the same port. Two peers sharing one
    port do not fail loudly: the gateway answers with whichever matched
    first, so one extension silently stops ringing and the fault looks like a
    dead handset.
    """
    if port not in gateway.PORTS:
        raise AdminError(f"неизвестный порт: {port}")

    config = gw.send("show running-config")
    peers = {p["tag"]: p for p in dial_peers(config)}
    if tag not in peers:
        raise AdminError(f"нет dial-peer с номером {tag}")
    if peers[tag]["kind"] != "pots":
        raise AdminError(f"dial-peer {tag} не является pots")

    for other_tag, peer in peers.items():
        if other_tag != tag and peer["port"] == port:
            raise AdminError(
                f"порт {port} уже занят абонентом {peer['pattern']} "
                f"(dial-peer {other_tag}). Сначала освободите его — иначе "
                f"один из двух абонентов перестанет звонить."
            )

    gw.send("configure terminal")
    try:
        gw.send(f"dial-peer voice {tag} pots")
        gw.send(f"port {port}")
        gw.send("exit")
    finally:
        gw.send("exit")


# ── changing port parameters ────────────────────────────────────────────

def set_parameter(gw: gateway.Gateway, port: str, key: str,
                  value: object) -> str:
    """Apply one whitelisted parameter to one port. Returns the command sent."""
    if port not in gateway.PORTS:
        raise AdminError(f"неизвестный порт: {port}")
    parameter = PARAMETERS.get(key)
    if parameter is None:
        raise AdminError(f"параметр {key!r} нельзя менять через интерфейс")

    if parameter.kind == "flag":
        # A flag is set by its bare command and cleared by "no <command>";
        # there is no on/off argument in this firmware.
        command = parameter.command if value else f"no {parameter.command}"
    elif parameter.kind == "int":
        try:
            number = int(value)                       # type: ignore[arg-type]
        except (TypeError, ValueError):
            raise AdminError(f"{parameter.label}: нужно целое число")
        if not parameter.minimum <= number <= parameter.maximum:
            raise AdminError(
                f"{parameter.label}: допустимо {parameter.minimum}"
                f"…{parameter.maximum}{' ' + parameter.unit if parameter.unit else ''}"
            )
        command = f"{parameter.command} {number}"
    elif parameter.kind == "choice":
        text = str(value)
        if text not in parameter.choices:
            raise AdminError(
                f"{parameter.label}: допустимо {', '.join(parameter.choices)}")
        command = f"{parameter.command} {text}"
    else:
        raise AdminError(f"неизвестный тип параметра: {parameter.kind}")

    gw.send("configure terminal")
    try:
        gw.send(f"voice-port {port}")
        gw.send(command)
        gw.send("exit")
    finally:
        gw.send("exit")
    return command


def set_admin_state(gw: gateway.Gateway, port: str, up: bool) -> None:
    """Administratively enable or disable a port.

    Distinct from the shutdown/no-shutdown cycle in gateway.clear_port(): that
    is a repair that ends up. This leaves the port wherever it is asked to,
    which is how a faulty port is taken out of service.
    """
    if port not in gateway.PORTS:
        raise AdminError(f"неизвестный порт: {port}")
    gw.send("configure terminal")
    try:
        gw.send(f"voice-port {port}")
        gw.send("no shutdown" if up else "shutdown")
        gw.send("exit")
    finally:
        gw.send("exit")


# ── diagnostics ─────────────────────────────────────────────────────────

# Read-only commands the interface may run. Every one was checked against
# this firmware; the list is closed so that no request can name its own.
DIAGNOSTICS = {
    "running-config": ("Конфигурация", "show running-config"),
    "voice-summary": ("Сводка портов", "show voice port summary"),
    "dial-peer": ("Таблица dial-peer", "show dial-peer"),
    "sip": ("Состояние SIP", "show sip"),
    "version": ("Версия и железо", "show version"),
    "interfaces": ("Интерфейсы", "show interfaces"),
    "logging": ("Журнал", "show logging"),
    "system": ("Задачи системы", "show system"),
    "clear-down-tone": ("Тоны отбоя", "show clear-down-tone"),
}


def diagnostic(gw: gateway.Gateway, name: str) -> str:
    entry = DIAGNOSTICS.get(name)
    if entry is None:
        raise AdminError(f"неизвестная диагностика: {name}")
    return gw.send(entry[1])


def probe(gw: gateway.Gateway, port: str) -> dict:
    """Check one port's availability, and say what is wrong if it is not.

    Availability is not the same as "the summary says Idle": a port that has
    been administratively shut is Idle and cannot take a call, and a port
    carrying a live call is busy without being faulty. Both are separated
    here, because the fix for each is different.
    """
    if port not in gateway.PORTS:
        raise AdminError(f"неизвестный порт: {port}")

    config = gw.send("show running-config")
    block = port_config_block(config, port)
    state = gw.port_state(port)
    shut = re.search(r"^\s*shutdown\s*$", block, re.M) is not None

    if shut:
        return {"port": port, "ok": False, "status": state.status,
                "reason": "порт административно выключен (shutdown)",
                "fix": "Включите порт кнопкой «Включить»."}
    if state.usable:
        return {"port": port, "ok": True, "status": state.status,
                "reason": "свободен и готов принять вызов", "fix": ""}
    if state.status == "Busy":
        return {"port": port, "ok": False, "status": state.status,
                "reason": "занят — идёт разговор или снята трубка",
                "fix": "Если разговора нет, проверьте трубку, затем «Освободить»."}
    return {"port": port, "ok": False, "status": state.status,
            "reason": f"состояние {state.status} — порт не примет вызов",
            "fix": "Нажмите «Освободить». Если состояние возвращается — "
                   "проверьте аппарат и кабель на этой линии."}


# ── the front panel ─────────────────────────────────────────────────────
#
# The AP1100F has a row of LEDs, and an operator standing at the device reads
# those rather than a web page. So the page mirrors them: same names, same
# order, same meaning — and, next to each, what the real LED should look like
# when things are right. That way the two can be compared without a manual.

def interfaces(gw: gateway.Gateway) -> dict[str, dict]:
    """Both Ethernet ports: link, speed, duplex, errors."""
    out = gw.send("show interfaces")
    found: dict[str, dict] = {}

    # Output is one paragraph per interface, each starting "Interface : etherN.0"
    for chunk in re.split(r"(?=Interface\s*:\s*ether)", out):
        name = re.search(r"Interface\s*:\s*(ether\d)", chunk)
        if not name:
            continue
        index = name.group(1)[-1]                     # "ether0" -> "0"
        up = bool(re.search(r"Ethernet\d is UP, Line protocol is UP", chunk))
        link = re.search(r"link status is (\d+) Mbps \((\S+)\)", chunk)
        address = re.search(r"IP Address\s*:\s*(\S+)", chunk)
        rate = re.search(r"tx (\d+) bps, rx (\d+) bps", chunk)
        errors_in = re.search(r"(\d+) input errors", chunk)
        errors_out = re.search(r"(\d+) output errors", chunk)

        found[f"lan{index}"] = {
            "name": f"LAN{index}",
            "up": up,
            "speed": int(link.group(1)) if link else 0,
            "duplex": link.group(2).lower() if link else "",
            "ip": (address.group(1) if address and address.group(1) != "no"
                   else ""),
            "tx_bps": int(rate.group(1)) if rate else 0,
            "rx_bps": int(rate.group(2)) if rate else 0,
            "errors": (int(errors_in.group(1)) if errors_in else 0)
                    + (int(errors_out.group(1)) if errors_out else 0),
        }
    return found


# What each LED on the physical device should be doing when the system is
# healthy. Shown beside the on-screen indicator so the two can be compared.
LED_GUIDE = {
    "power": {
        "label": "POWER",
        "expect": "Горит зелёным непрерывно",
        "wrong": "Не горит — нет питания. Проверьте блок питания и розетку.",
    },
    "run": {
        "label": "RUN",
        "expect": "Мигает зелёным примерно раз в секунду",
        "wrong": "Горит непрерывно или погас — прошивка не запустилась. "
                 "Перезагрузите шлюз по питанию.",
    },
    "lan0_100m": {
        "label": "LAN0 100M",
        "expect": "Горит — согласовано 100 Мбит/с",
        "wrong": "Не горит при активном линке — согласовано 10 Мбит/с. "
                 "Проверьте кабель и порт коммутатора.",
    },
    "lan0_link": {
        "label": "LAN0 LINK/ACT",
        "expect": "Горит при подключении, мигает при обмене данными",
        "wrong": "Не горит — нет линка. Это порт к АТС, без него связи нет.",
    },
    "lan1_link": {
        "label": "LAN1 LINK/ACT",
        "expect": "Погашен — второй порт в этой схеме не используется",
        "wrong": "Горит — в LAN1 что-то подключено; для этой схемы не требуется.",
    },
}

# What the FXS LED on the case actually does, per port state.
#
# The colour here is the colour of the real lamp, not a verdict on whether
# things are well. A free port's LED is dark, and drawing it green because
# "free is good" contradicts both the device and the caption beside it. Where
# the lamp is off, the on-screen one is off too; judgement is left to the
# text.
#
#   lamp:  off | green | orange   — what the LED shows
#   blink: steady | blink         — how it behaves
FXS_GUIDE = {
    "Idle":          ("off",    "steady", "Погашен",
                      "Порт свободен, трубка на месте"),
    "Busy":          ("green",  "steady", "Горит зелёным",
                      "Идёт разговор или снята трубка"),
    "Ringing":       ("green",  "blink",  "Мигает зелёным",
                      "Идёт вызов на аппарат"),
    "Waiting":       ("green",  "blink",  "Мигает зелёным",
                      "Ожидание набора номера"),
    "Disconnecting": ("orange", "steady", "Горит оранжевым",
                      "Порт занят и не освобождается — залипание"),
}


def panel(gw: gateway.Gateway) -> dict:
    """Everything the indicator panel shows, in one telnet session."""
    nets = interfaces(gw)
    states = gw.port_states()
    config = gw.send("show running-config")

    lan0 = nets.get("lan0", {})
    lan1 = nets.get("lan1", {})

    # POWER and RUN cannot be read over the network — if the CLI answered at
    # all, the device is powered and its firmware is running. That inference
    # is stated rather than hidden, so nobody reads a green dot as a
    # measurement it is not.
    # Each entry carries what the real lamp is doing — lamp/blink — and,
    # separately, whether that is the wanted state. The two are not the same:
    # LAN1 dark is correct here, and colouring it green because it is correct
    # would show a lamp the device does not light.
    lan0_up = bool(lan0.get("up"))
    lan0_fast = lan0.get("speed") == 100
    lan1_up = bool(lan1.get("up"))

    leds = [
        {"key": "power", "lamp": "green", "blink": "steady", "state": "ok",
         "value": "есть питание", "inferred": True, **LED_GUIDE["power"]},
        {"key": "run", "lamp": "green", "blink": "blink", "state": "ok",
         "value": "прошивка отвечает", "inferred": True, **LED_GUIDE["run"]},
        {"key": "lan0_100m",
         # This lamp is lit only at 100 Mbit/s; at 10 it is dark, and that is
         # what the panel must show rather than an amber warning colour.
         "lamp": "green" if lan0_fast else "off", "blink": "steady",
         "state": "ok" if lan0_fast else "warn",
         "value": f"{lan0.get('speed', 0)} Мбит/с {lan0.get('duplex', '')}".strip(),
         "inferred": False, **LED_GUIDE["lan0_100m"]},
        {"key": "lan0_link",
         "lamp": "green" if lan0_up else "off",
         "blink": "blink" if lan0_up else "steady",
         "state": "ok" if lan0_up else "bad",
         "value": (f"линк есть, {lan0.get('ip', '')}" if lan0_up
                   else "линка нет"),
         "inferred": False, **LED_GUIDE["lan0_link"]},
        {"key": "lan1_link",
         "lamp": "green" if lan1_up else "off",
         "blink": "blink" if lan1_up else "steady",
         "state": "ok" if not lan1_up else "warn",
         "value": "не подключён" if not lan1_up else "линк есть",
         "inferred": False, **LED_GUIDE["lan1_link"]},
    ]

    fxs = []
    for port in gateway.PORTS:
        state = states.get(port)
        status = state.status if state else "?"
        lamp, blink, led, meaning = FXS_GUIDE.get(
            status, ("orange", "steady", "Горит оранжевым", status))
        block = port_config_block(config, port)
        shut = re.search(r"^\s*shutdown\s*$", block, re.M) is not None
        # A shut port's LED is dark whatever the summary says about it.
        healthy = status == "Idle" and not shut
        fxs.append({
            "port": port,
            "extension": str(gateway.extension_for(port)),
            "status": status,
            "lamp": "off" if shut else lamp,
            "blink": "steady" if shut else blink,
            "state": "ok" if healthy else ("off" if shut else
                                           "bad" if status == "Disconnecting"
                                           else "warn"),
            "led": "Погашен (порт выключен)" if shut else led,
            "meaning": "Порт административно выключен" if shut else meaning,
            "shutdown": shut,
        })

    return {
        "leds": leds,
        "fxs": fxs,
        "host": gateway.HOST,
        "telnet": f"telnet {gateway.HOST}",
        "user": gateway.USER,
        "lan0": lan0,
        "lan1": lan1,
    }


def save_to_flash(gw: gateway.Gateway) -> str:
    """Persist the running configuration.

    Deliberately separate from every change above. Until this runs, the
    gateway can be power-cycled back to its last saved state, which is the
    only rollback this device has — there is no revert to a previous
    startup-config.
    """
    return gw.send("write")


if __name__ == "__main__":
    with gateway.Gateway() as gw:
        for entry in all_ports(gw):
            flags = []
            if entry["polarity_inverse"]:
                flags.append("polarity-inverse")
            if entry["shutdown"]:
                flags.append("SHUTDOWN")
            print(f"{entry['extension']:>4}  {entry['port']:5} "
                  f"{entry['status']:14} {entry['connection']:8} "
                  f"{' '.join(flags)}")
