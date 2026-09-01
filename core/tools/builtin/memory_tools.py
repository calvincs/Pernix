"""Pernix — Memory tools: remember, recall, deep_recall."""

from __future__ import annotations

import json
import logging
import re

logger = logging.getLogger("pernix.tools.memory")

# ---------------------------------------------------------------------------
# Write verdicts
#
# The store's return strings are an internal contract (consolidate, ingest and
# audit parse them) and stay as they are. What the model sees is translated
# here into a leading case-locked verdict token, because the untranslated
# strings cost a real save: a dedup refusal reads as neither "Error" nor
# "Saved", and the model reported a save that never landed.
#
# Two tokens, never one. The verdict says whether the write happened; a
# separate trailing VERIFY= token says whether a read-back of the markdown
# agreed. Blurring them lets a skimmer collapse "SAVED + mismatch" into
# "SAVED". VERIFY is only ever OK, MISMATCH, MISSING, STILL-PRESENT or
# UNVERIFIED — nothing hedged like PARTIALLY, which no read-back can support.
# ---------------------------------------------------------------------------

_SAVED_RE = re.compile(r"^Saved to (?P<file>\S+) \(epoch=(?P<epoch>\d+)\)")
_UPDATED_RE = re.compile(r"^Updated entry epoch=(?P<epoch>\d+) in '(?P<file>[^']+)'")
_DELETED_RE = re.compile(r"^Deleted entry epoch=(?P<epoch>\d+) from '(?P<file>[^']+)'")
_DUPLICATE_RE = re.compile(r'duplicate of (?P<file>[^\s@]+)@(?P<epoch>\d+): "(?P<preview>.*?)"\)', re.DOTALL)

_PREVIEW_CHARS = 120


def _reason(store_result: str) -> str:
    """The store's message with its leading 'Error: ' stripped."""
    text = store_result.strip()
    if text.lower().startswith("error:"):
        return text[6:].strip()
    return text


def _duplicate_verdict(store_result: str) -> str:
    """Translate the dedup refusal, keeping the supersede call intact.

    Without the action the model treats a blocked write as done and moves on,
    so the instruction is not decoration — it is the only path from "blocked"
    to "corrected".
    """
    m = _DUPLICATE_RE.search(store_result)
    if not m:
        return f"NOT SAVED — {_reason(store_result)}"
    f, e, preview = m.group("file"), m.group("epoch"), m.group("preview")
    return (
        f'NOT SAVED — duplicate of {f}@{e}: "{preview}". If your version is newer or more '
        f"accurate, repeat this remember() call with supersede='{f}@{e}' (one call), or "
        f"update_memory(file='{f}', epoch={e}, content=...)"
    )


def _verify_write(store, verdict: str, file_name: str, epoch: int, sent_content: str) -> str:
    """Read the entry back from markdown and append the VERIFY token.

    `sent_content` is compared after sanitize_entry_content, the same
    transform the store applies before writing — comparing against the raw
    text would report legitimate sanitization as a mismatch.
    """
    head = f"{verdict} file={file_name} epoch={epoch}"
    try:
        entry = store.get_entry(file_name, epoch)
    except Exception as e:
        logger.warning("Memory read-back failed for %s@%s: %s", file_name, epoch, e)
        return f"{head} VERIFY=UNVERIFIED — read-back failed ({e}); confirm with recall()"

    not_saved = "NOT SAVED" if verdict == "SAVED" else "NOT UPDATED"
    if entry is None:
        return f"{not_saved} — VERIFY=MISSING: write did not land (no entry epoch={epoch} in {file_name} on read-back)"

    from core.memory.format import sanitize_entry_content

    if entry.content.strip() != sanitize_entry_content(sent_content).strip():
        stored = entry.content.strip()[:_PREVIEW_CHARS].replace("\n", " ")
        return f'{head} VERIFY=MISMATCH — stored content differs from what you sent (stored: "{stored}")'
    return f"{head} VERIFY=OK"


# ---------------------------------------------------------------------------
# Federated read-side (agent-ergonomics plan P2: reads federate, writes
# govern). Knowledge lives in 6+ stores, each with its own governed write
# path; nothing requires six READ surfaces. deep_recall appends bounded,
# provenance-tagged hits from the other stores so "which store do I ask?"
# stops being the agent's problem. Every source is best-effort: a store
# that is off or broken contributes nothing, never an error.
# ---------------------------------------------------------------------------

_FED_PER_SOURCE = 3
_FED_SNIPPET_CHARS = 160


