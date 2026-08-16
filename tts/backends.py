"""Speaking with a model that cannot live in the same process as XTTS.

XTTS pins `transformers<5`; F5-TTS wants `transformers>=5`. Installing the
second into the farm's environment uninstalls the first, and the voices that
already work stop working — the pin is not caution, the fifth branch removed a
helper XTTS imports. So a second model does not get imported here at all. It
gets its own virtualenv and is run as a subprocess: one phrase in, one wav out,
no shared dependency tree to break.

That costs an interpreter start and a model load per phrase, which would be
unbearable for a thousand-phrase run and is fine for the handful an audition
makes. Comparing models is what this is for; if one of them wins, making it
fast is the next problem and a different one.

Where each environment lives is configuration rather than discovery: a farm
that has not installed a model simply has no directory there, and the engine
shows as unavailable in the panel instead of failing when someone clicks it.

    ~/f5venv      python -m pip install f5-tts
    ~/fishvenv    python -m pip install git+https://github.com/fishaudio/fish-speech

Both are looked for beside the home directory of whoever runs the farm.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# Where a model's own virtualenv is expected. Overridable per model through
# the environment, because a farm may keep them on a different disk.
import os

VENVS = {
    "f5": Path(os.environ.get("F5_VENV", Path.home() / "f5venv")),
    "fish": Path(os.environ.get("FISH_VENV", Path.home() / "fishvenv")),
}

# The Russian F5 weights, and the vocabulary they were trained against.
#
# Naming these is not optional. F5-TTS ships an English base model and loads
# it when asked for nothing in particular; handed Russian text it produces
# something confidently fluent that is not Russian. The fine-tune below was
# trained on 5000 hours of mostly Russian speech and understands the stress
# marks this project's text uses.
#
#     https://huggingface.co/Misha24-10/F5-TTS_RUSSIAN
#     F5TTS_v1_Base_v2/model_last_inference.safetensors  → checkpoint
#     F5TTS_v1_Base/vocab.txt                            → vocab
#
# The vocabulary belongs to the checkpoint: it decides which character is
# which token, and a mismatched pair tokenises the text into something the
# weights never saw during training.
F5_DIR = Path(os.environ.get("F5_MODEL_DIR", Path.home() / "f5ru"))
F5_CHECKPOINT = F5_DIR / "model_last_inference.safetensors"
F5_VOCAB = F5_DIR / "vocab.txt"

# Which architecture the checkpoint is, as F5-TTS names its configurations.
# The Russian model is a fine-tune of the v1 base and keeps its shape.
F5_ARCH = "F5TTS_v1_Base"

# Loading a model and generating one phrase. Generous: the first call on a
# cold machine downloads weights, and an audition that times out looks to the
# operator exactly like a model that cannot speak.
TIMEOUT = 1800


def interpreter(model: str) -> Path | None:
    """The python that can import this model, if this machine has one."""
    venv = VENVS.get(model)
    if venv is None:
        return None
    candidate = venv / "bin" / "python"
    return candidate if candidate.is_file() else None


def available(model: str) -> bool:
    """Whether this model can speak here: environment and weights both.

    The weights are checked, not just the environment, because the failure
    when they are missing is silent and expensive. F5-TTS falls back to its
    English base model rather than refusing, so a farm without the Russian
    checkpoint produces confident English-accented gibberish and looks like a
    bad model instead of a missing file.
    """
    if interpreter(model) is None:
        return False
    if model == "f5":
        return F5_CHECKPOINT.is_file() and F5_VOCAB.is_file()
    return True


def why_unavailable(model: str) -> str:
    """What is missing, in words worth showing the operator."""
    if interpreter(model) is None:
        return f"нет окружения для {model}: {VENVS.get(model)}"
    if model == "f5" and not (F5_CHECKPOINT.is_file() and F5_VOCAB.is_file()):
        return (f"нет русской модели в {F5_DIR} — без неё F5 говорит "
                f"по-английски. См. tts/README.md")
    return ""


def synthesise(model: str, *, text: str, sample: Path, target: Path,
               sample_rate: int = 24000, accent: bool = False) -> None:
    """One phrase, spoken by a model living in its own environment.

    The subprocess writes the wav itself and prints nothing that matters; what
    comes back on stdout is only used to explain a failure. Raises with the
    tail of stderr on anything that goes wrong, because the operator reading
    the panel needs the model's own complaint rather than a return code.
    """
    python = interpreter(model)
    if python is None:
        raise RuntimeError(
            f"модель {model!r} не установлена на ферме: нет {VENVS.get(model)}")
    if not sample.is_file():
        raise FileNotFoundError(f"нет образца голоса: {sample}")

    script = _SCRIPTS.get(model)
    if script is None:
        raise RuntimeError(f"неизвестная модель: {model!r}")

    # The reference text is what the sample says. Left empty on purpose: F5
    # transcribes the sample itself with Whisper, which is more reliable than
    # anything this side could supply, since nothing here knows what is on the
    # recording someone uploaded.
    payload = json.dumps({
        "text": text, "sample": str(sample), "target": str(target),
        "sample_rate": sample_rate, "ref_text": "",
        "arch": F5_ARCH,
        "checkpoint": str(F5_CHECKPOINT) if F5_CHECKPOINT.is_file() else "",
        "vocab": str(F5_VOCAB) if F5_VOCAB.is_file() else "",
        "accent": accent,
    })

    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as handle:
        handle.write(script)
        runner = Path(handle.name)
    try:
        result = subprocess.run([str(python), str(runner), payload],
                                capture_output=True, text=True, timeout=TIMEOUT)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"{model}: не уложилась в {TIMEOUT // 60} минут")
    finally:
        runner.unlink(missing_ok=True)

    if result.returncode != 0:
        tail = "\n".join((result.stderr or result.stdout).strip().splitlines()[-4:])
        raise RuntimeError(f"{model} не смогла произнести фразу:\n{tail}")
    if not target.is_file() or target.stat().st_size == 0:
        raise RuntimeError(f"{model} отработала, но файла нет: {target}")


# ── what runs inside each foreign environment ───────────────────────────
#
# Kept as source strings rather than as files in the package because they are
# executed by a different interpreter with a different set of imports, and a
# module that cannot be imported here should not sit in a directory that gets
# scanned. Each reads one json argument and writes one wav.

_F5 = '''
import json, sys
import numpy as np
import soundfile as sf
from f5_tts.api import F5TTS

job = json.loads(sys.argv[1])

# Which weights. F5TTS() with no arguments loads the English base model, and
# it will happily read Russian text with it — the output is fluent-sounding
# nonsense in an English accent, which is what "F5 speaks bad Russian" turned
# out to be. The Russian fine-tune is a separate checkpoint and has to be
# named, along with the vocabulary it was trained against; the two must match
# or the text is tokenised as something the weights never saw.
model = F5TTS(
    model=job["arch"],
    ckpt_file=job["checkpoint"],
    vocab_file=job["vocab"],
)

# Stress marks, if this checkpoint was trained with them. `+` before a stressed
# vowel is what the Russian fine-tune expects, and RUAccent decides where they
# go — a dictionary plus a model for the homographs, which are the only hard
# part ("зáмок" and "замóк" are the same letters).
#
# This is the same idea that had to be refused for XTTS, and it is worth being
# clear about why it works here and not there. XTTS has no grapheme-to-phoneme
# stage for Russian: a stress mark reaches its BPE tokenizer as an unknown
# character, splits the word into fragments it never saw, and makes the reading
# worse. This model was fine-tuned on text where every sentence carried the
# marks, so they are what it expects rather than what it trips over.
text = job["text"]
if job.get("accent"):
    try:
        from ruaccent import RUAccent
        accentizer = RUAccent()
        accentizer.load(omograph_model_size="turbo3.1", use_dictionary=True,
                        tiny_mode=False)
        text = accentizer.process_all(text)
    except Exception as exc:
        # Better unaccented than not at all: the model reads plain text too,
        # it simply guesses the stresses itself.
        print("ruaccent недоступен, читаю без ударений: %s" % exc,
              file=sys.stderr)

wav, rate, _ = model.infer(
    ref_file=job["sample"],
    ref_text=job["ref_text"],
    gen_text=text,
    remove_silence=True,
)
audio = np.asarray(wav, dtype=np.float32).reshape(-1)
peak = float(np.max(np.abs(audio))) if audio.size else 0.0
if peak > 1.0:
    audio = audio / peak
sf.write(job["target"], audio, int(rate))
'''

_FISH = '''
import json, sys, subprocess, tempfile, shutil
from pathlib import Path

# fish-speech ships a command line rather than a stable python api, and the
# command is the part its own documentation keeps working. Anything clever
# here would be guessing at internals that move between releases.
job = json.loads(sys.argv[1])
work = Path(tempfile.mkdtemp())
try:
    subprocess.run([sys.executable, "-m", "fish_speech.models.text2semantic.inference",
                    "--text", job["text"], "--prompt-text", job["ref_text"],
                    "--prompt-tokens", job["sample"],
                    "--output-dir", str(work)], check=True)
    made = sorted(work.glob("*.wav"))
    if not made:
        raise SystemExit("fish-speech не оставила wav")
    shutil.copy(made[0], job["target"])
finally:
    shutil.rmtree(work, ignore_errors=True)
'''

_SCRIPTS = {"f5": _F5, "fish": _FISH}
