"""SUMMARY record, CSV and a one-page HTML report built from the dataset rows."""
from __future__ import annotations

import csv
import html
import io
from collections import Counter, defaultdict
from typing import Any


def pct(a: int, b: int) -> float | None:
    return round(100 * a / b, 1) if b else None


def summarize(rows: list[dict[str, Any]], target: dict[str, Any], meta: dict[str, Any]) -> dict[str, Any]:
    by_engine: dict[str, dict[str, Any]] = {}
    for eng in sorted({r['engine'] for r in rows}):
        er = [r for r in rows if r['engine'] == eng]
        ans = [r for r in er if r['answerFound']]
        by_engine[eng] = {
            'attempted': len(er), 'answers': len(ans),
            'noAnswer': sum(1 for r in er if r['status'] == 'no_answer'),
            'blocked': sum(1 for r in er if r['status'] == 'blocked'),
            'errors': sum(1 for r in er if r['status'] == 'error'),
            'answerRatePct': pct(len(ans), len(er)),
            'targetMentioned': sum(1 for r in ans if r['targetMentioned']),
            'visibilitySharePct': pct(sum(1 for r in ans if r['targetMentioned']), len(ans)),
        }

    def share(groups: dict[Any, list[dict[str, Any]]], label_fn) -> list[dict[str, Any]]:
        out = []
        for k, g in groups.items():
            ans = [r for r in g if r['answerFound']]
            ranks = [r['mapsRank'] for r in g if r.get('mapsRank')]
            out.append({**label_fn(k, g), 'answers': len(ans), 'attempted': len(g),
                        'targetMentioned': sum(1 for r in ans if r['targetMentioned']),
                        'visibilitySharePct': pct(sum(1 for r in ans if r['targetMentioned']), len(ans)),
                        'bestPosition': min((r['targetPosition'] for r in ans if r.get('targetPosition')), default=None),
                        'mapsRankBest': min(ranks) if ranks else None})
        return out

    pts: dict[Any, list] = defaultdict(list)
    qs: dict[Any, list] = defaultdict(list)
    for r in rows:
        pts[(r['pointIndex'], r.get('pointLabel'), r['lat'], r['lng'])].append(r)
        qs[r['question']].append(r)
    per_point = share(pts, lambda k, g: {'pointIndex': k[0], 'pointLabel': k[1], 'lat': k[2], 'lng': k[3]})
    per_point.sort(key=lambda x: x['pointIndex'])
    per_q = share(qs, lambda k, g: {'question': k})

    comp: Counter = Counter()
    comp_info: dict[str, dict[str, Any]] = {}
    dom: Counter = Counter()
    dom_type: dict[str, str] = {}
    ans_all = [r for r in rows if r['answerFound']]
    for r in ans_all:
        seen = set()
        for e in r['businesses']:
            if e.get('isTarget'):
                continue
            key = e.get('placeId') or e.get('hexId') or e['name'].lower()
            if key in seen:
                continue
            seen.add(key)
            comp[key] += 1
            comp_info.setdefault(key, {'name': e.get('mapsName') or e['name'], 'placeId': e.get('placeId'),
                                       'rating': e.get('rating'), 'reviewCount': e.get('reviewCount')})
        for d in set(r['citedDomains']):
            dom[d] += 1
            dom_type[d] = r['citedDomainTypes'].get(d, 'other')
    top_comp = [{**comp_info[k], 'answersMentioning': n, 'sharePct': pct(n, len(ans_all))} for k, n in comp.most_common(15)]
    top_dom = [{'domain': d, 'type': dom_type[d], 'answersCiting': n, 'sharePct': pct(n, len(ans_all))} for d, n in dom.most_common(20)]
    ranks = [r['mapsRank'] for r in rows if r.get('mapsRank')]
    return {
        **meta, 'target': target,
        'answersFound': len(ans_all), 'rowsTotal': len(rows),
        'visibilitySharePct': pct(sum(1 for r in ans_all if r['targetMentioned']), len(ans_all)),
        'engines': by_engine, 'perPoint': per_point, 'perQuestion': per_q,
        'topCompetitors': top_comp, 'topCitedDomains': top_dom,
        'targetSiteCited': sum(1 for r in ans_all if r.get('targetSiteCited')),
        'mapsRankAverage': round(sum(ranks) / len(ranks), 2) if ranks else None,
    }


CSV_COLS = ['engine', 'pointIndex', 'pointLabel', 'lat', 'lng', 'question', 'sample', 'status', 'answerFound',
            'targetMentioned', 'targetPosition', 'businessesNamed', 'mapsRank', 'topBusiness', 'citedDomainsList']


