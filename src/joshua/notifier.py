"""Turns events and due reminders into Slack posts."""

from __future__ import annotations

import json
import logging
import zlib
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from joshua import reports
from joshua.config import Settings, parse_hours
from joshua.db import OpenTurn, Store, now_ts
from joshua.reminders import (
    T5,
    T30,
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
KEY_OFFER_KV = "apikey_offer:"


def key_offer_due(
    who: Sequence[m.Who], last_offered: dict[str, int | None], now: int, every: int
) -> list[str]:
    """Slack ids of mentioned players with no working key who haven't been offered the button lately."""
    due = []
    for w in who:
        if not w.slack_id or w.has_key or w.slack_id in due:
            continue
        last = last_offered.get(w.slack_id)
        if last is None or now - last >= every:
            due.append(w.slack_id)
    return due


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
        return m.Who(wargear_name, p.slack_id if p else None, p.has_key if p else True)

    async def post(
        self, msg: m.Message, mentioned: Sequence[m.Who] = (), now: int | None = None
    ) -> str | None:
        """Post to the channel, adding the "add your API key" button if a keyless player is due one."""
        now = now or now_ts()
        text, blocks = msg
        last = {}
        for w in mentioned:
            if w.slack_id and not w.has_key:
                raw = await self.store.kv_get(KEY_OFFER_KV + w.slack_id)
                last[w.slack_id] = int(raw) if raw else None
        due = key_offer_due(mentioned, last, now, self.settings.apikey_offer_days * 86400)
        if due:
            blocks = m.with_key_offer(blocks)
        ts = await self.poster.post(text, blocks)
        for slack_id in due:
            await self.store.kv_set(KEY_OFFER_KV + slack_id, str(now))
        return ts

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
                    if game.finished:
                        log.info("not announcing %s's elimination: %s is over", e.player, game.name)
                    elif await self.store.fogged(e.gameid):
                        log.info("not announcing %s's elimination: %s is fogged", e.player, game.name)
                    else:
                        who = await self.who(e.player)
                        await self.post(m.eliminated(who, game.name), [who], now)
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
        posted += await self._report_tick(now)
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
        key = (turn.gameid, turn.player, turn.turnstamp)
        sent_keys = await self.store.reminders_sent(*key)
        already = {r for r in plan if (r.kind, int(r.when.timestamp())) in sent_keys}
        send, skip = due_now(plan, already, _utc(now), _utc(turn.deadline) if turn.deadline else None)
        for r in skip:
            log.info("skipping stale reminder %s for %s / %s (catch-up)", r.kind, turn.game_name, turn.player)
            await self.store.record_reminder(*key, r.kind, int(r.when.timestamp()), posted=False, at=now)
        if send is None:
            return 0
        if send.kind not in (T30, T5) and (last := await self.store.last_posted(*key)):
            # The plan is recomputed every tick, so a change of hours or timezone mid-turn can
            # surface an unsent slot that's "due". Don't ping again if it's older than the last
            # ping or would come right on top of it. The final warnings always go out.
            if send.when.timestamp() <= last or now - last < self.cadence.min_gap.total_seconds():
                log.info(
                    "holding %s for %s / %s: pinged %ss ago",
                    send.kind,
                    turn.game_name,
                    turn.player,
                    now - last,
                )
                await self.store.record_reminder(
                    *key, send.kind, int(send.when.timestamp()), posted=False, at=now
                )
                return 0
        who = m.Who(turn.player, turn.slack_id, turn.has_key)
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
        await self.post((text, blocks), [who], now)
        await self.store.record_reminder(*key, send.kind, int(send.when.timestamp()), posted=True, at=now)
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
                    records=await self._records_for(winners, game.board),
                )
            await self.post(msg, who, now)
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
            await self.post(
                m.signup_nag(
                    game.name,
                    self.settings.join_url(game.gameid),
                    invited,
                    joined,
                    game.seats,
                    game.spots_left,
                ),
                invited,
                now,
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

    async def _records_for(self, winners: Sequence[str], board: str | None) -> list[m.RecordLine]:
        overall = {r.name.lower(): r for r in await self.store.records()}
        on_board = {r.name.lower(): r for r in await self.store.records(board)} if board else {}
        return [
            m.RecordLine(w, overall.get(w.lower()), on_board.get(w.lower()), board)
            for w in winners
            if w.lower() in overall
        ]

    async def _report_tick(self, now: int) -> int:
        """After-action reports for finished games whose history has been fetched."""
        if not is_loud(_utc(now), self.tz_for(None), LoudHours(*self.settings.loud_hours)):
            return 0
        posted = 0
        for pending in await self.store.reports_due():
            game = await self.store.game_state(pending.gameid)
            if game is None or not await self.store.is_tracked(pending.gameid):
                await self.store.finish_report(pending.gameid, "not tracked")
                continue
            board = await self.store.board(game.boardid)
            try:
                history = json.loads(zlib.decompress(pending.body))
                report = reports.render(game, board, history, self.settings.game_url(game.gameid))
            except Exception:  # noqa: BLE001 - a bad log mustn't block the tick forever
                log.exception("couldn't replay history for game %s", pending.gameid)
                await self.store.finish_report(pending.gameid, "replay failed")
                continue
            if report is None:
                await self.store.finish_report(pending.gameid, "empty history")
                continue
            await self.poster.post(*report)
            await self.store.finish_report(pending.gameid, "posted")
            posted += 1
        return posted
