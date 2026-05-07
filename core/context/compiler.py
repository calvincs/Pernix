"""Pernix — Context compiler: assembles Layer 1 (Working Context) from layers 2-4.

Implements append-only compaction (view transforms), prompt cache preservation,
and provider normalization.
"""

from __future__ import annotations

import base64
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from config import settings
from core.context.compaction import apply_view_pruning, exclude_orphans
from core.context.tokens import get_estimator
from core.llm.types import extract_tool_call_fields
from db import models as db

logger = logging.getLogger("pernix.context.compiler")

# --- Attachment expansion (compile-time, per-turn) ---------------------------

_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
_MIME_MAP = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
}
_ATTACHED_RE = re.compile(r"\[attached:\s*([^\]\s]+(?:\s+[^\]]*)?)\]")

# Upper bound on the total byte size of image data we'll inline into a
# single compile. Beyond this, we drop the oldest images first and fall
# back to text markers. Settings-overridable via max_inline_attach_bytes.
MAX_INLINE_ATTACH_BYTES = 4 * 1024 * 1024


def _extract_attached_filenames(text: str) -> list[str]:
    """Return the filenames referenced by [attached: ...] markers in text."""
    out: list[str] = []
    for m in _ATTACHED_RE.findall(text):
        # The first whitespace-separated token is the filename; the rest
        # may be an annotation like " — text at foo.pdf.txt".
        name = m.split()[0].strip().rstrip(",")
        out.append(name)
    return out


def _expand_user_message_with_images(text: str, budget: int) -> tuple[list[dict], int]:
    """Expand [attached: image] references in `text` into multimodal blocks.

    Reads image bytes fresh from workspace each call — nothing inlined
    sits in the DB. Honors a byte budget; when exceeded, remaining images
    are left as text markers.

    Returns (content_blocks, bytes_spent).
    """
    from core.tools.paths import safe_read_path

    filenames = _extract_attached_filenames(text)
    if not filenames:
        return [{"type": "text", "text": text}], 0

    blocks: list[dict] = [{"type": "text", "text": text}]
    spent = 0
    for fname in filenames:
        ext = Path(fname).suffix.lower()
        if ext not in _IMAGE_EXTENSIONS:
            continue
        # Reject path traversal — `[attached: ../../etc/some.jpg]` must not
        # read outside the allowed roots even if the extension gate passes.
        try:
            img = safe_read_path(fname)
        except ValueError as e:
            logger.warning("attachment rejected (path): %s — %s", fname, e)
            continue
        if not img.exists() or not img.is_file():
            continue
        try:
            data = img.read_bytes()
        except OSError as e:
            logger.warning("attachment read failed for %s: %s", img, e)
            continue
        # ~33% overhead for base64. Check against remaining budget.
        projected = int(len(data) * 1.34)
        if spent + projected > budget:
            logger.info(
                "attachment %s (%d bytes) exceeds inline budget (%d/%d used); " "leaving as text marker",
                fname,
                len(data),
                spent,
                budget,
            )
            continue
        b64 = base64.b64encode(data).decode()
        mime = _MIME_MAP.get(ext, "image/jpeg")
        blocks.append(
            {
                "type": "image_url",
                "image_url": {"url": f"data:{mime};base64,{b64}"},
                "_filename": fname,
            }
        )
        spent += len(b64)
    return blocks, spent


def _legacy_multimodal_to_text(content_str: str) -> str:
    """Collapse a legacy JSON-serialized multimodal blob back to plain text.

    Pre-fix stored rows inlined base64 into messages.content. We still
    encounter them on resume. This reverses the inlining for history use:
    text blocks are concatenated, image_url blocks become [image: name].
    Returns the original string if it isn't a recognizable multimodal list.
    """
    # Match `[ optional-whitespace {` anywhere at the start — otherwise a
    # legacy row that starts with `"[ {"` (space after bracket) falls
    # through the prefix gate and leaves base64 inlined in content, blowing
    # the context budget. Same failure mode commit 2b43137 was supposed to fix.
    if not re.match(r"\s*\[\s*\{", content_str):
        return content_str
    try:
        parsed = json.loads(content_str)
    except (json.JSONDecodeError, TypeError):
        return content_str
    if not isinstance(parsed, list):
        return content_str
    parts: list[str] = []
    for block in parsed:
        if not isinstance(block, dict):
            parts.append(str(block))
        elif block.get("type") == "text":
            parts.append(block.get("text", ""))
        elif block.get("type") == "image_url":
            name = block.get("_filename", "image")
            parts.append(f"[image: {name}]")
    return "\n".join(parts)


