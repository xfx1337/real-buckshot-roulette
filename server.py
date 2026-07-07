"""
Buckshot Roulette IRL — Web Server
FastAPI server with WebSocket for real-time updates.
Dealer dashboard + Player phone view.
"""

import asyncio
import json
import os
import copy
import time
from pathlib import Path
from contextlib import asynccontextmanager

from typing import Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from game_engine import (
    GameState, GamePhase, GameConfig, ItemType, ITEM_LABELS, ShellType,
    SOLO_DEFAULT_ROUNDS, MULTIPLAYER_DEFAULT_ROUNDS
)


# ── OpenAPI response models ──
# These describe response *shapes* for Swagger UI (/docs). The engine builds
# plain dicts at runtime (see game_engine.GameState.to_dict/player_view), so
# these models are documentation-only and intentionally lenient (extra
# fields are not rejected).

class OkResponse(BaseModel):
    ok: bool = True


class ErrorResponse(BaseModel):
    detail: str


class CreateGameResponse(BaseModel):
    ok: bool
    game_id: str
    game_mode: str


class ShootResponse(BaseModel):
    shell: str  # "live" | "blank"
    shooter: str
    target: str
    is_self_shot: bool
    damage: int
    target_hp_after: int
    extra_turn: bool


class UseItemResponse(BaseModel):
    ok: bool
    # Extra keys vary by item, e.g. "ejected", "shell", "display",
    # "stolen_item", "position", "total", "success" — see item docs below.


class ToggleShellsResponse(BaseModel):
    ok: bool
    show_shells: bool


class EspShellStatusResponse(BaseModel):
    ready: bool
    live: bool


class CurrentPlayerSummary(BaseModel):
    id: str
    name: str
    number: int


class PlayerPublic(BaseModel):
    id: str
    name: str
    number: int
    hp: int
    max_hp: int
    alive: bool
    connected: bool
    items: list[str]
    items_display: list[str]
    skip_next_turn: bool


class LogEntry(BaseModel):
    message: str
    type: str


class GameStateResponse(BaseModel):
    game_id: str
    phase: str
    game_mode: str
    current_round: int
    total_rounds: int
    shells_remaining: int
    saw_active: bool
    inverted: bool
    current_player: Optional[CurrentPlayerSummary] = None
    players: list[PlayerPublic]
    winner_id: Optional[str] = None
    winner_name: Optional[str] = None
    log: list[LogEntry]
    can_undo: Optional[bool] = None
    # Dealer-only fields (present when ?dealer=true): shells_sequence,
    # shells_display, dealt_items, last_burner_result, last_medicine_result,
    # last_magnify_result, live_count, blank_count, show_shells_to_players,
    # physical_magazine_limit, item_limits_global, item_limits_per_player.


class PlayerSummary(BaseModel):
    number: int
    name: str
    hp: int
    max_hp: int
    alive: bool
    handcuffed: bool


class PlayerStateResponse(BaseModel):
    game_id: str
    phase: str
    game_mode: str
    my_number: int
    my_name: str
    my_hp: int
    my_max_hp: int
    my_alive: bool
    my_handcuffed: bool
    is_my_turn: bool
    current_round: int
    total_rounds: int
    shells_remaining: int
    live_count: int
    blank_count: int
    saw_active: bool
    current_player_name: Optional[str] = None
    current_player_number: Optional[int] = None
    players_summary: list[PlayerSummary]
    winner_name: Optional[str] = None
    # Solo mode only: opponent_number, opponent_name, opponent_hp,
    # opponent_max_hp, opponent_alive, opponent_handcuffed, is_opponent_turn.

# ── Globals ──
game: GameState | None = None
undo_stack: list[GameState] = []
connected_clients: dict[str, list[WebSocket]] = {}  # "dealer" or player_id -> [ws]
dealer_ws_list: list[WebSocket] = []

def push_undo(state: GameState):
    global undo_stack
    undo_stack.append(state)
    if len(undo_stack) > 50:
        undo_stack.pop(0)


STATIC_DIR = Path(__file__).parent / "static"
TEMPLATE_DIR = Path(__file__).parent / "templates"


