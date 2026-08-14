#!/usr/bin/env bash
#
# Builds the TA-1132 dial reader and flashes it to an ESP32.
#
#   ./flash.sh              build, flash, then open the serial monitor
#   ./flash.sh build        build only, no board needed
#   ./flash.sh config       change the network, server or handset
#   ./flash.sh monitor      open the serial monitor on its own
#   ./flash.sh --port /dev/cu.usbserial-0001   name the port by hand
#
# The first run asks for the Wi-Fi network, the server and which handset the
# reader is wired to, and keeps the answers in src/config.local.h. What this
# machine can work out for itself — its own address on the network, the
# networks it has joined before — it fills in as the default.
#
# The sources live in src/ as main.cpp and config.h, which is what
# PlatformIO wants. arduino-cli instead wants a sketch directory whose .ino
# matches its name, so the build assembles one in build/ from those same
# files. Editing happens in src/; build/ is a copy and is overwritten every
# run.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKETCH="$HERE/build/ta1132"
LOCAL="$HERE/src/config.local.h"
FQBN="esp32:esp32:esp32"
BAUD=115200

PORT=""
ACTION="all"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --port|-p) PORT="${2:-}"; shift 2 ;;
        --fqbn)    FQBN="${2:-}"; shift 2 ;;
        build|flash|monitor|config|all) ACTION="$1"; shift ;;
        -h|--help) sed -n '3,22p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done

die() { echo "error: $*" >&2; exit 1; }

command -v arduino-cli >/dev/null \
    || die "arduino-cli is not installed — brew install arduino-cli"

# The ESP32 core carries the compiler, the WiFi and HTTPClient libraries,
# and the upload tool. Without it nothing below works.
if ! arduino-cli core list 2>/dev/null | grep -q '^esp32:esp32'; then
    echo "the esp32 core is missing; installing it"
    arduino-cli config add board_manager.additional_urls \
        https://raw.githubusercontent.com/espressif/arduino-esp32/gh-pages/package_esp32_index.json \
        2>/dev/null || true
    arduino-cli core update-index
    arduino-cli core install esp32:esp32
fi

# ── the sketch directory ───────────────────────────────────────────────

assemble() {
    rm -rf "$SKETCH"
    mkdir -p "$SKETCH"
    cp "$HERE/src/main.cpp" "$SKETCH/ta1132.ino"
    cp "$HERE/src/config.h" "$SKETCH/config.h"
    # config.h includes this when it exists; without it the firmware compiles
    # and says on the console that it has no network.
    [[ -f "$LOCAL" ]] && cp "$LOCAL" "$SKETCH/config.local.h"
    return 0
}

# ── settings ───────────────────────────────────────────────────────────
#
# Everything the firmware needs that this machine cannot work out on its own
# is asked for once and kept in src/config.local.h, which config.h includes.
# It holds a Wi-Fi password, so it does not belong in version control.
#
# What the machine can work out, it does: the address the server is reachable
# at is this machine's own address on the network, and the networks it might
# be asked to join are the ones it has joined before.

# The address an ESP32 on the same network would use to reach the server. Not
# the loopback: that is the one address that cannot work from another device.
guess_host() {
    local interface address
    interface="$(route -n get default 2>/dev/null | awk '/interface:/ {print $2}')"
    [[ -n "$interface" ]] && address="$(ipconfig getifaddr "$interface" 2>/dev/null)"
    if [[ -z "${address:-}" ]]; then
        # No default route, or a platform without ipconfig. Take the first
        # address that is neither loopback nor a link-local self-assignment.
        address="$(ifconfig 2>/dev/null \
            | awk '/inet / && $2 !~ /^127\./ && $2 !~ /^169\.254\./ {print $2; exit}')"
    fi
    printf '%s' "${address:-}"
}

# Networks this Mac has connected to before, most recent first, which is
# almost always the one the ESP32 should join too.
known_networks() {
    local device
    device="$(networksetup -listallhardwareports 2>/dev/null \
        | awk '/Hardware Port: Wi-Fi/ {getline; print $2}')"
    [[ -n "$device" ]] || return 0
    networksetup -listpreferredwirelessnetworks "$device" 2>/dev/null \
        | tail -n +2 | sed 's/^[[:space:]]*//' | grep -v '^$'
}

