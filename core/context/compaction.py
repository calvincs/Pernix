"""Pernix — Append-only compaction. Never modifies stored messages.

Phase 1: View-based pruning (at assembly time, zero cost)
Phase 2: Orphan exclusion (at assembly time, zero cost)
Phase 3: LLM summarization (append-only, writes new compaction marker)
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field

from config import settings
from core.context.tokens import get_estimator
from core.llm.types import extract_tool_call_fields, is_rejected_call
from db import models as db

logger = logging.getLogger("pernix.context.compaction")

# Roles that are context-assembly markers rather than real conversation turns.
# Mirror compile_context's active-window filter so the boundary id we record
# lines up with the messages the compiler will keep after compaction.
_MARKER_ROLES = frozenset({"compaction", "scout", "notice", "reflect", "model_divider", "eval"})

COMPACTION_PROMPT = """Summarize this conversation. Output structured JSON followed by a prose paragraph:

```json
{
  "goal": "one sentence describing the overall task",
  "progress": ["completed item 1", "completed item 2"],
  "files_created": ["path/to/file1.html", "path/to/file2.py"],
  "decisions": [{"decision": "...", "rationale": "..."}],
  "active_context": ["ongoing preference or instruction"],
  "next_steps": ["what remains to be done"]
}
```

Then write a 2-3 sentence prose summary below the JSON block for natural reading.

RULES:
- Be concise. Every token matters.
- Preserve workspace file paths exactly.
- Capture key decisions with rationale.
- Do not include raw code or tool output in the summary.
"""

COMPACTION_UPDATE_PROMPT = """Update this existing conversation summary with new information.

EXISTING SUMMARY:
{existing_summary}

NEW CONVERSATION (since last summary):
{new_content}

Output an updated summary in the same JSON + prose format. Merge new information, don't repeat old details."""


# ---------------------------------------------------------------------------
# Phase 1: View-based pruning (applied at assembly time)
# ---------------------------------------------------------------------------


def apply_view_pruning(
    messages: list[dict],
    keep_recent: int | None = None,
    min_chars: int | None = None,
) -> list[dict]:
    """Prune old tool results in a VIEW — original messages unchanged.

    Returns a new list where old, large tool results are stubbed. Stubbed
    entries carry a private ``_view_pruned`` marker (stripped before the LLM
    sees them) so the compiler can count and surface what was dropped.
    This is a view transform for context assembly, NOT a DB mutation.
    The caller decides WHETHER to prune (budget pressure); this function
    only decides WHAT to prune.
    """
    if keep_recent is None:
        keep_recent = int(getattr(settings, "view_prune_keep_recent", 30))
    if min_chars is None:
        min_chars = int(getattr(settings, "view_prune_min_chars", 2000))
    if len(messages) <= keep_recent:
        return list(messages)

    cutoff = len(messages) - keep_recent
    result = []
    for i, msg in enumerate(messages):
        if i < cutoff and msg.get("role") == "tool":
            # `or ""` not a get() default: the column is nullable and a NULL
            # row returns None, which len() then raises on.
            content = msg.get("content") or ""
            if len(content) > min_chars:
                preview = content[:80].replace("\n", " ")
                stub = f"[pruned — {len(content)} chars] {preview}..."
                result.append({**msg, "content": stub, "_view_pruned": True})
                continue
        result.append(msg)
    return result


# ---------------------------------------------------------------------------
# Phase 2: Orphan exclusion (applied at assembly time)
# ---------------------------------------------------------------------------


def exclude_orphans(messages: list[dict]) -> list[dict]:
    """Exclude tool messages whose tool_call_id doesn't match any assistant's tool_calls.

    Returns a new list with orphans removed. Original messages unchanged.
    """
    # Collect valid tool_call_ids from assistant messages
    valid_ids = set()
    for msg in messages:
        if msg.get("role") == "assistant" and msg.get("tool_calls"):
            tcs = msg["tool_calls"]
            if isinstance(tcs, str):
                try:
                    tcs = json.loads(tcs)
                except json.JSONDecodeError:
                    continue
            if isinstance(tcs, list):
                for tc in tcs:
                    if isinstance(tc, dict):
                        valid_ids.add(tc.get("id", ""))

    result = []
    for msg in messages:
        if msg.get("role") == "tool":
            tcid = msg.get("tool_call_id", "")
            if tcid and tcid not in valid_ids:
                continue  # Orphan — exclude
        result.append(msg)
    return repair_unanswered_tool_calls(result)


