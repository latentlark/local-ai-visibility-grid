"""Turn one AI answer into named businesses (matched to Google Maps place IDs) and cited sources."""
from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse, parse_qs, unquote

from rapidfuzz import fuzz

from .grid import haversine_km
from .maps import norm

STOP = re.compile(r'^(AI can make mistakes|AI responses may include mistakes|Sponsored|To get more features|Show all|'
                  r'Dive deeper in AI Mode|Ask anything)', re.M)
RAT = re.compile(r'^\d\.\d$')
CNT = re.compile(r'^\(\d[\d.,]*K?\)$')
BAD = {'AI Overview', 'AI Mode', 'Call', 'Directions', 'Website', 'Open', 'Closed', 'Sources', 'Show more'}
LABEL = re.compile(r'^(Location|Phone|Address|Rating|Hours|Details|Services|Why|Highlights|Specialty|Reviews?|Availability)\b.*:', re.I)


def answer_body(text: str) -> str:
    m = STOP.search(text or '')
    return text[:m.start()] if m else (text or '')


def text_names(text: str) -> list[str]:
    """Business names Google lists in the answer: a line followed by a rating + review count,
    or by 'Location:' / 'Phone:' / 'Address:' lines (AI Mode style)."""
    lines = [x.strip() for x in answer_body(text).split('\n') if x.strip()]
    out: list[str] = []
    for i, l in enumerate(lines[:-1]):
        if l in BAD or LABEL.match(l) or len(l) > 80 or l.endswith((':', '?', '.')) or RAT.match(l):
            continue
        nx = lines[i + 1]
        nx2 = lines[i + 2] if i + 2 < len(lines) else ''
        if (RAT.match(nx) and CNT.match(nx2)) or re.match(r'^(Location|Phone|Address):', nx):
            if norm(l) and norm(l) not in [norm(o) for o in out]:
                out.append(l)
    return out


def cid_of(hex_id: str | None) -> str | None:
    try:
        return str(int(hex_id.split(':')[1], 16)) if hex_id else None
    except (ValueError, IndexError):
        return None


def digits(phone: str | None) -> str | None:
    d = re.sub(r'\D', '', phone or '')
    return d[-10:] if len(d) >= 10 else None


def street_key(address: str | None) -> str | None:
    """'235 New Bern Pl #301, Raleigh, NC 27601' -> '235 new bern'."""
    if not address:
        return None
    first = norm(address.split(',')[0])
    m = re.match(r'(\d+)\s+(\w+(?:\s\w+)?)', first)
    return f'{m.group(1)} {m.group(2)}' if m else None


def build_entities(raw: dict[str, Any]) -> list[dict[str, Any]]:
    """Merge Google's own business cards (exact hex IDs) with names found in the answer text."""
    text = raw.get('text') or ''
    body = answer_body(text)
    ents: list[dict[str, Any]] = []
    seen_hex: set[str] = set()
    for c in raw.get('cards') or []:
        if not c.get('name') or c.get('hexId') in seen_hex:
            continue
        seen_hex.add(c['hexId'])
        ents.append({'name': c['name'], 'hexId': c['hexId'], 'cid': cid_of(c['hexId']), 'placeId': None,
                     'address': c.get('address'), 'phone': c.get('phone'), 'website': None,
                     '_websiteLink': c.get('websiteLink'), 'source': 'google_card'})
    for n in text_names(text):
        if any(fuzz.token_set_ratio(norm(n), norm(e['name'])) >= 90 for e in ents):
            continue
        ents.append({'name': n, 'hexId': None, 'cid': None, 'placeId': None, 'address': None, 'phone': None,
                     'website': None, 'source': 'answer_text'})
    low = body.lower()
    for e in ents:
        i = low.find(e['name'].lower())
        e['_idx'] = i if i >= 0 else 10 ** 9
    ents.sort(key=lambda e: e['_idx'])
    for i, e in enumerate(ents):
        e['position'] = i + 1
        e.pop('_idx', None)
    return ents


async def enrich_with_maps(ents: list[dict[str, Any]], mc, lat: float, lng: float, city_hint: str,
                           cache: dict, max_km: float = 60) -> None:
    """Attach Google Maps place IDs. Cards: confirm by hex ID. Text-only names: best fuzzy match near the point."""
    for e in ents:
        key = e['hexId'] or ('n:' + norm(e['name']) + f':{round(lat, 2)},{round(lng, 2)}')
        if key not in cache:
            q = e['name'] + (f' {e["address"]}' if e.get('address') else (f' {city_hint}' if city_hint else ''))
            try:
                res = await mc.ranked(q, lat, lng, 12.0, 5)
            except Exception:  # noqa: BLE001
                res = []
            hit, how = None, None
            if e['hexId']:
                hit = next((p for p in res if p.get('hexId') == e['hexId']), None)
                how = 'google_card_hex_id' if hit else 'google_card_hex_id (place ID not resolved)'
            else:
                best = None
                for p in res:
                    sc = fuzz.token_set_ratio(norm(e['name']), norm(p['name']))
                    near = p.get('lat') is None or haversine_km((p['lat'], p['lng']), (lat, lng)) <= max_km
                    if sc >= 85 and near and (best is None or sc > best[0]):
                        best = (sc, p)
                if best:
                    hit, how = best[1], f'maps_name_match ({best[0]:.0f})'
                else:
                    how = 'unmatched'
            cache[key] = (hit, how)
        hit, how = cache[key]
        e['matchMethod'] = how
        if hit:
            e['placeId'] = hit.get('placeId')
            e['hexId'] = e['hexId'] or hit.get('hexId')
            e['cid'] = e['cid'] or cid_of(hit.get('hexId'))
            e['mapsName'] = hit.get('name')
            e['address'] = e.get('address') or hit.get('address')
            e['phone'] = e.get('phone') or hit.get('phone')
            e['website'] = hit.get('website')
            e['rating'] = hit.get('rating')
            e['reviewCount'] = hit.get('reviewCount')


