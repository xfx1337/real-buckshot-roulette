#!/usr/bin/env bash
#
# Check that this machine can speak, and say what is missing if it cannot.
#
# There is nothing to install here any more. The informant's voice is a clone,
# and a cloned voice is too slow to synthesise at the table — so the whole
# vocabulary is generated ahead of time on a machine with a GPU
# (tts/pregenerate.py) and copied here as a directory of wav files. At the
# table the game only reads them.
#
# Which makes this a readiness check rather than a setup step. It answers the
# one question worth asking before a game: will every phrase the game can utter
# actually play, or is there one that will reach a player as a fault in the
# line.
#
#   ./tts/install.sh            what is installed and what is missing
#   ./tts/install.sh --voice X  check one particular voice
#
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="$HERE/../venv/bin/python"
[[ -x "$PY" ]] || PY="python3"

say() { printf '%s\n' "$*"; }

# ffmpeg is not needed to play a pre-generated phrase, but it is what generated
# them and what the telephony side uses elsewhere, so its absence is worth
# reporting rather than discovering later.
say "ffmpeg: $(command -v ffmpeg || echo 'не установлен')"
say ""

if [[ "${1:-}" == "--voice" && -n "${2:-}" ]]; then
    "$PY" -m tts.pregenerate check --name "$2"
else
    "$PY" -m tts.pregenerate check
fi

say ""
say "Голоса готовятся заранее, на машине с видеокартой:"
say "  python -m tts.pregenerate prepare запись.mp3 --name имя [--song]"
say "  python -m tts.pregenerate generate --name имя"
say "и каталог tts/cache/имя/ копируется сюда целиком. См. tts/README.md."
