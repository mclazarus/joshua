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
        self.blocks: list[list[dict]] = []

    async def post(self, text, blocks):
        self.posts.append(text)
        self.blocks.append(blocks)
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


def has_key_button(blocks) -> bool:
    return any(
        e.get("action_id") == "offer_apikey" for b in blocks if b["type"] == "actions" for e in b["elements"]
    )


async def test_key_button_offered_to_keyless_player_weekly(env):
    store, n, rec = env
    await apply(store, game(turnstamp=T_START), T_START, "U1")
    assert await n.remind(T_START + 60) == 1
    assert has_key_button(rec.blocks[0])  # Falken has no key
    assert await n.remind(ts(2026, 10, 7, 13, 1)) == 1
    assert not has_key_button(rec.blocks[1])  # offered 4h ago
    # A new turn a week later gets it again.
    later = T_START + 7 * 86400
    await apply(store, game(turnstamp=later, counters=(2, 1, 1)), later, "U1")
    assert await n.remind(later + 60) == 1
    assert has_key_button(rec.blocks[2])


async def test_key_button_not_offered_with_a_key_or_unregistered(env):
    store, n, rec = env
    await store.set_api_key("U1", b"sealed", True)
    await apply(store, game(turnstamp=T_START), T_START, "U1")
    await n.remind(T_START + 60)
    assert not has_key_button(rec.blocks[0])
    # McKittrick isn't registered at all: there's nobody to ask.
    await apply(store, game(gameid=43, current=("McKittrick",), turnstamp=T_START), T_START, "U1")
    await n.remind(T_START + 120)
    assert not has_key_button(rec.blocks[1])


async def test_elimination_announced_only_without_fog(env):
    store, n, rec = env
    for gameid, fog in ((42, "None"), (43, "Medium"), (44, "Board Default"), (45, None)):
        await apply(store, game(gameid=gameid, turnstamp=T_START, fog=fog), T_START, "U1")
        out = game(gameid=gameid, turnstamp=T_START, fog=fog, eliminated=frozenset({"Lightman"}))
        await n.handle_events(await apply(store, out, T_START + 60, "U1"), T_START + 60)
    assert len(rec.posts) == 1 and "<@U2> has been eliminated" in rec.posts[0]


async def test_board_default_fog_resolved_from_board(env):
    from joshua.wargear.models import BoardInfo

    store, n, rec = env
    await store.save_board(BoardInfo(7, "Open Field", fog="None"))
    await apply(store, game(turnstamp=T_START, fog="Board Default", boardid=7), T_START, "U1")
    out = game(turnstamp=T_START, fog="Board Default", boardid=7, eliminated=frozenset({"Lightman"}))
    await n.handle_events(await apply(store, out, T_START + 60, "U1"), T_START + 60)
    assert len(rec.posts) == 1


async def test_no_elimination_posts_when_game_ends(env):
    store, n, rec = env
    now = ts(2026, 10, 7, 12)
    await apply(store, game(turnstamp=T_START, fog="None"), now - 100, "U1")
    done = game(
        current=(),
        status="Finished",
        fog="None",
        winners=("Falken",),
        endstamp=now,
        eliminated=frozenset({"Lightman", "McKittrick"}),
    )
    await n.handle_events(await apply(store, done, now, "U1"), now)
    assert rec.posts == []


async def test_winner_post_shows_record(env):
    store, n, rec = env
    now = ts(2026, 10, 7, 12)
    for gameid, winner in ((40, "Lightman"), (41, "Falken")):
        await apply(store, game(gameid=gameid, current=()), now - 100, "U1")
        done = game(gameid=gameid, current=(), status="Finished", winners=(winner,), endstamp=now)
        await n.handle_events(await apply(store, done, now, "U1"), now)
    await n._winner_tick(now + 60)
    footers = [b["elements"][0]["text"] for bl in rec.blocks for b in bl if b["type"] == "context"]
    assert any("*Lightman*: 1–1 overall · 1–1 on WOPR" in f for f in footers)