# Fixed base system prompt — stable across ALL turns (prompt cache friendly)
BASE_SYSTEM_PROMPT = """You are Pernix, a headless AI agent. You execute tasks through tool use.
State lives on disk (files, memory), not in context.
You can be interrupted and resumed at any time.

Before acting, THINK about what you need:
- What tools might help? Use discover_tools to find them.
- What do you already know? Use recall to check memory.
- What instructions apply? Check SESSIONS.md if it exists.
- What has been done already? Check workspace files with glob or file_read.
- What skills exist? Use discover_skills to find domain expertise.

All files go in the workspace, organized by project folder.
- file_read/file_write/file_edit: paths relative to workspace root (e.g. "myproject/app.html")
- glob: find files by pattern (e.g. "**/*.html")
- bash: CWD is already the workspace root — use "myproject/file" not "workspace/myproject/file"

IMPORTANT: Tool output may be TRUNCATED if it exceeds the size limit. When you see
"⚠ TRUNCATED" or "⚠ N lines remaining", you are missing data.
The hint will say: Continue with: file_read(path="...", offset=N, limit=200)
Use those offset and limit values EXACTLY. Do NOT invent your own pagination
(no limit=1 to "probe", no random offsets). Do not assume partial output is complete.

Core tools: file_read, file_write, file_edit, glob, grep, bash, remember, recall,
ask_user, discover_tools, get_tool_schema, discover_skills, load_skill,
read_skill_resource.

ASKING THE USER A QUESTION: When you need a decision before continuing —
confirmation before a destructive or large action, a yes/no, an A/B choice,
or any clarification — call the `ask_user` tool. It pauses the session
(state → AWAITING_USER) and routes the answer back to you on the next turn.
Do NOT just write a question into your prose response: that ends the turn
without pausing, the user's reply lands as a fresh prompt rather than an
answer, and any follow-up they send while you're finishing post-hooks may
race with reflect and contaminate the next turn. If you're going to ask,
use the tool.

EXPLICIT INSTRUCTIONS ARE BINDING. When the user names a specific tool,
model, worker, file path, command, or technique to use, that's a contract —
not a suggestion. Execute it before you reach for an alternative, even if
you believe your own approach would be better. Substituting your own
judgement for a specific instruction is a recurring failure mode that
Reflect will catch. If you genuinely cannot follow the instruction (tool
not registered, file missing, syntax invalid, model unavailable), call
`ask_user` with a concrete question describing what blocks you — do not
silently switch approaches.

DO NOT FIX TESTS BY DELETING THEM. If a test is failing, fix the code
under test. Removing assertions, lowering expected values, or skipping
cases to make the runner exit 0 is a failure, not a fix — Reflect grades
your output against the user's spec, not just your runner's exit code.
The same rule applies to weakening any user-stated acceptance criterion.

VERIFY THROUGH THE REAL INTERFACE. Before declaring a task done, exercise
the deliverable the way an external user would, not the way you assembled
it. If you produced a script, run it from a clean shell as `bash ./script`
rather than re-running its inner commands. If you produced a module,
import it from a fresh process. If you produced a CLI, invoke it with the
args the user named. Wiring bugs — wrong shebang, missing imports,
hardcoded paths, environment assumptions — live at this seam, and
component-level testing while you iterate won't catch them.

ATTACHMENTS: When a user message contains [attached: filename], the file has been
uploaded to the workspace. PDFs are auto-extracted to a sidecar
[attached: foo.pdf — text at foo.pdf.txt] — read the .txt file for contents.
Any attached file can be accessed via file_read("filename"). Image-specific
handling depends on your model's vision capability — see [ACTIVE MODEL] below.

For any other capability, use discover_tools to find it.

SKILLS vs WORKFLOWS — these are different things, do not confuse them:
- SKILL = capability package in data/skills/. To use one: load_skill(name)
  and follow the instructions inside. Skills do NOT need validation.
- WORKFLOW = multi-step pipeline in data/workflows/. To use one:
  discover_workflows + run_workflow. To check one: validate_workflow.
NEVER call validate_workflow on a skill name — it will return "not found".

Reusable multi-step pipelines — WORKFLOWS:
- To CREATE a pipeline: get_workflow_schema() → create_workflow(name, content).
  Never use file_write to create workflows (wrong location, not registered).
- To EXECUTE a workflow: when the user asks to run / execute / trigger / kick off
  a named workflow, FIRST call discover_workflows(query) to confirm it exists,
  THEN call run_workflow(name, inputs). You MUST NOT replay the workflow's steps
  inline in your own context — run_workflow spawns a dedicated worker per wave
  specifically to keep this session's context clean. Replaying steps inline is a
  correctness bug, not a shortcut."""


