"""SQLite storage. One connection, owned by the running process."""

from __future__ import annotations

import json
import time
import zlib
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

import aiosqlite

from joshua.wargear.models import BoardInfo, GameState, PlayerState, is_fogged


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

    @property
    def has_key(self) -> bool:
        """Has a key WarGear hasn't rejected (untested counts)."""
        return bool(self.enc_api_key) and self.key_ok != 0


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
    has_key: bool = True


@dataclass(frozen=True)
class Record:
    """One player's results in finished, tracked games. Terminated games count as draws."""

    name: str
    wins: int = 0
    losses: int = 0
    draws: int = 0

    @property
    def played(self) -> int:
        return self.wins + self.losses + self.draws


@dataclass(frozen=True)
class PendingReport:
    gameid: int
    body: bytes


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
            boardid=g["boardid"],
            fog=g["fog"],
        )

    async def save_game(self, s: GameState, seen_via: str | None) -> None:
        t = now_ts()
        await self.db.execute(
            """INSERT INTO games(gameid, name, board, host, status, gametype, num_players, turnstamp,
                                 boot_time, createstamp, startstamp, endstamp, finished, winners,
                                 first_seen_at, last_seen_at, seen_via, boardid, fog)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(gameid) DO UPDATE SET
                 name = excluded.name, board = excluded.board, host = excluded.host,
                 status = excluded.status, gametype = excluded.gametype,
                 num_players = excluded.num_players,
                 turnstamp = excluded.turnstamp, boot_time = excluded.boot_time,
                 createstamp = excluded.createstamp, startstamp = excluded.startstamp,
                 endstamp = excluded.endstamp, finished = excluded.finished,
                 winners = excluded.winners, last_seen_at = excluded.last_seen_at,
                 seen_via = excluded.seen_via,
                 boardid = COALESCE(excluded.boardid, games.boardid),
                 fog = COALESCE(excluded.fog, games.fog)""",
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
                s.boardid,
                s.fog,
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
                       p.slack_id, p.tz, p.loud_hours,
                       COALESCE(p.enc_api_key IS NOT NULL AND COALESCE(p.key_ok, 1) != 0, 1) AS has_key
                FROM turns t JOIN games g ON g.gameid = t.gameid
                LEFT JOIN players p ON p.wargear_name = t.player
                WHERE t.ended_at IS NULL AND g.finished = 0 {where}
                ORDER BY t.deadline"""
        )
        return [OpenTurn(**{**dict(r), "has_key": bool(r["has_key"])}) for r in await cur.fetchall()]

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

    # --- fog -------------------------------------------------------------

    async def fogged(self, gameid: int) -> bool:
        """Is this game fogged? Resolves "Board Default" from the board's rules when we have them."""
        cur = await self.db.execute(
            "SELECT g.fog, b.fog FROM games g LEFT JOIN boards b ON b.boardid = g.boardid WHERE g.gameid = ?",
            (gameid,),
        )
        row = await cur.fetchone()
        return is_fogged(row[0], row[1]) if row else True

    # --- boards ----------------------------------------------------------

    async def save_board(self, b: BoardInfo) -> None:
        await self.db.execute(
            """INSERT OR REPLACE INTO boards(boardid, name, fog, attacker_sides, defender_sides, territories,
                                             fetched_at) VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                b.boardid,
                b.name,
                b.fog,
                b.attacker_sides,
                b.defender_sides,
                json.dumps({str(k): v for k, v in b.territories.items()}),
                now_ts(),
            ),
        )
        await self.db.commit()

    async def board(self, boardid: int | None) -> BoardInfo | None:
        if boardid is None:
            return None
        cur = await self.db.execute("SELECT * FROM boards WHERE boardid = ?", (boardid,))
        r = await cur.fetchone()
        if r is None:
            return None
        return BoardInfo(
            r["boardid"],
            r["name"],
            r["fog"],
            r["attacker_sides"] or 6,
            r["defender_sides"] or 6,
            {int(k): v for k, v in json.loads(r["territories"]).items()},
        )

    async def boards_needed(self) -> list[int]:
        """Boards of tracked live games and of games waiting on a history fetch, not fetched yet."""
        cur = await self.db.execute(
            f"""SELECT DISTINCT g.boardid FROM games g
                LEFT JOIN game_history h ON h.gameid = g.gameid
                WHERE g.boardid IS NOT NULL
                  AND g.boardid NOT IN (SELECT boardid FROM boards)
                  AND ((g.finished = 0 AND COALESCE(g.status, '') != 'Open' AND ({self._TRACKED}))
                       OR (h.gameid IS NOT NULL AND h.body IS NULL))
                ORDER BY g.boardid"""
        )
        return [r[0] for r in await cur.fetchall()]

    # --- history and after-action reports --------------------------------

    async def queue_history(self, gameid: int, at: int) -> None:
        await self.db.execute(
            "INSERT OR IGNORE INTO game_history(gameid, queued_at) VALUES (?, ?)", (gameid, at)
        )
        await self.db.commit()

    async def history_pending(
        self, tried_before: int, max_attempts: int
    ) -> list[tuple[int, int | None, str | None]]:
        """(gameid, boardid, seen_via) for histories still to fetch, oldest first."""
        cur = await self.db.execute(
            """SELECT h.gameid, g.boardid, g.seen_via FROM game_history h JOIN games g USING (gameid)
               WHERE h.body IS NULL AND h.attempts < ?
                 AND (h.last_attempt_at IS NULL OR h.last_attempt_at < ?)
               ORDER BY h.queued_at""",
            (max_attempts, tried_before),
        )
        return [(r[0], r[1], r[2]) for r in await cur.fetchall()]

    async def history_attempted(self, gameid: int, at: int) -> None:
        await self.db.execute(
            "UPDATE game_history SET attempts = attempts + 1, last_attempt_at = ? WHERE gameid = ?",
            (at, gameid),
        )
        await self.db.commit()

    async def save_history(self, gameid: int, payload: object, at: int) -> None:
        body = zlib.compress(json.dumps(payload, separators=(",", ":")).encode())
        await self.db.execute(
            "UPDATE game_history SET body = ?, fetched_at = ? WHERE gameid = ?", (body, at, gameid)
        )
        await self.db.commit()

    async def reports_due(self) -> list[PendingReport]:
        """Fetched histories not reported yet, once the game's first winner nag has gone out."""
        cur = await self.db.execute(
            """SELECT h.gameid, h.body FROM game_history h
               WHERE h.body IS NOT NULL AND h.report_posted_at IS NULL
                 AND NOT EXISTS (SELECT 1 FROM winner_nags w
                                 WHERE w.gameid = h.gameid AND w.kind = 'first' AND w.sent_at IS NULL)
               ORDER BY h.fetched_at"""
        )
        return [PendingReport(r[0], r[1]) for r in await cur.fetchall()]

    async def finish_report(self, gameid: int, outcome: str) -> None:
        await self.db.execute(
            "UPDATE game_history SET report_posted_at = ?, report_outcome = ? WHERE gameid = ?",
            (now_ts(), outcome, gameid),
        )
        await self.db.commit()

    # --- records ---------------------------------------------------------

    async def records(self, board: str | None = None) -> list[Record]:
        """Win/loss/draw per player over finished tracked games, optionally for one board.

        A game counts once it has winners (team wins count for every member) or was terminated
        (a draw). Games closed out as "Gone" with no winners don't count.
        """
        cur = await self.db.execute(
            f"""SELECT g.gameid, g.status, g.winners, gp.wargear_name FROM games g
                JOIN game_players gp ON gp.gameid = g.gameid
                WHERE g.finished = 1 AND ({self._TRACKED})
                  AND (? IS NULL OR g.board = ? COLLATE NOCASE)""",
            (board, board),
        )
        tally: dict[str, list[int]] = {}
        display: dict[str, str] = {}
        for _gameid, status, winners, name in await cur.fetchall():
            won = {w.lower() for w in json.loads(winners)}
            if "terminat" in (status or "").lower():
                col = 2
            elif won:
                col = 0 if name.lower() in won else 1
            else:
                continue
            display.setdefault(name.lower(), name)
            tally.setdefault(name.lower(), [0, 0, 0])[col] += 1
        records = [Record(display[k], *v) for k, v in tally.items()]
        return sorted(records, key=lambda r: (-r.wins, r.losses, r.name.lower()))

    async def boards_played(self) -> list[str]:
        cur = await self.db.execute(
            f"""SELECT DISTINCT g.board FROM games g
                WHERE g.finished = 1 AND g.board IS NOT NULL AND ({self._TRACKED}) ORDER BY g.board"""
        )
        return [r[0] for r in await cur.fetchall()]
