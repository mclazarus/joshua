from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from joshua.config import Settings
from joshua.db import Store
from joshua.notifier import Notifier
from joshua.sync import apply
from tests.test_sync import game

NY = ZoneInfo("America/New_York")


def ts(*a) -> int:
    return int(datetime(*a, tzinfo=NY).timestamp())


T_START = ts(2026, 10, 7, 9)  # Wed 09:00 NY


class Recorder:
    def __init__(self):
        self.posts: list[str] = []

    async def post(self, text, blocks):
        self.posts.append(text)
        return f"ts{len(self.posts)}"


@pytest.fixture
async def env():
    store = await Store.open(":memory:")
    await store.link("U1", "Falken")
    await store.link("U2", "Lightman")
    await store.set_tz("U1", "America/New_York")
    rec = Recorder()
    n = Notifier(store, rec, Settings(slack_channel_id="C1", default_tz="America/New_York"))
    yield store, n, rec
    await store.close()


async def test_turn_reminders_fire_once_each(env):
    store, n, rec = env
    await apply(store, game(turnstamp=T_START), T_START, "U1")
    assert await n.remind(T_START + 60) == 1
    assert "<@U1>" in rec.posts[0] and "DEFCON 5" in rec.posts[0]
    assert await n.remind(T_START + 120) == 0  # idempotent
    assert await n.remind(ts(2026, 10, 7, 13, 1)) == 1  # 4h nag
    assert "DEFCON" in rec.posts[1]


async def test_restart_after_downtime_sends_only_latest(env):
    store, n, rec = env
    await apply(store, game(turnstamp=T_START), T_START, "U1")
    # Bot was down all day; at 21:40 only the 21:30 last call goes out.
    assert await n.remind(ts(2026, 10, 7, 21, 40)) == 1
    assert "last call" in rec.posts[0]
    assert await n.remind(ts(2026, 10, 7, 21, 41)) == 0


async def test_final_warnings_in_quiet_hours(env):
    store, n, rec = env
    start = ts(2026, 10, 7, 3)  # skip at 03:00 Thursday
    await apply(store, game(turnstamp=start), start, "U1")
    await n.remind(ts(2026, 10, 7, 21, 31))  # catch up to the last call
    rec.posts.clear()
    assert await n.remind(ts(2026, 10, 8, 2, 31)) == 1
    assert "30 MINUTES" in rec.posts[0]
    assert await n.remind(ts(2026, 10, 8, 2, 56)) == 1
    assert "DEFCON 1" in rec.posts[1]


async def test_untracked_games_are_silent(env):
    store, n, rec = env
    await apply(store, game(turnstamp=T_START), T_START, "U1")
    await store.set_tracked(42, False)
    assert await n.remind(T_START + 60) == 0


async def test_winner_nag_then_followup(env):
    store, n, rec = env
    now = ts(2026, 10, 7, 12)
    await apply(store, game(turnstamp=T_START, current=("Lightman",)), now - 100, "U1")
    done = game(
        current=(),
        status="Finished",
        winners=("Lightman",),
        startstamp=now - 10 * 86400,
        endstamp=now,
        turnstamp=now,
    )
    events = await apply(store, done, now, "U1")
    await n.handle_events(events, now)
    nags = await store.due_winner_nags(now + 30 * 3600)
    assert {r["kind"] for r in nags} == {"first", "followup"}

    rec.posts.clear()
    await n._winner_tick(now + 60)
    assert len(rec.posts) == 1 and "<@U2> won" in rec.posts[0]
    await n._winner_tick(now + 25 * 3600)
    assert len(rec.posts) == 2 and "still waiting on a new game" in rec.posts[1]


async def test_followup_skipped_when_winner_hosts_new_game(env):
    store, n, rec = env
    now = ts(2026, 10, 7, 12)
    await apply(store, game(turnstamp=T_START, current=("Lightman",)), now - 100, "U1")
    done = game(current=(), status="Finished", winners=("Lightman",), endstamp=now, turnstamp=now)
    await n.handle_events(await apply(store, done, now, "U1"), now)
    rematch = game(gameid=43, host="Lightman", createstamp=now + 3600, turnstamp=now + 3600)
    await apply(store, rematch, now + 3600, "U1")
    await n._winner_tick(now + 25 * 3600)
    assert not any("still waiting on a new game" in p for p in rec.posts)
    [nag] = [
        r
        for r in await store.db.execute_fetchall("SELECT kind, outcome FROM winner_nags")
        if r[0] == "followup"
    ]
    assert nag[1] == "new game already hosted"