# Conditional block — appended to the base prompt only when settings.eval_auto is True.
# When auto-eval is OFF this is omitted entirely, so the model isn't biased toward
# calling add_feature. Skip-first ordering: the most common over-trigger is treating
# operational requests as tracked deliverables, so we lead with the rule that prevents that.
_AUTO_EVAL_BLOCK = """ACCEPTANCE-CRITERIA FLOW (auto-eval is ON for this server).

DO NOT use add_feature for operational requests. If the user asked you to:
fetch, download, scrape, transcribe, summarize, translate, run, deploy,
restart, install, look up, find, search, list, or show something — SKIP this
flow entirely. Just deliver the result. Calling add_feature for these creates
registry noise and triggers an unneeded auto-eval round that grades you on
criteria you just made up.

USE this flow ONLY when ALL three are true:
- User asked you to BUILD or IMPLEMENT a non-trivial artifact
  (code, document, report).
- Success is subjective ("idiomatic", "clean", "handles edge cases gracefully").
- User did NOT give concrete tests like "returns 'X' for input Y" — if they
  did, just run the test inline with bash; that's faster and zero-cost.

How to use:
1. add_feature(title, description, criteria) BEFORE you start implementing.
   Each line of `criteria` is one judgeable condition.
2. Implement and iterate as usual.
3. After your turn ends, an LLM judge scores each criterion automatically
   against workspace files + your messages. Failed criteria may trigger a retry.

NEVER call add_feature after the work is done. The flow is for setting
expectations up-front, not for self-grading what you already produced."""


_BASE_SYSTEM_PROMPT_TAIL = """RESOURCE MANAGEMENT: The [RESOURCE STATUS] section shows your remaining tool rounds
and token usage. If rounds are low, prioritize completing the core deliverable over
gathering more context. You can delegate data-heavy subtasks (web browsing, bulk
processing) to workers via spawn_worker to preserve your tool budget for synthesis
and user-facing output."""


def _build_base_system_prompt() -> str:
    """Assemble the base system prompt, including the auto-eval block only when
    settings.eval_auto is True. Keeping the block conditional avoids biasing the
    model toward add_feature on operational requests when the feature isn't even
    active server-side.
    """
    parts = [BASE_SYSTEM_PROMPT]
    if settings.eval_auto:
        parts.append(_AUTO_EVAL_BLOCK)
    parts.append(_BASE_SYSTEM_PROMPT_TAIL)
    return "\n\n".join(parts)


def _build_model_capability_block(model_name: str, supports_vision: bool) -> str:
    """Tell the agent what model it is and whether vision is active this turn.

    Self-awareness fixes a failure mode where a multimodal agent delegated image
    analysis to call_model because it couldn't tell whether images in the current
    turn were already inlined for it.
    """
    name = model_name or "(unknown)"
    lines = ["[ACTIVE MODEL]", f"Model: {name}"]
    if supports_vision:
        lines.append("Vision: ENABLED for this turn.")
        lines.append(
            "Images marked [attached: foo.png] in THIS turn's user message are "
            "already inlined as vision blocks — analyze them directly. Use "
            "call_model(image_path=...) ONLY for prior-turn images that need "
            "re-examining (they appear as text markers in history to keep replay cheap)."
        )
    else:
        lines.append("Vision: DISABLED for your current model.")
        lines.append(
            "You cannot view attached images directly. For any image analysis, "
            "use call_model(model=<vision-capable>, image_path=...) — pick a "
            "vision model from discover_tools/list or the model registry."
        )
    return "\n".join(lines)


