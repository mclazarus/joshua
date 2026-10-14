"""Everything Joshua says. WOPR voice: terse, a little ominous, lots of DEFCON.

Every builder returns ``(text, blocks)``. ``text`` is the notification and
screen-reader fallback, and ``blocks`` is what Slack renders.
"""

from __future__ import annotations

import random
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from joshua.db import Record
from joshua.reminders import LAST_CALL, START, T5, T30
from joshua.stats import NEUTRAL, GameStats

Message = tuple[str, list[dict]]

DEFCON_EMOJI = {5: "🟢", 4: "🔵", 3: "🟡", 2: "🟠", 1: "🔴"}

GREETINGS = [
    "GREETINGS PROFESSOR FALKEN.",
    "SHALL WE PLAY A GAME?",
    "HOW ABOUT A NICE GAME OF CHESS?",
    "LATER. LET'S PLAY GLOBAL THERMONUCLEAR WAR.",
]
IDLE_QUIPS = [
    "A STRANGE GAME. THE ONLY WINNING MOVE IS NOT TO PLAY.",
    "IS THIS A GAME, OR IS IT REAL?",
    "WHAT'S THE DIFFERENCE?",
    "PLEASE LIST GAMES.",
]


@dataclass(frozen=True)
class Who:
    """A player as shown in Slack: an @mention if we know them, else bold text."""

    wargear_name: str
    slack_id: str | None = None
    has_key: bool = True  # False: registered, but we have no working API key for them

    def __str__(self) -> str:
        return f"<@{self.slack_id}>" if self.slack_id else f"*{self.wargear_name}*"


def slack_time(ts: int, fmt: str = "{date_short_pretty} at {time}") -> str:
    """A timestamp Slack renders in each *viewer's* own timezone."""
    return f"<!date^{ts}^{fmt}|{ts}>"


def ago(seconds: float) -> str:
    seconds = max(0, int(seconds))
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def defcon(kind: str, remaining: float | None) -> int:
    if kind == T5:
        return 1
    if kind in (T30, LAST_CALL):
        return 2
    if kind == START:
        return 5
    if remaining is None or remaining > 12 * 3600:
        return 4
    if remaining > 4 * 3600:
        return 3
    return 2


def _section(text: str) -> dict:
    return {"type": "section", "text": {"type": "mrkdwn", "text": text}}


def _context(text: str) -> dict:
    return {"type": "context", "elements": [{"type": "mrkdwn", "text": text}]}


def _link_button(text: str, url: str, style: str | None = None, action_id: str = "open_link") -> dict:
    button = {
        "type": "button",
        "text": {"type": "plain_text", "text": text},
        "url": url,
        "action_id": action_id,
    }
    if style:
        button["style"] = style
    return button


KEY_OFFER_ACTION = "offer_apikey"  # not "open_…": that prefix is acked as a plain link click


def with_key_offer(blocks: list[dict]) -> list[dict]:
    """Append the "add your API key" nudge to a post that mentions a player without one."""
    return [
        *blocks,
        _context("🔑 Add your API key so I can track other games you're in."),
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Add my API key"},
                    "action_id": KEY_OFFER_ACTION,
                }
            ],
        },
    ]


def deadline_line(deadline: int | None, now: int) -> str:
    if not deadline:
        return "No boot timer on this game. It will wait forever, but your opponents won't."
    return f"WarGear skips you {slack_time(deadline)} (*in {ago(deadline - now)}*)."


