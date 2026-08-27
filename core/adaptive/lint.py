"""Pernix — Adaptive content lint: instructions in, narrative out.

The live-box audit (2026-08-27) found the store's policy slots filled with
narrative complaints — "Despite high-confidence verifications, the agent
repeatedly fails to..." — descriptive findings pasted verbatim into the
agent's every-turn prompt, where they cost tokens and change nothing. The
consumers of this store are prompts; prompts act on instructions. This lint
is the mechanical floor under every producer: content must read as
something an agent can DO, and negative claims about tools must carry the
fix, because a bare "X does not work" hardens into a refusal the agent
cites against itself for months after the actual problem was fixed (the
refine skill-proposal contract learned this first — core/refine.py).

Applied inside queue_producer_edits, so all four machine producers pass
through it (refine, dream, candor, telos). Human-authored entries take the
direct create path and are deliberately NOT linted — the human is the
authority the lint substitutes for. Delete/update-without-content edits
carry no prose and are skipped.
"""

from __future__ import annotations

import re

# Narrative-complaint shapes: observations about behavior, not instructions
# for it. These are the exact patterns the audit found saturating the
# policy slots.
_NARRATIVE_RES = (
    re.compile(r"^(despite|even though|although)\b", re.IGNORECASE),
    re.compile(
        r"\b(repeatedly fails?|appears? (?:to be )?ineffective|continues? to fail"
        r"|does not seem to|was not effective|remains? ineffective)\b",
        re.IGNORECASE,
    ),
)

# Negative tool claims are allowed ONLY with a fix clause — Candor's own
# template ("prefer an alternative or verify its output; see
# why_reliability(...)") is the model citizen here and must pass.
_NEGATIVE_CLAIM_RE = re.compile(
    r"\b(is broken|do(?:es)? not work|not working|unreliable|cannot be trusted|is unusable)\b",
    re.IGNORECASE,
)
_FIX_CLAUSE_RE = re.compile(
    r"\b(prefer|instead|verify|check|fall back|falls? back|use [^.;]{1,60}(?:instead|first)"
    r"|see why_reliability|workaround|fix|install|set [A-Z_]+=)\b",
    re.IGNORECASE,
)

# An instruction names an action or a condition->action shape. Deliberately
# broad — the lint is a floor, not a style guide.
_IMPERATIVE_RE = re.compile(
    r"\b(use|prefer|avoid|verify|check|run|call|write|read|stop|switch|route|retry"
    r"|do not|don'?t|never|always|must|should|when|before|after|instead|if|once|ensure"
    r"|treat|keep|skip|limit|cap|require)\b",
    re.IGNORECASE,
)

# Kinds whose content renders into a prompt as guidance and therefore must
# read as guidance. prompt_note is included: it lands in the agent prefix.
_LINTED_KINDS = frozenset({"policy", "routing_hint", "prompt_note"})


def lint_edit(edit: dict) -> str | None:
    """Reason the edit's content fails the actionability floor, or None.

    Only create/update edits with content are judged; deletes and bare
    version bumps carry no prose.
    """
    if not isinstance(edit, dict):
        return None
    if edit.get("action") not in ("create", "update"):
        return None
    content = str(edit.get("content") or "").strip()
    if not content:
        return None
    kind = str(edit.get("kind") or "")
    if kind not in _LINTED_KINDS:
        return None

    for rx in _NARRATIVE_RES:
        if rx.search(content):
            return (
                "narrative finding, not an instruction — state what the agent "
                "should DO (the observation belongs in the report/journal)"
            )
    if _NEGATIVE_CLAIM_RE.search(content) and not _FIX_CLAUSE_RE.search(content):
        return (
            "negative tool claim without a fix — capture the alternative or the "
            "repair step, never a bare 'X does not work'"
        )
    if not _IMPERATIVE_RE.search(content):
        return "no actionable directive found — content must tell the agent what to do and when"
    return None
