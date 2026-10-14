# WarGear API notes

WarGear publishes a Swagger 1.2 spec. The UI is at https://api.wargear.net/ and the raw spec
is at https://api.wargear.net/api-docs/<Resource>. The base URL is `https://www.wargear.net/rest`.
Every call takes `?api_key=…`.

**Keys:** Each account has exactly one key, and it's **read-only**. It looks like `wg_ro_<hex>`,
and you can find it under Settings → Account → API Keys
(https://www.wargear.net/settings/account#api_keys).

| Endpoint | Use in Joshua |
|---|---|
| `GET /GetGameList/my?viewselector=Live` | **Main poll.** Live games plus *Open* (sign-up) games the key owner is in |
| `GET /GetGameList/my?viewselector=Finished[&pagenumber=N]` | Looked up when a game drops off the Live list, to get its winners |
| `GET /GetGameList/player?player=<name>` | Returned `[]` in testing, so it isn't used. Every player needs their own key |
| `GET /GetCurrentTurns` | Not used. Returns `[]` when it isn't your turn, and only covers the key owner |
| `GET /GetHistoryUpdate/{gameid}` | Once per won, tracked game, for the after-action report. See below |
| `GET /GetBoardData/{boardid}` | Once per board ever: dice sides, territory names, and the board's fog level |

## Confirmed against real responses (2026-10-01)
Sanitized copies are in `tests/fixtures/`. To regenerate them, run `joshua discover`, then
`tests/fixtures/sanitize.py`.

- **Envelope:** the response is a bare JSON array of game objects. All numbers arrive as
  strings. A timestamp of `"0"` means unset.
- **`gamestatus`:**
  - `Live`
  - `Open` (taking sign-ups; shows up in the **Live** list, sometimes for years)
  - `Finished`
- **`gametype`** is `Private`/`Public`. The turn style is in **`gameplay_type`**: `Turn Based`
  or `Simultaneous`.
- **`players`:**
  - usually a dict keyed by seat, sometimes a list;
  - open games include empty seats with `name: null`;
  - in live games, `status`, `phase`, `cards` etc. are `"?"` (hidden);
  - in finished games, `status` is `Winner`, `Eliminated`, `Booted` or `Surrendered`;
  - in open games, `status` is `Joined` or `Invited`. Older games show `"?"` for invitees who
    never joined. `num_players` is the seat cap, and seats go first come, first served. The group
    often invites 6–7 people to a 5-seat game;
  - `turn_counter` is **per player**: the person whose turn it is has one less than those who
    have already gone this round;
  - `autoboot_next_turn: 1` means the player was skipped last turn.
- **`current_turn`:** a list of player **names**. It's `[null]` when nobody is up.
- **`winners`, `eliminated`:** PHP-serialized arrays of player **id hashes**, e.g.
  `a:1:{i:0;s:32:"<md5>";}`. They're `null` or `""` when empty. Team games list several winners.
- **`host`:** a player id hash.
- **Skip time = `turnstamp + boot_time`.** Checked against `time_remaining`, which matched to
  within the fetch latency. `time_remaining` is `9999999999` when no clock is running and `0`
  for open games.
- **`boot_type`:** `Skip`. Our games use `boot_time = 86400`.
- **Errors come back as HTTP 200.** A bad key returns `{"status":"Error","message":"Invalid API Key"}`.
  So the client checks the body, and it treats any non-list response from GetGameList as an
  error rather than as "no games".
- **`fog`:** `None`, `Light`, `Medium`, `Heavy`, `Board Default` or `""`. `Board Default`
  means the board's rules decide (`rules.fog` from GetBoardData). Joshua treats anything it
  can't resolve to `None` as fogged, and doesn't announce eliminations in fogged games.
  `count_attacks`/`count_fortifies`/`turn_counter` stay visible under fog; `status`, `cards`
  etc. show `"?"`.
- **`msgstamps`, `visitstamps`:** dicts keyed by player name, or `false`.
- **Finished list:** not sorted (15 games, from 2013 to 2026). `pagenumber` hasn't been tested
  yet. The poller walks up to 5 pages and stops at an empty page or one that repeats.

## GetHistoryUpdate (confirmed 2026-10-13)
The whole action log of a game, in one response (about 900KB for a 4-player, 66-turn game).
It comes from XML, so every action is wrapped: `{"history": {"turn": [{"@attributes": {...}}]}}`.
All values are strings, and `"0"` means unset. The fields are `turnid, timestamp, action, s`
(the acting seat; `N` = neutral), `gp, tid, fromid, toid, count, seats, cards, bmod, country`,
plus `ad, dd, al, dl` on attacks. `gp` holds player id hashes on `create_game`/`join_game` and
`On`/`Off` on attacks. `country` is the actor's country: never commit it.

The actions we use:
- **`select`** `s`, `tid`: initial territory assignment. **`neutralise`** `tid` plus
  **`place`** `s=N`: neutral territories. Scenario boards may skip `select`, so `place`,
  `fortify` and attacks also fill in unknown owners.
- **`attack`** `s, fromid, toid, ad, dd, bmod, al, dl`: **one roll**.
  - `ad`/`dd` are the **faces rolled**, e.g. `"6,4,1"` vs `"5,3"`.
  - Standard resolution: pair off the highest dice, ties go to the defender. All 1,775 rolls
    in the sample matched.
  - `bmod` is `"<attacker>,<defender>"` **extra die sides** from border modifiers. With `"2,0"`
    the attacker's faces run 1–8 on a d6 board.
- **`occupy`** `s, fromid, toid, count`: a conquest. `toid` is now owned by `s`.
- **`eliminate_player`** `s` → `seats`, followed by `capture_cards`.
  **`game_win`** `s`.
- Also: `start_turn`/`end_turn`, `bonus_units`, `trade_cards` (`cards` = `ABC`…),
  `award_card`, `capture_capital`, `assign_capital`, `factory_place`, `transfer`, `fortify`,
  `skip_turn`, `setup_complete`.

Not seen yet: `booted`, `surrender`, `team_win`, `game_terminate`.

## GetBoardData (confirmed 2026-10-13)
`{"board", "territories", "continents", "borders", "cards", "rules", "colors"}`, each wrapped in
`@attributes`. Joshua keeps:
- `board.boardname`;
- `rules.attacker_dice_sides`, `rules.defender_dice_sides` and `rules.fog`;
- `territories.territory[].{tid, name}`.

About 180KB for a 132-territory board.

## URLs
- Game: `https://www.wargear.net/games/view/{gameid}`
- Join an open game: `https://www.wargear.net/games/join/{gameid}` (the sign-up nag's button)
- Rematch: `…/games/view/{gameid}/Info?action=rematch`. **Deliberately not used**: the group finds WarGear's rematch flow unreliable.
- New game: `https://www.wargear.net/games/create`

## Rate limits
WarGear sits behind **Cloudflare** and sends `cache-control: no-cache` and no rate-limit headers
at all. Since there's no published limit, Joshua stays conservative:
- one request in flight at a time;
- at least 2s between requests;
- a poll every ~5 minutes with jitter;
- the fewest keys that cover every game.

It still honors `Retry-After`/`RateLimit-*` if they ever show up, and backs off on 429/5xx.
A Cloudflare challenge (a 403 with an HTML body) closes the gate the same way a 429 does. It
never marks a key as bad.

## Still open
- [ ] Does `pagenumber` work for `Finished`, and what's the page size?
- [ ] What does a terminated game look like? Does it leave the Live list, and with what status?
