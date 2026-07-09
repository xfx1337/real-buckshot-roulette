#!/usr/bin/env bash
#
# Автосбор сетевых параметров в config.json.
#
# Запусти скрипт — он сам определит:
#   • SSID текущей Wi-Fi сети         → esp.wifi_ssid
#   • пароль этой сети (из keychain)  → esp.wifi_password
#   • LAN-IP этого хоста + порт из config.json → esp.server_base_url
# и впишет их в корневой config.json (комментарии JSONC сохраняются —
# правятся только три строки-значения).
#
# Запуск (из любого места):
#   ./scripts/net_config.sh              # записать в config.json
#   ./scripts/net_config.sh --dry-run    # только показать, что нашлось, без записи
#   ./scripts/net_config.sh --flash      # после записи сразу перепрошить (esp/flash.sh)
#   ./scripts/net_config.sh --sudo       # macOS 26+: достать скрытый SSID через sudo wdutil
#
# Поддержка: macOS и Linux (NetworkManager). Пароль берётся из системного
# хранилища.
#
# macOS 26+ (Tahoe): ОС прячет SSID от терминала (показывает "<redacted>"),
# пока терминалу/IDE не выдан доступ в System Settings → Privacy & Security →
# Location Services. Выдай его — и SSID/пароль подтянутся сами. Иначе используй
# флаг --sudo (спросит пароль) либо впиши SSID/пароль в интерактивном промпте.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"   # scripts/ лежит на уровень ниже корня
CONFIG="$ROOT_DIR/config.json"

DRY_RUN=0
DO_FLASH=0
USE_SUDO="${NET_CONFIG_SUDO:-0}"
while [[ $# -gt 0 ]]; do
  case "$1" in
    -n|--dry-run) DRY_RUN=1; shift ;;
    -f|--flash)   DO_FLASH=1; shift ;;
    -s|--sudo)    USE_SUDO=1; shift ;;
    -h|--help)    grep '^#' "$0" | sed 's/^# \{0,1\}//' | sed '/^!/d'; exit 0 ;;
    *) echo "Неизвестный аргумент: $1" >&2; exit 1 ;;
  esac
done

[[ -f "$CONFIG" ]] || {
  echo "❌ Не найден $CONFIG. Скопируй config.example.json в config.json." >&2
  exit 1
}

OS="$(uname -s)"
SSID=""
PASSWORD=""
IP=""

# «Замаскированное» значение SSID считаем как «не определено».
# macOS 26+ отдаёт "<redacted>", если у терминала нет доступа к Локации.
is_redacted() { [[ -z "$1" || "$1" == *"<redacted>"* || "$1" == "redacted" ]]; }

# ── SSID + пароль + IP по платформам ──────────────────────────────
if [[ "$OS" == "Darwin" ]]; then
  IFACE="$(route get default 2>/dev/null | awk '/interface:/{print $2}')"
  [[ -z "$IFACE" ]] && IFACE="en0"

  # Метод 1: ipconfig getsummary (работает при доступе к Локации, без sudo).
  SSID="$(ipconfig getsummary "$IFACE" 2>/dev/null \
            | awk -F ' SSID : ' '/ SSID :/{print $2; exit}')"

  # Метод 2: system_profiler (иногда показывает SSID, когда getsummary нет).
  if is_redacted "$SSID"; then
    SSID="$(system_profiler SPAirPortDataType 2>/dev/null \
              | awk '/Current Network Information:/{getline; gsub(/^ *| *:$/,""); print; exit}')"
  fi

  # Метод 3: sudo wdutil — обходит гейт Локации, но спросит пароль sudo.
  # Включается флагом --sudo (или переменной NET_CONFIG_SUDO=1), чтобы обычный
  # запуск не висел на вводе пароля.
  if is_redacted "$SSID" && [[ "$USE_SUDO" -eq 1 ]]; then
    echo "▸ SSID скрыт ОС — пробую 'sudo wdutil info' (нужен пароль)…" >&2
    SSID="$(sudo wdutil info 2>/dev/null | awk -F ': ' '/[[:space:]]SSID/{print $2; exit}')"
  fi

  # Метод 4 (фолбэк): спросить у пользователя, если терминал интерактивный.
  if is_redacted "$SSID"; then
    SSID=""
    if [[ -t 0 ]]; then
      echo "⚠️  macOS скрыл SSID (нет доступа к Локации). Впиши имя Wi-Fi вручную." >&2
      read -r -p "   SSID (Enter — пропустить): " SSID || true
    fi
  fi

  # Пароль из связки ключей (может запросить подтверждение доступа к keychain).
  if [[ -n "$SSID" ]]; then
    PASSWORD="$(security find-generic-password -D 'AirPort network password' -a "$SSID" -w 2>/dev/null || true)"
    # Если пароль не отдался, а терминал интерактивный — спросим (скрытый ввод).
    if [[ -z "$PASSWORD" && -t 0 ]]; then
      echo "⚠️  Пароль для \"$SSID\" не найден в связке ключей." >&2
      read -r -s -p "   Пароль Wi-Fi (Enter — пропустить): " PASSWORD || true
      echo >&2
    fi
  fi

  # LAN-IP активного интерфейса.
  IP="$(ipconfig getifaddr "$IFACE" 2>/dev/null || true)"
  [[ -z "$IP" ]] && IP="$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || true)"

