import asyncio

from src.routes import Router, us_timezone
from src.serp import classify_status, is_blocked, search_url


class Ctx:
    def __init__(self, proxy):
        self.proxy, self.closed = proxy, False

    async def close(self):
        self.closed = True


def make(clock):
    made = []

    async def make_ctx(url):
        c = Ctx(url)
        made.append(c)
        return c

    async def pfn(sid):
        return f'http://proxy/{sid}'
    return made, make_ctx, pfn


def test_block_recycles_context_and_falls_back_to_proxy():
    t = [0.0]
    made, make_ctx, pfn = make(t)
    r = Router(make_ctx, pfn, clock=lambda: t[0])

    async def go():
        c1, kind, owned, gen = await r.acquire()
        assert kind == 'direct' and not owned
        c1b, *_ = await r.acquire()
        assert c1b is c1  # shared while healthy
        await r.report('direct', gen, 'blocked')
        assert c1.closed  # flagged cookie jar is never reused
        c2, kind2, owned2, _ = await r.acquire()
        assert kind2 == 'proxy' and owned2 and c2.proxy.startswith('http://proxy/lavg')
        c3, *_ = await r.acquire()
        assert c3.proxy != c2.proxy  # new session (IP) per try
        t[0] += 91  # cool-down over: back to the free direct IP with a fresh context
        c4, kind4, _, gen4 = await r.acquire()
        assert kind4 == 'direct' and c4 is not c1 and gen4 == 2
        # second streak cools longer
        await r.report('direct', gen4, 'blocked')
        t[0] += 91
        assert not r.direct_ok()
    asyncio.run(go())


def test_stale_block_report_does_not_close_new_context():
    made, make_ctx, pfn = make(None)
    r = Router(make_ctx, None)

    async def go():
        c1, _, _, g1 = await r.acquire()
        await r.report('direct', g1, 'blocked')
        r.direct_cool_until = 0
        c2, _, _, g2 = await r.acquire()
        await r.report('direct', g1, 'blocked')  # late report from the old context
        assert not c2.closed
    asyncio.run(go())


def test_no_proxy_waits_for_cooldown():
    t = [0.0]
    made, make_ctx, _ = make(t)
    r = Router(make_ctx, None, clock=lambda: t[0])

    async def go():
        _, _, _, g = await r.acquire()
        await r.report('direct', g, 'blocked')
        assert 89 < r.wait_secs() <= 90
    asyncio.run(go())


def test_proxy_pool_marked_dead_after_many_blocks():
    made, make_ctx, pfn = make(None)
    r = Router(make_ctx, pfn)
    r.direct_cool_until = 1e18

    async def go():
        for _ in range(12):
            await r.report('proxy', 0, 'blocked')
        assert not r.proxy_ok()
    asyncio.run(go())


def test_user_proxy_is_preferred_and_never_dropped():
    made, make_ctx, pfn = make(None)
    r = Router(make_ctx, pfn, prefer_proxy=True)

    async def go():
        _, kind, _, _ = await r.acquire()
        assert kind == 'proxy'
        for _ in range(20):
            await r.report('proxy', 0, 'blocked')
        assert r.proxy_ok()
    asyncio.run(go())


def test_blocked_in_row_counter():
    r = Router(None, None)
    for s in ['blocked', 'blocked', 'answer', 'blocked']:
        r.check_done(s)
    assert r.blocked_in_row == 1


def test_helpers():
    assert us_timezone(-97.74) == 'America/Chicago' and us_timezone(-78.6) == 'America/New_York'
    assert us_timezone(-122.4) == 'America/Los_Angeles' and us_timezone(-104.9) == 'America/Denver'
    assert is_blocked('https://www.google.com/sorry/index?continue=x', '')
    assert not is_blocked('https://www.google.com/search?q=x', '<html>')
    assert 'uule=' in search_url('dentist', 30.27, -97.74, 'google_aio')
    assert classify_status('google_aio', {'found': True, 'text': 'x' * 400}) == 'answer'
    assert classify_status('google_aio', {'found': False}) == 'no_answer'
