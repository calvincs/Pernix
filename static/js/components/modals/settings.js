// Pernix — Settings modal with tabs

import { el, text, clear, setSanitizedSvg } from '../../render.js';
import { del, get, post, setAuthToken } from '../../api.js';
import { getPermission, requestPermission } from '../../notifications.js';
import { runVoiceTest } from '../../voice.js';
import { announce, openOverlay } from '../../a11y.js';
import { notify } from '../../feedback.js';

let _overlay = null;
let _closeOverlay = null;  // teardown from a11y.js openOverlay()
let _statusTimer = null;  // pending auto-clear of the footer status line
let _original = {};  // snapshot of settings when modal opened
let _models = [];    // [{id, valid: null|true|false, info: {}}]
let _availableModels = [];  // [{id, provider, ...}] from GET /api/models
let _ollamaModels = [];     // [{name, size, family, parameter_size, quantization}]
let _ollamaError = '';
let _ollamaLoading = false;

// Settings the server refuses to change over the API (api/routers/health.py
// _LOCKED_FIELDS). A POST carrying them is dropped without a word, so the
// controls render read-only instead of pretending the edit took.
const LOCKED_KEYS = new Set([
    'shell_security_mode',
    'shell_allowlist',
    'shell_env_mode',
    'shell_env_denylist',
    'shell_env_allowlist',
    'auto_approve_dangerous',
    'auth_token',
]);

const LOCKED_NOTE = 'Edit-locked. The settings API rejects changes to this field so a prompt-injected '
    + 'agent cannot widen its own sandbox through it. Change it in data/settings.json and restart.';

// The server only reports restart_required for the network group, but many
// other values are read exactly once at startup — when the LLM router is
// constructed, when tools register — so a live save persists them while the
// running process keeps the old ones.
//
// Which keys those are is already declared, per field, as the `restart` string
// that renders the badge beside the control. A second hand-maintained list
// drifted from the badges the moment one was added: every RESTART_TOOLS field
// (web_search_enabled, candor_enabled, rlm_enabled, telos_enabled, …) wore a
// "restart" badge and then saved with a plain "Saved". Derive it instead. (S5)
const RESTART_EXTRA_KEYS = new Set([
    // List-valued editors with no field entry of their own. The Allowed
    // Origins section carries the same warning in its own help text.
    'cors_origins',
]);

function restartKeys() {
    const keys = new Set(RESTART_EXTRA_KEYS);
    for (const field of allSettingFields()) {
        if (field.restart) keys.add(field.key);
    }
    return keys;
}

const RESTART_ROUTER = 'Sizes a provider semaphore when the LLM router is built. Saving stores the '
    + 'value; the running process keeps its current slot count until the server restarts.';
const RESTART_TOOLS = 'Tool registration happens at startup. The runtime checks honour this '
    + 'immediately, but the agent-facing tools only appear or disappear after a restart.';

const NETWORK_FIELDS = [
    { key: 'network_enabled', label: 'Network Access', type: 'bool', risk: 'security', restart: 'Changes the bind address and TLS setup — applied at startup.' },
    { key: 'ssl_mode', label: 'SSL Certificate', type: 'select', options: ['self_signed', 'custom'], restart: 'Applied at startup.' },
    { key: 'ssl_cert_path', label: 'Certificate PEM Path', type: 'certpath', restart: 'Applied at startup.' },
    { key: 'ssl_key_path', label: 'Key PEM Path', type: 'certpath', restart: 'Applied at startup.' },
    {
        key: 'trust_local_requests',
        label: 'Trust Loopback Requests',
        type: 'bool',
        risk: 'security',
        hint: 'On: requests from 127.0.0.1/::1 skip the Bearer token — correct for a single-host box. '
            + 'Turn OFF when nginx, Caddy, Cloudflared or any other reverse proxy fronts Pernix: proxied '
            + 'requests arrive from loopback, so every remote visitor would bypass auth entirely.',
    },
];

