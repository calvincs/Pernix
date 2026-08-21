"""Pernix — Structured document ingestion into memory.

Parses a structured document (markdown with headings/sections) into
topical chunks, then uses an LLM to route each to the best memory file
(existing or new). Preserves all content — no selective filtering.

Hybrid approach:
  1. Parse sections by headings (no LLM, cheap)
  2. One LLM call to route ALL sections (sees existing files + descriptions)
  3. Save entries to routed files with dedup

Falls back to keyword routing if LLM is unavailable.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re

from config import settings

logger = logging.getLogger("pernix.memory.ingest")

# ---------------------------------------------------------------------------
# Section parsing (no LLM)
# ---------------------------------------------------------------------------


def parse_sections(text: str) -> list[dict]:
    """Split a markdown document into sections by headings.

    Returns list of {"heading": str, "level": int, "content": str, "index": int}.
    """
    sections = []
    current_heading = "preamble"
    current_level = 0
    current_lines: list[str] = []
    index = 0

    for line in text.split("\n"):
        heading_match = re.match(r"^(#{1,4})\s+(.+)", line)
        if heading_match:
            content = "\n".join(current_lines).strip()
            if content:
                sections.append(
                    {
                        "heading": current_heading,
                        "level": current_level,
                        "content": content,
                        "index": index,
                    }
                )
                index += 1
            current_heading = heading_match.group(2).strip()
            current_level = len(heading_match.group(1))
            current_lines = []
        else:
            current_lines.append(line)

    content = "\n".join(current_lines).strip()
    if content:
        sections.append(
            {
                "heading": current_heading,
                "level": current_level,
                "content": content,
                "index": index,
            }
        )

    return sections


def _clean_section_content(content: str) -> str:
    """Strip markdown formatting artifacts for memory storage."""
    content = re.sub(r"\n---+\n", "\n", content)
    content = re.sub(r"\n{3,}", "\n\n", content)
    return content.strip()


# ---------------------------------------------------------------------------
# Keyword-based routing (fallback when LLM unavailable)
# ---------------------------------------------------------------------------

# Canonical table lives in core.memory.routing alongside the store's
# NAMESPACE_KEYWORDS so the routing vocabularies can't drift apart again.
from core.memory.routing import TOPIC_KEYWORDS as _TOPIC_KEYWORDS  # noqa: E402


def route_section_keywords(heading: str, content: str) -> str:
    """Keyword-based fallback routing. Returns best-matching file name."""
    text = f"{heading} {content[:500]}".lower()
    best_file = "pernix.notes"
    best_score = 0
    for file_name, keywords in _TOPIC_KEYWORDS.items():
        score = sum(1 for kw in keywords if kw in text)
        if score > best_score:
            best_score = score
            best_file = file_name
    return best_file


# ---------------------------------------------------------------------------
# LLM routing (one call for all sections)
# ---------------------------------------------------------------------------

_ROUTE_PROMPT = """You are a memory file router. Given a list of document sections, assign each
to the best memory file. You may pick an existing file OR create a new one.

EXISTING MEMORY FILES:
{file_catalog}

SECTIONS TO ROUTE:
{section_list}

For each section, output a JSON array with one entry per section:
[{{"index": 0, "file": "chosen.file.name"}}, {{"index": 1, "file": "new.topic.name"}}, ...]

