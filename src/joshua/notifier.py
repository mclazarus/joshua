"""Turns events and due reminders into Slack posts."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from joshua.config import Settings, parse_hours
from joshua.db import OpenTurn, Store, now_ts
from joshua.reminders import (
    Cadence,
    LoudHours,
    Reminder,
    due_now,
    is_loud,
    next_loud_start,
    plan_reminders,
)
from joshua.slack import messages as m
from joshua.sync import Event, GameFinished, PlayerEliminated, TurnStarted

log = logging.getLogger(__name__)

FIRST, FOLLOWUP = "first", "followup"
# Ask about at most this many unknown WarGear names per tick.
IDENTITY_PROMPTS_PER_TICK = 2


class Poster(Protocol):
    async def post(self, text: str, blocks: list[dict]) -> str | None: ...


class PrintPoster:
    """Dry-run poster: prints instead of posting."""

    async def post(self, text: str, blocks: list[dict]) -> str | None:
        print(f"[would post] {text}")
        return None


def _utc(ts: int) -> datetime:
    return datetime.fromtimestamp(ts, UTC)


class Notifier:
    def __init__(self, store: Store, poster: Poster, settings: Settings):
        self.store = store
        self.poster = poster
        self.settings = settings
        self.cadence = Cadence.fast() if settings.joshua_fast_reminders else Cadence()

    # --- preferences -----------------------------------------------------

    def tz_for(self, tz: str | None) -> ZoneInfo:
        for name in (tz, self.settings.default_tz):
            if name:
                try:
                    return ZoneInfo(name)
                except (ZoneInfoNotFoundError, ValueError):
                    log.warning("unknown timezone %r", name)
        return ZoneInfo("UTC")

    def loud_for(self, spec: str | None) -> LoudHours:
        try:
            return LoudHours(*parse_hours(spec)) if spec else LoudHours(*self.settings.loud_hours)
        except ValueError:
            return LoudHours(*self.settings.loud_hours)

    async def who(self, wargear_name: str) -> m.Who:
        p = await self.store.player_by_name(wargear_name)
        return m.Who(wargear_name, p.slack_id if p else None)

    async def _loud_time(self, wargear_name: str, at: int) -> int:
        """``at``, or the next loud-hours start for this player if ``at`` is in quiet hours."""
        p = await self.store.player_by_name(wargear_name)
        tz = self.tz_for(p.tz if p else None)
        loud = self.loud_for(p.loud_hours if p else None)
        when = _utc(at)
        if is_loud(when, tz, loud):
            return at
        return int(next_loud_start(when, tz, loud).timestamp())

    # --- events ----------------------------------------------------------

    async def handle_events(self, events: list[Event], now: int | None = None) -> None:
        now = now or now_ts()
        for e in events:
            if not await self.store.is_tracked(e.gameid):
                log.info("game %s isn't tracked (needs 2 registered players or /wargear track)", e.gameid)
                continue
            game = await self.store.game_state(e.gameid)
            if game is None:
                continue
            match e:
                case TurnStarted():
                    turn = next(
                        (
                            t
                            for t in await self.store.open_turns()
                            if (t.gameid, t.player) == (e.gameid, e.player)
                        ),
                        None,
                    )
                    if turn:
                        schedule = ", ".join(
                            f"{r.kind}@{r.when.astimezone(self.tz_for(turn.tz)):%a %H:%M}"
                            for r in self.plan_for(turn)
                        )
                        log.info("turn started: %s / %s; reminders: %s", game.name, e.player, schedule)
                case PlayerEliminated():
                    await self.poster.post(*m.eliminated(await self.who(e.player), game.name))
                case GameFinished(terminated=True):
                    await self.poster.post(*m.terminated(game.name))
                case GameFinished():
                    for winner in e.winners:
                        first = await self._loud_time(winner, now)
                        followup_at = now + self.settings.winner_followup_hours * 3600
                        await self.store.schedule_winner_nag(e.gameid, winner, FIRST, first)
                        await self.store.schedule_winner_nag(
                            e.gameid, winner, FOLLOWUP, await self._loud_time(winner, followup_at)
                        )

    # --- periodic --------------------------------------------------------

    async def tick(self, now: int | None = None) -> int:
        """Send whatever is due. Returns how many messages were posted."""
        now = now or now_ts()
        posted = await self.remind(now)
        posted += await self._winner_tick(now)
        posted += await self._signup_tick(now)
        posted += await self._identity_tick(now)
        return posted

    async def remind(self, now: int) -> int:
        """Turn reminders only."""
        posted = 0
        for turn in await self.store.open_turns():
            posted += await self._turn_tick(turn, now)
        return posted

    def plan_for(self, turn: OpenTurn) -> list[Reminder]:
        return plan_reminders(
            _utc(turn.started_at),
            _utc(turn.deadline) if turn.deadline else None,
            self.tz_for(turn.tz),
            self.loud_for(turn.loud_hours),
            self.cadence,
        )

    async def _turn_tick(self, turn: OpenTurn, now: int) -> int:
        plan = self.plan_for(turn)
        sent_keys = await self.store.reminders_sent(turn.gameid, turn.player, turn.turnstamp)
        already = {r for r in plan if (r.kind, int(r.when.timestamp())) in sent_keys}
        send, skip = due_now(plan, already, _utc(now), _utc(turn.deadline) if turn.deadline else None)
        for r in skip:
            log.info("skipping stale reminder %s for %s / %s (catch-up)", r.kind, turn.game_name, turn.player)
            await self.store.record_reminder(
                turn.gameid, turn.player, turn.turnstamp, r.kind, int(r.when.timestamp()), posted=False
            )
        if send is None:
            return 0
        who = m.Who(turn.player, turn.slack_id)
        text, blocks = m.turn_reminder(
            send.kind,
            who,
            turn.game_name,
            self.settings.game_url(turn.gameid),
            turn.started_at,
            turn.deadline,
            now,
            self.tz_for(turn.tz),
        )
        log.info("reminder %s: %s / %s", send.kind, turn.game_name, turn.player)
        await self.poster.post(text, blocks)
        await self.store.record_reminder(
            turn.gameid, turn.player, turn.turnstamp, send.kind, int(send.when.timestamp()), posted=True
        )
        return 1

    async def _winner_tick(self, now: int) -> int:
        """Post due winner nags, one message per game even when a team won."""
        groups: dict[tuple[int, str], list[str]] = {}
        for nag in await self.store.due_winner_nags(now):
            groups.setdefault((nag["gameid"], nag["kind"]), []).append(nag["winner"])

        posted = 0
        for (gameid, kind), winners in groups.items():
            game = await self.store.game_state(gameid)

            async def done(outcome: str, gameid=gameid, kind=kind, winners=winners) -> None:
                for w in winners:
                    await self.store.finish_winner_nag(gameid, w, kind, outcome)

            if game is None:
                await done("game missing")
                continue
            if not await self.store.is_tracked(gameid):
                await done("game unwatched")
                continue
            who = [await self.who(w) for w in winners]
            if kind == FOLLOWUP:
                ended = game.endstamp or now - self.settings.winner_followup_hours * 3600
                if any([await self.store.games_hosted_since(w, ended) for w in winners]):
                    await done("new game already hosted")
                    continue
                msg = m.winner_followup(who, game.name, self.settings.wargear_new_game_url)
            else:
                duration = (game.endstamp - game.startstamp) if game.endstamp and game.startstamp else None
                msg = m.winner_first(
                    who,
                    game.name,
                    self.settings.game_url(gameid),
                    duration,
                    self.settings.wargear_new_game_url,
                )
            await self.poster.post(*msg)
            await done("posted")
            posted += 1
        return posted

    async def _signup_tick(self, now: int) -> int:
        """Nag invitees of recent open games that still have empty seats."""
        if not is_loud(_utc(now), self.tz_for(None), LoudHours(*self.settings.loud_hours)):
            return 0
        posted = 0
        created_after = now - self.settings.signup_max_age_days * 86400
        for game, nagged_at in await self.store.signup_games(created_after):
            if nagged_at and now - nagged_at < self.settings.signup_nag_hours * 3600:
                continue
            if not game.spots_left or not game.invited:
                continue
            invited = [await self.who(n) for n in game.invited]
            joined = [await self.who(n) for n in game.joined]
            await self.poster.post(
                *m.signup_nag(
                    game.name,
                    self.settings.game_url(game.gameid),
                    invited,
                    joined,
                    game.seats,
                    game.spots_left,
                )
            )
            await self.store.set_signup_nagged(game.gameid, now)
            posted += 1
        return posted

    async def _identity_tick(self, now: int) -> int:
        tz = self.tz_for(None)
        if not is_loud(_utc(now), tz, LoudHours(*self.settings.loud_hours)):
            return 0
        posted = 0
        for name, gameid in (await self.store.unmapped_names())[:IDENTITY_PROMPTS_PER_TICK]:
            game = await self.store.game_state(gameid)
            ts = await self.poster.post(
                *m.identity_prompt(name, game.name if game else f"#{gameid}", self.settings.game_url(gameid))
            )
            await self.store.record_identity_prompt(name, self.settings.slack_channel_id, ts)
            posted += 1
        return posted
