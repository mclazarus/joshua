"""After-action stats from a game's GetHistoryUpdate log. Pure: no I/O.

The log is every action in order (see docs/wargear-api.md). We replay it to track who owns each
territory, and score each dice roll against the odds:

* ``ad``/``dd`` are the actual faces rolled, e.g. ``"6,4,1"`` vs ``"5,3"``. Highest dice pair off
  and the defender wins ties; ``al``/``dl`` are the units each side lost.
* ``bmod`` is ``"<attacker>,<defender>"`` extra die *sides* from border modifiers: ``"2,0"`` means
  the attacker rolled d8s on a d6 board.

Luck is "enemy units killed minus what the odds expected". It's zero-sum per roll: whatever the
attacker gains, the defender loses.

We also count troops on each territory. ``place``/``factory_place`` add ``count``, ``occupy``,
``fortify`` and ``transfer`` move ``count`` (missing means 0; that reading matches every roll in
the sample game), and attacks remove ``al``/``dl``. From that:

* a **deathball** is a stack of more than ``DEATHBALL_MIN`` troops on one territory during its
  owner's turn;
* a **turtle** turn attacks at most one territory (enough for a card), not counting skipped turns.
  It's worse when the player started the turn with the most troops on the board;
* a **possible alliance** is two players who border each other (judged from the territories that
  were ever attacked across) yet go ``PACT_ROUNDS`` of their own turns each without attacking
  each other, while both attack the same third player. Only counted with 3+ players left, from
  each player's ``PACT_FROM_ROUND``th turn on (nobody has fought anyone in the opening), and
  never between teammates. In a team game with any team unknown, nobody is called allied.

Seats are strings: ``"1"``, ``"2"``… for players and ``"N"`` (or ``"0"``) for neutral territories.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from functools import lru_cache
from itertools import product
from typing import Any

NEUTRAL = {"N", "0", ""}
# Rolls whose outcome doesn't match "highest dice pair off, defender wins ties" mean the board
# uses rules we don't model. Past this share of rolls, we don't report luck at all.
MAX_MISMATCH = 0.05
# A stack has to be bigger than this to be a deathball.
DEATHBALL_MIN = 20
# Turns each, at peace while bordering, before two players look allied.
PACT_ROUNDS = 5
# Turns before a player's PACT_FROM_ROUND-th don't count towards an alliance.
PACT_FROM_ROUND = 3
# Don't enumerate more outcomes than this for one roll (3d8 vs 2d8 is 32k).
MAX_OUTCOMES = 2_000_000


@dataclass(frozen=True)
class Action:
    turnid: int
    timestamp: int
    action: str
    seat: str
    tid: int = 0
    fromid: int = 0
    toid: int = 0
    count: int = 0
    seats: str = ""
    cards: str = ""
    ad: tuple[int, ...] = ()
    dd: tuple[int, ...] = ()
    bmod: tuple[int, int] = (0, 0)
    al: int = 0
    dl: int = 0


def _int(v: Any) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def _dice(v: Any) -> tuple[int, ...]:
    return tuple(int(x) for x in str(v or "").split(",") if x.strip().isdigit())


def parse_history(payload: Any) -> list[Action]:
    """``{"history": {"turn": [{"@attributes": {...}}, ...]}}`` into a list of actions, in order."""
    turns = ((payload or {}).get("history") or {}).get("turn") or []
    if isinstance(turns, dict):
        turns = [turns]
    out = []
    for t in turns:
        a = t.get("@attributes", t) if isinstance(t, dict) else {}
        if not a.get("action"):
            continue
        bmod = _dice(a.get("bmod")) + (0, 0)
        out.append(
            Action(
                turnid=_int(a.get("turnid")),
                timestamp=_int(a.get("timestamp")),
                action=str(a["action"]),
                seat=str(a.get("s") or ""),
                tid=_int(a.get("tid")),
                fromid=_int(a.get("fromid")),
                toid=_int(a.get("toid")),
                count=_int(a.get("count")),
                seats=str(a.get("seats") or ""),
                cards=str(a.get("cards") or ""),
                ad=_dice(a.get("ad")),
                dd=_dice(a.get("dd")),
                bmod=(bmod[0], bmod[1]),
                al=_int(a.get("al")),
                dl=_int(a.get("dl")),
            )
        )
    return sorted(out, key=lambda x: x.turnid)


def game_win(actions: Iterable[Action]) -> Action | None:
    """The ``game_win`` action (``s`` = the winning seat), if the game has been won."""
    return next((a for a in actions if a.action == "game_win" and a.seat), None)


def resolve(ad: Iterable[int], dd: Iterable[int]) -> tuple[int, int]:
    """(attacker lost, defender lost) under standard rules: pair off highest dice, ties to defender."""
    al = dl = 0
    for a, d in zip(sorted(ad, reverse=True), sorted(dd, reverse=True), strict=False):
        if a > d:
            dl += 1
        else:
            al += 1
    return al, dl


@lru_cache(maxsize=256)
def expected_losses(a_dice: int, d_dice: int, a_sides: int, d_sides: int) -> tuple[float, float] | None:
    """Expected (attacker lost, defender lost) for one roll, by enumerating every outcome."""
    n = a_sides**a_dice * d_sides**d_dice
    if n == 0 or n > MAX_OUTCOMES:
        return None
    al = dl = 0
    d_rolls = list(product(range(1, d_sides + 1), repeat=d_dice))
    for ar in product(range(1, a_sides + 1), repeat=a_dice):
        for dr in d_rolls:
            x, y = resolve(ar, dr)
            al += x
            dl += y
    return al / n, dl / n


@dataclass
class PlayerStats:
    seat: str
    rolls: int = 0  # attack rolls made
    conquests: int = 0
    card_trades: int = 0
    units_lost: int = 0
    units_killed: int = 0
    luck: float = 0.0  # enemy units killed beyond what the odds expected
    peak_territories: int = 0
    peak_at: int | None = None
    deathball_turns: int = 0
    turtle_turns: int = 0
    turtle_leading: int = 0  # turtle turns started with the most troops on the board


@dataclass(frozen=True)
class Battle:
    """Consecutive rolls from one territory into another, in one turn."""

    attacker: str
    defender: str
    fromid: int
    toid: int
    attacker_lost: int
    defender_lost: int
    rolls: int
    conquered: bool
    at: int

    @property
    def losses(self) -> int:
        return self.attacker_lost + self.defender_lost


@dataclass(frozen=True)
class BloodyTurn:
    seat: str
    losses: int  # units lost by everyone during this player's turn
    at: int


@dataclass(frozen=True)
class Deathball:
    seat: str
    units: int
    tid: int
    at: int


@dataclass(frozen=True)
class Pact:
    """Two neighbours who left each other alone while both hit someone else."""

    seats: tuple[str, str]
    rounds: int  # turns each took in the pact, the fewer of the two
    enemies: tuple[str, ...]  # attacked by both during the pact
    start: int
    end: int


@dataclass
class _Truce:
    turns: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    hit: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))
    start: int = 0
    end: int = 0


@dataclass(frozen=True)
class Elimination:
    victim: str
    by: str | None  # None when booted or surrendered
    how: str  # "eliminated", "booted", "surrendered"
    at: int


@dataclass
class GameStats:
    players: dict[str, PlayerStats] = field(default_factory=dict)
    territories_total: int = 0
    biggest_battle: Battle | None = None
    bloodiest_turn: BloodyTurn | None = None
    biggest_deathball: Deathball | None = None
    pacts: list[Pact] = field(default_factory=list)
    eliminations: list[Elimination] = field(default_factory=list)
    luck_counted: bool = True

    def player(self, seat: str) -> PlayerStats:
        if seat not in self.players:
            self.players[seat] = PlayerStats(seat)
        return self.players[seat]

    def ranked(self, key: str, seats: Iterable[str]) -> list[PlayerStats]:
        """Players (only the given seats, so never neutral) sorted by ``key``, highest first."""
        wanted = set(seats)
        return sorted(
            (p for s, p in self.players.items() if s in wanted), key=lambda p: getattr(p, key), reverse=True
        )


def replay(
    actions: list[Action],
    attacker_sides: int = 6,
    defender_sides: int = 6,
    territories_total: int = 0,
    teams: Mapping[str, str | None] | None = None,
) -> GameStats:
    """``teams`` maps seat to team for a team game (None for a seat whose team is unknown).
    Leave it out for a free-for-all."""
    stats = GameStats()
    owner: dict[int, str] = {}
    units: dict[int, int] = defaultdict(int)
    mismatches = scored = 0

    # Territories that border each other, as far as the log shows: any pair ever attacked across.
    borders = {
        (x, y)
        for a in actions
        if a.action == "attack" and a.fromid and a.toid
        for x, y in ((a.fromid, a.toid), (a.toid, a.fromid))
    }

    battle: list[Action] = []
    battle_defender = ""

    def close_battle(conquered: bool = False) -> None:
        nonlocal battle
        if not battle:
            return
        b = Battle(
            attacker=battle[0].seat,
            defender=battle_defender,
            fromid=battle[0].fromid,
            toid=battle[0].toid,
            attacker_lost=sum(a.al for a in battle),
            defender_lost=sum(a.dl for a in battle),
            rolls=len(battle),
            conquered=conquered,
            at=battle[-1].timestamp,
        )
        if stats.biggest_battle is None or b.losses > stats.biggest_battle.losses:
            stats.biggest_battle = b
        battle = []

    turn_seat: str | None = None
    turn_losses = 0
    turn_at = 0
    turn_targets: set[int] = set()  # territories attacked this turn
    turn_victims: set[str] = set()  # seats attacked this turn
    turn_skipped = False
    turn_peak = 0
    turn_leading = False
    turn_neighbours: set[str] = set()
    truces: dict[tuple[str, str], _Truce] = {}
    turns_taken: dict[str, int] = defaultdict(int)

    def could_ally(x: str, y: str) -> bool:
        if teams is None:
            return True
        tx, ty = teams.get(x), teams.get(y)
        return bool(tx and ty and tx != ty)

    def players_on_board() -> set[str]:
        return {s for s in owner.values() if s not in NEUTRAL}

    def end_truce(pair: tuple[str, str]) -> None:
        t = truces.pop(pair, None)
        if t is None:
            return
        rounds = min(t.turns[pair[0]], t.turns[pair[1]])
        enemies = (t.hit[pair[0]] & t.hit[pair[1]]) - set(pair) - NEUTRAL
        if rounds >= PACT_ROUNDS and enemies and could_ally(*pair):
            stats.pacts.append(Pact(pair, rounds, tuple(sorted(enemies)), t.start, t.end))

    def grew(tid: int, at: int) -> None:
        nonlocal turn_peak
        if turn_seat is None or owner.get(tid) != turn_seat:
            return
        turn_peak = max(turn_peak, units[tid])
        if units[tid] > DEATHBALL_MIN and (
            stats.biggest_deathball is None or units[tid] > stats.biggest_deathball.units
        ):
            stats.biggest_deathball = Deathball(turn_seat, units[tid], tid, at)

    def open_turn(seat: str, at: int) -> None:
        nonlocal turn_seat, turn_at, turn_skipped, turn_peak, turn_leading, turn_neighbours
        turn_seat, turn_at, turn_skipped, turn_peak = seat, at, False, 0
        turns_taken[seat] += 1
        turn_targets.clear()
        turn_victims.clear()
        troops: dict[str, int] = defaultdict(int)
        for tid, s in owner.items():
            if s not in NEUTRAL:
                troops[s] += units[tid]
        turn_leading = bool(troops) and troops.get(seat, 0) == max(troops.values()) and len(troops) > 1
        turn_neighbours = set()
        if len(troops) >= 3:
            for x, y in borders:
                if owner.get(x) == seat and owner.get(y) not in NEUTRAL and owner.get(y) != seat:
                    turn_neighbours.add(owner[y])

    def close_turn(at: int) -> None:
        nonlocal turn_seat, turn_losses
        if (
            turn_seat
            and turn_losses
            and (stats.bloodiest_turn is None or turn_losses > stats.bloodiest_turn.losses)
        ):
            stats.bloodiest_turn = BloodyTurn(turn_seat, turn_losses, turn_at)
        if turn_seat and not turn_skipped:
            p = stats.player(turn_seat)
            if turn_peak > DEATHBALL_MIN:
                p.deathball_turns += 1
            if len(turn_targets) <= 1:
                p.turtle_turns += 1
                p.turtle_leading += turn_leading
        if turn_seat:
            for other in turn_victims:
                end_truce(tuple(sorted((turn_seat, other))))
            at_peace = turn_neighbours - turn_victims
            if turns_taken[turn_seat] < PACT_FROM_ROUND:
                at_peace = set()  # the opening: nobody has fought anyone yet
            for other in at_peace:
                pair = tuple(sorted((turn_seat, other)))
                t = truces.setdefault(pair, _Truce(start=turn_at))
                t.turns[turn_seat] += 1
                t.hit[turn_seat] |= turn_victims
                t.end = at
        turn_seat, turn_losses = None, 0

    def snapshot(at: int) -> None:
        counts: dict[str, int] = defaultdict(int)
        for seat in owner.values():
            if seat not in NEUTRAL:
                counts[seat] += 1
        for seat, n in counts.items():
            p = stats.player(seat)
            if n > p.peak_territories:
                p.peak_territories, p.peak_at = n, at

    for a in actions:
        if battle and not (
            a.action == "attack"
            and (a.seat, a.fromid, a.toid) == (battle[0].seat, battle[0].fromid, battle[0].toid)
        ):
            close_battle(conquered=a.action == "occupy" and a.toid == battle[0].toid)

        match a.action:
            case "select" if a.tid:
                owner[a.tid] = "N" if a.seat in NEUTRAL else a.seat
            case "neutralise" if a.tid:
                owner[a.tid] = "N"
            case "place" | "factory_place" if a.tid:
                # Only fills gaps (scenario setups don't log a select). In team games you can
                # place on a teammate's territory, so this never overrides a known owner.
                owner.setdefault(a.tid, "N" if a.seat in NEUTRAL else a.seat)
                units[a.tid] += a.count
                grew(a.tid, a.timestamp)
            case "occupy":
                owner.setdefault(a.fromid, a.seat)
                owner[a.toid] = a.seat
                stats.player(a.seat).conquests += 1
                units[a.fromid] -= a.count
                units[a.toid] += a.count
                grew(a.toid, a.timestamp)
            case "fortify" | "transfer":
                owner.setdefault(a.fromid, a.seat)
                owner.setdefault(a.toid, a.seat)
                units[a.fromid] -= a.count
                units[a.toid] += a.count
                grew(a.toid, a.timestamp)
            case "setup_complete":
                snapshot(a.timestamp)
            case "start_turn":
                close_turn(a.timestamp)
                open_turn(a.seat, a.timestamp)
            case "skip_turn":
                turn_skipped = True
            case "end_turn":
                close_turn(a.timestamp)
                snapshot(a.timestamp)
            case "trade_cards":
                stats.player(a.seat).card_trades += 1
            case "eliminate_player":
                stats.eliminations.append(Elimination(a.seats, a.seat, "eliminated", a.timestamp))
            case "booted" | "surrender":
                victim = a.seats if a.seats not in NEUTRAL else a.seat
                how = "booted" if a.action == "booted" else "surrendered"
                stats.eliminations.append(Elimination(victim, None, how, a.timestamp))
            case "attack":
                owner.setdefault(a.fromid, a.seat)
                defender = owner.get(a.toid, "N")
                units[a.fromid] -= a.al
                units[a.toid] -= a.dl
                turn_targets.add(a.toid)
                turn_victims.add(defender)
                if not battle:
                    battle_defender = defender
                battle.append(a)
                att, dfn = stats.player(a.seat), stats.player(defender)
                att.rolls += 1
                att.units_lost += a.al
                att.units_killed += a.dl
                dfn.units_lost += a.dl
                dfn.units_killed += a.al
                turn_losses += a.al + a.dl
                if a.ad and a.dd:
                    scored += 1
                    if resolve(a.ad, a.dd) != (a.al, a.dl):
                        mismatches += 1
                        continue
                    # Faces can exceed the board's sides when a border modifier adds sides.
                    a_sides = max(attacker_sides + a.bmod[0], *a.ad)
                    d_sides = max(defender_sides + a.bmod[1], *a.dd)
                    expected = expected_losses(len(a.ad), len(a.dd), a_sides, d_sides)
                    if expected:
                        att.luck += a.dl - expected[1]
                        dfn.luck += a.al - expected[0]
    close_battle()
    close_turn(actions[-1].timestamp if actions else 0)
    snapshot(actions[-1].timestamp if actions else 0)
    for pair in list(truces):
        end_truce(pair)
    stats.pacts.sort(key=lambda p: (-p.rounds, p.start))

    stats.territories_total = territories_total or len(owner)
    stats.luck_counted = scored > 0 and mismatches / scored <= MAX_MISMATCH
    return stats
