#!/usr/bin/env bash
#
# Hand the farm one folder of recordings and one queue, and let it work alone.
#
#   ./scripts/voices_farm_run.sh            залить и запустить
#   ./scripts/voices_farm_run.sh --status   что ферма успела
#
# Every voice in build/voices_flat/ is uploaded in a single scp of the whole
# directory, then one detached process on the farm walks the queue: cut a
# speaker sample, generate all 1002 phrases, zip, next voice. Nothing on this
# side waits, polls or reconnects — the link is used twice and then dropped.
#
# That is the point of the design rather than a convenience. The batch runs for
# many hours and the link to the farm is not reliable enough to hold for them;
# a loop that supervised the queue from here would turn every dropped
# connection into a stopped batch. So the queue lives on the far side, under
# setsid + nohup, detached from any terminal and from this ssh session's
# process group. Close the laptop and it keeps going.
#
# `tts/remote.py generate` is what makes the detachment necessary: it starts a
# background thread and returns immediately, so a generation launched over a
# plain `ssh gpufarm ...` dies with that session and leaves a job file reading
# "generating" with nothing behind it.
#
# Resumable in two layers. A voice whose zip is already collected is skipped
# whole, and inside a voice tts/remote._generate skips phrases already on disk.
# Re-running after any interruption costs one pass over the queue, not GPU time.
set -euo pipefail

HOST="${TTS_FARM_HOST:-gpufarm}"
REMOTE_DIR="${TTS_FARM_DIR:-\$HOME/backshot-tts}"
REMOTE_PYTHON="${TTS_FARM_PYTHON:-venv/bin/python}"
FLAT="${FLAT_DIR:-build/voices_flat}"

SSH_OPTS=(-o BatchMode=yes -o ConnectTimeout=30 -o ServerAliveInterval=15
          -o ServerAliveCountMax=8 -o TCPKeepAlive=yes)

say() { printf '%s\n' "$*" >&2; }

# ── что уже сделано ─────────────────────────────────────────────────────

if [[ "${1:-}" == "--status" ]]; then
  ssh "${SSH_OPTS[@]}" "$HOST" 'bash -s' <<'REMOTE'
cd "$HOME/backshot-tts" || exit 1
B=tts/work/.batch
echo "--- очередь ---"
tail -n 40 "$B/progress.txt" 2>/dev/null || echo "(батч не запускался)"
echo
echo "--- жив ли ---"
pgrep -af 'farm_batch.py' >/dev/null && echo "работает" || echo "не работает"
echo
echo "--- собрано ---"
ls -la tts/work/.collected 2>/dev/null | tail -n +2 || echo "(пусто)"
REMOTE
  exit 0
fi

# ── что заливаем ────────────────────────────────────────────────────────

[[ -f "${FLAT}/manifest.json" ]] || {
  say "нет ${FLAT}/manifest.json — сначала: python3 scripts/voices_prepare.py"
  exit 1
}

# The queue as two columns, name and filename, in manifest order. Built here
# rather than on the farm so the far side needs nothing but bash: the manifest
# is the one place that knows which file belongs to which voice after the
# folders were flattened and the long recordings trimmed.
QUEUE="$(python3 -c "
import json
data = json.load(open('${FLAT}/manifest.json'))
for v in data['voices']:
    print(v['voice'], v['file'])
")"
COUNT="$(printf '%s\n' "$QUEUE" | grep -c . || true)"
[[ "$COUNT" -gt 0 ]] || { say "очередь пуста"; exit 1; }

say "голосов в очереди: ${COUNT}"

# ── одна заливка ────────────────────────────────────────────────────────

say "== заливка ${FLAT} =="
# The whole directory in one scp rather than a file at a time. Big transfers to
# this farm have truncated before, and one command that either arrives or does
# not is easier to reason about than 25 that can each half-finish. -C because
# ogg is already compressed but the queue file and manifest are not, and the
# option costs nothing on the audio.
ssh "${SSH_OPTS[@]}" "$HOST" \
  "mkdir -p \$HOME/backshot-tts/tts/work/.inbox \$HOME/backshot-tts/tts/work/.batch \$HOME/backshot-tts/tts/work/.collected \$HOME/backshot-tts/scripts"

