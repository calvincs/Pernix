#!/usr/bin/env python3
"""Mobile/tablet acceptance gate for Pernix.

Normally driven by run.sh, which boots a throwaway instance and seeds it first:

  check.py <base_url> <shots_dir> <tag> <main_sid> <parent_sid> [LEVEL]

LEVEL: m1 (foundation: tiers, inert, bottom stack, model menu on-screen, desktop unchanged)
       m2 (everything: + row sheet, editor, worker strip, targets, inputs, header hide)

Exit 1 on any failed check at the requested level. Writes <shots>/<tag>-*.png and
<shots>/../check-<tag>.json. The desktop baseline is desktop-baseline.json NEXT TO
THIS FILE — it is committed, unlike everything under out/ — written when the tag is
"baseline" and compared against on every other run.
"""

import json
import os
import sys
import time
import traceback

from playwright.sync_api import sync_playwright

base, shots, tag, MAIN, PARENT = sys.argv[1:6]
LEVEL = (sys.argv[6] if len(sys.argv) > 6 else os.environ.get("LEVEL", "m2")).lower()
ROOT = os.path.dirname(shots.rstrip("/"))
HERE = os.path.dirname(os.path.abspath(__file__))
MAC_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15"
VPS = [
    ("phone-s", 360, 780, dict(is_mobile=True, has_touch=True)),
    ("phone", 390, 844, dict(is_mobile=True, has_touch=True)),
    ("phone-l", 844, 390, dict(is_mobile=True, has_touch=True)),
    ("tab-p", 768, 1024, dict(is_mobile=True, has_touch=True)),
    ("tab-air-p", 820, 1180, dict(is_mobile=True, has_touch=True)),
    ("tab-air-l", 1180, 820, dict(is_mobile=True, has_touch=True)),
    ("ipad-desk", 1024, 768, dict(is_mobile=False, has_touch=True, user_agent=MAC_UA)),
]
results = []  # (viewport, name, ok, detail, level)
console_errors = []


def check(vp, name, ok, detail="", level="m1"):
    results.append((vp, name, bool(ok), str(detail)[:220], level))


def rect(pg, sel):
    return pg.evaluate(
        """(sel) => { const e=document.querySelector(sel); if(!e) return null;
        const r=e.getBoundingClientRect(); const cs=getComputedStyle(e);
        return {l:Math.round(r.left),t:Math.round(r.top),r:Math.round(r.right),b:Math.round(r.bottom),w:Math.round(r.width),h:Math.round(r.height),
                display:cs.display, pos:cs.position, vis:cs.visibility, tf:cs.transform}; }""",
        sel,
    )


SMALL_JS = r"""() => {
  const vw=innerWidth, vh=innerHeight; const out={small:[], inputs:[]};
  const vis = el => { const r=el.getBoundingClientRect(); if(r.width===0||r.height===0) return false; const cs=getComputedStyle(el); if(cs.visibility==='hidden'||cs.display==='none'||cs.opacity==='0') return false; return r.bottom>0&&r.top<vh&&r.right>0&&r.left<vw; };
  const desc = el => { let s=el.tagName.toLowerCase(); if(el.id) s+='#'+el.id; else if(typeof el.className==='string'&&el.className.trim()) s+='.'+el.className.trim().split(/\s+/).slice(0,2).join('.'); return s; };
  document.querySelectorAll('button, a[href], input:not([type=hidden]):not([type=checkbox]):not([type=radio]), select, textarea, [role=button], [role=tab], [role=option], [role=menuitem]').forEach(el => {
    if(!vis(el)) return; if(el.closest('[inert]')) return; const r=el.getBoundingClientRect();
    if(r.width<28||r.height<28) out.small.push(desc(el)+' '+Math.round(r.width)+'x'+Math.round(r.height));
    if(/^(input|select|textarea)$/i.test(el.tagName)){ const fs=parseFloat(getComputedStyle(el).fontSize); if(fs<16) out.inputs.push(desc(el)+' '+fs+'px'); }
  });
  return out; }"""