ABORTED_CALL_STUB = "Error: tool call aborted before it returned — no result was recorded."


def _tool_call_ids(msg: dict) -> list[str]:
    tcs = msg.get("tool_calls")
    if isinstance(tcs, str):
        try:
            tcs = json.loads(tcs)
        except json.JSONDecodeError:
            return []
    if not isinstance(tcs, list):
        return []
    return [tc.get("id", "") for tc in tcs if isinstance(tc, dict) and tc.get("id")]


def repair_unanswered_tool_calls(messages: list[dict]) -> list[dict]:
    """Give every assistant tool_call a tool row, stubbing the ones that never got one.

    The reverse orphan of `exclude_orphans`: the assistant row with
    `tool_calls` is persisted *before* the round executes, so a round that
    dies mid-flight (cancel during a parallel batch, the executor backstop,
    a DB error while recording results) leaves it with no answers.
    OpenAI-format providers reject that transcript — "assistant message
    with tool_calls must be followed by tool messages" — on every later
    turn until compaction happens to fold the row; with an Ollama fallback
    configured the turn silently ran there instead. The stub is inserted
    after any results the round did record, so ordering stays intact.

    Answers are matched by tool_call_id across the whole list, never by
    adjacency. A tool that writes a message row of its own while it runs
    (view_image stamps a synthetic user note) lands that row BETWEEN the
    assistant row and its own tool result, because the note is persisted
    first and gets the lower id. An adjacency scan read the note as the end
    of the round, called a tool that HAD returned unanswered, and stubbed
    it — and the stub carried no `id`, which the compiler dereferenced
    (KeyError: 'id') before the round's first LLM call. The row order lives
    in the DB, so every later turn in that session died the same way.
    """
    answered = {m.get("tool_call_id") for m in messages if m.get("role") == "tool" and m.get("tool_call_id")}
    out: list[dict] = []
    i, n = 0, len(messages)
    while i < n:
        msg = messages[i]
        out.append(msg)
        i += 1
        if msg.get("role") != "assistant" or not msg.get("tool_calls"):
            continue
        missing = [tcid for tcid in _tool_call_ids(msg) if tcid not in answered]
        if not missing:
            continue
        # Keep whatever results this round did record ahead of the stubs.
        while i < n and messages[i].get("role") == "tool":
            out.append(messages[i])
            i += 1
        for tcid in missing:
            # Carry the assistant's id: a stub has no row of its own, and
            # the compiler stamps `_db_id` from this key for trim notices.
            out.append(
                {
                    "id": msg.get("id"),
                    "role": "tool",
                    "tool_call_id": tcid,
                    "content": ABORTED_CALL_STUB,
                }
            )
    return out


# ---------------------------------------------------------------------------
# Phase 3: LLM summarization (append-only)
# ---------------------------------------------------------------------------

# settings.compaction_keep_tokens (51k) is a global constant that knows
# nothing about the active model. The compiler's history share is roughly the
# context budget minus the output reservation, the system prompt and the tool
# schemas, and it fires compaction at settings.compaction_threshold of that.
# On any model whose window is smaller than ~57k — every Ollama model at the
# default ollama_num_ctx_cap — an unclamped keep_tokens exceeds the entire
# history the compiler will ever hold, the boundary scan below never breaks,
# and the whole live conversation gets folded into a summary.
_KEEP_HISTORY_FRACTION = 0.5
_KEEP_BUDGET_FRACTION = 0.25
_MIN_KEEP_TOKENS = 2_000


def _effective_context_budget(session_id: str) -> int:
    """Context budget for this session, mirroring the agent loop's derivation."""
    try:
        from sessions.manager import get_manager

        session = get_manager().get(session_id)
        override = int(getattr(session, "context_budget_override", 0) or 0) if session else 0
        if override:
            return override
    except Exception as e:
        logger.debug("Session budget override lookup failed for %s: %s", session_id, e)
    try:
        from core.llm.budget import derive_model_budget

        derived = derive_model_budget(settings.llm_model)
        if derived:
            return int(derived)
    except Exception as e:
        logger.debug("Model budget lookup failed: %s", e)
    return int(settings.context_budget)


