-- Fog level and board id per game, so eliminations in fogged games stay quiet
-- and stats can be grouped per board.
ALTER TABLE games ADD COLUMN boardid INTEGER;
ALTER TABLE games ADD COLUMN fog TEXT;       -- "None", "Light", "Medium", "Heavy", "Board Default"…

-- Board rules and territory names, fetched once per board.
CREATE TABLE boards (
    boardid        INTEGER PRIMARY KEY,
    name           TEXT,
    fog            TEXT,
    attacker_sides INTEGER,
    defender_sides INTEGER,
    territories    TEXT NOT NULL DEFAULT '{}',   -- JSON {tid: name}
    fetched_at     INTEGER NOT NULL
);

-- GetHistoryUpdate for finished tracked games, fetched once for the after-action report.
CREATE TABLE game_history (
    gameid           INTEGER PRIMARY KEY REFERENCES games(gameid) ON DELETE CASCADE,
    queued_at        INTEGER NOT NULL,
    attempts         INTEGER NOT NULL DEFAULT 0,
    last_attempt_at  INTEGER,
    fetched_at       INTEGER,
    body             BLOB,            -- zlib-compressed JSON, as WarGear returned it
    report_posted_at INTEGER,
    report_outcome   TEXT             -- 'posted', or why it was skipped
);
