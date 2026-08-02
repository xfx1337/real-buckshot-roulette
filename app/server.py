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

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, Form, HTTPException, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from app.game_engine import (
    GameState, GamePhase, GameConfig, ItemType, ITEM_LABELS, ShellType,
    SOLO_DEFAULT_ROUNDS, STORY_DEFAULT_ROUNDS, STORY_ONE_ROUND_DEFAULT_ROUNDS,
    MULTIPLAYER_DEFAULT_ROUNDS
)
from app import sound_config
from app import video_config

import socket
from urllib.parse import quote, unquote
from app import config as app_config

# ── Идентичность игрока через cookie ──────────────────────────────────────
# Чтобы игрок не плодил дубли при потере связи / кнопке «назад» / случайном
# уходе на главную, его личность (id + имя) хранится в долгоживущих cookie.
# id привязан к КОНКРЕТНОЙ игре (при новой игре старый id перестаёт совпадать
# с game.players), а имя переживает смену игры — чтобы предзаполнить форму join.
COOKIE_PLAYER_ID = "bsr_player_id"
COOKIE_PLAYER_NAME = "bsr_player_name"
COOKIE_MAX_AGE = 60 * 60 * 12  # 12 часов — с запасом на одну игровую сессию


def _set_player_cookies(resp, player_id: str, name: str) -> None:
    """Проставляет cookie идентичности на ответе (redirect/HTML). SameSite=Lax,
    http-доступ (не Secure) — игра крутится по локальному http, не https.

    Имя URL-кодируем: HTTP-заголовки (а cookie — это Set-Cookie header) должны
    быть latin-1, а имена бывают кириллическими/эмодзи. quote() превращает их в
    ASCII-безопасный %-escape; при чтении разворачиваем через _get_cookie_name."""
    resp.set_cookie(COOKIE_PLAYER_ID, player_id, max_age=COOKIE_MAX_AGE,
                    samesite="lax", httponly=False)
    resp.set_cookie(COOKIE_PLAYER_NAME, quote(name), max_age=COOKIE_MAX_AGE,
                    samesite="lax", httponly=False)


def _get_cookie_name(request: Request) -> str:
    """Читает и раскодирует имя игрока из cookie (обратное к quote() в
    _set_player_cookies). Пустая строка, если куки нет."""
    raw = request.cookies.get(COOKIE_PLAYER_NAME, "")
    return unquote(raw) if raw else ""


def _known_player_from_cookie(request: Request):
    """Возвращает Player из текущей игры, если cookie указывает на существующего
    в ней игрока; иначе None. Используется, чтобы вернуть потерявшегося игрока на
    его же экран без повторного ввода имени."""
    if not game:
        return None
    pid = request.cookies.get(COOKIE_PLAYER_ID)
    if pid and pid in game.players:
        return game.players[pid]
    return None


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
    fire: bool = False     # одноразовая команда дилера «щёлкни соленоидом»
    pending: bool = False  # выстрел был, дилер ещё не выбрал цель (соленоид заблокирован)


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
showscreen_ws_list: list[WebSocket] = []
tv_ws_list: list[WebSocket] = []  # TV video overlay WebSocket connections

# ── TV Video state ──
# Current video command being broadcast to all TV screens.
tv_video_state: dict = {"action": "idle", "video": None, "loop": False}

async def notify_showscreen(message: str, player_name: str, item: str):
    if not showscreen_ws_list:
        return
    data = json.dumps({"message": message, "player_name": player_name, "item": item}, ensure_ascii=False)
    dead = []
    for ws in showscreen_ws_list:
        try:
            await ws.send_text(data)
        except Exception:
            dead.append(ws)
    for ws in dead:
        showscreen_ws_list.remove(ws)

# Команда «принудительно щёлкнуть соленоидом» от дилера к плате. Связь с ESP32
# односторонняя (плата опрашивает сервер), поэтому команду кладём сюда, а плата
# забирает её флагом fire=true в ближайшем ответе /api/esp/shell_status и
# сбрасывает. Одноразовый импульс: выставили → плата один раз выстрелила.
esp_force_fire: bool = False

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
os.makedirs(str(video_config.VIDEOS_DIR), exist_ok=True)
app.mount("/videos", StaticFiles(directory=str(video_config.VIDEOS_DIR)), name="videos")
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
    # Если игрок уже в текущей игре (cookie указывает на живого) — не показываем
    # форму заново, а возвращаем его на свой экран. Так «назад» на главную или
    # потеря вкладки не превращаются в повторную регистрацию.
    known = _known_player_from_cookie(request)
    if known:
        return RedirectResponse(f"/player/{known.id}", status_code=303)
    # Иначе показываем форму, предзаполняя имя из cookie (если игрок уже играл —
    # например, дилер начал новую игру, и старый id уже невалиден).
    saved_name = _get_cookie_name(request)
    return templates.TemplateResponse("index.html", {"request": request, "saved_name": saved_name})


