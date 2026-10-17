# Changelog

Newest first. Every release gets a `## Build N · YYYY-MM-DD` entry, numbered one up from the last.
The release's commit is stamped into the image when it's built. Once a build has shipped, its
heading gets the short commit hash: `## Build N · YYYY-MM-DD · abc1234`.

## Build 6 · 2026-10-16
- After-action reports now call out the biggest deathball (a stack over 20 troops), who turtled
  (turns attacking one territory or none, and how many of those they started with the most
  troops), and possible alliances: neighbours who leave each other alone for 5+ turns each
  while both hit the same player. Turns 1–2 never count toward an alliance, and teammates are never called allies.
- `/wargear report <game>` shows you the after-action report for any finished game of yours.
  Only you see it, and it doesn't change any stats.
- Team games: a team win counts for the whole team.

## Build 5 · 2026-10-16 · 0c9ffdf
- Joshua now notices when a game ends. It reads the game's own history to find the winner, instead
  of hunting through WarGear's finished-games list, which could miss a game that just ended. The
  winner no longer gets "your turn" nags, and the winner post, records and report go out as
  they should.

## Build 4 · 2026-10-14 · cd014c7
- The "Join the game" button on sign-up nags now goes straight to WarGear's join page instead of
  the game's view page.

## Build 3 · 2026-10-14 · fe85f92
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
