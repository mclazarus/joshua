import json
from pathlib import Path

from joshua.wargear.models import parse_game_list

# Hand-written from the Swagger spec until Phase 0 captures real responses.
PAYLOAD = {
    "games": [
        {
            "gameid": "123456",
            "name": "Shall We Play A Game",
            "boardname": "Global Thermonuclear War",
            "host": "11",
            "gamestatus": "In Progress",
            "gametype": "Turn Based",
            "turnstamp": "1790000000",
            "boot_time": "86400",
            "players": [
                {"id": "11", "name": "Falken", "seat": "1", "status": "Playing"},
                {"id": "22", "name": "Lightman", "seat": "2", "status": "Playing"},
                {"id": "33", "name": "McKittrick", "seat": "3", "status": "Eliminated"},
            ],
            "current_turn": ["22"],
            "winners": [],
        }
    ]
}


def test_normalize_resolves_ids_to_names():
    [game] = parse_game_list(PAYLOAD)
    s = game.normalize()
    assert s.gameid == 123456
    assert s.current == {"Lightman"}
    assert s.host == "Falken"
    assert s.eliminated == {"McKittrick"}
    assert s.deadline == 1790000000 + 86400
    assert not s.finished


def test_finished_game_and_dict_shaped_lists():
    payload = {
        "1": {
            **PAYLOAD["games"][0],
            "gamestatus": "Finished",
            "winners": {"0": "11"},
            "current_turn": "",
            "players": {"a": {"id": "11", "name": "Falken", "seat": 1}},
        }
    }
    [game] = parse_game_list(payload)
    s = game.normalize()
    assert s.finished
    assert s.winners == ("Falken",)
    assert s.current == frozenset()


def test_seat_references_resolve():
    payload = {"games": [{**PAYLOAD["games"][0], "current_turn": [{"seat": 1}]}]}
    assert parse_game_list(payload)[0].normalize().current == {"Falken"}


# --- real (sanitized) WarGear responses, captured 2026-10-01 ---------------------------

FIXTURES = Path(__file__).parent / "fixtures"


def load(name):
    return {g.gameid: g.normalize() for g in parse_game_list(json.loads((FIXTURES / name).read_text()))}


def test_real_live_game():
    s = load("my_live.json")[81545973]
    assert s.status == "Live" and not s.finished and not s.open
    assert s.current == {"beringer"}  # current_turn holds names
    assert s.host == "mckittrick"  # host is an id hash, resolved via players
    assert s.gametype == "Turn Based"
    assert s.deadline == 1790867570 + 86400
    # time_remaining agrees with turnstamp + boot_time to within a minute of fetch time
    assert s.extra["time_remaining"] == 74326
    assert {p.name: p.turn_counter for p in s.players}["beringer"] == 4


def test_real_open_games_have_nobody_up():
    games = load("my_live.json")
    opens = [g for g in games.values() if g.open]
    assert len(opens) == 2
    assert all(not g.current and not g.finished for g in opens)
    assert all(p.name for g in opens for p in g.players)  # empty seats dropped


def test_real_finished_games_decode_php_serialized_ids():
    games = load("my_finished.json")
    assert games[81533430].winners == ("mckittrick",)
    assert games[81533430].eliminated == {"jennifer"}
    assert games[81533430].finished
    team = games[498655]
    assert team.simultaneous
    assert len(team.winners) == 3
