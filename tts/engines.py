"""The ways a phrase can be spoken, so two of them can be compared.

A cloned voice is judged by ear, and the ear needs something to compare
against. Until now there was one way to synthesise — XTTS with one fixed set
of sampling parameters — so "is this good?" had no second term, and a setting
that made every phrase slightly worse was invisible.

An engine here is a named way of turning text into audio: which model, and how
it is asked. The panel offers them as a list, an audition can run several at
once, and the results sit side by side under the same voice with the engine
named on each. That is the whole feature — the comparison is the point, not
any particular engine winning.

Two kinds live in the same list on purpose.

    XTTS presets    the same model with different sampling parameters. Cheap:
                    no extra weights, no extra dependencies, and the thing
                    most likely to be wrong is in here rather than in the
                    choice of model.

    other models    a different architecture entirely. Better ceiling, but
                    each one is gigabytes of weights and its own dependency
                    tree, so they are declared here and only usable on a farm
                    where someone installed them. `available()` reports which
                    is which rather than failing at synthesis time.

Why the parameters below are what they are is measured rather than guessed.
Generating the corpus's longest line at repetition_penalty 5.0 gives 9.7
seconds of audio; the same line at 2.5 gives 12.1. The model was not being
made more careful by the higher setting, it was being made to hurry, and the
clipped delivery that prompted all this is what hurrying sounds like. See
tts/README.md.
"""

from __future__ import annotations

from dataclasses import dataclass, field


# XTTS's own defaults, for reference: temperature 0.75, repetition_penalty
# 10.0, top_k 50, top_p 0.85, length_penalty 1.0. The repetition penalty is
# inherited from Tortoise, where it made sense for free text; speech is made
# of repeated acoustic tokens (held vowels, pauses, the deliberate repeat in
# every one of our lines) and a heavy penalty punishes exactly those.
@dataclass(frozen=True)
class Engine:
    """One named way of speaking, and what a human needs to choose it."""

    key: str                    # stable id: goes in filenames and JSON
    label: str                  # what the panel shows
    note: str                   # one line on what makes this one different
    model: str = "xtts"         # which backend synthesises it
    sampling: dict = field(default_factory=dict)   # backend-specific knobs
    cond_chunk: int = 30        # seconds per conditioning chunk (XTTS only)


# The reference sample is thirty seconds, and how it is divided changes the
# result. XTTS averages the latents of each chunk; one thirty-second chunk is
# a single average over everything, while several short ones give the model a
# steadier read on the speaker. The library's own default chunk is four.
COND_SECONDS = 30

ENGINES: tuple[Engine, ...] = (
    Engine(
        key="xtts_current",
        label="XTTS — как сейчас",
        note="то, чем озвучены имеющиеся голоса. Точка отсчёта для сравнения",
        sampling={"temperature": 0.7, "repetition_penalty": 5.0, "top_k": 50,
                  "top_p": 0.85, "length_penalty": 1.0,
                  "enable_text_splitting": False},
        cond_chunk=30,
    ),
    Engine(
        key="xtts_calm",
        label="XTTS — без спешки",
        note="штраф за повтор 2.5 вместо 5.0: та же фраза звучит на четверть "
             "дольше, потому что голос перестаёт частить и глотать окончания",
        sampling={"temperature": 0.7, "repetition_penalty": 2.5, "top_k": 50,
                  "top_p": 0.85, "length_penalty": 1.0,
                  "enable_text_splitting": False},
        cond_chunk=30,
    ),
    Engine(
        key="xtts_calm_chunk4",
        label="XTTS — без спешки, дробный образец",
        note="то же плюс образец режется по 4 с вместо одного куска на 30: "
             "модель усредняет несколько замеров голоса, а не один",
        sampling={"temperature": 0.7, "repetition_penalty": 2.5, "top_k": 50,
                  "top_p": 0.85, "length_penalty": 1.0,
                  "enable_text_splitting": False},
        cond_chunk=4,
    ),
    Engine(
        key="xtts_wide",
        label="XTTS — живее",
        note="без спешки, дробный образец и шире выборка (top_p 0.9, "
             "температура 0.75): интонация разнообразнее, но и разброс больше",
        sampling={"temperature": 0.75, "repetition_penalty": 2.5, "top_k": 50,
                  "top_p": 0.90, "length_penalty": 1.0,
                  "enable_text_splitting": False},
        cond_chunk=4,
    ),
)

# The engine used when nobody chose one: what the voices on disk were made
# with, so an unattended run keeps producing what the table already has.
DEFAULT = "xtts_current"


def get(key: str) -> Engine:
    """One engine by key, or the default if the key is unknown.

    Falling back rather than raising is deliberate: an engine key reaches here
    from a browser and from json files written by older versions, and refusing
    to speak because a preset was renamed would take the telephone down over a
    cosmetic change.
    """
    for engine in ENGINES:
        if engine.key == key:
            return engine
    for engine in ENGINES:
        if engine.key == DEFAULT:
            return engine
    return ENGINES[0]


def known(key: str) -> bool:
    return any(engine.key == key for engine in ENGINES)


def catalogue() -> list[dict]:
    """Every engine, as the panel needs to show it.

    Includes whether it can actually run here. A model whose weights are not
    installed is still listed — the operator should see that the option exists
    and what it would take — but marked so the panel can grey it out instead of
    letting someone start a job that fails a minute later.
    """
    out = []
    for engine in ENGINES:
        ready, why = _usable(engine)
        out.append({"key": engine.key, "label": engine.label,
                    "note": engine.note, "model": engine.model,
                    "ready": ready, "why": why})
    return out


def _usable(engine: Engine) -> tuple[bool, str]:
    """Whether this engine can synthesise on this machine, and why not.

    Only the backend is checked, not the weights: XTTS downloads its own on
    first use, and a model that is installed but unfetched is still a working
    choice — it just makes the first phrase slow.
    """
    if engine.model == "xtts":
        return _importable("TTS"), "не установлен coqui-tts"
    if engine.model == "f5":
        return _importable("f5_tts"), "не установлен f5-tts"
    if engine.model == "fish":
        return _importable("fish_speech"), "не установлен fish-speech"
    return False, f"неизвестный движок: {engine.model}"


def _importable(module: str) -> bool:
    import importlib.util

    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        return False