def _resolve_keep_tokens(session_id: str, history_budget: int | None) -> int:
    """compaction_keep_tokens clamped to what the model can actually hold."""
    configured = int(settings.compaction_keep_tokens)
    if history_budget and history_budget > 0:
        ceiling = int(history_budget * _KEEP_HISTORY_FRACTION)
    else:
        ceiling = int(_effective_context_budget(session_id) * _KEEP_BUDGET_FRACTION)
    return max(min(configured, ceiling), _MIN_KEEP_TOKENS)


def _active_turn_root_index(convo: list[dict]) -> int:
    """Index of the user message that opened the turn currently in flight.

    The compaction boundary must never advance past it. compile_context
    filters history on ``id > compacted_up_to`` *before* its pin can protect
    the active turn's user message, so folding that message leaves the agent
    resuming a live turn with a summary and no idea what it was asked.

    The live root is the last user message that already has a non-user
    message after it. A trailing run of user messages with nothing after them
    is queued for future turns (the compiler hides those behind
    turn_user_msg_id), so the root is the first of that run instead. Both
    readings err toward keeping one turn too many, never one too few.
    Returns -1 when the window holds no user message at all.
    """
    last_nonuser = -1
    for i, msg in enumerate(convo):
        if msg.get("role") != "user":
            last_nonuser = i

    for i in range(last_nonuser - 1, -1, -1):
        if convo[i].get("role") == "user":
            return i

    for i in range(last_nonuser + 1, len(convo)):
        if convo[i].get("role") == "user":
            return i
    return -1


def _clamp_boundary_to_live_turn(convo: list[dict], boundary_idx: int, turn_user_msg_id: int | None) -> int:
    """Never let the boundary advance past the live turn's root.

    When the agent loop knows its turn root (`turn_user_msg_id`), that row
    is authoritative. The positional guess in `_active_turn_root_index`
    stays as the fallback for callers that do not know it (the manual
    /compact endpoint), but on its own it picked an *injected* mid-turn
    user message as the root — `/api/chat/inject` puts one between two
    tool rounds — and folded the real ask and its earlier rounds into the
    summary, so the agent resumed with no verbatim request.
    """
    root_idx = -1
    if turn_user_msg_id is not None:
        for i, m in enumerate(convo):
            if m.get("id") == turn_user_msg_id and m.get("role") == "user":
                root_idx = i
                break
    if root_idx < 0:
        root_idx = _active_turn_root_index(convo)
    return root_idx if 0 <= root_idx < boundary_idx else boundary_idx


# ---------------------------------------------------------------------------
# Coverage-aware serialization
# ---------------------------------------------------------------------------

# What ONE summarizer call may be handed. The slice is split into as many of
# these as it takes; a group past a cutoff is summarized by the next call
# rather than dropped. The old single 60,000-char pass dropped ~70% of every
# steady-state slice and advanced the marker over it anyway.
_CHUNK_CHARS = 60_000
# Per-body clips. Whatever they cut is written down with the row's id — a
# prefix is not the message, and the coverage record has to say so.
_BODY_CLIP_CHARS = 4_000
_TOOL_CLIP_CHARS = 2_000
_ARG_CLIP_CHARS = 400
# A compaction stalls the turn that called it, so bound the calls it may
# make. Groups past the last chunk stay in the live window for the next one.
_MAX_CHUNKS = 8
# Clip pointers named in the stored summary's coverage footer / kept in the
# marker's metadata. The footer is read by the model, the metadata by us.
_FOOTER_CLIPS = 8
_META_CLIPS = 50

# Reasons a compaction did not write a marker. Only FAILED means the
# compactor tried and could not do it; NOTHING_TO_SUMMARIZE means there was
# nothing it was allowed to touch, which no caller should treat as an error.
COMPACTION_PERFORMED = "performed"
COMPACTION_FAILED = "failed"
NOTHING_TO_SUMMARIZE = "nothing_to_summarize"


@dataclass
class CompactionOutcome:
    """Why a compaction run ended the way it did.

    Filled in by `compact_with_llm` when a caller hands one in. The return
    value stays a plain bool — every caller that only asks "did it compact?"
    is unchanged — and this carries the part the agent loop needs: a refusal
    and a failure are both False, and only one of them may end a turn.

    The default is FAILED so an unpopulated outcome (a stub in a test, a
    caller that never passed one) reads as the cautious answer.
    """

    reason: str = COMPACTION_FAILED
    covered: int = 0
    chunks: int = 0