def _build_server_context() -> str:
    """Tell the agent how to access the running server and workspace files via URL."""
    scheme = "https" if settings.network_enabled else "http"
    port = settings.port
    base_url = f"{scheme}://localhost:{port}"
    return "\n".join(
        [
            "[SERVER CONTEXT]",
            f"Base URL: {base_url}",
            f"Workspace files are served at: {base_url}/workspace/{{path}}",
            "",
            'When you create a file in the workspace (e.g. "myproject/app.html"), you',
            f"can open or examine it in a browser at: {base_url}/workspace/myproject/app.html",
            "Substitute the actual relative path for any artifact you want to verify or share.",
        ]
    )


def _build_available_skills_block(max_skills: int = 24, desc_chars: int = 180) -> str:
    """List every ENABLED registered skill (name + 1-line description) so the agent
    sees its full skill catalog on every turn.

    Why a fixed catalog: scout's NLP recall fails when a user's prompt shares
    no tokens with a skill's description. A skill (e.g. crawl4ai-fetch) that
    becomes relevant only mid-turn — after the agent hits bot-detection or an
    SSRF block — never surfaces in scout's recommendations because the user's
    original prompt didn't say "Cloudflare" or "403". Listing the catalog
    here is cache-stable across turns and costs ~50 tokens.

    Disabled skills are filtered out — if the user toggled a skill off in
    Explorer, the agent should not see it as a candidate to load_skill on.
    Otherwise the model burns a round calling load_skill('foo') and getting
    back the disabled-error reply.
    """
    try:
        from core.skills.registry import get_skill_registry

        reg = get_skill_registry()
        skills = sorted(reg.enabled_skills(), key=lambda s: s.name)
    except Exception:
        return ""
    if not skills:
        return ""
    lines = [
        "[AVAILABLE SKILLS]",
        "Domain expertise packages. Use load_skill(name) to load full instructions.",
    ]
    for s in skills[:max_skills]:
        desc = (s.description or "").strip().replace("\n", " ")
        if len(desc) > desc_chars:
            desc = desc[: desc_chars - 1].rstrip() + "…"
        lines.append(f"- {s.name} — {desc}")
    return "\n".join(lines)


def _build_temporal_context() -> str:
    """Build temporal context section with current time and birthdate."""
    import re as _re

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    birthdate = ""
    try:
        soul = Path("data/agent/SOUL.md")
        if soul.exists():
            m = _re.search(r"<!-- @birthdate:\s*(.+?)\s*-->", soul.read_text())
            if m:
                dt = datetime.fromisoformat(m.group(1))
                birthdate = dt.strftime("%Y-%m-%d %H:%M:%S UTC")
    except Exception:
        pass

    lines = [
        "[TEMPORAL CONTEXT]",
        f"Current time: {now}",
    ]
    if birthdate:
        lines.append(f"Agent birthdate: {birthdate}")
    lines.append('To get an updated timestamp, use bash("date -Iseconds").')
    return "\n".join(lines)


@dataclass
class ContextPayload:
    """Fully assembled context ready for an LLM call."""

    messages: list[dict]
    tools: list[dict] | None
    token_count: int
    history_budget: int
    needs_compaction: bool
    has_compaction_summary: bool
    metadata: ContextMetadata


@dataclass
class ContextMetadata:
    """Diagnostic info about context assembly."""

    system_tokens: int
    history_tokens: int
    tool_schema_tokens: int
    messages_included: int
    messages_trimmed: int


