"""Turn raw `joshua discover` output into committed fixtures with fake names and ids.

uv run python tests/fixtures/sanitize.py
"""

import gzip
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


def scrub_history(payload: dict) -> dict:
    """Keep only the fields stats use. ``gp`` holds player id hashes and ``country`` is personal."""
    turns = payload["history"]["turn"]
    keep = []
    for t in turns:
        a = t["@attributes"]
        keep.append(
            {"@attributes": {k: v for k, v in a.items() if v not in ("0", "") and k not in ("gp", "country")}}
        )
    return {"history": {"turn": keep}}


def scrub_board(payload: dict) -> dict:
    """Board name, dice/fog rules and territory names: public board data, minus the designer."""
    board = payload["board"]["@attributes"]
    rules = payload["rules"]["@attributes"]
    return {
        "board": {"@attributes": {"boardid": board["boardid"], "boardname": board["boardname"]}},
        "rules": {
            "@attributes": {
                k: rules[k]
                for k in ("boardid", "attacker_dice_sides", "defender_dice_sides", "fog")
                if k in rules
            }
        },
        "territories": {
            "territory": [
                {"@attributes": {"tid": t["@attributes"]["tid"], "name": t["@attributes"]["name"]}}
                for t in payload["territories"]["territory"]
            ]
        },
    }


def main() -> None:
    live = json.loads((RAW / "my_live.json").read_text())
    finished = json.loads((RAW / "my_finished.json").read_text())
    (HERE / "my_live.json").write_text(json.dumps([scrub(g) for g in live], indent=1))
    pick = [g for g in finished if g["gameplay_type"] == "Simultaneous"][:1] + finished[-3:]
    (HERE / "my_finished.json").write_text(json.dumps([scrub(g) for g in pick], indent=1))

    # One finished game with its history and board, for the after-action stats.
    for history in sorted(RAW.glob("history_*.json")):
        gameid = int(history.stem.split("_")[1])
        game = next(g for g in finished if int(g["gameid"]) == gameid)
        board = RAW / f"board_{game['boardid']}.json"
        (HERE / "history_game.json").write_text(json.dumps(scrub(game), indent=1))
        with gzip.open(HERE / "history.json.gz", "wt") as f:
            json.dump(scrub_history(json.loads(history.read_text())), f, separators=(",", ":"))
        (HERE / "board.json").write_text(json.dumps(scrub_board(json.loads(board.read_text())), indent=1))
        break
    print("wrote fixtures; name map size", len(names))


if __name__ == "__main__":
    main()
