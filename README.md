# Buckshot Roulette IRL

A multiplayer IRL (In Real Life) adaptation of the popular game "Buckshot Roulette". 
This system provides the digital backend and frontend interfaces needed to manage a physical game session with 2-4 players and 1 Dealer (Game Master).

## Architecture Overview

- **Backend Framework**: Python with FastAPI and Uvicorn.
- **State Management**: A global, in-memory `GameEngine` class handles all game logic, phases, turn order, items, and health.
- **Real-time Communication**: WebSocket connections broadcast state changes instantly to all connected clients (Dealer and Players).
- **Frontend**: Vanilla HTML/JS/CSS styled as a retro CRT terminal. Templates are rendered via Jinja2.

## Project Structure

```
app/                  # Python server (FastAPI)
├── server.py         #   HTTP + WebSocket transport
├── game_engine.py    #   all game logic (state machine, shells, items, HP)
├── config.py         #   reads config.json from the repo root
├── templates/        #   Jinja2 pages (dealer, player, join, setup)
└── static/           #   CSS + icons
esp/                  # ESP32 firmware + flashing/config-gen scripts
scripts/              # host network auto-config (net_config.sh / .ps1)
docs/                 # ARCHITECTURE.md, RULES.md, AGENT_HANDOFF.md
reference/            # design mockups & decompiled Godot game (gitignored)
config.json           # single source of settings (copy of config.example.json)
start.sh              # one-command launcher (net → flash → docker compose up)
```

## How to Play (Roles)

### The Dealer (Game Master)
The Dealer hosts the game from a tablet or PC and manages the physical components (the shotgun, the shells, the items).
- Access the dealer interface via `/dealer`.
- The Dealer configures the game (including round lengths, item weights, and physical magazine limits), hands out physical items, loads the physical shotgun, and mirrors all physical actions in the digital interface.
- The Dealer can **undo** any action if a mistake was made during tracking.
- The system supports a physical magazine limit (e.g., a shotgun that only fits 4 shells). If a round requires more shells, the game will automatically pause and guide the dealer through a "Partial Reload" phase.
- The Dealer interface contains all secret information (shell order, medicine contents, phone results).

### The Players
The Players use their smartphones as visual monitors. 
- Access the player interface via `/` and click "join game".
- Players **do not** interact with their phones. All actions are performed in real life and logged by the Dealer.

#### Game Modes

**1. Multiplayer (2-4 Players)**
- Requires 2 to 4 real players connecting with their own phones.
- Player screens are placed horizontally in front of each player, displaying their personal HP and turn status.
- Shell generation follows a batch logic.

**2. Solo / 1v1 (Player vs Dealer)**
- Authentically recreates the original video game experience.
- Requires only **1 player** to connect via phone. The game automatically spawns a virtual `DEALER` opponent.
- The player's smartphone acts as the **central table monitor** and must be placed horizontally between the player and the physical dealer.
- The interface features a **split-screen (Dual-HP)** design: The Dealer's HP is displayed on the top half (inverted for the dealer to read from across the table) and the Player's HP on the bottom half.
- **Exclusive Item (Expired Medicine):** Available only in this mode. Functions with a true 50/50 chance (+2 HP or -1 HP). The physical dealer will be informed whether it is "Vodka" (+2) or "Water" (-1) via their interface to pour the correct drink.
- Shell generation mimics the original game (e.g., Round 1 is strictly 1 Live, 2 Blank).

## Quick Start (one command, Docker)

The whole project — network auto-config, board flashing, and the server — starts
with a single command:

```bash
./start.sh
```

What it does, in order:
1. **Auto-detects** the current Wi-Fi (SSID/password) and this host's LAN IP and
   writes them into `config.json` (via `scripts/net_config.sh`).
2. **Flashes the ESP32** with those settings over USB (via `esp/flash.sh`).
   *Runs on the host* — Docker Desktop on macOS/Windows does not pass USB through
   to containers, so flashing happens outside Docker (a plugged-in board is
   detected automatically; if none is found the step is skipped).
