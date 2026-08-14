#!/usr/bin/env bash
#
# ─────────────────────────────────────────────────────────────────────────────
#  Прошивка ESP32 телефонного аппарата — Wi-Fi берётся из config.json.
# ─────────────────────────────────────────────────────────────────────────────
#
#   ./voip/flash-wifi.sh                     прошить с настройками из config.json
#   ./voip/flash-wifi.sh --exten 103         прошить плату для аппарата 103
#   ./voip/flash-wifi.sh -p /dev/cu.usbserial-10
#   ./voip/flash-wifi.sh --monitor           после прошивки открыть Serial
#   ./voip/flash-wifi.sh --show              показать, что будет прошито, и выйти
#
# Это плата, которая читает рычаг и дисковый номеронабиратель телефона: она
# сообщает серверу, что трубку сняли, какие цифры набрали и что положили.
# Именно её off-hook запускает звук в трубке — шлюз этого сообщить не может.
#
# Зачем отдельный скрипт рядом с voip/esp/flash.sh. Тот спрашивает настройки
# в диалоге и хранит ответы в src/config.local.h — это правильно, когда плату
# настраивают впервые или переносят на другой аппарат. Но сеть в зале меняется
# целиком: меняется роутер, и перешить надо обе платы, а SSID с паролем уже
# лежат в config.json, из которого их берёт и сервер, и плата игры. Этот
# скрипт закрывает именно тот случай — прошить, ничего не спрашивая и не
# рискуя разойтись с остальным проектом.
#
# Что откуда:
#   config.json → esp.wifi_ssid, esp.wifi_password   сеть
#   config.json → voip_esp.extension                 номер аппарата (101–108)
#   config.json → voip_esp.dialer_token              общий токен, можно пустой
#   адрес сервера                                    определяется сам (LAN-IP)
#   порт сервера                                     server.port из config.json
#
# Прошивка (voip/esp/src/) не меняется: скрипт только собирает config.local.h,
# который она и так подключает.
#
# Требуется: arduino-cli, ядро esp32, плата по USB.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"   # voip/
ROOT="$(dirname "$HERE")"                              # корень проекта
ESP="$HERE/esp"
SKETCH="$ESP/build/ta1132"
LOCAL="$ESP/src/config.local.h"
CONFIG="$ROOT/config.json"

FQBN="${FQBN:-esp32:esp32:esp32}"
BAUD="${BAUD:-115200}"
PORT=""
MONITOR=0
SHOW_ONLY=0
EXTEN_OVERRIDE=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        -p|--port)    PORT="${2:-}"; shift 2 ;;
        --exten)      EXTEN_OVERRIDE="${2:-}"; shift 2 ;;
        --fqbn)       FQBN="${2:-}"; shift 2 ;;
        -m|--monitor) MONITOR=1; shift ;;
        --show)       SHOW_ONLY=1; shift ;;
        -h|--help)    grep '^#' "$0" | sed 's/^# \{0,1\}//' | sed '/^!/d'; exit 0 ;;
        *) echo "Неизвестный аргумент: $1" >&2; exit 2 ;;
    esac
done

die() { echo "❌ $*" >&2; exit 1; }

[[ -f "$CONFIG" ]] || die "не найден $CONFIG — скопируй config.example.json в config.json"
[[ -d "$ESP/src" ]] || die "не найдена прошивка в $ESP/src"

# ── Чтение config.json ───────────────────────────────────────────────────
#
# Через тот же стриппер комментариев, которым читает сервер (app/config.py):
# в config.json допускаются //-подписи, и наивный json.load на них падает.
PY="$ROOT/venv/bin/python"
[[ -x "$PY" ]] || PY="$(command -v python3 || true)"
[[ -n "$PY" ]] || die "не найден python3"

read_config() {
    "$PY" - "$CONFIG" "$ROOT" <<'PY'
import json, sys
from pathlib import Path

path, root = sys.argv[1], sys.argv[2]
sys.path.insert(0, root)
try:
    from app.config import strip_jsonc
except Exception:
    def strip_jsonc(text):
        return text

cfg = json.loads(strip_jsonc(Path(path).read_text(encoding="utf-8")))
esp = cfg.get("esp", {})
voip = cfg.get("voip_esp", {})

def out(key, value):
    # Через кавычки: SSID и пароль сплошь и рядом содержат пробелы, а пароль
    # ещё и символы, которые shell разберёт как свои, если их не защитить.
    print(f"{key}={json.dumps(str(value), ensure_ascii=False)}")

out("SSID", esp.get("wifi_ssid", ""))
out("PASSWORD", esp.get("wifi_password", ""))
out("PORT_NUM", cfg.get("server", {}).get("port", 8000))
out("EXTENSION", voip.get("extension", "105"))
out("TOKEN", voip.get("dialer_token", ""))
PY
}

eval "$(read_config)" || die "не удалось прочитать $CONFIG"

[[ -n "$SSID" ]] || die "в config.json пуст esp.wifi_ssid"
[[ "$SSID" != "YOUR_WIFI_SSID" ]] || \
    die "в config.json стоит заглушка esp.wifi_ssid — впиши настоящую сеть"

