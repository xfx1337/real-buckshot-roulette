#!/usr/bin/env bash
#
# ─────────────────────────────────────────────────────────────────────────────
#  Buckshot Roulette IRL — запуск всего проекта ОДНОЙ командой.
# ─────────────────────────────────────────────────────────────────────────────
#
#   ./start.sh
#
# Что делает по шагам:
#   ① Автоопределяет Wi-Fi (SSID/пароль) и LAN-IP этого хоста и вписывает их в
#     config.json  (переиспользует scripts/net_config.sh).
#   ② Прошивает ESP32 актуальными настройками через локальный arduino-cli
#     (esp/flash.sh). USB-прошивка делается на ХОСТЕ — Docker Desktop на
#     macOS/Windows не пробрасывает USB в контейнер.
#   ③ Собирает и поднимает сервер в Docker (docker compose up).
#
# Флаги:
#   ./start.sh --no-flash     не трогать плату (только сеть + сервер)
#   ./start.sh --no-net       не переопределять сеть в config.json (взять как есть)
#   ./start.sh --sudo         разрешить sudo для добычи скрытого SSID (macOS 26+)
#   ./start.sh --detach       поднять сервер в фоне (docker compose up -d)
#   ./start.sh --down         остановить сервер (docker compose down) и выйти
#
# Требования: docker + docker compose; для прошивки — arduino-cli и плата по USB.

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

DO_FLASH=1
DO_NET=1
USE_SUDO=0
DETACH=0
DO_DOWN=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --no-flash) DO_FLASH=0; shift ;;
    --no-net)   DO_NET=0; shift ;;
    --sudo)     USE_SUDO=1; shift ;;
    --detach|-d) DETACH=1; shift ;;
    --down)     DO_DOWN=1; shift ;;
    -h|--help)  grep '^#' "$0" | sed 's/^# \{0,1\}//' | sed '/^!/d'; exit 0 ;;
    *) echo "Неизвестный аргумент: $1" >&2; exit 1 ;;
  esac
done

# ── docker compose: v2 (плагин) или legacy docker-compose ──
if docker compose version >/dev/null 2>&1; then
  DC=(docker compose)
elif command -v docker-compose >/dev/null 2>&1; then
  DC=(docker-compose)
else
  echo "❌ Не найден docker compose. Установи Docker Desktop / docker-compose." >&2
  exit 1
fi

# ── --down: остановить и выйти ──
if [[ "$DO_DOWN" -eq 1 ]]; then
  echo "▸ Останавливаю сервер…"
  "${DC[@]}" down
  echo "✅ Остановлено."
  exit 0
fi

# ── config.json обязателен (из него читаем порт и сетевые значения) ──
if [[ ! -f "$ROOT_DIR/config.json" ]]; then
  echo "▸ config.json не найден — создаю из config.example.json."
  cp "$ROOT_DIR/config.example.json" "$ROOT_DIR/config.json"
fi

# ①  Автонастройка сети → config.json
if [[ "$DO_NET" -eq 1 ]]; then
  echo "───────────────────────────────────────────────"
  echo "① Определяю Wi-Fi и LAN-IP этого хоста…"
  echo "───────────────────────────────────────────────"
  NET_ARGS=()
  [[ "$USE_SUDO" -eq 1 ]] && NET_ARGS+=(--sudo)
  # net_config.sh сам впишет wifi_ssid / wifi_password / server_base_url.
  # Не валим весь запуск, если что-то из сети не определилось (может быть вписано
  # вручную) — печатаем предупреждение и идём дальше.
  # ${NET_ARGS[@]+"${NET_ARGS[@]}"} вместо "${NET_ARGS[@]}": на macOS bash 3.2
  # (система по умолчанию до сих пор её ставит) `set -u` + пустой массив в
  # "${arr[@]}" кидает "unbound variable" — баг движка, пофиксили только в 4.4+.
  bash "$ROOT_DIR/scripts/net_config.sh" ${NET_ARGS[@]+"${NET_ARGS[@]}"} || \
    echo "⚠️  net_config.sh завершился с ошибкой — проверь config.json вручную." >&2
  echo
fi