SMALL_ALLOW = (
    "a.skip-link",
    "span.setting-badge",
    "button.session-group-header",
    "div.session-group-header",
    "div.sg-toggle",
    "div.worker-summary",
    "button.load-earlier-btn",
)


def small_filtered(m):
    return [s for s in m["small"] if not any(s.startswith(a) for a in SMALL_ALLOW)]


def open_session(pg, sid, compact):
    if compact:
        pg.evaluate(
            "() => { const s=document.getElementById('sidebar'); if(!s.classList.contains('mobile-open')) document.getElementById('sidebar-toggle').click(); }"
        )
        time.sleep(0.35)
    pg.evaluate(f"() => document.querySelector('[data-sid=\"{sid}\"]')?.click()")
    time.sleep(1.2)
    if compact:
        pg.evaluate(
            "() => { const s=document.getElementById('sidebar'); if(s.classList.contains('mobile-open')) (document.querySelector('.mobile-scrim')||{click(){}}).click(); }"
        )
        time.sleep(0.4)
        pg.keyboard.press("Escape")
        time.sleep(0.2)


def run_vp(browser, name, w, h, opts):
    ctx = browser.new_context(viewport={"width": w, "height": h}, device_scale_factor=2, color_scheme="dark", **opts)
    pg = ctx.new_page()
    pg.on("console", lambda m: console_errors.append(f"[{name}] {m.text}") if m.type == "error" else None)
    pg.on("pageerror", lambda e: console_errors.append(f"[{name}] pageerror: {e}"))
    pg.goto(base + "/", wait_until="load")
    time.sleep(1.8)
    compact = w < 900

    def shot(label):
        pg.screenshot(path=f"{shots}/{tag}-{name}-{label}.png")

    # --- flags / stylesheets
    # `sheets` lists the stylesheets whose media query MATCHES. document.styleSheets
    # keeps a <link> whose media does not match (Chrome still fetches and parses it),
    # so an unfiltered list can never tell "gated off" from "not linked" and
    # "compact.css loaded only when compact" could not pass with a <link media> gate.
    flags = pg.evaluate(
        "() => ({touch: document.body.hasAttribute('data-touch'), compact: document.body.hasAttribute('data-compact'), mobile: document.body.hasAttribute('data-mobile'), sheets: [...document.styleSheets].filter(s => !s.media.mediaText || matchMedia(s.media.mediaText).matches).map(s => (s.href||'').split('/').pop()).filter(Boolean)})"
    )
    if LEVEL in ("m1", "m2"):
        check(name, "body[data-touch] stamped", flags["touch"], flags)
        check(name, "body[data-compact] matches width", flags["compact"] == compact, flags)
        check(
            name,
            "compact.css loaded only when compact",
            (("compact.css" in flags["sheets"]) == compact),
            flags["sheets"],
        )
        check(name, "touch.css loaded", "touch.css" in flags["sheets"], flags["sheets"])
    check(
        name, "no horizontal scroll (home)", pg.evaluate("() => document.documentElement.scrollWidth <= innerWidth + 1")
    )
    sb = rect(pg, "#status-bar")
    check(name, "status bar at top on touch", sb and sb["t"] == 0, sb)
    shot("home")

    # --- sidebar tier
    sbr = rect(pg, "#sidebar")
    scrim = pg.evaluate("() => !!document.querySelector('.mobile-scrim')")
    if LEVEL == "m2":
        # The drag handle for --sidebar-w is a mouse affordance: a 6px strip
        # is not something a finger can aim at, on a phone or on a docked
        # 1180px tablet. touch.css and compact.css each hide it, for their
        # own reason, so this has to fail on every touch viewport.
        check(
            name,
            "m2: no sidebar resize handle on touch",
            pg.evaluate(
                "() => { const e=document.getElementById('sidebar-resizer');"
                " return !e || getComputedStyle(e).display === 'none'; }"
            ),
            "",
            "m2",
        )
    if compact:
        check(name, "compact: sidebar off-canvas when closed", sbr and sbr["r"] <= 0, sbr)
        check(
            name,
            "compact: closed drawer is inert",
            pg.evaluate("() => document.getElementById('sidebar').hasAttribute('inert')"),
        )
        pg.evaluate("() => document.getElementById('sidebar-toggle').click()")
        time.sleep(0.5)
        sbo = rect(pg, "#sidebar")
        check(name, "compact: drawer opens on hamburger", sbo and sbo["l"] == 0 and sbo["w"] >= 240, sbo)
        check(
            name,
            "compact: #main inert while drawer open",
            pg.evaluate("() => document.getElementById('main').hasAttribute('inert')"),
        )
        check(
            name,
            "compact: focus moved into drawer",
            pg.evaluate("() => document.getElementById('sidebar').contains(document.activeElement)"),
        )
        if LEVEL == "m2":
            tw = pg.evaluate(
                "() => { const it=document.querySelector('.session-item:not(.pinned)'); if(!it) return -1; const t=it.querySelector('.session-title-text, .session-title'); if(!t) return -2; let box=t; while(box && getComputedStyle(box).overflow==='visible' && box!==it) box=box.parentElement; return Math.round(box.getBoundingClientRect().width); }"
            )
            check(name, "m2: session title gets >=140px in drawer", tw >= 140, f"title box width={tw}px", "m2")
            check(
                name,
                "m2: one overflow button per session row on touch",
                pg.evaluate(
                    "() => { const it=document.querySelector('.session-item'); if(!it) return false; const vis=[...it.querySelectorAll('button')].filter(b=>b.offsetParent && b.getBoundingClientRect().width>0); return vis.length <= 2 && !!it.querySelector('.session-menu-btn, .session-more, [data-action=\"more\"], [aria-label*=\"ctions\"]'); }"
                ),
                "",
                "m2",
            )
        shot("drawer")
        pg.keyboard.press("Escape")
        time.sleep(0.4)
        check(
            name,
            "compact: Esc closes drawer",
            pg.evaluate("() => !document.getElementById('sidebar').classList.contains('mobile-open')"),
        )
        check(
            name,
            "compact: focus returns to hamburger",
            pg.evaluate("() => document.activeElement && document.activeElement.id === 'sidebar-toggle'"),
        )
        pg.evaluate("() => document.querySelector('.mobile-scrim')?.click()")
        time.sleep(0.2)
    else:
        check(
            name,
            "wide: sidebar docked (left 0, <=300 wide, not fixed)",
            sbr and sbr["l"] == 0 and 0 < sbr["w"] <= 300 and sbr["pos"] != "fixed" and sbr["r"] > 0,
            sbr,
        )
        check(name, "wide: no scrim element", not scrim)
        mn = rect(pg, "#main")
        check(name, "wide: #main starts at sidebar edge", mn and sbr and abs(mn["l"] - sbr["r"]) <= 2, (mn, sbr))
        pg.evaluate("() => document.getElementById('sidebar-toggle').click()")
        time.sleep(0.5)
        sbc = rect(pg, "#sidebar")
        check(
            name,
            "wide: hamburger collapses docked sidebar",
            sbc and (sbc["w"] <= 4 or sbc["r"] <= 0 or sbc["display"] == "none" or sbc["vis"] == "hidden"),
            sbc,
        )
        check(
            name,
            "wide: collapsed sidebar inert",
            pg.evaluate("() => document.getElementById('sidebar').hasAttribute('inert')"),
        )
        pg.evaluate("() => document.getElementById('sidebar-toggle').click()")
        time.sleep(0.5)
        sbo = rect(pg, "#sidebar")
        check(name, "wide: hamburger re-expands", sbo and sbo["w"] >= 200, sbo)
        shot("docked")

    # --- session + chat
    open_session(pg, MAIN, compact)
    check(name, "session loads", pg.evaluate("() => document.querySelectorAll('#messages-inner .message').length >= 4"))
    mi = rect(pg, ".messages-inner")
    if not compact:
        check(name, "wide: chat column capped at desktop width", mi and mi["w"] <= 960, mi)
    check(
        name, "no horizontal scroll (chat)", pg.evaluate("() => document.documentElement.scrollWidth <= innerWidth + 1")
    )
    bs = pg.evaluate(
        "() => { const v=getComputedStyle(document.documentElement).getPropertyValue('--bottom-stack').trim(); const iw=document.getElementById('input-wrapper').getBoundingClientRect().height; return {v, iw: Math.round(iw)}; }"
    )
    check(
        name,
        "--bottom-stack set and >= composer height",
        bs["v"] and float(bs["v"].replace("px", "") or 0) >= bs["iw"] - 1,
        bs,
    )
    shot("chat")
    # model menu on screen
    pg.tap("#status-model") if opts.get("has_touch") else pg.click("#status-model")
    time.sleep(1.0)
    mm = pg.evaluate(
        "() => { const m=document.getElementById('model-menu') || document.querySelector('.model-sheet, [data-sheet=\"model\"]'); if(!m) return null; const r=m.getBoundingClientRect(); return {t:Math.round(r.top), b:Math.round(r.bottom), h:Math.round(r.height)}; }"
    )
    check(name, "model menu opens fully on screen", mm and mm["t"] >= 0 and mm["b"] <= h + 1 and mm["h"] > 40, mm)
    shot("model-menu")
    pg.keyboard.press("Escape")
    pg.evaluate("() => document.body.click()")
    time.sleep(0.3)
    if LEVEL == "m2":
        m = pg.evaluate(SMALL_JS)
        sm = small_filtered(m)
        check(name, "m2: no visible interactive element <28px (chat)", len(sm) == 0, sm[:12], "m2")
        check(name, "m2: no input <16px (chat)", len(m["inputs"]) == 0, m["inputs"][:8], "m2")
        # header rename input size
        pg.evaluate("() => document.getElementById('session-header-title')?.click()")
        time.sleep(0.5)
        hr = pg.evaluate(
            "() => { const i=document.getElementById('session-header-rename'); if(i) return {kind:'inline', fs: parseFloat(getComputedStyle(i).fontSize)}; const s=document.querySelector('.modal-overlay, .sheet-overlay'); return {kind: s ? 'sheet' : 'none'}; }"
        )
        check(
            name,
            "m2: title tap -> sheet on compact / 16px input otherwise",
            (hr["kind"] == "sheet") if compact else (hr["kind"] != "inline" or hr["fs"] >= 16),
            hr,
            "m2",
        )
        pg.keyboard.press("Escape")
        time.sleep(0.3)
    # keyboard-ish: short viewport
    if w > h and h <= 500:
        hdr = rect(pg, "#session-header")
        if LEVEL == "m2":
            check(
                name,
                "m2: landscape phone hides session header",
                (not hdr) or hdr["h"] == 0 or hdr["display"] == "none",
                hdr,
                "m2",
            )
    # workers
    open_session(pg, PARENT, compact)
    ws = rect(pg, "#worker-strip")
    if LEVEL == "m2" and compact:
        check(name, "m2: compact worker strip is one line (<=48px)", ws and ws["h"] <= 48, ws, "m2")
    shot("workers")

    # --- settings modal shape
    pg.evaluate("() => document.getElementById('settings-btn').click()")
    time.sleep(0.9)
    mc = rect(pg, ".modal-card")
    if compact or h < 700:
        check(name, "compact/short: settings is a bottom sheet", mc and mc["b"] >= h - 1 and mc["w"] >= w - 2, mc)
    else:
        check(
            name, "wide: settings is a centred card", mc and mc["w"] < w - 40 and mc["t"] > 10 and mc["b"] < h - 10, mc
        )
    if not compact:
        row = pg.evaluate(
            "() => { const r=document.querySelector('.setting-row:not(.setting-row-bool)'); return r ? getComputedStyle(r).flexDirection : null; }"
        )
        check(name, "wide: settings rows side-by-side", row == "row", row)
    if LEVEL == "m2":
        m = pg.evaluate(SMALL_JS)
        sm = small_filtered(m)
        check(name, "m2: no visible interactive element <28px (settings)", len(sm) == 0, sm[:12], "m2")
        check(name, "m2: no input <16px (settings)", len(m["inputs"]) == 0, m["inputs"][:8], "m2")
    shot("settings")
    pg.evaluate("() => document.querySelector('.modal-close')?.click()")
    time.sleep(0.4)
    pg.keyboard.press("Escape")
    time.sleep(0.3)
    check(name, "settings closed", pg.evaluate("() => !document.querySelector('.modal-overlay')"))

    # --- explorer tier
    pg.evaluate("() => document.getElementById('files-btn').click()")
    time.sleep(1.0)
    fp = rect(pg, "#file-panel.open")
    if compact:
        check(name, "compact: Explorer covers the viewport", fp and fp["w"] >= w - 2 and fp["h"] >= h - 2, fp)
        check(
            name,
            "compact: #main inert while Explorer open",
            pg.evaluate("() => document.getElementById('main').hasAttribute('inert')"),
        )
        check(
            name,
            "compact: focus moved into Explorer",
            pg.evaluate("() => document.getElementById('file-panel').contains(document.activeElement)"),
        )
    else:
        check(
            name, "wide: Explorer is a side column (360-600px)", fp and 360 <= fp["w"] <= 600 and fp["h"] >= h - 2, fp
        )
        ms = rect(pg, "#messages")
        check(
            name,
            "wide: chat still visible beside Explorer",
            ms and ms["w"] >= 380 and ms["r"] <= (fp["l"] + 2 if fp else 0),
            (ms, fp),
        )
        check(
            name,
            "wide: #main not inert with Explorer open",
            not pg.evaluate("() => document.getElementById('main').hasAttribute('inert')"),
        )
    check(
        name,
        "no horizontal scroll (explorer)",
        pg.evaluate("() => document.documentElement.scrollWidth <= innerWidth + 1"),
    )
    shot("explorer")
    if LEVEL == "m2":
        pg.evaluate("() => document.getElementById('fp-group-files')?.click()")
        time.sleep(0.5)
        pg.evaluate(
            "() => { const f=[...document.querySelectorAll('.fp-tree-item')].find(e=>!e.classList.contains('fp-tree-dir')); f && f.click(); }"
        )
        time.sleep(0.9)
        pg.evaluate(
            "() => { const b=[...document.querySelectorAll('#file-panel button')].find(x=>x.textContent.trim()==='edit'); b && b.click(); }"
        )
        time.sleep(1.2)
        ed = pg.evaluate(
            "() => { const ta=document.querySelector('#file-panel textarea.ce-fallback'); const mon=document.querySelector('#file-panel .monaco-editor'); return {ta: !!ta, fs: ta ? parseFloat(getComputedStyle(ta).fontSize) : null, h: ta ? Math.round(ta.getBoundingClientRect().height) : null, monaco: !!mon}; }"
        )
        check(
            name,
            "m2: touch editor is a 16px textarea, no Monaco",
            ed["ta"] and not ed["monaco"] and (ed["fs"] or 0) >= 16 and (ed["h"] or 0) >= 200,
            ed,
            "m2",
        )
        shot("editor")
        pg.evaluate(
            "() => { const b=[...document.querySelectorAll('#file-panel button')].find(x=>x.textContent.trim()==='cancel'); b && b.click(); }"
        )
        time.sleep(0.4)
        pg.evaluate("() => document.getElementById('fp-group-tuning')?.click()")
        time.sleep(0.6)
        pg.evaluate("() => document.querySelectorAll('.fp-subtab-btn')[1]?.click()")
        time.sleep(0.8)
        ov = pg.evaluate(
            "() => { const bs=[...document.querySelectorAll('#file-panel .adaptive-head button, #file-panel .adaptive-head .adaptive-btn')].filter(b=>b.offsetParent); const rs=bs.map(b=>b.getBoundingClientRect()); let overlap=false; for(let i=0;i<rs.length;i++) for(let j=i+1;j<rs.length;j++){ const a=rs[i], c=rs[j]; if(a.left<c.right-1 && c.left<a.right-1 && a.top<c.bottom-1 && c.top<a.bottom-1) overlap=true; } return {n: rs.length, overlap, cut: rs.some(r=>r.right>innerWidth+1)}; }"
        )
        check(
            name,
            "m2: Self-checks toolbar buttons do not overlap or overflow",
            ov["n"] > 0 and not ov["overlap"] and not ov["cut"],
            ov,
            "m2",
        )
        shot("selfchecks")
    pg.evaluate("() => document.querySelector('.fp-close')?.click()")
    time.sleep(0.4)
    check(name, "explorer closes", pg.evaluate("() => !document.querySelector('#file-panel.open')"))
    ctx.close()