@dataclass
class _Chunk:
    """One summarizer call's worth of COMPLETE message groups."""

    text: str = ""
    msg_ids: list[int] = field(default_factory=list)
    clips: list[dict] = field(default_factory=list)


def _text_of(content) -> str:
    """Message content as text, flattening the vision list form."""
    if isinstance(content, list):
        return " ".join(p.get("text", "") for p in content if isinstance(p, dict))
    return content if isinstance(content, str) else ("" if content is None else str(content))


def _clip(text: str, limit: int, msg_id, role: str, clips: list[dict]) -> str:
    """Clip a body, recording the omission and where to get the rest.

    The point of the record is that the summarizer's view and the boundary
    stop agreeing to disagree: the marker may advance over this row, but
    what the summarizer actually saw of it is written down, with a pointer
    the agent can follow.
    """
    if len(text) <= limit:
        return text
    clips.append({"msg_id": msg_id, "role": role, "kept": limit, "total": len(text)})
    where = f"session_read({msg_id})" if msg_id is not None else "search_sessions(query)"
    return f"{text[:limit]} … [clipped {len(text) - limit} chars — {where} for the full body]"


def _tool_calls_of(msg: dict) -> list[dict]:
    """The assistant row's tool calls, whether stored as JSON text or a list."""
    tcs = msg.get("tool_calls")
    if isinstance(tcs, str):
        try:
            tcs = json.loads(tcs)
        except (json.JSONDecodeError, TypeError):
            return []
    if not isinstance(tcs, list):
        return []
    return [tc for tc in tcs if isinstance(tc, dict)]


def _call_identity(tc: dict) -> str:
    """`bash(command='pytest -q', cwd='/srv')` — tool plus the arguments that
    say what it touched.

    An assistant tool round stores content "" and puts everything in
    `tool_calls`, which the old serializer never read: the whole round
    reached the summarizer as the twelve characters "[assistant] ", and the
    path or command it ran existed nowhere else in the input.
    """
    _id, name, args = extract_tool_call_fields(tc)
    parsed = args
    if isinstance(args, str):
        try:
            parsed = json.loads(args)
        except (json.JSONDecodeError, TypeError, ValueError):
            parsed = {"_raw_arguments": args}
    if not isinstance(parsed, dict):
        parsed = {"_raw_arguments": parsed}
    rendered = []
    for key, value in parsed.items():
        text = value if isinstance(value, str) else json.dumps(value, default=str)
        if len(text) > _ARG_CLIP_CHARS:
            text = text[:_ARG_CLIP_CHARS] + "…"
        rendered.append(f"{key}={text!r}")
    return f"{name or '?'}({', '.join(rendered)})"


def _call_status(tc: dict, results: dict) -> str:
    """Execution status for one proposed call.

    A refused call (`_rejected`, shipped in 8a1cd17) never ran; saying so is
    the difference between a summary that records an attempt and one that
    records a result the harness never produced.
    """
    if is_rejected_call(tc):
        return "REJECTED — never executed"
    row = results.get(tc.get("id", ""))
    if row is None:
        return "no result recorded"
    body = _text_of(row.get("content")).lstrip()
    return "error" if body.startswith(("Error:", "ERROR", "error:")) else "ok"


def _serialize_group(group: list[dict], clips: list[dict]) -> str:
    """Render one atomic group: its rows, its tool identities, its ids."""
    head = group[0]
    calls = _tool_calls_of(head) if head.get("role") == "assistant" else []
    names = {tc.get("id", ""): extract_tool_call_fields(tc)[1] for tc in calls}
    results = {m.get("tool_call_id", ""): m for m in group if m.get("role") == "tool"}

    lines: list[str] = []
    for msg in group:
        role = msg.get("role", "")
        mid = msg.get("id")
        tag = f"#{mid}" if mid is not None else "#?"
        body = _text_of(msg.get("content"))
        if msg is head and calls:
            if body.strip():
                lines.append(f"[assistant {tag}] {_clip(body, _BODY_CLIP_CHARS, mid, role, clips)}")
            for tc in calls:
                lines.append(f"[tool_call {tag}] {_call_identity(tc)} -> {_call_status(tc, results)}")
        elif role == "tool":
            name = names.get(msg.get("tool_call_id", "")) or "tool"
            lines.append(f"[tool_result {tag} {name}] {_clip(body, _TOOL_CLIP_CHARS, mid, role, clips)}")
        else:
            lines.append(f"[{role} {tag}] {_clip(body, _BODY_CLIP_CHARS, mid, role, clips)}")
    return "\n".join(lines)


