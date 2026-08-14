"""
Playing a sound into the handset's earpiece, through the mini jack.

The audio path here has nothing to do with SIP. The gateway's only job in a
call is to put ringing current on the line so the telephone's bells sound;
it never answers, so no RTP stream is ever established and Asterisk plays
into a channel that stays Ringing until it times out.

What actually carries the audio is a cable: the Mac's headphone output wired
into the handset's earpiece. Deciding when to start it is what the ESP is
for. It reads the hook switch directly, so the moment the receiver comes up
it says so over HTTP, and that event — not anything the gateway reports — is
what starts playback.

    off-hook from the ESP   ->  start()
    on-hook from the ESP    ->  stop()

One playback at a time, because there is one cable and one earpiece. Starting
a second sound stops the first rather than mixing them.

afplay is used rather than a library: it is part of macOS, it reads every
format ffmpeg wrote into sounds/, and it plays to whichever device is the
system default — which is the headphone jack whenever a plug is in it.
"""

from __future__ import annotations

import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# What plays the file. afplay ships with macOS and follows the default output
# device, so plugging into the jack is all the routing that is needed.
PLAYER = "/usr/bin/afplay"


class AudioError(RuntimeError):
    pass


@dataclass
class Playing:
    """What is coming out of the jack right now."""

    extension: str
    sound: str
    path: Path
    started: float
    loop: bool
    process: subprocess.Popen = field(repr=False)

    # Set while this playback is a ringing tone standing in for a sound that
    # has not started yet: (name, path, loop) of what follows. None once the
    # sound itself is playing, which is every playback that is not part of a
    # dialled call.
    pending: tuple | None = field(default=None, repr=False)

    # True for a call-progress tone — dial, ringback, busy — as opposed to
    # something out of the operator's library. What the caller is owed when
    # they dial differs between the two: a tone is the line talking and is
    # replaced without ceremony, a sound was asked for and is not.
    tone: bool = field(default=False, repr=False)

    @property
    def ringing(self) -> bool:
        """Whether what is audible is the tone rather than the sound."""
        return self.pending is not None

    @property
    def seconds(self) -> float:
        return time.time() - self.started

    def as_dict(self) -> dict:
        return {
            "extension": self.extension,
            "sound": self.sound,
            "loop": self.loop,
            "started": self.started,
            "seconds": round(self.seconds, 1),
            "ringing": self.ringing,
        }


