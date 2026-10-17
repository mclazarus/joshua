import pytest

from joshua import reports
from joshua.db import Store
from joshua.sync import apply
from tests.test_poller import FakeClient, wg_game

WON = {
    "history": {
        "turn": [
            {"@attributes": {"turnid": "1", "timestamp": "1000", "action": "select", "s": "1", "tid": "1"}},
            {"@attributes": {"turnid": "2", "timestamp": "1001", "action": "select", "s": "2", "tid": "2"}},
            {"@attributes": {"turnid": "3", "timestamp": "1002", "action": "game_win", "s": "2"}},
        ]
    }
}


def finished(gameid):
    return wg_game(gameid, ["Falken", "Lightman"], None, winners=["2"], gamestatus="Finished", endstamp=1002)


@pytest.fixture
async def env():
    store = await Store.open(":memory:")
    yield store, FakeClient()
    await store.close()


async def test_any_finished_game_of_yours_without_touching_the_store(env):
    store, client = env
    client.finished["key"] = [
        [wg_game(3, ["Falken"], None, gamestatus="Finished", endstamp=1)],
        [finished(7)],
    ]
    client.histories[7] = WON
    text, blocks = await reports.on_demand(store, client, "key", 7, "https://x/7")
    assert "AFTER-ACTION REPORT" in blocks[0]["text"]["text"] and "Lightman won" in text
    assert ("key", "Finished", 2) in client.calls  # found on page 2
    assert await store.game_state(7) is None and await store.history_body(7) is None


async def test_games_still_being_played_are_off_limits(env):
    store, client = env
    await apply(store, wg_game(7, ["Falken", "Lightman"], "1").normalize(), 1, None)
    with pytest.raises(reports.NoReport) as e:
        await reports.on_demand(store, client, "key", 7, "https://x/7")
    assert e.value.why == "live" and "still being fought" in e.value.text
    assert client.calls == []


async def test_unknown_game(env):
    store, client = env
    client.finished["key"] = [finished(3)]
    with pytest.raises(reports.NoReport) as e:
        await reports.on_demand(store, client, "key", 7, "https://x/7")
    assert e.value.why == "not_found"


async def test_uses_what_the_store_already_has(env):
    store, client = env
    await apply(store, finished(7).normalize(), 1, None)
    await store.queue_history(7, 1)
    await store.save_history(7, WON, 1)
    await reports.on_demand(store, client, "key", 7, "https://x/7")
    assert client.calls == []
