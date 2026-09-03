"""Pernix — Spaces: named/colored long-lived session groups (migration v33).

A space is one DB row plus slug-keyed artifacts. The slug is immutable after
creation because three on-disk namespaces key off it:

    memory files      pernix.space.<slug>.*
    directives        data/agent/spaces/<slug>/{SOUL,RULES,SESSIONS}.md
    workspace home    data/workspace/spaces/<slug>/

The shared REPL kernel keys off the space *id* (``space-<id>``) instead —
kernel state dirs are machine-managed, so hand-editability doesn't apply.

This module is the single resolution point: the compiler, scout, memory
store, kernel registry and cron dispatcher all answer "which space is this
session in, and where do its things live" through these helpers. Keep it
import-light (mirrors core/tools/paths.py) — it is imported from hot paths.
"""

from __future__ import annotations

import logging
import re
import threading
from pathlib import Path

from config import settings

logger = logging.getLogger("pernix.spaces")

# No dots: the slug must be a single dot-segment of a memory file name
# (pernix.space.<slug>.<topic>) under core/memory/store._NAME_RE.
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
MAX_SLUG_LEN = 40

DIRECTIVE_NAMES = ("SOUL", "RULES", "SESSIONS")

# Spaces change rarely (user CRUD only), so a plain cache with explicit
# invalidation from the writers beats a TTL. Keyed by space id; a None
# value caches "no such space" so deleted-space lookups stay cheap.
_space_cache: dict[str, dict | None] = {}
_cache_lock = threading.Lock()


def slugify(label: str) -> str:
    """Derive a slug from a label: lowercase, runs of non-alphanumerics
    become single hyphens. Raises ValueError when nothing usable remains."""
    slug = re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-_")[:MAX_SLUG_LEN]
    if not slug or not SLUG_RE.match(slug):
        raise ValueError(f"label {label!r} yields no valid slug")
    return slug


def invalidate_space_cache() -> None:
    """Called by every spaces-API write (create/update/delete)."""
    with _cache_lock:
        _space_cache.clear()


def get_space(space_id: str) -> dict | None:
    if not space_id:
        return None
    with _cache_lock:
        if space_id in _space_cache:
            return _space_cache[space_id]
    from db import models as db

    space = db.get_space(space_id)
    with _cache_lock:
        _space_cache[space_id] = space
    return space


def get_session_space(session_id: str) -> dict | None:
    """Resolve a session's space. Fast path: the live session object's
    space_id field; fallback: one indexed SELECT on the sessions row."""
    if not session_id:
        return None
    space_id: str | None = None
    try:
        from sessions.manager import get_manager

        live = get_manager().get(session_id)
        if live is not None:
            space_id = live.space_id
        else:
            from db import models as db

            row = db.get_session(session_id)
            space_id = row.get("space_id") if row else None
    except Exception:
        logger.debug("space resolution failed for %s", session_id[:12], exc_info=True)
        return None
    return get_space(space_id) if space_id else None


def space_slug_for_session(session_id: str) -> str | None:
    space = get_session_space(session_id)
    return space["slug"] if space else None


def space_agent_dir(space: dict) -> Path:
    """Per-space directive overrides — hand-editable, like data/agent."""
    return Path("data/agent") / "spaces" / space["slug"]


def space_workspace_home(space: dict) -> Path:
    return Path(settings.workspace_dir).resolve() / "spaces" / space["slug"]


def space_memory_prefix(space: dict) -> str:
    return f"pernix.space.{space['slug']}."


def kernel_key_for_session(session_id: str) -> str:
    """Space sessions share one kernel keyed by space id; everyone else
    keeps the per-session key. The 'space-' prefix cannot collide with
    session ids (12 lowercase hex chars)."""
    space = get_session_space(session_id)
    return f"space-{space['id']}" if space else session_id


def directive_path(fname: str, session_id: str) -> Path:
    """Resolve a directive file for a session: the space's override when it
    exists, else the shared default. Per-file fallback — a space that only
    defines RULES.md still gets the default SOUL.md and SESSIONS.md."""
    space = get_session_space(session_id)
    if space is not None:
        candidate = space_agent_dir(space) / fname
        if candidate.exists():
            return candidate
    return Path("data/agent") / fname
