# Joshua

> GREETINGS PROFESSOR FALKEN. SHALL WE PLAY A GAME?

Joshua is a Slack bot that watches our [WarGear](https://www.wargear.net) games. It posts in a
shared channel when it's your turn. It nags you every 4 hours during your loud hours, gives you
a last call before quiet hours, and always warns you 30 and 5 minutes before WarGear skips you.
When a game ends, it tells the winner to start the next one, then reminds them once more a day
later if they haven't. For new sign-up games, it pings invitees who haven't joined while seats
are still open. Named for the WOPR in *WarGames* (1983).

## How it works
- **WarGear:** uses WarGear's published REST API with players' own API keys (see
  [docs/wargear-api.md](docs/wargear-api.md)). It polls every ~5 minutes, through a global
  rate gate that honors `Retry-After` and `RateLimit` headers. It only uses as many keys as it
  takes to see every game.
- **Slack:** runs in Socket Mode, so it needs no inbound ports, tunnel or public URL.
- **Storage:** SQLite at `$DB_PATH`, owner-only. WarGear API keys are encrypted with
  `JOSHUA_SECRET_KEY` (Fernet) before they touch disk, are never accepted as CLI flags, and are
  redacted from the logs.

## Slack setup
1. Create an app from [slack/manifest.yaml](slack/manifest.yaml).
2. Generate an app-level token with `connections:write`. That's `SLACK_APP_TOKEN`.
3. Install the app to the workspace. That gives you `SLACK_BOT_TOKEN`.
4. Set the bot's avatar to something WOPR-ish.
5. Invite @Joshua to the war-room channel and set `SLACK_CHANNEL_ID`.

## Player setup (in Slack)
```
/wargear register <your WarGear name>
/wargear apikey            # opens a private modal
/wargear hours 08:00-22:00 # optional; uses your Slack timezone
```
Players who haven't registered get a "That's me" button the first time they show up in a game.

## Development
```
uv sync
uv run pytest
uv run joshua genkey                       # make a JOSHUA_SECRET_KEY
uv run joshua discover                     # dump raw API responses (gitignored); prompts for your key
uv run joshua dry-run --me YOURNAME        # poll once, print, post nothing; prompts for your key
cp .env.example .env && uv run joshua run
```

## Deploy
The image builds for arm64 with `--target production`. It's compatible with
`home-docks-infra/scripts/build-ship.sh`, and `compose.example.yaml` follows the same
conventions. The bot's state lives in `/srv/joshua/data`.