def _group_messages(messages: list[dict]) -> list[list[dict]]:
    """Split the slice into atomic units a chunk boundary may not fall inside.

    An assistant row that made tool calls travels with the tool rows that
    answered it — the `_rejected` stubs from 8a1cd17 included, since a
    rejection without its parent reads as work that ran.
    """
    groups: list[list[dict]] = []
    i, n = 0, len(messages)
    while i < n:
        msg = messages[i]
        group = [msg]
        i += 1
        if msg.get("role") == "assistant" and _tool_calls_of(msg):
            while i < n and messages[i].get("role") == "tool":
                group.append(messages[i])
                i += 1
        groups.append(group)
    return groups


def _chunk_slice(
    messages: list[dict],
    chunk_chars: int = _CHUNK_CHARS,
    max_chunks: int = _MAX_CHUNKS,
) -> list[_Chunk]:
    """Serialize the slice into bounded chunks of complete groups.

    Returns the chunks in transcript order. Each carries the ids it
    represents and the clips it had to make, so the caller can advance the
    boundary over exactly what a summarizer accepted and no further.
    """
    chunks: list[_Chunk] = []
    current = _Chunk()
    for group in _group_messages(messages):
        clips: list[dict] = []
        text = _serialize_group(group, clips)
        ids = [m["id"] for m in group if m.get("id") is not None]
        if current.text and len(current.text) + len(text) + 1 > chunk_chars:
            chunks.append(current)
            if len(chunks) >= max_chunks:
                return chunks
            current = _Chunk()
        current.text = f"{current.text}\n{text}" if current.text else text
        current.msg_ids.extend(ids)
        current.clips.extend(clips)
    if current.text:
        chunks.append(current)
    return chunks[:max_chunks]


def _coverage_footer(covered: list[int], clips: list[dict], chunks_used: int, groups_left: int) -> str:
    """What the summary does and does not stand for, in the summary itself.

    The marker's metadata holds the authoritative record; this is the part
    the model reads, so a clipped body stays reachable from the context the
    agent is actually holding.
    """
    if not covered:
        return ""
    lines = [
        f"\n\n[Coverage] Summarized msgs {min(covered)}–{max(covered)} "
        f"({len(covered)} messages) in {chunks_used} summarizer call(s)."
    ]
    if clips:
        shown = ", ".join(f"{c['msg_id']} ({c['kept']} of {c['total']} chars)" for c in clips[:_FOOTER_CLIPS])
        more = f", +{len(clips) - _FOOTER_CLIPS} more" if len(clips) > _FOOTER_CLIPS else ""
        lines.append(f"Bodies clipped before summarizing — session_read(msg_id) for the full text: {shown}{more}.")
    if groups_left:
        lines.append(f"{groups_left} later message group(s) were NOT summarized and remain in the live window.")
    return "\n".join(lines)