async def test_identity_prompt_once_per_name(env):
    store, n, rec = env
    await apply(store, game(turnstamp=T_START), T_START, "U1")
    await n._identity_tick(T_START)
    await n._identity_tick(T_START + 60)
    asks = [p for p in rec.posts if "McKittrick" in p]
    assert len(asks) == 1


async def test_team_win_is_one_post_tagging_everyone(env):
    store, n, rec = env
    now = ts(2026, 10, 7, 12)
    await apply(store, game(turnstamp=T_START, current=("Lightman",)), now - 100, "U1")
    done = game(current=(), status="Finished", winners=("Lightman", "Falken"), endstamp=now, turnstamp=now)
    await n.handle_events(await apply(store, done, now, "U1"), now)
    rec.posts.clear()
    await n._winner_tick(now + 60)
    assert len(rec.posts) == 1
    assert "<@U1>" in rec.posts[0] and "<@U2>" in rec.posts[0]


def signup_game(now, statuses=("Joined", "Joined", "Invited"), seats=3, created=None, **kw):
    from joshua.wargear.models import PlayerState

    names = ("Falken", "Lightman", "McKittrick")
    return game(
        current=(),
        status="Open",
        seats=seats,
        createstamp=created or now - 3600,
        turnstamp=None,
        startstamp=None,
        players=tuple(
            PlayerState(n, str(i), i, st) for i, (n, st) in enumerate(zip(names, statuses, strict=True), 1)
        ),
        **kw,
    )


async def test_signup_nag_pings_invitees_and_repeats_after_interval(env):
    store, n, rec = env
    await store.link("U3", "McKittrick")
    now = ts(2026, 10, 7, 10)
    await apply(store, signup_game(now), now, "U1")
    assert await n._signup_tick(now) == 1
    assert "<@U3>" in rec.posts[0] and "1 seat" in rec.posts[0]
    assert "<@U1>" not in rec.posts[0]  # joined players aren't nagged
    assert await n._signup_tick(now + 3600) == 0
    assert await n._signup_tick(now + 12 * 3600 + 1) == 0  # 22:00, quiet hours
    assert await n._signup_tick(ts(2026, 10, 8, 9)) == 1


async def test_signup_nag_stops_when_full_old_or_started(env):
    store, n, rec = env
    now = ts(2026, 10, 7, 10)
    # Full: 3 seats, 3 joined.
    await apply(store, signup_game(now, ("Joined",) * 3), now, "U1")
    assert await n._signup_tick(now) == 0
    # The "invite 7, first 5 play" case: seats full though invitees remain.
    await apply(store, signup_game(now, ("Joined", "Joined", "Invited"), seats=2, gameid=50), now, "U1")
    assert await n._signup_tick(now) == 0
    # Abandoned sign-up page from years ago.
    await apply(store, signup_game(now, gameid=51, created=now - 400 * 86400), now, "U1")
    assert await n._signup_tick(now) == 0
    # Started games aren't open any more.
    await apply(store, signup_game(now, gameid=52), now, "U1")
    await apply(store, game(gameid=52, turnstamp=now), now + 60, "U1")
    assert await n._signup_tick(now + 60) == 0
    assert rec.posts == []


async def test_unwatched_game_drops_pending_winner_nags(env):
    store, n, rec = env
    now = ts(2026, 10, 7, 12)
    await apply(store, game(turnstamp=T_START, current=("Lightman",)), now - 100, "U1")
    done = game(current=(), status="Finished", winners=("Lightman",), endstamp=now, turnstamp=now)
    await n.handle_events(await apply(store, done, now, "U1"), now)
    await store.set_tracked(42, False)
    rec.posts.clear()
    assert await n._winner_tick(now + 30 * 3600) == 0
    assert rec.posts == []


async def test_timezone_falls_back_to_key_owner_then_eastern(env):
    store, n, rec = env
    await store.link("U3", "McKittrick")
    await store.set_tz("U2", "America/Los_Angeles")  # Lightman: the key we saw the game through
    # McKittrick is registered but has no tz yet; Falken (U1) is Eastern.
    await apply(store, game(turnstamp=T_START, current=("McKittrick",)), T_START, "U2")
    [turn] = await store.open_turns()
    assert turn.tz == "America/Los_Angeles"
    # Unregistered player, game seen via U2's key: still Lightman's tz.
    await store.unlink("U3")
    await store.link("U4", "Beringer")  # keep the game tracked with 2+ registered players
    [turn] = await store.open_turns()
    assert (turn.slack_id, turn.tz) == (None, "America/Los_Angeles")
    # Key owner has no tz either: Eastern.
    await store.set_tz("U2", None)
    [turn] = await store.open_turns()
    assert n.tz_for(turn.tz).key == "America/New_York"