Rules:
- Prefer an existing file if the topic matches well
- Create a new file (dot-separated lowercase name) if NO existing file fits
- New file names should be descriptive: e.g., "research.erythritol", "pernix.youtube", "user.hardware"
- Keep file names short (2-3 segments, e.g., "pernix.tools" not "pernix.tools.video.extraction.pyav")
- Route identity/personal info about the USER to "user.profile"
- Route agent self-knowledge to "pernix.identity"
- Route lessons/mistakes to "pernix.lessons"
- Route code patterns/tools/workflows to "pernix.tools"
- When in doubt, pick the more specific file over a general one
- Output valid JSON only, no markdown fences
/no_think"""


def _build_file_catalog(store) -> str:
    """Build a catalog of existing files with descriptions for the LLM."""
    files = store.list_files()
    if not files:
        return "(no files yet — you will be creating the first ones)"

    lines = []
    for f in files:
        if f.entry_count > 0:
            lines.append(f"- {f.name} ({f.entry_count} entries): {f.description}")

    # Also show known topic names that don't exist yet as suggestions
    existing_names = {f.name for f in files}
    for topic in _TOPIC_KEYWORDS:
        if topic not in existing_names:
            lines.append(f"- {topic} (empty, suggested topic)")

    return "\n".join(lines) if lines else "(no files yet)"


def _build_section_list(sections: list[dict]) -> str:
    """Build a compact section list for the routing prompt."""
    lines = []
    for s in sections:
        preview = _clean_section_content(s["content"])[:200]
        lines.append(f"[{s['index']}] \"{s['heading']}\": {preview}")
    return "\n".join(lines)


async def _llm_route_sections(
    sections: list[dict],
    store,
) -> dict[int, str] | None:
    """Ask LLM to route sections to files. Returns {index: file_name} or None on failure."""
    from core.llm.client import get_llm_client

    catalog = _build_file_catalog(store)
    section_list = _build_section_list(sections)

    prompt = _ROUTE_PROMPT.format(file_catalog=catalog, section_list=section_list)

    client = get_llm_client()
    model = settings.background_model or settings.llm_model

    try:
        response = await client.chat(
            messages=[
                {"role": "system", "content": "You are a memory file router."},
                {"role": "user", "content": prompt},
            ],
            model=model,
            max_tokens=2000,
        )
        text = response.content.strip()
    except Exception as e:
        logger.warning("LLM routing call failed: %s", e)
        return None

    # Parse JSON response
    text = text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        text = "\n".join(lines[1:])
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()

    try:
        data = json.loads(text)
        if isinstance(data, list):
            routing = {}
            for item in data:
                idx = item.get("index")
                fname = item.get("file", "")
                if idx is not None and fname:
                    routing[int(idx)] = fname
            return routing
    except (json.JSONDecodeError, TypeError, ValueError):
        logger.warning("Failed to parse LLM routing response: %s", text[:200])

    return None


# ---------------------------------------------------------------------------
# Main ingest pipeline
# ---------------------------------------------------------------------------


async def ingest_document(
    text: str,
    source_name: str = "document",
    min_section_length: int = 50,
    use_llm: bool = True,
) -> dict:
    """Ingest a structured document into memory.

    Hybrid approach:
      1. Parse sections by headings (no LLM)
      2. Route via LLM (one call) or fall back to keyword matching
      3. Save entries with dedup

    Args:
        text: Full document text (markdown).
        source_name: Label for the source.
        min_section_length: Skip sections shorter than this.
        use_llm: Whether to use LLM for routing (falls back to keywords if False or unavailable).
    """
    from core.memory.store import get_memory_store

    store = get_memory_store()
    if not store:
        return {"error": "Memory system unavailable"}

    # Phase 1: Parse sections (no LLM)
    sections = parse_sections(text)
    if not sections:
        return {"error": "No sections found in document", "sections_parsed": 0}

    # Filter out very short sections before routing
    valid_sections = []
    skipped_short = 0
    for s in sections:
        content = _clean_section_content(s["content"])
        if len(content) < min_section_length:
            skipped_short += 1
        else:
            s["clean_content"] = content
            valid_sections.append(s)

    if not valid_sections:
        return {"error": "All sections too short", "sections_parsed": len(sections)}

    # Phase 2: Route sections (LLM with keyword fallback)
    routing: dict[int, str] = {}
    llm_routed = False

    if use_llm:
        llm_result = await _llm_route_sections(valid_sections, store)
        if llm_result:
            routing = llm_result
            llm_routed = True

    # Fill in any sections the LLM didn't route (or all if LLM was skipped/failed)
    for s in valid_sections:
        if s["index"] not in routing:
            routing[s["index"]] = route_section_keywords(s["heading"], s["clean_content"])

    # Phase 3: Save entries
    stats = {
        "sections_parsed": len(sections),
        "entries_saved": 0,
        "entries_superseded": 0,
        "entries_skipped_short": skipped_short,
        "entries_skipped_dup": 0,
        "files_used": {},
        "source": source_name,
        "routing_method": "llm" if llm_routed else "keywords",
    }

    for section in valid_sections:
        content = section["clean_content"]

        file_name = routing.get(section["index"], "pernix.notes")

        # Build tags from heading
        heading_words = re.findall(r"[a-zA-Z]+", section["heading"].lower())
        tags = ",".join(w for w in heading_words if len(w) > 2)[:100]
        if source_name:
            tags = f"{tags},ingested" if tags else "ingested"

        # Auto-infer weight
        upper = content.upper()
        weight = (
            "high"
            if any(kw in upper for kw in ("CRITICAL", "NEVER FORGET", "ALWAYS ", "IMPORTANT", "NEVER "))
            else "normal"
        )

        # Prepend heading as context
        entry_content = f"{section['heading']}: {content}" if section["heading"] != "preamble" else content

        # Cap to prevent bloat
        if len(entry_content) > 20000:
            entry_content = entry_content[:20000] + "... [truncated]"

        # Dedup gate lives inside add_or_supersede_entry: a re-ingested
        # document whose section has since been corrected upstream rewrites
        # the stored entry rather than being dropped for resembling it.
        result = str(
            await asyncio.to_thread(
                store.add_or_supersede_entry,
                content=entry_content,
                file_name=file_name,
                entry_type="finding",
                tags=tags,
                weight=weight,
                source="ingest",
            )
        )
        if result.startswith("Superseded"):
            stats["entries_superseded"] += 1
            continue
        if not result.startswith("Saved to"):
            stats["entries_skipped_dup"] += 1
            continue
        stats["entries_saved"] += 1
        stats["files_used"][file_name] = stats["files_used"].get(file_name, 0) + 1

    logger.info(
        "Ingested '%s': %d sections → %d saved, %d superseded, %d short, %d dup across %d files (%s routing)",
        source_name,
        stats["sections_parsed"],
        stats["entries_saved"],
        stats["entries_superseded"],
        stats["entries_skipped_short"],
        stats["entries_skipped_dup"],
        len(stats["files_used"]),
        stats["routing_method"],
    )
    return stats


# ---------------------------------------------------------------------------
# Sync wrapper for tool use
# ---------------------------------------------------------------------------


def ingest_document_sync(
    text: str,
    source_name: str = "document",
    min_section_length: int = 50,
    use_llm: bool = True,
) -> dict:
    """Synchronous wrapper for ingest_document.

    Tries to use the running event loop; creates one if needed.
    """

    try:
        loop = asyncio.get_running_loop()
        # We're inside an async context — schedule as a task
        import concurrent.futures

        with concurrent.futures.ThreadPoolExecutor() as pool:
            future = pool.submit(
                asyncio.run,
                ingest_document(text, source_name, min_section_length, use_llm),
            )
            return future.result(timeout=120)
    except RuntimeError:
        # No running loop — safe to use asyncio.run
        return asyncio.run(ingest_document(text, source_name, min_section_length, use_llm))


def correction_preamble(kind: str, approved_by: str = "human", source_ref: str = "") -> str:
    """The provenance stamp on a corrective memory entry.

    `approved_by` is "human" for a click in the Adaptive panel and "auto" for
    the veto-window drain. Entries used to say "human-approved" for both,
    which misattributed every auto-approved correction to the operator.
    """
    from config import settings

    label = "STALE-INFO CORRECTION" if kind == "memory_stale" else "CONTRADICTION RESOLVED"
    if approved_by == "auto":
        provenance = (
            f"auto-approved after the {settings.adaptive_auto_approve_after_hours}h veto window, adaptive review"
        )
    else:
        provenance = "human-approved via adaptive review"
    ref = f", {source_ref}" if source_ref else ""
    return f"{label} ({provenance}{ref})"


def apply_memory_correction(
    files: list[str],
    statement: str,
    source_ref: str = "",
    kind: str = "contradiction",
    approved_by: str = "human",
) -> list[str]:
    """Write a corrective entry into each cited memory file (audit P5).

    The mechanical effector for approved dream contradiction/stale findings:
    additive and non-destructive — the disputed entries stay, and recall now
    surfaces the correction next to them, which is what changes behavior.
    Returns the file names that received a corrective entry.
    """
    from core.memory.store import get_memory_store

    statement = (statement or "").strip()
    if not statement:
        return []
    preamble = correction_preamble(kind, approved_by, source_ref)
    store = get_memory_store()
    written: list[str] = []
    for fname in files:
        if not fname:
            continue
        try:
            result = store.add_entry(
                content=(
                    f"{preamble}: {statement[:1200]} "
                    f"— treat this note as overriding any conflicting older entries in this file."
                ),
                file_name=fname,
                entry_type="note",
                tags=f"correction,{kind}",
                weight="high",
                source="dream_fix",
            )
            if isinstance(result, str) and result.startswith("Error"):
                logger.warning("memory correction rejected for %s: %s", fname, result)
                continue
            written.append(fname)
        except Exception as e:
            logger.warning("memory correction write failed for %s: %s", fname, e)
    return written
