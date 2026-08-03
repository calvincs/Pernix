// Pernix — Voice input. One mic button, four engines behind it:
//
//   local_whisper / remote_whisper — MediaRecorder clip → POST /api/voice/transcribe
//                                    → text lands in the input box, editable before send
//   model_direct                   — MediaRecorder clip → pending attachment chip; the
//                                    audio-capable chat model hears the recording itself
//   web_speech                     — browser SpeechRecognition, live dictation into the
//                                    input box (audio goes to the browser vendor's
//                                    speech service — surfaced in Settings → Voice Input)
//
// The server (GET /api/voice/status) is the authority on which engine is
// configured and whether it's actually usable; the browser-dictation fallback
// only engages when the user has explicitly enabled it in settings.

import { get, getAuthToken } from './api.js';

let _deps = null;      // { textarea, addPendingFiles, appendMessage }
let _status = null;    // cached /api/voice/status payload
let _recorder = null;  // active MediaRecorder
let _stream = null;    // active getUserMedia stream (closed on stop)
let _recognition = null; // active SpeechRecognition
let _fallbackNoticeShown = false; // one privacy notice per page load, not per use

// Forgotten hot mic guard — dictation clips should be minutes, not hours.
const MAX_RECORD_MS = 5 * 60 * 1000;
let _autoStopTimer = null;

function _btn() {
    return document.getElementById('voice-btn');
}

async function _getStatus(force = false) {
    if (_status && !force) return _status;
    try {
        _status = await get('/api/voice/status');
    } catch (e) {
        console.warn('voice status unavailable:', e);
        _status = null;
    }
    return _status;
}

function _speechRecognitionCtor() {
    return window.SpeechRecognition || window.webkitSpeechRecognition || null;
}

async function _updateVisibility() {
    const btn = _btn();
    if (!btn) return;
    const st = await _getStatus(true);
    btn.hidden = !st || st.mode === 'off';
}

export function initVoice(deps) {
    _deps = deps;
    const btn = _btn();
    if (!btn) return;

    btn.addEventListener('click', () => {
        if (_recorder || _recognition) {
            stopVoice();
        } else {
            _start();
        }
    });

    // Escape cancels a live recording without transcribing (matches the
    // "preempt instantly" feel of the rest of the app).
    document.addEventListener('keydown', (e) => {
        if (e.key === 'Escape' && (_recorder || _recognition)) {
            stopVoice({ discard: true });
        }
    });

    _updateVisibility();
    // Settings saves can change the mode — re-check instead of holding a
    // stale verdict until reload.
    window.addEventListener('pernix:settings-saved', () => _updateVisibility());
}

export function stopVoice({ discard = false } = {}) {
    clearTimeout(_autoStopTimer);
    _autoStopTimer = null;
    if (_recognition) {
        const rec = _recognition;
        _recognition = null; // clear first — onend fires synchronously in some browsers
        try { rec.stop(); } catch { /* already stopped */ }
        _setUiState('idle');
    }
    if (_recorder) {
        const recorder = _recorder;
        _recorder = null;
        recorder._discard = discard;
        try { recorder.stop(); } catch { /* already stopped */ }
        // Track cleanup happens in onstop, after the last dataavailable.
    }
}

async function _start() {
    const st = await _getStatus(true);
    if (!st) {
        _deps.appendMessage('system', 'Voice input: could not reach the server for voice status.');
        return;
    }
    if (st.mode === 'off') return;

    let engine = st.mode;
    if (engine !== 'web_speech' && !st.usable) {
        // Configured engine can't run right now — browser dictation only if
        // the user opted into it (and its privacy ramifications) in settings.
        if (st.fallback_web_speech && _speechRecognitionCtor()) {
            engine = 'web_speech';
            if (!_fallbackNoticeShown) {
                _fallbackNoticeShown = true;
                _deps.appendMessage('notice',
                    `[voice: ${st.reason} — falling back to browser dictation. ` +
                    'Audio is processed by your browser vendor’s speech service, not this machine. ' +
                    'Configure this in Settings → Voice Input.]');
            }
        } else {
            _deps.appendMessage('system',
                `Voice input unavailable: ${st.reason}. ` +
                'Fix the engine or enable the browser-dictation fallback in Settings → Voice Input.');
            return;
        }
    }

    if (engine === 'web_speech') {
        _startWebSpeech(st);
    } else {
        await _startRecording(engine, st);
    }
}

