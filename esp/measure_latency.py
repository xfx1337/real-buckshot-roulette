#!/usr/bin/env python3
"""
Замер задержки «приём нажатия курка → срабатывание соленоида» на ESP32.

Прошивка (esp.ino) в момент выстрела печатает в Serial строку вида:
    [ЗАМЕР] Задержка курок→соленоид: 118.4 мс
Это ВНУТРЕННЯЯ задержка платы, измеренная её же micros(): от приёма ПЕРВОГО
валидного RF-пакета серии до момента setSolenoid(true). Она не искажена
задержкой USB-Serial, поэтому точна. Основную часть задержки составляет
ожидание ВТОРОГО пакета (нужны 2 для подтверждения курка) — это физика пульта.

Скрипт читает Serial, собирает все такие замеры и печатает статистику.

Запуск (из папки esp или из корня):
    python esp/measure_latency.py                # автоопределение порта, 60с
    python esp/measure_latency.py --seconds 30   # своё окно
    python esp/measure_latency.py --port /dev/cu.usbserial-10

Во время работы просто нажимай пульт-курок. Ctrl+C — завершить и показать итог.
"""

import argparse
import glob
import re
import statistics
import sys
import time

try:
    import serial  # pyserial
except ImportError:
    sys.exit("Нужен pyserial:  pip install pyserial  (или используй venv проекта)")

# Ловим и «боевой→соленоид», и «холостой→решение» — путь по таймингу одинаков.
LATENCY_RE = re.compile(r"\[ЗАМЕР\].*?:\s*([\d.]+)\s*мс")


def autodetect_port() -> str | None:
    # На macOS плата видна как /dev/cu.usbserial-*, на Linux — /dev/ttyUSB*.
    for pattern in ("/dev/cu.usbserial-*", "/dev/ttyUSB*", "/dev/ttyACM*"):
        found = sorted(glob.glob(pattern))
        if found:
            return found[0]
    return None


def main() -> None:
    ap = argparse.ArgumentParser(description="Замер задержки курок→соленоид (ESP32)")
    ap.add_argument("--port", help="Serial-порт платы (по умолчанию — автоопределение)")
    ap.add_argument("--baud", type=int, default=115200, help="Скорость Serial (по умолч. 115200)")
    ap.add_argument("--seconds", type=float, default=60.0, help="Сколько слушать, сек (по умолч. 60)")
    args = ap.parse_args()

    port = args.port or autodetect_port()
    if not port:
        sys.exit("Не найден порт платы. Подключи ESP32 или укажи --port.")

    # Открываем БЕЗ дёрганья DTR/RTS, чтобы не ресетить плату каждый запуск.
    s = serial.Serial()
    s.port = port
    s.baudrate = args.baud
    s.dtr = False
    s.rts = False
    s.timeout = 0.2
    try:
        s.open()
    except serial.SerialException as e:
        sys.exit(f"Не удалось открыть {port}: {e}\n"
                 f"Закрой Arduino IDE / другой монитор, если он держит порт.")

    print(f"Порт: {port} @ {args.baud}. Слушаю {args.seconds:.0f}с.")
    print(">>> НАЖИМАЙ ПУЛЬТ-КУРОК. Ctrl+C — завершить досрочно.\n")

    samples: list[float] = []
    start = time.time()
    try:
        while time.time() - start < args.seconds:
            raw = s.readline()
            if not raw:
                continue
            line = raw.decode(errors="replace").rstrip()
            m = LATENCY_RE.search(line)
            if m:
                val = float(m.group(1))
                samples.append(val)
                print(f"  #{len(samples):>2}  {val:7.1f} мс")
            elif "Курок" in line and "[ЗАМЕР]" not in line:
                # выстрел без метки (не было firstPacketMicros) — сообщаем
                pass
    except KeyboardInterrupt:
        print("\n(остановлено вручную)")
    finally:
        s.close()

    print("\n" + "=" * 40)
    if not samples:
        print("Замеров нет. Нажатия не пойманы — проверь, что пульт валидный")
        print("и что прошивка с блоком [ЗАМЕР] залита.")
        return

    print(f"Замеров: {len(samples)}")
    print(f"  min:    {min(samples):7.1f} мс")
    print(f"  сред.:  {statistics.mean(samples):7.1f} мс")
    print(f"  медиана:{statistics.median(samples):7.1f} мс")
    print(f"  max:    {max(samples):7.1f} мс")
    if len(samples) > 1:
        print(f"  разброс(σ): {statistics.pstdev(samples):5.1f} мс")
    print("=" * 40)
    print("Напоминание: декодер стреляет с ОДНОГО валидного кадра прямо из ISR.")
    print("Задержка ≈ длительность приёма кадра (~35мс для протокола 1). Если")
    print("pulse_us в config.json равен 0, добавляется один кадр на автоопределение")
    print("юнита — впиши точное значение из режима обучения, станет быстрее.")


if __name__ == "__main__":
    main()
