#!/usr/bin/env python3
"""
Buckshot Roulette IRL — запуск сервера напрямую, без Docker.

    python3 run.py                 обычный запуск
    python3 run.py --reload        автоперезапуск при правке кода (разработка)
    python3 run.py --no-mediamtx   не поднимать MediaMTX (камеры не нужны)
    python3 run.py --no-browser    не открывать браузер
    python3 run.py --port 8080     переопределить порт из config.json

Почему без Docker лучше для камер: MediaMTX работает в сети хоста напрямую,
поэтому SRT/WebRTC видят реальные адреса телефонов, а не внутренний
docker-мост 192.168.65.x. Меньше слой NAT — меньше поводов для обрыва потока.

MediaMTX поднимается этим же скриптом ДО старта сервера, чтобы камеры можно
было подключать сразу, не нажимая кнопку в панели дилера. Дочерний процесс
глушится в finally — Ctrl+C кладёт и сервер, и MediaMTX.
"""

import argparse
import json
import os
import platform
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

ROOT = Path(__file__).parent.resolve()
CONFIG_PATH = ROOT / "config.json"
EXAMPLE_PATH = ROOT / "config.example.json"
MEDIAMTX_DIR = ROOT / "mediamtx"


def mediamtx_binary() -> Path:
    return MEDIAMTX_DIR / ("mediamtx.exe" if sys.platform == "win32" else "mediamtx")


