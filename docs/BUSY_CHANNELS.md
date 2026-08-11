# История занятости каналов VoIP

## Описание

Система автоматического сбора и хранения информации о занятости VoIP каналов. Отслеживает все звонки в реальном времени и сохраняет их в SQLite базу данных для последующего анализа.

## Возможности

### Автоматический сбор данных

- **События звонков**: Отслеживание начала канала (Newchannel), изменения состояния (Newstate) и отбоя (Hangup)
- **Снимки состояния**: Периодическое сохранение состояния всех портов FXS
- **В реальном времени**: Интеграция с `DigitWatcher` для мгновенного реагирования на события AMI

### Хранение данных

База данных SQLite: `data/busy_channels.db`

**Таблица `calls`** — история звонков:
- `id` — уникальный идентификатор
- `exten` — расширение (101-108)
- `slot` — порт на шлюзе (0/0 - 0/7)
- `channel` — имя канала Asterisk
- `channel_state` — состояние канала (Ring, Up, Down и т.д.)
- `started_at` — время начала (unix timestamp)
- `ended_at` — время окончания (unix timestamp)
- `duration` — длительность в секундах
- `caller` — номер звонящего
- `connected` — номер абонента
- `hangup_cause` — причина отбоя

**Таблица `snapshots`** — снимки состояния портов:
- `id` — уникальный идентификатор
- `timestamp` — время снимка (unix timestamp)
- `exten` — расширение
- `slot` — порт
- `gateway_state` — состояние на шлюзе (Idle, On-Hook и т.д.)
- `channel` — активный канал (если есть)
- `channel_state` — состояние канала
- `duration` — длительность текущего звонка

## API эндпоинты

### GET /api/voip/busy/history

Получить историю звонков с фильтрацией.

**Параметры:**
- `exten` (optional) — фильтр по расширению (101-108)
- `since` (optional) — начало периода (unix timestamp)
- `until` (optional) — конец периода (unix timestamp)
- `limit` (optional) — максимум записей (по умолчанию 100)

**Пример:**
```bash
curl "http://localhost:8000/api/voip/busy/history?exten=101&limit=50"
```

**Ответ:**
```json
{
  "calls": [
    {
      "id": 1,
      "exten": "101",
      "slot": "0/0",
      "channel": "PJSIP/101@addpac-00000001",
      "channel_state": "Up",
      "started_at": 1735671234.56,
      "ended_at": 1735671250.12,
      "duration": 15,
      "caller": "101",
      "connected": "lobby",
      "hangup_cause": "Normal Clearing"
    }
  ]
}
```

### GET /api/voip/busy/statistics

Получить статистику по звонкам за период.

**Параметры:**
- `since` (optional) — начало периода (unix timestamp)

**Пример:**
```bash
# Статистика за последние 24 часа
curl "http://localhost:8000/api/voip/busy/statistics?since=$(date -v-24H +%s)"
```

**Ответ:**
```json
{
  "total_calls": 125,
  "completed_calls": 120,
  "avg_duration": 45.5,
  "total_duration": 5460,
  "by_extension": [
    {
      "exten": "101",
      "calls_count": 35,
      "avg_duration": 52.3,
      "total_duration": 1830,
      "last_call_at": 1735671234.56
    }
  ]
}
```

### GET /api/voip/busy/active

Получить текущие активные звонки.

**Пример:**
```bash
curl "http://localhost:8000/api/voip/busy/active"
```

**Ответ:**
```json
{
  "active_calls": [
    {
      "channel": "PJSIP/101@addpac-00000001",
      "exten": "101",
      "slot": "0/0",
      "channel_state": "Up",
      "started_at": 1735671234.56,
      "caller": "101",
      "connected": "lobby",
      "current_duration": 15
    }
  ]
}
```

## Веб интерфейс

### Раздел "История занятости каналов"

Доступен на странице `/voip` после секции "Набранные цифры и звонки".

**Фильтры:**
- **Расширение** — показать звонки только с выбранного расширения
- **Период** — последний час / 6 часов / 24 часа / неделя / вся история

**Статистика:**
- Всего звонков за период
- Завершённых звонков
- Средняя длительность
- Общее время разговоров

**Таблица истории:**
- Расширение и порт
- Время начала и окончания
- Длительность в секундах
- Имя канала
- Причина отбоя

Активные звонки выделяются фоном и показывают "активен" вместо времени окончания.

**Автообновление:** История обновляется каждые 30 секунд автоматически.

## Интеграция с существующей системой

### voip.py

