"""Pernix — Application entrypoint.

Usage:
    python run.py              # Normal start
    python run.py --rebuild    # Wipe all state and start clean (requires confirmation)
"""

import argparse
import logging
import os
import re
import shutil
import signal
import sys
import time
from pathlib import Path

from config import settings
from core.logging_setup import ACCESS_LOGGERS

# Query-string carriers for the access token. `token` is the onboarding param
# the auth middleware accepts; `access_token` is not used today and is matched
# so a future rename cannot silently reopen the hole.
_TOKEN_QS_RE = re.compile(r"""(?i)([?&](?:token|access_token)=)[^&\s"']*""")


def _redact_tokens(value: str) -> str:
    """Replace any ?token=<secret> value in a string with a placeholder."""
    return _TOKEN_QS_RE.sub(r"\1REDACTED", value)


class _TokenRedactFilter(logging.Filter):
    """Keep access tokens out of the access log.

    QR codes and shared links used to carry `/?token=<secret>`, which uvicorn
    writes verbatim: `GET /?token=<a working credential> HTTP/1.1 200`. On the
    Docker deployment stdout IS the log, so `docker compose logs` handed out
    live credentials to anyone who could read it, and to anywhere those logs
    get shipped. Deleting the line later does not help — a token that has been
    written down is already disclosed.

    Onboarding links now carry the token in the URL FRAGMENT, which browsers
    never transmit, so nothing reaches the server to log. This filter covers
    what that cannot: links already handed out, which must keep working.

    uvicorn.access formats as '%s - "%s %s HTTP/%s" %d' with the path in
    args[2], so the path is rewritten in place rather than the rendered
    message — that leaves _PollFilter's matching, and any structured handler,
    working on the normal shape.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        # granian.access logs `fmt % dict`. Its default line carries the path
        # WITHOUT the query string, but a custom format can add
        # %(query_string)s, so both fields are scrubbed.
        if isinstance(args, dict):
            patched = dict(args)
            path, qs = patched.get("path"), patched.get("query_string")
            if isinstance(path, str):
                patched["path"] = _redact_tokens(path)
            if isinstance(qs, str):
                patched["query_string"] = _redact_tokens("?" + qs)[1:]  # the regex anchors on ? or &
            record.args = patched
            return True
        if isinstance(args, tuple) and len(args) >= 3 and isinstance(args[2], str):
            if "token=" in args[2].lower():
                patched = list(args)
                patched[2] = _redact_tokens(args[2])
                record.args = tuple(patched)
        elif isinstance(record.msg, str) and "token=" in record.msg.lower():
            record.msg = _redact_tokens(record.msg)
        return True


def rebuild_start():
    """Wipe all runtime state back to a clean slate."""
    print("Performing rebuild — wiping all state...")

    # Database files
    for f in ["data/sessions.db", "data/sessions.db-wal", "data/sessions.db-shm"]:
        p = Path(f)
        if p.exists():
            p.unlink()
            print(f"  Removed {f}")

    # Memory files and index
    memories = Path("data/memories")
    if memories.exists():
        shutil.rmtree(memories)
        print(f"  Removed {memories}/")

    # Runtime state files
    # Note: settings.json and .env are preserved (user configuration)
    # Note: data/agent/ (SOUL.md, RULES.md, SESSIONS.md) is preserved — only the birthdate is reset.
    for f in [
        "data/registry.json",
        "data/registry_archive.json",
        "data/cron_jobs.json",
        "data/tools.json",
        "data/model_pref.txt",
    ]:
        p = Path(f)
        if p.exists():
            p.unlink()
            print(f"  Removed {f}")

    # Reset birthdate in SOUL.md so the next launch stamps a fresh birth time.
    # User-authored identity content below the birthdate line is preserved.
    import re as _re

    soul_path = Path("data/agent/SOUL.md")
    if soul_path.exists():
        content = soul_path.read_text()
        updated = _re.sub(r"<!-- @birthdate:.*?-->\n?", "", content).lstrip("\n")
        soul_path.write_text(updated)
        print("  Reset birthdate in data/agent/SOUL.md")

    # Workspace
    workspace = Path("data/workspace")
    if workspace.exists():
        shutil.rmtree(workspace)
        print(f"  Removed {workspace}/")

    # Logs
    for f in Path("data").glob("pernix.log*"):
        f.unlink()
        print(f"  Removed {f}")
    logs_dir = Path("data/logs")
    if logs_dir.exists():
        shutil.rmtree(logs_dir)
        print(f"  Removed {logs_dir}/")

    # Note: data/skills/, data/certs/, and data/agent/ are intentionally preserved.
    agent_files = (
        [f.name for f in Path("data/agent").glob("*.md") if f.name != "README.md"]
        if Path("data/agent").exists()
        else []
    )
    if agent_files:
        print(f"\n  Note: data/agent/ preserved ({', '.join(sorted(agent_files))}).")
        print("        To restore defaults: git checkout data/agent/")
    print("\nRebuild complete. All state wiped.\n")


def _setup_logging() -> None:
    """Configure root logger before uvicorn boots so uvicorn's loggers
    (which propagate to root when log_config=None) emit with the same
    format as the rest of the app."""
    from core.logging_setup import setup_logging

    setup_logging()


def _get_lan_ip() -> str:
    """Detect the machine's LAN IP via the default route. No packets sent."""
    import socket

    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "localhost"


