import gzip
import json
from pathlib import Path

import pytest

from joshua.stats import expected_losses, parse_history, replay, resolve
from joshua.wargear.models import parse_board, parse_game_list

FIXTURES = Path(__file__).parent / "fixtures"


def a(turnid, action, s="", **kw):
    return {
        "@attributes": {
            "turnid": str(turnid),
            "timestamp": str(1000 + turnid),
            "action": action,
            "s": s,
            **{k: str(v) for k, v in kw.items()},
        }
    }


def log(*turns):
    return parse_history({"history": {"turn": list(turns)}})


def test_resolve_pairs_highest_dice_and_ties_go_to_defender():
    assert resolve((6, 3, 1), (5, 3)) == (1, 1)
    assert resolve((2,), (2,)) == (1, 0)
    assert resolve((6, 6, 6), (1,)) == (0, 1)


def test_expected_losses_match_classic_risk_odds():
    # 3 vs 2: defender loses both 2890/7776, one each 2611/7776, attacker loses both 2275/7776.
    al, dl = expected_losses(3, 2, 6, 6)
    assert dl == pytest.approx((2 * 2890 + 2611) / 7776)
    assert al == pytest.approx((2 * 2275 + 2611) / 7776)
    assert expected_losses(1, 1, 6, 6)[1] == pytest.approx(15 / 36)


def test_luck_is_zero_sum_and_rewards_beating_the_odds():
    s = replay(
        log(
            a(1, "select", "1", tid=1),
            a(2, "select", "2", tid=2),
            a(3, "attack", "1", fromid=1, toid=2, ad="1", dd="6", al=1),  # expected: attacker loses
            a(4, "attack", "1", fromid=1, toid=2, ad="6", dd="1", dl=1),
        )
    )
    att, dfn = s.players["1"], s.players["2"]
    assert att.luck == pytest.approx(-dfn.luck)
    assert att.luck == pytest.approx(1 - 2 * 15 / 36)  # killed 1, expected 2 × 15/36
    assert s.luck_counted


def test_border_modifier_adds_die_sides():
    s = replay(
        log(
            a(1, "select", "1", tid=1),
            a(2, "select", "2", tid=2),
            a(3, "attack", "1", fromid=1, toid=2, ad="8", dd="6", bmod="2,0", dl=1),
        )
    )
    assert s.players["1"].luck == pytest.approx(1 - expected_losses(1, 1, 8, 6)[1])


def test_unknown_dice_rules_turn_luck_off():
    s = replay(
        log(
            a(1, "select", "1", tid=1),
            a(2, "select", "2", tid=2),
            a(3, "attack", "1", fromid=1, toid=2, ad="6", dd="1", al=1),
        )
    )  # attacker won but lost?
    assert not s.luck_counted


def test_battles_turns_and_territory():
    s = replay(
        log(
            a(1, "select", "1", tid=1),
            a(2, "select", "1", tid=2),
            a(3, "select", "2", tid=3),
            a(4, "setup_complete"),
            a(5, "start_turn", "1"),
            a(6, "attack", "1", fromid=1, toid=3, ad="6,5", dd="1", dl=1),
            a(7, "attack", "1", fromid=1, toid=3, ad="6,5", dd="1", dl=1),
            a(8, "occupy", "1", fromid=1, toid=3, count=2),
            a(9, "eliminate_player", "1", seats="2"),
            a(10, "end_turn", "1"),
        )
    )
    assert s.biggest_battle.losses == 2 and s.biggest_battle.conquered and s.biggest_battle.defender == "2"
    assert s.bloodiest_turn.seat == "1" and s.bloodiest_turn.losses == 2
    assert s.players["1"].peak_territories == 3 and s.players["2"].peak_territories == 1
    assert [(e.by, e.victim) for e in s.eliminations] == [("1", "2")]


def test_real_game_replay():
    history = json.loads(gzip.open(FIXTURES / "history.json.gz", "rt").read())
    board = parse_board(json.loads((FIXTURES / "board.json").read_text()), 6478)
    [g] = parse_game_list(json.loads((FIXTURES / "history_game.json").read_text()))
    state = g.normalize()
    assert board.fog == "Medium" and (board.attacker_sides, board.defender_sides) == (6, 6)
    assert len(board.territories) == 132

    s = replay(parse_history(history), board.attacker_sides, board.defender_sides, len(board.territories))
    assert s.luck_counted  # every roll matched standard rules
    seats = {str(p.seat): p for p in state.players}
    assert sum(p.luck for p in s.players.values()) == pytest.approx(0, abs=1e-6)
    # The kill board agrees with the finished game's statuses.
    victims = {e.victim for e in s.eliminations}
    assert {seats[v].status for v in victims} == {"Eliminated"}
    [winner] = state.winners
    last = s.eliminations[-1]
    assert seats[last.by].name == winner
    assert s.players[last.by].peak_territories == 132  # the winner ends up owning the board


