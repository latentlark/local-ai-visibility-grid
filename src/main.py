"""Local AI Visibility Grid: is a local business named in Google's AI answers at each point of a map grid?"""
from __future__ import annotations

import time as _time

import asyncio
import json
import os
import pathlib
import random
import re
from datetime import datetime, timezone
from typing import Any

import httpx
from apify import Actor
from playwright.async_api import async_playwright

from . import grid as G
from .entities import (build_entities, domain_of, domain_type, enrich_with_maps, goto_target, is_google_redirect,
                       is_target, same_brand, SKIP_LINK, cid_of)
from .maps import MapsClient, norm, parse_maps_url
from .report import summarize, to_csv, to_html
from .routes import Router, us_timezone
from .serp import fetch_answer, playwright_proxy

EVENTS = json.loads((pathlib.Path(__file__).parent / 'events.json').read_text())
EV = {'google_aio': EVENTS['aiOverviewAnswer'], 'google_aimode': EVENTS['aiModeAnswer'], 'maps': EVENTS['mapsRankCheck']}
MAX_GRID = 7
MAX_QUESTIONS = 10
MAX_SAMPLES = 5
AIMODE_STOP_AFTER_BLOCKS = 4
BLOCK_STOP_CHECKS = 8  # this many checks blocked in a row with no fresh route left: stop (rest free)


def gl_in(inp: dict[str, Any]) -> str:
    return (inp.get('country') or 'us').lower()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def parse_latlng(s: str) -> tuple[float, float] | None:
    m = re.fullmatch(r'\s*(-?\d+(?:\.\d+)?)\s*[,; ]\s*(-?\d+(?:\.\d+)?)\s*', s or '')
    if not m:
        return None
    lat, lng = float(m.group(1)), float(m.group(2))
    return (lat, lng) if -90 <= lat <= 90 and -180 <= lng <= 180 else None


def parse_points(raw: list[Any]) -> list[dict[str, Any]] | str:
    pts = []
    for i, x in enumerate(raw or []):
        label = None
        if isinstance(x, dict):
            ll = (x.get('lat'), x.get('lng'))
            label = x.get('label')
            ll = (float(ll[0]), float(ll[1])) if None not in ll else None
        else:
            s = str(x)
            if '|' in s:
                s, label = s.split('|', 1)
            ll = parse_latlng(s)
        if not ll:
            return f'Point {i + 1} ("{x}") is not "lat,lng". Example: "35.78045,-78.63518".'
        pts.append({'pointIndex': i, 'gridRow': None, 'gridCol': None, 'lat': round(ll[0], 6), 'lng': round(ll[1], 6),
                    'isCenter': i == 0, 'pointLabel': (label or '').strip() or f'point {i + 1}'})
    return pts


async def resolve_target(mc: MapsClient, inp: dict[str, Any], center: tuple[float, float] | None) -> dict[str, Any]:
    t: dict[str, Any] = {'placeId': (inp.get('placeId') or '').strip() or None, 'hexId': None, 'cid': None,
                         'name': (inp.get('businessName') or '').strip() or None, 'address': None, 'phone': None,
                         'website': None, 'lat': None, 'lng': None}
    if inp.get('mapsUrl'):
        u = parse_maps_url(inp['mapsUrl'])
        t['placeId'] = t['placeId'] or u['placeId']
        t['hexId'], t['cid'] = u['hexId'], u['cid']
        t['name'] = t['name'] or u['name']
    if t['hexId'] and not t['cid']:
        t['cid'] = cid_of(t['hexId'])
    if not t['name']:
        return t
    city = (inp.get('city') or '').strip()
    lat, lng = center or (0.0, 0.0)
    try:
        places = await mc.ranked(f'{t["name"]} {city}'.strip(), lat, lng, 13.0 if center else 3.0, 5)
    except Exception as e:  # noqa: BLE001
        Actor.log.warning(f'Business lookup failed: {e}')
        places = []

    def same_id(p):
        return (t['placeId'] and p.get('placeId') == t['placeId']) or (t['hexId'] and p.get('hexId') == t['hexId']) or \
               (t['cid'] and cid_of(p.get('hexId')) == str(t['cid']))
    best = next((p for p in places if same_id(p)), None)
    if not best and not (t['placeId'] or t['hexId'] or t['cid']):
        best = next((p for p in places if norm(p['name']) == norm(t['name'])), None) or \
            next((p for p in places if norm(t['name']) in norm(p['name']) or norm(p['name']) in norm(t['name'])), None)
    if best:
        t.update(placeId=best.get('placeId') or t['placeId'], hexId=best.get('hexId') or t['hexId'],
                 cid=cid_of(best.get('hexId')) or t['cid'], name=best['name'], lat=best.get('lat'), lng=best.get('lng'),
                 address=best.get('address'), phone=best.get('phone'), website=best.get('website'),
                 rating=best.get('rating'), reviewCount=best.get('reviewCount'))
    return t