const SECTIONS = [
    {
        title: 'LLM Providers',
        description: 'Configure endpoints and concurrency for LLM providers. Max Concurrent limits parallel requests per provider. Session LLM Timeout caps how long a single session may hold LLM slots — prevents a hung or runaway session from blocking others indefinitely (0 = unlimited). Reasoning applies to Ollama models that have a thinking mode (the qwen3 family, nemotron3, …) and is off for both roles by default: it buys quality on hard turns and costs latency and output tokens everywhere. If Primary and Background name the same model the two cannot be told apart — Primary\'s setting applies to both. Model selection is on the Models tab.',
        fields: [
            { key: 'llm_base_url', label: 'Ollama Base URL', type: 'text' },
            { key: 'llm_max_concurrent', label: 'Ollama Max Concurrent', type: 'number', min: 1, max: 20, restart: RESTART_ROUTER },
            { key: 'ollama_think', label: 'Ollama Reasoning — Primary', type: 'bool' },
            { key: 'ollama_think_background', label: 'Ollama Reasoning — Background', type: 'bool' },
            { key: 'openrouter_base_url', label: 'OpenRouter URL', type: 'text' },
            { key: 'openrouter_max_concurrent', label: 'OpenRouter Max Concurrent', type: 'number', min: 1, max: 20, restart: RESTART_ROUTER },
            { key: 'openrouter_cache_control', label: 'Anthropic Cache Breakpoints (via OpenRouter)', type: 'bool' },
            { key: 'openai_base_url', label: 'OpenAI URL (or any OpenAI-compatible server)', type: 'text' },
            { key: 'openai_max_concurrent', label: 'OpenAI Max Concurrent', type: 'number', min: 1, restart: RESTART_ROUTER },
            { key: 'llm_session_timeout', label: 'Session LLM Timeout (seconds)', type: 'number', min: 0, restart: RESTART_ROUTER },
            {
                key: 'fallback_burn_alert_share',
                label: 'Fallback-burn Alert Share (0–1 fraction)',
                type: 'number', min: 0, max: 1, step: 0.05,
                hint: 'When the Backup model serves at least this share of the trailing 24h\'s tokens, a high-urgency notification fires (once/day) — the signature of a wedged primary provider silently billing everything to the paid tier. Watch-only; 0 disables.',
            },
            {
                key: 'fallback_burn_min_tokens',
                label: 'Fallback-burn Volume Floor (tokens)',
                type: 'number', min: 0,
                hint: 'The alert stays quiet unless the 24h window carried at least this many total tokens — a quiet day that happened to fail over is noise, not the incident.',
            },
            { key: 'openrouter_api_key', label: 'OpenRouter API Key', type: 'apikey', envKey: 'OPENROUTER_API_KEY' },
            { key: 'openai_api_key', label: 'OpenAI API Key', type: 'apikey', envKey: 'OPENAI_API_KEY' },
        ],
    },
    {
        title: 'Context',
        description: 'Context is auto-managed by default: the harness reads each model\'s real window and completion cap from the provider (Ollama /api/show, OpenRouter /models), budgets against it, and pins Ollama\'s num_ctx so the server window matches — turn Auto off to force the manual Context Budget / Max Output Tokens instead. The Ollama Context Cap bounds KV-cache VRAM use on big-window models (0 = model max). Compaction automatically summarizes older messages when context fills up; critical threshold triggers a hard reset if compaction can\'t free enough space. View pruning is the cheaper step before compaction: under budget pressure it stubs oversized tool results out of the compiled view only — stored messages are never touched.',
        fields: [
            { key: 'context_auto', label: 'Auto (use model-reported limits)', type: 'bool' },
            { key: 'ollama_num_ctx_cap', label: 'Ollama Context Cap (tokens, 0 = model max)', type: 'number' },
            { key: 'context_budget', label: 'Context Budget (tokens, manual/fallback)', type: 'number' },
            { key: 'max_tokens', label: 'Max Output Tokens (tokens, ceiling)', type: 'number' },
            { key: 'compaction_threshold', label: 'Compaction Threshold (0–1 fraction)', type: 'number', step: 0.05 },
            { key: 'compaction_keep_tokens', label: 'Compaction Keep Tokens (tokens)', type: 'number' },
            { key: 'context_critical_threshold', label: 'Critical Reset Threshold (0–1 fraction)', type: 'number', step: 0.05 },
            { key: 'view_prune_pressure', label: 'View Prune Pressure (0–1 fraction of budget)', type: 'number', step: 0.05 },
            { key: 'view_prune_keep_recent', label: 'View Prune Keep Recent (messages)', type: 'number' },
            { key: 'view_prune_min_chars', label: 'View Prune Min Chars', type: 'number' },
        ],
    },
    {
        title: 'Agent',
        description: 'Limits on the agent loop. Max Tool Rounds is a backstop against a runaway loop, not a spend cap — goal token/time budgets and the stuck detector are the real guards, so a high value is fine. Scout sends a lightweight model (the Background role) ahead to discover relevant tools and context before the Primary model responds. Forced follow-up: when a reply ends by announcing more work ("Next, I\'ll…") without doing it, the harness injects one bounded in-turn nudge to keep the agent working instead of ending the turn.',
        fields: [
            { key: 'max_tool_rounds', label: 'Max Tool Rounds', type: 'number' },
            { key: 'scout_enabled', label: 'Scout Enabled', type: 'bool' },
            { key: 'scout_timeout', label: 'Scout Timeout (seconds)', type: 'number' },
            { key: 'forced_followup_enabled', label: 'Forced Follow-up Nudge', type: 'bool' },
            { key: 'forced_followup_max_per_turn', label: 'Max Forced Follow-ups / Turn', type: 'number', min: 0, max: 5 },
        ],
    },
    {
        title: 'Shell & Tools',
        description: 'Timeouts and security for tool execution. Strict shell security restricts commands to a built-in allowlist. Permissive mode allows any command. Size caps below: 0 = no cap. RLIMIT_FSIZE caps each bash subprocess\'s file writes; lift it for large model/video downloads.',
        fields: [
            { key: 'tool_timeout', label: 'Tool Timeout (seconds)', type: 'number' },
            { key: 'shell_timeout', label: 'Shell Timeout (seconds)', type: 'number' },
            { key: 'shell_security_mode', label: 'Shell Security', type: 'select', options: ['permissive', 'strict'], risk: 'security' },
            { key: 'shell_address_space_limit_bytes', label: 'Bash RLIMIT_AS (bytes, 0 = no cap)', type: 'number', min: 0 },
            { key: 'shell_fsize_limit_bytes', label: 'Bash RLIMIT_FSIZE (bytes, 0 = no cap)', type: 'number', min: 0 },
            { key: 'max_file_write_size', label: 'Max file_write Size (bytes, 0 = no cap)', type: 'number', min: 0 },
            { key: 'max_edit_read_size', label: 'Max file_edit Read Size (bytes, 0 = no cap)', type: 'number', min: 0 },
            { key: 'max_fetch_size', label: 'Max fetch_url Size (bytes)', type: 'number', min: 1024, max: 10000000 },
        ],
    },
    {
        title: 'Web',
        description: 'Web search uses Tavily (requires API key — free tier at tavily.com). Browser uses Playwright for JS-rendered page extraction. Disable headless for login flows or visual debugging.',
        fields: [
            { key: 'web_search_enabled', label: 'Web Search', type: 'bool', restart: RESTART_TOOLS },
            { key: 'tavily_api_key', label: 'Tavily API Key', type: 'apikey', envKey: 'TAVILY_API_KEY' },
            { key: 'browser_enabled', label: 'Enable Browser (Playwright)', type: 'bool', restart: RESTART_TOOLS },
            { key: 'browser_headless', label: 'Headless Mode', type: 'bool' },
            { key: 'browser_timeout', label: 'Page Load Timeout (seconds)', type: 'number', min: 5, max: 120 },
        ],
    },
    {
        title: 'Voice Input',
        description: 'Speech-to-text for the chat input. Each engine has a different privacy profile — the disclaimer below the engine selector says where your voice audio goes. Local Whisper transcribes on the Pernix server; Remote Whisper uploads recordings to an OpenAI-compatible endpoint; Direct-to-Model attaches the recording for an audio-capable chat model to hear; Browser Dictation uses your browser vendor\'s speech service.',
        fields: [
            { key: 'voice_mode', label: 'Engine', type: 'select', options: ['off', 'local_whisper', 'remote_whisper', 'model_direct', 'web_speech'] },
            { key: 'voice_whisper_model', label: 'Whisper Model', type: 'select', options: ['tiny', 'base', 'small', 'medium', 'large-v3'] },
            { key: 'voice_remote_url', label: 'Remote STT Base URL', type: 'text' },
            { key: 'voice_remote_model', label: 'Remote STT Model', type: 'text' },
            { key: 'voice_stt_api_key', label: 'Remote STT API Key', type: 'apikey', envKey: 'VOICE_STT_API_KEY' },
            { key: 'voice_language', label: 'Language', type: 'select', options: [
                { value: '', label: 'Auto-detect' },
                { value: 'ar', label: 'Arabic' },
                { value: 'zh', label: 'Chinese' },
                { value: 'cs', label: 'Czech' },
                { value: 'da', label: 'Danish' },
                { value: 'nl', label: 'Dutch' },
                { value: 'en', label: 'English' },
                { value: 'fi', label: 'Finnish' },
                { value: 'fr', label: 'French' },
                { value: 'de', label: 'German' },
                { value: 'el', label: 'Greek' },
                { value: 'he', label: 'Hebrew' },
                { value: 'hi', label: 'Hindi' },
                { value: 'hu', label: 'Hungarian' },
                { value: 'id', label: 'Indonesian' },
                { value: 'it', label: 'Italian' },
                { value: 'ja', label: 'Japanese' },
                { value: 'ko', label: 'Korean' },
                { value: 'no', label: 'Norwegian' },
                { value: 'pl', label: 'Polish' },
                { value: 'pt', label: 'Portuguese' },
                { value: 'ro', label: 'Romanian' },
                { value: 'ru', label: 'Russian' },
                { value: 'es', label: 'Spanish' },
                { value: 'sv', label: 'Swedish' },
                { value: 'th', label: 'Thai' },
                { value: 'tr', label: 'Turkish' },
                { value: 'uk', label: 'Ukrainian' },
                { value: 'vi', label: 'Vietnamese' },
            ] },
            { key: 'voice_auto_send', label: 'Auto-send After Dictation', type: 'bool' },
            { key: 'voice_web_speech_fallback', label: 'Browser Dictation Fallback', type: 'bool' },
        ],
    },
    {
        title: 'Memory',
        description: 'Automatic memory recall surfaces relevant past conversations at the start of each turn. The distillation audit is the feedback loop on memory quality: during snooze it re-derives the durable facts of an already-distilled session with the Background model and repairs anything the first pass missed. It costs a couple of background LLM calls per day — set the per-day count to 0 to keep the audit off without disabling the machinery.',
        fields: [
            { key: 'memory_recall', label: 'Auto-Recall', type: 'bool' },
            { key: 'distill_audit_enabled', label: 'Distillation Coverage Audit', type: 'bool' },
            {
                key: 'distill_audit_per_day',
                label: 'Audited Sessions / Day',
                type: 'number',
                min: 0,
                hint: 'Background LLM calls per day. 0 disables the audit.',
            },
        ],
    },
    {
        title: 'Background Work (Snooze)',
        description: 'The master switch for everything the agent does while you are idle: memory maintenance and distillation, dreaming, telos loops, canary sweeps, adaptive edits and embedding sweeps all run inside a snooze cycle. Turning Background Work off stops all of it and is the one control that reliably ends idle-time LLM spend, whatever the individual feature toggles say. Cooldown is how long the machine must be quiet before a cycle may start; the tick interval paces how often the scheduler even looks. The cycle time limit is a hang backstop, not a scheduler — a cycle normally ends when its activity ladder finishes or you start typing; raise it for slow local models.',
        fields: [
            {
                key: 'snooze_enabled',
                label: 'Background Work Enabled',
                type: 'bool',
                risk: 'autonomy',
                hint: 'Off = no idle-time LLM spend at all: memory maintenance, dream, telos, canary, adaptive and embedding sweeps are all skipped.',
            },
            { key: 'snooze_cooldown_minutes', label: 'Idle Cooldown (min)', type: 'number', min: 0 },
            {
                key: 'snooze_interval_ticks',
                label: 'Check Interval (ticks)',
                type: 'number',
                min: 1,
                hint: 'One tick is 60s of maintenance loop, so 10 = a check roughly every 10 minutes.',
            },
            { key: 'snooze_max_cycle_seconds', label: 'Cycle Time Limit (seconds)', type: 'number', min: 60 },
        ],
    },
    {
        title: 'Candor (Operational Memory)',
        description: 'Calibrated reliability tracking: tool outcomes and reflect verdicts feed an auditable evidence ledger, and scout receives an operational-intel brief flagging degraded tools, discovered conditions, and open questions. Observation capture, snooze maintenance, and the scout brief toggle immediately; the agent-facing tools (predict_reliability, why_reliability, reliability_questions) register at startup, so they appear/disappear after a restart.',
        fields: [
            { key: 'candor_enabled', label: 'Candor Enabled', type: 'bool', restart: RESTART_TOOLS },
            { key: 'candor_scout_brief', label: 'Scout Intel Brief', type: 'bool' },
            { key: 'candor_max_obs_per_turn', label: 'Max Observations / Turn', type: 'number' },
        ],
    },
    {
        title: 'RLM (Recursive Processing)',
        description: 'Recursive Language Models: the agent processes inputs far beyond the context window (huge files, corpora, transcripts) by writing code in a sandboxed REPL that holds the input as a variable and delegates chunks to sub-LLM calls. The caps guard against runaway runs: iterations bounds root turns, sub-calls bounds total LLM spend per run, depth 2+ allows recursive child RLMs. Caps apply immediately; the rlm_process tool registers at startup, so enabling/disabling takes a restart. RLM adds no model roles of its own: the root runs on your Primary model and sub-calls run on Background (both set under Models → Model Roles).',
        fields: [
            { key: 'rlm_enabled', label: 'RLM Enabled', type: 'bool', restart: RESTART_TOOLS },
            { key: 'rlm_max_iterations', label: 'Max Iterations', type: 'number' },
            { key: 'rlm_max_subcalls', label: 'Max Sub-calls / Run', type: 'number' },
            { key: 'rlm_max_concurrent_subcalls', label: 'Sub-call Concurrency', type: 'number' },
            { key: 'rlm_max_depth', label: 'Max Recursion Depth', type: 'number' },
            { key: 'rlm_timeout_seconds', label: 'Run Timeout (seconds)', type: 'number' },
            { key: 'rlm_run_retention_days', label: 'Run Data Retention (days)', type: 'number' },
        ],
    },
    {
        title: 'MCP Servers',
        description: 'Model Context Protocol: plug external tool servers into the agent. Servers are configured in the Explorer → MCP tab (or data/mcp_servers.json, standard mcpServers format); each connected server\'s tools register as mcp_<server>_<tool> and go through the normal safety gate, scout curation, and health metrics. Enabling/disabling applies immediately — no restart. Turning it off kills local server processes but keeps the tool names visible; their calls return a clear "disabled" error.',
        fields: [
            { key: 'mcp_enabled', label: 'MCP Enabled', type: 'bool' },
            { key: 'mcp_stdio_enabled', label: 'Allow stdio (local subprocess) Servers', type: 'bool', risk: 'security',
              hint: 'A stdio server is arbitrary local code. Off = remote (url) servers only.' },
            { key: 'mcp_default_safety', label: 'Default Tool Safety', type: 'select', options: ['safe', 'caution', 'dangerous'],
              hint: 'Stamped on MCP tools unless the server config overrides it. Server-sent destructive hints always escalate to dangerous.' },
            { key: 'mcp_call_timeout', label: 'Call Timeout (seconds)', type: 'number', min: 5 },
            { key: 'mcp_connect_timeout', label: 'Connect Timeout (seconds)', type: 'number', min: 5 },
            { key: 'mcp_idle_seconds', label: 'Suspend Idle stdio Servers After (seconds, 0 = never)', type: 'number', min: 0 },
            { key: 'mcp_max_servers', label: 'Max Servers', type: 'number', min: 1 },
            { key: 'mcp_max_tools_per_server', label: 'Max Tools per Server', type: 'number', min: 1 },
            { key: 'mcp_refresh_interval_s', label: 'Tool Re-check Interval (seconds, 0 = manual only)', type: 'number', min: 0 },
        ],
    },
    {
        title: 'Dream (Introspection)',
        description: 'Idle-time introspection: during snooze the agent examines its own memory, Candor evidence, and post-mortems, generates typed hypotheses about itself (contradictions, stale lessons, tool patterns), validates them against recorded outcomes, and writes a periodic dream report to workspace/dreams/. Hypotheses influence nothing until validated; replays/day bounds the counterfactual scout-replay spend (0 disables replay). All settings apply immediately.',
        fields: [
            { key: 'dream_enabled', label: 'Dreaming Enabled', type: 'bool' },
            { key: 'dream_hypotheses_per_cycle', label: 'Max Hypotheses / Step', type: 'number' },
            { key: 'dream_validation_replays_per_day', label: 'Scout Replays / Day', type: 'number' },
            { key: 'dream_report_interval_days', label: 'Report Interval (days)', type: 'number' },
            { key: 'dream_journal_retention_days', label: 'Journal Retention (days)', type: 'number' },
            { key: 'dream_rlm_probe', label: 'Deep Probes (RLM)', type: 'bool' },
            { key: 'dream_rlm_probe_interval_days', label: 'Probe Interval (days)', type: 'number' },
        ],
    },
    {
        title: 'Reflect',
        description: 'Post-task verification re-reads the agent\'s work and checks for mistakes or incomplete steps. If issues are found, the agent retries automatically. Min messages prevents reflect from firing on trivial exchanges. Deferred grading keeps interactive turns off the critical path: the grade runs in the background after a quiet period and can record lessons, but never retries the turn.',
        fields: [
            { key: 'reflect_enabled', label: 'Post-Task Verification', type: 'bool' },
            { key: 'reflect_max_retries', label: 'Max Retries', type: 'number' },
            { key: 'reflect_min_messages', label: 'Min Messages to Trigger', type: 'number' },
            { key: 'reflect_deferred_normal', label: 'Defer Grading (Interactive)', type: 'bool' },
            { key: 'reflect_defer_idle_s', label: 'Defer Delay (seconds)', type: 'number' },
            {
                key: 'reflect_nonpass_confidence_floor',
                label: 'Non-pass Confidence Floor (0–1 fraction)',
                type: 'number', min: 0, max: 1, step: 0.05,
                hint: 'A retry/escalate verdict the grader itself rates below this confidence is downgraded to pass-with-lessons — the prompt defines <0.5 as "evidence is ambiguous", and ambiguity should not burn a retry or fire an escalation. Malformed grades stay conservative. 0 disables.',
            },
            { key: 'post_mortem_retention_days', label: 'Post-mortem retention (days)', type: 'number' },
            {
                key: 'notification_retention_days',
                label: 'Notification retention (days)',
                type: 'number', min: 0, max: 365,
                hint: 'The bell is a recent-events surface, not an archive. 0 = keep forever (pre-v3.1 behavior).',
            },
        ],
    },
    {
        title: 'Evaluation',
        description: 'Feature-level QA against acceptance criteria in the feature registry (data/registry.json). When auto-evaluate is enabled, runs after each task to score registered features. Browser screenshots provide visual verification evidence.',
        fields: [
            { key: 'eval_auto', label: 'Auto-Evaluate', type: 'bool' },
            { key: 'eval_threshold', label: 'Pass Threshold (0–1 fraction)', type: 'number', step: 0.1 },
            { key: 'eval_max_retries', label: 'Max Retries', type: 'number' },
            { key: 'eval_browser_verify', label: 'Browser Screenshots', type: 'bool' },
        ],
    },
    {
        title: 'Orchestration',
        description: 'Controls for multi-worker task decomposition. Max workers limits parallel sub-agents. Stall threshold detects stuck workers. Plan review timeout is how long you have to approve a generated plan before it auto-proceeds.',
        fields: [
            { key: 'max_concurrent_workers', label: 'Max Workers', type: 'number' },
            { key: 'plan_review_timeout', label: 'Plan Review Timeout (seconds)', type: 'number' },
        ],
    },
    {
        title: 'Autonomy (Gates, Goals, Heartbeats, Kernel)',
        description: 'Long-running autonomous task substrate. Gates: deterministic shell checks Reflect cannot overrule. Goals: persistent objectives with budgets and auto-continuations. Heartbeats: recurring instructions steered into running work. Session kernel: a persistent per-session Python REPL whose variables survive turns and restarts.',
        fields: [
            { key: 'gates_enabled', label: 'Deterministic Gates', type: 'bool' },
            { key: 'goals_enabled', label: 'Persistent Goals', type: 'bool' },
            { key: 'heartbeats_enabled', label: 'Heartbeats', type: 'bool' },
            { key: 'session_kernel_enabled', label: 'Session Kernel (REPL)', type: 'bool', risk: 'autonomy', restart: RESTART_TOOLS },
        ],
    },
    {
        title: 'Canary Suite',
        description: 'Golden-task canaries: canned tasks with deterministic gates, run headlessly through the full pipeline. Change-driven: canaries run when something they cover changes (an adaptive batch, a skill edit, a model swap, a deploy), plus a small nightly heartbeat that keeps history warm. The Adaptive Layer\'s tripwire reads the post-batch results per task. Canary sessions are isolated and tool-allowlisted: computation and reads only.',
        fields: [
            { key: 'canary_enabled', label: 'Canary Suite Enabled', type: 'bool', restart: RESTART_TOOLS },
            { key: 'canary_schedule', label: 'Heartbeat Schedule (cron)', type: 'text' },
            {
                key: 'canary_heartbeat_per_night',
                label: 'Heartbeat Canaries per Night',
                type: 'number', min: 1, max: 10,
                hint: 'How many least-recently-run active canaries each scheduled heartbeat runs. Parked canaries sit out.',
            },
            {
                key: 'canary_post_batch_max',
                label: 'Post-batch Probe Size',
                type: 'number', min: 1, max: 12,
                hint: 'Cap on canaries per post-batch probe: the ones covering the batch\'s edit kinds first, sentinels riding along.',
            },
            { key: 'canary_retention_days', label: 'Run Retention (days)', type: 'number', min: 1, max: 365 },
            {
                key: 'canary_baseline_runs',
                label: 'Green Precondition Window',
                type: 'number', min: 1, max: 20,
                hint: 'A canary may testify against a batch only when this many trailing runs before the apply were all green.',
            },
            { key: 'canary_regression_delta', label: 'Passive Drift Delta (0–1 fraction)', type: 'number', step: 0.05 },
            {
                key: 'canary_park_after_passes',
                label: 'Park After Consecutive Passes',
                type: 'number', min: 3, max: 200,
                hint: 'Long-green canaries are parked: off the heartbeat, still in the suite, auto-unparked by any red run.',
            },
            {
                key: 'canary_auto_admit',
                label: 'Auto-admit New Canaries',
                type: 'bool',
                risk: 'autonomy',
                hint: 'Lets the agent write new canary specs into data/canaries/ without asking, once their gate '
                    + 'commands pass an allowlist proof and vetting runs. Off routes every new canary through you.',
            },
            {
                key: 'canary_auto_maintain',
                label: 'Auto-maintain Suite',
                type: 'bool',
                risk: 'autonomy',
                hint: 'The idle sweep promotes vetted canaries, tags flapping ones flaky, parks long-green ones, '
                    + 'syncs skill verify blocks, and retires exhausted probes. A canary whose latest run failed is '
                    + 'never auto-moved — except that a red run un-parks.',
            },
        ],
    },
    {
        title: 'Adaptive Layer',
        description: 'Governed machine-editable policy: routing hints and prompt notes the agent may auto-apply at idle (with full history and one-click rollback), and policies/worker specs that always wait for your approval. The canary tripwire flags any batch that makes the agent measurably worse. Run the canary suite for at least a week before enabling auto-apply.',
        fields: [
            { key: 'adaptive_enabled', label: 'Adaptive Layer Enabled', type: 'bool', risk: 'autonomy' },
            { key: 'adaptive_auto_apply', label: 'Auto-apply Low-risk Edits', type: 'bool', risk: 'autonomy' },
            { key: 'adaptive_auto_rollback', label: 'Auto-rollback on Canary Regression', type: 'bool', risk: 'autonomy' },
            { key: 'adaptive_max_auto_applies_per_day', label: 'Max Auto-applies / Day', type: 'number' },
            { key: 'adaptive_max_entries_per_kind', label: 'Max Entries / Kind', type: 'number' },
            { key: 'adaptive_edit_cooldown_hours', label: 'Edit Cooldown (hours)', type: 'number' },
            {
                key: 'adaptive_usage_retire_days',
                label: 'Retire Unused After (days)',
                type: 'number', min: 0, max: 365,
                hint: 'Entries with zero recorded uses (scout/reflect citations) over this many instrumented days are retired — journaled, rollbackable. 0 disables.',
            },
            {
                key: 'adaptive_prompt_note_ttl_days',
                label: 'Prompt-note TTL (days)',
                type: 'number', min: 0, max: 365,
                hint: 'Prompt notes have no producer-side retirement; this TTL is their backstop. 0 = keep forever.',
            },
            {
                key: 'adaptive_harmful_retire_min_uses',
                label: 'Failure-dominated Retire — Min Outcomes',
                type: 'number', min: 0, max: 100,
                hint: 'An entry with at least this many attributed outcomes (successes + failures from synthesis) whose success share falls below the threshold retires even though it is used. 0 disables.',
            },
            {
                key: 'adaptive_harmful_retire_max_success',
                label: 'Failure-dominated Retire — Success Floor (0–1 fraction)',
                type: 'number', min: 0, max: 1, step: 0.05,
                hint: 'Success share below this = failure-dominated. Journaled soft-delete, one-click rollback, candor/user sources exempt.',
            },
            {
                key: 'adaptive_suspect_ttl_days',
                label: 'Passive Suspect-flag TTL (days)',
                type: 'number', min: 0, max: 90,
                hint: 'A suspect flag from the passive post-mortem signal alone can never self-clear; it auto-clears after this many days. Canary-confirmed flags are exempt. 0 = flags wait for your dismiss.',
            },
            {
                key: 'adaptive_agent_notes_enabled',
                label: 'Agent Self-notes (adaptive_note tool)',
                type: 'bool',
                risk: 'autonomy',
                restart: RESTART_TOOLS,
                hint: 'Lets the live agent mint prompt notes and routing hints the moment it learns something — content lint applies, 2/day, normal pipeline + tripwire, never policy.',
            },
        ],
    },
    {
        title: 'Telos (Teleological Layer)',
        description: 'The operational question loop (carved down in v3.1): turn anomalies the rest of the system cannot explain mint questions, the SOUP generates falsifiable hypotheses at idle, supported claims can become scout routing hints, and a weekly entropy control keeps exploration from going stale. State lives in data/telos/ as markdown. Enabling the agent tools needs a restart; everything else applies immediately.',
        fields: [
            { key: 'telos_enabled', label: 'Telos Enabled', type: 'bool', restart: RESTART_TOOLS },
            { key: 'telos_schedule', label: 'Slow-loop Schedule (cron)', type: 'text' },
            { key: 'telos_serendipity_budget', label: 'Serendipity Budget (0–1 fraction)', type: 'number', step: 0.05 },
            { key: 'telos_eig_floor', label: 'Gate EIG Floor (0–1 fraction)', type: 'number', step: 0.05 },
            { key: 'telos_hypotheses_per_question', label: 'Hypotheses / Question', type: 'number' },
        ],
    },
    {
        title: 'Backups',
        description: 'The 24-hour maintenance tier writes a timestamped snapshot of the session database (SQLite VACUUM INTO, so it is consistent without stopping writes) plus a copy of the memory corpus into data/backups. Rotation is per-artifact — database snapshots and memory corpora rotate independently — so a restore always finds a matching pair. Snapshots are roughly the size of your live database, so the count is a disk-space decision.',
        fields: [
            {
                key: 'backup_keep_count',
                label: 'Snapshots to Keep',
                type: 'number',
                min: 0,
                max: 90,
                hint: '0 disables scheduled backups entirely. Values are clamped to 0–90 when the backup runs.',
            },
        ],
    },
    {
        title: 'Webhook Notifications',
        description: 'Pernix POSTs a JSON body to this URL whenever the agent calls ask_user and needs a human — the escape hatch for long autonomous runs you are not watching in the browser. Pair it with ntfy, Pushover, Slack, Discord or a home-automation hook. Leave the URL empty to disable.',
        fields: [
            {
                key: 'notify_webhook_url',
                label: 'Webhook URL',
                type: 'writeonly',
                hint: 'Write-only: the server redacts this value from the settings API, so the current URL is '
                    + 'never shown here. Type a new URL to replace it. Removing one entirely means editing '
                    + 'notify_webhook_url in data/settings.json — the API refuses to blank a URL field.',
            },
            { key: 'notify_webhook_timeout', label: 'Webhook Timeout (seconds)', type: 'number', min: 1, max: 60 },
        ],
    },
];

