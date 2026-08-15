#!/usr/bin/env bash
#
# ─────────────────────────────────────────────────────────────────────────────
#  Buckshot Roulette IRL — весь проект одной командой, без Docker.
# ─────────────────────────────────────────────────────────────────────────────
#
#   ./run-all.sh              поднять всё и открыть панели
#   ./run-all.sh --check      сказать, что сейчас поднято, и ничего не менять
#   ./run-all.sh --stop       остановить всё
#
# Что поднимается, в этом порядке:
#
#   ① сеть        192.168.100.2 на USB-Ethernet — к нему привязан SIP-транспорт
#   ② АТС         Asterisk (voip/scripts/pbx.sh) + звуки + чистка FXS-портов
#   ③ веб АТС     voip/scripts/web.py на :8080 — состояние трубок, звонок кнопкой
#   ④ игра        app/server.py + MediaMTX (run.py) на :8000 — игроки, ТВ, камеры
#
# Docker не участвует нигде. Порты слушаются процессами хоста напрямую, без
# проброса: MediaMTX видит реальные адреса телефонов, а не docker-мост, и SIP
# с RTP не проходят через NAT контейнера. Это единственный режим, в котором
# работают телефоны — Asterisk'у нужен адрес на интерфейсе этой машины, telnet
# до шлюза и наушниковый выход, из которого звук идёт в капсюль трубки.
#
# Порядок важен: Asterisk стартует первым и держит UDP 5060 монопольно. Второй
# экземпляр поднимется молча, но звонки будут падать с PJSIP_EUNSUPTRANSPORT —
# отказ, который читается как проблема шлюза и ею не является.
#
# Флаги:
#   --no-voip       без телефонии (игра + камеры)
#   --no-cams       без MediaMTX (камеры не нужны)
#   --no-flash      не прошивать ESP32 (по умолчанию и не прошивает, см. --flash)
#   --flash         прошить ESP32 по USB перед запуском
#   --no-net        не переопределять Wi-Fi/LAN-IP в config.json
#   --sudo          достать скрытый SSID через sudo wdutil (macOS 26+)
#   --no-browser    не открывать браузер
#   --no-farm       не поднимать туннель к машине с видеокартой
#   --port N        порт игрового сервера (по умолчанию из config.json)
#
# Требования: python3 + venv проекта, Asterisk (brew install asterisk),
# бинарь mediamtx в mediamtx/. Для прошивки — arduino-cli и плата по USB.
# Голос: каталог готовых фраз в tts/cache/ (см. tts/README.md). Чтобы
# добавлять новые голоса из панели, нужна машина с видеокартой — туннель к ней
# скрипт поднимает сам.

set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

PBX_IP=192.168.100.2
GATEWAY=192.168.100.3
VOIP_WEB_PORT=8080

# Машина с видеокартой, на которой клонируются голоса. Ферма там слушает
# только петлю — наружу порт не открыт, — поэтому дотягиваемся туннелем.
# Имя хоста берётся из ~/.ssh/config; переопределяется переменной окружения,
# если ферма переехала.
FARM_HOST="${FARM_HOST:-gpufarm}"
FARM_PORT=8770

DO_VOIP=1
DO_CAMS=1
DO_FLASH=0
DO_NET=1
DO_BROWSER=1
DO_FARM=1
NET_SUDO=0
GAME_PORT=""
MODE=start

while [[ $# -gt 0 ]]; do
  case "$1" in
    --no-voip)    DO_VOIP=0 ;;
    --no-cams)    DO_CAMS=0 ;;
    --flash)      DO_FLASH=1 ;;
    --no-flash)   DO_FLASH=0 ;;
    --no-net)     DO_NET=0 ;;
    --sudo)       NET_SUDO=1 ;;
    --no-browser) DO_BROWSER=0 ;;
    --no-farm)    DO_FARM=0 ;;
    --port)       shift; GAME_PORT="${1:-}" ;;
    --check)      MODE=check ;;
    --stop)       MODE=stop ;;
    -h|--help)    grep '^#' "$0" | sed 's/^# \{0,1\}//' | sed '/^!/d'; exit 0 ;;
    *) echo "неизвестный аргумент: $1" >&2; exit 1 ;;
  esac
  shift
done

# ── вывод ───────────────────────────────────────────────────────────────

