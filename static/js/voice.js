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

let _deps = null;      // { textarea, addPendingFiles, appendMessage, send }
let _status = null;    // cached /api/voice/status payload
let _recorder = null;  // active MediaRecorder
let _stream = null;    // active getUserMedia stream (closed on stop)
let _recognition = null; // active SpeechRecognition
let _fallbackNoticeShown = false; // one privacy notice per page load, not per use

// Forgotten hot mic guard — dictation clips should be minutes, not hours.
const MAX_RECORD_MS = 5 * 60 * 1000;
let _autoStopTimer = null;

// Press-duration gesture model (Telegram/WhatsApp/Discord-mobile style):
// a tap toggles (press again to stop — right for long dictation), while
// press-and-hold is push-to-talk (release stops and transcribes). A press
// shorter than HOLD_MS is a tap; longer is a hold.
const HOLD_MS = 500;
let _gestureDownAt = 0;       // timestamp of the press that started this gesture
let _gestureStopping = false; // press consumed as "stop" — ignore its release
let _hotkeyDown = false;      // a Ctrl/Cmd+Shift+M gesture is in flight
let _starting = false;        // engine spin-up in progress (getUserMedia etc.)
let _stopRequested = null;    // release arrived before spin-up finished: {discard}

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

    // Pointer events (not click) so tap-vs-hold can be distinguished.
    // preventDefault keeps focus in the textarea; pointer capture routes the
    // release here even if the pointer drifts off the button mid-hold.
    btn.addEventListener('pointerdown', (e) => {
        if (e.button !== 0) return; // main button/touch only
        e.preventDefault();
        try { btn.setPointerCapture(e.pointerId); } catch { /* capture unsupported */ }
        _pressStart();
    });
    btn.addEventListener('pointerup', () => _pressEnd());
    btn.addEventListener('pointercancel', () => _pressEnd());

    // Escape cancels a live recording without transcribing (matches the
    // "preempt instantly" feel of the rest of the app). Ctrl/Cmd+Shift+M is
    // the mic gesture key — the de-facto combo (Discord, Teams).
    document.addEventListener('keydown', (e) => {
        if (e.key === 'Escape' && (_recorder || _recognition || _starting)) {
            stopVoice({ discard: true });
            return;
        }
        if ((e.ctrlKey || e.metaKey) && e.shiftKey && e.key.toLowerCase() === 'm') {
            const b = _btn();
            if (!b || b.hidden) return; // no engine configured — leave the combo alone
            e.preventDefault();
            if (e.repeat) return; // held keys auto-repeat; one gesture per physical press
            _hotkeyDown = true;
            _pressStart();
        }
    });
    document.addEventListener('keyup', (e) => {
        if (_hotkeyDown && e.key.toLowerCase() === 'm') {
            _hotkeyDown = false;
            _pressEnd();
        }
    });

    _updateVisibility();
    // Settings saves can change the mode — re-check instead of holding a
    // stale verdict until reload.
    window.addEventListener('pernix:settings-saved', () => _updateVisibility());
}

// A press either starts the engine or, if one is live (or spinning up),
// stops it — and flags the gesture so its own release doesn't re-evaluate.
function _pressStart() {
    if (_recorder || _recognition || _starting) {
        _gestureStopping = true;
        stopVoice();
        return;
    }
    _gestureStopping = false;
    _gestureDownAt = Date.now();
    _start();
}

function _pressEnd() {
    if (_gestureStopping) {
        _gestureStopping = false;
        return;
    }
    if (!_gestureDownAt) return;
    const held = Date.now() - _gestureDownAt;
    _gestureDownAt = 0;
    // Hold = push-to-talk: release ends it. A quick tap toggles on and
    // stays listening until the next press.
    if (held >= HOLD_MS) stopVoice();
}

