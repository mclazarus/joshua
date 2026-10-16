import pytest
from cryptography.fernet import Fernet

from joshua.config import Settings
from joshua.crypto import KeyBox
from joshua.db import Store, now_ts
from joshua.poller import Poller
from joshua.sync import GameFinished
from joshua.wargear.client import BadApiKey, WarGearError
from joshua.wargear.models import BoardInfo, Game, Player
from joshua.wargear.ratelimit import RateGate


def wg_game(gameid, players, current, **kw):
    fields = dict(
        gameid=gameid,
        name=f"Game {gameid}",
        players=[Player(id=str(i), name=n, seat=i) for i, n in enumerate(players, 1)],
        current_turn=[current] if current else [],
        turnstamp=1_790_000_000,
        boot_time=86400,
        gamestatus="In Progress",
    )
    return Game(**{**fields, **kw})


class FakeClient:
    def __init__(self):
        self.gate = RateGate()
        self.calls: list[tuple[str, str]] = []
        self.live: dict[str, list[Game]] = {}
        self.finished: dict[str, list[Game]] = {}
        self.bad: set[str] = set()
        self.request_count = 0
        self.histories: dict[int, dict] = {}
        self.boards: dict[int, BoardInfo] = {}

    async def history(self, key, gameid):
        self.calls.append((key, "history", gameid))
        self.request_count += 1
        if gameid not in self.histories:
            raise WarGearError("no history")
        return self.histories[gameid]

    async def board(self, key, boardid):
        self.calls.append((key, "board", boardid))
        self.request_count += 1
        return self.boards.get(boardid) or BoardInfo(boardid, f"Board {boardid}")

    async def my_games(self, key, selector="Live", page=None):
        self.calls.append((key, selector) if page is None else (key, selector, page))
        self.request_count += 1
        if key in self.bad:
            raise BadApiKey("nope")
        if selector == "Live":
            return self.live.get(key, [])
        pages = self.finished.get(key, [])
        index = (page or 1) - 1
        if pages and not isinstance(pages[0], list):
            pages = [pages]
        return pages[index] if index < len(pages) else []


@pytest.fixture
async def env():
    store = await Store.open(":memory:")
    box = KeyBox(Fernet.generate_key().decode())
    client = FakeClient()
    for sid, name in (("U1", "Falken"), ("U2", "Lightman"), ("U3", "McKittrick")):
        await store.link(sid, name)
        await store.set_api_key(sid, box.seal(f"key-{name}"), None)
    poller = Poller(store, client, box, Settings())
    yield store, client, poller
    await store.close()


async def test_shared_games_cost_one_request_after_first_full_cycle(env):
    store, client, poller = env
    shared = wg_game(1, ["Falken", "Lightman", "McKittrick"], "2")
    for k in ("key-Falken", "key-Lightman", "key-McKittrick"):
        client.live[k] = [shared]
    await poller.poll_once()
    assert len(client.calls) == 3  # first cycle is a full refresh
    client.calls.clear()
    await poller.poll_once()
    assert client.calls == [("key-Falken", "Live")]


async def test_key_in_most_games_goes_first(env):
    store, client, poller = env
    g1 = wg_game(1, ["Falken", "Lightman"], "1")
    g2 = wg_game(2, ["McKittrick", "Lightman"], "1")
    client.live.update({"key-Falken": [g1], "key-McKittrick": [g2], "key-Lightman": [g1, g2]})
    await poller.poll_once()
    client.calls.clear()
    await poller.poll_once()
    # Lightman is in both games, so Lightman's key alone covers everything.
    assert client.calls == [("key-Lightman", "Live")]


async def test_bad_key_is_marked_and_skipped(env):
    store, client, poller = env
    client.bad.add("key-Falken")
    await poller.poll_once()
    assert (await store.player("U1")).key_ok == 0
    client.calls.clear()
    await poller.poll_once()
    assert all(k != "key-Falken" for k, _ in client.calls)


async def test_game_leaving_live_list_is_looked_up_in_finished(env):
    store, client, poller = env
    live = wg_game(7, ["Falken", "Lightman"], "1")
    client.live["key-Falken"] = [live]
    await poller.poll_once()
    client.live["key-Falken"] = []
    client.finished["key-Falken"] = [
        wg_game(7, ["Falken", "Lightman"], None, winners=["2"], gamestatus="Finished", endstamp=1_790_100_000)
    ]
    events = await poller.poll_once()
    assert GameFinished(7, ("Lightman",), False) in events
    assert 7 not in await store.live_gameids()


def won_history(seat, at):
    return {
        "history": {
            "turn": [{"@attributes": {"turnid": "9", "timestamp": str(at), "action": "game_win", "s": seat}}]
        }
    }


