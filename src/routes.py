"""Where each Google AI answer is loaded from, and what to do when Google blocks it.

Root cause of the 2026-11-14 scale test (63/100 blocked): every check came from the run's one IP in one shared
browser context. After ~35 searches Google flagged that IP + cookie jar, and every later check (and its retry, made
in the same flagged context) hit /sorry/. Fixes here:
- a block throws the flagged context away (new cookie jar) and cools the direct IP down with growing backoff;
- while the direct IP cools, checks go through Apify Proxy (datacenter, US) with a fresh session (new IP) per try,
  and a blocked session is never reused. Datacenter proxy has no per-GB fee;
- if every route keeps getting blocked, the run stops early instead of grinding through blocked checks
  (unfinished checks are marked and never charged).
"""
from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import Any, Awaitable, Callable

DIRECT_BACKOFF = (90.0, 180.0, 360.0, 600.0)  # cool-down of the run's own IP after each block streak


def us_timezone(lng: float) -> str:
    """Rough US time zone from longitude (Google compares it with the uule location)."""
    if lng > -87.5:
        return 'America/New_York'
    if lng > -101.0:
        return 'America/Chicago'
    if lng > -115.0:
        return 'America/Denver'
    return 'America/Los_Angeles'


class Router:
    """Hands out a browser context for one attempt and learns from blocks.

    make_ctx(proxy_url | None) -> new context. proxy_url_fn(session_id) -> proxy URL (or None if no proxy)."""

    def __init__(self, make_ctx: Callable[[str | None], Awaitable[Any]],
                 proxy_url_fn: Callable[[str], Awaitable[str | None]] | None, *, prefer_proxy: bool = False,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.make_ctx = make_ctx
        self.proxy_url_fn = proxy_url_fn
        self.prefer_proxy = prefer_proxy and proxy_url_fn is not None
        self.clock = clock
        self.direct_ctx: Any = None
        self.direct_gen = 0
        self.direct_cool_until = 0.0
        self.direct_blocks = 0  # block streaks so far (picks the backoff)
        self.direct_streak = 0
        self.proxy_blocks_in_row = 0
        self.proxy_dead = False
        self.proxy_errors_in_row = 0
        self.blocked_in_row = 0  # checks (not attempts) blocked in a row, any route
        self.stats = {'direct': 0, 'proxy': 0, 'directBlocked': 0, 'proxyBlocked': 0, 'contextsRecycled': 0}
        self._lock = asyncio.Lock()

    def proxy_ok(self) -> bool:
        return self.proxy_url_fn is not None and not self.proxy_dead

    def direct_ok(self) -> bool:
        return not self.prefer_proxy and self.clock() >= self.direct_cool_until

    def wait_secs(self) -> float:
        """How long until some route is usable (0 = now)."""
        if self.direct_ok() or self.proxy_ok():
            return 0.0
        return max(0.0, self.direct_cool_until - self.clock())

    async def acquire(self) -> tuple[Any, str, bool, int]:
        """-> (context, kind, owned, generation). owned contexts must be closed by the caller."""
        if self.direct_ok() or not self.proxy_ok():
            async with self._lock:
                if self.direct_ctx is None:
                    self.direct_ctx = await self.make_ctx(None)
                    self.direct_gen += 1
                self.stats['direct'] += 1
                return self.direct_ctx, 'direct', False, self.direct_gen
        if self.stats['proxy'] == 0 and not self.prefer_proxy:
            logging.getLogger('apify').info('Google blocked the direct IP: next checks go through Apify Proxy '
                                            '(datacenter, new IP per try) while it cools down.')
        sid = f'lavg{random.randrange(10**9)}'
        url = await self.proxy_url_fn(sid)
        self.stats['proxy'] += 1
        return await self.make_ctx(url), 'proxy', True, 0

    async def report(self, kind: str, gen: int, status: str) -> None:
        blocked = status == 'blocked'
        if kind == 'direct':
            if blocked:
                self.stats['directBlocked'] += 1
                async with self._lock:
                    if gen == self.direct_gen and self.direct_ctx is not None:
                        # the cookie jar is flagged now: never load another answer in it
                        old, self.direct_ctx = self.direct_ctx, None
                        self.stats['contextsRecycled'] += 1
                        try:
                            await old.close()
                        except Exception:  # noqa: BLE001
                            pass
                        self.direct_streak += 1
                        self.direct_cool_until = self.clock() + DIRECT_BACKOFF[min(self.direct_blocks, len(DIRECT_BACKOFF) - 1)]
                        self.direct_blocks += 1
            elif status in ('answer', 'no_answer'):
                self.direct_streak = 0
        else:
            if blocked:
                self.stats['proxyBlocked'] += 1
                self.proxy_blocks_in_row += 1
                if self.proxy_blocks_in_row >= 12 and not self.prefer_proxy:
                    self.proxy_dead = True  # this proxy pool is burned for Google; stop paying time for it
                    logging.getLogger('apify').info('Apify Proxy IPs are blocked by Google too: back to the direct IP.')
            elif status in ('answer', 'no_answer'):
                self.proxy_blocks_in_row = 0
                self.proxy_errors_in_row = 0
            if status == 'error':
                self.proxy_errors_in_row += 1
                if self.proxy_errors_in_row >= 6 and not self.prefer_proxy:
                    self.proxy_dead = True  # proxy unreachable (no access, no US IPs): go back to the direct IP

    def check_done(self, status: str) -> None:
        self.blocked_in_row = self.blocked_in_row + 1 if status == 'blocked' else 0

    async def close(self) -> None:
        if self.direct_ctx is not None:
            try:
                await self.direct_ctx.close()
            except Exception:  # noqa: BLE001
                pass
            self.direct_ctx = None
