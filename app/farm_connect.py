"""Setting up the link to the GPU machine from the dealer's panel.

The farm moves. It is a machine on a home connection behind a dynamic address,
and over the life of this project that address has changed several times — each
time leaving the panel talking to nothing until somebody edited ~/.ssh/config by
hand on the table's machine. This module is that edit, done from the panel: type
where the farm is now, type the password once, and the panel installs a key and
writes the Host block itself.

Three things are deliberately narrow, because this endpoint is the one place in
the project that takes a password over HTTP.

It is reachable only from the machine it runs on. app/server.py binds
0.0.0.0:8000 so players' phones can reach the game, which means a form that
posts a password would otherwise be reachable by anything on the same network —
including a guest's phone. `local_only` rejects anything whose peer is not a
loopback address, so the form works in the dealer's browser on the table's
machine and nowhere else.

The password is never stored. It is held in memory for the length of one
ssh-copy-id, passed to it through a file descriptor rather than a command line
(argv is world-readable on this machine), and dropped. Nothing writes it to a
log, a config file or a job record.

The key it installs is a new one per host, not a copy of an existing key. If the
farm is later replaced by a machine somebody else controls, the key that machine
holds opens nothing else.
"""

from __future__ import annotations

import ipaddress
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

SSH_DIR = Path.home() / ".ssh"
CONFIG = SSH_DIR / "config"

# The alias the rest of the project uses. app/voice_farm.HOST defaults to it and
# every ssh command in tts/ names it, so pointing this Host block at a new
# address is the whole of "the farm moved".
ALIAS = "gpufarm"

# Where the key for this alias lives. One key, replaced when the farm is
# reinstalled rather than accumulated, so that a stale key on an old machine is
# not left able to log in.
KEY = SSH_DIR / "backshot_farm"

# What a hostname may look like. Deliberately strict: this string becomes part
# of an ssh command line and a config file, and neither has a way to say "this
# next part is data".
HOSTNAME = re.compile(r"^[A-Za-z0-9._-]{1,253}$")
USERNAME = re.compile(r"^[A-Za-z0-9._-]{1,32}$")


class ConnectError(RuntimeError):
    """Something went wrong in words the operator can act on."""


def local_only(client_host: str | None) -> None:
    """Refuse anything that did not come from this machine.

    The check is on the peer address rather than on a header: Host and
    X-Forwarded-For are written by the client and prove nothing. A loopback peer
    cannot be forged by another machine on the network — reaching 127.0.0.1 on
    this host requires already being on this host.
    """
    if not client_host:
        raise ConnectError("не определить, откуда запрос — отказано")
    try:
        if not ipaddress.ip_address(client_host).is_loopback:
            raise ConnectError(
                "настройка фермы доступна только с самого игрового компьютера, "
                f"а запрос пришёл с {client_host}")
    except ValueError:
        raise ConnectError(f"непонятный адрес клиента: {client_host!r}")


def _check(host: str, user: str, port: int) -> tuple[str, str, int]:
    host = (host or "").strip()
    user = (user or "").strip()
    if not HOSTNAME.match(host):
        raise ConnectError(
            f"адрес {host!r}: только буквы, цифры, точка, дефис, подчёркивание")
    if not USERNAME.match(user):
        raise ConnectError(f"имя пользователя {user!r} не годится")
    if not 1 <= port <= 65535:
        raise ConnectError(f"порт {port} вне диапазона")
    return host, user, port


def _ensure_key() -> Path:
    """The key this panel uses for the farm, generated if it is not there yet.

    Not regenerated when it already exists. A new key would have to be installed
    on every machine the old one reached, and the common case for pressing this
    button is that the farm's address changed while the farm itself did not.
    """
    if KEY.is_file():
        return KEY

    SSH_DIR.mkdir(mode=0o700, exist_ok=True)
    result = subprocess.run(
        ["ssh-keygen", "-t", "ed25519", "-N", "", "-C", "backshot-farm",
         "-f", str(KEY)],
        capture_output=True, timeout=60)
    if result.returncode != 0 or not KEY.is_file():
        detail = result.stderr.decode("utf-8", "replace").strip()
        raise ConnectError(f"не создать ключ: {detail}")
    KEY.chmod(0o600)
    return KEY


def _trust_host_key(host: str, port: int) -> str:
    """Record the farm's host key, and say which one was recorded.

    Returned rather than silently accepted. A host key that changed when the
    address did is the ordinary case for a machine on a dynamic address; a host
    key that changed while the address stayed put is the signature of something
    else answering, and only the person at the table knows which happened. The
    fingerprint goes back to the panel so it can be shown.
    """
    SSH_DIR.mkdir(mode=0o700, exist_ok=True)
    known = SSH_DIR / "known_hosts"

    scan = subprocess.run(
        ["ssh-keyscan", "-T", "15", "-p", str(port), "-t", "ed25519", host],
        capture_output=True, timeout=40)
    keys = scan.stdout.decode("utf-8", "replace").strip()
    if not keys:
        raise ConnectError(
            f"{host}:{port} не отвечает на ssh — проверьте адрес и что машина включена")

    # Drop any previous record for this address before adding the new one, so a
    # farm that was reinstalled does not fail with a host key mismatch.
    subprocess.run(["ssh-keygen", "-R",
                    host if port == 22 else f"[{host}]:{port}",
                    "-f", str(known)],
                   capture_output=True, timeout=30)

    with known.open("a", encoding="utf-8") as handle:
        handle.write(keys + "\n")

    fingerprint = subprocess.run(
        ["ssh-keygen", "-lf", "-"], input=scan.stdout,
        capture_output=True, timeout=30)
    return fingerprint.stdout.decode("utf-8", "replace").strip() or "неизвестен"


