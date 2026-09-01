"""Pernix — MCP connection manager: one supervised connection per server.

Runs on the main FastAPI event loop (the SDK is asyncio-native); sync tool
wrappers on executor threads marshal in via run_coroutine_threadsafe — the
same bridge every other extension uses. Each connection is a supervisor task
that owns the SDK's async context stack (transport → ClientSession), because
anyio scopes must be entered and exited by the same task; other tasks only
await requests against the held session, which multiplexes concurrently.

Lifecycle per connection:
  connecting → ready → (degraded ⇄ connecting on failure, with backoff)
                     → idle (stdio suspended after mcp_idle_seconds; the
                             next call respawns it)
                     → stopped/disabled
Tools stay registered while a server is degraded or idle — a call returns a
clear error or transparently resumes the server. Removal/toggle-off is the
only path that unregisters.
"""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import AsyncExitStack
from pathlib import Path

from config import APP_VERSION, settings
from core.extensions.mcp.config import (
    MCPServerConfig,
    expand_mapping,
    expand_placeholders,
    load_server_configs,
    pernix_tool_name,
    save_server_configs,
)

logger = logging.getLogger("pernix.ext.mcp")

# stdio children's stderr lands here, one file per server (patchable in tests).
LOG_DIR = Path("data/logs")

_BACKOFF_INITIAL = 5.0
_BACKOFF_MAX = 300.0
# A server that was never ready alerts only after this many failed connect
# cycles (a wrong URL shouldn't page on the first boot-time blip); a server
# that WAS ready alerts on the first drop.
_NEVER_READY_ALERT_CYCLES = 3
# Consecutive protocol-level call failures before the breaker forces a
# reconnect cycle (transport-level failures reconnect immediately).
_CALL_FAILURE_BREAKER = 3
_MAX_TOOLS_HARD = 500  # pagination sanity bound


class MCPUnavailable(RuntimeError):
    """The server can't take this call right now (disabled, degraded, closed)."""


def _notify(title: str, body: str, urgency: str = "normal") -> None:
    """One-shot operator notification (web extension precedent). Best-effort."""
    try:
        from db import models as _db

        _db.add_notification(title=title, body=body, urgency=urgency)
    except Exception as e:
        logger.debug("MCP alert (%s) could not be persisted: %s", title, e)


