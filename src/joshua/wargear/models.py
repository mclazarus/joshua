"""WarGear API shapes, checked against real responses (see docs/wargear-api.md).

Quirks this module absorbs:
* numbers arrive as strings, and ``0`` means "unset" for timestamps;
* ``players`` is a dict keyed by seat (sometimes a list), with empty seats in open games;
* ``current_turn`` is a list of player *names*, ``[null]`` when nobody is up;
* ``winners``/``eliminated`` are PHP-serialized arrays of player *id hashes*
  (``a:1:{i:0;s:32:"…";}``), or null/"" when empty;
* ``host`` is a player id hash.

``Game.normalize()`` turns a ``Game`` into the ``GameState`` the rest of the bot uses.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator, model_validator

log = logging.getLogger(__name__)


PHP_STRING = re.compile(r's:\d+:"(.*?)";')
NO_TIMER = 9_999_999_999  # time_remaining when a game has no running clock
ELIMINATED_STATUSES = {"eliminated", "booted", "surrendered"}


def _as_list(v: Any) -> list[Any]:
    if v is None or v == "" or v is False:
        return []
    if isinstance(v, dict):
        v = list(v.values())
    elif isinstance(v, str):
        v = PHP_STRING.findall(v) if v.startswith("a:") else [p.strip() for p in v.split(",")]
    elif not isinstance(v, list):
        v = [v]
    return [x for x in v if x not in (None, "")]


class WGModel(BaseModel):
    model_config = ConfigDict(extra="allow", coerce_numbers_to_str=True)

    @model_validator(mode="before")
    @classmethod
    def _blank_is_none(cls, data: Any) -> Any:
        # WarGear uses "" and "?" (hidden by fog or not yet known) for missing values.
        if isinstance(data, dict):
            return {k: (None if v in ("", "?") else v) for k, v in data.items()}
        return data


class Player(WGModel):
    id: str | None = None
    name: str | None = None
    seat: int | None = None
    status: str | None = None
    team: str | None = None
    phase: str | None = None
    color_name: str | None = None
    turn_counter: int | None = None
    autoboot_next_turn: int | None = None


class Game(WGModel):
    gameid: int
    name: str = ""
    boardid: int | None = None
    boardname: str | None = None
    host: str | None = None
    num_players: int | None = None
    players: list[Player] = []
    eliminated: list[Any] = []
    current_turn: list[Any] = []
    winners: list[Any] = []
    gamestatus: str | None = None
    gametype: str | None = None  # Private/Public
    gameplay_type: str | None = None  # Turn Based/Simultaneous
    turnstamp: int | None = None
    createstamp: int | None = None
    startstamp: int | None = None
    endstamp: int | None = None
    boot_type: str | None = None
    boot_time: int | None = None
    time_remaining: int | None = None

    @field_validator("players", "eliminated", "current_turn", "winners", mode="before")
    @classmethod
    def _listify(cls, v: Any) -> list[Any]:
        return _as_list(v)

    def normalize(self) -> GameState:
        return GameState.from_game(self)


@dataclass(frozen=True)
class PlayerState:
    name: str
    id: str | None = None
    seat: int | None = None
    status: str | None = None
    turn_counter: int | None = None

    @property
    def eliminated(self) -> bool:
        return (self.status or "").lower() in ELIMINATED_STATUSES

    @property
    def joined(self) -> bool:
        """For open games: took a seat. Invitees show as "Invited" (or "?" on older games)."""
        return (self.status or "").lower() == "joined"


@dataclass(frozen=True)
class GameState:
    """A normalized snapshot of one game, keyed by WarGear player *names*."""

    gameid: int
    name: str
    board: str | None
    host: str | None
    status: str | None
    gametype: str | None
    players: tuple[PlayerState, ...]
    seats: int | None
    current: frozenset[str]
    winners: tuple[str, ...]
    eliminated: frozenset[str]
    turnstamp: int | None
    boot_time: int | None
    createstamp: int | None
    startstamp: int | None
    endstamp: int | None
    extra: dict[str, Any] = field(default_factory=dict, compare=False)

    @property
    def finished(self) -> bool:
        s = (self.status or "").lower()
        if s in ("finished", "terminated"):
            return True
        return bool(self.winners) or bool(self.endstamp)

    @property
    def open(self) -> bool:
        """Created but waiting for players to join."""
        return (self.status or "").lower() == "open"

    @property
    def joined(self) -> tuple[str, ...]:
        return tuple(p.name for p in self.players if p.joined)

    @property
    def invited(self) -> tuple[str, ...]:
        """Invited to an open game but haven't joined yet."""
        return tuple(p.name for p in self.players if not p.joined) if self.open else ()

    @property
    def spots_left(self) -> int | None:
        if not self.open or not self.seats:
            return None
        return max(0, self.seats - len(self.joined))

    @property
    def terminated(self) -> bool:
        return "terminat" in (self.status or "").lower()

    @property
    def simultaneous(self) -> bool:
        return "simul" in (self.gametype or "").lower()

    @property
    def deadline(self) -> int | None:
        """Unix time this turn gets skipped, if the game has a boot timer."""
        if self.turnstamp and self.boot_time:
            return self.turnstamp + self.boot_time
        return None

    @classmethod
    def from_game(cls, g: Game) -> GameState:
        players = tuple(
            PlayerState(
                name=p.name or f"seat {p.seat}",
                id=p.id,
                seat=p.seat,
                status=p.status,
                turn_counter=p.turn_counter,
            )
            for p in g.players
            if p.name or p.id  # open games list empty seats
        )

        def resolve(ref: Any) -> str:
            # Entries may be names, player ids, seats, or small dicts.
            if isinstance(ref, dict):
                ref = ref.get("name") or ref.get("id") or ref.get("seat")
            text = str(ref).strip()
            # Prefer id/name matches; a bare small number could also be a seat.
            for p in players:
                if text in (p.id, p.name):
                    return p.name
            for p in players:
                if p.seat is not None and text == str(p.seat):
                    return p.name
            return text

        eliminated = {resolve(e) for e in g.eliminated}
        eliminated |= {p.name for p in players if p.eliminated}
        winners = [resolve(w) for w in g.winners] or [
            p.name for p in players if (p.status or "").lower() == "winner"
        ]
        return cls(
            gameid=g.gameid,
            name=g.name,
            board=g.boardname,
            host=resolve(g.host) if g.host not in (None, "", "0") else None,
            status=g.gamestatus,
            gametype=g.gameplay_type or g.gametype,
            players=players,
            seats=g.num_players or None,
            current=frozenset(resolve(c) for c in g.current_turn),
            winners=tuple(winners),
            eliminated=frozenset(eliminated),
            turnstamp=g.turnstamp or None,
            boot_time=g.boot_time or None,
            createstamp=g.createstamp or None,
            startstamp=g.startstamp or None,
            endstamp=g.endstamp or None,
            extra={
                "time_remaining": None if g.time_remaining == NO_TIMER else g.time_remaining,
                "boot_type": g.boot_type,
            },
        )


def parse_game_list(payload: Any) -> list[Game]:
    """Pull games out of a GetGameList response, however it's nested."""
    games: list[Any] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if "gameid" in node:
                games.append(node)
                return
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(payload)
    seen: dict[int, Game] = {}
    for raw in games:
        try:
            g = Game.model_validate(raw)
        except ValidationError as e:
            log.warning("skipping unparseable game %s: %s", raw.get("gameid"), e)
            continue
        seen[g.gameid] = g
    return list(seen.values())
