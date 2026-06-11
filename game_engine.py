"""
Buckshot Roulette IRL — Game Engine
Core game logic: rounds, shells, items, health, turns.
"""

import random
import time
import uuid
from enum import Enum
from dataclasses import dataclass, field
from typing import Optional


class GamePhase(str, Enum):
    LOBBY = "lobby"
    ROUND_START = "round_start"
    DEALER_LOADING = "dealer_loading"      # Dealer must load shells
    DEALER_ITEMS = "dealer_items"          # Dealer must distribute items
    PLAYER_TURN = "player_turn"            # Active player's turn
    ROUND_OVER = "round_over"
    GAME_OVER = "game_over"


class ShellType(str, Enum):
    LIVE = "live"       # Red / боевой
    BLANK = "blank"     # Blue / холостой


class ItemType(str, Enum):
    BEER = "beer"
    HANDSAW = "handsaw"
    HANDCUFFS = "handcuffs"
    MAGNIFYING_GLASS = "magnifying_glass"
    CIGARETTES = "cigarettes"
    ADRENALINE = "adrenaline"
    BURNER_PHONE = "burner_phone"
    INVERTER = "inverter"
    EXPIRED_MEDICINE = "expired_medicine"
    MEDICINE_VODKA = "medicine_vodka"
    MEDICINE_WATER = "medicine_water"


ITEM_LABELS = {
    ItemType.BEER: ("Пиво", "Выбросить текущий патрон"),
    ItemType.HANDSAW: ("Пила", "Следующий выстрел x2"),
    ItemType.HANDCUFFS: ("Наручники", "Противник пропускает ход"),
    ItemType.MAGNIFYING_GLASS: ("Лупа", "Подсмотреть текущий патрон"),
    ItemType.CIGARETTES: ("Сигареты", "+1 HP"),
    ItemType.ADRENALINE: ("Адреналин", "Украсть предмет"),
    ItemType.BURNER_PHONE: ("Телефон", "Узнать патрон по номеру"),
    ItemType.INVERTER: ("Инвертор", "Инвертировать патрон"),
    ItemType.EXPIRED_MEDICINE: ("Лекарство", "50%: +2 HP / -1 HP"),
    ItemType.MEDICINE_VODKA: ("Лекарство (ВОДКА)", "+2 HP (знает только дилер)"),
    ItemType.MEDICINE_WATER: ("Лекарство (ВОДА)", "-1 HP (знает только дилер)"),
}


@dataclass
class Player:
    id: str
    name: str
    hp: int = 0
    max_hp: int = 0
    items: list = field(default_factory=list)
    alive: bool = True
    connected: bool = True
    handcuffs_state: int = 0
    # For multiplayer: player number shown on screen
    number: int = 0


@dataclass
class GameConfig:
    """Configurable game parameters."""
    # Per-round settings: list of dicts with keys: hp, items_per_player, max_shells
    rounds: list = field(default_factory=lambda: [
        {"hp": 2, "items_per_player": 0, "max_shells": 4},
        {"hp": 4, "items_per_player": 2, "max_shells": 6},
        {"hp": 6, "items_per_player": 4, "max_shells": 8},
    ])
    max_items_per_player: int = 8
    # Item pool weights (higher = more likely)
    item_weights: dict = field(default_factory=lambda: {
        ItemType.BEER: 10,
        ItemType.HANDSAW: 6,
        ItemType.HANDCUFFS: 7,
        ItemType.MAGNIFYING_GLASS: 8,
        ItemType.CIGARETTES: 8,
        ItemType.ADRENALINE: 4,
        ItemType.BURNER_PHONE: 5,
        ItemType.INVERTER: 5,
        ItemType.EXPIRED_MEDICINE: 3,
    })


@dataclass
class GameEvent:
    """A log entry for game events."""
    timestamp: float
    message: str
    event_type: str = "info"  # info, shot, item, round, system