async def stop(msg: str) -> None:
    """Input problems end SUCCEEDED with one explanatory row (never charged)."""
    Actor.log.warning(msg)
    await Actor.push_data({'error': msg, 'status': 'input_error', 'answerFound': False, 'scrapedAt': now_iso()})
    await Actor.set_status_message(msg, is_terminal=True)


class Redirects:
    """Turns google.com/goto?url=... links into the real cited URL with one plain HTTP request (no browser)."""

    def __init__(self) -> None:
        self.cache: dict[str, str | None] = {}
        self.client = httpx.AsyncClient(timeout=12, headers={'User-Agent': 'Mozilla/5.0'})
        self.sem = asyncio.Semaphore(6)

    async def resolve(self, h: str) -> str | None:
        if h in self.cache:
            return self.cache[h]
        v = goto_target(h)
        if not v:
            async with self.sem:
                try:
                    r = await self.client.get(h, follow_redirects=False)
                    v = r.headers.get('location')
                    if not v:
                        m = re.search(r'(?:URL=|href=")(https?://[^"\'>]+)', r.text[:5000])
                        v = m.group(1) if m else None
                except Exception:  # noqa: BLE001
                    v = None
        if v and is_google_redirect(v):
            v = goto_target(v)
        self.cache[h] = v
        return v

    async def close(self) -> None:
        await self.client.aclose()


