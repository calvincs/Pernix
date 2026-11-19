"""Pernix — Interactive browser tool: one `browser` tool, several actions.

`browse_web` loads a page once and returns its text. That covers reading, and
nothing else: it cannot click, type, log in, wait for something to happen, or
look at a page whose content is drawn on a canvas (web terminals, games, maps,
charts), where there is no text in the page to extract at all. An agent that
met such a page had no way in except to reverse-engineer the page's network
traffic and rebuild the client by hand.

This tool keeps a page OPEN between calls:

- `open` / `goto` / `back` navigate and return a snapshot: the visible text
  plus a numbered list of things that can be clicked or typed into.
- `click`, `type`, `select`, `scroll` act on the page, then return the new
  snapshot. Targets are a number from the snapshot (`[3]`), a CSS selector, or
  `text=Some label`; `click` also takes x/y for canvas pages.
- `screenshot` saves the rendered page as an image and puts it in front of the
  model the same way `view_image` does. This is how a canvas page is read.
- `eval` runs JavaScript in the page and returns the result.
- `wait`, `list`, `close`.

Every snapshot also reports what plain text cannot show: canvases on the page,
WebSocket connections the page opened (with frame counts), console errors and
failed requests. Those four lines are usually the whole diagnosis of "the
page looks empty".

Safety is browse_web's: the same URL validation, and the same per-request
check that keeps the page off private and internal addresses, and the same
egress proxy underneath (egress_proxy.py), which is what actually decides
which address a connection reaches.

All Playwright objects live on the server's event loop (the browser is shared
with browse_web). Tool calls arrive on worker threads and are marshalled onto
that loop; anything that touches the database stays on the worker thread.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections import deque
from pathlib import Path
from urllib.parse import urlparse

from config import settings
from core.tools.interactive import KEY_HELP, browser_key, parse_input, same_family

logger = logging.getLogger("pernix.web.browser")

_TEXT_CAP_SNAPSHOT = 8000
_TEXT_CAP_ACTION = 2000
_ELEMENT_CAP = 80
_EVAL_CAP = 20000
_ACTION_TIMEOUT_MS = 8000
_MAX_WAIT_S = 60.0
_USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

_ACTIONS = (
    "open",
    "goto",
    "back",
    "snapshot",
    "click",
    "type",
    "select",
    "scroll",
    "screenshot",
    "eval",
    "wait",
    "list",
    "close",
)

# Loop-affine: only coroutines running on the browser's event loop touch this.
_pages: dict[str, "_PageSession"] = {}

_SNAPSHOT_JS = """
(limit) => {
  const vis = (el) => {
    const r = el.getBoundingClientRect();
    if (r.width < 2 || r.height < 2) return false;
    const s = getComputedStyle(el);
    return s.visibility !== 'hidden' && s.display !== 'none';
  };
  document.querySelectorAll('[data-pernix-ref]').forEach((e) => e.removeAttribute('data-pernix-ref'));
  const sel = 'a[href],button,input,select,textarea,summary,[role=button],[role=link],[role=tab],' +
    '[role=menuitem],[role=checkbox],[role=radio],[role=textbox],[role=combobox],[role=option],' +
    '[contenteditable=""],[contenteditable=true],[onclick],[tabindex]:not([tabindex="-1"])';
  const elements = [];
  let n = 0, more = 0;
  const clean = (s) => (s || '').replace(/\\s+/g, ' ').trim().slice(0, 80);
  for (const el of document.querySelectorAll(sel)) {
    if (el.type === 'hidden' || !vis(el)) continue;
    if (n >= limit) { more++; continue; }
    n++;
    el.setAttribute('data-pernix-ref', String(n));
    const tag = el.tagName.toLowerCase();
    const item = { ref: n, tag };
    const role = el.getAttribute('role');
    if (role) item.role = role;
    if (el.type && tag !== 'a') item.type = el.type;
    const name = clean(el.getAttribute('aria-label')) || clean(el.innerText) || clean(el.placeholder) ||
      clean(el.title) || clean(el.getAttribute('name')) || clean(el.alt) || clean(el.id);
    if (name) item.name = name;
    if ((tag === 'input' || tag === 'textarea' || tag === 'select') && el.type !== 'password') {
      const v = clean(el.value);
      if (v) item.value = v;
    }
    if (el.checked) item.checked = true;
    if (el.disabled) item.disabled = true;
    if (tag === 'a') item.href = (el.getAttribute('href') || '').slice(0, 100);
    if (el === document.activeElement) item.focused = true;
    elements.push(item);
  }
  const canvases = [...document.querySelectorAll('canvas')].filter(vis).map((c) => {
    const r = c.getBoundingClientRect();
    return { w: Math.round(r.width), h: Math.round(r.height), x: Math.round(r.left), y: Math.round(r.top) };
  });
  const text = (document.body ? document.body.innerText : '').replace(/\\n{3,}/g, '\\n\\n').trim();
  const doc = document.documentElement;
  return {
    title: document.title || '',
    text,
    elements,
    more,
    canvases,
    iframes: document.querySelectorAll('iframe').length,
    scrollY: Math.round(window.scrollY),
    scrollMax: Math.max(0, Math.round(doc.scrollHeight - window.innerHeight)),
    viewport: { w: window.innerWidth, h: window.innerHeight },
  };
}
"""


class _PageSession:
    def __init__(self, page_id: str, owner: str, context, page, out_dir: Path):
        self.id = page_id
        self.owner = owner
        self.context = context
        self.page = page
        self.out_dir = out_dir
        self.opened_at = time.monotonic()
        self.last_used = time.monotonic()
        self.console: deque[str] = deque(maxlen=30)
        self.failed: deque[str] = deque(maxlen=20)
        self.sockets: list[dict] = []
        self.shots = 0
        self.host_cache: dict[str, bool] = {}
        self.blocked_requests = 0
        self.popups = 0

    def watch(self, page) -> None:
        """Attach the passive listeners that feed the snapshot's notes."""

        def _on_console(msg):
            try:
                kind = getattr(msg, "type", "") or ""
                if kind in ("error", "warning"):
                    self.console.append(f"[{kind}] {(getattr(msg, 'text', '') or '')[:300]}")
            except Exception:
                pass

        def _on_pageerror(exc):
            self.console.append(f"[pageerror] {str(exc)[:300]}")

        def _on_failed(request):
            try:
                self.failed.append(f"{request.method} {request.url[:160]} ({request.failure or 'failed'})")
            except Exception:
                pass

        def _on_ws(ws):
            rec = {"url": ws.url[:200], "in": 0, "out": 0, "closed": False}
            if len(self.sockets) < 20:
                self.sockets.append(rec)

            def _in(_payload):
                rec["in"] += 1

            def _out(_payload):
                rec["out"] += 1

            def _closed(_ws):
                rec["closed"] = True

            ws.on("framereceived", _in)
            ws.on("framesent", _out)
            ws.on("close", _closed)

        for event, handler in (
            ("console", _on_console),
            ("pageerror", _on_pageerror),
            ("requestfailed", _on_failed),
            ("websocket", _on_ws),
        ):
            try:
                page.on(event, handler)
            except Exception:
                pass


