"""(Shared with LatentLark google-maps-rank-tracker.) Google Maps search results for a keyword at a given lat/lng (the same JSON the Maps web app loads).

No login, no API key. One request returns up to 20 places in the order Maps shows them for that map view.
"""
from __future__ import annotations

import base64
import json
import re
import time
import urllib.parse
from typing import Any

import httpx


def redact_secrets(s: str) -> str:
    """Strip proxy credentials / Apify proxy passwords from any string before logging."""
    if not s:
        return s
    s = re.sub(r'apify_proxy_[A-Za-z0-9]+', 'apify_proxy_[REDACTED]', s)
    s = re.sub(r'(https?://[^:\s/@]+):([^@\s/]+)@', r'\1:[REDACTED]@', s)
    return s

UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36'
PAGE = 20
# Captured from google.com/maps/search (2026-10-26). {dist}/{lng}/{lat} = map view, {zoom}, {offset} = '!8iNN' for page 2+.
PB_TEMPLATE = open(__file__.replace('maps.py', 'pb_template.txt')).read().strip()
BASE_DIST_M = 27567.545  # "altitude" Maps sends for zoom 13.1 at 1024x768


def uule_for(lat: float, lng: float, radius_m: int = 65000) -> str:
    """Google 'uule' v2 location: tells Google the searcher stands at lat/lng (overrides IP location)."""
    s = (f'role: CURRENT_LOCATION\nproducer: DEVICE_LOCATION\nradius: {radius_m}\nlatlng <\n'
         f'  latitude_e7: {int(round(lat * 1e7))}\n  longitude_e7: {int(round(lng * 1e7))}\n>\n'
         f'timestamp: {int(time.time() * 1e6)}\n')
    return 'a ' + base64.b64encode(s.encode()).decode()


class BlockedError(Exception):
    pass


def _g(x: Any, *path: int) -> Any:
    for i in path:
        if not isinstance(x, list) or i >= len(x):
            return None
        x = x[i]
    return x


def dist_for_zoom(zoom: float) -> float:
    return BASE_DIST_M * 2 ** (13.1 - zoom)


def build_pb(lat: float, lng: float, zoom: float = 13.0, offset: int = 0) -> str:
    return PB_TEMPLATE.format(dist=round(dist_for_zoom(zoom), 3), lng=lng, lat=lat, zoom=zoom,
                              offset=f'!8i{offset}' if offset else '')


def parse_place(x: list) -> dict[str, Any] | None:
    name = _g(x, 11)
    if not isinstance(name, str):
        return None
    cats = _g(x, 13)
    hexid = _g(x, 10)
    return {
        'name': name,
        'placeId': _g(x, 78),
        'hexId': hexid if isinstance(hexid, str) else None,
        'lat': _g(x, 9, 2), 'lng': _g(x, 9, 3),
        'rating': _g(x, 4, 7), 'reviewCount': _g(x, 4, 8),
        'category': cats[0] if isinstance(cats, list) and cats else None,
        'address': _g(x, 39) if isinstance(_g(x, 39), str) else None,
        'website': _g(x, 7, 0) if isinstance(_g(x, 7, 0), str) else None,
        'phone': _g(x, 178, 0, 3) if isinstance(_g(x, 178, 0, 3), str) else None,
        'isAd': False,
    }


def _find_ad_core(x: Any, depth: int = 0) -> list | None:
    """Ad card core: [tracking, [None, None, lat, lng], name, headline, ..., position(7), ..., hexId(9), ...]."""
    if not isinstance(x, list) or depth > 8:
        return None
    if (len(x) > 9 and isinstance(_g(x, 2), str) and isinstance(_g(x, 1), list)
            and isinstance(_g(x, 1, 2), (int, float)) and isinstance(_g(x, 1, 3), (int, float))):
        return x
    for y in x:
        r = _find_ad_core(y, depth + 1)
        if r:
            return r
    return None


def parse_ad(item: list) -> dict[str, Any] | None:
    """Sponsored card in the results list (item[1] empty, item[5] = ad block with /aclk links)."""
    ad = _g(item, 5)
    if not isinstance(ad, list) or '/aclk' not in json.dumps(ad)[:20000]:
        return None
    core = _find_ad_core(ad)
    if not core:
        return None
    hexid = _g(core, 9)
    return {
        'name': core[2],
        'hexId': hexid if isinstance(hexid, str) else None,
        'placeId': None,
        'lat': _g(core, 1, 2), 'lng': _g(core, 1, 3),
        'headline': _g(ad, 0) if isinstance(_g(ad, 0), str) else None,
        'displayUrl': _g(ad, 3) if isinstance(_g(ad, 3), str) else None,
        'isAd': True,
    }


def parse_response(text: str, ads_out: list | None = None) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """Returns (ranked places, single place if Google jumped straight to one place)."""
    if text.startswith(")]}'"):
        text = text[text.index('\n') + 1:]
    text = text.removesuffix('/*""*/')
    d = json.loads(text)
    if isinstance(d, dict) and 'd' in d:
        d = json.loads(d['d'][4:])
    places = []
    for item in _g(d, 64) or []:
        p = parse_place(_g(item, 1))
        if p:
            places.append(p)  # organic only: sponsored cards have no item[1]
        elif ads_out is not None:
            ad = parse_ad(item)
            if ad:
                ad['adPosition'] = len(ads_out) + 1
                ads_out.append(ad)
    single = parse_place(_g(d, 0, 1, 0, 14)) if not places else None
    return places, single


