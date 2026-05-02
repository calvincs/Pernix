"""Pernix — Application entrypoint.

Usage:
    python run.py              # Normal start
    python run.py --rebuild    # Wipe all state and start clean (requires confirmation)
"""

import argparse
import os
import shutil
import signal
import sys
import time
from pathlib import Path

from config import settings


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
    # Note: data/agent/ (SOUL.md, RULES.md, AGENTS.md) is preserved — only the birthdate is reset.
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


def main():
    parser = argparse.ArgumentParser(description="Pernix Agent Server")
    parser.add_argument(
        "--rebuild", action="store_true", help="Wipe all state and start with a clean slate (requires confirmation)"
    )
    parser.add_argument("--host", default=None, help="Override host")
    parser.add_argument("--port", type=int, default=None, help="Override port")
    parser.add_argument("--qr", action="store_true", help="Show QR code and access URL on startup (network mode only)")
    args = parser.parse_args()

    # Clean stale restart flag from any prior interrupted restart
    Path("data/.restart").unlink(missing_ok=True)

    if args.rebuild:
        print("\nWARNING: This will wipe all sessions, memories, workspace, and logs.")
        print("Preserved: settings.json, .env, data/skills/, data/certs/, data/agent/")
        confirm = input("\nType 'yes' to confirm: ").strip()
        if confirm != "yes":
            print("Aborted.")
            sys.exit(0)
        rebuild_start()

    # Ensure agent identity dir exists and SOUL.md has a birthdate stamp.
    import re as _re
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
            access_url = f"{base}/?token={settings.auth_token}"
        else:
            lan_ip = _get_lan_ip()
            access_url = f"{scheme}://{lan_ip}:{port}/?token={settings.auth_token}"
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

    import logging

    import uvicorn

    # Suppress noisy polling endpoints from access logs
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

    logging.getLogger("uvicorn.access").addFilter(_PollFilter())

    # Suppress expected CancelledError tracebacks during graceful shutdown.
    # When uvicorn's graceful-shutdown timeout expires, it force-cancels any
    # in-flight SSE StreamingResponse tasks (specifically the Starlette
    # listen_for_disconnect coroutine).  These are harmless and expected.
    class _ShutdownNoiseFilter(logging.Filter):
        def filter(self, record: logging.LogRecord) -> bool:
            if record.exc_info and record.exc_info[1] is not None:
                import asyncio

                if isinstance(record.exc_info[1], asyncio.CancelledError):
                    return False
            return True

    logging.getLogger("uvicorn.error").addFilter(_ShutdownNoiseFilter())

    # Let uvicorn handle signals for graceful shutdown.
    # The default SIGINT handler (KeyboardInterrupt) works with uvicorn's
    # graceful shutdown. We only install a handler for the second Ctrl+C
    # (force quit).
    _shutting_down = False

    def _handle_signal(sig, frame):
        nonlocal _shutting_down
        if _shutting_down:
            print("\nForce quit.")
            sys.exit(1)
        _shutting_down = True
        print("\nShutting down gracefully... (Ctrl+C again to force)")
        # Raise KeyboardInterrupt so uvicorn can handle graceful shutdown
        raise KeyboardInterrupt

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    uvicorn.run(
        "api.app:app",
        host=host,
        port=port,
        **ssl_kwargs,
        timeout_graceful_shutdown=15,
        log_level="info",
    )

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