def _federated_sections(query: str) -> str:
    q = " ".join(query.split()).lower()
    if not q:
        return ""
    words = [w for w in q.split() if len(w) > 2][:6]
    if not words:
        return ""
    sections: list[str] = []

    # Adaptive store — the rules currently shaping behavior.
    try:
        from db.models import connect_sessions

        like = " OR ".join("(lower(title) LIKE ? OR lower(content) LIKE ?)" for _ in words)
        params: list[str] = []
        for w in words:
            params += [f"%{w}%", f"%{w}%"]
        with connect_sessions() as conn:
            rows = conn.execute(
                f"SELECT id, kind, source, title, content FROM adaptive_entries "
                f"WHERE status = 'active' AND ({like}) LIMIT {_FED_PER_SOURCE}",
                params,
            ).fetchall()
        for r in rows:
            body = " ".join(str(r["content"]).split())[:_FED_SNIPPET_CHARS]
            sections.append(f"[adaptive/{r['kind']} · {r['source']}] {r['title']}: {body}")
    except Exception:
        pass

    # Telos claims — validated beliefs with epistemic-class caps.
    try:
        from config import settings as _s

        if _s.telos_enabled:
            from core.telos.store import TelosStore

            store = TelosStore.open()
            hits = 0
            for c in store.list("claim"):
                text = str(c.get("text") or c.get("statement") or c.get("content") or "")
                if any(w in text.lower() for w in words):
                    conf = c.get("confidence")
                    tag = f" (conf {float(conf):.2f})" if conf is not None else ""
                    sections.append(f"[telos claim{tag}] {' '.join(text.split())[:_FED_SNIPPET_CHARS]}")
                    hits += 1
                    if hits >= 2:
                        break
    except Exception:
        pass

    # Skills — procedural knowledge that may already cover the topic.
    try:
        from core.skills.registry import get_skill_registry

        hits = 0
        for s in get_skill_registry().all_skills():
            hay = f"{s.name} {s.description} {' '.join(s.tags or [])}".lower()
            if any(w in hay for w in words):
                sections.append(f"[skill] {s.name}: {' '.join(s.description.split())[:_FED_SNIPPET_CHARS]}")
                hits += 1
                if hits >= _FED_PER_SOURCE:
                    break
    except Exception:
        pass

    # Session transcripts — raw history the curated store may not carry.
    try:
        from db.models import connect_sessions

        fts_q = '"' + q.replace('"', " ") + '"'
        with connect_sessions() as conn:
            rows = conn.execute(
                "SELECT session_id, snippet(messages_fts, 2, '', '', '…', 14) sn "
                f"FROM messages_fts WHERE messages_fts MATCH ? LIMIT {_FED_PER_SOURCE}",
                (fts_q,),
            ).fetchall()
        for r in rows:
            sections.append(f"[session {str(r['session_id'])[:12]}] …{' '.join(str(r['sn']).split())}…")
    except Exception:
        pass

    if not sections:
        return ""
    return "\n\nRELATED IN OTHER STORES (provenance-tagged; read-only federation):\n" + "\n".join(
        f"- {s}" for s in sections
    )


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


def _execute_deep_recall_tool(name: str, args: dict, memory_dir: str, space_slug: str | None = None) -> str:
    if name == "search_memory":
        query = args.get("query", "")
        top = min(args.get("top", 8), 15)
        try:
            from core.memory.store import get_memory_store

            store = get_memory_store()
            if not store:
                return "Memory unavailable."
            results = store.search(query, limit=top, space_slug=space_slug)
            if not results:
                return "No results found."
            from core.memory.search import format_result_line

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
                lines.append(format_result_line(r))
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


async def _deep_recall_async(query: str, context: str, space_slug: str | None = None) -> str:
    from config import settings
    from core.llm.client import get_llm_client

    client = get_llm_client()
    model = settings.background_model or settings.llm_model
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
            result = _execute_deep_recall_tool(tc.name, args, memory_dir, space_slug=space_slug)
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})

    return "Search completed but no synthesis produced."


