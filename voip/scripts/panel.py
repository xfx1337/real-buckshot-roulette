#!/usr/bin/env python3
"""A small web panel for the PBX and the AddPac gateway.

Everything this exposes was a shell command a moment ago: which handset is on
which port, whether the PBX can see the gateway, ringing a phone, clearing a
port that has latched, watching digits arrive. The point is to see all of it at
once and click instead of type.

    python3 scripts/panel.py                 # http://127.0.0.1:8100
    python3 scripts/panel.py --port 9000

Only the standard library is used, so it runs with no install step. It talks to
Asterisk over AMI and to the gateway over telnet, reusing addpac.py.
"""
import argparse
import html
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VOIP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(VOIP_DIR, "scripts"))

AMI_HOST, AMI_PORT = "127.0.0.1", 5038
AMI_USER, AMI_SECRET = "digits", "backshot-ami"
GATEWAY_IP = "192.168.100.3"
IFACE_ADDR = "192.168.100.2"
ASTERISK = os.path.join(VOIP_DIR, "asterisk-local", "sbin", "asterisk")
ASTERISK_CONF = os.path.join(
    VOIP_DIR, "asterisk-local", "etc", "asterisk", "asterisk.conf"
)

# Extension 10N reaches slot/port, in the order the gateway numbers them.
PORTS = [
    ("101", "0/0"), ("102", "0/1"), ("103", "0/2"), ("104", "0/3"),
    ("105", "1/0"), ("106", "1/1"), ("107", "1/2"), ("108", "1/3"),
]
SLOT_TO_NUMBER = {slot: number for number, slot in PORTS}

# What the dialplan offers a handset, for the buttons that place a call.
DESTINATIONS = [
    ("lobby@to-handset", "Лобби-музыка"),
    ("601@from-gateway", "Эхо-тест"),
    ("500@from-gateway", "Голосовая проба"),
]


