#!/bin/bash
# Convert an audio file into something Asterisk can play down a phone line.
#
#   ./scripts/add-sound.sh ../app/static/audio/defaults/ambient_lobby.ogg
#   ./scripts/add-sound.sh input.mp3 shot_fired      # under a chosen name
#
# The result lands in voip/sounds/ as 8 kHz mono signed-linear, which
# run-asterisk.sh installs on every start. Play it from the dialplan by name,
# without the extension: Playback(ambient_lobby).
set -euo pipefail

VOIP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [[ $# -lt 1 ]]; then
    sed -n '2,9p' "${BASH_SOURCE[0]}" | sed 's/^# \?//'
    exit 1
fi

INPUT="$1"
NAME="${2:-$(basename "${INPUT%.*}")}"
OUTPUT="$VOIP_DIR/sounds/$NAME.sln"

mkdir -p "$VOIP_DIR/sounds"

# 8 kHz mono is what the codecs on this trunk carry (alaw/ulaw), so resampling
# here costs nothing at call time. .sln is raw samples, no header.
ffmpeg -y -loglevel error -i "$INPUT" \
    -ac 1 -ar 8000 -f s16le -acodec pcm_s16le "$OUTPUT"

SECONDS_LONG=$(( $(stat -f%z "$OUTPUT") / 16000 ))
echo "$OUTPUT — ${SECONDS_LONG}s"
echo "Play it with: Playback($NAME)"