def remember(
    content: str, file: str = "", tags: str = "", weight: str = "", supersede: str = "", _context: dict | None = None
) -> str:
    """Save content to persistent memory. Returns a SAVED / NOT SAVED verdict.

    supersede='file@epoch' replaces that entry instead of appending — the
    single-call repair for a duplicate refusal (the refusal names this exact
    target), so correcting a stale fact costs one call, not a
    recall/update_memory round-trip (agent-ergonomics plan §4.5)."""
    from core.memory.store import get_memory_store

    store = get_memory_store()
    if store is None:
        return "NOT SAVED — Memory system unavailable"

    if supersede:
        target_file, sep, raw_epoch = supersede.partition("@")
        if not sep or not target_file or not raw_epoch.strip().isdigit():
            return f"NOT SAVED — supersede must be 'file@epoch' (got {supersede!r})"
        return update_memory(target_file.strip(), int(raw_epoch.strip()), content, _context=_context)

    # Auto-infer weight if not specified
    if not weight:
        upper = content.upper()
        if any(kw in upper for kw in ("CRITICAL", "NEVER FORGET", "ALWAYS ", "IMPORTANT", "NEVER ")):
            weight = "high"
        else:
            weight = "normal"

    try:
        from core.spaces import space_slug_for_session

        result = store.add_entry(
            content=content,
            file_name=file or None,
            tags=tags,
            weight=weight,
            source="user",
            space_slug=space_slug_for_session((_context or {}).get("session_id", "")),
        )
    except Exception as e:
        logger.error("Remember failed: %s", e)
        return f"NOT SAVED — {e}"

    if "already contains similar content" in result:
        return _duplicate_verdict(result)

    m = _SAVED_RE.match(result)
    if not m:
        return f"NOT SAVED — {_reason(result)}"

    # Only a landed write suppresses distillation for this session — a refused
    # or failed one leaves the fact unrecorded and distill must still see it.
    if _context and _context.get("session_id"):
        try:
            from db import models as db

            db.set_snooze_state(
                f"manual_save:{_context['session_id']}",
                str(int(__import__("time").time())),
            )
        except Exception:
            pass  # Non-critical, don't fail the save

    return _verify_write(store, "SAVED", m.group("file"), int(m.group("epoch")), content)


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
        from core.spaces import space_slug_for_session

        fetch = top * 2 if file else top
        results = store.search(
            query,
            limit=fetch,
            space_slug=space_slug_for_session((_context or {}).get("session_id", "")),
        )
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

        from core.memory.search import format_result_line

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
            lines.append(format_result_line(r))
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
        from core.spaces import space_slug_for_session

        _slug = space_slug_for_session((_context or {}).get("session_id", ""))
    except Exception:
        _slug = None
    try:
        ctx = _context or {}
        loop = ctx.get("_loop") or asyncio.get_running_loop()
        future = asyncio.run_coroutine_threadsafe(_deep_recall_async(query, context, space_slug=_slug), loop)
        return future.result(timeout=60) + _federated_sections(query)
    except Exception as e:
        logger.warning("deep_recall LLM agent failed, falling back to basic recall: %s", e)
        try:
            results = store.search(query, limit=8, space_slug=_slug)
            if not results:
                fed = _federated_sections(query)
                return ("No results found in memory." + fed) if fed else "No results found in memory."

            sid = (_context or {}).get("session_id", "")
            footer = ""
            if not include_seen:
                from core.memory.dedup import partition_seen

                new_results, _seen, footer = partition_seen(results, sid)
                if not new_results and footer:
                    return footer
                results = new_results

            from core.memory.search import format_result_line

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
                lines.append(format_result_line(r))
            body = "\n\n".join(lines) + _federated_sections(query)
            if footer:
                return f"{body}\n\n{footer}"
            return body
        except Exception as e2:
            return f"Error searching memory: {e2}"


def _coerce_epoch(epoch) -> int:
    """Accept an epoch as int, integral float, or numeric string — including
    scientific notation.

    Local models serialize large integers as scientific notation
    ('1.777154774e+09'); a hard int() parse cost 12 failed retries in one
    observed session (1e2806e0d2ea). Every observed case preserved the full
    digits in a different format, so an exact integral value is coerced; a
    genuinely fractional value still raises.
    """
    if isinstance(epoch, bool):
        raise ValueError(f"epoch must be an integer, got {epoch!r}")
    if isinstance(epoch, int):
        return epoch
    f = float(str(epoch).strip())
    if not f.is_integer():
        raise ValueError(f"epoch must be a whole number, got {epoch!r}")
    return int(f)


def update_memory(file: str, epoch: int, content: str, _context: dict | None = None) -> str:
    """Replace the content of a specific memory entry. Use recall() first to find the file
    and epoch of the entry to correct. All metadata (type, tags, weight) is preserved.
    Returns an UPDATED / NOT UPDATED verdict."""
    from core.memory.store import get_memory_store

    store = get_memory_store()
    if store is None:
        return "NOT UPDATED — Memory system unavailable"
    if not file or not content.strip():
        return "NOT UPDATED — file and content are required"
    try:
        epoch = _coerce_epoch(epoch)
    except (TypeError, ValueError):
        return "NOT UPDATED — epoch must be the entry's integer timestamp from the " f"recall output (got {epoch!r})"
    try:
        result = store.update_entry(file, epoch, content)
    except Exception as e:
        logger.error("update_memory failed: %s", e)
        return f"NOT UPDATED — {e}"

    m = _UPDATED_RE.match(result)
    if not m:
        return f"NOT UPDATED — {_reason(result)}"
    return _verify_write(store, "UPDATED", m.group("file"), int(m.group("epoch")), content)


