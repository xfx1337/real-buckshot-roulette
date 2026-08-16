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
from typing import Callable, Optional

import tts
from app import memes, test_mode, voip_service
from tts.sessions import Ticket

ROOT = Path(__file__).resolve().parent.parent

# How long the bells ring for an incoming call. Long enough to cross a room
# and pick up; the arming window in voip_service outlives it, so a receiver
# lifted after the ringing stops still gets its phrase.
RING_SECONDS = 30

# Told when a number is dialled, whatever came of it. Set by the server, which
# uses it to take the dialling instruction off the player's screen: the guide
# is there to get a number dialled, and once it has been the screen should stop
# telling anyone to dial it.
#
# Called from the dial reader's thread, so whatever is installed here has to be
# safe from one. Kept as a hook rather than an import because this module is
# the seam between tts/ and voip/, and neither of them should learn what a
# television is.
_on_dialled: Optional[Callable[[str, bool], None]] = None


def set_dial_listener(listener: Optional[Callable[[str, bool], None]]) -> None:
    """Install what to tell when a number is dialled. (number, redeemed)."""
    global _on_dialled
    _on_dialled = listener


def set_hangup_listener(listener: Optional[Callable[[str], None]]) -> None:
    """Install what to tell when the receiver goes back on its cradle.

    The magnifying glass rings the handset and puts "pick up the telephone" on
    the player's screen. Nothing else takes it off again: the ringing stops
    when they answer, but the screen has no way of knowing they did, so the
    line sat there for the whole thirty seconds over somebody who had already
    heard the phrase and hung up.

    The cradle is the one thing that knows. voip_service reports it, and only
    for a receiver that was genuinely lifted first — ringing current alone can
    twitch the hook switch, and a screen cleared by that would vanish just
    before the player reached the table.
    """
    voip_service.set_on_hook_listener(listener)


def _announce(number: str, redeemed: bool) -> None:
    """Tell the listener, never at the caller's expense.

    Somebody is holding a receiver waiting for a voice. A screen that fails to
    update is not a reason for them to hear nothing, so this swallows whatever
    the listener does.
    """
    if _on_dialled is None:
        return
    try:
        _on_dialled(number, redeemed)
    except Exception as exc:                                    # noqa: BLE001
        print(f"[tts] слушатель набора отказал на {number}: {exc}")


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
                 game_mode: str = "multiplayer",
                 silent: bool = False) -> Ticket:
    """Synthesise the informant's phrase and reserve a number for it.

    Returns the ticket, whose .number is what the television shows. Nothing is
    played and nothing is dialled yet: the player has to walk to the telephone
    and turn the disc, which is the whole point of the card.

    game_mode decides who answers: one-on-one is always the same antagonist,
    multiplayer is whoever the shuffle picks. See tts.pick_voice().
    """
    voice = tts.pick_voice(game_mode)
    try:
        if silent:
            return tts.issue_burner_silent(player_id=player_id,
                                           player_number=player_number,
                                           round_id=round_id, voice=voice)
        return tts.issue_burner(player_id=player_id,
                                player_number=player_number,
                                round_id=round_id, position=position,
                                total=total, shell=shell, voice=voice)
    except Exception as exc:                                    # noqa: BLE001
        # На репетиции без железа голоса на диске может не быть вовсе: они
        # приезжают с машины с GPU, и ждать их, чтобы проверить порядок
        # раундов, незачем. Номер всё равно выдаётся — карточка доходит до
        # экрана, игрок доходит до аппарата, сценарий доходит до конца.
        if not test_mode.mocking():
            raise
        return _silent_ticket(player_id=player_id, player_number=player_number,
                              round_id=round_id, position=position,
                              total=total, shell=shell, silent=silent,
                              voice=voice, reason=str(exc))


def _silent_ticket(*, player_id: str, player_number: int, round_id: int,
                   position: int, total: int, shell: str, silent: bool,
                   voice: str, reason: str) -> Ticket:
    """Номер без озвучки — для прогона, где синтезировать нечем.

    Билет настоящий и живёт в том же реестре, что и все остальные: его выдают,
    показывают на экране, набирают на диске и гасят по концу раунда. Отличается
    он одним — за ним нет файла, и заглушка телефонии, встретив его, скажет об
    этом в журнал вместо того, чтобы играть.
    """
    text = (tts.phrases.burner_silent() if silent
            else tts.phrases.burner(position=position, total=total,
                                    shell=shell))
    test_mode.note("голос", f"не синтезировано ({reason}); "
                            f"номер выдан беззвучно: {text}")
    return tts.registry.issue(text=text, audio=_SILENT_AUDIO,
                              player_id=player_id, player_number=player_number,
                              kind="burner", round_id=round_id, voice=voice)