# Reads one value into REPLY_VALUE, showing a default that Enter accepts.
#
# The answer comes back in a variable rather than on stdout because these
# prompts have to be seen as they are asked: run inside $(...) the prompt
# would be captured along with the answer and the screen would stay blank.
REPLY_VALUE=""
ask() {
    local prompt="$1" fallback="${2:-}" answer
    if [[ -n "$fallback" ]]; then
        read -r -p "$prompt [$fallback]: " answer
        REPLY_VALUE="${answer:-$fallback}"
    else
        read -r -p "$prompt: " answer
        REPLY_VALUE="$answer"
    fi
}

# The Wi-Fi network, into REPLY_VALUE. Offers the ones this machine knows,
# since typing an SSID by hand is the easiest thing here to get subtly wrong.
ask_ssid() {
    local -a networks=()
    local saved
    saved="$(current WIFI_SSID)"
    # Whatever is already configured goes first, so Enter keeps it.
    [[ -n "$saved" ]] && networks+=("$saved")
    while IFS= read -r line; do
        [[ "$line" == "$saved" ]] || networks+=("$line")
    done < <(known_networks)

    if [[ ${#networks[@]} -eq 0 ]]; then
        ask "  Wi-Fi network"
        return
    fi

    # A Mac remembers every network it has ever joined, which here was
    # twenty-six of them — a list long enough that the prompt scrolls off the
    # screen. macOS keeps them most-recently-joined first, so the top few are
    # the ones worth showing and the rest can be typed.
    local shown=8
    (( ${#networks[@]} < shown )) && shown=${#networks[@]}

    echo "  networks this Mac knows:"
    local i
    for (( i = 0; i < shown; i++ )); do
        printf '    %2d) %s\n' "$((i + 1))" "${networks[$i]}"
    done
    if (( ${#networks[@]} > shown )); then
        printf '    %2d) another one (%d more remembered)\n' \
               "$((shown + 1))" "$(( ${#networks[@]} - shown ))"
    else
        printf '    %2d) something else\n' "$((shown + 1))"
    fi
    networks=("${networks[@]:0:shown}")

    local choice
    read -r -p "  choose [1]: " choice
    choice="${choice:-1}"

    if [[ "$choice" =~ ^[0-9]+$ ]] && (( choice >= 1 && choice <= ${#networks[@]} )); then
        REPLY_VALUE="${networks[$((choice - 1))]}"
    else
        ask "  Wi-Fi network"
    fi
}

# The token the server checks. Read from the server's own environment when it
# is set there, so the two cannot disagree.
guess_token() {
    printf '%s' "${DIALER_TOKEN:-}"
}

# What a setting is set to now, falling back to the second argument when
# there is no saved value — so reconfiguring offers back what is already
# there, and a first run offers what this machine worked out.
current() {
    local value=""
    if [[ -f "$LOCAL" ]]; then
        value="$(sed -n \
            "s/^#define $1[[:space:]]*\"\{0,1\}\([^\"]*\)\"\{0,1\}[[:space:]]*$/\1/p" \
            "$LOCAL" | head -1)"
    fi
    printf '%s' "${value:-${2:-}}"
}

configure() {
    if [[ -f "$LOCAL" ]]; then
        echo "Changing the settings. Enter keeps what is there now."
    else
        echo "Setting up. Enter accepts what is in brackets."
    fi
    echo

    local ssid password host port extension token
    ask_ssid; ssid="$REPLY_VALUE"

    # The one setting never shown back: it is a password, and echoing it to
    # offer it as a default would put it on the screen. Enter keeps the stored
    # one instead, which is why the prompt says so only when there is one.
    local stored_password
    stored_password="$(current WIFI_PASSWORD)"
    if [[ -n "$stored_password" && "$ssid" == "$(current WIFI_SSID)" ]]; then
        read -r -s -p "  Wi-Fi password [unchanged]: " password; echo
        password="${password:-$stored_password}"
    else
        read -r -s -p "  Wi-Fi password: " password; echo
    fi

    # Each default is what is already set, falling back to what the machine
    # can work out on a first run.
    ask "  address of this machine, as the ESP32 sees it" \
        "$(current SERVER_HOST "$(guess_host)")"; host="$REPLY_VALUE"
    ask "  port the game server listens on" \
        "$(current SERVER_PORT 8000)"; port="$REPLY_VALUE"
    ask "  which handset this reader is wired to (101-108)" \
        "$(current EXTENSION 101)"; extension="$REPLY_VALUE"
    ask "  shared token, blank for none" \
        "$(current DIALER_TOKEN "$(guess_token)")"; token="$REPLY_VALUE"

    # A here-document rather than echo lines: the password may contain any
    # character, and this way nothing re-interprets it on the way to the file.
    cat > "$LOCAL" <<EOF
// Written by ./flash.sh. Holds a Wi-Fi password — do not commit it.
// Run ./flash.sh config to change any of this.
#pragma once

#define WIFI_SSID     "$ssid"
#define WIFI_PASSWORD "$password"

#define SERVER_HOST "$host"
#define SERVER_PORT $port

#define EXTENSION "$extension"
#define DIALER_TOKEN "$token"
EOF

    echo
    echo "saved to src/config.local.h"
    echo "  network   $ssid"
    echo "  server    $host:$port"
    echo "  handset   $extension"
    # The token is a secret and the password more so: neither is echoed back,
    # only whether there is one.
    echo "  token     $([[ -n "$token" ]] && echo set || echo none)"
    echo

    # The address is only reachable if the server is listening on it: bound to
    # loopback, which is what web.py does by default, it answers this machine
    # and nothing else.
    if [[ -n "$host" ]] && ! (exec 3<>"/dev/tcp/$host/$port") 2>/dev/null; then
        echo "note: nothing is answering on $host:$port yet."
        echo "start the server so the ESP32 can reach it:"
        echo "  python3 scripts/web.py --host 0.0.0.0 --port $port"
        echo
    fi
    exec 3<&- 2>/dev/null || true
}

build() {
    # First run, or the settings were removed: ask rather than build something
    # that cannot reach anything.
    [[ -f "$LOCAL" ]] || configure
    assemble
    echo "building for $FQBN"
    arduino-cli compile --fqbn "$FQBN" "$SKETCH"
}

# ── finding the board ──────────────────────────────────────────────────

# The two Bluetooth and debug ports are always present on a Mac and are
# never the board, so a port that is neither is the one to use.
find_port() {
    local found
    found="$(arduino-cli board list --format json 2>/dev/null \
        | python3 -c '
import json, sys
try:
    data = json.load(sys.stdin)
except Exception:
    sys.exit(0)
ports = data.get("detected_ports", data) if isinstance(data, dict) else data
for entry in ports or []:
    port = entry.get("port", entry)
    address = port.get("address", "")
    if port.get("protocol") != "serial":
        continue
    if "Bluetooth" in address or "debug-console" in address:
        continue
    print(address)
' | head -1)"
    printf '%s' "$found"
}

resolve_port() {
    if [[ -n "$PORT" ]]; then
        [[ -e "$PORT" ]] || die "no such port: $PORT"
        return
    fi
    PORT="$(find_port)"
    [[ -n "$PORT" ]] || die "no board found — plug the ESP32 in, or pass --port.
Ports seen now:
$(arduino-cli board list 2>&1 | sed 's/^/  /')

A board that never appears usually needs its USB-serial driver:
CP210x or CH340, depending on which chip the board carries."
    echo "found a board on $PORT"
}

upload() {
    resolve_port
    echo "flashing $PORT"
    # A board without auto-reset wiring has to be put into its bootloader by
    # hand, so say how before the upload rather than after it times out.
    arduino-cli upload --fqbn "$FQBN" --port "$PORT" "$SKETCH" || die \
        "upload failed. If it stopped at 'Connecting...', hold BOOT on the
board, tap EN, and run this again while BOOT is still held."
}

monitor() {
    resolve_port
    echo "monitoring $PORT at $BAUD — ctrl-c to stop"
    arduino-cli monitor --port "$PORT" --config "baudrate=$BAUD"
}

case "$ACTION" in
    config)  configure ;;
    build)   build ;;
    flash)   build; upload ;;
    monitor) monitor ;;
    all)     build; upload; echo; monitor ;;
esac