@app.get("/dealer", response_class=HTMLResponse, tags=["Pages"], summary="Панель дилера", include_in_schema=False)
async def dealer_page(request: Request):
    # Первичный онбординг: пока развёртывание не настроено (config.json содержит
    # значения-заглушки из примера), уводим дилера на мастер настройки /setup.
    # После сохранения config через мастер этот редирект больше не срабатывает.
    if not _safe_is_configured():
        return RedirectResponse(url="/setup", status_code=303)
    return templates.TemplateResponse("dealer.html", {"request": request, "game": game})


@app.get("/setup", response_class=HTMLResponse, tags=["Pages"], summary="Мастер первичной настройки", include_in_schema=False)
async def setup_page(request: Request):
    return templates.TemplateResponse("setup.html", {"request": request})


# ── API: Setup wizard (первичный онбординг развёртывания) ──

def _safe_is_configured() -> bool:
    """Обёртка над config.is_configured, которая не роняет страницу, если
    config.json временно нечитаем/битый — тогда считаем «не настроено» и ведём
    в мастер, где пользователь всё заполнит заново."""
    try:
        return app_config.is_configured(app_config.load_config())
    except Exception:
        return False


def _detect_lan_ip() -> str:
    """LAN-IP этого хоста — тот адрес, по которому плата и телефоны игроков
    достучатся до сервера.

    В Docker сокет-определение вернуло бы внутренний IP контейнера (172.x),
    бесполезный для платы. Поэтому start.sh пробрасывает реальный LAN-IP хоста
    в переменную HOST_LAN_IP — если она есть, берём её. Вне контейнера (bare
    Python) переменной нет, и мы определяем адрес через UDP-сокет к внешнему
    адресу (пакет никуда не шлётся — нужен лишь выбор исходящего интерфейса)."""
    env_ip = os.environ.get("HOST_LAN_IP", "").strip()
    if env_ip:
        return env_ip
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


class SetupPayload(BaseModel):
    wifi_ssid: str
    wifi_password: str
    server_ip: str            # только IP хоста; URL собираем сами с портом
    server_port: int = 8000
    trigger_code: int = 0     # код RF-пульта-курка (0 = режим обучения)


@app.get("/api/setup/status", tags=["Game Management"], summary="Статус первичной настройки", include_in_schema=False)
async def setup_status():
    """Настроено ли развёртывание + автоопределённый LAN-IP и текущие значения
    из config.json — чтобы мастер подставил их как значения по умолчанию."""
    try:
        cfg = app_config.load_config()
    except Exception:
        cfg = {}
    esp = cfg.get("esp", {}) if isinstance(cfg, dict) else {}
    return {
        "configured": _safe_is_configured(),
        "detected_ip": _detect_lan_ip(),
        "current": {
            "wifi_ssid": esp.get("wifi_ssid", ""),
            "wifi_password": esp.get("wifi_password", ""),
            "server_base_url": esp.get("server_base_url", ""),
            "server_port": cfg.get("server", {}).get("port", 8000) if isinstance(cfg, dict) else 8000,
            "trigger_code": esp.get("trigger_remote", {}).get("code", 0),
        },
    }


