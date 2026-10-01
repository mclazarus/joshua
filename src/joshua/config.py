"""Runtime configuration, read from environment variables."""

from __future__ import annotations

from datetime import time
from functools import cached_property

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def parse_hours(spec: str) -> tuple[time, time]:
    """Parse "08:00-22:00" into a (start, end) pair of times."""
    start, _, end = spec.partition("-")
    if not end:
        raise ValueError(f"loud hours must look like 08:00-22:00, got {spec!r}")
    return time.fromisoformat(start.strip()), time.fromisoformat(end.strip())


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    slack_bot_token: str = ""
    slack_app_token: str = ""
    slack_channel_id: str = ""
    # Comma separated Slack user IDs allowed to run admin commands.
    joshua_admins: str = ""
    # Fernet key used to encrypt stored WarGear API keys.
    joshua_secret_key: str = ""

    db_path: str = "./data/joshua.db"

    wargear_base_url: str = "https://www.wargear.net"
    wargear_api_path: str = "/rest"
    wargear_new_game_url: str = "https://www.wargear.net/games/create"
    # Minimum seconds between any two WarGear requests, across all keys.
    wargear_min_interval: float = 2.0

    poll_seconds: int = 300
    poll_jitter_seconds: int = 60
    # Re-check keys whose games are already covered by another key this often.
    full_refresh_seconds: int = 3600
    tick_seconds: int = 30

    default_tz: str = "America/New_York"
    default_loud_hours: str = "08:00-22:00"
    nag_interval_minutes: int = 240
    # Shrinks every reminder interval to minutes for testing in a sandbox channel.
    joshua_fast_reminders: bool = False

    winner_followup_hours: int = 24

    # Open (sign-up) games: nag invitees who haven't joined, during default loud hours.
    signup_nag_hours: int = 12
    # Open games older than this are abandoned sign-up pages; ignore them.
    signup_max_age_days: int = 14

    log_level: str = Field(default="INFO")

    @cached_property
    def admin_ids(self) -> set[str]:
        return {a.strip() for a in self.joshua_admins.split(",") if a.strip()}

    @cached_property
    def loud_hours(self) -> tuple[time, time]:
        return parse_hours(self.default_loud_hours)

    def game_url(self, gameid: int) -> str:
        return f"{self.wargear_base_url}/games/view/{gameid}"