[[ -n "$EXTEN_OVERRIDE" ]] && EXTENSION="$EXTEN_OVERRIDE"
case "$EXTENSION" in
    10[1-8]) ;;
    *) die "номер аппарата должен быть 101–108, а не «${EXTENSION}»" ;;
esac

# ── Адрес сервера, каким его видит ESP ───────────────────────────────────
#
# Не из server_base_url в config.json: там адрес для платы игры, и он может
# указывать на другую сеть. Берётся адрес этой машины на её сети — тот
# единственный, по которому до сервера дотянется другое устройство. Loopback
# не годится: с точки зрения ESP это он сам.
guess_host() {
    local interface address
    interface="$(route -n get default 2>/dev/null | awk '/interface:/ {print $2}')"
    [[ -n "$interface" ]] && address="$(ipconfig getifaddr "$interface" 2>/dev/null || true)"
    if [[ -z "${address:-}" ]]; then
        address="$(ifconfig 2>/dev/null \
            | awk '/inet / && $2 !~ /^127\./ && $2 !~ /^169\.254\./ {print $2; exit}')"
    fi
    printf '%s' "${address:-}"
}

HOST="${HOST:-$(guess_host)}"
[[ -n "$HOST" ]] || die "не удалось определить адрес этой машины — задай его: HOST=192.168.0.5 $0"

echo "▸ Сеть:        $SSID"
echo "▸ Сервер:      http://$HOST:$PORT_NUM/api/dialer"
echo "▸ Аппарат:     $EXTENSION"
echo "▸ Токен:       ${TOKEN:-(нет)}"
echo

if [[ "$SHOW_ONLY" -eq 1 ]]; then
    echo "Показ без прошивки (--show). Ничего не залито."
    exit 0
fi

# ── Инструменты ──────────────────────────────────────────────────────────
command -v arduino-cli >/dev/null 2>&1 \
    || die "не найден arduino-cli — brew install arduino-cli"

if ! arduino-cli core list 2>/dev/null | grep -q '^esp32:esp32'; then
    echo "▸ Ставлю ядро esp32 (один раз)…"
    arduino-cli config add board_manager.additional_urls \
        https://raw.githubusercontent.com/espressif/arduino-esp32/gh-pages/package_esp32_index.json \
        2>/dev/null || true
    arduino-cli core update-index
    arduino-cli core install esp32:esp32
fi

# ── Порт платы ───────────────────────────────────────────────────────────
if [[ -z "$PORT" ]]; then
    PORT="$(ls /dev/cu.usbserial-* /dev/cu.wchusbserial-* /dev/cu.SLAB_USBtoUART* 2>/dev/null | head -1 || true)"
    [[ -z "$PORT" ]] && PORT="$(ls /dev/ttyUSB* /dev/ttyACM* 2>/dev/null | head -1 || true)"
fi
[[ -n "$PORT" ]] || {
    echo "❌ Плата не найдена. Подключи ESP32 по USB или укажи порт: $0 -p /dev/cu.usbserial-XX" >&2
    arduino-cli board list >&2 || true
    exit 1
}
echo "▸ Порт платы:  $PORT"
echo

# ── config.local.h ───────────────────────────────────────────────────────
#
# Тот же файл, который пишет интерактивный voip/esp/flash.sh, и в том же
# формате: прошивка подключает его из config.h и не знает, кто его создал.
# Содержит пароль Wi-Fi, поэтому в git не попадает (voip/esp/.gitignore).
cat > "$LOCAL" <<EOF
// Сгенерировано voip/flash-wifi.sh из config.json. Содержит пароль Wi-Fi —
// не коммить. Чтобы поменять: правь config.json и запусти скрипт снова.
#pragma once

#define WIFI_SSID     "$SSID"
#define WIFI_PASSWORD "$PASSWORD"

#define SERVER_HOST "$HOST"
#define SERVER_PORT $PORT_NUM

#define EXTENSION "$EXTENSION"
#define DIALER_TOKEN "$TOKEN"
EOF
echo "① Настройки записаны в voip/esp/src/config.local.h"

# ── Сборка ───────────────────────────────────────────────────────────────
#
# arduino-cli требует, чтобы скетч лежал в папке с тем же именем, а исходник
# назывался .ino — отсюда сборочная директория вместо компиляции на месте.
rm -rf "$SKETCH"
mkdir -p "$SKETCH"
cp "$ESP/src/main.cpp" "$SKETCH/ta1132.ino"
cp "$ESP/src/config.h" "$SKETCH/config.h"
cp "$LOCAL" "$SKETCH/config.local.h"

echo "② Компилирую…"
arduino-cli compile --fqbn "$FQBN" "$SKETCH" || die "компиляция не удалась"

echo "③ Заливаю на плату…"
arduino-cli upload --fqbn "$FQBN" --port "$PORT" "$SKETCH" \
    || die "заливка не удалась — проверь кабель и порт"

echo
echo "✅ Готово. Аппарат $EXTENSION будет стучаться на http://$HOST:$PORT_NUM/api/dialer"
echo "   Проверить: сними трубку — в панели дилера, вкладка «Телефоны», появится событие."

if [[ "$MONITOR" -eq 1 ]]; then
    echo
    echo "④ Serial-монитор ($BAUD). Ctrl+C — выход."
    arduino-cli monitor --port "$PORT" --config "baudrate=$BAUD"
fi
