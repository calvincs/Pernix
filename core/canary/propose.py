"""Pernix — Canary proposal generation (plan §5 "growing the suite", §12.2).

Refine turns real failed turns into CANDIDATE canaries — the regression-test
convention at the behavior level. Two admission paths:

  human   — proposals ride adaptive_proposals (producer="canary_propose",
            dict payload); APPROVING one materializes the CANARY.md through a
            validated round-trip, then enqueues a manual vetting run.
  auto    — (canary_auto_admit) a spec whose gate commands pass the strict
            allowlist proof below materializes immediately, tagged
            flaky+vetting so it INFORMS but cannot trip the tripwire until
            the maintenance sweep has seen canary_vetting_runs consistent
            runs. Specs the machine cannot prove safe — and everything once
            the suite hits canary_max_suite — fall back to the human path.

The allowlist is the security boundary the human click used to be: gate
commands are LLM-authored from transcripts that contain untrusted content,
so auto-admission executes only a closed set of binaries on workspace-
relative paths with no shell metacharacters.
"""

from __future__ import annotations

import json
import logging
import re
import shlex
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import yaml

from config import settings
from db import models as db

logger = logging.getLogger("pernix.canary")

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,48}$")

# Auto-admission gate-command proof. Closed-world by construction: one plain
# command (no shell metacharacters survive, so no chaining/redirection/
# substitution), binary from a fixed read-only-ish set, every path-like token
# workspace-relative. `python -c` is excluded (arbitrary code); `python -m`
# is allowed only for pytest/unittest.
_SAFE_GATE_BINARIES = frozenset(
    {"python", "python3", "pytest", "grep", "diff", "cmp", "test", "[", "cat", "head", "tail", "wc", "ls"}
)
_SAFE_PYTHON_MODULES = frozenset({"pytest", "unittest"})
_SHELL_META_RE = re.compile(r"[;&|<>`$\n\\]")
_AUTO_TIMEOUT_CAP_S = 900

# Holdout resemblance (W5). Refine reads a transcript that may contain a
# canary prompt verbatim — a canary session's own, or one a user pasted —
# and proposing "a canary for this" would quietly re-admit a holdout task
# under a new name, with the answer now written into a reviewable file.
# Comparison is normalised (case, punctuation and whitespace folded away)
# and windowed: an exact-substring test never fires on a reworded copy, and
# a bag-of-words test fires on everything.
_HOLDOUT_WINDOW_WORDS = 10
# Fixed seed used only to materialise a generated holdout's prompt for this
# comparison. Never used for a run — a scored run always draws a fresh one.
_HOLDOUT_REFERENCE_SEED = 0
_NORMALISE_RE = re.compile(r"[^a-z0-9]+")

CANARY_PROPOSALS_PROMPT = """
ADDITIONALLY output a "canary_proposals" array in the same JSON object
(empty array when nothing qualifies). A canary proposal turns THIS
session's failure into a permanent regression check — a small, offline,
deterministic task with shell-command gates:

  "canary_proposals": [
    {
      "name": "kebab-case-name",
      "prompt": "self-contained task instructions for a fresh agent",
      "gates": [{"name": "g1", "command": "shell command, exit 0 = pass", "watch_paths": []}],
      "files": {"relative/path.txt": "seed fixture content"},
      "rationale": "which failure in this session this canary pins"
    }
  ]

Only propose when the session exposed a REPEATABLE failure class (not a
one-off env problem), the task can run offline against seeded fixture
files, and the gates are deterministic. At most 1 proposal. A human
reviews it before it joins the suite."""


def _normalise(text: str) -> list[str]:
    return [w for w in _NORMALISE_RE.sub(" ", (text or "").lower()).split() if w]


def _windows(words: list[str], n: int = _HOLDOUT_WINDOW_WORDS) -> set[str]:
    if len(words) < n:
        return {" ".join(words)} if words else set()
    return {" ".join(words[i : i + n]) for i in range(len(words) - n + 1)}


def holdout_prompts(base: Path | None = None) -> dict[str, str]:
    """{name: prompt} for every holdout canary.

    A generated holdout has no prompt in its file, so one is materialised
    from a fixed reference seed purely for comparison — the wording around
    the seeded values is what a copy would reproduce.
    """
    from core.canary.parser import scan_canaries

    out: dict[str, str] = {}
    for c in scan_canaries(base):
        if not c.holdout:
            continue
        prompt = c.prompt
        if not prompt and c.generated:
            try:
                from core.canary.fixtures import generate_variant

                prompt = generate_variant(c, _HOLDOUT_REFERENCE_SEED).prompt
            except Exception as e:
                logger.debug("Holdout prompt materialisation failed for '%s': %s", c.name, e)
                continue
        if prompt:
            out[c.name] = prompt
    return out


