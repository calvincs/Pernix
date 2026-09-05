"""Pernix — Grader hold-out: score the verifier against known answers.

Reflect grades every turn and nothing grades reflect. Its verdict mix has
drifted before (the 2026-08-27 calibration audit found ~1/3 of non-pass
verdicts over-strict in four repeating patterns) and the only way that was
ever noticed was a human reading transcripts. This is the standing version of
that read: a small set of turns whose right answer is already known, graded
nightly by the SAME prompt and the SAME parser the live path uses, scored, and
written to `snooze_state["trust.grader_holdout"]`.

Two rules make the number mean something:

  * The fixtures never enter the system. No session is created, no post-mortem
    is written, nothing touches memory or the workspace. A hold-out the loop
    can learn from is not a hold-out — it is training data with a score
    attached.
  * The prompt is imported, never copied. `REFLECT_PROMPT` and
    `_result_from_data` come from core/reflect.py, so a prompt edit changes
    what this measures on the next run instead of silently measuring the old
    rubric forever.

Scored on verdict, plus failure_cause when the expected verdict is non-pass:
"pass" is the whole answer for a pass, and "retry/agent" is the whole answer
for a failure — attributing a real failure to the wrong actor sends the next
retry at the wrong thing.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

from config import settings

logger = logging.getLogger("pernix.reflect.holdout")

FIXTURES_DIR = Path("data/eval/grader")
STATE_KEY = "trust.grader_holdout"

# Every key a fixture must carry to be scoreable.
_REQUIRED = ("id", "user_request", "final_response", "expected_verdict", "expected_failure_cause")


def load_fixtures(directory: Path | str | None = None) -> list[dict]:
    """Read the fixture set, sorted by filename. Malformed files are skipped.

    A broken fixture must not take the run down with it: the score is over
    the cases that could be read, and `n` says how many that was.
    """
    root = Path(directory) if directory else FIXTURES_DIR
    out: list[dict] = []
    try:
        paths = sorted(p for p in root.glob("*.json") if p.is_file())
    except OSError as e:
        logger.warning("Grader hold-out: cannot read %s: %s", root, e)
        return out
    for path in paths:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            logger.warning("Grader hold-out: skipping %s (%s)", path.name, e)
            continue
        if not isinstance(data, dict) or any(not str(data.get(k) or "") for k in _REQUIRED):
            logger.warning("Grader hold-out: skipping %s (missing required fields)", path.name)
            continue
        out.append(data)
    return out


def build_evidence(fixture: dict) -> str:
    """The fixture as the evidence blob reflect would have been handed.

    Mirrors the section order of `_build_compact_evidence`: tool summary,
    then the user's ask, then the attempt transcript, then the final
    response. The grader's rules refer to these headings by name (TOOL
    EXECUTION SUMMARY, USER REQUEST), so the shape is load-bearing.
    """
    parts: list[str] = []
    summary = fixture.get("tool_summary")
    if isinstance(summary, dict):
        lines = ["TOOL EXECUTION SUMMARY:"]
        if summary:
            for name, stats in sorted(summary.items()):
                stats = stats if isinstance(stats, dict) else {}
                lines.append(
                    f"- {name}: {int(stats.get('calls') or 0)} call(s), "
                    f"{int(stats.get('failures') or 0)} failure(s), "
                    f"{int(stats.get('total_latency_ms') or 0)}ms total"
                )
        else:
            lines.append("(no tool calls this attempt)")
        parts.append("\n".join(lines))

    parts.append(f"USER REQUEST:\n{fixture.get('user_request', '')}")

    transcript = str(fixture.get("transcript_excerpt") or "").strip()
    if transcript:
        parts.append("=" * 60)
        parts.append("ATTEMPT TRANSCRIPT (current attempt only — prior attempts elided)")
        parts.append("=" * 60)
        parts.append(transcript)

    parts.append(f"AGENT FINAL RESPONSE:\n{fixture.get('final_response') or '(no final assistant message)'}")
    return "\n\n".join(parts)


def expected_answer(fixture: dict) -> str:
    """The scoreable answer: "pass", or "<verdict>/<cause>" for a non-pass."""
    verdict = str(fixture.get("expected_verdict") or "").strip()
    cause = str(fixture.get("expected_failure_cause") or "none").strip()
    return verdict if verdict == "pass" else f"{verdict}/{cause}"


def _answer_of(verdict: str, cause: str) -> str:
    return verdict if verdict == "pass" else f"{verdict}/{cause or 'none'}"


async def _grade_evidence(evidence: str, model: str):
    """One grading call — the live rubric, none of the live side effects.

    Deliberately the narrowest possible seam: the same system prompt, the
    same JSON repair, the same `_result_from_data`. It does NOT go through
    `reflect_on_session`, which would need a session row, would write a
    post-mortem, and would put the fixture into the very corpus this is
    meant to stay out of. Tests monkeypatch this function.
    """
    from core.llm.client import get_llm_client
    from core.reflect import REFLECT_PROMPT, _result_from_data, _try_repair_json

    start = time.monotonic()
    response = await get_llm_client().chat(
        messages=[
            {"role": "system", "content": REFLECT_PROMPT},
            {"role": "user", "content": evidence},
        ],
        model=model,
        max_tokens=8192,
    )
    latency_ms = int((time.monotonic() - start) * 1000)
    raw = (response.content or "").strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        data = _try_repair_json(raw)
        if data is None:
            raise
    return _result_from_data(data, model, latency_ms)


async def run_holdout(directory: Path | str | None = None) -> dict:
    """Grade every fixture, score the run, and record it. Never raises.

    Returns (and stores under `trust.grader_holdout`)::

        {"accuracy": 0.89, "n": 9, "ran_at": "<iso>", "model": "<name>",
         "by_case": {"<id>": {"expected": "retry/agent",
                              "got": "pass", "ok": false}}}

    `accuracy` is None when nothing could be graded — no fixtures, or no
    model configured — so an empty run reads as "not measured" rather than
    as a perfect score.
    """
    fixtures = load_fixtures(directory)
    model = settings.llm_model or settings.background_model
    by_case: dict[str, dict] = {}
    graded = 0
    correct = 0

    for fx in fixtures:
        case_id = str(fx.get("id") or "")
        expected = expected_answer(fx)
        if not model:
            by_case[case_id] = {"expected": expected, "got": None, "ok": False, "error": "no model configured"}
            continue
        try:
            result = await _grade_evidence(build_evidence(fx), model)
        except Exception as e:
            logger.warning("Grader hold-out: %s could not be graded: %s", case_id, e)
            by_case[case_id] = {"expected": expected, "got": None, "ok": False, "error": type(e).__name__}
            continue
        got = _answer_of(result.verdict, result.failure_cause)
        ok = got == expected
        by_case[case_id] = {"expected": expected, "got": got, "ok": ok}
        graded += 1
        correct += 1 if ok else 0

    report = {
        "accuracy": round(correct / graded, 4) if graded else None,
        "n": graded,
        "by_case": by_case,
        "ran_at": datetime.now(timezone.utc).isoformat(),
        "model": model,
    }
    try:
        from db import models as db

        db.set_snooze_state(STATE_KEY, json.dumps(report))
    except Exception as e:
        logger.warning("Grader hold-out: could not record the run: %s", e)
    logger.info(
        "Grader hold-out: %s/%s correct (%s) on %s",
        correct,
        graded,
        f"{report['accuracy']:.0%}" if report["accuracy"] is not None else "not measured",
        model or "(no model)",
    )
    return report


def last_run() -> dict | None:
    """The stored report, or None when the hold-out has never run."""
    try:
        from db import models as db

        raw = db.get_snooze_state(STATE_KEY)
        return json.loads(raw) if raw else None
    except Exception:
        return None
