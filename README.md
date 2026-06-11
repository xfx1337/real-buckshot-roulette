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
- The Dealer configures the game, hands out physical items, loads the physical shotgun, and mirrors all physical actions in the digital interface.
- The Dealer interface contains all secret information (shell order, medicine contents, phone results).

### The Players
The Players use their smartphones as visual monitors. 
- Access the player interface via `/` and click "join game".
- The player screens should be placed horizontally in front of each player.
- The screens display current HP (lightning bolts), turn status (`[ this player turn ]`, `[ handcuffed ]`), and game outcomes.
- Players **do not** interact with their phones. All actions are performed in real life and logged by the Dealer.

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
