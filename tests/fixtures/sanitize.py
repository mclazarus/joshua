"""Turn raw `joshua discover` output into committed fixtures with fake names and ids.

uv run python tests/fixtures/sanitize.py
"""

import hashlib
import json
import re
from pathlib import Path

HERE = Path(__file__).parent
RAW = HERE / "raw"
CAST = [
    "falken",
    "lightman",
    "jennifer",
    "mckittrick",
    "beringer",
    "cabot",
    "watson",
    "malvin",
    "jim_sting",
    "pat_healy",
    "conley",
    "richter",
    "wigan",
    "nigan",
    "colonel",
    "joshua_jr",
]
names: dict[str, str] = {}
ids: dict[str, str] = {}


def fake_name(real: str) -> str:
    if real not in names:
        names[real] = CAST[len(names)] if len(names) < len(CAST) else f"player{len(names)}"
    return names[real]


def fake_id(real: str) -> str:
    if real and real not in ids:
        ids[real] = hashlib.md5(f"wopr-{real}".encode()).hexdigest()
    return ids.get(real, real)


def scrub(game: dict) -> dict:
    players = game.get("players") or {}
    items = players.values() if isinstance(players, dict) else players
    for p in items:
        if p.get("name"):
            p["name"] = fake_name(p["name"])
        if p.get("id"):
            p["id"] = fake_id(p["id"])
    if game.get("host"):
        game["host"] = fake_id(game["host"])
    game["current_turn"] = [fake_name(c) if c else c for c in game.get("current_turn") or []] or game.get(
        "current_turn"
    )
    for field in ("winners", "eliminated"):
        v = game.get(field)
        if isinstance(v, str):
            game[field] = re.sub(r'"([0-9a-f]{32})"', lambda m: f'"{fake_id(m.group(1))}"', v)
    for field in ("msgstamps", "visitstamps"):
        if isinstance(game.get(field), dict):
            game[field] = {fake_name(k): v for k, v in game[field].items()}
    game["name"] = f"Game {game['gameid']}"
    return game


def main() -> None:
    live = json.loads((RAW / "my_live.json").read_text())
    finished = json.loads((RAW / "my_finished.json").read_text())
    (HERE / "my_live.json").write_text(json.dumps([scrub(g) for g in live], indent=1))
    pick = [g for g in finished if g["gameplay_type"] == "Simultaneous"][:1] + finished[-3:]
    (HERE / "my_finished.json").write_text(json.dumps([scrub(g) for g in pick], indent=1))
    print("wrote fixtures; name map size", len(names))


if __name__ == "__main__":
    main()