# Путь, которого нет, — и это его работа. Билет обязан нести Path, а заглушка
# телефонии файл не открывает: она пишет строку в журнал. Настоящая телефония
# такой билет не увидит, потому что беззвучные выдаются только в mock.
_SILENT_AUDIO = ROOT / "tts" / "cache" / "__untts__.wav"


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
    # Мем-номера первыми: они живут в своём диапазоне (см. app/memes.py) и с
    # игровыми не пересекаются, так что порядок проверок ни на что не влияет,
    # кроме одного — мем не должен проходить через реестр билетов, где его нет
    # и где он получил бы отказ как чужой номер.
    if memes.known(number):
        pending = memes.redeem(number)
        if pending is not None:
            play_meme(ext, pending.meme)
            _announce(number, True)
            return {"ok": True, "number": number, "kind": "meme",
                    "meme": pending.meme.title}
        # Наш номер, но уже погашенный или истёкший. Отказ звучит тот же, что
        # у потраченного игрового: звонящий держит трубку, и молчание в ней
        # неотличимо от мёртвой линии.
        _announce(number, False)
        return None

    ticket = tts.redeem(number)
    if ticket is None:
        if not tts.registry.known(number):
            # Never ours. Could be one of the operator's test numbers.
            return None
        try:
            refusal = tts.refusal_audio(number).path
        except Exception as exc:                                # noqa: BLE001
            if not test_mode.mocking():
                raise
            test_mode.note("голос", f"отказ по {number} не синтезирован ({exc})")
            refusal = _SILENT_AUDIO
        test_mode.telephony().play_generated(
            ext, name=f"refuse_{number}", path=refusal,
            detail=f"набран {number}: номер уже использован", ringback=False)
        _announce(number, False)
        return {"ok": False, "number": number, "reason": "spent"}

    # Имя с голосом: файл называется по хэшу текста, а хэш от голоса не
    # зависит — одна и та же реплика двух информаторов дала бы в журнале
    # одну строку, и оператор не увидел бы, кто из них говорил.
    voice = ticket.voice or "?"
    test_mode.telephony().play_generated(
        ext, name=f"{voice}_{ticket.audio.stem}", path=ticket.audio,
        detail=f"набран {number}: подсказка для #{ticket.player_number} "
               f"голосом {voice}",
        ringback=True)
    _announce(number, True)
    return {"ok": True, "number": number, "player_id": ticket.player_id,
            "player_number": ticket.player_number, "kind": ticket.kind,
            "text": ticket.text, "voice": ticket.voice}


def install() -> None:
    """Let the telephony side ask this module about dialled numbers.

    Called once at startup, after voip_service is running.
    """
    voip_service.set_game_number_handler(redeem_dialled)
    # До первого выданного билета: мем-номера лежат внутри диапазона, из
    # которого реестр раздаёт игровые, и пересёкшийся номер играл бы мем
    # вместо подсказки про патрон. См. memes.reserve_numbers().
    memes.reserve_numbers()


# ── мемы: звонок, который игра не планировала ───────────────────────────
#
# Всё выше — телефон по делу: номер за карточку, звонок за лупу, в обоих
# случаях реплика синтезируется под конкретный патрон. Ниже — противоположное:
# готовый файл с диска, к раскладу отношения не имеющий, в случайный момент.
#
# Путей два, и это те же два, что и у игровых, потому что тракт один. Отличие
# только в том, откуда берётся звук: не из tts, а из app/memes.py.

def ring_meme(meme, *, ext: Optional[str] = None) -> dict:
    """Позвонить на трубку и приготовить мем к ответу.

    Гудков нет намеренно. Их даёт play_generated тому, кто набрал номер сам, —
    там они означают устанавливающееся соединение. Здесь соединение установил
    тот, кто снял трубку в ответ на звонок: гудок в его ухе означал бы, что
    звонит он, а звонили ему.
    """
    ext = ext or extension()
    test_mode.telephony().call_generated(ext, name=f"meme_{meme.path.stem}",
                                         path=meme.path, ring=RING_SECONDS)
    return {"ok": True, "extension": ext, "meme": meme.title}


def play_meme(ext: str, meme) -> dict:
    """Проиграть мем в уже поднятую трубку — за набранный номер.

    С гудками, в отличие от входящего: игрок набрал номер и держит трубку, а
    набор без КПВ звучит как номер, который не соединился.
    """
    test_mode.telephony().play_generated(
        ext, name=f"meme_{meme.path.stem}", path=meme.path,
        detail=f"мем: {meme.title}", ringback=True)
    return {"ok": True, "extension": ext, "meme": meme.title}


# ── the magnifying glass: call the player ───────────────────────────────

def ring_player(*, player_id: str, player_number: int, round_id: int,
                shell: str, game_mode: str = "multiplayer",
                ext: Optional[str] = None) -> dict:
    """Ring the handset and arm the chambered-round phrase against it.

    The gateway rings; the ESP reports the receiver coming up; voip_service
    plays what was armed. This returns as soon as the call is placed — the
    ringing takes half a minute and the request that played the card must not
    wait it out.
    """
    ext = ext or extension()
    voice = tts.pick_voice(game_mode)
    try:
        text, speech = tts.prepare_magnifier(player_id=player_id,
                                             player_number=player_number,
                                             round_id=round_id, shell=shell,
                                             voice=voice)
        path, spoken = speech.path, speech.voice
    except Exception as exc:                                    # noqa: BLE001
        # Как и у номера выше: на репетиции без голосов звонок всё равно
        # должен «пройти», иначе карточка лупы обрывается на пустом месте.
        if not test_mode.mocking():
            raise
        text = tts.phrases.magnifier(shell)
        test_mode.note("голос", f"реплика лупы не синтезирована ({exc})")
        path, spoken = _SILENT_AUDIO, voice
    test_mode.telephony().call_generated(ext, name=path.stem, path=path,
                                         ring=RING_SECONDS)
    return {"ok": True, "extension": ext, "text": text, "voice": spoken}


# ── housekeeping ────────────────────────────────────────────────────────

def end_round(round_id: Optional[int] = None) -> int:
    """Kill the numbers a finished round issued. None kills every one of them."""
    return tts.clear_round(round_id)


def status() -> dict:
    """Voice readiness and every number currently dialable."""
    state = tts.status()
    state["extension"] = extension()
    return state
