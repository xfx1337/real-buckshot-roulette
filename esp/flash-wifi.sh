#!/usr/bin/env bash
#
# ─────────────────────────────────────────────────────────────────────────────
#  Прошивка ESP32 игрового стола — Wi-Fi берётся из config.json.
# ─────────────────────────────────────────────────────────────────────────────
#
#   ./esp/flash-wifi.sh                      прошить с настройками из config.json
#   ./esp/flash-wifi.sh -p /dev/cu.usbserial-10
#   ./esp/flash-wifi.sh --monitor            после прошивки открыть Serial
#   ./esp/flash-wifi.sh --show               показать, что будет прошито, и выйти
#
# Это плата стола: радиокурок, соленоид и светодиод состояния патрона.
#
# Отличие от esp/flash.sh — не в том, что делает, а в том, что проверяет.
# Тот скрипт шьёт молча и падает уже на компиляции, когда в config.json стоит
# заглушка вместо сети или адрес сервера указывает в другую подсеть, чем эта
# машина. Плата после такой прошивки выглядит рабочей и молчит, а причина —
# в файле, который никто не открывал. Здесь все эти условия проверяются до
# заливки и называются словами.
#
# Что откуда:
#   config.json → esp.wifi_ssid, esp.wifi_password   сеть
#   config.json → esp.server_base_url                адрес сервера для платы
#   config.json → esp.pins, timings, trigger_remote  пины и тайминги
#
# Скетч (esp/esp.ino) не меняется: config.h генерируется esp/gen_config.py,
# как и раньше.
#
# Требуется: arduino-cli, ядро esp32, плата по USB.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"   # esp/
ROOT="$(dirname "$HERE")"                              # корень проекта
CONFIG="$ROOT/config.json"

FQBN="${FQBN:-esp32:esp32:esp32}"
UPLOAD_SPEED="${UPLOAD_SPEED:-115200}"   # на CP210x/macOS стабильнее 921600
BAUD="${BAUD:-115200}"
PORT=""
MONITOR=0
SHOW_ONLY=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        -p|--port)    PORT="${2:-}"; shift 2 ;;
        --fqbn)       FQBN="${2:-}"; shift 2 ;;
        -m|--monitor) MONITOR=1; shift ;;
        --show)       SHOW_ONLY=1; shift ;;
        -h|--help)    grep '^#' "$0" | sed 's/^# \{0,1\}//' | sed '/^!/d'; exit 0 ;;
        *) echo "Неизвестный аргумент: $1" >&2; exit 2 ;;
    esac
done

die() { echo "❌ $*" >&2; exit 1; }

[[ -f "$CONFIG" ]] || die "не найден $CONFIG — скопируй config.example.json в config.json"

PY="$ROOT/venv/bin/python"
[[ -x "$PY" ]] || PY="$(command -v python3 || true)"
[[ -n "$PY" ]] || die "не найден python3"

# ── Чтение config.json ───────────────────────────────────────────────────
#
# Тем же стриппером комментариев, которым читает сервер (app/config.py):
# в config.json допускаются //-подписи, и наивный json.load на них падает.
read_config() {
    "$PY" - "$CONFIG" "$ROOT" <<'PY'
import json, sys
from pathlib import Path
from urllib.parse import urlparse

path, root = sys.argv[1], sys.argv[2]
sys.path.insert(0, root)
try:
    from app.config import strip_jsonc
except Exception:
    def strip_jsonc(text):
        return text

cfg = json.loads(strip_jsonc(Path(path).read_text(encoding="utf-8")))
esp = cfg.get("esp", {})
url = urlparse(str(esp.get("server_base_url", "")))

def out(key, value):
    # Через кавычки: SSID и пароль сплошь и рядом содержат пробелы, а пароль
    # ещё и символы, которые shell разберёт как свои, если их не защитить.
    print(f"{key}={json.dumps(str(value), ensure_ascii=False)}")

out("SSID", esp.get("wifi_ssid", ""))
out("PASSWORD", esp.get("wifi_password", ""))
out("BASE_URL", esp.get("server_base_url", ""))
out("URL_HOST", url.hostname or "")
out("URL_PORT", url.port or "")
out("CFG_PORT", cfg.get("server", {}).get("port", 8000))
PY
}

eval "$(read_config)" || die "не удалось прочитать $CONFIG"

# ── Проверки, из-за которых плата обычно молчит после прошивки ───────────

[[ -n "$SSID" ]] || die "в config.json пуст esp.wifi_ssid"
[[ "$SSID" != "YOUR_WIFI_SSID" ]] || \
    die "в config.json стоит заглушка esp.wifi_ssid — впиши настоящую сеть"
[[ "$PASSWORD" != "YOUR_WIFI_PASSWORD" ]] || \
    die "в config.json стоит заглушка esp.wifi_password — впиши настоящий пароль"
[[ -n "$URL_HOST" ]] || \
    die "в config.json некорректен esp.server_base_url: «${BASE_URL}» (нужно http://АДРЕС:ПОРТ)"

# Адрес этой машины на её сети. Именно по нему плата и будет стучаться, а
# loopback с точки зрения ESP — она сама.
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
LAN_IP="$(guess_host)"

echo "▸ Сеть:        $SSID"
echo "▸ Сервер:      $BASE_URL"
echo "▸ Плата:       $FQBN"
echo

# Предупреждения, а не отказ: сервер может стоять на другой машине, и тогда
# несовпадение — норма. Но чаще это забытый после переезда адрес, поэтому о
# нём говорится вслух.
if [[ "$URL_HOST" == "127.0.0.1" || "$URL_HOST" == "localhost" || "$URL_HOST" == "0.0.0.0" ]]; then
    die "esp.server_base_url указывает на «${URL_HOST}» — это адрес самого сервера.
   С точки зрения платы это она сама, и до сервера она не достучится.
   Впиши адрес этой машины в сети${LAN_IP:+, например http://$LAN_IP:$CFG_PORT}."
fi
if [[ -n "$LAN_IP" && "$URL_HOST" != "$LAN_IP" ]]; then
    echo "⚠️  В config.json адрес сервера $URL_HOST, а эта машина — $LAN_IP." >&2
    echo "   Если сервер здесь, плата его не найдёт. Проверь esp.server_base_url." >&2
    echo >&2
fi
if [[ -n "$URL_PORT" && "$URL_PORT" != "$CFG_PORT" ]]; then
    echo "⚠️  В адресе сервера порт $URL_PORT, а сервер слушает $CFG_PORT." >&2
    echo >&2
fi

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

# ── Сборка и заливка ─────────────────────────────────────────────────────
#
# config.h генерируется из config.json тем же скриптом, что и всегда: он —
# единственное место, которое знает, как раскладывать пины и тайминги.
echo "① Генерирую config.h из config.json…"
"$PY" "$HERE/gen_config.py"

echo "② Компилирую…"
arduino-cli compile --fqbn "$FQBN" "$HERE" || die "компиляция не удалась"

echo "③ Заливаю на плату…"
arduino-cli upload --fqbn "$FQBN" -p "$PORT" \
    --upload-property "upload.speed=$UPLOAD_SPEED" "$HERE" \
    || die "заливка не удалась — проверь кабель и порт"

echo
echo "✅ Готово. Плата подключится к «${SSID}» и будет опрашивать $BASE_URL"
echo "   Проверить: в панели дилера светодиод состояния платы перестанет гореть красным."

if [[ "$MONITOR" -eq 1 ]]; then
    echo
    echo "④ Serial-монитор ($BAUD). Ctrl+C — выход."
    arduino-cli monitor -p "$PORT" -c "baudrate=$BAUD"
fi
