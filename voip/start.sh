#!/bin/bash
#
# Start everything: the network address, the PBX, and the web interface.
#
#   ./start.sh              bring it all up and open the interface
#   ./start.sh --check      report what is up and change nothing
#   ./start.sh --stop       stop the PBX and the interface
#   ./start.sh --port 9000  serve the interface on another port
#
# The one step that needs a password is adding 192.168.100.2 to the Ethernet
# interface: macOS will not let a normal process configure an interface, and
# the SIP transport cannot bind without that address. Everything else runs
# unprivileged.
#
# Which interface that address goes on is worked out rather than hard-coded.
# It has been en8 on this machine, but the name follows the USB port the
# adapter is in and changes when it is moved, so the script looks for the one
# that can actually reach the gateway.
#
set -u

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GATEWAY=192.168.100.3
PBX_IP=192.168.100.2
NETMASK=255.255.255.0
WEB_PORT=8080

# ── output ──────────────────────────────────────────────────────────────

if [ -t 1 ]; then
	BOLD=$'\033[1m'; DIM=$'\033[2m'; RED=$'\033[31m'
	GREEN=$'\033[32m'; YELLOW=$'\033[33m'; OFF=$'\033[0m'
else
	BOLD=""; DIM=""; RED=""; GREEN=""; YELLOW=""; OFF=""
fi

step()  { printf "%s\n" "${BOLD}$1${OFF}"; }
ok()    { printf "  ${GREEN}✓${OFF} %s\n" "$1"; }
warn()  { printf "  ${YELLOW}!${OFF} %s\n" "$1"; }
fail()  { printf "  ${RED}✗${OFF} %s\n" "$1"; }
note()  { printf "    ${DIM}%s${OFF}\n" "$1"; }

# ── finding the adapter ─────────────────────────────────────────────────

have_address() {
	ifconfig 2>/dev/null | grep -q "inet ${PBX_IP} "
}

gateway_reachable() {
	ping -c 1 -W 1000 "$GATEWAY" >/dev/null 2>&1
}

