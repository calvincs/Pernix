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
from dataclasses import dataclass

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

PROVENANCE (every entry): transcript lines are tagged with their message id, as in
"[assistant #412]". Add these fields:
  "source_msgs": [412, 455]   — the message ids the entry actually rests on
  "status": "verified" | "asserted" — "verified" ONLY when a [tool_result] line in the
      transcript shows the outcome; "asserted" when the assistant merely stated it.

CORRECTIONS AND FINAL OUTCOMES: when later material refutes, narrows or replaces an
earlier claim, extract the CORRECTED form — never the superseded one — and add
  "supersedes": "one line naming the earlier claim this replaces"
The end of a long investigation is where its conclusion lives; a hypothesis stated
early and refuted later belongs in memory in its refuted form, or not at all.

If there is nothing worth saving, respond with just: SKIP

Be selective — only save what would be valuable in a future session."""

# One extraction call's worth of transcript, and how many of them a single
# invocation may make. Material past the last chunk keeps its place behind the
# watermark and is picked up by the next turn or the snooze catch-up: this is
# a background hook at turn end, not a place to fire ten LLM calls.
_CHUNK_CHARS = 40_000
_MAX_CHUNKS = 3
# Long bodies are clipped HEAD AND TAIL. A head-only clip is what made this
# subsystem blind to corrections: a refutation, a final result and a "that
# turned out to be wrong" all live at the END of the message carrying them,
# and the old 800-character prefix cut every one of them off.
_MSG_HEAD, _MSG_TAIL = 1_200, 800
_TOOL_HEAD, _TOOL_TAIL = 400, 400
# Provenance rendering caps.
_MAX_SOURCE_MSGS = 6
_MAX_SUPERSEDES = 200


def _clip_body(text: str, head: int, tail: int) -> str:
    """Keep the opening and the ending, and say what came out of the middle."""
    if len(text) <= head + tail:
        return text
    return f"{text[:head]} … [{len(text) - head - tail} chars elided] … {text[-tail:]}"


def _transcript_line(msg: dict) -> str:
    """One transcript line, tagged with the message id an entry can cite."""
    role = msg.get("role", "")
    content = msg.get("content") or ""
    if not isinstance(content, str):
        content = str(content)
    if not content:
        return ""
    tag = f"#{msg['id']}" if msg.get("id") is not None else "#?"
    if role in ("user", "assistant", "reflect"):
        return f"[{role} {tag}] {_clip_body(content, _MSG_HEAD, _MSG_TAIL)}"
    if role == "tool":
        # Tool results carry what already landed ("SAVED file=... VERIFY=OK")
        # so the extractor does not re-extract it, and they are the only
        # evidence in the transcript that anything was actually verified.
        return f"[tool_result {tag}] {_clip_body(content, _TOOL_HEAD, _TOOL_TAIL)}"
    return ""


@dataclass
class _Chunk:
    """One extraction call's material, and the coverage it commits on success."""

    text: str
    last_id: int
    tool_ids: set
    is_tail: bool


_TAIL_NOTE = (
    "\n[This is the most recent material in the session. Where it contradicts anything "
    "earlier, it is the current state — extract the later form.]"
)


def _chunk_transcript(lines: list[tuple], header: str) -> list[_Chunk]:
    """Split tagged transcript lines into bounded chunks, oldest first.

    `lines` is (msg_id, role, rendered). Every chunk repeats the header, which
    carries the session's opening request: the point is to stop re-reading the
    oldest 40,000 characters on every turn, not to swap that for a tail-only
    view that forgets what was asked for.
    """
    chunks: list[_Chunk] = []
    body: list[str] = []
    tool_ids: set = set()
    last_id = 0
    for msg_id, role, rendered in lines:
        if body and len(header) + sum(len(b) + 1 for b in body) + len(rendered) > _CHUNK_CHARS:
            chunks.append(_Chunk("\n".join([header, *body]), last_id, tool_ids, False))
            body, tool_ids = [], set()
        body.append(rendered)
        last_id = max(last_id, msg_id)
        if role == "tool":
            tool_ids.add(msg_id)
    if body:
        chunks.append(_Chunk("\n".join([header, *body]) + _TAIL_NOTE, last_id, tool_ids, True))
    return chunks