const MODEL_SELECT_FIELDS = [
    // Three chat-model roles (2026-08 consolidation), any provider:
    // Primary = agent turns + quality-critical calls (compaction/reflect/eval);
    // Background = fast/offline tier (scout, titles, distill, snooze, dream,
    // telos, RLM sub-calls); Backup = used when Primary or Background fail.
    { key: 'llm_model', label: 'Primary Model', type: 'model-select' },
    { key: 'background_model', label: 'Background Model (scout/titles/idle work; empty = Primary)', type: 'model-select', allowEmpty: true },
    { key: 'fallback_model', label: 'Backup Model (used when Primary or Background fail)', type: 'model-select', allowEmpty: true },
    // Free text, not model-select: embedding models (nomic-embed-text, ...)
    // don't appear in the chat-model dropdown. Empty = lexical search only.
    { key: 'embedding_model', label: 'Embedding Model (Ollama; empty = lexical search only)', type: 'text' },
];

// ---------------------------------------------------------------------------
// Help tooltip
// ---------------------------------------------------------------------------

let _descUid = 0;

// A section's description used to live ONLY inside the `?` tooltip, which is
// hover-only — on a phone or tablet the entire explanation was unreachable.
// It now also renders as a paragraph the user can open in place. Collapsed by
// default so the 100+ controls stay scannable. (S6)
function buildSectionDesc(description) {
    const body = el('p', { class: 'settings-section-desc', id: `settings-desc-${++_descUid}` },
        [text(description)]);
    body.hidden = true;
    const toggle = el('button', {
        type: 'button',
        class: 'settings-section-desc-toggle',
        'aria-expanded': 'false',
        'aria-controls': body.id,
    }, [text('What\u2019s this?')]);
    toggle.addEventListener('click', () => {
        const opening = body.hidden;
        body.hidden = !opening;
        toggle.setAttribute('aria-expanded', String(opening));
        toggle.textContent = opening ? 'Hide' : 'What\u2019s this?';
    });
    return el('div', { class: 'settings-section-desc-wrap' }, [toggle, body]);
}

function buildHelpIcon(tip) {
    const icon = el('span', { class: 'section-help', tabindex: '0' }, [text('?')]);
    const tooltip = el('span', { class: 'section-help-tip' }, [text(tip)]);
    const wrapper = el('span', { class: 'section-help-wrap' }, [icon, tooltip]);

    const position = () => {
        const r = icon.getBoundingClientRect();
        tooltip.style.left = `${r.left + r.width / 2 - 140}px`;
        tooltip.style.top = `${r.bottom + 6}px`;
    };
    icon.addEventListener('mouseenter', position);
    icon.addEventListener('focus', position);

    return wrapper;
}

// ---------------------------------------------------------------------------
// Network & Security section wiring
// ---------------------------------------------------------------------------

let _restartRequired = false;

function _updateNetworkVisibility() {
    const enabled = document.getElementById('setting-network_enabled');
    const sslModeRow = document.getElementById('setting-ssl_mode')?.closest('.setting-row');
    const certRow = document.getElementById('row-ssl_cert_path');
    const keyRow = document.getElementById('row-ssl_key_path');
    const warning = document.getElementById('network-warning');
    const qrSection = document.getElementById('qr-access-section');
    const originsSection = document.getElementById('origins-section');

    const isEnabled = enabled?.checked;
    const sslMode = document.getElementById('setting-ssl_mode')?.value;
    const isCustom = isEnabled && sslMode === 'custom';

    if (sslModeRow) sslModeRow.style.display = isEnabled ? '' : 'none';
    if (certRow) certRow.style.display = isCustom ? '' : 'none';
    if (keyRow) keyRow.style.display = isCustom ? '' : 'none';
    if (warning) warning.style.display = isEnabled ? '' : 'none';
    if (originsSection) originsSection.style.display = isEnabled ? '' : 'none';
    // QR section only shows when network is already active (not just toggled on — needs restart first)
    if (qrSection) qrSection.style.display = (_original.network_enabled && _original.auth_token_set) ? '' : 'none';
}

function _wireNetworkSection() {
    // Insert warning banner at the top of the Network & Security section
    const networkToggle = document.getElementById('setting-network_enabled');
    if (!networkToggle) return;

    const section = networkToggle.closest('.settings-section');
    if (!section) return;

    const warning = el('div', {
        id: 'network-warning',
        style: `display: ${networkToggle.checked ? '' : 'none'}; `
             + 'background: var(--surface-hover, #2a1a1a); '
             + 'border-left: 3px solid var(--error, #e55); '
             + 'padding: 0.6rem 0.8rem; margin-bottom: 0.8rem; '
             + 'border-radius: 4px; font-size: 0.85rem; line-height: 1.4;',
    }, [
        el('strong', {}, [text('Network Mode: ')]),
        text('All API endpoints are protected by a Bearer token (auto-generated on first start). '),
        text('Share the access URL or QR code from the server console to connect remote devices. '),
        text('Self-signed certificates will show a browser warning on first visit.'),
    ]);

    // Insert warning after the h3 heading
    const heading = section.querySelector('h3');
    if (heading) {
        heading.after(warning);
    }

    // Wire visibility toggles
    networkToggle.addEventListener('change', _updateNetworkVisibility);
    const sslModeSelect = document.getElementById('setting-ssl_mode');
    if (sslModeSelect) {
        sslModeSelect.addEventListener('change', _updateNetworkVisibility);
    }

    // Initial visibility
    _updateNetworkVisibility();
}

// ---------------------------------------------------------------------------
// Voice Input section — per-engine privacy disclaimer + field visibility
// ---------------------------------------------------------------------------

const _VOICE_DISCLAIMERS = {
    off: 'Voice input is disabled — no mic button in the chat bar.',
    local_whisper: 'Private: recordings are transcribed on the Pernix server by faster-whisper and never leave your machines. Requires the faster-whisper package on the server; the chosen model downloads on first use (~150MB for "base").',
    remote_whisper: 'Your voice recordings are uploaded to the endpoint configured below for transcription. Whoever operates that endpoint receives your audio.',
    model_direct: 'Recordings attach to your message and the active chat model hears the audio itself — requires an audio-capable model. Audio stays on this machine with Ollama models, but leaves it when the model runs at a cloud provider.',
    web_speech: 'Dictation uses your browser vendor’s speech service (e.g. Google for Chrome). Your voice audio is sent to that vendor and processed under their privacy policy — it does not stay on your machines. Requires internet; not supported by every browser.',
};

let _voiceStatus = null; // /api/voice/status snapshot fetched when the modal opens

function _updateVoiceVisibility() {
    const mode = document.getElementById('setting-voice_mode')?.value || 'off';
    const rowVisibility = {
        voice_whisper_model: mode === 'local_whisper',
        voice_remote_url: mode === 'remote_whisper',
        voice_remote_model: mode === 'remote_whisper',
        voice_stt_api_key: mode === 'remote_whisper',
        voice_language: mode === 'local_whisper' || mode === 'remote_whisper' || mode === 'web_speech',
        // Auto-send needs a transcript to prove speech was captured, so it
        // only applies to the transcription engines — model_direct voice
        // notes stay manual.
        voice_auto_send: mode === 'local_whisper' || mode === 'remote_whisper' || mode === 'web_speech',
        // web_speech IS the fallback — offering it as its own fallback is noise
        voice_web_speech_fallback: mode !== 'off' && mode !== 'web_speech',
    };
    for (const [key, show] of Object.entries(rowVisibility)) {
        const row = document.getElementById(`setting-${key}`)?.closest('.setting-row');
        if (row) row.style.display = show ? '' : 'none';
    }

    const disc = document.getElementById('voice-disclaimer');
    if (disc) {
        disc.textContent = _VOICE_DISCLAIMERS[mode] || '';
        const warn = mode === 'web_speech';
        disc.style.borderLeftColor = warn ? 'var(--error, #e55)' : 'var(--accent-dim, #888)';
        disc.style.background = warn ? 'var(--surface-hover, #2a1a1a)' : 'var(--bg-soft, rgba(255,255,255,0.04))';
    }

    const note = document.getElementById('voice-fallback-note');
    if (note) note.style.display = rowVisibility.voice_web_speech_fallback ? '' : 'none';

    const testRow = document.getElementById('voice-test-row');
    if (testRow) testRow.style.display = mode === 'off' ? 'none' : 'flex';

    // Server-side reality check for the selected engine
    const statusLine = document.getElementById('voice-status-line');
    if (statusLine) {
        const st = _voiceStatus;
        let msg = '';
        if (st) {
            if (mode === 'local_whisper' && !st.whisper_installed) {
                msg = 'Server check: faster-whisper is not installed — pip install faster-whisper on the server.';
            } else if (mode === 'remote_whisper' && !st.remote_key_set) {
                msg = 'Server check: no Remote STT API key is set (fine for endpoints that don’t need one).';
            } else if (mode === 'model_direct' && st.mode === 'model_direct' && !st.usable) {
                msg = `Server check: ${st.reason}.`;
            }
        }
        statusLine.textContent = msg;
        statusLine.style.display = msg ? '' : 'none';
    }
}