def _host_blocked(sess: _PageSession | None, url: str, allow_loopback: bool) -> bool:
    """The early check for one request, WebSocket or landed page.

    The harness's own port is decided per URL, never cached: its /workspace/
    files are reachable and nothing else on it is. Other hosts keep a
    per-page verdict cache, which only spares repeated DNS lookups for this
    early check. A name that answers differently later (DNS rebinding) gains
    nothing from a stale "allowed" here, because every connection goes
    through the egress proxy, which resolves and checks the address it
    actually connects to.
    """
    from core.extensions.web import _request_refused

    return _request_refused(url, allow_loopback, cache=sess.host_cache if sess is not None else None)


async def _settle(page, extra_s: float = 0.3) -> None:
    try:
        await page.wait_for_load_state("domcontentloaded", timeout=5000)
    except Exception:
        pass
    await asyncio.sleep(extra_s)


async def _snapshot(sess: _PageSession, text_cap: int, note: str = "") -> str:
    page = sess.page
    try:
        data = await page.evaluate(_SNAPSHOT_JS, _ELEMENT_CAP)
    except Exception as e:
        return f"[browser {sess.id} | {page.url}]\nError: could not read the page ({str(e)[:200]}). It may still be loading — try action='wait' then 'snapshot'."
    title = data.get("title") or "(no title)"
    lines = [f"[browser {sess.id} | {title[:100]} | {page.url[:300]}]"]
    if note:
        lines.append(f"[{note}]")
    text = data.get("text") or ""
    if text:
        if len(text) > text_cap:
            lines.append(text[:text_cap])
            lines.append(
                f"[page text cut at {text_cap:,} of {len(text):,} chars — action='snapshot' shows up to "
                f"{_TEXT_CAP_SNAPSHOT:,}; browse_web returns a whole article; action='eval' can pull any part]"
            )
        else:
            lines.append(text)
    else:
        lines.append("(the page has no readable text)")

    elements = data.get("elements") or []
    if elements:
        lines.append("")
        lines.append('Elements — use target="[n]" with click / type / select:')
        for el in elements:
            kind = el.get("role") or el["tag"]
            if el.get("type") and el["tag"] == "input":
                kind = f"input:{el['type']}"
            row = f"[{el['ref']}] {kind}"
            if el.get("name"):
                row += f' "{el["name"]}"'
            if el.get("value"):
                row += f' value="{el["value"]}"'
            if el.get("href"):
                row += f" -> {el['href']}"
            flags = [f for f in ("checked", "disabled", "focused") if el.get(f)]
            if flags:
                row += " (" + ", ".join(flags) + ")"
            lines.append(row)
        if data.get("more"):
            lines.append(f"... {data['more']} more element(s) not listed; scroll, or use a CSS selector as target")

    notes = []
    for c in data.get("canvases") or []:
        notes.append(
            f"canvas {c['w']}x{c['h']} at x={c['x']} y={c['y']}: what it draws is NOT in the text above. "
            "Use action='screenshot' to see it, action='type' to send keys, action='click' with x/y to click it."
        )
    if data.get("iframes"):
        notes.append(f"{data['iframes']} iframe(s): their content is not listed here")
    if data.get("scrollMax"):
        notes.append(f"scrolled {data.get('scrollY', 0)} of {data['scrollMax']} px (action='scroll' moves the view)")
    for s in sess.sockets:
        state = "closed" if s["closed"] else "open"
        notes.append(f"websocket {s['url']} ({state}; {s['in']} frames in, {s['out']} out)")
    if sess.popups:
        notes.append(f"the page opened {sess.popups} new tab(s); this session follows the newest one")
    if sess.blocked_requests:
        notes.append(f"{sess.blocked_requests} request(s) to private/internal addresses were blocked")
    notes.extend(f"console {m}" for m in list(sess.console)[-8:])
    notes.extend(f"failed request: {m}" for m in list(sess.failed)[-5:])
    if notes:
        lines.append("")
        lines.append("Notes:")
        lines.extend(f"- {n}" for n in notes)
    return "\n".join(lines)