class MCPConnection:
    """Supervised connection to one MCP server."""

    def __init__(self, cfg: MCPServerConfig, manager: "MCPManager"):
        self.cfg = cfg
        self._manager = manager
        self.status = "disabled" if not cfg.enabled else "connecting"
        self.error = ""
        self.server_info = ""
        self.registered: dict[str, str] = {}  # pernix tool name -> remote tool name
        self.connected_at = 0.0
        self.last_used = 0.0
        self.last_refresh = 0.0
        self.consecutive_call_failures = 0
        self._session = None
        self._task: asyncio.Task | None = None
        self._closing = False
        self._suspend_requested = False
        self._inflight = 0
        self._was_ready = False
        self._fail_cycles = 0
        self._alerted = False
        self._refresh_task: asyncio.Task | None = None
        # Wake events use replace-after-wake semantics: setters never clear;
        # the supervisor swaps in a fresh Event right after it wakes, so a
        # set() landing while the supervisor isn't waiting stays pending and
        # short-circuits the next wait instead of being lost.
        self._resume_evt = asyncio.Event()  # wakes idle/backoff waits
        self._drop_evt = asyncio.Event()  # drops the held connection
        self._ready_waiters: list[asyncio.Future] = []

    # --- public surface (call on the event loop) ---

    @property
    def call_timeout(self) -> int:
        return self.cfg.timeout or max(5, settings.mcp_call_timeout)

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._closing = False
            self._task = asyncio.get_running_loop().create_task(self._supervise(), name=f"mcp-{self.cfg.name}")

    async def close(self) -> None:
        self._closing = True
        self._resume_evt.set()
        self._drop_evt.set()
        self._resolve_waiters(MCPUnavailable(f"MCP server '{self.cfg.name}' is shutting down"))
        if self._refresh_task is not None:
            self._refresh_task.cancel()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=6)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._task.cancel()
            except Exception:
                pass
        self.status = "stopped"
        self._session = None

    async def ensure_ready(self):
        """Return the live ClientSession, waking an idle/degraded server."""
        if not settings.mcp_enabled:
            raise MCPUnavailable("MCP is disabled (Settings → MCP Servers)")
        if self._closing or self.status in ("stopped", "disabled"):
            raise MCPUnavailable(f"MCP server '{self.cfg.name}' is {self.status}")
        if self.status == "ready" and self._session is not None:
            return self._session
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._ready_waiters.append(fut)
        self._resume_evt.set()
        try:
            await asyncio.wait_for(fut, timeout=settings.mcp_connect_timeout + 10)
        except asyncio.TimeoutError:
            raise MCPUnavailable(
                f"MCP server '{self.cfg.name}' did not become ready in time "
                f"(status {self.status}{': ' + self.error if self.error else ''})"
            ) from None
        if self._session is None:
            raise MCPUnavailable(f"MCP server '{self.cfg.name}' is {self.status}: {self.error}")
        return self._session

    async def call_tool(self, remote_name: str, arguments: dict | None):
        """One tool call. Transport failures trip an immediate reconnect;
        repeated protocol failures trip the breaker."""
        from mcp.shared.exceptions import MCPError

        session = await self.ensure_ready()
        self.last_used = time.time()
        self._inflight += 1
        try:
            result = await session.call_tool(
                remote_name,
                arguments or {},
                read_timeout_seconds=float(self.call_timeout),
                allow_input_required=True,
            )
        except asyncio.CancelledError:
            raise
        except MCPError:
            self._note_call_failure(transport=False)
            raise
        except Exception as e:
            # Anything below the protocol (closed stream, broken pipe, HTTP
            # error) — the held connection is suspect, drop and reconnect.
            self._note_call_failure(transport=True)
            raise MCPUnavailable(
                f"MCP server '{self.cfg.name}' connection failed mid-call ({e}); reconnecting in the background"
            ) from e
        finally:
            self._inflight -= 1
            self.last_used = time.time()
        self.consecutive_call_failures = 0
        return result

    def request_reconnect(self) -> None:
        self._drop_evt.set()
        self._resume_evt.set()

    def suspend(self) -> None:
        """Reap an idle stdio child; tools stay registered, next call resumes."""
        if self.status == "ready" and self.cfg.transport == "stdio" and self._inflight == 0 and not self._closing:
            self._suspend_requested = True
            self._drop_evt.set()

    def schedule_refresh(self) -> None:
        """Debounced tools/list re-check (listChanged notification, periodic sweep)."""
        if self._refresh_task is not None and not self._refresh_task.done():
            return
        self._refresh_task = asyncio.get_running_loop().create_task(self._refresh_tools())

    def snapshot(self) -> dict:
        return {
            "name": self.cfg.name,
            "transport": self.cfg.transport,
            "target": self.cfg.command if self.cfg.transport == "stdio" else self.cfg.url,
            "enabled": self.cfg.enabled,
            "status": self.status,
            "error": self.error,
            "server_info": self.server_info,
            "tool_count": len(self.registered),
            "tools": sorted(self.registered),
            "safety": self.cfg.safety or settings.mcp_default_safety,
            "connected_at": self.connected_at,
            "last_used": self.last_used,
        }

    # --- internals ---

    def _resolve_waiters(self, exc: Exception | None = None) -> None:
        for fut in self._ready_waiters:
            if not fut.done():
                if exc is None:
                    fut.set_result(None)
                else:
                    fut.set_exception(exc)
        self._ready_waiters.clear()

    def _note_call_failure(self, *, transport: bool) -> None:
        self.consecutive_call_failures += 1
        if transport or self.consecutive_call_failures >= _CALL_FAILURE_BREAKER:
            self.request_reconnect()

    async def _wait_event(self, evt_name: str, timeout: float | None) -> None:
        evt: asyncio.Event = getattr(self, evt_name)
        try:
            await asyncio.wait_for(evt.wait(), timeout)
        except asyncio.TimeoutError:
            pass
        setattr(self, evt_name, asyncio.Event())

    async def _supervise(self) -> None:
        backoff = _BACKOFF_INITIAL
        while not self._closing:
            self.status = "connecting"
            clean_drop = False
            try:
                async with AsyncExitStack() as stack:
                    read, write = await self._enter_transport(stack)
                    from mcp import ClientSession
                    from mcp import types as mcp_types

                    session = await stack.enter_async_context(
                        ClientSession(
                            read,
                            write,
                            message_handler=self._on_message,
                            client_info=mcp_types.Implementation(name="pernix", version=APP_VERSION),
                        )
                    )
                    init = await asyncio.wait_for(session.initialize(), settings.mcp_connect_timeout)
                    srv = getattr(init, "server_info", None)
                    if srv is not None:
                        self.server_info = f"{getattr(srv, 'name', '?')} {getattr(srv, 'version', '')}".strip()
                    tools = await asyncio.wait_for(self._list_all_tools(session), settings.mcp_connect_timeout)
                    self._manager.register_server_tools(self, tools)
                    self._session = session
                    self.connected_at = time.time()
                    self.last_refresh = time.time()
                    self.error = ""
                    self._fail_cycles = 0
                    self._was_ready = True
                    self._alerted = False
                    backoff = _BACKOFF_INITIAL
                    self.status = "ready"
                    # Consume any pending wake-up: a kick from ensure_ready
                    # during the connect is satisfied by being ready — left
                    # set, it would fall straight through the next idle wait
                    # and undo the first suspend.
                    self._resume_evt = asyncio.Event()
                    self._resolve_waiters()
                    logger.info(
                        "MCP server '%s' ready (%s): %d tools",
                        self.cfg.name,
                        self.server_info or self.cfg.transport,
                        len(self.registered),
                    )
                    # Hold the stack open, serving calls from other tasks,
                    # until a drop is requested (breaker, suspend, close).
                    await self._wait_event("_drop_evt", None)
                    clean_drop = True
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self._record_incident(e)
            finally:
                self._session = None
            if self._closing:
                break
            if self._suspend_requested:
                self._suspend_requested = False
                self.status = "idle"
                logger.info("MCP server '%s' suspended (idle)", self.cfg.name)
                await self._wait_event("_resume_evt", None)
                continue
            if clean_drop:
                # Dropped on purpose (breaker/reload) — retry immediately once;
                # a failure on that attempt lands in the backoff branch below.
                continue
            self.status = "degraded"
            await self._wait_event("_resume_evt", backoff)
            backoff = min(backoff * 2, _BACKOFF_MAX)
        self.status = "stopped"

    async def _enter_transport(self, stack: AsyncExitStack):
        cfg = self.cfg
        if cfg.transport == "stdio":
            if not settings.mcp_stdio_enabled:
                raise MCPUnavailable("stdio MCP servers are disabled (Settings → MCP Servers → Allow stdio servers)")
            from mcp import StdioServerParameters
            from mcp.client.stdio import stdio_client

            # Child stderr goes to a per-server log, not the Pernix console.
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            errlog = open(LOG_DIR / f"mcp_{cfg.name}.stderr.log", "a", encoding="utf-8", errors="replace")
            stack.callback(errlog.close)
            params = StdioServerParameters(
                command=cfg.command,
                args=[expand_placeholders(a, server=cfg.name) for a in cfg.args],
                env=expand_mapping(cfg.env, server=cfg.name) or None,
                cwd=cfg.cwd or None,
            )
            return await stack.enter_async_context(stdio_client(params, errlog=errlog))

        url = expand_placeholders(cfg.url, server=cfg.name)
        headers = expand_mapping(cfg.headers, server=cfg.name) or None
        if cfg.transport == "http":
            from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

            client = await stack.enter_async_context(create_mcp_http_client(headers=headers))
            return await stack.enter_async_context(streamable_http_client(url, http_client=client))
        # Legacy HTTP+SSE (deprecated in-spec; kept because the SDK ships it).
        from mcp.client.sse import sse_client

        return await stack.enter_async_context(sse_client(url, headers=headers))

    async def _list_all_tools(self, session) -> list:
        from mcp import types as mcp_types

        tools: list = []
        cursor: str | None = None
        while True:
            params = mcp_types.PaginatedRequestParams(cursor=cursor) if cursor else None
            result = await session.list_tools(params=params)
            tools.extend(result.tools)
            cursor = getattr(result, "next_cursor", None)
            if not cursor or len(tools) >= _MAX_TOOLS_HARD:
                break
        return tools

    async def _refresh_tools(self) -> None:
        try:
            session = self._session
            if session is None or self.status != "ready":
                return
            tools = await asyncio.wait_for(self._list_all_tools(session), settings.mcp_connect_timeout)
            self._manager.register_server_tools(self, tools)
            self.last_refresh = time.time()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning("MCP server '%s' tool refresh failed: %s", self.cfg.name, e)

    async def _on_message(self, message) -> None:
        """SDK message handler: watch for tools/list_changed, ignore the rest."""
        try:
            from mcp import types as mcp_types

            root = getattr(message, "root", message)
            if isinstance(root, mcp_types.ToolListChangedNotification):
                logger.info("MCP server '%s' announced a tool list change", self.cfg.name)
                self.schedule_refresh()
        except Exception as e:
            logger.debug("MCP message handler error (%s): %s", self.cfg.name, e)

    def _record_incident(self, e: Exception) -> None:
        self.error = str(e) or type(e).__name__
        self._fail_cycles += 1
        self.status = "degraded"
        self._resolve_waiters(MCPUnavailable(f"MCP server '{self.cfg.name}' failed to connect: {self.error}"))
        logger.warning("MCP server '%s' connection failed (cycle %d): %s", self.cfg.name, self._fail_cycles, self.error)
        if not self._alerted and (self._was_ready or self._fail_cycles >= _NEVER_READY_ALERT_CYCLES):
            self._alerted = True
            _notify(
                f"MCP server '{self.cfg.name}' unreachable",
                f"{self.error}\n\nIt will keep retrying with backoff. Fix the server or its config "
                f"(Explorer → MCP), or ask the agent to run mcp_reload_server('{self.cfg.name}').",
            )