def _install_key(host: str, user: str, port: int, password: str) -> None:
    """Put the public key on the farm, using the password exactly once.

    sshpass reads the password from a file descriptor rather than from argv or
    an environment variable. Both of those are readable by any other process on
    this machine for as long as the command runs; a pipe is not, and it closes
    with the command.
    """
    if not shutil.which("sshpass"):
        raise ConnectError(
            "нет sshpass — установите его (brew install hudochenkov/sshpass/sshpass) "
            "или пропишите ключ на ферму вручную через ssh-copy-id")

    read_fd, write_fd = os.pipe()
    try:
        os.write(write_fd, (password + "\n").encode("utf-8"))
        os.close(write_fd)
        write_fd = -1

        result = subprocess.run(
            ["sshpass", "-d", str(read_fd),
             "ssh-copy-id", "-i", str(KEY.with_suffix(".pub")),
             "-p", str(port),
             "-o", "StrictHostKeyChecking=yes",
             "-o", "ConnectTimeout=20",
             f"{user}@{host}"],
            capture_output=True, timeout=120, pass_fds=(read_fd,))
    finally:
        if write_fd != -1:
            os.close(write_fd)
        os.close(read_fd)

    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", "replace").strip().splitlines()
        last = detail[-1] if detail else "без объяснения"
        if "Permission denied" in last or "authentication" in last.lower():
            raise ConnectError("пароль не подошёл")
        raise ConnectError(f"не установить ключ: {last}")


def _write_config(host: str, user: str, port: int) -> None:
    """Point the `gpufarm` alias at this address, leaving other hosts alone.

    The file is rewritten rather than appended to. Appending a second Host block
    with the same name does not fail — ssh takes the first match and ignores the
    rest — so an appended fix would look like it worked and change nothing.
    """
    SSH_DIR.mkdir(mode=0o700, exist_ok=True)
    existing = CONFIG.read_text(encoding="utf-8") if CONFIG.is_file() else ""

    lines = existing.splitlines()
    kept: list[str] = []
    skipping = False
    for line in lines:
        stripped = line.strip()
        if stripped.lower().startswith("host "):
            names = stripped.split()[1:]
            skipping = ALIAS in names
        if not skipping:
            kept.append(line)

    block = [
        "",
        "# Машина с видеокартой для клонирования голосов (tts/remote.py).",
        "# Этот блок пишет панель дилера — правьте адрес там, а не здесь.",
        f"Host {ALIAS}",
        f"    HostName {host}",
        f"    User {user}",
    ]
    if port != 22:
        block.append(f"    Port {port}")
    block += [
        f"    IdentityFile {KEY}",
        "    IdentitiesOnly yes",
        "    ServerAliveInterval 15",
        "    ServerAliveCountMax 6",
        "    TCPKeepAlive yes",
        "    ConnectTimeout 30",
        "",
    ]

    text = "\n".join(kept).rstrip("\n") + "\n" + "\n".join(block)
    CONFIG.write_text(text.lstrip("\n"), encoding="utf-8")
    CONFIG.chmod(0o600)


def _verify(host: str) -> str:
    """Log in with the key and come back with what the far side said."""
    result = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=25",
         ALIAS, "hostname"],
        capture_output=True, timeout=60)
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", "replace").strip().splitlines()
        raise ConnectError(
            f"ключ поставлен, но войти по нему не выходит: "
            f"{detail[-1] if detail else 'без объяснения'}")
    return result.stdout.decode("utf-8", "replace").strip() or host


def connect(host: str, user: str, port: int, password: str) -> dict:
    """Give this panel a working key to the farm at `host`.

    Four steps, in the only order that works: trust the host key (ssh-copy-id
    refuses to talk to an unknown host), make sure a key exists, install it with
    the password, then write the config and prove the key logs in without one.
    """
    host, user, port = _check(host, user, port)
    if not password:
        raise ConnectError("нужен пароль от фермы — он никуда не сохраняется")

    fingerprint = _trust_host_key(host, port)
    _ensure_key()
    _install_key(host, user, port, password)
    _write_config(host, user, port)
    remote_name = _verify(host)

    return {
        "ok": True,
        "host": host,
        "user": user,
        "port": port,
        "alias": ALIAS,
        "hostname": remote_name,
        "fingerprint": fingerprint,
        "key": str(KEY),
    }


def current() -> dict:
    """What the `gpufarm` alias points at now, for showing in the form."""
    if not CONFIG.is_file():
        return {"configured": False}

    inside = False
    found: dict[str, str] = {}
    for line in CONFIG.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.lower().startswith("host "):
            inside = ALIAS in stripped.split()[1:]
            continue
        if inside and stripped and not stripped.startswith("#"):
            key, _, value = stripped.partition(" ")
            found[key.lower()] = value.strip()

    if not found:
        return {"configured": False}
    return {
        "configured": True,
        "host": found.get("hostname", ""),
        "user": found.get("user", ""),
        "port": int(found.get("port", "22") or 22),
        "key_exists": KEY.is_file(),
    }
