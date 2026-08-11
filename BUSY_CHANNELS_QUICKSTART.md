# Быстрый старт: Сбор busy каналов

## Что реализовано

✅ **Автоматический сбор** звонков в реальном времени через AMI события  
✅ **SQLite база данных** для хранения истории (`data/busy_channels.db`)  
✅ **API эндпоинты** для получения истории, статистики и активных звонков  
✅ **Веб интерфейс** с фильтрами, статистикой и таблицей истории  
✅ **Автообновление** истории каждые 30 секунд  

## Как использовать

### 1. Запустить сервер

```bash
python3 app/server.py
```

### 2. Открыть веб интерфейс

Перейти на http://localhost:8000/voip

### 3. Просмотреть историю

Прокрутить страницу вниз до раздела **"История занятости каналов"**

**Доступно:**
- Фильтрация по расширению (101-108)
- Фильтрация по периоду (час, 6 часов, 24 часа, неделя, вся история)
- Статистика: всего звонков, завершённых, средняя длительность, общее время
- Таблица с деталями каждого звонка

### 4. API запросы

```bash
# История звонков
curl "http://localhost:8000/api/voip/busy/history?limit=50"

# Статистика за последние 24 часа
curl "http://localhost:8000/api/voip/busy/statistics?since=$(date -v-24H +%s)"

# Текущие активные звонки
curl "http://localhost:8000/api/voip/busy/active"
```

## Структура файлов

```
app/
├── busy_tracker.py          # Основной модуль трекера
├── voip.py                  # Интеграция с DigitWatcher (изменён)
├── server.py                # API эндпоинты (изменён)
└── templates/
    └── voip.html            # Веб интерфейс с историей (изменён)

data/
└── busy_channels.db         # SQLite база данных (создаётся автоматически)

docs/
└── BUSY_CHANNELS.md         # Подробная документация

test_busy_tracker.py         # Тест трекера
```

## Что происходит автоматически

1. **При запуске сервера**: создаётся база данных `data/busy_channels.db` (если её нет)

2. **При звонке**: 
   - Событие `Newchannel` → запись в таблицу `calls` (начало звонка)
   - Событие `Newstate` → обновление состояния канала
   - Событие `Hangup` → обновление записи (время окончания, длительность, причина)

3. **Каждые 10 секунд**: сохраняется снимок состояния всех портов в таблицу `snapshots`

4. **В веб интерфейсе**: автообновление истории каждые 30 секунд

## Тестирование

Запустить тест:

```bash
python3 test_busy_tracker.py
```

Ожидаемый вывод:
```
=== Тест BusyTracker ===
...
✅ Тест завершён успешно!
```

## Проверка работы

### 1. Проверить базу данных

```bash
sqlite3 data/busy_channels.db "SELECT COUNT(*) FROM calls"
sqlite3 data/busy_channels.db "SELECT COUNT(*) FROM snapshots"
```

### 2. Посмотреть последние звонки

```bash
sqlite3 data/busy_channels.db "SELECT exten, datetime(started_at, 'unixepoch', 'localtime') as time, duration FROM calls ORDER BY started_at DESC LIMIT 10"
```

### 3. Проверить API

```bash
curl -s http://localhost:8000/api/voip/busy/statistics | python3 -m json.tool
```

## Очистка тестовых данных

```bash
rm data/busy_channels.db
rm data/test_busy_channels.db
```

При следующем запуске сервера база создастся заново.

## Поддержка

Подробная документация: `docs/BUSY_CHANNELS.md`

## Примеры SQL запросов

```sql
-- Топ-5 самых активных расширений
SELECT exten, COUNT(*) as calls 
FROM calls 
GROUP BY exten 
ORDER BY calls DESC 
LIMIT 5;

-- Звонки длиннее 60 секунд
SELECT exten, duration, hangup_cause 
FROM calls 
WHERE duration > 60 
ORDER BY started_at DESC;

-- Активность по часам
SELECT strftime('%H', datetime(started_at, 'unixepoch', 'localtime')) as hour,
       COUNT(*) as calls
FROM calls
GROUP BY hour
ORDER BY hour;
```

---

**Готово!** Система сбора busy каналов полностью интегрирована и работает.