def desktop_layout(browser):
    ctx = browser.new_context(viewport={"width": 1280, "height": 800}, color_scheme="dark", reduced_motion="reduce")
    pg = ctx.new_page()
    pg.on("console", lambda m: console_errors.append(f"[desktop] {m.text}") if m.type == "error" else None)
    pg.goto(base + "/", wait_until="load")
    time.sleep(1.8)
    out = {}

    def grab(label):
        out[label] = pg.evaluate(
            """() => { const q=s=>{const e=document.querySelector(s); if(!e) return null; const r=e.getBoundingClientRect(); const cs=getComputedStyle(e); return [Math.round(r.left),Math.round(r.top),Math.round(r.width),Math.round(r.height), cs.position, cs.display, cs.fontSize, cs.padding];};
            return {sidebar:q('#sidebar'), main:q('#main'), status:q('#status-bar'), input:q('#input-wrapper'), messages:q('#messages'), inner:q('.messages-inner'), fp:q('#file-panel'), modal:q('.modal-card'), toggle:q('#sidebar-toggle'), header:q('#session-header'), model:q('#status-model'), msg:q('#msg-input'), item:q('.session-item'), btn:q('#settings-btn'), sheets:[...document.styleSheets].map(s=>(s.href||'').split('/').pop()).filter(Boolean)}; }"""
        )
        pg.screenshot(path=f"{shots}/{tag}-desktop-{label}.png")

    grab("home")
    pg.evaluate(f"() => document.querySelector('[data-sid=\"{MAIN}\"]')?.click()")
    time.sleep(1.2)
    grab("chat")
    pg.click("#settings-btn")
    time.sleep(0.9)
    grab("settings")
    pg.keyboard.press("Escape")
    time.sleep(0.4)
    pg.click("#files-btn")
    time.sleep(0.9)
    grab("explorer")
    pg.click("#files-btn")
    time.sleep(0.4)
    pg.click("#sidebar-toggle")
    time.sleep(0.5)
    grab("collapsed")
    pg.click("#sidebar-toggle")
    time.sleep(0.4)
    ctx.close()
    return out