if [ -t 1 ]; then
  BOLD=$'\033[1m'; DIM=$'\033[2m'; RED=$'\033[31m'
  GREEN=$'\033[32m'; YELLOW=$'\033[33m'; OFF=$'\033[0m'
else
  BOLD=""; DIM=""; RED=""; GREEN=""; YELLOW=""; OFF=""
fi

step() { printf "\n%s\n" "${BOLD}$1${OFF}"; }
ok()   { printf "  ${GREEN}✓${OFF} %s\n" "$1"; }
warn() { printf "  ${YELLOW}!${OFF} %s\n" "$1"; }
fail() { printf "  ${RED}✗${OFF} %s\n" "$1"; }
note() { printf "    ${DIM}%s${OFF}\n" "$1"; }

# ── общее ───────────────────────────────────────────────────────────────

PY="$ROOT/venv/bin/python"
[[ -x "$PY" ]] || PY="$(command -v python3 || true)"
[[ -n "$PY" ]] || { echo "❌ Не найден python3." >&2; exit 1; }

if [[ ! -f "$ROOT/config.json" ]]; then
  echo "▸ config.json не найден — создаю из config.example.json."
  cp "$ROOT/config.example.json" "$ROOT/config.json"
fi

if [[ -z "$GAME_PORT" ]]; then
  GAME_PORT="$(grep -oE '"port"[[:space:]]*:[[:space:]]*[0-9]+' "$ROOT/config.json" \
               | grep -oE '[0-9]+' | head -1)"
  [[ -n "$GAME_PORT" ]] || GAME_PORT=8000
fi

lan_ip() {
  if [[ "$(uname -s)" == "Darwin" ]]; then
    ipconfig getifaddr "$(route -n get default 2>/dev/null | awk '/interface:/ {print $2}')" \
      2>/dev/null || echo localhost
  else
    hostname -I 2>/dev/null | awk '{print $1}'
  fi
}
LAN="$(lan_ip)"
[[ -n "$LAN" ]] || LAN=localhost

tcp_holder() { lsof -nP -iTCP:"$1" -sTCP:LISTEN -t 2>/dev/null | head -1; }
udp_holder() { lsof -nP -iUDP:"$1" -t 2>/dev/null | head -1; }
have_pbx_ip() { ifconfig 2>/dev/null | grep -q "inet ${PBX_IP} "; }
game_up() { curl -s -o /dev/null -m 2 "http://localhost:$GAME_PORT/setup"; }

