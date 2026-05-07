// Pernix — Question modal: shows agent questions and collects answers

import { el, text, clear } from '../../render.js';
import { get, post } from '../../api.js';

let _overlay = null;
let _pollTimer = null;
let _currentQuestionId = null;  // id of the question currently shown in the modal
const _dismissed = new Set();  // track dismissed question IDs to prevent auto-reopen

export function startQuestionPolling() {
    if (_pollTimer) return;
    _pollTimer = setInterval(checkQuestions, 5000);
    checkQuestions(); // immediate first check
}

export function stopQuestionPolling() {
    if (_pollTimer) {
        clearInterval(_pollTimer);
        _pollTimer = null;
    }
}

async function checkQuestions() {
    try {
        const data = await get('/api/questions');
        const questions = data.questions || [];
        const badge = document.getElementById('question-badge');
        if (questions.length > 0) {
            const undismissed = questions.filter(q => !_dismissed.has(q.id));
            badge.textContent = undismissed.length;
            badge.style.display = undismissed.length > 0 ? 'inline-block' : 'none';
            // Auto-open if not already showing and not previously dismissed
            if (!_overlay && undismissed.length > 0) {
                openQuestion(undismissed[0]);
            }
        } else {
            badge.style.display = 'none';
        }
    } catch {}
}

export async function openQuestionPanel() {
    try {
        const data = await get('/api/questions');
        const questions = data.questions || [];
        if (questions.length === 0) {
            return; // nothing to show
        }
        openQuestion(questions[0]);
    } catch {}
}

/** Close the modal only if it is currently showing the given question id. */
export function closeQuestionById(qid) {
    if (_currentQuestionId === qid) closeQuestion();
}

/**
 * Heuristic: does this question expect a yes/no answer?
 * Checks for explicit y/n markers, approval phrasing, and common question starters.
 */
function isYesNo(questionText) {
    const q = questionText.toLowerCase();
    if (/\(y\/n\)|\(yes\/no\)|yes or no|answer yes|answer no/.test(q)) return true;
    if (/^(approve|confirm|proceed|allow|enable|disable|delete|remove|reset|restart|continue|stop|cancel|skip|retry|overwrite|replace|create|update|install|uninstall|deploy|revert|merge|push|pull|run|execute)\b/.test(q)) return true;
    if (/^(would you|do you want|should i|can i|shall i|is this|are you|will you|have you|did you|does this|is it|are these|should we|can we|do you|shall we)\b/.test(q)) return true;
    if (/\?\s*$/.test(q) && q.length < 120 && /(want|like|ok|okay|good|correct|right|agree|sure|ready|proceed|confirm|approve)\?/.test(q)) return true;
    return false;
}

function openQuestion(q) {
    if (_overlay) closeQuestion();
    _currentQuestionId = q.id;

    const statusEl = el('span', { class: 'save-status' });

    async function submitAnswer(answer) {
        try {
            await post(`/api/questions/${q.id}/answer`, { answer });
            statusEl.textContent = 'Sent!';
            setTimeout(closeQuestion, 500);
            checkQuestions();
        } catch (e) {
            statusEl.textContent = `Error: ${e.message}`;
        }
    }

    let answerSection;

    if (isYesNo(q.question)) {
        // Yes / No / Other buttons
        const otherArea = el('div', { class: 'question-other-area', style: 'display:none' });
        const answerInput = el('textarea', {
            class: 'question-answer',
            placeholder: 'Type your answer...',
            rows: '3',
        });
        const sendBtn = el('button', { class: 'btn btn-primary', onClick: async () => {
            const answer = answerInput.value.trim();
            if (!answer) { statusEl.textContent = 'Please type an answer'; return; }
            await submitAnswer(answer);
        }}, [text('Send')]);
        otherArea.appendChild(answerInput);
        otherArea.appendChild(sendBtn);

        const yesBtn = el('button', { class: 'btn q-yes-btn', onClick: () => submitAnswer('Yes') }, [text('Yes')]);
        const noBtn  = el('button', { class: 'btn q-no-btn',  onClick: () => submitAnswer('No')  }, [text('No')]);
        const otherBtn = el('button', { class: 'btn q-other-btn', onClick: () => {
            otherArea.style.display = '';
            answerInput.focus();
        }}, [text('Other…')]);

        answerSection = el('div', { class: 'question-answer-section' }, [
            el('div', { class: 'q-yesno-btns' }, [yesBtn, noBtn, otherBtn]),
            otherArea,
        ]);
    } else {
        // Free-text for non-yes/no questions
        const answerInput = el('textarea', {
            class: 'question-answer',
            placeholder: 'Type your answer...',
            rows: '3',
        });
        answerSection = el('div', { class: 'question-answer-section' }, [
            el('label', {}, [text('Your answer:')]),
            answerInput,
        ]);
        // Override submitAnswer to read from textarea, attach footer Send
        answerSection._input = answerInput;
    }

    const footer = el('div', { class: 'modal-footer' }, [
        statusEl,
        el('button', { class: 'btn btn-secondary', onClick: () => dismissAndClose(q.id) }, [text('Dismiss')]),
        // For non-yes/no, add Send Answer button in footer
        ...(!isYesNo(q.question) ? [el('button', { class: 'btn btn-primary', onClick: async () => {
            const input = answerSection._input;
            const answer = input ? input.value.trim() : '';
            if (!answer) { statusEl.textContent = 'Please type an answer'; return; }
            await submitAnswer(answer);
        }}, [text('Send Answer')])] : []),
    ]);

    const card = el('div', { class: 'modal-card' }, [
        el('div', { class: 'modal-header' }, [
            el('h2', {}, [text('Agent Question')]),
            el('button', { class: 'modal-close', onClick: () => dismissAndClose(q.id) }, [text('×')]),
        ]),
        el('div', { class: 'modal-body' }, [
            // Source session
            q.session_title ? el('div', { class: 'question-source' }, [
                text(`From: ${q.session_title}`),
            ]) : null,
            // The question
            el('div', { class: 'question-text' }, [text(q.question)]),
            // Context if any
            q.context ? el('div', { class: 'question-context' }, [text(q.context)]) : null,
            // Answer input
            answerSection,
        ].filter(Boolean)),
        footer,
    ]);

    _overlay = el('div', { class: 'modal-overlay', onClick: (e) => {
        if (e.target === _overlay) closeQuestion();
    }}, [card]);

    document.body.appendChild(_overlay);

    // Auto-focus: textarea for open questions, nothing for yes/no (buttons are the CTA)
    if (!isYesNo(q.question) && answerSection._input) {
        answerSection._input.focus();
    }
    document.addEventListener('keydown', _onEsc);
}

async function dismissAndClose(questionId) {
    _dismissed.add(questionId);
    try {
        await post(`/api/questions/${questionId}/dismiss`);
    } catch {}
    closeQuestion();
    checkQuestions();
}

function closeQuestion() {
    if (_overlay) {
        document.body.removeChild(_overlay);
        _overlay = null;
    }
    _currentQuestionId = null;
    document.removeEventListener('keydown', _onEsc);
}

function _onEsc(e) {
    if (e.key === 'Escape') closeQuestion();
}
