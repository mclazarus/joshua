"""Everything Joshua says. WOPR voice: terse, a little ominous, lots of DEFCON.

Every builder returns ``(text, blocks)``. ``text`` is the notification and
screen-reader fallback, and ``blocks`` is what Slack renders.
"""

from __future__ import annotations

import random
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from joshua.db import Record
from joshua.release import Release
from joshua.reminders import LAST_CALL, START, T5, T30
from joshua.stats import DEATHBALL_MIN, NEUTRAL, GameStats

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


EASTERN = ZoneInfo("America/New_York")


def when_token(ts: int, tz: ZoneInfo, now: int) -> str:
    """A deadline Slack renders live in each viewer's timezone: "Tomorrow at 9:38 AM (In 24 hours)".

    Slack re-evaluates both halves whenever the message is *viewed*, so a reminder read the
    next morning still reads correctly. (Verified with /wargear timetest on 2026-10-02.)
    Server-written text like "tomorrow" or "in 23h 55m" goes stale. The fallback, for
    clients that can't render tokens, is the server-side text in the player's timezone.
    """
    return f"<!date^{ts}^{{date_short_pretty}} at {{time}} ({{ago}})|{local_when(ts, tz, now)}>"


def local_when(ts: int, tz: ZoneInfo, now: int) -> str:
    """'today at 9:14 PM EDT', 'tomorrow at …', 'Sunday at …', or 'Oct 12 at …', in ``tz``.

    Only correct as of ``now``. Used as the ``when_token`` fallback and by /wargear timetest.
    """
    when = datetime.fromtimestamp(ts, tz)
    days = (when.date() - datetime.fromtimestamp(now, tz).date()).days
    if days == 0:
        day = "today"
    elif days == 1:
        day = "tomorrow"
    elif days == -1:
        day = "yesterday"
    elif 1 < days < 7:
        day = when.strftime("%A")
    else:
        day = f"{when:%b} {when.day}"
    hour = when.hour % 12 or 12
    return f"{day} at {hour}:{when:%M} {when:%p} {when.tzname()}"


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


# Every Slack <!date> format token, for /wargear timetest.
SLACK_DATE_FORMATS = [
    "{date_short_pretty} at {time}",
    "{date_pretty} at {time}",
    "{date_long_pretty} at {time}",
    "{day_divider_pretty}",
    "{date_short} at {time}",
    "{date} at {time}",
    "{date_long} at {time}",
    "{date_num} {time_secs}",
    "{ago}",
    "{date_short_pretty} at {time} ({ago})",
]


def time_diagnostic(now: int, tz: ZoneInfo, offsets: Sequence[tuple[str, int]]) -> Message:
    """One deadline per section, rendered every way we could render it."""
    utc = ZoneInfo("UTC")
    blocks: list[dict] = [
        _section(
            "*🧪 TIME TEST.* Each block shows one deadline rendered every way we could.\n"
            f"Server now: `{datetime.fromtimestamp(now, utc):%Y-%m-%d %H:%M:%S} UTC` · "
            f"your Slack tz as I know it: `{tz.key}`\n"
            "Raw tokens render in *your* Slack timezone. Server lines are computed by the bot."
        )
    ]
    for label, offset in offsets:
        ts = now + offset
        utc_text = f"{datetime.fromtimestamp(ts, utc):%a %Y-%m-%d %H:%M} UTC"
        lines = [f"*{label}*: in {ago(offset)} · epoch `{ts}` · `{utc_text}`"]
        lines += [f"`{fmt}` → <!date^{ts}^{fmt}|fallback {ts}>" for fmt in SLACK_DATE_FORMATS]
        lines.append(f"`server, your tz` → {local_when(ts, tz, now)}")
        lines.append(f"`server, Eastern` → {local_when(ts, EASTERN, now)}")
        blocks.append({"type": "divider"})
        blocks.append(_section("\n".join(lines)))
    return "Time rendering test", blocks


def deadline_line(deadline: int | None, now: int, tz: ZoneInfo = EASTERN) -> str:
    if not deadline:
        return "No boot timer on this game. It will wait forever, but your opponents won't."
    return f"WarGear skips you *{when_token(deadline, tz, now)}*."


def turn_reminder(
    kind: str,
    who: Who,
    game: str,
    url: str,
    started_at: int,
    deadline: int | None,
    now: int,
    tz: ZoneInfo = EASTERN,
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
        _section(f"{banner}  {line}\n{deadline_line(deadline, now, tz)}"),
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
        fallback = f"{datetime.fromtimestamp(ts, EASTERN):%b %-d}" if ts else ""
        return f" (<!date^{ts}^{{date_short}}|{fallback}>)" if ts else ""

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

    if d := stats.biggest_deathball:
        balls = " · ".join(
            f"{who(p.seat)} {p.deathball_turns}"
            for p in stats.ranked("deathball_turns", seats)
            if p.deathball_turns
        )
        lines.append(
            f"💣 *Biggest deathball:* {who(d.seat)} stacked {d.units} troops on {place(d.tid)}{day(d.at)}."
            f" Turns with a deathball (over {DEATHBALL_MIN}): {balls}"
        )

    turtles = sorted(
        (p for p in stats.ranked("turtle_turns", seats) if p.turtle_turns),
        key=lambda p: (p.turtle_leading, p.turtle_turns),
        reverse=True,
    )
    if turtles:
        lines.append(
            "🐢 *Turtling:* "
            + " · ".join(
                f"{who(p.seat)} {p.turtle_turns} turn{'s' if p.turtle_turns != 1 else ''}"
                + (f" ({p.turtle_leading} while leading)" if p.turtle_leading else "")
                for p in turtles
            )
        )

    for pact in stats.pacts[:2]:
        a, b = pact.seats
        enemies = _names([who(e) for e in pact.enemies])
        lines.append(
            f"🤝 *Possible alliance:* {who(a)} and {who(b)} shared a border for {pact.rounds} turns each"
            f" without a shot fired, while both hit {enemies}{day(pact.start)}."
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
    return line, [
        _section(line),
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Oops, not me"},
                    "action_id": "undo_claim",
                    "value": wargear_name,
                }
            ],
        },
    ]