def sidebar_resizer(browser):
    """The desktop tier's own control: drag the sidebar's edge to resize it.

    Its own context, after the baseline pass, because it writes
    pernix:sidebar-width — a stored width would move every box the baseline
    records if the two shared a browser profile.
    """
    ctx = browser.new_context(viewport={"width": 1280, "height": 800}, color_scheme="dark",
                              reduced_motion="reduce")
    pg = ctx.new_page()
    pg.on("console", lambda m: console_errors.append(f"[resizer] {m.text}") if m.type == "error" else None)
    pg.on("pageerror", lambda e: console_errors.append(f"[resizer] pageerror: {e}"))
    pg.goto(base + "/", wait_until="load")
    time.sleep(1.8)

    h = pg.evaluate(
        """() => { const e=document.getElementById('sidebar-resizer'); if(!e) return null;
        const r=e.getBoundingClientRect();
        return {role:e.getAttribute('role'), orient:e.getAttribute('aria-orientation'),
                now:e.getAttribute('aria-valuenow'), min:e.getAttribute('aria-valuemin'),
                l:Math.round(r.left), w:Math.round(r.width),
                cursor:getComputedStyle(e).cursor}; }"""
    )
    check("resizer", "m2: handle is a labelled vertical separator",
          h and h["role"] == "separator" and h["orient"] == "vertical" and h["cursor"] == "col-resize", h, "m2")
    check("resizer", "m2: handle sits on the sidebar's seam at the default width",
          h and h["w"] == 6 and h["l"] == 267 and h["now"] == "270" and h["min"] == "200", h, "m2")

    width = "() => Math.round(document.getElementById('sidebar').getBoundingClientRect().width)"
    pg.mouse.move(270, 400)
    pg.mouse.down()
    pg.mouse.move(330, 400, steps=5)
    pg.mouse.move(390, 400, steps=5)
    pg.mouse.up()
    time.sleep(0.4)
    dragged = pg.evaluate(width)
    main_w = pg.evaluate("() => Math.round(document.getElementById('main').getBoundingClientRect().width)")
    check("resizer", "m2: dragging the edge +120px widens the sidebar to 390", dragged == 390, dragged, "m2")
    check("resizer", "m2: #main gives the width back", main_w == 1280 - 390 - 1, main_w, "m2")
    pg.screenshot(path=f"{shots}/{tag}-desktop-resized.png")

    pg.reload(wait_until="load")
    time.sleep(1.5)
    check("resizer", "m2: the width survives a reload", pg.evaluate(width) == 390, pg.evaluate(width), "m2")

    pg.dblclick("#sidebar-resizer")
    time.sleep(0.4)
    reset = pg.evaluate(
        "() => [Math.round(document.getElementById('sidebar').getBoundingClientRect().width),"
        " localStorage.getItem('pernix:sidebar-width')]"
    )
    check("resizer", "m2: double-click restores 270 and drops the stored width",
          reset[0] == 270 and reset[1] is None, reset, "m2")
    ctx.close()