def _rounds(n, extra=None, start=10):
    """``n`` rounds for seats 1-3. Seats 1 and 2 both hit seat 3; seat 3 never attacks.
    ``extra`` maps a round number to more actions for seat 1's turn in that round."""
    out, t = [], start
    for r in range(1, n + 1):
        for seat in "123":
            out.append(a(t, "start_turn", seat))
            t += 1
            if seat in "12":
                out.append(a(t, "attack", seat, fromid=int(seat), toid=3, ad="1", dd="6", al=1))
                t += 1
            if seat == "1":
                for kind, kw in (extra or {}).get(r, []):
                    out.append(a(t, kind, "1", **kw))
                    t += 1
            out.append(a(t, "end_turn", seat))
            t += 1
    return out


SETUP = [
    a(1, "select", "1", tid=1),
    a(2, "select", "2", tid=2),
    a(3, "select", "3", tid=3),
    a(4, "place", "1", tid=1, count=100),
    a(5, "place", "2", tid=2, count=100),
    a(6, "place", "3", tid=3, count=500),
    a(7, "setup_complete"),
]


HIT_2 = [("attack", dict(fromid=1, toid=2, ad="6", dd="1", dl=1))]


def test_neighbours_who_never_fight_while_ganging_up_look_allied():
    # Territories 1 and 2 border: seat 1 finally attacks seat 2 in round 8. Rounds 1-2 don't
    # count, so rounds 3-7 are the five turns each of peace.
    [pact] = replay(log(*SETUP, *_rounds(8, {8: HIT_2}))).pacts
    assert pact.seats == ("1", "2") and pact.rounds == 5 and pact.enemies == ("3",)


def test_the_opening_rounds_never_count_towards_an_alliance():
    assert replay(log(*SETUP, *_rounds(7, {7: HIT_2}))).pacts == []  # only rounds 3-6: four


def test_an_attack_between_them_resets_the_truce():
    assert replay(log(*SETUP, *_rounds(10, {6: HIT_2}))).pacts == []  # three turns of peace, then four


def test_teammates_are_never_allies():
    def pacts(teams):
        return replay(log(*SETUP, *_rounds(8, {8: HIT_2})), teams=teams).pacts

    assert pacts({"1": "1", "2": "1", "3": "2"}) == []
    assert [p.seats for p in pacts({"1": "1", "2": "2", "3": "2"})] == [("1", "2")]  # across teams
    assert pacts({"1": "1", "2": None, "3": "2"}) == []  # a team game with a team unknown


def test_turtles_and_deathballs():
    s = replay(
        log(
            *SETUP,
            *_rounds(
                3,
                {
                    1: [("place", dict(tid=1, count=0))],
                    2: [("skip_turn", {})],
                },
            ),
        )
    )
    # Seat 3 never attacks and has the most troops on the board every turn.
    assert (s.players["3"].turtle_turns, s.players["3"].turtle_leading) == (3, 3)
    # Seat 1 attacks one territory a turn, which still counts; its skipped turn doesn't.
    assert s.players["1"].turtle_turns == 2
    # Seat 3's 500 sit there from setup: a deathball only counts when it grows on your turn.
    # Seat 1 lost one attacking before the place.
    assert (s.biggest_deathball.seat, s.biggest_deathball.units) == ("1", 99)


def test_deathball_must_be_over_the_limit():
    def game(count):
        return replay(
            log(
                a(1, "select", "1", tid=1),
                a(2, "select", "2", tid=2),
                a(3, "start_turn", "1"),
                a(4, "place", "1", tid=1, count=count),
                a(5, "end_turn", "1"),
            )
        )

    assert game(20).biggest_deathball is None and game(20).players["1"].deathball_turns == 0
    assert game(21).biggest_deathball.units == 21 and game(21).players["1"].deathball_turns == 1


def test_real_game_deathballs_turtles_and_pacts():
    s = replay(parse_history(json.loads(gzip.open(FIXTURES / "history.json.gz", "rt").read())))
    assert (s.biggest_deathball.seat, s.biggest_deathball.units) == ("2", 70)
    assert all(p.turtle_leading <= p.turtle_turns for p in s.players.values())
    assert [p.seats for p in s.pacts] == [("2", "4")]  # ("1", "2") only kept the peace in the opening
