"""Slash commands, @mentions, the API key modal and the "That's me" button."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from slack_bolt.async_app import AsyncApp

from joshua.config import Settings, parse_hours
from joshua.crypto import KeyBox
from joshua.db import NameTaken, Store, now_ts
from joshua.notifier import Notifier
from joshua.poller import Poller
from joshua.slack import messages as m
from joshua.wargear.client import BadApiKey, WarGearClient, WarGearError

log = logging.getLogger(__name__)

USER_REF = re.compile(r"<@([UW][A-Z0-9]+)(?:\|[^>]*)?>")
GAME_REF = re.compile(r"(\d{3,})")
API_KEY_SHAPE = re.compile(r"^wg_[a-z]+_[0-9a-fA-F]{16,}$")


@dataclass
class Deps:
    store: Store
    client: WarGearClient
    keybox: KeyBox
    poller: Poller
    notifier: Notifier
    settings: Settings


async def game_rows(deps: Deps, only_player: str | None = None) -> list[m.GameRow]:
    rows = []
    now = now_ts()
    for g in await deps.store.tracked_games():
        if g.open and (g.createstamp or 0) < now - deps.settings.signup_max_age_days * 86400:
            continue  # abandoned sign-up pages linger in the Live list for years
        turns = [t for t in await deps.store.open_turns() if t.gameid == g.gameid]
        if only_player:
            turns = [t for t in turns if t.player.lower() == only_player.lower()]
            if not turns:
                continue
        waiting = [(m.Who(t.player, t.slack_id), t.started_at, t.deadline) for t in turns]
        players = []
        for p in g.players:
            if g.open:
                label = p.name if p.joined else f"_{p.name} (invited)_"
            else:
                label = f"~{p.name}~" if p.name in g.eliminated else p.name
            players.append(label)
        rows.append(
            m.GameRow(
                g.gameid,
                g.name,
                deps.settings.game_url(g.gameid),
                waiting,
                g.startstamp or g.createstamp,
                players,
                open=g.open,
                spots_left=g.spots_left,
            )
        )
    return rows


async def game_detail(deps: Deps, gameid: int) -> m.Message:
    rows = [r for r in await game_rows(deps) if r.gameid == gameid]
    if rows:
        return m.games_overview(rows, now_ts())
    line = f"I'M NOT TRACKING GAME #{gameid}. Try `/wargear track {gameid}`."
    return line, [{"type": "section", "text": {"type": "mrkdwn", "text": line}}]


def register(app: AsyncApp, deps: Deps) -> None:
    store, settings = deps.store, deps.settings

    def is_admin(user_id: str) -> bool:
        return not settings.admin_ids or user_id in settings.admin_ids

    async def refresh_tz(client, user_id: str) -> None:
        try:
            info = await client.users_info(user=user_id)
            await store.set_tz(user_id, info["user"].get("tz"))
        except Exception as e:  # noqa: BLE001 - tz is nice to have
            log.warning("couldn't read tz for %s: %s", user_id, e)

    @app.command("/wargear")
    async def wargear(ack, command, respond, client):
        await ack()
        user = command["user_id"]
        text = (command.get("text") or "").strip()
        verb, _, rest = text.partition(" ")
        verb, rest = verb.lower(), rest.strip()
        log.info("/wargear %s from %s", verb or "(none)", user)

        async def say(msg: m.Message, public: bool = False):
            await respond(text=msg[0], blocks=msg[1], response_type="in_channel" if public else "ephemeral")

        async def plain(line: str):
            await respond(text=line, response_type="ephemeral")

        match verb:
            case "" | "help":
                await say(m.help_message())

            case "register":
                name = rest.strip("`*\"' ")
                if not name:
                    return await plain("Usage: `/wargear register <your WarGear name>`")
                try:
                    await store.link(user, name)
                except NameTaken as e:
                    return await plain(
                        f"*{name}* is already claimed by <@{e.slack_id}>. "
                        "Ask an admin to `/wargear link` it if that's wrong."
                    )
                await refresh_tz(client, user)
                known = await store.games_for_player(name)
                hint = (
                    f"I can see you in {len(known)} game(s)."
                    if known
                    else "I don't see you in any games yet. Run `/wargear apikey` so I can look."
                )
                await plain(f"GREETINGS, {name.upper()}. {hint}")

            case "apikey":
                if not await store.player(user):
                    return await plain("Register first: `/wargear register <your WarGear name>`")
                await client.views_open(trigger_id=command["trigger_id"], view=apikey_modal(settings))

            case "hours":
                if not await store.player(user):
                    return await plain("Register first: `/wargear register <your WarGear name>`")
                if rest.lower() in ("", "default", "reset"):
                    await store.set_loud_hours(user, None)
                    return await plain(f"Back to the default: {settings.default_loud_hours}.")
                try:
                    parse_hours(rest)
                except ValueError:
                    return await plain("Usage: `/wargear hours 08:00-22:00` (your Slack timezone)")
                await store.set_loud_hours(user, rest.replace(" ", ""))
                await refresh_tz(client, user)
                await plain(
                    f"Understood. I'll only nag you {rest}, except for the final 30- and 5-minute warnings."
                )

            case "games" | "status" | "list":
                await say(m.games_overview(await game_rows(deps), now_ts()))

            case "game":
                if not (ref := GAME_REF.search(rest)):
                    return await plain("Usage: `/wargear game <id or link>`")
                await say(await game_detail(deps, int(ref.group(1))))

            case "me" | "mine":
                p = await store.player(user)
                if not p or not p.wargear_name:
                    return await plain("I don't know who you are. `/wargear register <name>`")
                await say(m.my_turns(await game_rows(deps, p.wargear_name), now_ts()))

            case "who":
                pairs = [(p.wargear_name, p.slack_id, p.tz) for p in await store.players() if p.wargear_name]
                await say(m.who_table(pairs))

            case "track" | "untrack":
                if not (ref := GAME_REF.search(rest)):
                    return await plain(f"Usage: `/wargear {verb} <game id or link>`")
                gameid = int(ref.group(1))
                if not await store.set_tracked(gameid, verb == "track"):
                    return await plain(
                        f"I can't see game #{gameid}. Someone in it needs to give me "
                        "an API key (`/wargear apikey`)."
                    )
                await plain(f"Game #{gameid} {'tracked' if verb == 'track' else 'untracked'}.")

            case "link":
                if not is_admin(user):
                    return await plain("ACCESS DENIED.")
                ref = USER_REF.search(rest)
                name = USER_REF.sub("", rest).strip("`*\"' ")
                if not ref or not name:
                    return await plain("Usage: `/wargear link @user <wargear name>`")
                await store.link(ref.group(1), name, force=True)
                await refresh_tz(client, ref.group(1))
                await plain(f"Linked *{name}* → <@{ref.group(1)}>.")

            case "unlink":
                target = USER_REF.search(rest)
                target_id = target.group(1) if target else user
                if target_id != user and not is_admin(user):
                    return await plain("ACCESS DENIED.")
                await store.unlink(target_id)
                await plain(f"<@{target_id}> unlinked and their API key deleted.")

            case _:
                await plain(f"{m.quip()} Try `/wargear help`.")

    @app.view("apikey_submit")
    async def apikey_submit(ack, body, view, client):
        user = body["user"]["id"]
        key = view["state"]["values"]["key"]["value"]["value"].strip()
        if not API_KEY_SHAPE.match(key):
            return await ack(
                response_action="errors", errors={"key": "That doesn't look like a WarGear API key."}
            )
        await ack()
        log.info("API key submitted by %s; verifying", user)
        p = await store.player(user)
        try:
            games = await deps.client.my_games(key, "Live")
        except BadApiKey:
            return await _ephemeral(client, settings, user, "WarGear rejected that key. ACCESS DENIED.")
        except WarGearError as e:
            # Store it anyway; the poller will try again later.
            await store.set_api_key(user, deps.keybox.seal(key), None)
            return await _ephemeral(
                client, settings, user, f"Saved, but I couldn't verify it right now ({e}). I'll keep trying."
            )
        await store.set_api_key(user, deps.keybox.seal(key), True)
        names = {pl.name.lower() for g in games for pl in g.players if pl.name}
        warn = ""
        if p and p.wargear_name and games and p.wargear_name.lower() not in names:
            warn = (
                f"\n⚠️ None of those games have a player named *{p.wargear_name}*. "
                "Double-check `/wargear register`."
            )
        deps.poller.wake.set()
        await _ephemeral(
            client,
            settings,
            user,
            f"Key accepted. I can see {len(games)} live game(s). WOPR is online.{warn}",
        )

    @app.action("claim_identity")
    async def claim_identity(ack, body, action, client):
        await ack()
        user = body["user"]["id"]
        name = action["value"]
        existing = await store.player(user)
        if existing and existing.wargear_name and existing.wargear_name.lower() != name.lower():
            return await _ephemeral(
                client,
                settings,
                user,
                f"You're already registered as *{existing.wargear_name}*. "
                "Ask an admin to `/wargear link` if you play as both.",
            )
        try:
            await store.link(user, name)
        except NameTaken as e:
            return await _ephemeral(client, settings, user, f"*{name}* is already <@{e.slack_id}>.")
        await refresh_tz(client, user)
        text, blocks = m.identity_claimed(name, user)
        await client.chat_update(
            channel=body["channel"]["id"], ts=body["message"]["ts"], text=text, blocks=blocks
        )

    @app.action(re.compile(r"^open_"))
    async def link_clicked(ack):
        await ack()

    @app.event("app_mention")
    async def mention(event, say):
        text = USER_REF.sub("", event.get("text", "")).lower()
        log.info("mention from %s: %r", event.get("user"), text.strip())
        thread = event.get("thread_ts") or event["ts"]
        user = event.get("user", "")

        async def reply(msg: m.Message):
            await say(text=msg[0], blocks=msg[1], thread_ts=thread)

        if ref := GAME_REF.search(text):
            return await reply(await game_detail(deps, int(ref.group(1))))
        if "help" in text or "what can you" in text:
            return await reply(m.help_message())
        if re.search(r"\b(my turn|mine|me)\b", text):
            p = await store.player(user)
            if p and p.wargear_name:
                return await reply(m.my_turns(await game_rows(deps, p.wargear_name), now_ts()))
        for g in await store.tracked_games():
            if g.name.lower() in text:
                return await reply(await game_detail(deps, g.gameid))
        if re.search(r"turn|waiting|games?|playing|running|status|how long|link|who", text):
            return await reply(m.games_overview(await game_rows(deps), now_ts()))
        if re.search(r"\b(hi|hello|hey|greetings)\b", text):
            return await say(text=m.greeting(), thread_ts=thread)
        await say(text=f"{m.quip()}\n_Try asking “whose turn is it?” or `/wargear help`._", thread_ts=thread)


async def _ephemeral(client, settings: Settings, user: str, text: str) -> None:
    try:
        await client.chat_postEphemeral(channel=settings.slack_channel_id, user=user, text=text)
    except Exception as e:  # noqa: BLE001
        log.warning("couldn't send ephemeral to %s: %s", user, e)


def apikey_modal(settings: Settings) -> dict:
    return {
        "type": "modal",
        "callback_id": "apikey_submit",
        "title": {"type": "plain_text", "text": "WarGear API key"},
        "submit": {"type": "plain_text", "text": "Save"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f"Copy your key from <{settings.wargear_base_url}/settings/account#api_keys|"
                        "WarGear → Account → API Keys>. It's the read-only key that starts with `wg_ro_`.\n"
                        "I encrypt it, and I only use it to check your game list."
                    ),
                },
            },
            {
                "type": "input",
                "block_id": "key",
                "label": {"type": "plain_text", "text": "API key"},
                "element": {"type": "plain_text_input", "action_id": "value"},
            },
        ],
    }
