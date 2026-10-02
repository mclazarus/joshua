from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from joshua.slack import messages as m

NY = ZoneInfo("America/New_York")
LA = ZoneInfo("America/Los_Angeles")


def ts(*a, tz=NY) -> int:
    return int(datetime(*a, tzinfo=tz).timestamp())


NOW = ts(2026, 10, 2, 14, 0)  # Friday 2:00 PM EDT


@pytest.mark.parametrize(
    ("deadline", "expected"),
    [
        (ts(2026, 10, 2, 21, 14), "today at 9:14 PM EDT"),
        (ts(2026, 10, 3, 11, 7), "tomorrow at 11:07 AM EDT"),
        (ts(2026, 10, 3, 0, 5), "tomorrow at 12:05 AM EDT"),
        (ts(2026, 10, 4, 12, 0), "Sunday at 12:00 PM EDT"),
        (ts(2026, 10, 12, 8, 30), "Oct 12 at 8:30 AM EDT"),
        (ts(2026, 10, 1, 23, 0), "yesterday at 11:00 PM EDT"),
    ],
)
def test_local_when_relative_days(deadline, expected):
    assert m.local_when(deadline, NY, NOW) == expected


def test_local_when_uses_the_players_calendar_day():
    # 1:30 AM Saturday in New York is still Friday evening in Los Angeles.
    deadline = ts(2026, 10, 3, 1, 30)
    assert m.local_when(deadline, NY, NOW) == "tomorrow at 1:30 AM EDT"
    assert m.local_when(deadline, LA, NOW) == "today at 10:30 PM PDT"


def test_reminder_deadline_is_a_live_slack_token_with_player_tz_fallback():
    deadline = ts(2026, 10, 3, 11, 7)
    text, blocks = m.turn_reminder(
        "nag", m.Who("falken", "U1"), "Global Thermonuclear War", "https://x", NOW - 3600, deadline, NOW, NY
    )
    body = blocks[0]["text"]["text"]
    token = f"<!date^{deadline}^{{date_short_pretty}} at {{time}} ({{ago}})|tomorrow at 11:07 AM EDT>"
    assert f"WarGear skips you *{token}*." in body


def test_time_diagnostic_renders_every_format():
    text, blocks = m.time_diagnostic(NOW, NY, [("tomorrow-ish", 23 * 3600 + 55 * 60)])
    body = blocks[-1]["text"]["text"]
    for fmt in m.SLACK_DATE_FORMATS:
        assert f"<!date^{NOW + 23 * 3600 + 55 * 60}^{fmt}|" in body
    assert "tomorrow at 1:55 PM EDT" in body
    assert all(len(b["text"]["text"]) < 3000 for b in blocks if b["type"] == "section")
