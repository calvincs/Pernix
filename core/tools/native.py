"""The fixed tool list a session sends with every model call.

The served Qwen template renders tools ahead of the system text, so a tool
list that changes between calls makes the whole prompt cold. Measured on a
50k prompt: 80% reused and 2.0 s with the same tools, 0-3% and 9 s with one
tool added, even at the end of the list. The list changed on about 320 of 523
real turns when scout chose it.

So the list holds the tools most turns need, and every other tool is reached
through `lookup` and `use_tool`. Of 523 real turns, 24 needed a tool outside
this set.

goal_create is native because the moment it matters is mid-turn on a long
job, when the agent would otherwise have to discover it through lookup: a
319-video ingest never found it and stopped at every budget cut.
"""

from __future__ import annotations

NATIVE_TOOLS = frozenset(
    {
        "ask_user",
        "await_workers",
        "bash",
        "browse_web",
        "call_model",
        "check_workers",
        "deep_recall",
        "file_edit",
        "file_read",
        "file_write",
        "get_worker_result",
        "glob",
        "goal_complete",
        "goal_create",
        "goal_status",
        "grep",
        "http_get",
        "job_kill",
        "job_start",
        "job_status",
        "job_tail",
        "load_skill",
        "lookup",
        "multiedit",
        "notify_user",
        "recall",
        "remember",
        "repl",
        "search_sessions",
        "search_web",
        "session_read",
        "spawn_worker",
        "use_tool",
        "view_image",
    }
)


def frozen_surface(registry, allowlist=None, excluded=None) -> list[str]:
    """This session's tool list, the same on every call.

    A scheduled job or worker kind's allow-list IS its surface: it is fixed for
    the session and may name tools outside the native set. Retry exclusions
    are still removed, so a retry with exclusions costs one cold prompt.
    """
    wanted = set(allowlist) if allowlist else set(NATIVE_TOOLS)
    active = {name for name in wanted if registry.exists(name) and not registry.is_disabled(name)}
    return sorted(active - set(excluded or ()))


def catalog_names(registry) -> list[str]:
    """Every enabled tool outside the native set, for the prompt's one-line list."""
    return sorted(t.name for t in registry.enabled_tools() if t.name not in NATIVE_TOOLS)