@asynccontextmanager
async def lifespan(app: FastAPI):
    os.makedirs(STATIC_DIR, exist_ok=True)
    os.makedirs(TEMPLATE_DIR, exist_ok=True)
    yield

app = FastAPI(
    title="Buckshot Roulette IRL",
    description=(
        "Сервер для реальной (IRL) игры Buckshot Roulette: дилер управляет партией через "
        "веб-панель, игроки следят за своим HP/предметами с телефона, опциональный ESP32 "
        "опрашивает статус патрона для физического триггера.\n\n"
        "В приложении ровно одна активная игра (глобальное состояние в памяти процесса, "
        "без БД). Создание новой игры (`POST /api/create_game`) полностью заменяет предыдущую.\n\n"
        "Реалтайм-обновления состояния идут через WebSocket (`/ws/dealer`, `/ws/player/{player_id}`); "
        "REST-эндпоинты ниже дублируют то же состояние через `GET /api/state` и `GET /api/player_state/{id}` "
        "для первичной загрузки/поллинга."
    ),
    version="1.0.0",
    lifespan=lifespan,
    openapi_tags=[
        {"name": "Game Management", "description": "Создание игры, присоединение игроков, старт, изменение конфигурации."},
        {"name": "Dealer Actions", "description": "Действия дилера в ходе партии: выстрелы, предметы, HP, раунды, undo."},
        {"name": "State", "description": "Чтение текущего состояния игры (тот же снимок, что рассылается по WebSocket)."},
        {"name": "ESP32", "description": "Read-only эндпоинт для физического триггера на базе ESP32."},
        {"name": "Pages", "description": "HTML-страницы (сервер-рендеринг через Jinja2), не являются JSON API."},
    ],
)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
templates = Jinja2Templates(directory=str(TEMPLATE_DIR))


# ── Broadcast ──

async def broadcast_state():
    """Send updated game state to all connected clients."""
    if not game:
        return
    # Dealer
    dealer_dict = game.to_dict(for_dealer=True)
    dealer_dict["can_undo"] = len(undo_stack) > 0
    dealer_data = json.dumps(dealer_dict, ensure_ascii=False)
    dead_ws = []
    for ws in dealer_ws_list:
        try:
            await ws.send_text(dealer_data)
        except Exception:
            dead_ws.append(ws)
    for ws in dead_ws:
        dealer_ws_list.remove(ws)

    # Players
    for pid, ws_list in list(connected_clients.items()):
        if pid == "dealer":
            continue
        player_data = json.dumps(game.player_view(pid), ensure_ascii=False)
        dead = []
        for ws in ws_list:
            try:
                await ws.send_text(player_data)
            except Exception:
                dead.append(ws)
        for ws in dead:
            ws_list.remove(ws)


# ── Pages ──

@app.get("/", response_class=HTMLResponse, tags=["Pages"], summary="Стартовая страница", include_in_schema=False)
async def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


@app.get("/dealer", response_class=HTMLResponse, tags=["Pages"], summary="Панель дилера", include_in_schema=False)
async def dealer_page(request: Request):
    return templates.TemplateResponse("dealer.html", {"request": request, "game": game})


@app.get("/player/{player_id}", response_class=HTMLResponse, tags=["Pages"], summary="Экран игрока", include_in_schema=False)
async def player_page(request: Request, player_id: str):
    if not game or player_id not in game.players:
        return templates.TemplateResponse("join.html", {"request": request, "error": "Игра не найдена или вы не зарегистрированы"})
    p = game.players[player_id]
    return templates.TemplateResponse("player.html", {"request": request, "player": p})


@app.get("/join", response_class=HTMLResponse, tags=["Pages"], summary="Форма присоединения к игре", include_in_schema=False)
async def join_page(request: Request):
    return templates.TemplateResponse("join.html", {"request": request, "error": None})


# ── API: Game Management ──

