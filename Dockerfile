# ── Buckshot Roulette IRL — серверный образ ──
# Только Python/FastAPI-сервер. Прошивка ESP32 и определение Wi-Fi живут на
# ХОСТЕ (см. start.sh): Docker Desktop на macOS/Windows не пробрасывает USB в
# контейнер, а Keychain/NetworkManager хоста изнутри не виден.
FROM python:3.12-slim

# Не писать .pyc, не буферизовать stdout (логи uvicorn видны сразу в docker logs).
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Сначала только зависимости — слой кэшируется, пока requirements.txt не менялся.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Затем код. config.json НЕ копируем в образ (в .dockerignore) — он монтируется
# томом на старте, чтобы правки сети/IP подхватывались без пересборки образа.
COPY server.py game_engine.py config.py ./
COPY templates/ ./templates/
COPY static/ ./static/

EXPOSE 8000

# Запуск через uvicorn напрямую (host/port берём из config.json внутри server.py
# нельзя — CMD статичен; поэтому слушаем 0.0.0.0, порт фиксируем 8000, а наружу
# его пробрасывает docker-compose). reload выключен — это прод-запуск в контейнере.
CMD ["python", "-m", "uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8000"]