class MapsClient:
    def __init__(self, proxy_url_fn=None, hl: str = 'en', gl: str = 'us', timeout: float = 30, pin_location: bool = True):
        self.proxy_url_fn = proxy_url_fn
        self.pin_location = pin_location
        self.base = 'https://www.google.com/search'
        self.bytes = 0
        self.hl, self.gl = hl, gl
        self.timeout = timeout
        self.requests = 0
        self._client: httpx.AsyncClient | None = None
        self._proxy: str | None = None

    async def _get_client(self, rotate: bool = False) -> httpx.AsyncClient:
        if self._client is None or rotate:
            if self._client is not None:
                await self._client.aclose()
            self._proxy = await self.proxy_url_fn() if self.proxy_url_fn else None
            self._client = httpx.AsyncClient(timeout=self.timeout, proxy=self._proxy, follow_redirects=True,
                                             headers={'User-Agent': UA, 'Accept-Language': f'{self.hl},en;q=0.8'},
                                             cookies={'CONSENT': 'YES+cb'})
        return self._client

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()

    async def search(self, query: str, lat: float, lng: float, zoom: float = 13.0, offset: int = 0,
                     retries: int = 3, pin: bool | None = None,
                     ads_out: list | None = None) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
        params = {'tbm': 'map', 'authuser': '0', 'hl': self.hl, 'gl': self.gl, 'q': query,
                  'pb': '!1s' + urllib.parse.quote(query) + build_pb(lat, lng, zoom, offset)}
        if (self.pin_location if pin is None else pin) and (lat or lng):
            params['uule'] = uule_for(lat, lng)
        last: Exception | None = None
        for attempt in range(retries):
            cl = await self._get_client(rotate=attempt > 0)
            try:
                self.requests += 1
                r = await cl.get(self.base, params=params)
                self.bytes += len(r.content)
                if r.status_code in (429, 403) or '/sorry/' in str(r.url) or 'unusual traffic' in r.text[:5000]:
                    raise BlockedError(redact_secrets(f'HTTP {r.status_code} {r.url}'))
                r.raise_for_status()
                return parse_response(r.text, ads_out)
            except (BlockedError, httpx.HTTPError, ValueError) as e:
                msg = redact_secrets(str(e))
                if isinstance(e, BlockedError):
                    last = BlockedError(msg)
                else:
                    last = type(e)(msg)
        raise last or BlockedError('unknown')

    async def ranked(self, query: str, lat: float, lng: float, zoom: float, depth: int,
                     ads_out: list | None = None) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        seen: set[str] = set()
        offset = 0
        while len(out) < depth:
            places, single = await self.search(query, lat, lng, zoom, offset,
                                               ads_out=ads_out if offset == 0 else None)
            if single and not out:
                places = [single]
            new = [p for p in places if (p.get('placeId') or p['name']) not in seen]
            for p in new:
                seen.add(p.get('placeId') or p['name'])
            out += new
            if len(places) < PAGE or not new:
                break
            offset += PAGE
        for i, p in enumerate(out[:depth]):
            p['rank'] = i + 1
        return out[:depth]

    async def geocode(self, text: str) -> dict[str, Any] | None:
        places, single = await self.search(text, 0, 0, zoom=3, pin=False)
        return single or (places[0] if places else None)


_NORM = re.compile(r'[^a-z0-9]+')


def norm(s: str | None) -> str:
    return _NORM.sub(' ', (s or '').lower()).strip()


def parse_maps_url(url: str) -> dict[str, str | None]:
    """Extract place id / hex id / name from a Google Maps place URL."""
    u = urllib.parse.unquote(url or '')
    pid = re.search(r'(?:place_id[:=]|!19s|!16s%2Fg|query_place_id=)(ChIJ[\w-]+)', u)
    hexid = re.search(r'(0x[0-9a-f]+:0x[0-9a-f]+)', u)
    name = re.search(r'/maps/place/([^/@?]+)', u)
    cid = re.search(r'[?&]cid=(\d+)', u)
    if cid and not hexid:
        hexid = None
    return {'placeId': pid.group(1) if pid else None, 'hexId': hexid.group(1) if hexid else None,
            'name': name.group(1).replace('+', ' ') if name else None, 'cid': cid.group(1) if cid else None}


def matches(p: dict[str, Any], target: dict[str, Any]) -> bool:
    if target.get('placeId') and p.get('placeId') == target['placeId']:
        return True
    if target.get('hexId') and p.get('hexId') == target['hexId']:
        return True
    if target.get('cid') and p.get('hexId') and int(p['hexId'].split(':')[1], 16) == int(target['cid']):
        return True
    if not (target.get('placeId') or target.get('hexId') or target.get('cid')) and target.get('name'):
        return norm(p.get('name')) == norm(target['name'])
    if p.get('isAd') and not target.get('hexId') and target.get('name'):
        return norm(p.get('name')) == norm(target['name'])  # ads carry no Place ID
    return False
