# Архитектура проекта — Buckshot Roulette IRL

Физическая реализация игры «Buckshot Roulette»: веб-панель дилера и телефоны
игроков управляют партией, а физический дробовик с соленоидом (ESP32 + RF-курок)
синхронизируется с игровой логикой.

---

## 1. Разделение ответственности (компоненты)

Каждый слой отвечает строго за своё. Игровая логика полностью изолирована в
`app/game_engine.py` и ничего не знает про HTTP/WebSocket/железо.

```mermaid
flowchart TB
    subgraph HW["🔫 Железо (ESP32)"]
        direction TB
        RF["RF-приёмник 433МГц<br/>(курок-пульт)"]
        ESP["esp/esp.ino<br/>прошивка"]
        SOL["Соленоид / актуатор"]
        LED["LED-индикатор<br/>боевого патрона"]
        RF --> ESP
        ESP --> SOL
        ESP --> LED
    end

    subgraph SRV["🖥️ Сервер (FastAPI)"]
        direction TB
        API["app/server.py<br/>HTTP + WebSocket транспорт"]
        ENGINE["app/game_engine.py<br/>GameState — вся игровая логика"]
        CFG["app/config.py / config.json<br/>единый конфиг"]
        API -->|"вызывает методы"| ENGINE
        API -.->|"host/port"| CFG
    end

    subgraph WEB["🌐 Браузерные клиенты"]
        direction TB
        DEALER["app/templates/dealer.html<br/>панель дилера"]
        PLAYER["app/templates/player.html<br/>экран игрока"]
    end

    ESP -->|"GET /api/esp/shell_status<br/>POST /api/esp/shoot"| API
    CFG -.->|"gen_config.py<br/>→ config.h"| ESP

    DEALER <-->|"WS /ws/dealer<br/>+ POST /api/*"| API
    PLAYER <-->|"WS /ws/player/{id}"| API

    classDef hw fill:#3a1414,stroke:#c45050,color:#e8ddce
    classDef srv fill:#14100f,stroke:#3fef6a,color:#e8ddce
    classDef web fill:#100c0b,stroke:#6e5f52,color:#e8ddce
    class RF,ESP,SOL,LED hw
    class API,ENGINE,CFG srv
    class DEALER,PLAYER web
```

| Слой | Файл | Ответственность | Чего НЕ делает |
|------|------|-----------------|----------------|
| **Железо** | `esp/esp.ino` | Ловит RF-сигнал курка, бьёт соленоидом на боевом патроне, опрашивает сервер | Не хранит игровое состояние, не считает урон |
| **Транспорт** | `app/server.py` | HTTP-эндпоинты, WebSocket-рассылка, undo-стек, сериализация | Не содержит игровых правил — делегирует движку |
| **Логика** | `app/game_engine.py` | State machine, патроны, предметы, HP, ходы, победа | Не знает про сеть, HTTP, железо |
| **Конфиг** | `app/config.py`, `config.json` | Единый источник настроек (Wi-Fi, адрес, пины) | — |
| **UI дилера** | `app/templates/dealer.html` | Управление партией, меню «в кого попали?» | Не считает логику — только шлёт команды |
| **UI игрока** | `app/templates/player.html` | Показ HP/предметов игроку | Read-only, без секретов (порядок патронов скрыт) |

---

## 2. UML — модель данных движка (class diagram)

Всё состояние партии держится в единственном объекте `GameState` (in-memory,
без БД). Ключевые перечисления — `GamePhase`, `ShellType`, `ItemType`.

```mermaid
classDiagram
    class GameState {
        +str game_id
        +GamePhase phase
        +GameConfig config
        +dict~str,Player~ players
        +list~str~ turn_order
        +int current_turn_idx
        +int current_round
        +list~ShellType~ shells
        +bool saw_active
        +bool inverted
        +bool pending_shot
        +str winner_id
        +list~GameEvent~ event_log
        +start_game()
        +confirm_shells_loaded()
        +shoot(target_id) dict
        +use_item_xxx(...) dict
        +esp_shell_status() dict
        +esp_shoot() dict
        +to_dict(for_dealer) dict
        +player_view(player_id) dict
    }

    class Player {
        +str id
        +str name
        +int number
        +int hp
        +int max_hp
        +bool alive
        +bool connected
        +list~ItemType~ items
        +int handcuffs_state
    }

    class GameConfig {
        +str game_mode
        +list~dict~ rounds
        +int max_items_per_player
        +int physical_magazine_limit
        +dict item_limits_global
        +dict item_limits_per_player
    }

    class GameEvent {
        +float timestamp
        +str message
        +str event_type
    }

    class GamePhase {
        <<enumeration>>
        LOBBY
        ROUND_START
        DEALER_LOADING
        DEALER_RELOADING
        DEALER_ITEMS
        PLAYER_TURN
        ROUND_OVER
        GAME_OVER
    }

    class ShellType {
        <<enumeration>>
        LIVE
        BLANK
    }

    class ItemType {
        <<enumeration>>
        BEER
        HANDSAW
        HANDCUFFS
        MAGNIFYING_GLASS
        CIGARETTES
        ADRENALINE
        BURNER_PHONE
        INVERTER
        EXPIRED_MEDICINE
    }

    GameState "1" *-- "2..4" Player : players
    GameState "1" *-- "1" GameConfig : config
    GameState "1" *-- "0..*" GameEvent : event_log
    GameState "1" o-- "1" GamePhase : phase
    GameState "1" *-- "0..*" ShellType : shells
    Player "1" *-- "0..*" ItemType : items
```

