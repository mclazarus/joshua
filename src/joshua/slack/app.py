"""Wires the Slack app, the poller and the reminder ticker into one process."""

from __future__ import annotations

import asyncio
import logging

from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
from slack_bolt.async_app import AsyncApp
from slack_sdk.web.async_client import AsyncWebClient

from joshua.config import Settings
from joshua.crypto import KeyBox
from joshua.db import Store, now_ts
from joshua.notifier import Notifier
from joshua.poller import Poller
from joshua.slack import commands
from joshua.wargear.client import WarGearClient
from joshua.wargear.ratelimit import RateGate

log = logging.getLogger(__name__)

TZ_REFRESH_SECONDS = 86400


class SlackPoster:
    def __init__(self, client: AsyncWebClient, channel: str):
        self.client = client
        self.channel = channel

    async def post(self, text: str, blocks: list[dict]) -> str | None:
        log.info("posting to %s: %s", self.channel, text)
        resp = await self.client.chat_postMessage(
            channel=self.channel, text=text, blocks=blocks, unfurl_links=False
        )
        return resp.get("ts")


async def ticker(
    notifier: Notifier,
    store: Store,
    handler: AsyncSocketModeHandler,
    client: AsyncWebClient,
    settings: Settings,
) -> None:
    last_tz = 0
    while True:
        try:
            if await handler.client.is_connected():
                await store.kv_set("slack_ok", str(now_ts()))
            await notifier.tick()
            if now_ts() - last_tz > 3600:
                last_tz = now_ts()
                for user in await store.stale_tz(now_ts() - TZ_REFRESH_SECONDS):
                    info = await client.users_info(user=user)
                    await store.set_tz(user, info["user"].get("tz"))
        except Exception:
            log.exception("tick crashed")
        await asyncio.sleep(settings.tick_seconds)


async def run(settings: Settings) -> None:
    for name in ("slack_bot_token", "slack_app_token", "slack_channel_id", "joshua_secret_key"):
        if not getattr(settings, name):
            raise SystemExit(f"{name.upper()} is not set")

    store = await Store.open(settings.db_path)
    keybox = KeyBox(settings.joshua_secret_key)
    wg = WarGearClient(
        settings.wargear_base_url, settings.wargear_api_path, RateGate(settings.wargear_min_interval)
    )
    app = AsyncApp(token=settings.slack_bot_token)
    poster = SlackPoster(app.client, settings.slack_channel_id)
    notifier = Notifier(store, poster, settings)
    poller = Poller(store, wg, keybox, settings, on_events=notifier.handle_events)
    commands.register(app, commands.Deps(store, wg, keybox, poller, notifier, settings))

    if not settings.admin_ids:
        log.warning("JOSHUA_ADMINS is empty: every Slack user can run admin commands")
    handler = AsyncSocketModeHandler(app, settings.slack_app_token)
    await handler.connect_async()
    log.info("WOPR online. SHALL WE PLAY A GAME?")
    log.info(
        "channel=%s db=%s default_tz=%s loud_hours=%s poll=%ss fast_reminders=%s players=%d keys=%d",
        settings.slack_channel_id,
        settings.db_path,
        settings.default_tz,
        settings.default_loud_hours,
        settings.poll_seconds,
        settings.joshua_fast_reminders,
        len(players := await store.players()),
        sum(1 for p in players if p.enc_api_key),
    )
    try:
        await asyncio.gather(
            poller.run_forever(),
            ticker(notifier, store, handler, app.client, settings),
        )
    finally:
        await handler.close_async()
        await wg.aclose()
        await store.close()
