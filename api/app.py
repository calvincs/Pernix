"""Pernix — FastAPI application with lifecycle management."""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from config import settings
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

    # 2.7 Workflow registry — scan data/workflows/ for WORKFLOW.md packages
    try:
        from core.workflows.registry import get_workflow_registry

        workflows_dir = Path("data/workflows")
        workflows_dir.mkdir(parents=True, exist_ok=True)
        wf_reg = get_workflow_registry()
        wf_count = wf_reg.scan(workflows_dir)
        logger.info("Workflows loaded: %d workflows scanned from %s", wf_count, workflows_dir)
    except Exception as e:
        logger.warning("Workflow registry scan failed (continuing without workflows): %s", e)

    # 2.75 Orphan workflow_runs sweep. run_workflow() is in-process and has no
    # resume path; any row stuck at status='running' across a restart is dead.
    # Marking these failed prevents misleading "still running" responses to
    # list_workflow_runs and keeps the dashboard honest.
    try:
        from db import models as _db_models

        orphans = _db_models.fail_orphaned_workflow_runs()
        if orphans:
            logger.warning("Marked %d orphaned workflow_runs as failed at startup", orphans)
    except Exception as e:
        logger.warning("Workflow orphan sweep failed: %s", e)

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
        from core.extensions.scheduling import init_scheduler

        init_scheduler()
    except Exception as e:
        logger.warning("Scheduler init failed: %s", e)

    # 5. Maintenance heartbeat
    from maintenance import get_maintenance

    maint = get_maintenance()
    maint.start()

    # 5b. Dream journal listener — narrates snooze cycles into the journal
    # session. Idles (no writes) unless dream_enabled; cancelled at shutdown.
    # NB: `import asyncio` here is required — a later local import in this
    # function makes the name function-local for the whole scope.
    import asyncio

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
    import asyncio

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


app = FastAPI(title="Pernix", lifespan=lifespan)

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
    chat,
    context,
    health,
    jobs,
    memory,
    models,
    push,
    questions,
    rlm,
    sessions,
    skills,
    tools,
    voice,
    workflows,
    workspace,
)

app.include_router(health.router)
app.include_router(sessions.router)
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
app.include_router(workflows.router)
app.include_router(rlm.router)
app.include_router(voice.router)


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


@app.get("/sw.js")
async def service_worker():
    import os

    from fastapi.responses import FileResponse

    sw_path = os.path.join(os.path.dirname(__file__), "..", "static", "sw.js")
    return FileResponse(sw_path, media_type="application/javascript", headers={"Service-Worker-Allowed": "/"})


# Mount static files
from pathlib import Path

from fastapi.staticfiles import StaticFiles

if Path("static").exists():
    app.mount("/static", StaticFiles(directory="static"), name="static")