---

## 3. State machine — фазы партии

Игра — конечный автомат. Переходы инициируются действиями дилера (кнопки в
панели) или автоматически при исчерпании патронов / гибели игроков.

```mermaid
stateDiagram-v2
    [*] --> LOBBY : create_game()
    LOBBY --> DEALER_LOADING : start_game()<br/>(≥2 игрока)

    DEALER_LOADING --> DEALER_ITEMS : confirm_shells_loaded()
    DEALER_RELOADING --> DEALER_ITEMS : confirm_shells_loaded()
    DEALER_ITEMS --> PLAYER_TURN : confirm_items_dealt()

    PLAYER_TURN --> PLAYER_TURN : shoot() / use_item()<br/>(остались патроны)
    PLAYER_TURN --> DEALER_RELOADING : магазин пуст,<br/>раунд продолжается
    PLAYER_TURN --> ROUND_OVER : остался 1 живой<br/>в раунде

    ROUND_OVER --> DEALER_LOADING : next_round()
    ROUND_OVER --> GAME_OVER : это был<br/>последний раунд

    PLAYER_TURN --> GAME_OVER : force_end()
    GAME_OVER --> LOBBY : create_game()
    GAME_OVER --> [*]

    note right of PLAYER_TURN
        pending_shot=true, когда пришёл
        физический выстрел с ESP и дилер
        ещё не выбрал цель
    end note
```

---

## 4. Алгоритм физического выстрела (ESP32 ↔ сервер ↔ дилер)

Ключевой поток проекта. Разделение: **соленоид бьёт мгновенно по кэшу** (без
задержки сети), а **игровой эффект (урон) подтверждает дилер**, чтобы патрон
списался из очереди ровно один раз.

```mermaid
sequenceDiagram
    autonumber
    participant P as 🔫 Игрок (курок)
    participant E as ESP32 (esp.ino)
    participant S as app/server.py
    participant G as game_engine (GameState)
    participant D as Панель дилера

    Note over E,S: Фоновый опрос раз в 5 сек
    loop каждые POLL_INTERVAL_MS
        E->>S: GET /api/esp/shell_status
        S->>G: esp_shell_status()
        G-->>S: {ready, live}
        S-->>E: {ready, live}
        Note over E: кэш обновлён;<br/>LED мигает если live
    end

    P->>E: нажатие курка (RF 433МГц)
    Note over E: код совпал с KNOWN_TRIGGER_CODE
    alt следующий патрон боевой (cachedLive)
        E->>E: импульс на соленоид (SOLENOID_PULSE_MS)
    else холостой
        E->>E: тишина
    end
    E->>E: pendingShoot = true

    Note over E: в loop() — неблокирующий POST
    E->>S: POST /api/esp/shoot
    S->>G: esp_shoot()
    Note over G: НЕ трогает очередь!<br/>ставит pending_shot=true
    G-->>S: {ok, fired, live}
    S->>D: broadcast_state() (WS)
    Note over D: показывает меню<br/>«⚡ В КОГО ПОПАЛИ?»

    D->>S: POST /api/shoot {target_id}
    S->>G: shoot(target_id)
    Note over G: shells.pop(0) — патрон ушёл 1 раз<br/>урон + смена хода<br/>pending_shot=false
    G-->>S: {shell, damage, target_hp_after}
    S->>D: broadcast_state() (WS) — меню исчезает
```

---

## 5. Развёртывание и конфиг

```mermaid
flowchart LR
    JSON["config.json<br/>(единый источник,<br/>gitignored)"]
    JSON -->|"app/config.py читает"| PY["app/server.py<br/>host/port"]
    JSON -->|"esp/gen_config.py<br/>генерирует"| H["esp/config.h<br/>(gitignored)"]
    H -->|"#include"| INO["esp.ino<br/>Wi-Fi, пины, код пульта"]
    EX["config.example.json<br/>(шаблон в git)"] -.->|"cp"| JSON

    classDef cfg fill:#14100f,stroke:#3fef6a,color:#e8ddce
    class JSON,PY,H,INO,EX cfg
```

**Запуск сервера:**
```bash
pip install -r requirements.txt
python -m app.server            # слушает config.json → server.host:server.port (0.0.0.0:8000)
```

**Прошивка ESP32:**
```bash
python esp/gen_config.py    # config.json → esp/config.h
arduino-cli compile --fqbn esp32:esp32:esp32 esp/
arduino-cli upload -p /dev/cu.usbserial-XX --fqbn esp32:esp32:esp32 esp/
```

---

## Ключевые архитектурные решения

1. **Игровая логика изолирована** — `app/game_engine.py` не импортирует ничего сетевого; тестируется отдельно от сервера.
2. **In-memory состояние** — один глобальный `GameState`, без БД (теряется при рестарте сервера).
3. **ESP не блокируется на сети** — соленоид срабатывает по кэшу, HTTP-запросы уходят из `loop()`, а не из обработчика курка (иначе watchdog ронял плату).
4. **Патрон списывается один раз** — ESP-выстрел только выставляет `pending_shot`; фактический `shells.pop(0)` и урон — при выборе цели дилером.
5. **Секреты вне git** — `config.json` и `esp/config.h` (Wi-Fi пароль) игнорируются; в репозитории только `config.example.json`.
