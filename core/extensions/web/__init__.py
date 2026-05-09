"""Pernix — Web extension: search_web, http_get, browse_web.

Tavily-only web search. browse_web uses Playwright + trafilatura for
JS-rendered page extraction.
"""

from __future__ import annotations

import atexit
import logging
import os
import signal as _signal
import threading
import time
from html.parser import HTMLParser
from urllib.parse import urlparse

from config import settings

logger = logging.getLogger("pernix.ext.web")

# Backend health tracking — fire a single notification on first Tavily fault
# rather than spamming the operator on every call.
_tavily_alert_lock = threading.Lock()
_tavily_alerted: bool = False


def _emit_backend_alert(title: str, body: str, urgency: str = "normal") -> None:
    """Push a one-shot operator notification when a search backend degrades.

    Best-effort: failures here are logged at debug because the alert is
    advisory — the wrapper still returns useful errors to the agent.
    """
    try:
        from db import models as _db

        _db.add_notification(title=title, body=body, urgency=urgency)
    except Exception as e:
        logger.debug("backend alert (%s) could not be persisted: %s", title, e)


# ---------------------------------------------------------------------------
# Browser lifecycle — async Playwright on the FastAPI loop.
#
# Playwright 1.58+ rejects sync_api use in any process where an asyncio loop
# is running. We run Playwright natively on the FastAPI loop using async_api,
# and bridge from the executor's worker thread (where sync tool functions
# execute) via asyncio.run_coroutine_threadsafe — the same pattern used by
# the evaluation, model_mgmt, scheduling, and orchestration extensions.
# ---------------------------------------------------------------------------

_browser = None  # Playwright async Browser instance (lives on _browser_loop)
_playwright = None  # AsyncPlaywright context manager
_browser_headless = None  # tracks mode for recycling on setting change
_driver_pid = None  # Playwright node driver subprocess PID
_browser_loop = None  # asyncio.AbstractEventLoop the browser is bound to
_browser_init_lock = None  # asyncio.Lock — created lazily on the loop


def _ensure_init_lock():
    """Lazy-create the asyncio init lock on the loop. Must be called on the loop."""
    global _browser_init_lock
    if _browser_init_lock is None:
        import asyncio as _asyncio

        _browser_init_lock = _asyncio.Lock()
    return _browser_init_lock


async def _get_browser_async():
    """Get or lazily create the browser. Must run on the FastAPI loop."""
    global _browser, _playwright, _browser_headless, _driver_pid, _browser_loop
    import asyncio as _asyncio

    desired_headless = settings.browser_headless

    async with _ensure_init_lock():
        # Recycle if headless mode changed
        if _browser is not None and _browser_headless != desired_headless:
            logger.info(
                "Browser mode changed (headless=%s -> %s), recycling",
                _browser_headless,
                desired_headless,
            )
            old_browser, old_pw = _browser, _playwright
            _browser = None
            _playwright = None
            _browser_headless = None
            _driver_pid = None
            try:
                await old_browser.close()
                await old_pw.stop()
            except Exception as e:
                logger.warning("Error closing old browser during recycle: %s", e)

        if _browser is not None and _browser.is_connected():
            return _browser

        # Stale handle: clear so launch path runs cleanly
        if _browser is not None:
            _browser = None
            _playwright = None
            _driver_pid = None

        try:
            from playwright.async_api import async_playwright
        except ImportError:
            raise RuntimeError(
                "Playwright is not installed. Install with: " "pip install playwright && playwright install chromium"
            )

        try:
            _playwright = await async_playwright().start()
            # Track driver subprocess PID for forceful cleanup on shutdown.
            # async_api wraps the impl in _impl_obj; sync_api exposes it directly.
            try:
                _driver_pid = _playwright._impl_obj._connection._transport._proc.pid
            except Exception:
                _driver_pid = None
            _browser = await _playwright.chromium.launch(
                headless=desired_headless,
                args=["--disable-blink-features=AutomationControlled"],
            )
            _browser_headless = desired_headless
            _browser_loop = _asyncio.get_running_loop()
            logger.info(
                "Playwright browser launched (headless=%s, driver_pid=%s)",
                desired_headless,
                _driver_pid,
            )
            return _browser
        except Exception as e:
            _playwright = None
            _browser = None
            _driver_pid = None
            _browser_loop = None
            err = str(e).lower()
            if "executable doesn't exist" in err or "browser" in err:
                raise RuntimeError(f"Chromium not installed. Run: playwright install chromium\n" f"Original error: {e}")
            raise