@dataclass(frozen=True)
class GameRow:
    gameid: int
    name: str
    url: str
    waiting_on: Sequence[tuple[Who, int, int | None, ZoneInfo]]  # who, turn started, deadline, their tz
    started: int | None
    players: Sequence[str]
    open: bool = False
    spots_left: int | None = None


def _idle_line(r: GameRow) -> str:
    if r.open and r.spots_left:
        return f"• open for sign-ups: {r.spots_left} seat{'s' if r.spots_left != 1 else ''} left"
    if r.open:
        return "• open for sign-ups"
    return "• nobody's turn right now"


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
        waits = "\n".join(
            f"• waiting on {who} for {ago(now - since)}"
            + (f", skips {when_token(dl, tz, now)}" if dl else "")
            for who, since, dl, tz in r.waiting_on
        ) or _idle_line(r)
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
        _, since, dl, tz = r.waiting_on[0]
        lines.append(
            f"• <{r.url}|{r.name}>: yours for {ago(now - since)}"
            + (f", skips *{when_token(dl, tz, now)}*" if dl else "")
        )
    return "Your turns", [_section("*Waiting on you:*\n" + "\n".join(lines))]


def who_table(pairs: Sequence[tuple[str, str | None, str | None]]) -> Message:
    if not pairs:
        line = "Nobody has registered. Try `/wargear register <your WarGear name>`."
        return line, [_section(line)]
    lines = [f"• *{name}* → <@{sid}>" + (f"  ({tz})" if tz else "") for name, sid, tz in pairs]
    return "Registered players", [_section("*Players on file:*\n" + "\n".join(lines))]


REPORT_USAGE = "Usage: `/wargear report <game id or link>`. Any finished game of yours, shown only to you."
REPORT_NEEDS_KEY = "I need your API key to read your game history. `/wargear apikey`"
REPORT_WORKING = "RETRIEVING BATTLE RECORDS. STAND BY."
REPORT_BUSY = "Still working on your last report. One at a time."


def no_report(why: str, game: str) -> str:
    return {
        "live": f"*{game}* is still being fought. Reports are for finished games only.",
        "not_found": f"Game {game} isn't in your finished games (I checked the first few pages). "
        "Is it yours, and is it over?",
        "empty": f"WarGear has no history for *{game}*.",
        "error": f"WarGear wouldn't hand over the records for {game}. Try again later.",
    }[why]


HELP = """*GREETINGS. SHALL WE PLAY A GAME?*
I'm Joshua. I watch our WarGear games and wake you up when it's your move.

`/wargear register <wargear name>`: tell me who you are on WarGear
`/wargear apikey`: give me a read-only API key so I can see your games
`/wargear hours 08:00-22:00`: when I'm allowed to nag you (your Slack timezone)
`/wargear games`: every game, whose turn, how long, and links
`/wargear game <id>`: one game in detail
`/wargear me`: what's waiting on you
`/wargear stats [board]`: win/loss/draw records, overall or for one board
`/wargear report <id|url>`: the after-action report for any finished game of yours, just for you
`/wargear who`: who's who
`/wargear unwatch <id|url>`: stop all reminders for a game  ·  `watch <id>` to resume
`/wargear notme`: "that's not me": drops your WarGear name, keeps your key
`/wargear unlink`: forget me entirely (name, key, settings)
`/wargear link @user <name>` / `unlink @user|<name>`: (admins) fix someone's mapping
`/wargear admins`: who can do that
`/wargear version`: which build I'm running
`/wargear changelog [build]`: what changed in this build, or an earlier one

You can also @mention me: _"whose turn is it?"_, _"what games are running?"_"""


def _build_line(r: Release, commit: str | None) -> str:
    return f"*Build {r.build}* · {r.date}" + (f" · `{commit}`" if commit else "")


def version_message(r: Release, commit: str | None) -> Message:
    line = f"JOSHUA {_build_line(r, commit or 'dev')}. `/wargear changelog` for what's new."
    return line, [_section(line)]


def changelog_message(r: Release, commit: str | None, builds: Sequence[int]) -> Message:
    # Unwrap the file's hard-wrapped bullets: Slack would keep every line break.
    lines: list[str] = []
    for n in r.notes.splitlines():
        if n.startswith("- "):
            lines.append("• " + n[2:])
        elif n.strip() and lines and n.startswith(" "):
            lines[-1] += " " + n.strip()
        else:
            lines.append(n)
    notes = "\n".join(lines)
    blocks = [_section(f"{_build_line(r, commit)}\n{notes}")]
    others = [str(b) for b in builds if b != r.build]
    if others:
        blocks.append(_context("Earlier builds: `/wargear changelog <build>` · " + ", ".join(others)))
    return f"Changelog for build {r.build}", blocks


def unknown_build(build: str, builds: Sequence[int]) -> str:
    return f"NO RECORD OF BUILD {build}. Builds on file: {', '.join(map(str, builds))}."


def help_message() -> Message:
    return "SHALL WE PLAY A GAME?", [_section(HELP)]


def greeting() -> str:
    return random.choice(GREETINGS)


def quip() -> str:
    return random.choice(IDLE_QUIPS)
