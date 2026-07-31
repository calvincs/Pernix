"""Pernix — Session memory distillation.

Async fire-and-forget: extracts key findings/decisions from a session
and saves them to persistent memory with dedup.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time

from config import settings

logger = logging.getLogger("pernix.memory.distill")

# The FILE ROUTING RULES below name canonical memory files — keep in sync
# with the routing vocabulary in core/memory/routing.py.
DISTILL_PROMPT = """You are a session memory distiller. Extract the most important findings,
decisions, and skills from this conversation.

Output a JSON array of entries. Each entry:
{{
  "type": "finding|decision|skill|note",
  "tags": "comma,separated,keywords",
  "weight": "high|normal",
  "file": "suggested.file.name",
  "content": "Self-contained description (include context, rationale)"
}}

EXISTING MEMORY FILES (MUST prefer these over creating new ones):
{existing_files}

When suggesting a "file" name, strongly prefer an existing file from the list above
if the topic matches. Only suggest a new name if the content is genuinely novel.

FILE ROUTING RULES:
- "user.profile": ONLY personal info about the user — name, location, employment, preferences,
  hardware, working style. Do NOT put system architecture, code patterns, or technical findings here.
- "pernix.config": system design, component details, agent loop behavior,
  tool schemas, extension internals, deployment settings.
- "pernix.lessons": operational lessons, mistakes, recovery patterns, critical gotchas.
- "pernix.tools": tool usage patterns, code workflows, command recipes.
- "pernix.research": external findings, third-party system analysis, study results.
- Skill-specific files (e.g. "youtube-transcription-skill"): ONLY knowledge specific to
  that skill's domain. General patterns learned during the skill belong in pernix.lessons or pernix.tools.

If the conversation includes [REFLECT] messages with retry verdicts, extract the lessons
learned (what worked, what failed, recovery strategies) as "skill" type entries with
weight "high". These are hard-won operational patterns worth preserving.

If there is nothing worth saving, respond with just: SKIP

Be selective — only save what would be valuable in a future session."""


async def distill_session(
    session_id: str,
    title: str,
    messages: list[dict],
    session_type: str = "normal",
) -> None:
    """Extract and save key knowledge from a session."""
    from core.llm.client import get_llm_client
    from core.memory.store import get_memory_store

    store = get_memory_store()
    if not store:
        return

    # Claim-origin provenance (coarse, session-level): if the session pulled
    # external content in, everything distilled from it is marked external so
    # downstream consumers (the dream evidence packs, future scout weighting)
    # can discount web-derived claims relative to operational records.
    origin = "external" if _session_used_web_tools(messages) else "internal"

    # Build conversation transcript (include tool results so LLM sees what was already saved)
    transcript_lines = [f"Session: {title} (type={session_type})"]
    for msg in messages:
        role = msg.get("role", "")
        content = msg.get("content", "")
        if role in ("user", "assistant", "reflect") and content:
            transcript_lines.append(f"[{role}] {content[:800]}")
        elif role == "tool" and content:
            # Include tool results (e.g., "Saved to user.profile") so the LLM
            # knows what entries were already saved and won't re-extract them
            transcript_lines.append(f"[tool_result] {content[:400]}")
    transcript = "\n".join(transcript_lines)

    if len(transcript) < 200:
        return

    # Build existing file list for the prompt (rich catalog: name + count + description)
    from core.memory.ingest import _build_file_catalog

    file_list_str = _build_file_catalog(store)
    prompt = DISTILL_PROMPT.format(existing_files=file_list_str)

    # LLM extraction
    client = get_llm_client()
    model = settings.background_model or settings.llm_model
    try:
        response = await client.chat(
            messages=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": transcript[:15000]},
            ],
            model=model,
            max_tokens=2000,
        )
        text = response.content.strip()
    except Exception as e:
        logger.warning("Distillation LLM call failed: %s", e)
        return

    if text.upper() == "SKIP":
        logger.debug("Distillation: LLM returned SKIP for session %s", session_id)
        return

    # Parse JSON entries
    entries = _parse_entries(text)
    if not entries:
        return

    # Save with dedup
    saved = 0
    skipped_dup = 0
    for entry in entries:
        content = entry.get("content", "")
        if not content:
            continue

        # Dedup check (multi-signal: BM25 top-3 + SequenceMatcher + Jaccard)
        if store.is_duplicate(content):
            skipped_dup += 1
            continue

        tags = entry.get("tags", "")
        # Add date tag
        tags = f"{tags},{time.strftime('%Y-%m-%d')}" if tags else time.strftime("%Y-%m-%d")
        if session_type == "worker":
            tags += ",worker"

        # add_entry enforces unique (file, epoch) identity at write time.
        await asyncio.to_thread(
            store.add_entry,
            content=content,
            file_name=entry.get("file") or None,
            entry_type=entry.get("type", "note"),
            tags=tags,
            weight=entry.get("weight", "normal"),
            source="distill",
            origin=origin,
        )
        saved += 1

    logger.info("Distilled session %s: %d saved, %d deduped", session_id, saved, skipped_dup)


_WEB_TOOLS = ("search_web", "browse_web", "http_get")


def _session_used_web_tools(messages: list[dict]) -> bool:
    """True when any assistant turn called a web-facing tool."""
    for m in messages:
        tc = m.get("tool_calls") or ""
        if tc and any(t in str(tc) for t in _WEB_TOOLS):
            return True
    return False


def _parse_entries(text: str) -> list[dict]:
    """Parse JSON array from LLM response, handling markdown fences."""
    # Strip markdown code fences
    text = text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        text = "\n".join(lines[1:])
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()

    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return [data]
        if isinstance(data, list):
            return data
    except json.JSONDecodeError:
        logger.debug("Failed to parse distillation JSON: %s", text[:200])
    return []