async def compact_with_llm(
    session_id: str,
    messages: list[dict],
    existing_summary: str | None = None,
    history_budget: int | None = None,
    turn_user_msg_id: int | None = None,
    outcome: CompactionOutcome | None = None,
) -> bool:
    """Run LLM summarization and append compaction marker. Never deletes messages.

    Returns True if compaction was performed. Pass an `outcome` to learn WHY
    a False came back — a refusal with nothing to summarize is not a failure.

    The boundary this records never runs ahead of what a summarizer actually
    accepted: rows are serialized in complete groups, chunked across as many
    calls as the slice needs, and `compacted_up_to` stops at the first row no
    accepted chunk covered.
    """
    outcome = outcome if outcome is not None else CompactionOutcome()
    from core.llm.client import get_llm_client

    estimator = get_estimator()

    # The compiled `messages` handed in by the agent have been stripped of their
    # DB ids (_strip_private_fields in compile_context), so we CANNOT derive the
    # compaction boundary from them — doing so recorded compacted_up_to=0 on
    # every run, which never advances the compiler's active-window pointer and
    # drives an unbounded re-compaction loop. Read the authoritative rows from
    # the DB, which carry real `id`s. `messages` is retained only for signature
    # back-compat and is intentionally unused for boundary/id resolution.
    _ = messages
    # Off-loop: the full transcript, tool results and all. compact_with_llm is
    # awaited directly by the agent loop, so an inline read here stalls every
    # other session's SSE for as long as the query takes.
    raw = await asyncio.to_thread(db.get_messages, session_id)

    # Resume from the most recent compaction marker: only summarize messages
    # added since it, carry its summary forward for merging, and never rewind
    # the pointer (which would re-summarize already-folded history).
    prev_compacted_up_to = 0
    if existing_summary is None:
        for m in reversed(raw):
            if m["role"] == "compaction":
                existing_summary = m["content"]
                try:
                    raw_meta = m.get("metadata") or m.get("tool_calls") or "{}"
                    prev_compacted_up_to = int(json.loads(raw_meta).get("compacted_up_to", 0))
                except (json.JSONDecodeError, TypeError, ValueError):
                    prev_compacted_up_to = 0
                break

    # Conversational messages not yet folded into a summary, oldest -> newest.
    convo = [m for m in raw if m["role"] not in _MARKER_ROLES and m["id"] > prev_compacted_up_to]

    # Find compaction boundary: keep recent messages totaling keep_tokens
    keep_tokens = _resolve_keep_tokens(session_id, history_budget)
    total = 0
    boundary_idx = len(convo)
    for i in range(len(convo) - 1, -1, -1):
        tokens = estimator.count_message(convo[i])
        if total + tokens > keep_tokens:
            boundary_idx = i + 1
            break
        total += tokens

    # Hard floor: whatever the token arithmetic says, the live turn stays.
    clamped = _clamp_boundary_to_live_turn(convo, boundary_idx, turn_user_msg_id)
    if clamped != boundary_idx:
        logger.info(
            "Compaction boundary clamped from %d to %d to preserve the active turn (session %s)",
            boundary_idx,
            clamped,
            session_id,
        )
        boundary_idx = clamped

    to_summarize = convo[:boundary_idx]
    if len(to_summarize) < 4:
        # Not a failure: there is nothing older than the live turn that this
        # compactor is permitted to fold. Callers must not read this as the
        # compactor having tried and lost (see _CompactionController).
        logger.debug("Nothing compaction may summarize (%d row(s) behind the live turn)", len(to_summarize))
        outcome.reason = NOTHING_TO_SUMMARIZE
        return False

    all_groups = _group_messages(to_summarize)
    chunks = _chunk_slice(to_summarize)
    if not chunks:
        outcome.reason = NOTHING_TO_SUMMARIZE
        return False

    # Call LLM. The agent loop synchronously awaits this call mid-turn, so it
    # must queue with the session's own scheduling identity — the default
    # background identity sorts last in the fair queue (priority inversion).
    from core.llm.client import chat_with_backup, ensure_session_budget, sched_identity

    client = get_llm_client()
    # Criticality tier (audit P3): the summary becomes the session's
    # permanent memory for the primary model — never author it on a
    # silently-weaker background model.
    model = settings.llm_model or settings.background_model
    sched_created_at, sched_priority = sched_identity(session_id)

    # One call per chunk, each one updating the running summary so prior
    # continuity survives across chunks as well as across compactions. A
    # chunk is only "covered" once its own summary passed the gates below;
    # the loop stops at the first one that did not, and the boundary stops
    # with it.
    summary = existing_summary or ""
    covered: list[int] = []
    clips: list[dict] = []
    chunks_used = 0
    for index, chunk in enumerate(chunks):
        if summary:
            prompt = COMPACTION_UPDATE_PROMPT.format(existing_summary=summary, new_content=chunk.text)
        else:
            prompt = COMPACTION_PROMPT + "\n\nCONVERSATION:\n" + chunk.text
        # Carrying the session_id subjects this call to the session's
        # wall-clock budget; guarantee headroom so a budget-exhausted turn
        # can still compact instead of dying with compaction_failed.
        ensure_session_budget(session_id, 120)
        try:
            response = await chat_with_backup(
                client,
                messages=[
                    {"role": "system", "content": "You are a conversation summarizer."},
                    {"role": "user", "content": prompt},
                ],
                model=model,
                max_tokens=4000,
                session_id=session_id,
                session_created_at=sched_created_at,
                session_priority=sched_priority,
            )
            candidate = response.content.strip()
        except Exception as e:
            logger.error("Compaction LLM call failed on chunk %d/%d: %s", index + 1, len(chunks), e)
            break

        # Quality gates, measured against the input this call was actually
        # given. The old gate divided by the whole slice — including the ~70%
        # the summarizer never saw — which made it 4.8x too lenient to ever
        # catch a bloated summary of a small prefix.
        before_tokens = estimator.count(summary) if summary else 0
        candidate_tokens = estimator.count(candidate)
        if candidate_tokens < 20:
            logger.warning("Compaction summary too short (%d tokens), chunk %d rejected", candidate_tokens, index + 1)
            break
        supplied_tokens = estimator.count(chunk.text)
        growth = candidate_tokens - before_tokens
        if supplied_tokens > 0 and growth > 0 and growth / supplied_tokens > 0.35:
            logger.warning(
                "Compaction chunk %d has poor compression (%.0f%% of the input supplied), rejected",
                index + 1,
                growth / supplied_tokens * 100,
            )
            break

        if candidate_tokens > 5000:
            candidate = candidate[:20000]  # ~5000 tokens
            logger.warning("Compaction summary truncated to ~5000 tokens")
        summary = candidate
        covered.extend(chunk.msg_ids)
        clips.extend(chunk.clips)
        chunks_used += 1

    if not covered:
        outcome.reason = COMPACTION_FAILED
        return False

    # Advance across covered rows ONLY. Walking the slice in order and
    # stopping at the first uncovered row is what makes a mid-slice chunk
    # failure safe: everything from there on stays in the live window.
    covered_set = set(covered)
    last_summarized_id = 0
    original_count = 0
    for m in to_summarize:
        mid = m.get("id")
        if mid is None or mid not in covered_set:
            break
        last_summarized_id = mid
        original_count += 1
    if last_summarized_id <= prev_compacted_up_to:
        logger.warning("Compaction covered nothing new (boundary would not advance), rejected")
        outcome.reason = COMPACTION_FAILED
        return False

    covered_prefix = [m["id"] for m in to_summarize[:original_count]]
    kept_clips = [c for c in clips if c.get("msg_id") in covered_set]
    groups_left = len(all_groups) - len(_group_messages(to_summarize[:original_count]))
    summary += _coverage_footer(covered_prefix, kept_clips, chunks_used, groups_left)
    summary_tokens = estimator.count(summary)

    # Append compaction marker (NEVER delete original messages). The coverage
    # record travels with it: what was summarized, in how many calls, and
    # every body that reached the summarizer clipped.
    await asyncio.to_thread(
        db.add_compaction,
        session_id=session_id,
        summary=summary,
        compacted_up_to=last_summarized_id,
        original_count=original_count,
        coverage={
            "covered_from": covered_prefix[0] if covered_prefix else 0,
            "covered_to": last_summarized_id,
            "covered_count": original_count,
            "chunks_used": chunks_used,
            "chunks_planned": len(chunks),
            "groups_deferred": groups_left,
            "clipped": kept_clips[:_META_CLIPS],
            "clipped_total": len(kept_clips),
        },
    )

    logger.info(
        "Compaction complete: summarized %d of %d messages in %d call(s) (%d tokens, %d clipped bodies)",
        original_count,
        len(to_summarize),
        chunks_used,
        summary_tokens,
        len(kept_clips),
    )

    try:
        from sessions.manager import get_manager

        session = get_manager().get(session_id)
        if session:
            session.emit_event(
                {
                    "type": "context.compacted",
                    "summarized_messages": original_count,
                    "summary_tokens": summary_tokens,
                    "chunks": chunks_used,
                    "deferred_groups": groups_left,
                }
            )
    except Exception as e:
        logger.debug("context.compacted emit skipped: %s", e)

    outcome.reason = COMPACTION_PERFORMED
    outcome.covered = original_count
    outcome.chunks = chunks_used
    return True