async def main() -> None:
    async with Actor:
        _t_at = Actor.configuration.timeout_at
        # Graceful time budget: stop starting checks early enough to write SUMMARY/REPORT before the platform timeout.
        deadline = (_t_at.timestamp() - max(90.0, min(300.0, (_t_at.timestamp() - _time.time()) * 0.1))) if _t_at else None
        inp: dict[str, Any] = await Actor.get_input() or {}
        questions = [q.strip() for q in (inp.get('questions') or []) if isinstance(q, str) and q.strip()][:MAX_QUESTIONS]
        if not questions:
            await stop('Add at least one question customers ask, e.g. "best emergency plumber near me".')
            return
        if not (inp.get('businessName') or inp.get('mapsUrl') or inp.get('placeId')):
            await stop('Add the business to check: its name (plus city) or its Google Maps URL.')
            return
        samples = max(1, min(int(inp.get('samplesPerPoint') or 2), MAX_SAMPLES))
        include_aimode = bool(inp.get('includeAiMode', False))
        include_aio = inp.get('includeAiOverviews', True) is not False
        include_maps = inp.get('includeMapsRank', True) is not False
        max_answers = max(1, int(inp.get('maxAnswers') or 60))
        conc = max(1, min(int(inp.get('maxConcurrency') or 2), 4))
        # Each Google results page needs real CPU. Apify gives 1 CPU core per 4 GB, so at 1-2 GB two pages at once
        # only halve each other's speed (and push memory to the limit). One page per started GB, max 4.
        mem_mb = int(os.environ.get('ACTOR_MEMORY_MBYTES') or os.environ.get('APIFY_MEMORY_MBYTES') or 0)
        if mem_mb and inp.get('_noConcCap') is not True:
            conc = max(1, min(conc, mem_mb // 1024))
        gl, hl = (inp.get('country') or 'us').lower(), inp.get('language') or 'en'
        maps_kw = (inp.get('mapsKeyword') or '').strip() or None
        city = (inp.get('city') or '').strip()
        if not (include_aio or include_aimode):
            await stop('Turn on at least one engine: AI Overviews or AI Mode (beta).')
            return

        proxy_cfg = None
        pc = inp.get('proxyConfiguration') or {}
        if pc.get('useApifyProxy') or pc.get('proxyUrls'):
            proxy_cfg = await Actor.create_proxy_configuration(actor_proxy_input=pc)
        # Block fallback: when Google blocks the run's own IP, retry through Apify datacenter proxy (US, new IP per
        # try). Datacenter proxy has no per-GB fee. Off with proxyFallback=false.
        fallback_cfg = None
        if not proxy_cfg and inp.get('proxyFallback', True) is not False and Actor.is_at_home():
            try:
                fallback_cfg = await Actor.create_proxy_configuration(country_code='US' if gl_in(inp) == 'us' else None)
            except Exception as e:  # noqa: BLE001
                Actor.log.info(f'No Apify Proxy fallback available ({type(e).__name__}); using the direct IP only.')
        mc = MapsClient(proxy_url_fn=(lambda: proxy_cfg.new_url()) if proxy_cfg else None, hl=hl, gl=gl)

        # Points
        explicit = inp.get('points') or []
        center = parse_latlng(inp.get('center') or '')
        if not center and (inp.get('center') or '').strip():
            g = await mc.geocode(inp['center'])
            if not g or g.get('lat') is None:
                await stop(f'Could not find the center "{inp["center"]}". Use "lat,lng" instead.')
                return
            center = (g['lat'], g['lng'])
        if not center and city and not explicit:
            g = await mc.geocode(city)
            if g and g.get('lat') is not None:
                center = (g['lat'], g['lng'])
        if explicit:
            pts = parse_points(explicit)
            if isinstance(pts, str):
                await stop(pts)
                return
            center = center or (pts[0]['lat'], pts[0]['lng'])
        target = await resolve_target(mc, inp, center)
        if not (target.get('placeId') or target.get('hexId') or target.get('cid')):
            Actor.log.warning(f'"{target.get("name")}" was not found on Google Maps near {center or city}. Matching by '
                              'exact name only (chains may mis-match). Paste the Google Maps URL for an exact match.')
        if not explicit:
            if not center and target.get('lat') is not None:
                center = (target['lat'], target['lng'])
            if not center:
                await stop('Could not place the grid. Add a center ("lat,lng"), a city, or the Google Maps URL.')
                return
            n = int(inp.get('gridSize') or 3)
            if n % 2 == 0 or not 1 <= n <= MAX_GRID:
                await stop(f'gridSize must be odd, 1-{MAX_GRID} (1, 3, 5 or 7).')
                return
            spacing = G.spacing_from(n, inp.get('gridSpacingKm'), inp.get('radiusKm'))
            pts = G.make_grid(center[0], center[1], n, spacing)
            for p in pts:
                p['pointLabel'] = 'center' if p['isCenter'] else f'r{p["gridRow"]}c{p["gridCol"]}'
        else:
            n, spacing = None, None
        engines = (['google_aio'] if include_aio else []) + (['google_aimode'] if include_aimode else [])
        tasks = [(eng, p, q, s) for s in range(1, samples + 1) for q in questions for p in pts for eng in engines]
        Actor.log.info(f'Target: {target.get("name")} ({target.get("placeId")}, {target.get("address")}) | '
                       f'{len(pts)} points x {len(questions)} questions x {samples} samples x {len(engines)} engines = '
                       f'{len(tasks)} checks (max {max_answers} paid answers)')

        cm = Actor.get_charging_manager()
        state = {'answers': 0, 'inflight': 0, 'stop': None, 'aimode_blocks_in_row': 0, 'aimode_off': False, 'charged': {}}
        maps_cache: dict = {}
        rank_cache: dict[tuple[int, str], dict[str, Any]] = {}
        rank_locks: dict[tuple[int, str], asyncio.Lock] = {}
        rows: list[dict[str, Any]] = []
        stats_secs = {'n': 0, 'gotoSecs': 0.0, 'pageSecs': 0.0, 'enrichSecs': 0.0}
        router_box: dict[str, Router] = {}
        sem = asyncio.Semaphore(conc)
        push_lock = asyncio.Lock()
        redir = Redirects()
        shots = bool(inp.get('saveScreenshots', False))
        tsite = domain_of(target['website']) if target.get('website') else None

        async def charge(ev: str) -> bool:
            # AI Mode is beta and unpriced — never charge it, even if an event key exists.
            if ev == EV['google_aimode']:
                return False
            r = await Actor.charge(event_name=ev, count=1)
            state['charged'][ev] = state['charged'].get(ev, 0) + int(getattr(r, 'charged_count', 0) or 0)
            return bool(getattr(r, 'event_charge_limit_reached', False))

        def can_afford(ev: str) -> bool:
            if ev == EV['google_aimode']:
                return True  # unpriced beta; do not stop the run on a missing event limit
            allowed = cm.calculate_max_event_charge_count_within_limit(ev)
            return allowed is None or allowed >= 1

        async def maps_rank(p: dict[str, Any], q: str) -> dict[str, Any]:
            key = (p['pointIndex'], q)
            lock = rank_locks.setdefault(key, asyncio.Lock())
            async with lock:
                if key in rank_cache:
                    return rank_cache[key]
                out: dict[str, Any] = {'mapsRank': None, 'mapsKeyword': maps_kw or q, 'mapsTop3': [], 'mapsError': None}
                if not can_afford(EV['maps']):
                    out['mapsError'] = 'skipped: max charge reached'
                    rank_cache[key] = out
                    return out
                try:
                    places = await mc.ranked(maps_kw or q, p['lat'], p['lng'], 15.0, 20)
                    hit = next((x for x in places if is_target({**x, 'cid': cid_of(x.get('hexId'))}, target)), None)
                    out['mapsRank'] = hit['rank'] if hit else None
                    out['mapsTop3'] = [{'rank': x['rank'], 'name': x['name'], 'placeId': x.get('placeId')} for x in places[:3]]
                    out['mapsResultsChecked'] = len(places)
                    if await charge(EV['maps']):
                        state['stop'] = 'max charge reached'
                except Exception as e:  # noqa: BLE001
                    out['mapsError'] = f'{type(e).__name__}: {e}'[:200]
                rank_cache[key] = out
                return out

        async def run(browser_box: dict, task) -> None:
            eng, p, q, s = task
            async with sem:
                if state['stop']:
                    return
                if deadline is not None and _time.time() >= deadline:
                    state['stop'] = 'run time budget reached (unfinished checks were not run and not charged)'
                    return
                if router_box['r'].blocked_in_row >= BLOCK_STOP_CHECKS and not router_box['r'].proxy_ok():
                    state['stop'] = ('blocked by Google on every route (unfinished checks were not run and not charged)')
                    return
                if eng == 'google_aimode' and state['aimode_off']:
                    row_status = 'skipped_aimode_blocked'
                    raw = {'status': row_status}
                elif state['answers'] + state['inflight'] >= max_answers:
                    state['stop'] = f'maxAnswers ({max_answers}) reached'
                    return
                elif not can_afford(EV[eng]):
                    state['stop'] = 'max charge reached'
                    return
                else:
                    state['inflight'] += 1
                    try:
                        if inp.get('paceRequests', True) is not False:
                            await asyncio.sleep(0.8 + random.random() * 1.2)
                        raw = await fetch_answer(router_box['r'], q, p['lat'], p['lng'], eng, gl=gl, hl=hl,
                                                 retries=3, screenshot=shots, deadline=deadline)
                    finally:
                        state['inflight'] -= 1
                    if raw['status'] == 'error' and 'closed' in (raw.get('error') or '').lower():
                        await browser_box['relaunch']()
                    stats_secs['n'] += 1
                    for k in ('gotoSecs', 'pageSecs'):
                        stats_secs[k] += raw.get(k) or 0
                if eng == 'google_aimode' and raw['status'] != 'skipped_aimode_blocked':
                    state['aimode_blocks_in_row'] = state['aimode_blocks_in_row'] + 1 if raw['status'] == 'blocked' else 0
                    if state['aimode_blocks_in_row'] >= AIMODE_STOP_AFTER_BLOCKS:
                        state['aimode_off'] = True
                found = raw['status'] == 'answer'
                row: dict[str, Any] = {
                    'engine': eng, 'pointIndex': p['pointIndex'], 'pointLabel': p.get('pointLabel'), 'gridRow': p.get('gridRow'),
                    'gridCol': p.get('gridCol'), 'lat': p['lat'], 'lng': p['lng'], 'question': q, 'sample': s,
                    'status': raw['status'], 'answerFound': found, 'answerText': (raw.get('text') or '')[:6000] if found else None,
                    'businesses': [], 'businessesNamed': 0, 'targetMentioned': False, 'targetPosition': None,
                    'targetNameInText': False, 'targetOtherBranchMentioned': False,
                    'citedUrls': [], 'citedDomains': [], 'sourceDomains': [], 'citedDomainTypes': {}, 'targetSiteCited': False,
                    'attempts': raw.get('attempts'), 'blocks': raw.get('blocks'), 'secs': raw.get('secs'),
                    'route': (raw.get('routes') or [None])[-1], 'gotoSecs': raw.get('gotoSecs'), 'waitSecs': raw.get('waitSecs'),
                    'transferBytes': raw.get('bytes'),
                    'error': raw.get('error'), 'charged': False, 'scrapedAt': now_iso(),
                }
                if found:
                    ents = build_entities(raw)
                    _te = _time.time()
                    await enrich_with_maps(ents, mc, p['lat'], p['lng'], city, maps_cache)
                    stats_secs['enrichSecs'] += _time.time() - _te
                    row['enrichSecs'] = round(_time.time() - _te, 1)
                    for e in ents:
                        wl = e.pop('_websiteLink', None)
                        if wl and not e.get('website'):
                            e['website'] = await redir.resolve(wl) if is_google_redirect(wl) else wl
                        e['isTarget'] = is_target(e, target)
                        e['sameBrandOtherBranch'] = (not e['isTarget']) and same_brand(e, target)
                    row['businesses'] = ents
                    row['businessesNamed'] = len(ents)
                    hit = next((e for e in ents if e['isTarget']), None)
                    row['targetMentioned'] = bool(hit)
                    row['targetPosition'] = hit['position'] if hit else None
                    row['targetOtherBranchMentioned'] = any(e['sameBrandOtherBranch'] for e in ents)
                    row['targetNameInText'] = bool(target.get('name')) and norm(target['name']) in norm(raw.get('text'))
                    urls: dict[str, bool] = {}  # url -> found outside a business card (a source/citation link)
                    for l in raw.get('links') or []:
                        h, v = l['h'], None
                        if is_google_redirect(h):
                            v = await redir.resolve(h)
                        elif h.startswith('http') and not SKIP_LINK.search(h) and 'google.' not in domain_of(h):
                            v = h.split('#:~:text=')[0]
                        if v and 'google.' not in domain_of(v):
                            urls[v] = urls.get(v, False) or not l.get('inCard')
                    own = {domain_of(e['website']) for e in ents if e.get('website')}
                    row['citedUrls'] = sorted(urls)
                    row['citedDomains'] = sorted({domain_of(u) for u in urls if domain_of(u)})
                    row['sourceDomains'] = sorted({domain_of(u) for u, src in urls.items() if src and domain_of(u)})
                    row['citedDomainTypes'] = {d: domain_type(d, own) for d in row['citedDomains']}
                    row['targetSiteCited'] = bool(tsite and tsite in row['citedDomains'])
                if include_maps and raw['status'] != 'skipped_aimode_blocked':
                    row.update(await maps_rank(p, q))
                if raw.get('screenshot'):
                    k = f'shot-{eng}-{p["pointIndex"]}-{questions.index(q)}-{s}'
                    await Actor.set_value(k, raw['screenshot'], content_type='image/png')
                    row['screenshotKey'] = k
                async with push_lock:
                    if found:
                        state['answers'] += 1
                        # Only successful Google AI Overview answers are billed. AI Mode is beta and free.
                        # no-answer / blocked / error rows are always free.
                        if eng == 'google_aimode':
                            row['charged'] = False
                        else:
                            row['charged'] = True
                            if await charge(EV[eng]):
                                state['stop'] = 'max charge reached'
                    await Actor.push_data(row)
                    rows.append(row)
                Actor.log.info(f'{eng} p{p["pointIndex"]} q{questions.index(q)} s{s}: {raw["status"]}'
                               f'{" target #" + str(row["targetPosition"]) if row["targetMentioned"] else ""} ({raw.get("secs")}s: '
                               f'load {raw.get("gotoSecs")}s, answer {raw.get("waitSecs")}s, {row["route"]}, try {raw.get("attempts")})')

        async with async_playwright() as pw:
            # Headful under the image's xvfb when a display exists: headless Chromium announces itself
            # ("HeadlessChrome" UA and client hints), which Google scores as a bot.
            headless = (not os.environ.get('DISPLAY')) if inp.get('_headless') is None else bool(inp.get('_headless'))

            async def launch():
                return await pw.chromium.launch(headless=headless, args=['--disable-blink-features=AutomationControlled',
                                                                         '--disable-dev-shm-usage', '--no-first-run',
                                                                         '--disable-gpu', '--blink-settings=imagesEnabled=false',
                                                                         '--disable-extensions', '--mute-audio',
                                                                         '--disable-background-networking',
                                                                         # every check opens its own window; never throttle
                                                                         # timers of a window xvfb reports as hidden
                                                                         '--disable-background-timer-throttling',
                                                                         '--disable-backgrounding-occluded-windows',
                                                                         '--disable-renderer-backgrounding',
                                                                         '--disable-features=IntensiveWakeUpThrottling,CalculateNativeWinOcclusion'])
            box = {'b': await launch()}
            ver = (box['b'].version or '131').split('.')[0]
            # Keep the browser's own platform and version (a Windows/131 UA on Linux/14x Chromium is a mismatch).
            ua = None if not headless else (f'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) '
                                             f'Chrome/{ver}.0.0.0 Safari/537.36')
            tz = us_timezone(center[1]) if gl == 'us' and center else 'America/New_York'

            async def make_ctx(proxy_url):
                ctx = await box['b'].new_context(locale=f'{hl}-{gl.upper()}', timezone_id=tz,
                                                 viewport={'width': 1280, 'height': 900}, user_agent=ua,
                                                 proxy=playwright_proxy(proxy_url),
                                                 # Google's service worker: its requests bypass Playwright's proxy-auth
                                                 # handling (pages stalled behind Apify Proxy) and it costs CPU anyway
                                                 service_workers='block')
                await ctx.add_cookies([{'name': 'CONSENT', 'value': 'YES+cb', 'domain': '.google.com', 'path': '/'}])
                return ctx

            pcfg = proxy_cfg or fallback_cfg
            proxy_fn = (lambda sid: pcfg.new_url(session_id=sid)) if pcfg else None
            router_box['r'] = Router(make_ctx, proxy_fn, prefer_proxy=bool(proxy_cfg))

            async def relaunch():
                await router_box['r'].close()
                try:
                    await box['b'].close()
                except Exception:  # noqa: BLE001
                    pass
                box['b'] = await launch()
            box['relaunch'] = relaunch
            await asyncio.gather(*(run(box, t) for t in tasks))
            if (state['stop'] or '').startswith('blocked by Google'):
                await Actor.push_data({'status': 'skipped_blocked', 'answerFound': False, 'charged': False,
                                       'checksNotRun': max(0, len(tasks) - len(rows)), 'scrapedAt': now_iso(),
                                       'error': 'Google blocked every route several times in a row, so the run stopped '
                                                'early: these checks were not run and not charged. Rerun later, or set a '
                                                'RESIDENTIAL US proxy in the Proxy field.'})
            if (state['stop'] or '').startswith('run time budget'):
                await Actor.push_data({'status': 'skipped_time_limit', 'answerFound': False, 'charged': False,
                                       'checksNotRun': max(0, len(tasks) - len(rows)), 'scrapedAt': now_iso(),
                                       'error': 'Run time budget reached: these checks were not run and not charged. '
                                                'Raise the run timeout or lower gridSize/samplesPerPoint.'})
            await router_box['r'].close()
            await box['b'].close()
        await mc.close()
        await redir.close()

        meta = {'questions': questions, 'pointsCount': len(pts), 'gridSize': n, 'gridSpacingKm': round(spacing, 3) if spacing else None,
                'samplesPerPoint': samples, 'engines': engines, 'country': gl, 'language': hl,
                'chargedEvents': state['charged'], 'stoppedEarly': state['stop'], 'aiModeAutoStopped': state['aimode_off'],
                'mapsRequests': mc.requests, 'routing': router_box['r'].stats if router_box else None,
                'avgSecs': {k: round(v / max(1, stats_secs['n']), 1) for k, v in stats_secs.items() if k != 'n'},
                'finishedAt': now_iso()}
        summary = summarize(rows, target, meta)
        await Actor.set_value('SUMMARY', summary)
        await Actor.set_value('REPORT.html', to_html(summary), content_type='text/html; charset=utf-8')
        await Actor.set_value('REPORT.csv', to_csv(rows), content_type='text/csv; charset=utf-8')
        msg = (f'{summary["answersFound"]} AI answers from {len(rows)} checks. {target.get("name")} named in '
               f'{summary["visibilitySharePct"] if summary["visibilitySharePct"] is not None else 0}% of answers. '
               f'See REPORT.html in the key-value store.')
        if state['stop']:
            msg += f' Stopped early: {state["stop"]}.'
        if state['aimode_off']:
            msg += ' AI Mode (beta) was blocked repeatedly and switched off for the rest of the run (not charged).'
        await Actor.set_status_message(msg, is_terminal=True)