def resembles_holdout(spec: dict, base: Path | None = None) -> str | None:
    """The holdout canary this proposal re-describes, or None.

    Compares the proposal's prompt AND its seed files against each holdout
    prompt: a proposal can copy the task into either.
    """
    try:
        holdouts = holdout_prompts(base)
    except Exception as e:
        logger.debug("Holdout listing failed: %s", e)
        return None
    if not holdouts:
        return None

    candidate = " ".join(
        [str(spec.get("prompt") or "")]
        + [str(k) for k in (spec.get("files") or {})]
        + [str(v) for v in (spec.get("files") or {}).values()]
    )
    cand_words = _normalise(candidate)
    cand_windows = _windows(cand_words)
    if not cand_windows:
        return None
    for name, prompt in sorted(holdouts.items()):
        if _windows(_normalise(prompt)) & cand_windows:
            return name
    return None


def is_gate_command_safe(command: str) -> str | None:
    """Prove one gate command safe for unreviewed execution. None = safe,
    else the reason it needs human eyes."""
    cmd = (command or "").strip()
    if not cmd:
        return "empty command"
    if _SHELL_META_RE.search(cmd):
        return "shell metacharacters"
    try:
        tokens = shlex.split(cmd)
    except ValueError as e:
        return f"unparseable: {e}"
    if not tokens:
        return "empty command"
    binary = Path(tokens[0]).name
    if binary != tokens[0]:
        return "binary must be a bare name (no path)"
    if binary not in _SAFE_GATE_BINARIES:
        return f"binary {binary!r} not in allowlist"
    if binary in ("python", "python3"):
        if len(tokens) < 3 or tokens[1] != "-m" or tokens[2] not in _SAFE_PYTHON_MODULES:
            return "python allowed only as 'python -m pytest|unittest'"
    for tok in tokens[1:]:
        if tok.startswith("/") or tok.startswith("~") or ".." in Path(tok).parts:
            return f"non-workspace-relative path {tok!r}"
    return None


def auto_admissible(spec: dict) -> str | None:
    """None when the whole spec qualifies for auto-admission, else the reason
    it falls back to human review. Assumes _validate_spec already passed."""
    if not (settings.canary_enabled and settings.canary_auto_admit):
        return "auto-admission disabled"
    for g in spec.get("gates") or []:
        reason = is_gate_command_safe(str(g.get("command") or ""))
        if reason:
            return f"gate '{g.get('name')}': {reason}"
        for wp in g.get("watch_paths") or []:
            if str(wp).startswith("/") or ".." in Path(str(wp)).parts:
                return f"gate '{g.get('name')}': non-relative watch_path {wp!r}"
    if str(spec.get("model") or "").strip():
        return "model overrides need human review"
    try:
        if int(spec.get("timeout") or 0) > _AUTO_TIMEOUT_CAP_S:
            return f"timeout above the {_AUTO_TIMEOUT_CAP_S}s auto cap"
    except (TypeError, ValueError):
        return "non-integer timeout"
    try:
        from core.canary.parser import scan_canaries

        if len(scan_canaries()) >= settings.canary_max_suite:
            return f"suite at canary_max_suite ({settings.canary_max_suite})"
    except Exception as e:
        return f"suite scan failed: {e}"
    return None


def queue_canary_proposals(proposals: list, producer: str, session_id: str = "") -> int:
    """Admit or store canary proposals. Returns count accepted (both paths).

    Auto-admissible specs materialize immediately (tagged flaky+vetting — see
    materialize_canary) with a vetting run enqueued; everything else keeps
    the human-review path.
    """
    stored = 0
    for p in proposals or []:
        if not isinstance(p, dict):
            continue
        err = _validate_spec(p)
        if err:
            logger.info("canary proposal rejected (%s): %s", producer, err)
            continue

        # Holdout rule (W5): never re-admit a holdout task under a new name.
        # Refused outright — not queued for review — because the reviewable
        # artifact would itself carry the answer.
        twin = resembles_holdout(p)
        if twin:
            logger.info("canary proposal rejected (%s): resembles holdout canary '%s'", producer, twin)
            continue

        fallback_reason = auto_admissible(p)
        if fallback_reason is None:
            name, mat_err = materialize_canary(p, vetting=True)
            if name is not None:
                stored += 1
                try:
                    from core.extensions.scheduling import enqueue_manual_canary

                    enqueue_manual_canary(name)
                except Exception as e:
                    logger.warning("Vetting run enqueue failed for auto-admitted '%s': %s", name, e)
                db.add_notification(
                    title=f"Canary auto-admitted: {name}",
                    body=(
                        f"Proposed by {producer}, gate commands allowlist-proven. Runs as "
                        f"flaky (informs, never trips) until {settings.canary_vetting_runs} "
                        f"consistent runs promote it. {str(p.get('rationale') or '')[:200]}"
                    ),
                    urgency="normal",
                )
                logger.info("Canary '%s' auto-admitted (producer=%s)", name, producer)
                continue
            fallback_reason = f"materialization failed: {mat_err}"

        evidence = [f"session:{session_id}"] if session_id else []
        db.adaptive_add_proposal(
            producer="canary_propose",
            payload_json=json.dumps({"canary": p}),
            evidence_json=json.dumps(evidence),
            rationale=f"[new canary '{p['name']}'] {str(p.get('rationale') or '')[:400]} "
            f"(proposed by {producer}; approving writes data/canaries/{p['name']}/CANARY.md "
            f"and queues a vetting run; not auto-admitted: {fallback_reason})",
        )
        stored += 1
    return stored


