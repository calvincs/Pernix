"""Pernix — skill-change detection + verify-block sync (the skills⇄canary bridge).

Skills are the one machine-editable surface that had no measurement at all:
no content hash, no change event, no behavioral test — only a syntax
pre-flight. This module closes both gaps from the maintenance sweep (idle
by construction, and a watermark scan catches every mutation path including
hand edits — the same reason `skill_reqs_hash:` lives there):

  detect — sha256 over each SKILL.md, watermarked in snooze_state under
           `skill_hash:{name}`. A changed skill triggers ONE targeted sweep
           of every canary covering `skill:{name}`. First sight only sets
           the watermark — a fresh deploy must not stampede the suite.
  sync   — a skill may embed its own behavioral test as a `verify:` block
           (prompt, gates, files?, timeout?) in SKILL.md frontmatter. It is
           materialized as the MANAGED canary `skill--{name}` with
           `covers: [skill:{name}]`, resynced whenever the skill changes,
           and retired when the block (or the skill) goes away. The test
           lives next to what it tests; nothing extra to manage.

SECURITY: verify-gate commands execute on the HOST (core/gates.py jails the
cwd, not the command), and SKILL.md is machine-editable — update_skill, the
API, proposal applies. So every gate must pass the same allowlist proof
that guards canary auto-admission (propose.is_gate_command_safe). A skill
whose verify gates fail the proof gets a notification and NO canary; the
canary session's own tool allowlist (runner.CANARY_TOOL_ALLOWLIST) fences
the agent side of the run.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone
from pathlib import Path

import yaml

from config import settings
from db import models as db

logger = logging.getLogger("pernix.canary")

VERIFY_PREFIX = "skill--"
VERIFY_TAG = "skill-verify"
# _NAME_RE allows 2-49 chars; leave room for the prefix, truncate + hash the rest.
_MAX_SKILL_CHARS = 49 - len(VERIFY_PREFIX)


def _skills_dir() -> Path:
    return Path(settings.skills_dir)


def verify_canary_name(skill_name: str) -> str:
    if len(skill_name) <= _MAX_SKILL_CHARS:
        return f"{VERIFY_PREFIX}{skill_name}"
    digest = hashlib.sha256(skill_name.encode("utf-8")).hexdigest()[:6]
    return f"{VERIFY_PREFIX}{skill_name[: _MAX_SKILL_CHARS - 7]}-{digest}"


def _skill_files(base: Path) -> dict[str, Path]:
    if not base.is_dir():
        return {}
    out: dict[str, Path] = {}
    for d in sorted(base.iterdir()):
        md = d / "SKILL.md"
        if d.is_dir() and md.is_file():
            out[d.name] = md
    return out


def _parse_verify_block(md: Path) -> tuple[dict | None, str]:
    """(verify_block, error). (None, "") when the skill has no verify block."""
    from core.canary.parser import CanaryParseError
    from core.skills.parser import parse_frontmatter_md

    try:
        fm, _body = parse_frontmatter_md(md, error_cls=CanaryParseError)
    except Exception as e:
        return None, f"unparseable SKILL.md: {e}"
    v = fm.get("verify")
    if v is None:
        return None, ""
    if not isinstance(v, dict):
        return None, "verify: must be a mapping (prompt, gates, files?, timeout?)"
    if not str(v.get("prompt") or "").strip():
        return None, "verify: needs a prompt"
    gates = v.get("gates")
    if not isinstance(gates, list) or not gates:
        return None, "verify: needs a non-empty gates list"
    for g in gates:
        if not isinstance(g, dict) or not g.get("name") or not g.get("command"):
            return None, "verify: each gate needs name and command"
    return v, ""


def _render_verify_canary(skill_name: str, v: dict, reviewed: str) -> str:
    from core.canary.parser import DEFAULT_TIMEOUT_S

    fm: dict = {
        "name": verify_canary_name(skill_name),
        "prompt": str(v["prompt"]),
        "gates": [
            {
                "name": str(g["name"]),
                "command": str(g["command"]),
                "watch_paths": [str(w) for w in (g.get("watch_paths") or [])],
            }
            for g in v["gates"]
        ],
        "timeout": int(v.get("timeout") or DEFAULT_TIMEOUT_S),
        "tags": [VERIFY_TAG],
        "covers": [f"skill:{skill_name}"],
        "last_reviewed": reviewed,
    }
    files = v.get("files") or {}
    if isinstance(files, dict) and files:
        fm["files"] = {str(k): str(v_) for k, v_ in files.items()}
    body = (
        f"MANAGED by skill verify-sync — edit the `verify:` block in "
        f"data/skills/{skill_name}/SKILL.md instead; changes here are "
        "overwritten on the next sync. Retiring this canary by hand only "
        "sticks if the verify block is removed too."
    )
    return f"---\n{yaml.safe_dump(fm, sort_keys=False, allow_unicode=True)}---\n\n{body}\n"


def _notify_unsafe_once(skill_name: str, digest: str, problem: str) -> None:
    """One notification per (skill, content) — not one per nightly sweep."""
    key = f"canary_verify_unsafe:{skill_name}"
    if db.get_snooze_state(key) == digest:
        return
    try:
        db.add_notification(
            title=f"Skill verify block not admitted: {skill_name}",
            body=(
                f"{problem}. Verify-gate commands run on the host, so they must "
                "pass the same allowlist proof as canary auto-admission "
                "(python -m pytest/unittest and a short list of read-only "
                "binaries; no shell metacharacters, no absolute paths). Fix the "
                "verify: block in the skill, or create the canary by hand via "
                "the Canary tab, where you are the authority."
            ),
            urgency="normal",
        )
        db.set_snooze_state(key, digest)
    except Exception as e:
        logger.warning("Verify-unsafe notification failed for '%s': %s", skill_name, e)


def _sync_verify_canary(skill_name: str, md: Path, digest: str, canaries_base: Path, stats: dict) -> None:
    from core.canary.parser import load_canary
    from core.canary.propose import is_gate_command_safe, write_canary_md

    v, err = _parse_verify_block(md)
    cname = verify_canary_name(skill_name)
    existing = load_canary(cname, base=canaries_base)

    if v is None:
        if err:
            stats["verify_unsafe"].append(skill_name)
            _notify_unsafe_once(skill_name, digest, err)
        elif existing is not None and VERIFY_TAG in existing.tags:
            # Verify block removed → the managed canary goes with it. Only
            # the managed one: a hand-authored canary that happens to share
            # the name is not this module's to retire.
            _retire(existing, canaries_base, f"verify block removed from skill '{skill_name}'", stats)
        return

    unsafe = [
        f"{g.get('name')}: {reason}"
        for g in v["gates"]
        if (reason := is_gate_command_safe(str(g.get("command") or "")))
    ]
    if unsafe:
        stats["verify_unsafe"].append(skill_name)
        _notify_unsafe_once(skill_name, digest, "; ".join(unsafe))
        return

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    # Compare with the EXISTING review date first: last_reviewed only
    # restamps on a real content change, otherwise every first sweep of a
    # new day would rewrite the file just to move the date.
    keep_date = (
        existing.last_reviewed
        if (existing is not None and VERIFY_TAG in existing.tags and existing.last_reviewed)
        else today
    )
    if existing is not None and existing.path is not None:
        try:
            if existing.path.read_text(encoding="utf-8") == _render_verify_canary(skill_name, v, keep_date):
                return  # already in sync
        except OSError:
            pass
    text = _render_verify_canary(skill_name, v, today)
    got, werr = write_canary_md(cname, text, base=canaries_base, overwrite=True)
    if werr:
        stats["verify_unsafe"].append(skill_name)
        _notify_unsafe_once(skill_name, digest, f"verify canary failed validation: {werr}")
        return
    stats["verify_synced"].append(cname)
    logger.info("Skill verify canary synced: %s (skill '%s')", cname, skill_name)


def _retire(canary_def, canaries_base: Path, reason: str, stats: dict) -> None:
    from core.canary.maintain import retire_canary

    if retire_canary(canary_def, canaries_base, reason=reason, by="skill-verify"):
        stats["verify_retired"].append(canary_def.name)


def sync_and_detect(base: Path | None = None, canaries_base: Path | None = None, is_cancelled=lambda: False) -> dict:
    """One watermark + verify-sync pass. Mechanical, bounded, never raises."""
    from core.canary.parser import canaries_dir, scan_canaries

    stats: dict = {"skills_changed": [], "verify_synced": [], "verify_retired": [], "verify_unsafe": []}
    base = base or _skills_dir()
    canaries_base = canaries_base or canaries_dir()
    skills = _skill_files(base)

    for name, md in skills.items():
        if is_cancelled():
            return stats
        try:
            text = md.read_text(encoding="utf-8")
        except OSError as e:
            logger.warning("Skill watermark read failed for '%s': %s", name, e)
            continue
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        key = f"skill_hash:{name}"
        prev = db.get_snooze_state(key)
        # First sight (no watermark) is NOT a change — a fresh deploy must
        # not read as "every skill just changed". The sync itself runs every
        # pass regardless: it is idempotent (text-compare short-circuit) and
        # self-heals a managed canary that went missing.
        if prev and prev != digest:
            stats["skills_changed"].append(name)
        try:
            _sync_verify_canary(name, md, digest, canaries_base, stats)
        except Exception as e:
            logger.warning("Skill verify sync failed for '%s': %s", name, e)
        db.set_snooze_state(key, digest)

    # Orphaned managed canaries: their skill is gone entirely.
    if not is_cancelled():
        for c in scan_canaries(canaries_base):
            if VERIFY_TAG not in c.tags or not c.name.startswith(VERIFY_PREFIX):
                continue
            covered = {s.split(":", 1)[1] for s in c.covers if s.startswith("skill:")}
            if covered and not (covered & set(skills)):
                try:
                    _retire(c, canaries_base, f"skill(s) {', '.join(sorted(covered))} no longer exist", stats)
                except Exception as e:
                    logger.warning("Orphan verify-canary retirement failed for '%s': %s", c.name, e)

    # One targeted sweep for everything that changed — per-name enqueues at
    # the same instant would race the skip-not-queue sweep lock.
    if stats["skills_changed"] and not is_cancelled():
        try:
            from core.extensions.scheduling import enqueue_targeted_sweep

            covered_names: list[str] = []
            wanted = {f"skill:{n}" for n in stats["skills_changed"]}
            for c in scan_canaries(canaries_base):
                if wanted & set(c.covers):
                    covered_names.append(c.name)
            if covered_names and enqueue_targeted_sweep(covered_names, reason="skill-change"):
                logger.info(
                    "Skill change (%s) → targeted canary sweep: %s",
                    ", ".join(stats["skills_changed"]),
                    ", ".join(covered_names),
                )
        except Exception as e:
            logger.warning("Skill-change sweep enqueue failed: %s", e)

    return stats