@app.post("/api/setup/save", tags=["Game Management"], summary="Сохранить настройки развёртывания", include_in_schema=False)
async def setup_save(payload: SetupPayload):
    """Записывает config.json из данных мастера. Требует, чтобы config.json был
    примонтирован read-write (см. docker-compose.yml). Сохраняет существующие
    пины/протокол пульта, меняет только сеть, адрес сервера и код курка."""
    # Стартуем от текущего config (сохранить пины и прочее), падать нельзя —
    # если файла нет, берём пример как базу.
    try:
        cfg = app_config.load_config()
    except Exception:
        try:
            with open(app_config._EXAMPLE_PATH, encoding="utf-8") as f:
                cfg = json.loads(app_config.strip_jsonc(f.read()))
        except Exception:
            cfg = {}

    cfg.setdefault("server", {})
    cfg["server"]["host"] = cfg["server"].get("host", "0.0.0.0")
    cfg["server"]["port"] = int(payload.server_port)

    esp = cfg.setdefault("esp", {})
    esp["wifi_ssid"] = payload.wifi_ssid
    esp["wifi_password"] = payload.wifi_password
    esp["server_base_url"] = f"http://{payload.server_ip}:{payload.server_port}"
    esp.setdefault("pins", {"trigger": 4, "solenoid": 5, "live_led": 2})
    remote = esp.setdefault("trigger_remote", {"code": 0, "protocol": 1, "bitlength": 24})
    remote["code"] = int(payload.trigger_code)

    try:
        app_config.save_config(cfg)
    except OSError as e:
        # Частый случай: config.json смонтирован read-only. Даём понятную
        # инструкцию, а не голый 500.
        raise HTTPException(
            status_code=500,
            detail=(
                "Не удалось записать config.json: "
                f"{e}. Убедись, что файл смонтирован для записи "
                "(docker-compose.yml монтирует его read-write)."
            ),
        )
    return {"ok": True, "server_base_url": esp["server_base_url"]}


# ── Полный серверный/железный конфиг для дилер-пульта (вкладка «Сервер/Железо») ──
# В отличие от мастера /setup (базовые поля), это отдаёт и принимает ВЕСЬ блок
# esp — пины, протокол пульта, тайминги — чтобы оператор менял любые параметры
# развёртывания прямо из панели дилера.

@app.get("/api/server_config", tags=["Game Management"], summary="Полный конфиг сервера/ESP", include_in_schema=False)
async def get_server_config():
    """Отдаёт server + esp секции config.json целиком (для вкладки «Сервер/Железо»
    в панели дилера) плюс автоопределённый LAN-IP."""
    try:
        cfg = app_config.load_config()
    except Exception:
        cfg = {}
    if not isinstance(cfg, dict):
        cfg = {}
    return {
        "detected_ip": _detect_lan_ip(),
        "server": cfg.get("server", {"host": "0.0.0.0", "port": 8000}),
        "esp": cfg.get("esp", {}),
    }


class ServerConfigPayload(BaseModel):
    # Любое подмножество полей; отсутствующие сохраняют текущее значение.
    server_port: Optional[int] = None
    wifi_ssid: Optional[str] = None
    wifi_password: Optional[str] = None
    server_ip: Optional[str] = None          # IP хоста; из него + порт строим server_base_url
    trigger_code: Optional[int] = None
    trigger_protocol: Optional[int] = None
    trigger_bitlength: Optional[int] = None
    pins: Optional[dict] = None              # {trigger, solenoid, live_led}
    timings: Optional[dict] = None           # весь блок esp.timings (мс/шт)


@app.post("/api/server_config/save", tags=["Game Management"], summary="Сохранить конфиг сервера/ESP", include_in_schema=False)
async def save_server_config(payload: ServerConfigPayload):
    """Записывает произвольное подмножество полей server/esp в config.json,
    сохраняя остальные. Требует read-write монтирования config.json."""
    try:
        cfg = app_config.load_config()
    except Exception:
        try:
            with open(app_config._EXAMPLE_PATH, encoding="utf-8") as f:
                cfg = json.loads(app_config.strip_jsonc(f.read()))
        except Exception:
            cfg = {}
    if not isinstance(cfg, dict):
        cfg = {}

    server = cfg.setdefault("server", {"host": "0.0.0.0", "port": 8000})
    esp = cfg.setdefault("esp", {})

    if payload.server_port is not None:
        server["port"] = int(payload.server_port)
    if payload.wifi_ssid is not None:
        esp["wifi_ssid"] = payload.wifi_ssid
    if payload.wifi_password is not None:
        esp["wifi_password"] = payload.wifi_password
    # server_base_url пересобираем, если задан IP (порт — из server.port).
    if payload.server_ip is not None:
        port = server.get("port", 8000)
        esp["server_base_url"] = f"http://{payload.server_ip}:{port}"

    remote = esp.setdefault("trigger_remote", {"code": 0, "protocol": 1, "bitlength": 24})
    if payload.trigger_code is not None:
        remote["code"] = int(payload.trigger_code)
    if payload.trigger_protocol is not None:
        remote["protocol"] = int(payload.trigger_protocol)
    if payload.trigger_bitlength is not None:
        remote["bitlength"] = int(payload.trigger_bitlength)

    if payload.pins is not None:
        pins = esp.setdefault("pins", {})
        for k in ("trigger", "solenoid", "live_led"):
            if k in payload.pins:
                pins[k] = int(payload.pins[k])
    if payload.timings is not None:
        timings = esp.setdefault("timings", {})
        for k, v in payload.timings.items():
            timings[k] = int(v)

    try:
        app_config.save_config(cfg)
    except OSError as e:
        raise HTTPException(
            status_code=500,
            detail=(
                f"Не удалось записать config.json: {e}. Проверь, что файл "
                "смонтирован для записи (docker-compose.yml монтирует его read-write)."
            ),
        )
    return {"ok": True, "esp": esp, "server": server}