@app.post(
    "/api/create_game",
    tags=["Game Management"],
    summary="Создать новую игру",
    description=(
        "Создаёт новую игру и **заменяет** текущую глобальную партию (если есть), "
        "очищая историю undo. Игра стартует в фазе `lobby`, игроки ещё не добавлены."
    ),
    response_model=CreateGameResponse,
)
async def create_game(
    game_mode: str = Form("multiplayer", description="Режим игры: `multiplayer` (2-4 игрока) или `solo` (1 на 1 с виртуальным DEALER)"),
):
    global game, undo_stack
    game = GameState()
    game.config.game_mode = game_mode
    if game_mode == "solo":
        game.config.rounds = [dict(r) for r in SOLO_DEFAULT_ROUNDS]
    undo_stack.clear()
    await broadcast_state()
    return {"ok": True, "game_id": game.game_id, "game_mode": game_mode}


@app.post(
    "/api/join",
    tags=["Game Management"],
    summary="Присоединиться к игре",
    description=(
        "Регистрирует нового игрока в текущей игре (только пока фаза `lobby`, максимум 4 игрока). "
        "Это HTML-form эндпоинт для страницы `/join`: при успехе делает редирект (303) на "
        "`/player/{player_id}`, при ошибке возвращает HTML-страницу `join.html` с сообщением об ошибке "
        "(а не JSON/HTTP-ошибку)."
    ),
    response_class=HTMLResponse,
)
async def join_game(request: Request, name: str = Form(..., description="Отображаемое имя игрока")):
    global game
    if not game:
        # Instead of 400 error, return to join page with error
        return templates.TemplateResponse("join.html", {
            "request": request,
            "error": "Игра не создана. Подождите, пока дилер создаст игру.",
        })
    try:
        player = game.add_player(name)
        await broadcast_state()
        return RedirectResponse(f"/player/{player.id}", status_code=303)
    except ValueError as e:
        return templates.TemplateResponse("join.html", {
            "request": request,
            "error": str(e),
        })


@app.post(
    "/api/start_game",
    tags=["Game Management"],
    summary="Начать игру",
    description=(
        "Переводит игру из `lobby` в первый раунд (генерирует патроны, назначает порядок ходов). "
        "Для `multiplayer` нужно минимум 2 игрока; для `solo` — 1 игрок (тогда автоматически "
        "добавляется виртуальный оппонент `DEALER`) либо ровно 2."
    ),
    response_model=OkResponse,
    responses={400: {"model": ErrorResponse, "description": "Игра не создана или не хватает игроков"}},
)
async def start_game():
    if not game:
        raise HTTPException(400, "Игра не создана")
    prev = copy.deepcopy(game)
    try:
        game.start_game()
        push_undo(prev)
        await broadcast_state()
        return {"ok": True}
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post(
    "/api/confirm_shells",
    tags=["Dealer Actions"],
    summary="Подтвердить физическую зарядку патронов",
    description=(
        "Дилер подтверждает, что физически зарядил дробовик сгенерированными патронами. "
        "Из фазы `dealer_reloading` переводит сразу в `player_turn` (дозарядка магазина); "
        "из фазы `dealer_loading` — в `dealer_items` (если раунду положены предметы) либо "
        "сразу в `player_turn`."
    ),
    response_model=OkResponse,
    responses={400: {"model": ErrorResponse, "description": "Игра не создана"}},
)
async def confirm_shells():
    if not game:
        raise HTTPException(400, "Игра не создана")
    prev = copy.deepcopy(game)
    game.confirm_shells_loaded()
    push_undo(prev)
    await broadcast_state()
    return {"ok": True}


@app.post(
    "/api/confirm_items",
    tags=["Dealer Actions"],
    summary="Подтвердить раздачу предметов",
    description="Дилер подтверждает, что физически раздал игрокам предметы. Переводит фазу `dealer_items` в `player_turn`.",
    response_model=OkResponse,
    responses={400: {"model": ErrorResponse, "description": "Игра не создана"}},
)
async def confirm_items():
    if not game:
        raise HTTPException(400, "Игра не создана")
    prev = copy.deepcopy(game)
    game.confirm_items_dealt()
    push_undo(prev)
    await broadcast_state()
    return {"ok": True}


