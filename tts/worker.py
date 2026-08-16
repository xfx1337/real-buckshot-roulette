"""One F5 process that stays alive and speaks phrase after phrase.

The generation path used to pay for a whole model every time it wanted a
sentence. tts/backends.py runs F5 as a subprocess — it has to, because F5 wants
transformers>=5 and XTTS pins below that, and the two cannot share an
interpreter — and the simplest way to run a subprocess is one per phrase. For
the handful of lines an audition needs that is fine, and it is what backends.py
was written for.

A voice is a thousand phrases. At one subprocess each that is a thousand
interpreter starts, a thousand loads of the same checkpoint onto the same GPU,
and a thousand Whisper transcriptions of the same thirty-second sample, because
`ref_text` was left empty and F5 fills it in by listening to the reference
again. Measured on the farm it came to roughly fifteen seconds a phrase, of
which the actual synthesis was a small part.

So this module inverts the loop. The process starts once, loads the checkpoint
once, transcribes the speaker sample once, and then reads phrases off stdin for
as long as they keep coming. Everything that was per-phrase and constant
becomes per-run and constant.

The protocol is one json object per line in each direction, chosen so the
caller can be an ordinary subprocess.Popen and needs no framing of its own:

    in   {"text": "...", "target": "/path/to.wav"}
    out  {"ok": true, "target": "...", "seconds": 4.4}
         {"ok": false, "target": "...", "error": "..."}

A phrase that fails answers and the loop continues, because one bad line out of
a thousand should cost that line rather than the run. Anything that kills the
process itself is the caller's problem to notice — it sees the pipe close.

Lives in the main package rather than beside the venv it runs in: it is part of
the farm's code and is deployed with it. Nothing here may be imported by the
farm's own interpreter, so it is only ever executed by ~/f5venv/bin/python.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path


def _emit(payload: dict) -> None:
    """Answer one request. Flushed because the caller is waiting on this line."""
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def main() -> None:
    # The configuration that does not change across a run: which checkpoint,
    # which sample, whether to place stress marks. Read once from argv rather
    # than repeated on every phrase, since a run is one voice by definition.
    config = json.loads(sys.argv[1])

    sample = config["sample"]
    accent = bool(config.get("accent"))

    # Progress goes to stderr throughout. Stdout carries the protocol and
    # nothing else, and F5 prints freely — see the redirect below.
    print("worker: loading model", file=sys.stderr, flush=True)
    started = time.time()

    import numpy as np
    import soundfile as sf
    import torch
    from f5_tts.api import F5TTS

    model = F5TTS(
        model=config["arch"],
        ckpt_file=config["checkpoint"],
        vocab_file=config["vocab"],
    )
    print(f"worker: model ready in {time.time() - started:.1f}s",
          file=sys.stderr, flush=True)

    # RUAccent, loaded once for the same reason as the model. The marks are
    # what this checkpoint was fine-tuned to read; see tts/backends.py for why
    # they help here and hurt XTTS.
    accentizer = None
    if accent:
        try:
            from ruaccent import RUAccent

            accentizer = RUAccent()
            accentizer.load(omograph_model_size="turbo3.1", use_dictionary=True,
                            tiny_mode=False)
            print("worker: ruaccent ready", file=sys.stderr, flush=True)
        except Exception as exc:                                  # noqa: BLE001
            # Better unaccented than not at all: the model reads plain text
            # too, it simply guesses the stresses itself.
            print(f"worker: ruaccent unavailable, reading unaccented: {exc}",
                  file=sys.stderr, flush=True)

    # What the sample says, worked out once.
    #
    # F5 transcribes the reference itself when handed an empty ref_text, which
    # is the right default for a single phrase and is quietly ruinous for a
    # thousand: it is a Whisper pass over the same thirty seconds of audio
    # before every line. Transcribing here and passing the text back in on
    # every call is the same computation done once.
    #
    # A failure here is not fatal. Falling back to "" restores exactly the old
    # per-phrase behaviour, which is slow but correct.
    ref_text = ""
    try:
        from f5_tts.infer.utils_infer import preprocess_ref_audio_text

        _, ref_text = preprocess_ref_audio_text(sample, "")
        print(f"worker: sample transcribed: {ref_text[:60]!r}",
              file=sys.stderr, flush=True)
    except Exception as exc:                                      # noqa: BLE001
        print(f"worker: could not pre-transcribe, every phrase will: {exc}",
              file=sys.stderr, flush=True)

    _emit({"ready": True})

    spoken = 0
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue

        job = json.loads(line)
        if job.get("stop"):
            break

        target = job["target"]
        began = time.time()
        try:
            text = job["text"]
            if accentizer is not None:
                text = accentizer.process_all(text)

            # F5 writes progress and timing to stdout with plain print(), which
            # would land in the middle of the protocol. Pointing its show_info
            # at stderr keeps stdout clean; the tqdm bar already goes to stderr.
            wav, rate, _ = model.infer(
                ref_file=sample,
                ref_text=ref_text,
                gen_text=text,
                remove_silence=True,
                show_info=lambda *a, **k: print(*a, file=sys.stderr, flush=True),
            )

            audio = np.asarray(wav, dtype=np.float32).reshape(-1)
            peak = float(np.max(np.abs(audio))) if audio.size else 0.0
            if peak > 1.0:
                audio = audio / peak
            Path(target).parent.mkdir(parents=True, exist_ok=True)
            sf.write(target, audio, int(rate))

            spoken += 1
            _emit({"ok": True, "target": target,
                   "seconds": round(time.time() - began, 2)})
        except Exception as exc:                                  # noqa: BLE001
            # One phrase failing is one phrase, not the run. The caller records
            # it and moves on; a line with no file is caught by the missing()
            # check before a voice is ever called finished.
            _emit({"ok": False, "target": target, "error": str(exc)})

    print(f"worker: spoke {spoken} phrases", file=sys.stderr, flush=True)


if __name__ == "__main__":
    main()
