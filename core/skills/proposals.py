"""Pernix — Apply a skill-improvement proposal to its target SKILL.md.

Proposals are written by reflect and refine when a skill visibly under-performs
(see core/refine.py). Two paths apply one:

  1. Explicit user action — POST /api/skills/proposals/{id}/apply, or the
     Apply button on the Skills tab (status 'applied').
  2. Auto-apply (on by default, ``skill_proposal_auto_apply``) —
     ``auto_apply_ripe_proposals`` (snooze Activity 13b) applies pending
     proposals older than ``skill_proposal_auto_apply_after_hours`` that pass
     ``_validate_for_auto_apply``, with a timestamped backup under
     data/skill_backups/ (status 'auto_applied'). Rollback is manual:
     ``restore_skill_backup`` or POST /api/skills/proposals/{id}/rollback.

A pending proposal nobody decides on within 30 days is archived by snooze
(``archive_stale_skill_proposals``, Activity 13b'').

Lived under core/workflows/ until the workflow engine was removed; proposals
target SKILL.md files and never had anything to do with workflows beyond
sharing that module.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from functools import wraps
from pathlib import Path

from config import settings
from core.tools.atomic import atomic_write, content_revision, file_revision, target_lock
from db import models as db

logger = logging.getLogger("pernix.skills.proposals")

# Machine-validation bound: a paste-ready SKILL.md addition is a paragraph
# or a short section, not a rewrite. Anything bigger than this needs human
# eyes regardless of age.
AUTO_APPLY_MAX_CHANGE_CHARS = 1500

# The 3.1 sweep applied 52 proposals in a month on the reference box and did
# damage the checks below now refuse (2026-10 surface prune review):
# youtube-whisper took 20 edits in two weeks and doubled; refine pasted
# instructions meant for a human editor ("Add a note at the top of Usage:
# ...") verbatim; a near-copy of an existing heading was appended as a new
# section; and 9 of 12 edited skills grew past the 5,000-char injection cap,
# so the added text never reached the prompt. A refused proposal is not
# lost: it waits in the Skills tab, and archives after 30 days.
AUTO_APPLY_MAX_PER_SKILL_30D = 3
SKILL_INJECT_MAX_CHARS = 5000  # mirrors core.scout.runner.SKILL_INJECT_MAX_CHARS
_HEADING_SIMILARITY = 0.6

# Text addressed to an editor rather than to the agent reading the skill:
# an edit verb followed, on the same line, by the thing to edit.
_EDITOR_INSTRUCTION_RE = re.compile(
    r"^\s*(?:[-*]\s*)?(?:please\s+)?"
    r"(?:add|insert|append|update|change|replace|remove|include|put|move|rewrite|expand|mention|document|create)\b"
    r"[^\n]{0,120}?\b(?:section|subsection|note|rule|line|step|heading|paragraph|gate|bullet|example|warning)s?\b",
    re.IGNORECASE,
)
_EDITOR_PLACEMENT_RE = re.compile(
    r"\b(?:at the (?:top|end|start|bottom) of|under the [^\n]{1,40}? (?:section|heading)|"
    r"to the [^\n]{1,40}? (?:section|heading))\b",
    re.IGNORECASE,
)
_FRONTMATTER_RE = re.compile(r"\A---\n.*?\n---\n", re.DOTALL)


def _reads_as_editor_instruction(change: str) -> bool:
    first = next((ln for ln in change.splitlines() if ln.strip()), "")
    return bool(_EDITOR_INSTRUCTION_RE.search(first) or _EDITOR_PLACEMENT_RE.search(change))


_QUOTES = "\"'“”‘’"


def unwrap_editor_instruction(change: str) -> str:
    """Return the skill text inside an instruction addressed to an editor.

    Refine used to write proposals as notes to a human ("Add a note under
    Usage: 'If python is missing, use .venv/bin/python.'"); the 3.1 sweep
    pasted those verbatim. Two shapes carry real text and are unwrapped:
    a first line that is only the instruction, followed by the text; and an
    instruction, a colon, then the text on the same line (outer quotes
    dropped). Anything else is returned unchanged — the caller decides.
    """
    text = change.strip()
    lines = text.splitlines()
    first_idx = next((i for i, ln in enumerate(lines) if ln.strip()), None)
    if first_idx is None or not _EDITOR_INSTRUCTION_RE.search(lines[first_idx]):
        return text
    first = lines[first_idx].strip()
    rest = "\n".join(lines[first_idx + 1 :]).strip()
    if first.endswith(":") and rest:
        return rest
    head, sep, tail = first.partition(": ")
    if sep and len(tail.strip()) >= 20:
        inner = tail.strip()
        if len(inner) >= 2 and inner[0] in _QUOTES and inner[-1] in _QUOTES:
            inner = inner[1:-1].strip()
        return (inner + ("\n" + rest if rest else "")).strip()
    return text


def _headings(body: str) -> list[str]:
    out = []
    for line in body.splitlines():
        s = line.lstrip()
        if s.startswith("#"):
            out.append(s.lstrip("#").strip())
    return out


def _near_duplicate_heading(body: str, section: str) -> str | None:
    """An existing heading the proposal's section nearly copies, when the
    section itself is not there (apply would append a second, near-identical
    heading at the end of the file)."""
    import difflib

    want = " ".join(section.lower().split())
    if not want:
        return None
    existing = _headings(body)
    if any(" ".join(h.lower().split()) == want for h in existing):
        return None
    for h in existing:
        norm = " ".join(h.lower().split())
        if not norm:
            continue
        if norm.startswith(want) or want.startswith(norm):
            return h
        if difflib.SequenceMatcher(None, norm, want).ratio() >= _HEADING_SIMILARITY:
            return h
    return None


def _body_chars(text: str) -> int:
    return len(_FRONTMATTER_RE.sub("", text, count=1))


BACKUP_STAMP_FORMAT = "%Y%m%d-%H%M%S"
ROLLBACK_FROM_STATUSES = ("applied", "auto_applied", "applying")


class ProposalApplyError(Exception):
    """Raised when a proposal cannot be applied (not found, unknown skill, etc)."""


@dataclass
class ApplyResult:
    proposal_id: str
    skill_name: str
    skill_md_path: str
    section: str
    section_existed: bool
    bytes_before: int
    bytes_after: int


def _find_section_bounds(body: str, section_name: str) -> tuple[int, int] | None:
    """Return (start_of_section_body, end_of_section_body) char offsets in body,
    or None if the section header is not found.

    "Section" means a Markdown heading line whose title case-insensitively matches
    `section_name`, of any heading level (##, ###, etc). The section body extends
    from the line after the header to the start of the next heading at the same
    level or a higher-level heading (or end of file).
    """
    lines = body.splitlines(keepends=True)
    target = section_name.strip().lower()

    header_idx = -1
    header_level = 0
    cursor = 0
    offsets: list[int] = []  # start offsets of each line
    for line in lines:
        offsets.append(cursor)
        cursor += len(line)

    for i, line in enumerate(lines):
        stripped = line.lstrip()
        if not stripped.startswith("#"):
            continue
        # Count leading # chars
        level = 0
        for ch in stripped:
            if ch == "#":
                level += 1
            else:
                break
        title = stripped[level:].strip().lower()
        if title == target:
            header_idx = i
            header_level = level
            break

    if header_idx < 0:
        return None

    # Find end: next heading at same or higher level
    end_idx = len(lines)
    for j in range(header_idx + 1, len(lines)):
        stripped = lines[j].lstrip()
        if not stripped.startswith("#"):
            continue
        level = 0
        for ch in stripped:
            if ch == "#":
                level += 1
            else:
                break
        if level <= header_level:
            end_idx = j
            break

    # Section body starts at the line after the header
    start_offset = offsets[header_idx + 1] if header_idx + 1 < len(lines) else len(body)
    end_offset = offsets[end_idx] if end_idx < len(lines) else len(body)
    return start_offset, end_offset


def _insert_under_section(body: str, section_name: str, change: str) -> tuple[str, bool]:
    """Insert `change` under the given section. If the section doesn't exist,
    append a new section at the end of the body. Returns (new_body, section_existed).
    """
    bounds = _find_section_bounds(body, section_name)
    block = change.strip()
    if not block:
        return body, False

    if bounds is None:
        # Append a new section at the end of the body
        separator = "" if body.endswith("\n\n") else ("\n" if body.endswith("\n") else "\n\n")
        new_body = body + separator + f"## {section_name}\n\n{block}\n"
        return new_body, False

    start, end = bounds
    section_body = body[start:end]
    # Preserve existing content; add the change as a new paragraph at the end
    # of the section body. Ensure blank-line separation before and after so
    # the next heading isn't visually glued to the inserted text.
    sep_before = (
        "" if section_body.endswith("\n\n") or not section_body else ("\n" if section_body.endswith("\n") else "\n\n")
    )
    new_section = section_body + sep_before + block + "\n\n"
    new_body = body[:start] + new_section + body[end:]
    return new_body, True


def _backup_skill_md(skill_name: str, skill_md: Path, kind: str = "") -> Path | None:
    """Copy SKILL.md to data/skill_backups/<skill>/SKILL.md.<UTC ts> before a
    write mutates it. Returns the backup path, or None on failure. Callers must refuse
    to modify the skill if its recovery copy cannot be created.

    The stamp is second-granularity, so two writes in the same second used to
    silently overwrite each other — which meant a rollback's own safety copy
    destroyed the very backup it was about to restore. Collisions now get a
    `-N` suffix, and `kind` marks a copy that is NOT an apply-time backup
    (`.pre-rollback`); recovery uses the exact apply-time filename journaled in SQLite.
    """
    try:
        backup_root = _backup_dir(skill_name)
        backup_root.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime(BACKUP_STAMP_FORMAT)
        tail = f".{kind}" if kind else ""
        dest = backup_root / f"SKILL.md.{ts}{tail}"
        n = 1
        while dest.exists():
            dest = backup_root / f"SKILL.md.{ts}-{n}{tail}"
            n += 1
        atomic_write(dest, skill_md.read_bytes().decode("utf-8"))
        return dest
    except OSError as e:
        logger.warning("Skill backup failed for '%s': %s", skill_name, e)
        return None


def _backup_dir(skill_name: str) -> Path:
    return Path(settings.skills_dir).parent / "skill_backups" / skill_name


def _serialized_skill_change(fn):
    @wraps(fn)
    def wrapped(proposal_id, *args, **kwargs):
        from core.skills.registry import get_skill_registry

        proposal = db.get_skill_proposal(proposal_id)
        if not proposal:
            raise ProposalApplyError(f"Proposal '{proposal_id}' not found")
        skill = get_skill_registry().get(proposal.get("skill_name") or "")
        if skill is None:
            raise ProposalApplyError(f"Skill '{proposal.get('skill_name')}' not found in registry")
        with target_lock(skill.path / "SKILL.md"):
            return fn(proposal_id, *args, **kwargs)

    return wrapped


def _rescan_after_change(reg) -> None:
    try:
        reg.rescan(Path(settings.skills_dir))
    except Exception as exc:
        # The mutation is already durably completed. A scan failure must not
        # invite the caller to repeat it; a later list/scan refreshes metadata.
        logger.warning("Skill registry reload failed after a completed change: %s", exc)


def _auto_apply_blocked() -> bool:
    try:
        from sessions.manager import get_manager

        return get_manager().has_active_work(strict=True)
    except Exception:
        # An unavailable idle check cannot authorize an unattended mutation.
        return True


@_serialized_skill_change
def restore_skill_backup(proposal_id: str, actor: str = "user") -> dict:
    """Undo one applied skill proposal by restoring its pre-apply backup.

    The sixth principle of the hardening plan is that every channel has an
    undo. Applying a proposal takes a timestamped backup; a README sentence
    telling a human to copy the file back by hand is not an undo, it is a hope.

    Restores the backup taken at apply time (not merely the newest), marks
    the proposal 'rolled_back', and journals both sides: a safety copy of the
    current SKILL.md lands in the same directory first, so the rollback is
    itself reversible.

    Raises ProposalApplyError when the proposal, skill, or backup is missing.
    """
    proposal = db.get_skill_proposal(proposal_id)
    if not proposal:
        raise ProposalApplyError(f"Proposal '{proposal_id}' not found")

    status = proposal.get("status", "pending")
    if status == "rolled_back":
        raise ProposalApplyError(f"Proposal '{proposal_id}' has already been rolled back")
    if status not in ROLLBACK_FROM_STATUSES:
        raise ProposalApplyError(
            f"Proposal '{proposal_id}' is '{status}' — only an applied proposal can be rolled back"
        )

    skill_name = proposal.get("skill_name") or ""
    if not skill_name:
        raise ProposalApplyError(f"Proposal '{proposal_id}' has no skill_name")

    from core.skills.registry import get_skill_registry

    reg = get_skill_registry()
    skill = reg.get(skill_name) if hasattr(reg, "get") else None
    if skill is None:
        skill = getattr(reg, "_skills", {}).get(skill_name)
    if skill is None:
        raise ProposalApplyError(f"Skill '{skill_name}' not found in registry")

    skill_md = skill.path / "SKILL.md"
    name = proposal.get("backup_name")
    if not name or Path(name).name != name or not proposal.get("after_revision"):
        raise ProposalApplyError("No exact backup record for this legacy proposal; restore its backup manually")
    backup = _backup_dir(skill_name) / name
    if not backup.is_file():
        raise ProposalApplyError(f"No backup for '{skill_name}': {backup}")
    current = skill_md.read_bytes().decode("utf-8") if skill_md.exists() else ""
    restored = backup.read_bytes().decode("utf-8")
    if content_revision(restored) != proposal.get("before_revision"):
        raise ProposalApplyError("Backup content changed; refusing rollback")
    revision = file_revision(skill_md)
    # The before revision also permits completing an interrupted rollback
    # whose file replacement succeeded but whose DB finalization failed.
    allowed = {proposal["after_revision"], proposal["before_revision"]}
    if revision not in allowed:
        raise ProposalApplyError(
            "Skill changed since this proposal was applied; roll back newer edits first or restore manually"
        )
    if _backup_skill_md(skill_name, skill_md, kind="pre-rollback") is None:
        raise ProposalApplyError("Backup failed; rollback cancelled")
    if file_revision(skill_md) != revision:
        raise ProposalApplyError("Skill changed during rollback; no changes made")
    try:
        atomic_write(skill_md, restored)
    except OSError as e:
        raise ProposalApplyError(f"Failed to restore SKILL.md: {e}") from e
    if not db.resolve_skill_proposal(proposal_id, "rolled_back", expected=(status,)):
        raise ProposalApplyError("Proposal changed during rollback; inspect its recovery record")
    _rescan_after_change(reg)

    result = {
        "proposal_id": proposal_id,
        "skill_name": skill_name,
        "skill_md_path": str(skill_md),
        "backup": str(backup),
        "from_status": status,
        "status": "rolled_back",
        "bytes_before": len(current),
        "bytes_after": len(restored),
        "actor": actor,
    }
    logger.info(
        "Skill proposal %s rolled back by %s: restored %s over %s (%d -> %d bytes)",
        proposal_id,
        actor,
        backup.name,
        skill_md,
        len(current),
        len(restored),
    )
    from core import notices

    notices.notify(
        "skills.rolled_back",
        title=f"Skill rolled back: {skill_name}",
        body=(
            f"Proposal {proposal_id} ({status}) was rolled back by {actor}. "
            f"{skill_md.name} restored from {backup.name} "
            f"({len(current)} -> {len(restored)} bytes). The state it replaced was "
            "backed up first, in the same directory."
        ),
        subject=skill_name,
        link={"kind": "tab", "tab": "skills"},
    )
    return result


@_serialized_skill_change
def apply_proposal(proposal_id: str, status_label: str = "applied", unwrap: bool = False) -> ApplyResult:
    """Apply a proposal to its target SKILL.md.

    Steps:
      1. Load the proposal row from the DB.
      2. Locate the target skill via the skill registry.
      3. Back up SKILL.md, then insert/append the proposed_change under the
         referenced section (or append as a new section if missing).
      4. Write the updated SKILL.md back to disk.
      5. Mark the proposal with `status_label` ('applied' for the human
         Apply button, 'auto_applied' for the veto-window sweep).

    Raises ProposalApplyError on missing proposal, unknown skill, or I/O error.
    Never auto-retries anything — the caller re-invokes explicitly.
    """
    proposal = db.get_skill_proposal(proposal_id)
    if not proposal:
        raise ProposalApplyError(f"Proposal '{proposal_id}' not found")

    status = proposal.get("status", "pending")
    allowed = ("pending",) if status_label == "auto_applied" else ("pending", "approved")
    if status not in allowed:
        raise ProposalApplyError(f"Proposal '{proposal_id}' cannot be applied from status '{status}'")
    if status_label not in ("applied", "auto_applied"):
        raise ProposalApplyError("Invalid application status")
    if status_label == "auto_applied":
        if not settings.skill_proposal_auto_apply or _auto_apply_blocked():
            raise ProposalApplyError("Auto-apply deferred while work is active or disabled")
        reason = _validate_for_auto_apply(proposal)
        if reason:
            raise ProposalApplyError(reason)

    skill_name = proposal.get("skill_name") or ""
    if not skill_name:
        raise ProposalApplyError(f"Proposal '{proposal_id}' has no skill_name")

    from core.skills.registry import get_skill_registry

    reg = get_skill_registry()
    skill = reg.get(skill_name) if hasattr(reg, "get") else None
    # get_skill_registry exposes different accessor names across versions;
    # fall back to the internal map if .get isn't present.
    if skill is None:
        skill = getattr(reg, "_skills", {}).get(skill_name)
    if skill is None:
        raise ProposalApplyError(f"Skill '{skill_name}' not found in registry")

    skill_md = skill.path / "SKILL.md"
    if not skill_md.exists():
        raise ProposalApplyError(f"SKILL.md not found for '{skill_name}' at {skill_md}")

    body = skill_md.read_bytes().decode("utf-8")
    section = (proposal.get("section") or "Notes").strip() or "Notes"
    change = (proposal.get("proposed_change") or "").strip()
    if unwrap:
        # Auto-apply writes only the skill text, never the note to an editor
        # around it (see unwrap_editor_instruction).
        change = unwrap_editor_instruction(change)
    if not change:
        raise ProposalApplyError(f"Proposal '{proposal_id}' has empty proposed_change")

    new_body, existed = _insert_under_section(body, section, change)
    if new_body == body:
        raise ProposalApplyError("No change would be made (empty proposed_change?)")

    before_revision = content_revision(body)
    backup = _backup_skill_md(skill_name, skill_md)
    if backup is None:
        raise ProposalApplyError("Backup failed; proposal not applied")
    if content_revision(backup.read_bytes()) != before_revision or file_revision(skill_md) != before_revision:
        raise ProposalApplyError("Skill changed during application; no changes made")
    if status_label == "auto_applied" and (not settings.skill_proposal_auto_apply or _auto_apply_blocked()):
        raise ProposalApplyError("Auto-apply deferred while work is active or disabled")
    if not db.claim_skill_proposal(proposal_id, status, backup.name, before_revision, content_revision(new_body)):
        raise ProposalApplyError("Proposal changed before application; no changes made")
    try:
        atomic_write(skill_md, new_body)
    except OSError as e:
        db.resolve_skill_proposal(proposal_id, status, expected=("applying",))
        raise ProposalApplyError(f"Failed to write SKILL.md: {e}") from e
    # If this commit fails, the durable 'applying' record holds the exact
    # backup and revisions. Rollback can safely recover an interrupted apply.
    if not db.resolve_skill_proposal(proposal_id, status_label, expected=("applying",)):
        raise ProposalApplyError("Could not finalize application; use rollback to recover")
    _rescan_after_change(reg)

    return ApplyResult(
        proposal_id=proposal_id,
        skill_name=skill_name,
        skill_md_path=str(skill_md),
        section=section,
        section_existed=existed,
        bytes_before=len(body),
        bytes_after=len(new_body),
    )


# A suggestion nobody acted on in a month is not going to be acted on; it
# only keeps the review.pending count from ever reaching zero.
STALE_PROPOSAL_DAYS = 30


def archive_stale_skill_proposals(days: int = STALE_PROPOSAL_DAYS) -> list[str]:
    """Archive pending skill proposals older than `days` (status 'archived').

    No LLM, no file writes: the row stays as history and refine's dedupe
    still sees it, so the same change is not proposed again. Returns the
    archived ids.
    """
    if days <= 0:
        return []
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    archived: list[str] = []
    for prop in db.iter_pending_skill_proposals(before=cutoff):
        if (prop.get("created_at") or "") >= cutoff:
            continue
        pid = str(prop.get("id"))
        if db.resolve_skill_proposal(pid, "archived"):
            archived.append(pid)
    if archived:
        logger.info("Skill proposals: archived %d pending past %d days", len(archived), days)
    return archived


def _validate_for_auto_apply(proposal: dict) -> str | None:
    """Machine checks a proposal must pass before the veto-window sweep may
    apply it. Returns None when valid, else a short skip/expire reason.

    Reasons prefixed 'expire:' mean the proposal can never become valid
    (target skill gone) — the sweep archives it. Everything else leaves the
    row pending for a human or a later sweep.
    """
    skill_name = (proposal.get("skill_name") or "").strip()
    if not skill_name:
        return "expire:no skill_name"

    change = (proposal.get("proposed_change") or "").strip()
    if not change:
        return "expire:empty proposed_change"
    if len(change) > AUTO_APPLY_MAX_CHANGE_CHARS:
        return f"skip:change exceeds {AUTO_APPLY_MAX_CHANGE_CHARS} chars (needs human review)"
    if "\x00" in change:
        return "expire:binary content"

    # Refine floors confidence at 0.6 before persisting; enforce the same
    # bar here so nothing below it ever applies unattended, whatever wrote
    # the row.
    try:
        if float(proposal.get("confidence") or 0.0) < 0.6:
            return "skip:confidence below 0.6 (needs human review)"
    except (TypeError, ValueError):
        return "skip:unparseable confidence"

    from core.skills.registry import get_skill_registry

    reg = get_skill_registry()
    skill = reg.get(skill_name) if hasattr(reg, "get") else None
    if skill is None:
        return "expire:skill not in registry"
    try:
        if reg.is_disabled(skill_name):
            return "skip:skill is disabled"
    except Exception:
        pass
    skill_md = skill.path / "SKILL.md"
    if not skill_md.exists():
        return "expire:SKILL.md missing"

    change = unwrap_editor_instruction(change)
    if _reads_as_editor_instruction(change):
        return "skip:reads as an instruction to an editor, not skill text (needs human review)"

    try:
        body = skill_md.read_bytes().decode("utf-8")
    except OSError as e:
        return f"skip:SKILL.md unreadable ({e})"
    section = (proposal.get("section") or "").strip()
    twin = _near_duplicate_heading(body, section)
    if twin is not None:
        return f"skip:section '{section}' nearly duplicates existing heading '{twin}' (needs human review)"
    new_body, _ = _insert_under_section(body, section, change)
    before, after = _body_chars(body), _body_chars(new_body)
    if before <= SKILL_INJECT_MAX_CHARS < after:
        return (
            f"skip:would push the skill past the {SKILL_INJECT_MAX_CHARS}-char prompt limit "
            f"({before} → {after}) (needs human review)"
        )

    month_ago = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    if db.count_auto_applied_skill_proposals_since(month_ago, skill_name=skill_name) >= AUTO_APPLY_MAX_PER_SKILL_30D:
        return f"skip:{AUTO_APPLY_MAX_PER_SKILL_30D} auto-applies to this skill in 30 days (needs human review)"
    return None


def auto_apply_ripe_proposals() -> dict:
    """Apply pending skill proposals that are past the window and pass the
    checks in ``_validate_for_auto_apply``.

    On by default (``skill_proposal_auto_apply``) so the owner does not have
    to click through proposals; a human can still reject anything in the
    Skills tab inside ``skill_proposal_auto_apply_after_hours``. Every apply
    is backed up under data/skill_backups/, day-capped and idle-only. A
    proposal that fails a check waits for a human instead.

    Returns {"applied": [...], "archived": [...], "skipped": n,
    "deferred": n, "summaries": [...]}.
    """
    out: dict = {"applied": [], "archived": [], "skipped": 0, "deferred": 0, "summaries": []}
    if not settings.skill_proposal_auto_apply:
        return out
    window_hours = max(0, settings.skill_proposal_auto_apply_after_hours)

    pending_count = db.count_pending_skill_proposals()
    if not pending_count:
        return out
    if _auto_apply_blocked():
        out["deferred"] = pending_count
        return out
    now = datetime.now(timezone.utc)
    cutoff = (now - timedelta(hours=window_hours)).isoformat()
    ripe = db.iter_pending_skill_proposals(before=cutoff)

    used = db.count_auto_applied_skill_proposals_since((now - timedelta(hours=24)).isoformat())
    budget = max(0, settings.skill_proposal_max_auto_applies_per_day - used)
    if budget <= 0:
        out["deferred"] = pending_count
        logger.info("Skill auto-apply deferred: daily cap reached (%d)", used)
        return out

    for prop in ripe:
        if budget <= 0 or _auto_apply_blocked() or not settings.skill_proposal_auto_apply:
            break
        pid = str(prop.get("id"))
        reason = _validate_for_auto_apply(prop)
        if reason is not None:
            if reason.startswith("expire:"):
                if db.resolve_skill_proposal(pid, "archived"):
                    out["archived"].append(pid)
                logger.info("Skill auto-apply archived %s: %s", pid, reason)
            else:
                out["skipped"] += 1
                logger.info("Skill auto-apply skipped %s: %s", pid, reason)
            continue
        try:
            result = apply_proposal(pid, status_label="auto_applied", unwrap=True)
            out["applied"].append(pid)
            out["summaries"].append(
                f"{result.skill_name} § {result.section}: "
                f"{(prop.get('problem') or '')[:120]} "
                f"(+{result.bytes_after - result.bytes_before}B, confidence "
                f"{float(prop.get('confidence') or 0):.2f})"
            )
            budget -= 1
        except ProposalApplyError as e:
            out["skipped"] += 1
            logger.warning("Skill auto-apply failed for %s: %s", pid, e)

    out["deferred"] = max(0, pending_count - len(out["applied"]) - len(out["archived"]) - out["skipped"])
    if out["applied"]:
        logger.info(
            "Skill proposals: auto-applied %d past the %dh veto window (%s)",
            len(out["applied"]),
            window_hours,
            ", ".join(out["applied"]),
        )
    return out