class MCPManager:
    """Owns every configured connection. Lives on the main event loop."""

    def __init__(self):
        self.connections: dict[str, MCPConnection] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        self.started = False

    # --- lifecycle ---

    async def start(self) -> None:
        """Load configs and spawn supervisors. Never blocks on any server."""
        self._loop = asyncio.get_running_loop()
        self.started = True
        configs = load_server_configs()
        if len(configs) > settings.mcp_max_servers:
            keep = dict(sorted(configs.items())[: settings.mcp_max_servers])
            skipped = sorted(set(configs) - set(keep))
            logger.warning(
                "mcp_servers.json has %d servers; cap is %d — skipping: %s",
                len(configs),
                settings.mcp_max_servers,
                ", ".join(skipped),
            )
            configs = keep
        for cfg in configs.values():
            self._spawn(cfg)
        if configs:
            logger.info("MCP manager started: %d server(s) configured", len(configs))

    async def shutdown(self) -> None:
        """Close every connection (kills stdio children). Tools stay registered;
        their calls fail with a clear error until the next start()."""
        self.started = False
        conns = list(self.connections.values())
        if conns:
            await asyncio.gather(*(c.close() for c in conns), return_exceptions=True)
        logger.info("MCP manager stopped")

    def _spawn(self, cfg: MCPServerConfig) -> MCPConnection:
        conn = MCPConnection(cfg, self)
        self.connections[cfg.name] = conn
        if cfg.enabled:
            conn.start()
        return conn

    # --- server CRUD (call on the event loop) ---

    async def add_server(self, cfg: MCPServerConfig) -> MCPConnection:
        """Add or replace a server: persist config, (re)spawn, wait for ready.

        The config is saved even when the first connect fails — the entry is
        visible/editable in the MCP tab either way — but the raised error
        tells the caller exactly what went wrong.
        """
        existing = self.connections.get(cfg.name)
        if existing is None and len(self.connections) >= settings.mcp_max_servers:
            raise ValueError(f"Server cap reached ({settings.mcp_max_servers}); remove one or raise mcp_max_servers")
        if existing is not None:
            await existing.close()
        self._persist()  # snapshot current set first so a crash keeps the file coherent
        conn = self._spawn(cfg)
        self._persist()
        if cfg.enabled:
            await conn.ensure_ready()
        return conn

    async def remove_server(self, name: str) -> bool:
        conn = self.connections.pop(name, None)
        if conn is None:
            return False
        await conn.close()
        self._unregister_tools(conn)
        self._persist()
        return True

    async def toggle_server(self, name: str, enabled: bool) -> MCPConnection:
        conn = self.connections.get(name)
        if conn is None:
            raise KeyError(f"No MCP server named '{name}'")
        conn.cfg.enabled = enabled
        self._persist()
        if enabled:
            conn.status = "connecting"
            conn.start()
        else:
            await conn.close()
            self._unregister_tools(conn)
            conn.status = "disabled"
        return conn

    async def reload_server(self, name: str) -> MCPConnection:
        """Full reconnect: re-reads this server's entry from disk (picking up
        hand edits), drops the connection, and waits for ready."""
        conn = self.connections.get(name)
        if conn is None:
            raise KeyError(f"No MCP server named '{name}'")
        disk = load_server_configs().get(name)
        if disk is not None:
            conn.cfg = disk
        if not conn.cfg.enabled:
            raise ValueError(f"MCP server '{name}' is disabled — enable it first")
        await conn.close()
        self._unregister_tools(conn)
        fresh = self._spawn(conn.cfg)
        await fresh.ensure_ready()
        return fresh

    def _persist(self) -> None:
        save_server_configs({name: c.cfg for name, c in self.connections.items()})

    # --- maintenance hooks (sync, called from the tick on the loop) ---

    def reap_idle(self) -> None:
        if settings.mcp_idle_seconds <= 0:
            return
        now = time.time()
        for conn in self.connections.values():
            anchor = max(conn.last_used, conn.connected_at)
            if conn.status == "ready" and anchor and now - anchor > settings.mcp_idle_seconds:
                conn.suspend()

    def refresh_due(self) -> None:
        if settings.mcp_refresh_interval_s <= 0:
            return
        now = time.time()
        for conn in self.connections.values():
            if conn.status == "ready" and now - conn.last_refresh > settings.mcp_refresh_interval_s:
                conn.schedule_refresh()

    def status_snapshot(self) -> list[dict]:
        return [c.snapshot() for _, c in sorted(self.connections.items())]

    # --- tool registration ---

    def register_server_tools(self, conn: MCPConnection, mcp_tools: list) -> None:
        """Register/refresh a server's tools in the ToolRegistry (diff-aware)."""
        from core.extensions.mcp.bridge import build_description, make_tool_fn
        from core.tools.registry import get_registry

        registry = get_registry()
        cfg = conn.cfg
        if cfg.tool_allowlist is not None:
            allowed = set(cfg.tool_allowlist)
            mcp_tools = [t for t in mcp_tools if t.name in allowed]
        mcp_tools = sorted(mcp_tools, key=lambda t: t.name)
        cap = max(1, settings.mcp_max_tools_per_server)
        if len(mcp_tools) > cap:
            logger.warning(
                "MCP server '%s' exposes %d tools; cap is %d — skipping %d (raise "
                "mcp_max_tools_per_server or set a tool_allowlist)",
                cfg.name,
                len(mcp_tools),
                cap,
                len(mcp_tools) - cap,
            )
            mcp_tools = mcp_tools[:cap]

        taken = {t.name for t in registry.all_tools()} - set(conn.registered)
        new_map: dict[str, str] = {}
        for tool in mcp_tools:
            pname = pernix_tool_name(cfg.name, tool.name, taken)
            taken.add(pname)
            new_map[pname] = tool.name
            annotations = getattr(tool, "annotations", None)
            registry.register(
                name=pname,
                func=make_tool_fn(self, cfg.name, tool.name),
                description=build_description(cfg.name, tool),
                parameters=tool.input_schema or {"type": "object", "properties": {}},
                category=f"mcp:{cfg.name}",
                tags=[cfg.name, "mcp"],
                # Registered ceiling sits above the bridge's own timeout so
                # the executor's wait_for is the last resort, not a race.
                timeout=conn.call_timeout + 15,
                parallel_safe=True,
                denied_session_types={"canary"},
                source="mcp",
                safety_level=self._resolve_safety(cfg, annotations),
                # idempotent_hint=False opts out of cross-round dedup; the
                # default (True) keeps dedup, which also guards against
                # accidental double-posts to external services.
                idempotent=getattr(annotations, "idempotent_hint", None) is not False,
            )
        removed = set(conn.registered) - set(new_map)
        for name in removed:
            registry.unregister(name)
        conn.registered = new_map
        registry.rebuild_index()
        if removed:
            logger.info("MCP server '%s': %d tool(s) removed on refresh: %s", cfg.name, len(removed), sorted(removed))

    @staticmethod
    def _resolve_safety(cfg: MCPServerConfig, annotations) -> str:
        """Config/default pick the level; server annotations may only tighten
        (they're server-controlled, i.e. untrusted). The user's per-tool
        override in tools.json is applied on top by registry.register()."""
        level = cfg.safety or settings.mcp_default_safety
        if level not in ("safe", "caution", "dangerous"):
            level = "caution"
        if annotations is not None and getattr(annotations, "destructive_hint", None) is True:
            level = "dangerous"
        return level

    def _unregister_tools(self, conn: MCPConnection) -> None:
        if not conn.registered:
            return
        from core.tools.registry import get_registry

        registry = get_registry()
        for name in conn.registered:
            registry.unregister(name)
        conn.registered = {}
        registry.rebuild_index()


