// Pernix — Settings modal with tabs

import { el, text, clear } from '../../render.js';
import { get, post } from '../../api.js';
import { getPermission, requestPermission } from '../../notifications.js';
import { runVoiceTest } from '../../voice.js';

function _showToast(message, type = 'info') {
    const colors = { success: 'var(--success, #4c8)', error: 'var(--error, #e55)', info: 'var(--text-dim)' };
    const toast = el('div', {
        style: `position:fixed; bottom:1.5rem; left:50%; transform:translateX(-50%);`
             + `background:var(--bg-surface); border:1px solid ${colors[type] || colors.info};`
             + `color:${colors[type] || colors.info}; font-family:var(--mono); font-size:var(--text-sm);`
             + `padding:0.6rem 1.2rem; border-radius:var(--radius); z-index:9999;`
             + `box-shadow:var(--shadow-md); pointer-events:none; white-space:nowrap;`,
    }, [text(message)]);
    document.body.append(toast);
    setTimeout(() => toast.remove(), 3500);
}

let _overlay = null;
let _original = {};  // snapshot of settings when modal opened
let _models = [];    // [{id, valid: null|true|false, info: {}}]
let _availableModels = [];  // [{id, provider, ...}] from GET /api/models
let _ollamaModels = [];     // [{name, size, family, parameter_size, quantization}]
let _ollamaError = '';
let _ollamaLoading = false;

const NETWORK_FIELDS = [
    { key: 'network_enabled', label: 'Network Access', type: 'bool' },
    { key: 'ssl_mode', label: 'SSL Certificate', type: 'select', options: ['self_signed', 'custom'] },
    { key: 'ssl_cert_path', label: 'Certificate PEM Path', type: 'certpath' },
    { key: 'ssl_key_path', label: 'Key PEM Path', type: 'certpath' },
];