@app.post(
    "/api/shoot",
    tags=["Dealer Actions"],
    summary="Выстрелить в цель",
    description=(
        "Дилер регистрирует выстрел текущего игрока (по очереди хода) в `target_id` (может "
        "совпадать со стрелком — выстрел в себя). Извлекает патрон из магазина, применяет "
        "урон/эффекты (пила x2, инвертор), логирует событие и продвигает ход. "
        "Холостой в себя даёт дополнительный ход (`extra_turn: true`)."
    ),
    response_model=ShootResponse,
    responses={400: {"model": ErrorResponse, "description": "Не фаза стрельбы, нет патронов или неверная цель"}},
)
async def shoot(target_id: str = Form(..., description="ID игрока-цели")):
    if not game:
        raise HTTPException(400, "Игра не создана")
    prev = copy.deepcopy(game)
    try:
        result = game.shoot(target_id)
        push_undo(prev)
        await broadcast_state()
        return result
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post(
    "/api/use_item",
    tags=["Dealer Actions"],
    summary="Использовать предмет",
    description="""\
Дилер регистрирует использование предмета игроком `player_id`. Набор полей ответа
и обязательность `target_id`/`stolen_item` зависят от значения `item`:

| `item` | требует | эффект | доп. поля ответа |
|---|---|---|---|
| `beer` | — | выбрасывает текущий патрон без выстрела | `ejected`: `"live"` \\| `"blank"` |
| `handsaw` | — | следующий выстрел этого игрока x2 урона | — |
| `handcuffs` | `target_id` | цель пропускает следующий ход | — |
| `magnifying_glass` | — | подсматривает текущий патрон (виден только дилеру) | `shell`, `display` |
| `cigarettes` | — | +1 HP (не выше `max_hp`) | — |
| `adrenaline` | `target_id`, `stolen_item` | крадёт предмет у цели и сразу использует его | `stolen_item` |
| `burner_phone` | — | раскрывает случайный патрон в магазине (для дилера) | `position`, `total`, `shell`, `display` |
| `inverter` | — | инвертирует текущий патрон (live↔blank) | — |
| `medicine_vodka` | — | исход "лекарства": +2 HP | `success`, `display` |
| `medicine_water` | — | исход "лекарства": -1 HP (может выбить игрока) | `success`, `display` |

Все варианты также возвращают `ok: true`.
""",
    response_model=UseItemResponse,
    responses={400: {"model": ErrorResponse, "description": "Игра не создана, нет предмета/патронов, неверная цель или неизвестный item"}},
)
async def use_item(
    player_id: str = Form(..., description="ID игрока, использующего предмет"),
    item: str = Form(..., description="Тип предмета: beer, handsaw, handcuffs, magnifying_glass, cigarettes, adrenaline, burner_phone, inverter, medicine_vodka, medicine_water"),
    target_id: str = Form(None, description="ID цели — обязателен для handcuffs и adrenaline"),
    stolen_item: str = Form(None, description="Тип похищаемого предмета — обязателен для adrenaline"),
):
    if not game:
        raise HTTPException(400, "Игра не создана")
    prev = copy.deepcopy(game)
    try:
        result = {}
        if item == "beer":
            result = game.use_item_beer(player_id)
        elif item == "handsaw":
            game.use_item_handsaw(player_id)
        elif item == "handcuffs":
            if not target_id:
                raise ValueError("Нужно выбрать цель для наручников")
            game.use_item_handcuffs(player_id, target_id)
        elif item == "magnifying_glass":
            result = game.use_item_magnifying_glass(player_id)
        elif item == "cigarettes":
            game.use_item_cigarettes(player_id)
        elif item == "adrenaline":
            if not target_id or not stolen_item:
                raise ValueError("Нужно выбрать цель и предмет для адреналина")
            result = game.use_item_adrenaline(player_id, target_id, stolen_item)
        elif item == "burner_phone":
            result = game.use_item_burner_phone(player_id)
        elif item == "inverter":
            game.use_item_inverter(player_id)
        elif item == "medicine_vodka":
            result = game.use_item_expired_medicine(player_id, is_vodka=True)
        elif item == "medicine_water":
            result = game.use_item_expired_medicine(player_id, is_vodka=False)
        else:
            raise ValueError(f"Неизвестный предмет: {item}")

        push_undo(prev)
        await broadcast_state()
        return {"ok": True, **result}
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post(
    "/api/adjust_hp",
    tags=["Dealer Actions"],
    summary="Вручную изменить HP игрока",
    description="Дилер напрямую корректирует HP игрока на `delta` (может быть отрицательным). Значение зажимается в `[0, max_hp]`; при падении до 0 игрок выбывает.",
    response_model=OkResponse,
    responses={400: {"model": ErrorResponse, "description": "Игра не создана"}},
)
async def adjust_hp(
    player_id: str = Form(..., description="ID игрока"),
    delta: int = Form(..., description="Изменение HP, например -1 или +2"),
):
    if not game:
        raise HTTPException(400, "Игра не создана")
    prev = copy.deepcopy(game)
    game.dealer_adjust_hp(player_id, delta)
    push_undo(prev)
    await broadcast_state()
    return {"ok": True}