def _kill_driver():
    """Forcefully kill and reap the Playwright node driver subprocess.

    Sync — safe to call from atexit. Safe to call multiple times.
    """
    global _driver_pid
    pid = _driver_pid
    if not pid:
        return
    _driver_pid = None
    # SIGTERM first
    try:
        os.kill(pid, _signal.SIGTERM)
    except (ProcessLookupError, OSError):
        return  # already gone
    # Brief wait, then SIGKILL
    time.sleep(0.3)
    try:
        os.kill(pid, _signal.SIGKILL)
    except (ProcessLookupError, OSError):
        pass  # already gone
    # Reap zombie
    try:
        os.waitpid(pid, os.WNOHANG)
    except (ChildProcessError, OSError):
        pass


async def _close_browser():
    """Shut down browser + Playwright. Must run on the loop the browser was bound to."""
    global _browser, _playwright, _browser_headless, _browser_loop
    if _browser is not None:
        try:
            await _browser.close()
        except Exception as e:
            logger.debug("Error closing browser: %s", e)
        _browser = None
    if _playwright is not None:
        try:
            await _playwright.stop()
        except Exception as e:
            logger.warning("Error stopping Playwright: %s", e)
        _playwright = None
    _browser_headless = None
    _browser_loop = None
    _kill_driver()
    logger.info("Playwright browser closed")


# Safety net: ensure driver subprocess is killed even if lifespan shutdown
# doesn't complete (e.g. force-quit with second Ctrl+C).
atexit.register(_kill_driver)


def search_web(query: str, num_results: int = 5, _context: dict | None = None) -> str:
    """Search the web using Tavily. Requires TAVILY_API_KEY."""
    if not settings.web_search_enabled:
        return "Error: Web search is disabled. Enable it in Settings → Web → Web Search, then try again."
    if not query.strip():
        return "Error: Empty search query"

    try:
        num_results = int(num_results)
    except (ValueError, TypeError):
        num_results = 5
    num_results = min(max(num_results, 1), 10)

    tavily_key = os.environ.get("TAVILY_API_KEY", "")
    if not tavily_key:
        return (
            "Error: Web search requires a Tavily API key. "
            "Add yours in Settings → Web → Tavily API Key "
            "(free tier available at tavily.com)."
        )

    try:
        return _tavily_search(query, num_results, tavily_key)
    except _TavilyKeyError:
        logger.warning("Tavily API key invalid")
        _alert_tavily_once(
            "Tavily API key rejected",
            "TAVILY_API_KEY is invalid. Update it in Settings → Web → Tavily API Key.",
            "high",
        )
        return "Error: Tavily API key is invalid. Update it in Settings → Web → Tavily API Key."
    except _TavilyLimitError:
        logger.warning("Tavily usage limit exceeded")
        _alert_tavily_once(
            "Tavily plan limit reached",
            "TAVILY_API_KEY is over its usage limit. Upgrade your plan or wait for the monthly reset.",
            "normal",
        )
        return "Error: Tavily usage limit reached. Upgrade your plan or wait for the monthly reset."
    except Exception as e:
        logger.warning("Tavily search failed: %s", e)
        return f"Error: Web search failed: {e}"


def _alert_tavily_once(title: str, body: str, urgency: str) -> None:
    """One-shot alert; resets when key is rotated (env vars are idempotent)."""
    global _tavily_alerted
    with _tavily_alert_lock:
        if _tavily_alerted:
            return
        _tavily_alerted = True
    _emit_backend_alert(title, body, urgency)


