"""Pernix — Memory tools: remember, recall, deep_recall."""

from __future__ import annotations

import json
import logging

logger = logging.getLogger("pernix.tools.memory")

# ---------------------------------------------------------------------------
# deep_recall: LLM-backed memory agent
# ---------------------------------------------------------------------------

_DEEP_RECALL_SYSTEM = """\
You are a memory search specialist. Search the user's persistent memory and \
return a clear, attributed answer.

Memory is stored in topic-specific markdown files (pernix.notes.md, \
pernix.lessons.md, pernix.tools.md, etc.). Use search_memory first; if results \
are empty or all scores < 2.0, try keyword variants or rg_memory as fallback.

Strategy:
1. Start with the primary query.
2. If weak/empty: decompose into individual keywords, try @tags: prefix, try synonyms.
3. Use rg_memory when search_memory returns nothing after 2 attempts.
4. Synthesize findings into a concise attributed answer: [source_file] Relevant finding...
5. If nothing found after exhausting strategies, say so clearly.

Scores: > 3.0 strong · 1.0–3.0 weak · < 1.0 noise. Do not trust results below 1.0.\
"""

_DEEP_RECALL_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search_memory",
            "description": (
                "FTS5 keyword search over persistent memory. "
                "Returns scored results. Score > 3.0 = strong, 1.0–3.0 = weak, < 1.0 = noise. "
                "Try multiple queries with different phrasings if first attempt is weak/empty."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search query (natural language, @tags: prefix, or compound terms)",
                    },
                    "top": {"type": "integer", "description": "Max results (default 8, max 15)"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "rg_memory",
            "description": (
                "Ripgrep search over raw memory markdown files. "
                "Use as fallback when search_memory returns nothing — "
                "searches file content directly, bypassing the FTS5 index."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "Search pattern (case-insensitive regex or plain term)",
                    },
                },
                "required": ["pattern"],
            },
        },
    },
]


def _execute_deep_recall_tool(name: str, args: dict, memory_dir: str) -> str:
    if name == "search_memory":
        query = args.get("query", "")
        top = min(args.get("top", 8), 15)
        try:
            from core.memory.store import get_memory_store

            store = get_memory_store()
            if not store:
                return "Memory unavailable."
            results = store.search(query, limit=top)
            if not results:
                return "No results found."
            lines = []
            for r in results:
                content = r.entry.content
                if len(content) > 6144:  # ~2048 tokens at 3 chars/token
                    logger.warning(
                        "Large memory entry in %s (%d chars, ~%d tokens)",
                        r.entry.file_name,
                        len(content),
                        len(content) // 3,
                    )
                lines.append(f"[{r.entry.file_name} epoch={r.entry.epoch} score={r.score:.1f}] {content}")
            return "\n\n".join(lines)
        except Exception as e:
            return f"search_memory error: {e}"

    if name == "rg_memory":
        pattern = args.get("pattern", "")
        if not pattern:
            return "Error: pattern required."
        try:
            from core.memory.search import rg_memory_text

            return rg_memory_text(pattern, memory_dir)
        except Exception as e:
            return f"rg_memory error: {e}"

    return f"Unknown tool: {name}"


async def _deep_recall_async(query: str, context: str) -> str:
    from config import settings
    from core.llm.client import get_llm_client

    client = get_llm_client()
    model = settings.background_model or settings.scout_model or settings.llm_model
    memory_dir = settings.memory_dir

    user_msg = f"Search for: {query}"
    if context:
        user_msg += f"\n\nContext: {context}"

    messages: list[dict] = [
        {"role": "system", "content": _DEEP_RECALL_SYSTEM},
        {"role": "user", "content": user_msg},
    ]

    for round_num in range(4):
        is_last = round_num >= 3
        try:
            response = await client.chat(
                messages,
                tools=None if is_last else _DEEP_RECALL_TOOLS,
                model=model,
                max_tokens=1200,
            )
        except Exception as e:
            logger.warning("deep_recall LLM call failed on round %d: %s", round_num, e)
            break

        if not response.tool_calls or is_last:
            return (response.content or "No relevant memory found.").strip()

        # Append assistant turn with tool calls
        messages.append(
            {
                "role": "assistant",
                "content": response.content or "",
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {"name": tc.name, "arguments": tc.arguments},
                    }
                    for tc in response.tool_calls
                ],
            }
        )

        # Execute each tool call and append results
        for tc in response.tool_calls:
            try:
                args = json.loads(tc.arguments) if tc.arguments else {}
            except json.JSONDecodeError:
                args = {}
            result = _execute_deep_recall_tool(tc.name, args, memory_dir)
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})

    return "Search completed but no synthesis produced."


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