function _wireVoiceSection() {
    const modeSelect = document.getElementById('setting-voice_mode');
    if (!modeSelect) return;
    const section = modeSelect.closest('.settings-section');
    if (!section) return;

    const boxStyle =
        'border-left: 3px solid var(--accent-dim, #888); '
        + 'background: var(--bg-soft, rgba(255,255,255,0.04)); '
        + 'padding: 0.6rem 0.8rem; margin: 0.5rem 0 0.8rem; '
        + 'border-radius: 4px; font-size: 0.85rem; line-height: 1.4;';

    const disclaimer = el('div', { id: 'voice-disclaimer', style: boxStyle });
    const statusLine = el('div', {
        id: 'voice-status-line',
        style: 'display: none; color: var(--error, #e55); font-size: 0.85rem; margin: 0 0 0.6rem;',
    });
    // Disclaimer sits directly under the engine selector, where the choice is made
    const modeRow = modeSelect.closest('.setting-row');
    modeRow.after(statusLine);
    modeRow.after(disclaimer);

    // The fallback toggle re-introduces the web_speech privacy trade-off, so
    // it carries its own acknowledgment text — enabling it IS the consent.
    const fallbackRow = document.getElementById('setting-voice_web_speech_fallback')?.closest('.setting-row');
    if (fallbackRow) {
        fallbackRow.after(el('div', {
            id: 'voice-fallback-note',
            style: boxStyle + 'border-left-color: var(--error, #e55);',
        }, [
            text('When the engine above is unavailable (whisper missing, model can’t hear audio), '
                + 'dictation falls back to your browser vendor’s speech service — your voice audio leaves '
                + 'this machine, same as Browser Dictation. Enabling this switch is your acknowledgment of that.'),
        ]));
    }

    // Test button — records a short clip and round-trips it through the
    // SAVED engine, so "it's configured" and "it works" stop being different
    // things. Unsaved edits would test the wrong engine; require save first.
    const testResult = el('span', {
        id: 'voice-test-result',
        style: 'font-size: 0.85rem; margin-left: 0.6rem; line-height: 1.4;',
    });
    const testBtn = el('button', { class: 'btn btn-secondary', id: 'voice-test-btn' }, [text('Test')]);
    const testRow = el('div', {
        id: 'voice-test-row',
        style: 'display: flex; align-items: center; margin: 0.2rem 0 0.8rem;',
    }, [testBtn, testResult]);
    section.appendChild(testRow);
    testBtn.addEventListener('click', () => _runVoiceTestClick(testBtn, testResult));

    modeSelect.addEventListener('change', _updateVoiceVisibility);
    _updateVoiceVisibility();

    // Availability facts (whisper installed? key set? model hears audio?)
    // arrive async — re-render the status line once they do.
    get('/api/voice/status')
        .then(st => { _voiceStatus = st; _updateVoiceVisibility(); })
        .catch(() => { _voiceStatus = null; });
}

async function _runVoiceTestClick(btn, resultEl) {
    const mode = document.getElementById('setting-voice_mode')?.value || 'off';
    resultEl.style.color = 'var(--text-dim)';
    if (mode === 'off') {
        resultEl.textContent = 'Select an engine first.';
        return;
    }
    const relevant = ['voice_mode', 'voice_whisper_model', 'voice_remote_url', 'voice_remote_model', 'voice_language'];
    const dirty = relevant.some(k => {
        const inp = document.getElementById(`setting-${k}`);
        return inp && inp.value !== String(_original[k] ?? '');
    });
    if (dirty) {
        resultEl.textContent = 'Save your changes first — Test runs the saved configuration.';
        return;
    }
    btn.disabled = true;
    resultEl.textContent = '';
    try {
        const res = await runVoiceTest(mode, (phase) => {
            btn.textContent = phase === 'listening' ? 'Listening — speak now…'
                : phase === 'transcribing' ? 'Transcribing…'
                : 'Checking…';
        });
        resultEl.style.color = res.ok ? 'var(--accent)' : 'var(--error, #e55)';
        resultEl.textContent = res.ok
            ? (res.text ? `✓ Heard: “${res.text}”` : `✓ ${res.detail}`)
            : `✗ ${res.error}`;
    } finally {
        btn.disabled = false;
        btn.textContent = 'Test';
    }
}

function _showRestartButton(reason = 'network changes') {
    const footer = document.querySelector('.modal-footer');
    if (!footer) return;

    const statusEl = footer.querySelector('.save-status');
    const message = `Saved \u00b7 restart required for: ${reason}`;

    // The restart endpoint is loopback-only (403 otherwise). A phone or
    // LAN-IP browser used to get the button anyway and a dead-end error —
    // show instructions instead.
    const isLoopback = ['localhost', '127.0.0.1', '[::1]'].includes(window.location.hostname);
    if (!isLoopback) {
        if (statusEl) {
            statusEl.className = 'save-status status-warn';
            statusEl.textContent =
                `${message} — restart from the server console (or from a localhost browser).`;
            announce(statusEl.textContent);
        }
        return;
    }

    if (statusEl) {
        statusEl.className = 'save-status status-warn';
        statusEl.textContent = message;
        announce(statusEl.textContent);
    }
    if (document.getElementById('restart-server-btn')) return;

    const restartBtn = el('button', {
        class: 'btn btn-primary btn-danger',
        id: 'restart-server-btn',
        onClick: async () => {
            restartBtn.disabled = true;
            restartBtn.textContent = 'Restarting\u2026';
            if (statusEl) {
                statusEl.className = 'save-status';
                statusEl.textContent = 'Server restarting\u2026';
            }
            try {
                const result = await post('/api/admin/restart');
                const newUrl = result.url;
                if (statusEl) {
                    statusEl.textContent = `Redirecting to ${newUrl} in a few seconds\u2026`;
                }
                // Wait for server to come back, then redirect
                setTimeout(() => { window.location.href = newUrl; }, 4000);
            } catch (e) {
                restartBtn.disabled = false;
                restartBtn.textContent = 'Restart Server';
                if (statusEl) {
                    statusEl.className = 'save-status status-error';
                    statusEl.textContent = `Restart failed: ${e.message}`;
                }
            }
        },
    }, [text('Restart Server')]);

    // Insert before Save button
    const saveBtn = document.getElementById('settings-save-btn');
    if (saveBtn) {
        saveBtn.before(restartBtn);
    } else {
        footer.appendChild(restartBtn);
    }
}

// ---------------------------------------------------------------------------
// Field builder
// ---------------------------------------------------------------------------

function buildModelSelect(key, value, allowEmpty) {
    const opts = [];
    if (allowEmpty) {
        opts.push(el('option', { value: '' }, [text('— none —')]));
    }

    // Group by provider
    const byProvider = {};
    for (const m of _availableModels) {
        const p = m.provider || 'unknown';
        if (!byProvider[p]) byProvider[p] = [];
        byProvider[p].push(m);
    }

    // If current value isn't in the list, add it so it doesn't get cleared
    const allIds = new Set(_availableModels.map(m => m.id));
    const currentMissing = value && !allIds.has(value);

    if (currentMissing) {
        opts.push(el('option', { value, selected: '' }, [text(`${value} (unavailable)`)]));
    }

    // Render optgroups, Ollama first
    const providerOrder = ['ollama', 'openrouter', 'openai',
        ...Object.keys(byProvider).filter(p => p !== 'ollama' && p !== 'openrouter' && p !== 'openai')];
    const providerLabels = { ollama: 'Ollama', openrouter: 'OpenRouter', openai: 'OpenAI' };

    for (const provider of providerOrder) {
        const models = byProvider[provider];
        if (!models || models.length === 0) continue;

        const groupOpts = models.map(m =>
            el('option', { value: m.id, ...(!currentMissing && m.id === value ? { selected: '' } : {}) },
                [text(m.id)]
            )
        );
        opts.push(el('optgroup', { label: providerLabels[provider] || provider }, groupOpts));
    }

    return el('select', { id: `setting-${key}` }, opts);
}

// Write-only fields whose value the server redacts. Some report a companion
// <key>_set flag; notify_webhook_url does not, so we must not claim "Not set"
// for a webhook that may well be configured.
function _writeOnlyPlaceholder(key) {
    const flag = _original[key + '_set'];
    if (flag === undefined) return 'Hidden — type a value to replace it';
    return flag ? 'Set (hidden for security)' : 'Not set';
}

function _buildBadge(cls, label, tip) {
    return el('span', { class: `setting-badge ${cls}`, title: tip, tabindex: '0' }, [text(label)]);
}

// Label column: the name, any risk/restart/locked badges, and an optional
// plain-language hint. Badges carry their explanation in `title` so the row
// stays scannable and the detail is one hover/focus away.
function _buildLabelCell(field) {
    const { key, label, risk, restart, hint } = field;
    const badges = [];
    if (risk === 'security') {
        badges.push(_buildBadge('badge-security', 'security',
            'Security-relevant: a wrong value here changes who can reach this server or what the agent may touch.'));
    } else if (risk === 'autonomy') {
        badges.push(_buildBadge('badge-autonomy', 'autonomy',
            'Autonomy/spend: this lets the agent act or spend tokens without a human in the loop.'));
    }
    if (LOCKED_KEYS.has(key)) badges.push(_buildBadge('badge-locked', 'locked', LOCKED_NOTE));
    if (restart) badges.push(_buildBadge('badge-restart', 'restart', restart));

    const children = [
        el('span', { class: 'setting-label-line' }, [
            el('label', { for: `setting-${key}` }, [text(label)]),
            ...badges,
        ]),
    ];
    if (hint) children.push(el('span', { class: 'setting-hint' }, [text(hint)]));
    return el('div', { class: 'setting-label-cell' }, children);
}

function buildField(field, value) {
    const { key, label, type, step, options, allowEmpty, min, max } = field;
    const locked = LOCKED_KEYS.has(key);

    let input;
    if (type === 'apikey') {
        // Masked API key field — never shows real value
        const isSet = !!_original[key + '_set'];
        input = el('input', {
            type: 'password',
            id: `setting-${key}`,
            placeholder: isSet ? '••••••••' : 'Not set',
            value: '',
            autocomplete: 'off',
        });
    } else if (type === 'certpath' || type === 'writeonly') {
        // Write-only field — the server redacts the stored value, so the input
        // starts empty and only a typed value is ever sent.
        input = el('input', {
            type: 'text',
            id: `setting-${key}`,
            placeholder: _writeOnlyPlaceholder(key),
            value: '',
            autocomplete: 'off',
        });
    } else if (type === 'bool') {
        input = el('input', { type: 'checkbox', id: `setting-${key}` });
        input.checked = !!value;
    } else if (type === 'model-select') {
        input = buildModelSelect(key, value, allowEmpty);
    } else if (type === 'select') {
        // Options are either raw strings ('self_signed') or {value, label}
        // pairs when the stored value shouldn't double as the display text.
        input = el('select', { id: `setting-${key}` },
            (options || []).map(opt => {
                const val = typeof opt === 'object' ? opt.value : opt;
                const label = typeof opt === 'object' ? opt.label : opt;
                return el('option', { value: val, ...(val === value ? { selected: '' } : {}) }, [text(label)]);
            })
        );
    } else if (type === 'number') {
        input = el('input', {
            type: 'number',
            id: `setting-${key}`,
            value: String(value ?? ''),
            step: String(step || (Number.isInteger(value) ? 1 : 0.01)),
            ...(min === undefined ? {} : { min: String(min) }),
            ...(max === undefined ? {} : { max: String(max) }),
        });
    } else {
        input = el('input', { type: 'text', id: `setting-${key}`, value: String(value ?? '') });
    }

    if (locked) {
        input.disabled = true;
        input.setAttribute('aria-describedby', `locked-${key}`);
    }

    const rowChildren = [_buildLabelCell(field), input];
    const row = el('div', {
        // Toggles keep the label and the box on one line even on a phone: the
        // label is the real tap target (for=), so a full-width label beats a
        // 24px checkbox stacked under its own caption.
        class: `setting-row${type === 'bool' ? ' setting-row-bool' : ''}`
             + `${locked ? ' setting-locked' : ''}${field.risk ? ' setting-risk-' + field.risk : ''}`,
        id: `row-${key}`,
        'data-key': key,
    }, rowChildren);

    if (locked) {
        row.append(el('span', { class: 'setting-locked-note', id: `locked-${key}` }, [text(LOCKED_NOTE)]));
    }
    return row;
}

function _rebuildModelSelects() {
    const allFields = [
        ...SECTIONS.flatMap(s => s.fields.filter(f => f.type === 'model-select')),
        ...MODEL_SELECT_FIELDS,
    ];
    for (const field of allFields) {
        const existing = document.getElementById(`setting-${field.key}`);
        if (!existing) continue;
        const currentValue = existing.value;
        const replacement = buildModelSelect(field.key, currentValue, field.allowEmpty);
        existing.replaceWith(replacement);
    }
}

function allSettingFields() {
    return [...SECTIONS.flatMap(s => s.fields), ...NETWORK_FIELDS, ...MODEL_SELECT_FIELDS];
}

function _labelFor(key) {
    return allSettingFields().find(f => f.key === key)?.label || key;
}

// Put a control back to the value the server actually holds. Used when a save
// comes back without the key we asked for, so the UI never keeps displaying a
// value the server refused.
function _revertField(key) {
    const field = allSettingFields().find(f => f.key === key);
    const input = document.getElementById(`setting-${key}`);
    if (field && input) {
        const prev = _original[key];
        if (field.type === 'bool') input.checked = !!prev;
        else if (field.type === 'certpath' || field.type === 'writeonly') {
            input.value = '';
            input.placeholder = _writeOnlyPlaceholder(key);
        } else input.value = prev === undefined || prev === null ? '' : String(prev);
        return;
    }
    // List-valued editors keep their state outside the DOM.
    if (key === 'openrouter_models') {
        _models = (_original.openrouter_models || []).map(id => ({ id, valid: null, info: null }));
        renderModelList();
    } else if (key === 'cors_origins') {
        const editor = document.getElementById('origins-editor');
        if (editor) editor.replaceWith(_buildOriginsEditor(_original.cors_origins || []));
    }
}

function collectChanges() {
    const changes = {};
    const allFields = [...SECTIONS.flatMap(s => s.fields), ...NETWORK_FIELDS];
    for (const field of allFields) {
        const input = document.getElementById(`setting-${field.key}`);
        if (!input) continue;

        if (field.type === 'apikey') continue;  // handled separately
        // Locked fields render disabled; sending them would be a no-op the
        // server drops silently, which is exactly the lie we're removing.
        if (LOCKED_KEYS.has(field.key)) continue;
        if (field.type === 'certpath' || field.type === 'writeonly') {
            // Write-only: only a typed value is ever sent. There is no "clear"
            // path — update_settings rejects an empty string for any *_url
            // field that currently holds a value.
            const val = input.value.trim();
            if (val) changes[field.key] = val;
            continue;
        }

        let newVal;
        if (field.type === 'bool') {
            newVal = input.checked;
        } else if (field.type === 'number') {
            newVal = input.value === '' ? _original[field.key] : Number(input.value);
        } else {
            newVal = input.value;
        }

        if (newVal !== _original[field.key]) {
            changes[field.key] = newVal;
        }
    }

    // Include model selects from Models tab
    for (const field of MODEL_SELECT_FIELDS) {
        const input = document.getElementById(`setting-${field.key}`);
        if (!input) continue;
        if (input.value !== _original[field.key]) {
            changes[field.key] = input.value;
        }
    }

    // Include model list if changed
    const currentIds = _models.map(m => m.id);
    const originalIds = (_original.openrouter_models || []);
    if (JSON.stringify(currentIds) !== JSON.stringify(originalIds)) {
        changes.openrouter_models = currentIds;
    }

    // shell_env_mode / shell_env_denylist / shell_env_allowlist are deliberately
    // absent: the settings API locks them, so posting them changed nothing and
    // reported success anyway. The Environment tab now renders them read-only.

    // Include CORS origins if changed
    if (JSON.stringify(_corsOrigins) !== JSON.stringify(_original.cors_origins || [])) {
        changes.cors_origins = _corsOrigins;
    }

    return changes;
}

