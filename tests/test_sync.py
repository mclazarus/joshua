from dataclasses import replace

import pytest

from joshua.db import Store
from joshua.sync import (
    GameAppeared,
    GameFinished,
    PlayerEliminated,
    TurnEnded,
    TurnStarted,
    apply,
    diff,
)
from joshua.wargear.models import GameState, PlayerState

T0 = 1_790_000_000


def game(current=("Falken",), turnstamp=T0, counters=(1, 1, 1), **kw) -> GameState:
    names = ("Falken", "Lightman", "McKittrick")
    base = dict(
        gameid=42,
        name="Global Thermonuclear War",
        board="WOPR",
        host="Falken",
        status="In Progress",
        gametype="Turn Based",
        players=tuple(
            PlayerState(n, str(i), i, "Playing", c)
            for i, (n, c) in enumerate(zip(names, counters, strict=True), 1)
        ),
        seats=None,
        current=frozenset(current),
        winners=(),
        eliminated=frozenset(),
        turnstamp=turnstamp,
        boot_time=86400,
        createstamp=T0 - 1000,
        startstamp=T0 - 900,
        endstamp=None,
    )
    base.update(kw)
    return GameState(**base)


def test_first_sight_starts_current_turn():
    assert diff(None, game(), T0 + 60) == [
        GameAppeared(42, "Falken", False),
        TurnStarted(42, "Falken", T0, T0, T0 + 86400),
    ]


def test_turn_passes():
    events = diff(game(), game(current=("Lightman",), turnstamp=T0 + 500), T0 + 600)
    assert events == [TurnEnded(42, "Falken"), TurnStarted(42, "Lightman", T0 + 500, T0 + 500, T0 + 86900)]


def test_no_change_no_events():
    assert diff(game(), game(), T0 + 600) == []


def test_same_player_new_turn_by_counter():
    events = diff(game(counters=(1, 1, 1)), game(turnstamp=T0 + 99, counters=(2, 2, 1)), T0 + 100)
    assert events == [TurnEnded(42, "Falken"), TurnStarted(42, "Falken", T0 + 99, T0 + 99, T0 + 99 + 86400)]


def test_mid_turn_activity_is_not_a_new_turn_when_counter_is_stable():
    assert diff(game(), game(turnstamp=T0 + 99), T0 + 100) == []


def test_simultaneous_turnstamp_change_without_counters_is_ignored():
    prev = game(current=("Falken", "Lightman"), gametype="Simultaneous", counters=(None, None, None))
    new = replace(prev, turnstamp=T0 + 50, current=frozenset({"Falken"}))
    assert diff(prev, new, T0 + 60) == [TurnEnded(42, "Lightman")]


def test_elimination_and_finish():
    prev = game(current=("Lightman",))
    new = game(
        current=(),
        status="Finished",
        winners=("Lightman",),
        endstamp=T0 + 9000,
        eliminated=frozenset({"Falken", "McKittrick"}),
    )
    assert diff(prev, new, T0 + 9001) == [
        TurnEnded(42, "Lightman"),
        PlayerEliminated(42, "Falken"),
        PlayerEliminated(42, "McKittrick"),
        GameFinished(42, ("Lightman",), False),
    ]


def test_finished_game_seen_first_time_is_not_a_finish_event():
    events = diff(None, game(current=(), status="Finished", winners=("Falken",)), T0)
    assert events == [GameAppeared(42, "Falken", True)]


@pytest.fixture
async def store():
    s = await Store.open(":memory:")
    yield s
    await s.close()


async def test_apply_round_trips_through_store(store):
    await apply(store, game(), T0, "U1")
    assert await store.open_turn_for(42, "Falken") == (T0, T0 + 86400)
    # Reloading from the DB and diffing against an identical snapshot is a no-op.
    assert await apply(store, game(), T0 + 300, "U1") == []
    await apply(store, game(current=("Lightman",), turnstamp=T0 + 400), T0 + 600, "U1")
    assert await store.open_turn_for(42, "Falken") is None
    assert await store.open_turn_for(42, "Lightman") == (T0 + 400, T0 + 400 + 86400)


async def test_tracking_needs_two_registered_players(store):
    await apply(store, game(), T0, "U1")
    assert not await store.is_tracked(42)
    await store.link("U1", "Falken")
    assert not await store.is_tracked(42)
    await store.link("U2", "lightman")  # case-insensitive
    assert await store.is_tracked(42)
    await store.set_tracked(42, False)
    assert not await store.is_tracked(42)
    assert [t.player for t in await store.open_turns()] == []
    await store.set_tracked(42, None)
    [turn] = await store.open_turns()
    assert (turn.player, turn.slack_id) == ("Falken", "U1")
    assert await store.unmapped_names() == [("McKittrick", 42)]


async def test_notme_releases_name_keeps_key_and_reopens_prompt(store):
    await apply(store, game(), T0, "U1")
    await store.link("U1", "Falken")
    await store.link("U2", "Lightman")
    await store.link("U3", "McKittrick")  # keeps the game tracked after Falken is released
    await store.set_api_key("U1", b"sealed", True)
    await store.record_identity_prompt("Falken", "C1", "1.0")
    assert await store.release_name("U1") == "Falken"
    p = await store.player("U1")
    assert p.wargear_name is None and p.enc_api_key == b"sealed"
    assert ("Falken", 42) in await store.unmapped_names()  # will be asked about again
    await store.link("U4", "Falken")  # and someone else can claim it


async def test_unlink_forgets_everything_and_reopens_prompt(store):
    await apply(store, game(), T0, "U1")
    await store.link("U1", "Falken")
    await store.link("U2", "Lightman")
    await store.link("U3", "McKittrick")
    await store.record_identity_prompt("Falken", "C1", "1.0")
    assert await store.unlink("U1") == "Falken"
    assert await store.player("U1") is None
    assert ("Falken", 42) in await store.unmapped_names()


async def test_unwatch_silences_and_is_listed(store):
    await apply(store, game(), T0, "U1")
    await store.link("U1", "Falken")
    await store.link("U2", "Lightman")
    assert await store.open_turns()
    await store.set_tracked(42, False)
    assert await store.open_turns() == []
    assert await store.unwatched_games() == [(42, "Global Thermonuclear War")]
    await store.set_tracked(42, True)
    assert await store.unwatched_games() == []