@app.get("/player/{player_id}", response_class=HTMLResponse, tags=["Pages"], summary="Экран игрока", include_in_schema=False)
async def player_page(request: Request, player_id: str):
    if game and player_id in game.players:
        p = game.players[player_id]
        # Освежаем cookie на каждом заходе на свой экран — чтобы личность
        # переживала долгую сессию (max-age отсчитывается заново).
        resp = templates.TemplateResponse("player.html", {"request": request, "player": p})
        _set_player_cookies(resp, p.id, p.name)
        return resp

    # Запрошенного id в игре нет (устаревшая ссылка/новая игра). Если cookie
    # указывает на живого игрока текущей игры — уводим на его актуальный экран.
    known = _known_player_from_cookie(request)
    if known:
        return RedirectResponse(f"/player/{known.id}", status_code=303)

    # Совсем незнакомец — на форму входа, с предзаполнением имени из cookie.
    saved_name = _get_cookie_name(request)
    return templates.TemplateResponse("join.html", {
        "request": request,
        "error": "Игра не найдена или вы не зарегистрированы",
        "saved_name": saved_name,
    })


@app.get("/join", response_class=HTMLResponse, tags=["Pages"], summary="Форма присоединения к игре", include_in_schema=False)
async def join_page(request: Request):
    # Уже в игре по cookie — сразу на свой экран, без повторного ввода имени.
    known = _known_player_from_cookie(request)
    if known:
        return RedirectResponse(f"/player/{known.id}", status_code=303)
    saved_name = _get_cookie_name(request)
    return templates.TemplateResponse("join.html", {"request": request, "error": None, "saved_name": saved_name})


@app.get("/telejoin", response_class=HTMLResponse, tags=["Pages"], summary="Форма присоединения (TV режим)", include_in_schema=False)
async def telejoin_page(request: Request):
    """Форма входа для TV-режима: открывается на компьютере и транслируется на
    кинескопный телевизор. Вместо /player/ перенаправляет на /teleplayer/."""
    known = _known_player_from_cookie(request)
    if known:
        return RedirectResponse(f"/teleplayer/{known.id}", status_code=303)
    saved_name = _get_cookie_name(request)
    return templates.TemplateResponse("telejoin.html", {"request": request, "error": None, "saved_name": saved_name})


@app.get("/teleplayer/{player_id}", response_class=HTMLResponse, tags=["Pages"], summary="Экран игрока (TV режим)", include_in_schema=False)
async def teleplayer_page(request: Request, player_id: str):
    """Экран игрока для TV: без fullscreen-gate и wake lock (не нужны на
    компьютере). Использует тот же WebSocket /ws/player/{id}."""
    if game and player_id in game.players:
        p = game.players[player_id]
        resp = templates.TemplateResponse("teleplayer.html", {"request": request, "player": p})
        _set_player_cookies(resp, p.id, p.name)
        return resp

    known = _known_player_from_cookie(request)
    if known:
        return RedirectResponse(f"/teleplayer/{known.id}", status_code=303)

    saved_name = _get_cookie_name(request)
    return templates.TemplateResponse("telejoin.html", {
        "request": request,
        "error": "Игра не найдена или вы не зарегистрированы",
        "saved_name": saved_name,
    })


