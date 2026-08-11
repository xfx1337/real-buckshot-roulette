#!/bin/bash
# Start the locally built Asterisk against the configs in voip/etc.
#
#   ./scripts/run-asterisk.sh          # run in the foreground
#   ./scripts/run-asterisk.sh -d       # run detached, as a daemon
#   ./scripts/run-asterisk.sh -r       # attach a console to a running instance
#   ./scripts/run-asterisk.sh -x "..." # run one CLI command and exit
#
# Configs are copied from voip/etc into the install tree on every start, so
# voip/etc stays the single place to edit them.
set -euo pipefail

VOIP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PREFIX="$VOIP_DIR/asterisk-local"
ASTERISK="$PREFIX/sbin/asterisk"
IFACE_ADDR="192.168.100.2"

if [[ ! -x "$ASTERISK" ]]; then
    echo "Asterisk is not built. See voip/README.md." >&2
    exit 1
fi

# -r and -x talk to an instance that is already running; no setup needed.
if [[ "${1:-}" == "-r" || "${1:-}" == "-x" ]]; then
    exec "$ASTERISK" -C "$PREFIX/etc/asterisk/asterisk.conf" "$@"
fi

# pjsip.conf binds to the en6 address, and Asterisk refuses to start if it is
# missing. en6 loses its static address on reboot.
if ! ifconfig | grep -q "inet $IFACE_ADDR "; then
    echo "$IFACE_ADDR is not configured on any interface. Restore it with:" >&2
    echo "  sudo ipconfig set en6 MANUAL $IFACE_ADDR 255.255.255.0" >&2
    exit 1
fi

# asterisk.conf carries absolute paths, so the prefix is substituted in.
sed "s|@PREFIX@|$PREFIX|g" "$VOIP_DIR/etc/asterisk.conf" \
    > "$PREFIX/etc/asterisk/asterisk.conf"
for conf in pjsip.conf extensions.conf rtp.conf modules.conf manager.conf; do
    cp "$VOIP_DIR/etc/$conf" "$PREFIX/etc/asterisk/$conf"
done

# Game audio, converted to 8 kHz mono by scripts/add-sound.sh. Copied in the
# same way as the configs, so a rebuilt install tree still has it.
if compgen -G "$VOIP_DIR/sounds/*.sln" > /dev/null; then
    cp "$VOIP_DIR"/sounds/*.sln "$PREFIX/var/lib/asterisk/sounds/en/"
fi

mkdir -p "$PREFIX/var/run/asterisk" "$PREFIX/var/log/asterisk"

if [[ "${1:-}" == "-d" ]]; then
    "$ASTERISK" -C "$PREFIX/etc/asterisk/asterisk.conf"
    echo "started — attach with ./scripts/run-asterisk.sh -r"
else
    exec "$ASTERISK" -C "$PREFIX/etc/asterisk/asterisk.conf" -f -vvv
fi