// ---------------------------------------------------------------------------
// Browser dictation (Web Speech API)
// ---------------------------------------------------------------------------

function _startWebSpeech(st) {
    const Ctor = _speechRecognitionCtor();
    if (!Ctor) {
        _deps.appendMessage('system',
            'This browser has no speech recognition (Web Speech API). ' +
            'Use Chrome/Edge, or pick a whisper engine in Settings → Voice Input.');
        return;
    }
    const ta = _deps.textarea();
    const rec = new Ctor();
    rec.continuous = true;
    rec.interimResults = true;
    if (st.language) rec.lang = st.language;

    // Dictation appends to whatever was already typed; interim results
    // preview in place and are replaced by the final transcript.
    let base = ta.value ? ta.value.replace(/\s+$/, '') + ' ' : '';
    let finalText = '';

    rec.onresult = (e) => {
        let interim = '';
        for (let i = e.resultIndex; i < e.results.length; i++) {
            const r = e.results[i];
            if (r.isFinal) finalText += r[0].transcript;
            else interim += r[0].transcript;
        }
        ta.value = base + (finalText + interim).trimStart();
        ta.dispatchEvent(new Event('input'));
    };
    rec.onerror = (e) => {
        // 'no-speech' and 'aborted' are routine ends, not failures worth a message
        if (e.error && e.error !== 'no-speech' && e.error !== 'aborted') {
            _deps.appendMessage('system', `Voice dictation error: ${e.error}`);
        }
        if (_recognition === rec) {
            _recognition = null;
            _setUiState('idle');
        }
    };
    rec.onend = () => {
        if (_recognition === rec) {
            _recognition = null;
            _setUiState('idle');
        }
        ta.focus();
    };

    try {
        rec.start();
    } catch (e) {
        _deps.appendMessage('system', `Could not start dictation: ${e.message}`);
        return;
    }
    _recognition = rec;
    _setUiState('recording');
    _autoStopTimer = setTimeout(() => stopVoice(), MAX_RECORD_MS);
}

// ---------------------------------------------------------------------------
// Microphone recording (whisper transcription + model_direct voice notes)
// ---------------------------------------------------------------------------

function _pickMimeType() {
    const candidates = ['audio/webm;codecs=opus', 'audio/webm', 'audio/mp4', 'audio/ogg'];
    for (const c of candidates) {
        if (window.MediaRecorder && MediaRecorder.isTypeSupported(c)) return c;
    }
    return '';
}

function _extForMime(mime) {
    if (mime.includes('webm')) return '.webm';
    if (mime.includes('ogg')) return '.ogg';
    // Safari records audio/mp4 — name it .m4a so the server's ffmpeg→WAV
    // sidecar path (AUDIO_CONVERT_EXTENSIONS) recognizes it.
    if (mime.includes('mp4')) return '.m4a';
    return '.webm';
}

