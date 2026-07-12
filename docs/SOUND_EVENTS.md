# Sound Events — карта озвучки

Полный список игровых событий для sound engine. Источник — `game_engine.py._log()` (`event_type`) + переходы `GamePhase`. Всё летит клиенту через единый WebSocket-снапшот `server.py:257 broadcast_state()` в поле `log[]` (последние 30 записей) и `phase`.

**Механизм детекта на клиенте:**
1. Диффить `phase` — фазовые звуки.
2. Сравнивать хвост `log[]` с прошлым состоянием — точечные звуки (по `type` + парсинг `message`).
3. Фоновый ambient по текущей `phase`.

Различимость (боевой/холостой, водка/вода) требует парсинга текста `message` — либо добавить явные event-поля в state.

**Аудио-ассеты.** Пути в колонке «Аудио» относительны базы:
`reference/Buckshot Roulette/`
Файлы из оригинала Buckshot Roulette (`.ogg` / `.wav`). `mp_*` — из мультиплеерного набора (`multiplayer/audio/...`). При деплое скопировать нужные в `app/static/audio/`.

---

## A. Фазы игры (переходы `GamePhase`)

| key | Событие | Источник | Аудио |
|---|---|---|---|
| `player_join` | Игрок присоединился | `game_engine.py:186` | `audio/kick door enter lobby.ogg` |
| `game_start` | Старт игры | `game_engine.py:202` | `audio/button_start main.ogg` |
| `round_start` | Начало раунда (`=== РАУНД N ===`) | `game_engine.py:229` | `audio/round blinker wave.ogg` |
| `dealer_loading` | Дробовик заряжен | `game_engine.py:361` | `audio/load single shell.ogg` |
| `dealer_reloading` | Требуется дозарядка | `game_engine.py:575` | `audio/rack shotgun.ogg` |
| `dealer_items` | Раздача предметов | `game_engine.py:374` | `audio/open briefcase.ogg` |
| `turn_start` | Начало хода игрока | `game_engine.py:451` / `558` | `multiplayer/audio/main audio/mp_display_turn order bootup1.ogg` |
| `round_win` | Раунд выигран | `game_engine.py:581` | `audio/winner.ogg` |
| `round_draw` | Ничья в раунде | `game_engine.py:583` | `audio/round indicator shut down.ogg` |
| `game_over` | Победитель / конец игры | `game_engine.py:830` / `833` | `audio/playtest win.ogg` |

## B. Выстрелы (`type:"shot"`)

| key | Событие | Источник | Аудио |
|---|---|---|---|
| `trigger_pull` | Физический курок нажат, ждём цель (`[КУРОК]`) | `game_engine.py:490` | `audio/gun foley1.ogg` |
| `shot_live` | БОЕВОЙ попал −1 HP | `game_engine.py:641` | `audio/temp gunshot_live.wav` |
| `shot_live_saw` | БОЕВОЙ x2 (пила) −2 HP | `game_engine.py:634`+`641` | `multiplayer/audio/main audio/mp_gun fire5.ogg` |
| `shot_blank` | ХОЛОСТОЙ | `game_engine.py:659` | `audio/temp gunshot_blank.wav` |
| `blank_self_extra` | Холостой в себя → доп. ход | `game_engine.py:666` | `multiplayer/audio/main audio/mp_dry fire1.wav` |
| `player_dead` | Игрок выбыл | `game_engine.py:647` | `audio/splatter1.ogg` |
| `ammo_empty` | Капсюли кончились | `game_engine.py:632` / `511` | `multiplayer/audio/main audio/mp_dry fire0.wav` |
| `revolver_reload` | Револьвер перезаряжен | `game_engine.py:503` | `audio/rack shotgun.ogg` |
| `new_magazine` | Новый магазин (все патроны расстреляны) | `game_engine.py:567` | `audio/shell latch1.ogg` |

## C. Предметы (`type:"item"`)

| key | Предмет | Эффект/звук | Источник | Аудио |
|---|---|---|---|---|
| `item_beer` | Пиво | выброс патрона | `game_engine.py:688` | `audio/player use beer.ogg` |
| `item_handsaw` | Пила | следующий выстрел x2 | `game_engine.py:696` | `audio/player use handsaw.ogg` |
| `item_handcuffs` | Наручники | цель пропустит ход | `game_engine.py:705` | `audio/player use handcuffs.ogg` |
| `item_magnify` | Лупа | подсмотр патрона | `game_engine.py:716` | `audio/player use magnifier.ogg` |
| `item_cigarettes` | Сигареты | +1 HP | `game_engine.py:731` | `audio/player use cigarettes.ogg` |
| `item_cigarettes_blocked` | Сигареты | нет эффекта (story stage3) | `game_engine.py:725` | `audio/health counter error1.ogg` |
| `item_adrenaline` | Адреналин | кража предмета | `game_engine.py:743` | `audio/player use adrenaline.ogg` |
| `item_phone_hint` | Телефон | подсказка по патрону | `game_engine.py:762` | `audio/player use burner phone.ogg` |
| `item_phone_silence` | Телефон | тишина (мало патронов) | `game_engine.py:754` | `audio/error vol3.ogg` |
| `item_inverter` | Инвертор | инверсия патрона | `game_engine.py:769` | `audio/player use inverter.ogg` |
| `item_medicine_vodka` | Лекарство → Водка | +2 HP удача | `game_engine.py:779` | `audio/player use medicine.ogg` |
| `item_medicine_water` | Лекарство → Вода | −1 HP неудача | `game_engine.py:784` | `audio/health counter reduce health.ogg` |
| `item_medicine_death` | Смерть от лекарства | выбыл | `game_engine.py:787` | `audio/player death medicine.ogg` |
| `item_dealt` | Предметы выданы игроку | `game_engine.py:441` | `audio/main compartment show items.ogg` |
| `handcuff_skip` | Пропуск хода (наручники) | `game_engine.py:548` | `audio/player check handcuffs.ogg` |