def to_csv(rows: list[dict[str, Any]]) -> str:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=CSV_COLS, extrasaction='ignore')
    w.writeheader()
    for r in rows:
        w.writerow({**r, 'businessesNamed': len(r['businesses']),
                    'topBusiness': r['businesses'][0]['name'] if r['businesses'] else '',
                    'citedDomainsList': ' '.join(r['citedDomains'])})
    return buf.getvalue()


def _color(v: float | None) -> str:
    if v is None:
        return '#cbd5e1'
    if v >= 66:
        return '#16a34a'
    if v >= 33:
        return '#eab308'
    if v > 0:
        return '#f97316'
    return '#dc2626'


def to_html(s: dict[str, Any]) -> str:
    e = html.escape
    t = s['target']
    pts = s['perPoint']
    grid = s.get('gridSize')
    cells = ''
    if grid and len(pts) == grid * grid:
        for row in range(grid):
            cells += '<tr>' + ''.join(
                f'<td style="background:{_color(p["visibilitySharePct"])}">{"—" if p["visibilitySharePct"] is None else str(round(p["visibilitySharePct"])) + "%"}'
                f'<br><small>Maps #{p["mapsRankBest"] or "20+"}</small></td>'
                for p in pts[row * grid:(row + 1) * grid]) + '</tr>'
        heat = f'<table class="heat">{cells}</table><p class="note">Top row = north, left = west. % = share of AI answers that name the business. Maps # = best organic Maps rank at that point.</p>'
    else:
        heat = ''

    def table(rows: list[dict[str, Any]], cols: list[tuple[str, str]]) -> str:
        h = ''.join(f'<th>{e(c[1])}</th>' for c in cols)
        b = ''.join('<tr>' + ''.join(f'<td>{e("" if r.get(c[0]) is None else str(r.get(c[0])))}</td>' for c in cols) + '</tr>' for r in rows)
        return f'<table><tr>{h}</tr>{b}</table>'

    eng = ''.join(f'<li><b>{e(k)}</b>: {v["answers"]}/{v["attempted"]} answers, named in {v["targetMentioned"]} '
                  f'({v["visibilitySharePct"] if v["visibilitySharePct"] is not None else "—"}%)</li>' for k, v in s['engines'].items())
    return f'''<!doctype html><html><head><meta charset="utf-8"><title>Local AI Visibility: {e(t.get("name") or "")}</title>
<style>body{{font-family:system-ui,Arial;margin:24px;color:#0f172a;max-width:1000px}}h1{{color:#0f766e}}h2{{color:#b7791f;margin-top:28px}}
table{{border-collapse:collapse;margin:8px 0}}td,th{{border:1px solid #e2e8f0;padding:6px 8px;font-size:13px;text-align:left}}
.heat td{{width:90px;height:60px;text-align:center;color:#fff;font-weight:700}}.note{{color:#64748b;font-size:12px}}</style></head><body>
<h1>Local AI Visibility Grid: {e(t.get("name") or "")}</h1>
<p>{e(t.get("address") or "")} · Place ID {e(t.get("placeId") or "n/a")} · {s["answersFound"]} AI answers from {s["rowsTotal"]} checks · run {e(s.get("finishedAt") or "")}</p>
<p><b>AI visibility share: {s["visibilitySharePct"] if s["visibilitySharePct"] is not None else "—"}%</b> of AI answers name the business. Its own website was cited in {s["targetSiteCited"]} answers. Average Maps rank: {s["mapsRankAverage"] or "not in top 20"}.</p>
<ul>{eng}</ul>
<h2>Grid</h2>{heat}{table(pts, [("pointIndex", "#"), ("pointLabel", "Point"), ("lat", "Lat"), ("lng", "Lng"), ("answers", "Answers"), ("targetMentioned", "Named"), ("visibilitySharePct", "Share %"), ("bestPosition", "Best position"), ("mapsRankBest", "Maps rank")])}
<h2>Per question</h2>{table(s["perQuestion"], [("question", "Question"), ("answers", "Answers"), ("targetMentioned", "Named"), ("visibilitySharePct", "Share %"), ("mapsRankBest", "Best Maps rank")])}
<h2>Competitors AI names most</h2>{table(s["topCompetitors"], [("name", "Business"), ("answersMentioning", "Answers"), ("sharePct", "Share %"), ("rating", "Rating"), ("reviewCount", "Reviews")])}
<h2>Sites AI cites most</h2>{table(s["topCitedDomains"], [("domain", "Domain"), ("type", "Type"), ("answersCiting", "Answers"), ("sharePct", "Share %")])}
<p class="note">Logged-out Google answers, location pinned per grid point. AI answers change between runs; use several samples per point. Made with LatentLark Local AI Visibility Grid on Apify.</p>
</body></html>'''