// ---------------------------------------------------------------------------
// Models tab
// ---------------------------------------------------------------------------

function formatBytes(bytes) {
    if (!bytes) return '';
    const gb = bytes / (1024 * 1024 * 1024);
    return gb >= 1 ? `${gb.toFixed(1)} GB` : `${(bytes / (1024 * 1024)).toFixed(0)} MB`;
}

async function refreshOllamaModels() {
    _ollamaLoading = true;
    _ollamaError = '';
    renderOllamaList();
    try {
        const data = await get('/api/models/ollama');
        _ollamaModels = data.models || [];
        _ollamaError = data.error || '';
    } catch (e) {
        _ollamaModels = [];
        _ollamaError = e.message || 'Failed to fetch';
    }
    _ollamaLoading = false;
    renderOllamaList();
}

function renderOllamaList() {
    const listEl = document.getElementById('ollama-model-list');
    if (!listEl) return;
    clear(listEl);

    const refreshBtn = document.getElementById('ollama-refresh-btn');
    if (refreshBtn) {
        refreshBtn.classList.toggle('spinning', _ollamaLoading);
    }

    if (_ollamaLoading) {
        listEl.appendChild(el('div', { class: 'or-empty' }, [text('Loading\u2026')]));
        return;
    }

    if (_ollamaError) {
        listEl.appendChild(el('div', { class: 'ollama-error' }, [text(_ollamaError)]));
    }

    if (_ollamaModels.length === 0 && !_ollamaError) {
        listEl.appendChild(el('div', { class: 'or-empty' }, [text('No models found on Ollama server')]));
        return;
    }

    for (const m of _ollamaModels) {
        const meta = [m.parameter_size, m.quantization, formatBytes(m.size)]
            .filter(Boolean).join(' \u00b7 ');
        const item = el('div', { class: 'or-model-item' }, [
            el('span', { class: 'or-model-id' }, [text(m.name)]),
            ...(meta ? [el('span', { class: 'or-model-info' }, [text(meta)])] : []),
        ]);
        listEl.appendChild(item);
    }
}

function buildOllamaSection() {
    const listEl = el('div', { class: 'or-model-list', id: 'ollama-model-list' });
    const refreshBtn = el('button', {
        class: 'ollama-refresh', id: 'ollama-refresh-btn', title: 'Refresh model list',
        'aria-label': 'Refresh the Ollama model list',
    }, [text('\u21bb')]);
    refreshBtn.addEventListener('click', refreshOllamaModels);

    const section = el('div', { class: 'settings-section' }, [
        el('div', { class: 'ollama-header' }, [
            el('h3', {}, [text('Ollama Models')]),
            refreshBtn,
        ]),
        el('p', { class: 'or-hint' }, [text('Models available on the configured Ollama server.')]),
        listEl,
    ]);

    renderOllamaList();
    return section;
}

function buildModelsTab() {
    const listEl = el('div', { class: 'or-model-list', id: 'or-model-list' });

    const addInput = el('input', {
        type: 'text',
        class: 'or-model-input',
        placeholder: 'e.g. anthropic/claude-sonnet-4',
    });

    const addBtn = el('button', { class: 'btn btn-primary btn-sm' }, [text('Add')]);
    addBtn.addEventListener('click', () => addModel(addInput, listEl));
    addInput.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') addModel(addInput, listEl);
    });

    const container = el('div', { class: 'settings-section' }, [
        el('h3', {}, [text('OpenRouter Models')]),
        el('p', { class: 'or-hint' }, [text('Add OpenRouter model IDs to make them available for any model role.')]),
        listEl,
        el('div', { class: 'or-model-add' }, [addInput, addBtn]),
    ]);

    renderModelList(listEl);

    // Model role selects
    const selectFields = MODEL_SELECT_FIELDS.map(f => buildField(f, _original[f.key]));
    const selectSection = el('div', { class: 'settings-section' }, [
        el('h3', {}, [text('Model Roles')]),
        el('p', { class: 'or-hint' }, [text('Primary handles conversations and every quality-critical call (compaction summaries, reflect verdicts, eval). Background is the fast/offline tier: scout planning, titles, memory distillation, idle-time work, and RLM sub-calls. Backup is used whenever a Primary or Background call fails. Any configured provider works for any role.')]),
        ...selectFields,
    ]);

    return el('div', {}, [selectSection, buildOllamaSection(), container]);
}

function renderModelList(listEl) {
    if (!listEl) listEl = document.getElementById('or-model-list');
    if (!listEl) return;
    clear(listEl);

    if (_models.length === 0) {
        listEl.appendChild(el('div', { class: 'or-empty' }, [text('No OpenRouter models configured')]));
        return;
    }

    for (let i = 0; i < _models.length; i++) {
        const m = _models[i];
        const idx = i;

        // Status indicator
        let statusEl;
        if (m.valid === null) {
            statusEl = el('span', { class: 'or-status checking' }, [text('\u2022')]);
        } else if (m.valid) {
            statusEl = el('span', { class: 'or-status valid' }, [text('\u2713')]);
        } else {
            statusEl = el('span', { class: 'or-status invalid' }, [text('\u2717')]);
        }

        // Pricing info
        let infoText = '';
        if (m.valid && m.info) {
            const pc = formatCost(m.info.prompt_cost);
            const cc = formatCost(m.info.completion_cost);
            const ctx = m.info.context_length ? `${Math.round(m.info.context_length / 1000)}k ctx` : '';
            infoText = `${pc}/${cc}${ctx ? ' ' + ctx : ''}`;
        } else if (m.valid === false) {
            infoText = 'not found';
        }

        const removeBtn = el('button', {
            class: 'or-remove', title: 'Remove', 'aria-label': `Remove the model ${m.id}`,
        }, [text('\u00d7')]);
        removeBtn.addEventListener('click', () => {
            _models.splice(idx, 1);
            renderModelList(listEl);
        });

        const item = el('div', { class: 'or-model-item' }, [
            statusEl,
            el('span', { class: 'or-model-id' }, [text(m.id)]),
            ...(infoText ? [el('span', { class: 'or-model-info' }, [text(infoText)])] : []),
            removeBtn,
        ]);
        listEl.appendChild(item);
    }
}

function addModel(inputEl, listEl) {
    const id = inputEl.value.trim();
    if (!id) return;
    if (_models.some(m => m.id === id)) {
        inputEl.style.borderColor = 'var(--error)';
        setTimeout(() => { inputEl.style.borderColor = ''; }, 1500);
        return;
    }

    _models.push({ id, valid: null, info: null });
    inputEl.value = '';
    renderModelList(listEl);
    validateModel(_models.length - 1, listEl);
}

async function validateModel(index, listEl) {
    if (index >= _models.length) return;
    const m = _models[index];
    try {
        const result = await get(`/api/models/validate?model=${encodeURIComponent(m.id)}`);
        if (index < _models.length && _models[index].id === m.id) {
            _models[index].valid = result.valid;
            _models[index].info = result;
            renderModelList(listEl);
        }
    } catch {
        if (index < _models.length && _models[index].id === m.id) {
            _models[index].valid = false;
            renderModelList(listEl);
        }
    }
}

function validateAllModels(listEl) {
    for (let i = 0; i < _models.length; i++) {
        validateModel(i, listEl);
    }
}

function formatCost(costStr) {
    const cost = parseFloat(costStr || '0');
    if (cost === 0) return 'free';
    // OpenRouter costs are per-token, convert to per 1M tokens
    const perMillion = cost * 1_000_000;
    if (perMillion >= 1) return `$${perMillion.toFixed(perMillion >= 10 ? 0 : 1)}`;
    return `$${perMillion.toFixed(2)}`;
}

// ---------------------------------------------------------------------------
// Environment tab
// ---------------------------------------------------------------------------

let _envVarNames = [];  // host env var names from GET /api/env-vars
let _envDenylist = [];
let _envAllowlist = [];

// What each mode hands to a bash child. Wording is deliberately blunt about
// the blast radius: the server process holds every provider API key, so
// "passthrough" copies all of them into every command the agent runs, and into
// anything that command spawns.
const _ENV_MODE_HINTS = {
    passthrough: 'Every host environment variable — including OPENROUTER_API_KEY, OPENAI_API_KEY, '
        + 'TAVILY_API_KEY and anything else this process holds — is copied into every shell command '
        + 'and into whatever those commands spawn. Any command that exfiltrates its own environment '
        + 'exfiltrates your keys.',
    denylist: 'Every host variable is passed EXCEPT the ones listed below. Safer than passthrough, '
        + 'but it fails open: a key added to the environment later is passed until someone remembers '
        + 'to deny it.',
    allowlist: 'Only the variables listed below are passed. PATH, HOME and VIRTUAL_ENV are then set '
        + 'explicitly to the workspace venv, so the sandbox still works. Fails closed — a new API key '
        + 'in the environment is not handed out unless you add it here. This is the default.',
};

function buildEnvTab(settings) {
    _envDenylist = [...(settings.shell_env_denylist || [])];
    _envAllowlist = [...(settings.shell_env_allowlist || [])];

    // Matches the config.py default. It changed passthrough -> allowlist when
    // passthrough was found to hand every provider key to every shell child.
    const mode = settings.shell_env_mode || 'allowlist';

    const modeSelect = el('select', { id: 'setting-shell_env_mode' },
        ['passthrough', 'denylist', 'allowlist'].map(opt =>
            el('option', { value: opt, ...(opt === mode ? { selected: '' } : {}) }, [text(opt)])
        )
    );
    modeSelect.disabled = true;

    const hintEl = el('p', {
        class: `or-hint env-mode-hint${mode === 'passthrough' ? ' env-mode-unsafe' : ''}`,
        id: 'env-mode-hint',
    }, [text(_ENV_MODE_HINTS[mode] || '')]);

    const listEl = el('div', { class: 'or-model-list', id: 'env-var-list' });
    _renderEnvList(listEl, mode);

    const modeRow = el('div', {
        class: 'setting-row setting-locked setting-risk-security',
        id: 'row-shell_env_mode',
        'data-key': 'shell_env_mode',
    }, [
        _buildLabelCell({ key: 'shell_env_mode', label: 'Env Mode', risk: 'security' }),
        modeSelect,
        el('span', { class: 'setting-locked-note' }, [text(LOCKED_NOTE)]),
    ]);

    return el('div', { class: 'settings-section' }, [
        el('h3', {}, [
            text('Shell Environment'),
            buildHelpIcon(
                'Which host environment variables a bash tool call can see. This whole group is '
                + 'edit-locked in the settings API — a prompt-injected agent must not be able to POST '
                + 'itself back to passthrough and read every API key. Change shell_env_mode, '
                + 'shell_env_allowlist and shell_env_denylist in data/settings.json, then restart.'
            ),
        ]),
        modeRow,
        hintEl,
        listEl,
    ]);
}

function _getActiveEnvList(mode) {
    return mode === 'denylist' ? _envDenylist : _envAllowlist;
}

function _renderEnvList(listEl, mode) {
    clear(listEl);

    if (mode === 'passthrough') {
        const known = _envVarNames.length;
        listEl.appendChild(el('div', { class: 'or-empty' }, [text(
            known
                ? `Every host variable is passed, including ${known} provider API key(s) this server holds.`
                : 'Every host variable is passed to shell commands.'
        )]));
        return;
    }

    const list = _getActiveEnvList(mode);

    if (list.length === 0) {
        listEl.appendChild(el('div', { class: 'or-empty' }, [text(
            mode === 'denylist'
                ? 'No variables blocked — every host variable is passed.'
                : 'No variables allowed — shell commands get only PATH, HOME and VIRTUAL_ENV.'
        )]));
        return;
    }

    // /api/env-vars only probes for provider API keys, so the one honest status
    // we can render is "this entry is a live API key" — which is also the
    // reading that matters: allowlisted means handed out, denylisted means withheld.
    for (const varName of list) {
        const children = [];
        if (_envVarNames.includes(varName)) {
            const blocked = mode === 'denylist';
            children.push(el('span', {
                class: `or-status ${blocked ? 'valid' : 'invalid'}`,
                title: blocked
                    ? 'A provider API key this server holds — blocked from shell commands.'
                    : 'A provider API key this server holds — passed to every shell command.',
            }, [text(blocked ? '\u2713' : '\u2717')]));
        }
        children.push(el('span', { class: 'or-model-id' }, [text(varName)]));
        listEl.appendChild(el('div', { class: 'or-model-item' }, children));
    }
}

// ---------------------------------------------------------------------------
// Tab management
// ---------------------------------------------------------------------------

function buildNetworkTab(settings) {
    const fields = NETWORK_FIELDS.map(f => buildField(f, settings[f.key]));
    const section = el('div', { class: 'settings-section' }, [
        el('h3', {}, [
            text('Network & Security'),
            buildHelpIcon('Expose the server on the network with HTTPS. Requires a server restart to take effect.'),
        ]),
        ...fields,
    ]);

    // --- QR Code access button (visible when network enabled + token set) ---
    const qrSection = el('div', {
        class: 'settings-section',
        id: 'qr-access-section',
        style: (settings.network_enabled && settings.auth_token_set) ? '' : 'display:none',
    }, [
        el('h3', {}, [
            text('Remote Access'),
            buildHelpIcon(
                'Scan the QR code with a mobile device to connect automatically, or copy the '
                + 'token to sign a device in by hand. Both carry the same access token. '
                + 'Regenerating mints a new one and signs every other device out.'
            ),
        ]),
        el('div', { style: 'display:flex; gap:0.5rem; align-items:center; flex-wrap:wrap;' }, [
            _buildQRButton(),
            _buildShowTokenButton(),
            // Destructive, but it lives here rather than in a separate section:
            // this is where you come looking for it, and a "Danger Zone" further
            // down the page was missed entirely by the one person using it.
            _buildRegenerateButton(),
        ]),
    ]);

    // --- Allowed Origins editor (visible when network enabled) ---
    const originsSection = el('div', {
        class: 'settings-section',
        id: 'origins-section',
        style: settings.network_enabled ? '' : 'display:none',
    }, [
        el('h3', {}, [
            text('Allowed Origins'),
            buildHelpIcon(
                'The first origin is used as the base URL for QR codes and access links. '
                + 'Add your server\'s LAN address or proxy/VPN hostname '
                + '(e.g. https://your-server-ip:8090). Changes require a restart.'
            ),
        ]),
        _buildOriginsEditor(settings.cors_origins || []),
    ]);

    return el('div', {}, [section, qrSection, originsSection]);
}

