"""
Wiring the voice to the telephone.

tts/ produces a phrase and a number. voip/ rings a handset and plays a file
into an earpiece. Neither imports the other, on purpose: one is game writing,
the other is a cable and a gateway. This is the piece that puts them together,
and it is the only file that knows both.

There is one telephone, on one extension, and every player walks to it. Which
extension is configuration (config.json, voip_esp.extension) rather than a
constant, because it is a fact about how the table is wired.

Two directions, matching the two cards:

    dialled     the player is given a number, walks over, and dials it. The
                disc reader reports the digits; redeem_dialled() below turns
                them into the phrase that plays. No gateway involved at all —
                the receiver is already off the hook, so the audio simply
                starts in the jack.

    rung        the game calls the player. This is the one thing the gateway
                is still needed for: ringing current on the line is what makes
                the bells sound, and nothing else in the rig can produce it.
                The receiver coming up is reported by the ESP, and that is
                what starts the phrase.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import tts
from app import voip_service
from tts.sessions import Ticket

ROOT = Path(__file__).resolve().parent.parent

# How long the bells ring for an incoming call. Long enough to cross a room
# and pick up; the arming window in voip_service outlives it, so a receiver
# lifted after the ringing stops still gets its phrase.
RING_SECONDS = 30


def extension() -> str:
    """The handset every player uses."""
    try:
        config = json.loads((ROOT / "config.json").read_text())
        value = str(config.get("voip_esp", {}).get("extension", "")).strip()
        return value or "105"
    except (OSError, ValueError):
        return "105"


# ── the burner phone: hand out a number ─────────────────────────────────

def issue_number(*, player_id: str, player_number: int, round_id: int,
                 position: int, total: int, shell: str,
                 silent: bool = False) -> Ticket:
    """Synthesise the informant's phrase and reserve a number for it.

    Returns the ticket, whose .number is what the television shows. Nothing is
    played and nothing is dialled yet: the player has to walk to the telephone
    and turn the disc, which is the whole point of the card.
    """
    if silent:
        return tts.issue_burner_silent(player_id=player_id,
                                       player_number=player_number,
                                       round_id=round_id)
    return tts.issue_burner(player_id=player_id, player_number=player_number,
                            round_id=round_id, position=position, total=total,
                            shell=shell)


def redeem_dialled(ext: str, number: str) -> Optional[dict]:
    """A number arriving from the disc reader.

    Installed into voip_service as the game's number handler, so it is asked
    about every dialled number before Asterisk's own slots are consulted.
    Everything about a game number is decided here, in the game's process:
    Asterisk never knew these numbers existed.

    Returns None for a number the game did not issue — that is how the
    telephony side is told to carry on down its old path, where the operator's
    test slots still live.

    The caller is standing there with the receiver at their ear, so every
    outcome is audible. A number that was issued and is now spent still plays
    something — the exchange refusing it — because silence down a telephone is
    indistinguishable from a dead line.
    """
    ticket = tts.redeem(number)
    if ticket is None:
        if not tts.registry.known(number):
            # Never ours. Could be one of the operator's test numbers.
            return None
        speech = tts.refusal_audio(number)
        voip_service.play_generated(
            ext, name=f"refuse_{number}", path=speech.path,
            detail=f"набран {number}: номер уже использован", ringback=False)
        return {"ok": False, "number": number, "reason": "spent"}

    voip_service.play_generated(
        ext, name=ticket.audio.stem, path=ticket.audio,
        detail=f"набран {number}: подсказка для #{ticket.player_number}",
        ringback=True)
    return {"ok": True, "number": number, "player_id": ticket.player_id,
            "player_number": ticket.player_number, "kind": ticket.kind,
            "text": ticket.text}


def install() -> None:
    """Let the telephony side ask this module about dialled numbers.

    Called once at startup, after voip_service is running.
    """
    voip_service.set_game_number_handler(redeem_dialled)


# ── the magnifying glass: call the player ───────────────────────────────

def ring_player(*, player_id: str, player_number: int, round_id: int,
                shell: str, ext: Optional[str] = None) -> dict:
    """Ring the handset and arm the chambered-round phrase against it.

    The gateway rings; the ESP reports the receiver coming up; voip_service
    plays what was armed. This returns as soon as the call is placed — the
    ringing takes half a minute and the request that played the card must not
    wait it out.
    """
    ext = ext or extension()
    text, speech = tts.prepare_magnifier(player_id=player_id,
                                         player_number=player_number,
                                         round_id=round_id, shell=shell)
    voip_service.call_generated(ext, name=speech.path.stem, path=speech.path,
                                ring=RING_SECONDS)
    return {"ok": True, "extension": ext, "text": text}


# ── housekeeping ────────────────────────────────────────────────────────

def end_round(round_id: Optional[int] = None) -> int:
    """Kill the numbers a finished round issued. None kills every one of them."""
    return tts.clear_round(round_id)


def status() -> dict:
    """Voice readiness and every number currently dialable."""
    state = tts.status()
    state["extension"] = extension()
    return state
