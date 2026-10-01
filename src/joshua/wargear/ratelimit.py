"""A single gate that every WarGear request passes through.

WarGear doesn't document rate limits, so we stay polite and listen to whatever
the server tells us:

* ``Retry-After`` on 429/503, as delta-seconds or an HTTP date.
* ``X-RateLimit-Limit`` / ``-Remaining`` / ``-Reset`` (GitHub style; reset may be
  an epoch timestamp or delta seconds).
* ``RateLimit-Limit`` / ``-Remaining`` / ``-Reset`` (IETF draft, reset is delta
  seconds), and the structured ``RateLimit: limit=.., remaining=.., reset=..``.

With none of those, a 429 triggers exponential backoff with jitter. The gate is
global: one 429 pauses requests for every key, because WarGear sees one client.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from email.utils import parsedate_to_datetime

log = logging.getLogger(__name__)

BACKOFF_BASE = 60.0
BACKOFF_CAP = 3600.0
# Below this fraction of the quota left, spread remaining requests over the window.
LOW_WATER = 0.2
# A reset value larger than this is an epoch timestamp, not a delta.
EPOCH_THRESHOLD = 1_000_000_000


@dataclass(frozen=True)
class LimitInfo:
    limit: int | None = None
    remaining: int | None = None
    reset_after: float | None = None  # seconds from now
    retry_after: float | None = None  # seconds from now


def _int(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return int(float(value.strip()))
    except ValueError:
        return None


def _reset_seconds(value: str | None, now: float) -> float | None:
    n = _int(value)
    if n is None:
        return None
    if n > EPOCH_THRESHOLD:
        return max(0.0, n - now)
    return max(0.0, float(n))


def _retry_after(value: str | None, now: float) -> float | None:
    if value is None:
        return None
    n = _int(value)
    if n is not None:
        return max(0.0, float(n))
    try:
        return max(0.0, parsedate_to_datetime(value).timestamp() - now)
    except (TypeError, ValueError):
        return None


def parse_limit_headers(headers: Mapping[str, str], now: float) -> LimitInfo:
    h = {k.lower(): v for k, v in headers.items()}

    limit = _int(h.get("x-ratelimit-limit") or h.get("ratelimit-limit"))
    remaining = _int(h.get("x-ratelimit-remaining") or h.get("ratelimit-remaining"))
    reset = _reset_seconds(h.get("x-ratelimit-reset") or h.get("ratelimit-reset"), now)

    structured = h.get("ratelimit")
    if structured:
        parts = {}
        for item in structured.replace(";", ",").split(","):
            key, _, val = item.strip().partition("=")
            parts[key.strip().lower()] = val.strip()
        limit = limit if limit is not None else _int(parts.get("limit"))
        remaining = remaining if remaining is not None else _int(parts.get("remaining"))
        if reset is None and "reset" in parts:
            reset = _reset_seconds(parts["reset"], now)

    return LimitInfo(
        limit=limit,
        remaining=remaining,
        reset_after=reset,
        retry_after=_retry_after(h.get("retry-after"), now),
    )


class RateGate:
    def __init__(
        self,
        min_interval: float = 2.0,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], object] = asyncio.sleep,
        rng: random.Random | None = None,
    ):
        self.min_interval = min_interval
        self._clock = clock
        self._sleep = sleep
        self._rng = rng or random.Random()
        self._lock = asyncio.Lock()
        self._last_request = 0.0
        self._spacing = min_interval
        self.blocked_until = 0.0
        self._backoff = BACKOFF_BASE

    def blocked_for(self) -> float:
        """Seconds until requests are allowed again (0 if open)."""
        return max(0.0, self.blocked_until - self._clock())

    async def acquire(self) -> None:
        """Wait for our turn. Callers hold no lock while the request is in flight;
        WarGear calls are serialized by the client instead."""
        async with self._lock:
            now = self._clock()
            wait = max(self.blocked_until - now, self._last_request + self._spacing - now, 0.0)
            if wait > 0:
                await self._sleep(wait)
            self._last_request = self._clock()

    def observe(self, status: int, headers: Mapping[str, str]) -> None:
        now = self._clock()
        info = parse_limit_headers(headers, now)

        if status in (429, 503):
            delay = info.retry_after
            if delay is None and info.remaining == 0:
                delay = info.reset_after
            if delay is None:
                delay = self._backoff * (1 + self._rng.random() * 0.25)
                self._backoff = min(self._backoff * 2, BACKOFF_CAP)
            self._block(now + delay, f"HTTP {status}")
            return

        if 500 <= status < 600:
            self._block(now + min(self._backoff, BACKOFF_CAP), f"HTTP {status}")
            self._backoff = min(self._backoff * 2, BACKOFF_CAP)
            return

        self._backoff = BACKOFF_BASE
        if info.retry_after:
            self._block(now + info.retry_after, "Retry-After on success")

        if info.remaining is not None and info.reset_after is not None:
            if info.remaining <= 0:
                self._block(now + info.reset_after, "quota exhausted")
                self._spacing = self.min_interval
            elif info.limit and info.remaining / info.limit < LOW_WATER:
                # Spread what's left evenly across the rest of the window.
                self._spacing = max(self.min_interval, info.reset_after / info.remaining)
                log.info(
                    "WarGear quota low (%s/%s); spacing requests %.0fs apart",
                    info.remaining,
                    info.limit,
                    self._spacing,
                )
            else:
                self._spacing = self.min_interval

    def _block(self, until: float, why: str) -> None:
        if until > self.blocked_until:
            self.blocked_until = until
            log.warning("WarGear rate gate closed for %.0fs (%s)", until - self._clock(), why)