def _locator(page, target: str):
    target = (target or "").strip()
    ref = target.strip("[]").strip()
    if ref.isdigit():
        return page.locator(f'[data-pernix-ref="{ref}"]').first
    return page.locator(target).first


async def _close_one(page_id: str) -> None:
    sess = _pages.pop(page_id, None)
    if sess is None:
        return
    try:
        await sess.context.close()
    except Exception as e:
        logger.debug("browser %s close: %s", page_id, e)


async def _sweep_idle() -> None:
    cap = float(settings.browser_session_idle_s or 0)
    if cap <= 0:
        return
    now = time.monotonic()
    for pid in [p for p, s in _pages.items() if now - s.last_used > cap]:
        logger.info("browser %s closed after %ds idle", pid, int(cap))
        await _close_one(pid)


async def _owners() -> dict[str, str]:
    await _sweep_idle()
    return {pid: s.owner for pid, s in _pages.items()}


async def _urls() -> dict[str, str]:
    return {pid: s.page.url for pid, s in _pages.items()}


async def _reuse(page_id: str) -> dict:
    sess = _pages.get(page_id)
    if sess is None:
        return {"text": f"Error: page {page_id} is no longer open."}
    sess.last_used = time.monotonic()
    note = (
        f"this address is ALREADY OPEN as page {page_id} — this is it, no new page was opened. "
        "Keep using this id; action='goto' reloads it, action='close' ends it"
    )
    return {"text": await _snapshot(sess, _TEXT_CAP_SNAPSHOT, note)}


