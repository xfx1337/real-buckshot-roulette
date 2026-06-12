# Buckshot Roulette IRL

A multiplayer IRL (In Real Life) adaptation of the popular game "Buckshot Roulette". 
This system provides the digital backend and frontend interfaces needed to manage a physical game session with 2-4 players and 1 Dealer (Game Master).

## Architecture Overview

- **Backend Framework**: Python with FastAPI and Uvicorn.
- **State Management**: A global, in-memory `GameEngine` class handles all game logic, phases, turn order, items, and health.
- **Real-time Communication**: WebSocket connections broadcast state changes instantly to all connected clients (Dealer and Players).
- **Frontend**: Vanilla HTML/JS/CSS styled as a retro CRT terminal. Templates are rendered via Jinja2.

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

## How to Run

1. **Prerequisites**: Python 3.9+
2. **Install dependencies**:
   ```bash
   pip install fastapi uvicorn websockets jinja2
   ```
3. **Start the server**:
   ```bash
   python server.py
   ```
4. **Access the application**:
   - The app will be available on your local network at `http://0.0.0.0:8000`.
   - Ensure all players and the dealer are on the same Wi-Fi network to connect via the host's local IP address (e.g., `http://192.168.1.X:8000`).

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