def _apply_provenance(entry: dict, chunk: _Chunk, session_id: str) -> tuple[str, str]:
    """Stamp an entry with where it came from and how well it is supported.

    A "verified" status is checked, not taken: the entry has to cite a
    message id that really is a tool result in the material it was extracted
    from. Otherwise it is an agent assertion, and says so — textual overlap
    with a transcript is not evidence that anything was run.

    A correction keeps the claim it replaces in its own text, so superseding
    the older entry does not erase what changed.
    """
    content = str(entry.get("content") or "")
    raw = entry.get("source_msgs")
    cited: list[int] = []
    for value in raw if isinstance(raw, list) else []:
        try:
            cited.append(int(value))
        except (TypeError, ValueError):
            continue
    verified = bool(chunk.tool_ids & set(cited))
    label = "tool-verified" if verified else "agent assertion, no tool result cited"
    cite = ",".join(str(i) for i in cited[:_MAX_SOURCE_MSGS]) or "unrecorded"
    content += f"\n[Provenance: session {session_id}, msgs {cite} — {label}]"

    supersedes = str(entry.get("supersedes") or "").strip()
    if supersedes:
        content += f"\n[Corrects an earlier claim: {supersedes[:_MAX_SUPERSEDES]}]"

    tags = "tool-verified" if verified else "agent-asserted"
    if supersedes:
        tags += ",correction"
    return content, tags


