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
import sqlite3
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


# The two routes that legitimately 404 until the trust-loop backend (W2)
# merges: the client asks ONCE per page load whether this server can store a
# message rating, and the Trust tab asks for the metrics. Chromium logs a
# fetch that comes back 404 as a console error, so an unfiltered gate would
# fail on the exact degradation path the client is written to handle — the
# one message_feedback() below asserts. Once the routes exist nothing matches
# this and the allowance costs nothing.
PENDING_ROUTES = ("/feedback", "/api/trust")

# Their settings siblings: rows registered here in the browser whose keys the
# 3.2 backend adds. Same rule — until the server publishes them, they do not
# render at all.
PENDING_SETTINGS = (
    "adaptive_pm_drift_rollback",
    "skill_proposal_auto_rollback",
    "reflect_next_turn_grading",
    "grader_holdout_enabled",
    "grader_holdout_schedule",
)


def _pending_route_404(msg):
    if "404" not in msg.text:
        return False
    url = ((msg.location or {}).get("url") or "").split("?")[0]
    return any(url.endswith(suffix) for suffix in PENDING_ROUTES)


def console_sink(prefix):
    """Collect console errors for `prefix`, minus the expected 3.2 404s."""

    def sink(msg):
        if msg.type != "error" or _pending_route_404(msg):
            return
        console_errors.append(f"[{prefix}] {msg.text}")

    return sink


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


# ---------------------------------------------------------------------------
# The seam between spaces and the plain list — colour rail, indent, heading
# ---------------------------------------------------------------------------
# Three checks, one probe: they all read the same DOM the seeded Pernix /
# Research lab / Scale spaces sit in above the plain time buckets, so one
# pg.evaluate gathers what each of them needs.
SPACE_SEAM_JS = r"""() => {
  const out = {};

  // 1. the colour rail — what still says "inside this space" once the
  // header's own dot has scrolled off the top. Every OPEN space body needs
  // one, centred under that header's chevron.
  out.rails = [...document.querySelectorAll('.space-group-body:not(.collapsed)')].map(b => {
      const header = b.previousElementSibling;
      const arrow = header ? header.querySelector('.sg-arrow') : null;
      if (!arrow) return {ok: false, reason: 'no header arrow'};
      const cs = getComputedStyle(b, '::before');
      const br = b.getBoundingClientRect();
      const ar = arrow.getBoundingClientRect();
      const center = br.left + (parseFloat(cs.left) || 0) + (parseFloat(cs.width) || 0) / 2;
      return {width: cs.width, bg: cs.backgroundColor,
              center: Math.round(center), arrowLeft: Math.round(ar.left), arrowRight: Math.round(ar.right)};
  });

  // 2. the indent — a row inside a space vs. a row in the plain list below.
  const spaceRow = document.querySelector('.space-group-body .session-item:not(.worker):not(.active)');
  const topRow = document.querySelector('.session-group-body:not(.space-group-body) .session-item:not(.worker):not(.active)');
  out.spaceFound = !!spaceRow;
  out.topFound = !!topRow;
  if (spaceRow) out.spacePad = parseFloat(getComputedStyle(spaceRow).paddingLeft);
  if (topRow) out.topPad = parseFloat(getComputedStyle(topRow).paddingLeft);

  // 3. the Sessions heading at the seam itself.
  const heading = document.querySelector('.sessions-header');
  if (heading) {
      const label = heading.querySelector('.sessions-header-label');
      const hr = heading.getBoundingClientRect();
      const openBodies = [...document.querySelectorAll('.space-group-body:not(.collapsed)')];
      const firstBucket = document.querySelector('.session-group-header:not(.space-group-header)');
      out.heading = {
          text: (label ? label.textContent : heading.textContent || '').trim(),
          top: Math.round(hr.top),
          maxSpaceBottom: openBodies.length
              ? Math.round(Math.max(...openBodies.map(b => b.getBoundingClientRect().bottom))) : null,
          bucketTop: firstBucket ? Math.round(firstBucket.getBoundingClientRect().top) : null,
          borderTopWidth: getComputedStyle(heading).borderTopWidth,
      };
  } else {
      out.heading = null;
  }
  return out;
}"""


def space_seam_checks(pg, name):
    """Rail, indent and Sessions-heading — run wherever the seeded spaces sit
    above the plain list, on every tier where the sidebar is on screen.

    A top-level row only has to exist SOMEWHERE in the DOM — the outer time
    buckets always render their rows, collapsed or not, unlike a space's own
    folded buckets — but this opens one anyway if every top-level header
    happens to be collapsed, so the measurement never depends on which
    buckets a previous step left open.
    """
    pg.evaluate(
        "() => { const h=[...document.querySelectorAll('.session-group-header:not(.space-group-header)')]"
        ".find(x => x.classList.contains('collapsed')); if (h) h.click(); }"
    )
    time.sleep(0.3)
    r = pg.evaluate(SPACE_SEAM_JS)

    rails = r.get("rails") or []
    check(
        name,
        "m2: colour rail under every open space",
        bool(rails)
        and all(
            x.get("width") == "2px"
            and x.get("bg") not in (None, "", "rgba(0, 0, 0, 0)", "transparent")
            and x.get("arrowLeft") is not None
            and x["arrowLeft"] - 1 <= x["center"] <= x["arrowRight"] + 1
            for x in rails
        ),
        rails,
        "m2",
    )

    check(
        name,
        "m2: rows inside a space sit 10px right of top-level rows",
        r.get("spaceFound") and r.get("topFound") and round(r.get("spacePad", -999) - r.get("topPad", -999)) == 10,
        r,
        "m2",
    )

    hd = r.get("heading")
    check(
        name,
        "m2: Sessions heading sits between the last space and the first bucket",
        bool(hd)
        and hd.get("text") == "Sessions"
        and hd.get("maxSpaceBottom") is not None
        and hd.get("bucketTop") is not None
        and hd["top"] >= hd["maxSpaceBottom"] - 1
        and hd["top"] <= hd["bucketTop"] + 1
        and hd.get("borderTopWidth") == "1px",
        hd,
        "m2",
    )


# ---------------------------------------------------------------------------
# Space suggestions — the sidebar rows (v35)
# ---------------------------------------------------------------------------
# seed.py leaves two pending suggestions: a new "Fact checking" space over five
# loose chats with a RULES draft, and a move of three loose chats into the
# seeded "Pernix" space. Both are read-only here — the pass that accepts and
# declines them runs last, once, because it rewrites the seeded database.

SUGGEST_ROWS_JS = r"""() => {
  const rows = [...document.querySelectorAll('.space-suggest-row')].map(r => {
      const b = r.getBoundingClientRect();
      return {
          text: ((r.querySelector('.space-suggest-text') || {}).textContent || '').trim(),
          meta: ((r.querySelector('.space-suggest-meta') || {}).textContent || '').trim(),
          id: r.getAttribute('data-suggestion-id') || '',
          group: r.getAttribute('data-group') || '',
          role: r.getAttribute('role') || '',
          tab: r.getAttribute('tabindex') || '',
          session: r.classList.contains('session-item'),
          top: Math.round(b.top),
          h: Math.round(b.height),
      };
  });
  const open = [...document.querySelectorAll('.space-group-body:not(.collapsed)')];
  const heading = document.querySelector('.sessions-header');
  return {
      rows,
      spaceBottom: open.length
          ? Math.round(Math.max(...open.map(b => b.getBoundingClientRect().bottom))) : null,
      headingTop: heading ? Math.round(heading.getBoundingClientRect().top) : null,
  };
}"""


def suggestion_row_checks(pg, name):
    r = pg.evaluate(SUGGEST_ROWS_JS)
    rows = r.get("rows") or []
    check(
        name,
        "m2: both suggestions render as rows of their own, newest first",
        len(rows) == 2
        and rows[0]["text"] == "Suggested · Fact checking"
        and rows[0]["meta"] == "5 sessions"
        and rows[1]["text"] == "3 chats belong in Pernix"
        and rows[1]["meta"] == "review"
        and all(
            x["group"] == "suggested" and x["role"] == "button" and x["tab"] == "0" and x["id"] and not x["session"]
            for x in rows
        )
        and all(x["h"] >= 28 for x in rows),
        rows,
        "m2",
    )
    check(
        name,
        "m2: suggestions sit under the last space and above the Sessions heading",
        len(rows) == 2
        and r.get("spaceBottom") is not None
        and r.get("headingTop") is not None
        and all(x["top"] >= r["spaceBottom"] - 1 for x in rows)
        and all(x["top"] < r["headingTop"] for x in rows),
        r,
        "m2",
    )