# Suppress noisy polling endpoints from access logs. Both servers render
# '"METHOD /path HTTP/x" status', which is all _PollFilter matches on.
# Granian logs the path without its query string, so a poll of
# /api/sessions?limit=500 arrives here as "GET /api/sessions ".
_QUIET_PREFIXES = (
    "GET /api/health ",
    "GET /api/sessions ",
    "GET /api/questions ",
    "GET /api/notifications ",
    "GET /api/jobs/status ",
)
# Session-specific polling paths: /status and /events contain a session ID
# in the middle, so match by suffix fragment instead of prefix.
_QUIET_SESSION_SUFFIXES = ("/status ", "/events ")


class _PollFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        if "200" not in msg:
            return True
        if any(p in msg for p in _QUIET_PREFIXES):
            return False
        if "/api/sessions/" in msg and any(s in msg for s in _QUIET_SESSION_SUFFIXES):
            return False
        return True


class _ShutdownNoiseFilter(logging.Filter):
    """Drop the expected CancelledError tracebacks of a graceful shutdown.

    When the shutdown timeout expires the server cancels in-flight SSE
    StreamingResponse tasks (Starlette's listen_for_disconnect). Harmless.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if record.exc_info and record.exc_info[1] is not None:
            import asyncio

            if isinstance(record.exc_info[1], asyncio.CancelledError):
                return False
        return True


def _install_log_filters() -> None:
    # Redaction goes on before _PollFilter so nothing downstream — including
    # any handler added later — can ever see an un-redacted path.
    for name in ACCESS_LOGGERS:
        logging.getLogger(name).addFilter(_TokenRedactFilter())
        logging.getLogger(name).addFilter(_PollFilter())
    for name in ("uvicorn.error", "_granian"):
        logging.getLogger(name).addFilter(_ShutdownNoiseFilter())


# How long a stop waits for open requests — live SSE streams among them —
# before it cancels them. docker-compose's stop_grace_period must outlast
# this plus the lifespan shutdown.
GRACEFUL_SHUTDOWN_S = 15

UVICORN_DEPRECATION = (
    "Serving with uvicorn is deprecated and will be removed in a future release; "
    "Pernix will serve every mode with Granian. See docs/upgrade.md."
)


def _choose_server(requested: str, *, tls: bool) -> str:
    """Granian wherever HTTP/2 can be had, uvicorn only for plain HTTP.

    Browsers speak HTTP/2 only over TLS. On HTTP/1.1 Chrome allows six
    connections per host, and each Pernix tab holds up to three open streams,
    so two tabs used to fill the pool and every other request waited (the
    60 s reload of 2026-11-04). Granian negotiates HTTP/2, which carries every
    stream and request of every tab over one connection.
    """
    if requested == "auto":
        requested = "granian" if tls else "uvicorn"
    if requested == "uvicorn":
        logging.getLogger("pernix").warning(UVICORN_DEPRECATION)
    return requested


def _pkcs8_key(key_path: str) -> str:
    """Granian reads PKCS#8 keys only. Hand it one, converting if needed.

    Pernix's own self-signed key is PKCS#8 already; a custom key may be a
    traditional "BEGIN RSA/EC PRIVATE KEY". The converted copy sits beside the
    original, owner-readable only, and is rewritten when the original changes.
    """
    src = Path(key_path)
    head = src.read_bytes()[:64]
    if b"BEGIN PRIVATE KEY" in head:
        return key_path
    if b"ENCRYPTED" in head:
        raise SystemExit(f"Encrypted private keys are not supported: {key_path}")
    from cryptography.hazmat.primitives import serialization

    dst = src.with_name(src.name + ".pkcs8.pem")
    if not dst.exists() or dst.stat().st_mtime < src.stat().st_mtime:
        key = serialization.load_pem_private_key(src.read_bytes(), password=None)
        pem = key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
        )
        dst.touch(mode=0o600, exist_ok=True)
        dst.write_bytes(pem)
    return str(dst)


def _serve_uvicorn(host: str, port: int, ssl_kwargs: dict) -> None:
    """Deprecated: plain-HTTP fallback, one connection per stream."""
    import uvicorn

    # Let uvicorn handle signals for graceful shutdown. The default SIGINT
    # handler (KeyboardInterrupt) works with uvicorn's graceful shutdown; we
    # only add the second Ctrl+C (force quit).
    _shutting_down = False

    def _handle_signal(sig, frame):
        nonlocal _shutting_down
        if _shutting_down:
            print("\nForce quit.")
            sys.exit(1)
        _shutting_down = True
        print("\nShutting down gracefully... (Ctrl+C again to force)")
        raise KeyboardInterrupt

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    uvicorn.run(
        "api.app:app",
        host=host,
        port=port,
        **ssl_kwargs,
        timeout_graceful_shutdown=GRACEFUL_SHUTDOWN_S,
        log_level="info",
        log_config=None,
    )


def _serve_granian(host: str, port: int, ssl_kwargs: dict) -> None:
    """Serve with Granian, HTTP/2 over TLS, inside this process.

    Embedded rather than Granian's worker processes: the worker would be a
    fresh interpreter wherever multiprocessing spawns instead of forking
    (macOS, Python 3.14), and it would silently lose what main() set up in
    this one — the in-memory --dangerous flag, which by design never touches
    disk or the environment, and the token-redacting log filters. Pernix keeps
    its live state in one process anyway, so one worker is all it can use.
    """
    import asyncio

    from granian.constants import HTTPModes, Interfaces
    from granian.log import LogLevels
    from granian.server.embed import Server

    from api.app import app

    tls = {}
    if ssl_kwargs:
        tls = {
            "ssl_cert": Path(ssl_kwargs["ssl_certfile"]),
            "ssl_key": Path(_pkcs8_key(ssl_kwargs["ssl_keyfile"])),
        }

    # Granian's own handlers would print past the app's format; route its
    # loggers to the root handlers _setup_logging installed instead.
    log_config = {
        "loggers": {
            "_granian": {"handlers": [], "propagate": True},
            "granian.access": {"handlers": [], "propagate": True},
        },
    }

    server = Server(
        app,
        address=host,
        port=port,
        interface=Interfaces.ASGI,
        http=HTTPModes.auto,
        log_level=LogLevels.info,
        log_dictconfig=log_config,
        log_access=True,
        # The root formatter already stamps the time; Granian's default adds a second.
        log_access_format='%(addr)s - "%(method)s %(path)s %(protocol)s" %(status)d %(dt_ms).1fms',
        **tls,
    )
    server.workers_kill_timeout = GRACEFUL_SHUTDOWN_S

    async def _run() -> None:
        loop = asyncio.get_running_loop()
        stopping = False

        def _on_signal() -> None:
            nonlocal stopping
            if stopping:
                print("\nForce quit.")
                os._exit(1)
            stopping = True
            print("\nShutting down gracefully... (Ctrl+C again to force)")
            # Streams first. Granian runs the lifespan shutdown only once open
            # requests are done, and a live SSE stream is never done on its
            # own: it held every stop for the full grace, was cancelled, and
            # the lifespan shutdown (turn drain, browser and MCP close) never
            # ran. Ending them here is the lifespan's own first step, early.
            from api.app import end_all_streams

            end_all_streams()
            server.stop()

        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, _on_signal)
        try:
            await server.serve()
        except (TimeoutError, asyncio.TimeoutError):
            # A worker that outlived GRACEFUL_SHUTDOWN_S was cancelled.
            logging.getLogger("pernix").warning("server: open requests cancelled after %ss", GRACEFUL_SHUTDOWN_S)

    asyncio.run(_run())


def main():
    parser = argparse.ArgumentParser(description="Pernix Agent Server")
    parser.add_argument(
        "--rebuild", action="store_true", help="Wipe all state and start with a clean slate (requires confirmation)"
    )
    parser.add_argument("--host", default=None, help="Override host")
    parser.add_argument("--port", type=int, default=None, help="Override port")
    parser.add_argument("--qr", action="store_true", help="Show QR code and access URL on startup (network mode only)")
    parser.add_argument(
        "--dangerous",
        action="store_true",
        help=(
            "Bypass the dangerous-tool approval gate for this process lifetime. "
            "Shell commands, file writes, and web requests execute without ask_user confirmation. "
            "This flag cannot be set via settings.json or environment variables — "
            "it must be passed explicitly at startup."
        ),
    )
    parser.add_argument(
        "--server",
        choices=("auto", "granian", "uvicorn"),
        default=os.environ.get("PERNIX_SERVER", "auto"),
        help=(
            "HTTP server. auto (default): Granian when TLS is on (HTTP/2), uvicorn on plain HTTP. "
            "The uvicorn path is deprecated and will be removed in a future release."
        ),
    )
    args = parser.parse_args()

    # Clean stale restart flag from any prior interrupted restart
    Path("data/.restart").unlink(missing_ok=True)

    # --dangerous: activate in-memory only (never saved to disk or read from env)
    if args.dangerous:
        settings.auto_approve_dangerous = True
        print("  ⚠  WARNING: --dangerous flag active — all tool approvals are bypassed")

    if args.rebuild:
        print("\nWARNING: This will wipe all sessions, memories, workspace, and logs.")
        print("Preserved: settings.json, .env, data/skills/, data/certs/, data/agent/")
        confirm = input("\nType 'yes' to confirm: ").strip()
        if confirm != "yes":
            print("Aborted.")
            sys.exit(0)
        rebuild_start()

    # Ensure agent identity dir exists and SOUL.md has a birthdate stamp.
    from datetime import datetime, timezone

    Path("data").mkdir(parents=True, exist_ok=True)
    Path("data/agent").mkdir(parents=True, exist_ok=True)
    Path("data/skills").mkdir(parents=True, exist_ok=True)
    _soul_path = Path("data/agent/SOUL.md")
    _birthdate_line = f"<!-- @birthdate: {datetime.now(timezone.utc).isoformat()} -->\n"
    if not _soul_path.exists():
        _soul_path.write_text(_birthdate_line)
    elif "<!-- @birthdate:" not in _soul_path.read_text():
        _soul_path.write_text(_birthdate_line + _soul_path.read_text())

    # Determine bind address and SSL from network settings
    if not args.host and settings.network_enabled:
        host = "0.0.0.0"
    else:
        host = args.host or settings.host  # default: 127.0.0.1
    port = args.port or settings.port

    # Auto-generate auth token for network mode
    if settings.network_enabled and not settings.auth_token:
        import secrets

        settings.auth_token = secrets.token_urlsafe(32)
        settings.save()

    ssl_kwargs = {}
    if settings.network_enabled:
        from core.certs import ensure_ssl_certs

        cert, key = ensure_ssl_certs(settings.ssl_mode, settings.ssl_cert_path, settings.ssl_key_path)
        ssl_kwargs = {"ssl_keyfile": key, "ssl_certfile": cert}
        scheme = "https"
    else:
        scheme = "http"

    print(f"\n  Pernix \u2192 {scheme}://{host}:{port}")

    # Network mode: print shareable access URL + QR code for mobile onboarding
    if settings.network_enabled and settings.auth_token and args.qr:
        if settings.cors_origins:
            base = settings.cors_origins[0].rstrip("/")
            access_url = f"{base}/#token={settings.auth_token}"
        else:
            lan_ip = _get_lan_ip()
            access_url = f"{scheme}://{lan_ip}:{port}/#token={settings.auth_token}"
        print(f"  Auth token: {settings.auth_token}")
        print(f"  Access URL: {access_url}")
        try:
            import qrcode

            qr = qrcode.QRCode(box_size=1, border=1)
            qr.add_data(access_url)
            qr.make(fit=True)
            print()
            qr.print_ascii(invert=True)
        except ImportError:
            pass  # qrcode package not installed

    print()

    _install_log_filters()
    _setup_logging()
    server = _choose_server(args.server, tls=bool(ssl_kwargs))

    if server == "granian":
        _serve_granian(host, port, ssl_kwargs)
    else:
        _serve_uvicorn(host, port, ssl_kwargs)

    # Check for restart request (set by POST /api/admin/restart)
    restart_flag = Path("data/.restart")
    if restart_flag.exists():
        restart_flag.unlink(missing_ok=True)
        print("\nRestarting server...\n")
        time.sleep(1.5)  # allow OS to release port from TCP TIME_WAIT before rebinding
        restart_args = [a for a in sys.argv if a != "--rebuild"]
        os.execv(sys.executable, [sys.executable] + restart_args)


if __name__ == "__main__":
    main()
