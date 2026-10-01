from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from joshua.reminders import (
    LAST_CALL,
    NAG,
    START,
    T5,
    T30,
    Cadence,
    LoudHours,
    Reminder,
    due_now,
    is_loud,
    next_loud_start,
    plan_reminders,
)

NY = ZoneInfo("America/New_York")
LA = ZoneInfo("America/Los_Angeles")
DEFAULT = LoudHours(time(8), time(22))
DAY = timedelta(hours=24)


def at(y, mo, d, h, mi=0, tz=NY):
    return datetime(y, mo, d, h, mi, tzinfo=tz)


def schedule(start, tz=NY, loud=DEFAULT, deadline_after=DAY):
    plan = plan_reminders(start, start + deadline_after if deadline_after else None, tz, loud)
    return [(r.when.astimezone(tz).strftime("%a %H:%M"), r.kind) for r in plan]


def test_turn_starts_mid_morning():
    # Wed 2026-10-07 09:00 NY, skip Thu 09:00.
    assert schedule(at(2026, 10, 7, 9)) == [
        ("Wed 09:00", START),
        ("Wed 13:00", NAG),
        ("Wed 17:00", NAG),
        ("Wed 21:30", LAST_CALL),  # 21:00 nag folded into the last call
        ("Thu 08:00", NAG),  # quiet hours push the next nag to morning
        ("Thu 08:30", T30),
        ("Thu 08:55", T5),
    ]


def test_turn_starts_in_quiet_hours_waits_for_morning():
    # Starts 02:00, skip 02:00 next day: first ping at 08:00.
    assert schedule(at(2026, 10, 7, 2)) == [
        ("Wed 08:00", START),
        ("Wed 12:00", NAG),
        ("Wed 16:00", NAG),
        ("Wed 20:00", NAG),
        ("Wed 21:30", LAST_CALL),
        ("Thu 01:30", T30),  # final warnings fire even in quiet hours
        ("Thu 01:55", T5),
    ]


def test_turn_starts_late_evening():
    # 21:45 start: notified immediately; the 21:30 last call is already past.
    assert schedule(at(2026, 10, 7, 21, 45)) == [
        ("Wed 21:45", START),
        ("Thu 08:00", NAG),
        ("Thu 12:00", NAG),
        ("Thu 16:00", NAG),
        ("Thu 20:00", NAG),
        ("Thu 21:15", T30),
        ("Thu 21:40", T5),
    ]


def test_deadline_inside_loud_hours_drops_nags_in_final_half_hour():
    # Start 10:20, skip in 4h10m: the 14:20 nag would fall after T-30 (14:00).
    plan = schedule(at(2026, 10, 7, 10, 20), deadline_after=timedelta(hours=4, minutes=10))
    assert plan == [("Wed 10:20", START), ("Wed 14:00", T30), ("Wed 14:25", T5)]


def test_quiet_start_with_deadline_before_morning_only_gets_final_warnings():
    plan = schedule(at(2026, 10, 7, 1), deadline_after=timedelta(hours=5))
    assert plan == [("Wed 05:30", T30), ("Wed 05:55", T5)]


def test_no_boot_timer_keeps_nagging_without_final_warnings():
    plan = schedule(at(2026, 10, 7, 9), deadline_after=None)
    kinds = {k for _, k in plan}
    assert T30 not in kinds and T5 not in kinds
    assert plan[0] == ("Wed 09:00", START)
    assert len(plan) > 10


def test_player_timezone_shifts_loud_hours():
    # 06:00 NY is 03:00 LA: an LA player is pinged at 08:00 LA (11:00 NY).
    start = at(2026, 10, 7, 6)
    plan = plan_reminders(start, start + DAY, LA, DEFAULT)
    assert plan[0] == Reminder(at(2026, 10, 7, 8, tz=LA), START)


def test_dst_fall_back_keeps_wall_clock_loud_hours():
    # US DST ends 2026-11-01 at 02:00. A turn starting Sat 23:00 is pinged at
    # Sun 08:00 local, which is 25 hours of wall time later in UTC terms.
    start = at(2026, 10, 31, 23)
    plan = plan_reminders(start, start + DAY, NY, DEFAULT)
    first = plan[0]
    assert first.kind == START
    assert first.when.astimezone(NY).hour == 8
    assert first.when.astimezone(NY).date().day == 1
    assert (plan[-1].when - plan[-2].when) == timedelta(minutes=25)


def test_wrapping_loud_hours():
    night_owl = LoudHours(time(12), time(2))
    assert is_loud(at(2026, 10, 7, 1), NY, night_owl)
    assert not is_loud(at(2026, 10, 7, 3), NY, night_owl)
    assert next_loud_start(at(2026, 10, 7, 3), NY, night_owl) == at(2026, 10, 7, 12)


def test_every_reminder_is_before_deadline_and_sorted():
    for hour in range(24):
        start = at(2026, 10, 7, hour, 17)
        plan = plan_reminders(start, start + DAY, NY, DEFAULT)
        whens = [r.when for r in plan]
        assert whens == sorted(whens)
        assert all(start <= w < start + DAY for w in whens)
        assert plan[-2].kind == T30 and plan[-1].kind == T5
        # Nothing but final warnings in the last 30 minutes.
        assert all(r.kind in (T30, T5) for r in plan if r.when >= start + DAY - timedelta(minutes=30))


def test_fast_cadence_shrinks_intervals():
    start = at(2026, 10, 7, 9)
    plan = plan_reminders(start, start + timedelta(minutes=20), NY, DEFAULT, Cadence.fast())
    assert [r.kind for r in plan][:2] == [START, NAG]
    assert plan[1].when - plan[0].when == timedelta(minutes=4)


class TestDueNow:
    start = at(2026, 10, 7, 9)
    deadline = start + DAY
    plan = plan_reminders(start, deadline, NY, DEFAULT)

    def test_nothing_due_yet(self):
        assert due_now(self.plan, set(), self.start - timedelta(minutes=1), self.deadline) == (None, [])

    def test_sends_latest_and_skips_backlog(self):
        send, skip = due_now(self.plan, set(), at(2026, 10, 7, 18), self.deadline)
        assert send == Reminder(at(2026, 10, 7, 17), NAG)
        assert [r.when.hour for r in skip] == [9, 13]

    def test_already_sent_is_not_resent(self):
        sent = {r for r in self.plan if r.when <= at(2026, 10, 7, 17)}
        assert due_now(self.plan, sent, at(2026, 10, 7, 18), self.deadline) == (None, [])

    def test_past_deadline_sends_nothing(self):
        send, skip = due_now(self.plan, set(), self.deadline + timedelta(minutes=1), self.deadline)
        assert send is None
        assert len(skip) == len(self.plan)


@pytest.mark.parametrize("kind", [T30, T5])
def test_final_warnings_fire_in_quiet_hours(kind):
    start = at(2026, 10, 7, 3)  # skip at 03:00 tomorrow, deep in quiet hours
    plan = plan_reminders(start, start + DAY, NY, DEFAULT)
    r = next(r for r in plan if r.kind == kind)
    assert not is_loud(r.when, NY, DEFAULT)