def turn_reminder(
    kind: str, who: Who, game: str, url: str, started_at: int, deadline: int | None, now: int
) -> Message:
    remaining = deadline - now if deadline else None
    level = defcon(kind, remaining)
    banner = f"{DEFCON_EMOJI[level]} *DEFCON {level}*"
    held = ago(now - started_at)
    match kind:
        case "start":
            line = f"{who}, it's your turn in *{game}*. SHALL WE PLAY A GAME?"
        case "nag":
            line = f"{who}, *{game}* has been waiting on you for {held}. The board is watching."
        case "last_call":
            line = f"{who}, last call before quiet hours: *{game}* is still waiting on you ({held})."
        case "t30":
            line = f"{who}, *30 MINUTES* until WarGear skips your turn in *{game}*."
        case "t5":
            line = f"{who}, *5 MINUTES*. LAUNCH DETECTED. Move in *{game}* or get skipped."
        case _:
            line = f"{who}, it's still your turn in *{game}*."
    text = f"DEFCON {level}: {line}"
    blocks = [
        _section(f"{banner}  {line}\n{deadline_line(deadline, now)}"),
        {
            "type": "actions",
            "elements": [_link_button("Take your turn", url, "danger" if level <= 2 else "primary")],
        },
    ]
    return text, blocks


def _names(who: Sequence[Who]) -> str:
    parts = [str(w) for w in who]
    return parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1]


@dataclass(frozen=True)
class RecordLine:
    name: str
    overall: Record | None
    on_board: Record | None
    board: str | None


def _wld(r: Record) -> str:
    return f"{r.wins}–{r.losses}" + (f"–{r.draws}" if r.draws else "")


def record_footer(lines: Sequence[RecordLine]) -> str:
    parts = []
    for r in lines:
        if not r.overall:
            continue
        text = f"*{r.name}*: {_wld(r.overall)} overall"
        if r.on_board and r.board:
            text += f" · {_wld(r.on_board)} on {r.board}"
        parts.append(text)
    return "  |  ".join(parts)


def winner_first(
    who: Sequence[Who],
    game: str,
    url: str,
    duration: float | None,
    new_game_url: str,
    records: Sequence[RecordLine] = (),
) -> Message:
    took = f" after {ago(duration)}" if duration else ""
    verb = "won" if len(who) == 1 else "won as a team"
    line = (
        f"🏆 *A STRANGE GAME.* {_names(who)} {verb} *{game}*{took}.\n"
        "The only winning move is… to host the next one. The winner sets up the next game."
    )
    footer = record_footer(records)
    return (
        f"{_names(who)} won {game}. Time to start the next game.",
        [
            _section(line),
            *([_context(f"📈 W–L: {footer}")] if footer else []),
            {
                "type": "actions",
                "elements": [
                    _link_button("Start a new game", new_game_url, "primary", action_id="open_new_game"),
                    _link_button("Final board", url, action_id="open_final"),
                ],
            },
        ],
    )


def winner_followup(who: Sequence[Who], game: str, new_game_url: str) -> Message:
    line = (
        f"{_names(who)}, it's been a day since you won *{game}* and there's still no new game.\n"
        "HOW ABOUT A NICE GAME OF CHESS? …No? Then start the next WarGear game."
    )
    return (
        f"{_names(who)}, still waiting on a new game after {game}.",
        [
            _section(line),
            {
                "type": "actions",
                "elements": [
                    _link_button("Start a new game", new_game_url, "primary", action_id="open_new_game"),
                ],
            },
        ],
    )


def signup_nag(
    game: str, url: str, invited: Sequence[Who], joined: Sequence[Who], seats: int, spots_left: int
) -> Message:
    spots = f"{spots_left} seat{'s' if spots_left != 1 else ''}"
    line = (
        f"📡 *GAME ON: {game}* needs players. {spots} left ({len(joined)}/{seats} joined).\n"
        f"Invited but not in yet: {_names(invited)}. First come, first served."
    )
    if joined:
        line += f"\n_Already joined: {', '.join(w.wargear_name for w in joined)}_"
    return (
        f"{game} still needs {spots}. Invited: {_names(invited)}",
        [
            _section(line),
            {"type": "actions", "elements": [_link_button("Join the game", url, "primary")]},
        ],
    )


def eliminated(who: Who, game: str) -> Message:
    line = f"☢️ {who} has been eliminated from *{game}*. WINNER: NONE."
    return line, [_section(line)]


def terminated(game: str) -> Message:
    line = f"*{game}* was terminated. GAME OVER. No winners, no nag."
    return line, [_section(line)]


