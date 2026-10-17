"""After-action reports: the channel's (one per won game) and the private on-demand kind.

``render`` is pure. ``on_demand`` gathers what a report needs for any finished game of yours,
using what the store already has before asking WarGear, and writes nothing about the game:
no history, no records, no "report posted" mark. Board rules are cached like the poller does.
"""

from __future__ import annotations

import json
import logging
import zlib
from typing import Any

from joshua.db import Store
from joshua.poller import finished_pages
from joshua.slack import messages as m
from joshua.stats import parse_history, replay
from joshua.wargear.client import WarGearClient
from joshua.wargear.models import BoardInfo, GameState

log = logging.getLogger(__name__)


class NoReport(Exception):
    """Why there's no report: ``why`` is a key for ``messages.no_report``."""

    def __init__(self, why: str, game: str):
        super().__init__(why, game)
        self.why, self.game = why, game

    @property
    def text(self) -> str:
        return m.no_report(self.why, self.game)


def render(game: GameState, board: BoardInfo | None, history: Any, url: str) -> m.Message | None:
    """The report, or None when the history has no actions."""
    actions = parse_history(history)
    if not actions:
        return None
    stats = replay(
        actions,
        board.attacker_sides if board else 6,
        board.defender_sides if board else 6,
        len(board.territories) if board else 0,
        teams={str(p.seat): p.team for p in game.players} if game.team_game else None,
    )
    names = {str(p.seat): p.name for p in game.players if p.seat is not None}
    return m.after_action_report(
        game.name, url, stats, names, board.territories if board else {}, game.winners
    )


async def on_demand(store: Store, client: WarGearClient, key: str, gameid: int, url: str) -> m.Message:
    """A report for one finished game, seen through ``key``. Raises NoReport."""
    game = await store.game_state(gameid)
    if game is not None and gameid in await store.live_gameids():
        raise NoReport("live", game.name)  # fog: no peeking at a game still being played
    if game is None or not game.finished:  # unknown to us, or closed out without a winner
        async for g in finished_pages(client, key):
            if g.gameid == gameid:
                game = g.normalize()
                break
        else:
            raise NoReport("not_found", str(gameid))

    board = await store.board(game.boardid)
    if board is None and game.boardid:
        board = await client.board(key, game.boardid)
        await store.save_board(board)

    body = await store.history_body(gameid)
    history = json.loads(zlib.decompress(body)) if body else await client.history(key, gameid)
    report = render(game, board, history, url)
    if report is None:
        raise NoReport("empty", game.name)
    return report