def _tavily_search(query: str, num_results: int, api_key: str) -> str:
    """Search via Tavily API with proper error handling."""
    try:
        from tavily import TavilyClient
    except ImportError:
        raise RuntimeError("tavily-python not installed")

    try:
        client = TavilyClient(api_key=api_key)
        response = client.search(
            query=query,
            search_depth="basic",
            max_results=num_results,
            include_answer=True,
            timeout=20,
        )
    except Exception as e:
        err_str = str(e).lower()
        if "invalid" in err_str and "key" in err_str:
            raise _TavilyKeyError(str(e))
        if "limit" in err_str or "exceeded" in err_str:
            raise _TavilyLimitError(str(e))
        raise

    results = response.get("results", [])
    lines = []

    # Include AI-generated summary if available
    answer = response.get("answer")
    if answer:
        lines.append(f"**Summary:** {answer}")

    if not results and not answer:
        return f"No results found for: {query}"

    for r in results:
        title = r.get("title", "")
        url = r.get("url", "")
        snippet = r.get("content", "")[:300]
        lines.append(f"**{title}**\n{url}\n{snippet}")

    return "\n\n---\n\n".join(lines)


class _TavilyKeyError(Exception):
    pass


class _TavilyLimitError(Exception):
    pass


def http_get(url: str, _context: dict | None = None) -> str:
    """Fetch content from a URL. Returns plain text, max 100KB."""
    try:
        url = _validate_url(url, allow_loopback=_loopback_allowed())
    except ValueError as e:
        return f"Error: {e}"
    import httpx

    try:
        with httpx.Client(timeout=15.0, follow_redirects=True) as client:
            resp = client.get(url)
            resp.raise_for_status()
            content = resp.text
            if len(content) > settings.max_fetch_size:
                content = content[: settings.max_fetch_size] + f"\n[truncated at {settings.max_fetch_size} bytes]"
            return content
    except Exception as e:
        return f"Error fetching {url}: {e}"


# ---------------------------------------------------------------------------
# browse_web — Playwright-based page rendering + trafilatura extraction
# ---------------------------------------------------------------------------


def _emit_browse_event(ctx: dict | None, event: dict) -> None:
    """Emit a browse event from the tool thread. Safe: emit_event uses threading.Lock."""
    if not ctx:
        return
    sid = ctx.get("session_id")
    if not sid:
        return
    try:
        from sessions.manager import get_manager

        session = get_manager().get(sid)
        if session:
            session.emit_event(event)
    except Exception:
        pass  # non-critical — don't break browsing if event emission fails


def _is_blocked_host(hostname: str, allow_loopback: bool = False) -> bool:
    """Check if a hostname resolves to a private/internal IP (SSRF protection).

    When allow_loopback=True, loopback addresses (127.0.0.0/8, ::1, "localhost")
    are permitted — appropriate for localhost-mode where the agent needs to
    browse its own workspace server. RFC1918 private ranges, link-local,
    reserved blocks, and cloud-metadata IPs stay blocked regardless.
    """
    import ipaddress
    import socket

    blocked_hosts = {"metadata.google.internal", "metadata.goog"}
    loopback_hosts = {"localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback"}

    low = hostname.lower()
    if low in blocked_hosts:
        return True
    if low in loopback_hosts:
        return not allow_loopback

    try:
        # Resolve DNS to check actual IP
        for info in socket.getaddrinfo(hostname, None, socket.AF_UNSPEC, socket.SOCK_STREAM):
            addr = info[4][0]
            ip = ipaddress.ip_address(addr)
            if ip.is_loopback:
                if allow_loopback:
                    continue
                return True
            if ip.is_private or ip.is_link_local or ip.is_reserved:
                return True
            # AWS/cloud metadata endpoint (link-local but belt-and-braces)
            if str(ip) == "169.254.169.254":
                return True
    except (socket.gaierror, ValueError):
        # Fail-secure: block on DNS failure. Playwright resolves DNS independently
        # and may succeed where Python's resolver fails (split-horizon, transient).
        logger.warning("SSRF check: DNS resolution failed for %s — blocking", hostname)
        return True

    return False