# Ethernet-интерфейс, воткнутый в шлюз: тот, что уже в его подсети, иначе
# первый активный без собственного IPv4. Wi-Fi исключён — адрес на нём
# привяжет транспорт туда, откуда шлюз не ответит.
find_adapter() {
  local candidate
  candidate=$(ifconfig 2>/dev/null | awk '
    /^[a-z][a-z0-9]*:/ { iface = substr($1, 1, length($1) - 1) }
    /inet 192\.168\.100\./ { print iface; exit }')
  [[ -n "$candidate" ]] && { echo "$candidate"; return 0; }

  for candidate in $(ifconfig -l 2>/dev/null | tr ' ' '\n' | grep -E '^en[0-9]+$'); do
    ifconfig "$candidate" 2>/dev/null | grep -q "status: active" || continue
    ifconfig "$candidate" 2>/dev/null | grep -qE "inet [0-9]" && continue
    echo "$candidate"; return 0
  done
  return 1
}

open_url() {
  [[ "$DO_BROWSER" -eq 1 ]] || return 0
  if command -v open >/dev/null 2>&1; then open "$1" >/dev/null 2>&1 || true
  elif command -v xdg-open >/dev/null 2>&1; then xdg-open "$1" >/dev/null 2>&1 || true
  fi
}

# ── режим --check ───────────────────────────────────────────────────────

do_check() {
  step "Проверка"

  local adapter; adapter=$(find_adapter || true)
  [[ -n "$adapter" ]] && ok "Ethernet к шлюзу: $adapter" || fail "Ethernet-адаптер к шлюзу не найден"
  have_pbx_ip && ok "$PBX_IP поднят" || fail "$PBX_IP не поднят"
  ping -c 1 -W 1000 "$GATEWAY" >/dev/null 2>&1 \
    && ok "шлюз $GATEWAY отвечает" || fail "шлюз $GATEWAY молчит"

  bash "$ROOT/voip/scripts/pbx.sh" status >/dev/null 2>&1 \
    && ok "Asterisk запущен" || fail "Asterisk не запущен"
  [[ -n "$(tcp_holder "$VOIP_WEB_PORT")" ]] \
    && ok "веб АТС на :$VOIP_WEB_PORT" || fail "веб АТС не запущен"

  game_up && ok "игровой сервер на :$GAME_PORT" || fail "игровой сервер не запущен"
  [[ -n "$(udp_holder 8890)" ]] \
    && ok "MediaMTX (SRT :8890)" || fail "MediaMTX не запущен"

  VOICE_STATE=$("$PY" -c "
import tts
s = tts.engine.available()
print(s['voice'], s['phrases'], s['expected'], int(bool(s['voices'])))
" 2>/dev/null || echo "")
  if [[ -z "$VOICE_STATE" ]]; then
    fail "голос: не проверить (tts не импортируется)"
  else
    read -r V_NAME V_HAVE V_WANT V_ANY <<<"$VOICE_STATE"
    if [[ "$V_ANY" == "0" ]]; then
      fail "голос: ни одного не установлено"
    elif [[ "$V_HAVE" -ge "$V_WANT" ]]; then
      ok "голос «$V_NAME»: все $V_WANT фраз"
    else
      fail "голосу «$V_NAME» не хватает $((V_WANT - V_HAVE)) фраз из $V_WANT"
    fi
  fi

  # Ферма нужна только для добавления голосов, поэтому её отсутствие — не
  # отказ: игра идёт на том, что уже лежит за столом.
  if [[ -n "$(tcp_holder "$FARM_PORT")" ]]; then
    FARM_GPU=$(curl -s --max-time 5 "http://127.0.0.1:$FARM_PORT/health" 2>/dev/null \
      | sed -n 's/.*"gpu":"\([^"]*\)".*/\1/p')
    [[ -n "$FARM_GPU" ]] \
      && ok "ферма голосов: $FARM_GPU" \
      || warn "туннель к ферме есть, но она не отвечает"
  else
    note "ферма голосов не подключена (новые голоса добавить нельзя)"
  fi

  echo
  (cd "$ROOT/voip" && "$PY" scripts/health.py 2>&1) || true
}

# ── режим --stop ────────────────────────────────────────────────────────

kill_and_wait() {
  local pattern="$1" label="$2"
  pgrep -f "$pattern" >/dev/null 2>&1 || { ok "$label не запущен"; return 0; }
  pkill -f "$pattern" 2>/dev/null || true
  for _ in $(seq 1 10); do
    pgrep -f "$pattern" >/dev/null 2>&1 || break
    sleep 0.5
  done
  if pgrep -f "$pattern" >/dev/null 2>&1; then
    warn "$label не ответил на TERM — добиваю"
    pkill -9 -f "$pattern" 2>/dev/null || true
  fi
  ok "$label остановлен"
}

do_stop() {
  step "Остановка"
  # run.py зовёт uvicorn внутри себя, поэтому строки «app.server:app» в
  # командной строке нет — по ней ловится только форма с --reload, где uvicorn
  # поднимает отдельный процесс. Бьём по обеим: иначе --stop рапортует «не
  # запущен», сервер продолжает отвечать в браузере, и следующий запуск падает
  # на занятом порте.
  kill_and_wait "run.py" "игровой сервер"
  kill_and_wait "app.server:app" "uvicorn (--reload)"
  kill_and_wait "mediamtx/mediamtx" "MediaMTX"
  kill_and_wait "scripts/web.py" "веб АТС"
  # Только туннель, не саму ферму: она служба на чужой машине и живёт своей
  # жизнью, а на ней может идти генерация, которой до этого Mac дела нет.
  kill_and_wait "$FARM_PORT:127.0.0.1:$FARM_PORT" "туннель к ферме"
  bash "$ROOT/voip/scripts/pbx.sh" stop 2>&1 | sed 's/^/  /' || true
  # Адрес намеренно оставляем: он ничему не мешает, а снять его — значит
  # спросить пароль ещё раз и потребовать его же на следующем запуске.
  if have_pbx_ip; then
    adapter=$(find_adapter || echo "<интерфейс>")
    ok "$PBX_IP оставлен поднятым — следующий запуск не спросит пароль"
    note "Снять вручную: sudo ifconfig $adapter -alias $PBX_IP"
  fi
  echo
  echo "✅ Остановлено."
}

case "$MODE" in
  check) do_check; exit 0 ;;
  stop)  do_stop;  exit 0 ;;