elif [[ "$OS" == "Linux" ]]; then
  # SSID активного подключения
  if command -v nmcli >/dev/null 2>&1; then
    SSID="$(nmcli -t -f active,ssid dev wifi 2>/dev/null | awk -F: '/^yes:/{print $2; exit}')"
    if [[ -n "$SSID" ]]; then
      # Пароль сохранённого профиля (нужны права на чтение секретов)
      PASSWORD="$(nmcli -s -g 802-11-wireless-security.psk connection show "$SSID" 2>/dev/null || true)"
    fi
  elif command -v iwgetid >/dev/null 2>&1; then
    SSID="$(iwgetid -r 2>/dev/null || true)"
  fi
  # LAN-IP: адрес интерфейса с дефолтным маршрутом
  IFACE="$(ip route show default 2>/dev/null | awk '/default/{print $5; exit}')"
  [[ -n "$IFACE" ]] && IP="$(ip -4 -o addr show "$IFACE" 2>/dev/null | awk '{print $4}' | cut -d/ -f1 | head -1)"
  [[ -z "$IP" ]] && IP="$(hostname -I 2>/dev/null | awk '{print $1}')"

else
  echo "❌ Неподдерживаемая ОС: $OS (используй net_config.ps1 на Windows)." >&2
  exit 1
fi

# ── Порт сервера из config.json (для server_base_url) ──────────────
PORT="$(grep -oE '"port"[[:space:]]*:[[:space:]]*[0-9]+' "$CONFIG" | grep -oE '[0-9]+' | head -1)"
[[ -z "$PORT" ]] && PORT="8000"

SERVER_URL=""
[[ -n "$IP" ]] && SERVER_URL="http://$IP:$PORT"

# ── Отчёт ─────────────────────────────────────────────────────────
echo "▸ ОС:            $OS"
echo "▸ SSID:          ${SSID:-<не определён>}"
echo "▸ Пароль:        $([[ -n "$PASSWORD" ]] && echo '<найден>' || echo '<не найден — впиши вручную>')"
echo "▸ IP хоста:      ${IP:-<не определён>}"
echo "▸ server_base_url: ${SERVER_URL:-<не построен>}"
echo

if [[ "$DRY_RUN" -eq 1 ]]; then
  echo "ℹ️  --dry-run: config.json не изменён."
  exit 0
fi

# ── Замена значения одного ключа в config.json (сохраняя комментарии) ──
# Двухуровневое экранирование:
#   1) JSON: \ → \\ и " → \" (чтобы значение осталось валидной JSON-строкой);
#   2) sed:  & \ и разделитель | (спецсимволы в replacement-части s|||).
json_escape() { printf '%s' "$1" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g'; }
sed_escape()  { printf '%s' "$1" | sed -e 's/[\\&|]/\\&/g'; }

set_key() {
  local key="$1" val="$2"
  [[ -z "$val" ]] && return 0
  local esc; esc="$(sed_escape "$(json_escape "$val")")"
  # Меняем только строковое значение до закрывающей кавычки; хвост строки
  # (запятая + //-комментарий) сохраняется группой \2.
  if grep -qE "\"$key\"[[:space:]]*:[[:space:]]*\"" "$CONFIG"; then
    sed -i.bak -E "s|(\"$key\"[[:space:]]*:[[:space:]]*\")[^\"]*(\".*)|\1$esc\2|" "$CONFIG"
  else
    echo "⚠️  Ключ \"$key\" не найден в config.json — пропущен." >&2
  fi
}

set_key "wifi_ssid" "$SSID"
set_key "wifi_password" "$PASSWORD"
set_key "server_base_url" "$SERVER_URL"
rm -f "$CONFIG.bak"

echo "✅ config.json обновлён."
[[ -z "$SSID" ]]     && echo "⚠️  SSID не определён — проверь Wi-Fi и впиши вручную." >&2
[[ -z "$PASSWORD" ]] && echo "⚠️  Пароль не найден — впиши esp.wifi_password вручную." >&2
[[ -z "$IP" ]]       && echo "⚠️  IP не определён — впиши esp.server_base_url вручную." >&2

if [[ "$DO_FLASH" -eq 1 ]]; then
  echo
  echo "▸ Перепрошиваю плату…"
  "$ROOT_DIR/esp/flash.sh"
fi
