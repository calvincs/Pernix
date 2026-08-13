"""Pernix — Configuration with disk persistence."""

import json
import logging
import os
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

logger = logging.getLogger("pernix.config")

DATA_DIR = Path("data")
SETTINGS_PATH = DATA_DIR / "settings.json"
ENV_PATH = Path(".env")

# Fields that are machine-specific and should not be persisted
_NO_PERSIST = {
    "db_path",
    "host",
    "port",
    "workspace_dir",
    "memory_dir",
    "skills_dir",
    "candor_store_dir",
    "telos_dir",
}

# Fields that are runtime-only — set via CLI flags, never read from settings.json
# or .env, and never written back to disk. Their default in the dataclass is
# always the safe value; the CLI is the only activation path.
_RUNTIME_ONLY = {"auto_approve_dangerous"}


def _parse_env(path: Path = ENV_PATH) -> dict[str, str]:
    """Parse .env file into dict. Handles KEY=value and KEY="quoted value"."""
    env = {}
    if not path.exists():
        return env
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        # Strip surrounding quotes
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
            value = value[1:-1]
        env[key] = value
    return env


@dataclass
class Settings:
    """Application settings. Persisted to data/settings.json."""

    # --- LLM Providers ---
    llm_base_url: str = "http://localhost:11434/v1"
    # Three chat-model roles, any provider (2026-08 consolidation):
    #   llm_model        — PRIMARY: agent turns AND every quality-critical
    #                      call (compaction summaries, reflect verdicts,
    #                      eval). Anything the primary must trust runs here.
    #   background_model — BACKGROUND: the fast/offline tier — scout, titles,
    #                      distill/ingest, snooze activities, dream, telos,
    #                      RLM sub-calls. Empty = llm_model.
    #   fallback_model   — BACKUP: used when a Primary or Background call
    #                      fails (stream failover, provider failover, scout
    #                      last resort, one-shot retry). Empty = no backup.
    # Per-request overrides (switch_model, spawn_worker(model=), worker
    # specs) remain the task-scoped axis.
    llm_model: str = ""
    fallback_model: str = ""
    background_model: str = ""
    # Embedding model (not a chat role): local embedding model served by
    # Ollama (e.g. "nomic-embed-text"). Setting it IS the switch — empty
    # keeps every search purely lexical. Vectors live in a rebuildable
    # sidecar next to the FTS index; embedding happens during snooze, never
    # on the write path.
    embedding_model: str = ""
    embedding_batch_size: int = 16  # texts per /api/embed call during snooze sweeps
    llm_max_concurrent: int = 1  # Max concurrent Ollama requests (semaphore slots)
    llm_session_timeout: int = 1800  # Max seconds any session may hold LLM slots (0 = unlimited)
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    openrouter_max_concurrent: int = 4  # Max concurrent OpenRouter requests
    openrouter_models: list = field(default_factory=list)
    # Prompt-cache breakpoints for anthropic/* models via OpenRouter (plan
    # 1b): the lead system message is split into content-parts with
    # cache_control markers at the static-prefix and scout-section
    # boundaries. Other models/providers are untouched.
    openrouter_cache_control: bool = True
    # Native OpenAI-compatible provider (adaptation plan 1a). The API key is
    # env-only (OPENAI_API_KEY) — never a settings field, because
    # settings.json is plaintext on disk. base_url is overridable so any
    # OpenAI-compatible server works (vLLM, LM Studio, llama.cpp server).
    # Listing models in openai_models is recommended: it routes bare names
    # (gpt-4o) to this provider and keeps the UI dropdown curated.
    openai_base_url: str = "https://api.openai.com/v1"
    openai_max_concurrent: int = 4  # Max concurrent OpenAI requests
    openai_models: list = field(default_factory=list)
    # Force supports_vision=True for models where auto-detection misses multimodal capability.
    vision_model_overrides: list = field(default_factory=list)
    # Force supports_audio=True for models where auto-detection misses audio capability.
    # Audio-capable Ollama models (gemma4, nemotron3, …) accept WAV bytes via the
    # same `images[]` field; the server dispatches by RIFF/WAVE magic bytes.
    audio_model_overrides: list = field(default_factory=list)

    # --- Context ---
    # Context-auto (2026-08): the harness derives per-model limits from live
    # provider metadata instead of manual configuration. When True (default):
    # the context budget is the active model's real window × 0.9 (Ollama
    # /api/show, OpenRouter /models), the output request is capped by the
    # provider-reported completion limit, and Ollama requests carry an
    # explicit num_ctx so the server window matches the harness budget
    # (without it Ollama silently truncates at its own default). When False:
    # context_budget / max_tokens below rule unconditionally.
    context_auto: bool = True
    # VRAM guard for context_auto on Ollama: KV-cache size scales with
    # num_ctx, so running a 256K-window model at full width can exhaust GPU
    # memory or crawl. Effective Ollama window = min(model max, this cap).
    # 0 = uncapped (trust the model max).
    ollama_num_ctx_cap: int = 65_536
    # Fallback context budget when the model registry does not report a
    # context_length for the active model (audit P2: the budget is otherwise
    # derived per-session from the registry at turn start).
    context_budget: int = 192_000
    max_tokens: int = 32_000
    compaction_threshold: float = 0.75
    compaction_keep_tokens: int = 51_000
    context_critical_threshold: float = 0.85
    # View pruning (audit P2). Previously an unconditional hardcode: every
    # tool result >300 chars beyond the last 10 messages was stubbed on every
    # compile, regardless of budget pressure, with no event. Now it only
    # engages when history chars exceed view_prune_pressure of the (char-
    # equivalent) budget, keeps more recent messages intact, and only stubs
    # genuinely large results. The compiler emits context.view_pruned.
    view_prune_keep_recent: int = 30
    view_prune_min_chars: int = 2000
    view_prune_pressure: float = 0.5
    # Ceiling on the total bytes of attachment data inlined into one compile
    # (core/context/compiler.py). Past it, the oldest attachments are dropped
    # back to text markers. 32MB fits audio (a 19MB WAV → ~25MB base64).
    max_inline_attach_bytes: int = 32 * 1024 * 1024

    # --- Agent Loop ---
    # Raised 10 -> 50 (audit P2): ten rounds was a weak-local-model-era value
    # that manufactured its own failure (round_ceiling -> reflect escalate) and
    # forced the goal-continuation machinery to paper over it. Goal token/time
    # budgets and the stuck detector are the real spend guards.
    max_tool_rounds: int = 50
    # (max_continuations removed in plan 3b — it was referenced nowhere in
    #  core logic; goal continuation_budget is the real, per-goal knob.)

    # --- Scout ---
    scout_enabled: bool = True
    scout_timeout: int = 90
    # Per-item char cap on memory search results injected into scout's user
    # content. Smaller = less context pressure on long-running sessions.
    scout_preload_memory_char_limit: int = 600
    # Retry once when primary scout returns a structurally-valid but empty
    # approach_guidance — this pattern indicates the LLM gave up, not that the
    # task needed no planning.
    scout_retry_on_empty_approach: bool = True
    # When scout (Background role) exhausts its retries, it falls through to
    # settings.fallback_model (Backup) for one more attempt before the
    # deterministic stub report.

    # --- Tools / Shell ---
    tool_timeout: int = 300
    shell_timeout: int = 30
    # Threads for ordinary tool calls. Tools run on their own pool so they can
    # never occupy asyncio's default executor, which every API route needs for
    # its DB reads — see the pool comments in core/tools/executor.py. Sized
    # above the default executor (min(32, cpu+4)) because occupants are blocked
    # on IO, not burning CPU; each also bounds concurrent subprocesses, so
    # raising it costs memory and PIDs rather than throughput.
    tool_executor_workers: int = 32
    # Threads for long-running idle-time background work (dream deep probes,
    # canary maintenance, synthesis, backups, memory dedup). Same reasoning as
    # tool_executor_workers — these must never occupy asyncio's default
    # executor, which every API route needs for its DB reads. Small on purpose:
    # occupants are heavyweight and idle-time-only, so a hard ceiling on
    # concurrency matters more than throughput. See core/pools.py.
    background_executor_workers: int = 8
    auto_approve_dangerous: bool = False  # Allow "dangerous" tools without ask_user confirmation
    shell_security_mode: str = "permissive"
    # Per-process virtual-address-space cap applied to every `bash` child via
    # RLIMIT_AS. Must be high enough for Node.js/V8 (CodeRange reserve ~1GB),
    # Playwright, and NumPy/OpenBLAS thread pools. 0 disables the cap.
    shell_address_space_limit_bytes: int = 8 * 1024 * 1024 * 1024  # 8 GB
    # Per-bash-subprocess file write cap (RLIMIT_FSIZE). 0 = no cap.
    # Was hardcoded to 100 MB; raised to 2 GB so legitimate downloads (model
    # files, video, build artifacts) work while still catching dd-loop runaways.
    shell_fsize_limit_bytes: int = 2 * 1024 * 1024 * 1024  # 2 GB
    # Max bytes for file_write / file_edit / multiedit per call. 0 = no cap.
    max_file_write_size: int = 100 * 1024 * 1024  # 100 MB
    # Max file size for file_edit's whole-file fuzzy-match path. Above this the
    # agent should grep + targeted old_string, or sed/awk via bash. 0 = no cap.
    max_edit_read_size: int = 5 * 1024 * 1024  # 5 MB
    shell_allowlist: list = field(
        default_factory=lambda: [
            "python",
            "python3",
            "node",
            "npm",
            "npx",
            "git",
            "ls",
            "cat",
            "head",
            "tail",
            "grep",
            "find",
            "wc",
            "sort",
            "uniq",
            "diff",
            "make",
            "cargo",
            "pip",
            "pip3",
            "curl",
            "wget",
            "tar",
            "unzip",
            "mkdir",
            "cp",
            "mv",
            "touch",
            "echo",
            "pwd",
            "which",
            "date",
            "tee",
            "chmod",
            "file",
            "xargs",
        ]
    )
    # "passthrough" | "denylist" | "allowlist". Defaults to allowlist: the
    # server process holds every provider API key, and "passthrough" handed a
    # copy of os.environ to every bash child (and anything it spawned). The
    # allowlist builds a minimal env from shell_env_allowlist below; PATH, HOME
    # and VIRTUAL_ENV are then set explicitly to the workspace venv, so the
    # sandbox still works. Same posture the RLM child already takes.
    shell_env_mode: str = "allowlist"
    shell_env_denylist: list = field(default_factory=list)
    shell_env_allowlist: list = field(
        default_factory=lambda: [
            "PATH",
            "HOME",
            "LANG",
            "LC_ALL",
            "TMPDIR",
            "AUDIODEV",
            "AUDIODRIVER",
            "PULSE_SERVER",
            "XDG_RUNTIME_DIR",
            "DISPLAY",
            "WAYLAND_DISPLAY",
        ]
    )

    # --- Memory ---
    memory_recall: bool = True
    # Distillation coverage audit (snooze Activity 14b): sampled re-derivation
    # of a distilled session's durable facts, checked against the store. The
    # feedback loop on the distillation lens itself — misses are recorded (and
    # repaired) instead of staying invisible to every downstream consumer.
    distill_audit_enabled: bool = True
    distill_audit_per_day: int = 2  # sampled sessions per UTC day (0 disables)

    # --- Candor (operational-memory add-on, off by default) ---
    # Calibrated reliability tracking via the external `candor` package.
    # All call sites gate on candor_enabled at runtime (hot toggle), except
    # tool registration which follows the web-extension pattern (restart).
    candor_enabled: bool = False
    candor_scout_brief: bool = True  # inject [OPERATIONAL INTEL] into scout preload
    candor_max_obs_per_turn: int = 200  # safety valve on turn-end emission volume

    # --- Gates / goals / heartbeats (long-running work, plan Phase 3) ---
    # Deterministic gates: user-authored shell checks that run before
    # Reflect; a failing gate mechanically clamps a pass verdict to retry.
    # A passing gate verifies only what that gate checks.
    gates_enabled: bool = False
    # Persistent cross-turn goals with budgets; only goal_complete finishes
    # one. continuation defaults are opt-in per goal (plan 3b).
    goals_enabled: bool = False
    # Heartbeats: recurring instructions steered into running work (3c).
    heartbeats_enabled: bool = False

    # --- Golden-task canary suite (plan 3.5, off by default) ---
    # Canned tasks + deterministic gates run headlessly through the full
    # pipeline in session_type="canary" sessions. The Phase 4 tripwire's
    # primary signal. Zero rows, zero behavior change while off.
    canary_enabled: bool = False
    canaries_dir: str = "data/canaries"
    canary_schedule: str = "0 3 * * *"  # nightly sweep cron expression
    canary_max_concurrent: int = 1
    canary_retention_days: int = 30
    # Regression detection (consumed by the Phase 4 tripwire): compare a
    # post-batch sweep's pass rate against the trailing N scheduled sweeps;
    # a drop larger than the delta is a tripwire signal.
    canary_baseline_runs: int = 3
    canary_regression_delta: float = 0.15
    # Graduated autonomy (suite self-management, active only under
    # canary_enabled). Auto-admission replaces the human approval click with
    # mechanical gates: an allowlist proof over the gate commands plus vetting
    # runs; specs the machine can't prove safe still queue for human review.
    # The maintenance sweep promotes vetted canaries, tags flapping ones
    # flaky, retires long-green ones to .retired/ quarantine, and purges the
    # quarantine after a retention window. Hard invariant (enforced in
    # core/canary/maintain.py): a canary whose latest run failed is never
    # auto-mutated — only a pass streak or a human moves it.
    canary_auto_admit: bool = True
    canary_auto_maintain: bool = True
    canary_vetting_runs: int = 3  # consistent runs required to promote out of vetting
    canary_retire_after_passes: int = 25  # consecutive passes before auto-retirement
    canary_purge_after_days: int = 30  # quarantined canaries older than this are deleted
    canary_max_suite: int = 24  # auto-admission stops at this suite size (human path stays open)

    # --- Adaptive Layer (plan §6, off by default) ---
    # Governed machine-editable policy store. While off: zero rows, compiler
    # output byte-identical, no producer emits edits.
    adaptive_enabled: bool = False
    # ON by default (takes effect only once adaptive_enabled): low-risk kinds
    # (routing_hint, prompt_note) auto-apply at idle, subject to the per-day
    # and cooldown caps below; high-risk kinds are always proposal-gated.
    # Set False to route every edit — low-risk included — through proposals,
    # e.g. while building canary baselines.
    adaptive_auto_apply: bool = True
    # Promote a canary-regression tripwire hit to automatic rollback. Off
    # until the metric earns trust; a hit only flags the batch 'suspect'.
    adaptive_auto_rollback: bool = False
    adaptive_max_entries_per_kind: int = 12
    adaptive_max_auto_applies_per_day: int = 6
    adaptive_edit_cooldown_hours: int = 24
    # Passive tripwire: post-mortem retry drift over this many organic turns
    # after a batch (canary-stamped post-mortems excluded).
    adaptive_tripwire_window_turns: int = 20
    adaptive_max_pending_proposals: int = 40  # review queue cap (0 = unbounded)
    adaptive_max_pending_per_producer: int = 12  # one producer's share of it (0 = unbounded)
    adaptive_proposal_ttl_days: int = 30  # pending proposals lapse after this (0 = never)

    # --- Session kernel (persistent per-session REPL, off by default) ---
    # Adaptation plan Phase 2: a plain-scaffold ChildREPL per session whose
    # namespace survives tool rounds, turns, and compaction (I1), and — via
    # per-variable dill snapshots — restarts. Same containment posture as
    # RLM (I7): defense-in-depth, the container/VM is the boundary.
    session_kernel_enabled: bool = False
    # Idle reap threshold. Deliberately BELOW the session reap (1800s in
    # maintenance.py): session reap pops the AgentSession, and a kernel
    # outliving its session would leak as an orphan process.
    kernel_idle_seconds: int = 1500
    kernel_snapshot_max_bytes: int = 256 * 1024 * 1024
    kernel_max_concurrent: int = 3  # live kernels across all sessions (LRU reap beyond)
    # Tool results larger than this (chars) are loaded into the kernel as
    # tool_result_<n> variables with only a head/tail stub in context
    # (prompt-as-a-variable, plan 2c). Every tool except the small exclusion
    # set in core/tools/executor.py qualifies.
    large_result_bind_threshold: int = 20_000

    # --- RLM (recursive long-input processing add-on, off by default) ---
    # Recursive Language Models engine (core/extensions/rlm): processes inputs
    # beyond the context window in a sandboxed child REPL. All call sites gate
    # on rlm_enabled at runtime (hot toggle), except tool registration which
    # follows the Candor pattern (restart). The rlm_* caps exist to prevent
    # runaway recursion/spend; model roles fall back per resolve_*_model().
    rlm_enabled: bool = False
    rlm_max_iterations: int = 20  # root REPL turns per run
    rlm_max_depth: int = 1  # 1 = llm_query only; 2+ enables rlm_query recursion
    rlm_max_subcalls: int = 50  # sub-LLM call ledger, shared across depths
    rlm_max_concurrent_subcalls: int = 3
    rlm_timeout_seconds: int = 900  # wall clock per run
    rlm_run_retention_days: int = 30  # workspace/rlm/<run_id> purge age

    # --- Dream (idle-time introspection add-on, off by default) ---
    # Hypothesis generation over memory/Candor/post-mortems, validated against
    # recorded outcomes, promoted only through gates — docs/dev/dream-plan.md.
    # Fully inert when off: snooze Activity 14 is skipped and no dream tables
    # are read or written. All call sites gate on dream_enabled at runtime.
    dream_enabled: bool = False
    dream_hypotheses_per_cycle: int = 3  # cap on new hypotheses per dream step
    dream_max_pending: int = 60  # validation backlog cap: above it, generation pauses
    dream_validation_replays_per_day: int = 4  # counterfactual scout-replay budget
    dream_report_interval_days: int = 7  # dream report cadence
    dream_journal_retention_days: int = 14  # journal sessions kept (1/day)
    dream_rlm_probe: bool = False  # deep cross-file probes via RLM (needs rlm_enabled)
    dream_rlm_probe_interval_days: int = 7  # min days between probes

    # --- TELOS (teleological layer add-on, off by default) ---
    # Non-convergent drive with correction machinery over the task loop:
    # anomaly->question->hypothesis fast loop (snooze Activity 16), daily
    # ordo/binding slow loops via cron, weekly hevel/reconcile/entropy.
    # State is markdown+YAML under telos_dir plus an append-only JSONL trace
    # ledger. Fully inert when off: no dirs created, no reads, no writes.
    # All call sites gate on telos_enabled at runtime (hot toggle), except
    # tool registration which follows the Candor pattern (restart).
    telos_enabled: bool = False
    telos_dir: str = "data/telos"
    # The root objective: a question with no satisfaction predicate —
    # unsatisfiable by construction (spec §4.1). Re-expression is an
    # operator edit here, never an agent write.
    telos_root_text: str = "What is actually going on here, and what is it for?"
    telos_schedule: str = "0 4 * * *"  # daily slow-loop cron (UTC)
    telos_serendipity_budget: float = 0.15  # non-goal question share (§3.2)
    telos_eig_floor: float = 0.15  # testability-gate admission floor (§3.4)
    telos_hypotheses_per_question: int = 3  # SOUP output cap per generation
    telos_max_gated_backlog: int = 12  # above it, every step evaluates
    telos_max_eval_tokens: int = 20_000  # gate's cost_est ceiling
    telos_question_max_attempts: int = 3  # generation passes before abandonment
    telos_soup_retention_days: int = 30  # speculation-pool prune window (0 = keep forever)
    telos_soup_context_entries: int = 10  # memory entries in the band sample
    telos_budget_share_max: float = 0.35  # binding-monitor 7d share alarm (§5.2)
    telos_claims_floor_per_window: int = 1  # binding: new-claims floor
    telos_divergence_max: float = 0.15  # ledger reconciliation alarm (§5.4)

    # --- Evaluation (extension) ---
    eval_auto: bool = False
    eval_threshold: float = 0.7
    eval_max_retries: int = 2
    eval_browser_verify: bool = False

    # --- Planning ---
    plan_review_timeout: int = 120

    # --- Snooze (idle-time self-optimization) ---
    snooze_enabled: bool = True
    snooze_interval_ticks: int = 10  # Check every N maintenance ticks (N * 60s)
    # Hang backstop per cycle — NOT a scheduler. A cycle runs until its
    # activity ladder completes; user activity ends it early (graceful yield,
    # watermark resume). This bound only kills a genuinely wedged cycle.
    # 15 min accommodates slow local models; bump it for very large ones.
    snooze_max_cycle_seconds: int = 900
    # Wall-clock ceiling on one scheduled dispatch (cron fire / heartbeat idle
    # tick). Replaces the old implicit tool_timeout × max_tool_rounds product,
    # which silently quintupled to ~4.2h when max_tool_rounds went 10→50 — a
    # wedged job should fail and notify within the hour.
    cron_dispatch_timeout: int = 3600
    snooze_cooldown_minutes: int = 5  # Min idle time before Snooze starts
    snooze_dedup_interval_days: int = 7  # Days between dedup sweeps per file
    snooze_consolidation_interval_hours: int = 24  # Hours between consolidation scans
    snooze_consolidation_cluster_threshold: float = 0.55  # Min pair score to cluster

    # --- Session Queuing ---
    max_pending_messages: int = 10  # Max queued messages per session (backpressure)

    # --- Orchestration (extension) ---
    max_concurrent_workers: int = 5
    stall_threshold: int = 120

    # --- Web Search ---
    web_search_enabled: bool = True  # search_web tool only active when True

    # --- Storage ---
    max_fetch_size: int = 100_000

    # --- Backups (maintenance.py 24h tier; scripts/backup.py on demand) ---
    # How many timestamped snapshots to keep in data/backups. Rotation is
    # per-artifact (DB snapshots and memory corpora rotate independently), so
    # a restore always has a matching pair. 0 disables scheduled backups;
    # values are clamped to 0..90 at use time because a typo here fills the
    # disk on a machine nobody is watching.
    backup_keep_count: int = 7

    # --- Browser (Playwright) ---
    browser_enabled: bool = True  # browse_web tool only registered when True
    browser_headless: bool = True  # False = headed mode (for login flows, debugging)
    browser_timeout: int = 30  # Page load timeout in seconds

    # --- Voice Input (STT) ---
    # How the mic button turns speech into chat input. Each engine has a
    # different privacy profile (surfaced as a disclaimer in Settings → Voice):
    #   off           — no mic button
    #   local_whisper — transcribed on this machine via faster-whisper; audio never leaves the box
    #   remote_whisper— recording uploaded to an OpenAI-compatible /audio/transcriptions endpoint
    #   model_direct  — recording attached to the message; the active chat model hears the audio
    #                   (local for Ollama models, leaves the machine for cloud providers)
    #   web_speech    — browser dictation; audio goes to the browser vendor's speech service
    voice_mode: str = "off"
    voice_whisper_model: str = "base"  # faster-whisper size: tiny | base | small | medium | large-v3
    voice_remote_url: str = ""  # OpenAI-compatible base URL, e.g. https://api.openai.com/v1
    voice_remote_model: str = "whisper-1"  # model name sent to the remote transcription endpoint
    voice_language: str = ""  # ISO-639-1 hint for whisper engines ("" = autodetect)
    # Fall back to browser dictation when the chosen engine is unavailable
    # (whisper not installed, model has no audio support). Off by default —
    # enabling it is the user's explicit acknowledgment that fallback audio
    # is processed by their browser vendor, not this machine.
    voice_web_speech_fallback: bool = False
    # Send the message automatically once dictation produces a non-empty
    # transcript. Applies to the transcription engines only — model_direct
    # voice notes stay manual (no transcript exists to prove speech was
    # captured before spending a model turn).
    voice_auto_send: bool = False

    # --- Reflect (post-execution verification) ---
    reflect_enabled: bool = True  # Run Reflect after agent turns
    reflect_max_retries: int = 2  # Max retry attempts before giving up
    reflect_max_retries_worker: int = 2  # Separate cap for worker sessions — bounds fan-out cost (2 retries allowed)
    reflect_min_messages: int = 3  # Min messages to trigger (skip trivial exchanges)
    reflect_full_transcript: bool = (
        False  # DEPRECATED: reflect now always sees the per-attempt transcript; kept as a no-op for backwards compat
    )
    reflect_emit_digest_on_pass: bool = (
        False  # Have reflect emit a turn_digest even on pass verdicts (debug/audit; default off saves tokens)
    )
    reflect_experience: bool = (
        True  # Parse reflect's per-turn experience read (sentiment, friction, user observations)
        # and feed it to Candor / post-mortems / user-profile memory. Prompt always asks for it.
    )
    reflect_digest_max_chars_per_excerpt: int = (
        2000  # Per-call result_excerpt cap inside the turn_digest (defensive trim at parse time)
    )
    reflect_retry_budget_cap_s: int = (
        600  # Ceiling on the computed min-budget-for-retry threshold (seconds). Prevents high
        # scout_timeout values from blocking retries when plenty of wall-clock time remains.
        # Formula is min(scout_timeout × 3 + 30, this cap). Raise to be more conservative.
    )
    post_mortem_retention_days: int = 90  # Days to keep synthesized post-mortems before snooze sweeps them

    # --- Notifications ---
    notify_webhook_url: str = ""  # POST here when ask_user fires (empty = disabled)
    notify_webhook_timeout: int = 10  # HTTP timeout in seconds
    vapid_private_key: str = ""  # EC P-256 PEM; auto-generated at first startup
    vapid_public_key: str = ""  # URL-safe base64; auto-generated at first startup
    vapid_subject: str = "mailto:admin@localhost"  # Identifies the push sender

    # --- CORS ---
    cors_origins: list = field(default_factory=list)  # Empty = localhost only

    # --- Network & Security ---
    network_enabled: bool = False  # True = bind 0.0.0.0 + require HTTPS
    ssl_mode: str = "self_signed"  # "self_signed" | "custom"
    ssl_cert_path: str = ""  # Custom cert PEM path (redacted in API)
    ssl_key_path: str = ""  # Custom key PEM path (redacted in API)
    auth_token: str = ""  # Bearer token for network mode (auto-generated)
    # Skip auth for requests originating from 127.0.0.1/::1. Correct for the
    # default single-host deployment. Set false when a reverse proxy fronts
    # Pernix: proxied requests arrive from loopback and would otherwise
    # bypass the token entirely.
    trust_local_requests: bool = True

    # --- Server (not persisted) ---
    db_path: str = "data/sessions.db"
    host: str = "127.0.0.1"
    port: int = 8090
    workspace_dir: str = "data/workspace"
    memory_dir: str = "data/memories"
    skills_dir: str = "data/skills"
    candor_store_dir: str = "data/candor"

    @property
    def workspace_venv_python(self) -> str:
        """Path to the workspace venv Python executable."""
        return str(Path(self.workspace_dir).resolve() / ".venv" / "bin" / "python")

    def save(self) -> None:
        """Persist settings to JSON, excluding machine-specific fields."""
        import tempfile

        DATA_DIR.mkdir(parents=True, exist_ok=True)
        data = {k: v for k, v in asdict(self).items() if k not in _NO_PERSIST | _RUNTIME_ONLY}
        # Atomic write: temp file + rename prevents corruption on concurrent saves
        tmp_fd, tmp_path = tempfile.mkstemp(dir=str(DATA_DIR), suffix=".tmp")
        try:
            with os.fdopen(tmp_fd, "w") as f:
                json.dump(data, f, indent=2)
            os.replace(tmp_path, str(SETTINGS_PATH))
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    @classmethod
    def load(cls) -> "Settings":
        """Load settings from JSON with type coercion. Unknown keys ignored."""
        instance = cls()
        if not SETTINGS_PATH.exists():
            return instance

        try:
            data = json.loads(SETTINGS_PATH.read_text())
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Failed to load settings: %s", e)
            return instance

        valid_fields = {f.name for f in fields(instance)} - _NO_PERSIST - _RUNTIME_ONLY
        for key, value in data.items():
            if key not in valid_fields:
                continue
            current = getattr(instance, key)
            try:
                # Skip empty strings when the default is non-empty (e.g. base URLs)
                if isinstance(current, str) and current and value == "":
                    continue
                if isinstance(current, bool):
                    if isinstance(value, bool):
                        setattr(instance, key, value)
                    else:
                        setattr(instance, key, str(value).lower() in ("true", "1", "yes"))
                else:
                    setattr(instance, key, type(current)(value))
            except (ValueError, TypeError) as e:
                logger.warning("Failed to coerce setting %s=%r: %s", key, value, e)

        return instance