const SECTIONS = [
    {
        title: 'LLM Providers',
        description: 'Configure endpoints and concurrency for LLM providers. Max Concurrent limits parallel requests per provider. Session LLM Timeout caps how long a single session may hold LLM slots — prevents a hung or runaway session from blocking others indefinitely (0 = unlimited). Model selection is on the Models tab.',
        fields: [
            { key: 'llm_base_url', label: 'Ollama Base URL', type: 'text' },
            { key: 'llm_max_concurrent', label: 'Ollama Max Concurrent', type: 'number' },
            { key: 'openrouter_base_url', label: 'OpenRouter URL', type: 'text' },
            { key: 'openrouter_max_concurrent', label: 'OpenRouter Max Concurrent', type: 'number' },
            { key: 'openai_base_url', label: 'OpenAI URL (or any OpenAI-compatible server)', type: 'text' },
            { key: 'openai_max_concurrent', label: 'OpenAI Max Concurrent', type: 'number' },
            { key: 'llm_session_timeout', label: 'Session LLM Timeout (s)', type: 'number' },
            { key: 'openrouter_api_key', label: 'OpenRouter API Key', type: 'apikey', envKey: 'OPENROUTER_API_KEY' },
            { key: 'openai_api_key', label: 'OpenAI API Key', type: 'apikey', envKey: 'OPENAI_API_KEY' },
        ],
    },
    {
        title: 'Context',
        description: 'Token budget for conversations. Compaction automatically summarizes older messages when context fills up. Critical threshold triggers a hard reset if compaction can\'t free enough space.',
        fields: [
            { key: 'context_budget', label: 'Context Budget', type: 'number' },
            { key: 'max_tokens', label: 'Max Output Tokens', type: 'number' },
            { key: 'compaction_threshold', label: 'Compaction Threshold', type: 'number', step: 0.05 },
            { key: 'compaction_keep_tokens', label: 'Compaction Keep Tokens', type: 'number' },
            { key: 'context_critical_threshold', label: 'Critical Reset Threshold', type: 'number', step: 0.05 },
        ],
    },
    {
        title: 'Agent',
        description: 'Limits on the agent loop. Tool rounds caps tool calls per turn; continuations caps self-continue cycles. Scout sends a lightweight model ahead to discover relevant tools and context before the primary model responds.',
        fields: [
            { key: 'max_tool_rounds', label: 'Max Tool Rounds', type: 'number' },
            { key: 'max_continuations', label: 'Max Continuations', type: 'number' },
            { key: 'scout_enabled', label: 'Scout Enabled', type: 'bool' },
            { key: 'scout_timeout', label: 'Scout Timeout (s)', type: 'number' },
        ],
    },
    {
        title: 'Shell & Tools',
        description: 'Timeouts and security for tool execution. Strict shell security restricts commands to a built-in allowlist. Permissive mode allows any command. Size caps below: 0 = no cap. RLIMIT_FSIZE caps each bash subprocess\'s file writes; lift it for large model/video downloads.',
        fields: [
            { key: 'tool_timeout', label: 'Tool Timeout (s)', type: 'number' },
            { key: 'shell_timeout', label: 'Shell Timeout (s)', type: 'number' },
            { key: 'shell_security_mode', label: 'Shell Security', type: 'select', options: ['permissive', 'strict'] },
            { key: 'shell_address_space_limit_bytes', label: 'Bash RLIMIT_AS (bytes, 0 = no cap)', type: 'number' },
            { key: 'shell_fsize_limit_bytes', label: 'Bash RLIMIT_FSIZE (bytes, 0 = no cap)', type: 'number' },
            { key: 'max_file_write_size', label: 'Max file_write Size (bytes, 0 = no cap)', type: 'number' },
            { key: 'max_edit_read_size', label: 'Max file_edit Read Size (bytes, 0 = no cap)', type: 'number' },
        ],
    },
    {
        title: 'Web',
        description: 'Web search uses Tavily (requires API key — free tier at tavily.com). Browser uses Playwright for JS-rendered page extraction. Disable headless for login flows or visual debugging.',
        fields: [
            { key: 'web_search_enabled', label: 'Web Search', type: 'bool' },
            { key: 'tavily_api_key', label: 'Tavily API Key', type: 'apikey', envKey: 'TAVILY_API_KEY' },
            { key: 'browser_enabled', label: 'Enable Browser (Playwright)', type: 'bool' },
            { key: 'browser_headless', label: 'Headless Mode', type: 'bool' },
            { key: 'browser_timeout', label: 'Page Load Timeout (s)', type: 'number' },
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
        description: 'Automatic memory recall surfaces relevant past conversations at the start of each turn.',
        fields: [
            { key: 'memory_recall', label: 'Auto-Recall', type: 'bool' },
        ],
    },
    {
        title: 'Candor (Operational Memory)',
        description: 'Calibrated reliability tracking: tool outcomes and reflect verdicts feed an auditable evidence ledger, and scout receives an operational-intel brief flagging degraded tools, discovered conditions, and open questions. Observation capture, snooze maintenance, and the scout brief toggle immediately; the agent-facing tools (predict_reliability, why_reliability, reliability_questions) register at startup, so they appear/disappear after a restart.',
        fields: [
            { key: 'candor_enabled', label: 'Candor Enabled', type: 'bool' },
            { key: 'candor_scout_brief', label: 'Scout Intel Brief', type: 'bool' },
            { key: 'candor_max_obs_per_turn', label: 'Max Observations / Turn', type: 'number' },
        ],
    },
    {
        title: 'RLM (Recursive Processing)',
        description: 'Recursive Language Models: the agent processes inputs far beyond the context window (huge files, corpora, transcripts) by writing code in a sandboxed REPL that holds the input as a variable and delegates chunks to sub-LLM calls. The caps guard against runaway runs: iterations bounds root turns, sub-calls bounds total LLM spend per run, depth 2+ allows recursive child RLMs. Caps and models apply immediately; the rlm_process tool registers at startup, so enabling/disabling takes a restart. Root and sub-call models are chosen under Models → Model Roles.',
        fields: [
            { key: 'rlm_enabled', label: 'RLM Enabled', type: 'bool' },
            { key: 'rlm_max_iterations', label: 'Max Iterations', type: 'number' },
            { key: 'rlm_max_subcalls', label: 'Max Sub-calls / Run', type: 'number' },
            { key: 'rlm_max_concurrent_subcalls', label: 'Sub-call Concurrency', type: 'number' },
            { key: 'rlm_max_depth', label: 'Max Recursion Depth', type: 'number' },
            { key: 'rlm_timeout_seconds', label: 'Run Timeout (s)', type: 'number' },
            { key: 'rlm_run_retention_days', label: 'Run Data Retention (days)', type: 'number' },
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
        description: 'Post-task verification re-reads the agent\'s work and checks for mistakes or incomplete steps. If issues are found, the agent retries automatically. Min messages prevents reflect from firing on trivial exchanges.',
        fields: [
            { key: 'reflect_enabled', label: 'Post-Task Verification', type: 'bool' },
            { key: 'reflect_max_retries', label: 'Max Retries', type: 'number' },
            { key: 'reflect_min_messages', label: 'Min Messages to Trigger', type: 'number' },
            { key: 'post_mortem_retention_days', label: 'Post-mortem retention (days)', type: 'number' },
        ],
    },
    {
        title: 'Evaluation',
        description: 'Feature-level QA against acceptance criteria in the feature registry (data/registry.json). When auto-evaluate is enabled, runs after each task to score registered features. Browser screenshots provide visual verification evidence.',
        fields: [
            { key: 'eval_auto', label: 'Auto-Evaluate', type: 'bool' },
            { key: 'eval_threshold', label: 'Pass Threshold', type: 'number', step: 0.1 },
            { key: 'eval_max_retries', label: 'Max Retries', type: 'number' },
            { key: 'eval_browser_verify', label: 'Browser Screenshots', type: 'bool' },
        ],
    },
    {
        title: 'Orchestration',
        description: 'Controls for multi-worker task decomposition. Max workers limits parallel sub-agents. Stall threshold detects stuck workers. Plan review timeout is how long you have to approve a generated plan before it auto-proceeds.',
        fields: [
            { key: 'max_concurrent_workers', label: 'Max Workers', type: 'number' },
            { key: 'stall_threshold', label: 'Stall Threshold (s)', type: 'number' },
            { key: 'plan_review_timeout', label: 'Plan Review Timeout (s)', type: 'number' },
        ],
    },
];

const MODEL_SELECT_FIELDS = [
    { key: 'llm_model', label: 'Primary Model', type: 'model-select' },
    { key: 'scout_model', label: 'Scout Model', type: 'model-select', allowEmpty: true },
    { key: 'background_model', label: 'Background Model', type: 'model-select', allowEmpty: true },
    { key: 'reflect_model', label: 'Reflect Model', type: 'model-select', allowEmpty: true },
    { key: 'fallback_model', label: 'Fallback Model', type: 'model-select', allowEmpty: true },
    { key: 'rlm_root_model', label: 'RLM Root Model', type: 'model-select', allowEmpty: true },
    { key: 'rlm_sub_model', label: 'RLM Sub-call Model', type: 'model-select', allowEmpty: true },
    // Free text, not model-select: embedding models (nomic-embed-text, ...)
    // don't appear in the chat-model dropdown. Empty = lexical search only.
    { key: 'embedding_model', label: 'Embedding Model (Ollama; empty = lexical search only)', type: 'text' },
];

// Resolve the effective reflect model given the configured fallback chain:
// reflect_model -> background_model -> scout_model -> llm_model.
// Mirrors core/reflect.py so the UI warning matches runtime behavior.
function _effectiveReflectModel({ reflect_model, background_model, scout_model, llm_model }) {
    return reflect_model || background_model || scout_model || llm_model || '';
}

// ---------------------------------------------------------------------------
// Help tooltip
// ---------------------------------------------------------------------------

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
    const revokeEl = document.getElementById('revoke-access-section');
    if (revokeEl) revokeEl.style.display = (_original.network_enabled && _original.auth_token_set) ? '' : 'none';
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

function _showRestartButton() {
    const footer = document.querySelector('.modal-footer');
    if (!footer || document.getElementById('restart-server-btn')) return;

    const statusEl = footer.querySelector('.save-status');

    // The restart endpoint is loopback-only (403 otherwise). A phone or
    // LAN-IP browser used to get the button anyway and a dead-end error —
    // show instructions instead.
    const isLoopback = ['localhost', '127.0.0.1', '[::1]'].includes(window.location.hostname);
    if (!isLoopback) {
        if (statusEl) {
            statusEl.style.color = 'var(--warning, #fa3)';
            statusEl.textContent =
                'Restart required to apply network changes — restart from the server console (or from a localhost browser).';
        }
        return;
    }

    if (statusEl) {
        statusEl.style.color = 'var(--warning, #fa3)';
        statusEl.textContent = 'Restart required to apply network changes';
    }

    const restartBtn = el('button', {
        class: 'btn btn-primary',
        id: 'restart-server-btn',
        style: 'background: var(--error, #e55);',
        onClick: async () => {
            restartBtn.disabled = true;
            restartBtn.textContent = 'Restarting\u2026';
            if (statusEl) {
                statusEl.style.color = '';
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
                    statusEl.style.color = 'var(--error)';
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

function buildField(field, value) {
    const { key, label, type, step, options, allowEmpty } = field;

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
        return el('div', { class: 'setting-row' }, [
            el('label', { for: `setting-${key}` }, [text(label)]),
            input,
        ]);
    } else if (type === 'certpath') {
        // Write-only path field — never shows stored value
        const isSet = !!_original[key + '_set'];
        input = el('input', {
            type: 'text',
            id: `setting-${key}`,
            placeholder: isSet ? 'Set (hidden for security)' : 'Not set',
            value: '',
            autocomplete: 'off',
        });
        return el('div', { class: 'setting-row', id: `row-${key}` }, [
            el('label', { for: `setting-${key}` }, [text(label)]),
            input,
        ]);
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
        });
    } else {
        input = el('input', { type: 'text', id: `setting-${key}`, value: String(value ?? '') });
    }

    return el('div', { class: 'setting-row' }, [
        el('label', { for: `setting-${key}` }, [text(label)]),
        input,
    ]);
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

function collectChanges() {
    const changes = {};
    const allFields = [...SECTIONS.flatMap(s => s.fields), ...NETWORK_FIELDS];
    for (const field of allFields) {
        const input = document.getElementById(`setting-${field.key}`);
        if (!input) continue;

        if (field.type === 'apikey') continue;  // handled separately
        if (field.type === 'certpath') {
            // Write-only: only include if user typed a new value
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

    // Include env mode if changed
    const modeInput = document.getElementById('setting-shell_env_mode');
    if (modeInput && modeInput.value !== _original.shell_env_mode) {
        changes.shell_env_mode = modeInput.value;
    }

    // Include env lists if changed
    if (JSON.stringify(_envDenylist) !== JSON.stringify(_original.shell_env_denylist || [])) {
        changes.shell_env_denylist = _envDenylist;
    }
    if (JSON.stringify(_envAllowlist) !== JSON.stringify(_original.shell_env_allowlist || [])) {
        changes.shell_env_allowlist = _envAllowlist;
    }

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
        el('p', { class: 'or-hint' }, [text('Add OpenRouter model IDs to make them available as primary/scout/background models.')]),
        listEl,
        el('div', { class: 'or-model-add' }, [addInput, addBtn]),
    ]);

    renderModelList(listEl);

    // Model role selects
    const selectFields = MODEL_SELECT_FIELDS.map(f => buildField(f, _original[f.key]));
    const reflectWarning = el('div', {
        id: 'reflect-model-warning',
        style: 'display:none; background:var(--surface-hover, #2a1f10); '
             + 'border-left:3px solid var(--warning, #fa3); padding:0.6rem 0.8rem; '
             + 'margin-top:0.5rem; border-radius:4px; font-size:0.85rem; line-height:1.4;',
    }, []);
    const selectSection = el('div', { class: 'settings-section' }, [
        el('h3', {}, [text('Model Roles')]),
        el('p', { class: 'or-hint' }, [text('Primary handles conversations; scout runs parallel research; background handles titles/memory; reflect diagnoses failures and plans retries; fallback activates during rate limits. RLM root orchestrates recursive long-input runs (falls back to primary); RLM sub-call handles the chunk work those runs delegate (falls back to background, then primary).')]),
        ...selectFields,
        reflectWarning,
    ]);

    const wrapper = el('div', {}, [selectSection, buildOllamaSection(), container]);
    // Wire warning now that the selects exist in the subtree.
    setTimeout(_updateReflectWarning, 0);
    for (const key of ['reflect_model', 'background_model', 'scout_model', 'llm_model']) {
        const sel = selectSection.querySelector(`#setting-${key}`);
        if (sel) sel.addEventListener('change', _updateReflectWarning);
    }
    return wrapper;
}

function _updateReflectWarning() {
    const warning = document.getElementById('reflect-model-warning');
    if (!warning) return;
    const read = (k) => document.getElementById(`setting-${k}`)?.value ?? '';
    const effective = _effectiveReflectModel({
        reflect_model: read('reflect_model'),
        background_model: read('background_model'),
        scout_model: read('scout_model'),
        llm_model: read('llm_model'),
    });
    const primary = read('llm_model');
    if (effective && primary && effective === primary) {
        clear(warning);
        warning.append(
            el('strong', {}, [text('Reflect will diagnose its own output. ')]),
            text(`Reflect resolves to "${effective}", the same model as Primary. `),
            text('A weak model critiquing itself tends to produce retry strategies it cannot execute. '),
            text('Consider setting Reflect Model to a stronger model (e.g. an OpenRouter Claude or GPT) for higher-quality failure analysis.'),
        );
        warning.style.display = '';
    } else {
        warning.style.display = 'none';
    }
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

        const removeBtn = el('button', { class: 'or-remove' }, [text('\u00d7')]);
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

function buildEnvTab(settings) {
    _envDenylist = [...(settings.shell_env_denylist || [])];
    _envAllowlist = [...(settings.shell_env_allowlist || [])];

    const mode = settings.shell_env_mode || 'passthrough';

    // Mode selector
    const modeSelect = el('select', { id: 'setting-shell_env_mode' },
        ['passthrough', 'denylist', 'allowlist'].map(opt =>
            el('option', { value: opt, ...(opt === mode ? { selected: '' } : {}) }, [text(opt)])
        )
    );

    const modeHints = {
        passthrough: 'All host environment variables are passed to shell commands.',
        denylist: 'All host vars are passed EXCEPT those listed below.',
        allowlist: 'Only the vars listed below are passed (plus PATH and HOME).',
    };

    const hintEl = el('p', { class: 'or-hint', id: 'env-mode-hint' }, [text(modeHints[mode])]);

    // List container
    const listEl = el('div', { class: 'or-model-list', id: 'env-var-list' });

    // Add input
    const addInput = el('input', {
        type: 'text',
        class: 'or-model-input',
        placeholder: 'e.g. MY_VARIABLE',
        id: 'env-var-input',
    });
    const addBtn = el('button', { class: 'btn btn-primary btn-sm' }, [text('Add')]);
    addBtn.addEventListener('click', () => _addEnvVar(addInput, listEl, modeSelect.value));
    addInput.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') _addEnvVar(addInput, listEl, modeSelect.value);
    });
    const addRow = el('div', { class: 'or-model-add', id: 'env-add-row' }, [addInput, addBtn]);

    // Mode change handler
    modeSelect.addEventListener('change', () => {
        const m = modeSelect.value;
        hintEl.textContent = modeHints[m];
        _renderEnvList(listEl, m);
        // Show/hide add row and list based on mode
        const isPassthrough = m === 'passthrough';
        addRow.style.display = isPassthrough ? 'none' : '';
    });

    // Initial render
    _renderEnvList(listEl, mode);
    addRow.style.display = mode === 'passthrough' ? 'none' : '';

    const container = el('div', { class: 'settings-section' }, [
        el('h3', {}, [text('Shell Environment')]),
        el('div', { class: 'setting-row' }, [
            el('label', {}, [text('Env Mode')]),
            modeSelect,
        ]),
        hintEl,
        listEl,
        addRow,
    ]);

    return container;
}

function _getActiveEnvList(mode) {
    return mode === 'denylist' ? _envDenylist : _envAllowlist;
}

function _renderEnvList(listEl, mode) {
    clear(listEl);

    if (mode === 'passthrough') {
        // Show info about host vars
        if (_envVarNames.length > 0) {
            listEl.appendChild(el('div', { class: 'or-empty' },
                [text(`${_envVarNames.length} host vars available (all passed through)`)]
            ));
        } else {
            listEl.appendChild(el('div', { class: 'or-empty' }, [text('All host environment variables will be passed through')]));
        }
        return;
    }

    const list = _getActiveEnvList(mode);
    const label = mode === 'denylist' ? 'blocked' : 'allowed';

    if (list.length === 0) {
        listEl.appendChild(el('div', { class: 'or-empty' }, [text(`No vars ${label} — use input below to add`)]));
        return;
    }

    for (let i = 0; i < list.length; i++) {
        const varName = list[i];
        const idx = i;
        const removeBtn = el('button', { class: 'or-remove' }, [text('\u00d7')]);
        removeBtn.addEventListener('click', () => {
            list.splice(idx, 1);
            _renderEnvList(listEl, mode);
        });

        const exists = _envVarNames.length === 0 || _envVarNames.includes(varName);
        const statusEl = el('span', { class: `or-status ${exists ? 'valid' : 'invalid'}` },
            [text(exists ? '\u2713' : '\u2717')]
        );

        const item = el('div', { class: 'or-model-item' }, [
            statusEl,
            el('span', { class: 'or-model-id' }, [text(varName)]),
            removeBtn,
        ]);
        listEl.appendChild(item);
    }
}

function _addEnvVar(inputEl, listEl, mode) {
    const name = inputEl.value.trim().toUpperCase();
    if (!name) return;

    const list = _getActiveEnvList(mode);
    if (list.includes(name)) {
        inputEl.style.borderColor = 'var(--error)';
        setTimeout(() => { inputEl.style.borderColor = ''; }, 1500);
        return;
    }

    list.push(name);
    inputEl.value = '';
    _renderEnvList(listEl, mode);
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
            buildHelpIcon('Scan the QR code with a mobile device to connect automatically. The link contains your auth token.'),
        ]),
        el('div', { style: 'display:flex; gap:0.5rem; align-items:center;' }, [
            _buildQRButton(),
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

    // --- Revoke Remote Access section (visible when network enabled + token set) ---
    const revokeSection = el('div', {
        class: 'settings-section',
        id: 'revoke-access-section',
        style: (settings.network_enabled && settings.auth_token_set) ? '' : 'display:none',
    }, [
        el('h3', {}, [
            text('Danger Zone'),
            buildHelpIcon('Regenerate the auth token to immediately invalidate all existing remote sessions. Local access is unaffected.'),
        ]),
        el('div', { style: 'display:flex; gap:0.5rem; align-items:center;' }, [
            _buildRevokeButton(),
        ]),
    ]);

    return el('div', {}, [section, qrSection, revokeSection, originsSection]);
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
            qrContainer.innerHTML = svg;
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

function _buildRevokeButton() {
    const btn = el('button', {
        class: 'btn-secondary',
        style: 'font-family:var(--mono); font-size:var(--text-sm); padding:0.4rem 0.8rem; cursor:pointer; border-color:var(--error, #e55); color:var(--error, #e55);',
    }, [text('Revoke Remote Access')]);

    btn.addEventListener('click', () => {
        const overlay = el('div', { class: 'modal-overlay' });
        const card = el('div', {
            class: 'modal-card',
            style: 'max-width:340px; padding:1.5rem;',
        });
        const title = el('h2', {
            style: 'font-size:var(--text-lg); margin-bottom:0.75rem;',
        }, [text('Revoke Remote Access?')]);
        const body = el('p', {
            style: 'font-family:var(--mono); font-size:var(--text-sm); color:var(--text-dim); margin-bottom:1.25rem; line-height:1.5;',
        }, [text('This will regenerate the auth token. All existing remote sessions (QR links, shared URLs) will stop working immediately. Local access is unaffected.')]);
        const btnRow = el('div', { style: 'display:flex; gap:0.5rem; justify-content:flex-end;' });
        const cancelBtn = el('button', {
            class: 'btn-secondary',
            style: 'padding:0.4rem 0.8rem; cursor:pointer;',
        }, [text('Cancel')]);
        const confirmBtn = el('button', {
            class: 'btn-primary',
            style: 'padding:0.4rem 0.8rem; cursor:pointer; background:var(--error, #e55); border-color:var(--error, #e55);',
        }, [text('Revoke')]);

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
            confirmBtn.textContent = 'Revoking\u2026';
            try {
                await post('/api/settings/auth-token/regenerate');
                close();
                _showToast('Remote access token regenerated. Existing remote sessions are now invalid.', 'success');
            } catch (e) {
                confirmBtn.disabled = false;
                confirmBtn.textContent = 'Revoke';
                let errEl = card.querySelector('.revoke-error');
                if (!errEl) {
                    errEl = el('div', { class: 'revoke-error', style: 'color:var(--error, #e55); font-family:var(--mono); font-size:var(--text-xs); margin-top:0.75rem;' });
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
    const row = el('div', { class: 'settings-row' }, [
        el('label', { class: 'settings-label' }, [text('Browser Notifications')]),
        el('div', { class: 'settings-control', style: 'display:flex; align-items:center; gap:0.75rem;' }, [statusEl]),
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
                `Permanently delete ${toDelete} session(s) and all of their messages?\n\n`
                + `This cascades to any worker sessions and cannot be undone.`
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
            _showToast(`Pruned ${result.purged} session(s)`, 'success');
        } catch (e) {
            statusEl.textContent = `Prune failed: ${e.message || e}`;
            statusEl.style.color = 'var(--error)';
            _showToast('Prune failed', 'error');
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
            el('li', {}, [text('A prompt injection or a misbehaving workflow can cause irreversible damage silently.')]),
            el('li', {}, [text('Only enable if you fully trust the current task context. Disable it when done.')]),
        ]),
    ]);

    // Run Dangerously — read-only status badge (only settable via --dangerous at startup)
    const isEnabled = !!settings.auto_approve_dangerous;
    const statusBadge = el('span', {
        style: 'font-size:var(--text-xs); font-weight:700; padding:2px 10px; border-radius:3px; '
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
        const clearBtn = el('button', { class: 'btn btn-secondary', style: 'font-size:var(--text-xs); padding:2px 8px;' }, [text('Clear all')]);
        clearBtn.addEventListener('click', async () => {
            try {
                await fetch('/api/settings/tool-approvals', { method: 'DELETE', headers: { 'Content-Type': 'application/json' } });
                renderScopes();
            } catch { /* ignore */ }
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
        securityPlaceholder.innerHTML = '';
        securityPlaceholder.appendChild(content);
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
        });
    });

    return { tabBar, contents };
}

// ---------------------------------------------------------------------------
// Modal lifecycle
// ---------------------------------------------------------------------------

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
        console.error('Failed to load settings:', e);
        return;
    }
    _original = { ...settings };

    // Initialize model list from settings
    const modelIds = settings.openrouter_models || [];
    _models = (Array.isArray(modelIds) ? modelIds : []).map(id => ({ id, valid: null, info: null }));

    const { tabBar, contents } = buildTabs(settings);
    const statusEl = el('span', { class: 'save-status' });

    const card = el('div', { class: 'modal-card' }, [
        el('div', { class: 'modal-header' }, [
            el('h2', {}, [text('Settings')]),
            el('button', { class: 'modal-close', onClick: closeSettings }, [text('\u00d7')]),
        ]),
        tabBar,
        el('div', { class: 'modal-body' }, contents),
        el('div', { class: 'modal-footer' }, [
            statusEl,
            el('button', { class: 'btn btn-secondary', onClick: closeSettings }, [text('Cancel')]),
            el('button', {
                class: 'btn btn-primary',
                id: 'settings-save-btn',
                onClick: async () => {
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
                        statusEl.textContent = 'No changes';
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
                        if (Object.keys(changes).length > 0) {
                            result = await post('/api/settings', changes);
                            saved.push(...(result.updated || []));
                        }

                        // Handle SSL validation errors
                        if (result.ssl_errors && result.ssl_errors.length > 0) {
                            statusEl.style.color = 'var(--error)';
                            statusEl.textContent = result.ssl_errors.join('; ');
                            return;
                        }

                        // Clear certpath inputs after save (write-only)
                        for (const f of NETWORK_FIELDS) {
                            if (f.type !== 'certpath') continue;
                            const inp = document.getElementById(`setting-${f.key}`);
                            if (inp && inp.value) {
                                inp.value = '';
                                inp.placeholder = 'Set (hidden for security)';
                                _original[f.key + '_set'] = true;
                            }
                        }

                        statusEl.style.color = '';
                        statusEl.textContent = `Saved: ${saved.join(', ')}`;
                        _original = { ..._original, ...changes };

                        // Live features (voice mic button) re-check their
                        // config on this instead of holding stale state.
                        window.dispatchEvent(new CustomEvent('pernix:settings-saved', { detail: { saved } }));

                        // If openrouter_models changed, refresh model dropdowns
                        if (changes.openrouter_models) {
                            try {
                                const m = await get('/api/models');
                                _availableModels = (m.models || []).sort((a, b) => a.id.localeCompare(b.id));
                                _rebuildModelSelects();
                            } catch (e) {
                                console.warn('Failed to refresh models after save:', e);
                            }
                        }

                        // Show restart button if network settings changed
                        if (result.restart_required) {
                            _restartRequired = true;
                            _showRestartButton();
                        } else {
                            setTimeout(() => { statusEl.textContent = ''; }, 3000);
                        }
                    } catch (e) {
                        statusEl.textContent = `Error: ${e.message}`;
                        statusEl.style.color = 'var(--error)';
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
    document.addEventListener('keydown', _onEsc);

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

export function closeSettings() {
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
    document.removeEventListener('keydown', _onEsc);
}

function _onEsc(e) {
    if (e.key === 'Escape') closeSettings();
}