class Player:
    """The one thing playing into the earpiece.

    Every method is safe to call from any thread: the web server's request
    threads start playback, and the watcher thread below ends it, so the two
    can land at the same moment.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._current: Playing | None = None
        # Called with (extension, sound, reason) when a sound ends on its own
        # rather than being stopped. The web app uses it to log the end of a
        # call and to release the handset.
        self.on_finish = None

    # ── state ───────────────────────────────────────────────────────────

    def current(self) -> dict | None:
        with self._lock:
            return self._current.as_dict() if self._current else None

    def is_playing(self, extension: str | None = None) -> bool:
        with self._lock:
            if self._current is None:
                return False
            return extension is None or self._current.extension == extension

    # ── starting and stopping ───────────────────────────────────────────

    def start(self, extension: str, sound_name: str, path: Path,
              loop: bool = False, tone: bool = False) -> Playing:
        """Begin playing a file into the earpiece.

        Anything already playing is stopped first. There is one cable, so a
        second sound cannot join the first — and silently refusing the new one
        would be worse: the operator asked for it, and the handset would keep
        playing something they had moved on from.
        """
        if not path.is_file():
            raise AudioError(f"нет файла для воспроизведения: {path}")

        self.stop(reason="replaced")

        try:
            process = subprocess.Popen(
                [PLAYER, str(path)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
            )
        except OSError as exc:
            raise AudioError(f"не удалось запустить воспроизведение: {exc}") from exc

        playing = Playing(extension=extension, sound=sound_name, path=path,
                          started=time.time(), loop=loop, process=process,
                          tone=tone)
        with self._lock:
            self._current = playing

        # Waiting on the process in a thread of its own: the caller is a web
        # request and must return at once, but something has to reap the
        # process and say when the sound ended, or a finished file leaves the
        # handset marked busy forever.
        threading.Thread(target=self._watch, args=(playing,),
                         name=f"audio-{extension}", daemon=True).start()
        return playing

    def _watch(self, playing: Playing) -> None:
        """Wait for one playback to end, and loop it if it was asked for."""
        while True:
            playing.process.wait()

            with self._lock:
                # Something else took the earpiece while this was playing, or
                # it was stopped: either way this playback is over and must
                # not restart, or a looping sound would come back from the
                # dead after the receiver went down.
                if self._current is not playing:
                    return
                if not playing.loop:
                    self._current = None

            if not playing.loop:
                if self.on_finish:
                    try:
                        self.on_finish(playing.extension, playing.sound, "finished")
                    except Exception:                          # noqa: BLE001
                        pass
                return

            # Looping: start the file again and keep watching the new process.
            try:
                process = subprocess.Popen(
                    [PLAYER, str(playing.path)],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    stdin=subprocess.DEVNULL,
                )
            except OSError:
                with self._lock:
                    if self._current is playing:
                        self._current = None
                if self.on_finish:
                    try:
                        self.on_finish(playing.extension, playing.sound, "failed")
                    except Exception:                          # noqa: BLE001
                        pass
                return
            playing.process = process

    def start_tone(self, extension: str, tone: Path, name: str,
                   seconds: float | None = None) -> Playing:
        """Play a call-progress tone on a loop, with nothing to follow it.

        The dial tone and the busy tone are not sounds anyone asked for —
        they are the line telling the caller what it is doing — so they end
        the way a real one does: when the receiver goes down, or when it is
        replaced by whatever the caller dialled.

        seconds caps it. The busy tone is capped because a receiver left off
        the hook would otherwise carry it until the process died; the dial
        tone is not, because an exchange holds it until you dial or hang up
        and there is nothing to be gained by being less patient than that.
        """
        playing = self.start(extension, name, tone, loop=True, tone=True)

        if seconds is None:
            return playing

        def expire() -> None:
            deadline = time.time() + seconds
            while time.time() < deadline:
                time.sleep(0.05)
                with self._lock:
                    if self._current is not playing:
                        return
            # reason="expired" rather than the default: nothing failed and
            # nobody hung up, the tone simply ran its course.
            self.stop(extension, reason="expired")

        threading.Thread(target=expire, name=f"tone-{extension}",
                         daemon=True).start()
        return playing

    def start_sequence(self, extension: str, tone: Path, seconds: float,
                       sound_name: str, path: Path, loop: bool = False,
                       on_answer=None) -> Playing:
        """Play a tone for a while, then the sound proper.

        What a caller expects after dialling is ringing, then the thing they
        dialled — so the ringback plays first and the music follows it. Both
        go through start(), so from every other angle this is one playback
        after another and nothing else has to know a sequence is running.

        The tone loops, because it is one cadence of a call-progress tone and
        the wait is longer than one cadence. It is replaced rather than
        stopped: start() takes the earpiece from whatever holds it, so the
        music simply arrives and the tone ends in the same instant, with no
        silence between them.

        Returns the tone's playback, which is what is audible when this
        returns. The caller is a web request and must not wait out the
        ringing.
        """
        ringing = self.start(extension, sound_name, tone, loop=True, tone=True)
        # Named for what will play, not for what is playing. This entry is
        # what the page and the call record read while the tone runs, and the
        # sound the caller asked for is the answer they want to that; the
        # ringing is a stage of getting there, reported separately as an
        # event.
        ringing.pending = (sound_name, path, loop)

        def follow() -> None:
            deadline = time.time() + seconds
            # Woken often rather than slept through, so a receiver put down
            # during the ringing ends the call then instead of playing the
            # music into a cradled handset seconds later.
            while time.time() < deadline:
                time.sleep(0.05)
                with self._lock:
                    if self._current is not ringing:
                        return          # stopped, or something else took over

            with self._lock:
                if self._current is not ringing:
                    return

            try:
                self.start(extension, sound_name, path, loop=loop)
                if on_answer:
                    # After the sound is up, so a failure to start it is
                    # never reported as an answer.
                    try:
                        on_answer(extension, sound_name)
                    except Exception:                          # noqa: BLE001
                        pass
            except AudioError:
                # The tone is still playing and would loop forever on a file
                # that cannot be opened. Silence says the call failed, which
                # is true, and on_finish releases the handset.
                self.stop(extension, reason="failed")

        threading.Thread(target=follow, name=f"ring-{extension}",
                         daemon=True).start()
        return ringing

    @staticmethod
    def _kill(playing: Playing) -> None:
        """End one playback's process. The caller has already taken it out of
        _current, so the watcher will not restart it."""
        try:
            playing.process.terminate()
            try:
                playing.process.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                playing.process.kill()
        except OSError:
            pass

    def stop_tone(self, extension: str, reason: str = "dialled") -> bool:
        """Silence a call-progress tone, and only that. False if what is
        playing is a sound, or nothing is.

        Used where a tone has been answered by the caller doing something —
        dialling the first digit — and the sound they may already be
        listening to must survive it.
        """
        # Claimed under the lock rather than checked and then stopped. The
        # sequence that follows a ringback swaps the tone for the sound from
        # a thread of its own, so between a check and a stop() the tone can
        # become the music — and stop() looks at the extension, not at what
        # is playing, so it would silence the sound this exists to protect.
        with self._lock:
            playing = self._current
            if playing is None or not playing.tone:
                return False
            if playing.extension != extension:
                return False
            self._current = None

        self._kill(playing)
        if self.on_finish:
            try:
                self.on_finish(playing.extension, playing.sound, reason)
            except Exception:                                  # noqa: BLE001
                pass
        return True

    def stop(self, extension: str | None = None, reason: str = "stopped") -> bool:
        """Silence the earpiece. False if nothing was playing.

        With an extension given, only that handset's sound is stopped: an
        on-hook from one telephone must not cut off a sound playing for
        another.
        """
        with self._lock:
            playing = self._current
            if playing is None:
                return False
            if extension is not None and playing.extension != extension:
                return False
            # Cleared before the process is killed, so the watcher sees the
            # playback is no longer current and does not restart a loop.
            self._current = None

        self._kill(playing)

        if reason != "replaced" and self.on_finish:
            try:
                self.on_finish(playing.extension, playing.sound, reason)
            except Exception:                                  # noqa: BLE001
                pass
        return True


# The single player, shared by everything in the process. One cable, one
# earpiece, one of these.
player = Player()


def output_device() -> str:
    """What the system is playing through, for the interface to show.

    The jack has to have a plug in it for any of this to be audible, and
    "nothing came out of the handset" looks identical whether the cable is
    unplugged or the file failed. Naming the device separates the two.
    """
    try:
        out = subprocess.run(
            ["system_profiler", "SPAudioDataType"],
            capture_output=True, text=True, timeout=10,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return "неизвестно"

    # The default output device is the one whose block carries the flag; the
    # device's own name is the last heading above it.
    name = ""
    for line in out.splitlines():
        stripped = line.strip()
        if stripped.endswith(":") and not stripped.startswith("Default"):
            candidate = stripped[:-1].strip()
            if candidate and candidate not in ("Audio", "Devices"):
                name = candidate
        if stripped.startswith("Default Output Device: Yes"):
            return name or "неизвестно"
    return "неизвестно"


if __name__ == "__main__":
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import sounds                                              # noqa: E402

    print(f"выход: {output_device()}\n")

    library = sounds.library()
    if not library:
        print(f"нет звуков в {sounds.SOURCE_DIR}")
        raise SystemExit(1)

    choice = sys.argv[1] if len(sys.argv) > 1 else next(iter(library))
    sound = sounds.resolve(choice, library)
    print(f"играю {sound.name} ({sound.seconds:.0f} с), ctrl-c чтобы остановить")

    player.start("105", sound.name, sound.source)
    try:
        while player.is_playing():
            time.sleep(0.3)
    except KeyboardInterrupt:
        player.stop()
        print("\nостановлено")
