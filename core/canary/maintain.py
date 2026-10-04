"""Pernix — canary suite file helpers: frontmatter rewrites, retirement, purge.

The suite used to maintain itself on every idle cycle — promote vetted
canaries, tag flapping ones flaky, park long-green ones, retire exhausted
probes, raise suite-health alerts. On the reference box that machinery
produced notices about a suite nobody was adding to, so it was retired in
3.2. The suite is small and hand-curated now; what stays is what the API
and retention need:

  _rewrite_frontmatter — validated in-place edit (PATCH-style updates such
                         as "reviewed today").
  retire_canary        — move a canary into the `.retired/` quarantine.
  purge_quarantine     — delete retired canaries past
                         canary_purge_after_days (run by snooze retention).
"""

from __future__ import annotations

import json
import logging
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import yaml

from config import settings
from core.canary.parser import CanaryDef, CanaryParseError, canaries_dir, parse_canary_md
from core.skills.parser import parse_frontmatter_md

logger = logging.getLogger("pernix.canary")

_RETIRED_DIRNAME = ".retired"
_RETIRED_MARKER = "retired.json"


def retired_dir(base: Path | None = None) -> Path:
    return (base or canaries_dir()) / _RETIRED_DIRNAME


def _rewrite_frontmatter(path: Path, updates: dict) -> bool:
    """Update CANARY.md frontmatter keys in place, preserving the body and
    any keys this code doesn't know about (hand-authored extras survive).

    Validated round-trip like materialize_canary: the new text must reparse
    through the real parser before it replaces the original. False on any
    failure — a rewrite must never leave a broken file behind.
    """
    try:
        fm, body = parse_frontmatter_md(path, error_cls=CanaryParseError)
        fm.update(updates)
        text = f"---\n{yaml.safe_dump(fm, sort_keys=False, allow_unicode=True)}---\n\n{body.strip()}\n"
        tmp = Path(tempfile.mkdtemp(prefix="canary-maint-")) / path.parent.name / "CANARY.md"
        tmp.parent.mkdir(parents=True)
        try:
            tmp.write_text(text, encoding="utf-8")
            parse_canary_md(tmp)  # raises on any invariant break
            path.write_text(text, encoding="utf-8")
        finally:
            shutil.rmtree(tmp.parent.parent, ignore_errors=True)
        return True
    except Exception as e:
        logger.warning("Canary frontmatter rewrite failed for %s: %s", path, e)
        return False


def purge_quarantine(base: Path | None = None) -> list[str]:
    """Delete quarantined canaries past canary_purge_after_days.

    The `.retired/` quarantine is fed by the DELETE API and by a human
    moving a directory in by hand. This pass drains it after its grace
    window; an unreadable marker is left for a human.
    """
    purged: list[str] = []
    root = retired_dir(base)
    if not root.is_dir():
        return purged
    cutoff_days = max(1, settings.canary_purge_after_days)
    now = datetime.now(timezone.utc)
    for d in sorted(root.iterdir()):
        marker = d / _RETIRED_MARKER
        if not d.is_dir() or not marker.is_file():
            continue
        try:
            retired_at = datetime.fromisoformat(json.loads(marker.read_text())["retired_at"])
            if retired_at.tzinfo is None:
                retired_at = retired_at.replace(tzinfo=timezone.utc)
        except Exception:
            continue  # unreadable marker: leave it for a human
        if (now - retired_at).days >= cutoff_days:
            shutil.rmtree(d, ignore_errors=True)
            purged.append(d.name)
    if purged:
        logger.info("Canary quarantine purge: %s", ", ".join(purged))
    return purged


def retire_canary(c: CanaryDef, base: Path, reason: str, by: str) -> bool:
    """Move a canary into the `.retired/` quarantine with a dated marker.

    Not deletion: the quarantine keeps the directory for
    canary_purge_after_days (purge_quarantine drains it), so a retirement is
    reversible by moving the directory back for the whole grace window.
    """
    if c.path is None:
        return False
    src = c.path.parent
    dest = retired_dir(base) / c.name
    try:
        if dest.exists():
            dest = retired_dir(base) / f"{c.name}-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}"
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dest))
        (dest / _RETIRED_MARKER).write_text(
            json.dumps({"retired_at": datetime.now(timezone.utc).isoformat(), "reason": reason, "by": by})
        )
        return True
    except Exception as e:
        logger.warning("Canary retirement failed for '%s': %s", c.name, e)
        return False
