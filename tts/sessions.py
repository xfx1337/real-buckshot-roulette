"""
The numbers a player is told to dial, and what each of them plays.

A number here is not a telephone extension and nothing in Asterisk knows about
it. The dial reader on the handset (voip/esp/src/main.cpp) sends three digits
over HTTP the moment a player finishes turning the disc, and the game decides
what those digits mean. That is the whole of the telephony involved: the
number is a token the game issued, and dialling it is how a player redeems it.

Which makes the range free. It is not carved out of a dialplan and it does not
have to avoid the test extensions, so any three digits work as long as the
first is not a zero — a leading zero on a rotary disc is ten pulses and the
slowest digit to dial, and starting every number with it would be a small
cruelty.

A number is:

    one-shot     redeemed once, then dead. Dialling it again gets the
                 unobtainable-number message, not the hint a second time,
                 because a hint that can be replayed is a hint the whole table
                 hears when someone dials it on speaker.

    short-lived  it dies with the round it was issued in. A number that
                 outlives its round would name a shell that has since been
                 fired.

    unique       never reused while another is live, so two players holding
                 numbers can never dial into each other's hint.
"""

from __future__ import annotations

import random
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# Three digits, never starting with a zero. 800 numbers, which is more than a
# night uses, so collisions are rare even before the uniqueness check below.
FIRST = 100
LAST = 999

# How long an unredeemed number stays alive, in seconds. Generous: the cost of
# being too long is a number nobody dials, which expires quietly; the cost of
# being too short is a player standing at the telephone with a number that has
# just died in their hand. A round rarely runs past this, and ending a round
# clears the numbers anyway (see clear_round).
LIFETIME = 600.0


@dataclass
class Ticket:
    """One issued number and the phrase behind it."""

    number: str
    text: str                # what the voice says, kept for the log
    audio: Path              # the generated file, ready to play
    player_id: str
    player_number: int       # the seat number, for log lines
    kind: str                # "burner" | "magnifier"
    round_id: int
    issued: float
    used: Optional[float] = field(default=None)

    @property
    def alive(self) -> bool:
        return self.used is None and (time.time() - self.issued) < LIFETIME

    @property
    def seconds_left(self) -> float:
        return max(0.0, LIFETIME - (time.time() - self.issued))

    def as_dict(self) -> dict:
        return {
            "number": self.number,
            "player_id": self.player_id,
            "player_number": self.player_number,
            "kind": self.kind,
            "round_id": self.round_id,
            "text": self.text,
            "issued": self.issued,
            "used": self.used,
            "seconds_left": round(self.seconds_left, 1),
            "alive": self.alive,
        }


class Registry:
    """Every number currently worth dialling.

    Safe from any thread: numbers are issued from the request that plays a
    card and redeemed from the request the dial reader makes, and those are
    different threads that can land in the same instant.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._tickets: dict[str, Ticket] = {}
        # Numbers already spent this round. Kept after redemption so a second
        # dial of the same number can be told apart from a number that was
        # never issued — the player hears the same refusal either way, but the
        # operator's log should not report a player's own number as unknown.
        self._spent: dict[str, Ticket] = {}

    # ── issuing ─────────────────────────────────────────────────────────

    def _free_number(self) -> str:
        """A number no live ticket is using."""
        with_us = set(self._tickets) | set(self._spent)
        # Bounded rather than looped forever: with 800 numbers and a handful
        # live, this succeeds on the first attempt essentially always, and a
        # game that somehow filled the range should say so rather than hang
        # the request that is holding a player at the telephone.
        for _ in range(200):
            candidate = str(random.randint(FIRST, LAST))
            if candidate not in with_us:
                return candidate
        raise RuntimeError("свободных номеров не осталось")

    def issue(self, *, text: str, audio: Path, player_id: str,
              player_number: int, kind: str, round_id: int) -> Ticket:
        """Reserve a number for one phrase and hand it back."""
        with self._lock:
            self._sweep()
            ticket = Ticket(number=self._free_number(), text=text, audio=audio,
                            player_id=player_id, player_number=player_number,
                            kind=kind, round_id=round_id, issued=time.time())
            self._tickets[ticket.number] = ticket
            return ticket

    # ── redeeming ───────────────────────────────────────────────────────

    def redeem(self, number: str) -> Optional[Ticket]:
        """Claim a dialled number, or None if it is not a live one.

        Claiming and marking used happen together under the lock: the dial
        reader can repeat a number when a request is slow, and two claims of
        the same number must not both come back holding a hint.
        """
        with self._lock:
            self._sweep()
            ticket = self._tickets.pop(number, None)
            if ticket is None:
                return None
            ticket.used = time.time()
            self._spent[number] = ticket
            return ticket

    def known(self, number: str) -> bool:
        """Whether this number was ever issued, live or spent.

        For the log only. The caller decides what the player hears, and both
        cases sound the same down the line.
        """
        with self._lock:
            return number in self._tickets or number in self._spent

    # ── housekeeping ────────────────────────────────────────────────────

    def _sweep(self) -> None:
        """Drop what has timed out. Called under the lock by everything above."""
        now = time.time()
        dead = [n for n, t in self._tickets.items()
                if (now - t.issued) >= LIFETIME]
        for number in dead:
            self._spent[number] = self._tickets.pop(number)

    def clear_round(self, round_id: Optional[int] = None) -> int:
        """Kill every number from a finished round. Returns how many died.

        Called when a round ends. Without it a number issued in one round
        would still play in the next, naming a shell out of a magazine that no
        longer exists — the single worst thing this system could do, because
        the player has no way of telling a stale hint from a true one.
        """
        with self._lock:
            if round_id is None:
                gone = len(self._tickets)
                self._tickets.clear()
                self._spent.clear()
                return gone
            dead = [n for n, t in self._tickets.items() if t.round_id == round_id]
            for number in dead:
                del self._tickets[number]
            for number in [n for n, t in self._spent.items()
                           if t.round_id == round_id]:
                del self._spent[number]
            return len(dead)

    def live(self) -> list[dict]:
        """Every number a player could still dial, for the dealer's panel."""
        with self._lock:
            self._sweep()
            return [t.as_dict() for t in
                    sorted(self._tickets.values(), key=lambda t: t.issued)]


# One registry for the process, like the audio player it feeds.
registry = Registry()