def load_env() -> None:
    """Load .env file into os.environ (does not overwrite existing vars)."""
    for key, value in _parse_env().items():
        if key not in os.environ:
            os.environ[key] = value


def write_env_var(key: str, value: str | None, path: Path = ENV_PATH) -> None:
    """Persist or remove a single KEY=value pair in .env.

    Round-trips the file so unrelated keys, blank lines, and comments are
    preserved. Setting value=None or "" removes the entry.

    Used by /api/settings/apikey so a key set in the Settings UI survives
    restarts (without it, only os.environ is updated and the next process
    re-reads .env from disk and loses the value).
    """
    key = key.strip()
    if not key:
        return
    lines: list[str] = []
    if path.exists():
        lines = path.read_text().splitlines()

    new_lines: list[str] = []
    replaced = False
    for line in lines:
        stripped = line.strip()
        # Preserve comments and blanks verbatim
        if not stripped or stripped.startswith("#"):
            new_lines.append(line)
            continue
        if "=" not in stripped:
            new_lines.append(line)
            continue
        existing_key, _, _existing_value = stripped.partition("=")
        if existing_key.strip() != key:
            new_lines.append(line)
            continue
        # Match: replace or skip
        if value:
            new_lines.append(f"{key}={value}")
            replaced = True
        else:
            replaced = True  # drop the line entirely
    if value and not replaced:
        # Append at end with a trailing newline if needed
        if new_lines and new_lines[-1].strip():
            new_lines.append("")
        new_lines.append(f"{key}={value}")

    # Always end with a newline so subsequent appends don't run into the last value
    output = "\n".join(new_lines)
    if output and not output.endswith("\n"):
        output += "\n"
    path.write_text(output)


# Load env on import, then create settings singleton
load_env()
settings = Settings.load()

# Populate openrouter_models from env if not already set in settings.json
if not settings.openrouter_models:
    env_models = os.environ.get("OPENROUTER_MODELS", "")
    if env_models:
        settings.openrouter_models = [m.strip() for m in env_models.split(",") if m.strip()]

# Populate openai_models from env if not already set in settings.json
if not settings.openai_models:
    env_models = os.environ.get("OPENAI_MODELS", "")
    if env_models:
        settings.openai_models = [m.strip() for m in env_models.split(",") if m.strip()]
