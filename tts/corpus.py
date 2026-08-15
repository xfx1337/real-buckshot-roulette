"""Every phrase the game can ever say, enumerated.

The voice is not synthesised during a game any more. A cloned voice takes
seconds per phrase on the machine at the table, and a player stands at the
handset with the receiver already at their ear — there is nowhere to hide that
pause. So the whole vocabulary is generated ahead of time, somewhere with a
GPU, and the game only ever reads files off a disk.

That trade only works if the enumeration here is complete. A phrase the game
can produce but this file does not list is a phrase that reaches the player as
a fault in the line, so the rule for editing phrases.py is: if you add a line
the voice can say, add it here too.

Completeness is why the openers and closers are expanded rather than sampled.
phrases.py picks one of each at random, which multiplies every line by nine.
Nine times a thousand phrases is nothing on a GPU and it keeps the voice from
sounding the same all evening, so the perm is taken in full.

The magazine size ceiling is a fact about the game rather than about audio:
MAX_SHELLS is what the table actually deals, and enumerating past it would
generate a phrase no round can ask for.
"""

from __future__ import annotations

from dataclasses import dataclass

from tts import phrases

# The largest magazine a round deals. phrases.ORDINALS counts to ten, but the
# game never fills that far, and each unused size costs a slice of the perm.
MAX_SHELLS = 8

# Which shells can be named out loud. Taken from phrases rather than repeated,
# so a new shell type added there is enumerated here without an edit.
SHELL_KINDS = tuple(phrases.SHELLS)


@dataclass(frozen=True)
class Line:
    """One phrase to generate, and enough context to name it in a progress log."""

    text: str
    kind: str          # "burner" | "magnifier" | "service"
    detail: str        # human-readable, for the operator watching a warm-up


def _wrapped(body: str) -> list[str]:
    """One line of game information, under every opener and closer.

    Mirrors phrases._wrap, which composes the same three parts but picks its
    two ends at random. Whatever it can pick has to already exist on disk.
    """
    return [f"{opener} {body} {closer}"
            for opener in phrases.OPENERS
            for closer in phrases.CLOSERS]


# The wording of every line lives in phrases.py and is taken from there rather
# than restated here. It was restated once, and the two copies drifted the
# moment the punctuation was reworked for intonation: phrases.py said one thing,
# the enumeration generated another, and every phrase the game actually spoke
# was missing from the cache. Reaching into the private helpers below is the
# price of having exactly one copy of the words.

def _burner_body(position: int, total: int, shell: str) -> str:
    """The informant's hint, without its opener and closer."""
    return phrases._burner_body(position, total, shell)


def _magnifier_body(shell: str) -> str:
    """The chambered round, without its opener and closer."""
    return phrases._magnifier_body(shell)


def lines(max_shells: int = MAX_SHELLS) -> list[Line]:
    """Every phrase the game can utter, deduplicated, in a stable order.

    Stable because a warm-up is interrupted and resumed more often than it is
    run to completion in one sitting, and an operator watching "412 of 1002"
    should be able to leave and come back to the same count.
    """
    out: list[Line] = []
    seen: set[str] = set()

    def add(text: str, kind: str, detail: str) -> None:
        if text not in seen:
            seen.add(text)
            out.append(Line(text=text, kind=kind, detail=detail))

    # The burner phone: one shell named by its place in the magazine. Position
    # cannot exceed the magazine it sits in, so the pairs are triangular.
    for total in range(1, max_shells + 1):
        for position in range(1, total + 1):
            for shell in SHELL_KINDS:
                for text in _wrapped(_burner_body(position, total, shell)):
                    add(text, "burner", f"{position}/{total} {shell}")

    # A magazine too short to give anything away. One fixed line, no wrapper.
    add(phrases.burner_silent(), "burner", "нечего продавать")

    # The magnifying glass: the round in the chamber.
    for shell in SHELL_KINDS:
        for text in _wrapped(_magnifier_body(shell)):
            add(text, "magnifier", f"в стволе {shell}")

    # What a caller hears instead of a hint. Said by the exchange, not the
    # informant, so these carry no opener or closer.
    add(phrases.wrong_number(), "service", "номер не обслуживается")
    add(phrases.expired(), "service", "абонент недоступен")

    return out


def count(max_shells: int = MAX_SHELLS) -> int:
    """How many phrases a full warm-up generates."""
    return len(lines(max_shells))


if __name__ == "__main__":
    import sys

    limit = int(sys.argv[1]) if len(sys.argv) > 1 else MAX_SHELLS
    all_lines = lines(limit)
    by_kind: dict[str, int] = {}
    for line in all_lines:
        by_kind[line.kind] = by_kind.get(line.kind, 0) + 1
    print(f"магазин до {limit} патронов: {len(all_lines)} фраз")
    for kind, n in sorted(by_kind.items()):
        print(f"  {kind}: {n}")
