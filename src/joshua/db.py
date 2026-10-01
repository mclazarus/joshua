"""SQLite storage. One connection, owned by the running process."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

import aiosqlite

from joshua.wargear.models import GameState, PlayerState


def now_ts() -> int:
    return int(time.time())


@dataclass(frozen=True)
class PlayerRow:
    slack_id: str
    wargear_name: str | None
    tz: str | None
    loud_hours: str | None
    enc_api_key: bytes | None
    key_ok: int | None


@dataclass(frozen=True)
class OpenTurn:
    gameid: int
    game_name: str
    player: str
    turnstamp: int
    started_at: int
    deadline: int | None
    slack_id: str | None
    tz: str | None
    loud_hours: str | None


def _prepare_db_file(path: Path) -> None:
    """Create the DB owner-only (it holds encrypted API keys). Runs once at startup."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch(mode=0o600, exist_ok=True)
    path.chmod(0o600)


class NameTaken(Exception):
    def __init__(self, slack_id: str):
        super().__init__(slack_id)
        self.slack_id = slack_id


class Store:
    def __init__(self, conn: aiosqlite.Connection):
        self.db = conn

    @classmethod
    async def open(cls, path: str) -> Store:
        if path != ":memory:":
            _prepare_db_file(Path(path))
        conn = await aiosqlite.connect(path)
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA foreign_keys = ON")
        await conn.execute("PRAGMA journal_mode = WAL")
        store = cls(conn)
        await store.migrate()
        return store

    async def close(self) -> None:
        await self.db.close()

    async def migrate(self) -> None:
        cur = await self.db.execute("PRAGMA user_version")
        (version,) = await cur.fetchone()
        scripts = sorted(p for p in resources.files("joshua.migrations").iterdir() if p.name.endswith(".sql"))
        for script in scripts:
            n = int(script.name.split("_", 1)[0])
            if n > version:
                await self.db.executescript(script.read_text())
                await self.db.execute(f"PRAGMA user_version = {n}")
        await self.db.commit()

    # --- key/value -------------------------------------------------------

    async def kv_get(self, key: str) -> str | None:
        cur = await self.db.execute("SELECT value FROM kv WHERE key = ?", (key,))
        row = await cur.fetchone()
        return row[0] if row else None

    async def kv_set(self, key: str, value: str) -> None:
        await self.db.execute(
            "INSERT INTO kv(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        await self.db.commit()

    # --- players ---------------------------------------------------------

    @staticmethod
    def _player(row: aiosqlite.Row | None) -> PlayerRow | None:
        if row is None:
            return None
        return PlayerRow(
            row["slack_id"],
            row["wargear_name"],
            row["tz"],
            row["loud_hours"],
            row["enc_api_key"],
            row["key_ok"],
        )

    async def player(self, slack_id: str) -> PlayerRow | None:
        cur = await self.db.execute("SELECT * FROM players WHERE slack_id = ?", (slack_id,))
        return self._player(await cur.fetchone())

    async def player_by_name(self, wargear_name: str) -> PlayerRow | None:
        cur = await self.db.execute("SELECT * FROM players WHERE wargear_name = ?", (wargear_name,))
        return self._player(await cur.fetchone())

    async def players(self) -> list[PlayerRow]:
        cur = await self.db.execute("SELECT * FROM players ORDER BY wargear_name")
        return [self._player(r) for r in await cur.fetchall()]

    async def link(self, slack_id: str, wargear_name: str, *, force: bool = False) -> None:
        """Map a Slack user to a WarGear name. ``force`` steals the name from whoever had it."""
        existing = await self.player_by_name(wargear_name)
        if existing and existing.slack_id != slack_id:
            if not force:
                raise NameTaken(existing.slack_id)
            await self.db.execute(
                "UPDATE players SET wargear_name = NULL, updated_at = ? WHERE slack_id = ?",
                (now_ts(), existing.slack_id),
            )
        t = now_ts()
        await self.db.execute(
            """INSERT INTO players(slack_id, wargear_name, created_at, updated_at) VALUES (?, ?, ?, ?)
               ON CONFLICT(slack_id) DO UPDATE SET wargear_name = excluded.wargear_name,
                                                   updated_at = excluded.updated_at""",
            (slack_id, wargear_name, t, t),
        )
        await self.db.commit()

    async def unlink(self, slack_id: str) -> None:
        await self.db.execute("DELETE FROM players WHERE slack_id = ?", (slack_id,))
        await self.db.commit()

    async def set_api_key(self, slack_id: str, sealed: bytes | None, ok: bool | None) -> None:
        await self.db.execute(
            "UPDATE players SET enc_api_key = ?, key_ok = ?, updated_at = ? WHERE slack_id = ?",
            (sealed, None if ok is None else int(ok), now_ts(), slack_id),
        )
        await self.db.commit()

    async def set_key_ok(self, slack_id: str, ok: bool) -> None:
        await self.db.execute("UPDATE players SET key_ok = ? WHERE slack_id = ?", (int(ok), slack_id))
        await self.db.commit()

    async def set_tz(self, slack_id: str, tz: str | None) -> None:
        await self.db.execute(
            "UPDATE players SET tz = ?, tz_checked_at = ? WHERE slack_id = ?", (tz, now_ts(), slack_id)
        )
        await self.db.commit()

    async def stale_tz(self, older_than: int) -> list[str]:
        cur = await self.db.execute(
            "SELECT slack_id FROM players WHERE tz_checked_at IS NULL OR tz_checked_at < ?",
            (older_than,),
        )
        return [r[0] for r in await cur.fetchall()]

    async def set_loud_hours(self, slack_id: str, spec: str | None) -> None:
        await self.db.execute(
            "UPDATE players SET loud_hours = ?, updated_at = ? WHERE slack_id = ?",
            (spec, now_ts(), slack_id),
        )
        await self.db.commit()

    # --- games -----------------------------------------------------------

    async def game_state(self, gameid: int) -> GameState | None:
        cur = await self.db.execute("SELECT * FROM games WHERE gameid = ?", (gameid,))
        g = await cur.fetchone()
        if g is None:
            return None
        cur = await self.db.execute("SELECT * FROM game_players WHERE gameid = ? ORDER BY seat", (gameid,))
        prows = await cur.fetchall()
        cur = await self.db.execute(
            "SELECT player FROM turns WHERE gameid = ? AND ended_at IS NULL", (gameid,)
        )
        current = frozenset(r[0] for r in await cur.fetchall())
        return GameState(
            gameid=g["gameid"],
            name=g["name"],
            board=g["board"],
            host=g["host"],
            status=g["status"],
            gametype=g["gametype"],
            seats=g["num_players"],
            players=tuple(
                PlayerState(p["wargear_name"], p["wargear_id"], p["seat"], p["status"], p["turn_counter"])
                for p in prows
            ),
            current=current,
            winners=tuple(json.loads(g["winners"])),
            eliminated=frozenset(p["wargear_name"] for p in prows if p["eliminated"]),
            turnstamp=g["turnstamp"],
            boot_time=g["boot_time"],
            createstamp=g["createstamp"],
            startstamp=g["startstamp"],
            endstamp=g["endstamp"],
        )

    async def save_game(self, s: GameState, seen_via: str | None) -> None:
        t = now_ts()
        await self.db.execute(
            """INSERT INTO games(gameid, name, board, host, status, gametype, num_players, turnstamp,
                                 boot_time, createstamp, startstamp, endstamp, finished, winners,
                                 first_seen_at, last_seen_at, seen_via)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(gameid) DO UPDATE SET
                 name = excluded.name, board = excluded.board, host = excluded.host,
                 status = excluded.status, gametype = excluded.gametype,
                 num_players = excluded.num_players,
                 turnstamp = excluded.turnstamp, boot_time = excluded.boot_time,
                 createstamp = excluded.createstamp, startstamp = excluded.startstamp,
                 endstamp = excluded.endstamp, finished = excluded.finished,
                 winners = excluded.winners, last_seen_at = excluded.last_seen_at,
                 seen_via = excluded.seen_via""",
            (
                s.gameid,
                s.name,
                s.board,
                s.host,
                s.status,
                s.gametype,
                s.seats,
                s.turnstamp,
                s.boot_time,
                s.createstamp,
                s.startstamp,
                s.endstamp,
                int(s.finished),
                json.dumps(list(s.winners)),
                t,
                t,
                seen_via,
            ),
        )
        await self.db.execute("DELETE FROM game_players WHERE gameid = ?", (s.gameid,))
        await self.db.executemany(
            """INSERT OR REPLACE INTO game_players(gameid, wargear_name, wargear_id, seat, status,
                                                    turn_counter, eliminated)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            [
                (s.gameid, p.name, p.id, p.seat, p.status, p.turn_counter, int(p.name in s.eliminated))
                for p in s.players
            ],
        )
        await self.db.commit()

    async def set_tracked(self, gameid: int, tracked: bool | None) -> bool:
        cur = await self.db.execute(
            "UPDATE games SET tracked = ? WHERE gameid = ?",
            (None if tracked is None else int(tracked), gameid),
        )
        await self.db.commit()
        return cur.rowcount > 0

    _TRACKED = """
        g.tracked = 1 OR (g.tracked IS NULL AND (
            SELECT COUNT(*) FROM game_players gp JOIN players p ON p.wargear_name = gp.wargear_name
            WHERE gp.gameid = g.gameid) >= 2)
    """

    async def tracked_games(self, *, live: bool = True) -> list[GameState]:
        cur = await self.db.execute(
            f"SELECT gameid FROM games g WHERE ({self._TRACKED}) AND finished = ? ORDER BY gameid",
            (0 if live else 1,),
        )
        return [s for (gid,) in await cur.fetchall() if (s := await self.game_state(gid))]

    async def is_tracked(self, gameid: int) -> bool:
        cur = await self.db.execute(
            f"SELECT 1 FROM games g WHERE g.gameid = ? AND ({self._TRACKED})", (gameid,)
        )
        return await cur.fetchone() is not None

    async def live_gameids(self) -> set[int]:
        cur = await self.db.execute("SELECT gameid FROM games WHERE finished = 0")
        return {r[0] for r in await cur.fetchall()}

    async def games_for_player(self, wargear_name: str) -> set[int]:
        cur = await self.db.execute(
            "SELECT gp.gameid FROM game_players gp JOIN games g USING (gameid) "
            "WHERE gp.wargear_name = ? AND g.finished = 0",
            (wargear_name,),
        )
        return {r[0] for r in await cur.fetchall()}

    async def signup_games(self, created_after: int) -> list[tuple[GameState, int | None]]:
        """Tracked open games created recently, with when we last nagged about them."""
        cur = await self.db.execute(
            f"""SELECT gameid, signup_nagged_at FROM games g
                WHERE status = 'Open' AND finished = 0 AND createstamp >= ? AND ({self._TRACKED})
                ORDER BY createstamp""",
            (created_after,),
        )
        rows = await cur.fetchall()
        return [(s, nagged) for gid, nagged in rows if (s := await self.game_state(gid))]

    async def set_signup_nagged(self, gameid: int, at: int) -> None:
        await self.db.execute("UPDATE games SET signup_nagged_at = ? WHERE gameid = ?", (at, gameid))
        await self.db.commit()

    async def games_hosted_since(self, host: str, since: int) -> list[int]:
        cur = await self.db.execute(
            "SELECT gameid FROM games WHERE host = ? AND COALESCE(createstamp, first_seen_at) >= ?",
            (host, since),
        )
        return [r[0] for r in await cur.fetchall()]

    async def unmapped_names(self) -> list[tuple[str, int]]:
        """WarGear names in tracked live games that no Slack user has claimed."""
        cur = await self.db.execute(
            f"""SELECT gp.wargear_name, MIN(gp.gameid) FROM game_players gp
                JOIN games g ON g.gameid = gp.gameid
                WHERE g.finished = 0 AND ({self._TRACKED})
                  AND gp.wargear_name NOT IN (SELECT wargear_name FROM players WHERE wargear_name IS NOT NULL)
                  AND gp.wargear_name NOT IN (SELECT wargear_name FROM identity_prompts)
                GROUP BY gp.wargear_name"""
        )
        return [(r[0], r[1]) for r in await cur.fetchall()]

    # --- turns -----------------------------------------------------------

    async def start_turn(
        self, gameid: int, player: str, turnstamp: int, started_at: int, deadline: int | None
    ) -> None:
        await self.db.execute(
            """INSERT INTO turns(gameid, player, turnstamp, started_at, deadline) VALUES (?, ?, ?, ?, ?)
               ON CONFLICT DO UPDATE SET deadline = excluded.deadline, ended_at = NULL""",
            (gameid, player, turnstamp, started_at, deadline),
        )
        await self.db.commit()

    async def end_turn(self, gameid: int, player: str, at: int) -> None:
        await self.db.execute(
            "UPDATE turns SET ended_at = ? WHERE gameid = ? AND player = ? AND ended_at IS NULL",
            (at, gameid, player),
        )
        await self.db.commit()

    async def open_turn_for(self, gameid: int, player: str) -> tuple[int, int | None] | None:
        cur = await self.db.execute(
            "SELECT turnstamp, deadline FROM turns WHERE gameid = ? AND player = ? AND ended_at IS NULL",
            (gameid, player),
        )
        row = await cur.fetchone()
        return (row[0], row[1]) if row else None

    async def open_turns(self, *, tracked_only: bool = True) -> list[OpenTurn]:
        where = f"AND ({self._TRACKED})" if tracked_only else ""
        cur = await self.db.execute(
            f"""SELECT t.gameid, g.name AS game_name, t.player, t.turnstamp, t.started_at, t.deadline,
                       p.slack_id, p.tz, p.loud_hours
                FROM turns t JOIN games g ON g.gameid = t.gameid
                LEFT JOIN players p ON p.wargear_name = t.player
                WHERE t.ended_at IS NULL AND g.finished = 0 {where}
                ORDER BY t.deadline"""
        )
        return [OpenTurn(**dict(r)) for r in await cur.fetchall()]

    # --- reminders -------------------------------------------------------

    async def reminders_sent(self, gameid: int, player: str, turnstamp: int) -> set[tuple[str, int]]:
        cur = await self.db.execute(
            "SELECT kind, due_at FROM reminders_sent WHERE gameid = ? AND player = ? AND turnstamp = ?",
            (gameid, player, turnstamp),
        )
        return {(r[0], r[1]) for r in await cur.fetchall()}

    async def record_reminder(
        self, gameid: int, player: str, turnstamp: int, kind: str, due_at: int, posted: bool
    ) -> None:
        await self.db.execute(
            "INSERT OR IGNORE INTO reminders_sent VALUES (?, ?, ?, ?, ?, ?, ?)",
            (gameid, player, turnstamp, kind, due_at, now_ts(), int(posted)),
        )
        await self.db.commit()

    # --- winner nags -----------------------------------------------------

    async def schedule_winner_nag(self, gameid: int, winner: str, kind: str, due_at: int) -> None:
        await self.db.execute(
            "INSERT OR IGNORE INTO winner_nags(gameid, winner, kind, due_at) VALUES (?, ?, ?, ?)",
            (gameid, winner, kind, due_at),
        )
        await self.db.commit()

    async def due_winner_nags(self, now: int) -> list[aiosqlite.Row]:
        cur = await self.db.execute(
            "SELECT * FROM winner_nags WHERE sent_at IS NULL AND due_at <= ? ORDER BY due_at", (now,)
        )
        return list(await cur.fetchall())

    async def finish_winner_nag(self, gameid: int, winner: str, kind: str, outcome: str) -> None:
        await self.db.execute(
            "UPDATE winner_nags SET sent_at = ?, outcome = ? WHERE gameid = ? AND winner = ? AND kind = ?",
            (now_ts(), outcome, gameid, winner, kind),
        )
        await self.db.commit()

    # --- identity prompts ------------------------------------------------

    async def record_identity_prompt(self, name: str, channel: str | None, ts: str | None) -> None:
        await self.db.execute(
            "INSERT OR REPLACE INTO identity_prompts VALUES (?, ?, ?, ?)", (name, now_ts(), channel, ts)
        )
        await self.db.commit()