esac

# ── ① сеть ──────────────────────────────────────────────────────────────

printf "\n%s\n" "${BOLD}Buckshot Roulette IRL — запуск${OFF}"

if [[ "$DO_NET" -eq 1 ]]; then
  step "① Wi-Fi и LAN-IP"
  # Вывод НЕ глушим. net_config.sh пишет и сообщения, и промпты ("SSID",
  # "Пароль Wi-Fi") в stderr, а на macOS 26 ОС прячет SSID от терминала, так
  # что до ручного ввода доходит регулярно. Скрытый промпт выглядит как
  # зависший скрипт, Enter в него молча пропускает настройку, и игра потом
  # поднимается с чужим адресом в config.json.
  NET_ARGS=()
  [[ "$NET_SUDO" -eq 1 ]] && NET_ARGS+=(--sudo)
  # Без sed-отступа: он бы съел промпты, которые печатаются без перевода
  # строки, и ввод пришлось бы делать вслепую. PIPESTATUS тут не помог бы —
  # труба ломает и интерактивность тоже.
  bash "$ROOT/scripts/net_config.sh" ${NET_ARGS[@]+"${NET_ARGS[@]}"}
  NET_RC=$?
  if [[ "$NET_RC" -eq 0 ]]; then
    LAN="$(lan_ip)"; [[ -n "$LAN" ]] || LAN=localhost
    ok "config.json обновлён, LAN-IP $LAN"
  else
    warn "автонастройка сети не удалась — беру config.json как есть"
    note "Проверь server_base_url вручную или открой мастер /setup."
  fi

  # Заглушки означают, что настройка так и не прошла: плата с ними в Wi-Fi не
  # войдёт, а сервер отправит дилера в мастер /setup. Сказать об этом здесь
  # дешевле, чем выяснять это на столе с игроками.
  if grep -qE '"wifi_ssid"[[:space:]]*:[[:space:]]*("YOUR_WIFI_SSID"|"")' "$ROOT/config.json"; then
    warn "wifi_ssid в config.json не заполнен"
    note "SSID скрыт macOS 26. Варианты:"
    note "  ./run-all.sh --sudo        достать SSID через sudo wdutil"
    note "  выдать Терминалу доступ к Локации (Настройки → Конфиденциальность)"
    note "  вписать SSID и пароль руками в config.json"
    note "Без этого игра работает, но ESP32 в Wi-Fi не войдёт."
  fi
else
  ok "сеть: config.json взят как есть (LAN-IP $LAN)"
fi

# ── прошивка ESP32 (по флагу) ───────────────────────────────────────────

if [[ "$DO_FLASH" -eq 1 ]]; then
  step "Прошивка ESP32"
  if ! command -v arduino-cli >/dev/null 2>&1; then
    warn "arduino-cli не установлен — пропускаю"
    note "brew install arduino-cli"
  elif ! ls /dev/cu.usbserial-* /dev/ttyUSB* /dev/ttyACM* >/dev/null 2>&1; then
    warn "плата по USB не найдена — пропускаю"
  else
    bash "$ROOT/esp/flash.sh" 2>&1 | sed 's/^/  /' || warn "прошивка не удалась"
  fi
fi

# ── ② АТС ───────────────────────────────────────────────────────────────