// --- QR Code button + overlay ---

function _buildQRButton() {
    const btn = el('button', {
        class: 'btn-secondary',
        style: 'font-family:var(--mono); font-size:var(--text-sm); padding:0.4rem 0.8rem; cursor:pointer;',
    }, [text('Show QR Code')]);

    btn.addEventListener('click', async () => {
        btn.disabled = true;
        btn.textContent = 'Loading\u2026';
        try {
            const { getAuthToken } = await import('../../api.js');
            const headers = {};
            const t = getAuthToken();
            if (t) headers['Authorization'] = `Bearer ${t}`;
            const resp = await fetch('/api/settings/access-qr', { headers });
            if (!resp.ok) throw new Error(resp.statusText);
            const svg = await resp.text();

            // Build overlay
            const overlay = el('div', { class: 'modal-overlay' });
            const card = el('div', {
                class: 'modal-card',
                style: 'max-width:360px; text-align:center; padding:1.5rem;',
            });

            const title = el('h2', { style: 'margin-bottom:0.5rem; font-size:var(--text-lg);' }, [text('Scan to Connect')]);
            const qrContainer = el('div', {
                style: 'background:#fff; border-radius:8px; padding:12px; display:inline-block; margin:0.75rem 0;',
            });
            // Raw SVG markup off an HTTP response — inline SVG is a scripting
            // context, so sanitize rather than assigning innerHTML directly.
            setSanitizedSvg(qrContainer, svg);
            // Make SVG responsive
            const svgEl = qrContainer.querySelector('svg');
            if (svgEl) {
                svgEl.removeAttribute('width');
                svgEl.removeAttribute('height');
                svgEl.style.width = '220px';
                svgEl.style.height = '220px';
            }

            const hint = el('div', {
                style: 'font-family:var(--mono); font-size:var(--text-xs); color:var(--text-dim); margin-top:0.5rem;',
            }, [text('Scan with your phone camera to open the app with auth')]);

            const closeBtn = el('button', {
                class: 'btn-primary',
                style: 'margin-top:1rem; padding:0.4rem 1.5rem;',
            }, [text('Close')]);

            card.append(title, qrContainer, hint, closeBtn);
            overlay.append(card);
            document.body.append(overlay);

            const close = () => overlay.remove();
            closeBtn.addEventListener('click', close);
            overlay.addEventListener('click', (e) => { if (e.target === overlay) close(); });
            document.addEventListener('keydown', function esc(e) {
                if (e.key === 'Escape') { close(); document.removeEventListener('keydown', esc); }
            });
        } catch (e) {
            console.error('QR code failed:', e);
        }
        btn.disabled = false;
        btn.textContent = 'Show QR Code';
    });

    return btn;
}

// --- Revoke Remote Access button ---

// --- Token reveal + copy ---

/* Copy with a fallback, because the primary path is not always available where
   it matters most. navigator.clipboard requires a secure context, and a LAN box
   served over a self-signed certificate does not reliably qualify on iOS — the
   exact device this button exists for. execCommand is deprecated and still the
   only thing that works there. */
async function _copyText(value, srcInput) {
    try {
        await navigator.clipboard.writeText(value);
        return true;
    } catch { /* fall through */ }
    try {
        srcInput.removeAttribute('readonly');
        srcInput.select();
        srcInput.setSelectionRange(0, value.length);
        const ok = document.execCommand('copy');
        srcInput.setAttribute('readonly', '');
        return ok;
    } catch {
        return false;
    }
}

/* Shared by "Show Token" and by the confirmation that follows a regenerate —
   after rotating you want the new token in front of you, not a second click
   away. */
function _openTokenOverlay(token, { title, note }) {
    const overlay = el('div', { class: 'modal-overlay' });
    // Wide enough for a 43-char urlsafe token at 16px mono. Narrower and the
    // value scrolls out of sight, which defeats the point of showing it.
    const card = el('div', { class: 'modal-card', style: 'max-width:520px; padding:1.5rem;' });

    const field = el('input', {
        type: 'text',
        readonly: '',
        value: token,
        // 16px is not cosmetic: iOS zooms the page in on focus for any input
        // below it and never zooms back.
        style: 'width:100%; font-family:var(--mono); font-size:16px; padding:0.5rem;'
             + 'background:var(--bg-surface); color:var(--text-bright);'
             + 'border:1px solid var(--border); border-radius:var(--radius);',
    });
    field.addEventListener('focus', () => field.select());

    const copyBtn = el('button', {
        class: 'btn-secondary',
        style: 'padding:0.4rem 0.8rem; cursor:pointer;',
    }, [text('Copy')]);
    copyBtn.addEventListener('click', async () => {
        const ok = await _copyText(token, field);
        copyBtn.textContent = ok ? 'Copied' : 'Select and copy manually';
        setTimeout(() => { copyBtn.textContent = 'Copy'; }, 2000);
    });

    const closeBtn = el('button', {
        class: 'btn-primary',
        style: 'padding:0.4rem 0.8rem; cursor:pointer;',
    }, [text('Done')]);

    const row = el('div', {
        style: 'display:flex; gap:0.5rem; justify-content:flex-end; margin-top:1rem;',
    }, [copyBtn, closeBtn]);

    card.append(
        el('h2', { style: 'font-size:var(--text-lg); margin-bottom:0.75rem;' }, [text(title)]),
        el('p', {
            style: 'font-family:var(--mono); font-size:var(--text-sm); color:var(--text-dim);'
                 + 'margin-bottom:1rem; line-height:1.5;',
        }, [text(note)]),
        field,
        row,
    );
    overlay.append(card);
    document.body.append(overlay);

    const close = () => overlay.remove();
    closeBtn.addEventListener('click', close);
    overlay.addEventListener('click', (e) => { if (e.target === overlay) close(); });
    document.addEventListener('keydown', function esc(e) {
        if (e.key === 'Escape') { close(); document.removeEventListener('keydown', esc); }
    });
}

function _buildShowTokenButton() {
    const btn = el('button', {
        class: 'btn-secondary',
        style: 'font-family:var(--mono); font-size:var(--text-sm); padding:0.4rem 0.8rem; cursor:pointer;',
    }, [text('Show Token')]);

    btn.addEventListener('click', async () => {
        btn.disabled = true;
        const label = btn.textContent;
        btn.textContent = 'Loading\u2026';
        try {
            const data = await get('/api/settings/auth-token');
            if (!data.token) {
                notify('error', 'No access token is set — enable Network mode first.');
                return;
            }
            _openTokenOverlay(data.token, {
                title: 'Access Token',
                note: 'Paste this into the sign-in box on another device. Anyone holding it '
                    + 'has full access to this agent — treat it like a password.',
            });
        } catch (e) {
            notify('error', `Could not read the token: ${e.message}`);
        } finally {
            btn.disabled = false;
            btn.textContent = label;
        }
    });

    return btn;
}

// --- Regenerate token (destructive) ---

function _buildRegenerateButton() {
    const btn = el('button', {
        class: 'btn-secondary',
        style: 'font-family:var(--mono); font-size:var(--text-sm); padding:0.4rem 0.8rem;'
             + 'cursor:pointer; border-color:var(--error, #e55); color:var(--error, #e55);',
    }, [text('Regenerate Token')]);

    btn.addEventListener('click', () => {
        const overlay = el('div', { class: 'modal-overlay' });
        const card = el('div', { class: 'modal-card', style: 'max-width:380px; padding:1.5rem;' });
        const title = el('h2', {
            style: 'font-size:var(--text-lg); margin-bottom:0.75rem;',
        }, [text('Regenerate access token?')]);
        const body = el('p', {
            style: 'font-family:var(--mono); font-size:var(--text-sm); color:var(--text-dim);'
                 + 'margin-bottom:1.25rem; line-height:1.5;',
        }, [text(
            'Every other signed-in device is signed out immediately — phones, tablets, '
            + 'and any QR link or shared URL you handed out. They will each need the new '
            + 'token to get back in. This browser stays signed in.'
        )]);
        const btnRow = el('div', { style: 'display:flex; gap:0.5rem; justify-content:flex-end;' });
        const cancelBtn = el('button', {
            class: 'btn-secondary', style: 'padding:0.4rem 0.8rem; cursor:pointer;',
        }, [text('Cancel')]);
        const confirmBtn = el('button', {
            class: 'btn-primary',
            style: 'padding:0.4rem 0.8rem; cursor:pointer;'
                 + 'background:var(--error, #e55); border-color:var(--error, #e55);',
        }, [text('Regenerate')]);

        btnRow.append(cancelBtn, confirmBtn);
        card.append(title, body, btnRow);
        overlay.append(card);
        document.body.append(overlay);

        const close = () => overlay.remove();
        cancelBtn.addEventListener('click', close);
        overlay.addEventListener('click', (e) => { if (e.target === overlay) close(); });
        document.addEventListener('keydown', function esc(e) {
            if (e.key === 'Escape') { close(); document.removeEventListener('keydown', esc); }
        });

        confirmBtn.addEventListener('click', async () => {
            confirmBtn.disabled = true;
            confirmBtn.textContent = 'Regenerating\u2026';
            try {
                const data = await post('/api/settings/auth-token/regenerate');
                // Adopt the new token here first. The old one is dead the moment
                // the server saves it, so without this the browser that pressed
                // the button is the next thing to be signed out.
                if (data.token) setAuthToken(data.token);
                close();
                _openTokenOverlay(data.token, {
                    title: 'New access token',
                    note: 'All other devices are now signed out. Copy this to sign them back '
                        + 'in, or use Show QR Code.',
                });
            } catch (e) {
                confirmBtn.disabled = false;
                confirmBtn.textContent = 'Regenerate';
                let errEl = card.querySelector('.revoke-error');
                if (!errEl) {
                    errEl = el('div', {
                        class: 'revoke-error',
                        style: 'color:var(--error, #e55); font-family:var(--mono);'
                             + 'font-size:var(--text-xs); margin-top:0.75rem;',
                    });
                    card.append(errEl);
                }
                errEl.textContent = `Error: ${e.message}`;
            }
        });
    });

    return btn;
}

// --- Allowed Origins list editor ---

let _corsOrigins = [];

function _buildOriginsEditor(origins) {
    _corsOrigins = [...origins];
    const container = el('div', { id: 'origins-editor' });

    function render() {
        clear(container);
        const list = el('div', { style: 'display:flex; flex-direction:column; gap:0.25rem; margin-bottom:0.5rem;' });

        for (let i = 0; i < _corsOrigins.length; i++) {
            const idx = i;
            const row = el('div', {
                style: 'display:flex; align-items:center; gap:0.4rem;',
            });
            const label = el('span', {
                style: 'font-family:var(--mono); font-size:var(--text-sm); color:var(--text); flex:1; '
                     + 'overflow:hidden; text-overflow:ellipsis; white-space:nowrap;',
            }, [text(_corsOrigins[idx])]);
            const removeBtn = el('button', {
                style: 'background:none; border:none; color:var(--error); cursor:pointer; font-size:var(--text-md); padding:0 4px;',
                title: 'Remove',
                'aria-label': `Remove the allowed origin ${_corsOrigins[idx]}`,
            }, [text('\u00d7')]);
            removeBtn.addEventListener('click', () => {
                _corsOrigins.splice(idx, 1);
                render();
            });
            row.append(label, removeBtn);
            list.append(row);
        }

        if (_corsOrigins.length === 0) {
            list.append(el('span', {
                style: 'font-family:var(--mono); font-size:var(--text-xs); color:var(--text-faint);',
            }, [text('No custom origins. Defaults to same-origin only.')]));
        }

        const addRow = el('div', { style: 'display:flex; gap:0.4rem;' });
        const input = el('input', {
            type: 'text',
            placeholder: 'https://your-server-ip:8090',
            style: 'flex:1; font-family:var(--mono); font-size:var(--text-sm); '
                 + 'padding:0.3rem 0.5rem; background:var(--bg-surface); border:1px solid var(--border); '
                 + 'border-radius:var(--radius); color:var(--text); outline:none;',
        });
        const addBtn = el('button', {
            class: 'btn-secondary',
            style: 'font-size:var(--text-xs); padding:0.3rem 0.6rem; cursor:pointer;',
        }, [text('Add')]);

        function addOrigin() {
            const val = input.value.trim();
            if (!val) return;
            // Basic validation: must start with http:// or https://
            if (!val.match(/^https?:\/\/.+/)) {
                input.style.borderColor = 'var(--error)';
                setTimeout(() => { input.style.borderColor = ''; }, 1500);
                return;
            }
            // Remove trailing slash for consistency
            const normalized = val.replace(/\/+$/, '');
            if (!_corsOrigins.includes(normalized)) {
                _corsOrigins.push(normalized);
            }
            render();
        }

        addBtn.addEventListener('click', addOrigin);
        input.addEventListener('keydown', (e) => { if (e.key === 'Enter') addOrigin(); });
        addRow.append(input, addBtn);

        container.append(list, addRow);
    }

    render();
    return container;
}