async def probe_server(cfg: MCPServerConfig) -> dict:
    """Dry-run connect: open transport, initialize, list tools, tear down.

    Registers nothing and touches no persisted state — the REST test
    endpoint and UI "Test" button use it before a config is saved.
    """
    conn = MCPConnection(cfg, manager=None)  # type: ignore[arg-type]  # only _enter_transport is used
    async with AsyncExitStack() as stack:
        read, write = await conn._enter_transport(stack)
        from mcp import ClientSession
        from mcp import types as mcp_types

        session = await stack.enter_async_context(
            ClientSession(
                read,
                write,
                client_info=mcp_types.Implementation(name="pernix", version=APP_VERSION),
            )
        )
        init = await asyncio.wait_for(session.initialize(), settings.mcp_connect_timeout)
        tools = await asyncio.wait_for(conn._list_all_tools(session), settings.mcp_connect_timeout)
        srv = getattr(init, "server_info", None)
        return {
            "ok": True,
            "server_info": (f"{getattr(srv, 'name', '?')} {getattr(srv, 'version', '')}".strip() if srv else ""),
            "tools": sorted(t.name for t in tools),
        }


# ---------------------------------------------------------------------------
# Module singleton
# ---------------------------------------------------------------------------

_manager: MCPManager | None = None


def get_mcp_manager() -> MCPManager:
    global _manager
    if _manager is None:
        _manager = MCPManager()
    return _manager


def get_mcp_manager_if_started() -> MCPManager | None:
    return _manager if _manager is not None and _manager.started else None