@app.get("/showscreen", response_class=HTMLResponse, tags=["Pages"], summary="Экран показа скрытых сообщений", include_in_schema=False)
async def showscreen_page(request: Request):
    return templates.TemplateResponse("showscreen.html", {"request": request})



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
    game_mode: str = Form("multiplayer", description="Режим игры: `multiplayer` (2-4 игрока), `solo` (1 на 1 с виртуальным DEALER), `story` (сюжетный режим, 3 стадии) или `story_one_round` (1 раунд, 4 HP, предметы после первой дозарядки)")
):
    global game, undo_stack
    game = GameState()
    game.config.game_mode = game_mode
    if game_mode == "solo":
        game.config.rounds = [dict(r) for r in SOLO_DEFAULT_ROUNDS]
    elif game_mode == "story":
        game.config.rounds = [dict(r) for r in STORY_DEFAULT_ROUNDS]
    elif game_mode == "story_one_round":
        game.config.rounds = [dict(r) for r in STORY_ONE_ROUND_DEFAULT_ROUNDS]
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
            "saved_name": name,
        })

    # Реконнект: если cookie указывает на игрока, УЖЕ существующего в этой игре —
    # это тот же человек вернулся (переподключение/«назад»/новая вкладка). Не
    # создаём дубль, просто возвращаем его на свой экран. Работает и после старта
    # игры, когда add_player() уже запрещён.
    known = _known_player_from_cookie(request)
    if known:
        known.connected = True
        await broadcast_state()
        resp = RedirectResponse(f"/player/{known.id}", status_code=303)
        _set_player_cookies(resp, known.id, known.name)
        return resp

    # Новый игрок. add_player сам бросит ValueError, если фаза уже не lobby или
    # набралось 4 игрока — показываем это на форме.
    try:
        player = game.add_player(name)
        await broadcast_state()
        resp = RedirectResponse(f"/player/{player.id}", status_code=303)
        _set_player_cookies(resp, player.id, player.name)
        return resp
    except ValueError as e:
        return templates.TemplateResponse("join.html", {
            "request": request,
            "error": str(e),
            "saved_name": name,
        })


@app.post(
    "/api/telejoin",
    tags=["Game Management"],
    summary="Присоединиться к игре (TV режим)",
    description=(
        "Аналог `/api/join` для TV-режима: при успехе редиректит на `/teleplayer/{player_id}` "
        "вместо `/player/{player_id}`. Регистрирует того же игрока в той же игре."
    ),
    response_class=HTMLResponse,
)
async def telejoin_game(request: Request, name: str = Form(..., description="Отображаемое имя игрока")):
    global game
    if not game:
        return templates.TemplateResponse("telejoin.html", {
            "request": request,
            "error": "Игра не создана. Подождите, пока дилер создаст игру.",
            "saved_name": name,
        })

    known = _known_player_from_cookie(request)
    if known:
        known.connected = True
        await broadcast_state()
        resp = RedirectResponse(f"/teleplayer/{known.id}", status_code=303)
        _set_player_cookies(resp, known.id, known.name)
        return resp

    try:
        player = game.add_player(name)
        await broadcast_state()
        resp = RedirectResponse(f"/teleplayer/{player.id}", status_code=303)
        _set_player_cookies(resp, player.id, player.name)
        return resp
    except ValueError as e:
        return templates.TemplateResponse("telejoin.html", {
            "request": request,
            "error": str(e),
            "saved_name": name,
        })