async function _startRecording(engine, st) {
    if (!navigator.mediaDevices?.getUserMedia) {
        _deps.appendMessage('system',
            'Microphone access requires a secure context (HTTPS). ' +
            'This page is served over plain HTTP — enable network mode TLS or open via localhost.');
        return;
    }
    let stream;
    try {
        stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    } catch (e) {
        _deps.appendMessage('system', `Microphone unavailable: ${e.message}`);
        return;
    }

    const mime = _pickMimeType();
    let recorder;
    try {
        recorder = new MediaRecorder(stream, mime ? { mimeType: mime } : undefined);
    } catch (e) {
        stream.getTracks().forEach(t => t.stop());
        _deps.appendMessage('system', `Recording not supported in this browser: ${e.message}`);
        return;
    }

    const chunks = [];
    recorder.ondataavailable = (e) => {
        if (e.data && e.data.size > 0) chunks.push(e.data);
    };
    recorder.onstop = async () => {
        stream.getTracks().forEach(t => t.stop());
        if (_stream === stream) _stream = null;
        _setUiState('idle');

        const blob = new Blob(chunks, { type: recorder.mimeType || mime || 'audio/webm' });
        // Sub-0.5s blobs are misclicks, not speech
        if (recorder._discard || blob.size < 2048) return;

        const ext = _extForMime(blob.type);
        const ts = new Date().toISOString().replace(/[:.]/g, '-').slice(0, 19);
        const file = new File([blob], `voice-note-${ts}${ext}`, { type: blob.type });

        if (engine === 'model_direct') {
            // Rides the normal attachment pipeline: chip → upload →
            // [attached: …] → ffmpeg WAV sidecar → audio-capable model.
            _deps.addPendingFiles([file]);
            return;
        }
        await _transcribeUpload(file);
    };

    _stream = stream;
    _recorder = recorder;
    recorder.start();
    _setUiState('recording');
    _autoStopTimer = setTimeout(() => stopVoice(), MAX_RECORD_MS);
}

async function _transcribeUpload(file) {
    _setUiState('busy');
    try {
        const fd = new FormData();
        fd.append('file', file);
        const headers = {};
        const token = getAuthToken();
        if (token) headers['Authorization'] = `Bearer ${token}`;
        const resp = await fetch('/api/voice/transcribe', { method: 'POST', headers, body: fd });
        if (!resp.ok) {
            let detail = resp.statusText;
            try { detail = (await resp.json()).detail || detail; } catch { /* keep statusText */ }
            throw new Error(detail);
        }
        const data = await resp.json();
        if (data.text) {
            _insertAtCursor(data.text);
        } else {
            _deps.appendMessage('system', 'Voice: no speech detected in the recording.');
        }
    } catch (e) {
        _deps.appendMessage('system', `Transcription failed: ${e.message}`);
    } finally {
        _setUiState('idle');
        _deps.textarea().focus();
    }
}

function _insertAtCursor(text) {
    const ta = _deps.textarea();
    const start = ta.selectionStart ?? ta.value.length;
    const end = ta.selectionEnd ?? ta.value.length;
    const before = ta.value.slice(0, start);
    const needsSpace = before && !/\s$/.test(before);
    const inserted = (needsSpace ? ' ' : '') + text;
    ta.value = before + inserted + ta.value.slice(end);
    ta.selectionStart = ta.selectionEnd = start + inserted.length;
    ta.dispatchEvent(new Event('input'));
}

// ---------------------------------------------------------------------------
// Button / placeholder state
// ---------------------------------------------------------------------------

let _savedPlaceholder = null;

function _setUiState(state) {
    const btn = _btn();
    const ta = _deps.textarea();
    if (!btn) return;
    btn.classList.toggle('recording', state === 'recording');
    btn.classList.toggle('busy', state === 'busy');
    btn.title = state === 'recording' ? 'Stop (Esc cancels)'
        : state === 'busy' ? 'Transcribing…'
        : 'Voice input';
    if (state === 'recording') {
        if (_savedPlaceholder === null) _savedPlaceholder = ta.placeholder;
        ta.placeholder = 'Listening…';
    } else if (state === 'busy') {
        if (_savedPlaceholder === null) _savedPlaceholder = ta.placeholder;
        ta.placeholder = 'Transcribing…';
    } else if (_savedPlaceholder !== null) {
        ta.placeholder = _savedPlaceholder;
        _savedPlaceholder = null;
    }
}
