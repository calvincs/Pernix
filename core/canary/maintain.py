"""Pernix — Canary suite auto-maintenance (graduated autonomy).

One idle-time sweep over the suite, all mechanical (no LLM):

  promote — a vetting canary (auto-admitted, flaky:true) with
            canary_vetting_runs consistent passing runs becomes a full
            tripwire-capable canary; consistently MIXED outcomes settle it
            as established-flaky instead (informs forever, never trips).
  flaky   — an established canary flapping across recent runs (>= _MIN_FLIPS
            outcome changes in the last _FLAP_WINDOW) is tagged flaky.
  retire  — a canary green for canary_retire_after_passes consecutive runs
            has stopped carrying information; it moves to .retired/
            quarantine (never straight to deletion) with a dated marker.
  purge   — quarantined canaries older than canary_purge_after_days are
            deleted for good.
  review  — a healthy canary's last_reviewed is bumped (semantics under
            auto-maintenance: "last verified healthy by this sweep"), which
            keeps the retention staleness nudge quiet without a human.

HARD INVARIANT (the Goodhart lock): a canary whose LATEST run failed is
untouchable by every mutation above. A failing canary is doing its job —
silencing the alarm is the one move an autonomous suite manager must never
make. Only a pass streak (or a human editing the file) moves it. At most one
mutation per canary per sweep.
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
from db import models as db

logger = logging.getLogger("pernix.canary")

_RETIRED_DIRNAME = ".retired"
_RETIRED_MARKER = "retired.json"
_FLAP_WINDOW = 8
_MIN_FLIPS = 3
_REVIEW_BUMP_DAYS = 60
_RUN_SCAN_LIMIT = 200


def retired_dir(base: Path | None = None) -> Path:
    return (base or canaries_dir()) / _RETIRED_DIRNAME


def _rewrite_frontmatter(path: Path, updates: dict) -> bool:
    """Update CANARY.md frontmatter keys in place, preserving the body and
    any keys this code doesn't know about (hand-authored extras survive).

    Validated round-trip like materialize_canary: the new text must reparse
    through the real parser before it replaces the original. False on any
    failure — a maintenance sweep must never leave a broken file behind.
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


def _flips(runs: list[dict]) -> int:
    """Outcome direction changes across a newest-first run window."""
    outcomes = [bool(r.get("passed")) for r in runs]
    return sum(1 for a, b in zip(outcomes, outcomes[1:]) if a != b)


def _reviewed_age_days(c: CanaryDef) -> int | None:
    if not c.last_reviewed:
        return None
    try:
        reviewed = datetime.fromisoformat(str(c.last_reviewed))
    except ValueError:
        return None
    if reviewed.tzinfo is None:
        reviewed = reviewed.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - reviewed).days


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _retire(c: CanaryDef, reason: str, base: Path) -> bool:
    """Quarantine, never delete: move the directory under .retired/ with a
    dated marker so purge (and a human un-retiring) has the full story."""
    src = c.path.parent if c.path else None
    if src is None or not src.is_dir():
        return False
    dest_root = retired_dir(base)
    dest = dest_root / c.name
    if dest.exists():
        logger.warning("Retire skipped: %s already exists in quarantine", c.name)
        return False
    try:
        dest_root.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dest))
        (dest / _RETIRED_MARKER).write_text(
            json.dumps({"retired_at": datetime.now(timezone.utc).isoformat(), "reason": reason}),
            encoding="utf-8",
        )
        return True
    except Exception as e:
        logger.warning("Canary retire failed for %s: %s", c.name, e)
        return False


def _purge_quarantine(base: Path) -> list[str]:
    """Delete quarantined canaries past canary_purge_after_days."""
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
    return purged


def _maintain_one(c: CanaryDef, base: Path, stats: dict) -> None:
    """Apply at most one mutation to one canary. The I-fail lock is the
    first check: nothing below it can ever touch a failing canary."""
    runs = db.list_canary_runs(task=c.name, limit=_RUN_SCAN_LIMIT)
    if not runs:
        return  # freshly admitted, vetting run still queued — nothing to say
    if not runs[0].get("passed"):
        return  # GOODHART LOCK: latest run failed → untouchable

    md = c.path
    if md is None:
        return

    # Promotion / settling for vetting canaries.
    if "vetting" in c.tags:
        if len(runs) < max(1, settings.canary_vetting_runs):
            return
        window = runs[: max(1, settings.canary_vetting_runs)]
        new_tags = [t for t in c.tags if t != "vetting"]
        if all(r.get("passed") for r in window):
            if _rewrite_frontmatter(md, {"flaky": False, "tags": new_tags, "last_reviewed": _today()}):
                stats["promoted"].append(c.name)
        else:
            # Mixed outcomes across the vetting window: settle as
            # established-flaky — it keeps informing, never trips.
            if _rewrite_frontmatter(md, {"flaky": True, "tags": new_tags, "last_reviewed": _today()}):
                stats["settled_flaky"].append(c.name)
        return

    # Flap detection for established canaries.
    if not c.flaky and len(runs) >= _FLAP_WINDOW and _flips(runs[:_FLAP_WINDOW]) >= _MIN_FLIPS:
        if _rewrite_frontmatter(md, {"flaky": True, "last_reviewed": _today()}):
            stats["flaky_tagged"].append(c.name)
        return

    # Retirement: a long-green canary carries no information anymore.
    retire_after = max(1, settings.canary_retire_after_passes)
    if len(runs) >= retire_after and all(r.get("passed") for r in runs[:retire_after]):
        if _retire(c, f"passed {retire_after} consecutive runs", base):
            stats["retired"].append(c.name)
        return

    # Healthy and quiet: keep the review clock current so the staleness
    # nudge only ever fires for canaries this sweep cannot vouch for.
    age = _reviewed_age_days(c)
    if age is not None and age >= _REVIEW_BUMP_DAYS:
        if _rewrite_frontmatter(md, {"last_reviewed": _today()}):
            stats["reviewed"].append(c.name)


def run_maintenance(is_cancelled=lambda: False, base: Path | None = None) -> dict:
    """One full maintenance sweep. Mechanical, bounded, never raises."""
    stats: dict = {
        "promoted": [],
        "settled_flaky": [],
        "flaky_tagged": [],
        "retired": [],
        "purged": [],
        "reviewed": [],
    }
    if not (settings.canary_enabled and settings.canary_auto_maintain):
        return stats
    base = base or canaries_dir()

    from core.canary.parser import scan_canaries

    for c in scan_canaries(base):
        if is_cancelled():
            return stats
        try:
            _maintain_one(c, base, stats)
        except Exception as e:
            logger.warning("Canary maintenance failed for '%s': %s", c.name, e)

    if not is_cancelled():
        stats["purged"] = _purge_quarantine(base)

    changed = {k: v for k, v in stats.items() if v}
    if changed:
        summary = "; ".join(f"{k}: {', '.join(v)}" for k, v in changed.items())
        logger.info("Canary maintenance: %s", summary)
        try:
            db.add_notification(
                title="Canary suite auto-maintenance",
                body=f"{summary}. Retired canaries sit in {_RETIRED_DIRNAME}/ for "
                f"{settings.canary_purge_after_days}d before purge.",
                urgency="normal",
            )
        except Exception:
            pass
    return stats
