"""When to nag someone about their turn.

The rules:

* Turn start: ping right away during loud hours, otherwise when loud hours begin.
* Every ``nag_every`` (4h) after that, during loud hours. A nag that would land in
  quiet hours moves to the next loud-hours start, and the 4h chain restarts there.
* A "last call" 30 minutes before loud hours end.
* T-30 and T-5 minutes before the skip, *even during quiet hours*.
* Ordinary reminders inside the final 30 minutes are dropped (T-30/T-5 cover it),
  and a nag within 30 minutes of a last call is folded into the last call.

Everything here is pure: callers pass in times and get back a schedule.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

START = "start"
NAG = "nag"
LAST_CALL = "last_call"
T30 = "t30"
T5 = "t5"

# How long to keep nagging a turn with no boot timer.
NO_DEADLINE_HORIZON = timedelta(days=7)


@dataclass(frozen=True)
class LoudHours:
    start: time
    end: time

    def contains(self, local: time) -> bool:
        if self.start < self.end:
            return self.start <= local < self.end
        # Window wraps past midnight, e.g. 20:00-02:00.
        return local >= self.start or local < self.end


@dataclass(frozen=True)
class Cadence:
    nag_every: timedelta = timedelta(hours=4)
    final_warning: timedelta = timedelta(minutes=30)
    last_warning: timedelta = timedelta(minutes=5)
    last_call_before_quiet: timedelta = timedelta(minutes=30)
    # Never send two ordinary reminders closer together than this (final warnings excepted).
    min_gap: timedelta = timedelta(minutes=30)

    @classmethod
    def fast(cls) -> Cadence:
        """Hours become minutes and minutes become seconds, for sandbox testing."""
        return cls(timedelta(minutes=4), *(timedelta(seconds=x) for x in (30, 5, 30, 30)))


DEFAULT_CADENCE = Cadence()


@dataclass(frozen=True, order=True)
class Reminder:
    when: datetime
    kind: str


def is_loud(dt: datetime, tz: ZoneInfo, loud: LoudHours) -> bool:
    return loud.contains(dt.astimezone(tz).time())


def _boundaries(dt: datetime, tz: ZoneInfo, at: time) -> list[datetime]:
    """Local ``at`` on the days around ``dt``, as aware datetimes, ascending."""
    local_day = dt.astimezone(tz).date()
    return [datetime.combine(local_day + timedelta(days=d), at, tzinfo=tz) for d in (-1, 0, 1, 2)]


def next_loud_start(dt: datetime, tz: ZoneInfo, loud: LoudHours) -> datetime:
    """First start of loud hours strictly after ``dt``."""
    return next(b for b in _boundaries(dt, tz, loud.start) if b > dt)


def loud_ends_between(a: datetime, b: datetime, tz: ZoneInfo, loud: LoudHours) -> list[datetime]:
    """Every end of loud hours in the half-open interval (a, b]."""
    out: list[datetime] = []
    cursor = a
    while True:
        nxt = next(e for e in _boundaries(cursor, tz, loud.end) if e > cursor)
        if nxt > b:
            return out
        out.append(nxt)
        cursor = nxt


def plan_reminders(
    turn_start: datetime,
    deadline: datetime | None,
    tz: ZoneInfo,
    loud: LoudHours,
    cadence: Cadence = DEFAULT_CADENCE,
) -> list[Reminder]:
    """The full reminder schedule for one player's turn, sorted by time."""
    hard_stop = deadline if deadline else turn_start + NO_DEADLINE_HORIZON
    # Ordinary reminders stop when the final warnings take over.
    cutoff = hard_stop - cadence.final_warning if deadline else hard_stop

    first = turn_start if is_loud(turn_start, tz, loud) else next_loud_start(turn_start, tz, loud)
    plan: list[Reminder] = []
    if first < cutoff:
        plan.append(Reminder(first, START))

    last_calls = [
        end - cadence.last_call_before_quiet for end in loud_ends_between(turn_start, hard_stop, tz, loud)
    ]
    last_calls = [lc for lc in last_calls if first < lc < cutoff]
    plan += [Reminder(lc, LAST_CALL) for lc in last_calls]

    t = first
    while True:
        t = t + cadence.nag_every
        if not is_loud(t, tz, loud):
            t = next_loud_start(t, tz, loud)
        if t >= cutoff:
            break
        if any(abs(t - lc) <= cadence.last_call_before_quiet for lc in last_calls):
            continue
        plan.append(Reminder(t, NAG))

    if deadline:
        for offset, kind in ((cadence.final_warning, T30), (cadence.last_warning, T5)):
            when = deadline - offset
            if when > turn_start:
                plan.append(Reminder(when, kind))

    return sorted(plan)


def due_now(
    plan: list[Reminder],
    already_sent: set[Reminder],
    now: datetime,
    deadline: datetime | None,
) -> tuple[Reminder | None, list[Reminder]]:
    """Choose which reminder to send now.

    Returns ``(send, skip)``. ``send`` is the most recent reminder that's due and
    unsent. ``skip`` holds older due reminders to record as sent without posting,
    so the bot doesn't post a burst of catch-up messages after downtime.
    """
    if deadline and now >= deadline:
        return None, [r for r in plan if r not in already_sent]
    due = [r for r in plan if r.when <= now and r not in already_sent]
    if not due:
        return None, []
    return due[-1], due[:-1]
