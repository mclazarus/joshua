import pytest
from cryptography.fernet import Fernet

from joshua.config import Settings
from joshua.crypto import KeyBox
from joshua.db import Store
from joshua.poller import Poller
from joshua.sync import GameFinished
from joshua.wargear.client import BadApiKey
from joshua.wargear.models import Game, Player
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
