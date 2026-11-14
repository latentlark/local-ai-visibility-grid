"""Load one Google AI Overview / AI Mode answer in a real browser and extract text, business cards and links.

Logged-out, one fresh browser context per answer, location pinned with Google's `uule` parameter.
"""
from __future__ import annotations

import time
import urllib.parse
from typing import Any
from urllib.parse import unquote, urlparse

from .maps import uule_for

UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) '
      'Chrome/131.0.0.0 Safari/537.36')

# Runs in the page. Returns {found, text, cards[], links[]} for the AI answer block only.
EXTRACT_JS = r'''(engine)=>{
 let root=null;
 if(engine==='google_aio'){
   const heading=[...document.querySelectorAll('h1,h2,div,span')].find(e=>e.childElementCount<=1&&e.innerText&&e.innerText.trim()==='AI Overview');
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


async def wait_for_answer(pg, engine: str) -> dict[str, Any]:
    """Poll until the AI block exists and its text stops growing (AI answers stream in)."""
    limit = 24 if engine == 'google_aimode' else 12
    last, stable, r = -1, 0, {'found': False}
    for i in range(limit):
        await pg.wait_for_timeout(1000)
        r = await pg.evaluate(EXTRACT_JS, engine)
        if not r.get('found') and engine == 'google_aio' and i >= 6:
            break  # no AI Overview on this page
        n = len(r.get('text') or '') if r.get('found') else -1
        if n > 300 and n == last:
            stable += 1
            if stable >= 2:
                break
        else:
            stable = 0
        last = n
    return r


async def fetch_answer(browser, query: str, lat: float, lng: float, engine: str, *, gl: str = 'us', hl: str = 'en',
                       retries: int = 2, block_assets: bool = True, screenshot: bool = False,
                       proxy: dict | None = None, proxy_url_fn=None, context=None) -> dict[str, Any]:
    rec: dict[str, Any] = {'status': 'error', 'attempts': 0, 'blocks': 0, 'bytes': 0, 'error': None}
    t0 = time.time()
    shared = context
    for att in range(retries):
        rec['attempts'] += 1
        own_ctx = shared is None
        if own_ctx:
            pxy = playwright_proxy(await proxy_url_fn()) if proxy_url_fn is not None else proxy
            ctx = await browser.new_context(locale=f'{hl}-{gl.upper()}', timezone_id='America/New_York',
                                            viewport={'width': 1280, 'height': 1000}, user_agent=UA, proxy=pxy)
            await ctx.add_cookies([{'name': 'CONSENT', 'value': 'YES+cb', 'domain': '.google.com', 'path': '/'}])
            if block_assets:
                async def route(r):
                    if lean_should_continue(r.request.url, r.request.resource_type):
                        await r.continue_()
                    else:
                        await r.abort()
                await ctx.route('**/*', route)
        else:
            ctx = shared
        pg = await ctx.new_page()
        nbytes = [0]

        async def _on_resp(resp):
            try:
                nbytes[0] += len(await resp.body())
            except Exception:
                nbytes[0] += int(resp.headers.get('content-length') or 0)
        pg.on('response', _on_resp)
        try:
            await pg.goto(search_url(query, lat, lng, engine, gl, hl), timeout=45000, wait_until='load')
            head = (await pg.content())[:30000]
            if is_blocked(pg.url, head):
                rec['blocks'] += 1
                rec['status'] = 'blocked'
            else:
                r = await wait_for_answer(pg, engine)
                for label in ('Show more', 'Show all'):
                    try:
                        for bt in await pg.get_by_role('button', name=label).all():
                            if await bt.is_visible():
                                await bt.click(timeout=2000)
                                await pg.wait_for_timeout(1200)
                    except Exception:  # noqa: BLE001
                        pass
                if r.get('found'):
                    r = await pg.evaluate(EXTRACT_JS, engine)
                rec.update(r)
                rec['status'] = classify_status(engine, r)
            if screenshot:
                rec['screenshot'] = await pg.screenshot()
        except Exception as e:  # noqa: BLE001
            rec['status'] = 'error'
            rec['error'] = f'{type(e).__name__}: {e}'[:300]
        finally:
            rec['bytes'] += nbytes[0]
            try:
                await pg.close()
            except Exception:
                pass
            if own_ctx:
                await ctx.close()
        if rec['status'] in ('answer', 'no_answer'):
            break
    rec['secs'] = round(time.time() - t0, 1)
    return rec