# ②  Прошивка ESP32 (на хосте, через USB)
if [[ "$DO_FLASH" -eq 1 ]]; then
  echo "───────────────────────────────────────────────"
  echo "② Прошиваю ESP32 (USB, локальный arduino-cli)…"
  echo "───────────────────────────────────────────────"
  if ! command -v arduino-cli >/dev/null 2>&1; then
    echo "⚠️  arduino-cli не установлен — пропускаю прошивку." >&2
    echo "    Установи:  brew install arduino-cli   (и запусти ./start.sh снова)" >&2
  elif ! ls /dev/cu.usbserial-* /dev/ttyUSB* /dev/ttyACM* >/dev/null 2>&1; then
    echo "⚠️  Плата ESP32 по USB не найдена — пропускаю прошивку." >&2
    echo "    Подключи плату и запусти ./start.sh снова (или ./esp/flash.sh)." >&2
  else
    # flash.sh сам перегенерирует config.h из config.json, скомпилит и зальёт.
    bash "$ROOT_DIR/esp/flash.sh" || \
      echo "⚠️  Прошивка не удалась — сервер всё равно подниму." >&2
  fi
  echo
fi

# ③  Сервер в Docker
echo "───────────────────────────────────────────────"
echo "③ Поднимаю сервер в Docker…"
echo "───────────────────────────────────────────────"

# Порт хоста для проброса берём из config.json (server.port). Внутри контейнера
# сервер всегда слушает 8000; наружу отдаём на этот порт (см. docker-compose.yml).
SERVER_PORT="$(grep -oE '"port"[[:space:]]*:[[:space:]]*[0-9]+' "$ROOT_DIR/config.json" \
                 | grep -oE '[0-9]+' | head -1)"
[[ -z "$SERVER_PORT" ]] && SERVER_PORT=8000
export SERVER_PORT

# LAN-IP хоста: и для ссылки в выводе, и для проброса в контейнер (мастер /setup
# подставит его как адрес сервера — изнутри Docker реальный IP хоста не виден).
if [[ "$(uname -s)" == "Darwin" ]]; then
  HOST_IP="$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || echo localhost)"
else
  HOST_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
  [[ -z "$HOST_IP" ]] && HOST_IP=localhost
fi
export HOST_LAN_IP="$HOST_IP"

# Настроено ли развёртывание (реальные значения в config.json, не заглушки)?
# Если нет — сервер сам откроет мастер /setup при заходе на /dealer.
CONFIGURED=1
grep -qE '"(wifi_ssid)"[[:space:]]*:[[:space:]]*"YOUR_WIFI_SSID"' "$ROOT_DIR/config.json" && CONFIGURED=0
grep -qE '"(server_base_url)"[[:space:]]*:[[:space:]]*"http://192\.168\.1\.100:8000"' "$ROOT_DIR/config.json" && CONFIGURED=0

echo "▸ Порт сервера: $SERVER_PORT"
if [[ "$CONFIGURED" -eq 0 ]]; then
  echo "▸ Первый запуск — откроется мастер настройки: http://$HOST_IP:$SERVER_PORT/dealer"
  echo "  (он сам перенаправит на /setup: заполни Wi-Fi и адрес сервера)"
else
  echo "▸ Веб-интерфейс дилера: http://$HOST_IP:$SERVER_PORT/dealer"
fi
echo

# Открыть страницу в браузере (macOS: open, Linux: xdg-open). Первый запуск —
# сразу на /dealer, откуда сервер сам перекинет на мастер /setup.
open_browser() {
  local url="http://$HOST_IP:$SERVER_PORT/dealer"
  if command -v open >/dev/null 2>&1; then open "$url" >/dev/null 2>&1 || true
  elif command -v xdg-open >/dev/null 2>&1; then xdg-open "$url" >/dev/null 2>&1 || true
  fi
}

if [[ "$DETACH" -eq 1 ]]; then
  "${DC[@]}" up --build -d
  # Дождёмся, пока сервер начнёт отвечать, и откроем браузер.
  for _ in $(seq 1 15); do
    curl -s -o /dev/null "http://localhost:$SERVER_PORT/setup" && break
    sleep 1
  done
  open_browser
  echo
  echo "✅ Сервер поднят в фоне.  Логи:  ${DC[*]} logs -f    Стоп:  ./start.sh --down"
else
  # Foreground: up блокирует до Ctrl+C. Браузер открываем из подпроцесса, дав
  # серверу время подняться (сам up это не даёт сделать после себя).
  (
    for _ in $(seq 1 20); do
      curl -s -o /dev/null "http://localhost:$SERVER_PORT/setup" && break
      sleep 1
    done
    open_browser
  ) &
  # --build пересобирает при изменениях кода. Ctrl+C останавливает сервер.
  "${DC[@]}" up --build
fi