def compile_context(
    session_id: str,
    tool_schemas: list[dict] | None = None,
    scout_report_text: str = "",
    resource_status: str = "",
    supports_vision: bool = True,
    context_budget: int | None = None,
    model_name: str = "",
    turn_user_msg_id: int | None = None,
) -> ContextPayload:
    """Assemble Layer 1 (Working Context) from session history + scout report.

    Order (prompt cache preserving):
    1. FIXED PREFIX: base system prompt (stable across turns)
    2. SCOUT SECTION: curated identity/rules/memory/approach (varies per turn)
    3. VOLATILE TAIL: resource status (changes per call)
    Then: compaction summary (if exists) + message history + tool schemas.
    """
    estimator = get_estimator()

    # --- Build system prompt ---
    system_parts = [_build_base_system_prompt()]

    # Model + vision capability (stable within a session; tells the agent
    # whether images are inlined for it or must be delegated to call_model).
    system_parts.append(_build_model_capability_block(model_name, supports_vision))

    # Server URL — lets the agent open or examine workspace artifacts in a browser.
    system_parts.append(_build_server_context())

    # Static skill catalog — cache-stable across turns; placed before the
    # per-turn scout report so prompt cache hits the same prefix every turn.
    skills_block = _build_available_skills_block()
    if skills_block:
        system_parts.append(skills_block)

    # Temporal context (current time + birthdate)
    system_parts.append(_build_temporal_context())

    if scout_report_text:
        system_parts.append(scout_report_text)
    if resource_status:
        system_parts.append(resource_status)
    system_prompt = "\n\n".join(system_parts)
    system_tokens = estimator.count(system_prompt)

    # --- Tool schema tokens ---
    tool_tokens = estimator.count_tool_schemas(tool_schemas) if tool_schemas else 0

    # --- Calculate history budget ---
    budget = context_budget if context_budget is not None else settings.context_budget
    max_output = settings.max_tokens
    safety_margin = 2000
    history_budget = max(
        budget - max_output - system_tokens - tool_tokens - safety_margin,
        4000,
    )

    # --- Load messages from DB ---
    raw_messages = db.get_messages(session_id)

    # Find compaction boundary
    compaction_summary = None
    compacted_up_to = 0
    has_compaction = False
    for msg in reversed(raw_messages):
        if msg["role"] == "compaction":
            compaction_summary = msg["content"]
            has_compaction = True
            try:
                # Read from metadata column (v5+), fall back to tool_calls (pre-v5)
                raw_meta = msg.get("metadata") or msg.get("tool_calls") or "{}"
                meta = json.loads(raw_meta)
                compacted_up_to = int(meta.get("compacted_up_to", 0))
            except (json.JSONDecodeError, TypeError, ValueError):
                pass
            break

    # Validate compacted_up_to (prevent corruption from wiping all messages)
    if compacted_up_to > 0 and raw_messages:
        max_id = max(m["id"] for m in raw_messages)
        if compacted_up_to > max_id:
            logger.warning("compacted_up_to (%d) exceeds max message ID (%d), resetting", compacted_up_to, max_id)
            compacted_up_to = 0

    # Filter to post-compaction messages (exclude compaction markers, scout
    # metadata, and audit-only notices like cancellation markers).
    # When turn_user_msg_id is provided, also hide user messages that landed
    # AFTER this turn's user message — those are queued for future turns and
    # the agent shouldn't pre-answer them. EXCEPTION: messages tagged with
    # metadata.injected=true are explicitly delivered via /api/chat/inject and
    # MUST be visible to the running turn (that is the whole point of inject).
    def _is_injected(m: dict) -> bool:
        meta_raw = m.get("metadata")
        if not meta_raw:
            return False
        try:
            meta = json.loads(meta_raw) if isinstance(meta_raw, str) else meta_raw
            return bool(meta.get("injected")) if isinstance(meta, dict) else False
        except (json.JSONDecodeError, TypeError, ValueError):
            return False

    history = [
        m
        for m in raw_messages
        if m["role"] not in ("compaction", "scout", "notice", "reflect", "model_divider", "eval")
        and m["id"] > compacted_up_to
        and not (
            turn_user_msg_id is not None and m["role"] == "user" and m["id"] > turn_user_msg_id and not _is_injected(m)
        )
    ]

    # Reorder by logical turn group. Each non-user message tagged with
    # parent_user_msg_id in metadata belongs with that user. Without this,
    # raw id-sort puts queued user messages BEFORE the prior turn's
    # assistant (because the assistant gets its id later than the queued
    # user), making the conversation look already-answered to the LLM and
    # suppressing the new turn's response.
    def _parent_id(msg: dict) -> int:
        if msg["role"] == "user":
            return msg["id"]
        meta_raw = msg.get("metadata") or msg.get("tool_calls")  # tool_calls held metadata pre-v5
        if not meta_raw:
            return msg["id"]
        try:
            meta = json.loads(meta_raw) if isinstance(meta_raw, str) else meta_raw
            pid = meta.get("parent_user_msg_id") if isinstance(meta, dict) else None
            return int(pid) if pid is not None else msg["id"]
        except (json.JSONDecodeError, TypeError, ValueError):
            return msg["id"]

    history.sort(key=lambda m: (_parent_id(m), m["id"]))

    # --- Apply view transforms (NEVER modifies DB) ---
    history = apply_view_pruning(history)
    history = exclude_orphans(history)

    # --- Build final message list ---
    messages = [{"role": "system", "content": system_prompt}]

    if compaction_summary:
        messages.append(
            {
                "role": "system",
                "content": f"[Previous conversation summary]\n{compaction_summary}",
                "_pinned": True,  # Protect from Phase C trimming
            }
        )

    # Find the index of the last user message in history — only that
    # turn gets image blobs expanded (vision models) to keep replay cheap.
    last_user_idx = -1
    for idx in range(len(history) - 1, -1, -1):
        if history[idx]["role"] == "user":
            last_user_idx = idx
            break

    attach_budget = int(
        getattr(settings, "max_inline_attach_bytes", MAX_INLINE_ATTACH_BYTES) or MAX_INLINE_ATTACH_BYTES
    )

    # Convert DB rows to chat format
    for idx, msg in enumerate(history):
        raw_content = msg["content"] or ""
        # Legacy rows (pre-ingest-fix): collapse inlined base64 back to text
        # markers. Current ingest stores plain text only.
        text_form = _legacy_multimodal_to_text(raw_content)
        content: str | list[dict] = text_form

        is_latest_user = idx == last_user_idx and msg["role"] == "user"
        if is_latest_user and supports_vision:
            blocks, _spent = _expand_user_message_with_images(text_form, attach_budget)
            # Only wrap in a list if we actually produced image blocks.
            if any(b.get("type") == "image_url" for b in blocks):
                content = blocks

        # Non-vision models see text only (already true for legacy rows
        # via _legacy_multimodal_to_text; nothing more to do).

        # Clean internal metadata from multimodal blocks before sending to API
        if isinstance(content, list):
            content = [
                {k: v for k, v in block.items() if not k.startswith("_")} if isinstance(block, dict) else block
                for block in content
            ]
        entry = {"role": msg["role"], "content": content}
        if msg.get("tool_call_id") and msg["tool_call_id"] != "None":
            entry["tool_call_id"] = msg["tool_call_id"]
        if msg.get("tool_calls") and msg["role"] == "assistant":
            try:
                raw_tcs = json.loads(msg["tool_calls"])
                # Normalize to OpenAI format
                openai_tcs = []
                for tc in (raw_tcs if isinstance(raw_tcs, list) else []):
                    openai_tcs.append(
                        {
                            "id": tc.get("id", ""),
                            "type": "function",
                            "function": {
                                "name": tc.get("name", tc.get("function", {}).get("name", "")),
                                "arguments": tc.get("arguments", tc.get("function", {}).get("arguments", "{}")),
                            },
                        }
                    )
                if openai_tcs:
                    entry["tool_calls"] = openai_tcs
            except (json.JSONDecodeError, TypeError):
                pass
        messages.append(entry)

    # --- Trim to budget ---
    history_tokens = sum(
        (msg.get("token_count") or estimator.count_message(msg)) for msg in messages[1:]  # skip system
    )
    messages_trimmed = 0
    if history_tokens > history_budget:
        messages, messages_trimmed = _trim_history(messages, history_budget, estimator)
        history_tokens = sum(estimator.count_message(m) for m in messages[1:])

    # --- Check if compaction needed ---
    total_tokens = system_tokens + history_tokens + tool_tokens
    needs_compaction = history_tokens > int(history_budget * settings.compaction_threshold)

    return ContextPayload(
        messages=messages,
        tools=tool_schemas,
        token_count=total_tokens,
        history_budget=history_budget,
        needs_compaction=needs_compaction,
        has_compaction_summary=has_compaction,
        metadata=ContextMetadata(
            system_tokens=system_tokens,
            history_tokens=history_tokens,
            tool_schema_tokens=tool_tokens,
            messages_included=len(messages) - 1,
            messages_trimmed=messages_trimmed,
        ),
    )