# The adapter is whichever Ethernet interface is up and carrying a link.
# Wi-Fi and virtual interfaces are excluded: putting the address on one of
# those binds the transport somewhere the gateway cannot answer.
find_adapter() {
	local candidate
	# An interface already in the gateway's subnet is the strongest signal —
	# it is the one physically wired to the AP1100F.
	candidate=$(ifconfig 2>/dev/null | awk '
		/^[a-z][a-z0-9]*:/ { iface = substr($1, 1, length($1) - 1) }
		/inet 192\.168\.100\./ { print iface; exit }
	')
	if [ -n "$candidate" ]; then
		echo "$candidate"
		return 0
	fi

	# Otherwise the first Ethernet interface with a live link and no IPv4
	# address of its own: a USB adapter wired to the gateway looks exactly
	# like that, while the interface carrying the house network already has
	# an address and must not be touched.
	for candidate in $(ifconfig -l 2>/dev/null | tr ' ' '\n' | grep -E '^en[0-9]+$'); do
		ifconfig "$candidate" 2>/dev/null | grep -q "status: active" || continue
		# Any IPv4 address at all means the interface belongs to another
		# network. Matching on a prefix would let 192.168.49.x through and
		# put the PBX address on the Wi-Fi side, where the gateway cannot
		# answer and the transport binds to the wrong place.
		if ifconfig "$candidate" 2>/dev/null | grep -qE "inet [0-9]"; then
			continue
		fi
		echo "$candidate"
		return 0
	done
	return 1
}

# ── steps ───────────────────────────────────────────────────────────────

# Look for the gateway on the wire rather than trusting the address in the
# configuration. The AP1100F answers ARP even when it will not answer ping,
# so a scan of the subnet finds it whichever address it ended up with —
# useful when someone has changed it, or when the adapter has moved and the
# old address no longer matches.
discover_gateway() {
	local adapter="$1"
	local found=""

	# Cheapest first: it is almost always where it was last seen.
	if ping -c 1 -W 500 "$GATEWAY" >/dev/null 2>&1; then
		echo "$GATEWAY"
		return 0
	fi

	# Prime the ARP cache by pinging the broadcast address, then read back
	# whatever answered. Far quicker than probing 254 addresses one by one,
	# and the gateway replies to this even with telnet closed.
	ping -c 2 -W 500 255.255.255.255 >/dev/null 2>&1
	ping -c 2 -W 500 192.168.100.255 >/dev/null 2>&1
	sleep 1

	# AddPac's OUI is 00:02:a4 — the gateway is identifiable by it, so a
	# neighbour on the subnet cannot be mistaken for it.
	found=$(arp -an 2>/dev/null | grep -i "0:2:a4" | head -1 |
	        sed -n 's/.*(\([0-9.]*\)).*/\1/p')
	if [ -n "$found" ]; then
		echo "$found"
		return 0
	fi

	# Failing that, anything in the subnet that is not us.
	found=$(arp -an 2>/dev/null | grep "192.168.100" |
	        grep -v "incomplete" | grep -v "$PBX_IP" | head -1 |
	        sed -n 's/.*(\([0-9.]*\)).*/\1/p')
	[ -n "$found" ] && { echo "$found"; return 0; }

	return 1
}

sudo_command() {
	printf "\n%s\n" "${BOLD}Выполните в терминале:${OFF}"
	printf "\n    ${GREEN}sudo ifconfig %s alias %s 255.255.255.255${OFF}\n\n" \
	       "$1" "$PBX_IP"
	note "Затем запустите ./start.sh снова."
	note "Команда добавляет второй адрес и не трогает существующие."
	note "После перезагрузки Mac её нужно повторить."
}

bring_up_address() {
	step "Сеть"

	if have_address; then
		ok "$PBX_IP поднят"
		return 0
	fi

	local adapter
	adapter=$(find_adapter)
	if [ -z "$adapter" ]; then
		fail "не найден Ethernet-адаптер, подключённый к шлюзу"
		note "Проверьте, что USB-адаптер вставлен и кабель идёт в шлюз."
		note "Посмотреть интерфейсы: ifconfig -l"
		return 1
	fi

	fail "$PBX_IP не поднят на $adapter"
	note "Без этого адреса SIP-транспорт не привяжется и АТС не запустится."
	sudo_command "$adapter"
	return 1
}

check_gateway() {
	step "Шлюз"

	local found
	found=$(discover_gateway "$(find_adapter)")

	if [ -z "$found" ]; then
		fail "шлюз не найден в сети 192.168.100.0/24"
		note "Проверьте питание шлюза и кабель Ethernet."
		note "АТС запустится, но звонки работать не будут."
		return 1
	fi

	local latency
	latency=$(ping -c 1 -W 1000 "$found" 2>/dev/null |
	          sed -n 's/.*time=\([0-9.]*\).*/\1/p')
	ok "найден по адресу $found${latency:+, $latency мс}"

	# The address is written into two places — scripts/gateway.py for telnet
	# and etc/pjsip.conf for SIP — so a gateway that has moved needs both
	# changed. Saying so beats a working ping next to calls that fail.
	if [ "$found" != "$GATEWAY" ]; then
		warn "в конфигурации указан $GATEWAY, а шлюз отвечает с $found"
		note "Исправьте HOST в scripts/gateway.py и contact/match в etc/pjsip.conf,"
		note "иначе телнет и звонки пойдут не по тому адресу."
		return 1
	fi

	# Telnet is what the interface drives the gateway through; a gateway that
	# pings but refuses telnet has used up its few sessions.
	if ! nc -z -w 3 "$found" 23 >/dev/null 2>&1; then
		warn "telnet (порт 23) не отвечает"
		note "Обычно это исчерпанные сессии — они освободятся сами за минуту."
	fi
	return 0
}

start_pbx() {
	step "АТС"
	if "$ROOT/scripts/pbx.sh" status >/dev/null 2>&1; then
		ok "Asterisk уже запущен"
		return 0
	fi

	local output
	output=$("$ROOT/scripts/pbx.sh" start 2>&1)
	if echo "$output" | grep -q "^started:"; then
		ok "$(echo "$output" | sed 's/^started: //' | cut -c1-60)"
		return 0
	fi

	fail "Asterisk не запустился"
	echo "$output" | sed 's/^/    /'
	return 1
}

prepare_sounds() {
	step "Звуки"
	local output
	output=$(cd "$ROOT" && python3 scripts/sounds.py 2>&1)
	local count
	count=$(echo "$output" | grep -cE "^  [0-9]+\.")
	if [ "$count" -gt 0 ]; then
		ok "готово файлов: $count"
		echo "$output" | grep -E "^  [0-9]+\." | sed 's/^  /    /' | head -6
	else
		warn "в sounds/ нет файлов — положите mp3 или wav"
	fi
}

free_ports() {
	step "Порты FXS"
	gateway_reachable || { warn "пропущено, шлюз недоступен"; return 0; }

	local output
	output=$(cd "$ROOT" && python3 - <<-'PY' 2>&1
		import sys
		sys.path.insert(0, "scripts")
		import gateway
		try:
		    states = gateway.status()
		except gateway.GatewayError as exc:
		    print(f"error {exc}")
		    raise SystemExit(0)
		stuck = [p for p, s in states.items() if not s.usable]
		print("stuck " + (",".join(stuck) if stuck else ""))
	PY
	)

	case "$output" in
		error*) warn "${output#error }"; return 0 ;;
	esac

	local stuck="${output#stuck }"
	if [ -z "$stuck" ]; then
		ok "все 8 портов свободны"
		return 0
	fi

	warn "заняты: $stuck — освобождаю"
	(cd "$ROOT" && python3 -c "
import sys; sys.path.insert(0,'scripts'); import gateway
cycled, still = gateway.cycle_everything()
print('  ' + ('не освободились: ' + ', '.join(still) if still else 'освобождены'))
" 2>&1 | tail -1)
}

start_web() {
	step "Веб-интерфейс"

	local holder
	holder=$(lsof -nP -iTCP:"$WEB_PORT" -sTCP:LISTEN -t 2>/dev/null | head -1)
	if [ -n "$holder" ]; then
		if ps -p "$holder" -o command= 2>/dev/null | grep -q "web.py"; then
			warn "уже запущен (pid $holder) — перезапускаю, чтобы подхватил код"
			kill "$holder" 2>/dev/null
			sleep 1
		else
			fail "порт $WEB_PORT занят другим процессом (pid $holder)"
			ps -p "$holder" -o command= 2>/dev/null | sed 's/^/    /'
			return 1
		fi
	fi

	ok "http://127.0.0.1:$WEB_PORT"

	# The dial reader in esp/ posts from another device on the network, so the
	# server has to answer on the address that device can reach. Bound to
	# loopback it would answer this machine and nothing else, and the ESP32's
	# every report would fail with no sign of it here.
	local lan
	lan=$(ipconfig getifaddr "$(route -n get default 2>/dev/null \
		| awk '/interface:/ {print $2}')" 2>/dev/null)
	[ -n "$lan" ] && ok "http://$lan:$WEB_PORT — этот адрес видит ESP32"

	echo
	printf "%s\n" "${DIM}Ctrl+C — остановить интерфейс. АТС продолжит работать;${OFF}"
	printf "%s\n" "${DIM}остановить всё: ./start.sh --stop${OFF}"
	echo

	# Runs in the foreground: its log is the thing worth watching, and
	# backgrounding it would leave nowhere for errors to appear.
	cd "$ROOT" && exec python3 scripts/web.py --host 0.0.0.0 --port "$WEB_PORT"
}

# ── modes ───────────────────────────────────────────────────────────────

do_check() {
	step "Проверка"

	local adapter
	adapter=$(find_adapter)
	if [ -n "$adapter" ]; then
		ok "адаптер: $adapter"
	else
		fail "Ethernet-адаптер к шлюзу не найден"
	fi

	if have_address; then
		ok "$PBX_IP поднят"
	else
		fail "$PBX_IP не поднят"
		[ -n "$adapter" ] && sudo_command "$adapter"
	fi

	local found
	found=$(discover_gateway "$adapter")
	if [ -n "$found" ]; then
		ok "шлюз: $found"
		[ "$found" != "$GATEWAY" ] && \
			warn "в конфигурации $GATEWAY — адреса расходятся"
	else
		fail "шлюз не найден"
	fi

	"$ROOT/scripts/pbx.sh" status >/dev/null 2>&1 \
		&& ok "Asterisk запущен" || fail "Asterisk не запущен"
	lsof -nP -iTCP:"$WEB_PORT" -sTCP:LISTEN -t >/dev/null 2>&1 \
		&& ok "веб-интерфейс на $WEB_PORT" || fail "веб-интерфейс не запущен"

	echo
	(cd "$ROOT" && python3 scripts/health.py 2>&1)
}

do_stop() {
	step "Остановка"
	local holder
	holder=$(lsof -nP -iTCP:"$WEB_PORT" -sTCP:LISTEN -t 2>/dev/null | head -1)
	if [ -n "$holder" ]; then
		kill "$holder" 2>/dev/null && ok "веб-интерфейс остановлен"
	else
		ok "веб-интерфейс не запущен"
	fi
	"$ROOT/scripts/pbx.sh" stop 2>&1 | sed 's/^/  /'
	note "Адрес $PBX_IP остаётся поднятым; снять:"
	note "sudo ifconfig <интерфейс> -alias $PBX_IP"
}

usage() {
	sed -n '3,12p' "$0" | sed 's/^# \?//'
}

# ── entry ───────────────────────────────────────────────────────────────

MODE=start
while [ $# -gt 0 ]; do
	case "$1" in
		--check)  MODE=check ;;
		--stop)   MODE=stop ;;
		--port)   shift; WEB_PORT="${1:-8080}" ;;
		-h|--help) usage; exit 0 ;;
		*) echo "неизвестный аргумент: $1"; usage; exit 1 ;;
	esac
	shift
done

case "$MODE" in
	check) do_check; exit 0 ;;
	stop)  do_stop;  exit 0 ;;
esac

echo
printf "%s\n" "${BOLD}Телефония — запуск${OFF}"
echo

bring_up_address || exit 1
check_gateway || true          # the interface is still worth having
start_pbx || exit 1
prepare_sounds
free_ports
start_web