async def distill_session(
    session_id: str,
    title: str,
    messages: list[dict],
    session_type: str = "normal",
) -> None:
    """Extract and save key knowledge from a session."""
    # Canary isolation (trust-loop hardening W5): eval transcripts never
    # become memory. sessions/hooks._maybe_distill already returns early and
    # every background selector excludes the type in SQL, but the guard lives
    # HERE too — this is the only funnel every distill path passes through, so
    # a future caller cannot reopen the leak by forgetting a WHERE clause.
    if session_type == "canary":
        return

    from core.llm.client import get_llm_client
    from core.memory.store import get_memory_store

    store = get_memory_store()
    if not store:
        return

    from db import models as db

    # Claim-origin provenance (coarse, session-level): if the session pulled
    # external content in, everything distilled from it is marked external so
    # downstream consumers (the dream evidence packs, future scout weighting)
    # can discount web-derived claims relative to operational records.
    origin = "external" if _session_used_web_tools(messages) else "internal"

    # Only the material this session has not already been distilled from
    # (v38). Running the whole transcript through every turn meant that past
    # ~40,000 clipped characters the extractor's input was FROZEN: two
    # consecutive turns sent byte-identical bytes, burning a background call
    # each time, while every later correction sat outside the prefix forever.
    watermark = await asyncio.to_thread(db.get_distill_watermark, session_id)
    fresh = [m for m in messages if int(m.get("id") or 0) > watermark]
    if not fresh:
        logger.debug("Distillation: nothing new past msg %d for session %s", watermark, session_id)
        return

    # The header rides every chunk. The opening request is what states the
    # session's standing constraints, and dropping it in favour of the newest
    # material would trade one bias for its mirror image.
    header_lines = [f"Session: {title} (type={session_type})"]
    if watermark:
        opening = next((m for m in messages if m.get("role") == "user" and (m.get("content") or "")), None)
        covered = len(messages) - len(fresh)
        header_lines.append(
            f"[context] {covered} earlier message(s) in this session were distilled previously "
            f"(up to msg {watermark}) and are not repeated here."
        )
        if opening is not None and int(opening.get("id") or 0) <= watermark:
            header_lines.append(f"[context · original request #{opening.get('id')}] {(opening['content'])[:800]}")
    header = "\n".join(header_lines)

    lines = []
    for msg in fresh:
        rendered = _transcript_line(msg)
        if rendered:
            lines.append((int(msg.get("id") or 0), msg.get("role", ""), rendered))
    if not lines:
        return

    chunks = _chunk_transcript(lines, header)
    if sum(len(c.text) for c in chunks) < 200:
        return
    if len(chunks) > _MAX_CHUNKS:
        logger.info(
            "Distillation: %d chunks of new material for %s, taking %d this pass",
            len(chunks),
            session_id,
            _MAX_CHUNKS,
        )
        chunks = chunks[:_MAX_CHUNKS]

    # Build existing file list for the prompt (rich catalog: name + count + description)
    from core.memory.ingest import _build_file_catalog

    file_list_str = _build_file_catalog(store)
    prompt = DISTILL_PROMPT.format(existing_files=file_list_str)

    # Space routing (v33): a space session's knowledge belongs in the
    # space's own bucket unless it is genuinely deployment-wide. The slug
    # also scopes add_or_supersede_entry's auto-routing below.
    space_slug = None
    try:
        from core.spaces import space_slug_for_session

        space_slug = space_slug_for_session(session_id)
    except Exception:
        space_slug = None
    if space_slug:
        prompt += (
            f"\n\nSPACE ROUTING: this session belongs to the space '{space_slug}'. Route "
            f"session-scoped facts to pernix.space.{space_slug}.<topic> files (e.g. "
            f"pernix.space.{space_slug}.research). Use the canonical global files above "
            f"only for facts that matter deployment-wide (user profile, system config)."
        )

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

    # LLM extraction, one call per chunk. Coverage is committed per chunk and
    # only after both the extraction and every store write for it succeeded:
    # a failed call or a failed write leaves the material uncovered, and the
    # next turn sees it again.
    client = get_llm_client()
    model = settings.background_model or settings.llm_model
    saved = superseded = skipped_dup = skipped_restated = 0
    covered_to = watermark

    for index, chunk in enumerate(chunks):
        try:
            response = await client.chat(
                messages=[
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": chunk.text},
                ],
                model=model,
                max_tokens=2000,
            )
            text = response.content.strip()
        except Exception as e:
            logger.warning("Distillation LLM call failed on chunk %d/%d: %s", index + 1, len(chunks), e)
            break

        if text.upper() == "SKIP":
            logger.debug("Distillation: LLM returned SKIP for session %s chunk %d", session_id, index + 1)
            entries: list[dict] = []
        else:
            entries = _parse_entries(text)
            if not entries:
                # Unparseable output is a failed extraction, not an empty
                # one. Committing coverage here would retire the material on
                # the strength of a response nobody could read.
                logger.warning("Distillation: unparseable output for %s chunk %d", session_id, index + 1)
                break

        try:
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

                content, provenance_tags = _apply_provenance(entry, chunk, session_id)
                tags = ",".join(t for t in (str(entry.get("tags") or ""), provenance_tags) if t)
                content, tags = _apply_grounding_guard(content, tags, entry, chunk.text)
                # Add date tag
                tags = f"{tags},{time.strftime('%Y-%m-%d')}" if tags else time.strftime("%Y-%m-%d")
                if session_type == "worker":
                    tags += ",worker"

                # add_or_supersede_entry runs the multi-signal dedup gate
                # itself and, when a blocked write is a correction of the
                # entry blocking it, rewrites that entry in place instead of
                # dropping the correction — the similarity that makes a
                # correction detectable is exactly what made the old
                # is_duplicate-then-continue discard it. It also enforces
                # unique (file, epoch) identity at write time.
                # Threaded: with an embedding model set the gate runs a hybrid
                # search whose query embedding is a blocking HTTP call, and
                # this hook runs on the event loop at turn end.
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
                        space_slug=space_slug,
                    )
                )
                if result.startswith("Superseded"):
                    superseded += 1
                elif _is_saved(result):
                    saved += 1
                else:
                    skipped_dup += 1
        except Exception as e:
            logger.warning("Distillation storage failed for %s chunk %d: %s", session_id, index + 1, e)
            break

        covered_to = chunk.last_id
        await asyncio.to_thread(db.set_distill_watermark, session_id, covered_to)

    logger.info(
        "Distilled session %s up to msg %d (was %d): %d saved, %d superseded, %d deduped, "
        "%d dropped as restating the agent's own entries",
        session_id,
        covered_to,
        watermark,
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


def _apply_grounding_guard(content: str, tags: str, entry: dict, transcript: str) -> tuple[str, str]:
    """Down-rank enumerated candidates the transcript does not verbatim support.

    Tagging alone was not enough: the recall renderer never showed tags, so a
    1%-grounded candidate that opened with "Solved ARC2 136b0064" at
    @weight:high was read as authoritative by the next worker (field case
    pernix.arc_agi_game_completion_protocols @1787694955). Verification
    status belongs in the claim text itself, and an unsupported claim must
    not carry high weight.
    """
    if not _ENUMERATION_RE.search(content):
        return content, tags
    grounding = _trigram_grounding(content, transcript)
    if grounding >= 0.1:
        return content, tags
    tags = f"{tags},unverified-distill" if tags else "unverified-distill"
    logger.info(
        "Distill: enumerated candidate for %s has %.0f%% verbatim grounding in the transcript — tagged unverified-distill",
        entry.get("file") or "?",
        grounding * 100,
    )
    content = (
        f"UNVERIFIED (distilled at {grounding:.0%} verbatim grounding — "
        f"treat as hypothesis, not established fact): {content}"
    )
    if entry.get("weight") == "high":
        entry["weight"] = "normal"
    return content, tags
