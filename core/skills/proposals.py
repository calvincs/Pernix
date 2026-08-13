"""Pernix — Apply a reviewed skill-improvement proposal to its target SKILL.md.

Proposals are written by reflect and refine when a skill visibly under-performs
(see core/refine.py). Applying one is always an explicit user action — POST
/api/skills/proposals/{id}/apply, or the Apply button on the Skills tab. Nothing
applies a proposal automatically: the review step is the point.

Lived under core/workflows/ until the workflow engine was removed; proposals
target SKILL.md files and never had anything to do with workflows beyond
sharing that module.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from db import models as db

logger = logging.getLogger("pernix.skills.proposals")


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


def apply_proposal(proposal_id: str) -> ApplyResult:
    """Apply a proposal to its target SKILL.md.

    Steps:
      1. Load the proposal row from the DB.
      2. Locate the target skill via the skill registry.
      3. Read SKILL.md, insert/append the proposed_change under the referenced
         section (or append as a new section if the section is missing).
      4. Write the updated SKILL.md back to disk.
      5. Mark the proposal status='applied' in the DB.

    Raises ProposalApplyError on missing proposal, unknown skill, or I/O error.
    Never auto-retries anything — the user re-invokes explicitly.
    """
    proposal = db.get_skill_proposal(proposal_id)
    if not proposal:
        raise ProposalApplyError(f"Proposal '{proposal_id}' not found")

    status = proposal.get("status", "pending")
    if status == "applied":
        raise ProposalApplyError(f"Proposal '{proposal_id}' has already been applied")

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

    body = skill_md.read_text(encoding="utf-8")
    section = (proposal.get("section") or "Notes").strip() or "Notes"
    change = (proposal.get("proposed_change") or "").strip()
    if not change:
        raise ProposalApplyError(f"Proposal '{proposal_id}' has empty proposed_change")

    new_body, existed = _insert_under_section(body, section, change)
    if new_body == body:
        raise ProposalApplyError("No change would be made (empty proposed_change?)")

    # Atomic write: temp file + rename
    tmp = skill_md.with_suffix(".md.tmp")
    try:
        tmp.write_text(new_body, encoding="utf-8")
        tmp.replace(skill_md)
    except OSError as e:
        raise ProposalApplyError(f"Failed to write SKILL.md: {e}") from e
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass

    # Mark applied in DB. "applied" is a new status — ensure the DB helper
    # accepts it (resolve_skill_proposal whitelists a small set).
    try:
        db.resolve_skill_proposal(proposal_id, "applied")
    except ValueError:
        # Older DB helper versions reject "applied" — use a raw update.
        logger.debug("resolve_skill_proposal rejected 'applied' — using raw update")
        from db.models import _now, connect_sessions

        with connect_sessions() as conn:
            conn.execute(
                "UPDATE skill_improvement_proposals SET status=?, resolved_at=? WHERE id=?",
                ("applied", _now(), proposal_id),
            )

    # Reload the skill registry so the edit is visible to subsequent calls
    # without requiring a server restart.
    try:
        reg.rescan() if hasattr(reg, "rescan") else None
    except Exception as e:
        logger.debug("Skill registry rescan failed after apply: %s", e)

    return ApplyResult(
        proposal_id=proposal_id,
        skill_name=skill_name,
        skill_md_path=str(skill_md),
        section=section,
        section_existed=existed,
        bytes_before=len(body),
        bytes_after=len(new_body),
    )
