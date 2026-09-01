"""Pernix — Tool registry with discovery index and synonym search."""

from __future__ import annotations

import inspect
import json
import logging
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

logger = logging.getLogger("pernix.tools.registry")

TOOLS_CONFIG_PATH = Path("data/tools.json")

# ---------------------------------------------------------------------------
# Synonym map for natural language tool discovery
# ---------------------------------------------------------------------------

SYNONYMS: dict[str, list[str]] = {
    "search": ["web", "find", "lookup", "query", "research", "internet", "google"],
    "write": ["create", "save", "output", "generate", "make"],
    "edit": ["modify", "replace", "change", "update", "patch", "fix", "refactor"],
    "read": ["open", "load", "view", "inspect", "check", "show", "get"],
    "glob": ["find", "locate", "pattern", "match", "discover", "list"],
    "grep": ["regex", "codebase", "usage", "reference", "import", "definition", "pattern"],
    "run": ["execute", "shell", "command", "terminal", "bash", "process"],
    "git": ["version", "commit", "diff", "vcs", "source", "repo", "branch"],
    "parallel": ["worker", "delegate", "orchestrate", "concurrent", "spawn", "multi"],
    "schedule": ["cron", "recurring", "automated", "periodic", "timer", "job"],
    "evaluate": ["test", "verify", "validate", "check", "qa", "assess", "grade"],
    "browse": ["web", "browser", "playwright", "html", "render", "navigate", "console", "debug"],
    "remember": ["save", "store", "note", "record", "memory", "persist"],
    "recall": ["retrieve", "fetch", "search", "find", "memory", "knowledge"],
    "artifact": ["file", "document", "output", "deliverable", "result", "product"],
    "model": [
        "llm",
        "switch",
        "provider",
        "ai",
        "openai",
        "anthropic",
        "claude",
        "gpt",
        "gemini",
        "ollama",
        "chat",
        "completion",
    ],
    "tool": ["capability", "function", "action", "utility"],
    "mcp": [
        "server",
        "integration",
        "connector",
        "external",
        "protocol",
        "plugin",
        "service",
    ],
    "rlm": [
        "recursive",
        "long context",
        "huge",
        "large",
        "corpus",
        "transcript",
        "summarize",
        "analyze",
        "document",
    ],
}