@app.post(
    "/api/start_game",
    tags=["Game Management"],
    summary="Начать игру",
    description=(
        "Переводит игру из `lobby` в первый раунд (генерирует патроны, назначает порядок ходов). "
        "Для `multiplayer` нужно минимум 2 игрока; для `solo`/`story` — 1 игрок (тогда автоматически "
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
    "/api/reload_revolver",
    tags=["Dealer Actions"],
    summary="Перезарядить игрушечный револьвер",
    description=(
        "Дилер физически перезарядил игрушечный револьвер (капсюли/пистоны). "
        "Сбрасывает счётчик капсюлей на полную ёмкость."
    ),
    response_model=OkResponse,
    responses={400: {"model": ErrorResponse, "description": "Игра не создана"}},
)
async def reload_revolver():
    if not game:
        raise HTTPException(400, "Игра не создана")
    prev = copy.deepcopy(game)
    game.reload_revolver()
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
            player_name = game.players[player_id].name
            await notify_showscreen(f"Лупа показала:\n{result['display']}", player_name, "magnifying_glass")
        elif item == "cigarettes":
            game.use_item_cigarettes(player_id)
        elif item == "adrenaline":
            if not target_id or not stolen_item:
                raise ValueError("Нужно выбрать цель и предмет для адреналина")
            result = game.use_item_adrenaline(player_id, target_id, stolen_item)
        elif item == "burner_phone":
            result = game.use_item_burner_phone(player_id)
            player_name = game.players[player_id].name
            await notify_showscreen(f"Телефон сообщил:\n{result['display']}", player_name, "burner_phone")
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
    from app.game_engine import GameEvent
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

- `game_mode`: `"solo"` \\| `"story"` \\| `"multiplayer"` — при смене режима подставляет дефолтные раунды для этого режима
- `rounds`: список `{hp, items_per_player, max_shells}` по раундам (переопределяет дефолты)
- `item_limits_global`: `{item_type: max_count}` — лимит копий предмета одновременно на столе
- `item_limits_per_player`: `{item_type: max_count}` — лимит копий предмета у одного игрока
- `show_shells_to_players`: bool — показывать ли игрокам число патронов при зарядке
- `physical_magazine_limit`: int — сколько патронов физически помещается в дробовик за раз (0 = без лимита)
- `max_items_per_player`: int — общий потолок предметов на руках у одного игрока
- `revolver_capacity`: int — ёмкость капсюлей игрушечного револьвера (≥1)
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
            elif data["game_mode"] == "story":
                game.config.rounds = [dict(r) for r in STORY_DEFAULT_ROUNDS]
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
            game.config.physical_magazine_limit = int(data["physical_magazine_limit"])
        if "max_items_per_player" in data:
            game.config.max_items_per_player = int(data["max_items_per_player"])
        if "revolver_capacity" in data:
            cap = max(1, int(data["revolver_capacity"]))
            game.config.revolver_capacity = cap
            # В лобби (единственная фаза, где правим конфиг) револьвер ещё не
            # расходован — сразу подтягиваем текущий запас к новой ёмкости.
            game.revolver_ammo = cap
        if "max_live_shells" in data:
            game.config.max_live_shells = max(0, int(data["max_live_shells"]))
        if "max_blank_shells" in data:
            game.config.max_blank_shells = max(0, int(data["max_blank_shells"]))
        await broadcast_state()
        return {"ok": True}
    except Exception as e:
        raise HTTPException(400, str(e))


# ── Sound engine (озвучка) ────────────────────────────────────────────────
# Стандартные звуки берутся из папки reference/Buckshot Roulette/. Оператор
# может через веб включать/выключать событие, загружать свой файл или сбросить
# на стандартный — изменения сразу применяются на бэкенде (см. app/sound_config.py).

@app.get("/api/audio/config", tags=["Game Management"], summary="Список звуковых событий с настройками", include_in_schema=False)
async def audio_config():
    return {"events": sound_config.get_config()}


@app.post("/api/audio/toggle", tags=["Game Management"], summary="Вкл/выкл звук события", include_in_schema=False)
async def audio_toggle(key: str = Form(...), enabled: bool = Form(...)):
    try:
        sound_config.set_enabled(key, enabled)
    except KeyError:
        raise HTTPException(404, f"Неизвестное событие: {key}")
    return {"ok": True, "key": key, "enabled": enabled}


@app.post("/api/audio/volume", tags=["Game Management"], summary="Установить громкость события", include_in_schema=False)
async def audio_volume(key: str = Form(...), volume: float = Form(...)):
    try:
        sound_config.set_volume(key, volume)
    except KeyError:
        raise HTTPException(404, f"Неизвестное событие: {key}")
    return {"ok": True, "key": key, "volume": volume}


@app.post("/api/audio/reset", tags=["Game Management"], summary="Сбросить событие на стандартный звук", include_in_schema=False)
async def audio_reset(key: str = Form(...)):
    try:
        sound_config.reset_custom(key)
    except KeyError:
        raise HTTPException(404, f"Неизвестное событие: {key}")
    return {"ok": True, "key": key}


@app.post("/api/audio/upload", tags=["Game Management"], summary="Загрузить свой звук для события", include_in_schema=False)
async def audio_upload(key: str = Form(...), file: UploadFile = File(...)):
    content = await file.read()
    if not content:
        raise HTTPException(400, "Пустой файл")
    if len(content) > 15 * 1024 * 1024:
        raise HTTPException(400, "Файл больше 15 МБ")
    try:
        saved = sound_config.save_upload(key, file.filename or "sound", content)
    except KeyError:
        raise HTTPException(404, f"Неизвестное событие: {key}")
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "key": key, "filename": saved}