export function stopVoice({ discard = false } = {}) {
    // Release can beat engine spin-up (getUserMedia prompt, recognizer
    // start). Park the request; _start() honors it the moment it's live.
    if (_starting && !_recorder && !_recognition) {
        _stopRequested = { discard };
        return;
    }
    clearTimeout(_autoStopTimer);
    _autoStopTimer = null;
    if (_recognition) {
        const rec = _recognition;
        _recognition = null; // clear first — onend fires synchronously in some browsers
        rec._discard = discard; // onend uses this to skip auto-send / restore text
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
    _starting = true;
    _stopRequested = null;
    try {
        // Cached status keeps the press-to-listening gap imperceptible — a
        // blocking refetch here used to swallow the first words. The cache
        // is refreshed on load and whenever settings save.
        const st = await _getStatus();
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
    } finally {
        _starting = false;
        if (_stopRequested) {
            const req = _stopRequested;
            _stopRequested = null;
            if (_recorder || _recognition) stopVoice(req);
        }
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
        if (rec._discard) {
            // Esc = cancel: put back whatever was typed before dictation.
            ta.value = base.trimEnd();
            ta.dispatchEvent(new Event('input'));
        } else if (_status?.auto_send && finalText.trim()) {
            // Speech was detected — auto-send if enabled. A manual send mid-
            // dictation is safe: it empties the input, and send() no-ops on
            // an empty message.
            _deps.send();
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

async function _postTranscribe(file) {
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
    return ((await resp.json()).text || '').trim();
}

async function _transcribeUpload(file) {
    _setUiState('busy');
    try {
        const text = await _postTranscribe(file);
        if (text) {
            _insertAtCursor(text);
            // Speech was detected — that's the auto-send gate. An empty
            // transcript never sends, so a misclick can't fire a message.
            if (_status?.auto_send) _deps.send();
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
// Settings → Voice Input "Test" — round-trip the saved engine without
// touching the chat input. Resolves {ok, text?|detail?, error?}; never
// rejects, so callers just render the result.
// ---------------------------------------------------------------------------

const TEST_RECORD_MS = 4000;

export function runVoiceTest(mode, onPhase = () => {}) {
    if (mode === 'web_speech') return _testWebSpeech(onPhase);
    if (mode === 'local_whisper' || mode === 'remote_whisper') return _testWhisper(onPhase);
    if (mode === 'model_direct') return _testModelDirect(onPhase);
    return Promise.resolve({ ok: false, error: 'Select an engine first' });
}

function _testWebSpeech(onPhase) {
    return new Promise((resolve) => {
        const Ctor = _speechRecognitionCtor();
        if (!Ctor) {
            resolve({ ok: false, error: 'This browser has no Web Speech API (try Chrome/Edge)' });
            return;
        }
        const rec = new Ctor();
        rec.interimResults = false;
        let settled = false;
        let timer = null;
        const done = (res) => {
            if (settled) return;
            settled = true;
            clearTimeout(timer);
            try { rec.stop(); } catch { /* already stopped */ }
            resolve(res);
        };
        rec.onresult = (e) => done({ ok: true, text: e.results[0][0].transcript.trim() });
        rec.onerror = (e) => done({
            ok: false,
            error: e.error === 'not-allowed' ? 'Microphone permission denied'
                : e.error === 'network' ? 'Speech service unreachable — browser dictation needs internet'
                : `Dictation error: ${e.error}`,
        });
        rec.onend = () => done({ ok: false, error: 'No speech detected — try again' });
        timer = setTimeout(() => done({ ok: false, error: 'No speech detected — try again' }), TEST_RECORD_MS + 4000);
        onPhase('listening');
        try {
            rec.start();
        } catch (e) {
            done({ ok: false, error: e.message });
        }
    });
}

async function _testWhisper(onPhase) {
    let file;
    try {
        file = await _recordClip(TEST_RECORD_MS, onPhase);
    } catch (e) {
        return { ok: false, error: e.message };
    }
    onPhase('transcribing');
    try {
        const text = await _postTranscribe(file);
        if (!text) return { ok: false, error: 'Engine responded but heard no speech — try again' };
        return { ok: true, text };
    } catch (e) {
        return { ok: false, error: e.message };
    }
}

async function _testModelDirect(onPhase) {
    onPhase('checking');
    if (!navigator.mediaDevices?.getUserMedia) {
        return { ok: false, error: 'Microphone requires a secure context (HTTPS)' };
    }
    try {
        const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
        stream.getTracks().forEach(t => t.stop());
    } catch (e) {
        return { ok: false, error: `Microphone unavailable: ${e.message}` };
    }
    const st = await _getStatus(true);
    if (!st) return { ok: false, error: 'Could not reach the server for voice status' };
    if (!st.usable) return { ok: false, error: st.reason || 'Engine not usable' };
    return { ok: true, detail: 'Microphone OK; the active model accepts audio. Recordings will attach to your messages.' };
}

/** Record a fixed-length clip from the mic and return it as a File. */
function _recordClip(ms, onPhase) {
    return new Promise((resolve, reject) => {
        if (!navigator.mediaDevices?.getUserMedia) {
            reject(new Error('Microphone requires a secure context (HTTPS)'));
            return;
        }
        navigator.mediaDevices.getUserMedia({ audio: true }).then((stream) => {
            const mime = _pickMimeType();
            let recorder;
            try {
                recorder = new MediaRecorder(stream, mime ? { mimeType: mime } : undefined);
            } catch (e) {
                stream.getTracks().forEach(t => t.stop());
                reject(new Error(`Recording not supported: ${e.message}`));
                return;
            }
            const chunks = [];
            recorder.ondataavailable = (e) => {
                if (e.data && e.data.size > 0) chunks.push(e.data);
            };
            recorder.onstop = () => {
                stream.getTracks().forEach(t => t.stop());
                const blob = new Blob(chunks, { type: recorder.mimeType || mime || 'audio/webm' });
                if (blob.size < 2048) {
                    reject(new Error('Recording was empty — is the microphone muted?'));
                    return;
                }
                resolve(new File([blob], `voice-test${_extForMime(blob.type)}`, { type: blob.type }));
            };
            onPhase('listening');
            recorder.start();
            setTimeout(() => {
                try { recorder.stop(); } catch { /* already stopped */ }
            }, ms);
        }, (e) => reject(new Error(`Microphone unavailable: ${e.message}`)));
    });
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
    btn.title = state === 'recording' ? 'Listening — tap to stop, or release if holding (Esc cancels)'
        : state === 'busy' ? 'Transcribing…'
        : 'Voice input — tap to toggle, hold to talk (Ctrl+Shift+M)';
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