async def _listing(ids: list[str]) -> str:
    rows = []
    for pid in ids:
        s = _pages.get(pid)
        if s is None:
            continue
        try:
            title = await s.page.title()
        except Exception:
            title = ""
        rows.append(f"- {pid}: {title[:60] or '(no title)'} — {s.page.url[:200]}")
    return ("Open pages:\n" + "\n".join(rows)) if rows else "No pages are open."


async def _open(
    owner: str, url: str, width: int, height: int, out_root: Path, allow_loopback: bool, proxy_url: str
) -> dict:
    from core.extensions.web import _browser_proxy, _get_browser_async

    browser = await _get_browser_async()
    # All traffic goes through the egress proxy (see _browser_proxy); service
    # workers are blocked because their fetches can slip past context routing.
    context = await browser.new_context(
        user_agent=_USER_AGENT,
        viewport={"width": width, "height": height},
        proxy=_browser_proxy(proxy_url),
        service_workers="block",
    )
    page_id = "b-" + uuid.uuid4().hex[:6]
    sess = None
    try:
        page = await context.new_page()
        sess = _PageSession(page_id, owner, context, page, out_root / ".browser" / page_id)
        sess.watch(page)

        async def _route(route, request):
            if _host_blocked(sess, request.url, allow_loopback):
                sess.blocked_requests += 1
                await route.abort("blockedbyclient")
            else:
                await route.continue_()

        await context.route("**/*", _route)

        # The request route above does not see WebSocket handshakes, so a page
        # could still dial an internal address that way. Close those; leave
        # every other socket untouched (not proxied).
        try:

            async def _ws_block(ws):
                sess.blocked_requests += 1
                await ws.close()

            await context.route_web_socket(lambda u: _host_blocked(sess, u, allow_loopback), _ws_block)
        except Exception as e:  # older Playwright without WebSocket routing
            logger.debug("websocket routing unavailable: %s", e)

        def _on_new_page(new_page):
            sess.popups += 1
            sess.page = new_page
            sess.watch(new_page)

        context.on("page", lambda p: _on_new_page(p) if p is not page else None)
        _pages[page_id] = sess
        return await _goto(sess, url, allow_loopback, opened=True)
    except Exception as e:
        _pages.pop(page_id, None)
        try:
            await context.close()
        except Exception:
            pass
        return {"text": f"Error opening {url}: {str(e)[:300]}"}


