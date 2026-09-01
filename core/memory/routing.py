"""Pernix — Shared memory-file routing vocabulary and name canonicalization.

Single home for the symbols that decide *which file* a memory entry lands
in. Three writers route content (store auto-route, ingest section routing,
distill's LLM prompt rules) and consolidation has to undo their drift —
keeping the vocabulary and the name-normalization logic in one module is
what keeps those four views from diverging again (they used to be
copy-pasted in store.py / ingest.py / consolidate.py).

The two keyword tables stay distinct on purpose:

- NAMESPACE_KEYWORDS: store.add_entry auto-route fallback. Small curated
  topical buckets matched against raw entry content when no file name is
  suggested.
- TOPIC_KEYWORDS: ingest.py keyword fallback when LLM routing is
  unavailable. Matched against section heading + content; richer and more
  domain-specific because ingested documents arrive pre-structured.

distill.py's DISTILL_PROMPT also names canonical files (user.profile,
pernix.lessons, pernix.tools, ...) in its FILE ROUTING RULES — when adding
or renaming a canonical file here, update that prompt too.
"""

from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# Space buckets (v33): pernix.space.<slug>.* files belong to one space
# ---------------------------------------------------------------------------

_SPACE_FILE_RE = re.compile(r"^pernix\.space\.([a-z0-9][a-z0-9_-]*)\.")
SPACE_PREFIX_FMT = "pernix.space.{slug}."


def space_bucket(file_name: str) -> str | None:
    """The space slug a memory file belongs to, or None for global files.

    Consolidation, reroute and merge sweeps use this as a hard boundary:
    entries never move between buckets (space↔space or space↔global), no
    matter how name- or content-similar the files look.
    """
    m = _SPACE_FILE_RE.match(file_name or "")
    return m.group(1) if m else None


# ---------------------------------------------------------------------------
# Name canonicalization (used by store routing and consolidation clustering)
# ---------------------------------------------------------------------------


def normalize_file_name(name: str) -> str:
    """Canonicalize a memory file name for comparison.

    Strips extensions, noise suffixes, normalizes separators to underscore.
    """
    name = name.lower()
    # Strip common format-like suffixes (files ending in _txt, _json, etc.)
    for suffix in ("_txt", "_json", "_py", "_html", "_log", "_csv"):
        if name.endswith(suffix):
            name = name[: -len(suffix)]
    # Normalize separators to underscore
    name = re.sub(r"[-.]", "_", name)
    # Collapse double underscores
    while "__" in name:
        name = name.replace("__", "_")
    # Strip noise suffixes that don't add topical value
    for noise in (
        "_notes",
        "_log",
        "_summary",
        "_overview",
        "_report",
        "_analysis",
        "_strategy",
        "_guide",
        "_spec",
        "_template",
    ):
        if name.endswith(noise):
            name = name[: -len(noise)]
    return name.strip("_")


def name_tokens(name: str) -> set[str]:
    """Split a file name into word tokens (length > 2)."""
    return {t for t in re.split(r"[._-]", name.lower()) if len(t) > 2}


# ---------------------------------------------------------------------------
# Routing keyword tables
# ---------------------------------------------------------------------------

# store.add_entry auto-route fallback (content keywords → namespace bucket)
NAMESPACE_KEYWORDS: dict[str, list[str]] = {
    "user.profile": ["user", "profile", "age", "location", "name", "preference", "likes", "dislikes"],
    "pernix.decisions": ["decided", "decision", "chose", "rationale", "why we"],
    "pernix.preferences": ["prefer", "preference", "style", "convention", "always", "never"],
    "pernix.research": ["found", "research", "discovered", "learned", "source"],
    "pernix.debugging": ["debug", "fix", "bug", "error", "workaround", "solved"],
    "pernix.config": ["config", "setting", "environment", "variable", "parameter"],
    "pernix.tools": ["tool", "function", "utility", "command", "usage pattern"],
    "pernix.tasks": ["task", "todo", "milestone", "goal", "objective"],
    "pernix.notes": [],  # default fallback
}

# ingest.py keyword fallback when LLM routing is unavailable
# (section heading + content → topic file)
TOPIC_KEYWORDS: dict[str, list[str]] = {
    "user.profile": [
        "identity",
        "name",
        "location",
        "personal",
        "profile",
        "who i am",
        "personality",
        "traits",
        "preferences",
        "user context",
        "employer",
        "timezone",
        "family",
        "background",
        "self-description",
    ],
    "pernix.identity": [
        "pernix",
        "agent identity",
        "self-understanding",
        "philosophy",
        "operational",
        "core principle",
        "state lives on disk",
        "autonomy",
    ],
    "pernix.lessons": [
        "lesson",
        "critical",
        "mistake",
        "never forget",
        "learned",
        "failure",
        "error pattern",
        "correct workflow",
        "never again",
    ],
    "pernix.tools": [
        "tool",
        "pyav",
        "ffmpeg",
        "yt-dlp",
        "pillow",
        "extraction",
        "pattern",
        "template",
        "code",
        "script",
        "workflow",
        "pipeline",
        "frame extraction",
        "video processing",
    ],
    "pernix.debugging": [
        "fix",
        "bug",
        "debug",
        "workaround",
        "solved",
        "problem",
        "lightbox",
        "modal",
        "css",
        "ui fix",
        "root cause",
    ],
    "pernix.research": [
        "research",
        "analysis",
        "methodology",
        "evidence",
        "findings",
        "study",
        "paper",
        "investigation",
    ],
    "pernix.architecture": [
        "architecture",
        "layer",
        "context system",
        "design",
        "state management",
        "session",
        "scout",
        "distributed",
    ],
    "pernix.guidelines": [
        "guideline",
        "rule",
        "behavioral",
        "decision-making",
        "framework",
        "communication",
        "style",
        "approach",
        "never forget list",
    ],
    "pernix.projects": [
        "project",
        "gallery",
        "website",
        "unfinished",
        "follow-up",
        "continuation",
        "status",
    ],
}
