"""Polls WarGear and keeps the store in sync.

Each cycle walks the registered API keys and skips any key whose known games
were already covered by an earlier key in the same cycle. So with N players who
all share the same games, a normal cycle costs one request, not N. Once an hour
every key is checked anyway, to spot new games.

A live game missing from every Live list it should be on has probably ended,
so the owning key's Finished list is fetched once to learn who won.
"""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable

from joshua.config import Settings
from joshua.crypto import KeyBox
from joshua.db import PlayerRow, Store, now_ts
from joshua.sync import Event, apply
from joshua.wargear.client import BadApiKey, RateLimited, WarGearClient, WarGearError

log = logging.getLogger(__name__)

# A live game nobody has seen for this long gets closed out.
GONE_AFTER = 2 * 86400
# The Finished list is unordered and may be paginated; don't walk it forever.
MAX_FINISHED_PAGES = 5
# Look up a missing game in Finished lists at most this often.
FINISHED_RECHECK = 3600

EventSink = Callable[[list[Event]], Awaitable[None]]


class Poller:
    def __init__(
        self,
        store: Store,
        client: WarGearClient,
        keybox: KeyBox,
        settings: Settings,
        on_events: EventSink | None = None,
    ):
        self.store = store
        self.client = client
        self.keybox = keybox
        self.settings = settings
        self.on_events = on_events
        self._last_full = 0
        self._finished_checked: dict[int, int] = {}
        self.wake = asyncio.Event()

    async def _keys(self) -> list[tuple[PlayerRow, str, set[int]]]:
        """Usable keys, plus the live games we last saw each key's owner in."""
        out = []
        for p in await self.store.players():
            if not p.enc_api_key or p.key_ok == 0:
                continue
            try:
                key = self.keybox.open(p.enc_api_key)
            except ValueError as e:
                log.error("can't decrypt key for %s: %s", p.slack_id, e)
                continue
            mine = await self.store.games_for_player(p.wargear_name) if p.wargear_name else set()
            out.append((p, key, mine))
        # Greedy set cover: keys that have worked and see the most games go first.
        return sorted(out, key=lambda k: (k[0].key_ok == 0, -len(k[2]), k[0].slack_id))

    async def poll_once(self) -> list[Event]:
        if (wait := self.client.gate.blocked_for()) > 0:
            log.info("skipping poll; WarGear gate closed for %.0fs", wait)
            return []

        now = now_ts()
        full = now - self._last_full >= self.settings.full_refresh_seconds
        requests_before = self.client.request_count
        covered: set[int] = set()
        queried: list[tuple[PlayerRow, str]] = []
        events: list[Event] = []

        for player, key, mine in await self._keys():
            if not full and mine and mine <= covered:
                continue
            try:
                games = await self.client.my_games(key, "Live")
            except RateLimited:
                log.warning("rate limited mid-cycle; finishing early")
                break
            except BadApiKey as e:
                log.warning("WarGear rejected the API key for %s: %s", player.slack_id, e)
                await self.store.set_key_ok(player.slack_id, False)
                continue
            except WarGearError as e:
                log.warning("poll via %s failed: %s", player.slack_id, e)
                continue
            if player.key_ok != 1:
                await self.store.set_key_ok(player.slack_id, True)
            queried.append((player, key))
            for g in games:
                if g.gameid in covered:
                    continue
                covered.add(g.gameid)
                events += await apply(self.store, g.normalize(), now, player.slack_id)

        if full and queried:
            self._last_full = now

        events += await self._close_out_missing(covered, queried, now)
        await self.store.kv_set("last_poll_ok", str(now))
        await self.store.kv_set("last_poll_requests", str(self.client.request_count))

        log.info(
            "poll done: %s, %d key(s) queried, %d live game(s) seen, %d event(s), %d request(s)",
            "full refresh" if full else "covered keys only",
            len(queried),
            len(covered),
            len(events),
            self.client.request_count - requests_before,
        )
        for e in events:
            log.info("event: %s", e)
        if events and self.on_events:
            await self.on_events(events)
        return events

    async def _close_out_missing(
        self, covered: set[int], queried: list[tuple[PlayerRow, str]], now: int
    ) -> list[Event]:
        missing = (await self.store.live_gameids()) - covered
        if not missing or not queried:
            return []
        events: list[Event] = []
        for player, key in queried:
            if not player.wargear_name:
                continue
            theirs = missing & await self.store.games_for_player(player.wargear_name)
            theirs = {g for g in theirs if now - self._finished_checked.get(g, 0) >= FINISHED_RECHECK}
            if not theirs:
                continue
            for gameid in theirs:
                self._finished_checked[gameid] = now
            try:
                async for g in self._finished_pages(key):
                    if g.gameid in theirs:
                        events += await apply(self.store, g.normalize(), now, player.slack_id)
                        missing.discard(g.gameid)
                        theirs.discard(g.gameid)
                        if not theirs:
                            break
            except WarGearError as e:
                log.warning("couldn't fetch finished games via %s: %s", player.slack_id, e)
                break
        for gameid in missing:
            state = await self.store.game_state(gameid)
            row = await self.store.db.execute_fetchall(
                "SELECT last_seen_at FROM games WHERE gameid = ?", (gameid,)
            )
            if state and row and now - row[0][0] > GONE_AFTER:
                log.info("game %s not seen for %ss; closing it out", gameid, now - row[0][0])
                await self.store.db.execute(
                    "UPDATE games SET finished = 1, status = COALESCE(status, 'Gone') WHERE gameid = ?",
                    (gameid,),
                )
                await self.store.db.execute(
                    "UPDATE turns SET ended_at = ? WHERE gameid = ? AND ended_at IS NULL", (now, gameid)
                )
                await self.store.db.commit()
        return events

    async def _finished_pages(self, key: str):
        """Yield finished games page by page. Stops at an empty page, or at a page that
        repeats the previous one (meaning WarGear ignores ``pagenumber``)."""
        previous: set[int] = set()
        for page in range(1, MAX_FINISHED_PAGES + 1):
            games = await self.client.my_games(key, "Finished", page=None if page == 1 else page)
            ids = {g.gameid for g in games}
            if not ids or ids == previous:
                return
            previous = ids
            for g in games:
                yield g

    async def run_forever(self) -> None:
        while True:
            try:
                await self.poll_once()
            except Exception:
                log.exception("poll cycle crashed")
            jitter = random.uniform(-self.settings.poll_jitter_seconds, self.settings.poll_jitter_seconds)
            delay = max(30.0, self.settings.poll_seconds + jitter, self.client.gate.blocked_for())
            self.wake.clear()
            try:
                await asyncio.wait_for(self.wake.wait(), timeout=delay)
            except TimeoutError:
                pass