def lan_ip() -> str:
    """LAN-адрес этого хоста — тот, по которому до сервера достучатся телефон и
    ESP32. Берём через UDP-сокет к внешнему адресу: пакет никуда не уходит
    (UDP без соединения), но ядро выбирает исходящий интерфейс и его адрес.
    Надёжнее, чем hostname -I: не путается в loopback и docker-мостах."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def ensure_config() -> dict:
    if not CONFIG_PATH.exists():
        if not EXAMPLE_PATH.exists():
            sys.exit(f"❌ Нет ни {CONFIG_PATH.name}, ни {EXAMPLE_PATH.name}.")
        print(f"▸ {CONFIG_PATH.name} не найден — создаю из {EXAMPLE_PATH.name}.")
        CONFIG_PATH.write_text(EXAMPLE_PATH.read_text(encoding="utf-8"), encoding="utf-8")
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)


def port_busy(port: int, udp: bool = False) -> bool:
    """Занят ли порт. Проверяем ДО старта: MediaMTX при занятом порте просто
    умирает, а сервер тогда молча работает без камер — неочевидный отказ."""
    kind = socket.SOCK_DGRAM if udp else socket.SOCK_STREAM
    s = socket.socket(socket.AF_INET, kind)
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("0.0.0.0", port))
        return False
    except OSError:
        return True
    finally:
        s.close()


def start_mediamtx() -> subprocess.Popen | None:
    """Поднимает MediaMTX как дочерний процесс. Возвращает None, если не вышло —
    сервер всё равно стартует, камеры просто будут недоступны."""
    binary = mediamtx_binary()
    if not binary.exists():
        print(f"⚠️  Бинарь MediaMTX не найден: {binary}")
        print("    Камеры работать не будут. Скачать: https://github.com/bluenviron/mediamtx/releases")
        return None

    # Бинарь мог приехать из релизного архива или из git без бита x.
    if not os.access(binary, os.X_OK):
        binary.chmod(0o755)

    # macOS-бинарь в Docker-томе — Mach-O, на Linux не пойдёт, и наоборот.
    if sys.platform != "win32":
        try:
            kind = subprocess.run(["file", "-b", str(binary)], capture_output=True,
                                  text=True, timeout=5).stdout
            host_is_mac = sys.platform == "darwin"
            if host_is_mac and "Mach-O" not in kind:
                print(f"⚠️  MediaMTX собран не под macOS ({kind.strip()}) — пропускаю запуск.")
                return None
            if not host_is_mac and "ELF" not in kind:
                print(f"⚠️  MediaMTX собран не под Linux ({kind.strip()}) — пропускаю запуск.")
                return None
        except (OSError, subprocess.SubprocessError):
            pass  # нет утилиты file — просто пробуем запустить

    if port_busy(8890, udp=True):
        print("⚠️  UDP-порт 8890 занят — MediaMTX уже запущен (или висит Docker).")
        print("    Останови старый экземпляр:  docker compose down   /   pkill mediamtx")
        return None

    try:
        proc = subprocess.Popen(
            [str(binary)],
            cwd=str(MEDIAMTX_DIR),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            encoding="utf-8",
            errors="replace",
        )
    except OSError as e:
        print(f"⚠️  Не удалось запустить MediaMTX: {e}")
        return None

    # Печатаем только важное: полный лог MediaMTX забивает консоль сервера, а
    # он и так виден целиком в панели дилера.
    def pump() -> None:
        for line in iter(proc.stdout.readline, ""):
            low = line.lower()
            if any(w in low for w in ("err", "warn", "fail", "publishing", "srt")):
                print(f"  [mediamtx] {line.rstrip()}")

    threading.Thread(target=pump, daemon=True).start()

    time.sleep(1.0)
    if proc.poll() is not None:
        print(f"⚠️  MediaMTX завершился сразу (код {proc.returncode}).")
        return None

    print("▸ MediaMTX запущен (SRT :8890, WebRTC :8889, RTMP :1935, RTSP :8554)")
    return proc


def main() -> None:
    ap = argparse.ArgumentParser(description="Запуск Buckshot Roulette IRL без Docker")
    ap.add_argument("--reload", action="store_true", help="автоперезапуск при правке кода")
    ap.add_argument("--no-mediamtx", action="store_true", help="не запускать MediaMTX")
    ap.add_argument("--no-browser", action="store_true", help="не открывать браузер")
    ap.add_argument("--host", default=None, help="переопределить host из config.json")
    ap.add_argument("--port", type=int, default=None, help="переопределить port из config.json")
    args = ap.parse_args()

    cfg = ensure_config()
    host = args.host or cfg["server"]["host"]
    port = args.port or int(cfg["server"]["port"])
    ip = lan_ip()

    print("───────────────────────────────────────────────")
    print("  Buckshot Roulette IRL — запуск без Docker")
    print("───────────────────────────────────────────────")
    print(f"▸ Python:  {platform.python_version()}  ({sys.executable})")
    print(f"▸ LAN-IP:  {ip}")

    if port_busy(port):
        sys.exit(f"❌ Порт {port} уже занят. Останови старый сервер "
                 f"(docker compose down) или запусти с --port ДРУГОЙ.")

    mtx = None if args.no_mediamtx else start_mediamtx()

    # Экраны, которые крутятся на этом же компьютере, открываем по localhost:
    # выбор устройства вывода звука (setSinkId / enumerateDevices) работает
    # только в secure context, а http://<LAN-IP> для браузера незащищённый —
    # там список колонок пуст и звук нельзя развести по разным устройствам.
    # LAN-адрес остаётся для телефонов игроков.
    print(f"▸ Дилер:   http://localhost:{port}/dealer")
    print(f"▸ Камеры:  http://localhost:{port}/cams")
    print(f"▸ Экран игрока (ТВ): http://localhost:{port}/telejoin")
    print(f"▸ Для телефонов игроков: http://{ip}:{port}/join")
    if mtx:
        print(f"▸ SRT для IRL Pro:  srt://{ip}:8890?streamid=publish:cam1")
        print("    (cam1…cam4 — по одной камере на телефон; streamid должен совпадать")
        print("     с плиткой, которую смотришь на /cams)")
    print("▸ Стоп: Ctrl+C")
    print()

    if not args.no_browser:
        # Ждём в фоне, пока uvicorn поднимет сокет: открыть браузер до этого —
        # получить ERR_CONNECTION_REFUSED.
        def open_later() -> None:
            for _ in range(30):
                if port_busy(port):  # порт занят = сервер слушает
                    webbrowser.open(f"http://localhost:{port}/dealer")
                    return
                time.sleep(0.5)

        threading.Thread(target=open_later, daemon=True).start()

    # server.py импортирует пакет app.* — корень проекта должен быть на пути,
    # иначе запуск из другого каталога падает на ModuleNotFoundError.
    sys.path.insert(0, str(ROOT))

    import uvicorn

    try:
        uvicorn.run("app.server:app", host=host, port=port, reload=args.reload)
    except KeyboardInterrupt:
        pass
    finally:
        if mtx and mtx.poll() is None:
            print("\n▸ Останавливаю MediaMTX…")
            mtx.terminate()
            try:
                mtx.wait(timeout=5)
            except subprocess.TimeoutExpired:
                mtx.kill()
        print("✅ Остановлено.")


if __name__ == "__main__":
    main()