def asterisk_cli(command, timeout=15):
    """Run one Asterisk CLI command, returning its output or an error string."""
    if not os.path.exists(ASTERISK):
        return "", "Asterisk не собран"
    try:
        result = subprocess.run(
            [ASTERISK, "-C", ASTERISK_CONF, "-rx", command],
            capture_output=True, text=True, timeout=timeout,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        return "", str(exc)
    # Asterisk prints this to stderr on every -rx call under this prefix; it is
    # noise, not a failure.
    error = "\n".join(
        line for line in result.stderr.splitlines()
        if "maximum file descriptor" not in line
    ).strip()
    return result.stdout, error


def gateway_cli(*commands, timeout=90):
    """Run commands on the gateway through addpac.py."""
    script = os.path.join(VOIP_DIR, "scripts", "addpac.py")
    try:
        result = subprocess.run(
            [sys.executable, script, *commands],
            capture_output=True, text=True, timeout=timeout,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        return "", str(exc)
    return result.stdout, result.stderr.strip()


def host_address():
    """The address pjsip.conf binds to, and whether en6 currently has it."""
    try:
        out = subprocess.run(
            ["ifconfig"], capture_output=True, text=True, timeout=5
        ).stdout
    except (subprocess.TimeoutExpired, OSError):
        return False, "неизвестно"
    if f"inet {IFACE_ADDR} " in out:
        return True, IFACE_ADDR
    # en6 falls back to a self-assigned address when the dock drops out, which
    # is the single most common reason nothing works.
    match = re.search(r"en6:.*?inet (\S+)", out, re.S)
    return False, match.group(1) if match else "нет адреса"


def ping_gateway():
    try:
        result = subprocess.run(
            ["ping", "-c", "1", "-W", "1000", GATEWAY_IP],
            capture_output=True, timeout=5,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


def asterisk_running():
    try:
        out = subprocess.run(
            ["pgrep", "-f", "asterisk-local/sbin/asterisk"],
            capture_output=True, text=True, timeout=5,
        ).stdout
        return bool(out.strip())
    except (subprocess.TimeoutExpired, OSError):
        return False


def parse_ports(text):
    """Pull the per-port table out of 'show voice port summary'."""
    ports = {}
    for line in text.splitlines():
        # " 1/ 2     FXS       Idle            0     0     none ..."
        match = re.match(r"\s*(\d)/\s*(\d)\s+(\w+)\s+(\S+)\s", line)
        if not match:
            continue
        slot = f"{match.group(1)}/{match.group(2)}"
        status = match.group(4)
        tie = "plar" if " plar " in line else "none"
        ports[slot] = {"status": status, "tie": tie}
    return ports


def trunk_status():
    """Whether Asterisk can currently reach the gateway."""
    out, _ = asterisk_cli("pjsip show contacts")
    if "Avail" in out and "Unavail" not in out.split("Avail")[0][-40:]:
        match = re.search(r"Avail\s+([\d.]+)", out)
        return True, (match.group(1) if match else "")
    return False, ""


def active_channels():
    out, _ = asterisk_cli("core show channels verbose")
    channels = []
    for line in out.splitlines():
        if not line.startswith("PJSIP/") and not line.startswith("Local/"):
            continue
        parts = line.split()
        if len(parts) < 6:
            continue
        channels.append({
            "name": parts[0],
            "context": parts[1],
            "extension": parts[2],
            "state": parts[4],
            "application": parts[5] if len(parts) > 5 else "",
        })
    return channels


class DigitReader(threading.Thread):
    """Reads AMI in the background and keeps the last events seen.

    This is the same event stream phone_digits.py prints, kept in memory so the
    page can show it without a second process holding the connection.
    """

    def __init__(self):
        super().__init__(daemon=True)
        self.events = []
        self.connected = False
        self.lock = threading.Lock()
        self.stop_flag = threading.Event()

    def add(self, text):
        with self.lock:
            self.events.append({"at": time.strftime("%H:%M:%S"), "text": text})
            del self.events[:-40]

    def snapshot(self):
        with self.lock:
            return list(reversed(self.events))

    def run(self):
        while not self.stop_flag.is_set():
            try:
                self._session()
            except (OSError, RuntimeError):
                self.connected = False
                time.sleep(3)

    def _session(self):
        sock = socket.create_connection((AMI_HOST, AMI_PORT), timeout=10)
        sock.settimeout(1.0)
        sock.sendall(
            f"Action: Login\r\nUsername: {AMI_USER}\r\n"
            f"Secret: {AMI_SECRET}\r\nEvents: on\r\n\r\n".encode()
        )
        self.connected = True
        buffer = b""
        while not self.stop_flag.is_set():
            try:
                chunk = sock.recv(4096)
            except socket.timeout:
                continue
            if not chunk:
                break
            buffer += chunk
            while b"\r\n\r\n" in buffer:
                raw, buffer = buffer.split(b"\r\n\r\n", 1)
                self._handle(raw.decode("utf-8", "replace"))
        sock.close()
        self.connected = False

    def _handle(self, raw):
        event = {}
        for line in raw.split("\r\n"):
            if ": " in line:
                key, value = line.split(": ", 1)
                event[key.lower()] = value
        name = event.get("event", "")
        caller = event.get("calleridnum", "")
        who = f"{caller} ({dict(PORTS).get(caller, '?')})" if caller else "?"
        if name == "Newchannel":
            exten = event.get("exten", "")
            if exten and exten not in ("s", ""):
                self.add(f"{who} набрал {exten}")
        elif name == "DTMFEnd" and event.get("direction", "Received") == "Received":
            digit = event.get("digit", "")
            if digit:
                self.add(f"{who} нажал {digit}")


reader = DigitReader()


def collect_state():
    """Everything the page shows, gathered in one go."""
    address_ok, address = host_address()
    gateway_up = ping_gateway() if address_ok else False
    running = asterisk_running()

    ports = {}
    gateway_error = ""
    if gateway_up:
        out, err = gateway_cli("show voice port summary")
        ports = parse_ports(out)
        if not ports:
            gateway_error = err or "шлюз не ответил"

    trunk_ok, rtt = (trunk_status() if running else (False, ""))

    return {
        "address_ok": address_ok,
        "address": address,
        "gateway_up": gateway_up,
        "asterisk_running": running,
        "trunk_ok": trunk_ok,
        "trunk_rtt": rtt,
        "ports": [
            {
                "slot": slot,
                "number": number,
                "status": ports.get(slot, {}).get("status", "—"),
                "tie": ports.get(slot, {}).get("tie", "—"),
            }
            for number, slot in PORTS
        ],
        "channels": active_channels() if running else [],
        "events": reader.snapshot(),
        "ami_connected": reader.connected,
        "destinations": DESTINATIONS,
    }


PAGE = """<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>VoIP — пульт</title>
<style>
:root {
  --bg: #0a0a0a; --panel: #14100f; --elevated: #181310;
  --green: #3fef6a; --green-dim: #16351f;
  --red: #c45050; --red-dim: #3a1414;
  --amber: #c4a95a; --amber-dim: #2a2213;
  --text: #a99a8c; --bright: #e8ddce; --dim: #6e5f52;
  --border: #241614; --border-light: #322217;
}
* { margin: 0; padding: 0; box-sizing: border-box; }
body {
  background: var(--bg); color: var(--text);
  font-family: 'Share Tech Mono', 'JetBrains Mono', Consolas, monospace;
  font-size: 14px; line-height: 1.5; padding: 20px;
}
h1 { font-size: 18px; color: var(--bright); font-weight: 500;
     letter-spacing: 2px; margin-bottom: 4px; }
.sub { color: var(--dim); font-size: 12px; margin-bottom: 20px; }
.grid { display: grid; gap: 16px; grid-template-columns: repeat(auto-fit, minmax(330px, 1fr));
        max-width: 1400px; }
.card { background: var(--panel); border: 1px solid var(--border);
        border-radius: 3px; padding: 14px; }
.card h2 { font-size: 11px; text-transform: uppercase; letter-spacing: 2px;
           color: var(--dim); font-weight: 400; margin-bottom: 12px;
           border-bottom: 1px solid var(--border); padding-bottom: 8px; }
.row { display: flex; justify-content: space-between; align-items: center;
       padding: 7px 0; border-bottom: 1px solid var(--border); }
.row:last-child { border-bottom: none; }
.led { display: inline-block; width: 8px; height: 8px; border-radius: 50%;
       margin-right: 8px; vertical-align: middle; }
.ok { background: var(--green); box-shadow: 0 0 6px var(--green); }
.bad { background: var(--red); box-shadow: 0 0 6px var(--red); }
.warn { background: var(--amber); box-shadow: 0 0 6px var(--amber); }
.off { background: #2a2a2a; }
.val { color: var(--bright); }
.hint { color: var(--dim); font-size: 11px; margin-top: 8px; line-height: 1.6; }
code { background: var(--elevated); padding: 2px 6px; border-radius: 2px;
       color: var(--amber); font-size: 12px; }
table { width: 100%; border-collapse: collapse; }
th { text-align: left; font-weight: 400; color: var(--dim); font-size: 11px;
     text-transform: uppercase; letter-spacing: 1px; padding: 6px 4px;
     border-bottom: 1px solid var(--border); }
td { padding: 7px 4px; border-bottom: 1px solid var(--border); }
tr:last-child td { border-bottom: none; }
.tag { font-size: 11px; padding: 2px 7px; border-radius: 2px; display: inline-block; }
.t-idle { background: var(--green-dim); color: var(--green); }
.t-busy { background: var(--amber-dim); color: var(--amber); }
.t-stuck { background: var(--red-dim); color: var(--red); }
.t-none { background: #1c1c1c; color: var(--dim); }
button { background: var(--elevated); color: var(--text);
         border: 1px solid var(--border-light); border-radius: 2px;
         padding: 5px 10px; font-family: inherit; font-size: 12px;
         cursor: pointer; transition: all .12s; }
button:hover:not(:disabled) { border-color: var(--green); color: var(--green); }
button:disabled { opacity: .35; cursor: not-allowed; }
button.wide { width: 100%; padding: 8px; margin-bottom: 6px; }
button.danger:hover:not(:disabled) { border-color: var(--red); color: var(--red); }
select { background: var(--elevated); color: var(--text); font-family: inherit;
         border: 1px solid var(--border-light); padding: 5px; font-size: 12px;
         border-radius: 2px; width: 100%; margin-bottom: 8px; }
.log { background: #0d0b0a; border: 1px solid var(--border); border-radius: 2px;
       padding: 8px; height: 190px; overflow-y: auto; font-size: 12px; }
.log div { padding: 2px 0; border-bottom: 1px solid #1a1412; }
.log .ts { color: var(--dim); margin-right: 8px; }
.empty { color: var(--dim); font-style: italic; padding: 10px 0; font-size: 12px; }
#toast { position: fixed; bottom: 20px; right: 20px; background: var(--elevated);
         border: 1px solid var(--border-light); border-left: 2px solid var(--green);
         padding: 10px 16px; border-radius: 2px; color: var(--bright);
         font-size: 12px; opacity: 0; transition: opacity .2s; max-width: 420px; }
#toast.show { opacity: 1; }
#toast.err { border-left-color: var(--red); }
.actions { display: flex; gap: 5px; }
</style>
</head>
<body>
<h1>VOIP — ПУЛЬТ</h1>
<div class="sub">Asterisk + AddPac AP1100F · обновляется каждые 3 с</div>
<div class="grid">
  <div class="card">
    <h2>Связь</h2>
    <div id="health"></div>
    <div class="hint" id="health-hint"></div>
  </div>
  <div class="card">
    <h2>Позвонить на трубку</h2>
    <select id="call-port"></select>
    <select id="call-dest"></select>
    <button class="wide" onclick="call()">Позвонить</button>
    <button class="wide danger" onclick="act('hangup')">Сбросить все звонки</button>
    <div class="hint">Трубка звонит, при снятии играет выбранное.</div>
  </div>
  <div class="card">
    <h2>Шлюз</h2>
    <button class="wide" onclick="act('plar_off')">Снять PLAR со всех портов</button>
    <button class="wide" onclick="act('reload')">Перечитать диалплан</button>
    <button class="wide danger" onclick="act('write')"
            title="Перезапишет конфиг шлюза целиком">Сохранить конфиг во флеш</button>
    <div class="hint">PLAR заставляет трубку звонить на 700 сразу при снятии,
      из-за чего набор не читается. Он возвращается после перезагрузки шлюза,
      пока не сохранить конфиг.</div>
  </div>
  <div class="card" style="grid-column: 1 / -1;">
    <h2>Порты FXS</h2>
    <table>
      <thead><tr><th>Порт</th><th>Номер</th><th>Состояние</th><th>PLAR</th><th></th></tr></thead>
      <tbody id="ports"></tbody>
    </table>
    <div class="hint"><b>Idle</b> — свободен · <b>Busy</b> — трубка снята или идёт звонок ·
      <b>Disconnecting</b> — залип, нужен сброс. Порт с подключённой трубкой при снятии
      должен становиться Busy; если остаётся Idle — шлюз трубку не видит.</div>
  </div>
  <div class="card">
    <h2>Активные звонки</h2>
    <div id="channels"></div>
  </div>
  <div class="card">
    <h2>Набранные цифры</h2>
    <div class="log" id="events"></div>
  </div>
</div>
<div id="toast"></div>
<script>
let busy = false;

function led(state) {
  return '<span class="led ' + state + '"></span>';
}

function toast(text, isError) {
  const el = document.getElementById('toast');
  el.textContent = text;
  el.className = 'show' + (isError ? ' err' : '');
  setTimeout(() => { el.className = ''; }, 4000);
}

function render(s) {
  const health = [
    ['Адрес en6', s.address_ok, s.address,
     s.address_ok ? '' : 'sudo ipconfig set en6 MANUAL 192.168.100.2 255.255.255.0'],
    ['Шлюз 192.168.100.3', s.gateway_up, s.gateway_up ? 'отвечает' : 'недоступен', ''],
    ['Asterisk', s.asterisk_running, s.asterisk_running ? 'работает' : 'не запущен',
     s.asterisk_running ? '' : './scripts/run-asterisk.sh -d'],
    ['SIP-транк', s.trunk_ok, s.trunk_ok ? ('доступен ' + s.trunk_rtt + ' мс') : 'недоступен', ''],
    ['AMI', s.ami_connected, s.ami_connected ? 'подключён' : 'нет связи', ''],
  ];
  document.getElementById('health').innerHTML = health.map(([name, ok, value]) =>
    '<div class="row"><span>' + led(ok ? 'ok' : 'bad') + name +
    '</span><span class="val">' + value + '</span></div>').join('');
  const fix = health.filter(h => !h[1] && h[3]).map(h => h[3]);
  document.getElementById('health-hint').innerHTML = fix.length
    ? 'Выполни в терминале:<br><code>' + fix.join('</code><br><code>') + '</code>' : '';

  document.getElementById('ports').innerHTML = s.ports.map(p => {
    let cls = 't-none', label = p.status;
    if (p.status === 'Idle') cls = 't-idle';
    else if (p.status === 'Busy') cls = 't-busy';
    else if (p.status === 'Disconnecting') { cls = 't-stuck'; label = 'Залип'; }
    return '<tr><td>' + p.slot + '</td><td class="val">' + p.number + '</td>' +
      '<td><span class="tag ' + cls + '">' + label + '</span></td>' +
      '<td>' + (p.tie === 'plar' ? '<span class="tag t-stuck">PLAR</span>'
                                 : '<span class="tag t-none">нет</span>') + '</td>' +
      '<td class="actions">' +
      '<button onclick="reset(\\'' + p.slot + '\\')">Сброс</button>' +
      '<button onclick="ring(\\'' + p.number + '\\')">Звонок</button>' +
      '</td></tr>';
  }).join('');

  document.getElementById('channels').innerHTML = s.channels.length
    ? s.channels.map(c => '<div class="row"><span>' + c.name +
        '</span><span class="val">' + c.state + ' · ' + c.application +
        '</span></div>').join('')
    : '<div class="empty">нет активных звонков</div>';

  document.getElementById('events').innerHTML = s.events.length
    ? s.events.map(e => '<div><span class="ts">' + e.at + '</span>' + e.text + '</div>').join('')
    : '<div class="empty">пока ничего не набрано</div>';

  const portSelect = document.getElementById('call-port');
  if (!portSelect.options.length) {
    portSelect.innerHTML = s.ports.map(p =>
      '<option value="' + p.number + '">' + p.number + ' — порт ' + p.slot + '</option>').join('');
    document.getElementById('call-dest').innerHTML = s.destinations.map(d =>
      '<option value="' + d[0] + '">' + d[1] + '</option>').join('');
  }
}

async function refresh() {
  if (busy) return;
  try {
    const response = await fetch('/api/state');
    render(await response.json());
  } catch (e) { /* the panel keeps its last view if a poll fails */ }
}

async function post(action, params) {
  busy = true;
  try {
    const response = await fetch('/api/' + action, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(params || {}),
    });
    const result = await response.json();
    toast(result.message, !result.ok);
  } catch (e) {
    toast('Ошибка запроса: ' + e, true);
  } finally {
    busy = false;
    refresh();
  }
}

const act = a => post(a);
const reset = slot => post('reset_port', {slot});
const ring = number => post('call', {
  number, destination: document.getElementById('call-dest').value});
const call = () => post('call', {
  number: document.getElementById('call-port').value,
  destination: document.getElementById('call-dest').value});

refresh();
setInterval(refresh, 3000);
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # The panel is chatty enough on its own.

    def _send(self, code, body, content_type="application/json"):
        payload = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        if self.path == "/":
            self._send(200, PAGE, "text/html")
        elif self.path == "/api/state":
            self._send(200, json.dumps(collect_state()))
        else:
            self._send(404, json.dumps({"error": "not found"}))

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        try:
            params = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            params = {}
        action = self.path.rsplit("/", 1)[-1]
        handler = getattr(self, f"action_{action}", None)
        if handler is None:
            self._send(404, json.dumps({"ok": False, "message": "неизвестное действие"}))
            return
        try:
            ok, message = handler(params)
        except Exception as exc:  # a failed button must not kill the panel
            ok, message = False, f"ошибка: {exc}"
        self._send(200, json.dumps({"ok": ok, "message": message}))

    def action_call(self, params):
        number = str(params.get("number", ""))
        destination = str(params.get("destination", "lobby@to-handset"))
        if number not in SLOT_TO_NUMBER.values():
            return False, "неизвестный номер"
        _, error = asterisk_cli(
            f"originate PJSIP/{number}@addpac extension {destination}", timeout=8
        )
        if error:
            return False, error
        return True, f"Звоню на {number} — снимай трубку"

    def action_hangup(self, params):
        asterisk_cli("channel request hangup all")
        return True, "Все звонки сброшены"

    def action_reset_port(self, params):
        slot = str(params.get("slot", ""))
        if slot not in SLOT_TO_NUMBER:
            return False, "неизвестный порт"
        _, error = gateway_cli(
            "configure terminal", f"voice-port {slot}",
            "shutdown", "no shutdown", "exit", "exit",
        )
        if error:
            return False, error
        return True, f"Порт {slot} сброшен"

    def action_plar_off(self, params):
        commands = ["configure terminal"]
        for slot in SLOT_TO_NUMBER:
            commands += [f"voice-port {slot}", "no connection", "exit"]
        commands.append("exit")
        _, error = gateway_cli(*commands, timeout=180)
        if error:
            return False, error
        return True, "PLAR снят со всех портов"

    def action_reload(self, params):
        source = os.path.join(VOIP_DIR, "etc", "extensions.conf")
        target = os.path.join(
            VOIP_DIR, "asterisk-local", "etc", "asterisk", "extensions.conf"
        )
        if os.path.exists(source):
            with open(source, "rb") as src, open(target, "wb") as dst:
                dst.write(src.read())
        _, error = asterisk_cli("dialplan reload")
        if error:
            return False, error
        return True, "Диалплан перечитан"

    def action_write(self, params):
        _, error = gateway_cli("write", "y", timeout=120)
        if error:
            return False, error
        return True, "Конфиг шлюза сохранён во флеш"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8100)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()

    reader.start()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"пульт: http://{args.host}:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nостановлен")
    return 0


if __name__ == "__main__":
    sys.exit(main())