def run_vp(browser, name, w, h, opts):
    ctx = browser.new_context(viewport={"width": w, "height": h}, device_scale_factor=2, color_scheme="dark", **opts)
    pg = ctx.new_page()
    pg.on("console", console_sink(name))
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
            # Scoped off .space-group-body: a space row now sits 10px further
            # right than a plain one, and this check is about the general
            # drawer budget, not about spaces — that guarantee is the desktop
            # "session title inside a space keeps >=180px" check below,
            # deliberately not asserted this tight on a narrow drawer. Without
            # the scope this picked whichever row happened to render first,
            # which on the seeded data is the Pernix space's.
            tw = pg.evaluate(
                "() => { const it=document.querySelector('.session-group-body:not(.space-group-body) .session-item:not(.pinned)'); if(!it) return -1; const t=it.querySelector('.session-title-text, .session-title'); if(!t) return -2; let box=t; while(box && getComputedStyle(box).overflow==='visible' && box!==it) box=box.parentElement; return Math.round(box.getBoundingClientRect().width); }"
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
            space_seam_checks(pg, name)
            # One phone is enough for the drawer's copy of this: the rows are
            # the same DOM at every compact width and the pass that acts on
            # them runs once, at the end, on the desktop tier.
            if name == "phone":
                suggestion_row_checks(pg, name)
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
        if LEVEL == "m2":
            space_seam_checks(pg, name)
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
        # `.fp-tree-item.file` is what file-panel.js actually paints on a file
        # row (a folder and the ".." row get `.dir`, an upload in flight gets
        # `.fp-upload-row`). This used to filter on `.fp-tree-dir`, a class
        # that has never existed anywhere, so the filter passed everything and
        # the check clicked whatever sorted first — opening a FOLDER, and then
        # finding no editor, whenever the throwaway workspace had one.
        pg.evaluate("() => { const f=document.querySelector('.fp-tree-item.file'); f && f.click(); }")
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
    pg.on("console", console_sink("desktop"))
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

    # S2 — the space header's controls are an overlay on this tier, not boxes
    # in the line. In flow they reserved 48px of a 253px row whether or not
    # anyone was pointing at the header, and the seeded long label read at
    # 122px. Asserted on the mouse tier only: the touch header keeps "+" and
    # one 44px overflow button, both in the line by design.
    #
    # The count is deliberately not pinned — the overlay is out of flow, so
    # what it costs the label is zero however many controls it holds (v34
    # added "archive idle sessions" and the label stayed at 188px). What is
    # pinned is that every one of them is a 24px target and none of them is
    # reachable until the header is pointed at.
    if LEVEL == "m2":
        sh = pg.evaluate("""() => { const h=[...document.querySelectorAll('.space-group-header')]
                  .find(x => (x.querySelector('.space-label')||{}).textContent?.startsWith('Research lab'));
                if(!h) return null;
                const a=h.querySelector('.space-actions'), l=h.querySelector('.space-label');
                if(!a || !l) return {overlay: !!a, label: !!l};
                const cs=getComputedStyle(a);
                return {overlay:true, op:cs.opacity, pe:cs.pointerEvents,
                        labelW: Math.round(l.getBoundingClientRect().width),
                        targets: [...a.querySelectorAll('.space-btn')].map(b => {
                            const r=b.getBoundingClientRect();
                            return Math.round(r.width)+'x'+Math.round(r.height); })}; }""")
        check(
            "desktop",
            "m2: space header actions overlay hidden at rest, long label >=180px",
            bool(sh)
            and sh.get("op") == "0"
            and sh.get("pe") == "none"
            and len(sh.get("targets") or []) >= 3
            and all(t == "24x24" for t in sh.get("targets") or [])
            and sh.get("labelW", 0) >= 180,
            sh,
            "m2",
        )
        space_seam_checks(pg, "desktop")
        suggestion_row_checks(pg, "desktop")

        # Desktop only — the same title-box measurement the m2 drawer check
        # above uses, aimed at a row inside a space instead of the drawer's
        # first row. The space's own +10px indent has to come out of the
        # title's ceiling, not out of the space's identity, so it is pinned
        # at 180px (a desktop sidebar is wider than a drawer, hence more than
        # the drawer's 140px) rather than left unpinned.
        tw = pg.evaluate(
            "() => { const it=document.querySelector('.space-group-body .session-item:not(.worker)'); if(!it) return -1; const t=it.querySelector('.session-title-text, .session-title'); if(!t) return -2; let box=t; while(box && getComputedStyle(box).overflow==='visible' && box!==it) box=box.parentElement; return Math.round(box.getBoundingClientRect().width); }"
        )
        check("desktop", "m2: session title inside a space keeps >=180px", tw >= 180, f"title box width={tw}px", "m2")
    ctx.close()
    return out


# ---------------------------------------------------------------------------
# State timeline — the Lane tab, the Story under it, and the Map's colours
# ---------------------------------------------------------------------------

# seed.py's last block builds this session: three turns of state-log rows with
# the messages db.get_turns parses back into a scout report, a reflect chain,
# an eval gate, a compaction and a notice. run.sh only forwards main and
# parent, so it is looked up by title in the throwaway's own sqlite — the same
# file seed.py has just written. (The arc used to be written from HERE, which
# meant the gate's fixtures lived in two files and only one tab had one.)
TIMELINE_TITLE = "State timeline — three turns"

# The seeded turns, as the lane has to draw them: turn id, then each phase's
# share of that turn's own elapsed_ms. Every row is normalised to itself, so
# these are the percentages of the row's bar, not of anything global.
LANE_SHAPE = [
    ("1", [10, 30, 5, 10, 40, 5]),  # a reflect retry: the arc runs twice
    ("2", [10, 25, 20, 40, 5]),  # a compaction round trip in the middle
    ("3", [10, 70, 20]),  # plain, and the one whose Story is asserted
]
LANE_TICKS = [3, 1, 3]  # tool calls per turn — one tick each
LANE_ERROR_TICKS = [1, 0, 1]


def timeline_sid():
    path = os.path.join(os.getcwd(), "data", "sessions.db")
    if not os.path.exists(path):
        return ""
    conn = sqlite3.connect(path, timeout=10)
    try:
        row = conn.execute("SELECT id FROM sessions WHERE title = ? LIMIT 1", (TIMELINE_TITLE,)).fetchone()
        return row[0] if row else ""
    finally:
        conn.close()


TIMELINE = timeline_sid()


def open_timeline(pg, tab=None):
    """Open the seeded timeline session and its State timeline modal."""
    pg.evaluate(f"() => document.querySelector('[data-sid=\"{TIMELINE}\"]')?.click()")
    time.sleep(1.2)
    pg.click("#state-badge")
    time.sleep(0.6)
    if tab:
        pg.evaluate(
            "(name) => [...document.querySelectorAll('#timeline-modal .tab-btn')]"
            ".find(b => b.textContent.trim() === name)?.click()",
            tab,
        )
        time.sleep(0.5)


# The contrast maths, shared by the graph pass and the lane pass. Both ask the
# same question — is what was painted actually readable — of different marks.
CONTRAST_JS = r"""
  const lum = (c) => { const f = (x) => { x/=255; return x<=0.03928 ? x/12.92 : ((x+0.055)/1.055)**2.4; };
                       return 0.2126*f(c[0])+0.7152*f(c[1])+0.0722*f(c[2]); };
  // Two serialisations, and reading the second one as the first is the whole
  // of the bug 3deb575 fixed in readColor(): a computed color-mix() comes back
  // as `color(srgb 0.807 0.845 0.861)` — 0..1 floats — where a plain hex comes
  // back as `rgb(138, 100, 16)`. The lane's segments ARE color-mix() tokens, so
  // a probe that assumed 0..255 would call every one of them black.
  const parse = (s) => { const t=String(s||'').trim(); const m=t.match(/[-+]?(?:\d*\.\d+|\d+)/g);
                         if (!m || m.length < 3) return null;
                         const n = m.slice(0,3).map(Number);
                         return t.startsWith('color(') ? n.map(x => x * 255) : n; };
  const ratio = (a, b) => { const x=lum(a), y=lum(b); return (Math.max(x,y)+0.05)/(Math.min(x,y)+0.05); };
"""

TOKENS_JS = """async () => {
  const t = await import('/static/js/theme.js');
  const out = {};
  for (const k of ['--state-processing-bg','--state-processing-fg','--state-paused-bg',
                   '--state-paused-fg','--accent','--bg']) out[k] = t.hex(k);
  return out; }"""

# The ten states of sessions/state_v2.py, which is what the map draws whether
# or not the session ever visited them.
MAP_STATES = [
    "idle_ready",
    "scouting",
    "processing",
    "finalizing",
    "cancelling",
    "compacting",
    "awaiting_user",
    "awaiting_workers",
    "pause_requested",
    "paused",
]

# The seeded arc, read back off the map as edges rather than as rows: three
# turns, one of which retries (finalizing→scouting) and one of which compacts
# (processing↔compacting). Seven of the 31 edges, and the other 24 stay faint.
MAP_USED_EDGES = {
    "idle_ready||scouting": 3,
    "scouting||processing": 4,
    "processing||finalizing": 4,
    "finalizing||scouting": 1,
    "finalizing||idle_ready": 3,
    "processing||compacting": 1,
    "compacting||processing": 1,
}

STATE_MAP_JS = r"""() => {""" + CONTRAST_JS + r"""
  const bad = [];
  const states = {}, edges = {};
  for (const r of document.querySelectorAll('#timeline-map .tl-map-state')) {
    // The state name is the second class: `tl-map-state tl-map-<name>`.
    const name = [...r.classList].map(c => c.startsWith('tl-map-') && c !== 'tl-map-state' ? c.slice(7) : '')
                   .find(Boolean) || '?';
    const cs = getComputedStyle(r);
    states[name] = {fill: cs.fill, stroke: cs.stroke, width: cs.strokeWidth,
                    current: r.classList.contains('current'), live: r.classList.contains('live')};
    const rc = parse(cs.fill);
    // Every palette colour is a long way from #000; the regression this pass
    // was written for painted them exactly #000000/#010101, so proximity to
    // black — or no fill at all — is the signature.
    if (!rc || cs.fill === 'none' || cs.fill === 'rgba(0, 0, 0, 0)') { bad.push(name + ': box ' + cs.fill); continue; }
    if (Math.max(rc[0], rc[1], rc[2]) <= 8) { bad.push(name + ': box ' + cs.fill); continue; }
    const label = document.querySelector('#timeline-map .tl-map-label.tl-map-' + name);
    const lc = label ? parse(getComputedStyle(label).fill) : null;
    if (!lc) { bad.push(name + ': label has no fill'); continue; }
    const ratioLabel = ratio(rc, lc);
    if (ratioLabel < 3) bad.push(name + ': label ' + ratioLabel.toFixed(2) + ':1 on its box');
  }
  for (const p of document.querySelectorAll('#timeline-map .tl-map-edge')) {
    const cs = getComputedStyle(p);
    edges[p.dataset.edge] = {used: p.classList.contains('used'),
                             op: Math.round(parseFloat(cs.opacity) * 100) / 100,
                             width: parseFloat(cs.strokeWidth)};
  }
  // A count sits at a table coordinate, not on its edge, so pairing the two
  // by position would be fragile. Read them in document order instead: the
  // drawing emits each label immediately after the path it belongs to.
  const labelled = [...document.querySelectorAll('#timeline-map .tl-map-edges > *')]
      .reduce((acc, n) => { if (n.tagName === 'path') acc.last = n.dataset.edge;
                            else if (n.tagName === 'text') acc.out[acc.last] = n.textContent.trim();
                            return acc; }, {out: {}, last: null}).out;
  const flat = [...document.querySelectorAll('.tl-dwell-seg, .tl-dwell-dot')]
      .map(s => getComputedStyle(s).backgroundColor)
      .filter(c => c === 'rgb(0, 0, 0)' || c === 'rgba(0, 0, 0, 0)');
  return {states, edges, labelled, bad, dwell: flat.length,
          svgs: document.querySelectorAll('#timeline-map svg').length,
          overflow: Math.round(document.documentElement.scrollWidth - innerWidth),
          status: (document.querySelector('.timeline-map-status')||{}).textContent || ''}; }"""


def state_map_colours(browser):
    """m1: the State timeline's Map is the machine, painted from the palette.

    Its own context, after the baseline pass, because it opens a session the
    other passes have no reason to touch.

    Two ways readColor() (static/js/theme.js) has handed back the wrong colour,
    each of which painted every box, label and dwell bar black in both themes:

      * the --state-*-fg/-bg pairs are color-mix() expressions, and a
        color-mix() computes to `color(srgb 0.807 0.845 0.861)` — 0..1 floats —
        where a plain hex token computes to `rgb(138, 100, 16)`;
      * the reduced-motion block in tokens.css puts a .01ms transition-duration
        on `*`, which includes the probe span readColor() resolves tokens on, so
        the value read back is the interpolated one — the previous colour, in
        oklab(). Every token then reads as whatever was read first.

    This context asks for reduced motion, so one pass covers both. The map
    itself no longer goes anywhere near that bridge — its rects take their
    --state-* pair from layout.css by class — so the token half of this check
    now guards theme.js on behalf of the sigil and Monaco, and the map half
    asserts that the CSS route is actually wired up.
    """
    problems = []
    if not TIMELINE:
        problems.append(f"no seeded session titled {TIMELINE_TITLE!r}")

    ctx = browser.new_context(viewport={"width": 1280, "height": 800}, color_scheme="dark", reduced_motion="reduce")
    pg = ctx.new_page()
    pg.on("console", console_sink("state-map"))
    pg.on("pageerror", lambda e: console_errors.append(f"[state-map] pageerror: {e}"))
    vendor_reqs = []
    pg.on("request", lambda r: vendor_reqs.append(r.url) if "mermaid" in r.url else None)
    pg.goto(base + "/", wait_until="load")
    time.sleep(1.8)

    toks = pg.evaluate(TOKENS_JS)
    problems += [f"{k} reads {v}" for k, v in toks.items() if v in ("#000000", "#010101")]
    if toks.get("--state-processing-bg") == toks.get("--state-processing-fg"):
        problems.append("state fg and bg read the same: " + str(toks.get("--state-processing-bg")))

    # The Map is not the tab the modal opens on; it draws on the first switch.
    open_timeline(pg, tab="Map")
    try:
        pg.wait_for_selector("#timeline-map .tl-map-state", timeout=15000)
        time.sleep(0.4)
        m = pg.evaluate(STATE_MAP_JS)
        problems += m["bad"]
        if m["dwell"]:
            problems.append(f"{m['dwell']} black time-in-state segments")
        drawn = sorted(m["states"])
        if drawn != sorted(MAP_STATES):
            problems.append(f"states drawn {drawn}")
        if m["svgs"] != 1:
            problems.append(f"{m['svgs']} svgs in the map container")
        # The 31 edges of TRANSITIONS, of which the seeded arc took seven.
        if len(m["edges"]) != 31:
            problems.append(f"{len(m['edges'])} edges drawn, the table has 31")
        used = {k for k, v in m["edges"].items() if v["used"]}
        if used != set(MAP_USED_EDGES):
            problems.append(f"used edges {sorted(used)}")
        for key, n in MAP_USED_EDGES.items():
            if m["labelled"].get(key) != f"{n}×":
                problems.append(f"{key} labelled {m['labelled'].get(key)!r}, seeded {n}×")
        # An unused edge has to be visibly fainter, or "solid where it ran" is
        # not saying anything.
        faint = [v for k, v in m["edges"].items() if not v["used"]]
        if any(v["op"] >= 1 or v["width"] >= 1.6 for v in faint):
            problems.append("an unused edge is drawn as solid as a used one")
        # The seeded session ends parked in idle_ready, so that is the lit box
        # and nothing pulses.
        lit = [k for k, v in m["states"].items() if v["current"]]
        if lit != ["idle_ready"]:
            problems.append(f"lit states {lit}")
        elif m["states"]["idle_ready"]["width"] not in ("2px", "2"):
            problems.append(f"idle_ready lit at stroke-width {m['states']['idle_ready']['width']}")
        if any(v["live"] for v in m["states"].values()):
            problems.append("a state pulses in a session that is parked")
        if m["overflow"] > 0:
            problems.append(f"the map scrolls the page {m['overflow']}px sideways")
    except Exception as e:  # noqa: BLE001
        status = pg.evaluate("() => (document.querySelector('.timeline-map-status')||{}).textContent || ''")
        problems.append(f"map never rendered: {str(e)[:80]} status={status!r}")
    # 3.3MB of diagram library, deleted in this batch. Nothing may ask for it.
    if vendor_reqs:
        problems.append(f"requested {vendor_reqs[0]}")
    pg.screenshot(path=f"{shots}/{tag}-state-map.png")
    check("state-map", "the state map is the machine, painted from the palette", not problems, problems[:6])
    ctx.close()


# --- the Lane tab -----------------------------------------------------------

LANE_JS = r"""() => {""" + CONTRAST_JS + r"""
  const rows = [...document.querySelectorAll('#timeline-lane .tl-lane-row')];
  const box = e => e.getBoundingClientRect();
  const flat = [], dim = [];
  for (const s of document.querySelectorAll('#timeline-lane .tl-lane-seg')) {
    const c = getComputedStyle(s).backgroundColor;
    const p = parse(c);
    if (!p || c === 'rgba(0, 0, 0, 0)' || Math.max(p[0], p[1], p[2]) <= 8) flat.push((s.dataset.state||'?') + ': ' + c);
  }
  const card = document.querySelector('#timeline-story .tl-card');
  const prose = document.querySelector('#timeline-story .tl-story-prose');
  if (card && prose) {
    const r = ratio(parse(getComputedStyle(card).backgroundColor), parse(getComputedStyle(prose).color));
    if (r < 4.5) dim.push('story prose ' + r.toFixed(2) + ':1 on its card');
  } else {
    dim.push('no story prose on screen');
  }
  return {
    tabs: [...document.querySelectorAll('#timeline-modal .tab-btn')].map(b => b.textContent.trim()),
    active: ((document.querySelector('#timeline-modal .tab-btn.active')||{}).textContent||'').trim(),
    turns: rows.map(r => r.dataset.turn),
    current: rows.filter(r => r.getAttribute('aria-current') === 'true').map(r => r.dataset.turn),
    rowH: rows.map(r => Math.round(box(r).height)),
    barW: rows.map(r => { const b = r.querySelector('.tl-lane-bar'); return b ? box(b).width : 0; }),
    segs: rows.map(r => [...r.querySelectorAll('.tl-lane-seg')].map(s => [s.dataset.state, box(s).width])),
    ticks: rows.map(r => r.querySelectorAll('.tl-lane-tick').length),
    errTicks: rows.map(r => r.querySelectorAll('.tl-lane-tick.error').length),
    eyebrows: [...document.querySelectorAll('#timeline-story .tl-card-eyebrow')].map(e => e.textContent.trim()),
    verdicts: [...document.querySelectorAll('#timeline-story .tl-chip-pass, #timeline-story .tl-chip-retry,'
              + ' #timeline-story .tl-chip-escalate, #timeline-story .tl-chip-fail')].map(c => c.textContent.trim()),
    story: (document.getElementById('timeline-story')||{}).textContent || '',
    flat, dim,
    overflow: Math.round(document.documentElement.scrollWidth - innerWidth),
  }; }"""


def lane_geometry(lane):
    """Every way the seeded shape and the drawn shape can disagree."""
    bad = []
    if lane["turns"] != [t for t, _ in LANE_SHAPE]:
        bad.append(f"rows {lane['turns']} not {[t for t, _ in LANE_SHAPE]}")
        return bad
    for i, (turn, shares) in enumerate(LANE_SHAPE):
        segs = lane["segs"][i]
        if len(segs) != len(shares):
            bad.append(f"T{turn}: {len(segs)} segments, seeded {len(shares)}")
            continue
        # A phase's share of its own turn, in pixels of its own bar.
        for (state, width), share in zip(segs, shares):
            want = lane["barW"][i] * share / 100
            if abs(width - want) > 2:
                bad.append(f"T{turn} {state}: {width:.1f}px, {share}% of the bar is {want:.1f}px")
    if "compacting" not in [s[0] for s in lane["segs"][1]]:
        bad.append("T2 has no compacting segment")
    if lane["ticks"] != LANE_TICKS:
        bad.append(f"ticks {lane['ticks']} not {LANE_TICKS}")
    if lane["errTicks"] != LANE_ERROR_TICKS:
        bad.append(f"error ticks {lane['errTicks']} not {LANE_ERROR_TICKS}")
    return bad


def timeline_lane(browser, light=False):
    """m2: the Lane tab draws the seeded turns, and the Story reads the newest.

    Its own context for the same reason the graph pass has one — it opens a
    session nothing else touches, and the light half needs its own profile
    because the theme is a localStorage choice.
    """
    theme = "light" if light else "dark"
    ctx = browser.new_context(viewport={"width": 1280, "height": 900}, color_scheme=theme, reduced_motion="reduce")
    if light:
        ctx.add_init_script("try { localStorage.setItem('pernix_theme', 'light'); } catch (e) {}")
    pg = ctx.new_page()
    pg.on("console", console_sink(f"lane-{theme}"))
    pg.on("pageerror", lambda e: console_errors.append(f"[lane-{theme}] pageerror: {e}"))
    pg.goto(base + "/", wait_until="load")
    time.sleep(1.8)
    open_timeline(pg)
    pg.wait_for_selector("#timeline-lane .tl-lane-row", timeout=15000)
    time.sleep(0.4)
    lane = pg.evaluate(LANE_JS)
    pg.screenshot(path=f"{shots}/{tag}-timeline-lane-{theme}.png")

    if not light:
        check(
            "timeline-lane",
            "m2: the modal opens on Lane, oldest turn first",
            lane["tabs"] == ["Lane", "Map", "Timeline"]
            and lane["active"] == "Lane"
            and lane["turns"] == ["1", "2", "3"]
            and lane["current"] == ["3"],
            lane,
            "m2",
        )
        geom = lane_geometry(lane)
        check("timeline-lane", "m2: each bar is its own turn's phases, to scale", not geom, geom[:6], "m2")
        # The newest turn's story: the plan it opened with, the verdict it
        # closed on, the gate that ran, and what the whole turn cost.
        story = lane["story"]
        missing = [
            s for s in ("Run the suite, read the one failure", "gate tests", "620 / 180 / 800 tok") if s not in story
        ]
        if lane["eyebrows"] != ["Plan", "Act", "Verify"]:
            missing.append(f"cards {lane['eyebrows']}")
        if "pass" not in lane["verdicts"]:
            missing.append(f"verdict chips {lane['verdicts']}")
        check("timeline-lane", "m2: the Story reads the newest turn", not missing, missing[:6], "m2")

        # ArrowUp is the lane's own navigation: previous row, and the story
        # follows the selection rather than waiting for a click.
        pg.evaluate("() => document.querySelector('#timeline-lane .tl-lane-row[data-turn=\"3\"]').focus()")
        pg.keyboard.press("ArrowUp")
        time.sleep(0.4)
        up = pg.evaluate(LANE_JS)
        check(
            "timeline-lane",
            "m2: ArrowUp selects the previous turn and the story follows",
            up["current"] == ["2"] and "Walk the archive year by year" in up["story"] and "Remembers" in up["eyebrows"],
            {"current": up["current"], "eyebrows": up["eyebrows"], "story": up["story"][:160]},
            "m2",
        )

    check(
        "timeline-lane",
        f"m2: {theme} — every phase segment is painted and the story reads",
        not lane["flat"] and not lane["dim"],
        (lane["flat"] + lane["dim"])[:6],
        "m2",
    )
    ctx.close()


def timeline_lane_touch(browser):
    """m2: the lane on a 390px phone — 44px rows, and no sideways scroll.

    The bar is 100% of a grid column by construction, so the only way this
    fails is a row that stops being a grid: a label column that will not
    truncate, or a story field that keeps its key and value side by side.
    """
    ctx = browser.new_context(
        viewport={"width": 390, "height": 844},
        device_scale_factor=2,
        color_scheme="dark",
        is_mobile=True,
        has_touch=True,
    )
    pg = ctx.new_page()
    pg.on("console", console_sink("lane-touch"))
    pg.on("pageerror", lambda e: console_errors.append(f"[lane-touch] pageerror: {e}"))
    pg.goto(base + "/", wait_until="load")
    time.sleep(1.8)
    open_session(pg, TIMELINE, True)
    pg.click("#state-badge")
    time.sleep(0.8)
    pg.wait_for_selector("#timeline-lane .tl-lane-row", timeout=15000)
    time.sleep(0.4)
    lane = pg.evaluate(LANE_JS)
    sheet = rect(pg, "#timeline-modal")
    pg.screenshot(path=f"{shots}/{tag}-timeline-lane-touch.png")
    check(
        "timeline-lane",
        "m2: touch — lane rows are 44px and nothing scrolls sideways",
        lane["turns"] == ["1", "2", "3"]
        and all(h >= 44 for h in lane["rowH"])
        and lane["overflow"] <= 0
        and bool(sheet)
        and sheet["w"] == 390,
        {"rowH": lane["rowH"], "overflow": lane["overflow"], "sheet": sheet},
        "m2",
    )
    ctx.close()


def sidebar_resizer(browser):
    """The desktop tier's own control: drag the sidebar's edge to resize it.

    Its own context, after the baseline pass, because it writes
    pernix:sidebar-width — a stored width would move every box the baseline
    records if the two shared a browser profile.
    """
    ctx = browser.new_context(viewport={"width": 1280, "height": 800}, color_scheme="dark", reduced_motion="reduce")
    pg = ctx.new_page()
    pg.on("console", console_sink("resizer"))
    pg.on("pageerror", lambda e: console_errors.append(f"[resizer] pageerror: {e}"))
    pg.goto(base + "/", wait_until="load")
    time.sleep(1.8)

    h = pg.evaluate("""() => { const e=document.getElementById('sidebar-resizer'); if(!e) return null;
        const r=e.getBoundingClientRect();
        return {role:e.getAttribute('role'), orient:e.getAttribute('aria-orientation'),
                now:e.getAttribute('aria-valuenow'), min:e.getAttribute('aria-valuemin'),
                l:Math.round(r.left), w:Math.round(r.width),
                cursor:getComputedStyle(e).cursor}; }""")
    check(
        "resizer",
        "m2: handle is a labelled vertical separator",
        h and h["role"] == "separator" and h["orient"] == "vertical" and h["cursor"] == "col-resize",
        h,
        "m2",
    )
    check(
        "resizer",
        "m2: handle sits on the sidebar's seam at the default width",
        h and h["w"] == 6 and h["l"] == 267 and h["now"] == "270" and h["min"] == "200",
        h,
        "m2",
    )

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
    check(
        "resizer",
        "m2: double-click restores 270 and drops the stored width",
        reset[0] == 270 and reset[1] is None,
        reset,
        "m2",
    )
    ctx.close()


# ---------------------------------------------------------------------------
# The sidebar at a thousand sessions
# ---------------------------------------------------------------------------

SPACE_PROBE = r"""(label) => {
  const h = [...document.querySelectorAll('.space-group-header')]
      .find(x => (x.querySelector('.space-label')||{}).textContent === label);
  if (!h) return null;
  const body = h.nextElementSibling;
  const sa = body.querySelector('.space-show-all');
  return {
    buckets: [...body.querySelectorAll('.space-bucket-header')].map(b => ({
        label: b.querySelector('.space-bucket-label').textContent,
        collapsed: b.classList.contains('collapsed'),
        count: b.querySelector('.sg-count').hidden ? null : b.querySelector('.sg-count').textContent,
        h: Math.round(b.getBoundingClientRect().height)})),
    rows: body.querySelectorAll('.session-item').length,
    showAll: sa ? sa.textContent : null,
    showAllH: sa ? Math.round(sa.getBoundingClientRect().height) : 0,
    headerCount: (h.querySelector('.sg-count')||{}).textContent}; }"""

CLICK_SHOW_ALL = r"""(label) => {
  const h = [...document.querySelectorAll('.space-group-header')]
      .find(x => (x.querySelector('.space-label')||{}).textContent === label);
  h.nextElementSibling.querySelector('.space-show-all').click(); }"""

LEGEND_STATE = r"""async () => {
  const m = await import('/static/js/components/sidebar.js');
  const entry = document.querySelector('.legend-item[data-type="canary"]');
  return {hidden: m.getHiddenTypes(), query: m.sessionsQuery(),
          canaryRows: document.querySelectorAll('.session-item .session-dot.canary').length,
          rows: document.querySelectorAll('.session-item').length,
          count: (entry.querySelector('.legend-count')||{}).textContent,
          entryHidden: entry.hidden}; }"""


def sidebar_scale(browser):
    """m2: what keeps the sidebar readable once there are a thousand sessions.

    Its own context, after the baseline pass, because everything here writes
    pernix:sidebar — the hidden types, the per-space folds, the "show all".

    showArchived is seeded before the first paint for a reason. app.js builds
    the main list's URL and this commit does not own that file, so the request
    that proves the exclusion reaches the SERVER is the one sidebar.js makes
    itself: the archive's fetch, which takes the same sessionsQuery(). When
    app.js's own fetch carries it too —

        const data = await get(`/api/sessions?limit=${_sessionWindow}${sessionsQuery()}`);

    — the same assertion covers the main list without changing a line here.
    """
    ctx = browser.new_context(viewport={"width": 1280, "height": 800}, color_scheme="dark", reduced_motion="reduce")
    ctx.add_init_script(
        "try { localStorage.setItem('pernix:sidebar', JSON.stringify({showArchived: true})); } catch (e) {}"
    )
    pg = ctx.new_page()
    pg.on("console", console_sink("scale"))
    pg.on("pageerror", lambda e: console_errors.append(f"[scale] pageerror: {e}"))
    pg.goto(base + "/", wait_until="load")
    time.sleep(1.8)

    # --- a space groups its sessions by time, and folds the long tail
    rest = pg.evaluate(SPACE_PROBE, "Scale")
    labels = [b["label"] for b in (rest or {}).get("buckets", [])]
    older = next((b for b in (rest or {}).get("buckets", []) if b["label"] == "Older"), None)
    check(
        "sidebar-scale",
        "m2: a 20-session space buckets by time, Older folded, Show all 20 at the end",
        bool(rest)
        and labels == ["Today", "Yesterday", "This Week", "This Month", "Older"]
        and bool(older)
        and older["collapsed"]
        and older["count"] == "4"
        and rest["showAll"] == "Show all 20"
        and rest["headerCount"] == "20"
        and all(b["h"] >= 28 for b in rest["buckets"])
        and rest["showAllH"] >= 28,
        rest,
        "m2",
    )
    # The point of the fold: a space builds a readable number of rows, not one
    # per session. 47 of them in the DOM six times a minute is what this cost.
    check(
        "sidebar-scale",
        "m2: at rest the space holds at most 15 rows",
        bool(rest) and 0 < rest["rows"] <= 15,
        rest,
        "m2",
    )
    pg.evaluate(CLICK_SHOW_ALL, "Scale")
    time.sleep(0.7)
    shown = pg.evaluate(SPACE_PROBE, "Scale")
    check(
        "sidebar-scale",
        "m2: Show all unfolds every bucket and every one of the 20",
        bool(shown)
        and shown["rows"] == 20
        and shown["showAll"] == "Show fewer"
        and not any(b["collapsed"] for b in shown["buckets"]),
        shown,
        "m2",
    )
    pg.screenshot(path=f"{shots}/{tag}-desktop-space-expanded.png")
    pg.evaluate(CLICK_SHOW_ALL, "Scale")  # back to at-rest for the legend pass
    time.sleep(0.6)

    # --- hiding a type in the legend leaves it out of the request
    reqs = []
    pg.on("request", lambda r: reqs.append(r.url) if "/api/sessions?" in r.url else None)
    pg.evaluate("() => document.querySelector('.legend-item[data-type=\"canary\"]').click()")
    time.sleep(1.6)
    leg = pg.evaluate(LEGEND_STATE)
    carried = [u for u in reqs if "exclude_types=canary" in u]
    check(
        "sidebar-scale",
        "m2: hiding Self-check leaves canaries off the page and out of the request",
        leg["hidden"] == ["canary"]
        and leg["query"] == "&exclude_types=canary"
        and leg["canaryRows"] == 0
        and leg["rows"] > 0
        and bool(carried),
        {**leg, "carried": carried[:2], "requests": reqs[:4]},
        "m2",
    )
    # A count that fell to zero when the type was switched off would erase the
    # only control that switches it back on.
    check(
        "sidebar-scale",
        "m2: the legend keeps naming the type it is hiding",
        leg["count"] == "30" and not leg["entryHidden"],
        leg,
        "m2",
    )
    pg.screenshot(path=f"{shots}/{tag}-desktop-legend-filtered.png")
    ctx.close()


# ---------------------------------------------------------------------------
# Space suggestions — the review sheet, the accept and the decline
# ---------------------------------------------------------------------------
# This pass REWRITES the seeded database: accepting creates a space and moves
# five chats into it, declining marks a topic refused. run.sh boots one server
# and seeds once per run, so it goes last and runs exactly once — anything
# asserted after it would be asserting against a fixture this changed.

OPEN_SUGGESTION_JS = r"""(needle) => {
  const r = [...document.querySelectorAll('.space-suggest-row')]
      .find(x => ((x.querySelector('.space-suggest-text') || {}).textContent || '').includes(needle));
  if (!r) return false;
  r.click();
  return true;
}"""

SHEET_JS = r"""() => {
  const card = document.querySelector('.sugg-sheet-card');
  if (!card) return null;
  const boxes = [...card.querySelectorAll('.sugg-member-box')];
  const area = card.querySelector('.sugg-dir-text');
  const pre = card.querySelector('.sugg-dir-default');
  const primary = card.querySelector('.sugg-accept');
  return {
      title: ((card.querySelector('.modal-header h2') || {}).textContent || '').trim(),
      why: ((card.querySelector('.sugg-why') || {}).textContent || '').trim(),
      members: boxes.length,
      checked: boxes.filter(b => b.checked).length,
      tabs: [...card.querySelectorAll('.sugg-dir-tabbar .tab-btn')].map(b => b.textContent.trim()),
      primary: primary ? primary.textContent.trim() : '',
      buttons: [...card.querySelectorAll('.modal-footer button')].map(b => b.textContent.trim()),
      skip: ((card.querySelector('.sugg-dir-skip') || {}).textContent || '').trim(),
      defaultHead: pre ? pre.textContent.slice(0, 60) : '',
      areaHead: area ? area.value.slice(0, 60) : '',
      areaTail: area ? area.value.trim().slice(-51) : '',
      nameFields: card.querySelectorAll('.space-label-input').length,
  };
}"""

AFTER_ACCEPT_JS = r"""() => {
  const h = [...document.querySelectorAll('.space-group-header')]
      .find(x => ((x.querySelector('.space-label') || {}).textContent || '') === 'Fact checking');
  const body = h ? h.nextElementSibling : null;
  return {
      space: !!h,
      count: h ? ((h.querySelector('.sg-count') || {}).textContent || '') : '',
      rows: body ? body.querySelectorAll('.session-item').length : 0,
      titles: body
          ? [...body.querySelectorAll('.session-title-text')].map(t => t.textContent.trim()) : [],
      suggestions: [...document.querySelectorAll('.space-suggest-row')]
          .map(r => ((r.querySelector('.space-suggest-text') || {}).textContent || '').trim()),
      sheet: !!document.querySelector('.sugg-sheet-card'),
  };
}"""

DECLINED_JS = r"""() => {
  const host = document.querySelector('.sugg-declined');
  if (!host) return null;
  return {
      visible: !!host.offsetParent,
      rows: [...host.querySelectorAll('.sugg-declined-row')].map(r => ({
          label: ((r.querySelector('.sugg-declined-label') || {}).textContent || '').trim(),
          meta: ((r.querySelector('.sugg-declined-meta') || {}).textContent || '').trim(),
      })),
      notes: [...host.querySelectorAll('.sugg-note')].map(n => n.textContent.trim()),
      clearAll: !!host.querySelector('.sugg-declined-all button'),
  };
}"""

CLEAR_DECLINED_JS = r"""(label) => {
  const row = [...document.querySelectorAll('.sugg-declined-row')]
      .find(r => ((r.querySelector('.sugg-declined-label') || {}).textContent || '').includes(label));
  if (!row) return false;
  row.querySelector('button').click();
  return true;
}"""


def space_suggestions_flow(browser):
    ctx = browser.new_context(viewport={"width": 1280, "height": 800}, color_scheme="dark", reduced_motion="reduce")
    pg = ctx.new_page()
    pg.on("console", console_sink("suggest"))
    pg.on("pageerror", lambda e: console_errors.append(f"[suggest] pageerror: {e}"))
    pg.goto(base + "/", wait_until="load")
    time.sleep(2.0)

    # --- the new-space sheet
    opened = pg.evaluate(OPEN_SUGGESTION_JS, "Fact checking")
    time.sleep(1.0)
    sheet = pg.evaluate(SHEET_JS) if opened else None
    pg.screenshot(path=f"{shots}/{tag}-desktop-suggest-sheet.png")
    check(
        "suggestions",
        "m2: the sheet offers five ticked chats, a name and the three buttons",
        bool(sheet)
        and sheet["title"] == "Make this a space?"
        and sheet["members"] == 5
        and sheet["checked"] == 5
        and sheet["nameFields"] == 1
        and sheet["primary"] == "Create space"
        and "Not now" in sheet["buttons"]
        and "Don’t suggest this" in sheet["buttons"]
        and bool(sheet["why"]),
        sheet,
        "m2",
    )
    check(
        "suggestions",
        "m2: the RULES tab holds the default with the drafted section appended",
        bool(sheet)
        and sheet["tabs"] == ["RULES"]
        and len(sheet["defaultHead"]) > 20
        and sheet["areaHead"] == sheet["defaultHead"]
        and sheet["areaTail"] == "- Separate the claim, the evidence and the verdict."
        and sheet["skip"] == "Use the default instead",
        sheet,
        "m2",
    )

    # --- accepting it: the space appears with those five chats, the row goes
    pg.evaluate("() => document.querySelector('.sugg-accept').click()")
    time.sleep(2.5)
    after = pg.evaluate(AFTER_ACCEPT_JS)
    pg.screenshot(path=f"{shots}/{tag}-desktop-suggest-accepted.png")
    check(
        "suggestions",
        "m2: accepting builds the space, files the five chats and drops the row",
        after["space"]
        and after["count"] == "5"
        and after["rows"] == 5
        and any("Verify the three citations" in t for t in after["titles"])
        and not after["sheet"]
        and after["suggestions"] == ["3 chats belong in Pernix"],
        after,
        "m2",
    )

    # --- the move sheet, then declining it
    pg.evaluate(OPEN_SUGGESTION_JS, "belong in Pernix")
    time.sleep(1.0)
    move = pg.evaluate(SHEET_JS)
    check(
        "suggestions",
        "m2: a move names its target, has no name field and counts what it would move",
        bool(move)
        and move["title"] == "Move these into Pernix?"
        and move["members"] == 3
        and move["checked"] == 3
        and move["nameFields"] == 0
        and move["tabs"] == []
        and move["primary"] == "Move 3 sessions",
        move,
        "m2",
    )
    pg.evaluate("() => document.querySelector('.sugg-reject').click()")
    time.sleep(2.0)
    gone = pg.evaluate(
        "() => ({rows: document.querySelectorAll('.space-suggest-row').length,"
        " sheet: !!document.querySelector('.sugg-sheet-card')})"
    )
    check(
        "suggestions",
        "m2: declining closes the sheet and takes the row off the list",
        gone["rows"] == 0 and not gone["sheet"],
        gone,
        "m2",
    )

    # --- and the declined topic is listed, with the control that re-arms it
    pg.click("#settings-btn")
    time.sleep(1.2)
    pg.evaluate("() => document.querySelector('.tab-btn[data-tab=\"autonomy\"]').click()")
    time.sleep(1.0)
    dec = pg.evaluate(DECLINED_JS)
    pg.screenshot(path=f"{shots}/{tag}-desktop-suggest-declined.png")
    check(
        "suggestions",
        "m2: Autonomy & idle work lists the declined topic and says it can come back",
        bool(dec)
        and dec["visible"]
        and len(dec["rows"]) == 1
        and dec["rows"][0]["label"] == "Pernix deploys → Pernix"
        and "3 chats" in dec["rows"][0]["meta"]
        and dec["clearAll"]
        and "Cleared topics can be suggested again." in dec["notes"],
        dec,
        "m2",
    )
    pg.evaluate(CLEAR_DECLINED_JS, "Pernix deploys")
    time.sleep(1.2)
    cleared = pg.evaluate(DECLINED_JS)
    check(
        "suggestions",
        "m2: Clear empties the declined list",
        bool(cleared) and cleared["rows"] == [] and "Nothing declined." in cleared["notes"],
        cleared,
        "m2",
    )
    ctx.close()


# ---------------------------------------------------------------------------
# The thumbs on an assistant answer, and the Trust tab beside them
# ---------------------------------------------------------------------------
# Both surfaces call routes that arrive with the trust-loop backend (W2), so
# these passes supply the routes themselves with page.route(). That is not a
# workaround: stubbing is the only way to drive the whole loop — a stored
# rating, a toggle, an undo, an optional note — deterministically, without a
# turn actually running. The un-stubbed half (this server has neither route)
# is asserted by trust_loop_absent() at the end, because "the controls are
# simply not there" is what the client promises on an older server.

TRUST_PAYLOAD = {
    "grader": {
        "agreement": 0.72,
        "n": 25,
        "holdout": {"accuracy": 0.9, "n": 10, "ran_at": "2026-09-04T00:00:00Z", "model": "qwen3-27b"},
    },
    "outcomes": {
        "by_source": {"llm": 140, "next_turn": 31, "user": 9},
        "graded_7d": 180,
        "user_turns_7d": 190,
    },
    "entries": {"by_status": {"active": 12, "retired": 3}, "unfounded": 2},
    "canaries": {"contaminated_14d": 0, "runs_14d": 44, "fails_14d": 1},
    "trials": [
        {
            "entry_id": "prefer-rg",
            "title": "Prefer ripgrep over find",
            "treated": {"n": 41, "successes": 33},
            "control": {"n": 39, "successes": 25},
            "p": 0.0412,
            "status": "running",
        }
    ],
}

# The toolbar (or sheet trigger) on the first assistant answer and the first
# user message: what each carries, what it is named, and how big it is.
ACTIONS_JS = r"""() => {
  const read = (m) => m ? [...m.querySelectorAll('.msg-actions button')].map(b => {
      const r = b.getBoundingClientRect();
      const svg = b.querySelector('svg');
      return {cls: b.className, label: b.getAttribute('aria-label'),
              pressed: b.getAttribute('aria-pressed'),
              icon: svg ? svg.getAttribute('class') : '',
              w: Math.round(r.width), h: Math.round(r.height)};
  }) : null;
  const a = document.querySelector('#messages-inner .message.assistant[data-message-id]');
  const u = document.querySelector('#messages-inner .message.user[data-message-id]');
  return {mid: a ? a.dataset.messageId : null, assistant: read(a), user: read(u),
          anyThumb: document.querySelectorAll('.msg-feedback-btn').length};
}"""

SHEET_ROWS_JS = r"""() => {
  const card = document.querySelector('.sheet-card');
  if (!card) return null;
  return {title: (card.querySelector('.sheet-title')||{textContent:''}).textContent.trim(),
          items: [...card.querySelectorAll('.sheet-item')].map(i =>
              (i.querySelector('.sheet-item-label')||{textContent:''}).textContent.trim()),
          hints: [...card.querySelectorAll('.sheet-item-hint')].map(h => h.textContent.trim())};
}"""

TRUST_JS = r"""() => {
  const c = document.getElementById('fp-trust');
  if (!c) return null;
  const t = (n) => (n ? n.textContent.trim() : '');
  // A number pushed against the panel's own edge is the shape of a tab that
  // forgot to take the Explorer's padding — which is exactly what happened
  // the first time this tab was written.
  const panel = document.getElementById('file-panel');
  const edge = panel ? panel.getBoundingClientRect().right : 0;
  return {children: c.children.length,
          cut: [...c.querySelectorAll('.trust-stat-row, .trust-trial')]
                 .filter(r => r.getBoundingClientRect().right > edge - 2).length,
          stats: [...c.querySelectorAll('.trust-stat')].map(s => ({
              label: t(s.querySelector('.trust-stat-label')),
              value: t(s.querySelector('.trust-stat-value')),
              note: t(s.querySelector('.trust-stat-note'))})),
          trials: [...c.querySelectorAll('.trust-trial')].map(x => x.textContent.replace(/\s+/g, ' ').trim()),
          empties: [...c.querySelectorAll('.adaptive-empty')].map(x => x.textContent.trim()),
          text: c.textContent}; }"""


def _settle(probe, timeout=8.0, step=0.15):
    """Poll `probe` until it answers with something truthy, or give up.

    Every surface here waits on a fetch the page made, and a flat sleep long
    enough to be safe on a loaded machine is a flat sleep wasted on every
    other run. On a timeout the caller re-reads the raw state, so a genuine
    failure still reports what was actually on screen.
    """
    deadline = time.time() + timeout
    value = probe()
    while not value and time.time() < deadline:
        time.sleep(step)
        value = probe()
    return value


def _actions(pg, thumbs=True):
    """The message toolbar, once the transcript has actually rendered."""

    def probe():
        r = pg.evaluate(ACTIONS_JS)
        if not r or not r["assistant"]:
            return None
        if thumbs and not r["anyThumb"]:
            return None
        return r

    return _settle(probe) or pg.evaluate(ACTIONS_JS)


def _writes(posts, n):
    """Wait for the client's nth write to reach the stub."""
    _settle(lambda: len(posts) >= n or None)
    return posts


def _stub_feedback(ctx, posts, items=None):
    """Answer both feedback routes, and record every write the client makes."""
    body = json.dumps({"items": items or []})

    def handler(route):
        req = route.request
        if req.method != "POST":
            route.fulfill(status=200, content_type="application/json", body=body)
            return
        try:
            payload = json.loads(req.post_data or "{}")
        except Exception:
            payload = {"unparsed": req.post_data}
        posts.append(payload)
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "message_id": req.url.rstrip("/").split("/")[-2],
                    "signal": payload.get("signal"),
                    "note": payload.get("note"),
                }
            ),
        )

    ctx.route("**/feedback", handler)