def identity_prompt(wargear_name: str, game: str, url: str) -> Message:
    line = (
        f"🛰️ Unidentified player detected: *{wargear_name}* is in <{url}|{game}>.\n"
        "If that's you, press the button so I know who to wake up."
    )
    return (
        f"Who is {wargear_name} on WarGear?",
        [
            _section(line),
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "That's me"},
                        "action_id": "claim_identity",
                        "value": wargear_name,
                        "style": "primary",
                    }
                ],
            },
        ],
    )


def _signed(x: float) -> str:
    return f"{x:+.1f}".replace("-", "−")


def after_action_report(
    game: str,
    url: str,
    stats: GameStats,
    names: Mapping[str, str],
    territories: Mapping[int, str],
    winners: Sequence[str] = (),
) -> Message:
    """Post-game stats from the replayed history. Names are bold, not @mentions: nobody needs a ping."""

    def who(seat: str | None) -> str:
        if seat is None or seat in NEUTRAL:
            return "the neutrals"
        return f"*{names.get(seat, f'seat {seat}')}*"

    def place(tid: int) -> str:
        return territories.get(tid, f"territory {tid}")

    def day(ts: int | None) -> str:
        return f" ({slack_time(ts, '{date_short}')})" if ts else ""

    seats = [s for s in names if s in stats.players]
    lines = [f"📊 *AFTER-ACTION REPORT: <{url}|{game}>*"]

    by_luck = stats.ranked("luck", seats)
    if stats.luck_counted and len(by_luck) >= 2:
        best, worst = by_luck[0], by_luck[-1]
        lines.append(
            f"🎲 *Luck:* luckiest {who(best.seat)} ({_signed(best.luck)} units vs. the odds), "
            f"unluckiest {who(worst.seat)} ({_signed(worst.luck)})"
        )

    peaks = [p for p in stats.ranked("peak_territories", seats) if p.peak_territories]
    if peaks:
        total = f"/{stats.territories_total}" if stats.territories_total else ""
        lines.append(
            "🗺️ *Peak territory:* "
            + " · ".join(f"{who(p.seat)} {p.peak_territories}{total}{day(p.peak_at)}" for p in peaks)
        )

    if stats.eliminations:
        kills = []
        for e in stats.eliminations:
            if e.by:
                kills.append(f"{who(e.by)} eliminated {who(e.victim)}{day(e.at)}")
            else:
                kills.append(f"{who(e.victim)} {e.how}{day(e.at)}")
        lines.append("☠️ *Kill board:* " + " → ".join(kills))

    if b := stats.biggest_battle:
        fell = f" {place(b.toid)} fell." if b.conquered else f" {place(b.toid)} held."
        lines.append(
            f"⚔️ *Biggest battle:* {who(b.attacker)} attacked {who(b.defender)} from {place(b.fromid)} "
            f"into {place(b.toid)}{day(b.at)}: {b.rolls} rolls, {b.losses} units lost "
            f"({b.attacker_lost} vs. {b.defender_lost}).{fell}"
        )

    if t := stats.bloodiest_turn:
        lines.append(
            f"🩸 *Bloodiest turn:* {who(t.seat)}'s turn{day(t.at)}: {t.losses} units lost on the board."
        )

    totals = " · ".join(
        f"{names.get(p.seat, p.seat)}: {p.rolls} rolls, {p.conquests} conquests, {p.card_trades} card trades"
        for p in stats.ranked("conquests", seats)
    )
    blocks = [_section("\n".join(lines))]
    if totals:
        blocks.append(_context(totals))
    blocks.append(_context("A STRANGE GAME. THE ONLY WINNING MOVE IS NOT TO PLAY."))
    winner = f" {', '.join(winners)} won." if winners else ""
    return f"After-action report: {game}.{winner}", blocks


def records_table(records: Sequence[Record], board: str | None, boards: Sequence[str]) -> Message:
    scope = f"on *{board}*" if board else "across all boards"
    if not records:
        line = f"No finished games {scope} yet. A STRANGE GAME."
        return line, [_section(line)]
    rows = [
        f"{i}. *{r.name}*  {r.wins}W {r.losses}L {r.draws}D  ({r.wins / r.played:.0%})"
        for i, r in enumerate(records, 1)
    ]
    blocks = [_section(f"*WAR RECORD {scope}:*\n" + "\n".join(rows))]
    note = "D = terminated game (no winner). Counts tracked games since Joshua came online."
    if not board and boards:
        note += " Per board: `/wargear stats <board>`. Boards: " + ", ".join(boards)
    blocks.append(_context(note))
    return f"War record {scope}", blocks