@app.get("/api/audio/file/{key}", tags=["Game Management"], summary="Стрим эффективного звука события", include_in_schema=False)
async def audio_file(key: str, preview: bool = False):
    # preview=1 — прослушивание из настроек: играем даже если событие выключено.
    if not preview and not sound_config.is_enabled(key):
        # Событие выключено — сообщаем клиенту пустотой (звук не проигрывается).
        raise HTTPException(204, "disabled")
    path = sound_config.resolve_file(key)
    if not path:
        raise HTTPException(404, "Файл не найден")
    return FileResponse(str(path), media_type=sound_config.mime_for(path))


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
    global esp_force_fire
    # fire=true — одноразовая команда «щёлкни соленоидом сейчас» от дилера.
    # Потребляем флаг при первой же выдаче, чтобы плата выстрелила ровно раз.
    fire = esp_force_fire
    esp_force_fire = False

    if not game:
        return {"ready": False, "live": False, "fire": fire}
    status = game.esp_shell_status()
    status["fire"] = fire
    return status


@app.post(
    "/api/esp/force_fire",
    tags=["ESP32"],
    summary="Принудительно щёлкнуть соленоидом (команда дилера)",
    description=(
        "Дилер вручную инициирует физический удар соленоида, минуя игровую логику "
        "(боевой патрон/курок). Сервер выставляет одноразовый флаг `fire=true`, "
        "который плата ESP32 заберёт в ближайшем опросе `/api/esp/shell_status` "
        "(задержка ≤ интервала опроса, обычно до 1 c) и щёлкнет соленоидом один раз. "
        "Тратит один капсюль барабана револьвера (как боевой выстрел). "
        "Работает только с прошивкой, читающей поле `fire`."
    ),
    response_model=OkResponse,
)
async def esp_force_fire_cmd():
    global esp_force_fire
    esp_force_fire = True
    # Принудительный удар соленоида тратит капсюль барабана — как боевой выстрел.
    if game:
        game.consume_revolver_ammo()
        await broadcast_state()
    return {"ok": True}


@app.post(
    "/api/esp/shoot",
    tags=["ESP32"],
    summary="Продвинуть патрон по сигналу физического курка",
    description=(
        "Вызывается прошивкой ESP32 в момент физического выстрела (RF-курок). "
        "Выталкивает текущий патрон из очереди, чтобы статус показал следующий, "
        "и рассылает обновление состояния дилеру/игрокам. **Не наносит урон и не "
        "меняет ход** — игровую логику по-прежнему ведёт дилер через веб-интерфейс."
    ),
)
async def esp_shoot():
    if not game:
        return {"ok": False, "fired": False}
    result = game.esp_shoot()
    if result.get("fired"):
        await broadcast_state()
    return result


# ── TV Video API ──

async def broadcast_tv(msg: dict):
    """Send a video command to all connected TV screens."""
    data = json.dumps(msg, ensure_ascii=False)
    dead = []
    for ws in tv_ws_list:
        try:
            await ws.send_text(data)
        except Exception:
            dead.append(ws)
    for ws in dead:
        tv_ws_list.remove(ws)


@app.get("/api/tv/videos", tags=["TV"], summary="Список доступных видеофайлов", include_in_schema=False)
async def tv_list_videos():
    return {"videos": video_config.list_videos()}


@app.get("/api/tv/config", tags=["TV"], summary="Конфигурация видео-слотов", include_in_schema=False)
async def tv_get_config():
    cfg = video_config.load_config()
    return {
        "config": cfg,
        "videos": video_config.list_videos(),
        "slot_labels": video_config.SLOT_LABELS,
    }


@app.post("/api/tv/config/save", tags=["TV"], summary="Сохранить конфигурацию видео", include_in_schema=False)
async def tv_save_config(request: Request):
    data = await request.json()
    cfg = video_config.load_config()
    if "videos" in data:
        cfg["videos"].update(data["videos"])
    if "auto_play" in data:
        cfg["auto_play"].update(data["auto_play"])
    if "loop" in data:
        cfg.setdefault("loop", {}).update(data["loop"])
    if "settings" in data:
        cfg.setdefault("settings", {}).update(data["settings"])
    video_config.save_config(cfg)
    return {"ok": True}


