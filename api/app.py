"""Pernix — FastAPI application with lifecycle management."""

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from config import APP_VERSION, settings
from db.database import init_db

logger = logging.getLogger("pernix.api")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown lifecycle."""
    # Startup — configure logging with console + rotating file.
    # Skip if run.py already configured the root logger (the normal path);
    # this branch covers test imports and other entry points that load the
    # app without going through run.py.
    root = logging.getLogger()
    if not root.handlers:
        from logging.handlers import RotatingFileHandler
        from pathlib import Path as _P

        log_dir = _P("data/logs")
        log_dir.mkdir(parents=True, exist_ok=True)
        log_fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
        root.setLevel(logging.INFO)
        console = logging.StreamHandler()
        console.setFormatter(log_fmt)
        root.addHandler(console)
        file_h = RotatingFileHandler(
            log_dir / "pernix.log",
            maxBytes=10_000_000,
            backupCount=3,
            encoding="utf-8",
        )
        file_h.setFormatter(log_fmt)
        root.addHandler(file_h)
    logger.info("Pernix starting on %s:%d", settings.host, settings.port)

    # 0. Capture the main event loop so tool threads can marshal event
    # delivery back onto it (core.events.run_on_loop).
    from core.events import set_main_loop

    set_main_loop()

    # 1. Database
    init_db()

    # 2. Tool registry + extensions
    from core.extensions import load_extensions
    from core.tools.builtin import load_builtin_tools
    from core.tools.registry import get_registry

    registry = get_registry()
    load_builtin_tools(registry)
    builtin_count = len(registry.all_tools())
    extensions = load_extensions(registry)
    registry.load_config()
    registry.rebuild_index()
    logger.info(
        "Tools loaded: %d registered (%d builtin + %d from %d extensions)",
        len(registry.all_tools()),
        builtin_count,
        len(registry.all_tools()) - builtin_count,
        len(extensions),
    )

    # 2.6 Skill registry — scan data/skills/ for SKILL.md packages
    from pathlib import Path

    from core.skills.registry import get_skill_registry

    skills_dir = Path(settings.skills_dir)
    skills_dir.mkdir(parents=True, exist_ok=True)
    skill_reg = get_skill_registry()
    skill_count = skill_reg.scan(skills_dir)
    logger.info("Skills loaded: %d skills scanned from %s", skill_count, skills_dir)

    # 2.7 MCP manager — connects the servers in data/mcp_servers.json in the
    # background and registers their tools as each comes ready. start()
    # only spawns supervisor tasks; a slow or dead server never blocks boot.
    if settings.mcp_enabled:
        try:
            from core.extensions.mcp.manager import get_mcp_manager

            await get_mcp_manager().start()
        except Exception as e:
            logger.warning("MCP manager start failed (continuing): %s", e)

    # 2.8 Orphan rlm_runs sweep — same reasoning: the RLM engine is synchronous
    # and its child self-reaps with the server, so a 'running' row across a
    # restart is dead. Retention later purges the dir + row.
    try:
        from db import models as _db_models

        rlm_orphans = _db_models.fail_orphaned_rlm_runs()
        if rlm_orphans:
            logger.warning("Marked %d orphaned rlm_runs at startup", rlm_orphans)
    except Exception as e:
        logger.warning("RLM orphan sweep failed: %s", e)

    # 2.4 Token estimator — built lazily on the first compile, which put a
    # cold tiktoken load (a 1.7MB CDN download when the cache is empty) on
    # the first turn after every deploy. Warm it here instead, off the
    # request path and off the event loop. Not awaited: nothing before the
    # first compile needs it, and a slow CDN must not hold up startup.
    async def _warm_token_estimator() -> None:
        try:
            from core.context.tokens import get_estimator

            await asyncio.to_thread(get_estimator)
        except Exception as e:
            logger.debug("Token estimator warm-up failed (falls back on first use): %s", e)

    asyncio.create_task(_warm_token_estimator())

    # 2.5 Model registry — populate from provider APIs
    from core.llm.client import get_llm_client

    llm_client = get_llm_client()
    try:
        await llm_client.populate_registry()
    except Exception as e:
        logger.warning("Failed to populate model registry at startup: %s", e)

    # 3. Session manager + agent runner
    from core.agent import run_agent
    from sessions.manager import get_manager

    manager = get_manager()
    manager.set_agent_runner(run_agent)

    # 3.1 Reconcile sessions left in AWAITING_WORKERS by the previous run.
    # Workers may have completed while the server was down; without this
    # sweep, parents would silently wait until the reaper's backstop fires.
    try:
        resumed = await manager.reconcile_awaiting_workers()
        if resumed:
            logger.info("Reconciled %d AWAITING_WORKERS session(s) at startup", resumed)
    except Exception as e:
        logger.warning("AWAITING_WORKERS reconcile failed (continuing): %s", e)

    # 3.2 Reconcile sessions left in PROCESSING by the previous run.
    # Any session still in PROCESSING at startup has a dead agent task
    # (server restarted mid-turn). Reset them to IDLE_READY immediately
    # rather than waiting up to 5 minutes for the reaper to fire.
    try:
        reset = await manager.reconcile_processing_sessions()
        if reset:
            logger.info("Reconciled %d stuck PROCESSING session(s) at startup", reset)
    except Exception as e:
        logger.warning("PROCESSING reconcile failed (continuing): %s", e)

    # 3.3 Reconcile sessions left in any other non-terminal state (scouting,
    # compacting, cancelling, finalizing, pause). No asyncio task survives a
    # restart, so these are always phantom turns — without the sweep a new
    # prompt queues behind them for 5-15 minutes until the reaper fires.
    try:
        reset = await manager.reconcile_interrupted_sessions()
        if reset:
            logger.info("Reconciled %d interrupted session(s) at startup", reset)
    except Exception as e:
        logger.warning("Interrupted-session reconcile failed (continuing): %s", e)

    # 1.5 VAPID key generation (idempotent — skipped if keys already present)
    if not settings.vapid_private_key or not settings.vapid_public_key:
        try:
            from core.push import generate_vapid_keys

            generate_vapid_keys()
            logger.info("VAPID keys generated and saved")
        except Exception as e:
            logger.warning("VAPID key generation failed (pywebpush installed?): %s", e)

    # 3.5 Notification dispatcher (headless question alerts, webhooks)
    from core.notify import get_dispatcher

    notifier = get_dispatcher()
    notifier.start()

    # 3.9 Cron uncertain-run sweep — must run BEFORE the scheduler starts so
    # no job fires into a half-reconciled table. Claimed/running rows left by
    # a dead process are marked 'uncertain' and never replayed; the user gets
    # a notification listing what may or may not have fired. Adaptation plan 1c.
    try:
        from core.extensions.scheduling import reconcile_cron_runs

        reconcile_cron_runs()
    except Exception as e:
        logger.warning("Cron run reconcile failed (continuing): %s", e)

    # 3.95 Orphan goal sweep (plan 3b): goals whose session row is gone can
    # never complete — mark them error. Live sessions' goals stay active
    # across restarts by design.
    try:
        from db import models as _db_models

        orphan_goals = _db_models.reconcile_orphan_goals()
        if orphan_goals:
            logger.warning("Marked %d orphaned goal(s) as error at startup", orphan_goals)
    except Exception as e:
        logger.warning("Goal reconcile failed (continuing): %s", e)

    # 4. Scheduler (must init on main event loop before worker threads call it)
    try:
        from core.extensions.scheduling import ensure_canary_schedule, ensure_telos_schedule, init_scheduler

        init_scheduler()
        # Canary nightly heartbeat: derived from settings each boot, never
        # persisted — a no-op while canary_enabled is off.
        ensure_canary_schedule()
        # TELOS daily slow loops (ordo/binding + weekly audits): same
        # transient pattern — a no-op while telos_enabled is off.
        ensure_telos_schedule()
    except Exception as e:
        logger.warning("Scheduler init failed: %s", e)

    # 4b. Deploy detection: a new version means new code drove the agent, so
    # the whole canary suite re-baselines. APP_VERSION plus the git sha when
    # one is obtainable (the baked image usually has no .git — the version
    # string alone still catches releases). Never allowed to fail the boot.
    try:
        from core.extensions.scheduling import enqueue_full_sweep
        from db import models as _db

        _sha = ""
        try:
            import subprocess

            _sha = subprocess.run(
                ["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, timeout=5
            ).stdout.strip()
        except Exception:
            pass
        if not _sha:
            # No .git in the image (the deployment norm — code is baked via
            # COPY and .dockerignore drops the repo). Version alone missed
            # every same-version rebuild, so the box's "full sweep on deploy"
            # trigger never fired between releases. Fall back to a content
            # hash over the shipped code: it changes exactly when the code
            # does — hand-patched containers included — and is deterministic
            # across restarts of the same image.
            _sha = _compute_code_hash()
        _stamp = f"{APP_VERSION}+{_sha}" if _sha else APP_VERSION
        _seen = _db.get_snooze_state("app_version_seen")
        # Boot markers for the turn-boundary ledger: a session whose last
        # turn predates this boot gets one line saying the platform
        # restarted — or was UPDATED, when the stamp changed. Exhibit A of
        # the agent-ergonomics plan: the agent asked for a feature that had
        # deployed 12 days earlier, because no channel carried "the platform
        # changed" to the platform's operator.
        from datetime import datetime as _dt
        from datetime import timezone as _tz

        _db.set_snooze_state("app_last_boot_at", _dt.now(_tz.utc).isoformat())
        _db.set_snooze_state("app_last_boot_was_deploy", "1" if (_seen and _seen != _stamp) else "0")
        if _seen != _stamp:
            _db.set_snooze_state("app_version_seen", _stamp)
            # First boot ever (no stamp) is a fresh install, not a deploy.
            if _seen and settings.canary_enabled:
                logger.info("Deploy detected (%s -> %s): full canary sweep queued", _seen, _stamp)
                enqueue_full_sweep("deploy", delay_s=300)
    except Exception as e:
        logger.warning("Deploy detection failed (continuing): %s", e)

    # 4c. SYSTEM-MAP: the agent's machine-generated map of its own machinery
    # (schema, routes, blocks, stores). Regenerated every boot so it can't
    # drift; referenced from [SERVER CONTEXT]. Never allowed to fail the boot.
    try:
        from core.context.system_map import write_system_map

        await asyncio.to_thread(write_system_map, app)
    except Exception as e:
        logger.warning("SYSTEM-MAP generation failed (continuing): %s", e)

    # 5. Maintenance heartbeat
    from maintenance import get_maintenance

    maint = get_maintenance()
    maint.start()

    # 5b. Dream journal listener — narrates snooze cycles into the journal
    # session. Idles (no writes) unless dream_enabled; cancelled at shutdown.
    from core.dream.journal import run_journal_listener

    app.state.dream_journal_task = asyncio.create_task(run_journal_listener())

    # 6. Initialize SSE shutdown event on the main event loop
    from api.streaming import get_shutdown_event

    get_shutdown_event()

    logger.info("Pernix ready")
    yield

    # Shutdown — signal SSE streams first, then clean up
    logger.info("Pernix shutting down")

    # 1. Signal all SSE generators to exit cleanly
    from api.streaming import signal_shutdown

    signal_shutdown()

    # 2. Wake up all SSE subscriber queues so they notice the shutdown flag
    from sessions.manager import get_manager as _get_mgr

    _mgr = _get_mgr()
    for sid in _mgr.active_session_ids():
        s = _mgr.get(sid)
        if s:
            for q in list(s.subscribers):
                try:
                    q.put_nowait({"type": "_shutdown"})
                except Exception:
                    pass
    for q in list(_mgr._global_subscribers):
        try:
            q.put_nowait({"type": "_shutdown"})
        except Exception:
            pass
    from core.events import get_event_bus as _get_bus

    for q in list(_get_bus()._subscribers):
        try:
            q.put_nowait({"type": "_shutdown"})
        except Exception:
            pass

    # 3. Cancel any running agent tasks
    for sid in _mgr.active_session_ids():
        s = _mgr.get(sid)
        if s and s.task and not s.task.done():
            s.task.cancel()

    # Brief pause to let SSE generators finish their current iteration
    await asyncio.sleep(0.5)

    try:
        await maint.stop()
    except Exception:
        pass
    try:
        await notifier.stop()
    except Exception:
        pass
    try:
        from core.extensions.scheduling import _get_scheduler

        sched = _get_scheduler()
        if sched and hasattr(sched, "running") and sched.running:
            sched.shutdown(wait=False)
            logger.info("Scheduler shut down")
    except Exception:
        pass
    try:
        from core.extensions.web import _close_browser, _kill_driver

        # Async _close_browser runs directly on this loop — no to_thread bridge.
        await asyncio.wait_for(_close_browser(), timeout=3)
    except asyncio.TimeoutError:
        logger.warning("Browser close timed out, forcing driver kill")
        _kill_driver()
    except Exception:
        _kill_driver()
    try:
        # Kills stdio MCP children and closes HTTP transports. Bounded: a
        # wedged server must not hold the whole shutdown hostage.
        from core.extensions.mcp.manager import get_mcp_manager_if_started

        _mcp = get_mcp_manager_if_started()
        if _mcp is not None:
            await asyncio.wait_for(_mcp.shutdown(), timeout=8)
    except Exception:
        pass
    try:
        from core.llm.client import get_llm_client

        await get_llm_client().close()
    except Exception:
        pass
    try:
        # Releases the Candor store's writer flock. No-op if the bridge was
        # never created (candor_enabled=false or unused).
        from core.extensions.candor.bridge import shutdown_candor_bridge

        await shutdown_candor_bridge()
    except Exception:
        pass
    try:
        task = getattr(app.state, "dream_journal_task", None)
        if task is not None:
            task.cancel()
    except Exception:
        pass
    logger.info("Shutdown complete")


app = FastAPI(title="Pernix", version=APP_VERSION, lifespan=lifespan)

# CORS middleware — adjust for network vs localhost mode.
# This is configured once at startup. Runtime changes to network_enabled require
# a server restart (network_enabled is in _RESTART_FIELDS in api/routers/health.py),
# and the Settings UI shows a restart button when restart_required is returned by
# POST /api/settings. No dynamic CORS reconfiguration is needed.
from fastapi.middleware.cors import CORSMiddleware

if settings.network_enabled:
    if settings.cors_origins:
        _cors_origins = settings.cors_origins
        _cors_credentials = True
    else:
        # Network mode without explicit origins: allow any origin.
        # Browser spec forbids Access-Control-Allow-Origin: * with credentials.
        _cors_origins = ["*"]
        _cors_credentials = False
else:
    _default_origins = [
        f"http://localhost:{settings.port}",
        f"http://127.0.0.1:{settings.port}",
    ]
    _cors_origins = settings.cors_origins if settings.cors_origins else _default_origins
    _cors_credentials = True

app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=_cors_credentials,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization", "Last-Event-ID", "Cache-Control"],
)

# Auth middleware — require Bearer token for non-public paths in network mode.
# Uses pure ASGI (not BaseHTTPMiddleware) to avoid task-group wrapping on SSE
# streaming responses, which caused CancelledError tracebacks during shutdown.
import json as _json
import secrets
from urllib.parse import parse_qs as _parse_qs

from starlette.types import ASGIApp, Receive, Scope, Send

_PUBLIC_PREFIXES = ("/static/", "/favicon.ico")
_PUBLIC_EXACT = {"/", "/api/health", "/sw.js"}


def _extract_token(scope: Scope) -> str | None:
    """Extract auth token from Bearer header, pernix_auth cookie, or ?token= query param."""
    headers = scope.get("headers", [])

    for name, value in headers:
        if name == b"authorization":
            decoded = value.decode("latin-1")
            if decoded.startswith("Bearer "):
                return decoded[7:]
            break

    for name, value in headers:
        if name == b"cookie":
            for pair in value.decode("latin-1").split(";"):
                pair = pair.strip()
                if "=" not in pair:
                    continue
                key, _, val = pair.partition("=")
                if key.strip() == "pernix_auth":
                    return val.strip()
            break

    query_string = scope.get("query_string", b"")
    if query_string:
        params = _parse_qs(query_string.decode("latin-1"))
        token_list = params.get("token")
        if token_list:
            return token_list[0]

    return None


async def _send_401(send: Send) -> None:
    """Send a 401 Unauthorized JSON response via raw ASGI."""
    body = _json.dumps({"detail": "Unauthorized"}).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": 401,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body, "more_body": False})


class _AuthMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        if not settings.network_enabled or not settings.auth_token:
            await self.app(scope, receive, send)
            return

        if scope.get("method") == "OPTIONS":
            await self.app(scope, receive, send)
            return

        # Loopback bypass. Correct for the default single-host deployment,
        # where the UI and the server share the machine. Wrong the moment a
        # reverse proxy terminates TLS locally: every proxied request then
        # arrives from 127.0.0.1 and skips auth entirely. Operators in that
        # topology set trust_local_requests = false.
        client = scope.get("client")
        from api.routers.health import is_local_client

        if settings.trust_local_requests and client and is_local_client(client[0]):
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        if path in _PUBLIC_EXACT or any(path.startswith(p) for p in _PUBLIC_PREFIXES):
            await self.app(scope, receive, send)
            return

        # Constant-time: a plain != leaks the shared secret's prefix through
        # comparison timing.
        presented = _extract_token(scope)
        if presented is None or not secrets.compare_digest(presented, settings.auth_token):
            await _send_401(send)
            return

        await self.app(scope, receive, send)


app.add_middleware(_AuthMiddleware)

# Mount routers
from api.routers import (
    adaptive,
    canary,
    chat,
    context,
    health,
    jobs,
    mcp,
    memory,
    models,
    push,
    questions,
    rlm,
    sessions,
    skills,
    spaces,
    telos,
    tools,
    voice,
    workspace,
)

app.include_router(health.router)
app.include_router(adaptive.router)
app.include_router(canary.router)
app.include_router(sessions.router)
app.include_router(spaces.router)
app.include_router(chat.router)
app.include_router(tools.router)
app.include_router(models.router)
app.include_router(memory.router)
app.include_router(context.router)
app.include_router(questions.router)
app.include_router(workspace.router)
app.include_router(jobs.router)
app.include_router(skills.router)
app.include_router(push.router)
app.include_router(rlm.router)
app.include_router(telos.router)
app.include_router(voice.router)
app.include_router(mcp.router)


@app.get("/")
async def root():
    """Serve the frontend SPA."""
    from pathlib import Path

    from fastapi.responses import HTMLResponse

    index = Path("static/index.html")
    if index.exists():
        return HTMLResponse(
            index.read_text(),
            headers={
                "Cross-Origin-Opener-Policy": "same-origin",
                "Cross-Origin-Embedder-Policy": "credentialless",
            },
        )
    return JSONResponse({"message": "Pernix API", "docs": "/docs"})


@app.get("/favicon.ico")
async def favicon():
    import os

    from fastapi.responses import FileResponse

    png_path = os.path.join(os.path.dirname(__file__), "..", "static", "img", "favicon.png")
    return FileResponse(png_path, media_type="image/png")


def _compute_build_id() -> str:
    """Deploy fingerprint for PWA cache-busting: a hash over the shipped
    static assets' identity (path, size, mtime). Any rebuild that changes an
    asset changes the id; restarts of the same image keep it stable. Served
    into sw.js so clients detect new builds without manual version bumps."""
    import hashlib
    from pathlib import Path as _Path

    h = hashlib.sha256()
    static_root = _Path(__file__).resolve().parent.parent / "static"
    try:
        for p in sorted(static_root.rglob("*")):
            if p.is_file():
                st = p.stat()
                h.update(f"{p.relative_to(static_root)}:{st.st_size}:{int(st.st_mtime)}".encode())
    except OSError:
        pass
    return h.hexdigest()[:12]


BUILD_ID = _compute_build_id()


def _compute_code_hash() -> str:
    """Content hash of the shipped code — the deploy-stamp fallback when the
    image carries no .git. Contents only (no sizes/mtimes), sorted paths, so
    the value is deterministic for a given code state and changes exactly
    when the code does."""
    import hashlib
    from pathlib import Path as _Path

    root = _Path(__file__).resolve().parent.parent
    h = hashlib.sha256()
    try:
        trees = ["api", "core", "sessions", "db", "static"]
        files: list[_Path] = [root / "config.py", root / "run.py", root / "maintenance.py"]
        for tree in trees:
            files.extend((root / tree).rglob("*.py"))
        files.extend((root / "static").rglob("*.js"))
        files.extend((root / "static").rglob("*.css"))
        files.extend((root / "static").rglob("*.html"))
        for p in sorted(f for f in files if f.is_file() and "__pycache__" not in f.parts):
            h.update(str(p.relative_to(root)).encode())
            h.update(p.read_bytes())
    except OSError:
        return ""
    return h.hexdigest()[:12]


@app.get("/sw.js")
async def service_worker():
    import os

    from fastapi.responses import Response

    sw_path = os.path.join(os.path.dirname(__file__), "..", "static", "sw.js")
    with open(sw_path, encoding="utf-8") as f:
        body = f.read().replace("__BUILD__", BUILD_ID)
    # no-cache: the SW script itself must never be HTTP-stale, or clients
    # keep running the previous build's precache for up to 24h.
    return Response(
        body,
        media_type="application/javascript",
        headers={"Service-Worker-Allowed": "/", "Cache-Control": "no-cache"},
    )


# Mount static files
from pathlib import Path

from fastapi.staticfiles import StaticFiles


class _RevalidatingStatic(StaticFiles):
    """Static assets with `Cache-Control: no-cache`.

    Not "don't cache" — "revalidate before reuse". Without it the browser
    picks a heuristic freshness lifetime from Last-Modified, so after a
    deploy the service worker's precache could be satisfied from the HTTP
    cache and stamp an old module beside a new one in the same versioned
    cache. Revalidation is one conditional request that answers 304 on a
    LAN, and it is what makes the SW's cache-busting reliable.
    """

    def file_response(self, *args, **kwargs):
        resp = super().file_response(*args, **kwargs)
        resp.headers.setdefault("Cache-Control", "no-cache")
        return resp


if Path("static").exists():
    app.mount("/static", _RevalidatingStatic(directory="static"), name="static")