async def test_game_leaving_live_list_is_closed_out_from_its_history(env):
    # The Finished list is unsorted and paginated, and can miss a game that just ended.
    store, client, poller = env
    client.live["key-Falken"] = [wg_game(7, ["Falken", "Lightman"], "Lightman")]
    await poller.poll_once()
    assert [t.player for t in await store.open_turns()] == ["Lightman"]
    client.live["key-Falken"] = []
    client.histories[7] = won_history("2", 1_790_100_000)
    client.calls.clear()
    events = await poller.poll_once()
    assert GameFinished(7, ("Lightman",), False) in events
    assert not any(c[1] == "Finished" for c in client.calls)
    assert 7 not in await store.live_gameids()
    assert await store.open_turns() == []  # no more "your turn" nags for the winner
    game = await store.game_state(7)
    assert game.winners == ("Lightman",) and game.endstamp == 1_790_100_000
    # The history is kept for the after-action report: no second fetch.
    client.calls.clear()
    await poller.poll_once()
    assert _extras(client) == []
    assert [r.gameid for r in await store.reports_due()] == [7]


async def test_history_without_a_win_falls_back_to_finished_list(env):
    store, client, poller = env
    client.live["key-Falken"] = [wg_game(7, ["Falken", "Lightman"], "1")]
    await poller.poll_once()
    client.live["key-Falken"] = []
    client.histories[7] = {"history": {"turn": []}}
    client.finished["key-Falken"] = [
        wg_game(7, ["Falken", "Lightman"], None, winners=["1"], gamestatus="Finished", endstamp=2)
    ]
    events = await poller.poll_once()
    assert ("key-Falken", "history", 7) in client.calls
    assert GameFinished(7, ("Falken",), False) in events


async def test_closed_gate_skips_cycle(env):
    store, client, poller = env
    client.gate.observe(429, {"Retry-After": "600"})
    assert await poller.poll_once() == []
    assert client.calls == []


async def test_finished_lookup_walks_pages_and_rechecks_hourly(env):
    store, client, poller = env
    client.live["key-Falken"] = [wg_game(7, ["Falken", "Lightman"], "1")]
    await poller.poll_once()
    client.live["key-Falken"] = []
    old = wg_game(3, ["Falken", "Lightman"], None, gamestatus="Finished", endstamp=1)
    client.finished["key-Falken"] = [[old], [old]]  # pagenumber ignored: page 2 repeats page 1
    client.calls.clear()
    await poller.poll_once()
    assert ("key-Falken", "Finished", 2) in client.calls
    assert ("key-Falken", "Finished", 3) not in client.calls
    client.calls.clear()
    await poller.poll_once()
    assert not any(c[1] == "Finished" for c in client.calls)  # not again within the hour


async def test_api_keys_are_encrypted_on_disk(tmp_path):
    path = tmp_path / "joshua.db"
    store = await Store.open(str(path))
    box = KeyBox(Fernet.generate_key().decode())
    secret = "wg_ro_0123456789abcdef0123456789abcdef"
    await store.link("U1", "Falken")
    await store.set_api_key("U1", box.seal(secret), True)
    await store.close()
    raw = b"".join(p.read_bytes() for p in tmp_path.iterdir())
    assert secret.encode() not in raw
    assert b"0123456789abcdef" not in raw
    assert path.stat().st_mode & 0o077 == 0  # owner-only
    store = await Store.open(str(path))
    assert box.open((await store.player("U1")).enc_api_key) == secret
    await store.close()


def _extras(client):
    return [c for c in client.calls if c[1] in ("history", "board")]


async def test_won_game_fetches_board_and_history_once(env):
    store, client, poller = env
    client.live["key-Falken"] = [wg_game(7, ["Falken", "Lightman"], "1", boardid=99)]
    await poller.poll_once()
    assert _extras(client) == [("key-Falken", "board", 99)]  # live tracked game: board for its fog
    client.calls.clear()
    client.live["key-Falken"] = [
        wg_game(7, ["Falken", "Lightman"], None, boardid=99, winners=["2"], gamestatus="Finished", endstamp=2)
    ]
    client.histories[7] = {"history": {"turn": []}}
    await poller.poll_once()
    assert _extras(client) == [("key-Falken", "history", 7)]
    client.calls.clear()
    await poller.poll_once()
    await poller.poll_once()
    assert _extras(client) == []
    assert [r.gameid for r in await store.reports_due()] == [7]


async def test_history_not_fetched_for_untracked_or_terminated_games(env):
    store, client, poller = env
    await store.unlink("U2")  # Lightman unregistered: game 7 has one registered player
    client.live["key-Falken"] = [wg_game(7, ["Falken", "Lightman"], "1")]
    await poller.poll_once()
    client.live["key-Falken"] = [wg_game(7, ["Falken", "Lightman"], None, winners=["1"], endstamp=2)]
    await poller.poll_once()
    assert _extras(client) == []


async def test_history_retries_are_capped(env):
    store, client, poller = env
    client.live["key-Falken"] = [wg_game(7, ["Falken", "Lightman"], "1")]
    await poller.poll_once()
    client.live["key-Falken"] = [wg_game(7, ["Falken", "Lightman"], None, winners=["1"], endstamp=2)]
    await poller.poll_once()  # queues the history; the fetch fails
    falken = await store.player("U1")
    start = now_ts()
    for minutes in (10, 61, 65, 125, 190, 300):
        await poller._fetch_extras([(falken, "key-Falken")], start + minutes * 60)
    assert sum(1 for c in client.calls if c[1] == "history") == 3  # first try, then hourly, then give up