def recall(
    query: str,
    top: int = 5,
    file: str = "",
    include_seen: bool = False,
    _context: dict | None = None,
) -> str:
    """Fast FTS5 memory search. Returns scored results (score > 3.0 = strong,
    1.0–3.0 = weak, < 1.0 = noise). Full content is returned per result.
    On empty/weak results, use deep_recall() for LLM-synthesized search with
    keyword reformulation. Never use grep or file_read for memory.

    Entries already surfaced earlier in this session are collapsed to a short
    `file@epoch` reference footer to avoid re-emitting the same body twice.
    Set include_seen=True to bypass that dedup and re-pull full text.
    """
    from core.memory.store import get_memory_store

    store = get_memory_store()
    if store is None:
        return "Error: Memory system unavailable"

    try:
        fetch = top * 2 if file else top
        results = store.search(query, limit=fetch)
        if file:
            results = [r for r in results if r.entry.file_name.lower() == file.lower()][:top]
        if not results:
            return "No results found in memory."

        sid = (_context or {}).get("session_id", "")
        footer = ""
        if not include_seen:
            from core.memory.dedup import partition_seen

            new_results, _seen, footer = partition_seen(results, sid)
            if not new_results and footer:
                # Every result was already surfaced — return just the footer.
                return footer
            results = new_results

        lines = []
        for r in results:
            content = r.entry.content
            if len(content) > 6144:  # ~2048 tokens at 3 chars/token
                logger.warning(
                    "Large memory entry in %s (%d chars, ~%d tokens)",
                    r.entry.file_name,
                    len(content),
                    len(content) // 3,
                )
            lines.append(f"[{r.entry.file_name} epoch={r.entry.epoch} score={r.score:.1f}] {content}")
        body = "\n\n".join(lines)
        if footer:
            return f"{body}\n\n{footer}"
        return body
    except Exception as e:
        logger.error("Recall failed: %s", e)
        return f"Error searching memory: {e}"


def deep_recall(
    query: str,
    context: str = "",
    include_seen: bool = False,
    _context: dict | None = None,
) -> str:
    """LLM-backed memory search with synthesis. Searches memory using multiple
    strategies (FTS5 + ripgrep fallback), reformulates queries on weak/empty
    results, and returns a clean attributed answer. Raw search results stay
    inside the sub-agent — they do not pollute the caller's context.

    Use when: recall() returns empty/weak results, the query is complex or
    multi-faceted, or cross-file synthesis is needed. Pass context= to help
    the model focus on what's relevant. include_seen=True bypasses the
    per-session dedup ledger (only matters for the fallback path, which
    emits raw entries; the LLM path returns synthesis)."""
    import asyncio

    from core.memory.store import get_memory_store

    store = get_memory_store()
    if store is None:
        return "Error: Memory system unavailable"

    try:
        ctx = _context or {}
        loop = ctx.get("_loop") or asyncio.get_running_loop()
        future = asyncio.run_coroutine_threadsafe(_deep_recall_async(query, context), loop)
        return future.result(timeout=60)
    except Exception as e:
        logger.warning("deep_recall LLM agent failed, falling back to basic recall: %s", e)
        try:
            results = store.search(query, limit=8)
            if not results:
                return "No results found in memory."

            sid = (_context or {}).get("session_id", "")
            footer = ""
            if not include_seen:
                from core.memory.dedup import partition_seen

                new_results, _seen, footer = partition_seen(results, sid)
                if not new_results and footer:
                    return footer
                results = new_results

            lines = []
            for r in results:
                content = r.entry.content
                if len(content) > 6144:  # ~2048 tokens at 3 chars/token
                    logger.warning(
                        "Large memory entry in %s (%d chars, ~%d tokens)",
                        r.entry.file_name,
                        len(content),
                        len(content) // 3,
                    )
                lines.append(f"[{r.entry.file_name} epoch={r.entry.epoch} score={r.score:.1f}] {content}")
            body = "\n\n".join(lines)
            if footer:
                return f"{body}\n\n{footer}"
            return body
        except Exception as e2:
            return f"Error searching memory: {e2}"


def update_memory(file: str, epoch: int, content: str, _context: dict | None = None) -> str:
    """Replace the content of a specific memory entry. Use recall() first to find the file
    and epoch of the entry to correct. All metadata (type, tags, weight) is preserved."""
    from core.memory.store import get_memory_store

    store = get_memory_store()
    if store is None:
        return "Error: Memory system unavailable"
    if not file or not content.strip():
        return "Error: file and content are required"
    return store.update_entry(file, int(epoch), content)