def _loopback_allowed() -> bool:
    """Agent-originated fetches may hit loopback only in localhost (single-user) mode.

    In network mode the server binds 0.0.0.0 with shared auth, so loopback
    could reach co-tenant services — keep the block on. In localhost mode the
    agent is already OS-sandboxed and needs to browse its own /workspace/ view.
    """
    return not getattr(settings, "network_enabled", False)


def _is_self_loopback(hostname: str, port: int | None) -> bool:
    """True iff URL points at the harness's own listen address.

    Carve-out for SSRF in network mode: the agent owns this server, so
    reaching its own port over loopback is not privilege escalation. Other
    loopback ports may be co-tenant services and stay blocked. Without this,
    the agent has no way to test workspace files it just wrote (e.g. open
    https://localhost:<port>/workspace/index.html in browse_web).
    """
    own_port = getattr(settings, "port", None)
    if own_port is None or port != own_port:
        return False
    low = hostname.lower()
    if low in {"localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback"}:
        return True
    try:
        import ipaddress

        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


def _validate_url(url: str, *, allow_loopback: bool = False) -> str:
    """Validate and normalize URL. Raises ValueError for invalid/dangerous URLs."""
    url = url.strip()
    if not url:
        raise ValueError("Empty URL")
    # Block dangerous schemes before adding default
    _lower = url.lower()
    for bad in ("file:", "data:", "javascript:", "vbscript:", "ftp:", "blob:"):
        if _lower.startswith(bad):
            raise ValueError(f"Blocked URL scheme: {bad}")
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"Unsupported URL scheme: {parsed.scheme}. Only http/https allowed.")
    if not parsed.netloc:
        raise ValueError("Invalid URL: missing domain")

    # SSRF protection — block internal/private IPs (loopback conditionally allowed).
    # Self-loopback (harness's own port) is always allowed: the agent owns this server.
    hostname = parsed.hostname or ""
    if _is_self_loopback(hostname, parsed.port):
        return url
    if _is_blocked_host(hostname, allow_loopback=allow_loopback):
        raise ValueError(f"Blocked: {hostname} resolves to a private/internal address")

    return url


# Max HTML size to pass to trafilatura (5MB)
_MAX_HTML_BYTES = 5_000_000