# Tool co-occurrence: discovering one should surface related tools
TOOL_COOCCURRENCE: dict[str, list[str]] = {
    "spawn_worker": [
        "check_workers",
        "await_workers",
        "get_worker_result",
        "get_worker_transcript",
        "message_worker",
        "cancel_worker",
    ],
    "check_workers": ["spawn_worker", "await_workers", "get_worker_result", "get_worker_transcript", "message_worker"],
    "await_workers": ["spawn_worker", "check_workers", "get_worker_result", "get_worker_transcript"],
    "get_worker_result": ["spawn_worker", "check_workers", "await_workers", "get_worker_transcript"],
    "get_worker_transcript": ["spawn_worker", "check_workers", "get_worker_result"],
    "message_worker": ["spawn_worker", "check_workers", "get_worker_result"],
    # One-directional on purpose: recommending rlm_process pulls in the file
    # tools needed to stage sources, but everyday file_read use must not drag
    # the (enabled-only) rlm_process schema into every session's active set.
    "rlm_process": ["file_read", "file_write", "glob"],
    "add_feature": ["list_features", "mark_feature_passed"],
    "evaluate": ["list_features", "add_feature", "browse_web"],
    "create_tool": ["update_tool", "list_custom_tools"],
    "schedule_job": ["list_scheduled_jobs", "remove_scheduled_job", "set_job_state", "update_scheduled_job"],
    "set_job_state": ["list_scheduled_jobs", "schedule_job"],
    "update_scheduled_job": ["list_scheduled_jobs", "schedule_job"],
    "file_edit": ["multiedit", "file_read", "file_write"],
    "multiedit": ["file_edit", "file_read"],
    "glob": ["grep", "file_read"],
    "grep": ["glob", "file_read"],
    "remember": ["recall"],
    "recall": ["remember"],
    "search_web": ["http_get", "browse_web"],
    "http_get": ["search_web", "browse_web"],
    "browse_web": ["search_web", "http_get"],
    "mcp_list_servers": ["mcp_add_server", "mcp_reload_server", "mcp_remove_server"],
    "mcp_add_server": ["mcp_list_servers", "mcp_reload_server"],
    "mcp_reload_server": ["mcp_list_servers"],
}


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class ToolDef:
    """Definition of a registered tool."""

    name: str
    description: str
    parameters: dict  # JSON Schema
    function: Callable
    category: str = "core"
    tags: list[str] = field(default_factory=list)
    timeout: int = 300
    # Ceiling for a caller-supplied `timeout` argument. 0 = no override
    # allowed (the dispatch timeout is always `timeout`). Set this when a
    # tool's own schema exposes a `timeout` parameter, otherwise the
    # executor's wait_for fires at `timeout` and the documented override
    # silently does nothing — see core/tools/executor.py:_resolve_timeout.
    max_timeout: int = 0
    parallel_safe: bool = False
    # Session types this tool refuses to run in (plan §5, generalizing the old
    # worker_allowed bool: worker_allowed=False ≙ {"worker"}). Empty = allowed
    # everywhere. Memory-write tools add "canary" so synthetic runs can read
    # memory but never mutate it.
    denied_session_types: set[str] = field(default_factory=set)
    source: str = "builtin"  # builtin | extension | custom
    safety_level: str = "safe"  # safe | caution | dangerous
    # Long-poll tools (await_workers, rlm_process) block their thread for up
    # to 30-60 minutes waiting on OTHER work. They run on a dedicated executor
    # so they can never starve the shared to_thread pool that the very workers
    # they're waiting on need for their own tool calls.
    long_poll: bool = False
    # False for tools where an identical repeated call MUST re-execute
    # (repl: `next(pages)` twice must run twice — the mutated state is the
    # kernel namespace, invisible to the file-based dedup invalidators).
    # Checked by the cross-round dedup in core/agent.py; True keeps today's
    # stub-the-duplicate behavior. Adaptation plan 2c.
    idempotent: bool = True

    def to_openai_schema(self) -> dict:
        """Convert to OpenAI function-calling tool format."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


@dataclass
class ToolSummary:
    """Lightweight tool info returned by discover_tools."""

    name: str
    description: str
    category: str
    tags: list[str]

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "category": self.category,
            "tags": self.tags,
        }


@dataclass
class ToolHealthMetrics:
    """Per-tool operational metrics.

    Thread-safe: all mutating methods hold a threading.Lock so concurrent
    asyncio.to_thread tool calls don't lose count updates.
    """

    total_calls: int = 0
    success_count: int = 0
    failure_count: int = 0
    timeout_count: int = 0
    total_latency_ms: int = 0
    last_error: str = ""
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def record_success(self, latency_ms: int) -> None:
        with self._lock:
            self.total_calls += 1
            self.success_count += 1
            self.total_latency_ms += latency_ms

    def record_failure(self, error: str, latency_ms: int = 0) -> None:
        with self._lock:
            self.total_calls += 1
            self.failure_count += 1
            self.total_latency_ms += latency_ms
            self.last_error = error

    def record_timeout(self, latency_ms: int = 0) -> None:
        with self._lock:
            self.total_calls += 1
            self.timeout_count += 1
            self.total_latency_ms += latency_ms
            self.last_error = "timeout"

    @property
    def success_rate(self) -> float:
        return self.success_count / max(self.total_calls, 1)

    @property
    def avg_latency_ms(self) -> float:
        return self.total_latency_ms / max(self.total_calls, 1)


# ---------------------------------------------------------------------------
# Tool Index (discovery engine)
# ---------------------------------------------------------------------------


def _tokenize(text: str) -> set[str]:
    """Split text into searchable tokens."""
    return {w.lower() for w in re.findall(r"[a-zA-Z]+", text) if len(w) > 2}


def _expand_synonyms(tokens: set[str]) -> set[str]:
    """Expand tokens with synonym matches."""
    expanded = set(tokens)
    for token in tokens:
        for key, syns in SYNONYMS.items():
            if token == key or token in syns:
                expanded.add(key)
                expanded.update(syns)
    return expanded


@dataclass
class _IndexEntry:
    name: str
    description: str
    category: str
    tags: list[str]
    name_tokens: set[str] = field(default_factory=set)
    desc_tokens: set[str] = field(default_factory=set)
    tag_tokens: set[str] = field(default_factory=set)
    param_tokens: set[str] = field(default_factory=set)

    def to_summary(self) -> ToolSummary:
        return ToolSummary(
            name=self.name,
            description=self.description,
            category=self.category,
            tags=self.tags,
        )


class ToolIndex:
    """Search index for tool discovery."""

    def __init__(self):
        self._entries: dict[str, _IndexEntry] = {}

    def rebuild(self, tools: dict[str, ToolDef]) -> None:
        """Rebuild index from registry."""
        self._entries.clear()
        for name, tool in tools.items():
            param_names = set(tool.parameters.get("properties", {}).keys())
            entry = _IndexEntry(
                name=name,
                description=tool.description,
                category=tool.category,
                tags=list(tool.tags),
                name_tokens=_tokenize(name.replace("_", " ")),
                desc_tokens=_tokenize(tool.description),
                tag_tokens={t.lower() for t in tool.tags},
                param_tokens={p.lower() for p in param_names},
            )
            self._entries[name] = entry

    def search(self, query: str, category: str | None = None, limit: int = 10) -> list[ToolSummary]:
        """Search tools by natural language query."""
        query_tokens = _tokenize(query)
        expanded = _expand_synonyms(query_tokens)

        scored: list[tuple[_IndexEntry, float]] = []
        for entry in self._entries.values():
            if category and entry.category != category:
                continue

            score = 0.0
            # Name match (strongest signal)
            score += len(expanded & entry.name_tokens) * 3.0
            # Tag match (strong signal)
            score += len(expanded & entry.tag_tokens) * 2.0
            # Description word overlap
            score += len(expanded & entry.desc_tokens) * 1.0
            # Parameter name match (weak signal)
            score += len(expanded & entry.param_tokens) * 0.5

            if score > 0:
                scored.append((entry, score))

        scored.sort(key=lambda x: -x[1])

        # Apply co-occurrence: include related tools
        results: list[ToolSummary] = []
        seen: set[str] = set()
        for entry, _score in scored[:limit]:
            if entry.name not in seen:
                results.append(entry.to_summary())
                seen.add(entry.name)
                # Add co-occurring tools
                for co_name in TOOL_COOCCURRENCE.get(entry.name, []):
                    if co_name not in seen and co_name in self._entries:
                        results.append(self._entries[co_name].to_summary())
                        seen.add(co_name)

        return results[:limit]


# ---------------------------------------------------------------------------
# Tool Registry
# ---------------------------------------------------------------------------


class ToolRegistry:
    """Central registry for all tools (built-in, extension, custom)."""

    def __init__(self):
        self._tools: dict[str, ToolDef] = {}
        self._disabled: set[str] = set()
        self._safety_overrides: dict[str, str] = {}
        self.metrics: dict[str, ToolHealthMetrics] = {}
        self.index = ToolIndex()

    def register(
        self,
        name: str,
        func: Callable,
        description: str,
        parameters: dict,
        category: str = "core",
        tags: list[str] | None = None,
        timeout: int = 300,
        max_timeout: int = 0,
        parallel_safe: bool = False,
        denied_session_types: set[str] | None = None,
        source: str = "builtin",
        safety_level: str | None = None,
        long_poll: bool = False,
        idempotent: bool = True,
    ) -> None:
        """Register a tool.

        safety_level: "safe" (read-only), "caution" (write/network/spawn),
                      "dangerous" (arbitrary execution). Defaults to "caution"
                      for custom tools, "safe" for builtins.
        long_poll:    True for tools that block their thread waiting on other
                      sessions' work — they run on a dedicated executor.
        max_timeout:  Ceiling for a caller-supplied `timeout` argument. Required
                      for any tool whose schema exposes `timeout`, otherwise the
                      executor caps the call at `timeout` regardless.
        """
        if safety_level is None:
            safety_level = "caution" if source == "custom" else "safe"
        self._tools[name] = ToolDef(
            name=name,
            description=description,
            parameters=parameters,
            function=func,
            category=category,
            tags=tags or [],
            timeout=timeout,
            max_timeout=max_timeout,
            parallel_safe=parallel_safe,
            denied_session_types=set(denied_session_types or ()),
            source=source,
            safety_level=safety_level,
            long_poll=long_poll,
            idempotent=idempotent,
        )
        # A user's persisted safety override outranks the code default — same
        # semantics load_config applies at boot, extended to tools that
        # register after it (MCP servers connect asynchronously post-boot).
        if name in self._safety_overrides:
            self._tools[name].safety_level = self._safety_overrides[name]
        self.metrics.setdefault(name, ToolHealthMetrics())
        logger.debug("Registered tool: %s [%s]", name, source)

    def unregister(self, name: str) -> bool:
        """Remove a tool from the registry. Returns True if it existed.

        Metrics, the disabled flag, and any safety override are all kept —
        they are name-keyed on purpose so a tool that re-registers (an MCP
        server refresh, a reconnect) keeps its history and the user's
        settings. Callers must rebuild_index() after an unregister batch,
        same as after registration.
        """
        return self._tools.pop(name, None) is not None

    def get(self, name: str) -> ToolDef | None:
        return self._tools.get(name)

    def exists(self, name: str) -> bool:
        return name in self._tools

    def all_tools(self) -> list[ToolDef]:
        return list(self._tools.values())

    def enabled_tools(self) -> list[ToolDef]:
        return [t for t in self._tools.values() if t.name not in self._disabled]

    def get_schemas(self, names: list[str] | None = None) -> list[dict]:
        """Get OpenAI-format schemas for specific tools (or all enabled)."""
        if names is not None:
            return [self._tools[n].to_openai_schema() for n in names if n in self._tools and n not in self._disabled]
        # Sorted for deterministic prompt cache stability
        return [t.to_openai_schema() for t in sorted(self.enabled_tools(), key=lambda t: t.name)]

    def get_summary(self, name: str) -> ToolSummary | None:
        tool = self._tools.get(name)
        if not tool:
            return None
        return ToolSummary(
            name=tool.name,
            description=tool.description,
            category=tool.category,
            tags=list(tool.tags),
        )

    # --- Discovery ---

    def rebuild_index(self) -> None:
        """Rebuild the discovery index from current tools."""
        self.index.rebuild(self._tools)
        logger.info("Tool index rebuilt: %d tools indexed", len(self._tools))

    def discover(self, query: str, category: str | None = None, limit: int = 10) -> list[ToolSummary]:
        """Search for tools by natural language query.

        Disabled tools are filtered out so they never surface to the scout,
        the agent's discover_tools call, or any other consumer.
        """
        results = self.index.search(query, category=category, limit=limit)
        if not self._disabled:
            return results
        return [s for s in results if s.name not in self._disabled]

    def expand_cooccurrence(self, names: set[str] | list[str]) -> set[str]:
        """Return names plus their co-occurring siblings that exist + are enabled.

        Ensures a tool's natural cohort (e.g. spawn_worker → check_workers,
        get_worker_result, ...) travels together into active_tools so the
        model sees the full schema instead of guessing sibling names.
        """
        out = set(names)
        for name in list(out):
            for sibling in TOOL_COOCCURRENCE.get(name, []):
                if sibling in self._tools and sibling not in self._disabled:
                    out.add(sibling)
        return out

    # --- Enable/Disable ---

    def disable(self, name: str) -> None:
        self._disabled.add(name)
        self._save_config()

    def enable(self, name: str) -> None:
        self._disabled.discard(name)
        self._save_config()

    def is_disabled(self, name: str) -> bool:
        return name in self._disabled

    # --- Safety level ---

    _VALID_SAFETY_LEVELS = frozenset({"safe", "caution", "dangerous"})

    def set_safety_level(self, name: str, level: str) -> None:
        if level not in self._VALID_SAFETY_LEVELS:
            raise ValueError(f"Invalid safety level '{level}'. Must be: safe, caution, dangerous")
        tool = self._tools.get(name)
        if not tool:
            raise KeyError(f"Unknown tool '{name}'")
        tool.safety_level = level
        self._safety_overrides[name] = level
        self._save_config()

    # --- Config persistence ---

    def load_config(self) -> None:
        if TOOLS_CONFIG_PATH.exists():
            try:
                data = json.loads(TOOLS_CONFIG_PATH.read_text())
                self._disabled = set(data.get("disabled", []))
                for name, level in data.get("safety_levels", {}).items():
                    if level not in self._VALID_SAFETY_LEVELS:
                        continue
                    # Keep the override even when the tool isn't registered
                    # yet: late registrants (MCP tools land after their server
                    # connects, post-boot) pick it up in register(), and
                    # _save_config no longer drops it from tools.json.
                    self._safety_overrides[name] = level
                    if name in self._tools:
                        self._tools[name].safety_level = level
            except (json.JSONDecodeError, OSError) as e:
                logger.warning("Failed to load tools config: %s", e)

    def _save_config(self) -> None:
        TOOLS_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        data: dict = {"disabled": sorted(self._disabled)}
        if self._safety_overrides:
            data["safety_levels"] = dict(sorted(self._safety_overrides.items()))
        TOOLS_CONFIG_PATH.write_text(json.dumps(data, indent=2))

    # --- Execution (delegates to executor.py, but basic sync here) ---

    def execute_sync(self, name: str, arguments: dict, context: dict | None = None) -> str | tuple[str, dict]:
        """Synchronous tool execution with context injection and argument recovery."""
        tool = self._tools.get(name)
        if not tool:
            return f"Error: Unknown tool '{name}'. Use discover_tools to find available tools."
        if name in self._disabled:
            return f"Error: Tool '{name}' is currently disabled."

        # Inject _context if the function accepts it
        sig = inspect.signature(tool.function)
        if "_context" in sig.parameters and context is not None:
            arguments = {**arguments, "_context": context}

        # Filter out arguments the function doesn't accept (prevents TypeError)
        valid_params = set(sig.parameters.keys())
        has_kwargs = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values())
        if not has_kwargs:
            unknown = set(arguments.keys()) - valid_params
            if unknown:
                logger.warning(
                    "Tool '%s' called with unknown params: %s (accepted: %s)",
                    name,
                    unknown,
                    valid_params - {"_context"},
                )
                arguments = {k: v for k, v in arguments.items() if k in valid_params}

        # Scope the session's workspace override to this call. execute_sync
        # runs in the same thread as the tool function (to_thread or the
        # long-poll executor), so a set/reset around the invocation is
        # visible to every paths.workspace() call the tool makes and to
        # nothing else.
        from core.tools import paths as _paths

        _ws_token = _paths.WORKSPACE_OVERRIDE.set((context or {}).get("workspace_override"))
        try:
            result = tool.function(**arguments)
            # Tools can return (str, dict) to include structured metadata
            if isinstance(result, tuple) and len(result) == 2:
                content, metadata = result
                if content is None:
                    logger.warning("Tool '%s' returned None content — tool must return a string", name)
                    return f"Error: Tool '{name}' returned no output", metadata
                return str(content), metadata
            if result is None:
                logger.warning("Tool '%s' returned None — tool must return a string", name)
                return f"Error: Tool '{name}' returned no output"
            return str(result)
        except TypeError as e:
            # Argument mismatch — provide helpful error
            expected = [
                p.name for p in sig.parameters.values() if p.name != "_context" and p.default is inspect.Parameter.empty
            ]
            return f"Error calling '{name}': {e}. Required parameters: {expected}."
        except Exception as e:
            return f"Error: {e}"
        finally:
            # Always reset: the long-poll executor reuses thread contexts
            # across calls, so a leaked override would bleed into the next
            # session's tool call on that thread.
            _paths.WORKSPACE_OVERRIDE.reset(_ws_token)

    # --- Health report ---

    def get_health_report(self) -> list[dict]:
        return [
            {
                "name": name,
                "category": self._tools[name].category if name in self._tools else "",
                "enabled": name not in self._disabled,
                "total_calls": m.total_calls,
                "success_rate": round(m.success_rate, 3),
                "avg_latency_ms": round(m.avg_latency_ms, 1),
                "timeout_count": m.timeout_count,
                "last_error": m.last_error,
            }
            for name, m in self.metrics.items()
            if m.total_calls > 0
        ]


# ---------------------------------------------------------------------------
# Module singleton
# ---------------------------------------------------------------------------

_registry: ToolRegistry | None = None


def get_registry() -> ToolRegistry:
    global _registry
    if _registry is None:
        _registry = ToolRegistry()
    return _registry