@app.post(
    "/api/force_end",
    tags=["Dealer Actions"],
    summary="Принудительно завершить игру",
    description="Дилер немедленно переводит игру в фазу `game_over`, вне зависимости от текущего состояния.",
    response_model=OkResponse,
    responses={400: {"model": ErrorResponse, "description": "Игра не создана"}},
)
async def force_end():
    if not game:
        raise HTTPException(400, "Игра не создана")
    prev = copy.deepcopy(game)
    game.force_end_game()
    push_undo(prev)
    await broadcast_state()
    return {"ok": True}


@app.post(
    "/api/force_round_over",
    tags=["Dealer Actions"],
    summary="Принудительно завершить раунд",
    description="Дилер немедленно переводит текущий раунд в фазу `round_over`, не дожидаясь естественного завершения.",
    response_model=OkResponse,
    responses={400: {"model": ErrorResponse, "description": "Игра не создана"}},
)
async def api_force_round_over():
    if not game:
        raise HTTPException(400, "Игра не создана")
    prev = copy.deepcopy(game)
    game.force_round_over()
    push_undo(prev)
    await broadcast_state()
    return {"ok": True}


@app.post(
    "/api/clear_special",
    tags=["Dealer Actions"],
    summary="Очистить всплывающие результаты предметов",
    description=(
        "Сбрасывает `last_magnify_result`, `last_burner_result` и `last_medicine_result` "
        "(разовые подсказки для дилера от лупы/телефона/лекарства) после того, как дилер их прочитал. "
        "Не влияет на состояние игры, если игра ещё не создана."
    ),
    response_model=OkResponse,
)
async def clear_special():
    if game:
        game.last_magnify_result = None
        game.last_burner_result = None
        game.last_medicine_result = None
    return {"ok": True}


@app.post(
    "/api/toggle_shells",
    tags=["Dealer Actions"],
    summary="Переключить видимость патронов для игроков",
    description="Включает/выключает показ количества боевых/холостых патронов игрокам во время фазы `dealer_loading`.",
    response_model=ToggleShellsResponse,
    responses={400: {"model": ErrorResponse, "description": "Игра не создана"}},
)
async def toggle_shells():
    if not game:
        raise HTTPException(400, "Игра не создана")
    game.show_shells_to_players = not game.show_shells_to_players
    await broadcast_state()
    return {"ok": True, "show_shells": game.show_shells_to_players}


@app.post(
    "/api/next_round",
    tags=["Dealer Actions"],
    summary="Перейти к следующему раунду",
    description=(
        "Из фазы `round_over` запускает следующий раунд конфигурации (`GameConfig.rounds`). "
        "Если раундов больше нет, определяет победителя (если остался ровно один живой игрок) "
        "и переводит игру в `game_over`."
    ),
    response_model=OkResponse,
    responses={400: {"model": ErrorResponse, "description": "Игра не создана"}},
)
async def next_round():
    if not game:
        raise HTTPException(400, "Игра не создана")
    prev = copy.deepcopy(game)
    game.next_round()
    push_undo(prev)
    await broadcast_state()
    return {"ok": True}


@app.post(
    "/api/remove_player",
    tags=["Game Management"],
    summary="Удалить игрока из игры",
    description="Помечает игрока как выбывшего и отключённого (используется дилером, например при выходе игрока). Может завершить раунд, если остался один живой игрок.",
    response_model=OkResponse,
    responses={400: {"model": ErrorResponse, "description": "Игра не создана"}},
)
async def remove_player(player_id: str = Form(..., description="ID игрока для удаления")):
    if not game:
        raise HTTPException(400, "Игра не создана")
    prev = copy.deepcopy(game)
    game.remove_player(player_id)
    push_undo(prev)
    await broadcast_state()
    return {"ok": True}