# The destination is written relative to the login home rather than with an
# explicit $HOME: scp does not run a shell on the far side, so a $HOME there
# arrives as four literal characters and becomes a directory of that name.
scp "${SSH_OPTS[@]}" -C -q "${FLAT}"/* \
  "${HOST}:backshot-tts/tts/work/.inbox/"

# ── очередь и её исполнитель ────────────────────────────────────────────

say "== запуск батча =="

# The queue travels as data and the runner as code, kept apart so that no voice
# name or filename is ever interpolated into a shell command. The runner reads
# the queue line by line; a Russian filename or a space in one is a value it
# reads, never a fragment of script it executes.
printf '%s\n' "$QUEUE" | ssh "${SSH_OPTS[@]}" "$HOST" \
  "cat > \$HOME/backshot-tts/tts/work/.batch/queue.txt"

# The runner is a Python program rather than a shell loop, and it does the
# generating itself in the foreground.
#
# The shell version could not work. `tts.remote generate` marks a job as
# generating, starts a daemon thread and returns, so the thread dies with the
# process that started it — a queue of `remote generate` calls leaves every job
# file reading "generating" with nothing behind it. Waiting on the job file
# afterwards then waits forever on a number nobody is still incrementing.
scp "${SSH_OPTS[@]}" -C -q scripts/farm_batch.py \
  "${HOST}:backshot-tts/scripts/farm_batch.py"

# setsid so the batch is not in this ssh session's process group and does not
# take its SIGHUP; nohup and a closed stdin for the same reason at the shell
# level. Without both, the queue stops at whichever voice it had reached when
# the connection dropped — and this connection drops.
#
# TTS_WORKERS is three, and the number is set by system memory rather than by
# the card.
#
# Four was tried first, chosen from GPU memory alone: four checkpoints fit in
# 16 GB with room to spare, and the card sat at 98%. But each worker also holds
# about 6 GB of ordinary RAM, and this machine has 25 GB — so four put it 5 GB
# into swap, and phrases that took 1.7 s while memory lasted took 6.4 s once it
# did not. The card was never the constraint; it looked busy because a worker
# waiting on the disk still holds its context.
#
# Three fit in RAM with a margin. Three workers not swapping beat four that do,
# and the ceiling to watch when changing this is `free -h`, not nvidia-smi.
ssh "${SSH_OPTS[@]}" "$HOST" "TTS_WORKERS=${TTS_WORKERS:-3} bash -s" <<'LAUNCH'
cd "$HOME/backshot-tts" || exit 1
B=tts/work/.batch
pgrep -f 'farm_batch.py' >/dev/null && { echo "батч уже работает, второй не запускаю"; exit 0; }
# Appended to, never truncated. The log is the only record of what a run that
# lasted overnight actually did, and a restart to change a setting must not be
# the thing that erases it.
printf '\n=== перезапуск %s (воркеров %s) ===\n' "$(date '+%F %T')" "${TTS_WORKERS:-3}" >> "$B/progress.txt"
TTS_WORKERS="${TTS_WORKERS:-3}" setsid nohup venv/bin/python scripts/farm_batch.py "$B/queue.txt" \
  >> "$B/batch.log" 2>&1 < /dev/null &
sleep 5
pgrep -f 'farm_batch.py' >/dev/null && echo "запущен (воркеров ${TTS_WORKERS:-3})" || { echo "НЕ ЗАПУСТИЛСЯ"; tail -20 "$B/batch.log"; exit 1; }
LAUNCH

say ""
say "батч работает на ферме сам. соединение больше не нужно."
say "проверить:  ./scripts/voices_farm_run.sh --status"
say "архивы:     ~/backshot-tts/tts/work/.collected/ на ферме"
