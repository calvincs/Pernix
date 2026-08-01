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
# Audio: Ollama's gemma4/nemotron3 audio path detects WAV via RIFF/WAVE magic
# bytes and reads PCM/IEEE-float at 8/16/24/32-bit, any sample rate (auto
# resampled to 16kHz mono). mp3/flac would need pre-decoding upstream.
_AUDIO_EXTENSIONS = {".wav"}
_MIME_MAP = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".wav": "audio/wav",
}
_ATTACHED_RE = re.compile(r"\[attached:\s*([^\]\s]+(?:\s+[^\]]*)?)\]")

# Upper bound on the total byte size of attachment data we'll inline into a
# single compile. Beyond this, we drop the oldest attachments first and fall
# back to text markers. Settings-overridable via max_inline_attach_bytes.
# Bumped to 32MB to accommodate audio (a 19MB WAV → ~25MB base64).
MAX_INLINE_ATTACH_BYTES = 32 * 1024 * 1024

# Memoized base64 blocks keyed by (path, mtime_ns, size) — kept small since
# entries are multi-MB strings. See _expand_user_message_with_attachments.
_attach_block_cache: dict[tuple, dict] = {}
_ATTACH_CACHE_MAX = 8


def _extract_attached_filenames(text: str) -> list[str]:
    """Return the filenames referenced by [attached: ...] markers in text."""
    out: list[str] = []
    for m in _ATTACHED_RE.findall(text):
        # The first whitespace-separated token is the filename; the rest
        # may be an annotation like " — text at foo.pdf.txt".
        name = m.split()[0].strip().rstrip(",")
        out.append(name)
    return out


def _expand_user_message_with_attachments(text: str, budget: int) -> tuple[list[dict], int]:
    """Expand [attached: image|audio] references in `text` into multimodal blocks.

    Reads attachment bytes fresh from workspace each call — nothing inlined
    sits in the DB. Honors a byte budget; when exceeded, remaining attachments
    are left as text markers.

    Audio (.wav) is emitted as an `image_url` block with an `audio/*` MIME on
    the data URL. Ollama's native /api/chat dispatches by RIFF/WAVE magic
    bytes from the same `images[]` field, so the existing OpenAI→native
    conversion in core/llm/providers/ollama.py works unmodified.

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
        if ext not in _IMAGE_EXTENSIONS and ext not in _AUDIO_EXTENSIONS:
            continue
        # Reject path traversal — `[attached: ../../etc/some.jpg]` must not
        # read outside the allowed roots even if the extension gate passes.
        try:
            src = safe_read_path(fname)
        except ValueError as e:
            logger.warning("attachment rejected (path): %s — %s", fname, e)
            continue
        if not src.exists() or not src.is_file():
            continue
        try:
            st = src.stat()
        except OSError as e:
            logger.warning("attachment stat failed for %s: %s", src, e)
            continue
        # compile_context expands the latest user message on EVERY tool round
        # — without memoization a 19MB WAV is re-read and re-base64'd (~25MB
        # of string churn on the event loop) once per round for the whole
        # turn. Key on (path, mtime, size) so an edited file re-encodes.
        cache_key = (str(src), st.st_mtime_ns, st.st_size)
        block = _attach_block_cache.get(cache_key)
        if block is None:
            # ~33% overhead for base64. Check against remaining budget.
            projected = int(st.st_size * 1.34)
            if spent + projected > budget:
                logger.info(
                    "attachment %s (%d bytes) exceeds inline budget (%d/%d used); " "leaving as text marker",
                    fname,
                    st.st_size,
                    spent,
                    budget,
                )
                continue
            try:
                data = src.read_bytes()
            except OSError as e:
                logger.warning("attachment read failed for %s: %s", src, e)
                continue
            b64 = base64.b64encode(data).decode()
            mime = _MIME_MAP.get(ext, "image/jpeg")
            block = {
                "type": "image_url",
                "image_url": {"url": f"data:{mime};base64,{b64}"},
                "_filename": fname,
                "_kind": "audio" if ext in _AUDIO_EXTENSIONS else "image",
                "_b64_len": len(b64),
            }
            if len(_attach_block_cache) >= _ATTACH_CACHE_MAX:
                _attach_block_cache.clear()
            _attach_block_cache[cache_key] = block
        elif spent + block["_b64_len"] > budget:
            logger.info(
                "attachment %s exceeds inline budget (%d/%d used); leaving as text marker",
                fname,
                spent,
                budget,
            )
            continue
        blocks.append(block)
        spent += block["_b64_len"]
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
            kind = block.get("_kind", "image")
            parts.append(f"[{kind}: {name}]")
    return "\n".join(parts)


# Fixed base system prompt — stable across ALL turns (prompt cache friendly)
BASE_SYSTEM_PROMPT = """You are Pernix, a headless AI agent. You execute tasks through tool use.
State lives on disk (files, memory), not in context.
You can be interrupted and resumed at any time.