function buildNotificationsSection() {
    const perm = getPermission();
    const statusText = { granted: 'Enabled', denied: 'Blocked by browser', default: 'Not enabled', unsupported: 'Not supported' }[perm] || perm;
    const statusColor = perm === 'granted' ? 'var(--success, #4c8)' : perm === 'denied' ? 'var(--error, #e55)' : 'var(--text-dim)';

    const statusEl = el('span', { style: `color:${statusColor}; font-size:var(--text-sm);` }, [text(statusText)]);
    const row = el('div', { class: 'setting-row', 'data-key': 'browser_notifications' }, [
        el('div', { class: 'setting-label-cell' }, [
            el('span', { class: 'setting-label-line' }, [el('label', {}, [text('Browser Notifications')])]),
        ]),
        el('div', { style: 'display:flex; align-items:center; gap:0.75rem;' }, [statusEl]),
    ]);

    const children = [el('h3', {}, [text('Notifications'), buildHelpIcon('System notifications let the agent alert you even when this tab is not in focus. Click a notification to jump to the originating session.')]), row];

    if (perm === 'default') {
        const btn = el('button', { class: 'btn btn-secondary', style: 'margin-top:0.5rem; font-size:var(--text-sm);' }, [text('Enable Notifications')]);
        btn.addEventListener('click', async () => {
            const granted = await requestPermission();
            statusEl.textContent = granted ? 'Enabled' : 'Permission denied';
            statusEl.style.color = granted ? 'var(--success, #4c8)' : 'var(--error, #e55)';
            if (granted) btn.remove();
        });
        children.push(btn);
    } else if (perm === 'denied') {
        children.push(el('p', { style: 'font-size:var(--text-sm); color:var(--text-dim); margin-top:0.4rem;' }, [
            text('Notifications are blocked. To enable, click the lock icon in your browser\'s address bar and allow notifications for this site.'),
        ]));
    }

    return el('div', { class: 'settings-section' }, children);
}

function buildSessionCleanupSection() {
    const daysInput = el('input', {
        type: 'number', min: '0', value: '7',
        id: 'setting-cleanup-keep-days',
    });
    const minInput = el('input', {
        type: 'number', min: '0', value: '5',
        id: 'setting-cleanup-keep-min',
    });

    const statusEl = el('div', {
        style: 'font-size:var(--text-sm); color:var(--text-dim); margin-top:0.5rem; min-height:1.2em;',
    });

    async function _computePreview() {
        const keepDays = Math.max(0, parseInt(daysInput.value || '0', 10));
        const keepMin = Math.max(0, parseInt(minInput.value || '0', 10));
        const cutoff = new Date(Date.now() - keepDays * 86400000).toISOString();
        const data = await get('/api/sessions?limit=1000');
        const sessions = data.items || [];
        // Mirror server logic in api/routers/sessions.py:purge_sessions —
        // sessions sorted by updated_at DESC, candidates older than cutoff,
        // keep the first keep_min of those candidates.
        const candidates = sessions.filter(s => (s.updated_at || '') < cutoff);
        const toDelete = candidates.length > keepMin ? candidates.length - keepMin : 0;
        return { toDelete, candidates: candidates.length, keepDays, keepMin };
    }

    const previewBtn = el('button', {
        class: 'btn btn-secondary',
        style: 'font-size:var(--text-sm);',
    }, [text('Preview')]);
    previewBtn.addEventListener('click', async () => {
        previewBtn.disabled = true;
        try {
            const { toDelete, candidates, keepDays, keepMin } = await _computePreview();
            statusEl.textContent = candidates === 0
                ? `No sessions older than ${keepDays} day(s).`
                : `${toDelete} session(s) would be deleted (${candidates} older than ${keepDays} day(s); keeping the most recent ${keepMin} of those).`;
            statusEl.style.color = 'var(--text-dim)';
        } catch (e) {
            statusEl.textContent = `Preview failed: ${e.message || e}`;
            statusEl.style.color = 'var(--error)';
        } finally {
            previewBtn.disabled = false;
        }
    });

    const pruneBtn = el('button', {
        class: 'btn btn-secondary',
        style: 'font-size:var(--text-sm); color:var(--error); border-color:var(--error);',
    }, [text('Prune now')]);
    pruneBtn.addEventListener('click', async () => {
        pruneBtn.disabled = true;
        previewBtn.disabled = true;
        try {
            const { toDelete, keepDays, keepMin } = await _computePreview();
            if (!toDelete) {
                statusEl.textContent = `Nothing to prune (no sessions older than ${keepDays} day(s) past the keep-${keepMin} floor).`;
                statusEl.style.color = 'var(--text-dim)';
                return;
            }
            if (!confirm(
                `Permanently delete ${toDelete} session(s) and all of their messages `
                + '— this cannot be undone. It cascades to any worker sessions.'
            )) {
                statusEl.textContent = 'Cancelled.';
                statusEl.style.color = 'var(--text-dim)';
                return;
            }
            const result = await post('/api/sessions/purge', {
                keep_days: keepDays,
                keep_min: keepMin,
            });
            statusEl.textContent = `Pruned ${result.purged} session(s).`;
            statusEl.style.color = 'var(--success)';
            notify('success', `Pruned ${result.purged} session(s)`);
        } catch (e) {
            statusEl.textContent = `Prune failed: ${e.message || e}`;
            statusEl.style.color = 'var(--error)';
            notify('error', 'Prune failed');
        } finally {
            pruneBtn.disabled = false;
            previewBtn.disabled = false;
        }
    });

    const actionRow = el('div', {
        style: 'display:flex; gap:0.5rem; margin-top:0.6rem;',
    }, [previewBtn, pruneBtn]);

    return el('div', { class: 'settings-section' }, [
        el('h3', {}, [
            text('Session Cleanup'),
            buildHelpIcon(
                'Permanently delete old sessions to keep the database tidy. '
                + '"Older than" sets the age cutoff (sessions whose updated_at is past this many days are candidates). '
                + '"Always keep" preserves the N most recent of those candidates as a safety floor. '
                + 'Worker sessions cascade with their parent. Cron-bound sessions are protected automatically by the daily maintenance task; this manual prune does not skip them, so use with care if you have active cron jobs.'
            ),
        ]),
        el('div', { class: 'setting-row' }, [
            el('label', { for: 'setting-cleanup-keep-days' }, [text('Older than (days)')]),
            daysInput,
        ]),
        el('div', { class: 'setting-row' }, [
            el('label', { for: 'setting-cleanup-keep-min' }, [text('Always keep (most recent old)')]),
            minInput,
        ]),
        actionRow,
        statusEl,
    ]);
}

async function buildSecurityTab(settings) {
    // Warning block
    const warningBlock = el('div', { class: 'security-warning-block' }, [
        el('div', { class: 'security-warning-title' }, [text('⚠  Danger Zone — Read Before Enabling')]),
        el('ul', { class: 'security-warning-list' }, [
            el('li', {}, [text('Bypasses the dangerous-tool confirmation gate for every tool call in every session.')]),
            el('li', {}, [text('Shell commands, file writes, and network requests execute without asking for approval.')]),
            el('li', {}, [text('Workers and cron jobs are fully exempt — there is no human in the loop.')]),
            el('li', {}, [text('A prompt injection or a misbehaving agent loop can cause irreversible damage silently.')]),
            el('li', {}, [text('Only enable if you fully trust the current task context. Disable it when done.')]),
        ]),
    ]);

    // Run Dangerously — read-only status badge (only settable via --dangerous at startup)
    const isEnabled = !!settings.auto_approve_dangerous;
    const statusBadge = el('span', {
        style: 'font-size:var(--text-xs); font-weight:500; padding:2px 10px; border-radius:3px; '
             + (isEnabled
                 ? 'background:color-mix(in srgb,var(--error,#c25450) 15%,var(--bg)); color:var(--error,#c25450); border:1px solid var(--error,#c25450);'
                 : 'background:var(--bg-surface); color:var(--text-faint); border:1px solid var(--border);'),
    }, [text(isEnabled ? 'ENABLED' : 'DISABLED')]);
    const toggleSection = el('div', { class: 'settings-section' }, [
        el('h3', {}, [
            text('Execution Mode'),
            buildHelpIcon(
                'Run Dangerously bypasses the dangerous-tool approval gate entirely. '
                + 'This setting can only be activated by starting the server with the '
                + '--dangerous flag: python run.py --dangerous. '
                + 'It cannot be changed while the server is running to prevent a rogue '
                + 'process or prompt injection from elevating its own privileges.'
            ),
        ]),
        el('div', { class: 'setting-row' }, [
            el('div', {}, [
                el('label', {}, [text('Run Dangerously')]),
                el('div', { style: 'font-size:var(--text-xs); color:var(--text-faint); margin-top:2px;' }, [
                    text('Set at startup only: python run.py --dangerous'),
                ]),
            ]),
            statusBadge,
        ]),
    ]);

    // Approved Scopes section
    const scopesContainer = el('div', {});
    async function renderScopes() {
        clear(scopesContainer);
        let data = {};
        try { data = await get('/api/settings/tool-approvals'); } catch { /* ignore */ }
        const tools = Object.keys(data);
        const clearBtn = el('button', {
            class: 'btn btn-secondary',
            style: 'font-size:var(--text-xs); padding:2px 8px;',
        }, [text('Clear all')]);
        clearBtn.disabled = tools.length === 0;
        clearBtn.addEventListener('click', async () => {
            // Unprompted, one click wiped every scope the user had ever
            // approved and said nothing either way — including when the
            // request failed, because the bare fetch() carried no auth header
            // and its rejection was swallowed. (S10)
            const scopeCount = tools.reduce((n, t) => n + (data[t] || []).length, 0);
            if (!confirm(
                `Clear every remembered approval (${scopeCount} scope${scopeCount === 1 ? '' : 's'} `
                + `across ${tools.length} tool${tools.length === 1 ? '' : 's'}) — this cannot be undone. `
                + 'The agent will ask again the next time it needs each of them.'
            )) return;
            clearBtn.disabled = true;
            try {
                await del('/api/settings/tool-approvals');
                notify('success', 'Remembered approvals cleared');
                await renderScopes();
            } catch (e) {
                clearBtn.disabled = false;
                notify('error', `Could not clear the remembered approvals: ${e.message || e}`);
            }
        });
        scopesContainer.appendChild(el('div', { class: 'settings-section' }, [
            el('h3', {}, [
                text('Remembered Approvals'),
                buildHelpIcon(
                    'Scopes approved via approve_dangerous_tool() are persisted here so the '
                    + 'agent does not need to ask again for previously approved actions. '
                    + 'Clear all to require re-confirmation for every dangerous action.'
                ),
            ]),
            tools.length === 0
                ? el('div', { class: 'security-scopes-list' }, [
                    el('div', { class: 'security-scopes-empty' }, [text('No remembered approvals.')]),
                  ])
                : el('div', { class: 'security-scopes-list' }, tools.map(toolName =>
                    el('div', { class: 'security-scopes-tool' }, [
                        el('div', { class: 'security-scopes-tool-name' }, [text(toolName)]),
                        ...data[toolName].map(scope =>
                            el('div', { class: 'security-scopes-scope' }, [text('• ' + scope)])
                        ),
                    ])
                )),
            el('div', { style: 'margin-top:var(--sp-2); display:flex; justify-content:flex-end;' }, [clearBtn]),
        ]));
    }
    await renderScopes();

    return el('div', {}, [warningBlock, toggleSection, scopesContainer]);
}

// ---------------------------------------------------------------------------
// Search / filter
//
// With 100+ controls spread over five tabs, "where is that setting" is the
// dominant cost of this modal. Typing a query drops the tab boundary and
// filters every section at once; clearing it restores the tab you were on.
// ---------------------------------------------------------------------------

let _searchQuery = '';

function _sectionTitle(section) {
    const h3 = section.querySelector('h3');
    if (!h3) return '';
    // Direct text nodes only. The section help tooltip lives inside the
    // heading, and its paragraph-long description would match almost anything.
    return Array.from(h3.childNodes)
        .filter(n => n.nodeType === Node.TEXT_NODE)
        .map(n => n.textContent)
        .join(' ');
}

// Blocks that are not settings sections — the Security tab's Danger Zone
// banner, the tab wrappers' loose children — have no rows to match, so they
// would sit in the results under every query. Hide them while filtering.
function _filterStrayBlocks(body, active) {
    const walk = (parent) => {
        for (const child of parent.children) {
            if (child.classList.contains('settings-section')) continue;
            if (child.querySelector('.settings-section')) {
                child.classList.remove('filtered-out');
                walk(child);
                continue;
            }
            child.classList.toggle('filtered-out', active);
        }
    };
    for (const tab of body.querySelectorAll('.tab-content')) walk(tab);
}

function _applySettingsFilter(query) {
    const changed = query !== _searchQuery;
    _searchQuery = query;
    const card = _overlay?.querySelector('.modal-card');
    const body = _overlay?.querySelector('.modal-body');
    if (!card || !body) return;

    const q = query.trim().toLowerCase();
    card.classList.toggle('settings-search-active', !!q);
    // The result set is a different document; keeping the old offset lands the
    // user in the middle of it (or past its end) with no visible matches.
    if (changed) body.scrollTop = 0;

    const countEl = document.getElementById('settings-search-count');
    const sections = body.querySelectorAll('.settings-section');
    _filterStrayBlocks(body, !!q);

    if (!q) {
        for (const section of sections) {
            section.classList.remove('filtered-out');
            for (const row of section.querySelectorAll('.setting-row')) row.classList.remove('filtered-out');
        }
        if (countEl) countEl.textContent = '';
        return;
    }

    let rowHits = 0;
    let sectionHits = 0;
    for (const section of sections) {
        const titleHit = _sectionTitle(section).toLowerCase().includes(q);
        let hitsHere = 0;
        for (const row of section.querySelectorAll('.setting-row')) {
            // Rows a conditional already hid (voice engine, SSL mode, network
            // off) stay hidden — the filter must not resurrect them.
            if (row.style.display === 'none') continue;
            const hit = titleHit || `${row.dataset.key || ''} ${row.textContent}`.toLowerCase().includes(q);
            row.classList.toggle('filtered-out', !hit);
            if (hit) hitsHere++;
        }
        const show = titleHit || hitsHere > 0;
        section.classList.toggle('filtered-out', !show);
        if (show) sectionHits++;
        rowHits += hitsHere;
    }

    if (countEl) {
        countEl.textContent = rowHits
            ? `${rowHits} setting${rowHits === 1 ? '' : 's'}`
            : sectionHits
                ? `${sectionHits} section${sectionHits === 1 ? '' : 's'}`
                : 'No matches';
    }
}

