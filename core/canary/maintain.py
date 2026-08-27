"""Pernix — Canary suite auto-maintenance (graduated autonomy).

One idle-time sweep over the suite, all mechanical (no LLM):

  promote — a vetting canary (auto-admitted, flaky:true) with
            canary_vetting_runs consistent passing runs becomes a full
            tripwire-capable canary; consistently MIXED outcomes settle it
            as established-flaky instead (informs forever, never trips).
  flaky   — an established canary flapping across recent runs (>= _MIN_FLIPS
            outcome changes in the last _FLAP_WINDOW) is tagged flaky.
  demote  — a canary green for canary_retire_after_passes consecutive runs
            gets its scheduled cadence doubled (run every Nth sweep) instead
            of being retired. It stays in the scheduled pool because the
            adaptive tripwire's baseline is computed from scheduled runs of
            these same tasks; removing the stable ones shrinks the
            denominator of the only signal allowed to auto-rollback.
  purge   — quarantined canaries older than canary_purge_after_days are
            deleted for good. Nothing in this sweep quarantines any more;
            the pass drains what earlier versions (and humans) left behind.
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
from core.canary.parser import MAX_CADENCE, CanaryDef, CanaryParseError, canaries_dir, parse_canary_md
from core.skills.parser import parse_frontmatter_md
from db import models as db

logger = logging.getLogger("pernix.canary")

_RETIRED_DIRNAME = ".retired"
_RETIRED_MARKER = "retired.json"
_FLAP_WINDOW = 8
_MIN_FLIPS = 3
_REVIEW_BUMP_DAYS = 60
_RUN_SCAN_LIMIT = 200
# Trailing scheduled runs the health check reads. Three consecutive failing
# nightly sweeps with no pass between them is not noise.
_HEALTH_WINDOW = 3
_HEALTH_STATE_KEY = "canary_health_alert"


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


def _describe(item) -> str:
    """Render one stats entry — demotions carry their new cadence."""
    if isinstance(item, dict):
        return f"{item.get('name')} (cadence {item.get('cadence')})"
    return str(item)


def _purge_quarantine(base: Path) -> list[str]:
    """Delete quarantined canaries past canary_purge_after_days.

    Auto-maintenance no longer quarantines anything (long-green canaries are
    demoted to a reduced cadence instead), so this drains what earlier
    versions of the sweep — and any human moving a directory into
    `.retired/` — left behind.
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

    # Demotion: a long-green canary is cheap to keep and expensive to lose.
    #
    # It used to be retired here, on the reasoning that a canary green for 25
    # consecutive runs carries no information. That is true of a test suite
    # under active development and false of a regression tripwire, whose
    # entire value is that green stays green: the adaptive tripwire computes
    # its baseline from SCHEDULED runs of these same tasks
    # (core/adaptive/tripwire.py), so retiring the stable ones shrinks the
    # denominator of the only signal allowed to auto-rollback. Demoting to a
    # reduced cadence keeps the canary in the scheduled pool — still
    # producing baseline rows, still run in full by every post-batch sweep —
    # at a fraction of the cost.
    retire_after = max(1, settings.canary_retire_after_passes)
    if len(runs) >= retire_after and all(r.get("passed") for r in runs[:retire_after]):
        target = min(MAX_CADENCE, max(2, c.cadence * 2))
        if target > c.cadence and _rewrite_frontmatter(md, {"cadence": target, "last_reviewed": _today()}):
            stats["demoted"].append({"name": c.name, "cadence": target})
        return

    # Healthy and quiet: keep the review clock current so the staleness
    # nudge only ever fires for canaries this sweep cannot vouch for.
    age = _reviewed_age_days(c)
    if age is not None and age >= _REVIEW_BUMP_DAYS:
        if _rewrite_frontmatter(md, {"last_reviewed": _today()}):
            stats["reviewed"].append(c.name)


def _scheduled_runs(name: str, window: int) -> list[dict]:
    """The trailing scheduled runs for one canary, newest first."""
    rows = [r for r in db.list_canary_runs(task=name, limit=_RUN_SCAN_LIMIT) if r.get("trigger") == "scheduled"]
    return rows[:window]


def _is_noop_run(row: dict) -> bool:
    """True when the gates scored a workspace the agent never touched.

    Since v30 the runner records this directly as outcome='noop'. Rows from
    before the column existed (outcome NULL) fall back to the original
    heuristic: a real canary turn costs tokens and takes seconds, so zero
    tokens and a sub-second finish mean the agent never executed — the gates
    ran against the seeded fixtures and every one of them 'failed'. That is
    a harness break, worth separating from an honest failure because the
    remedy is completely different.
    """
    if row.get("outcome"):
        return row["outcome"] == "noop"
    return not row.get("passed") and int(row.get("tokens") or 0) == 0 and float(row.get("duration_s") or 0.0) < 1.0


