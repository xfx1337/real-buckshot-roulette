#!/usr/bin/env bash
#
# Перепрошивка ESP32 одной командой.
#   1) генерирует esp/config.h из корневого config.json (единый источник настроек)
#   2) компилирует скетч
#   3) заливает на плату
#
# Запуск (из любого места):
#   ./esp/flash.sh                 # авто: порт определяется сам
#   ./esp/flash.sh -p /dev/cu.usbserial-10   # указать порт вручную
#   ./esp/flash.sh -m              # после прошивки открыть Serial-монитор
#
# Требуется: arduino-cli + установленное ядро esp32 (arduino-cli core install esp32:esp32).

set -euo pipefail

# ── Пути (скрипт лежит в esp/, корень проекта — на уровень выше) ──
ESP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(dirname "$ESP_DIR")"

# ── Параметры (можно переопределить флагами/переменными окружения) ──
FQBN="${FQBN:-esp32:esp32:esp32}"      # плата: generic «ESP32 Dev Module» (в ядре 3.x именно esp32, НЕ esp32dev)
UPLOAD_SPEED="${UPLOAD_SPEED:-115200}" # 115200 стабильнее на CP210x/macOS, чем 921600
BAUD="${BAUD:-115200}"                 # скорость Serial-монитора
PORT=""
OPEN_MONITOR=0

usage() {
  grep '^#' "$0" | sed 's/^# \{0,1\}//' | sed '/^!/d'
  exit "${1:-0}"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    -p|--port)    PORT="$2"; shift 2 ;;
    -m|--monitor) OPEN_MONITOR=1; shift ;;
    -h|--help)    usage 0 ;;
    *) echo "Неизвестный аргумент: $1" >&2; usage 1 ;;
  esac
done

# ── Проверки инструментов ──
command -v arduino-cli >/dev/null 2>&1 || {
  echo "❌ Не найден arduino-cli. Установи:  brew install arduino-cli" >&2; exit 1;
}

# ── Определяем порт, если не задан ──
if [[ -z "$PORT" ]]; then
  # macOS: /dev/cu.usbserial-*  |  Linux: /dev/ttyUSB* /dev/ttyACM*
  PORT="$(ls /dev/cu.usbserial-* 2>/dev/null | head -1 || true)"
  [[ -z "$PORT" ]] && PORT="$(ls /dev/ttyUSB* /dev/ttyACM* 2>/dev/null | head -1 || true)"
fi
if [[ -z "$PORT" ]]; then
  echo "❌ Плата не найдена. Подключи ESP32 по USB или укажи порт:  $0 -p /dev/cu.usbserial-XX" >&2
  echo "   Список портов:" >&2
  arduino-cli board list >&2 || true
  exit 1
fi

# ── Python для генератора config.h (venv проекта, иначе системный) ──
PY="python3"
[[ -x "$ROOT_DIR/venv/bin/python" ]] && PY="$ROOT_DIR/venv/bin/python"

echo "▸ Порт:        $PORT"
echo "▸ Плата:       $FQBN"
echo "▸ Скорость:    $UPLOAD_SPEED"
echo

# 1) config.json → esp/config.h
echo "① Генерирую config.h из config.json…"
"$PY" "$ESP_DIR/gen_config.py"

# 2) компиляция
echo "② Компилирую…"
arduino-cli compile --fqbn "$FQBN" "$ESP_DIR"

# 3) заливка
echo "③ Заливаю на плату…"
arduino-cli upload --fqbn "$FQBN" -p "$PORT" \
  --upload-property "upload.speed=$UPLOAD_SPEED" "$ESP_DIR"

echo
echo "✅ Готово: прошито на $PORT"

# 4) опционально — Serial-монитор
if [[ "$OPEN_MONITOR" -eq 1 ]]; then
  echo "④ Открываю Serial-монитор ($BAUD). Ctrl+C — выход."
  arduino-cli monitor -p "$PORT" -c "baudrate=$BAUD"
fi