3. **Builds and runs the server** in Docker (`docker compose up`), exposing it on
   the host's LAN IP so the board and every player's phone can reach it.

Useful flags:

```bash
./start.sh --no-flash    # skip the board (network + server only)
./start.sh --no-net      # keep config.json as-is (don't re-detect the network)
./start.sh --sudo        # allow sudo to read a hidden SSID (macOS 26+)
./start.sh --detach      # run the server in the background
./start.sh --down        # stop the server
```

**Requirements:** Docker + Docker Compose (and, for flashing, `arduino-cli` with a
board on USB). Nothing else — Python and all dependencies live inside the image.

**First-run setup wizard.** On a fresh machine (when `config.json` still holds the
example placeholders) `start.sh` opens the browser and the server redirects `/dealer`
to a web wizard at **`/setup`**. It auto-detects the host's LAN IP and asks for the
Wi-Fi SSID/password, server address and RF trigger code, then writes `config.json`
and drops you into the dealer dashboard to create the first game. Once configured,
`/dealer` opens normally; the wizard stays reachable at `/setup` for later edits.
(For this the container mounts `config.json` read-write — see `docker-compose.yml`.)

Prefer to run the server in Docker directly (no host-side steps):

```bash
docker compose up --build          # foreground
docker compose up --build -d       # background
```

`config.json` is mounted into the container read-only, so edits to network/IP/port
apply on the next start without rebuilding the image.

## How to Run (bare Python, without Docker)

1. **Prerequisites**: Python 3.9+
2. **Install dependencies**:
   ```bash
   pip install fastapi uvicorn websockets jinja2
   ```
3. **Start the server**:
   ```bash
   python -m app.server
   ```
4. **Access the application**:
   - The app will be available on your local network at `http://0.0.0.0:8000`.
   - Ensure all players and the dealer are on the same Wi-Fi network to connect via the host's local IP address (e.g., `http://192.168.1.X:8000`).

## ESP32 Hardware (Solenoid Trigger)

The physical shotgun uses an ESP32 that talks to the server over Wi-Fi. All settings
live in one place — `config.json` in the repo root (copy it from `config.example.json`).
The Python server reads it directly; the firmware gets the same values via a generated
`esp/config.h`.

**Auto-fill the network settings.** Instead of editing Wi-Fi SSID / password / server URL
by hand, run the collector for your OS — it detects the current Wi-Fi network and this
host's LAN IP and writes them into `config.json` (comments are preserved):

```bash
./scripts/net_config.sh            # macOS / Linux
./scripts/net_config.sh --dry-run  # just show what was detected
./scripts/net_config.sh --flash    # write, then reflash the board
```
```powershell
./scripts/net_config.ps1           # Windows / PowerShell
./scripts/net_config.ps1 -DryRun
./scripts/net_config.ps1 -Flash
```

It fills `esp.wifi_ssid`, `esp.wifi_password` (from the OS keychain / saved profile) and
`esp.server_base_url` (`http://<host-IP>:<port>`). If the password can't be read it's left
untouched with a warning — set it manually.

**Flash the board.** After the config is set, generate `esp/config.h`, compile and upload:

```bash
./esp/flash.sh                     # port auto-detected; requires arduino-cli
```

## Design & Style Guide

- **Theme**: Retro CRT terminal aesthetic.
- **Colors**: Strictly uses a predefined palette:
  - Background: Very dark grey/black (`#0a0a0a`)
  - Text: Light grey (`#e0e0e0`)
  - Accent Green: Bright phosphor green (`#4af626`)
  - Accent Red: Danger red (`#ff2a2a`)
- **Typography**: Uses monospace fonts (Courier/Consolas) to mimic terminal output.
- **Animations**: Subtle CRT flicker, blinking cursors, and CSS transitions for round changes.
- **Responsiveness**: Player screens are strictly optimized for horizontal (landscape) mobile view.