async def _browse_and_extract_async(url: str, allow_loopback: bool, ctx: dict | None) -> str:
    """Full nav + content extraction as one coroutine. Runs on the FastAPI loop."""
    browser = await _get_browser_async()
    timeout_ms = settings.browser_timeout * 1000
    context = None
    page = None
    console_msgs: list[str] = []
    page_errors: list[str] = []
    html = ""
    title = ""
    final_url = url
    try:
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        )
        page = await context.new_page()

        # Console + runtime-error capture so agents iterating on local HTML
        # get direct feedback instead of guessing from silent renders.
        def _on_console(msg):
            try:
                t = getattr(msg, "type", "") or ""
                if t in ("error", "warning"):
                    text = getattr(msg, "text", "") or ""
                    if len(console_msgs) < 50:
                        console_msgs.append(f"[{t}] {text[:500]}")
            except Exception:
                pass

        def _on_pageerror(exc):
            try:
                if len(page_errors) < 20:
                    page_errors.append(str(exc)[:1000])
            except Exception:
                pass

        try:
            page.on("console", _on_console)
            page.on("pageerror", _on_pageerror)
        except Exception:
            pass

        # SSRF: intercept requests to block navigation to internal hosts.
        # Self-loopback (harness's own port) is allowed even in network mode.
        async def _ssrf_route_handler(route, request):
            from urllib.parse import urlparse as _urlparse

            parsed_req = _urlparse(request.url)
            req_host = parsed_req.hostname or ""
            if (
                req_host
                and not _is_self_loopback(req_host, parsed_req.port)
                and _is_blocked_host(req_host, allow_loopback=allow_loopback)
            ):
                await route.abort("blockedbyclient")
            else:
                await route.continue_()

        await page.route("**/*", _ssrf_route_handler)

        await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        try:
            await page.wait_for_load_state("networkidle", timeout=min(timeout_ms, 10_000))
        except Exception:
            pass

        final_url = page.url
        try:
            final_parsed = urlparse(final_url)
            final_host = final_parsed.hostname or ""
            if (
                final_host
                and not _is_self_loopback(final_host, final_parsed.port)
                and _is_blocked_host(final_host, allow_loopback=allow_loopback)
            ):
                return f"Error: Redirect to blocked internal address: {final_host}"
        except Exception:
            pass

        html = await page.content()
        title = await page.title()
    except Exception as e:
        err = str(e)
        if "timeout" in err.lower():
            return f"Error: Page load timed out after {settings.browser_timeout}s for {url}"
        return f"Error navigating to {url}: {err}"
    finally:
        if page:
            try:
                await page.close()
            except Exception:
                pass
        if context:
            try:
                await context.close()
            except Exception:
                pass

    diag: list[str] = []
    for e in page_errors:
        diag.append(f"[pageerror] {e}")
    diag.extend(console_msgs)

    # Cap HTML size before extraction to prevent OOM
    if len(html) > _MAX_HTML_BYTES:
        html = html[:_MAX_HTML_BYTES]
        logger.warning("HTML truncated to %d bytes before extraction for %s", _MAX_HTML_BYTES, url)

    # Trafilatura — pure CPU. Capped at 5MB above; runs inline. If profiling
    # later shows blocking, wrap in `await asyncio.to_thread(...)`.
    content = None
    try:
        import trafilatura

        content = trafilatura.extract(
            html,
            output_format="markdown",
            include_links=True,
            include_images=False,
            include_tables=True,
            favor_recall=True,
        )
    except ImportError:
        logger.warning("trafilatura not installed, falling back to raw text extraction")
    except Exception as e:
        logger.warning("trafilatura extraction failed: %s, falling back to raw text", e)

    if not content:
        try:

            class _TextExtractor(HTMLParser):
                def __init__(self):
                    super().__init__()
                    self.parts = []
                    self._skip = False

                def handle_starttag(self, tag, attrs):
                    if tag in ("script", "style", "noscript"):
                        self._skip = True

                def handle_endtag(self, tag):
                    if tag in ("script", "style", "noscript"):
                        self._skip = False

                def handle_data(self, data):
                    if not self._skip:
                        t = data.strip()
                        if t:
                            self.parts.append(t)

            extractor = _TextExtractor()
            extractor.feed(html)
            content = "\n".join(extractor.parts)
        except Exception:
            content = "(Failed to extract content)"

    max_size = settings.max_fetch_size
    if len(content) > max_size:
        content = content[:max_size] + f"\n\n[truncated at {max_size} bytes]"

    header = f"# {title}\n**URL:** {final_url}\n\n---\n\n" if title else f"**URL:** {final_url}\n\n---\n\n"

    diag_section = ""
    if diag:
        joined = "\n".join(f"- {line}" for line in diag[:60])
        diag_section = f"\n\n---\n\n## Console Output\n\n{joined}\n"

    _emit_browse_event(ctx, {"type": "browse.done", "url": final_url, "title": title or ""})
    return header + content + diag_section


