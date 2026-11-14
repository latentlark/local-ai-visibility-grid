"""Load one Google AI Overview / AI Mode answer in a real browser and extract text, business cards and links.

Logged-out, location pinned with Google's `uule` parameter.
"""
from __future__ import annotations

import asyncio
import time
import urllib.parse
from typing import Any
from urllib.parse import unquote, urlparse

from .maps import uule_for

UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) '
      'Chrome/131.0.0.0 Safari/537.36')  # legacy; the browser now keeps its own matching UA (see main.make_ctx)

# Runs in the page. Returns {found, text, cards[], links[]} for the AI answer block only.
EXTRACT_JS = r'''(engine)=>{
 let root=null;
 if(engine==='google_aio'){
   // Native XPath over text nodes instead of innerText on every div/span (that was O(page^2) text building).
   let heading=null;
   const xs=document.evaluate("//text()[normalize-space(.)='AI Overview']",document,null,XPathResult.ORDERED_NODE_SNAPSHOT_TYPE,null);
   for(let i=0;i<xs.snapshotLength&&!heading;i++){
     // outermost element that is still just the heading, e.g. <div role=heading><strong>AI Overview</strong></div>
     let e=xs.snapshotItem(i).parentElement;
     while(e&&e.parentElement&&(e.parentElement.textContent||'').trim()==='AI Overview') e=e.parentElement;
     if(!e||!e.getClientRects().length) continue;
     heading=e;
   }
   if(!heading) return {found:false, reason:'no_ai_overview'};
   const learn=document.querySelector('a[href*="p=ai_overviews"]');
   root=heading;
   if(learn){while(root&&!root.contains(learn)) root=root.parentElement;}
   else {for(let i=0;i<6&&root.parentElement;i++) root=root.parentElement;}
 } else {
   root=document.querySelector('[role=main]')||document.body;
 }
 if(!root) return {found:false, reason:'no_root'};
 const SKIPTAG=new Set(['STYLE','SCRIPT','NOSCRIPT','svg','SVG','IMG']);
 const BADN=new Set(['Call','Directions','Website','Open','Closed','Open 24 hours','Sponsored','Order','Book','Menu']);
 const leaves=(node)=>{const out=[];const rec=(e)=>{if(SKIPTAG.has(e.tagName)) return; if(e.childElementCount===0){const t=(e.textContent||'').trim(); if(t) out.push(t); return;} for(const c of e.children) rec(c);}; rec(node); return out;};
 const hasRating=(node)=>leaves(node).some(t=>/^\d\.\d$/.test(t));
 const dirs=[...root.querySelectorAll('a[href*="/maps/dir/"]')];
 const cards=[]; const cardNodes=[];
 for(const a of dirs){
   const hex=(a.href.match(/(0x[0-9a-f]+:0x[0-9a-f]+)/)||[])[1]; if(!hex) continue;
   let addr=null; const m=a.href.match(/\/maps\/dir\/\/([^/]*)\/data=/); if(m){try{addr=decodeURIComponent(m[1].replace(/\+/g,' '))}catch(e){addr=m[1]}}
   if(addr==="''"||addr==='') addr=null;
   let n=a, node=null;
   for(let i=0;i<16&&n.parentElement;i++){
     const p=n.parentElement;
     if(p.querySelectorAll('a[href*="/maps/dir/"]').length>1) break;
     n=p; if(hasRating(n)){node=n; break;}
   }
   node=node||n;
   const L=leaves(node).filter(t=>t.length<=90);
   const ri=L.findIndex(t=>/^\d\.\d$/.test(t));
   let name=null;
   if(ri>0) name=L[ri-1]; else name=L.find(t=>!BADN.has(t)&&!/^[\d.()+, -]+$/.test(t))||null;
   const tel=node.querySelector('a[href^="tel:"]');
   const web=[...node.querySelectorAll('a[href]')].find(x=>(x.textContent||'').trim()==='Website');
   cards.push({hexId:hex,name,address:addr,phone:tel?tel.getAttribute('href').slice(4):null,websiteLink:web?web.href:null});
   cardNodes.push(node);
 }
 const links=[...root.querySelectorAll('a[href]')].map(a=>({t:(a.innerText||'').trim().slice(0,160),h:a.href,inCard:cardNodes.some(c=>c.contains(a))}));
 return {found:true,text:root.innerText||'',cards,links};
}'''