def _stub_trust(ctx, state):
    ctx.route(
        "**/api/trust",
        lambda route: route.fulfill(status=200, content_type="application/json", body=json.dumps(state["payload"])),
    )


def _open_trust_tab(pg):
    pg.click("#files-btn")
    time.sleep(0.9)
    pg.evaluate("() => document.getElementById('fp-group-tuning')?.click()")
    time.sleep(0.6)
    pg.evaluate("() => document.getElementById('fp-tab-trust')?.click()")
    time.sleep(1.0)


def message_feedback(browser):
    """m2 (mouse): the thumbs live in the hover toolbar, and they toggle."""
    posts = []
    ctx = browser.new_context(viewport={"width": 1280, "height": 800}, color_scheme="dark")
    _stub_feedback(ctx, posts)
    pg = ctx.new_page()
    pg.on("console", console_sink("feedback"))
    pg.on("pageerror", lambda e: console_errors.append(f"[feedback] pageerror: {e}"))
    pg.goto(base + "/", wait_until="load")
    time.sleep(1.6)
    open_session(pg, MAIN, False)

    a = _actions(pg)
    pg.screenshot(path=f"{shots}/{tag}-desktop-feedback-rest.png")
    check(
        "feedback",
        "m2: an assistant answer carries copy plus both thumbs, unpressed",
        bool(a)
        and a["mid"]
        and [b["label"] for b in a["assistant"]] == ["Copy message", "Helpful", "Not helpful"]
        and [b["pressed"] for b in a["assistant"]] == [None, "false", "false"],
        a,
        "m2",
    )
    check(
        "feedback",
        "m2: a user message is never rated",
        bool(a) and all("msg-feedback" not in b["cls"] for b in (a["user"] or [])),
        a["user"] if a else None,
        "m2",
    )

    pg.evaluate("() => document.querySelector('.msg-feedback-up').click()")
    _writes(posts, 1)
    up = _settle(lambda: (lambda r: r if r["assistant"][1]["pressed"] == "true" else None)(pg.evaluate(ACTIONS_JS)))
    up = up or pg.evaluate(ACTIONS_JS)
    pg.screenshot(path=f"{shots}/{tag}-desktop-feedback-up.png")
    check(
        "feedback",
        "m2: thumbs-up presses, fills its icon and posts signal=up",
        up["assistant"][1]["pressed"] == "true"
        and "thumb-up-filled" in up["assistant"][1]["icon"]
        and up["assistant"][2]["pressed"] == "false"
        and posts == [{"signal": "up"}],
        {"btn": up["assistant"][1], "posts": posts},
        "m2",
    )

    pg.evaluate("() => document.querySelector('.msg-feedback-up').click()")
    _writes(posts, 2)
    off = _settle(lambda: (lambda r: r if r["assistant"][1]["pressed"] == "false" else None)(pg.evaluate(ACTIONS_JS)))
    off = off or pg.evaluate(ACTIONS_JS)
    check(
        "feedback",
        "m2: pressing it again clears the rating and posts signal=null",
        off["assistant"][1]["pressed"] == "false"
        and "thumb-up-filled" not in off["assistant"][1]["icon"]
        and posts[-1] == {"signal": None},
        {"btn": off["assistant"][1], "posts": posts[-1:]},
        "m2",
    )

    focusable = pg.evaluate(
        "() => { const b=document.querySelector('.msg-feedback-down'); b.focus();"
        " return document.activeElement === b; }"
    )
    check("feedback", "m2: a thumb takes keyboard focus", focusable, "", "m2")

    pg.evaluate("() => document.querySelector('.msg-feedback-down').click()")
    _writes(posts, 3)
    note = _settle(
        lambda: pg.evaluate(
            "() => { const c=document.querySelector('.msg-note-card'); if(!c) return null;"
            " return {heading: c.querySelector('h2').textContent.trim(),"
            "  focused: document.activeElement === c.querySelector('.msg-note-input'),"
            "  buttons: [...c.querySelectorAll('.modal-footer button')].map(b=>b.textContent.trim())}; }"
        )
    )
    pg.screenshot(path=f"{shots}/{tag}-desktop-feedback-note.png")
    check(
        "feedback",
        "m2: thumbs-down saves first, then offers a skippable note",
        bool(note) and note["focused"] and note["buttons"] == ["Skip", "Save note"] and posts[-1] == {"signal": "down"},
        {"note": note, "posts": posts[-1:]},
        "m2",
    )
    pg.fill(".msg-note-input", "it answered a different question")
    pg.evaluate(
        "() => [...document.querySelectorAll('.msg-note-card .modal-footer button')]"
        ".find(b => b.textContent.trim() === 'Save note').click()"
    )
    _writes(posts, 4)
    gone = _settle(lambda: pg.evaluate("() => !document.querySelector('.msg-note-card')"))
    check(
        "feedback",
        "m2: the note closes the prompt and rides a second write",
        gone and posts[-1] == {"signal": "down", "note": "it answered a different question"},
        {"posts": posts[-1:]},
        "m2",
    )
    ctx.close()