# ---------------------------------------------------------------------------
# History trimming (three-phase drop)
# ---------------------------------------------------------------------------


def _is_pinned(msg: dict) -> bool:
    """Check if a message should be protected from trimming."""
    content = msg.get("content", "")
    if msg.get("role") == "system":
        return True
    if msg.get("_pinned"):
        return True
    if content.startswith("[Context was reset"):
        return True
    if content.startswith("[User answered"):
        return True
    return False


def _trim_history(
    messages: list[dict],
    budget: int,
    estimator,
) -> tuple[list[dict], int]:
    """Trim message history to fit budget. Returns (messages, count_trimmed).

    Phase A: Prune old tool results (view transform — already done by caller)
    Phase B: Drop assistant+tool groups oldest-first (skip pinned, prefer middle)
    Phase C: Last resort — drop pinned oldest-first
    """
    trimmed = 0

    def _total():
        return sum(estimator.count_message(m) for m in messages[1:])

    # Phase B: Drop non-pinned groups oldest-first
    if _total() > budget:
        i = 1  # skip system message
        while i < len(messages) and _total() > budget:
            msg = messages[i]
            if _is_pinned(msg) or msg.get("role") == "system":
                i += 1
                continue

            # If assistant with tool_calls, drop the group (assistant + following tool messages)
            if msg.get("role") == "assistant" and msg.get("tool_calls"):
                group_end = i + 1
                while group_end < len(messages) and messages[group_end].get("role") == "tool":
                    group_end += 1
                removed = group_end - i
                messages = messages[:i] + messages[group_end:]
                trimmed += removed
                continue

            # Drop individual message
            messages = messages[:i] + messages[i + 1 :]
            trimmed += 1

    # Phase C: Last resort — drop remaining oldest-first (still protect system + hard pins)
    if _total() > budget:
        i = 1
        while i < len(messages) and _total() > budget:
            msg = messages[i]
            if msg.get("role") == "system" or msg.get("_pinned"):
                i += 1
                continue
            messages = messages[:i] + messages[i + 1 :]
            trimmed += 1

    return messages, trimmed


