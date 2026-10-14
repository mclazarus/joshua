# Changelog

Newest first. Every release gets a `## Build N · YYYY-MM-DD` entry, numbered one up from the last.
The release's commit is stamped into the image when it's built. Once a build has shipped, its
heading gets the short commit hash: `## Build N · YYYY-MM-DD · abc1234`.

## Build 3 · 2026-10-14
- `/wargear version` shows which build is running and its commit.
- `/wargear changelog [build]` shows what changed in the current build or any earlier one.
- `/wargear unwatch <id>` silences a game's reminders, nags and report; `/wargear watch <id>`
  turns them back on. These replace `track`/`untrack`, which still work.
- Claimed the wrong name? `/wargear notme` or the "Oops, not me" button undoes it.
  `/wargear unlink` forgets you entirely, and `/wargear admins` lists who can fix mappings.
- Deadlines show in each reader's own timezone, with a readable fallback.
- No more double pings when someone changes their hours in the middle of a turn.

## Build 2 · 2026-10-14 · 261f490
- Posts that mention a registered player with no API key offer an "Add my API key" button, at
  most once a week per player.
- Fogged games no longer announce eliminations, and `/wargear games` no longer strikes out
  eliminated players in them.
- Won games get an after-action report: dice luck vs. the odds, peak territory, the kill board,
  the biggest battle and the bloodiest turn.
- `/wargear stats [board]` shows win/loss/draw records, overall or for one board. Winner posts
  get a records footer.

## Build 1 · 2026-10-01 · 7a1fe80
- First release. Joshua watches the group's WarGear games and posts in one shared channel.
- Turn reminders: at turn start, every 4 hours during your loud hours, a last call before quiet
  hours, and warnings 30 and 5 minutes before WarGear skips you.
- Winner nags to start the next game, with a follow-up a day later.
- Sign-up nags for invitees who haven't joined an open game.
- `/wargear register`, `apikey`, `hours`, `games`, `game`, `me`, `who`, `track`/`untrack` and
  `link`/`unlink`, plus @mentions.