class GameState:
    """Core game state machine."""

    def __init__(self):
        self.game_id: str = str(uuid.uuid4())[:8]
        self.config: GameConfig = GameConfig()
        self.players: dict[str, Player] = {}  # id -> Player
        self.turn_order: list[str] = []  # player ids in order
        self.current_turn_idx: int = 0
        self.phase: GamePhase = GamePhase.LOBBY
        self.current_round: int = 0  # 0-indexed
        self.shells: list[ShellType] = []
        self.shells_display: list[ShellType] = []  # original load for dealer
        self.saw_active: bool = False  # next shot does double damage
        self.inverted: bool = False   # current shell is inverted
        self.event_log: list[GameEvent] = []
        self.winner_id: Optional[str] = None
        self.created_at: float = time.time()
        # Track items dealt this sub-round for dealer display
        self.dealt_items: dict[str, list] = {}
        # Burner phone result (for dealer to write on paper)
        self.last_burner_result: Optional[str] = None
        # Medicine result
        self.last_medicine_result: Optional[str] = None
        # Magnifying glass result
        self.last_magnify_result: Optional[str] = None
        # Track if we have performed the hardcoded 1st round initialization
        self.first_round_generated: bool = False
        # Whether to show shell counts to players during dealer_loading phase
        self.show_shells_to_players: bool = True

    def _log(self, msg: str, event_type: str = "info"):
        self.event_log.append(GameEvent(time.time(), msg, event_type))

    # ── Player Management ──

    def add_player(self, name: str) -> Player:
        if self.phase != GamePhase.LOBBY:
            raise ValueError("Нельзя добавить игрока после начала игры")
        if len(self.players) >= 4:
            raise ValueError("Максимум 4 игрока")
        pid = str(uuid.uuid4())[:8]
        number = len(self.players) + 1
        p = Player(id=pid, name=name, number=number)
        self.players[pid] = p
        self._log(f">> Игрок #{number} [{name}] присоединился", "system")
        return p

    def remove_player(self, pid: str):
        if pid in self.players:
            p = self.players[pid]
            p.alive = False
            p.connected = False
            self._log(f">> Игрок #{p.number} [{p.name}] покинул игру", "system")
            self._check_game_over()

    def get_alive_players(self) -> list[Player]:
        return [p for p in self.players.values() if p.alive]

    # ── Game Flow ──

    def start_game(self):
        if len(self.players) < 2:
            raise ValueError("Нужно минимум 2 игрока")
        self.turn_order = [p.id for p in sorted(self.players.values(), key=lambda x: x.number)]
        self.current_round = 0
        self._start_round()

    def _start_round(self):
        rc = self.config.rounds[self.current_round]
        self.phase = GamePhase.ROUND_START
        self._log(f"=== РАУНД {self.current_round + 1} ===", "round")

        # Все игроки возрождаются в новом раунде
        for p in self.players.values():
            p.alive = True
            p.hp = rc["hp"]
            p.max_hp = rc["hp"]
            p.handcuffs_state = 0
            p.items = []  # Clear items at round start

        self.saw_active = False
        self.inverted = False
        self._generate_shells()

    def _generate_shells(self):
        rc = self.config.rounds[self.current_round]
        
        # Hardcode first round rule: exactly 1 live, 2 blanks
        if self.current_round == 0 and not self.first_round_generated:
            live_count = 1
            blank_count = 2
            total = 3
            self.first_round_generated = True
        else:
            total = random.randint(2, rc["max_shells"])
            # Формула для ограничения перекосов (избегаем 5:1 при 6 патронах)
            live_count = random.randint(max(1, total//2 - 1), min(total - 1, total//2 + 1))
            blank_count = total - live_count

        self.shells = (
            [ShellType.LIVE] * live_count +
            [ShellType.BLANK] * blank_count
        )
        random.shuffle(self.shells)
        self.shells_display = list(self.shells)

        self._log(
            f"Дробовик заряжен: {live_count} боевых, {blank_count} холостых (всего {total})",
            "round"
        )
        self.phase = GamePhase.DEALER_LOADING

    def confirm_shells_loaded(self):
        """Dealer confirms they physically loaded the shells."""
        self.phase = GamePhase.DEALER_ITEMS
        rc = self.config.rounds[self.current_round]
        items_count = rc["items_per_player"]

        if items_count == 0:
            self.dealt_items = {}
            self._confirm_items_dealt()
            return

        # Generate items for each alive player
        self.dealt_items = {}
        pool = list(self.config.item_weights.keys())
        weights = [self.config.item_weights[i] for i in pool]

        for p in self.get_alive_players():
            slots_free = self.config.max_items_per_player - len(p.items)
            count = min(items_count, slots_free)
            chosen = random.choices(pool, weights=weights, k=count)
            
            final_chosen = []
            for item in chosen:
                if item == ItemType.EXPIRED_MEDICINE:
                    if random.random() < 0.5:
                        final_chosen.append(ItemType.MEDICINE_VODKA)
                    else:
                        final_chosen.append(ItemType.MEDICINE_WATER)
                else:
                    final_chosen.append(item)

            p.items.extend(final_chosen)
            self.dealt_items[p.id] = final_chosen
            names = ", ".join(ITEM_LABELS[i][0] for i in final_chosen)
            self._log(f"Игроку #{p.number} «{p.name}» выданы: {names}", "item")

    def confirm_items_dealt(self):
        """Dealer confirms items have been physically distributed."""
        self._confirm_items_dealt()

    def _confirm_items_dealt(self):
        self.current_turn_idx = self._find_first_alive_idx(self.current_turn_idx)
        self.phase = GamePhase.PLAYER_TURN
        cp = self.players[self.turn_order[self.current_turn_idx]]
        self._log(f">> Ход игрока #{cp.number} [{cp.name}]", "info")

    # ── Turn Management ──

    def get_current_player(self) -> Optional[Player]:
        if self.phase != GamePhase.PLAYER_TURN:
            return None
        if self.current_turn_idx >= len(self.turn_order):
            return None
        return self.players.get(self.turn_order[self.current_turn_idx])

    def _find_first_alive_idx(self, start: int) -> int:
        n = len(self.turn_order)
        for i in range(n):
            idx = (start + i) % n
            p = self.players[self.turn_order[idx]]
            if p.alive:
                return idx
        return start

    def _advance_turn(self):
        """Move to next alive player."""
        n = len(self.turn_order)
        next_idx = (self.current_turn_idx + 1) % n

        # Clear handcuffs that have finished their visual duration
        for p in self.players.values():
            if p.handcuffs_state == 1:
                p.handcuffs_state = 0

        # Find next alive, non-skipped player
        for _ in range(n):
            p = self.players[self.turn_order[next_idx]]
            if p.alive:
                if p.handcuffs_state == 2:
                    p.handcuffs_state = 1
                    self._log(f">> Игрок #{p.number} [{p.name}] пропускает ход (наручники)", "info")
                    next_idx = (next_idx + 1) % n
                    continue
                break
            next_idx = (next_idx + 1) % n

        self.current_turn_idx = next_idx
        self.saw_active = False
        self.inverted = False
        cp = self.players[self.turn_order[self.current_turn_idx]]
        self._log(f"Ход игрока #{cp.number} «{cp.name}»", "info")

    def _check_shells_empty(self):
        """If no shells left, reload or end round."""
        if len(self.shells) == 0:
            alive = self.get_alive_players()
            if len(alive) <= 1:
                self._check_game_over()
                return
            # Check if we should go to next round or reload within same round
            self._log(">> Все патроны расстреляны. Перезарядка...", "round")
            self.saw_active = False
            self.inverted = False
            self._generate_shells()

    def _check_game_over(self):
        alive = self.get_alive_players()
        if len(alive) <= 1:
            if len(alive) == 1:
                self._log(f">>> РАУНД ВЫИГРАЛ: Игрок #{alive[0].number} [{alive[0].name}]", "round")
            else:
                self._log(">>> Ничья. В раунде никто не выжил.", "round")
            self.phase = GamePhase.ROUND_OVER
            return True
        return False

    # ── Shooting ──

    def shoot(self, target_id: str) -> dict:
        """
        Execute a shot at target_id.
        Returns dict with shot result info for dealer.
        """
        if self.phase != GamePhase.PLAYER_TURN:
            raise ValueError("Сейчас не фаза стрельбы")
        if not self.shells:
            raise ValueError("Нет патронов в магазине")

        shooter = self.get_current_player()
        target = self.players.get(target_id)
        if not target or not target.alive:
            raise ValueError("Неверная цель")

        shell = self.shells.pop(0)
        # Apply inverter
        if self.inverted:
            shell = ShellType.BLANK if shell == ShellType.LIVE else ShellType.LIVE
            self.inverted = False

        is_self_shot = (shooter.id == target_id)
        damage = 0
        result = {
            "shell": shell.value,
            "shooter": shooter.id,
            "target": target_id,
            "is_self_shot": is_self_shot,
            "damage": 0,
            "target_hp_after": target.hp,
            "extra_turn": False,
        }

        if shell == ShellType.LIVE:
            damage = 2 if self.saw_active else 1
            target.hp = max(0, target.hp - damage)
            result["damage"] = damage
            result["target_hp_after"] = target.hp

            dmg_text = f"(-{damage} HP)" if damage > 1 else "(-1 HP)"
            self._log(
                f"[БОЕВОЙ] #{shooter.number} [{shooter.name}] -> #{target.number} [{target.name}] {dmg_text}",
                "shot"
            )

            if target.hp <= 0:
                target.alive = False
                self._log(f">>> Игрок #{target.number} [{target.name}] выбыл", "shot")

            self.saw_active = False

            if self._check_game_over():
                self.current_turn_idx = (self.current_turn_idx + 1) % len(self.turn_order)
            else:
                self._advance_turn()
                if len(self.shells) == 0:
                    self._check_shells_empty()
        else:
            # Blank
            self._log(
                f"[ХОЛОСТОЙ] #{shooter.number} [{shooter.name}] -> #{target.number} [{target.name}]",
                "shot"
            )
            self.saw_active = False

            if is_self_shot:
                result["extra_turn"] = True
                self._log(f">> Игрок #{shooter.number} получает дополнительный ход", "info")
                # Don't advance turn — same player goes again
                if len(self.shells) == 0:
                    self._check_shells_empty()
            else:
                self._advance_turn()
                if len(self.shells) == 0:
                    self._check_shells_empty()

        return result

    # ── Items ──

    def use_item_beer(self, player_id: str) -> dict:
        """Eject current shell without firing."""
        p = self.players[player_id]
        self._remove_item(p, ItemType.BEER)
        if not self.shells:
            raise ValueError("Нет патронов")
        ejected = self.shells.pop(0)
        if self.inverted:
            ejected = ShellType.BLANK if ejected == ShellType.LIVE else ShellType.LIVE
            self.inverted = False
        self._log(f">> #{p.number} использовал пиво. Выброшен: {'БОЕВОЙ' if ejected == ShellType.LIVE else 'ХОЛОСТОЙ'}", "item")
        if len(self.shells) == 0:
            self._check_shells_empty()
        return {"ejected": ejected.value}

    def use_item_handsaw(self, player_id: str):
        p = self.players[player_id]
        self._remove_item(p, ItemType.HANDSAW)
        self.saw_active = True
        self._log(f">> #{p.number} отпилил ствол. Следующий выстрел x2", "item")

    def use_item_handcuffs(self, player_id: str, target_id: str):
        p = self.players[player_id]
        t = self.players[target_id]
        self._remove_item(p, ItemType.HANDCUFFS)
        if t.handcuffs_state > 0:
            raise ValueError(f"Игрок #{t.number} уже в наручниках")
        t.handcuffs_state = 2
        self._log(f">> #{p.number} надел наручники на #{t.number} [{t.name}]", "item")

    def use_item_magnifying_glass(self, player_id: str) -> dict:
        p = self.players[player_id]
        self._remove_item(p, ItemType.MAGNIFYING_GLASS)
        if not self.shells:
            raise ValueError("Нет патронов")
        current = self.shells[0]
        if self.inverted:
            current = ShellType.BLANK if current == ShellType.LIVE else ShellType.LIVE
        self.last_magnify_result = f"{'БОЕВОЙ' if current == ShellType.LIVE else 'ХОЛОСТОЙ'}"
        self._log(f">> #{p.number} использовал лупу (результат виден дилеру)", "item")
        return {"shell": current.value, "display": self.last_magnify_result}

    def use_item_cigarettes(self, player_id: str):
        p = self.players[player_id]
        self._remove_item(p, ItemType.CIGARETTES)
        old_hp = p.hp
        p.hp = min(p.hp + 1, p.max_hp)
        gained = p.hp - old_hp
        self._log(f">> #{p.number} использовал сигареты (+{gained} HP, теперь {p.hp}/{p.max_hp})", "item")

    def use_item_adrenaline(self, player_id: str, target_id: str, stolen_item: str) -> dict:
        """Steal an item from target and use it."""
        p = self.players[player_id]
        t = self.players[target_id]
        self._remove_item(p, ItemType.ADRENALINE)
        item_type = ItemType(stolen_item)
        if item_type not in t.items:
            raise ValueError(f"У игрока #{t.number} нет предмета {stolen_item}")
        t.items.remove(item_type)
        p.items.append(item_type)
        self._log(f">> #{p.number} украл [{ITEM_LABELS[item_type][0]}] у #{t.number}", "item")
        return {"stolen_item": stolen_item}

    def use_item_burner_phone(self, player_id: str) -> dict:
        p = self.players[player_id]
        self._remove_item(p, ItemType.BURNER_PHONE)
        if not self.shells:
            raise ValueError("Нет патронов")

        if len(self.shells) <= 2:
            self.last_burner_result = "Голос в трубке молчит... (слишком мало патронов)"
            self._log(f">> #{p.number} слушает телефон, но там лишь тишина", "item")
            return {"index": -1, "shell": "unknown", "display": self.last_burner_result}

        # Reveal a random shell position
        idx = random.randint(0, len(self.shells) - 1)
        shell = self.shells[idx]
        label = "БОЕВОЙ" if shell == ShellType.LIVE else "ХОЛОСТОЙ"
        self.last_burner_result = f"Патрон #{idx + 1} из {len(self.shells)}: {label}"
        self._log(f">> #{p.number} использовал телефон (подсказка у дилера)", "item")
        return {"position": idx + 1, "total": len(self.shells), "shell": shell.value, "display": self.last_burner_result}

    def use_item_inverter(self, player_id: str):
        p = self.players[player_id]
        self._remove_item(p, ItemType.INVERTER)
        self.inverted = not self.inverted
        self._log(f">> #{p.number} использовал инвертор. Текущий патрон инвертирован", "item")

    def use_item_expired_medicine(self, player_id: str, is_vodka: bool) -> dict:
        p = self.players[player_id]
        if is_vodka:
            self._remove_item(p, ItemType.MEDICINE_VODKA)
            old_hp = p.hp
            p.hp = min(p.hp + 2, p.max_hp)
            gained = p.hp - old_hp
            self.last_medicine_result = f"УДАЧА! +{gained} HP (водка)"
            self._log(f">> #{p.number}: УДАЧА +{gained} HP (теперь {p.hp}/{p.max_hp}) -- выпита водка", "item")
        else:
            self._remove_item(p, ItemType.MEDICINE_WATER)
            p.hp = max(0, p.hp - 1)
            self.last_medicine_result = "НЕУДАЧА! -1 HP (вода)"
            self._log(f">> #{p.number}: НЕУДАЧА -1 HP (теперь {p.hp}/{p.max_hp}) -- выпита вода", "item")
            if p.hp <= 0:
                p.alive = False
                self._log(f">>> Игрок #{p.number} [{p.name}] выбыл от испорченного лекарства", "shot")
                if self._check_game_over():
                    if self.turn_order[self.current_turn_idx] == player_id:
                        self.current_turn_idx = (self.current_turn_idx + 1) % len(self.turn_order)
                else:
                    if self.turn_order[self.current_turn_idx] == player_id:
                        self._advance_turn()
        return {"success": is_vodka, "display": self.last_medicine_result}

    def dealer_adjust_hp(self, player_id: str, delta: int):
        """Dealer manually adjusts player HP."""
        p = self.players[player_id]
        old_hp = p.hp
        p.hp = max(0, min(p.hp + delta, p.max_hp))
        self._log(f">> Дилер изменил HP #{p.number}: {old_hp} -> {p.hp}", "system")
        if p.hp <= 0 and p.alive:
            p.alive = False
            self._log(f">>> Игрок #{p.number} [{p.name}] выбыл", "shot")
            if self._check_game_over():
                if self.turn_order and self.turn_order[self.current_turn_idx] == player_id:
                    self.current_turn_idx = (self.current_turn_idx + 1) % len(self.turn_order)
            else:
                if self.turn_order and self.turn_order[self.current_turn_idx] == player_id:
                    self._advance_turn()

    def force_end_game(self):
        """Dealer force-ends the game."""
        self._log(">>> Игра завершена дилером", "system")
        self.phase = GamePhase.GAME_OVER

    def force_round_over(self):
        """Dealer force-ends the current round."""
        self._log(">>> Раунд завершен дилером", "system")
        self.phase = GamePhase.ROUND_OVER

    def next_round(self):
        """Advance to next round (called when all players in current round done)."""
        self.current_round += 1
        if self.current_round >= len(self.config.rounds):
            # All rounds done — last man standing wins
            alive = self.get_alive_players()
            if len(alive) == 1:
                self.winner_id = alive[0].id
                self._log(f">>> ПОБЕДИТЕЛЬ: #{alive[0].number} [{alive[0].name}]", "round")
            else:
                self._log("Все раунды пройдены!", "round")
            self.phase = GamePhase.GAME_OVER
        else:
            self._start_round()

    def _remove_item(self, player: Player, item: ItemType):
        if item not in player.items:
            raise ValueError(f"У игрока нет предмета {item.value}")
        player.items.remove(item)

    # ── Serialization ──

    def to_dict(self, for_dealer: bool = False) -> dict:
        """Serialize game state. If for_dealer, include sensitive info."""
        alive_players = self.get_alive_players()
        current = self.get_current_player()

        data = {
            "game_id": self.game_id,
            "phase": self.phase.value,
            "current_round": self.current_round + 1,
            "total_rounds": len(self.config.rounds),
            "shells_remaining": len(self.shells),
            "saw_active": self.saw_active,
            "inverted": self.inverted,
            "current_player": {
                "id": current.id,
                "name": current.name,
                "number": current.number,
            } if current else None,
            "players": [
                {
                    "id": p.id,
                    "name": p.name,
                    "number": p.number,
                    "hp": p.hp,
                    "max_hp": p.max_hp,
                    "alive": p.alive,
                    "connected": p.connected,
                    "items": [i.value for i in p.items] if for_dealer else [],
                    "items_display": [ITEM_LABELS[i][0] for i in p.items] if for_dealer else [],
                    "skip_next_turn": p.handcuffs_state > 0,
                }
                for p in sorted(self.players.values(), key=lambda x: x.number)
            ],
            "winner_id": self.winner_id,
            "winner_name": self.players[self.winner_id].name if self.winner_id else None,
        }

        if for_dealer:
            data["shells_sequence"] = [s.value for s in self.shells]
            data["shells_display"] = [s.value for s in self.shells_display]
            data["dealt_items"] = {
                pid: [ITEM_LABELS[ItemType(i)][0] for i in items]
                for pid, items in self.dealt_items.items()
            } if self.dealt_items else {}
            data["last_burner_result"] = self.last_burner_result
            data["last_medicine_result"] = self.last_medicine_result
            data["last_magnify_result"] = self.last_magnify_result
            # Live/blank counts
            live_c = sum(1 for s in self.shells if s == ShellType.LIVE)
            blank_c = sum(1 for s in self.shells if s == ShellType.BLANK)
            data["live_count"] = live_c
            data["blank_count"] = blank_c
            data["show_shells_to_players"] = self.show_shells_to_players

        # Recent log (last 20 events)
        data["log"] = [
            {"message": e.message, "type": e.event_type}
            for e in self.event_log[-30:]
        ]

        return data

    def player_view(self, player_id: str) -> dict:
        """Minimal view for a player's phone."""
        p = self.players.get(player_id)
        if not p:
            return {"error": "Игрок не найден"}
        current = self.get_current_player()
        is_my_turn = current and current.id == player_id

        # When shells are hidden, show HP screen instead of shell counts during loading
        effective_phase = self.phase.value
        if not self.show_shells_to_players and self.phase == GamePhase.DEALER_LOADING:
            effective_phase = "player_turn"

        return {
            "game_id": self.game_id,
            "phase": effective_phase,
            "my_number": p.number,
            "my_name": p.name,
            "my_hp": p.hp,
            "my_max_hp": p.max_hp,
            "my_alive": p.alive,
            "my_handcuffed": p.handcuffs_state > 0,
            "is_my_turn": is_my_turn,
            "current_round": self.current_round + 1,
            "total_rounds": len(self.config.rounds),
            "shells_remaining": len(self.shells),
            "live_count": sum(1 for s in self.shells if s == ShellType.LIVE),
            "blank_count": sum(1 for s in self.shells if s == ShellType.BLANK),
            "saw_active": self.saw_active,
            "current_player_name": current.name if current else None,
            "current_player_number": current.number if current else None,
            "players_summary": [
                {
                    "number": pl.number,
                    "name": pl.name,
                    "hp": pl.hp,
                    "max_hp": pl.max_hp,
                    "alive": pl.alive,
                }
                for pl in sorted(self.players.values(), key=lambda x: x.number)
            ],
            "winner_name": self.players[self.winner_id].name if self.winner_id else None,
        }