VOIP_OK=0
if [[ "$DO_VOIP" -eq 1 ]]; then
  step "② Телефония"

  if have_pbx_ip; then
    ok "$PBX_IP поднят"
  else
    adapter=$(find_adapter || true)
    if [[ -z "$adapter" ]]; then
      fail "Ethernet-адаптер к шлюзу не найден — телефоны работать не будут"
      note "Проверь USB-адаптер и кабель до шлюза. Список: ifconfig -l"
    else
      # Адрес поднимаем сами. macOS не даёт непривилегированному процессу
      # настроить интерфейс, поэтому тут единственный за весь запуск sudo.
      # alias добавляет ВТОРОЙ адрес и не трогает существующие: LAN-адрес
      # машины и всё, что на интерфейсе уже есть, остаются на месте.
      # Слетает при перезагрузке Mac, снять вручную:
      #   sudo ifconfig <адаптер> -alias 192.168.100.2
      warn "$PBX_IP не поднят на $adapter — поднимаю"
      note "Нужен пароль: macOS не даёт настроить интерфейс без прав."
      note "Команда добавляет второй адрес и не трогает существующие."
      if sudo ifconfig "$adapter" alias "$PBX_IP" 255.255.255.255; then
        # ifconfig возвращает 0 и когда адрес не встал (например, интерфейс
        # ушёл down между проверкой и вызовом). Перечитываем состояние, а не
        # верим коду возврата: Asterisk потом падает с "Can't assign requested
        # address", и связать это с успешным на вид шагом непросто.
        if have_pbx_ip; then
          ok "$PBX_IP поднят на $adapter"
        else
          fail "команда прошла, но адреса на интерфейсе нет"
          note "Проверь: ifconfig $adapter"
        fi
      else
        fail "не удалось поднять $PBX_IP (пароль не введён или отказ sudo)"
        note "Вручную: sudo ifconfig $adapter alias $PBX_IP 255.255.255.255"
        note "Без телефонии: ./run-all.sh --no-voip"
      fi
    fi
  fi

  if have_pbx_ip; then
    ping -c 1 -W 1000 "$GATEWAY" >/dev/null 2>&1 \
      && ok "шлюз $GATEWAY отвечает" \
      || warn "шлюз $GATEWAY молчит — АТС поднимется, звонки не пойдут"

    if bash "$ROOT/voip/scripts/pbx.sh" status >/dev/null 2>&1; then
      ok "Asterisk уже запущен"
      VOIP_OK=1
    else
      out=$(bash "$ROOT/voip/scripts/pbx.sh" start 2>&1)
      if echo "$out" | grep -q "^started:"; then
        ok "$(echo "$out" | sed 's/^started: //' | cut -c1-60)"
        VOIP_OK=1
      else
        fail "Asterisk не запустился"
        echo "$out" | sed 's/^/    /'
      fi
    fi
  fi

  if [[ "$VOIP_OK" -eq 1 ]]; then
    # Звуки конвертируются заранее: библиотека читается при первом открытии
    # панели, и перекодировать трёхминутный файл в тот момент — пауза ровно
    # там, где оператор ждёт список.
    (cd "$ROOT/voip" && "$PY" scripts/sounds.py >/dev/null 2>&1) && ok "звуки готовы" \
      || warn "sounds.py отработал с ошибкой — проверь voip/sounds/"

    # Залипшие FXS-порты: трубка, брошенная мимо рычага в прошлой партии,
    # держит порт занятым, и следующий звонок на неё просто не проходит.
    if ping -c 1 -W 1000 "$GATEWAY" >/dev/null 2>&1; then
      stuck=$(cd "$ROOT/voip" && "$PY" - <<-'PY' 2>/dev/null
			import sys
			sys.path.insert(0, "scripts")
			import gateway
			try:
			    states = gateway.status()
			except Exception:
			    raise SystemExit(0)
			print(",".join(p for p, s in states.items() if not s.usable))
			PY
      )
      if [[ -n "${stuck:-}" ]]; then
        warn "заняты FXS-порты: $stuck — освобождаю"
        (cd "$ROOT/voip" && "$PY" -c "
import sys; sys.path.insert(0,'scripts'); import gateway
cycled, still = gateway.cycle_everything()
print('  ' + ('не освободились: ' + ', '.join(still) if still else '  освобождены'))
" 2>&1 | tail -1)
      else
        ok "все FXS-порты свободны"
      fi
    fi
  fi
fi

# ── ③ веб АТС ───────────────────────────────────────────────────────────

if [[ "$VOIP_OK" -eq 1 ]]; then
  step "③ Веб-панель АТС"
  holder=$(tcp_holder "$VOIP_WEB_PORT")
  if [[ -n "$holder" ]]; then
    if ps -p "$holder" -o command= 2>/dev/null | grep -q "web.py"; then
      warn "уже запущен (pid $holder) — перезапускаю, чтобы подхватил код"
      kill "$holder" 2>/dev/null || true
      sleep 1
    else
      fail "порт $VOIP_WEB_PORT занят другим процессом (pid $holder)"
      ps -p "$holder" -o command= 2>/dev/null | sed 's/^/    /'
    fi
  fi
  if [[ -z "$(tcp_holder "$VOIP_WEB_PORT")" ]]; then
    # В фоне: в foreground он занял бы терминал, а смотреть в этом запуске
    # надо лог игрового сервера. Свой лог пишет в logs/voip-web.log.
    mkdir -p "$ROOT/logs"
    (cd "$ROOT/voip" && nohup "$PY" scripts/web.py --host 0.0.0.0 \
       --port "$VOIP_WEB_PORT" >"$ROOT/logs/voip-web.log" 2>&1 &)
    sleep 1
    [[ -n "$(tcp_holder "$VOIP_WEB_PORT")" ]] \
      && ok "http://127.0.0.1:$VOIP_WEB_PORT  (лог: logs/voip-web.log)" \
      || fail "не поднялся — смотри logs/voip-web.log"
  fi
fi

# ── голос ───────────────────────────────────────────────────────────────

step "Голос"
# Голос информатора — каталог заранее сгенерированных фраз, скопированный с
# машины с видеокартой. Проверять надо не «есть ли движок», а «все ли фразы на
# месте»: скопированный наполовину голос молчит ровно на недостающей, то есть
# посреди партии, у игрока с трубкой в руке.
VOICE_STATE=$("$PY" -c "
import tts
s = tts.engine.available()
print(s['voice'], s['phrases'], s['expected'], int(bool(s['voices'])))
" 2>/dev/null || echo "")
if [[ -z "$VOICE_STATE" ]]; then
  fail "не проверить голос (tts не импортируется)"
else
  read -r V_NAME V_HAVE V_WANT V_ANY <<<"$VOICE_STATE"
  if [[ "$V_ANY" == "0" ]]; then
    fail "ни одного голоса не установлено — телефон и лупа будут молчать"
    note "Голоса готовятся на машине с видеокартой, см. tts/README.md"
  elif [[ "$V_HAVE" -ge "$V_WANT" ]]; then
    ok "голос «$V_NAME»: все $V_WANT фраз"
  else
    warn "голосу «$V_NAME» не хватает $((V_WANT - V_HAVE)) фраз из $V_WANT"
    note "Скопируйте каталог tts/cache/$V_NAME/ целиком заново"
  fi
fi

# ── машина с видеокартой ────────────────────────────────────────────────
#
# Нужна только чтобы добавлять новые голоса из панели. Игра без неё идёт
# как ни в чём не бывало: голоса, которые уже забраны за стол, лежат на
# диске, и телефон с лупой работают без всякой сети. Поэтому здесь ничего
# не падает — только сообщается, доступна ферма или нет.

if [[ "$DO_FARM" -eq 1 ]]; then
  step "Ферма голосов"

  # Занятый порт ещё не значит живой туннель: ssh переживает разрыв сети,
  # продолжая слушать локально, и запросы в такой канал висят до таймаута.
  # Панель при этом выглядит рабочей, а загруженный голос молча пропадает.
  # Поэтому спрашиваем саму ферму, и мёртвый туннель сносим.
  if [[ -n "$(tcp_holder "$FARM_PORT")" ]]; then
    if curl -s --max-time 6 -o /dev/null "http://127.0.0.1:$FARM_PORT/health" 2>/dev/null; then
      ok "туннель уже поднят на :$FARM_PORT"
      FARM_UP=1
    else
      warn "туннель на :$FARM_PORT висит — пересоздаю"
      pkill -9 -f "$FARM_PORT:127.0.0.1:$FARM_PORT" 2>/dev/null || true
      # ssh освобождает сокет не мгновенно, а занятый порт уронит новый
      # проброс с ExitOnForwardFailure.
      for _ in $(seq 1 10); do
        [[ -z "$(tcp_holder "$FARM_PORT")" ]] && break
        sleep 0.5
      done
      FARM_UP=0
    fi
  fi

  if [[ "${FARM_UP:-0}" -eq 1 ]]; then
    :
  elif ! ssh -G "$FARM_HOST" 2>/dev/null | grep -q "^hostname $FARM_HOST$"; then
    # ssh -G разворачивает ~/.ssh/config; если hostname остался равен имени,
    # записи о такой машине нет и подключаться некуда.
    # ServerAliveCountMax=3 при интервале 15 с: разорванный канал закрывается
    # сам примерно за минуту, а не остаётся слушать порт молча.
    if ssh -o BatchMode=yes -o ConnectTimeout=8 -f -N \
           -o ExitOnForwardFailure=yes \
           -o ServerAliveInterval=15 -o ServerAliveCountMax=3 \
           -L "$FARM_PORT:127.0.0.1:$FARM_PORT" "$FARM_HOST" 2>/dev/null; then
      ok "туннель к $FARM_HOST поднят"
      FARM_UP=1
    else
      warn "не достучаться до $FARM_HOST — новые голоса добавить не выйдет"
      note "Игра при этом работает: уже установленные голоса лежат локально"
      FARM_UP=0
    fi
  else
    warn "хост «$FARM_HOST» не описан в ~/.ssh/config"
    note "Без него вкладка «Голоса» скажет, что ферма не настроена"
    FARM_UP=0
  fi

  if [[ "${FARM_UP:-0}" -eq 1 ]]; then
    # Спрашиваем саму ферму, а не порт: туннель поднимается и тогда, когда на
    # той стороне служба лежит, и разница видна только по ответу.
    FARM_GPU=$(curl -s --max-time 8 "http://127.0.0.1:$FARM_PORT/health" 2>/dev/null \
      | sed -n 's/.*"gpu":"\([^"]*\)".*/\1/p')
    if [[ -n "$FARM_GPU" ]]; then
      ok "ферма отвечает: $FARM_GPU"
      export TTS_FARM="http://127.0.0.1:$FARM_PORT"
    else
      warn "туннель есть, но ферма не отвечает — служба на той стороне лежит"
      note "Поднять: ssh $FARM_HOST 'systemctl --user restart voicefarm'"
    fi
  fi
fi

# ── ④ игра и камеры ─────────────────────────────────────────────────────

step "④ Игровой сервер"

holder=$(tcp_holder "$GAME_PORT")
if [[ -n "$holder" ]]; then
  fail "порт $GAME_PORT занят (pid $holder)"
  ps -p "$holder" -o command= 2>/dev/null | sed 's/^/    /'
  note "Останови: ./run-all.sh --stop"
  exit 1
fi

# MediaMTX поднимает сам run.py — до uvicorn, чтобы камеры цеплялись сразу,
# не дожидаясь кнопки в панели дилера. Он же его и глушит на Ctrl+C.
if [[ "$DO_CAMS" -eq 1 && -n "$(udp_holder 8890)" ]]; then
  warn "UDP 8890 занят — MediaMTX уже запущен, run.py его пропустит"
fi

cat <<EOF

  ${BOLD}Открыть на этом Mac${OFF}
    дилер        http://localhost:$GAME_PORT/dealer
    камеры       http://localhost:$GAME_PORT/cams
    показ-экран  http://localhost:$GAME_PORT/showscreen
    ТВ-игрок     http://localhost:$GAME_PORT/telejoin
$( [[ "$VOIP_OK" -eq 1 ]] && echo "    АТС          http://127.0.0.1:$VOIP_WEB_PORT" )

  ${BOLD}Открыть с телефонов${OFF}
    игроки       http://$LAN:$GAME_PORT/join
    камера (SRT) srt://$LAN:8890?streamid=publish:cam1   (cam1…cam4)

  ${DIM}localhost для экранов этой машины — не LAN-IP: выбор устройства вывода
  звука работает только в secure context, по http://<LAN-IP> список колонок пуст.${OFF}

  ${DIM}Ctrl+C — остановит игру и MediaMTX. АТС и её панель продолжат работать;
  остановить всё: ./run-all.sh --stop${OFF}

EOF

RUN_ARGS=()
[[ "$DO_CAMS" -eq 1 ]] || RUN_ARGS+=(--no-mediamtx)
[[ "$DO_BROWSER" -eq 1 ]] || RUN_ARGS+=(--no-browser)
RUN_ARGS+=(--port "$GAME_PORT")

# exec: run.py дальше владеет терминалом, и Ctrl+C уходит прямо ему — он
# глушит MediaMTX в своём finally. Промежуточный bash съел бы сигнал и оставил
# MediaMTX висеть на 8890, из-за чего следующий запуск остался бы без камер.
exec "$PY" run.py ${RUN_ARGS[@]+"${RUN_ARGS[@]}"}