def message_feedback_touch(browser):
    """m2 (finger): one overflow button instead of a toolbar, thumbs as rows."""
    posts = []
    ctx = browser.new_context(
        viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True, color_scheme="dark"
    )
    _stub_feedback(ctx, posts)
    pg = ctx.new_page()
    pg.on("console", console_sink("feedback-touch"))
    pg.on("pageerror", lambda e: console_errors.append(f"[feedback-touch] pageerror: {e}"))
    pg.goto(base + "/", wait_until="load")
    time.sleep(1.6)
    open_session(pg, MAIN, True)

    a = _actions(pg, thumbs=False)
    check(
        "feedback-touch",
        "m2: an assistant answer collapses to one 36px overflow button",
        bool(a)
        and len(a["assistant"] or []) == 1
        and "msg-menu-btn" in a["assistant"][0]["cls"]
        and a["assistant"][0]["w"] >= 36
        and a["assistant"][0]["h"] >= 36,
        a["assistant"] if a else None,
        "m2",
    )
    pg.evaluate("() => document.querySelector('.msg-menu-btn').click()")
    sheet = _settle(lambda: pg.evaluate(SHEET_ROWS_JS))
    pg.screenshot(path=f"{shots}/{tag}-phone-feedback-sheet.png")
    check(
        "feedback-touch",
        "m2: its sheet holds copy and both thumbs, named in words",
        bool(sheet) and sheet["items"] == ["Copy message", "Helpful", "Not helpful"],
        sheet,
        "m2",
    )
    pg.evaluate(
        "() => [...document.querySelectorAll('.sheet-item')]"
        ".find(i => i.textContent.trim().startsWith('Helpful')).click()"
    )
    _writes(posts, 1)
    closed = _settle(lambda: pg.evaluate("() => !document.querySelector('.sheet-card')"))
    check(
        "feedback-touch",
        "m2: picking Helpful writes the rating and closes the sheet",
        posts == [{"signal": "up"}] and closed,
        posts,
        "m2",
    )
    pg.evaluate("() => document.querySelector('.msg-menu-btn').click()")
    again = _settle(lambda: (lambda r: r if r and r["hints"] else None)(pg.evaluate(SHEET_ROWS_JS)))
    again = again or pg.evaluate(SHEET_ROWS_JS)
    check(
        "feedback-touch",
        "m2: the sheet marks the rating already given",
        bool(again) and again["hints"] == ["your rating"],
        again,
        "m2",
    )
    pg.keyboard.press("Escape")
    time.sleep(0.3)
    ctx.close()