with sync_playwright() as p:
    browser = p.chromium.launch()
    for name, w, h, opts in VPS:
        try:
            run_vp(browser, name, w, h, opts)
        except Exception as e:
            check(name, "viewport run completed", False, f"{e}\n{traceback.format_exc()[-400:]}")
    try:
        lay = desktop_layout(browser)
        bl = os.path.join(HERE, "desktop-baseline.json")
        if tag == "baseline":
            json.dump(lay, open(bl, "w"), indent=1)
            check("desktop", "baseline written", True)
        elif os.path.exists(bl):
            base_l = json.load(open(bl))
            diffs = []
            for label, d in base_l.items():
                for k, v in d.items():
                    if k == "sheets":
                        continue
                    nv = (lay.get(label) or {}).get(k)
                    if v is None or nv is None:
                        if v != nv:
                            diffs.append(f"{label}.{k}: {v} -> {nv}")
                        continue
                    if any(abs(a - b) > 1 for a, b in zip(v[:4], nv[:4])) or v[4:] != nv[4:]:
                        diffs.append(f"{label}.{k}: {v} -> {nv}")
            check("desktop", "desktop layout identical to baseline", len(diffs) == 0, diffs[:10])
        else:
            check("desktop", "desktop baseline present", False, "run with tag=baseline first")
    except Exception as e:
        check("desktop", "desktop pass completed", False, str(e))
    if LEVEL == "m2":
        try:
            sidebar_resizer(browser)
        except Exception as e:
            check("resizer", "m2: resizer pass completed", False, f"{e}\n{traceback.format_exc()[-400:]}", "m2")
    browser.close()

check("all", "no console errors", len(console_errors) == 0, console_errors[:6])
json.dump(
    {"level": LEVEL, "results": results, "console_errors": console_errors},
    open(os.path.join(ROOT, f"check-{tag}.json"), "w"),
    indent=1,
)
fails = [r for r in results if not r[2]]
print(f"== mobile check [{tag}] level={LEVEL} ==")
cur = None
for vp, nm, ok, det, lv in results:
    if vp != cur:
        print(f"--- {vp}")
        cur = vp
    print(f"  {'PASS' if ok else 'FAIL'}  {nm}" + (f"   <- {det}" if not ok and det else ""))
print(f"\n{len(results) - len(fails)}/{len(results)} passed" + (f", {len(fails)} FAILED" if fails else ""))
sys.exit(1 if fails else 0)
