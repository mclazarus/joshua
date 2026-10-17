import pytest

from joshua.wargear.finished import MAX_REQUESTS, find_finished
from tests.test_poller import FakeClient, wg_game


def game(gameid):
    return wg_game(gameid, ["Falken", "Lightman"], None, gamestatus="Finished", endstamp=1)


@pytest.fixture
def client():
    """Shaped like the real list on 2026-10-16: an odd page 1, then 17 pages sorted by id, the
    last one short, and any page past the end returning the last page."""
    c = FakeClient()
    page1 = [game(i) for i in (900, 5, 4000, 70)]
    ids = [i for i in range(1000, 1000 + 15 * 16 + 10) if i != 1101]  # 1101 never finished
    sorted_pages = [[game(i) for i in ids[n : n + 15]] for n in range(0, len(ids), 15)]
    c.finished["key"] = [page1, *sorted_pages]
    return c


def finished_calls(c):
    return [x for x in c.calls if x[1] == "Finished"]


async def test_a_game_that_just_ended_is_one_request(client):
    assert (await find_finished(client, "key", 1249)).gameid == 1249
    assert finished_calls(client) == [("key", "Finished", 9999)]


@pytest.mark.parametrize("gameid", [1000, 1014, 1015, 1100, 1102, 1200, 1239])
async def test_older_games_are_found_by_bisecting(client, gameid):
    assert (await find_finished(client, "key", gameid)).gameid == gameid
    assert len(finished_calls(client)) <= 10


async def test_the_odd_first_page_is_checked_too(client):
    assert (await find_finished(client, "key", 4000)).gameid == 4000


async def test_missing_games(client):
    assert await find_finished(client, "key", 5000) is None  # newer than anything finished
    assert await find_finished(client, "key", 1101) is None  # a gap mid-list
    assert await find_finished(client, "key", 999) is None  # older than anything
    assert len(finished_calls(client)) <= 3 * MAX_REQUESTS


async def test_short_lists_and_empty_lists():
    c = FakeClient()
    c.finished["key"] = [[game(5), game(3)]]  # one page: page 1 is the last page
    assert (await find_finished(c, "key", 3)).gameid == 3
    assert await find_finished(c, "key", 4) is None
    assert await find_finished(FakeClient(), "key", 4) is None
