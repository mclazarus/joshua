"""joshua: run | dry-run | discover | healthcheck | genkey"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import logging
import os
import re
import sqlite3
import sys
import time
from pathlib import Path

from joshua.config import Settings

API_KEY_PATTERN = re.compile(r"wg_[a-z]+_[0-9a-fA-F]+")


class RedactKeys(logging.Filter):
    """Belt and braces: never let a WarGear API key reach the logs (e.g. in a request URL)."""

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        if API_KEY_PATTERN.search(msg):
            record.msg, record.args = API_KEY_PATTERN.sub("wg_***", msg), None
        return True


def _setup_logging(level: str) -> None:
    logging.basicConfig(level=level.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    for handler in logging.getLogger().handlers:
        handler.addFilter(RedactKeys())
    logging.getLogger("slack_bolt").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("aiosqlite").setLevel(logging.WARNING)
    logging.getLogger("slack_sdk").setLevel(logging.WARNING)


def cmd_run(settings: Settings, _args) -> None:
    from joshua.slack.app import run

    asyncio.run(run(settings))


def cmd_genkey(_settings: Settings, _args) -> None:
    from cryptography.fernet import Fernet

    print(Fernet.generate_key().decode())


def cmd_healthcheck(settings: Settings, _args) -> None:
    """Healthy if a poll finished recently and Slack's socket is connected."""
    now = int(time.time())
    try:
        conn = sqlite3.connect(f"file:{settings.db_path}?mode=ro", uri=True, timeout=5)
        kv = dict(conn.execute("SELECT key, value FROM kv").fetchall())
    except sqlite3.Error as e:
        sys.exit(f"unhealthy: db: {e}")
    poll_age = now - int(kv.get("last_poll_ok", 0))
    slack_age = now - int(kv.get("slack_ok", 0))
    limit = 3 * settings.poll_seconds + settings.poll_jitter_seconds
    problems = []
    if poll_age > limit:
        problems.append(f"last poll {poll_age}s ago")
    if slack_age > max(300, 5 * settings.tick_seconds):
        problems.append(f"slack socket last seen {slack_age}s ago")
    if problems:
        sys.exit("unhealthy: " + "; ".join(problems))
    print(f"ok: poll {poll_age}s ago, slack {slack_age}s ago")


async def _discover(settings: Settings, args) -> None:
    """Phase 0 helper: call each endpoint and save the raw JSON, plus print rate-limit headers."""
    import httpx

    from joshua.wargear.client import USER_AGENT

    key = _api_key()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    calls = [
        ("current_turns", "/GetCurrentTurns", {}),
        ("my_live", "/GetGameList/my", {"viewselector": "Live"}),
        ("my_finished", "/GetGameList/my", {"viewselector": "Finished"}),
        ("my_turn", "/GetGameList/my", {"viewselector": "My Turn"}),
    ]
    if args.player:
        calls.append(("player", "/GetGameList/player", {"player": args.player}))
    if args.game:
        calls.append(("history", f"/GetHistoryUpdate/{args.game}", {}))
    base = settings.wargear_base_url + settings.wargear_api_path
    async with httpx.AsyncClient(base_url=base, headers={"User-Agent": USER_AGENT}, timeout=30) as http:
        for name, path, params in calls:
            resp = await http.get(path, params={**params, "api_key": key})
            interesting = {
                k: v
                for k, v in resp.headers.items()
                if any(
                    s in k.lower()
                    for s in ("rate", "retry", "limit", "cache", "etag", "last-modified", "server", "cf-")
                )
            }
            print(f"== {name}: HTTP {resp.status_code} {len(resp.content)} bytes")
            for k, v in interesting.items():
                print(f"   {k}: {v}")
            try:
                body = resp.json()
                (out / f"{name}.json").write_text(json.dumps(body, indent=2))
            except ValueError:
                (out / f"{name}.txt").write_text(resp.text)
            await asyncio.sleep(3)  # be polite even while exploring
    print(f"saved to {out}/ (gitignored; sanitize before copying into tests/fixtures)")


