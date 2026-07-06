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

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from game_engine import (
    GameState, GamePhase, GameConfig, ItemType, ITEM_LABELS, ShellType,
    SOLO_DEFAULT_ROUNDS, STORY_DEFAULT_ROUNDS, MULTIPLAYER_DEFAULT_ROUNDS
)

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

app = FastAPI(title="Buckshot Roulette IRL", lifespan=lifespan)
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

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


@app.get("/dealer", response_class=HTMLResponse)
async def dealer_page(request: Request):
    return templates.TemplateResponse("dealer.html", {"request": request, "game": game})


@app.get("/player/{player_id}", response_class=HTMLResponse)
async def player_page(request: Request, player_id: str):
    if not game or player_id not in game.players:
        return templates.TemplateResponse("join.html", {"request": request, "error": "Игра не найдена или вы не зарегистрированы"})
    p = game.players[player_id]
    return templates.TemplateResponse("player.html", {"request": request, "player": p})


@app.get("/join", response_class=HTMLResponse)
async def join_page(request: Request):
    return templates.TemplateResponse("join.html", {"request": request, "error": None})


# ── API: Game Management ──

@app.post("/api/create_game")
async def create_game(game_mode: str = Form("multiplayer")):
    global game, undo_stack
    game = GameState()
    game.config.game_mode = game_mode
    if game_mode == "solo":
        game.config.rounds = [dict(r) for r in SOLO_DEFAULT_ROUNDS]
    elif game_mode == "story":
        game.config.rounds = [dict(r) for r in STORY_DEFAULT_ROUNDS]
    undo_stack.clear()
    await broadcast_state()
    return {"ok": True, "game_id": game.game_id, "game_mode": game_mode}


@app.post("/api/join")
async def join_game(request: Request, name: str = Form(...)):
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


@app.post("/api/start_game")
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


@app.post("/api/confirm_shells")
async def confirm_shells():
    if not game:
        raise HTTPException(400, "Игра не создана")
    prev = copy.deepcopy(game)
    game.confirm_shells_loaded()
    push_undo(prev)
    await broadcast_state()
    return {"ok": True}


@app.post("/api/confirm_items")
async def confirm_items():
    if not game:
        raise HTTPException(400, "Игра не создана")
    prev = copy.deepcopy(game)
    game.confirm_items_dealt()
    push_undo(prev)
    await broadcast_state()
    return {"ok": True}


@app.post("/api/shoot")
async def shoot(target_id: str = Form(...)):
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


@app.post("/api/use_item")
async def use_item(
    player_id: str = Form(...),
    item: str = Form(...),
    target_id: str = Form(None),
    stolen_item: str = Form(None),
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


@app.post("/api/adjust_hp")
async def adjust_hp(player_id: str = Form(...), delta: int = Form(...)):
    if not game:
        raise HTTPException(400, "Игра не создана")
    prev = copy.deepcopy(game)
    game.dealer_adjust_hp(player_id, delta)
    push_undo(prev)
    await broadcast_state()
    return {"ok": True}


@app.post("/api/force_end")
async def force_end():
    if not game:
        raise HTTPException(400, "Игра не создана")
    prev = copy.deepcopy(game)
    game.force_end_game()
    push_undo(prev)
    await broadcast_state()
    return {"ok": True}


@app.post("/api/force_round_over")
async def api_force_round_over():
    if not game:
        raise HTTPException(400, "Игра не создана")
    prev = copy.deepcopy(game)
    game.force_round_over()
    push_undo(prev)
    await broadcast_state()
    return {"ok": True}


@app.post("/api/clear_special")
async def clear_special():
    if game:
        game.last_magnify_result = None
        game.last_burner_result = None
        game.last_medicine_result = None
    return {"ok": True}


@app.get("/api/esp/next_shell")
async def esp_next_shell():
    if not game:
        return {"shell": "empty"}
    if not game.shells:
        return {"shell": "empty"}
    
    # Get the next shell
    next_shell = game.shells[0]
    
    # Take into account the inverter
    if game.inverted:
        from game_engine import ShellType
        next_shell = ShellType.BLANK if next_shell == ShellType.LIVE else ShellType.LIVE
        
    return {"shell": next_shell.value}


@app.get("/api/esp/shot_fired")
async def esp_shot_fired_get():
    if not game:
        raise HTTPException(400, "Игра не создана")
    if game.phase != GamePhase.PLAYER_TURN:
        raise HTTPException(400, "Сейчас не фаза стрельбы")
    
    game.pending_shot = True
    await broadcast_state()
    return {"ok": True}


@app.post("/api/esp/shot_fired")
async def esp_shot_fired_post():
    if not game:
        raise HTTPException(400, "Игра не создана")
    if game.phase != GamePhase.PLAYER_TURN:
        raise HTTPException(400, "Сейчас не фаза стрельбы")
    
    game.pending_shot = True
    await broadcast_state()
    return {"ok": True}


@app.post("/api/toggle_shells")
async def toggle_shells():
    if not game:
        raise HTTPException(400, "Игра не создана")
    game.show_shells_to_players = not game.show_shells_to_players
    await broadcast_state()
    return {"ok": True, "show_shells": game.show_shells_to_players}


@app.post("/api/next_round")
async def next_round():
    if not game:
        raise HTTPException(400, "Игра не создана")
    prev = copy.deepcopy(game)
    game.next_round()
    push_undo(prev)
    await broadcast_state()
    return {"ok": True}


@app.post("/api/remove_player")
async def remove_player(player_id: str = Form(...)):
    if not game:
        raise HTTPException(400, "Игра не создана")
    prev = copy.deepcopy(game)
    game.remove_player(player_id)
    push_undo(prev)
    await broadcast_state()
    return {"ok": True}

@app.post("/api/undo")
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


@app.post("/api/update_config")
async def update_config(config_json: str = Form(...)):
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
            game.config.physical_magazine_limit = data["physical_magazine_limit"]
        await broadcast_state()
        return {"ok": True}
    except Exception as e:
        raise HTTPException(400, str(e))


@app.get("/api/state")
async def get_state(dealer: bool = False):
    if not game:
        return {"phase": "no_game"}
    if dealer:
        data = game.to_dict(for_dealer=True)
        data["can_undo"] = len(undo_stack) > 0
        return data
    return game.to_dict(for_dealer=False)


@app.get("/api/player_state/{player_id}")
async def get_player_state(player_id: str):
    if not game:
        return {"phase": "no_game"}
    return game.player_view(player_id)


# ── WebSockets ──

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
    uvicorn.run("server:app", host="0.0.0.0", port=8000, reload=True)
