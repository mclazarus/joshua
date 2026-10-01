"""Turn "what WarGear says now" plus "what we knew before" into events.

``diff`` is pure. ``apply`` writes the result to the store. Reminders don't hang
off these events: they come from the open-turn rows in the DB, so a restart or a
game that only just became tracked still gets its reminders.
"""

from __future__ import annotations

from dataclasses import dataclass

from joshua.db import Store
from joshua.wargear.models import GameState


@dataclass(frozen=True)
class TurnStarted:
    gameid: int
    player: str
    turnstamp: int
    started_at: int
    deadline: int | None


@dataclass(frozen=True)
class TurnEnded:
    gameid: int
    player: str


@dataclass(frozen=True)
class PlayerEliminated:
    gameid: int
    player: str


@dataclass(frozen=True)
class GameFinished:
    gameid: int
    winners: tuple[str, ...]
    terminated: bool


@dataclass(frozen=True)
class GameAppeared:
    gameid: int
    host: str | None
    finished: bool


Event = TurnStarted | TurnEnded | PlayerEliminated | GameFinished | GameAppeared


def _counter(state: GameState, player: str) -> int | None:
    return next((p.turn_counter for p in state.players if p.name == player), None)


def _new_turn(prev: GameState, new: GameState, player: str) -> bool:
    """Is ``player``, current in both snapshots, on a *different* turn now?

    This happens in a 2-player game when the turn goes around between polls.
    Prefer the per-player turn counter. Fall back to turnstamp, except in
    simultaneous games, where anyone's move changes it.
    """
    before, after = _counter(prev, player), _counter(new, player)
    if before is not None and after is not None:
        return after != before
    if new.simultaneous:
        return False
    return bool(new.turnstamp and prev.turnstamp and new.turnstamp != prev.turnstamp)


def diff(prev: GameState | None, new: GameState, now: int) -> list[Event]:
    events: list[Event] = []
    if prev is None:
        events.append(GameAppeared(new.gameid, new.host, new.finished))

    before = prev.current if prev else frozenset()
    after = frozenset() if new.finished else new.current

    for player in sorted(before):
        if player not in after or (prev and _new_turn(prev, new, player)):
            events.append(TurnEnded(new.gameid, player))

    for player in sorted(after):
        if player not in before or (prev and _new_turn(prev, new, player)):
            stamp = new.turnstamp or now
            deadline = stamp + new.boot_time if new.boot_time else None
            events.append(TurnStarted(new.gameid, player, stamp, stamp, deadline))

    if prev is not None:
        for player in sorted(new.eliminated - prev.eliminated):
            events.append(PlayerEliminated(new.gameid, player))
        if new.finished and not prev.finished:
            events.append(GameFinished(new.gameid, new.winners, new.terminated))

    return events


async def apply(store: Store, new: GameState, now: int, seen_via: str | None) -> list[Event]:
    prev = await store.game_state(new.gameid)
    events = diff(prev, new, now)
    await store.save_game(new, seen_via)
    for e in events:
        match e:
            case TurnEnded():
                await store.end_turn(e.gameid, e.player, now)
            case TurnStarted():
                await store.start_turn(e.gameid, e.player, e.turnstamp, e.started_at, e.deadline)
    return events
