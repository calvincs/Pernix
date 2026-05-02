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

function openQuestion(q) {
    if (_overlay) closeQuestion();
    _currentQuestionId = q.id;

    const answerInput = el('textarea', {
        class: 'question-answer',
        placeholder: 'Type your answer...',
        rows: '3',
    });

    const statusEl = el('span', { class: 'save-status' });

    const card = el('div', { class: 'modal-card' }, [
        el('div', { class: 'modal-header' }, [
            el('h2', {}, [text('Agent Question')]),
            el('button', { class: 'modal-close', onClick: () => dismissAndClose(q.id) }, [text('\u00d7')]),
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
            el('div', { class: 'question-answer-section' }, [
                el('label', {}, [text('Your answer:')]),
                answerInput,
            ]),
        ].filter(Boolean)),
        el('div', { class: 'modal-footer' }, [
            statusEl,
            el('button', { class: 'btn btn-secondary', onClick: () => dismissAndClose(q.id) }, [text('Dismiss')]),
            el('button', { class: 'btn btn-primary', onClick: async () => {
                const answer = answerInput.value.trim();
                if (!answer) {
                    statusEl.textContent = 'Please type an answer';
                    return;
                }
                try {
                    await post(`/api/questions/${q.id}/answer`, { answer });
                    statusEl.textContent = 'Sent!';
                    setTimeout(closeQuestion, 500);
                    checkQuestions(); // refresh badge
                } catch (e) {
                    statusEl.textContent = `Error: ${e.message}`;
                }
            }}, [text('Send Answer')]),
        ]),
    ]);

    _overlay = el('div', { class: 'modal-overlay', onClick: (e) => {
        if (e.target === _overlay) closeQuestion();
    }}, [card]);

    document.body.appendChild(_overlay);
    answerInput.focus();
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