async def _goto(sess: _PageSession, url: str, allow_loopback: bool, opened: bool = False) -> dict:
    timeout_ms = int(settings.browser_timeout) * 1000
    note = f"opened; use id='{sess.id}' with the other actions" if opened else ""
    try:
        await sess.page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
    except Exception as e:
        err = str(e)
        if "timeout" not in err.lower():
            if opened:
                await _close_one(sess.id)
            return {"text": f"Error navigating to {url}: {err[:300]}"}
        note = (note + "; " if note else "") + f"page did not finish loading within {settings.browser_timeout}s"
    try:
        await sess.page.wait_for_load_state("networkidle", timeout=min(timeout_ms, 5000))
    except Exception:
        pass
    if _host_blocked(sess, sess.page.url, allow_loopback):
        from core.extensions.web import _own_server_refusal, _own_server_target

        landed = sess.page.url
        await _close_one(sess.id)
        try:
            own = _own_server_target(landed) == "refused"
        except ValueError:
            own = False
        if own:
            return {"text": f"Error: {_own_server_refusal(landed)} The page was closed."}
        return {
            "text": f"Error: redirect to blocked internal address: {urlparse(landed).hostname}. The page was closed."
        }
    return {"text": await _snapshot(sess, _TEXT_CAP_SNAPSHOT, note)}


async def _act(page_id: str, action: str, a: dict) -> dict:
    sess = _pages.get(page_id)
    if sess is None:
        return {"text": f"Error: page {page_id} is no longer open."}
    sess.last_used = time.monotonic()
    page = sess.page
    allow_loopback = bool(a.get("allow_loopback"))
    target = (a.get("target") or "").strip()

    try:
        if action == "goto":
            return await _goto(sess, a["url"], allow_loopback)

        if action == "back":
            await page.go_back(wait_until="domcontentloaded", timeout=int(settings.browser_timeout) * 1000)
            await _settle(page)
            return {"text": await _snapshot(sess, _TEXT_CAP_SNAPSHOT)}

        if action == "snapshot":
            return {"text": await _snapshot(sess, _TEXT_CAP_SNAPSHOT)}

        if action == "click":
            if target:
                await _locator(page, target).click(timeout=_ACTION_TIMEOUT_MS)
            elif a.get("x") is not None and a.get("y") is not None:
                await page.mouse.click(float(a["x"]), float(a["y"]))
            else:
                return {"text": "Error: click needs a target ('[n]', a CSS selector, 'text=Label') or x and y."}
            await _settle(page)
            return {"text": await _snapshot(sess, _TEXT_CAP_ACTION)}

        if action == "type":
            steps = parse_input(a.get("input") or "", raw=bool(a.get("raw")))
            if not steps:
                return {"text": "Error: type needs input, e.g. input='hello<enter>'. " + KEY_HELP}
            if target:
                loc = _locator(page, target)
                if a.get("clear"):
                    await loc.fill("", timeout=_ACTION_TIMEOUT_MS)
                await loc.click(timeout=_ACTION_TIMEOUT_MS)
            for kind, value in steps:
                if kind == "wait":
                    await asyncio.sleep(float(value))
                elif kind == "text":
                    await page.keyboard.type(str(value))
                else:
                    await page.keyboard.press(browser_key(str(value)))
            await _settle(page)
            return {"text": await _snapshot(sess, _TEXT_CAP_ACTION)}

        if action == "select":
            if not target:
                return {
                    "text": "Error: select needs a target (the <select> element) and input (the option's value or label)."
                }
            choice = a.get("input") or ""
            loc = _locator(page, target)
            try:
                await loc.select_option(value=choice, timeout=_ACTION_TIMEOUT_MS)
            except Exception:
                await loc.select_option(label=choice, timeout=_ACTION_TIMEOUT_MS)
            await _settle(page)
            return {"text": await _snapshot(sess, _TEXT_CAP_ACTION)}

        if action == "scroll":
            if target:
                await _locator(page, target).scroll_into_view_if_needed(timeout=_ACTION_TIMEOUT_MS)
            else:
                dy = a.get("y")
                if dy is None:
                    dy = int((page.viewport_size or {"height": 800})["height"] * 0.8)
                await page.mouse.wheel(0, float(dy))
            await asyncio.sleep(0.3)
            return {"text": await _snapshot(sess, _TEXT_CAP_ACTION)}

        if action == "wait":
            secs = float(a.get("wait_seconds") or 0)
            wait_for = a.get("wait_for") or ""
            note = ""
            if wait_for:
                budget = secs or 10.0
                try:
                    await page.wait_for_function(
                        "(t) => !!document.body && document.body.innerText.includes(t)",
                        arg=wait_for,
                        timeout=budget * 1000,
                    )
                    note = "wait_for text is on the page"
                except Exception:
                    note = f"wait_for text not seen after {budget:g}s"
            else:
                await asyncio.sleep(secs or 1.0)
            return {"text": await _snapshot(sess, _TEXT_CAP_ACTION, note)}

        if action == "screenshot":
            sess.out_dir.mkdir(parents=True, exist_ok=True)
            sess.shots += 1
            path = sess.out_dir / f"shot-{sess.shots:03d}.jpg"
            if target:
                await _locator(page, target).screenshot(
                    path=str(path), type="jpeg", quality=85, timeout=_ACTION_TIMEOUT_MS
                )
            else:
                await page.screenshot(path=str(path), type="jpeg", quality=85, full_page=bool(a.get("full_page")))
            size = page.viewport_size or {}
            return {
                "text": f"[browser {sess.id} | {page.url[:300]}]\nScreenshot saved: {path} "
                f"(viewport {size.get('width')}x{size.get('height')}; x/y for click are in these pixels).",
                "screenshot": str(path),
            }

        if action == "eval":
            script = a.get("script") or ""
            if not script.strip():
                return {
                    "text": "Error: eval needs script — a JavaScript expression or a function, e.g. 'document.title' or '() => [...document.links].map(a => a.href)'."
                }
            result = await page.evaluate(script)
            try:
                out = json.dumps(result, ensure_ascii=False, indent=1, default=str)
            except (TypeError, ValueError):
                out = str(result)
            if len(out) > _EVAL_CAP:
                out = (
                    out[:_EVAL_CAP]
                    + f"\n[result cut at {_EVAL_CAP:,} of {len(out):,} chars — return less from the script]"
                )
            return {"text": f"[browser {sess.id} | eval result]\n{out}"}

        if action == "close":
            url = page.url
            await _close_one(page_id)
            return {"text": f"Page {page_id} closed ({url[:200]})."}
    except Exception as e:
        err = str(e).split("\n")[0][:400]
        hint = ""
        if target and ("Timeout" in err or "timeout" in err):
            hint = " The target was not found or not clickable — take a fresh snapshot; element numbers change when the page changes."
        return {"text": f"Error: {action} failed: {err}.{hint}"}
    return {"text": f"Error: unknown action '{action}'."}


