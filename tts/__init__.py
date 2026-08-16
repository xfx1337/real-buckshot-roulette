"""
The voice on the telephone.

Two of the game's cards stopped being screen text and became telephone calls.
This package is what makes that possible: it writes what the voice says,
synthesises it, and issues the numbers that connect a player to their own
phrase.

    burner phone      the television shows a number, the player dials it on
                      the rotary disc, and the informant tells them where one
                      shell sits in the magazine.

    magnifying glass  the telephone rings, the player lifts the receiver, and
                      the informant names the round in the chamber.

The informant speaks in a cloned voice: someone's recording, turned into a
speaker sample once, and from then on that is who answers the telephone. None
of that happens here or during a game — the vocabulary is finite, it is
generated ahead of time on a machine with a GPU (tts/pregenerate.py), and at
the table a phrase is a file already on disk.

Everything a caller outside this package needs is here. The pieces underneath
are separable on purpose — engine.py knows nothing about the game, phrases.py
knows nothing about audio, sessions.py knows nothing about either — but no
caller should have to assemble them.

Where this stops: it produces a file and a number. Ringing a telephone, and
playing into an earpiece, belong to voip/ and stay there.
"""

from __future__ import annotations

import random
from pathlib import Path
from typing import Optional

from tts import corpus, engine, engines, phrases, sessions
from tts.engine import Speech, TTSError
from tts.sessions import Ticket, registry

__all__ = [
    "TTSError", "Speech", "Ticket", "registry",
    "corpus", "engine", "phrases", "sessions",
    "issue_burner", "prepare_magnifier", "redeem", "refusal_audio",
    "status", "clear_round", "voices", "use_voice",
    "STORY_VOICE", "pick_voice",
]

# Who answers the telephone when there is only one player at the table.
#
# One-on-one is the story: the same antagonist across the whole night, so the
# voice has to be the same one every time or he is not a character, he is
# whoever the shuffle picked. Multiplayer has no such thread — every player is
# calling their own informant — and there the shuffle is the point.
STORY_VOICE = "mariarti"

# The modes that count as one-on-one. Named rather than inferred from the
# player count: a multiplayer game that happens to have one player left alive
# is still multiplayer, and its informant should still be a stranger.
STORY_MODES = frozenset({"solo", "story", "story_one_round"})


def _voice(text: str, voice: str | None = None) -> Speech:
    return engine.speak(text, voice=voice)


def installed_voices() -> list[str]:
    """Names of every voice with phrases on this disk, in a stable order.

    engine.voices() reports the whole record for the dealer's panel; this is
    only the names, which is all the picker below needs.
    """
    return [v["name"] for v in engine.voices()]


def pick_voice(game_mode: str) -> str:
    """Which cloned voice answers this call.

    One-on-one gets Moriarty, always. Multiplayer gets whoever the shuffle
    lands on, so the table cannot learn the informant by his voice.

    Falls back to the current voice when the wanted one has no cache directory
    here — the voices arrive by being copied from the GPU machine, and a table
    set up with only one of them should still be able to play a card. Returning
    a name that cannot speak would fail in engine.speak() with a message about
    a missing phrase, which is the wrong thing to tell the operator when the
    real fault is a voice that was never copied.
    """
    available = installed_voices()
    if not available:
        return engine.current_voice()

    if game_mode in STORY_MODES:
        return STORY_VOICE if STORY_VOICE in available else engine.current_voice()

    return random.choice(available)


# ── the burner phone: a number to dial ──────────────────────────────────

def issue_burner(*, player_id: str, player_number: int, round_id: int,
                 position: int, total: int, shell: str,
                 voice: str | None = None) -> Ticket:
    """Generate the informant's phrase and reserve a number that plays it.

    position/total/shell come from the game engine, which has already decided
    which shell is given away. This does not choose — it only says out loud
    what was chosen, so that the hint a player hears and the hint the dealer's
    panel shows can never disagree.

    voice is who says it, from pick_voice() above. None keeps whatever the
    dealer's panel last selected.
    """
    text = phrases.burner(position=position, total=total, shell=shell)
    speech = _voice(text, voice)
    return registry.issue(text=text, audio=speech.path, player_id=player_id,
                          player_number=player_number, kind="burner",
                          round_id=round_id, voice=speech.voice)


def issue_burner_silent(*, player_id: str, player_number: int,
                        round_id: int, voice: str | None = None) -> Ticket:
    """The same, for a magazine too short to give anything away.

    Still a real number the player dials. The card was spent, so they are owed
    the walk to the telephone and an answer at the end of it — being told
    there is nothing is an answer, and hearing it from the informant's own
    voice is the game working, not failing.
    """
    text = phrases.burner_silent()
    speech = _voice(text, voice)
    return registry.issue(text=text, audio=speech.path, player_id=player_id,
                          player_number=player_number, kind="burner",
                          round_id=round_id, voice=speech.voice)


# ── the magnifying glass: a call that comes to them ─────────────────────

def prepare_magnifier(*, player_id: str, player_number: int, round_id: int,
                      shell: str, voice: str | None = None) -> tuple[str, Speech]:
    """Produce what the incoming call will play.

    No number is issued: nobody dials this one. Returns the text and the
    generated file, which the caller hands to the telephony side to arm
    against the handset before the bells start.
    """
    text = phrases.magnifier(shell)
    return text, _voice(text, voice)


# ── a dialled number arriving from the disc reader ──────────────────────

def redeem(number: str) -> Optional[Ticket]:
    """Claim a number a player has just dialled, or None if it is not live."""
    return registry.redeem(number)


def refusal_audio(number: str) -> Speech:
    """What a caller hears for a number that plays nothing.

    A number that was issued and already spent gets the "unobtainable" line; a
    number that was never issued gets "no such number". The caller cannot see
    a screen while holding a receiver, so the difference has to be audible or
    it does not exist.

    Spoken by whoever the spent number was issued in the voice of, so a player
    who dials their own number twice hears the same person refuse them. A
    number nobody issued has no voice of its own and gets the current one.
    """
    spent = registry.voice_of(number)
    text = phrases.expired() if registry.known(number) else phrases.wrong_number()
    return _voice(text, spent)


# ── housekeeping and health ─────────────────────────────────────────────

def clear_round(round_id: Optional[int] = None) -> int:
    """Kill the numbers from a finished round. See sessions.Registry."""
    return registry.clear_round(round_id)


def status() -> dict:
    """Whether this machine can speak, and what is currently dialable."""
    return {"engines": engine.available(), "tickets": registry.live()}


# ── the cloned voice ────────────────────────────────────────────────────

def voices() -> list[dict]:
    """Every cloned voice installed here, and how complete each one is.

    A voice arrives as a directory of pre-generated phrases copied from the
    machine that has the GPU (see tts/pregenerate.py). This is how the dealer's
    panel finds out which ones are on this disk, and whether one of them was
    copied half-way.
    """
    return engine.voices()


def use_voice(name: str) -> None:
    """Speak as someone else from the next phrase onward."""
    engine.use_voice(name)