@app.post(
    "/api/undo",
    tags=["Dealer Actions"],
    summary="Отменить последнее действие",
    description=(
        "Откатывает состояние игры к снапшоту перед последним мутирующим действием дилера "
        "(история хранит до 50 шагов). Полезно при ошибке дилера (например, выстрел не в ту цель)."
    ),
    response_model=OkResponse,
    responses={400: {"model": ErrorResponse, "description": "Игра не создана или стек отмены пуст"}},
)
async def undo_action():
    global game, undo_stack
    if not game:
        raise HTTPException(400, "Игра не создана")
    if not undo_stack:
        raise HTTPException(400, "Нечего отменять")
    game = undo_stack.pop()
    from game_engine import GameEvent
    game.event_log.append(GameEvent(time.time(), ">> ДЕЙСТВИЕ ОТМЕНЕНО ДИЛЕРОМ", "system"))
    await broadcast_state()
    return {"ok": True}


@app.post(
    "/api/update_config",
    tags=["Game Management"],
    summary="Обновить конфигурацию игры",
    description="""\
Обновляет настройки игры из меню дилера. Разрешено только в фазе `lobby`.
Тело — JSON-строка (form-поле `config_json`) с любым подмножеством ключей:

- `game_mode`: `"solo"` \\| `"multiplayer"` — при смене режима подставляет дефолтные раунды для этого режима
- `rounds`: список `{hp, items_per_player, max_shells}` по раундам (переопределяет дефолты)
- `item_limits_global`: `{item_type: max_count}` — лимит копий предмета одновременно на столе
- `item_limits_per_player`: `{item_type: max_count}` — лимит копий предмета у одного игрока
- `show_shells_to_players`: bool — показывать ли игрокам число патронов при зарядке
- `physical_magazine_limit`: int — сколько патронов физически помещается в дробовик за раз (0 = без лимита)
""",
    response_model=OkResponse,
    responses={400: {"model": ErrorResponse, "description": "Игра не создана, не фаза lobby, либо некорректный JSON"}},
)
async def update_config(config_json: str = Form(..., description="JSON-объект с настройками, см. описание эндпоинта")):
    """Update game config from dealer menu (only in lobby)."""
    if not game:
        raise HTTPException(400, "Игра не создана")
    if game.phase != GamePhase.LOBBY:
        raise HTTPException(400, "Конфигурацию можно менять только в лобби")
    try:
        data = json.loads(config_json)
        if "game_mode" in data:
            game.config.game_mode = data["game_mode"]
            if data["game_mode"] == "solo":
                game.config.rounds = [dict(r) for r in SOLO_DEFAULT_ROUNDS]
            else:
                game.config.rounds = [dict(r) for r in MULTIPLAYER_DEFAULT_ROUNDS]
        if "rounds" in data:
            game.config.rounds = data["rounds"]
        if "item_limits_global" in data:
            game.config.item_limits_global = {k: int(v) for k, v in data["item_limits_global"].items()}
        if "item_limits_per_player" in data:
            game.config.item_limits_per_player = {k: int(v) for k, v in data["item_limits_per_player"].items()}
        if "show_shells_to_players" in data:
            game.show_shells_to_players = data["show_shells_to_players"]
        if "physical_magazine_limit" in data:
            game.config.physical_magazine_limit = data["physical_magazine_limit"]
        await broadcast_state()
        return {"ok": True}
    except Exception as e:
        raise HTTPException(400, str(e))


@app.get(
    "/api/state",
    tags=["State"],
    summary="Получить текущее состояние игры",
    description=(
        "Возвращает полное состояние игры. С `?dealer=true` включает приватные данные "
        "(предметы игроков, порядок патронов, результаты лупы/телефона/лекарства, лимиты) — "
        "предназначено только для дашборда дилера. Без флага возвращает публичную версию "
        "(без содержимого чужих предметов и очереди патронов). Это тот же снимок, что рассылается по WebSocket."
    ),
    response_model=None,
)
async def get_state(dealer: bool = False) -> GameStateResponse | dict:
    if not game:
        return {"phase": "no_game"}
    if dealer:
        data = game.to_dict(for_dealer=True)
        data["can_undo"] = len(undo_stack) > 0
        return data
    return game.to_dict(for_dealer=False)