def playwright_proxy(url: str | None) -> dict | None:
    """Turn an HTTP proxy URL into Playwright's proxy dict. Never log the URL (it can carry a password)."""
    if not url:
        return None
    u = urlparse(url)
    if not u.hostname:
        return None
    port = u.port or (443 if (u.scheme or 'http') == 'https' else 80)
    cfg: dict = {'server': f'{u.scheme or "http"}://{u.hostname}:{port}'}
    if u.username:
        cfg['username'] = unquote(u.username)
    if u.password:
        cfg['password'] = unquote(u.password)
    return cfg


# Blocked inside the browser (CDP Network.setBlockedURLs), not by a Python route handler: routing every request
# through Python cost a CDP round trip per request, which on a quarter CPU core made each page take 20-40 s.
BLOCK_PATTERNS = ['*.png*', '*.jpg*', '*.jpeg*', '*.gif*', '*.webp*', '*.svg*', '*.ico*', '*.woff*', '*.ttf*',
                  '*.mp4*', '*.webm*', '*encrypted-tbn*', '*/gen_204*', '*/client_204*', '*/log?*', '*/maps/vt*',
                  '*maps.googleapis.com*', '*googleadservices*', '*doubleclick*', '*youtube.com*', '*ytimg.com*',
                  '*fonts.googleapis.com*', '*fonts.gstatic.com*', '*lh3.googleusercontent.com*', '*lh5.googleusercontent.com*']


def lean_should_continue(url: str, rtype: str) -> bool:
    """Keep document/XHR/fetch plus Google scripts; drop images, CSS, fonts, media and third-party junk."""
    if rtype in ('image', 'media', 'font', 'stylesheet', 'ping', 'manifest'):
        return False
    h = (urlparse(url).hostname or '').lower()
    googleish = any(h == d or h.endswith('.' + d) for d in
                    ('google.com', 'gstatic.com', 'googleapis.com', 'googleusercontent.com'))
    if rtype in ('document', 'xhr', 'fetch', 'script', 'websocket') and googleish:
        return True
    return rtype in ('document', 'xhr', 'fetch')


def search_url(query: str, lat: float, lng: float, engine: str, gl: str = 'us', hl: str = 'en') -> str:
    qs = {'q': query, 'hl': hl, 'gl': gl, 'uule': uule_for(lat, lng)}
    if engine == 'google_aimode':
        qs['udm'] = '50'
    return 'https://www.google.com/search?' + urllib.parse.urlencode(qs)


def is_blocked(url: str, html_head: str) -> bool:
    return '/sorry/' in url or 'unusual traffic' in html_head or 'detected unusual' in html_head


def classify_status(engine: str, r: dict[str, Any]) -> str:
    """answer = a real AI answer we can charge for; no_answer = Google showed none (free)."""
    if not r.get('found'):
        return 'no_answer'
    text = r.get('text') or ''
    if engine == 'google_aimode':
        return 'answer' if len(text) > 400 and ('AI Mode' in text or r.get('cards') or len(text) > 1200) else 'no_answer'
    return 'answer' if len(text) > 300 else 'no_answer'


# Wait for the AI Overview inside the page in ONE call (no CDP round trip per poll). Finds the heading with a native
# XPath over text nodes and measures textContent (no layout); the heavy EXTRACT_JS runs once at the end.
WAIT_JS = r"""async ([engine, maxMs, noAnswerMs, step]) => {
  const sleep = (ms) => new Promise(r => setTimeout(r, ms));
  const findRoot = () => {
    if (engine !== 'google_aio') return document.querySelector('[role=main]') || document.body;
    const it = document.evaluate("//text()[normalize-space(.)='AI Overview']", document, null, XPathResult.ORDERED_NODE_SNAPSHOT_TYPE, null);
    for (let i = 0; i < it.snapshotLength; i++) {
      let e = it.snapshotItem(i).parentElement;  // e.g. <div role=heading><strong>AI Overview</strong></div>
      while (e && e.parentElement && (e.parentElement.textContent || '').trim() === 'AI Overview') e = e.parentElement;
      if (!e || !e.getClientRects().length) continue;
      let root = e; const learn = document.querySelector('a[href*="p=ai_overviews"]');
      if (learn) { while (root && !root.contains(learn)) root = root.parentElement; }
      else { for (let k = 0; k < 6 && root.parentElement; k++) root = root.parentElement; }
      if (root) return root;
    }
    return null;
  };
  const t0 = Date.now(); let last = -1, stable = 0, seen = false;
  while (Date.now() - t0 < maxMs) {
    await sleep(step);
    const root = findRoot();
    if (!root) { if (!seen && Date.now() - t0 >= noAnswerMs) break; continue; }
    seen = true;
    const n = (root.textContent || '').length;
    if (n > 300 && n === last) { if (++stable >= 2) break; } else stable = 0;
    last = n;
  }
  return {seen, ms: Date.now() - t0};
}"""