function buildSearchBar() {
    const input = el('input', {
        type: 'search',
        id: 'settings-search',
        class: 'settings-search-input',
        placeholder: 'Search settings…',
        'aria-label': 'Search all settings',
        autocomplete: 'off',
        title: 'Searches every tab at once. Matches setting names, hints, and the '
             + 'security / autonomy / restart / locked markers.',
    });
    const count = el('span', {
        id: 'settings-search-count',
        class: 'settings-search-count',
        role: 'status',
        'aria-live': 'polite',
    });
    input.addEventListener('input', () => _applySettingsFilter(input.value));
    return el('div', { class: 'settings-search' }, [input, count]);
}

function buildTabs(settings) {
    // Tab buttons
    const generalTab = el('button', { class: 'tab-btn active', 'data-tab': 'general' }, [text('General')]);
    const modelsTab = el('button', { class: 'tab-btn', 'data-tab': 'models' }, [text('Models')]);
    const envTab = el('button', { class: 'tab-btn', 'data-tab': 'environment' }, [text('Environment')]);
    const networkTab = el('button', { class: 'tab-btn', 'data-tab': 'network' }, [text('Network')]);
    const securityTab = el('button', { class: 'tab-btn', 'data-tab': 'security' }, [text('Security')]);
    const tabBar = el('div', { class: 'tab-bar' }, [generalTab, modelsTab, envTab, networkTab, securityTab]);

    // General tab content
    const generalSections = SECTIONS.map(section => {
        const fields = section.fields.map(f => buildField(f, settings[f.key]));
        const heading = [text(section.title)];
        if (section.description) heading.push(buildHelpIcon(section.description));
        return el('div', { class: 'settings-section' }, [
            el('h3', {}, heading),
            ...(section.description ? [buildSectionDesc(section.description)] : []),
            ...fields,
        ]);
    });
    generalSections.push(buildNotificationsSection());
    generalSections.push(buildSessionCleanupSection());
    const generalContent = el('div', { class: 'tab-content active', 'data-tab': 'general' }, generalSections);

    // Models tab content
    const modelsContent = el('div', { class: 'tab-content', 'data-tab': 'models' }, [buildModelsTab()]);

    // Environment tab content
    const envContent = el('div', { class: 'tab-content', 'data-tab': 'environment' }, [buildEnvTab(settings)]);

    // Network tab content
    const networkContent = el('div', { class: 'tab-content', 'data-tab': 'network' }, [buildNetworkTab(settings)]);

    // Security tab content — async build, render placeholder then swap in
    const securityPlaceholder = el('div', { class: 'tab-content', 'data-tab': 'security' }, [
        el('div', { style: 'padding:var(--sp-4); color:var(--text-faint); font-size:var(--text-sm);' }, [text('Loading…')]),
    ]);
    buildSecurityTab(settings).then(content => {
        clear(securityPlaceholder);
        securityPlaceholder.appendChild(content);
        // The tab arrives after the user may already be searching.
        if (_searchQuery) _applySettingsFilter(_searchQuery);
    });

    // Tab switching
    const tabs = [generalTab, modelsTab, envTab, networkTab, securityTab];
    const contents = [generalContent, modelsContent, envContent, networkContent, securityPlaceholder];
    tabs.forEach((tab, i) => {
        tab.addEventListener('click', () => {
            tabs.forEach(t => t.classList.remove('active'));
            contents.forEach(c => c.classList.remove('active'));
            tab.classList.add('active');
            contents[i].classList.add('active');
            // The body is one shared scroller. Without this, switching tabs
            // lands you partway down (or past the end of) the new one.
            const body = _overlay?.querySelector('.modal-body');
            if (body) body.scrollTop = 0;
        });
    });

    return { tabBar, contents };
}

// ---------------------------------------------------------------------------
// Modal lifecycle
// ---------------------------------------------------------------------------

// /api/settings failing made the gear icon do nothing at all — no modal, no
// message, no way to try again, and no way to tell a dead server from a dead
// button. Render the shell with the reason and a Retry instead. (F2)
function _openLoadFailure(message, opts) {
    const retryBtn = el('button', { class: 'btn btn-primary' }, [text('Retry')]);
    const card = el('div', { class: 'modal-card' }, [
        el('div', { class: 'modal-header' }, [
            el('h2', {}, [text('Settings')]),
            el('button', {
                class: 'modal-close',
                title: 'Close',
                'aria-label': 'Close settings',
                onClick: closeSettings,
            }, [text('\u00d7')]),
        ]),
        el('div', { class: 'modal-body' }, [
            el('div', { class: 'settings-load-error', role: 'alert' }, [
                el('strong', {}, [text('Settings could not be loaded.')]),
                el('p', {}, [text(message)]),
                el('p', {}, [text(
                    'Nothing has been changed. The server may be restarting, or the '
                    + 'request may have been rejected — retry once it is back.',
                )]),
            ]),
        ]),
        el('div', { class: 'modal-footer' }, [
            el('button', { class: 'btn btn-secondary', onClick: closeSettings }, [text('Close')]),
            retryBtn,
        ]),
    ]);
    retryBtn.addEventListener('click', () => {
        closeSettings();
        openSettings(opts);
    });

    _overlay = el('div', { class: 'modal-overlay' }, [card]);
    _overlay.addEventListener('click', (e) => { if (e.target === _overlay) closeSettings(); });
    document.body.appendChild(_overlay);
    _closeOverlay = openOverlay(card, { onClose: closeSettings, initialFocus: retryBtn });
    announce('Settings could not be loaded', { assertive: true });
}

export async function openSettings(opts = {}) {
    if (_overlay) {
        // Already open — just switch to the requested tab if specified
        if (opts.tab) {
            const btn = _overlay.querySelector(`.tab-btn[data-tab="${opts.tab}"]`);
            if (btn) btn.click();
        }
        return;
    }

    let settings;
    try {
        const [s, m, ev, om] = await Promise.all([
            get('/api/settings'),
            get('/api/models'),
            get('/api/env-vars').catch(() => ({ vars: [] })),
            get('/api/models/ollama').catch(() => ({ models: [] })),
        ]);
        settings = s;
        _availableModels = (m.models || []).sort((a, b) => a.id.localeCompare(b.id));
        _envVarNames = ev.vars || [];
        _ollamaModels = om.models || [];
        _ollamaError = om.error || '';
    } catch (e) {
        _openLoadFailure(e.message || String(e), opts);
        return;
    }
    _original = { ...settings };

    // Initialize model list from settings
    const modelIds = settings.openrouter_models || [];
    _models = (Array.isArray(modelIds) ? modelIds : []).map(id => ({ id, valid: null, info: null }));

    const { tabBar, contents } = buildTabs(settings);
    const statusEl = el('span', { class: 'save-status', role: 'status' });

    const card = el('div', { class: 'modal-card' }, [
        el('div', { class: 'modal-header' }, [
            el('h2', {}, [text('Settings')]),
            el('button', {
                class: 'modal-close',
                title: 'Close',
                'aria-label': 'Close settings',
                onClick: closeSettings,
            }, [text('\u00d7')]),
        ]),
        tabBar,
        buildSearchBar(),
        el('div', { class: 'modal-body' }, contents),
        el('div', { class: 'modal-footer' }, [
            statusEl,
            el('button', { class: 'btn btn-secondary', onClick: closeSettings }, [text('Cancel')]),
            el('button', {
                class: 'btn btn-primary',
                id: 'settings-save-btn',
                onClick: async () => {
                    // A pending auto-clear from the previous save would wipe
                    // whatever this one has to say a couple of seconds later.
                    clearTimeout(_statusTimer);
                    const changes = collectChanges();

                    // Handle API keys separately — never part of normal settings
                    const apikeyFields = SECTIONS.flatMap(s => s.fields.filter(f => f.type === 'apikey'));
                    const apikeyChanges = [];
                    for (const f of apikeyFields) {
                        const inp = document.getElementById(`setting-${f.key}`);
                        const val = inp ? inp.value.trim() : '';
                        if (val) apikeyChanges.push({ field: f, input: inp, value: val });
                    }

                    if (Object.keys(changes).length === 0 && apikeyChanges.length === 0) {
                        statusEl.className = 'save-status status-muted';
                        statusEl.textContent = 'No changes';
                        announce('No changes to save');
                        return;
                    }
                    try {
                        const saved = [];

                        // Save API keys via dedicated endpoint
                        for (const { field: f, input: inp, value: val } of apikeyChanges) {
                            await post('/api/settings/apikey', { key: f.envKey, value: val });
                            inp.value = '';
                            inp.placeholder = '••••••••';
                            _original[f.key + '_set'] = true;
                            saved.push(f.label);
                        }

                        let result = {};
                        const requested = Object.keys(changes);
                        if (requested.length > 0) {
                            result = await post('/api/settings', changes);
                            saved.push(...(result.updated || []));
                        }

                        // Handle SSL validation errors
                        if (result.ssl_errors && result.ssl_errors.length > 0) {
                            statusEl.className = 'save-status status-error';
                            statusEl.textContent = result.ssl_errors.join('; ');
                            announce(statusEl.textContent, { assertive: true });
                            return;
                        }

                        // The server drops rejected values (unknown key, locked
                        // field, bad enum, out of bounds, coercion failure) and
                        // reports only what it accepted. Anything we asked for
                        // and did not get back never happened — say so, and put
                        // the control back to the value the server actually holds
                        // instead of leaving a number on screen that isn't real.
                        const accepted = new Set(result.updated || []);
                        const rejected = requested.filter(k => !accepted.has(k));
                        for (const key of rejected) _revertField(key);

                        // Blank write-only inputs whose value the server took.
                        for (const f of allSettingFields()) {
                            if (f.type !== 'certpath' && f.type !== 'writeonly') continue;
                            if (!accepted.has(f.key)) continue;
                            const inp = document.getElementById(`setting-${f.key}`);
                            if (!inp) continue;
                            _original[f.key + '_set'] = true;
                            inp.value = '';
                            inp.placeholder = _writeOnlyPlaceholder(f.key);
                        }

                        // Only accepted keys become the new baseline; merging the
                        // whole request would make the next diff think a rejected
                        // value had stuck.
                        for (const key of accepted) {
                            if (key in changes) _original[key] = changes[key];
                        }

                        // Live features (voice mic button) re-check their
                        // config on this instead of holding stale state.
                        window.dispatchEvent(new CustomEvent('pernix:settings-saved', { detail: { saved } }));

                        // If openrouter_models changed, refresh model dropdowns
                        if (accepted.has('openrouter_models')) {
                            try {
                                const m = await get('/api/models');
                                _availableModels = (m.models || []).sort((a, b) => a.id.localeCompare(b.id));
                                _rebuildModelSelects();
                            } catch (e) {
                                console.warn('Failed to refresh models after save:', e);
                            }
                        }

                        if (rejected.length > 0) {
                            statusEl.className = 'save-status status-error';
                            statusEl.textContent =
                                `Rejected by the server and reverted: ${rejected.map(_labelFor).join(', ')}. `
                                + 'The value was out of range or the field is edit-locked.';
                            announce(statusEl.textContent, { assertive: true });
                            return;
                        }

                        // restart_required only covers the network group; every
                        // other badged field is read once at startup.
                        const restarts = restartKeys();
                        const needsRestart = [...accepted].filter(k => restarts.has(k));
                        if (result.restart_required || needsRestart.length > 0) {
                            _restartRequired = true;
                            _showRestartButton(needsRestart.map(_labelFor).join(', ') || 'network changes');
                        } else {
                            statusEl.className = 'save-status';
                            statusEl.textContent = `Saved: ${saved.join(', ')}`;
                            announce(statusEl.textContent);
                            _statusTimer = setTimeout(() => { statusEl.textContent = ''; }, 3000);
                        }
                    } catch (e) {
                        statusEl.className = 'save-status status-error';
                        statusEl.textContent = `Error: ${e.message}`;
                        announce(statusEl.textContent, { assertive: true });
                    }
                },
            }, [text('Save')]),
        ]),
    ]);

    // Mousedown guard: prevent accidental close when dragging from inside card to overlay
    let _mouseDownTarget = null;
    _overlay = el('div', { class: 'modal-overlay' }, [card]);
    _overlay.addEventListener('mousedown', (e) => { _mouseDownTarget = e.target; });
    _overlay.addEventListener('click', (e) => {
        if (e.target === _overlay && _mouseDownTarget === _overlay) closeSettings();
        _mouseDownTarget = null;
    });

    document.body.appendChild(_overlay);
    // Focus trap + inert + focus restore. Esc keeps this modal's own rule
    // (back out of the search before closing the whole thing).
    _closeOverlay = openOverlay(card, { onClose: _onEsc });

    // If a specific tab was requested, activate it now that the DOM is live.
    if (opts.tab) {
        const btn = _overlay.querySelector(`.tab-btn[data-tab="${opts.tab}"]`);
        if (btn) btn.click();
    }

    // Wire network section visibility toggles (must be after DOM append)
    _wireNetworkSection();
    _wireVoiceSection();

    // Validate models after modal is visible
    const listEl = document.getElementById('or-model-list');
    if (_models.length > 0 && listEl) {
        validateAllModels(listEl);
    }
}

// Everything typed into this modal lives in the DOM until Save, so every
// dismissal — the ×, Cancel, the backdrop, Escape — is a discard. (S2)
function _hasUnsavedChanges() {
    if (!_overlay) return false;
    if (Object.keys(collectChanges()).length > 0) return true;
    // API keys never reach collectChanges(): the server only ever reports
    // whether one is set, so there is nothing to diff a typed key against.
    return SECTIONS.some(section => section.fields.some(f =>
        f.type === 'apikey'
        && (document.getElementById(`setting-${f.key}`)?.value || '').trim() !== ''
    ));
}

export function closeSettings() {
    if (_hasUnsavedChanges() && !confirm('Discard unsaved settings changes?')) return;
    if (_closeOverlay) { _closeOverlay(); _closeOverlay = null; }
    if (_overlay) {
        document.body.removeChild(_overlay);
        _overlay = null;
    }
    _models = [];
    _availableModels = [];
    _envVarNames = [];
    _envDenylist = [];
    _envAllowlist = [];
    _corsOrigins = [];
    _restartRequired = false;
    _searchQuery = '';
    clearTimeout(_statusTimer);
}

// Called by openOverlay() on Escape.
function _onEsc() {
    // Escape backs out of the search first — closing the whole modal because
    // the user wanted to clear a filter loses every unsaved edit.
    const search = document.getElementById('settings-search');
    if (search && search.value) {
        search.value = '';
        _applySettingsFilter('');
        return;
    }
    closeSettings();
}
