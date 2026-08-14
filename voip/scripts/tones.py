"""The tones a caller hears before the music starts.

Dialling from the handset is answered by this machine, not by the exchange:
the dial is read by the ESP and the sound leaves through the mini jack, so
nothing in the path is a telephone line and none of the tones a line would
carry exist. They are generated here instead, as ordinary WAV files that the
same player plays through the same jack as the music.

Two of them, both to the Soviet/Russian standard, which is a single 425 Hz
tone switched at different rates:

  dial      continuous, heard when the receiver is lifted and the exchange
            is waiting for digits
  ringback  one second on, four off — КПВ, the "your call is ringing at the
            other end" tone
  busy      0.07 on, 0.07 off — the "engaged, or no such number" signal.
            Five times the rate of the СИП standard, on purpose: fast enough
            to be heard as a buzz rather than a beeping, which no part of a
            working call sounds like. The caller knows it failed without
            being told anything

They are written once into var/ and reused. Not into sounds/: everything
there is offered to the operator as a sound they can assign to a number, and
a ringback tone in that list is noise. These are part of the call, not
programme material.

Generated rather than shipped as files because they are exactly specified —
a frequency, a cadence, and a level — and a generator states the specification
in the only place it can be checked against.
"""

from __future__ import annotations

import math
import struct
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Alongside the logs rather than in sounds/, so the operator's library stays
# what the operator put there.
TONE_DIR = ROOT / "var" / "tones"

# The Russian call-progress frequency. One tone does all of them; only the
# cadence differs.
FREQUENCY = 425.0

# afplay follows the system output device and resamples as needed, so the
# rate only has to be one every encoder agrees on.
SAMPLE_RATE = 8000

# Below full scale on purpose. These play into an earpiece held against an
# ear, and a tone at full amplitude there is unpleasant; the music that
# follows is mastered well below peak, so matching it also keeps the two from
# arriving at noticeably different volumes.
AMPLITUDE = 0.25

# Ringback: КПВ is one second of tone and four of silence. The whole file is
# one cadence, played on a loop.
RINGBACK_ON, RINGBACK_OFF = 1.0, 4.0

# Busy and unobtainable: the same tone, chopped. One cadence per file, looped.
#
# Five times the rate of the СИП standard, which is 0.35 on and 0.35 off.
# That is deliberate and it is the operator's call: at 0.07 the interruption
# stops reading as a slow beeping and becomes a buzz, which is harder to
# mistake for the ringback and unmistakably means something went wrong.
#
# 0.07 s is 560 samples at 8 kHz and 29.75 periods of 425 Hz — not a whole
# number, so the tone is cut mid-cycle and the edge is a click. Audible, and
# here it is wanted: it is what gives the buzz its edge. A cadence this fast
# is a texture rather than a rhythm, and a clean gate would soften exactly
# the quality that makes it read as a fault.
BUSY_ON, BUSY_OFF = 0.07, 0.07

# How many cadences go into the file. See busy() for why this is not 1: at
# 0.14 s a cadence, restarting the player every cadence is audible. Seven
# makes the file just under a second.
BUSY_CADENCES = 7

# Dial tone is continuous. Written as a few seconds and looped rather than as
# one long file, which keeps it small and makes the loop seamless — the file
# is a whole number of periods, so the joint falls at a zero crossing.
#
# Three seconds of 425 Hz is 1275 whole periods, so the end of the file meets
# its own start at a zero crossing and the loop is inaudible. A length that
# did not divide evenly would click once a cycle, which on a continuous tone
# is the whole character of the sound.
DIAL_SECONDS = 3.0


def _samples(seconds: float, silent: bool = False) -> bytes:
    """One stretch of tone, or of silence, as 16-bit mono samples."""
    count = int(SAMPLE_RATE * seconds)
    if silent:
        return b"\0\0" * count

    peak = int(AMPLITUDE * 32767)
    step = 2.0 * math.pi * FREQUENCY / SAMPLE_RATE
    return b"".join(
        struct.pack("<h", int(peak * math.sin(step * i))) for i in range(count)
    )


def _write(path: Path, payload: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    # To a temporary name first, then moved into place. The player may be
    # asked for this file at any moment, and a half-written WAV plays as a
    # click or fails outright.
    staging = path.with_suffix(".part")
    with wave.open(str(staging), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(SAMPLE_RATE)
        out.writeframes(payload)
    staging.replace(path)
    return path


def _ensure(name: str, build) -> Path:
    """The named tone, generated if it is not already there."""
    path = TONE_DIR / f"{name}.wav"
    if path.is_file() and path.stat().st_size > 44:
        return path
    return _write(path, build())


def ringback() -> Path:
    """КПВ: one second of 425 Hz, four of silence. Meant to be looped."""
    return _ensure(
        "ringback",
        lambda: _samples(RINGBACK_ON) + _samples(RINGBACK_OFF, silent=True),
    )


def dial() -> Path:
    """The continuous tone that says the exchange is waiting for digits."""
    return _ensure("dial", lambda: _samples(DIAL_SECONDS))


def busy() -> Path:
    """0.07 on, 0.07 off. Engaged, or a number that does not exist.

    One tone for both because a Russian exchange uses one for both, and the
    distinction does not help the caller: either way the number they dialled
    is not going to answer and the thing to do is put the receiver down.
    """
    # Many cadences per file rather than one, unlike the ringback.
    #
    # Looping is done by starting the player again, which costs however long
    # it takes to spawn a process — tens of milliseconds. Against the
    # ringback's five-second cadence that is nothing. Against this one's 0.14
    # it is a gap of the same order as the cadence itself, heard as the buzz
    # stumbling several times a second.
    #
    # So the file carries its own repetition and the loop runs about once a
    # second instead of seven times.
    return _ensure(
        "busy",
        lambda: (_samples(BUSY_ON) + _samples(BUSY_OFF, silent=True))
        * BUSY_CADENCES,
    )


def build_all() -> list[Path]:
    """Generate every tone. Called at start-up so the first call is not the
    one that pays for the generation."""
    return [dial(), ringback(), busy()]


if __name__ == "__main__":
    for tone in build_all():
        seconds = tone.stat().st_size / (SAMPLE_RATE * 2)
        print(f"{tone}  {seconds:.1f}s")
