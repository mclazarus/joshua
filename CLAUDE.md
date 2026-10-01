# Joshua

A Slack bot themed on *WarGames* (1983). It watches our group's WarGear (wargear.net, a RISK-like
game) games and nags people in one shared channel:
- when it's their turn;
- when they're about to be skipped;
- when they've won and should start the next game;
- when they've been invited to a sign-up game and haven't joined.

Start with README.md and **docs/wargear-api.md**. The API doc records what the real WarGear API
returns, which differs from its own Swagger spec.

## Commands
- `uv run pytest -q`, `make lint`, `make fmt`. Ruff is set to line length 110.
- `uv run joshua dry-run --me <wargear name>`: polls once against real WarGear, prints what it
  would post, posts nothing. It prompts for the API key.
- `uv run joshua discover`: dumps raw API responses into `tests/fixtures/raw/` (gitignored). Then
  `uv run python tests/fixtures/sanitize.py` rebuilds the committed fixtures, with real
  usernames/ids swapped for WarGames characters and fake hashes.
- `make dev-up` / `make dev-logs` / `make dev-down`: run the image locally against a sandbox Slack
  workspace, using `.env` and `./data`.
- uv is pinned to managed Python (`python-preference = "only-managed"`) because the system Pythons
  on this Mac are broken.

## Architecture (src/joshua)
- `wargear/client.py`: the **only** way to call wargear.net. It allows one request in flight,
  sends everything through `RateGate`, and maps errors to exceptions.
- `wargear/ratelimit.py`: one global gate shared by all keys. It honors `Retry-After` and
  `X-RateLimit-*`/`RateLimit*`, and backs off exponentially on 429/5xx and Cloudflare 403s.
- `wargear/models.py`: lenient pydantic models. `Game.normalize()` produces a `GameState` keyed
  by player names.
- `poller.py`: every ~5 min. Greedy key coverage: the key in the most games goes first, and keys
  whose games are already covered are skipped (all keys get checked hourly). A game that drops
  off the Live list is looked up in the paginated, unsorted Finished list, at most hourly.
- `sync.py`: a pure `diff(prev, new) -> events`, plus `apply()`, which writes them to the store.
  New turns are detected with the per-player `turn_counter`.
- `reminders.py`: the pure `plan_reminders()` / `due_now()` scheduler.
- `notifier.py`: once a tick, works out what's due and posts it:
  - turn reminders;
  - winner nags, one post per game even for team wins;
  - sign-up nags;
  - "who is X?" prompts.
- `slack/commands.py`: `/wargear` subcommands, @mentions, the API-key modal, and the "That's me"
  button. `slack/app.py` wires it all together in Socket Mode.
- `db.py` + `migrations/NNN_*.sql`: SQLite. A game is "tracked" if it's forced on, or if it has
  2 or more registered players.

## Rules
- Keep scheduling logic pure, and pass `now` in. Tests hard-code times in America/New_York.
- Never call wargear.net any way except through `WarGearClient`, and never add polling that
  bypasses the poller's key coverage. The group must not get flagged by WarGear or Cloudflare.
- WarGear returns errors as **HTTP 200** `{"status":"Error","message":"Invalid API Key"}`.
  Treat any non-list GetGameList response as an error, never as "no games".
- API keys (`wg_ro_<hex>`, read-only, one per account):
  - Fernet-encrypted with `JOSHUA_SECRET_KEY` before they touch disk.
  - The DB file is owner-only (0600).
  - Never taken as CLI flags.
  - Redacted from logs.
  - Never print them or write them to fixtures.
- All user-facing copy lives in `slack/messages.py` and stays in the WOPR voice: "SHALL WE PLAY
  A GAME?", DEFCON 5 for turn start down to DEFCON 1 at T-5, "A STRANGE GAME."
- Don't link WarGear's rematch feature. The group finds it unreliable, so winner nags link to
  `/games/create`.
- Schema changes go in a new migration file once anything is deployed. (`001` was edited before
  the first deploy, which is fine.)

## Product rules (agreed with the user)
- **Channel:** everything posts to one shared channel (`SLACK_CHANNEL_ID`). No DMs.
- **Turn reminders:**
  - post at turn start, or when the player's loud hours begin;
  - nag every 4h during loud hours (default 08:00–22:00 in their Slack timezone, changeable
    with `/wargear hours`);
  - post a last call 30 minutes before quiet hours;
  - T-30 and T-5 warnings fire even during quiet hours;
  - every reminder shows the skip time. Skip time = `turnstamp + boot_time`; our games use 24h.
- **Winner nag:** right away (moved to loud hours if needed), then again 24h later unless the
  winner has already hosted a new game.
- **Sign-up nag:**
  - only for tracked Open games created in the last 14 days;
  - every 12h during default loud hours, pinging invitees who haven't joined;
  - stops when the seats (`num_players`) are full. The group invites 6–7 people to 5-seat games,
    first come first served.
- **Game type:** the group always plays turn-based games. Simultaneous games are handled but
  less tested.

## Deployment
Production will be a Raspberry Pi called **paradox**. Its infra repo hasn't been decided. Follow
the conventions in `~/src/home-docks-infra`:
- `scripts/build-ship.sh` builds an arm64 image from `git archive` with
  `--target production`, tagged with the git SHA;
- state lives in `/srv/joshua/data`;
- secrets come from `op run` with `op://Home Infrastructure/joshua/<field>`;
- the healthcheck tests real work (a recent poll and a live Slack socket).

`compose.example.yaml` is the template.

## Status / open questions
- The Slack app (`slack/manifest.yaml`) is waiting on workspace-admin access. Testing happens in
  a spare sandbox workspace first.
- Unknown: whether `pagenumber` works on Finished; what a terminated game looks like; whether a
  game you're only *invited* to appears in your own Live list.