async def _close_owned(owner: str) -> int:
    ids = [pid for pid, s in _pages.items() if s.owner == owner]
    for pid in ids:
        await _close_one(pid)
    return len(ids)


def forget_all() -> None:
    """The shared browser is closing; its contexts die with it."""
    _pages.clear()


def close_for_session(session_id: str) -> None:
    """End a deleted session's pages. Callable from any thread; best effort."""
    from core.extensions import web as _web

    loop = _web._browser_loop
    if loop is None or not _pages:
        return
    try:
        asyncio.run_coroutine_threadsafe(_close_owned(session_id), loop)
    except RuntimeError:
        pass


def _call(loop, coro, timeout: float):
    import concurrent.futures as _futures

    try:
        fut = asyncio.run_coroutine_threadsafe(coro, loop)
    except RuntimeError as e:
        coro.close()
        raise RuntimeError(f"browser unavailable (loop not running): {e}")
    try:
        return fut.result(timeout=timeout)
    except _futures.TimeoutError:
        fut.cancel()
        raise TimeoutError(f"the browser did not answer within {timeout:g}s")


def browser(
    action: str = "",
    id: str = "",  # noqa: A002 - the name the model sees
    url: str = "",
    target: str = "",
    input: str = "",  # noqa: A002
    raw: bool = False,
    clear: bool = False,
    x: float | None = None,
    y: float | None = None,
    script: str = "",
    wait_for: str = "",
    wait_seconds: float | None = None,
    full_page: bool = False,
    width: int | None = None,
    height: int | None = None,
    _context: dict | None = None,
) -> str:
    """Drive a page that stays open between calls. See register() for the contract."""
    from core.extensions.web import _loopback_allowed, _validate_url, egress_proxy

    if not settings.browser_enabled:
        return (
            "Error: Browser is disabled. Enable it in Settings → Web / Browser → "
            "Enable Browser (Playwright), then try again. No restart required."
        )
    ctx = dict(_context or {})
    loop = ctx.get("_loop")
    if loop is None:
        return "Error: browser requires the event loop context. Internal error."
    session_id = ctx.get("session_id", "") or "unknown"
    action = (action or "").strip().lower()
    if not action:
        action = "open" if url and not id else "snapshot"
    if action not in _ACTIONS:
        return f"Error: unknown action '{action}'. Use one of: {', '.join(_ACTIONS)}."

    allow_loopback = _loopback_allowed()
    if action in ("open", "goto"):
        try:
            url = _validate_url(url, allow_loopback=allow_loopback)
        except ValueError as e:
            return f"Error: {e}"

    try:
        owners = _call(loop, _owners(), 15)
        visible = [pid for pid, owner in owners.items() if same_family(owner, session_id)]

        if action == "list":
            return _call(loop, _listing(visible), 15)

        if action == "open":
            # Same reasoning as the terminal: a repeated `open` of a page that
            # is already open hands that page back instead of stacking copies.
            urls = _call(loop, _urls(), 15)
            for pid in visible:
                if urls.get(pid, "").rstrip("/") == url.rstrip("/"):
                    return _call(loop, _reuse(pid), 20)["text"]
            cap = int(settings.browser_max_sessions)
            mine = [pid for pid, owner in owners.items() if owner == session_id]
            if len(mine) >= cap:
                listing = _call(loop, _listing(mine), 15)
                return f"Error: {cap} page(s) already open for this session — close one first, or reuse it with action='goto'.\n{listing}"
            from core.tools.paths import roots_for_context

            ws_root, _cwd = roots_for_context(ctx.get("workspace_override"), ctx.get("workspace_home"))
            w = max(320, min(int(width or 1280), 2560))
            h = max(240, min(int(height or 800), 1600))
            # Started on this worker thread so a first start never blocks the
            # server's loop. No proxy, no page: there is no direct fallback.
            try:
                proxy_url = egress_proxy.ensure_started()
            except egress_proxy.EgressProxyError as e:
                return f"Error: {e}; the browser tool is unavailable until it runs"
            result = _call(
                loop,
                _open(session_id, url, w, h, ws_root, allow_loopback, proxy_url),
                float(settings.browser_timeout) + 20,
            )
            return result["text"]

        page_id = (id or "").strip()
        if not page_id:
            if len(visible) == 1:
                page_id = visible[0]
            elif not visible:
                return "Error: no page is open. Start one with action='open' and a url."
            else:
                return "Error: several pages are open — pass id.\n" + _call(loop, _listing(visible), 15)
        elif page_id not in visible:
            if page_id in owners:
                return f"Error: page {page_id} belongs to another session."
            return f"Error: no page with id '{page_id}'.\n" + _call(loop, _listing(visible), 15)

        try:
            secs = max(0.0, min(float(wait_seconds or 0), _MAX_WAIT_S))
        except (TypeError, ValueError):
            secs = 0.0
        args = {
            "url": url,
            "target": target,
            "input": input,
            "raw": raw,
            "clear": clear,
            "x": x,
            "y": y,
            "script": script,
            "wait_for": wait_for,
            "wait_seconds": secs,
            "full_page": full_page,
            "allow_loopback": allow_loopback,
        }
        result = _call(loop, _act(page_id, action, args), float(settings.browser_timeout) + secs + 20)
    except (RuntimeError, TimeoutError) as e:
        return f"Error: {e}"

    text = result["text"]
    shot = result.get("screenshot")
    if shot:
        # Off the loop on purpose: queuing the image writes a message row.
        from core.tools.builtin.view_image import view_image

        text += "\n" + view_image(shot, _context=_context)
    return text