def forget(file: str, epoch: int, _context: dict | None = None) -> str:
    """Permanently delete a specific memory entry. Use recall() first to find the file
    and epoch of the entry to remove. This cannot be undone. Returns a
    DELETED / NOT DELETED verdict."""
    from core.memory.store import get_memory_store

    store = get_memory_store()
    if store is None:
        return "NOT DELETED — Memory system unavailable"
    if not file:
        return "NOT DELETED — file is required"
    try:
        epoch = _coerce_epoch(epoch)
    except (TypeError, ValueError):
        return "NOT DELETED — epoch must be the entry's integer timestamp from the " f"recall output (got {epoch!r})"
    try:
        result = store.delete_entry(file, epoch)
    except Exception as e:
        logger.error("forget failed: %s", e)
        return f"NOT DELETED — {e}"

    m = _DELETED_RE.match(result)
    if not m:
        return f"NOT DELETED — {_reason(result)}"

    file_name, deleted_epoch = m.group("file"), int(m.group("epoch"))
    head = f"DELETED file={file_name} epoch={deleted_epoch}"
    try:
        entry = store.get_entry(file_name, deleted_epoch)
    except Exception as e:
        logger.warning("Memory read-back failed for %s@%s: %s", file_name, deleted_epoch, e)
        return f"{head} VERIFY=UNVERIFIED — read-back failed ({e}); confirm with recall()"
    if entry is not None:
        return (
            f"NOT DELETED — VERIFY=STILL-PRESENT: entry epoch={deleted_epoch} is still in " f"{file_name} on read-back"
        )
    return f"{head} VERIFY=OK"


def register(reg) -> None:
    reg.register(
        name="remember",
        func=remember,
        description=(
            "Save knowledge to persistent memory. Survives across sessions. Use for decisions, "
            "findings, preferences, techniques worth retaining. The result always begins SAVED "
            "or NOT SAVED — only 'SAVED file=<f> epoch=<n> VERIFY=OK' means the entry is on "
            "disk (VERIFY is a read-back of the stored text; MISMATCH means it landed but "
            "differs from what you sent). 'NOT SAVED — duplicate of <f>@<e>' means nothing was "
            "written: if your version is newer or more accurate, repeat the call with "
            "supersede='<f>@<e>' (one-call repair), or call update_memory(file, epoch, "
            "content) — otherwise the correction is lost."
        ),
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
                "supersede": {
                    "type": "string",
                    "description": (
                        "'file@epoch' of an existing entry to REPLACE with this content — the "
                        "one-call repair when a save was refused as a duplicate of that entry."
                    ),
                },
            },
            "required": ["content"],
        },
        category="memory",
        tags=["remember", "save", "store", "memory", "persist", "note", "record"],
        timeout=30,
        parallel_safe=True,
        denied_session_types={"canary"},  # plan §5: canaries read memory, never write
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
        denied_session_types={"canary"},
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
            "LLM-backed memory search with synthesis, FEDERATED across every knowledge "
            "store: long-term memory (FTS5 + ripgrep, query reformulation, attributed "
            "answer) plus provenance-tagged hits from adaptive entries, telos claims, "
            "skills, and session transcripts — one query instead of guessing which store "
            "to ask. Use when: recall() returns empty/weak results, the query is complex, "
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
            "Use when a stored fact is wrong, outdated, or needs clarification — including "
            "after remember() returns 'NOT SAVED — duplicate of <f>@<e>'. The result always "
            "begins UPDATED or NOT UPDATED; only 'UPDATED file=<f> epoch=<n> VERIFY=OK' means "
            "the new text is on disk."
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
        denied_session_types={"canary"},
    )

    reg.register(
        name="forget",
        func=forget,
        description=(
            "Permanently delete a memory entry. LAST RESORT — its calibrated reliability is "
            "poor (single-digit percent on the live ledger), almost always from a stale epoch: "
            "the epoch MUST come from a recall() run THIS turn, never from memory or an "
            "earlier session. Prefer update_memory to correct a wrong fact (same freshness "
            "rule, but a failed update loses nothing). "
            "First call recall() to find the entry — the output shows [file epoch=N score=X.X]. "
            "Then call forget with that file and epoch. Cannot be undone. "
            "The result always begins DELETED or NOT DELETED; only 'DELETED file=<f> "
            "epoch=<n> VERIFY=OK' means the entry is gone from disk."
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
        denied_session_types={"canary"},
    )