async def wait_for_answer(pg, engine: str) -> dict[str, Any]:
    """Wait (inside the page) until the AI block exists and its text stops growing, then extract once."""
    max_ms = 22000 if engine == 'google_aimode' else 13000
    await pg.evaluate(WAIT_JS, [engine, max_ms, 8000, 700])
    return await pg.evaluate(EXTRACT_JS, engine)


async def load_once(ctx, query: str, lat: float, lng: float, engine: str, gl: str, hl: str,
                    screenshot: bool) -> dict[str, Any]:
    """One attempt in one context. Returns {status, ...} with timings; never raises."""
    out: dict[str, Any] = {'status': 'error', 'bytes': 0, 'error': None}
    pg = await ctx.new_page()
    t0 = time.time()
    try:
        try:
            cdp = await ctx.new_cdp_session(pg)
            await cdp.send('Network.enable')
            await cdp.send('Network.setBlockedURLs', {'urls': BLOCK_PATTERNS})
        except Exception:  # noqa: BLE001 - non-Chromium: load everything
            pass
        # domcontentloaded, not load: 'load' waited for every Google script and made checks take ~40 s.
        await pg.goto(search_url(query, lat, lng, engine, gl, hl), timeout=30000, wait_until='domcontentloaded')
        out['gotoSecs'] = round(time.time() - t0, 1)
        if is_blocked(pg.url, ''):
            out['status'] = 'blocked'
        else:
            head = (await pg.content())[:30000]
            if is_blocked(pg.url, head):
                out['status'] = 'blocked'
            else:
                r = await wait_for_answer(pg, engine)
                if r.get('found'):
                    clicked = False
                    for label in ('Show more', 'Show all'):
                        try:
                            for bt in await pg.get_by_role('button', name=label).all():
                                if await bt.is_visible():
                                    await bt.click(timeout=2000)
                                    clicked = True
                        except Exception:  # noqa: BLE001
                            pass
                    if clicked:
                        await pg.wait_for_timeout(1000)
                        r = await pg.evaluate(EXTRACT_JS, engine)
                out.update(r)
                out['status'] = classify_status(engine, r)
        out['waitSecs'] = round(time.time() - t0 - out.get('gotoSecs', 0), 1)
        if screenshot:
            out['screenshot'] = await pg.screenshot()
    except Exception as e:  # noqa: BLE001
        out['status'] = 'error'
        out['error'] = f'{type(e).__name__}: {e}'.split('\n')[0][:300]
    finally:
        try:
            out['bytes'] = int(await pg.evaluate("()=>performance.getEntries().reduce((a,e)=>a+(e.transferSize||0),0)"))
        except Exception:  # noqa: BLE001
            out['bytes'] = 0
        out['pageSecs'] = round(time.time() - t0, 1)
        try:
            await pg.close()
        except Exception:  # noqa: BLE001
            pass
    return out


async def fetch_answer(router, query: str, lat: float, lng: float, engine: str, *, gl: str = 'us', hl: str = 'en',
                       retries: int = 3, screenshot: bool = False, deadline: float | None = None) -> dict[str, Any]:
    """Load one answer. A blocked try never reuses its context or proxy session: the router hands the next try a
    fresh cookie jar (and a new proxy IP when proxies are available)."""
    rec: dict[str, Any] = {'status': 'error', 'attempts': 0, 'blocks': 0, 'bytes': 0, 'error': None, 'routes': []}
    t0 = time.time()
    for att in range(retries):
        if deadline is not None and time.time() >= deadline:
            break
        w = router.wait_secs()
        if w > 0:
            if deadline is not None and time.time() + w >= deadline:
                break
            await asyncio.sleep(w)
        rec['attempts'] += 1
        ctx, kind, owned, gen = await router.acquire()
        rec['routes'].append(kind)
        try:
            r = await load_once(ctx, query, lat, lng, engine, gl, hl, screenshot)
        finally:
            if owned:
                try:
                    await ctx.close()
                except Exception:  # noqa: BLE001
                    pass
        await router.report(kind, gen, r['status'])
        rec['bytes'] += r.pop('bytes', 0)
        if r['status'] == 'blocked':
            rec['blocks'] += 1
        rec.update(r)
        if rec['status'] in ('answer', 'no_answer'):
            break
    router.check_done(rec['status'])
    rec['secs'] = round(time.time() - t0, 1)
    return rec