def browse_web(url: str, _context: dict | None = None) -> str:
    """Navigate to a URL with a real browser, extract clean content as markdown.

    Stays sync to fit the tool registry signature. Internally bridges from the
    executor's worker thread to the FastAPI asyncio loop via
    asyncio.run_coroutine_threadsafe — async Playwright runs on the loop.
    """
    if not settings.browser_enabled:
        return (
            "Error: Browser is disabled. Enable it in Settings → Web / Browser → "
            "Enable Browser (Playwright), then try again. No restart required."
        )

    allow_loopback = _loopback_allowed()

    try:
        url = _validate_url(url, allow_loopback=allow_loopback)
    except ValueError as e:
        return f"Error: {e}"

    _emit_browse_event(_context, {"type": "browse.start", "url": url})

    import asyncio as _asyncio
    import concurrent.futures as _futures

    loop = (_context or {}).get("_loop")
    if loop is None:
        return "Error: browse_web requires the event loop context. Internal error."

    coro = _browse_and_extract_async(url, allow_loopback, _context)
    try:
        fut = _asyncio.run_coroutine_threadsafe(coro, loop)
    except RuntimeError as e:
        return f"Error: browser unavailable (loop not running): {e}"

    # Inner timeout = browser_timeout + 10s grace; the executor's outer timeout
    # (registered tool timeout) is the upper bound.
    try:
        return fut.result(timeout=settings.browser_timeout + 10)
    except _futures.TimeoutError:
        fut.cancel()
        return f"Error: browse_web timed out after {settings.browser_timeout}s for {url}"
    except _futures.CancelledError:
        return "Error: browse_web was cancelled"
    except Exception as e:
        return f"Error navigating to {url}: {e}"


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def register(reg) -> None:
    """Register web extension tools."""
    if settings.web_search_enabled:
        reg.register(
            name="search_web",
            func=search_web,
            description="Search the web for information using Tavily. Requires TAVILY_API_KEY set in Settings → Web. Returns titles, URLs, and snippets.",
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query"},
                    "num_results": {"type": "integer", "description": "Number of results (default 5, max 10)"},
                },
                "required": ["query"],
            },
            category="web",
            tags=["search", "web", "internet", "find", "lookup", "research", "google", "information"],
            timeout=60,
            parallel_safe=True,
            source="extension",
            safety_level="dangerous",
        )

    reg.register(
        name="http_get",
        func=http_get,
        description="Fetch content from a URL. Returns plain text. Max 100KB. Follows redirects.",
        parameters={
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "URL to fetch"},
            },
            "required": ["url"],
        },
        category="web",
        tags=["http", "fetch", "url", "get", "download", "web", "page", "content"],
        timeout=15,
        parallel_safe=True,
        source="extension",
        safety_level="safe",
    )

    # Register browse_web if playwright is installed and browser_enabled is set
    try:
        import playwright  # noqa: F401

        if not settings.browser_enabled:
            logger.info("browse_web tool not registered (browser_enabled=False)")
            return

        reg.register(
            name="browse_web",
            func=browse_web,
            description=(
                "Navigate to a URL with a full browser engine (renders JavaScript). "
                "Extracts clean markdown content from the page and appends a "
                "'## Console Output' section with any JS errors/warnings the page "
                "logged (empty if none). Use this for JS-heavy sites, SPAs, or "
                "pages http_get returns garbled content for. "
                "In localhost mode you can browse workspace files directly at "
                "http://localhost:8090/workspace/<file> to verify rendering and "
                "catch console errors without spinning up your own server. "
                "Slower than http_get (~2-10s) but much more reliable for modern websites. "
                "Requires browser_enabled=True in settings."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "URL to navigate to (http/https only)"},
                },
                "required": ["url"],
            },
            category="web",
            tags=[
                "browse",
                "browser",
                "web",
                "page",
                "javascript",
                "render",
                "navigate",
                "playwright",
                "content",
                "fetch",
                "url",
                "validate",
                "test",
                "verify",
                "html",
                "console",
                "debug",
            ],
            timeout=60,
            parallel_safe=True,
            source="extension",
            safety_level="dangerous",
        )
        logger.info("browse_web tool registered (playwright available)")
    except ImportError:
        logger.info("playwright not installed — browse_web tool not available")
