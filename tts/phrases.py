"""
What the voice on the other end of the line actually says.

Kept apart from the engine because these are game writing, not machinery. The
engine turns any string into audio; this file decides which strings exist, and
it is the file to edit when the voice should say something different.

Two things shape every line here.

The first is that it is heard once, through a telephone earpiece, by someone
who is nervous. There is no rewind. So each phrase says the useful part twice
in different words — "третий патрон боевой... повторяю, третий боевой" — and
nothing else competes with it.

The second is that a synthesised voice reads digits badly. "3" is read as a
bare numeral and lands as noise; "третий" is a word and survives a bad line.
So numbers are spelled into words before they are ever spoken.
"""

from __future__ import annotations

import random

# Numbers as the voice should say them. The magazine never holds more than a
# handful of shells, and a dialled number is three digits, so this is all the
# counting the game ever needs.
ORDINALS = {
    1: "первый", 2: "второй", 3: "третий", 4: "четвёртый", 5: "пятый",
    6: "шестой", 7: "седьмой", 8: "восьмой", 9: "девятый", 10: "десятый",
}

# The same counts as a quantity of shells, agreeing with the noun the way the
# voice has to say it out loud: "три патрона", "пять патронов".
COUNTS = {
    1: "один патрон", 2: "два патрона", 3: "три патрона", 4: "четыре патрона",
    5: "пять патронов", 6: "шесть патронов", 7: "семь патронов",
    8: "восемь патронов", 9: "девять патронов", 10: "десять патронов",
}

DIGITS = {
    "0": "ноль", "1": "один", "2": "два", "3": "три", "4": "четыре",
    "5": "пять", "6": "шесть", "7": "семь", "8": "восемь", "9": "девять",
}

# How a shell is named out loud. Matches the words the dealer's panel uses, so
# a player who hears one and later sees the other is not comparing vocabularies.
SHELLS = {
    "live": "боевой",
    "blank": "холостой",
    "silver": "серебряный",
}

# How the informant opens and signs off. A single anonymous operator, never
# named: more unsettling than any amount of machinery for varying who calls.
#
# The punctuation here is not style. A full stop tells the synthesiser to end
# the intonation contour — drop the pitch, pause, start the next phrase from
# nothing — and these lines used to be built from six short sentences in a row.
# The result was a voice that reset six times in fifteen seconds, which is
# heard as intonation lurching from word to word rather than as someone
# speaking.
#
# So the pauses that carry meaning are kept and the rest are demoted. A comma
# or a dash holds the contour open across the break; a full stop closes it. The
# rule of thumb: one stop where the thought genuinely lands, commas everywhere
# the voice would only draw breath.
OPENERS = (
    "Слушай внимательно, повторять не буду —",
    "У меня мало времени, запоминай —",
    "Тихо, слушай —",
)

CLOSERS = (
    "Больше не звони.",
    "Забудь этот номер.",
    "Всё, клади трубку.",
)


def spell_number(number: str) -> str:
    """A dialled number as separate spoken digits.

    "четыреста двадцать семь" is one number heard once and gone; "четыре, два,
    семь" is three tokens a player can hold in their head long enough to turn
    the dial three times. The comma matters — it is the pause the voice takes
    between digits.
    """
    return ", ".join(DIGITS.get(character, character) for character in str(number))


def ordinal(index: int) -> str:
    """A position in the magazine as a word."""
    return ORDINALS.get(index, str(index))


def count(total: int) -> str:
    """A number of shells, with the noun already agreeing."""
    return COUNTS.get(total, f"{total} патронов")


def shell_word(shell: str) -> str:
    return SHELLS.get(shell, shell)


def _wrap(body: str) -> str:
    """Put one line of game information between an opening and a sign-off.

    The opener ends on a dash and the body starts lowercase, so the two are one
    sentence and the voice carries its contour across the join instead of
    landing and restarting. The sign-off does begin afresh: by then the hint
    has been delivered and a real pause before "больше не звони" is the pause
    the writing wants.
    """
    return f"{random.choice(OPENERS)} {body} {random.choice(CLOSERS)}"


# ── the burner phone: a shell somewhere in the magazine ──────────────────

def _burner_body(position: int, total: int, shell: str) -> str:
    """The hint itself, without an opener or a sign-off.

    Two sentences, not four. The count and the shell belong to one thought and
    are joined by a comma; the repetition is its own sentence because it is a
    genuine restart — the voice saying the important part again — and that is
    the one place a full stop earns its pause. See OPENERS for why this is
    counted so carefully.

    Separate from burner() because tts/corpus.py has to enumerate this line
    under every opener and closer, and it must get the words from here rather
    than restating them.
    """
    word = shell_word(shell)
    place = ordinal(position)
    return (f"в стволе {count(total)}, {place} патрон — {word}. "
            f"Повторяю: {place} — {word}.")


def burner(position: int, total: int, shell: str) -> str:
    """The phone's own hint: one shell, named by its place in the magazine."""
    return _wrap(_burner_body(position, total, shell))


def burner_silent() -> str:
    """What the phone says when the magazine is too short to give anything away.

    The game already had this case; it was a line of text on the dealer's
    screen. Out loud it has to still sound like someone on the other end,
    because silence down a telephone reads as a fault in the line rather than
    as a refusal.
    """
    return ("Слушай — патронов слишком мало, тут нечего продавать. "
            "Не звони по этому номеру больше.")


# ── the magnifying glass: the shell in the chamber ───────────────────────

def magnifier(shell: str) -> str:
    """The chambered round, which is what the glass shows.

    Arrives as an incoming call rather than something the player dials, so it
    opens differently: the voice called them, and it says so.
    """
    return _wrap(_magnifier_body(shell))


def _magnifier_body(shell: str) -> str:
    """The chambered round, without an opener or a sign-off.

    Same reason as _burner_body: corpus.py enumerates this under every wrapper
    and must not hold its own copy of the words.
    """
    word = shell_word(shell)
    return (f"тот, что сейчас в стволе — {word}. "
            f"Ещё раз: в стволе {word}.")


# ── what the television shows ────────────────────────────────────────────

def dial_instruction(number: str) -> str:
    """The line on the screen telling a player which number to dial."""
    return f"НАБЕРИ НОМЕР {number}"


def incoming_instruction() -> str:
    """The line on the screen when the telephone is ringing for them."""
    return "ВХОДЯЩИЙ ВЫЗОВ — СНИМИ ТРУБКУ"


# ── failures the player hears rather than reads ──────────────────────────

def wrong_number() -> str:
    """A number that is not the one they were given, or one already used.

    Said in the voice of the exchange, not of the informant: the player has to
    be able to tell "you dialled wrong" from "the informant had nothing", and
    the difference has to be audible, since neither reaches them as text.
    """
    return "Этот номер не обслуживается. Проверьте номер и наберите снова."


def expired() -> str:
    """A number dialled after its round ended."""
    return "Абонент недоступен. Соединение разорвано."
