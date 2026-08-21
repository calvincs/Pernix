"""Pernix — Session memory distillation.

Async fire-and-forget: extracts key findings/decisions from a session
and saves them to persistent memory with dedup.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time

from config import settings

logger = logging.getLogger("pernix.memory.distill")


_SAVED_RE = re.compile(r"\b(?:SAVED|UPDATED) file=([A-Za-z0-9_.\-]+) epoch=(\d+)")
_ENUMERATION_RE = re.compile(r"(?:^|\s)(?:[1-9]\)|[1-9]\.|\([1-9]\))\s")
_WORD_RE = re.compile(r"[a-z0-9][a-z0-9_\-']{2,}")
_STOP = frozenset(
    "the and for with that this from into over under about after before when while where which "
    "what have has had was were are been being not but nor its their they them then than also "
    "into onto via per each every both all any some such only just more most less very".split()
)


def _agent_saved_entries(messages: list[dict], store) -> list[tuple[str, str, str]]:
    """(file, type, content) for every entry the agent itself wrote during the
    session, read back from the store — the tool results carry their ids."""
    out: list[tuple[str, str, str]] = []
    seen: set[tuple[str, int]] = set()
    for m in messages:
        if m.get("role") != "tool":
            continue
        for file_name, epoch in _SAVED_RE.findall(m.get("content") or ""):
            key = (file_name, int(epoch))
            if key in seen:
                continue
            seen.add(key)
            try:
                entry = store.get_entry(file_name, int(epoch))
            except Exception:
                entry = None
            if entry is not None and getattr(entry, "content", ""):
                out.append((file_name, str(getattr(entry, "entry_type", "") or ""), entry.content))
    return out


def _content_words(text: str) -> set[str]:
    return {w for w in _WORD_RE.findall((text or "").lower()) if w not in _STOP}


def _restates(
    candidate: str, candidate_file: str, candidate_type: str, saved: list[tuple[str, str, str]]
) -> tuple[str, str] | None:
    """The agent-saved entry a distilled candidate restates, if any.

    Similarity is the overlap coefficient (shared content words over the
    smaller vocabulary): a paraphrase that invents half its specifics still
    shares its topic words with the original, which Jaccard under-reads (the
    live fabricated top-6 scored 0.14 Jaccard against the real list). Same
    file needs a modest overlap — less when the entry type matches too, a
    second "decision" about the same list being the exact failure — while
    another file needs a strong one.
    """
    cw = _content_words(candidate)
    if not cw:
        return None
    for file_name, entry_type, content in saved:
        sw = _content_words(content)
        if not sw:
            continue
        overlap = len(cw & sw) / min(len(cw), len(sw))
        same_file = file_name == candidate_file
        same_type = bool(candidate_type) and candidate_type == entry_type
        if same_file and (overlap >= 0.25 or (same_type and overlap >= 0.15)):
            return file_name, content
        if overlap >= 0.6:
            return file_name, content
    return None


def _trigram_grounding(candidate: str, transcript: str) -> float:
    """Share of the candidate's word trigrams that occur verbatim in the
    transcript. Paraphrase lowers it, so it is only read when the candidate
    is an enumeration — lists of specifics are where invention shows."""
    words = _WORD_RE.findall((candidate or "").lower())
    if len(words) < 6:
        return 1.0
    hay = " ".join(_WORD_RE.findall((transcript or "").lower()))
    grams = [" ".join(words[i : i + 3]) for i in range(len(words) - 2)]
    return sum(1 for g in grams if g in hay) / len(grams)


def _is_saved(result: str) -> bool:
    """True for either shape of a landed write.

    The store returns "Saved to <file> (epoch=N)"; the memory tools translate
    that into the model-facing "SAVED file=<f> epoch=<n> VERIFY=OK". Both are
    accepted so this counter cannot silently mislabel a save as a dedup skip
    when a write path is routed through the tool layer.
    """
    return result.startswith("Saved to") or result.startswith("SAVED ")


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
            # Include tool results (e.g. "SAVED file=user.profile epoch=... VERIFY=OK",
            # or "Saved to user.profile" in transcripts predating the verdict
            # contract) so the LLM knows what entries were already saved and
            # won't re-extract them
            transcript_lines.append(f"[tool_result] {content[:400]}")
    transcript = "\n".join(transcript_lines)

    if len(transcript) < 200:
        return

    # Build existing file list for the prompt (rich catalog: name + count + description)
    from core.memory.ingest import _build_file_catalog

    file_list_str = _build_file_catalog(store)
    prompt = DISTILL_PROMPT.format(existing_files=file_list_str)

    # Entries the agent wrote itself this session are authoritative: the
    # distiller must not restate, extend or re-list them (told here, and
    # enforced by _restates below — the prompt alone did not stop a second
    # "decision" entry with an invented list on the live box).
    agent_saved = await asyncio.to_thread(_agent_saved_entries, messages, store)
    if agent_saved:
        prompt += (
            "\n\nENTRIES THE AGENT ALREADY SAVED THIS SESSION (authoritative — do NOT restate, "
            "summarize, extend or re-list them; omit any candidate that covers the same decision, "
            "finding or list):\n" + "\n".join(f"- [{f} · {t or 'note'}] {c[:600]}" for f, t, c in agent_saved[:12])
        )

    # LLM extraction
    client = get_llm_client()
    model = settings.background_model or settings.llm_model
    try:
        response = await client.chat(
            messages=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": transcript[:40000]},
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
    superseded = 0
    skipped_dup = 0
    skipped_restated = 0
    for entry in entries:
        content = entry.get("content", "")
        if not content:
            continue

        restated = _restates(content, entry.get("file") or "", str(entry.get("type") or ""), agent_saved)
        if restated is not None:
            skipped_restated += 1
            logger.info(
                "Distill: dropped a candidate for %s that restates the agent's own entry in %s",
                entry.get("file") or "?",
                restated[0],
            )
            continue

        tags = entry.get("tags", "")
        if _ENUMERATION_RE.search(content):
            grounding = _trigram_grounding(content, transcript)
            if grounding < 0.1:
                tags = f"{tags},unverified-distill" if tags else "unverified-distill"
                logger.info(
                    "Distill: enumerated candidate for %s has %.0f%% verbatim grounding in the transcript — tagged unverified-distill",
                    entry.get("file") or "?",
                    grounding * 100,
                )
        # Add date tag
        tags = f"{tags},{time.strftime('%Y-%m-%d')}" if tags else time.strftime("%Y-%m-%d")
        if session_type == "worker":
            tags += ",worker"

        # add_or_supersede_entry runs the multi-signal dedup gate itself and,
        # when a blocked write is a correction of the entry blocking it,
        # rewrites that entry in place instead of dropping the correction —
        # the similarity that makes a correction detectable is exactly what
        # made the old is_duplicate-then-continue discard it. It also enforces
        # unique (file, epoch) identity at write time.
        # Threaded: with an embedding model set the gate runs a hybrid search
        # whose query embedding is a blocking HTTP call, and this hook runs on
        # the event loop at turn end.
        result = str(
            await asyncio.to_thread(
                store.add_or_supersede_entry,
                content=content,
                file_name=entry.get("file") or None,
                entry_type=entry.get("type", "note"),
                tags=tags,
                weight=entry.get("weight", "normal"),
                source="distill",
                origin=origin,
            )
        )
        if result.startswith("Superseded"):
            superseded += 1
        elif _is_saved(result):
            saved += 1
        else:
            skipped_dup += 1

    logger.info(
        "Distilled session %s: %d saved, %d superseded, %d deduped, %d dropped as restating the agent's own entries",
        session_id,
        saved,
        superseded,
        skipped_dup,
        skipped_restated,
    )


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
