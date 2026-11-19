"""Pernix — the egress proxy: the one place the web tools resolve and connect.

The address guard in this package (`_validate_url`, the browser route
handlers) resolves a name and decides whether it is safe. The fetch that
follows used to resolve the name AGAIN to connect: httpx through the OS
resolver, Chromium through its own. A hostile name can answer the check with a
public address and the connect with a private one (DNS rebinding), so the
address that was checked was not the address that was used.

This proxy closes that gap. http_get, browse_web and the browser tool send
their traffic through it, and for each connection it resolves the name once,
tests every address it got back, and connects to a vetted address by its IP
literal. The name is never handed back to a resolver after the check.

It speaks the two forms clients use with an HTTP proxy:

- `CONNECT host:port` — a byte tunnel. TLS runs end to end through it, so the
  client still verifies the real server's certificate.
- an absolute-URI request (`GET http://host/path HTTP/1.1`) for plain http —
  rewritten to origin form and sent with `Connection: close`, so one request
  per connection is all the proxy ever has to understand.

It runs on its own daemon thread with its own event loop: http_get calls come
from tool worker threads and the browser lives on the server's loop, and the
proxy must keep working whichever of those is busy or gone.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
import threading

logger = logging.getLogger("pernix.ext.web.egress")

# Request line plus headers. A client that sends more than this before the
# blank line is not a browser or httpx talking to a proxy.
_HEADER_LIMIT = 64 * 1024
_HEADER_TIMEOUT_S = 30.0
_CONNECT_TIMEOUT_S = 15.0
# A connection with no bytes in EITHER direction for this long is closed. One
# direction alone going quiet is normal (a download, a WebSocket that only
# receives), so the clock is shared by both.
_IDLE_TIMEOUT_S = 300.0
_START_TIMEOUT_S = 5.0

_HOP_BY_HOP = {"proxy-connection", "proxy-authorization", "connection", "keep-alive"}


class EgressProxyError(RuntimeError):
    """The proxy could not be started. Callers fail closed on this."""


_lock = threading.Lock()
_thread: threading.Thread | None = None
_loop: asyncio.AbstractEventLoop | None = None
_server: asyncio.base_events.Server | None = None
_url: str | None = None
_port: int | None = None


def ensure_started() -> str:
    """Start the proxy once and return its URL, e.g. ``http://127.0.0.1:41234``.

    Raises EgressProxyError when it cannot start. There is deliberately no
    fallback to direct connections: a tool that cannot go through the proxy
    does not go out at all.
    """
    global _thread, _loop, _server, _url, _port
    with _lock:
        if _url is not None and _thread is not None and _thread.is_alive():
            return _url
        loop = asyncio.new_event_loop()
        ready = threading.Event()
        box: dict = {}

        def _run() -> None:
            asyncio.set_event_loop(loop)
            try:
                srv = loop.run_until_complete(asyncio.start_server(_handle, host="127.0.0.1", port=0))
                box["server"] = srv
            except Exception as e:  # noqa: BLE001 - reported to the caller below
                box["error"] = e
                ready.set()
                loop.close()
                return
            ready.set()
            try:
                loop.run_forever()
            finally:
                try:
                    srv.close()
                    loop.run_until_complete(srv.wait_closed())
                except Exception:
                    pass
                loop.close()

        t = threading.Thread(target=_run, name="pernix-egress-proxy", daemon=True)
        t.start()
        if not ready.wait(_START_TIMEOUT_S) or "server" not in box:
            err = box.get("error") or "it did not come up in time"
            raise EgressProxyError(f"the egress proxy could not start: {err}")
        srv = box["server"]
        _thread, _loop, _server = t, loop, srv
        _port = srv.sockets[0].getsockname()[1]
        _url = f"http://127.0.0.1:{_port}"
        logger.info("egress proxy listening on %s", _url)
        return _url


def stop(timeout: float = 2.0) -> None:
    """Stop the proxy (tests, interpreter exit). Safe to call when not started.

    The thread is a daemon, so a process that never calls this still exits; this
    only closes the listener cleanly and lets a later ensure_started begin fresh.
    """
    global _thread, _loop, _server, _url, _port
    with _lock:
        loop, t = _loop, _thread
        _thread = _loop = _server = None
        _url = _port = None
    if loop is not None:
        try:
            loop.call_soon_threadsafe(loop.stop)
        except RuntimeError:
            pass  # loop already closed
    if t is not None:
        t.join(timeout)


def proxy_port() -> int | None:
    return _port


# ---------------------------------------------------------------------------
# Destination checks
# ---------------------------------------------------------------------------


def _refusal_for(ip: ipaddress._BaseAddress, port: int, own_workspace: bool = False) -> str:
    """'' when the proxy may connect to ip:port, otherwise the reason it may not.

    own_workspace is True only for a plain-http request whose URL the proxy
    has read and found to be a /workspace/ path on the harness's own address.
    """
    from core.extensions import web as _web

    plain = ip.ipv4_mapped if ip.version == 6 and ip.ipv4_mapped is not None else ip
    if plain.is_loopback:
        # The harness's own server is reachable only at /workspace/ paths.
        # The browser's request route cannot be the one to enforce that: it
        # is not shown redirect hops, so a page could bounce a request off a
        # redirect into the API. Here every hop is a new request with its
        # path in plain sight, so this is where the rule is kept. Inside a
        # CONNECT tunnel the path cannot be seen, so a tunnel to the own port
        # is refused outright. So is the proxy's own port: a loop back into
        # it would only chain connections.
        own = getattr(_web.settings, "port", None)
        if port == own:
            return "" if own_workspace else "the harness's own server is reachable only at /workspace/ paths"
        if port == _port:
            return "the egress proxy itself is not a destination"
    if _web._ip_blocked(ip, allow_loopback=_web._loopback_allowed()):
        return f"{ip} is a private or internal address"
    return ""


async def _vetted_addresses(host: str, port: int, own_workspace: bool = False) -> tuple[list[tuple], str]:
    """Resolve host ONCE; return (sockaddr infos, refusal). Any blocked address
    refuses the whole name: a name that also answers with a private address is
    not one to trust with the public answer either."""
    try:
        infos = await asyncio.get_running_loop().run_in_executor(
            None, socket.getaddrinfo, host, port, socket.AF_UNSPEC, socket.SOCK_STREAM
        )
    except (OSError, UnicodeError) as e:
        return [], f"could not resolve {host}: {e}"
    if not infos:
        return [], f"could not resolve {host}"
    for family, _type, _proto, _canon, sockaddr in infos:
        try:
            ip = ipaddress.ip_address(sockaddr[0].split("%", 1)[0])
        except ValueError:
            return [], f"{host} resolved to an unreadable address"
        reason = _refusal_for(ip, port, own_workspace)
        if reason:
            return [], f"{host} -> {reason}"
    return infos, ""


async def _connect_vetted(infos: list[tuple]):
    """Connect to the first vetted address that answers, by IP literal.

    The socket is built and connected here from the sockaddr the check saw,
    so nothing below this can resolve the name a second time.
    """
    loop = asyncio.get_running_loop()
    last: Exception | None = None
    for family, type_, proto, _canon, sockaddr in infos:
        sock = socket.socket(family, type_, proto)
        sock.setblocking(False)
        try:
            await asyncio.wait_for(loop.sock_connect(sock, sockaddr), _CONNECT_TIMEOUT_S)
            return await asyncio.open_connection(sock=sock, limit=_HEADER_LIMIT)
        except (OSError, asyncio.TimeoutError) as e:
            last = e
            sock.close()
    raise ConnectionError(f"could not connect: {last}")


# ---------------------------------------------------------------------------
# Connection handling
# ---------------------------------------------------------------------------


def _reply(writer: asyncio.StreamWriter, status: int, reason: str) -> None:
    """Write a short error response. The reason also goes in the status line,
    because httpx reports a refused CONNECT by its status line alone."""
    phrase = " ".join(reason.replace("\r", " ").replace("\n", " ").split())[:300]
    phrase = phrase.encode("ascii", "replace").decode("ascii")
    body = f"Blocked by the egress guard: {phrase}\n".encode()
    writer.write(
        f"HTTP/1.1 {status} {phrase or 'Forbidden'}\r\n"
        "Content-Type: text/plain; charset=utf-8\r\n"
        "X-Pernix-Egress: refused\r\n"
        f"Content-Length: {len(body)}\r\n"
        "Connection: close\r\n\r\n".encode() + body
    )


def _split_host_port(authority: str, default_port: int) -> tuple[str, int]:
    """'host:port', '[v6]:port' or 'host' -> (host, port). ValueError on junk."""
    authority = authority.strip()
    if not authority or any(c in authority for c in "/@\\ \t"):
        raise ValueError("bad authority")
    if authority.startswith("["):
        end = authority.index("]")
        host, rest = authority[1:end], authority[end + 1 :]
        if rest and not rest.startswith(":"):
            raise ValueError("bad authority")
        port_s = rest[1:] if rest else ""
    elif authority.count(":") == 1:
        host, port_s = authority.split(":")
    elif ":" in authority:
        raise ValueError("bare IPv6 without brackets")
    else:
        host, port_s = authority, ""
    port = int(port_s) if port_s else default_port
    if not host or not 0 < port < 65536:
        raise ValueError("bad host or port")
    return host, port


async def _pipe(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter, clock: list[float], peer_clock: list[float]
) -> None:
    """Copy reader -> writer until EOF, error, or both directions idle."""
    loop = asyncio.get_running_loop()
    while True:
        try:
            data = await asyncio.wait_for(reader.read(65536), _IDLE_TIMEOUT_S)
        except asyncio.TimeoutError:
            if loop.time() - max(clock[0], peer_clock[0]) >= _IDLE_TIMEOUT_S:
                return
            continue
        if not data:
            return
        clock[0] = loop.time()
        writer.write(data)
        await writer.drain()


async def _splice(cr, cw, ur, uw) -> None:
    """Run both directions; when either ends, the connection is over."""
    now = asyncio.get_running_loop().time()
    up_clock, down_clock = [now], [now]
    tasks = [
        asyncio.ensure_future(_pipe(cr, uw, up_clock, down_clock)),
        asyncio.ensure_future(_pipe(ur, cw, down_clock, up_clock)),
    ]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def _close(writer: asyncio.StreamWriter | None) -> None:
    if writer is None:
        return
    try:
        writer.close()
        await asyncio.wait_for(writer.wait_closed(), 2.0)
    except Exception:
        pass


async def _handle(cr: asyncio.StreamReader, cw: asyncio.StreamWriter) -> None:
    """One client connection. Never raises: every path ends in a close."""
    uw = None
    try:
        try:
            head = await asyncio.wait_for(cr.readuntil(b"\r\n\r\n"), _HEADER_TIMEOUT_S)
        except (asyncio.LimitOverrunError, ValueError):
            _reply(cw, 431, "request headers too large")
            return
        except (asyncio.IncompleteReadError, asyncio.TimeoutError, ConnectionError):
            return
        if len(head) > _HEADER_LIMIT:
            _reply(cw, 431, "request headers too large")
            return
        try:
            text = head.decode("latin-1")
            request_line, *header_lines = text[:-4].split("\r\n")
            method, target, version = request_line.split(" ")
        except ValueError:
            _reply(cw, 400, "malformed request")
            return

        if method.upper() == "CONNECT":
            try:
                host, port = _split_host_port(target, 443)
            except ValueError:
                _reply(cw, 400, "malformed CONNECT target")
                return
            infos, refusal = await _vetted_addresses(host, port)
            if refusal:
                _reply(cw, 403, refusal)
                return
            try:
                ur, uw = await _connect_vetted(infos)
            except ConnectionError as e:
                _reply(cw, 502, f"{host}:{port} {e}")
                return
            cw.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
            await cw.drain()
            await _splice(cr, cw, ur, uw)
            return

        # Plain http, absolute form. Anything else (origin form, https:// in a
        # request line, ftp://) is not a proxy request this proxy serves.
        if not target.lower().startswith("http://"):
            _reply(cw, 400, "only CONNECT and absolute http:// requests are proxied")
            return
        rest = target[len("http://") :]
        slash = min((i for i in (rest.find("/"), rest.find("?")) if i >= 0), default=len(rest))
        authority, path = rest[:slash], rest[slash:] or "/"
        if path.startswith("?"):
            path = "/" + path
        try:
            host, port = _split_host_port(authority, 80)
        except ValueError:
            _reply(cw, 400, "malformed request URL")
            return
        # The harness's own server: only its /workspace/ files, and only by a
        # loopback name. Judged on the URL of this very request, so a
        # redirect that lands here is judged like any other request.
        from core.extensions import web as _web

        try:
            own = _web._own_server_target(target)
        except ValueError:
            _reply(cw, 400, "malformed request URL")
            return
        if own == "refused":
            _reply(cw, 403, "the harness's own server is reachable only at /workspace/ paths")
            return
        infos, refusal = await _vetted_addresses(host, port, own_workspace=own == "workspace")
        if refusal:
            _reply(cw, 403, refusal)
            return
        headers = []
        has_host = False
        for line in header_lines:
            name = line.split(":", 1)[0].strip().lower()
            if name in _HOP_BY_HOP:
                continue
            has_host = has_host or name == "host"
            headers.append(line)
        if not has_host:
            headers.insert(0, f"Host: {authority}")
        headers.append("Connection: close")
        try:
            ur, uw = await _connect_vetted(infos)
        except ConnectionError as e:
            _reply(cw, 502, f"{host}:{port} {e}")
            return
        uw.write(
            f"{method} {path} {version}\r\n".encode("latin-1") + "\r\n".join(headers).encode("latin-1") + b"\r\n\r\n"
        )
        await uw.drain()
        # The body (if any) and the response are then copied as bytes. The
        # upstream closes after one response because of Connection: close; a
        # client that pipelines a second request on this connection only
        # reaches the same vetted address, and gets nothing back.
        await _splice(cr, cw, ur, uw)
    except Exception as e:  # noqa: BLE001 - a handler must never take the loop down
        logger.debug("egress proxy connection error: %s", e)
    finally:
        try:
            await cw.drain()
        except Exception:
            pass
        await _close(uw)
        await _close(cw)
