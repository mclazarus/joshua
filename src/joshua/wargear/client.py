"""A small, polite async client for the WarGear REST API."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from joshua import __version__
from joshua.wargear.models import Game, parse_game_list
from joshua.wargear.ratelimit import RateGate

log = logging.getLogger(__name__)

USER_AGENT = f"joshua-slackbot/{__version__} (turn reminders for a private Slack group)"


class WarGearError(Exception):
    pass


class RateLimited(WarGearError):
    """The gate is closed; try again after ``retry_in`` seconds."""

    def __init__(self, retry_in: float):
        super().__init__(f"WarGear rate gate closed for {retry_in:.0f}s")
        self.retry_in = retry_in


class BadApiKey(WarGearError):
    pass


class WarGearClient:
    def __init__(
        self,
        base_url: str = "https://www.wargear.net",
        api_path: str = "/rest",
        gate: RateGate | None = None,
        http: httpx.AsyncClient | None = None,
    ):
        self.gate = gate or RateGate()
        self._http = http or httpx.AsyncClient(
            base_url=base_url.rstrip("/") + api_path,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            timeout=httpx.Timeout(20.0),
            follow_redirects=True,
        )
        # One request in flight at a time, across every key.
        self._serial = asyncio.Lock()
        self.request_count = 0

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _get(self, path: str, api_key: str, **params: Any) -> Any:
        if (wait := self.gate.blocked_for()) > 0:
            raise RateLimited(wait)
        async with self._serial:
            await self.gate.acquire()
            query = {k: v for k, v in params.items() if v not in (None, "")}
            query["api_key"] = api_key
            try:
                resp = await self._http.get(path, params=query)
            except httpx.HTTPError as e:
                # Network failures count as server trouble for backoff purposes.
                self.gate.observe(503, {})
                raise WarGearError(f"{path}: {e.__class__.__name__}") from e
            finally:
                self.request_count += 1
            self.gate.observe(resp.status_code, resp.headers)

        if resp.status_code == 429:
            raise RateLimited(self.gate.blocked_for())
        is_json = "json" in resp.headers.get("content-type", "")
        if resp.status_code == 403 and not is_json:
            # A Cloudflare challenge page, not a key problem. Back off like a 429.
            self.gate.observe(429, resp.headers)
            raise RateLimited(self.gate.blocked_for())
        if resp.status_code == 401:
            raise BadApiKey(f"{path}: HTTP 401")
        if resp.status_code >= 400:
            raise WarGearError(f"{path}: HTTP {resp.status_code}")
        try:
            data = resp.json()
        except ValueError as e:
            raise WarGearError(f"{path}: response was not JSON") from e
        # Errors come back as HTTP 200: {"status": "Error", "message": "Invalid API Key"}
        if isinstance(data, dict) and str(data.get("status", "")).lower() == "error":
            message = str(data.get("message") or "unknown error")
            if "api key" in message.lower():
                raise BadApiKey(message)
            raise WarGearError(f"{path}: {message}")
        return data

    async def my_games(self, api_key: str, selector: str = "Live", page: int | None = None) -> list[Game]:
        data = await self._get("/GetGameList/my", api_key, viewselector=selector, pagenumber=page)
        if not isinstance(data, list):
            # An empty list is the only "no games" answer we trust; anything else is suspect.
            raise WarGearError(f"GetGameList/my: unexpected {type(data).__name__} response")
        return parse_game_list(data)

    async def player_games(self, api_key: str, player: str) -> list[Game]:
        data = await self._get("/GetGameList/player", api_key, player=player)
        return parse_game_list(data)

    async def current_turns(self, api_key: str) -> Any:
        return await self._get("/GetCurrentTurns", api_key)

    async def raw(self, path: str, api_key: str, **params: Any) -> Any:
        """Unparsed call, used by the discovery CLI."""
        return await self._get(path, api_key, **params)
