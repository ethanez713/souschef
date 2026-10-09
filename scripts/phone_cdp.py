"""Drive and measure the SousChef tab in the phone's Chrome over DevTools (needs `adb forward tcp:9222 localabstract:chrome_devtools_remote`). Usage: phone_cdp.py "nav URL :: audit :: shot name". Commands: nav, tap, type, eval, audit, shot, full, text, wait, back."""
# ruff: noqa
import asyncio, base64, json, os, sys, urllib.request
import websockets

SHOTS = "/tmp/phone_shots/"
os.makedirs(SHOTS, exist_ok=True)

AUDIT = r"""
(() => {
  const vis = e => { const r = e.getBoundingClientRect(), s = getComputedStyle(e);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none' && +s.opacity > 0.05; };
  const label = e => (e.innerText || e.value || e.getAttribute('aria-label') || e.title || e.name || e.placeholder || e.className || '').toString().trim().replace(/\s+/g, ' ').slice(0, 28);
  const lum = c => { const a = c.map(v => { v /= 255; return v <= .03928 ? v / 12.92 : ((v + .055) / 1.055) ** 2.4; }); return .2126 * a[0] + .7152 * a[1] + .0722 * a[2]; };
  const rgb = s => { const m = s.match(/[\d.]+/g).map(Number); return { c: m.slice(0, 3), a: m.length > 3 ? m[3] : 1 }; };
  const bg = e => { for (; e; e = e.parentElement) { const b = rgb(getComputedStyle(e).backgroundColor); if (b.a > 0.9) return b.c; } return [255, 255, 255]; };
  const targets = [...document.querySelectorAll('a,button,input:not([type=hidden]),select,textarea,summary,[role=button],[onclick],[hx-post],[hx-get]')].filter(vis);
  const small = targets.map(e => { const r = e.getBoundingClientRect(); return { t: e.tagName.toLowerCase() + (e.type ? ':' + e.type : ''), l: label(e), w: Math.round(r.width), h: Math.round(r.height) }; }).filter(x => Math.min(x.w, x.h) < 44);
  const fonts = {}, low = {};
  const w = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  for (let n; n = w.nextNode();) { const t = n.textContent.trim(); if (!t) continue; const p = n.parentElement; if (!vis(p)) continue;
    const s = getComputedStyle(p), fs = Math.round(parseFloat(s.fontSize) * 10) / 10; fonts[fs] = (fonts[fs] || 0) + 1;
    const f = rgb(s.color), b = bg(p), l1 = lum(f.c), l2 = lum(b), cr = (Math.max(l1, l2) + .05) / (Math.min(l1, l2) + .05);
    if (cr < 4.5 && f.a > 0.9) { const k = s.color + ' on ' + b.join(',') + ' @' + fs; (low[k] = low[k] || { cr: Math.round(cr * 10) / 10, n: 0, eg: t.slice(0, 24) }).n++; } }
  const g = {}; small.forEach(x => { const k = x.t + ' ' + x.w + 'x' + x.h; (g[k] = g[k] || { n: 0, eg: x.l }).n++; });
  return JSON.stringify({ url: location.pathname + location.search, title: document.title, vw: innerWidth, vh: innerHeight, docH: document.documentElement.scrollHeight,
    hOverflow: document.documentElement.scrollWidth - innerWidth, targets: targets.length, smallTargets: small.length, small: g, fonts, lowContrast: low,
    fixed: [...document.querySelectorAll('*')].filter(e => ['fixed', 'sticky'].includes(getComputedStyle(e).position) && vis(e)).map(e => e.tagName.toLowerCase() + '.' + e.className + ' ' + Math.round(e.getBoundingClientRect().height) + 'px') });
})()
"""

