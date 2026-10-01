-- Slack users we know about, and the WarGear name they play as.
CREATE TABLE players (
    slack_id      TEXT PRIMARY KEY,
    wargear_name  TEXT UNIQUE COLLATE NOCASE,
    tz            TEXT,
    loud_hours    TEXT,           -- "08:00-22:00"; NULL uses the default
    enc_api_key   BLOB,           -- Fernet-encrypted WarGear API key
    key_ok        INTEGER,        -- 1 good, 0 rejected by WarGear, NULL untested
    tz_checked_at INTEGER,
    created_at    INTEGER NOT NULL,
    updated_at    INTEGER NOT NULL
);

CREATE TABLE games (
    gameid        INTEGER PRIMARY KEY,
    name          TEXT NOT NULL,
    board         TEXT,
    host          TEXT,
    status        TEXT,
    gametype      TEXT,
    num_players   INTEGER,        -- seat cap; open games fill first-come first-served
    turnstamp     INTEGER,
    boot_time     INTEGER,
    createstamp   INTEGER,
    startstamp    INTEGER,
    endstamp      INTEGER,
    finished      INTEGER NOT NULL DEFAULT 0,
    winners       TEXT NOT NULL DEFAULT '[]',   -- JSON list of WarGear names
    tracked       INTEGER,        -- NULL automatic, 1 forced on, 0 forced off
    first_seen_at INTEGER NOT NULL,
    last_seen_at  INTEGER NOT NULL,
    seen_via      TEXT,           -- slack_id whose key last returned this game
    signup_nagged_at INTEGER      -- last "still needs players" post for an open game
);

CREATE TABLE game_players (
    gameid        INTEGER NOT NULL REFERENCES games(gameid) ON DELETE CASCADE,
    wargear_name  TEXT NOT NULL COLLATE NOCASE,
    wargear_id    TEXT,
    seat          INTEGER,
    status        TEXT,
    turn_counter  INTEGER,
    eliminated    INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (gameid, wargear_name)
);

-- One row per player per turn. A turn is open while ended_at IS NULL.
CREATE TABLE turns (
    gameid        INTEGER NOT NULL REFERENCES games(gameid) ON DELETE CASCADE,
    player        TEXT NOT NULL COLLATE NOCASE,
    turnstamp     INTEGER NOT NULL,
    started_at    INTEGER NOT NULL,   -- when the turn began (turnstamp, or when first seen)
    deadline      INTEGER,            -- when WarGear will skip them
    ended_at      INTEGER,
    PRIMARY KEY (gameid, player, turnstamp)
);
CREATE INDEX turns_open ON turns(ended_at) WHERE ended_at IS NULL;

CREATE TABLE reminders_sent (
    gameid        INTEGER NOT NULL,
    player        TEXT NOT NULL COLLATE NOCASE,
    turnstamp     INTEGER NOT NULL,
    kind          TEXT NOT NULL,
    due_at        INTEGER NOT NULL,
    sent_at       INTEGER NOT NULL,
    posted        INTEGER NOT NULL,   -- 0 if skipped as catch-up backlog
    PRIMARY KEY (gameid, player, turnstamp, kind, due_at)
);

CREATE TABLE winner_nags (
    gameid        INTEGER NOT NULL,
    winner        TEXT NOT NULL COLLATE NOCASE,
    kind          TEXT NOT NULL,      -- 'first' or 'followup'
    due_at        INTEGER NOT NULL,
    sent_at       INTEGER,
    outcome       TEXT,               -- 'posted', or why it was skipped
    PRIMARY KEY (gameid, winner, kind)
);

-- "Who is Foo on WarGear?" prompts, so each name is only asked about once.
CREATE TABLE identity_prompts (
    wargear_name  TEXT PRIMARY KEY COLLATE NOCASE,
    posted_at     INTEGER NOT NULL,
    channel       TEXT,
    message_ts    TEXT
);

CREATE TABLE kv (
    key           TEXT PRIMARY KEY,
    value         TEXT NOT NULL
);
