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

ROLLBACK (trust-loop hardening W5): a skill's own verify canary failing
shortly after that skill was edited by an auto-applied proposal is the
closest thing to a measured regression this surface has. With
``skill_proposal_auto_rollback`` on, that pairing restores the backup taken
at apply time and marks the proposal 'rolled_back'. Off by default — the
signal earns trust the way adaptive_auto_rollback does.

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
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

from config import settings
from db import models as db

logger = logging.getLogger("pernix.canary")

VERIFY_PREFIX = "skill--"
VERIFY_TAG = "skill-verify"
# How long after an auto-apply a verify failure still implicates it. Long
# enough that a nightly heartbeat gets several chances to notice, short
# enough that an unrelated failure weeks later is not blamed on it.
ROLLBACK_WINDOW_DAYS = 7
# How many recent verify runs the rollback check reads per sweep.
_ROLLBACK_RUN_SCAN = 50
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


def _render_verify_canary(skill_name: str, v: dict, reviewed: str, *, parked: bool = False, flaky: bool = False) -> str:
    """The canonical file for a verify canary.

    `parked` and `flaky` belong to maintenance, not to the skill: a sync
    that dropped them would un-park the canary, maintenance would park it
    again next cycle, and the two writers would trade the file (and a
    notification) every twenty minutes — the 2026-09-05 storm.
    """
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
    if parked:
        fm["parked"] = True
    if flaky:
        fm["flaky"] = True
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


_MAINTENANCE_LINES = ("last_reviewed:", "parked:", "flaky:")


def _same_verify_content(a: str, b: str) -> bool:
    """Equal once the lines maintenance owns are ignored."""

    def strip(text: str) -> str:
        return "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith(_MAINTENANCE_LINES))

    return strip(a) == strip(b)


def _retired_copy_text(cname: str, canaries_base: Path) -> str | None:
    """The CANARY.md of a retired copy of `cname`, when one is quarantined."""
    from core.canary.maintain import retired_dir

    try:
        path = retired_dir(canaries_base) / cname / "CANARY.md"
        return path.read_text(encoding="utf-8") if path.exists() else None
    except OSError:
        return None


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

    # A verify canary someone retired stays retired while the verify block
    # it was built from is unchanged. Re-materialising it every sweep made
    # retirement from the Canary tab impossible to keep.
    if existing is None:
        retired_text = _retired_copy_text(cname, canaries_base)
        if retired_text is not None and _same_verify_content(retired_text, _render_verify_canary(skill_name, v, today)):
            return

    # Compare with the EXISTING review date first: last_reviewed only
    # restamps on a real content change, otherwise every first sweep of a
    # new day would rewrite the file just to move the date. Maintenance
    # flags are carried over for the same reason.
    managed = existing is not None and VERIFY_TAG in existing.tags
    keep_date = existing.last_reviewed if (managed and existing.last_reviewed) else today
    flags = {"parked": bool(getattr(existing, "parked", False)), "flaky": bool(getattr(existing, "flaky", False))}
    if existing is not None and existing.path is not None:
        try:
            if existing.path.read_text(encoding="utf-8") == _render_verify_canary(skill_name, v, keep_date, **flags):
                return  # already in sync
        except OSError:
            pass
    text = _render_verify_canary(skill_name, v, today, **flags)
    got, werr = write_canary_md(cname, text, base=canaries_base, overwrite=True)
    if werr:
        stats["verify_unsafe"].append(skill_name)
        _notify_unsafe_once(skill_name, digest, f"verify canary failed validation: {werr}")
        return
    stats["verify_synced"].append(cname)
    logger.info("Skill verify canary synced: %s (skill '%s')", cname, skill_name)


def _covered_skill(canary_def) -> str:
    """The skill a managed verify canary tests, from its `covers:` list."""
    for ref in canary_def.covers:
        if ref.startswith("skill:"):
            return ref.split(":", 1)[1]
    return ""