def trust_tab(browser):
    """m2: the Trust tab is counts, with a real empty state for the trials."""
    state = {"payload": json.loads(json.dumps(TRUST_PAYLOAD))}
    ctx = browser.new_context(viewport={"width": 1280, "height": 800}, color_scheme="dark")
    _stub_trust(ctx, state)
    _stub_feedback(ctx, [])
    pg = ctx.new_page()
    pg.on("console", console_sink("trust"))
    pg.on("pageerror", lambda e: console_errors.append(f"[trust] pageerror: {e}"))
    pg.goto(base + "/", wait_until="load")
    time.sleep(1.6)
    _open_trust_tab(pg)

    t = _settle(lambda: (lambda r: r if r and r["stats"] else None)(pg.evaluate(TRUST_JS)))
    t = t or pg.evaluate(TRUST_JS) or {}
    pg.screenshot(path=f"{shots}/{tag}-desktop-trust.png")
    want = [
        ("Reflect agrees with the user", "72%"),
        ("Hold-out accuracy", "90%"),
        ("You said so", "9"),
        ("Your next message", "31"),
        ("Reflect said so", "140"),
        ("Turns graded (7d)", "180"),
        ("active", "12"),
        ("retired", "3"),
        ("Unfounded", "2"),
        ("Runs", "44"),
        ("Failures", "1"),
        ("Contaminated", "0"),
    ]
    got = [(s["label"], s["value"]) for s in t.get("stats", [])]
    check(
        "trust",
        "m2: every /api/trust number renders, with its sample size beside it",
        got == want
        and "25 turns" in t["stats"][0]["note"]
        and "10 fixtures" in t["stats"][1]["note"]
        and "190 turns you sent" in t["stats"][5]["note"],
        {"got": got, "notes": [s["note"] for s in t.get("stats", [])][:6]},
        "m2",
    )
    check(
        "trust",
        "m2: no number is pushed against the panel edge",
        t.get("cut") == 0,
        {"cut": t.get("cut")},
        "m2",
    )
    check(
        "trust",
        "m2: a trial reports both arms and its p-value",
        len(t.get("trials") or []) == 1
        and "Prefer ripgrep over find" in t["trials"][0]
        and "treated 33/41 \u00b7 control 25/39 \u00b7 p 0.041" in t["trials"][0],
        t.get("trials"),
        "m2",
    )

    # The batch that ships trial arms is later than the one that ships this
    # tab, so the empty state is the state it will actually open in.
    state["payload"]["trials"] = []
    pg.evaluate(
        "() => [...document.querySelectorAll('#fp-trust .adaptive-btn')]"
        ".find(b => b.textContent.trim().endsWith('Refresh')).click()"
    )
    empty = _settle(lambda: (lambda r: r if r and r["stats"] and not r["trials"] else None)(pg.evaluate(TRUST_JS)))
    empty = empty or pg.evaluate(TRUST_JS) or {}
    check(
        "trust",
        "m2: with no trials the section says so rather than going blank",
        empty.get("trials") == []
        and any(e.startswith("No entries on trial") for e in empty.get("empties", []))
        and "Trial arms (0)" in empty.get("text", ""),
        empty.get("empties"),
        "m2",
    )
    ctx.close()