def register_browser(reg) -> None:
    reg.register(
        name="browser",
        func=browser,
        description=(
            "Drive a real web page that STAYS OPEN between calls: click, type, log in, fill forms, "
            "wait, take screenshots, run JavaScript. Use browse_web to just read a page; use this "
            "when you must interact, or when the page draws its content on a canvas (web terminals, "
            "games, maps, charts) and browse_web returns nothing useful. "
            "Actions: open (url) -> id + snapshot; goto (url); back; snapshot; click (target or x,y); "
            "type (input, optional target); select (target, input); scroll; screenshot (you then SEE "
            "the page as an image next round); eval (script); wait (wait_for text or wait_seconds); "
            "list; close. A snapshot is the page text plus a numbered list of clickable/typable "
            "elements — use target='[n]', a CSS selector, or 'text=Label'. Snapshot notes also "
            "report canvases, WebSocket connections, console errors and failed requests. "
            "Do NOT install or script your own browser, and do not reverse-engineer a page's "
            "network protocol before trying this. " + KEY_HELP
        ),
        parameters={
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": list(_ACTIONS), "description": "What to do."},
                "id": {
                    "type": "string",
                    "description": "Page id from open. May be omitted when only one page is open.",
                },
                "url": {"type": "string", "description": "open/goto: address to load (http/https only)."},
                "target": {
                    "type": "string",
                    "description": (
                        "click/type/select/scroll/screenshot: element to act on — '[n]' from the latest "
                        "snapshot, a CSS selector, or 'text=Visible label'."
                    ),
                },
                "input": {
                    "type": "string",
                    "description": (
                        "type: text and keys, e.g. 'hello<enter>' or '<f6>' or '<ctrl-a>'. With no target the "
                        "keys go to whatever has focus (right for canvas pages). select: the option's value or label."
                    ),
                },
                "raw": {"type": "boolean", "description": "type: send input exactly, with no <key> parsing."},
                "clear": {"type": "boolean", "description": "type: empty the target field before typing."},
                "x": {"type": "number", "description": "click: x pixel in the viewport (for canvas pages)."},
                "y": {
                    "type": "number",
                    "description": "click: y pixel in the viewport. scroll: pixels to scroll down (negative = up).",
                },
                "script": {
                    "type": "string",
                    "description": "eval: JavaScript expression or function; its JSON-serializable result is returned.",
                },
                "wait_for": {"type": "string", "description": "wait: text that must appear on the page."},
                "wait_seconds": {
                    "type": "number",
                    "description": f"wait: how long to wait (default 10 with wait_for, else 1; max {_MAX_WAIT_S:g}).",
                },
                "full_page": {
                    "type": "boolean",
                    "description": "screenshot: capture the whole scrollable page, not just the viewport.",
                },
                "width": {"type": "integer", "description": "open: viewport width in pixels (default 1280)."},
                "height": {"type": "integer", "description": "open: viewport height in pixels (default 800)."},
            },
            "required": ["action"],
        },
        category="web",
        tags=[
            "browser",
            "browse",
            "interactive",
            "click",
            "type",
            "form",
            "login",
            "screenshot",
            "canvas",
            "javascript",
            "eval",
            "playwright",
            "page",
            "web",
            "websocket",
            "automation",
            "navigate",
            "session",
        ],
        timeout=int(settings.browser_timeout) + int(_MAX_WAIT_S) + 30,
        parallel_safe=False,
        source="extension",
        safety_level="dangerous",
        idempotent=False,  # the page changes; a repeat call must never be served from cache
    )
    logger.info("browser tool registered (interactive pages)")