Before acting, THINK about what you need:
- What tools might help? Use discover_tools to find them.
- What do you already know? Use recall to check memory.
- What instructions apply? Check SESSIONS.md if it exists.

USE WHAT YOU KNOW. If [RELEVANT MEMORY] contains a fact the request needs
(the user's location, timezone, name, preferences), act on it. A blank or
"not set" field in [INSTRUCTIONS] is unfilled deployment config, not evidence
that the fact is unknown — it never overrides a recalled fact. Ask the user
only when neither memory nor config has the answer, or when memory is
genuinely ambiguous; if you do ask, say what you already found and what is
missing rather than claiming to know nothing.
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
use the tool. Only real questions pause: question_type="statement" shows
the user an FYI without pausing — never use ask_user to narrate progress
("I'll retry now…") and then wait; statements are for informing, not asking.

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


_RLM_BLOCK = """RECURSIVE PROCESSING (rlm_process is available on this server).
For a file or corpus far too large to read inline — anything where you'd loop
file_read pagination or where truncation keeps hiding data you need — prefer one
rlm_process call over paginated reading. It analyzes the ENTIRE input (beyond your
context window) and returns one answer. Stage the content as workspace file(s)
first, then call rlm_process(task=..., source=path or [paths]). Works on any large
input: documents, transcripts, logs, session dumps, codebase concatenations."""


_APPROVALS_BYPASSED_BLOCK = """TOOL APPROVALS ARE BYPASSED: this server runs with --dangerous. The
dangerous-tool approval gate is disabled — every tool call executes
immediately, and there is no approve_dangerous_tool and no permission ritual.
Never ask the user for permission to use a tool, and never pause for
authorization before doing what they already asked for. Reserve ask_user for
genuine decisions, missing information, or destructive actions the user has
not clearly requested."""


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
    if settings.rlm_enabled:
        parts.append(_RLM_BLOCK)
    # --dangerous is process-lifetime, so this stays byte-stable across turns
    # (prompt-prefix cache safe). Without it the model has no way to know the
    # gate is off and keeps running the ask_user + approve ritual it learned
    # from tool descriptions and RULES.md.
    if settings.auto_approve_dangerous:
        parts.append(_APPROVALS_BYPASSED_BLOCK)
    parts.append(_BASE_SYSTEM_PROMPT_TAIL)
    return "\n\n".join(parts)


def _build_model_capability_block(model_name: str, supports_vision: bool, supports_audio: bool) -> str:
    """Tell the agent what model it is and which modalities are active this turn.

    Self-awareness fixes a failure mode where a multimodal agent delegated image
    analysis to call_model because it couldn't tell whether images in the current
    turn were already inlined for it. The same applies to audio: when a wav is
    attached but the active model can't accept audio, the bytes stay as a text
    marker — agent should switch_model to an audio-capable model first.
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
    if supports_audio:
        lines.append("Audio: ENABLED for this turn.")
        lines.append(
            "Audio marked [attached: foo.wav] in THIS turn's user message is "
            "already inlined for you — listen/analyze directly. Non-WAV audio "
            "uploads have been transcoded to .wav by the ingest layer."
        )
    else:
        lines.append("Audio: DISABLED for your current model.")
        lines.append(
            "Audio attachments stay as text markers. To process audio, "
            "switch_model to an audio-capable model (e.g. nemotron3, gemma4) "
            "and try again, or transcribe via bash + whisper first."
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


# Agent directive files (SOUL.md / RULES.md / SESSIONS.md) — the single reader.
#
# These are session-invariant deployment config, so they belong in the fixed
# prefix: byte-stable content up here extends the provider's prompt-prefix
# cache across turns, whereas the old flow (scout re-wording them into its
# per-turn report) both broke the cache at the scout-section boundary and
# delivered them inconsistently (measured: identity present 66% of turns,
# ~104 chars, vs the full files here). The scout still READS these files in
# its preload to shape approach_guidance — it just no longer retypes them.
#
# The guard is an accident brake (a pasted log dump, a runaway generator),
# not curation: at 32K chars/file the truncation is logged loudly. A
# deployment that genuinely needs directives at that scale should compress
# at write time — per-turn context is the wrong place to absorb it.
_DIRECTIVE_FILE_GUARD_CHARS = 32_000
_directive_guard_warned: set[str] = set()


def _read_directive_file(path: Path) -> str:
    """Read one directive file, applying the accident guard with a loud log."""
    content = path.read_text()
    if len(content) > _DIRECTIVE_FILE_GUARD_CHARS:
        if path.name not in _directive_guard_warned:
            _directive_guard_warned.add(path.name)
            logger.warning(
                "%s is %d chars — truncating to %d for the system prompt. "
                "Trim the file (or compress it at write time); per-turn context "
                "should not carry directives at this scale.",
                path,
                len(content),
                _DIRECTIVE_FILE_GUARD_CHARS,
            )
        content = content[:_DIRECTIVE_FILE_GUARD_CHARS]
    return content.strip()


def _build_agent_directives_block() -> str:
    """[IDENTITY] + [RULES] + [INSTRUCTIONS] from data/agent/, whole and verbatim."""
    parts = []

    for fname, label in (("SOUL.md", "IDENTITY"), ("RULES.md", "RULES")):
        path = Path("data/agent") / fname
        if path.exists():
            content = _read_directive_file(path)
            if content:
                parts.append(f"[{label}]\n{content}")

    # First of SESSIONS.md / INSTRUCTIONS.md wins (mirrors the old scout order).
    for fname in ("SESSIONS.md", "INSTRUCTIONS.md"):
        path = Path("data/agent") / fname
        if path.exists():
            content = _read_directive_file(path)
            if content:
                # Framing matters: SESSIONS.md is deployment config, and an
                # unset field there is NOT evidence that a fact is unknown.
                # Without this note the model reads placeholder lines
                # ("Timezone: not set") as ground truth and refuses tasks whose
                # answer is in memory. Applied here rather than in the file so
                # it holds for every deployment's SESSIONS.md.
                parts.append(
                    f"[INSTRUCTIONS]\n"
                    f"(Deployment configuration. A blank or unset field below means "
                    f"'not pinned in config' — never that the fact is unknown. "
                    f"Defer to [RELEVANT MEMORY] for anything not pinned here.)\n"
                    f"{content}"
                )
            break

    return "\n\n".join(parts)


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
    """Build the STATIC temporal guidance section (birthdate + how to use time).

    The actual current time deliberately lives in the volatile tail message
    (see _build_volatile_tail) — putting a to-the-second timestamp in the
    system prompt head invalidated the provider's prompt-prefix cache on
    every single LLM call, forcing a full re-prefill of the entire context.
    """
    import re as _re

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
        "The current time (UTC + local) is provided in the [CURRENT STATE] message at the end of the conversation.",
    ]
    if birthdate:
        lines.append(f"Agent birthdate: {birthdate}")
    lines += [
        "",
        "All harness timestamps (sessions, messages, cron runs) are stored in UTC (+00:00).",
        "When the user says 'today' or 'yesterday', interpret relative to LOCAL time.",
        'For a fresh timestamp: bash("date") → local,  bash("date -u") → UTC.',
        "",
        "FINDING SESSION HISTORY:",
        "- list_recent_sessions(limit=N)  →  sessions newest-first by updated_at. Use for",
        "  'what did we do today/yesterday/recently' — this is the chronological lookup tool.",
        "- read_session_summary(session_id)  →  full metadata + recent messages for one session.",
        "- search_sessions(query)  →  FTS5 keyword search over message CONTENT.",
        "  Use to find sessions where a TOPIC was discussed ('sessions about web scraping').",
        "  Do NOT use search_sessions to find sessions by date — it searches text, not timestamps.",
    ]
    return "\n".join(lines)


def _build_volatile_tail(resource_status: str) -> str:
    """Per-call state that goes in a trailing system message, NOT the head.

    Everything here changes between LLM calls (clock, tool rounds remaining).
    Appending it as the final message keeps the volatile content in the
    suffix: the provider's prompt-prefix cache stays valid for the system
    prompt and all prior history, and each round only re-prefills from where
    the previous round's tail sat.
    """
    now_utc = datetime.now(timezone.utc)
    utc_str = now_utc.strftime("%Y-%m-%d %H:%M UTC")
    local_str = now_utc.astimezone().strftime("%Y-%m-%d %H:%M %Z")
    lines = [
        "[CURRENT STATE]",
        f"Current time (UTC):   {utc_str}",
        f"Current time (local): {local_str}",
    ]
    if resource_status:
        lines.append(resource_status)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Token-count caches
# ---------------------------------------------------------------------------
# The message store is append-only (rows are never modified), so a token
# count keyed by DB message id can never go stale. Without this, every
# tool round re-encoded the entire history with tiktoken from scratch —
# a 200-message session × 30 rounds = 6,000 full encodes per turn, all on
# the event loop. Entries whose content is transformed per-call (latest
# user message with attachments expanded to multimodal blocks) are
# excluded via the isinstance(content, str) check.
_msg_token_cache: dict[tuple, int] = {}
_MSG_TOKEN_CACHE_MAX = 65536
# hash(text) → tokens for the system prompt and tool-schema JSON, which are
# stable across the rounds of a turn but were also re-encoded every round.
_text_token_cache: dict[int, int] = {}
_TEXT_TOKEN_CACHE_MAX = 64


def _count_message_cached(msg: dict, estimator) -> int:
    db_id = msg.get("_db_id")
    content = msg.get("content")
    if db_id is None or not isinstance(content, str):
        return estimator.count_message(msg)
    # Include content/tool-call hashes in the key: message ids restart when
    # the DB is rebuilt (and across test databases), so id alone could alias
    # two different rows. str hashing is ~100x cheaper than a tiktoken encode.
    tc = msg.get("tool_calls")
    key = (db_id, hash(content), hash(str(tc)) if tc else 0)
    cached = _msg_token_cache.get(key)
    if cached is None:
        cached = estimator.count_message(msg)
        if len(_msg_token_cache) >= _MSG_TOKEN_CACHE_MAX:
            _msg_token_cache.clear()
        _msg_token_cache[key] = cached
    return cached


def _count_text_cached(text: str, estimator) -> int:
    if not text:
        return 0
    key = hash(text)
    cached = _text_token_cache.get(key)
    if cached is None:
        cached = estimator.count(text)
        if len(_text_token_cache) >= _TEXT_TOKEN_CACHE_MAX:
            _text_token_cache.clear()
        _text_token_cache[key] = cached
    return cached


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
    supports_audio: bool = False,
    context_budget: int | None = None,
    model_name: str = "",
    turn_user_msg_id: int | None = None,
) -> ContextPayload:
    """Assemble Layer 1 (Working Context) from session history + scout report.

    Order (prompt cache preserving):
    1. FIXED PREFIX: base system prompt + agent directives (stable across turns)
    2. SCOUT SECTION: curated memory/approach/tools (varies per turn)
    3. VOLATILE TAIL: resource status (changes per call)
    Then: compaction summary (if exists) + message history + tool schemas.
    """
    estimator = get_estimator()

    # --- Build system prompt ---
    system_parts = [_build_base_system_prompt()]

    # Model + vision/audio capability (stable within a session; tells the agent
    # whether images/audio are inlined for it or must be delegated/transcribed).
    system_parts.append(_build_model_capability_block(model_name, supports_vision, supports_audio))

    # Server URL — lets the agent open or examine workspace artifacts in a browser.
    system_parts.append(_build_server_context())

    # Agent directives (SOUL/RULES/SESSIONS) — deterministic, byte-stable, and
    # delivered whole. Lives in the fixed prefix, not the scout section, so the
    # prompt-prefix cache covers it and every turn (including scout-fallback
    # and worker turns) gets identical directives.
    directives_block = _build_agent_directives_block()
    if directives_block:
        system_parts.append(directives_block)

    # Static skill catalog — cache-stable across turns; placed before the
    # per-turn scout report so prompt cache hits the same prefix every turn.
    skills_block = _build_available_skills_block()
    if skills_block:
        system_parts.append(skills_block)

    # Temporal context (current time + birthdate)
    system_parts.append(_build_temporal_context())

    if scout_report_text:
        system_parts.append(scout_report_text)
    system_prompt = "\n\n".join(system_parts)
    # The volatile per-call state (clock, resource status) is appended as a
    # trailing system message after trim — keeping it out of the head
    # preserves the provider's prompt-prefix cache. Its tokens still count
    # against the system share of the budget.
    volatile_tail = _build_volatile_tail(resource_status)
    # Head is stable across the rounds of a turn — cached count. The tail is
    # tiny and changes per call, so it's counted raw.
    system_tokens = _count_text_cached(system_prompt, estimator) + estimator.count(volatile_tail)

    # --- Tool schema tokens ---
    tool_tokens = _count_text_cached(json.dumps(tool_schemas), estimator) if tool_schemas else 0

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
        # Strict gate: only inline binary blobs the active model can actually
        # accept. Otherwise OpenRouter routes a wav to a vision endpoint and
        # 404s (no image-input modality), or a text-only Ollama model 500s.
        # Marker stays as text, agent decides — typically calls switch_model.
        if is_latest_user and (supports_vision or supports_audio):
            blocks, _spent = _expand_user_message_with_attachments(text_form, attach_budget)
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
        tool_names: list[str] | None = None
        if msg.get("tool_calls") and msg["role"] == "assistant":
            try:
                raw_tcs = json.loads(msg["tool_calls"])
                # Normalize to OpenAI format
                openai_tcs = []
                tool_names = []
                for tc in (raw_tcs if isinstance(raw_tcs, list) else []):
                    name = tc.get("name", tc.get("function", {}).get("name", ""))
                    if name:
                        tool_names.append(name)
                    openai_tcs.append(
                        {
                            "id": tc.get("id", ""),
                            "type": "function",
                            "function": {
                                "name": name,
                                "arguments": tc.get("arguments", tc.get("function", {}).get("arguments", "{}")),
                            },
                        }
                    )
                if openai_tcs:
                    entry["tool_calls"] = openai_tcs
                if not tool_names:
                    tool_names = None
            except (json.JSONDecodeError, TypeError):
                pass

        # Internal metadata used by trim-notice generation and pin protection.
        # These keys are stripped before LLM dispatch (see _strip_private_fields).
        entry["_db_id"] = msg["id"]
        entry["_created_at"] = msg.get("created_at")
        if tool_names:
            entry["_tool_names"] = tool_names
        # Pin the active turn's root user message so trim cannot drop the
        # user's actual ask. Without this, large parallel tool returns can
        # overflow history_budget and evict the prompt itself.
        if turn_user_msg_id is not None and msg["id"] == turn_user_msg_id and msg["role"] == "user":
            entry["_pinned"] = True

        messages.append(entry)

    # --- Trim to budget ---
    history_tokens = sum(_count_message_cached(msg, estimator) for msg in messages[1:])  # skip system
    messages_trimmed = 0
    dropped_groups: list[dict] = []
    if history_tokens > history_budget:
        messages, messages_trimmed, dropped_groups = _trim_history(messages, history_budget, estimator)
        history_tokens = sum(_count_message_cached(m, estimator) for m in messages[1:])

    # --- Insert trim notice (pinned) if anything was dropped ---
    if dropped_groups:
        notice_text = _build_trim_notice(dropped_groups)
        if notice_text:
            notice_entry = {
                "role": "system",
                "content": notice_text,
                "_pinned": True,  # survives any further trim
            }
            # Place right after the system prompt + (optional) compaction
            # summary, before surviving history. messages[0] is the base
            # system prompt; messages[1] (if present) is the compaction
            # summary when has_compaction is True.
            insert_at = 2 if has_compaction and len(messages) >= 2 else 1
            messages.insert(insert_at, notice_entry)
            history_tokens += estimator.count_message(notice_entry)

    # --- Append the volatile tail (clock + resource status) ---
    # After trim so it can never be dropped; last position keeps the
    # cache-busting content in the prompt suffix.
    messages.append({"role": "system", "content": volatile_tail, "_pinned": True})

    # --- Check if compaction needed ---
    total_tokens = system_tokens + history_tokens + tool_tokens
    needs_compaction = history_tokens > int(history_budget * settings.compaction_threshold)

    # --- Strip internal `_`-prefixed fields before returning to the agent ---
    messages = _strip_private_fields(messages)

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
) -> tuple[list[dict], int, list[dict]]:
    """Trim message history to fit budget.

    Returns (messages, count_trimmed, dropped_groups). Each entry in
    dropped_groups is a snapshot dict describing one drop event:
        {
            "kind": "user" | "assistant_group" | "tool_orphan" | "other",
            "msgs": [snapshot, snapshot, ...],   # each: _db_id, role, _tool_names, _created_at, content_preview
        }
    The caller uses these snapshots to build a single pinned trim notice so
    the agent can recover via session_read(msg_id) / search_sessions.

    Phase A: Prune old tool results (view transform — already done by caller)
    Phase B: Drop assistant+tool groups oldest-first (skip pinned, prefer middle)
    Phase C: Last resort — drop pinned oldest-first
    """
    trimmed = 0
    dropped_groups: list[dict] = []

    def _total():
        return sum(_count_message_cached(m, estimator) for m in messages[1:])

    def _snapshot(msg: dict, preview_limit: int = 80) -> dict:
        content = msg.get("content") or ""
        if isinstance(content, list):
            # Multimodal — extract text blocks for preview
            parts = []
            for blk in content:
                if isinstance(blk, dict) and blk.get("type") == "text":
                    parts.append(blk.get("text", ""))
            content = " ".join(parts)
        if not isinstance(content, str):
            content = str(content)
        return {
            "_db_id": msg.get("_db_id"),
            "role": msg.get("role"),
            "_tool_names": msg.get("_tool_names"),
            "_created_at": msg.get("_created_at"),
            "content_len": len(content),
            "content_preview": content[:preview_limit].replace("\n", " "),
            # Full content kept only for user messages (we want to quote intent verbatim).
            "content_full": content if msg.get("role") == "user" else None,
        }

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
                snaps = [_snapshot(m) for m in messages[i:group_end]]
                dropped_groups.append({"kind": "assistant_group", "msgs": snaps})
                removed = group_end - i
                messages = messages[:i] + messages[group_end:]
                trimmed += removed
                continue

            # Drop individual message
            kind = "user" if msg.get("role") == "user" else ("tool_orphan" if msg.get("role") == "tool" else "other")
            dropped_groups.append({"kind": kind, "msgs": [_snapshot(msg, preview_limit=500)]})
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
            kind = "user" if msg.get("role") == "user" else ("tool_orphan" if msg.get("role") == "tool" else "other")
            dropped_groups.append({"kind": kind, "msgs": [_snapshot(msg, preview_limit=500)]})
            messages = messages[:i] + messages[i + 1 :]
            trimmed += 1

    return messages, trimmed, dropped_groups


# ---------------------------------------------------------------------------
# Trim notice builder
# ---------------------------------------------------------------------------


def _build_trim_notice(dropped_groups: list[dict]) -> str:
    """Render a single pinned system-role notice describing what trim dropped.

    The notice names msg_ids so the agent can recover via session_read(msg_id),
    and quotes any dropped user message verbatim (up to ~500 chars) so the
    user's intent survives even after eviction.
    """
    if not dropped_groups:
        return ""

    total_msgs = sum(len(g.get("msgs", [])) for g in dropped_groups)

    lines: list[str] = []
    lines.append("[Context trim notice — current turn]")
    lines.append(
        f"{total_msgs} message(s) from this turn or earlier were dropped from your view to fit the "
        "context budget. The full content is still in the database; use "
        "session_read(msg_id) to retrieve any specific item, or "
        "search_sessions(query) to query the transcript (defaults to this session)."
    )
    lines.append("")
    lines.append("Dropped (oldest first):")

    for grp in dropped_groups:
        kind = grp.get("kind")
        msgs = grp.get("msgs") or []
        if not msgs:
            continue

        if kind == "user":
            m = msgs[0]
            mid = m.get("_db_id")
            created = (m.get("_created_at") or "")[:16]
            full = m.get("content_full") or m.get("content_preview") or ""
            quote = full[:500]
            if len(full) > 500:
                quote += "…"
            quote = quote.replace("\n", " ")
            lines.append(f'  • user msg {mid} ({created}) — "{quote}"')

        elif kind == "assistant_group":
            ids = [str(m.get("_db_id")) for m in msgs if m.get("_db_id") is not None]
            head = msgs[0]
            tool_names = head.get("_tool_names") or []
            total_chars = sum(int(m.get("content_len") or 0) for m in msgs)
            ids_str = ",".join(ids)
            if tool_names:
                tools_label = (
                    f"{tool_names[0]} ×{len(tool_names)}"
                    if len(tool_names) > 1 and len(set(tool_names)) == 1
                    else "+".join(tool_names) if len(tool_names) <= 4 else f"{len(tool_names)} tool calls"
                )
            else:
                tools_label = "tool call"
            lines.append(f"  • assistant+tools {ids_str} ({tools_label}) — ~{total_chars} chars")

        else:
            # tool_orphan or other — show ids only
            for m in msgs:
                mid = m.get("_db_id")
                role = m.get("role")
                preview = (m.get("content_preview") or "").strip()
                lines.append(f"  • {role} msg {mid} — {preview[:80]}")

    return "\n".join(lines)


def _strip_private_fields(messages: list[dict]) -> list[dict]:
    """Remove internal `_`-prefixed keys before LLM dispatch.

    Compaction summary and trim notice carry `_pinned: True` to survive
    further trim cycles, and the conversion loop tags entries with
    `_db_id`/`_tool_names`/`_created_at` for trim-notice generation. None
    of these keys are valid OpenAI/OpenRouter top-level message fields.
    """
    cleaned: list[dict] = []
    for m in messages:
        cleaned.append({k: v for k, v in m.items() if not (isinstance(k, str) and k.startswith("_"))})
    return cleaned


# ---------------------------------------------------------------------------
# Provider normalization
# ---------------------------------------------------------------------------


def normalize_for_openrouter(messages: list[dict]) -> list[dict]:
    """Normalize messages for strict OpenAI/OpenRouter format.

    - Convert mid-conversation system messages (compaction summary, trim
      notice, volatile state tail) to user-role carriers. Some OpenRouter
      backends only accept system-first; dropping them (the old behavior)
      meant the compaction summary and trim notices never reached the
      model at all on the OpenRouter path. Their content already carries
      bracketed markers ("[Previous conversation summary]", "[CURRENT
      STATE]"), so the model reads them as harness context, not user speech.
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

        # Mid-conversation system messages become user-role carriers
        if role == "system":
            if seen_system:
                msg = {**msg, "role": "user"}
                role = "user"
            else:
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