def trust_loop_absent(browser):
    """m2: on a server without the 3.2 routes, neither surface breaks."""
    ctx = browser.new_context(viewport={"width": 1280, "height": 800}, color_scheme="dark")
    pg = ctx.new_page()
    pg.on("console", console_sink("absent"))
    pg.on("pageerror", lambda e: console_errors.append(f"[absent] pageerror: {e}"))
    pg.goto(base + "/", wait_until="load")
    time.sleep(1.6)
    open_session(pg, MAIN, False)

    a = _actions(pg, thumbs=False)
    check(
        "absent",
        "m2: no rating store means no thumbs, and copy is untouched",
        bool(a) and a["anyThumb"] == 0 and [b["label"] for b in a["assistant"]] == ["Copy message"],
        a["assistant"] if a else None,
        "m2",
    )
    _open_trust_tab(pg)
    t = _settle(lambda: (lambda r: r if r and r["children"] else None)(pg.evaluate(TRUST_JS)))
    t = t or pg.evaluate(TRUST_JS)
    pg.screenshot(path=f"{shots}/{tag}-desktop-trust-absent.png")
    check(
        "absent",
        "m2: the Trust tab says one line and draws nothing else",
        bool(t) and t["children"] == 1 and t["text"].strip() == "Trust metrics need the 3.2 backend",
        t,
        "m2",
    )

    # The same question in Settings, and the sharper edge of it. A row for a
    # key this server does not publish cannot be saved — and, because an unset
    # key is undefined where an unticked box is false, collectChanges() reads
    # it as an edit, so Escape asks whether to discard changes nobody made and
    # the modal never closes. The rows land on their own once the server has
    # the keys.
    pg.evaluate("() => document.querySelector('.fp-close')?.click()")
    time.sleep(0.4)
    pg.click("#settings-btn")
    time.sleep(1.2)
    rows = pg.evaluate("() => [...document.querySelectorAll('.setting-row')].map(r => r.id)")
    pg.keyboard.press("Escape")
    time.sleep(0.7)
    closed = pg.evaluate("() => !document.querySelector('.settings-card') && !document.querySelector('.confirm-card')")
    pending = [r for r in rows if r.split("row-")[-1] in PENDING_SETTINGS]
    check(
        "absent",
        "m2: Settings hides a row this server has no key for, and still closes",
        closed and not pending and "row-reflect_enabled" in rows,
        {"closed": closed, "pending_rows": pending, "rows": len(rows)},
        "m2",
    )
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
    try:
        state_map_colours(browser)
    except Exception as e:
        check(
            "state-map",
            "the state map is the machine, painted from the palette",
            False,
            f"{e}\n{traceback.format_exc()[-400:]}",
        )
    if LEVEL == "m2":
        for light in (False, True):
            try:
                timeline_lane(browser, light=light)
            except Exception as e:
                check(
                    "timeline-lane",
                    f"m2: lane pass completed ({'light' if light else 'dark'})",
                    False,
                    f"{e}\n{traceback.format_exc()[-400:]}",
                    "m2",
                )
        try:
            timeline_lane_touch(browser)
        except Exception as e:
            check(
                "timeline-lane", "m2: lane touch pass completed", False, f"{e}\n{traceback.format_exc()[-400:]}", "m2"
            )
        try:
            sidebar_resizer(browser)
        except Exception as e:
            check("resizer", "m2: resizer pass completed", False, f"{e}\n{traceback.format_exc()[-400:]}", "m2")
        try:
            sidebar_scale(browser)
        except Exception as e:
            check("sidebar-scale", "m2: scale pass completed", False, f"{e}\n{traceback.format_exc()[-400:]}", "m2")
        # LAST, and once: this one accepts a suggestion and declines the other,
        # which creates a space, moves five chats and marks a topic refused.
        # Every read-only check above has already run against the seeded state.
        try:
            space_suggestions_flow(browser)
        except Exception as e:
            check("suggestions", "m2: suggestion pass completed", False, f"{e}\n{traceback.format_exc()[-400:]}", "m2")
        for fn, vp in (
            (message_feedback, "feedback"),
            (message_feedback_touch, "feedback-touch"),
            (trust_tab, "trust"),
            (trust_loop_absent, "absent"),
        ):
            try:
                fn(browser)
            except Exception as e:
                check(vp, "m2: pass completed", False, f"{e}\n{traceback.format_exc()[-400:]}", "m2")
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