def is_target(e: dict[str, Any], t: dict[str, Any]) -> bool:
    """Exact IDs first (chains have many branches with the same name), then phone, then street address."""
    if t.get('placeId') and e.get('placeId'):
        return e['placeId'] == t['placeId']
    if t.get('hexId') and e.get('hexId'):
        return e['hexId'] == t['hexId']
    if t.get('cid') and e.get('cid'):
        return str(e['cid']) == str(t['cid'])
    if digits(t.get('phone')) and digits(e.get('phone')):
        return digits(t['phone']) == digits(e['phone'])
    if street_key(t.get('address')) and street_key(e.get('address')):
        return street_key(t['address']) == street_key(e['address'])
    if not (t.get('placeId') or t.get('hexId') or t.get('cid')):
        return norm(e.get('name')) == norm(t.get('name'))
    return False


GENERIC_SITES = {
    'yelp.com', 'angi.com', 'thumbtack.com', 'bbb.org', 'homeadvisor.com', 'nextdoor.com',
    'yellowpages.com', 'tripadvisor.com', 'healthgrades.com', 'zocdoc.com', 'expertise.com',
    'threebestrated.com', 'reddit.com', 'quora.com', 'facebook.com', 'instagram.com',
    'youtube.com', 'youtu.be', 'tiktok.com', 'wikipedia.org', 'twitter.com', 'x.com',
    'linkedin.com', 'linktr.ee', 'squarespace.com', 'wixsite.com', 'wix.com', 'wordpress.com',
    'google.com', 'bit.ly',
}


def _generic_domain(d: str) -> bool:
    d = (d or '').lower()
    return any(d == k or d.endswith('.' + k) for k in GENERIC_SITES)


def same_brand(e: dict[str, Any], t: dict[str, Any]) -> bool:
    """Same chain, different location. Name similarity first; else a shared business website domain."""
    if not t.get('name'):
        return False
    if fuzz.token_set_ratio(norm(e.get('name') or ''), norm(t['name'])) >= 90:
        return True
    ed, td = domain_of(e.get('website') or ''), domain_of(t.get('website') or '')
    return bool(ed and td and ed == td and not _generic_domain(ed))


SKIP_LINK = re.compile(r'^(tel:|javascript:|mailto:)|//(www\.)?google\.[a-z.]+/(search|maps|preferences|setprefs|advanced_search|webhp)|'
                       r'gstatic\.com|accounts\.google|support\.google\.com|policies\.google|youtube\.com/redirect')


def goto_target(h: str) -> str | None:
    """Some Google links carry the target in ?url=/q= as plain text; others need an HTTP redirect lookup."""
    q = parse_qs(urlparse(h).query)
    for k in ('url', 'q'):
        v = (q.get(k) or [None])[0]
        if v and v.startswith('http'):
            return unquote(v)
    return None


def is_google_redirect(h: str) -> bool:
    return bool(re.match(r'https?://(www\.)?google\.[a-z.]+/(goto|url)\?', h))


DIR_TYPES = {'yelp.com': 'directory/review', 'angi.com': 'directory/review', 'thumbtack.com': 'directory/review',
             'bbb.org': 'directory/review', 'homeadvisor.com': 'directory/review', 'nextdoor.com': 'directory/review',
             'yellowpages.com': 'directory/review', 'tripadvisor.com': 'directory/review', 'healthgrades.com': 'directory/review',
             'zocdoc.com': 'directory/review', 'expertise.com': 'listicle', 'threebestrated.com': 'listicle',
             'reddit.com': 'forum', 'quora.com': 'forum', 'facebook.com': 'social', 'instagram.com': 'social',
             'youtube.com': 'video', 'tiktok.com': 'video', 'wikipedia.org': 'reference'}


def domain_of(u: str) -> str:
    return urlparse(u).netloc.lower().removeprefix('www.').removeprefix('m.')


def domain_type(d: str, own_sites: set[str]) -> str:
    if d in own_sites:
        return 'named business site'
    for k, v in DIR_TYPES.items():
        if d == k or d.endswith('.' + k):
            return v
    return 'other'