@app.get(
    "/api/player_state/{player_id}",
    tags=["State"],
    summary="Получить состояние для конкретного игрока",
    description=(
        "Возвращает минимальный вид состояния для телефона конкретного игрока: его HP, предметы, "
        "чей ход, и (в solo-режиме) HP оппонента. Это тот же снимок, что рассылается по "
        "`/ws/player/{player_id}`."
    ),
    response_model=None,
)
async def get_player_state(player_id: str) -> PlayerStateResponse | dict:
    if not game:
        return {"phase": "no_game"}
    return game.player_view(player_id)


# ── API: ESP32 physical trigger ──

@app.get(
    "/api/esp/shell_status",
    tags=["ESP32"],
    summary="Статус следующего патрона (для физического триггера)",
    description=(
        "Read-only статус патрона, который выстрелит следующим — для соленоидного триггера ESP32. "
        "Предназначен для непрерывного опроса (например, раз в секунду), чтобы устройство всегда "
        "имело свежий закэшированный ответ перед физическим спуском. **Не потребляет патрон и не "
        "изменяет состояние игры** — фактический выстрел по-прежнему выполняется только действием "
        "дилера `POST /api/shoot` в веб-интерфейсе.\n\n"
        "`ready=false`, если сейчас не фаза `player_turn` или магазин пуст."
    ),
    response_model=EspShellStatusResponse,
)
async def esp_shell_status():
    """
    Read-only status of the shell currently up next, for the ESP32 solenoid
    trigger. Polled continuously (e.g. every second) so the device always
    has a fresh answer cached locally before the physical trigger is pulled.
    Does not consume a shell or affect game state — the dealer's own
    "Shoot" action in the web UI remains the only thing that fires a shot.
    """
    if not game:
        return {"ready": False, "live": False}
    return game.esp_shell_status()


# ── WebSockets ──
#
# WebSocket-эндпоинты не отображаются в OpenAPI/Swagger (спецификация OpenAPI 3.0
# не описывает WS), поэтому их протокол задокументирован здесь текстом:
#
# GET/WS /ws/dealer
#   Без входящих сообщений от клиента (кроме keep-alive пингов, которые игнорируются).
#   При подключении и на каждое изменение состояния сервер шлёт JSON-сообщение —
#   тот же формат, что и `GET /api/state?dealer=true` (включая поле `can_undo`).
#
# GET/WS /ws/player/{player_id}
#   Помечает игрока подключённым (`connected=true`) на время жизни сокета.
#   При подключении и на каждое изменение состояния сервер шлёт JSON-сообщение —
#   тот же формат, что и `GET /api/player_state/{player_id}`.

@app.websocket("/ws/dealer")
async def ws_dealer(ws: WebSocket):
    await ws.accept()
    dealer_ws_list.append(ws)
    try:
        if game:
            d = game.to_dict(for_dealer=True)
            d["can_undo"] = len(undo_stack) > 0
            await ws.send_text(json.dumps(d, ensure_ascii=False))
        while True:
            await ws.receive_text()  # Keep alive
    except WebSocketDisconnect:
        pass
    finally:
        if ws in dealer_ws_list:
            dealer_ws_list.remove(ws)


@app.websocket("/ws/player/{player_id}")
async def ws_player(ws: WebSocket, player_id: str):
    await ws.accept()
    if player_id not in connected_clients:
        connected_clients[player_id] = []
    connected_clients[player_id].append(ws)
    if game and player_id in game.players:
        game.players[player_id].connected = True
    try:
        if game:
            await ws.send_text(json.dumps(game.player_view(player_id), ensure_ascii=False))
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        if player_id in connected_clients and ws in connected_clients[player_id]:
            connected_clients[player_id].remove(ws)


# ── Run ──

if __name__ == "__main__":
    import uvicorn
    from config import SERVER_HOST, SERVER_PORT
    uvicorn.run("server:app", host=SERVER_HOST, port=SERVER_PORT, reload=True)
