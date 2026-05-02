"""Pernix — Memory tools: remember, recall."""

from __future__ import annotations

import logging

logger = logging.getLogger("pernix.tools.memory")


def remember(content: str, file: str = "", tags: str = "", weight: str = "", _context: dict | None = None) -> str:
    """Save content to persistent memory."""
    from core.memory.store import get_memory_store

    store = get_memory_store()
    if store is None:
        return "Error: Memory system unavailable"

    # Auto-infer weight if not specified
    if not weight:
        upper = content.upper()
        if any(kw in upper for kw in ("CRITICAL", "NEVER FORGET", "ALWAYS ", "IMPORTANT", "NEVER ")):
            weight = "high"
        else:
            weight = "normal"

    try:
        result = store.add_entry(
            content=content,
            file_name=file or None,
            tags=tags,
            weight=weight,
            source="user",
        )

        # Track manual save for this session so distillation can skip it
        if _context and _context.get("session_id"):
            try:
                from db import models as db

                db.set_snooze_state(
                    f"manual_save:{_context['session_id']}",
                    str(int(__import__("time").time())),
                )
            except Exception:
                pass  # Non-critical, don't fail the save

        return result
    except Exception as e:
        logger.error("Remember failed: %s", e)
        return f"Error saving to memory: {e}"


def ingest(
    content: str = "", file_path: str = "", source_name: str = "", use_llm: bool = True, _context: dict | None = None
) -> str:
    """Ingest a structured document into memory, routing sections to topic files.

    Hybrid approach: parses by headings (cheap), then uses LLM to route each
    section to the best existing file or a new one. Falls back to keyword
    routing if LLM unavailable. Preserves all content — no selective filtering.
    Pass either content (text) or file_path (reads from disk).
    """
    from core.memory.ingest import ingest_document_sync

    if file_path and not content:
        try:
            from pathlib import Path

            p = Path(file_path)
            if not p.exists():
                return f"Error: File not found: {file_path}"
            content = p.read_text()
            if not source_name:
                source_name = p.name
        except Exception as e:
            return f"Error reading file: {e}"

    if not content:
        return "Error: Provide either content or file_path"

    if not source_name:
        source_name = "document"

    try:
        stats = ingest_document_sync(content, source_name=source_name, use_llm=use_llm)
        if "error" in stats:
            return f"Error: {stats['error']}"

        files_summary = ", ".join(f"{f}({n})" for f, n in sorted(stats["files_used"].items()))
        return (
            f"Ingested '{source_name}': {stats['entries_saved']} entries saved "
            f"across {len(stats['files_used'])} files [{files_summary}] "
            f"via {stats.get('routing_method', 'keywords')} routing. "
            f"{stats['entries_skipped_dup']} duplicates skipped, "
            f"{stats['entries_skipped_short']} short sections skipped."
        )
    except Exception as e:
        logger.error("Ingest failed: %s", e)
        return f"Error during ingestion: {e}"


def recall(query: str, top: int = 5, _context: dict | None = None) -> str:
    """Search persistent memory."""
    from core.memory.store import get_memory_store

    store = get_memory_store()
    if store is None:
        return "Error: Memory system unavailable"

    try:
        results = store.search(query, limit=top)
        if not results:
            return "No results found in memory."

        lines = []
        for r in results:
            lines.append(f"[{r.entry.file_name} score={r.score:.1f}] {r.entry.content[:400]}")
        return "\n\n".join(lines)
    except Exception as e:
        logger.error("Recall failed: %s", e)
        return f"Error searching memory: {e}"


def register(reg) -> None:
    reg.register(
        name="remember",
        func=remember,
        description="Save knowledge to persistent memory. Survives across sessions. Use for decisions, findings, preferences, techniques worth retaining.",
        parameters={
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "What to remember (self-contained, include context)"},
                "file": {
                    "type": "string",
                    "description": "Memory file name (e.g. pernix.decisions). Auto-routed if omitted.",
                },
                "tags": {"type": "string", "description": "Comma-separated tags for search"},
                "weight": {
                    "type": "string",
                    "enum": ["normal", "high"],
                    "description": "Importance level. Auto-inferred from content if omitted (CRITICAL/NEVER/ALWAYS → high).",
                },
            },
            "required": ["content"],
        },
        category="memory",
        tags=["remember", "save", "store", "memory", "persist", "note", "record"],
        timeout=30,
        parallel_safe=True,
    )

    reg.register(
        name="ingest",
        func=ingest,
        description="Ingest a structured document into memory, routing sections to appropriate topic files. Use for bulk knowledge import, backup restoration, or processing reference documents. Preserves all content with full fidelity.",
        parameters={
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": "Document text to ingest (markdown with headings). Use this OR file_path.",
                },
                "file_path": {"type": "string", "description": "Path to a file to ingest. Use this OR content."},
                "source_name": {"type": "string", "description": "Label for the source (e.g., 'MEMORY_BACKUP.md')"},
                "use_llm": {
                    "type": "boolean",
                    "description": "Use LLM to route sections to files (default true). Set false for keyword-only routing.",
                },
            },
        },
        category="memory",
        tags=["ingest", "import", "restore", "bulk", "memory", "document", "backup"],
        timeout=120,
        parallel_safe=False,
    )

    reg.register(
        name="recall",
        func=recall,
        description="Search persistent memory for relevant knowledge from previous sessions.",
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query (natural language)"},
                "top": {"type": "integer", "description": "Max results (default 5)"},
            },
            "required": ["query"],
        },
        category="memory",
        tags=["recall", "search", "memory", "retrieve", "find", "knowledge"],
        timeout=30,
        parallel_safe=True,
    )