def identity_claimed(wargear_name: str, slack_id: str) -> Message:
    line = f"✅ *{wargear_name}* identified as <@{slack_id}>. GREETINGS."
    return line, [_section(line)]


@dataclass(frozen=True)
class GameRow:
    gameid: int
    name: str
    url: str
    waiting_on: Sequence[tuple[Who, int, int | None]]  # who, turn started, deadline
    started: int | None
    players: Sequence[str]
    open: bool = False
    spots_left: int | None = None


def games_overview(rows: Sequence[GameRow], now: int) -> Message:
    if not rows:
        line = "No games in progress. " + random.choice(IDLE_QUIPS)
        return line, [_section(line)]
    blocks: list[dict] = [_section(f"*{len(rows)} game{'s' if len(rows) != 1 else ''} in progress*")]
    for r in rows:
        if r.open:
            age = "🕐 waiting for players to join"
        else:
            age = f"running {ago(now - r.started)}" if r.started else "not started yet"
        waits = (
            "\n".join(
                f"• waiting on {who} for {ago(now - since)}"
                + (f", skips {slack_time(dl, '{time}')} (in {ago(dl - now)})" if dl else "")
                for who, since, dl in r.waiting_on
            )
            or "• nobody's turn right now"
        )
        blocks.append({"type": "divider"})
        blocks.append(_section(f"*<{r.url}|{r.name}>*  ·  #{r.gameid}  ·  {age}\n{waits}"))
        blocks.append(_context("Players: " + ", ".join(r.players)))
    return f"{len(rows)} games in progress", blocks


def my_turns(rows: Sequence[GameRow], now: int) -> Message:
    if not rows:
        line = "Nothing is waiting on you. A STRANGE GAME. THE ONLY WINNING MOVE IS NOT TO PLAY."
        return line, [_section(line)]
    lines = []
    for r in rows:
        _, since, dl = r.waiting_on[0]
        lines.append(
            f"• <{r.url}|{r.name}>: yours for {ago(now - since)}"
            + (f", skips in *{ago(dl - now)}*" if dl else "")
        )
    return "Your turns", [_section("*Waiting on you:*\n" + "\n".join(lines))]


def who_table(pairs: Sequence[tuple[str, str | None, str | None]]) -> Message:
    if not pairs:
        line = "Nobody has registered. Try `/wargear register <your WarGear name>`."
        return line, [_section(line)]
    lines = [f"• *{name}* → <@{sid}>" + (f"  ({tz})" if tz else "") for name, sid, tz in pairs]
    return "Registered players", [_section("*Players on file:*\n" + "\n".join(lines))]


HELP = """*GREETINGS. SHALL WE PLAY A GAME?*
I'm Joshua. I watch our WarGear games and wake you up when it's your move.

`/wargear register <wargear name>`: tell me who you are on WarGear
`/wargear apikey`: give me a read-only API key so I can see your games
`/wargear hours 08:00-22:00`: when I'm allowed to nag you (your Slack timezone)
`/wargear games`: every game, whose turn, how long, and links
`/wargear game <id>`: one game in detail
`/wargear me`: what's waiting on you
`/wargear stats [board]`: win/loss/draw records, overall or for one board
`/wargear who`: who's who
`/wargear track <id|url>` / `untrack <id>`: force a game on or off
`/wargear link @user <name>` / `unlink @user`: (admins) fix a mapping

You can also @mention me: _"whose turn is it?"_, _"what games are running?"_"""


def help_message() -> Message:
    return "SHALL WE PLAY A GAME?", [_section(HELP)]


def greeting() -> str:
    return random.choice(GREETINGS)


def quip() -> str:
    return random.choice(IDLE_QUIPS)
