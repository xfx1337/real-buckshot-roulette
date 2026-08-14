#!/bin/bash
#
# Start, stop and talk to the PBX.
#
#   ./scripts/pbx.sh start     start detached
#   ./scripts/pbx.sh stop      stop it
#   ./scripts/pbx.sh restart   stop, then start
#   ./scripts/pbx.sh status    is it up, and is the trunk usable
#   ./scripts/pbx.sh cli       attach a console
#   ./scripts/pbx.sh x "..."   run one CLI command
#
set -u

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
AST=/opt/homebrew/sbin/asterisk
CONF="$ROOT/etc/asterisk.conf"
RUNDIR="$ROOT/var/run"
GW=192.168.100.3
PBX_IP=192.168.100.2

# Asterisk prints this on every invocation when the shell's descriptor limit is
# below what it asks for. It is harmless and drowns out real output.
filter() { grep -v "^Cannot open maximum file descriptor"; }

cli() { "$AST" -C "$CONF" -rx "$*" 2>&1 | filter; }

running() { pgrep -f "asterisk -C $CONF" >/dev/null 2>&1; }

# The address the transport binds to has to exist before Asterisk starts, or it
# fails with "Can't assign requested address" and the reason is not obvious
# from the log. en8 loses a manually set address across reboots.
check_ip() {
	if ! ifconfig 2>/dev/null | grep -q "inet $PBX_IP "; then
		echo "!! $PBX_IP is not on any interface."
		echo "   The SIP transport cannot bind and Asterisk will not start."
		echo "   Fix with: sudo ipconfig set en8 MANUAL $PBX_IP 255.255.255.0"
		return 1
	fi
	return 0
}

start() {
	if running; then
		echo "already running (pid $(pgrep -f "asterisk -C $CONF" | head -1))"
		return 0
	fi

	check_ip || return 1

	# Only one process may hold UDP 5060. A second one starts happily, fails to
	# bind its transport, and then every call from it dies with
	# PJSIP_EUNSUPTRANSPORT while the trunk looks merely unreachable — a fault
	# that reads like a gateway problem and is not one.
	local holder
	holder=$(lsof -nP -iUDP:5060 -t 2>/dev/null | head -1)
	if [ -n "$holder" ]; then
		echo "!! UDP 5060 is held by pid $holder:"
		ps -p "$holder" -o command= 2>/dev/null | sed 's/^/   /'
		echo "   Stop it first; two PBXes on one port break calls in confusing ways."
		return 1
	fi

	# A control socket left by a process that is gone makes the CLI connect to
	# nothing and report the PBX as running.
	rm -f "$RUNDIR/asterisk.ctl" "$RUNDIR/asterisk.pid"

	mkdir -p "$RUNDIR" "$ROOT/var/log" "$ROOT/var/spool"
	ulimit -n 4096 2>/dev/null

	"$AST" -C "$CONF" -f >/dev/null 2>&1 &
	disown 2>/dev/null

	for _ in $(seq 1 20); do
		sleep 0.5
		if cli "core show version" 2>/dev/null | grep -q Asterisk; then
			echo "started: $(cli 'core show version' | head -1)"
			return 0
		fi
	done

	echo "!! did not come up within 10s; last errors:"
	tail -5 "$ROOT/var/log/messages" 2>/dev/null | sed 's/^/   /'
	return 1
}

stop() {
	if ! running; then
		rm -f "$RUNDIR/asterisk.ctl" "$RUNDIR/asterisk.pid"
		echo "not running"
		return 0
	fi
	cli "core stop now" >/dev/null 2>&1
	for _ in $(seq 1 10); do
		sleep 0.5
		running || break
	done
	# An orphan whose control socket was removed cannot be reached by
	# "core stop now", and this build has ignored SIGTERM before.
	running && pkill -9 -f "asterisk -C $CONF"
	sleep 1
	rm -f "$RUNDIR/asterisk.ctl" "$RUNDIR/asterisk.pid"
	echo "stopped"
}

status() {
	printf '%-12s ' "pbx:"
	if running; then echo "up (pid $(pgrep -f "asterisk -C $CONF" | head -1))"
	else echo "DOWN"; return 1; fi

	printf '%-12s ' "$PBX_IP:"
	ifconfig 2>/dev/null | grep -q "inet $PBX_IP " && echo "present" || echo "MISSING"

	# "No objects found" here next to an AOR that exists is the signature of a
	# second Asterisk holding the port.
	printf '%-12s ' "transport:"
	cli "pjsip show transports" | grep -q transport-udp && echo "bound 5060" || echo "NOT BOUND"

	printf '%-12s ' "gateway:"
	ping -c 1 -W 1000 "$GW" >/dev/null 2>&1 && echo "$GW replies" || echo "$GW SILENT"

	# NonQual is the wanted state: the contact is usable and not being probed.
	# The AP1100F does not answer OPTIONS, and an Unavail contact makes
	# Asterisk refuse to place calls through it at all.
	printf '%-12s ' "trunk:"
	cli "pjsip show contacts" | awk '/addpac/ {print $3, $4; found=1} END {if (!found) print "NO CONTACT"}'

	printf '%-12s ' "channels:"
	cli "core show channels" | awk '/active channel/ {print $1 " active"}'
}

case "${1:-status}" in
	start)   start ;;
	stop)    stop ;;
	restart) stop; sleep 1; start ;;
	status)  status ;;
	cli)     exec "$AST" -C "$CONF" -r ;;
	x)       shift; cli "$@" ;;
	*)       sed -n '3,12p' "$0" | sed 's/^# \?//' ;;
esac