def _api_key() -> str:
    """From $WARGEAR_API_KEY, or a hidden prompt. Never a CLI flag: those land in shell history."""
    key = os.environ.get("WARGEAR_API_KEY") or getpass.getpass("WarGear API key (input hidden): ")
    if not key.strip():
        sys.exit("no API key given")
    return key.strip()


def cmd_discover(settings: Settings, args) -> None:
    asyncio.run(_discover(settings, args))


async def _dry_run(settings: Settings, args) -> None:
    """Poll once with real WarGear data, print what Joshua would say, post nothing."""
    from joshua.crypto import KeyBox
    from joshua.db import Store, now_ts
    from joshua.notifier import Notifier, PrintPoster
    from joshua.poller import Poller
    from joshua.slack import messages as m
    from joshua.slack.commands import Deps, game_rows
    from joshua.wargear.client import WarGearClient
    from joshua.wargear.ratelimit import RateGate

    key = _api_key()
    from cryptography.fernet import Fernet

    keybox = KeyBox(Fernet.generate_key().decode())
    store = await Store.open(":memory:")
    await store.link("UDRYRUN", args.me or "dry-run")
    await store.set_api_key("UDRYRUN", keybox.seal(key), None)
    wg = WarGearClient(
        settings.wargear_base_url, settings.wargear_api_path, RateGate(settings.wargear_min_interval)
    )
    poller = Poller(store, wg, keybox, settings)
    events = await poller.poll_once()
    for gid in await store.live_gameids():
        await store.set_tracked(gid, True)
    print(f"{len(events)} events, {wg.request_count} WarGear request(s)\n")
    for e in events:
        print("  event:", e)
    notifier = Notifier(store, PrintPoster(), settings)
    deps = Deps(store, wg, keybox, poller, notifier, settings)
    text, blocks = m.games_overview(await game_rows(deps), now_ts())
    print("\n/wargear games →")
    for b in blocks:
        if b.get("type") == "section":
            print("  " + b["text"]["text"].replace("\n", "\n  "))
    print("\nReminders due right now:")
    await notifier.tick()
    print("\nUpcoming schedule:")
    for turn in await store.open_turns():
        for r in notifier.plan_for(turn):
            if r.when.timestamp() > now_ts():
                print(f"  {turn.game_name} / {turn.player}: {r.kind:9} {r.when.astimezone():%a %H:%M %Z}")
    await wg.aclose()
    await store.close()


def cmd_dry_run(settings: Settings, args) -> None:
    asyncio.run(_dry_run(settings, args))


def main() -> None:
    parser = argparse.ArgumentParser(prog="joshua", description="SHALL WE PLAY A GAME?")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run", help="run the bot")
    sub.add_parser("genkey", help="print a new JOSHUA_SECRET_KEY")
    sub.add_parser("healthcheck", help="exit non-zero if unhealthy")
    d = sub.add_parser("discover", help="dump raw WarGear API responses (Phase 0)")
    d.add_argument("--player", help="also try GetGameList/player for this name")
    d.add_argument("--game", type=int, help="also fetch GetHistoryUpdate for this game")
    d.add_argument("--out", default="tests/fixtures/raw")
    r = sub.add_parser("dry-run", help="poll once and print what would be posted")
    r.add_argument("--me", help="your WarGear name")
    args = parser.parse_args()

    # Anything we create (the DB with encrypted keys, its WAL files) is owner-only.
    os.umask(0o077)
    settings = Settings()
    _setup_logging(settings.log_level)
    {
        "run": cmd_run,
        "genkey": cmd_genkey,
        "healthcheck": cmd_healthcheck,
        "discover": cmd_discover,
        "dry-run": cmd_dry_run,
    }[args.cmd](settings, args)


if __name__ == "__main__":
    main()