Трекер интегрирован в два места:

1. **DigitWatcher._handle()** — обработка событий AMI:
   - `Newchannel` → `tracker.on_channel_new()`
   - `Newstate` → `tracker.on_channel_state_change()`
   - `Hangup` → `tracker.on_channel_hangup()`

2. **collect_status()** — сохранение снимков:
   - После сбора состояния всех портов вызывается `tracker.save_snapshot()`
   - Выполняется каждый раз при вызове `/api/voip/status` (обычно каждые 10 секунд)

### Инициализация

Трекер создаётся автоматически при первом вызове `get_tracker()`. База данных инициализируется при создании трекера.

## Обслуживание

### Очистка старых данных

Метод `cleanup_old_data(days)` удаляет записи старше указанного количества дней:

```python
from app.busy_tracker import get_tracker

tracker = get_tracker()
tracker.cleanup_old_data(days=30)  # Удалить данные старше 30 дней
```

Рекомендуется настроить периодическую очистку через cron или systemd timer.

### Резервное копирование

База данных — это обычный SQLite файл, который можно копировать:

```bash
# Создать резервную копию
cp data/busy_channels.db data/busy_channels_backup_$(date +%Y%m%d).db

# Или использовать sqlite3
sqlite3 data/busy_channels.db ".backup data/busy_channels_backup.db"
```

### Размер базы данных

Примерный расчёт:
- 1 звонок ≈ 0.5 KB в таблице `calls`
- 1 снимок (8 портов) ≈ 1 KB в таблице `snapshots`
- Снимки каждые 10 секунд = 8,640 снимков/день ≈ 8.6 MB/день
- За 30 дней ≈ 260 MB

Рекомендуется регулярная очистка старых данных.

## Примеры использования

### Python API

```python
from app.busy_tracker import get_tracker

tracker = get_tracker()

# Получить звонки с расширения 101 за последний час
import time
since = time.time() - 3600
calls = tracker.get_calls_history(exten="101", since=since, limit=50)

for call in calls:
    print(f"{call['exten']}: {call['duration']} сек")

# Статистика за сегодня
from datetime import datetime
today_start = datetime.now().replace(hour=0, minute=0, second=0).timestamp()
stats = tracker.get_statistics(since=today_start)
print(f"Звонков сегодня: {stats['total_calls']}")

# Текущие активные звонки
active = tracker.get_active_calls()
print(f"Сейчас активно: {len(active)} звонков")
```

### SQL запросы

Прямой доступ к базе данных:

```bash
sqlite3 data/busy_channels.db

# Топ-5 самых активных расширений
SELECT exten, COUNT(*) as calls, SUM(duration) as total_sec
FROM calls 
WHERE ended_at IS NOT NULL
GROUP BY exten 
ORDER BY calls DESC 
LIMIT 5;

# Средняя длительность по часам дня
SELECT strftime('%H', datetime(started_at, 'unixepoch', 'localtime')) as hour,
       COUNT(*) as calls,
       AVG(duration) as avg_duration
FROM calls
WHERE ended_at IS NOT NULL
GROUP BY hour
ORDER BY hour;

# Звонки длиннее 60 секунд
SELECT exten, started_at, duration, hangup_cause
FROM calls
WHERE duration > 60
ORDER BY started_at DESC
LIMIT 20;
```

## Troubleshooting

### База данных не создаётся

Убедитесь, что директория `data/` существует и доступна для записи:

```bash
mkdir -p data
chmod 755 data
```

### События не записываются

Проверьте, что DigitWatcher запущен. В логе должно быть:

```
АТС: поток событий подключён
```

Проверьте в коде, что трекер импортирован и вызывается в `voip.py`.

### История не отображается в веб интерфейсе

Откройте консоль браузера (F12) и проверьте ошибки JavaScript. Убедитесь, что API эндпоинты отвечают:

```bash
curl http://localhost:8000/api/voip/busy/history
```

## Производительность

- **Запись событий**: Асинхронная, не блокирует обработку AMI событий
- **Запрос истории**: Индексы на `exten`, `started_at`, `ended_at` для быстрого поиска
- **Снимки**: Сохраняются батчем (все 8 портов за одну транзакцию)

## Будущие улучшения

- [ ] Экспорт в CSV/JSON
- [ ] Графики временной активности (charts.js)
- [ ] Алерты при аномальной активности
- [ ] Интеграция с системой мониторинга (Prometheus/Grafana)
- [ ] Детектирование паттернов звонков
- [ ] Архивирование старых данных в отдельные файлы