def _validate_spec(p: dict) -> str | None:
    name = str(p.get("name") or "").strip()
    if not _NAME_RE.match(name):
        return f"invalid name {name!r} (kebab-case, 2-49 chars)"
    if not str(p.get("prompt") or "").strip():
        return "prompt is required"
    gates = p.get("gates")
    if not isinstance(gates, list) or not gates:
        return "at least one gate is required"
    for g in gates:
        if not isinstance(g, dict) or not g.get("name") or not g.get("command"):
            return "each gate needs name and command"
    files = p.get("files") or {}
    if not isinstance(files, dict):
        return "files must be a mapping"
    for rel in files:
        if Path(rel).is_absolute() or ".." in Path(rel).parts:
            return f"files key {rel!r} must be workspace-relative"
    return None


def write_canary_md(name: str, text: str, base: Path | None = None, overwrite: bool = False) -> tuple[str | None, str]:
    """Write raw CANARY.md text as data/canaries/<name>/CANARY.md, validated.

    The shared write path for every canary producer (proposal
    materialization, the CRUD API, skill verify-sync): render into a temp
    dir, round-trip through the real parser, then move/replace — a broken
    file never lands in the suite. Returns (name, "") or (None, error).
    """
    from core.canary.parser import canaries_dir, parse_canary_md

    base = base or canaries_dir()
    target_dir = base / name
    if target_dir.exists() and not overwrite:
        return None, f"canary '{name}' already exists"

    tmp_root = Path(tempfile.mkdtemp(prefix="canary-write-"))
    try:
        tmp_dir = tmp_root / name
        tmp_dir.mkdir()
        tmp_md = tmp_dir / "CANARY.md"
        tmp_md.write_text(text, encoding="utf-8")
        parsed = parse_canary_md(tmp_md)  # raises CanaryParseError on any invariant break
        if parsed.name != name:
            return None, f"frontmatter name '{parsed.name}' must match directory name '{name}'"
        base.mkdir(parents=True, exist_ok=True)
        if target_dir.exists():
            (target_dir / "CANARY.md").write_text(text, encoding="utf-8")
        else:
            shutil.move(str(tmp_dir), str(target_dir))
    except Exception as e:
        return None, f"write failed: {e}"
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)
    return name, ""


def materialize_canary(spec: dict, base: Path | None = None, vetting: bool = False) -> tuple[str | None, str]:
    """Write an approved proposal as data/canaries/<name>/CANARY.md.

    Validated round-trip: render → parse_canary_md on a temp copy → move.
    Returns (name, "") on success or (None, error).

    vetting=True (auto-admission) stamps flaky:true plus auto-admitted/
    vetting tags: the canary informs but cannot trip the tripwire until the
    maintenance sweep promotes it on consistent runs.
    """
    from core.canary.parser import canaries_dir

    err = _validate_spec(spec)
    if err:
        return None, err
    base = base or canaries_dir()
    name = spec["name"]
    target_dir = base / name
    if target_dir.exists():
        return None, f"canary '{name}' already exists"

    # Holdout rule (W5): a proposal-derived edit never lands on a holdout,
    # even one whose directory was moved out from under the suite.
    twin = resembles_holdout(spec, base)
    if twin:
        return None, f"resembles holdout canary '{twin}'"

    tags = [str(t) for t in (spec.get("tags") or ["proposed"])]
    if vetting:
        tags += [t for t in ("auto-admitted", "vetting") if t not in tags]
    fm = {
        "name": name,
        "prompt": spec["prompt"],
        "gates": [
            {
                "name": str(g["name"]),
                "command": str(g["command"]),
                "watch_paths": [str(w) for w in (g.get("watch_paths") or [])],
            }
            for g in spec["gates"]
        ],
        "timeout": int(spec.get("timeout") or 600),
        "tags": tags,
        "flaky": bool(vetting),
        "last_reviewed": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
    }
    files = spec.get("files") or {}
    if files:
        fm["files"] = {str(k): str(v) for k, v in files.items()}
    provenance = (
        "Machine-proposed, AUTO-ADMITTED (gate commands allowlist-proven).\n"
        "Runs as flaky until the maintenance sweep promotes it on "
        f"{settings.canary_vetting_runs} consistent runs."
        if vetting
        else "Machine-proposed, human-approved. Review the gates before trusting\n"
        "this canary's signal; tag `flaky: true` if it proves unstable."
    )
    body = f"{str(spec.get('rationale') or 'Proposed from a real failed turn.').strip()}\n\n{provenance}"
    text = f"---\n{yaml.safe_dump(fm, sort_keys=False, allow_unicode=True)}---\n\n{body}\n"

    got, err = write_canary_md(name, text, base=base)
    if err:
        return None, f"materialization failed: {err}"
    logger.info("Canary '%s' materialized from approved proposal", name)
    return got, ""