def check_suite_health(canaries: list[CanaryDef]) -> dict:
    """Absolute health of the measurement substrate itself.

    Every other signal in this subsystem is RELATIVE — the adaptive tripwire
    asks whether a batch made the pass rate worse than baseline. None of them
    can see a suite that is uniformly broken, because a broken suite moves
    the baseline down with it: with baseline 0%, `base - now` can never reach
    the regression delta and the tripwire is silently disarmed.

    So this asks the absolute question instead. It never mutates a canary —
    it only reports, because a failing canary is doing its job.
    """
    chronic: list[str] = []
    noop: list[str] = []
    judged = 0
    for c in (c for c in canaries if not c.flaky):
        runs = _scheduled_runs(c.name, _HEALTH_WINDOW)
        if len(runs) < _HEALTH_WINDOW:
            continue  # not enough scheduled history to judge
        judged += 1
        if any(r.get("passed") for r in runs):
            continue
        chronic.append(c.name)
        if all(_is_noop_run(r) for r in runs):
            noop.append(c.name)
    # Blackout = every canary with enough history is failing. One failing
    # canary is a signal about that task; all of them is a signal about the
    # harness, and the two want different urgencies.
    blackout = judged > 1 and len(chronic) == judged
    return {"chronic": chronic, "noop": noop, "blackout": blackout}


def _report_suite_health(health: dict) -> None:
    """Notify on suite health, deduped so it nags once per day, not per sweep."""
    chronic = health.get("chronic") or []
    if not chronic:
        db.set_snooze_state(_HEALTH_STATE_KEY, "")
        return
    signature = f"{_today()}|{','.join(sorted(chronic))}"
    if db.get_snooze_state(_HEALTH_STATE_KEY) == signature:
        return

    noop = health.get("noop") or []
    if noop:
        title = "Canary suite: the agent is not running"
        body = (
            f"{len(noop)} canary task(s) scored without executing the agent — zero tokens, "
            f"sub-second runs, every gate failing on missing files: {', '.join(sorted(noop))}. "
            "This is a harness failure, not a quality regression; the gates are being scored "
            "against the seeded fixtures. Until it is fixed the suite measures nothing and the "
            "adaptive tripwire is disarmed."
        )
        urgency = "high"
    elif health.get("blackout"):
        title = "Canary suite: every scored canary is failing"
        body = (
            f"All {len(chronic)} non-flaky canaries failed their last {_HEALTH_WINDOW} scheduled "
            "sweeps. A uniformly failing suite drags the tripwire baseline to 0%, and a baseline "
            "of 0% can never register a regression — so the adaptive layer is applying batches "
            "with no working safety net. Investigate before trusting further auto-applies."
        )
        urgency = "high"
    else:
        title = "Canary suite: chronically failing task(s)"
        body = (
            f"{', '.join(sorted(chronic))} failed the last {_HEALTH_WINDOW} scheduled sweeps with "
            "no pass in between. Either the agent has genuinely regressed on this task or the "
            "canary needs updating; both are worth a look, and neither will surface on its own."
        )
        urgency = "normal"
    try:
        db.add_notification(title=title, body=body, urgency=urgency)
        db.set_snooze_state(_HEALTH_STATE_KEY, signature)
    except Exception as e:
        logger.warning("Canary health notification failed: %s", e)


def run_maintenance(is_cancelled=lambda: False, base: Path | None = None) -> dict:
    """One full maintenance sweep. Mechanical, bounded, never raises."""
    stats: dict = {
        "promoted": [],
        "settled_flaky": [],
        "flaky_tagged": [],
        "demoted": [],
        "purged": [],
        "reviewed": [],
        "unhealthy": [],
    }
    if not (settings.canary_enabled and settings.canary_auto_maintain):
        return stats
    base = base or canaries_dir()

    from core.canary.parser import scan_canaries

    suite = list(scan_canaries(base))
    for c in suite:
        if is_cancelled():
            return stats
        try:
            _maintain_one(c, base, stats)
        except Exception as e:
            logger.warning("Canary maintenance failed for '%s': %s", c.name, e)

    if not is_cancelled():
        stats["purged"] = _purge_quarantine(base)

    # Health reporting runs last and mutates nothing: the sweep above is all
    # success-shaped (promote/demote/review), so without this a suite that
    # fails everything produces an entirely empty maintenance report.
    if not is_cancelled():
        try:
            health = check_suite_health(suite)
            stats["unhealthy"] = health["chronic"]
            _report_suite_health(health)
        except Exception as e:
            logger.warning("Canary health check failed: %s", e)

    changed = {k: v for k, v in stats.items() if v}
    if changed:
        summary = "; ".join(f"{k}: {', '.join(_describe(i) for i in v)}" for k, v in changed.items())
        logger.info("Canary maintenance: %s", summary)
    # 'unhealthy' is reported by _report_suite_health with its own urgency and
    # its own dedupe; folding it in here would double-notify and bury a
    # measurement outage under routine bookkeeping.
    mutations = {k: v for k, v in changed.items() if k != "unhealthy"}
    if mutations:
        try:
            db.add_notification(
                title="Canary suite auto-maintenance",
                body="; ".join(f"{k}: {', '.join(_describe(i) for i in v)}" for k, v in mutations.items())
                + ". Demoted canaries stay in the scheduled sweep at a "
                "reduced cadence so the tripwire keeps its baseline.",
                urgency="normal",
            )
        except Exception:
            pass
    return stats
