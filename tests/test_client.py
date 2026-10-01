import json
from pathlib import Path

import httpx
import pytest

from joshua.wargear.client import BadApiKey, RateLimited, WarGearClient, WarGearError
from joshua.wargear.ratelimit import RateGate

LIVE = (Path(__file__).parent / "fixtures" / "my_live.json").read_text()


async def no_sleep(_):
    return None


def client_for(handler) -> WarGearClient:
    http = httpx.AsyncClient(base_url="https://wg.test/rest", transport=httpx.MockTransport(handler))
    return WarGearClient(gate=RateGate(min_interval=0, sleep=no_sleep), http=http)


async def test_live_list_parses_and_sends_key_and_selector():
    seen = {}

    def handler(req: httpx.Request):
        seen.update(req.url.params)
        return httpx.Response(200, text=LIVE, headers={"content-type": "application/json"})

    games = await client_for(handler).my_games("wg_ro_abc", "Live")
    assert len(games) == 3
    assert seen == {"viewselector": "Live", "api_key": "wg_ro_abc"}


async def test_invalid_key_is_http_200_with_error_body():
    # Captured from the real API on 2026-10-01.
    body = json.dumps({"status": "Error", "message": "Invalid API Key"})
    c = client_for(lambda req: httpx.Response(200, text=body, headers={"content-type": "application/json"}))
    with pytest.raises(BadApiKey):
        await c.my_games("wg_ro_bad")


async def test_cloudflare_challenge_backs_off_instead_of_blaming_the_key():
    c = client_for(
        lambda req: httpx.Response(
            403, text="<html>Just a moment...</html>", headers={"content-type": "text/html"}
        )
    )
    with pytest.raises(RateLimited):
        await c.my_games("wg_ro_abc")
    assert c.gate.blocked_for() > 0


async def test_429_closes_gate_and_next_call_fails_fast():
    calls = []

    def handler(req):
        calls.append(req)
        return httpx.Response(429, headers={"Retry-After": "120"})

    c = client_for(handler)
    with pytest.raises(RateLimited):
        await c.my_games("k")
    with pytest.raises(RateLimited):
        await c.my_games("k")
    assert len(calls) == 1


async def test_unexpected_shape_is_an_error_not_an_empty_list():
    c = client_for(lambda req: httpx.Response(200, json={"surprise": True}))
    with pytest.raises(WarGearError):
        await c.my_games("k")