def _recent_verify_failures(canaries_base: Path) -> dict[str, str]:
    """{skill_name: created_at} for verify canaries whose LATEST run failed.

    Only the latest run counts: a skill that failed and then went green again
    has already been fixed, and rolling it back would undo the fix. A
    contaminated run is not evidence of anything (W5), and only an honest
    gate_fail implicates the edit — a timeout or a harness error is
    suite health.
    """
    from core.canary.parser import scan_canaries

    out: dict[str, str] = {}
    for c in scan_canaries(canaries_base):
        if VERIFY_TAG not in c.tags:
            continue
        skill = _covered_skill(c)
        if not skill:
            continue
        runs = [
            r for r in db.list_canary_runs(task=c.name, limit=_ROLLBACK_RUN_SCAN) if r.get("outcome") != "contaminated"
        ]
        if runs and runs[0].get("outcome") == "gate_fail":
            out[skill] = runs[0].get("created_at") or ""
    return out


def check_verify_rollbacks(canaries_base: Path, stats: dict) -> None:
    """Roll back an auto-applied skill proposal its own verify canary failed.

    Flag-gated (``skill_proposal_auto_rollback``, default off) and bounded to
    proposals auto-applied within ROLLBACK_WINDOW_DAYS of the failing run.
    Idempotent by construction: a rolled-back proposal leaves the
    'auto_applied' status, so it is never selected twice.
    """
    if not settings.skill_proposal_auto_rollback:
        return
    try:
        failures = _recent_verify_failures(canaries_base)
    except Exception as e:
        logger.warning("Verify-failure scan failed: %s", e)
        return
    if not failures:
        return

    from core.skills.proposals import ProposalApplyError, restore_skill_backup

    for skill, failed_at in sorted(failures.items()):
        try:
            when = datetime.fromisoformat(failed_at) if failed_at else datetime.now(timezone.utc)
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
        except ValueError:
            when = datetime.now(timezone.utc)
        floor = (when - timedelta(days=ROLLBACK_WINDOW_DAYS)).isoformat()

        candidates = [
            p
            for p in db.list_skill_proposals(skill_name=skill, status="auto_applied", limit=50)
            if floor <= (p.get("resolved_at") or "") <= (failed_at or when.isoformat())
        ]
        if not candidates:
            continue
        newest = max(candidates, key=lambda p: p.get("resolved_at") or "")
        pid = str(newest.get("id"))
        try:
            result = restore_skill_backup(pid, actor="skill-verify")
        except ProposalApplyError as e:
            logger.warning("Auto-rollback of skill proposal %s failed: %s", pid, e)
            stats.setdefault("verify_rollback_failed", []).append(pid)
            continue
        stats.setdefault("verify_rolled_back", []).append(pid)
        logger.info("Skill '%s' auto-rolled-back after its verify canary failed (proposal %s)", skill, pid)
        try:
            db.add_notification(
                title=f"Skill auto-rolled-back: {skill}",
                body=(
                    f"The verify canary for '{skill}' gate-failed within "
                    f"{ROLLBACK_WINDOW_DAYS} days of proposal {pid} being auto-applied, so "
                    f"{result['skill_md_path']} was restored from {Path(result['backup']).name}. "
                    "The state it replaced was backed up first. Turn this off with "
                    "skill_proposal_auto_rollback."
                ),
                urgency="high",
            )
        except Exception as e:
            logger.debug("Auto-rollback notification failed for '%s': %s", skill, e)


def _retire(canary_def, canaries_base: Path, reason: str, stats: dict) -> None:
    from core.canary.maintain import retire_canary

    if retire_canary(canary_def, canaries_base, reason=reason, by="skill-verify"):
        stats["verify_retired"].append(canary_def.name)


def sync_and_detect(base: Path | None = None, canaries_base: Path | None = None, is_cancelled=lambda: False) -> dict:
    """One watermark + verify-sync pass. Mechanical, bounded, never raises."""
    from core.canary.parser import canaries_dir, scan_canaries

    stats: dict = {
        "skills_changed": [],
        "verify_synced": [],
        "verify_retired": [],
        "verify_unsafe": [],
        "verify_rolled_back": [],
    }
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

    # Undo before re-measuring: a verify canary that is currently red for a
    # skill an auto-applied proposal just edited rolls that proposal back
    # (flag-gated). Runs after the sync so a freshly (re)materialized verify
    # canary is on disk to be read.
    if not is_cancelled():
        try:
            check_verify_rollbacks(canaries_base, stats)
        except Exception as e:
            logger.warning("Verify-failure rollback check failed: %s", e)

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