class Tab:
    def __init__(self, ws): self.ws, self.n, self.events = ws, 0, []
    async def call(self, method, **params):
        self.n += 1; mid = self.n
        await self.ws.send(json.dumps({"id": mid, "method": method, "params": params}))
        while True:
            m = json.loads(await self.ws.recv())
            if m.get("id") == mid:
                if "error" in m: raise RuntimeError(m["error"])
                return m["result"]
            self.events.append(m)
    async def ev(self, js):
        r = await self.call("Runtime.evaluate", expression=js, returnByValue=True, awaitPromise=True)
        if "exceptionDetails" in r: return "JS ERROR: " + json.dumps(r["exceptionDetails"])[:400]
        return r["result"].get("value")
    async def settle(self, t=0.9): 
        await asyncio.sleep(t)
        for _ in range(20):
            if await self.ev("document.readyState") == "complete": break
            await asyncio.sleep(0.2)
    async def shot(self, name, full=False):
        outs = []
        if full: await self.ev("scrollTo(0,0)")
        for i in range(5 if full else 1):
            m = await self.call("Page.getLayoutMetrics")
            v = m["cssVisualViewport"]
            r = await self.call("Page.captureScreenshot", format="jpeg", quality=70,
                                clip={"x": 0, "y": v["pageY"], "width": v["clientWidth"], "height": v["clientHeight"], "scale": 0.5})
            fn = name + (f"-{i+1}" if full else "") + ".jpg"
            open(SHOTS + fn, "wb").write(base64.b64decode(r["data"])); outs.append(fn)
            if not full or v["pageY"] + v["clientHeight"] >= m["cssContentSize"]["height"] - 2: break
            await self.ev(f"scrollBy(0,{v['clientHeight'] - 130})"); await asyncio.sleep(0.35)
        return "shots " + " ".join(outs)
    async def tap(self, sel):
        js = f"""(() => {{ const q = {json.dumps(sel)}; let e;
          if (q.startsWith('text=')) {{ const t = q.slice(5).toLowerCase(); e = [...document.querySelectorAll('a,button,summary,label,input[type=submit],[role=button]')].find(x => (x.innerText||x.value||x.getAttribute('aria-label')||'').trim().toLowerCase() === t)
             || [...document.querySelectorAll('a,button,summary,label,[role=button]')].find(x => (x.innerText||x.getAttribute('aria-label')||'').trim().toLowerCase().includes(t)); }}
          else e = document.querySelector(q);
          if (!e) return null; e.scrollIntoView({{block:'center'}}); const r = e.getBoundingClientRect(); const x = r.x + r.width/2, y = r.y + r.height/2; const vv = visualViewport;
          const top = document.elementFromPoint(x, y); return {{x: x - vv.offsetLeft, y: y - vv.offsetTop, kb: Math.round(innerHeight - vv.height), hit: top === e || e.contains(top) || (top && top.contains(e)), top: top ? top.tagName + '.' + top.className : null}}; }})()"""
        p = await self.ev(js)
        if not p: return f"tap {sel}: NOT FOUND"
        await self.call("Input.dispatchTouchEvent", type="touchStart", touchPoints=[{"x": p["x"], "y": p["y"]}])
        await self.call("Input.dispatchTouchEvent", type="touchEnd", touchPoints=[])
        await self.settle()
        return f"tap {sel}: ok at {p['x']:.0f},{p['y']:.0f} kb={p['kb']}" + ("" if p["hit"] else f" (OBSCURED by {p['top']})") + " -> " + str(await self.ev("location.pathname+location.search"))

async def main(cmds):
    tabs = json.load(urllib.request.urlopen("http://localhost:9222/json"))
    tab = next(t for t in tabs if t["type"] == "page" and ("localhost:801" in t["url"] or "127.0.0.1:801" in t["url"]))
    async with websockets.connect(tab["webSocketDebuggerUrl"], max_size=None) as ws:
        t = Tab(ws)
        for d in ("Page", "Runtime", "Log", "Network"): await t.call(d + ".enable")
        for c in cmds:
            op, _, arg = c.strip().partition(" ")
            if op == "nav": await t.call("Page.navigate", url=arg); await t.settle(); out = "nav -> " + str(await t.ev("location.href"))
            elif op == "eval": out = await t.ev(arg)
            elif op == "audit": out = await t.ev(AUDIT)
            elif op == "shot": out = await t.shot(arg)
            elif op == "full": out = await t.shot(arg, True)
            elif op == "tap": out = await t.tap(arg)
            elif op == "type": 
                sel, _, text = arg.partition(" <- "); await t.ev(f"(()=>{{const e=document.querySelector({json.dumps(sel)}); e.focus(); e.select&&e.select();}})()"); await t.call("Input.insertText", text=text); out = f"typed into {sel}"
            elif op == "text": out = await t.ev("document.querySelector(%s)?.innerText" % json.dumps(arg or "main, body"))
            elif op == "wait": await asyncio.sleep(float(arg)); out = "waited"
            elif op == "back": await t.ev("history.back()"); await t.settle(); out = "back -> " + str(await t.ev("location.pathname"))
            else: out = "unknown " + op
            print(f"## {c.strip()[:70]}\n{out}")
        for e in t.events:
            m, p = e.get("method"), e.get("params", {})
            if m == "Log.entryAdded" and p["entry"]["level"] in ("error", "warning"): print("LOG", p["entry"]["level"], p["entry"]["text"][:200], p["entry"].get("url", "")[-60:])
            if m == "Runtime.exceptionThrown": print("EXC", json.dumps(p["exceptionDetails"])[:300])
            if m == "Network.responseReceived" and p["response"]["status"] >= 400: print("HTTP", p["response"]["status"], p["response"]["url"][-70:])

asyncio.run(main(" ".join(sys.argv[1:]).split("::")))