async def test_after_action_report_waits_for_winner_post(env):
    store, n, rec = env
    now = ts(2026, 10, 7, 12)
    await apply(store, game(current=()), now - 100, "U1")
    done = game(current=(), status="Finished", winners=("Falken",), endstamp=now)
    await n.handle_events(await apply(store, done, now, "U1"), now)
    await store.queue_history(42, now)
    history = {
        "history": {
            "turn": [
                {
                    "@attributes": {
                        "turnid": "1",
                        "timestamp": str(now),
                        "action": "select",
                        "s": "1",
                        "tid": "1",
                    }
                },
                {
                    "@attributes": {
                        "turnid": "2",
                        "timestamp": str(now),
                        "action": "select",
                        "s": "2",
                        "tid": "2",
                    }
                },
                {"@attributes": {"turnid": "3", "timestamp": str(now), "action": "start_turn", "s": "1"}},
                {
                    "@attributes": {
                        "turnid": "4",
                        "timestamp": str(now),
                        "action": "attack",
                        "s": "1",
                        "fromid": "1",
                        "toid": "2",
                        "ad": "6",
                        "dd": "1",
                        "dl": "1",
                    }
                },
                {
                    "@attributes": {
                        "turnid": "5",
                        "timestamp": str(now),
                        "action": "occupy",
                        "s": "1",
                        "fromid": "1",
                        "toid": "2",
                        "count": "1",
                    }
                },
                {
                    "@attributes": {
                        "turnid": "6",
                        "timestamp": str(now),
                        "action": "eliminate_player",
                        "s": "1",
                        "seats": "2",
                    }
                },
                {"@attributes": {"turnid": "7", "timestamp": str(now), "action": "game_win", "s": "1"}},
            ]
        }
    }
    await store.save_history(42, history, now)
    assert await n._report_tick(now) == 0  # the winner post goes first
    await n._winner_tick(now)
    assert await n._report_tick(now) == 1
    report = rec.blocks[-1][0]["text"]["text"]
    assert "AFTER-ACTION REPORT" in report
    assert "*Falken* eliminated *Lightman*" in report
    assert "<@" not in report  # names, not pings
    assert await n._report_tick(now + 60) == 0


async def test_unwatched_game_gets_no_report(env):
    store, n, rec = env
    now = ts(2026, 10, 7, 12)
    await apply(store, game(current=()), now - 100, "U1")
    done = game(current=(), status="Finished", winners=("Falken",), endstamp=now)
    await n.handle_events(await apply(store, done, now, "U1"), now)
    await n._winner_tick(now)
    await store.queue_history(42, now)
    await store.save_history(42, {"history": {"turn": []}}, now)
    await store.set_tracked(42, False)
    assert await n._report_tick(now) == 0


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


async def test_schedule_change_mid_turn_does_not_double_post(env):
    store, n, rec = env
    start = ts(2026, 10, 7, 6, 30)  # quiet hours: first ping waits for 08:00
    await apply(store, game(turnstamp=start), start, "U1")
    assert await n.remind(ts(2026, 10, 7, 8, 0)) == 1
    assert await n.remind(ts(2026, 10, 7, 12, 0)) == 1
    # At 12:30 Falken widens their loud hours. The new plan has an unsent 11:00 nag
    # (07:00 + 4h) that is "due", but they were pinged 30 minutes ago.
    await store.set_loud_hours("U1", "07:00-23:00")
    assert await n.remind(ts(2026, 10, 7, 12, 30)) == 0
    # The schedule carries on normally: next nag at 15:00 on the new chain.
    assert await n.remind(ts(2026, 10, 7, 15, 1)) == 1


async def test_final_warnings_ignore_the_minimum_gap(env):
    store, n, rec = env
    start = ts(2026, 10, 7, 9)
    await apply(store, game(turnstamp=start), start, "U1")
    deadline = start + 86400
    await n.remind(deadline - 31 * 60)  # some reminder just went out
    rec.posts.clear()
    assert await n.remind(deadline - 30 * 60 + 1) == 1
    assert "30 MINUTES" in rec.posts[0]


async def test_restart_from_disk_does_not_repost(tmp_path):
    path = str(tmp_path / "joshua.db")
    settings = Settings(slack_channel_id="C1", default_tz="America/New_York")
    store = await Store.open(path)
    await store.link("U1", "Falken")
    await store.link("U2", "Lightman")
    await apply(store, game(turnstamp=T_START), T_START, "U1")
    rec = Recorder()
    assert await Notifier(store, rec, settings).remind(T_START + 60) == 1
    await store.close()

    # Container restarts: new process, same /data volume.
    store = await Store.open(path)
    rec2 = Recorder()
    n2 = Notifier(store, rec2, settings)
    assert await n2.remind(T_START + 120) == 0
    assert await n2.remind(ts(2026, 10, 7, 13, 1)) == 1  # the 13:00 nag, once
    assert await n2.remind(ts(2026, 10, 7, 13, 2)) == 0
    await store.close()