@app.post("/api/tv/volume", tags=["TV"], summary="Изменить громкость TV", include_in_schema=False)
async def tv_set_volume(request: Request):
    """Set volume for TV videos. Body: {"volume": 100}"""
    data = await request.json()
    vol = data.get("volume", 100)
    
    # Save to config
    cfg = video_config.load_config()
    cfg.setdefault("settings", {})["volume"] = vol
    video_config.save_config(cfg)
    
    # Broadcast to TVs
    await broadcast_tv({"action": "set_volume", "volume": vol})
    return {"ok": True}


@app.post("/api/tv/play", tags=["TV"], summary="Воспроизвести видео на TV", include_in_schema=False)
async def tv_play(request: Request):
    """Play a video on all connected TV screens.
    Body: {"video": "filename.mp4", "loop": false} or {"slot": "intro"}"""
    global tv_video_state
    data = await request.json()
    slot = data.get("slot")
    video = data.get("video")
    loop = data.get("loop", False)
    if slot and not video:
        cfg = video_config.load_config()
        video = cfg["videos"].get(slot)
    if not video:
        raise HTTPException(400, "Не указан видеофайл")
        
    cfg = video_config.load_config()
    vol = cfg.get("settings", {}).get("volume", 100)
    
    # Get file modification time for smart cache busting
    filepath = video_config.VIDEOS_DIR / video
    mtime = int(os.path.getmtime(filepath)) if filepath.exists() else 0
    
    tv_video_state = {"action": "play", "video": video, "loop": loop, "volume": vol, "mtime": mtime}
    await broadcast_tv(tv_video_state)
    return {"ok": True, "video": video}


@app.post("/api/tv/pause", tags=["TV"], summary="Поставить видео на паузу", include_in_schema=False)
async def tv_pause():
    global tv_video_state
    tv_video_state["action"] = "pause"
    await broadcast_tv({"action": "pause"})
    return {"ok": True}


@app.post("/api/tv/resume", tags=["TV"], summary="Снять видео с паузы", include_in_schema=False)
async def tv_resume():
    global tv_video_state
    tv_video_state["action"] = "play"
    await broadcast_tv({"action": "resume"})
    return {"ok": True}


@app.post("/api/tv/stop", tags=["TV"], summary="Остановить видео", include_in_schema=False)
async def tv_stop():
    global tv_video_state
    tv_video_state = {"action": "idle", "video": None, "loop": False}
    await broadcast_tv({"action": "stop"})
    return {"ok": True}


@app.get("/api/tv/state", tags=["TV"], summary="Текущее состояние видео", include_in_schema=False)
async def tv_state():
    return tv_video_state


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
#
# GET/WS /ws/tv
#   Подключение TV-экрана для получения видео-команд.
#   Сервер шлёт: {"action": "play"|"pause"|"resume"|"stop", "video": "...", "loop": bool}
#   Клиент шлёт: {"event": "ended"} — видео закончилось.

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


@app.websocket("/ws/showscreen")
async def ws_showscreen(ws: WebSocket):
    await ws.accept()
    showscreen_ws_list.append(ws)
    try:
        while True:
            await ws.receive_text()  # Keep alive
    except WebSocketDisconnect:
        pass
    finally:
        if ws in showscreen_ws_list:
            showscreen_ws_list.remove(ws)


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


@app.websocket("/ws/tv")
async def ws_tv(ws: WebSocket):
    """WebSocket for TV video overlay. Server pushes video commands;
    client sends {"event": "ended"} when video finishes."""
    await ws.accept()
    tv_ws_list.append(ws)
    try:
        # Send current video state on connect
        if tv_video_state["action"] != "idle":
            await ws.send_text(json.dumps(tv_video_state, ensure_ascii=False))
        while True:
            msg = await ws.receive_text()
            try:
                data = json.loads(msg)
                if data.get("event") == "ended":
                    # Video finished — notify dealer via broadcast_state
                    pass  # Future: auto-play next video logic
            except Exception:
                pass
    except WebSocketDisconnect:
        pass
    finally:
        if ws in tv_ws_list:
            tv_ws_list.remove(ws)


# ── Run ──

if __name__ == "__main__":
    import uvicorn
    from app.config import SERVER_HOST, SERVER_PORT
    uvicorn.run("app.server:app", host=SERVER_HOST, port=SERVER_PORT, reload=True)
