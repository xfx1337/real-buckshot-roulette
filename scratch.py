import copy
from game_engine import GameState, Player

def test_deepcopy():
    g = GameState()
    g.add_player("Alice")
    g.add_player("Bob")
    g.start_game()
    g2 = copy.deepcopy(g)
    g.players[list(g.players.keys())[0]].hp = 999
    
    print("Original HP:", g.players[list(g.players.keys())[0]].hp)
    print("Copied HP:", g2.players[list(g2.players.keys())[0]].hp)

if __name__ == "__main__":
    test_deepcopy()