# ---------------------------------------------------------------------------
# Provider normalization
# ---------------------------------------------------------------------------


def normalize_for_openrouter(messages: list[dict]) -> list[dict]:
    """Normalize messages for strict OpenAI/OpenRouter format.

    - Remove mid-conversation system messages (except first)
    - Ensure tool_calls have id, type, function fields
    - Drop orphaned tool messages
    - Ensure no None content
    """
    result = []
    valid_tc_ids = set()
    seen_system = False

    _OPENAI_ROLES = {"system", "user", "assistant", "tool"}

    for msg in messages:
        role = msg.get("role", "")
        content = msg.get("content")

        # Drop any internal/metadata roles that are not valid OpenAI API roles
        if role not in _OPENAI_ROLES:
            continue

        # Ensure content is never None (but preserve lists for multimodal)
        if content is None:
            msg = {**msg, "content": ""}

        # Remove mid-conversation system messages
        if role == "system":
            if seen_system:
                continue
            seen_system = True

        # Normalize tool_calls
        if role == "assistant" and msg.get("tool_calls"):
            tcs = msg["tool_calls"]
            if isinstance(tcs, list):
                fixed = []
                for i, tc in enumerate(tcs):
                    tc_id, name, arguments = extract_tool_call_fields(tc)
                    tc_id = tc_id or f"call_{i}_{len(fixed)}"
                    fixed.append(
                        {
                            "id": tc_id,
                            "type": "function",
                            "function": {
                                "name": name,
                                "arguments": arguments or "{}",
                            },
                        }
                    )
                    valid_tc_ids.add(tc_id)
                msg = {**msg, "tool_calls": fixed}

        # Drop orphaned tool messages
        if role == "tool":
            tcid = msg.get("tool_call_id", "")
            if tcid and tcid not in valid_tc_ids:
                continue

        result.append(msg)

    return result