## D. Действия дилера / системные (`type:"system"`)

| key | Событие | Источник | Аудио |
|---|---|---|---|
| `hp_adjust` | Дилер изменил HP | `game_engine.py:801` | `audio/health counter beep2.wav` |
| `hp_adjust_death` | Игрок выбыл от правки HP | `game_engine.py:804` | `audio/splatter1.ogg` |
| `confirm_shells` | Подтвердил заряд | `server.py:677` | `audio/shell latch2.ogg` |
| `confirm_items` | Подтвердил раздачу | `server.py:695` | `audio/briefcase latch1.ogg` |
| `toggle_shells` | Переключил показ патронов | `server.py:907` | `audio/crt_part click.ogg` |
| `force_end_game` | Force-end игры | `game_engine.py:814` | `audio/crt_turn off display2.ogg` |
| `force_round_over` | Force-end раунда | `game_engine.py:819` | `audio/round indicator shut down.ogg` |
| `next_round` | Следующий раунд | `server.py:929` | `audio/round blinker wave.ogg` |
| `undo` | Отмена действия | `server.py:970` | `audio/button press2.ogg` |
| `player_leave` | Игрок покинул игру | `game_engine.py:194` | `multiplayer/audio/misc audio/mp_beep exit.ogg` |

## E. UI / веб (клиентские, звук в браузере)

| key | Событие | Аудио |
|---|---|---|
| `ui_click` | Клик кнопки | `audio/button_press.ogg` |
| `ui_settings_open` | Открытие меню настроек | `audio/crt_show icons.ogg` |
| `ui_settings_close` | Закрытие меню настроек | `audio/crt_turn off display2.ogg` |
| `ui_join` | Join / create игры | `audio/check item_pickup.ogg` |
| `ui_tab_switch` | Переключение вкладок экрана | `audio/button_hover.ogg` |

## F. «Отсутствие события» (ambient / тишина, loop)

| key | Состояние | Условие | Аудио |
|---|---|---|---|
| `ambient_lobby` | Лобби, ждём игроков | `phase == lobby` | `audio/club ambience1.ogg` |
| `ambient_idle_turn` | Игрок думает, ход не сделан | `phase == player_turn`, простой | `multiplayer/audio/music/mp_music desolate loop2.ogg` |
| `ambient_pending` | Курок нажат, дилер не выбрал цель (напряжение) | `pending_shot == true` | `audio/heartbeat effect.ogg` |
| `ambient_between_rounds` | Пауза между раундами | `round_over` → `next_round` | `multiplayer/audio/music/mp_music resolve.ogg` |
| `ambient_loading` | Дилер заряжает/дозаряжает | `dealer_loading` / `dealer_reloading` | `audio/ambience_fluorescent light.ogg` |

## G. Фоновая музыка (главный loop, опционально)

| key | Когда | Аудио |
|---|---|---|
| `bgm_main` | Активная игра (фон) | `audio/music/music main_techno techno.ogg` |
| `bgm_main_loop` | Продолжение | `audio/music/music main second loop_techno techno.ogg` |
| `bgm_death` | Экран смерти/поражения | `audio/music_true death vol1.ogg` |

---

## Архитектура (для реализации)

- **Единый bus:** `server.py:257 broadcast_state()` — все изменения состояния.
- **Логгер событий:** `game_engine.py:172 _log(msg, event_type)` — каждое действие с типом `info | shot | item | round | system`.
- **WS-эндпоинты:** `/ws/dealer` (`server.py:1169`, полный стейт), `/ws/player/{id}` (`server.py:1187`, вид игрока).
- **Sound-relevant поля стейта:** `phase`, `pending_shot`, `saw_active`, `inverted`, `winner_id`, `log[]`.
- **Выделенного звукового канала нет** — клиент парсит `log[].type` + `message`.
- **Ассеты:** оригинал в `reference/Buckshot Roulette/audio/` (+ `multiplayer/audio/`). Для веба скопировать выбранные в `app/static/audio/` и грузить через `<audio>` / Web Audio API.

**Итого: ~50 различимых событий** для озвучки + ambient + BGM.