def forget(file: str, epoch: int, _context: dict | None = None) -> str:
    """Permanently delete a specific memory entry. Use recall() first to find the file
    and epoch of the entry to remove. This cannot be undone."""
    from core.memory.store import get_memory_store

    store = get_memory_store()
    if store is None:
        return "Error: Memory system unavailable"
    if not file:
        return "Error: file is required"
    return store.delete_entry(file, int(epoch))


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
        description=(
            "Fast FTS5 search over CURATED long-term memory (insights, decisions, "
            "summaries — not raw transcript). Scores: > 3.0 strong · 1.0–3.0 weak · "
            "< 1.0 noise. Full content returned per result. On empty/weak results, "
            "use deep_recall() for LLM-synthesized search. NOTE: for verbatim "
            "message history of this or any other session, use `search_sessions` "
            "instead — `recall` cannot see raw transcript or trimmed-from-view "
            "messages. Never use grep or file_read for memory — they cannot reach "
            "the memory directory. Entries already surfaced earlier in this "
            "session are collapsed to a `file@epoch` reference footer; pass "
            "include_seen=true to re-pull full text."
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search query (natural language, @tags:, or compound terms)",
                },
                "top": {"type": "integer", "description": "Max results (default 5)"},
                "file": {
                    "type": "string",
                    "description": "Optional: restrict search to one memory file by name (e.g. pernix.lessons)",
                },
                "include_seen": {
                    "type": "boolean",
                    "description": (
                        "Default false — entries already shown this session collapse to "
                        "a reference footer. Set true to bypass dedup and re-pull full "
                        "content (only needed if the original tool result has scrolled "
                        "out of view or you genuinely need to re-examine the body)."
                    ),
                },
            },
            "required": ["query"],
        },
        category="memory",
        tags=["recall", "search", "memory", "retrieve", "find", "knowledge"],
        timeout=30,
        parallel_safe=True,
    )

    reg.register(
        name="deep_recall",
        func=deep_recall,
        description=(
            "LLM-backed memory search with synthesis. Searches memory using multiple "
            "strategies (FTS5 + ripgrep fallback), reformulates queries on weak/empty results, "
            "and returns a clean attributed answer. Raw search noise stays inside the sub-agent. "
            "Use when: recall() returns empty/weak results, the query is complex, "
            "or cross-file synthesis is needed. Pass context= to focus the search. "
            "include_seen=true bypasses the per-session dedup ledger (only affects "
            "the fallback path — the LLM path returns synthesis, not raw entries)."
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "What to find (natural language, tags, epoch, topic, etc.)",
                },
                "context": {
                    "type": "string",
                    "description": (
                        "Optional framing for the search model — why you need this and "
                        "what would be relevant (e.g. 'debugging a whisper transcription failure')"
                    ),
                },
                "include_seen": {
                    "type": "boolean",
                    "description": (
                        "Default false — fallback-path entries already shown this session "
                        "collapse to a reference footer. Set true to re-pull full text."
                    ),
                },
            },
            "required": ["query"],
        },
        category="memory",
        tags=["recall", "search", "memory", "retrieve", "find", "knowledge", "deep", "synthesize"],
        timeout=60,
        parallel_safe=True,
    )

    reg.register(
        name="update_memory",
        func=update_memory,
        description=(
            "Correct or rewrite an existing memory entry. "
            "First call recall() to find the entry — the output shows [file epoch=N score=X.X]. "
            "Then call update_memory with that file and epoch to replace its content. "
            "Use when a stored fact is wrong, outdated, or needs clarification."
        ),
        parameters={
            "type": "object",
            "properties": {
                "file": {"type": "string", "description": "Memory file name (e.g. pernix.lessons)"},
                "epoch": {"type": "integer", "description": "Epoch of the entry to update (from recall output)"},
                "content": {"type": "string", "description": "Replacement content for the entry"},
            },
            "required": ["file", "epoch", "content"],
        },
        category="memory",
        tags=["memory", "update", "correct", "fix", "edit", "amend"],
        safety_level="caution",
        timeout=15,
        parallel_safe=False,
    )

    reg.register(
        name="forget",
        func=forget,
        description=(
            "Permanently delete a memory entry. "
            "First call recall() to find the entry — the output shows [file epoch=N score=X.X]. "
            "Then call forget with that file and epoch. Cannot be undone. "
            "Prefer update_memory to correct wrong facts rather than deleting them entirely."
        ),
        parameters={
            "type": "object",
            "properties": {
                "file": {"type": "string", "description": "Memory file name (e.g. pernix.lessons)"},
                "epoch": {"type": "integer", "description": "Epoch of the entry to delete (from recall output)"},
            },
            "required": ["file", "epoch"],
        },
        category="memory",
        tags=["memory", "delete", "forget", "remove", "purge"],
        safety_level="caution",
        timeout=15,
        parallel_safe=False,
    )
