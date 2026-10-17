"""joshua: run | dry-run | discover | healthcheck | genkey | version | changelog"""

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
    if args.paging:
        calls = [
            ("won_p1", "/GetGameList/my", {"viewselector": "Games I Won"}),
            ("won_p2", "/GetGameList/my", {"viewselector": "Games I Won", "pagenumber": 2}),
        ]
        if args.host:
            calls.append(("host_p1", "/GetGameList/host", {"host": args.host}))
            calls.append(("host_p2", "/GetGameList/host", {"host": args.host, "pagenumber": 2}))
    base = settings.wargear_base_url + settings.wargear_api_path
    async with httpx.AsyncClient(base_url=base, headers={"User-Agent": USER_AGENT}, timeout=30) as http:
        if args.paging:
            await _probe_finished_pages(http, key, out, args.find)
        for name, path, params in calls:
            try:
                resp = await http.get(path, params={**params, "api_key": key})
            except httpx.HTTPError as e:
                print(f"== {name}: {type(e).__name__}; skipping")
                continue
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
            body = None
            try:
                body = resp.json()
                (out / f"{name}.json").write_text(json.dumps(body, indent=2))
            except ValueError:
                (out / f"{name}.txt").write_text(resp.text)
            if args.paging and isinstance(body, list):
                print("   " + _page_summary(body, args.find))
            await asyncio.sleep(3)  # be polite even while exploring
    print(f"saved to {out}/ (gitignored; sanitize before copying into tests/fixtures)")


def _page_summary(games: list, find: int | None) -> str:
    """Count, id range and end-date range of one page of games: no names."""
    from datetime import UTC, datetime

    if not games:
        return "empty"
    ids = [int(g["gameid"]) for g in games]
    ends = sorted(int(g.get("endstamp") or 0) for g in games)

    def day(ts: int) -> str:
        return f"{datetime.fromtimestamp(ts, UTC):%Y-%m-%d}" if ts else "-"

    found = f"  ** has {find} **" if find in ids else ""
    return f"{len(games)} games, ids {min(ids)}-{max(ids)}, ended {day(ends[0])} to {day(ends[-1])}{found}"


async def _probe_finished_pages(http, key: str, out: Path, find: int | None, max_pages: int = 30) -> None:
    """Walk the Finished list until a page is empty or repeats, to learn page size and order."""
    previous: set[int] = set()
    for page in range(1, max_pages + 1):
        resp = await http.get(
            "/GetGameList/my", params={"viewselector": "Finished", "pagenumber": page, "api_key": key}
        )
        try:
            body = resp.json()
        except ValueError:
            body = resp.text[:200]
        if not isinstance(body, list):
            print(f"== finished page {page}: HTTP {resp.status_code}, not a list: {body}; stopping")
            return
        (out / f"finished_p{page}.json").write_text(json.dumps(body, indent=2))
        ids = {int(g["gameid"]) for g in body}
        print(f"== finished page {page}: {_page_summary(body, find)}")
        if not ids or ids == previous:
            print("   (empty or same as the previous page; stopping)")
            break
        previous = ids
        await asyncio.sleep(3)
    else:
        print(f"   stopped after {max_pages} pages")
    # wargear/finished.py relies on a far-off page returning the last page.
    await asyncio.sleep(3)
    resp = await http.get(
        "/GetGameList/my", params={"viewselector": "Finished", "pagenumber": 9999, "api_key": key}
    )
    body = resp.json()
    same = isinstance(body, list) and {int(g["gameid"]) for g in body} == previous
    print(f"== finished page 9999: {_page_summary(body, find) if isinstance(body, list) else body}")
    verdict = "same as the last page: good" if same else "NOT the last page: rethink wargear/finished.py"
    print(f"   {verdict}")


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
    if args.report:
        await _print_report(store, wg, key, args.report, settings.game_url(args.report))
    await wg.aclose()
    await store.close()


async def _print_report(store, wg, key: str, gameid: int, url: str) -> None:
    """The after-action report for one finished game, as /wargear report shows it."""
    from joshua import reports

    try:
        text, blocks = await reports.on_demand(store, wg, key, gameid, url)
    except reports.NoReport as e:
        print(f"\n{e.text}")
        return
    print(f"\nAfter-action report ({wg.request_count} WarGear requests in total) →\n  {text}")
    for b in blocks:
        body = b["text"]["text"] if b["type"] == "section" else b["elements"][0]["text"]
        print("  " + body.replace("\n", "\n  "))


def cmd_dry_run(settings: Settings, args) -> None:
    asyncio.run(_dry_run(settings, args))


def cmd_version(settings: Settings, args) -> None:
    from joshua import release

    r = release.current()
    print(f"joshua build {r.build} ({r.date}, {release.commit() or 'unknown commit'})")


def cmd_changelog(settings: Settings, args) -> None:
    from joshua import release

    r = release.find(args.build) if args.build else release.current()
    if r is None:
        sys.exit(f"No build {args.build}. Builds: {', '.join(str(x.build) for x in release.releases())}")
    print(f"Build {r.build} · {r.date}" + (f" · {r.commit}" if r.commit else "") + "\n" + r.notes)


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
    d.add_argument(
        "--paging",
        action="store_true",
        help="instead: walk every Finished page, and try 'Games I Won' and --host, to learn paging",
    )
    d.add_argument("--host", help="with --paging: also try GetGameList/host for this name")
    d.add_argument("--find", type=int, help="with --paging: say which page has this game id")
    r = sub.add_parser("dry-run", help="poll once and print what would be posted")
    r.add_argument("--me", help="your WarGear name")
    r.add_argument("--report", type=int, help="also print the after-action report for this finished game")
    sub.add_parser("version", help="print the build number and commit")
    c = sub.add_parser("changelog", help="print the changelog for this build, or another one")
    c.add_argument("build", type=int, nargs="?")
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
        "version": cmd_version,
        "changelog": cmd_changelog,
    }[args.cmd](settings, args)


if __name__ == "__main__":
    main()
