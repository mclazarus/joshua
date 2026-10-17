"""Find one game in a key's Finished list without walking the whole thing.

How WarGear pages ``GetGameList/my?viewselector=Finished`` (measured 2026-10-16, 264 games):

* 15 games a page; ``pagenumber`` works.
* Page 1 is an odd set of 15 in no order, and those games appear on no other page.
* Pages 2..N hold everything else, sorted by game id, oldest first.
* A page past the end returns the last page. So ``LAST_PAGE`` fetches page N without knowing N,
  and a game that just ended is usually there: one request.

Anything else is a search over pages 2..N by game id: gallop up (2, 4, 8…) until a page starts
past the game or is the last page again, then bisect. About 2·log2(N) requests, capped.
"""

from __future__ import annotations

import logging

from joshua.wargear.client import WarGearClient
from joshua.wargear.models import Game

log = logging.getLogger(__name__)

LAST_PAGE = 9999
MAX_REQUESTS = 16


async def find_finished(client: WarGearClient, key: str, gameid: int) -> Game | None:
    """The finished game ``gameid`` as ``key`` sees it, or None if it isn't in their list."""
    cache: dict[int, list[Game]] = {}

    async def page(n: int) -> list[Game]:
        if n not in cache:
            if len(cache) >= MAX_REQUESTS:
                raise _GaveUp
            cache[n] = await client.my_games(key, "Finished", page=None if n == 1 else n)
        return cache[n]

    def hit(games: list[Game]) -> Game | None:
        return next((g for g in games if g.gameid == gameid), None)

    try:
        last = await page(LAST_PAGE)
        if found := hit(last):
            return found
        last_ids = {g.gameid for g in last}
        if last and gameid < min(last_ids):
            found = await _bisect(page, hit, gameid, last_ids)
            if found:
                return found
        return hit(await page(1))
    except _GaveUp:
        log.warning("gave up looking for game %s after %d Finished pages", gameid, MAX_REQUESTS)
        return None


class _GaveUp(Exception):
    pass


async def _bisect(page, hit, gameid: int, last_ids: set[int]) -> Game | None:
    """Search pages 2..N-1 (sorted by game id) for ``gameid``, which is below the last page."""

    async def where(n: int) -> int:
        """-1: the game is on an earlier page; 0: this page's range (or it's here); 1: a later page."""
        games = await page(n)
        ids = [g.gameid for g in games]
        if not ids or set(ids) == last_ids:
            return -1  # n is past the end (or the last page itself)
        if gameid < min(ids):
            return -1
        if gameid > max(ids):
            return 1
        return 0

    lo, hi = 2, 2
    while (w := await where(hi)) > 0:
        lo, hi = hi + 1, hi * 2
    if w == 0:
        return hit(await page(hi))
    hi -= 1
    while lo <= hi:
        mid = (lo + hi) // 2
        w = await where(mid)
        if w == 0:
            return hit(await page(mid))
        if w < 0:
            hi = mid - 1
        else:
            lo = mid + 1
    return None
