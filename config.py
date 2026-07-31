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
    "workflows_dir",
    "candor_store_dir",
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
    llm_model: str = ""
    fallback_model: str = ""
    background_model: str = ""
    scout_model: str = ""
    llm_max_concurrent: int = 1  # Max concurrent Ollama requests (semaphore slots)
    llm_session_timeout: int = 1800  # Max seconds any session may hold LLM slots (0 = unlimited)
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    openrouter_max_concurrent: int = 2  # Max concurrent OpenRouter requests
    openrouter_models: list = field(default_factory=list)
    # Force supports_vision=True for models where auto-detection misses multimodal capability.
    vision_model_overrides: list = field(default_factory=list)
    # Force supports_audio=True for models where auto-detection misses audio capability.
    # Audio-capable Ollama models (gemma4, nemotron3, …) accept WAV bytes via the
    # same `images[]` field; the server dispatches by RIFF/WAVE magic bytes.
    audio_model_overrides: list = field(default_factory=list)

    # --- Context ---
    context_budget: int = 192_000
    max_tokens: int = 32_000
    compaction_threshold: float = 0.75
    compaction_keep_tokens: int = 51_000
    context_critical_threshold: float = 0.85

    # --- Agent Loop ---
    max_tool_rounds: int = 10
    max_continuations: int = 5

    # --- Scout ---
    scout_enabled: bool = True
    scout_timeout: int = 90
    # Per-item char cap on memory search results injected into scout's user
    # content. Smaller = less context pressure on long-running sessions.
    scout_preload_memory_char_limit: int = 300
    # Retry once when primary scout returns a structurally-valid but empty
    # approach_guidance — this pattern indicates the LLM gave up, not that the
    # task needed no planning.
    scout_retry_on_empty_approach: bool = True
    # When primary scout_model exhausts its retries, scout falls through to
    # settings.fallback_model for one more attempt before the deterministic
    # stub report. No dedicated scout_fallback_model — the unified
    # Settings → Models → Fallback Model is the single source of truth.

    # --- Tools / Shell ---
    tool_timeout: int = 300
    shell_timeout: int = 30
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
    shell_env_mode: str = "passthrough"  # "passthrough" | "denylist" | "allowlist"
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

    # --- Candor (operational-memory add-on, off by default) ---
    # Calibrated reliability tracking via the external `candor` package.
    # All call sites gate on candor_enabled at runtime (hot toggle), except
    # tool registration which follows the web-extension pattern (restart).
    candor_enabled: bool = False
    candor_scout_brief: bool = True  # inject [OPERATIONAL INTEL] into scout preload
    candor_max_obs_per_turn: int = 200  # safety valve on turn-end emission volume

    # --- RLM (recursive long-input processing add-on, off by default) ---
    # Recursive Language Models engine (core/extensions/rlm): processes inputs
    # beyond the context window in a sandboxed child REPL. All call sites gate
    # on rlm_enabled at runtime (hot toggle), except tool registration which
    # follows the Candor pattern (restart). The rlm_* caps exist to prevent
    # runaway recursion/spend; model roles fall back per resolve_*_model().
    rlm_enabled: bool = False
    rlm_root_model: str = ""  # or llm_model
    rlm_sub_model: str = ""  # or background_model or llm_model
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
    dream_validation_replays_per_day: int = 4  # counterfactual scout-replay budget
    dream_report_interval_days: int = 7  # dream report cadence
    dream_journal_retention_days: int = 14  # journal sessions kept (1/day)
    dream_rlm_probe: bool = False  # deep cross-file probes via RLM (needs rlm_enabled)
    dream_rlm_probe_interval_days: int = 7  # min days between probes

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

    # --- Browser (Playwright) ---
    browser_enabled: bool = True  # browse_web tool only registered when True
    browser_headless: bool = True  # False = headed mode (for login flows, debugging)
    browser_timeout: int = 30  # Page load timeout in seconds

    # --- Reflect (post-execution verification) ---
    reflect_enabled: bool = True  # Run Reflect after agent turns
    reflect_max_retries: int = 2  # Max retry attempts before giving up
    reflect_max_retries_worker: int = (
        2  # Separate (lower) cap for worker sessions — bounds fan-out cost (1 retry allowed)
    )
    reflect_min_messages: int = 3  # Min messages to trigger (skip trivial exchanges)
    reflect_full_transcript: bool = (
        False  # DEPRECATED: reflect now always sees the per-attempt transcript; kept as a no-op for backwards compat
    )
    reflect_model: str = ""  # Model for failure analysis (empty = use background_model)
    reflect_emit_digest_on_pass: bool = (
        False  # Have reflect emit a turn_digest even on pass verdicts (debug/audit; default off saves tokens)
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
    workflows_dir: str = "data/workflows"
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
