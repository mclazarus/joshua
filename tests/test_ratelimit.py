import random
from email.utils import formatdate

import pytest

from joshua.wargear.ratelimit import BACKOFF_BASE, RateGate, parse_limit_headers

NOW = 1_790_000_000.0


class Clock:
    def __init__(self, t=NOW):
        self.t = t
        self.slept: list[float] = []

    def __call__(self):
        return self.t

    async def sleep(self, s):
        self.slept.append(s)
        self.t += s


def gate(clock, **kw):
    return RateGate(min_interval=2.0, clock=clock, sleep=clock.sleep, rng=random.Random(0), **kw)


def test_parse_github_style_epoch_reset():
    info = parse_limit_headers(
        {"X-RateLimit-Limit": "100", "X-RateLimit-Remaining": "7", "X-RateLimit-Reset": str(int(NOW) + 90)},
        NOW,
    )
    assert (info.limit, info.remaining, info.reset_after) == (100, 7, 90)


def test_parse_ietf_delta_and_structured():
    assert parse_limit_headers({"RateLimit-Remaining": "0", "RateLimit-Reset": "45"}, NOW).reset_after == 45
    info = parse_limit_headers({"RateLimit": "limit=10, remaining=3, reset=20"}, NOW)
    assert (info.limit, info.remaining, info.reset_after) == (10, 3, 20)


def test_parse_retry_after_http_date():
    info = parse_limit_headers({"Retry-After": formatdate(NOW + 120, usegmt=True)}, NOW)
    assert info.retry_after == pytest.approx(120, abs=1)


def test_429_honors_retry_after():
    c = Clock()
    g = gate(c)
    g.observe(429, {"Retry-After": "300"})
    assert g.blocked_for() == 300


def test_429_without_headers_backs_off_exponentially_with_cap():
    c = Clock()
    g = gate(c)
    waits = []
    for _ in range(10):
        g.observe(429, {})
        waits.append(g.blocked_for())
        c.t += waits[-1]
    assert BACKOFF_BASE <= waits[0] <= BACKOFF_BASE * 1.25
    assert waits[1] > waits[0] * 1.5
    assert max(waits) <= 3600 * 1.25


def test_success_resets_backoff():
    c = Clock()
    g = gate(c)
    g.observe(429, {})
    g.observe(429, {})
    c.t += 10_000
    g.observe(200, {})
    g.observe(429, {})
    assert g.blocked_for() <= BACKOFF_BASE * 1.25


def test_exhausted_quota_blocks_until_reset():
    c = Clock()
    g = gate(c)
    g.observe(200, {"X-RateLimit-Limit": "60", "X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "600"})
    assert g.blocked_for() == 600


async def test_low_quota_spreads_requests():
    c = Clock()
    g = gate(c)
    g.observe(200, {"X-RateLimit-Limit": "100", "X-RateLimit-Remaining": "5", "X-RateLimit-Reset": "500"})
    await g.acquire()
    await g.acquire()
    assert c.slept[-1] == pytest.approx(100)


async def test_acquire_respects_min_interval():
    c = Clock()
    g = gate(c)
    await g.acquire()
    await g.acquire()
    assert c.slept == [2.0]


def test_block_never_shortens():
    c = Clock()
    g = gate(c)
    g.observe(429, {"Retry-After": "900"})
    g.observe(429, {"Retry-After": "10"})
    assert g.blocked_for() == 900
